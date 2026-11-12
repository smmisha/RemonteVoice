"""Worker-side authenticated broker client. No driver, UI or service startup.

All I/O belongs to one owning worker. The UI consumes cached observations only.
The verifier must authenticate the actual server process from protected policy.
Local endpoint identities must be verified separately before routing is enabled.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass

from .broker_protocol import MAX_CONTROL_FRAME
from .mic_broker import PeerIdentity

_HEADER = struct.Struct('<4sBBH')


class BrokerClientError(RuntimeError):
    pass


class BrokerClientOpenError(BrokerClientError):
    """Handshake failed with resources still owned by the creating worker."""
    def __init__(self, cleanup_owner) -> None:
        super().__init__('Broker setup cleanup is not confirmed')
        self.cleanup_owner = cleanup_owner


@dataclass(frozen=True)
class BrokerObservation:
    available: bool
    mic_requested: bool
    writer_ready: bool
    cleanup_pending: bool
    observed_at: float


class BrokerPipeClient:
    def __init__(self, pipe, verifier, *, timeout: float = 0.25, clock=time.monotonic) -> None:
        if not math.isfinite(timeout) or not 0.05 <= timeout <= 0.5:
            raise ValueError('Invalid client I/O deadline')
        self._pipe, self._verifier = pipe, verifier
        self._timeout, self._clock = timeout, clock
        self._thread = threading.get_ident()
        self._handle = pipe.handle
        self._closed = False
        self._cleanup_confirmed = False
        self._sequence = -1
        self._advance_time = 0.0
        self._observation: BrokerObservation | None = None
        try:
            peer = verifier.verify_pipe_peer(self._handle, server_side=False)
            if type(peer) is not PeerIdentity:
                raise PermissionError('Server verification failed')
            self._peer = peer
            if not self._ack(3):  # Only muted heartbeat handshake; never enable implicitly.
                raise BrokerClientError('Broker handshake rejected')
        except BaseException as error:
            try:
                self._close_preserving(error)
            except BaseException as cleanup_error:
                if not self._cleanup_confirmed:
                    fatal = (error if not isinstance(error, Exception) else
                             cleanup_error if not isinstance(cleanup_error, Exception) else None)
                    if fatal is not None:
                        # Preserve the process-fatal type. The dedicated owner
                        # must terminate; this marker merely retains resources.
                        try:
                            fatal.cleanup_owner = self
                        except Exception:
                            pass
                        raise fatal
                    raise BrokerClientOpenError(self) from None
                raise
            raise

    @property
    def observation(self) -> BrokerObservation | None:
        """Cached only; callers must check observed_at for staleness."""
        return self._observation

    @property
    def cleanup_confirmed(self) -> bool:
        return self._cleanup_confirmed

    def _owner(self) -> None:
        if threading.get_ident() != self._thread or self._closed:
            raise BrokerClientError('Broker connection is unavailable on this worker')

    def _close_pipe(self) -> None:
        self._closed = True
        self._observation = None
        self._cleanup_confirmed = False
        errors = []
        try:
            self._pipe.close()
        except BaseException as error:
            errors.append(error)
        try:
            self._verifier.forget_pipe_peer(self._handle)
        except BaseException as error:
            errors.append(error)
        if errors:
            fatal = next((error for error in errors if not isinstance(error, Exception)), None)
            raise fatal if fatal is not None else errors[0]
        self._cleanup_confirmed = True

    def _close_preserving(self, original: BaseException) -> None:
        try:
            self._close_pipe()
        except BaseException:
            if not isinstance(original, Exception):
                raise original
            raise

    def _exchange(self, command: int, payload: bytes, size: int) -> bytes:
        self._owner()
        try:
            if len(payload) > MAX_CONTROL_FRAME - 8:
                raise BrokerClientError('Invalid broker payload')
            if not self._verifier.validate_pipe_token(self._handle, self._peer, server_side=False):
                raise PermissionError('Server identity changed')
            self._pipe.write_all(_HEADER.pack(b'RVMB', 1, command, len(payload)) + payload, self._timeout)
            header = self._pipe.read_exact(8, self._timeout)
            if header != _HEADER.pack(b'RVMB', 1, command | 0x80, size):
                raise BrokerClientError('Invalid broker response')
            return self._pipe.read_exact(size, self._timeout)
        except BaseException as error:
            self._close_preserving(error)
            raise

    def _ack(self, command: int, payload: bytes = b'') -> bool:
        value = self._exchange(command, payload, 1)
        if value not in (b'\0', b'\1'):
            self._close_pipe()
            raise BrokerClientError('Invalid broker acknowledgement')
        return value == b'\1'

    def heartbeat(self) -> bool:
        return self._ack(3)

    def set_microphone_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise ValueError('Expected explicit microphone state')
        self._observation = None  # ACK is not evidence of readiness.
        return self._ack(1, bytes((enabled,)))

    def submit(self, frame: bytes) -> bool:
        if type(frame) is not bytes or len(frame) != 1920:
            raise ValueError('Expected exactly 480 float32 microphone samples')
        if not all(math.isfinite(value) for value in struct.unpack('<480f', frame)):
            raise ValueError('Nonfinite microphone sample')
        return self._ack(2, frame)

    def observe(self) -> BrokerObservation:
        flags, sequence = struct.unpack('<BQ', self._exchange(5, b'', 9))
        if flags & ~15 or sequence < self._sequence or not sequence:
            self._close_pipe()
            raise BrokerClientError('Invalid broker status')
        if sequence != self._sequence:
            self._advance_time = self._clock()
        self._sequence = sequence
        status = BrokerObservation(bool(flags & 1), bool(flags & 2), bool(flags & 4),
                                   bool(flags & 8), self._advance_time)
        if ((status.writer_ready and not status.mic_requested)
                or (status.available and (status.cleanup_pending or status.mic_requested and not status.writer_ready))):
            self._close_pipe()
            raise BrokerClientError('Inconsistent broker status')
        self._observation = status
        return status

    def close(self) -> None:
        if self._closed and not self._cleanup_confirmed:
            if threading.get_ident() != self._thread:
                raise BrokerClientError('Cleanup belongs to the owning worker')
            self._close_pipe()
            return
        if not self._closed:
            self._owner()
            try:
                self._ack(4)
            finally:
                if not self._closed:
                    self._close_pipe()
