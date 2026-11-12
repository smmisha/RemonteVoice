"""Capture-owner observation against inert Python recorders, never audio devices."""
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from remontevoice import capture_wait_timing as wait


MONITORING_AVAILABLE = (
    sys.implementation.name == 'cpython' and sys.version_info >= (3, 14)
    and hasattr(sys, 'monitoring')
)


class PeriodSource:
    @property
    def deviceperiod(self):
        self.period_calls += 1
        if self.on_period is not None:
            self.on_period()
        if self.period_error is not None:
            raise self.period_error
        return (.01, .004)


class FakeRecorder(PeriodSource):
    def __init__(self):
        self.result = object()
        self.period_calls = 0
        self.chunk_calls = 0
        self.period_error = None
        self.chunk_error = None
        self.on_period = None
        self.sleep_seconds = 0

    def _record_chunk(self):
        self.chunk_calls += 1
        self.deviceperiod
        time.sleep(self.sleep_seconds)
        if self.chunk_error is not None:
            raise self.chunk_error
        return self.result


class CaptureWaitCapabilityTests(unittest.TestCase):
    def test_construction_does_not_evaluate_property_or_execute_recorder(self):
        recorder = FakeRecorder()
        recorder.period_error = RuntimeError('property must not be read during setup')
        observed = wait.CaptureWaitTiming(recorder)
        observed.close()
        self.assertEqual((recorder.period_calls, recorder.chunk_calls), (0, 0))

    def test_pre314_runtime_soft_skips_without_claiming_a_monitoring_slot(self):
        recorder = FakeRecorder()
        monitoring = Mock()
        runtime = SimpleNamespace(implementation=SimpleNamespace(name='cpython'),
                                  version_info=(3, 13, 9), monitoring=monitoring)
        with patch.object(wait, 'sys', runtime):
            with wait.CaptureWaitTiming(recorder) as observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.status, 'unsupported')
        self.assertFalse(observed.snapshot()['usable'])
        monitoring.use_tool_id.assert_not_called()

    def test_missing_monitoring_soft_skips_and_preserves_result(self):
        recorder = FakeRecorder()
        runtime = SimpleNamespace(implementation=SimpleNamespace(name='cpython'), version_info=(3, 14, 0))
        with patch.object(wait, 'sys', runtime):
            with wait.CaptureWaitTiming(recorder) as observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.status, 'unsupported')
        self.assertFalse(observed.snapshot()['usable'])

    def test_incomplete_monitoring_capabilities_soft_skip_without_acquiring_any_slot(self):
        recorder = FakeRecorder()
        monitoring = SimpleNamespace(use_tool_id=Mock())
        runtime = SimpleNamespace(implementation=SimpleNamespace(name='cpython'),
                                  version_info=(3, 14, 0), monitoring=monitoring)
        with patch.object(wait, 'sys', runtime):
            with wait.CaptureWaitTiming(recorder) as observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.status, 'unsupported')
        self.assertEqual(observed.failure_stage, 'monitoring_capabilities')
        monitoring.use_tool_id.assert_not_called()

    def test_non_cpython_runtime_soft_skips_without_acquiring_any_slot(self):
        recorder = FakeRecorder()
        monitoring = Mock()
        runtime = SimpleNamespace(implementation=SimpleNamespace(name='pypy'),
                                  version_info=(3, 14, 0), monitoring=monitoring)
        with patch.object(wait, 'sys', runtime):
            with wait.CaptureWaitTiming(recorder) as observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.status, 'unsupported')
        self.assertEqual(observed.failure_stage, 'runtime_unsupported')
        monitoring.use_tool_id.assert_not_called()

    def test_unsupported_recorder_soft_skips_without_mutation(self):
        recorder = SimpleNamespace(_record_chunk=Mock(return_value=object()))
        before = vars(recorder).copy()
        with wait.CaptureWaitTiming(recorder) as observed:
            result = recorder._record_chunk()
        self.assertIs(result, before['_record_chunk'].return_value)
        self.assertEqual(vars(recorder), before)
        self.assertEqual(observed.status, 'unsupported')
        self.assertFalse(observed.snapshot()['usable'])


@unittest.skipUnless(MONITORING_AVAILABLE, 'Capture-wait monitoring requires verified CPython3.14+ semantics')
class CaptureWaitMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.monitor = sys.monitoring
        self.tools_before = [self.monitor.get_tool(i) for i in range(6)]
        self.addCleanup(self.assert_tools_restored)
        if any(self.tools_before[i] is not None for i in (3, 4)):
            self.skipTest('Capture diagnostic slots already belong to another tool')

    def assert_tools_restored(self):
        self.assertEqual([self.monitor.get_tool(i) for i in range(6)], self.tools_before)

    def observe(self, recorder=None, **options):
        recorder = recorder or FakeRecorder()
        observed = wait.CaptureWaitTiming(recorder, **options)
        self.addCleanup(observed.close)
        return recorder, observed

    def test_mro_property_and_real_record_chunk_are_observed_without_wrapping(self):
        recorder = FakeRecorder()
        recorder.sleep_seconds = .001
        original_chunk = recorder._record_chunk
        original_property = PeriodSource.deviceperiod
        ticks = iter((10, 20, 25, 60))
        recorder, observed = self.observe(recorder, clock=lambda: next(ticks))
        with observed:
            self.assertIs(recorder._record_chunk(), recorder.result)
            snapshot = observed.snapshot()
            tool = observed.tool
            self.assertTrue(snapshot['usable'])
        self.assertEqual(recorder._record_chunk, original_chunk)
        self.assertIs(PeriodSource.deviceperiod, original_property)
        self.assertEqual((recorder.period_calls, recorder.chunk_calls), (1, 1))
        self.assertEqual(observed.stats['getter_calls'], 1)
        self.assertEqual(observed.stats['getter_ns'], 10)
        self.assertEqual(observed.stats['sleep_calls'], 1)
        self.assertEqual(observed.stats['sleep_ns'], 35)
        self.assertEqual(observed.stats['requested_ns'], 1_000_000)
        self.assertTrue(observed.snapshot()['usable'])
        self.assertEqual(observed.metadata()['status'], 'complete')
        self.assertIsNone(self.monitor.get_tool(tool))
        self.assertEqual(self.monitor.get_events(tool), 0)
        self.assertEqual(self.monitor.get_local_events(tool, FakeRecorder._record_chunk.__code__), 0)
        self.assertEqual(self.monitor.get_local_events(tool, PeriodSource.deviceperiod.fget.__code__), 0)

    def test_property_error_identity_and_type_are_preserved(self):
        recorder, observed = self.observe()
        sentinel = OSError('inert property failure')
        recorder.period_error = sentinel
        with observed:
            with self.assertRaises(OSError) as error:
                recorder._record_chunk()
        self.assertIs(error.exception, sentinel)
        self.assertEqual(observed.stats['getter_calls'], 1)
        self.assertEqual(observed.stats['getter_errors'], 1)
        self.assertEqual(observed.stats['sleep_calls'], 0)
        self.assertTrue(observed.snapshot()['usable'])

    def test_chunk_error_identity_is_preserved_after_successful_waits(self):
        recorder, observed = self.observe()
        sentinel = RuntimeError('inert target failure')
        recorder.chunk_error = sentinel
        with observed:
            with self.assertRaises(RuntimeError) as error:
                recorder._record_chunk()
        self.assertIs(error.exception, sentinel)
        self.assertEqual(observed.stats['getter_errors'], 0)
        self.assertEqual(observed.stats['sleep_errors'], 0)
        self.assertTrue(observed.snapshot()['usable'])

    def test_actual_c_sleep_error_is_observed_without_changing_target_exception(self):
        recorder, observed = self.observe()
        recorder.sleep_seconds = -.001
        with observed:
            with self.assertRaises(ValueError) as error:
                recorder._record_chunk()
        self.assertIn('sleep length', str(error.exception))
        self.assertEqual(observed.stats['sleep_calls'], 1)
        self.assertEqual(observed.stats['sleep_errors'], 1)
        self.assertGreaterEqual(observed.stats['requested_ns'], 0)
        self.assertTrue(observed.snapshot()['usable'])

    def test_other_recorder_and_other_thread_do_not_contribute_spans(self):
        recorder, observed = self.observe()
        other = FakeRecorder()
        results = []
        with observed:
            recorder._record_chunk()
            expected = dict(observed.stats)
            self.assertIs(other._record_chunk(), other.result)
            worker = threading.Thread(target=lambda: results.append(recorder._record_chunk()), daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, [recorder.result])
            self.assertEqual(observed.stats, expected)
        self.assertTrue(observed.snapshot()['usable'])

    def test_entering_from_a_different_owner_thread_is_explicitly_unsupported(self):
        recorder, observed = self.observe()
        results = []
        def enter():
            with observed:
                results.append(recorder._record_chunk())
        worker = threading.Thread(target=enter, daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [recorder.result])
        self.assertEqual(observed.status, 'unsupported')
        self.assertEqual(observed.failure_stage, 'wrong_owner_thread')
        self.assertFalse(observed.snapshot()['usable'])

    def test_busy_slots_remain_owned_by_foreign_tools(self):
        names = {3: 'remontevoice-test-busy3', 4: 'remontevoice-test-busy4'}
        try:
            for tool, name in names.items():
                self.monitor.use_tool_id(tool, name)
            recorder, observed = self.observe()
            with observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
            self.assertEqual(observed.status, 'unsupported')
            self.assertEqual(observed.failure_stage, 'tool_ids_busy')
            for tool, name in names.items():
                self.assertEqual(self.monitor.get_tool(tool), name)
        finally:
            for tool, name in names.items():
                if self.monitor.get_tool(tool) == name:
                    self.monitor.free_tool_id(tool)

    def test_partial_acquisition_failure_reconciles_and_frees_only_its_claim(self):
        original_use = self.monitor.use_tool_id
        for after_claim in (False, True):
            with self.subTest(after_claim=after_claim):
                recorder, observed = self.observe()
                def failed_acquire(tool, name):
                    if after_claim:
                        original_use(tool, name)
                    raise OSError('inert acquisition failure')
                with patch.object(self.monitor, 'use_tool_id', failed_acquire):
                    with observed:
                        self.assertIs(recorder._record_chunk(), recorder.result)
                self.assertEqual(observed.status, 'unsupported')
                self.assertEqual(observed.failure_stage, 'tool_acquire')
                self.assertFalse(observed.snapshot()['usable'])
                self.assert_tools_restored()

    def test_callback_registration_failure_cannot_replace_target_result(self):
        original_register = self.monitor.register_callback
        for fail_at in (1, 3):
            with self.subTest(fail_at=fail_at):
                recorder, observed = self.observe()
                calls = [0]
                def register(*args):
                    calls[0] += 1
                    if calls[0] == fail_at:
                        raise OSError('inert registration failure')
                    return original_register(*args)
                with patch.object(self.monitor, 'register_callback', register):
                    with observed:
                        self.assertIs(recorder._record_chunk(), recorder.result)
                self.assertEqual(observed.status, 'unsupported')
                self.assertFalse(observed.snapshot()['usable'])
                self.assert_tools_restored()

    def test_lost_slot_ownership_never_frees_the_replacement_tool(self):
        recorder, observed = self.observe()
        foreign = 'remontevoice-test-replacement'
        with observed:
            tool = observed.tool
            self.monitor.free_tool_id(tool)
            self.monitor.use_tool_id(tool, foreign)
            self.assertIs(recorder._record_chunk(), recorder.result)
            self.assertFalse(observed.snapshot()['usable'])
            self.assertEqual(observed.metadata()['failure_stage'], 'tool_ownership_lost')
        try:
            self.assertFalse(observed.snapshot()['usable'])
            self.assertEqual(observed.failure_stage, 'tool_ownership_lost')
            self.assertEqual(self.monitor.get_tool(tool), foreign)
        finally:
            if self.monitor.get_tool(tool) == foreign:
                self.monitor.free_tool_id(tool)

    def test_failed_active_ownership_observation_is_sticky_and_does_not_claim_completeness(self):
        recorder, observed = self.observe()
        with observed:
            self.assertIs(recorder._record_chunk(), recorder.result)
            with patch.object(self.monitor, 'get_tool', side_effect=OSError('inert ownership observation failure')):
                self.assertFalse(observed.snapshot()['usable'])
            self.assertFalse(observed.snapshot()['usable'])
            self.assertIsNotNone(observed.metadata()['failure_stage'])
        self.assertFalse(observed.snapshot()['usable'])

    def test_ownership_replacement_between_cleanup_steps_cannot_clear_foreign_hooks(self):
        recorder, observed = self.observe()
        foreign = 'remontevoice-test-replacement-during-cleanup'
        original_set_local = self.monitor.set_local_events
        original_free = self.monitor.free_tool_id
        original_use = self.monitor.use_tool_id
        with observed:
            self.assertIs(recorder._record_chunk(), recorder.result)
            tool = observed.tool
            def replace_after_first_local_clear(tool_id, code, events):
                original_set_local(tool_id, code, events)
                original_free(tool_id)
                original_use(tool_id, foreign)
            with patch.object(self.monitor, 'set_local_events', side_effect=replace_after_first_local_clear) as local, \
                 patch.object(self.monitor, 'set_events') as global_events, \
                 patch.object(self.monitor, 'register_callback') as callbacks, \
                 patch.object(self.monitor, 'free_tool_id') as free:
                observed.close()
            try:
                self.assertEqual(local.call_count, 1)
                global_events.assert_not_called()
                callbacks.assert_not_called()
                free.assert_not_called()
                self.assertEqual(self.monitor.get_tool(tool), foreign)
                self.assertFalse(observed.snapshot()['usable'])
            finally:
                if self.monitor.get_tool(tool) == foreign:
                    original_free(tool)

    def test_cleanup_interruption_never_masks_an_existing_target_error(self):
        for original_type, cleanup_type in ((OSError, KeyboardInterrupt), (KeyboardInterrupt, SystemExit)):
            with self.subTest(original=original_type.__name__, cleanup=cleanup_type.__name__):
                recorder, observed = self.observe()
                original = original_type('inert target failure')
                cleanup = cleanup_type('inert cleanup interruption')
                with self.assertRaises(original_type) as error:
                    with patch.object(self.monitor, 'free_tool_id', side_effect=cleanup):
                        with observed:
                            raise original
                self.assertIs(error.exception, original)
                self.assertEqual(observed.status, 'interrupted')
                self.assertTrue(observed.metadata()['cleanup_pending'])
                observed.close()
                self.assertEqual(observed.status, 'interrupted')
                self.assertFalse(observed.snapshot()['usable'])

    def test_an_existing_profiler_is_preserved(self):
        previous = sys.getprofile()
        def profiler(frame, event, argument):
            return None
        recorder, observed = self.observe()
        try:
            sys.setprofile(profiler)
            with observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
            self.assertIs(sys.getprofile(), profiler)
            self.assertTrue(observed.snapshot()['usable'])
        finally:
            sys.setprofile(previous)

    def test_observer_clock_failures_preserve_outputs_and_original_target_errors(self):
        for fail_at, target_failure in ((1, False), (2, False), (3, False), (4, False), (1, True), (2, True)):
            with self.subTest(fail_at=fail_at, target_failure=target_failure):
                count = [0]
                def clock():
                    count[0] += 1
                    if count[0] == fail_at:
                        raise OSError('inert observer clock failure')
                    return time.perf_counter_ns()
                recorder, observed = self.observe(clock=clock)
                sentinel = OSError('inert original target failure')
                if target_failure:
                    recorder.period_error = sentinel
                with observed:
                    if target_failure:
                        with self.assertRaises(OSError) as error:
                            recorder._record_chunk()
                        self.assertIs(error.exception, sentinel)
                    else:
                        self.assertIs(recorder._record_chunk(), recorder.result)
                self.assertFalse(observed.snapshot()['usable'])
                self.assertEqual(observed.status, 'failed')
                self.assertEqual(observed.stats['observer_errors'], 1)
                self.assertEqual(observed.error_type, 'OSError')
                incomplete = observed.incomplete_spans
                observed.close()
                self.assertEqual(observed.incomplete_spans, incomplete)
                self.assert_tools_restored()

    def test_owner_identity_lookup_failure_remains_observational(self):
        recorder, observed = self.observe()
        with observed:
            runtime = SimpleNamespace(current_thread=Mock(side_effect=RuntimeError('inert identity failure')))
            with patch.object(wait, 'threading', runtime):
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertFalse(observed.snapshot()['usable'])
        self.assertEqual(observed.failure_stage, 'getter_start')

    def test_early_close_preserves_incomplete_count_across_repeated_close(self):
        recorder, observed = self.observe()
        recorder.on_period = observed.close
        with observed:
            self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.incomplete_spans, 1)
        self.assertFalse(observed.snapshot()['usable'])
        self.assertFalse(observed.snapshot()['usable'])
        observed.close()
        self.assertEqual(observed.incomplete_spans, 1)

    def test_cleanup_failure_stays_visible_until_explicit_owned_cleanup_retry(self):
        recorder, observed = self.observe()
        with patch.object(self.monitor, 'free_tool_id', side_effect=OSError('inert cleanup failure')):
            with observed:
                self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertEqual(observed.status, 'cleanup_failed')
        self.assertTrue(observed.metadata()['cleanup_pending'])
        self.assertIsNotNone(observed.tool)
        self.assertFalse(observed.snapshot()['usable'])
        observed.close()
        self.assertIsNone(observed.tool)
        self.assertEqual(observed.status, 'failed')
        self.assertFalse(observed.metadata()['cleanup_pending'])
        self.assertFalse(observed.snapshot()['usable'])

    def test_user_interruption_in_body_is_identity_preserved_and_sticky(self):
        for kind in (KeyboardInterrupt, SystemExit):
            with self.subTest(kind=kind.__name__):
                recorder, observed = self.observe()
                sentinel = kind('inert user cancellation')
                with self.assertRaises(kind) as error:
                    with observed:
                        raise sentinel
                self.assertIs(error.exception, sentinel)
                self.assertEqual(observed.status, 'interrupted')
                for _ in range(2):
                    observed.close()
                    self.assertEqual(observed.status, 'interrupted')
                    self.assertFalse(observed.snapshot()['usable'])

    def test_user_interruption_in_callback_is_not_swallowed_or_reclassified(self):
        sentinel = KeyboardInterrupt('inert callback cancellation')
        recorder, observed = self.observe(clock=Mock(side_effect=sentinel))
        with self.assertRaises(KeyboardInterrupt) as error:
            with observed:
                recorder._record_chunk()
        self.assertIs(error.exception, sentinel)
        self.assertEqual(observed.status, 'interrupted')
        observed.close()
        self.assertEqual(observed.status, 'interrupted')
        self.assertFalse(observed.snapshot()['usable'])

    def test_user_interruption_during_cleanup_retains_owned_retry_and_sticky_status(self):
        recorder, observed = self.observe()
        sentinel = KeyboardInterrupt('inert cleanup cancellation')
        with self.assertRaises(KeyboardInterrupt) as error:
            with patch.object(self.monitor, 'free_tool_id', side_effect=sentinel):
                with observed:
                    self.assertIs(recorder._record_chunk(), recorder.result)
        self.assertIs(error.exception, sentinel)
        self.assertEqual(observed.status, 'interrupted')
        self.assertTrue(observed.metadata()['cleanup_pending'])
        observed.close()
        self.assertEqual(observed.status, 'interrupted')
        self.assertFalse(observed.metadata()['cleanup_pending'])
        self.assertFalse(observed.snapshot()['usable'])


if __name__ == '__main__':
    unittest.main()
