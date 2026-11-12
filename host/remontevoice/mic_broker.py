"""Microphone broker lifecycle; native Windows IPC adapters live separately.

The server must obtain PeerIdentity through a trusted, injected authenticate()
adapter that verifies the Windows connection token and executable image. Never
construct that identity from a request payload. The hosting IPC adapter must
bind the returned capability to that connection and close it on disconnect.

Call poll() frequently (at most 50 ms apart) on a separate service worker; all
operations except cached is_available() belong off the UI thread. Muted owners
need no writer. Factories construct inert, fresh DriverMicWriter objects only.
No driver session, nonce, command, device path or microphone is client-selected.
"""
from __future__ import annotations

import hmac
import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .driver_mic import DriverMicWriter, MIC_SAMPLES, MicWriterState


@dataclass(frozen=True, repr=False)
class PeerIdentity:
    user_sid: str
    logon_sid: str
    session_id: int
    image_sha256: str

    def __post_init__(self) -> None:
        for sid in (self.user_sid, self.logon_sid):
            if type(sid) is not str or not re.fullmatch(r"S-\d+(?:-\d+)+", sid, re.ASCII):
                raise ValueError("Invalid authenticated SID")
        if type(self.session_id) is not int or not 0 <= self.session_id < 2**32:
            raise ValueError("Invalid authenticated session")
        if type(self.image_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.image_sha256):
            raise ValueError("Expected canonical executable SHA-256")


@dataclass(frozen=True)
class BrokerPolicy:
    expected: PeerIdentity | None
    admission: Callable[[PeerIdentity], bool]

    def __post_init__(self) -> None:
        if self.expected is not None and type(self.expected) is not PeerIdentity:
            raise TypeError("Expected a configured authenticated identity")
        if not callable(self.admission):
            raise TypeError("An explicit server-side admission callback is required")

    def allows(self, peer: object) -> bool:
        if self.expected is None or type(peer) is not PeerIdentity or peer != self.expected:
            return False
        try:
            return self.admission(peer) is True
        except Exception:
            return False


@dataclass(frozen=True)
class BrokerStatus:
    connected: bool
    microphone_requested: bool
    writer_ready: bool
    cleanup_pending: bool
    available: bool


class MicBroker:
    """One authenticated connection, one opaque capability, one active writer.

    Failed cleanup retains the writer and blocks replacement. close() and mute
    close ingress immediately; only poll() performs bounded writer.stop().
    """

    def __init__(self, *, authenticate: Callable[[], PeerIdentity], policy: BrokerPolicy,
                 writer_factory: Callable[[], DriverMicWriter],
                 clock: Callable[[], float] = time.monotonic,
                 heartbeat_timeout: float = 0.75, stop_timeout: float = 0.05) -> None:
        if not callable(authenticate) or not callable(writer_factory) or type(policy) is not BrokerPolicy:
            raise TypeError("Trusted authentication, policy and writer factory are required")
        if not math.isfinite(heartbeat_timeout) or not 0 < heartbeat_timeout <= 0.95:
            raise ValueError("Heartbeat deadline plus worker polling must not exceed one second")
        if not math.isfinite(stop_timeout) or not 0 <= stop_timeout <= 1:
            raise ValueError("Invalid bounded stop deadline")
        self._authenticate, self._policy, self._factory = authenticate, policy, writer_factory
        self._clock, self._heartbeat_timeout, self._stop_timeout = clock, heartbeat_timeout, stop_timeout
        self._lock = threading.RLock()
        self._capability: bytes | None = None
        self._writer: DriverMicWriter | None = None
        self._enabled = self._stopping = self._available = False
        self._last_heartbeat = 0.0

    def is_available(self) -> bool:
        # A single cached bool: never acquire a lock or inspect native resources.
        return self._available

    @property
    def cleanup_confirmed(self) -> bool:
        """Cached ownership result; a worker retaining resources remains visible."""
        return self._writer is None

    def status(self) -> BrokerStatus:
        """Worker-side state only; no native calls and no capability disclosure."""
        with self._lock:
            return BrokerStatus(self._capability is not None, self._enabled,
                                self._enabled and self._available and not self._stopping,
                                self._stopping, self._available)

    def shutdown(self) -> None:
        """Trusted service owner closes ingress even after capability expiry."""
        with self._lock:
            self._retire()

    def _schedule_stop(self) -> None:
        self._available = False
        if self._writer is not None and not self._stopping:
            self._stopping = True
            try:
                self._writer.request_stop()
            except Exception:
                pass  # Retain ownership; poll() must still confirm cleanup.

    def _retire(self) -> None:
        self._capability = None
        self._enabled = False
        self._schedule_stop()

    def _expire(self) -> None:
        if self._capability is not None and self._clock() - self._last_heartbeat >= self._heartbeat_timeout:
            self._retire()

    def _owns(self, capability: object) -> bool:
        self._expire()
        return (type(capability) is bytes and len(capability) == 32 and self._capability is not None
                and hmac.compare_digest(capability, self._capability))

    def connect(self) -> bytes | None:
        """Authenticate the adapter's current connection, never caller-supplied claims."""
        with self._lock:
            self._expire()
            if self._capability is not None or self._writer is not None:
                return None
            try:
                if not self._policy.allows(self._authenticate()):
                    return None
                capability = secrets.token_bytes(32)
            except Exception:
                return None
            self._capability = capability
            self._last_heartbeat = self._clock()
            self._enabled = False
            self._available = True
            return capability

    def set_microphone_enabled(self, capability: bytes, enabled: bool) -> bool:
        with self._lock:
            if type(enabled) is not bool or not self._owns(capability) or (enabled and self._stopping):
                return False
            if self._enabled == enabled:
                return True
            self._enabled = enabled
            if not enabled and self._writer is not None:
                self._schedule_stop()
            else:
                self._available = not enabled and self._writer is None
            return True

    def submit(self, capability: bytes, float32bytes: bytes) -> bool:
        with self._lock:
            if (not self._owns(capability) or not self._enabled or self._stopping or
                    not self._available or self._writer is None or
                    type(float32bytes) is not bytes or len(float32bytes) != MIC_SAMPLES * 4):
                return False
            try:
                return self._writer.submit(float32bytes) is True
            except Exception:
                self._retire()
                return False

    def heartbeat(self, capability: bytes) -> bool:
        with self._lock:
            if not self._owns(capability):
                return False
            self._last_heartbeat = self._clock()
            return True

    def close(self, capability: bytes) -> bool:
        with self._lock:
            if not self._owns(capability):
                return False
            self._retire()
            return True

    def poll(self) -> None:
        """Service-worker tick; cleanup attempts are bounded and ownership retained."""
        with self._lock:
            self._expire()
            if self._stopping:
                try:
                    self._writer.stop(timeout=self._stop_timeout)
                    snapshot = self._writer.snapshot
                    if not snapshot.cleanup_confirmed or snapshot.worker_alive:
                        return
                except Exception:
                    return
                self._writer = None
                self._stopping = False
                self._expire()
            if self._capability is None:
                self._available = False
                return
            if not self._enabled:
                self._available = self._writer is None
                return
            if self._writer is None:
                try:
                    self._writer = self._factory()
                    if self._writer is None:
                        raise TypeError("Writer factory returned no writer")
                    self._writer.start()
                except Exception:
                    self._retire()
                    return
            self._expire()
            if self._capability is None:
                return
            try:
                state = self._writer.snapshot.state
                if state not in (MicWriterState.STARTING, MicWriterState.STREAMING):
                    self._retire()
                else:
                    self._available = state == MicWriterState.STREAMING
            except Exception:
                self._retire()
