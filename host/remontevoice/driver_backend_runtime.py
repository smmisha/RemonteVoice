"""Undeployed owner of one muted broker connection across phone sessions.

Native endpoint inventory belongs to its own worker. Cached readiness keeps the
oldest underlying observation time; reading it never refreshes native liveness.
Phone sessions borrow exclusive sinks; releasing one confirms mute, but only the
runtime closes the underlying connection. No routes/defaults are changed here.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from .audio_backend import DriverAudioBackend, DriverBackendSnapshot
from .broker_mic_sink import BrokerSinkSnapshot
from .windows_driver_endpoints import DriverEndpointPair


class DriverRuntimeError(RuntimeError):
    pass


class DriverRuntimeStopError(DriverRuntimeError):
    """Ownership is retained; replacement is prohibited until process cleanup."""


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Runtime deadline must be between zero and one second')


@dataclass(frozen=True)
class DriverRuntimeSnapshot:
    started: bool
    stopping: bool
    session_active: bool
    cleanup_confirmed: bool
    error: str | None


def _unavailable() -> DriverBackendSnapshot:
    return DriverBackendSnapshot(1, False, False, '', '', 0.0)


class DriverBackendRuntime:
    def __init__(self, inventory, sink_factory, *, clock=time.monotonic):
        if not callable(getattr(inventory, 'observe', None)) or not callable(sink_factory) or not callable(clock):
            raise TypeError('Trusted endpoint inventory and inert broker factory required')
        self._inventory, self._factory, self._clock = inventory, sink_factory, clock
        self._lock = threading.RLock()
        self._control = threading.Lock()
        self._stop = threading.Event()
        self._used = False
        self._starting = False
        self._construction_unknown = False
        self._sink = self._lease = None
        self._workers = []
        self._pair = None
        self._inventory_at = 0.0
        self._inventory_used = False
        self._inventory_clean = True
        self._inventory_fatal: BaseException | None = None
        self._error = None
        self._cached = _unavailable()
        self._guard_health = (False, 0.0, 0.0, 0.0)
        self._health_sample_sequence = 0
        self._health_published_sequence = 0
        self.backend = DriverAudioBackend(self.observe, clock=clock, max_age=0.5)

    def observe(self) -> DriverBackendSnapshot:
        """Atomic cache read only: no locks, native I/O or timestamp renewal."""
        return self._cached

    def guard_is_available(self) -> bool:
        """Cached session-continuity check; never use to activate device routes.

        During an actual control or phone-lease release, a fresh authenticated
        connection may be healthy while BEGIN/END is awaiting confirmation. Grace never
        refreshes source timestamps and cannot exceed the original control's
        deadline. Startup, route activation and new leases remain strictly ready.
        """
        # A lease can begin END or finish while these cache-only reads run.
        # Retry a changed lifecycle observation instead of stopping the Host on
        # a mixture of pre-release flags and post-release readiness. Never wait
        # for a lock/native call or extend a transition's original deadline.
        for _ in range(3):
            if self._stop.is_set() or self._error is not None:
                return False
            lease = self._lease
            state = lease._guard_state() if lease is not None else None
            available = self._guard_observation(lease)
            if self._stop.is_set() or self._error is not None:
                return False
            if self._lease is lease and (lease is None or lease._guard_state() == state):
                return available
        return False

    def _guard_observation(self, lease) -> bool:
        if lease is not None:
            if lease._released:
                # A clean release may finish after this lock-free read began.
                return self._lease is not lease and self.backend.is_available()
            if lease._closing:
                deadline = lease._release_deadline
            elif not lease._active():
                return False
            elif lease._control_inflight:
                deadline = lease._transition_deadline
            else:
                return self.backend.is_available()
            try:
                now = self._clock()
                healthy, inventory, observed, progress = self._guard_health
                return bool(lease._started and healthy
                            and 0 <= deadline - now <= 1
                            and now < deadline
                            and all(0 <= now - stamp < 0.5 for stamp in (inventory, observed, progress)))
            except Exception:
                return False
        return self.backend.is_available()

    @property
    def snapshot(self) -> DriverRuntimeSnapshot:
        with self._lock:
            clean = (self._stop.is_set() and not self._starting and self._sink is None and not self._construction_unknown
                     and (not self._inventory_used or self._inventory_clean)
                     and self._inventory_fatal is None
                     and not any(w.is_alive() for w in self._workers))
            return DriverRuntimeSnapshot(self._used, self._stop.is_set(), self._lease is not None,
                                         clean, self._error)

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._error = self._error or reason
            self._cached = _unavailable()
            self._guard_health = (False, 0.0, 0.0, 0.0)

    def start(self) -> None:
        """Off-event-loop startup; the underlying connection is always muted."""
        with self._lock:
            if self._used:
                raise DriverRuntimeError('Driver runtime is single-use')
            self._used = True
            self._starting = True
        try:
            with self._control:
                self._start_owned()
        finally:
            with self._lock:
                self._starting = False

    def _start_owned(self):
        try:
            if self._stop.is_set():
                raise DriverRuntimeError('Runtime startup cancelled')
            # Hold ownership before starting any worker, including constructor
            # failures that return an explicit native cleanup owner.
            try:
                self._sink = self._factory()
            except BaseException as error:
                self._sink = getattr(error, 'cleanup_owner', None)
                self._construction_unknown = self._sink is None
                raise
            if self._sink is None:
                self._construction_unknown = True
                raise DriverRuntimeError('Broker factory returned no owner')
            if self._stop.is_set():
                raise DriverRuntimeError('Runtime startup cancelled')
            self._sink.start()
            if self._sink.set_enabled(False, timeout=1.0) is not True:
                raise DriverRuntimeError('Muted broker startup not confirmed')
            status = self._sink.snapshot
            if (type(status) is not BrokerSinkSnapshot or status.enabled is not False
                    or status.writer_ready is not False or not self._sink.is_available()):
                raise DriverRuntimeError('Broker did not start muted')
            with self._lock:
                if self._stop.is_set():
                    raise DriverRuntimeError('Runtime startup cancelled')
                self._workers = [threading.Thread(target=self._inventory_worker, name='rv-driver-inventory', daemon=True),
                                 threading.Thread(target=self._health_worker, name='rv-driver-health', daemon=True)]
                for worker in self._workers:
                    worker.start()
        except BaseException:
            self._fail('runtime_start_failed')
            # Caller retains this runtime and must call stop to confirm cleanup.
            raise

    def _inventory_worker(self) -> None:
        try:
            while not self._stop.is_set():
                started = self._clock()
                with self._lock:
                    self._inventory_used = True
                    self._inventory_clean = False
                primary = None
                try:
                    pair = self._inventory.observe()
                except BaseException as error:
                    primary = error
                finally:
                    # The inventory owns its COM apartment/handles. Observe its
                    # cached ownership result on this same worker; never attempt
                    # cross-thread COM cleanup after an observation failure.
                    try:
                        self._inventory_clean = self._inventory.cleanup_confirmed is True
                    except BaseException as error:
                        self._inventory_clean = False
                        if primary is None or isinstance(primary, Exception) and not isinstance(error, Exception):
                            primary = error
                if primary is not None:
                    raise primary
                if not self._inventory_clean:
                    raise DriverRuntimeError('Endpoint inventory cleanup unconfirmed')
                now = self._clock()
                if (type(pair) is not DriverEndpointPair or not 0 <= now - started < 0.5
                        or any(type(v) is not str or not v.strip() or len(v) > 1024
                               for v in (pair.capture_endpoint_id, pair.microphone_endpoint_id))
                        or pair.capture_endpoint_id.casefold() == pair.microphone_endpoint_id.casefold()):
                    raise DriverRuntimeError('Invalid endpoint observation')
                with self._lock:
                    if self._pair is not None and self._pair != pair:
                        raise DriverRuntimeError('Driver endpoint identities changed')
                    self._pair, self._inventory_at = pair, started
                self._stop.wait(0.25)
        except BaseException as error:
            if not isinstance(error, Exception):
                self._inventory_fatal = error
            self._fail('endpoint_inventory_failed')

    def _health_worker(self) -> None:
        try:
            while not self._stop.is_set():
                self._refresh_health()
                self._stop.wait(0.04)
        except BaseException:
            self._fail('broker_health_failed')
        finally:
            self._cached = _unavailable()

    def _refresh_health(self):
        # Only cached sink data: this may run after an off-loop control has
        # completed so its immediate readiness check observes that confirmation.
        with self._lock:
            self._health_sample_sequence += 1
            sequence = self._health_sample_sequence
            pair, inventory_at, sink = self._pair, self._inventory_at, self._sink
        if pair is None or sink is None:
            return
        status = sink.snapshot
        now = self._clock()
        stamps = (inventory_at, status.observed_at, status.progress_at)
        transport = (type(status) is BrokerSinkSnapshot
                 and all(type(t) in (int, float) and math.isfinite(t) and 0 <= now - t < 0.5 for t in stamps)
                 and type(status.enabled) is bool and type(status.writer_ready) is bool
                 and status.worker_alive is True and status.cleanup_confirmed is False
                 and status.error is None)
        valid = transport and sink.is_available() is True
        with self._lock:
            # The monitor and a completed control can sample concurrently. An
            # older pending-END result must not replace a newer confirmed mute.
            if (sequence >= self._health_published_sequence
                    and not self._stop.is_set() and self._error is None):
                self._health_published_sequence = sequence
                self._guard_health = (transport, *stamps)
                self._cached = (DriverBackendSnapshot(1, valid, valid,
                    pair.capture_endpoint_id, pair.microphone_endpoint_id, min(stamps),
                    status.enabled, status.writer_ready, False) if valid else _unavailable())

    def acquire_sink(self):
        """Off-loop exclusive factory for DriverAudioBridge; does not reconnect."""
        with self._lock:
            if self._stop.is_set() or self._error is not None or self._lease is not None or not self.backend.is_available():
                raise DriverRuntimeError('Driver session unavailable')
            self._lease = _SinkLease(self)
            return self._lease

    def request_stop(self) -> None:
        """Close admission immediately; no native I/O, joins or cleanup here."""
        self._stop.set()
        self._cached = _unavailable()
        self._guard_health = (False, 0.0, 0.0, 0.0)

    def stop(self, timeout: float = 1.0) -> None:
        _timeout(timeout)
        self.request_stop()  # Close ingress before waiting for a pending enable.
        deadline = time.monotonic() + timeout
        if not self._control.acquire(timeout=max(0, deadline - time.monotonic())):
            self._fail('control_cleanup_pending')
            raise DriverRuntimeStopError('Driver control is still running')
        try:
            sink = self._sink
            if sink is not None:
                sink.stop(timeout=max(0, deadline - time.monotonic()))
                if sink.snapshot.cleanup_confirmed is not True:
                    raise DriverRuntimeStopError('Broker connection cleanup unconfirmed')
                with self._lock:
                    self._sink = None
                    if self._lease is not None:
                        self._lease._released = True
                        self._lease = None
        except BaseException:
            self._fail('broker_cleanup_pending')
            raise
        finally:
            self._control.release()
            for worker in self._workers:
                if worker.ident is not None and worker is not threading.current_thread():
                    worker.join(max(0, deadline - time.monotonic()))
        if any(w.is_alive() for w in self._workers):
            self._fail('inventory_cleanup_pending')
            raise DriverRuntimeStopError('Endpoint monitor is still running')
        if self._construction_unknown:
            raise DriverRuntimeStopError('Broker construction ownership is unknown')
        if self._inventory_used:
            try:
                self._inventory_clean = self._inventory.cleanup_confirmed is True
            except BaseException as error:
                self._inventory_clean = False
                if not isinstance(error, Exception) and self._inventory_fatal is None:
                    self._inventory_fatal = error
        if self._inventory_fatal is not None:
            raise DriverRuntimeStopError('Endpoint inventory failed fatally') from self._inventory_fatal
        if self._inventory_used and not self._inventory_clean:
            raise DriverRuntimeStopError('Endpoint inventory native cleanup unconfirmed')


class _SinkLease:
    def __init__(self, runtime):
        self._runtime = runtime
        self._started = self._closing = self._released = False
        self._control_inflight = False
        self._transition_deadline = 0.0
        self._release_deadline = 0.0

    def _guard_state(self):
        return (self._started, self._closing, self._released, self._control_inflight,
                self._transition_deadline, self._release_deadline)

    def _active(self):
        runtime = self._runtime
        return (runtime._lease is self and not self._closing and not self._released
                and not runtime._stop.is_set() and runtime._error is None)

    @property
    def snapshot(self):
        runtime = self._runtime
        with runtime._lock:
            status = runtime._sink.snapshot if runtime._sink is not None else None
            return BrokerSinkSnapshot(0, 0, 0, bool(status and status.enabled),
                bool(status and status.writer_ready), self._active(), self._released,
                runtime._error, status.observed_at if status else 0.0, status.progress_at if status else 0.0)

    def start(self):
        with self._runtime._lock:
            if self._started or not self._active() or not self._runtime.backend.is_available():
                raise DriverRuntimeError('Driver sink lease unavailable')
            self._started = True

    def is_available(self):
        return bool(self._started and self._active() and self._runtime.backend.is_available())

    def set_enabled(self, enabled: bool, timeout: float = 1.0):
        _timeout(timeout)
        if type(enabled) is not bool:
            raise ValueError('Expected an explicit microphone state')
        runtime = self._runtime
        deadline = time.monotonic() + timeout
        if not runtime._control.acquire(timeout=timeout):
            runtime._fail('driver_control_timeout')
            return False
        try:
            if (not self._started or not self._active()
                    or enabled and not runtime.backend.is_available()):
                return False
            remaining = max(0, deadline - time.monotonic())
            self._transition_deadline = runtime._clock() + remaining
            self._control_inflight = True
            result = runtime._sink.set_enabled(enabled, timeout=remaining) is True
            if runtime._clock() >= self._transition_deadline:
                runtime._fail('driver_control_timeout')
                return False
            if not result:
                runtime._fail('driver_control_failed')
            runtime._refresh_health()
            return result and self._active()
        except BaseException:
            runtime._fail('driver_control_failed')
            raise
        finally:
            self._control_inflight = False
            self._transition_deadline = 0.0
            runtime._control.release()

    def submit(self, payload):
        runtime = self._runtime
        with runtime._lock:
            return bool(self._started and self._active() and runtime.backend.is_available()
                        and runtime._sink is not None
                        and runtime._sink.submit(payload))

    def stop(self, timeout: float = 1.0):
        _timeout(timeout)
        runtime = self._runtime
        with runtime._lock:
            if self._released:
                return
            if not self._closing:
                self._release_deadline = runtime._clock() + timeout
            self._closing = True  # Reject new enable/PCM before joining a control.
        deadline = time.monotonic() + timeout
        if not runtime._control.acquire(timeout=timeout):
            runtime._fail('lease_release_pending')
            raise DriverRuntimeStopError('Lease control is still running')
        try:
            if self._released:
                return
            sink = runtime._sink
            if sink is None or sink.set_enabled(False, timeout=max(0, deadline - time.monotonic())) is not True:
                raise DriverRuntimeStopError('Lease mute not confirmed')
            status = sink.snapshot
            if not sink.is_available() or status.enabled is not False or status.writer_ready is not False:
                raise DriverRuntimeStopError('Lease writer cleanup unconfirmed')
            # Publish confirmed muted readiness before retiring the lease, so
            # the UI health check never sees an empty lease with a pending cache.
            runtime._refresh_health()
            with runtime._lock:
                if runtime._lease is self:
                    runtime._lease = None
                self._released = True
        except BaseException:
            runtime._fail('lease_cleanup_pending')
            raise
        finally:
            runtime._control.release()
