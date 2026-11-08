"""Windows virtual-device audio bridge using WASAPI through SoundCard."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import soundcard as sc
from .audio_compression import encode_system_frame
from .audio_session_diagnostics import log_worker_failure

from .windows_audio_capture import guard_capture_silence
from .audio_timing import SystemAudioTiming
from .windows_audio_playback import guard_playback_buffer

from .protocol import (
    FRAME_SAMPLES,
    MIC_CHANNELS,
    MIC_FRAME,
    ProtocolError,
    SAMPLE_RATE,
    SYSTEM_CHANNELS,
    validate_audio_payload,
)

StatusCallback = Callable[[str], None]
LOGGER = logging.getLogger("remotevoice.audio")
# Native WASAPI capacity is independent of the 10ms read/wire packet. A single
# packet-sized capture buffer overruns while the consumer briefly waits for
# the event loop (for example during HD encoding), permanently losing PCM.
SYSTEM_CAPTURE_BUFFER_FRAMES = FRAME_SAMPLES * 10  # 100ms capacity, 10ms reads.
# Android permits ten 10ms microphone packets in flight (19210 wire bytes).
# Retain one permitted burst; larger bursts still discard the oldest PCM.
PHONE_QUEUE_FRAMES = 10
VIRTUAL_DEVICE_MARKERS = (
    "cable",
    "virtual",
    "voicemeeter",
    "vb-audio",
    "vb audio",
    "remotevoice",
)


class AudioStopError(RuntimeError):
    """Audio workers are still alive after a bounded shutdown attempt."""


class _AudioDiagnostics:
    """Opt-in scalar counters only; never retain frames or endpoint identifiers."""

    _INTERVAL_SECONDS = 3.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_emitted = time.monotonic()
        self._finalized = False
        self._reset()

    def _reset(self) -> None:
        self._system_frames = 0
        self._system_peak = 0.0
        self._mic_frames_rx = 0
        self._rx_peak = 0.0
        self._played_nonempty = 0
        self._play_peak = 0.0
        self._zero_fill = 0

    @staticmethod
    def _peak(samples: np.ndarray) -> float:
        peak = float(np.max(np.abs(samples), initial=0.0))
        return peak if math.isfinite(peak) else 0.0

    def record_system(self, samples: np.ndarray) -> None:
        self._record("system", self._peak(samples))

    def record_received(self, payload: bytes) -> None:
        self._record("received", self._peak(np.frombuffer(payload, dtype="<f4")))

    def record_played(self, samples: np.ndarray, *, zero_fill: bool) -> None:
        self._record("zero_fill" if zero_fill else "played", self._peak(samples))

    def _record(self, kind: str, peak: float) -> None:
        snapshot = None
        with self._lock:
            if self._finalized:
                return
            if kind == "system":
                self._system_frames += 1
                self._system_peak = max(self._system_peak, peak)
            elif kind == "received":
                self._mic_frames_rx += 1
                self._rx_peak = max(self._rx_peak, peak)
            else:
                if kind == "zero_fill":
                    self._zero_fill += 1
                else:
                    self._played_nonempty += 1
                self._play_peak = max(self._play_peak, peak)
            now = time.monotonic()
            if now - self._last_emitted >= self._INTERVAL_SECONDS:
                snapshot = self._snapshot(now)
        if snapshot is not None:
            self._emit(snapshot, final=False)

    def _snapshot(self, now: float) -> tuple[float, int, float, int, float, int, float, int]:
        snapshot = (
            now - self._last_emitted,
            self._system_frames,
            self._system_peak,
            self._mic_frames_rx,
            self._rx_peak,
            self._played_nonempty,
            self._play_peak,
            self._zero_fill,
        )
        self._reset()
        self._last_emitted = now
        return snapshot

    def flush(self) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            snapshot = self._snapshot(time.monotonic())
        self._emit(snapshot, final=True)

    @staticmethod
    def _emit(snapshot: tuple[float, int, float, int, float, int, float, int], *, final: bool) -> None:
        LOGGER.info(
            "audio_diagnostics final=%s interval_s=%.3f system_frames=%d system_peak=%.6f "
            "mic_frames_rx=%d rx_peak=%.6f played_nonempty=%d play_peak=%.6f zero_fill=%d",
            final,
            *snapshot,
        )


def is_virtual_audio_device(name: str) -> bool:
    folded = name.casefold()
    return any(marker in folded for marker in VIRTUAL_DEVICE_MARKERS)


def virtual_device_family(name: str) -> str:
    without_direction = re.sub(
        r"\b(input|output|capture|recording|playback|render)\b", " ", name.casefold()
    )
    return re.sub(r"[^a-z0-9]+", " ", without_direction).strip()


def list_audio_devices() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return playback speakers and recording microphones, including loopbacks."""
    speakers = [
        {"id": str(device.id), "name": str(device.name)}
        for device in sc.all_speakers()
        if is_virtual_audio_device(str(device.name))
    ]
    microphones = [
        {
            "id": str(device.id),
            "name": str(device.name),
            "loopback": str(bool(getattr(device, "isloopback", False))),
        }
        for device in sc.all_microphones(include_loopback=True)
        if is_virtual_audio_device(str(device.name))
        and not bool(getattr(device, "isloopback", False))
    ]
    return speakers, microphones


class AudioBridge:
    """Streams desktop capture to iPhone and iPhone mic to a virtual playback device."""

    _STOP_JOIN_TIMEOUT_SECONDS = 1.0
    _PROGRESS_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        capture_device_id: str,
        playback_device_id: str,
        loop: asyncio.AbstractEventLoop,
        websocket: Any,
        send_lock: asyncio.Lock,
        on_status: StatusCallback,
    ) -> None:
        self.capture_device_id = capture_device_id
        self.playback_device_id = playback_device_id
        self.loop = loop
        self.websocket = websocket
        self.send_lock = send_lock
        self.on_status = on_status
        self._stop = threading.Event()
        self._mic_enabled = threading.Event()
        self._phone_frames: queue.Queue[bytes] = queue.Queue(maxsize=PHONE_QUEUE_FRAMES)
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._system_compression = False
        self._progress_lock = threading.Lock()
        self._progress: dict[str, float] = {}
        trace_directory = os.environ.get('REMOTEVOICE_AUDIO_TRACE_DIR')
        tcp_observer = None
        if trace_directory:
            transport = getattr(websocket, 'transport', None)
            sock = transport.get_extra_info('socket') if transport is not None else None
            if sock is not None:
                from .windows_tcp_observer import read_tcp_statistics
                tcp_observer = lambda: read_tcp_statistics(sock)
        self._system_timing = SystemAudioTiming(trace_directory=trace_directory, tcp_observer=tcp_observer,
            trace_packet_limit=os.environ.get('REMOTEVOICE_AUDIO_TRACE_PACKETS') if trace_directory else None)
        self._diagnostics = (
            _AudioDiagnostics() if os.environ.get("REMOTEVOICE_AUDIO_DIAGNOSTICS") == "1" else None
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stop.is_set():
                raise RuntimeError("Аудиодвижок уже запускался; нужен новый экземпляр")
            self._start_workers()

    def _start_workers(self) -> None:
        self._validate_audio_devices()
        self._launch_audio_workers()

    def _validate_audio_devices(self) -> None:
        capture = sc.get_microphone(self.capture_device_id, include_loopback=True)
        playback = sc.get_speaker(self.playback_device_id)
        if capture is None or playback is None:
            raise RuntimeError("Выбранное аудиоустройство больше недоступно")
        if not is_virtual_audio_device(str(capture.name)):
            raise RuntimeError("Захват Windows разрешён только с виртуального аудиоустройства")
        if not is_virtual_audio_device(str(playback.name)):
            raise RuntimeError("Передача голоса разрешена только на виртуальное аудиоустройство")
        if virtual_device_family(str(capture.name)) == virtual_device_family(str(playback.name)):
            raise RuntimeError(
                "Для двух направлений выберите две разные виртуальные аудиопары"
            )

    def _launch_audio_workers(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("Запуск аудио отменён")
        with self._progress_lock:
            now = time.monotonic()
            self._progress = {"capture": now, "playback": now}

        threads = [
            threading.Thread(target=self._capture_system_audio, name="rv-capture", daemon=True),
            threading.Thread(target=self._play_phone_audio, name="rv-playback", daemon=True),
        ]
        self._started = True
        try:
            for thread in threads:
                thread.start()
                self._threads.append(thread)
        except BaseException:
            # Retain every successfully started worker for stop(), including a
            # partial startup where the second Thread.start() fails.
            self._stop.set()
            raise

    @property
    def diagnostic_id(self) -> str:
        return self._system_timing.session_id

    @property
    def microphone_enabled(self) -> bool:
        return self._mic_enabled.is_set()

    def set_microphone_enabled(self, enabled: bool) -> None:
        if enabled:
            self._mic_enabled.set()
        else:
            self._mic_enabled.clear()
            self._clear_phone_frames()

    def submit_phone_audio(self, payload: bytes | memoryview) -> None:
        if not self._mic_enabled.is_set() or self._stop.is_set():
            return
        if len(payload) != FRAME_SAMPLES * MIC_CHANNELS * 4:
            return
        frame = bytes(payload)
        try:
            validate_audio_payload(MIC_FRAME, frame)
        except ProtocolError:
            return
        if self._diagnostics is not None:
            self._diagnostics.record_received(frame)
        try:
            self._phone_frames.put_nowait(frame)
        except queue.Full:
            try:
                self._phone_frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._phone_frames.put_nowait(frame)
            except queue.Full:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._mic_enabled.clear()
        self._clear_phone_frames()
        with self._lifecycle_lock:
            deadline = time.monotonic() + self._STOP_JOIN_TIMEOUT_SECONDS
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
            # Native record/play calls may not respond to the stop flag. Never
            # lose their handles or report that cleanup has completed.
            self._threads[:] = [thread for thread in self._threads if thread.is_alive()]
            if self._diagnostics is not None:
                self._diagnostics.flush()
            if self._threads:
                raise AudioStopError("Не удалось подтвердить остановку аудиопотоков")
            self._system_timing.flush_trace()

    def _mark_progress(self, direction: str) -> None:
        with self._progress_lock:
            self._progress[direction] = time.monotonic()

    def assert_progress(self) -> None:
        """Detect a stalled native call even while the network still responds."""
        if self._stop.is_set():
            return
        with self._progress_lock:
            stale = any(time.monotonic() - tick >= self._PROGRESS_TIMEOUT_SECONDS
                        for tick in self._progress.values())
        if stale:
            raise RuntimeError("Аудиоустройство перестало передавать звук")

    def enable_system_compression(self) -> None:
        """Configure only the not-yet-started, explicitly negotiated session."""
        with self._lifecycle_lock:
            if self._started or self._stop.is_set():
                raise RuntimeError("Audio format must be negotiated before startup")
            self._system_compression = True

    def _capture_system_audio(self) -> None:
        try:
            source = sc.get_microphone(self.capture_device_id, include_loopback=True)
            with source.recorder(
                samplerate=SAMPLE_RATE,
                channels=SYSTEM_CHANNELS,
                blocksize=SYSTEM_CAPTURE_BUFFER_FRAMES,
            ) as recorder:
                try:
                    self._system_timing.observe_capture(recorder)
                    guard_capture_silence(recorder)
                    self.on_status("audio_active")
                    while not self._stop.is_set():
                        self._system_timing.mark('read_start')
                        samples = np.asarray(
                            recorder.record(numframes=FRAME_SAMPLES), dtype="<f4"
                        )
                        self._system_timing.mark('read_end')
                        samples = self._stereo(samples)
                        packet = encode_system_frame(samples.tobytes(), compressed=self._system_compression)
                        if self._diagnostics is not None:
                            self._diagnostics.record_system(samples)
                        future = asyncio.run_coroutine_threadsafe(
                            self._send(packet), self.loop
                        )
                        try:
                            future.result(timeout=1.0)
                            self._system_timing.finish()
                            self._mark_progress("capture")
                        except Exception as exc:
                            if not self._stop.is_set():
                                log_worker_failure(LOGGER, session_id=self.diagnostic_id, stage="system_send", error=exc)
                                self._report_failure(f"Передача звука ноутбука: {exc}")
                            return
                finally:
                    self._system_timing.close_capture_observation()
        except Exception as exc:
            if not self._stop.is_set():
                log_worker_failure(LOGGER, session_id=self.diagnostic_id, stage="system_capture", error=exc)
                self._report_failure(f"Захват аудио Windows: {exc}")

    def _play_phone_audio(self) -> None:
        try:
            output = sc.get_speaker(self.playback_device_id)
            frame_duration = FRAME_SAMPLES / SAMPLE_RATE
            next_frame_at = time.monotonic()
            with output.player(
                samplerate=SAMPLE_RATE,
                channels=SYSTEM_CHANNELS,
                blocksize=FRAME_SAMPLES,
            ) as player:
                guard_playback_buffer(player, stop_event=self._stop)
                while not self._stop.is_set():
                    try:
                        payload = self._phone_frames.get(timeout=frame_duration)
                    except queue.Empty:
                        payload = b""

                    has_phone_frame = self._mic_enabled.is_set() and bool(payload)
                    if has_phone_frame:
                        mono = np.frombuffer(payload, dtype="<f4").reshape(
                            FRAME_SAMPLES, MIC_CHANNELS
                        )
                        stereo = np.repeat(mono, SYSTEM_CHANNELS, axis=1)
                    else:
                        stereo = np.zeros(
                            (FRAME_SAMPLES, SYSTEM_CHANNELS), dtype=np.float32
                        )
                    player.play(stereo)
                    self._mark_progress("playback")
                    if self._diagnostics is not None:
                        self._diagnostics.record_played(stereo, zero_fill=not has_phone_frame)

                    next_frame_at += frame_duration
                    delay = next_frame_at - time.monotonic()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_frame_at = time.monotonic()
        except Exception as exc:
            if not self._stop.is_set():
                log_worker_failure(LOGGER, session_id=self.diagnostic_id, stage="phone_playback", error=exc)
                self._report_failure(f"Подача микрофона в Windows: {exc}")

    def _send(self, packet: bytes) -> Any:
        async def send_locked() -> None:
            self._system_timing.mark('send_enter')
            async with self.send_lock:
                # A timed-out worker or stop() may have ended the session while
                # this coroutine was waiting for a control-message send.
                if self._stop.is_set():
                    return
                self._system_timing.mark('send_lock')
                await self.websocket.send(packet)
                self._system_timing.mark('send_done')

        return send_locked()

    def _report_failure(self, message: str) -> None:
        self._stop.set()
        try:
            self.on_status("audio_error: " + message)
        except Exception:
            pass  # A failed UI observer must not prevent closing the session.
        try:
            asyncio.run_coroutine_threadsafe(
                self.websocket.close(code=1011, reason="Audio device error"), self.loop
            )
        except RuntimeError:
            pass

    def _clear_phone_frames(self) -> None:
        while True:
            try:
                self._phone_frames.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _stereo(samples: np.ndarray) -> np.ndarray:
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        elif samples.shape[1] > 2:
            samples = samples[:, :2]
        # Non-finite endpoint samples become silence. nan_to_num's default
        # maps +/-Inf to +/-float32 max, which passes protocol validation and
        # reaches the phone as a full-scale spike.
        return np.ascontiguousarray(
            np.nan_to_num(samples, copy=False, nan=0.0, posinf=0.0, neginf=0.0), dtype="<f4"
        )
