"""Undeployed driver microphone writer; one object per explicit mic activation.

Transport calls belong to a dedicated worker. A stalled call is observed without
closing its native handle/buffers from another thread. The caller must use
``is_available`` with the existing route guard, and retain failed writers until
``stop`` confirms cleanup. This module never selects Windows default devices.
"""
from __future__ import annotations

import math
import secrets
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Protocol

import numpy as np

MIC_SAMPLES = 480
PCM_BYTES = MIC_SAMPLES * 2
WIRE_BYTES = 1000
_HEADER = struct.Struct("<IIQ16sII")


class MicCommand(IntEnum):
    BEGIN = 1
    WRITE = 2
    END = 3
    KEEPALIVE = 4


class DriverMicError(RuntimeError):
    pass


class DriverMicStopError(DriverMicError):
    """A native call still owns its resources; do not claim it was stopped."""


class DriverBufferFull(DriverMicError):
    """One PCM packet was rejected; this alone does not end the session."""


class MicTransport(Protocol):
    def send(self, message: bytes) -> None: ...
    def close(self) -> None: ...


def encode_command(command: MicCommand, session: int, nonce: bytes, pcm: bytes = b"") -> bytes:
    if not isinstance(command, MicCommand):
        raise ValueError("Unknown driver command")
    if type(session) is not int or not 0 < session < 2**64:
        raise ValueError("Invalid session")
    if type(nonce) is not bytes or len(nonce) != 16 or not any(nonce):
        raise ValueError("Invalid lease nonce")
    if type(pcm) is not bytes or len(pcm) != (PCM_BYTES if command == MicCommand.WRITE else 0):
        raise ValueError("Invalid PCM packet")
    return _HEADER.pack(1, command, session, nonce, len(pcm), 0) + pcm.ljust(PCM_BYTES, b"\0")


def float_mono_to_pcm16(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or len(payload) != MIC_SAMPLES * 4:
        raise ValueError("Expected one 48 kHz float32 mono frame")
    samples = np.frombuffer(payload, dtype="<f4")
    if not np.isfinite(samples).all():
        raise ValueError("Nonfinite microphone sample")
    clipped = np.clip(samples, -1.0, 1.0)
    return np.rint(clipped * np.where(clipped < 0, 32768.0, 32767.0)).astype("<i2").tobytes()


class MicWriterState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    STREAMING = "streaming"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True)
class MicWriterSnapshot:
    state: MicWriterState
    queued: int
    dropped: int
    sent: int
    worker_alive: bool
    cleanup_confirmed: bool
    error: str | None


class DriverMicWriter:
    """Single-use, bounded producer; construction does not open or enable a mic.

    ``start`` must follow the user's microphone action. A watchdog marks native
    stalls failed; it cannot cancel a defective kernel driver. Stop stays bounded
    and reports a still-live worker rather than freeing pending I/O resources.
    """

    def __init__(self, transport_factory: Callable[[], MicTransport], *,
                 clock: Callable[[], float] = time.monotonic,
                 call_timeout: float = 0.5) -> None:
        if not math.isfinite(call_timeout) or not 0.05 <= call_timeout <= 1.0:
            raise ValueError("Invalid driver call deadline")
        self._factory = transport_factory
        self._clock = clock
        self._timeout = call_timeout
        self._cv = threading.Condition()
        self._state = MicWriterState.IDLE
        self._queue: deque[tuple[float, bytes]] = deque()
        self._stop = False
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._inflight: float | None = None
        self._transport: MicTransport | None = None
        self._dropped = self._sent = 0
        self._error: str | None = None

    @property
    def snapshot(self) -> MicWriterSnapshot:
        with self._cv:
            alive = bool(self._worker and self._worker.is_alive())
            return MicWriterSnapshot(self._state, len(self._queue), self._dropped, self._sent,
                                     alive, not alive and self._transport is None, self._error)

    def is_available(self) -> bool:
        with self._cv:
            return self._state == MicWriterState.STREAMING

    def start(self) -> None:
        with self._cv:
            if self._state != MicWriterState.IDLE:
                raise DriverMicError("Microphone writer is single-use")
            # Fresh identity for each user activation; never publish it in status/logs.
            session = secrets.randbits(64) or 1
            nonce = secrets.token_bytes(16)
            if not any(nonce):
                raise DriverMicError("Microphone lease entropy unavailable")
            self._state = MicWriterState.STARTING
            self._worker = threading.Thread(target=self._run, args=(session, nonce),
                                            name="rv-driver-mic", daemon=True)
            self._watchdog = threading.Thread(target=self._watch, name="rv-driver-health", daemon=True)
            try:
                self._worker.start()
                self._watchdog.start()
            except Exception:
                self._fail_locked("worker_start_failed")
                raise

    def submit(self, payload: bytes | memoryview) -> bool:
        # Copy before enqueue: a caller cannot mutate an accepted packet later.
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return False
        if (payload.nbytes if isinstance(payload, memoryview) else len(payload)) != MIC_SAMPLES * 4:
            return False
        with self._cv:
            if self._state != MicWriterState.STREAMING:
                return False
        try:
            pcm = float_mono_to_pcm16(bytes(payload))
        except ValueError:
            return False
        with self._cv:
            if self._state != MicWriterState.STREAMING:
                return False
            if len(self._queue) == 10:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((self._clock(), pcm))
            self._cv.notify_all()
            return True

    def request_stop(self) -> None:
        with self._cv:
            self._stop = True
            self._queue.clear()
            if self._state == MicWriterState.IDLE:
                self._state = MicWriterState.CLOSED
                self._done.set()
            elif self._state in (MicWriterState.STARTING, MicWriterState.STREAMING):
                self._state = MicWriterState.STOPPING
            self._cv.notify_all()

    def stop(self, timeout: float = 1.0) -> None:
        if not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise ValueError("Invalid stop deadline")
        self.request_stop()
        deadline = time.monotonic() + timeout
        for worker in (self._worker, self._watchdog):
            if worker and worker.ident is not None and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        if any(t and t.is_alive() for t in (self._worker, self._watchdog)):
            raise DriverMicStopError("Microphone driver call has not finished")
        with self._cv:
            if self._transport is not None:
                raise DriverMicStopError("Microphone driver handle cleanup unconfirmed")

    def _fail_locked(self, reason: str) -> None:
        self._state = MicWriterState.FAILED
        self._error = self._error or reason
        self._stop = True
        self._queue.clear()
        self._cv.notify_all()

    def _watch(self) -> None:
        while not self._done.wait(0.025):
            with self._cv:
                if self._inflight is not None and self._clock() - self._inflight >= self._timeout:
                    self._fail_locked("driver_call_timeout")
                    return

    def _call(self, action: Callable[[], object]) -> object:
        with self._cv:
            self._inflight = self._clock()
        try:
            return action()
        finally:
            with self._cv:
                if self._clock() - self._inflight >= self._timeout:
                    self._fail_locked("driver_call_timeout")
                self._inflight = None

    def _run(self, session: int, nonce: bytes) -> None:
        transport = None
        began = False
        try:
            transport = self._call(self._factory)
            with self._cv:
                self._transport = transport
                cancelled = self._stop
            if cancelled:
                return
            began = True  # END is also attempted when BEGIN's acknowledgement is lost.
            self._call(lambda: transport.send(encode_command(MicCommand.BEGIN, session, nonce)))
            with self._cv:
                if not self._stop:
                    self._state = MicWriterState.STREAMING
            next_ping = self._clock() + 0.25
            while True:
                with self._cv:
                    if self._stop:
                        break
                    now = self._clock()
                    while self._queue and now - self._queue[0][0] >= 0.1:
                        self._queue.popleft()
                        self._dropped += 1
                    if self._queue:
                        _, pcm = self._queue.popleft()
                        command = MicCommand.WRITE
                    elif now >= next_ping:
                        pcm, command = b"", MicCommand.KEEPALIVE
                    else:
                        self._cv.wait(min(0.25, next_ping - now))
                        continue
                try:
                    self._call(lambda: transport.send(encode_command(command, session, nonce, pcm)))
                except DriverBufferFull:
                    if command != MicCommand.WRITE:
                        raise
                    with self._cv:
                        self._dropped += 1
                else:
                    if command == MicCommand.WRITE:
                        with self._cv:
                            self._sent += 1
                next_ping = self._clock() + 0.25
        except Exception:
            with self._cv:
                self._fail_locked("driver_transport_failed")
        finally:
            if transport is not None:
                try:
                    if began:
                        self._call(lambda: transport.send(encode_command(MicCommand.END, session, nonce)))
                except Exception:
                    with self._cv:
                        self._fail_locked("driver_end_failed")
                finally:
                    try:
                        self._call(transport.close)
                        with self._cv:
                            self._transport = None
                    except Exception:
                        with self._cv:
                            self._fail_locked("driver_close_failed")
            with self._cv:
                self._queue.clear()
                if self._state != MicWriterState.FAILED:
                    self._state = MicWriterState.CLOSED
            self._done.set()
