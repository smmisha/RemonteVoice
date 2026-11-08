"""Worker-owned installed-client bootstrap; construction and import are inert.

Only the fixed protected client manifest may supply installation policy. Manifest
pins outlive every client/partial owner and are released only after exact cleanup
confirmation. No wire/CLI identity claims, service/elevation/process launch or
microphone activation are accepted here. The factory belongs on BrokerMicSink's
worker; every operation and cleanup retry stays on that same worker.
"""
from __future__ import annotations

import math
import threading

from .broker_composition import InstalledBrokerClient
from .broker_pipe_client import BrokerClientError, BrokerClientOpenError, BrokerObservation
from .windows_broker_manifest import PinnedInstalledManifest


class BrokerClientBootstrapError(BrokerClientError):
    pass


def _timeout(timeout: float) -> None:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.05 <= timeout <= 0.5:
        raise ValueError("Invalid broker client I/O deadline")


class BootstrapBrokerClient:
    """Single-use owner with cached observation and cleanup state only.

    An open failure exposes this outer owner, including partial construction,
    through BrokerClientOpenError.cleanup_owner. Fatal types are preserved and
    carry the same owner marker. Never replace an owner with unknown cleanup.
    """

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._thread: int | None = None
        self._used = self._opened = self._closed = self._opening = False
        self._confirmed = True  # The inert constructor owns no native resource.
        self._manifest = self._client = None
        self._pending: list[object] = []
        self._construction_unknown = False
        self._observation: BrokerObservation | None = None

    @property
    def cleanup_confirmed(self) -> bool:
        return self._confirmed and not self._opening

    @property
    def observation(self) -> BrokerObservation | None:
        return self._observation

    def _require_worker(self, *, active: bool = True) -> None:
        if self._thread is not None and self._thread != threading.get_ident():
            raise BrokerClientBootstrapError("Bootstrap client belongs to its worker")
        if active and (not self._opened or self._closed or self._client is None):
            raise BrokerClientBootstrapError("Bootstrap broker client is unavailable")

    def _retain_partial(self, error: BaseException, phase: str) -> None:
        partial = getattr(error, "cleanup_owner", None)
        if partial is not None and partial is not self and partial is not self._manifest:
            if phase == "manifest construction" and self._manifest is None:
                self._manifest = partial
            elif all(partial is not owner for owner in self._pending):
                self._pending.append(partial)
        elif partial is None and phase in ("manifest construction", "client construction"):
            # A constructor that never returned cannot prove it owned nothing.
            self._construction_unknown = True

    def open(self, timeout: float = 0.25) -> BootstrapBrokerClient:
        _timeout(timeout)
        with self._state_lock:
            if self._used:
                raise BrokerClientBootstrapError("Bootstrap client is single-use")
            self._used, self._opening, self._confirmed = True, True, False
            self._thread = threading.get_ident()
        phase = "manifest construction"
        try:
            self._manifest = PinnedInstalledManifest("client")
            phase = "manifest load"
            installation = self._manifest.load()
            if self._manifest.is_verified is not True:
                raise BrokerClientBootstrapError("Protected client manifest is not verified")
            phase = "client construction"
            self._client = InstalledBrokerClient(installation)
            self._pending.append(self._client)  # Own it before its native open.
            phase = "client open"
            if self._client.open(timeout=timeout) is not self._client:
                raise BrokerClientBootstrapError("Installed broker client did not return its owner")
            self._opened = True
            return self
        except BaseException as original:
            self._retain_partial(original, phase)
            cleanup_error = None
            try:
                self.close()
            except BaseException as error:
                cleanup_error = error
            fatal = (original if not isinstance(original, Exception) else
                     cleanup_error if cleanup_error is not None and not isinstance(cleanup_error, Exception) else None)
            if fatal is not None:
                try:
                    fatal.cleanup_owner = self
                except Exception:
                    pass
                raise fatal
            raise BrokerClientOpenError(self) from None
        finally:
            self._opening = False

    def observe(self) -> BrokerObservation:
        self._require_worker()
        try:
            status = self._client.observe()
            if type(status) is not BrokerObservation:
                raise BrokerClientBootstrapError("Invalid installed broker observation")
            self._observation = status
            return status
        except BaseException:
            self._observation = None
            raise

    def heartbeat(self) -> bool:
        self._require_worker()
        try:
            return self._client.heartbeat()
        except BaseException:
            self._observation = None
            raise

    def set_microphone_enabled(self, enabled: bool) -> bool:
        self._require_worker()
        self._observation = None  # An acknowledgement does not establish readiness.
        return self._client.set_microphone_enabled(enabled)

    def submit(self, payload: bytes) -> bool:
        self._require_worker()
        try:
            return self._client.submit(payload)
        except BaseException:
            self._observation = None
            raise

    def close(self) -> None:
        with self._state_lock:
            self._require_worker(active=False)
            if self._closed and self._confirmed:
                return
            if self._thread is None:
                self._thread = threading.get_ident()
            self._used = self._closed = True
            self._observation, self._confirmed = None, False
        errors, retained = [], []
        for owner in self._pending:
            try:
                owner.close()
                if owner.cleanup_confirmed is not True:
                    raise BrokerClientBootstrapError("Installed broker client cleanup is unconfirmed")
            except BaseException as error:
                retained.append(owner)
                errors.append(error)
        self._pending = retained
        if self._construction_unknown:
            errors.append(BrokerClientBootstrapError("Bootstrap construction cleanup is unknown"))
        # Never unlock the manifest while any inner I/O/verifier owner is live.
        if not errors and self._manifest is not None:
            try:
                self._manifest.close()
                if self._manifest.cleanup_confirmed is not True:
                    raise BrokerClientBootstrapError("Protected manifest cleanup is unconfirmed")
            except BaseException as error:
                errors.append(error)
        if errors:
            fatal = next((error for error in errors if not isinstance(error, Exception)), None)
            if fatal is not None:
                raise fatal
            raise BrokerClientBootstrapError("Bootstrap client cleanup is unconfirmed") from None
        self._confirmed = True


def open_installed_broker_client(timeout: float = 0.25) -> BootstrapBrokerClient:
    """BrokerMicSink worker factory; never launches or elevates the broker."""
    return BootstrapBrokerClient().open(timeout)
