"""Direct microphone sink over the RemonteVoice driver (development path).

The protected broker path is not accepted yet; this sink lets the Host feed the
driver directly when it runs with the rights the driver requires
(SYSTEM/Administrators, see src/Main/rv_mic_access.h). It presents the same
surface DriverBackendRuntime expects from a broker sink, and it never touches
the driver until start() is called.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .broker_mic_sink import BrokerSinkSnapshot
from .driver_mic import DriverMicError, DriverMicWriter, MIC_SAMPLES
from .windows_driver_transport import WindowsDriverMicTransport

_FRAME_BYTES = MIC_SAMPLES * 4


class DirectMicSinkError(RuntimeError):
    pass


class DirectMicSink:
    """Single-use sink: start streams, set_enabled gates submitted PCM."""

    def __init__(self, instance_id: str, *, factory: Callable[[], DriverMicWriter] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._instance = instance_id
        self._factory = factory or (lambda: DriverMicWriter(lambda: WindowsDriverMicTransport(instance_id)))
        self._clock = clock
        self._lock = threading.RLock()
        self._writer: DriverMicWriter | None = None
        self._started = self._stopped = self._enabled = self._used = False
        self._sent = self._dropped = 0
        self._error: str | None = None

    @property
    def snapshot(self) -> BrokerSinkSnapshot:
        with self._lock:
            writer = self._writer
            ready = bool(writer is not None and writer.is_available())
            alive = bool(writer is not None and writer.snapshot.worker_alive)
            now = self._clock()
            return BrokerSinkSnapshot(
                queued=0,
                dropped=self._dropped,
                sent=self._sent,
                enabled=self._enabled and ready,
                writer_ready=self._enabled and ready,
                worker_alive=alive,
                cleanup_confirmed=not alive and self._stopped,
                error=self._error,
                observed_at=now,
                progress_at=now,
            )

    def is_available(self) -> bool:
        with self._lock:
            return bool(self._started and not self._stopped and self._writer is not None
                        and self._writer.is_available())

    def start(self) -> None:
        with self._lock:
            if self._used:
                raise DirectMicSinkError("Direct microphone sink is single-use")
            self._used = True
            try:
                writer = self._factory()
                writer.start()
            except BaseException as error:
                self._error = f"writer_start_failed: {type(error).__name__}"
                raise DirectMicSinkError("Direct microphone writer could not start") from None
            self._writer = writer
            self._started = True
        deadline = time.monotonic() + 3.0
        while not self.is_available() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.is_available():
            with self._lock:
                self._error = "driver_not_streaming"
            raise DirectMicSinkError("Direct microphone driver did not start streaming")

    def set_enabled(self, enabled: bool, timeout: float = 1.0) -> bool:
        if type(enabled) is not bool:
            raise ValueError("Expected an explicit microphone state")
        with self._lock:
            if not self._started or self._stopped:
                return False
            self._enabled = enabled
            return True

    def submit(self, floatbytes: bytes) -> bool:
        if type(floatbytes) is not bytes or len(floatbytes) != _FRAME_BYTES:
            return False
        with self._lock:
            writer = self._writer
            enabled = self._enabled and not self._stopped
            if not enabled or writer is None:
                return False
            self._sent += 1
        try:
            return writer.submit(floatbytes) is True
        except DriverMicError:
            with self._lock:
                self._dropped += 1
            return False

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._enabled = False
            writer = self._writer
        if writer is not None:
            try:
                writer.request_stop()
            except DriverMicError:
                pass
