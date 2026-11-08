"""Source-only own-driver bridge; no UI selection, route changes or deployment.

The playback_device_id argument identifies the verified virtual microphone's
recording endpoint in this subclass. Phone PCM goes only through BrokerMicSink;
no SoundCard speaker/player is opened. start/stop and microphone control belong
off the event loop; the existing capture worker remains shared with AudioBridge.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from .audio import AudioBridge, AudioStopError, StatusCallback, is_virtual_audio_device, sc
from .audio_backend import DriverAudioBackend
from .broker_mic_sink import BrokerMicSink
from .protocol import FRAME_SAMPLES, MIC_FRAME, SAMPLE_RATE, ProtocolError, validate_audio_payload


class DriverAudioBridge(AudioBridge):
    _CONTROL_TIMEOUT_SECONDS = 1.0

    def __init__(self, capture_device_id: str, playback_device_id: str,
                 loop: asyncio.AbstractEventLoop, websocket: Any, send_lock: asyncio.Lock,
                 on_status: StatusCallback, *, driver_backend: DriverAudioBackend,
                 sink_factory: Callable[[], BrokerMicSink]) -> None:
        if not isinstance(driver_backend, DriverAudioBackend) or not callable(sink_factory):
            raise TypeError("A trusted driver backend and inert broker sink factory are required")
        super().__init__(capture_device_id, playback_device_id, loop, websocket, send_lock, on_status)
        self._driver_backend, self._sink_factory = driver_backend, sink_factory
        self._sink: BrokerMicSink | None = None
        self._control_lock = threading.Lock()
        self._control_generation = 0
        self._transition_deadline = 0.0

    def _trusted_endpoints(self, capture_id: str | None = None) -> bool:
        try:
            return (self._driver_backend.is_available() is True and
                    self._driver_backend.matches_endpoints(
                        self.capture_device_id if capture_id is None else capture_id,
                        self.playback_device_id) is True)
        except Exception:
            return False

    def _validate_audio_devices(self) -> None:
        if self._stop.is_set() or not self._trusted_endpoints():
            raise RuntimeError("Подтверждённые аудиоустройства драйвера недоступны")
        capture = sc.get_microphone(self.capture_device_id, include_loopback=True)
        if (capture is None or not self._trusted_endpoints(str(capture.id))
                or not is_virtual_audio_device(str(capture.name))):
            raise RuntimeError("Устройство захвата драйвера не подтверждено")
        with self._control_lock:
            if self._stop.is_set() or self._sink is not None:
                raise RuntimeError("Запуск аудио отменён")
            self._sink = self._sink_factory()  # Inert; retained before any worker starts.
            sink = self._sink
        try:
            sink.start()
            if self._stop.is_set() or sink.set_enabled(False, timeout=self._CONTROL_TIMEOUT_SECONDS) is not True:
                raise RuntimeError("Muted broker startup failed")
            if self._stop.is_set() or not sink.is_available() or not self._trusted_endpoints():
                raise RuntimeError("Driver startup observation failed")
        except Exception:
            self._stop.set()
            try:
                sink.stop(timeout=self._CONTROL_TIMEOUT_SECONDS)
                if sink.snapshot.cleanup_confirmed is not True:
                    raise AudioStopError("Broker cleanup remains unconfirmed")
            except Exception:
                raise AudioStopError("Не удалось подтвердить остановку брокера") from None
            raise RuntimeError("Аудиокомпонент драйвера не готов") from None

    def _capture_system_audio(self) -> None:
        # Recheck trusted identity immediately before the shared exact-ID opener.
        if not self._trusted_endpoints():
            if not self._stop.is_set():
                self._report_failure("Устройство захвата драйвера недоступно")
            return
        super()._capture_system_audio()

    def set_microphone_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("Ожидается явное состояние микрофона")
        with self._control_lock:
            self._mic_enabled.clear()
            self._clear_phone_frames()
            self._control_generation += 1
            generation = self._control_generation
            sink = self._sink
            if self._stop.is_set() or sink is None:
                raise RuntimeError("Компонент микрофона недоступен")
            self._transition_deadline = time.monotonic() + self._CONTROL_TIMEOUT_SECONDS
        try:
            accepted = sink.set_enabled(enabled, timeout=self._CONTROL_TIMEOUT_SECONDS) is True
            ready = accepted and sink.is_available()
        except Exception:
            ready = False
        with self._control_lock:
            if generation != self._control_generation:
                raise RuntimeError("Переключение микрофона отменено")
            self._transition_deadline = 0.0
            if self._stop.is_set() or not ready:
                self._mic_enabled.clear()
                self._clear_phone_frames()
                raise RuntimeError("Компонент микрофона не подтвердил готовность")
            if enabled:
                self._mic_enabled.set()

    def _sink_healthy(self, sink: BrokerMicSink) -> bool:
        try:
            healthy = sink.is_available() is True and self._driver_backend.is_available() is True
        except Exception:
            healthy = False
        if healthy:
            return True
        with self._control_lock:
            return time.monotonic() < self._transition_deadline

    def _play_phone_audio(self) -> None:
        sink = self._sink
        try:
            if sink is None:
                raise RuntimeError("Missing broker sink")
            while not self._stop.is_set():
                if not self._sink_healthy(sink):
                    raise RuntimeError("Broker telemetry unavailable")
                try:
                    payload = self._phone_frames.get(timeout=FRAME_SAMPLES / SAMPLE_RATE)
                except queue.Empty:
                    payload = b""
                if self._stop.is_set():
                    break
                if payload and self._mic_enabled.is_set():
                    try:
                        validate_audio_payload(MIC_FRAME, payload)
                    except ProtocolError:
                        continue
                    if not sink.submit(payload) and not self._sink_healthy(sink):
                        raise RuntimeError("Broker microphone ingress unavailable")
                self._mark_progress("playback")
        except Exception:
            if not self._stop.is_set():
                self._mic_enabled.clear()
                self._clear_phone_frames()
                self._report_failure("Подача микрофона через драйвер недоступна")
                if sink is not None:
                    try:
                        sink.stop(timeout=0)
                    except Exception:
                        pass  # Retain the sink; stop() must confirm cleanup later.

    def stop(self) -> None:
        self._stop.set()
        with self._control_lock:
            self._control_generation += 1
            self._transition_deadline = 0.0
            self._mic_enabled.clear()
            self._clear_phone_frames()
            sink = self._sink
        failed = False
        if sink is not None:
            try:
                sink.stop(timeout=self._STOP_JOIN_TIMEOUT_SECONDS)
                if sink.snapshot.cleanup_confirmed is not True:
                    failed = True
            except Exception:
                failed = True
        try:
            super().stop()
        except Exception:
            failed = True
        if failed:
            raise AudioStopError("Не удалось подтвердить остановку аудиокомпонента драйвера") from None
