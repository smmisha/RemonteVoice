"""Undeployed, bounded microphone sink around the authenticated broker client.

Construction is inert. start() opens a MUTED connection on one dedicated worker;
only explicit set_enabled(True) may request a microphone. That blocking method
belongs off the event loop. submit() and is_available() perform no pipe/native
I/O. All client methods, including retryable close, stay on the owning worker.

Heartbeat and observation are scheduled every 75/40 ms while I/O progresses.
A stalled worker or unchanged server progress invalidates cached health within
500 ms. An unconfirmed close retains the client and owning worker; another
stop() requests a cleanup retry. This module is not selected by the current Host.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .broker_pipe_client import BrokerObservation, BrokerPipeClient

_FRAME = struct.Struct("<480f")


class BrokerSinkError(RuntimeError):
    pass


class BrokerSinkStopError(BrokerSinkError):
    """The owning worker/client remains live; cleanup is not confirmed."""


@dataclass(frozen=True)
class BrokerSinkSnapshot:
    queued: int
    dropped: int
    sent: int
    enabled: bool
    writer_ready: bool
    worker_alive: bool
    cleanup_confirmed: bool
    error: str | None
    observed_at: float = 0.0
    progress_at: float = 0.0


def _timeout(value: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Sink deadline must be between zero and one second")


class BrokerMicSink:
    def __init__(self, client_factory: Callable[[], BrokerPipeClient], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(client_factory) or not callable(clock):
            raise TypeError("An inert client factory and trusted local clock are required")
        self._factory, self._clock = client_factory, clock
        self._cv = threading.Condition()
        self._worker: threading.Thread | None = None
        self._client: BrokerPipeClient | None = None
        self._used = self._stop = self._desired = self._ready = self._available = False
        self._generation = 0
        self._confirmed = -1
        self._cleanup_retry = 0
        self._queue: deque[tuple[float, bytes]] = deque()
        self._dropped = self._sent = 0
        self._error: str | None = None
        self._progress_at = self._observed_at = 0.0
        self._health = (False, 0.0, 0.0)

    @property
    def snapshot(self) -> BrokerSinkSnapshot:
        with self._cv:
            alive = bool(self._worker and self._worker.is_alive())
            return BrokerSinkSnapshot(len(self._queue), self._dropped, self._sent, self._desired,
                self._ready, alive, not alive and self._client is None, self._error,
                self._observed_at, self._progress_at)

    def is_available(self) -> bool:
        available, progress, observed = self._health  # No lock or pipe access.
        try:
            now = self._clock()
            return available and 0 <= now - progress < 0.5 and 0 <= now - observed < 0.5
        except Exception:
            return False

    def _publish(self) -> None:
        self._health = (self._available and not self._stop, self._progress_at, self._observed_at)

    def _fail_locked(self, error: str) -> None:
        self._error = self._error or error
        self._stop = True
        self._available = self._ready = False
        self._queue.clear()
        self._publish()
        self._cv.notify_all()

    def start(self) -> None:
        with self._cv:
            if self._used:
                raise BrokerSinkError("Broker microphone sink is single-use")
            self._used = True
            self._worker = threading.Thread(target=self._run, name="rv-broker-mic", daemon=True)
            try:
                self._worker.start()
            except Exception:
                self._fail_locked("worker_start_failed")
                raise BrokerSinkError("Broker worker could not start") from None

    def set_enabled(self, enabled: bool, timeout: float = 1.0) -> bool:
        """Wait off-loop for matching observed state; an ACK alone never succeeds."""
        _timeout(timeout)
        if type(enabled) is not bool:
            raise ValueError("Expected an explicit microphone state")
        deadline = time.monotonic() + timeout
        with self._cv:
            if not self._used or self._stop:
                return False
            if enabled != self._desired:
                self._generation += 1
                self._desired = enabled
                self._ready = self._available = False
                self._queue.clear()
                self._publish()
                self._cv.notify_all()
            generation = self._generation
            while not self._stop and generation == self._generation:
                if self._confirmed == generation and self.is_available():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail_locked("activation_timeout")
                    return False
                self._cv.wait(remaining)
            return False

    def submit(self, floatbytes: bytes) -> bool:
        if type(floatbytes) is not bytes or len(floatbytes) != _FRAME.size:
            return False
        if not all(math.isfinite(value) for value in _FRAME.unpack(floatbytes)):
            return False
        with self._cv:
            if self._stop or not self._desired or not self._ready or not self.is_available():
                return False
            now = self._clock()
            while self._queue and now - self._queue[0][0] >= 0.1:
                self._queue.popleft()
                self._dropped += 1
            if len(self._queue) == 10:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((now, floatbytes))
            self._cv.notify_all()
            return True

    def stop(self, timeout: float = 1.0) -> None:
        _timeout(timeout)
        with self._cv:
            self._used = self._stop = True
            self._ready = self._available = False
            self._queue.clear()
            self._cleanup_retry += 1
            self._publish()
            self._cv.notify_all()
            worker = self._worker
        if worker is threading.current_thread():
            raise BrokerSinkStopError("The broker worker cannot join itself")
        if worker and worker.ident is not None:
            worker.join(timeout)
        with self._cv:
            if (worker and worker.is_alive()) or self._client is not None:
                raise BrokerSinkStopError("Broker client cleanup is not confirmed")

    def _call(self, action):
        started = self._clock()
        value = action()
        now = self._clock()
        with self._cv:
            if not 0 <= now - started < 0.5:
                raise BrokerSinkError("broker_call_stalled")
            self._progress_at = now
            self._publish()
        return value

    def _observe(self, status: BrokerObservation, sent_generation: int) -> None:
        now = self._clock()
        if (type(status) is not BrokerObservation
                or any(type(value) is not bool for value in (status.available, status.mic_requested,
                                                            status.writer_ready, status.cleanup_pending))
                or type(status.observed_at) not in (int, float) or not math.isfinite(status.observed_at)
                or status.writer_ready and not status.mic_requested
                or status.available and (status.cleanup_pending or status.mic_requested and not status.writer_ready)
                or not 0 <= now - status.observed_at < 0.5):
            raise BrokerSinkError("broker_status_stale")
        with self._cv:
            if self._stop:
                return
            if self._confirmed == self._generation and sent_generation == self._generation:
                if (not status.available or status.cleanup_pending
                        or status.mic_requested != self._desired or status.writer_ready != self._desired):
                    raise BrokerSinkError("broker_endpoint_unavailable")
            self._observed_at = status.observed_at
            if (sent_generation == self._generation and status.mic_requested == self._desired
                    and status.available and not status.cleanup_pending
                    and (status.writer_ready if self._desired else not status.writer_ready)):
                self._confirmed = self._generation
                self._ready = self._desired
                self._available = True
            elif not status.available and not status.mic_requested and not status.cleanup_pending:
                raise BrokerSinkError("broker_endpoint_unavailable")
            self._publish()
            self._cv.notify_all()

    def _run(self) -> None:
        fatal_error = None
        try:
            client = self._factory()  # Factory does the verified, muted handshake.
            with self._cv:
                self._client = client
                cancelled = self._stop
            if cancelled:
                return
            initial = self._call(client.observe)
            if initial.mic_requested or initial.writer_ready or initial.cleanup_pending:
                raise BrokerSinkError("broker_connection_not_muted")
            self._observe(initial, 0)
            sent_generation = 0
            next_heartbeat, next_observe = self._clock(), self._clock() + 0.04
            while True:
                with self._cv:
                    if self._stop:
                        break
                    now = self._clock()
                    if not 0 <= now - self._observed_at < 0.5:
                        raise BrokerSinkError("broker_status_stale")
                    generation, desired = self._generation, self._desired
                    while self._queue and now - self._queue[0][0] >= 0.1:
                        self._queue.popleft()
                        self._dropped += 1
                    if generation != sent_generation:
                        action, payload = "enable", (generation, desired)
                    elif now >= next_heartbeat:
                        action, payload = "heartbeat", None
                    elif now >= next_observe:
                        action, payload = "observe", None
                    elif self._queue and self._ready:
                        action, payload = "submit", self._queue.popleft()[1]
                    else:
                        self._cv.wait(min(0.02, max(0, min(next_heartbeat, next_observe) - now)))
                        continue
                if action == "enable":
                    generation, desired = payload
                    if self._call(lambda: client.set_microphone_enabled(desired)) is not True:
                        raise BrokerSinkError("broker_enable_rejected")
                    sent_generation, next_observe = generation, self._clock()
                elif action == "heartbeat":
                    if self._call(client.heartbeat) is not True:
                        raise BrokerSinkError("broker_heartbeat_rejected")
                    next_heartbeat = self._clock() + 0.075
                elif action == "observe":
                    self._observe(self._call(client.observe), sent_generation)
                    next_observe = self._clock() + 0.04
                else:
                    accepted = self._call(lambda: client.submit(payload))
                    with self._cv:
                        self._sent += accepted is True
                        self._dropped += accepted is not True
        except Exception as error:
            with self._cv:
                # A native/client constructor may fail before returning while
                # still owning cancelled I/O or identity handles. Its explicit
                # cleanup owner must stay on this worker, never become "clean".
                if self._client is None and getattr(error, 'cleanup_owner', None) is not None:
                    self._client = error.cleanup_owner
                self._fail_locked("broker_connection_failed")
        except BaseException as error:
            fatal_error = error
            with self._cv:
                if self._client is None and getattr(error, 'cleanup_owner', None) is not None:
                    self._client = error.cleanup_owner
                self._fail_locked("broker_identity_fatal")
        finally:
            try:
                self._cleanup()
            finally:
                if fatal_error is not None:
                    raise fatal_error

    def _cleanup(self) -> None:
        with self._cv:
            self._available = self._ready = False
            self._queue.clear()
            self._publish()
        while self._client is not None:
            with self._cv:
                attempted_retry = self._cleanup_retry
            try:
                self._client.close()
                if self._client.cleanup_confirmed is not True:
                    raise BrokerSinkStopError("Broker client cleanup unconfirmed")
            except Exception:
                with self._cv:
                    self._fail_locked("broker_close_failed")
                    self._cv.wait_for(lambda: self._cleanup_retry != attempted_retry)
            else:
                with self._cv:
                    self._client = None
                    self._cv.notify_all()
