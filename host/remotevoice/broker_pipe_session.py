"""One Windows pipe connection bound to broker lifecycle; no service installation.

Only a dedicated broker process may own this session. Its admission policy and
peer verifier come from protected deployment configuration, never the pipe wire.
Client-side server verification remains required before the first request.
"""
from __future__ import annotations

import threading
import time
import struct
from collections.abc import Callable

from .broker_protocol import BrokerConnection, BrokerProtocolError, control_body_size
from .mic_broker import BrokerPolicy, MicBroker, PeerIdentity

_HELLO = b'RVMB\x01\x03\x00\x00'  # Empty heartbeat; no caller-supplied identity.


class BrokerSessionError(RuntimeError):
    pass


class BrokerPipeSession:
    """Single-use service-worker session with independent deadline polling.

    The owner must retain this object and refuse another connection while
    cleanup_confirmed is false. Fatal verifier failures propagate as BaseException
    and require the dedicated broker process to terminate; never resume dispatch.
    """

    def __init__(self, pipe, verifier, policy: BrokerPolicy, writer_factory: Callable, *,
                 read_timeout: float = 0.25, cleanup_timeout: float = 1.0) -> None:
        if not 0.05 <= read_timeout <= 0.5 or not 0.05 <= cleanup_timeout <= 2:
            raise ValueError('Invalid service deadlines')
        self._pipe, self._verifier = pipe, verifier
        self._policy, self._writer_factory = policy, writer_factory
        self._read_timeout, self._cleanup_timeout = read_timeout, cleanup_timeout
        self._broker: MicBroker | None = None
        self._connection: BrokerConnection | None = None
        self._worker: threading.Thread | None = None
        self._finish = threading.Event()
        self._worker_failed = threading.Event()
        self._poll_fatal: BaseException | None = None
        self._poll_ready = threading.Event()
        self._used = False
        self._pipe_closed = False
        self._identity_cleaned = False
        self._fatal_failure = False
        self._cleanup_failed = False
        self._last_poll: float | None = None
        self._poll_sequence = 0

    @property
    def cleanup_confirmed(self) -> bool:
        return (self._pipe_closed and self._identity_cleaned and not self._fatal_failure and not self._cleanup_failed
                and not (self._worker and self._worker.is_alive())
                and (self._broker is None or self._broker.cleanup_confirmed))

    def is_available(self) -> bool:
        return bool(self._worker_is_current() and self._broker and self._broker.is_available())

    def _worker_is_current(self) -> bool:
        return bool(not self._finish.is_set() and not self._worker_failed.is_set()
                    and self._last_poll is not None and 0 <= time.monotonic() - self._last_poll <= 0.25)

    def _poll(self) -> None:
        cleanup_deadline = None
        try:
            while True:
                if self._finish.is_set():
                    self._broker.shutdown()
                self._broker.poll()
                self._last_poll = time.monotonic()
                self._poll_sequence += 1
                self._poll_ready.set()
                if self._finish.is_set():
                    if self._broker.cleanup_confirmed:
                        return
                    if cleanup_deadline is None:
                        cleanup_deadline = time.monotonic() + self._cleanup_timeout
                    if time.monotonic() >= cleanup_deadline:
                        return  # Retain broker/writer; cleanup_confirmed stays false.
                self._finish.wait(0.025) if not self._finish.is_set() else time.sleep(0.025)
        except BaseException as error:
            if not isinstance(error, Exception):
                self._poll_fatal = error
                self._fatal_failure = True
            self._worker_failed.set()
            self._poll_ready.set()
            # Unavailable immediately; main session closes admission/resources.

    def _raise_poll_fatal(self) -> None:
        if self._poll_fatal is not None:
            raise self._poll_fatal

    def _status_payload(self) -> bytes:
        status = self._broker.status()
        # The polling worker can finish BEGIN/END after status() releases its lock.
        # All broker flags must describe that one snapshot, including availability.
        available = status.available and self._worker_is_current()
        flags = (int(available) | (int(status.microphone_requested) << 1)
                 | (int(status.writer_ready) << 2) | (int(status.cleanup_pending) << 3))
        return struct.pack('<BQ', flags, self._poll_sequence)

    def _frame(self) -> bytes:
        header = self._pipe.read_exact(8, self._read_timeout)
        size = control_body_size(header)
        return header + (self._pipe.read_exact(size, self._read_timeout) if size else b'')

    def serve(self, stop: threading.Event) -> None:
        if self._used:
            raise BrokerSessionError('Session is single-use')
        self._used = True
        fatal_error: BaseException | None = None
        pipe_handle = self._pipe.handle
        try:
            first = self._frame()
            if first != _HELLO:
                raise BrokerProtocolError('Authentication handshake required')
            peer = self._verifier.verify_pipe_peer(self._pipe.handle, server_side=True)
            if type(peer) is not PeerIdentity:
                raise PermissionError('Invalid peer verification')
            self._broker = MicBroker(authenticate=lambda: peer, policy=self._policy,
                                     writer_factory=self._writer_factory)
            self._connection = BrokerConnection(self._broker, status_payload=self._status_payload)
            self._worker = threading.Thread(target=self._poll, name='rv-broker-poll', daemon=True)
            self._worker.start()
            ready = self._poll_ready.wait(0.1)
            self._raise_poll_fatal()
            if not ready or self._worker_failed.is_set():
                raise BrokerSessionError('Broker worker did not become ready')
            self._pipe.write_all(self._connection.handle_frame(first), self._read_timeout)
            while not stop.is_set() and not self._worker_failed.is_set():
                frame = self._frame()
                self._raise_poll_fatal()
                # Windows last-message token is checked before privileged dispatch.
                if not self._verifier.validate_pipe_token(self._pipe.handle, peer, server_side=True):
                    raise PermissionError('Peer changed')
                response = self._connection.handle_frame(frame)
                self._pipe.write_all(response, self._read_timeout)
                if frame[5] == 4:  # Explicit close; no further pipe messages.
                    # DisconnectNamedPipe discards unread output. Let the client
                    # consume its ACK and close before disconnecting server end.
                    try:
                        extra = self._pipe.read_exact(1, self._read_timeout)
                        if extra:
                            raise BrokerProtocolError('Data after close')
                    except (EOFError, TimeoutError):
                        pass
                    except Exception as error:
                        from .windows_broker_pipe import PipeEOFError, PipeTimeoutError
                        if not isinstance(error, (PipeEOFError, PipeTimeoutError)):
                            raise
                    break
            self._raise_poll_fatal()
            if self._worker_failed.is_set():
                raise BrokerSessionError('Broker worker failed')
        except Exception as error:
            raise BrokerSessionError('Broker pipe session failed') from error
        except BaseException as error:
            fatal_error = error
            self._fatal_failure = True
            raise
        finally:
            self._finish.set()
            cleanup_errors = []
            if fatal_error is None:
                fatal_error = self._poll_fatal
            if fatal_error is None:
                try:
                    if self._worker and self._worker.ident is not None:
                        # The poll worker owns all privileged cleanup after start.
                        # Calling shutdown here could race a fatal poll exception.
                        self._worker.join(self._cleanup_timeout + 0.1)
                    elif self._broker:
                        self._broker.shutdown()
                except BaseException as error:
                    cleanup_errors.append(error)
            if fatal_error is None:
                fatal_error = self._poll_fatal
            # Closing the pipe is allowed even on fatal token reversion failure;
            # never perform further driver work on that thread.
            try:
                self._pipe.close()
                self._pipe_closed = True
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                self._verifier.forget_pipe_peer(pipe_handle)
                self._identity_cleaned = True
            except BaseException as error:
                cleanup_errors.append(error)
            self._cleanup_failed = bool(cleanup_errors)
            if fatal_error is None:
                fatal_error = self._poll_fatal
            # Never let ordinary cleanup errors turn a failed RevertToSelf into
            # a recoverable authentication rejection at the process boundary.
            if fatal_error is not None:
                self._fatal_failure = True
                raise fatal_error
            if cleanup_errors:
                fatal_cleanup = next((error for error in cleanup_errors if not isinstance(error, Exception)), None)
                if fatal_cleanup is not None:
                    self._fatal_failure = True
                    raise fatal_cleanup
                raise cleanup_errors[0]
