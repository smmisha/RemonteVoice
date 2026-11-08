import threading
import time
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from remotevoice.broker_mic_sink import BrokerSinkSnapshot
from remotevoice.driver_backend_runtime import DriverBackendRuntime, DriverRuntimeError, DriverRuntimeStopError
from remotevoice.driver_audio_bridge import DriverAudioBridge
from remotevoice.windows_driver_endpoints import DriverEndpointPair


PAIR = DriverEndpointPair('render-id', 'microphone-id')


def wait_for(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Runtime did not reach expected state')
        time.sleep(0.002)


class Sink:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = self.closed = self.enabled = False
        self.available = True
        self.controls = []
        self.frames = []
        self.stop_calls = 0
        self.stop_error = self.control_hook = None
        self.stamp = None

    @property
    def snapshot(self):
        now = self.clock() if self.stamp is None else self.stamp
        return BrokerSinkSnapshot(0, 0, len(self.frames), self.enabled, self.enabled,
                                  self.started and not self.closed, self.closed, None, now, now)

    def start(self):
        self.started = True

    def is_available(self):
        return self.available and self.started and not self.closed

    def set_enabled(self, enabled, timeout):
        self.controls.append(enabled)
        if self.control_hook:
            self.control_hook(enabled)
        if not self.is_available():
            return False
        self.enabled = enabled
        return True

    def submit(self, payload):
        if not self.enabled:
            return False
        self.frames.append(payload)
        return True

    def stop(self, timeout):
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error
        self.enabled = False
        self.closed = True


class DriverBackendRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        self.inventory = Mock()
        self.inventory.cleanup_confirmed = True
        self.inventory.observe.return_value = PAIR
        self.factory = Mock(return_value=self.sink)
        self.runtime = DriverBackendRuntime(self.inventory, self.factory)
        self.release = []

    def tearDown(self):
        for event in self.release:
            event.set()
        self.sink.stop_error = None
        self.runtime.stop()
        self.assertTrue(self.runtime.snapshot.cleanup_confirmed)

    def start(self):
        self.runtime.start()
        wait_for(self.runtime.backend.is_available)

    def test_constructor_inert_start_muted_and_cached_readiness_never_inventories(self):
        self.factory.assert_not_called()
        self.inventory.observe.assert_not_called()
        self.start()
        self.assertEqual(self.sink.controls, [False])
        self.assertFalse(self.runtime.observe().mic_requested)
        self.assertTrue(self.runtime.backend.matches_endpoints(*('render-id', 'microphone-id')))
        snapshot = self.runtime.observe()
        with self.runtime._lock:
            self.assertIs(self.runtime.observe(), snapshot)
        self.factory.assert_called_once_with()
        with self.assertRaises(DriverRuntimeError):
            self.runtime.start()

    def test_two_phone_sessions_reuse_one_connection_and_release_only_after_mute(self):
        self.start()
        for _ in range(2):
            lease = self.runtime.acquire_sink()
            lease.start()
            with self.assertRaises(DriverRuntimeError):
                self.runtime.acquire_sink()
            self.assertTrue(lease.set_enabled(True))
            self.assertTrue(lease.is_available())
            self.assertTrue(lease.submit(b'synthetic'))
            lease.stop()
            self.assertTrue(lease.snapshot.cleanup_confirmed)
            self.assertFalse(lease.submit(b'old frame'))
            self.assertFalse(lease.set_enabled(True))
            self.assertFalse(self.sink.enabled)
        self.factory.assert_called_once_with()
        self.assertEqual(self.sink.stop_calls, 0)
        self.runtime.stop()
        self.assertEqual(self.sink.stop_calls, 1)

    def test_stop_request_closes_all_admission_without_native_control_or_cleanup(self):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.assertTrue(lease.set_enabled(True))
        controls = list(self.sink.controls)
        self.runtime.request_stop()
        self.assertFalse(self.runtime.backend.is_available())
        self.assertFalse(self.runtime.guard_is_available())
        self.assertFalse(lease.submit(b'late frame'))
        self.assertFalse(lease.set_enabled(True))
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        self.assertEqual(self.sink.controls, controls)
        self.assertEqual(self.sink.stop_calls, 0)
        self.assertFalse(self.runtime.snapshot.cleanup_confirmed)
        self.runtime.stop()
        self.assertEqual(self.sink.stop_calls, 1)
        self.assertTrue(self.runtime.snapshot.cleanup_confirmed)

    def test_actual_bridge_start_stop_reborrows_without_a_second_broker_factory(self):
        self.start()
        for _ in range(2):
            bridge = DriverAudioBridge('render-id', 'microphone-id',
                MagicMock(spec=asyncio.AbstractEventLoop),
                SimpleNamespace(close=AsyncMock(), send=AsyncMock()),
                MagicMock(spec=asyncio.Lock), Mock(),
                driver_backend=self.runtime.backend, sink_factory=self.runtime.acquire_sink)
            with patch('remotevoice.driver_audio_bridge.sc.get_microphone', return_value=SimpleNamespace(
                    id='render-id', name='RemoteVoice System Audio', isloopback=True)), patch.object(bridge, '_launch_audio_workers'):
                bridge.start()
            self.assertFalse(bridge.microphone_enabled)
            bridge.set_microphone_enabled(True)
            self.assertTrue(bridge.microphone_enabled)
            bridge.stop()
            self.assertFalse(self.sink.enabled)
            self.assertFalse(self.runtime.snapshot.session_active)
        self.factory.assert_called_once_with()
        self.assertEqual(self.sink.stop_calls, 0)

    def test_inventory_and_sink_timestamp_age_are_not_renewed_by_cached_reads(self):
        self.start()
        old = time.monotonic() - 1
        self.sink.stamp = old
        wait_for(lambda: not self.runtime.backend.is_available())
        self.assertFalse(self.runtime.observe().broker_authenticated)
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()

    def test_expired_inventory_cannot_admit_enable_or_pcm_on_existing_lease(self):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.assertTrue(lease.set_enabled(True))
        with self.runtime._lock:
            self.runtime._inventory_at = time.monotonic() - 1
        self.runtime._refresh_health()
        self.assertFalse(lease.set_enabled(True))
        self.assertFalse(lease.submit(b'old endpoint'))
        lease.stop()  # Explicit mute remains possible even with unavailable IDs.

    def test_constructor_without_cleanup_owner_never_claims_clean(self):
        runtime = DriverBackendRuntime(self.inventory, Mock(side_effect=RuntimeError('partial open')))
        with self.assertRaises(RuntimeError):
            runtime.start()
        with self.assertRaises(DriverRuntimeStopError):
            runtime.stop()
        self.assertFalse(runtime.snapshot.cleanup_confirmed)
        with self.assertRaises(DriverRuntimeError):
            runtime.start()

    def test_disappearing_or_changed_endpoints_stop_readiness_and_block_replacement(self):
        self.start()
        self.inventory.observe.side_effect = RuntimeError('device disappeared')
        wait_for(lambda: self.runtime.snapshot.error == 'endpoint_inventory_failed')
        self.assertFalse(self.runtime.backend.is_available())
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        self.factory.assert_called_once_with()

    def test_endpoint_id_change_is_not_silently_adopted(self):
        self.start()
        self.inventory.observe.return_value = DriverEndpointPair('replacement', 'microphone-id')
        wait_for(lambda: self.runtime.snapshot.error == 'endpoint_inventory_failed')
        self.assertFalse(self.runtime.backend.matches_endpoints('replacement', 'microphone-id'))

    def test_broker_failure_invalidates_backend_without_reconnecting(self):
        self.start()
        self.sink.available = False
        wait_for(lambda: not self.runtime.backend.is_available())
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        self.factory.assert_called_once_with()

    def pending_control(self, *, timeout=1.0):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)
        def control(enabled):
            if enabled:
                self.sink.available = False  # BEGIN accepted, writer not ready.
                entered.set()
                release.wait(1)
                self.sink.available = True
        self.sink.control_hook = control
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(lease.set_enabled(True, timeout=timeout)))
        worker.start()
        self.assertTrue(entered.wait(1))
        wait_for(lambda: not self.runtime.backend.is_available())
        return lease, release, worker, outcome

    def test_pending_explicit_begin_keeps_continuity_guard_but_never_route_readiness(self):
        lease, release, worker, outcome = self.pending_control()
        self.assertTrue(lease._control_inflight)
        self.assertTrue(self.runtime.guard_is_available())
        self.assertFalse(self.runtime.backend.is_available())
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [True])
        self.assertFalse(lease._control_inflight)
        self.assertEqual(lease._transition_deadline, 0.0)
        self.assertTrue(self.runtime.backend.is_available())

    def test_pending_begin_stale_progress_cannot_use_transition_deadline_as_health(self):
        _, release, worker, _ = self.pending_control()
        self.sink.stamp = time.monotonic() - 1
        wait_for(lambda: not self.runtime.guard_is_available())
        self.assertFalse(self.runtime.backend.is_available())
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_transition_deadline_expires_without_renewal_and_late_ready_is_rejected(self):
        lease, release, worker, outcome = self.pending_control(timeout=0.08)
        original_deadline = lease._transition_deadline
        wait_for(lambda: time.monotonic() >= original_deadline)
        self.runtime._refresh_health()  # Fresh metadata cannot renew the control.
        self.assertEqual(lease._transition_deadline, original_deadline)
        self.assertFalse(self.runtime.guard_is_available())
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [False])
        self.assertEqual(self.runtime.snapshot.error, 'driver_control_timeout')
        self.assertFalse(self.runtime.guard_is_available())

    def test_failed_control_ack_clears_transition_and_never_gets_health_grace(self):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.sink.control_hook = lambda enabled: setattr(self.sink, 'available', False)
        self.assertFalse(lease.set_enabled(True))
        self.assertFalse(lease._control_inflight)
        self.assertEqual(lease._transition_deadline, 0.0)
        self.assertFalse(self.runtime.guard_is_available())

    def test_runtime_stop_wins_over_live_transition_and_late_enable(self):
        lease, release, worker, outcome = self.pending_control()
        self.assertTrue(self.runtime.guard_is_available())
        with self.assertRaises(DriverRuntimeStopError):
            self.runtime.stop(timeout=0.01)
        self.assertFalse(self.runtime.guard_is_available())
        self.assertFalse(lease.submit(b'late PCM'))
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [False])
        self.runtime.stop()
        self.assertFalse(self.sink.enabled)

    def test_failed_lease_mute_retains_ownership_and_blocks_new_phone(self):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.assertTrue(lease.set_enabled(True))
        self.sink.available = False
        with self.assertRaises(DriverRuntimeStopError):
            lease.stop()
        self.assertFalse(lease.snapshot.cleanup_confirmed)
        self.assertTrue(self.runtime.snapshot.session_active)
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        self.sink.available = True
        lease.stop()
        self.assertTrue(lease.snapshot.cleanup_confirmed)
        # A cleanup failure leaves readiness failed even after ownership release.
        self.assertFalse(self.runtime.backend.is_available())

    def pending_release(self, timeout=0.8):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.assertTrue(lease.set_enabled(True))
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)

        def control(enabled):
            if not enabled:
                self.sink.available = False  # END pending; transport still progresses.
                entered.set()
                release.wait(1)
                self.sink.available = True

        self.sink.control_hook = control
        outcome = []

        def stop():
            try:
                lease.stop(timeout=timeout)
                outcome.append(None)
            except BaseException as error:
                outcome.append(error)

        worker = threading.Thread(target=stop)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.assertTrue(entered.wait(1))
        wait_for(lambda: not self.runtime.backend.is_available())
        return lease, release, worker, outcome

    def test_pending_release_preserves_guard_continuity_without_admitting_pcm_or_phone(self):
        lease, release, worker, outcome = self.pending_release()
        self.assertTrue(self.runtime.guard_is_available())
        self.assertFalse(lease.submit(b'late PCM'))
        with self.assertRaises(DriverRuntimeError):
            self.runtime.acquire_sink()
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [None])
        self.assertTrue(lease.snapshot.cleanup_confirmed)
        self.assertTrue(self.runtime.guard_is_available())
        self.assertFalse(self.runtime.snapshot.session_active)
        self.assertFalse(self.runtime.observe().mic_requested)

    def test_pending_release_grace_never_hides_stale_transport_or_runtime_stop(self):
        _, release, worker, outcome = self.pending_release()
        self.assertTrue(self.runtime.guard_is_available())
        self.sink.stamp = time.monotonic() - 1
        wait_for(lambda: not self.runtime.guard_is_available())
        self.sink.stamp = None
        wait_for(self.runtime.guard_is_available)
        self.runtime.request_stop()
        self.assertFalse(self.runtime.guard_is_available())
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [None])
        self.assertFalse(self.runtime.backend.is_available())

    def test_pending_release_cannot_renew_its_original_guard_deadline(self):
        lease, release, worker, outcome = self.pending_release(timeout=0.08)
        self.assertTrue(self.runtime.guard_is_available())
        deadline = lease._release_deadline
        wait_for(lambda: time.monotonic() >= deadline)
        self.runtime._refresh_health()
        self.assertEqual(lease._release_deadline, deadline)
        self.assertFalse(self.runtime.guard_is_available())
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [None])

    def assert_guard_tolerates_release_between_reads(self, *, before_active):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        self.assertTrue(lease.set_enabled(True))
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)
        outcomes = []

        def control(enabled):
            if not enabled:
                self.sink.available = False
                entered.set()
                release.wait(2)
                self.sink.available = True

        def end_phone():
            try:
                lease.stop(timeout=0.8)
                outcomes.append(None)
            except BaseException as error:
                outcomes.append(error)

        self.sink.control_hook = control
        worker = threading.Thread(target=end_phone)
        target, attribute = ((lease, '_active') if before_active else
                             (self.runtime.backend, 'is_available'))
        original = getattr(target, attribute)
        caller = threading.get_ident()
        triggered = False

        def interrupted_read():
            nonlocal triggered
            if threading.get_ident() == caller and not triggered:
                triggered = True
                worker.start()
                self.assertTrue(entered.wait(1))
                # Wait through the real health worker, without calling the
                # patched backend method recursively.
                wait_for(lambda: not self.runtime.observe().broker_authenticated)
            return original()

        try:
            with patch.object(target, attribute, side_effect=interrupted_read):
                self.assertTrue(self.runtime.guard_is_available())
            self.assertTrue(triggered)
            self.assertFalse(lease.submit(b'late PCM'))
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [None])
        self.assertTrue(lease.snapshot.cleanup_confirmed)
        self.assertTrue(self.runtime.guard_is_available())

    def test_guard_rechecks_release_started_after_reading_closing_flag(self):
        self.assert_guard_tolerates_release_between_reads(before_active=True)

    def test_guard_rechecks_release_started_after_active_check_before_backend_read(self):
        self.assert_guard_tolerates_release_between_reads(before_active=False)

    def test_older_health_sample_cannot_overwrite_confirmed_release_readiness(self):
        lease, release, worker, outcome = self.pending_release()
        sampled, publish = threading.Event(), threading.Event()
        self.release.append(publish)
        original = self.sink.is_available
        captured = False

        def delayed_observation():
            nonlocal captured
            available = original()
            if threading.current_thread().name == 'rv-driver-health' and not captured:
                captured = True
                sampled.set()
                publish.wait(2)
            return available

        try:
            with patch.object(self.sink, 'is_available', side_effect=delayed_observation):
                self.assertTrue(sampled.wait(1))
                release.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome, [None])
                self.assertTrue(lease.snapshot.cleanup_confirmed)
                self.assertTrue(self.runtime.guard_is_available())
                # Pause at the health worker's next wait, after its attempted
                # stale publication, to inspect that exact state deterministically.
                published = threading.Event()
                resume = threading.Event()
                self.release.append(resume)
                original_wait = self.runtime._stop.wait

                def observe_publication(timeout):
                    if threading.current_thread().name == 'rv-driver-health':
                        published.set()
                        resume.wait(2)
                    return original_wait(timeout)

                with patch.object(self.runtime._stop, 'wait', side_effect=observe_publication):
                    publish.set()
                    self.assertTrue(published.wait(1))
                    try:
                        self.assertTrue(self.runtime.guard_is_available())
                        self.assertTrue(self.runtime.backend.is_available())
                    finally:
                        resume.set()
        finally:
            release.set()
            publish.set()
            worker.join(2)

    def test_newer_pending_sample_cannot_suppress_confirmed_release_publication(self):
        lease, release, worker, outcome = self.pending_release()
        confirmed_sample, publish_confirmed = threading.Event(), threading.Event()
        later_sample, publish_later = threading.Event(), threading.Event()
        self.release.extend((publish_confirmed, publish_later))
        original_clock = self.runtime._clock
        captured = False

        def pause_sample():
            nonlocal captured
            if threading.current_thread() is worker and not captured:
                captured = True
                confirmed_sample.set()
                publish_confirmed.wait(2)
            elif (threading.current_thread().name == 'rv-driver-health'
                  and confirmed_sample.is_set()):
                later_sample.set()
                publish_later.wait(2)
            return original_clock()

        try:
            with patch.object(self.runtime, '_clock', side_effect=pause_sample):
                release.set()
                self.assertTrue(confirmed_sample.wait(1))
                self.assertTrue(later_sample.wait(1))
                publish_confirmed.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome, [None])
                self.assertTrue(lease.snapshot.cleanup_confirmed)
                self.assertTrue(self.runtime.guard_is_available())
        finally:
            release.set()
            publish_confirmed.set()
            publish_later.set()
            worker.join(2)

    def test_runtime_stop_preserves_failed_sink_for_explicit_cleanup_retry(self):
        self.start()
        self.sink.stop_error = RuntimeError('native ownership retained')
        with self.assertRaises(RuntimeError):
            self.runtime.stop()
        self.assertFalse(self.runtime.snapshot.cleanup_confirmed)
        self.assertIs(self.runtime._sink, self.sink)
        self.sink.stop_error = None
        self.runtime.stop()
        self.assertTrue(self.runtime.snapshot.cleanup_confirmed)
        self.factory.assert_called_once_with()

    def test_inventory_pending_native_cleanup_is_not_hidden_by_worker_exit(self):
        inventory = Mock(cleanup_confirmed=True)
        def observe():
            inventory.cleanup_confirmed = False
            raise RuntimeError('retained native COM reference')
        inventory.observe.side_effect = observe
        runtime = DriverBackendRuntime(inventory, lambda: Sink())
        runtime.start()
        wait_for(lambda: runtime.snapshot.error == 'endpoint_inventory_failed')
        with self.assertRaisesRegex(DriverRuntimeStopError, 'native cleanup unconfirmed'):
            runtime.stop()
        self.assertFalse(any(worker.is_alive() for worker in runtime._workers))
        self.assertFalse(runtime.snapshot.cleanup_confirmed)
        self.assertIs(runtime._inventory, inventory)
        self.assertIsNone(runtime._sink)

    def test_inventory_fatal_is_retained_even_when_cached_native_cleanup_is_true(self):
        class InventoryFatal(BaseException):
            pass
        fatal = InventoryFatal()
        inventory = Mock(cleanup_confirmed=True)
        inventory.observe.side_effect = fatal
        runtime = DriverBackendRuntime(inventory, lambda: Sink())
        runtime.start()
        wait_for(lambda: runtime.snapshot.error == 'endpoint_inventory_failed')
        with self.assertRaisesRegex(DriverRuntimeStopError, 'failed fatally') as raised:
            runtime.stop()
        self.assertIs(raised.exception.__cause__, fatal)
        self.assertIs(runtime._inventory_fatal, fatal)
        self.assertFalse(runtime.snapshot.cleanup_confirmed)
        self.assertFalse(any(worker.is_alive() for worker in runtime._workers))

    def test_fatal_observe_is_not_replaced_by_a_failing_cleanup_observation(self):
        class InventoryFatal(BaseException):
            pass
        fatal = InventoryFatal()
        class Inventory:
            def observe(self):
                raise fatal
            @property
            def cleanup_confirmed(self):
                raise RuntimeError('cached ownership observation failed')
        runtime = DriverBackendRuntime(Inventory(), lambda: Sink())
        runtime.start()
        wait_for(lambda: runtime.snapshot.error == 'endpoint_inventory_failed')
        with self.assertRaises(DriverRuntimeStopError) as raised:
            runtime.stop()
        self.assertIs(raised.exception.__cause__, fatal)
        self.assertFalse(runtime.snapshot.cleanup_confirmed)

    def test_stalled_inventory_stays_owned_and_stop_is_bounded(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)
        def observe():
            entered.set()
            release.wait(2)
            return PAIR
        self.inventory.observe.side_effect = observe
        self.assertTrue(entered.wait(1))
        wait_for(lambda: not self.runtime.backend.is_available())
        started = time.monotonic()
        with self.assertRaises(DriverRuntimeStopError):
            self.runtime.stop(timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(self.runtime.snapshot.cleanup_confirmed)
        release.set()
        self.runtime.stop()

    def test_lease_stop_blocks_pcm_immediately_and_late_enable_cannot_reopen(self):
        self.start()
        lease = self.runtime.acquire_sink()
        lease.start()
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)
        def control(enabled):
            if enabled:
                entered.set()
                release.wait(1)
        self.sink.control_hook = control
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(lease.set_enabled(True)))
        worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(DriverRuntimeStopError):
            lease.stop(timeout=0.01)
        self.assertFalse(lease.submit(b'late'))
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [False])
        lease.stop()
        self.assertFalse(self.sink.enabled)
        self.assertTrue(lease.snapshot.cleanup_confirmed)

    def test_stop_during_factory_cannot_report_clean_or_start_late_connection(self):
        entered, release = threading.Event(), threading.Event()
        self.release.append(release)
        def factory():
            entered.set()
            release.wait(1)
            return self.sink
        self.runtime = DriverBackendRuntime(self.inventory, factory)
        errors = []
        def start():
            try:
                self.runtime.start()
            except DriverRuntimeError as error:
                errors.append(error)
        worker = threading.Thread(target=start)
        worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(DriverRuntimeStopError):
            self.runtime.stop(timeout=0.01)
        self.assertFalse(self.runtime.snapshot.cleanup_confirmed)
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.sink.started)
        self.runtime.stop()


if __name__ == '__main__':
    unittest.main()
