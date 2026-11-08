"""Inert installed-driver controller for explicitly trusted Host composition.

start()/stop() belong off the UI thread. The fixed client manifest stays pinned
through endpoint inventory, broker-client lifetime and confirmed runtime cleanup.
This module never installs/launches/elevates a broker, enables a microphone or
changes Windows routes/defaults. A controller belongs to one Host run only.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from .audio_backend import DriverAudioBackend, DriverBackendSnapshot
from .broker_client_bootstrap import open_installed_broker_client
from .broker_mic_sink import BrokerMicSink
from .driver_backend_runtime import DriverBackendRuntime
from .windows_broker_manifest import PinnedInstalledManifest
from .windows_driver_endpoints import WindowsDriverEndpointInventory

_UNAVAILABLE = DriverBackendSnapshot(1, False, False, "", "", 0.0)


class InstalledDriverHostError(RuntimeError):
    pass


class InstalledDriverHostStopError(InstalledDriverHostError):
    """Controller/manifest/runtime ownership remains retained and unconfirmed."""


@dataclass(frozen=True)
class InstalledDriverHostSnapshot:
    cleanup_confirmed: bool
    error: str | None
    starting: bool
    stopping: bool


class InstalledDriverHost:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._control = threading.Lock()
        self._stop = threading.Event()
        self._used = self._starting = self._started = False
        self._manifest = self._runtime = self._inventory = None
        self._manifest_clean = self._runtime_clean = self._clean = True
        self._construction_unknown = False
        self._error: str | None = None
        self.backend = DriverAudioBackend(self.observe, max_age=0.5)

    @property
    def snapshot(self) -> InstalledDriverHostSnapshot:
        with self._lock:
            return InstalledDriverHostSnapshot(self._clean and not self._starting,
                self._error, self._starting, self._stop.is_set())

    def observe(self) -> DriverBackendSnapshot:
        # Runtime.observe() itself is a cache read. No monitor/native work here.
        runtime = self._runtime
        if not self._started or self._stop.is_set() or self._error is not None or runtime is None:
            return _UNAVAILABLE
        try:
            return runtime.observe()
        except Exception:
            return _UNAVAILABLE

    def guard_is_available(self) -> bool:
        runtime = self._runtime
        if not self._started or self._stop.is_set() or self._error is not None or runtime is None:
            return False
        try:
            return runtime.guard_is_available() is True
        except Exception:
            return False

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._error = self._error or reason

    def _check_starting(self) -> None:
        if self._stop.is_set():
            raise InstalledDriverHostError("Installed driver startup cancelled")

    def _signal_runtime_stop(self) -> None:
        # Public runtime.stop(0) closes its ingress even if its startup/control
        # worker is still blocked. Final cleanup is serialized and checked below.
        runtime = self._runtime
        if runtime is not None and not self._runtime_clean:
            try:
                runtime.stop(timeout=0)
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._used or self._stop.is_set():
                raise InstalledDriverHostError("Installed driver controller is single-use")
            self._used = self._starting = True
            self._clean = False
        phase = "before construction"
        try:
            with self._control:
                self._check_starting()
                phase = "manifest construction"
                self._manifest = PinnedInstalledManifest("client")
                self._manifest_clean = False
                phase = "manifest load"
                installation = self._manifest.load()
                self._check_starting()
                if self._manifest.is_verified is not True:
                    raise InstalledDriverHostError("Protected client manifest is not verified")
                self._inventory = WindowsDriverEndpointInventory(installation.driver_instance_id)
                self._check_starting()
                phase = "runtime construction"
                self._runtime = DriverBackendRuntime(self._inventory,
                    lambda: BrokerMicSink(open_installed_broker_client))
                self._runtime_clean = False
                phase = "runtime start"
                self._check_starting()
                self._runtime.start()
                self._check_starting()
                self._started = True
        except BaseException as error:
            # Retain an explicit owner even when its constructor never returned.
            partial = getattr(error, "cleanup_owner", None)
            if phase == "manifest construction" and self._manifest is None:
                self._manifest, self._manifest_clean = partial, partial is None
                self._construction_unknown = partial is None
            elif phase == "runtime construction" and self._runtime is None:
                self._runtime, self._runtime_clean = partial, partial is None
                self._construction_unknown = partial is None
            self._stop.set()
            self._fail("installed_driver_start_failed")
            try:
                self._signal_runtime_stop()
            except BaseException:
                if not isinstance(error, Exception):
                    raise error
                raise
            if not isinstance(error, Exception):
                raise
            raise InstalledDriverHostError("Installed driver component could not start") from None
        finally:
            with self._lock:
                self._starting = False

    def acquire_sink(self):
        runtime = self._runtime
        if runtime is None or not self.backend.is_available():
            raise InstalledDriverHostError("Installed driver session is unavailable")
        lease = runtime.acquire_sink()
        if self._stop.is_set():
            # Runtime retains the lease; its pending stop owns the cleanup.
            raise InstalledDriverHostError("Installed driver session was cancelled")
        return lease

    def request_stop(self) -> None:
        """UI-safe cancellation signal; final stop/cleanup still runs off-UI."""
        self._stop.set()
        runtime = self._runtime
        if runtime is not None:
            request = getattr(runtime, "request_stop", None)
            if callable(request):
                request()

    def stop(self, timeout: float = 1.0) -> None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 1:
            raise ValueError("Controller deadline must be between zero and one second")
        self.request_stop()  # Admission closes before waiting for start/cleanup.
        deadline = time.monotonic() + timeout
        self._signal_runtime_stop()
        if not self._control.acquire(timeout=max(0, deadline - time.monotonic())):
            self._fail("installed_driver_start_cleanup_pending")
            raise InstalledDriverHostStopError("Installed driver startup is still running")
        try:
            if self._runtime is not None and not self._runtime_clean:
                self._runtime.stop(timeout=max(0, deadline - time.monotonic()))
                if self._runtime.snapshot.cleanup_confirmed is not True:
                    raise InstalledDriverHostStopError("Installed driver runtime cleanup is unconfirmed")
                self._runtime_clean = True
            if self._construction_unknown:
                raise InstalledDriverHostStopError("Installed driver construction ownership is unknown")
            if self._manifest is not None and not self._manifest_clean:
                self._manifest.close()
                if self._manifest.cleanup_confirmed is not True:
                    raise InstalledDriverHostStopError("Installed driver manifest cleanup is unconfirmed")
                self._manifest_clean = True
            with self._lock:
                self._clean = self._runtime_clean and self._manifest_clean
        except BaseException as error:
            self._fail("installed_driver_cleanup_pending")
            if not isinstance(error, Exception):
                raise
            raise InstalledDriverHostStopError("Installed driver cleanup is unconfirmed") from None
        finally:
            self._control.release()
