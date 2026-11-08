"""Explicit protected-client VM probe; default is muted IPC only.

No probe runs on import. Synthetic PCM is an explicit separate opt-in and opens
only the capture endpoint proved by the pinned installation/PnP inventory. Audio
is reduced to scalar metrics in memory; no audio file or endpoint ID is reported.
The entry point MUST exit the dedicated probe process with code 70 whenever
cleanup_confirmed is False. Never reuse the process after unknown native cleanup.
"""
from __future__ import annotations

import math
import struct
import threading
import time

from .broker_client_bootstrap import open_installed_broker_client
from .broker_pipe_client import BrokerObservation
from .windows_broker_manifest import PinnedInstalledManifest
from .windows_driver_endpoints import DriverEndpointPair, WindowsDriverEndpointInventory

_SEND_FRAMES = 50
_RATE, _SAMPLES = 48000, 480
_SILENCE_FRAMES = 9600
_RETAINED: list[object] = []


class _ProbeFailure(Exception):
    def __init__(self, code):
        self.code = code


def _capture_source(identifier):
    import soundcard
    return soundcard.get_microphone(identifier, include_loopback=False)


class _Capture:
    def __init__(self, identifier):
        self.identifier = identifier
        self.ready, self.done, self.stop_requested = threading.Event(), threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, name="rv-lab-virtual-capture", daemon=True)
        self.cleanup_confirmed = True
        self.fatal = False
        self.error = None
        self.manager = self.source = None
        self.frames = self.signal_frames = 0
        self.squares = self.peak = self.sine = self.cosine = 0.0
        self.silence_frames = 0
        self.silence_squares = self.silence_peak = 0.0
        self.silence_done = threading.Event()
        self._silence_after = None
        self._phase_lock = threading.Lock()

    def start(self):
        self.thread.start()

    def _run(self):
        entered = attempted = False
        original = None
        try:
            import numpy as np
            self.source = _capture_source(self.identifier)
            if (self.source is None or type(self.source.id) is not str or self.source.id != self.identifier
                    or getattr(self.source, "isloopback", True) is not False):
                raise _ProbeFailure("virtual_capture_identity_mismatch")
            attempted, self.cleanup_confirmed = True, False
            self.manager = self.source.recorder(samplerate=_RATE, channels=1, blocksize=4800)
            recorder = self.manager.__enter__()
            entered = True
            self.ready.set()
            deadline = time.monotonic() + 3.0
            total = 0
            while not self.stop_requested.is_set() and total < 4 * _RATE and time.monotonic() < deadline:
                with self._phase_lock:
                    silence_after = self._silence_after
                read_started = time.monotonic()
                samples = np.asarray(recorder.record(numframes=_SAMPLES), dtype=np.float64)
                if samples.shape not in ((_SAMPLES,), (_SAMPLES, 1)) or not np.isfinite(samples).all():
                    raise _ProbeFailure("capture_data_invalid")
                samples = samples.reshape(_SAMPLES)
                total += _SAMPLES
                if silence_after is not None:
                    # Discard the drain window and any read begun before it.
                    # A muted status alone is not evidence of silent PCM.
                    if read_started < silence_after:
                        continue
                    self.silence_squares += float(np.dot(samples, samples))
                    self.silence_peak = max(self.silence_peak, float(np.max(np.abs(samples))))
                    self.silence_frames += _SAMPLES
                    if self.silence_frames >= _SILENCE_FRAMES:
                        self.silence_done.set()
                        break
                    continue
                phase = (2 * math.pi * 900 / _RATE) * np.arange(self.frames, self.frames + _SAMPLES)
                self.squares += float(np.dot(samples, samples))
                self.peak = max(self.peak, float(np.max(np.abs(samples))))
                self.signal_frames += int(np.count_nonzero(np.abs(samples) > 0.015))
                self.sine += float(np.dot(samples, np.sin(phase)))
                self.cosine += float(np.dot(samples, np.cos(phase)))
                self.frames += _SAMPLES
            if not self.stop_requested.is_set() and not self.silence_done.is_set():
                raise _ProbeFailure("capture_window_incomplete")
        except BaseException as error:
            original = error
            self.error = error.code if isinstance(error, _ProbeFailure) else "virtual_capture_failed"
            if not isinstance(error, Exception):
                self.fatal = True
                self.error = "fatal_capture_error"
        finally:
            if entered:
                try:
                    self.manager.__exit__(type(original) if original else None, original,
                                          original.__traceback__ if original else None)
                    self.cleanup_confirmed = True
                except BaseException:
                    self.cleanup_confirmed = False
                    self.error = "capture_cleanup_unconfirmed"
            elif not attempted:
                self.cleanup_confirmed = True
            self.done.set()

    def stop(self):
        self.stop_requested.set()
        if self.thread.ident is not None:
            self.thread.join(0.5)
        return not self.thread.is_alive() and self.cleanup_confirmed and not self.fatal

    def begin_silence(self):
        with self._phase_lock:
            self._silence_after = time.monotonic() + 0.2

    def metrics(self):
        rms = math.sqrt(self.squares / self.frames) if self.frames else 0.0
        fraction = 2 * (self.sine**2 + self.cosine**2) / (self.frames * self.squares) if self.squares else 0.0
        silence_rms = math.sqrt(self.silence_squares / self.silence_frames) if self.silence_frames else 0.0
        return {"captured_frames": self.frames, "rms": round(rms, 6),
                "peak": round(self.peak, 6), "tone_fraction": round(fraction, 6),
                "silence_frames": self.silence_frames, "silence_rms": round(silence_rms, 6),
                "silence_peak": round(self.silence_peak, 6)}


def _observe(client, report):
    status = client.observe()
    report["observations"] += 1
    if (type(status) is not BrokerObservation
            or any(type(value) is not bool for value in (status.available, status.mic_requested,
                                                        status.writer_ready, status.cleanup_pending))
            or type(status.observed_at) not in (float, int) or not math.isfinite(status.observed_at)
            or not 0 <= time.monotonic() - status.observed_at < 0.5):
        raise _ProbeFailure("broker_status_invalid")
    return status


def _heartbeat(client, report):
    if client.heartbeat() is not True:
        raise _ProbeFailure("broker_heartbeat_rejected")
    report["heartbeats"] += 1


def _wait_state(client, report, enabled):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        _heartbeat(client, report)
        status = _observe(client, report)
        if (status.available and not status.cleanup_pending and status.mic_requested is enabled
                and status.writer_ready is enabled):
            return
        time.sleep(0.02)
    raise _ProbeFailure("microphone_state_timeout")


def run_probe(*, synthetic_pcm: bool = False) -> dict:
    report = dict(success=False, cleanup_confirmed=True, error=None, synthetic_pcm=synthetic_pcm,
                  observations=0, heartbeats=0, sent_frames=0, captured_frames=0,
                  rms=0.0, peak=0.0, tone_fraction=0.0, silence_frames=0,
                  silence_rms=0.0, silence_peak=0.0, silence_verified=False)
    if type(synthetic_pcm) is not bool:
        report.update(error="invalid_probe_argument", synthetic_pcm=False)
        return report
    if _RETAINED:
        report.update(error="prior_cleanup_unconfirmed", cleanup_confirmed=False)
        return report
    client = manifest = inventory = capture = None
    phase, unknown, fatal = "broker_open_failed", False, False
    try:
        if synthetic_pcm:
            phase = "manifest_load_failed"
            manifest = PinnedInstalledManifest("client")
            installation = manifest.load()
            if manifest.is_verified is not True:
                raise _ProbeFailure("manifest_unverified")
            phase = "endpoint_inventory_failed"
            inventory = WindowsDriverEndpointInventory(installation.driver_instance_id)
            pair = inventory.observe()
            if type(pair) is not DriverEndpointPair or inventory.cleanup_confirmed is not True:
                raise _ProbeFailure("endpoint_inventory_unconfirmed")
        phase = "broker_open_failed"
        client = open_installed_broker_client(timeout=0.2)
        for _ in range(3):
            status = _observe(client, report)
            if (not status.available or status.mic_requested or status.writer_ready or status.cleanup_pending):
                raise _ProbeFailure("broker_not_muted_ready")
            _heartbeat(client, report)
        if synthetic_pcm:
            phase = "microphone_enable_failed"
            if client.set_microphone_enabled(True) is not True:
                raise _ProbeFailure(phase)
            _wait_state(client, report, True)  # An enable ACK alone is not readiness.
            phase = "virtual_capture_failed"
            capture = _Capture(pair.microphone_endpoint_id)
            capture.start()
            deadline = time.monotonic() + 0.5
            while not capture.ready.is_set():
                if capture.done.is_set():
                    raise _ProbeFailure(capture.error or "virtual_capture_failed")
                if time.monotonic() >= deadline:
                    raise _ProbeFailure("virtual_capture_start_timeout")
                _heartbeat(client, report)
                time.sleep(0.02)
            phase = "synthetic_submit_failed"
            started = time.monotonic()
            for packet in range(_SEND_FRAMES):
                if capture.done.is_set() and capture.error:
                    raise _ProbeFailure(capture.error)
                payload = struct.pack("<480f", *(0.18 * math.sin(2 * math.pi * 900 *
                    (packet * _SAMPLES + sample) / _RATE) for sample in range(_SAMPLES)))
                if client.submit(payload) is not True:
                    raise _ProbeFailure(phase)
                report["sent_frames"] += 1
                if packet % 5 == 0:
                    _heartbeat(client, report)
                time.sleep(max(0, started + (packet + 1) * 0.01 - time.monotonic()))
            phase = "microphone_disable_failed"
            if client.set_microphone_enabled(False) is not True:
                raise _ProbeFailure("microphone_disable_failed")
            _wait_state(client, report, False)
            capture.begin_silence()
            deadline = time.monotonic() + 0.9
            while not capture.silence_done.is_set():
                if capture.done.is_set():
                    raise _ProbeFailure(capture.error or "silence_capture_incomplete")
                if time.monotonic() >= deadline:
                    raise _ProbeFailure("silence_capture_timeout")
                _heartbeat(client, report)
                time.sleep(0.02)
            if not capture.stop():
                raise _ProbeFailure("capture_cleanup_unconfirmed")
            report.update(capture.metrics())
            if capture.error:
                raise _ProbeFailure(capture.error)
            if (capture.frames < 12000 or capture.signal_frames < 12000 or
                    not 0.02 <= report["rms"] <= 0.30 or not 0.02 <= report["peak"] <= 0.35
                    or report["tone_fraction"] < 0.35):
                raise _ProbeFailure("synthetic_signal_rejected")
            if (report["silence_frames"] < _SILENCE_FRAMES or
                    report["silence_rms"] > 0.001 or report["silence_peak"] > 0.005):
                raise _ProbeFailure("post_disable_not_silent")
            report["silence_verified"] = True
        report["success"] = True
    except BaseException as error:
        report["error"] = error.code if isinstance(error, _ProbeFailure) else phase
        partial = getattr(error, "cleanup_owner", None)
        if client is None and phase == "broker_open_failed":
            client, unknown = partial, partial is None
        elif manifest is None and phase == "manifest_load_failed":
            manifest, unknown = partial, partial is None
        fatal = not isinstance(error, Exception)
        if fatal:
            report["error"] = "fatal_probe_error"
    finally:
        clean = not unknown and not fatal
        if capture is not None:
            try:
                clean = capture.stop() and clean
            except BaseException:
                clean = False
        if client is not None and not fatal:
            try:
                client.close()
                clean = client.cleanup_confirmed is True and clean
            except BaseException:
                clean = False
        if inventory is not None and not fatal:
            try:
                clean = inventory.cleanup_confirmed is True and clean
            except BaseException:
                clean = False
        if manifest is not None and clean:
            try:
                manifest.close()
                clean = manifest.cleanup_confirmed is True
            except BaseException:
                clean = False
        if not clean:
            _RETAINED.append((client, manifest, inventory, capture))
            report.update(success=False, cleanup_confirmed=False)
            report["error"] = report["error"] or "cleanup_unconfirmed"
    return report
