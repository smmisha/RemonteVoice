"""Opt-in wait exports and capture-owner cleanup; no audio module/device import."""
import ast
import builtins
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from remotevoice.audio_timing import SystemAudioTiming
from test_capture_wait_timing import FakeRecorder, MONITORING_AVAILABLE


class ScalarWait:
    def __init__(self):
        self.stats = dict(getter_calls=0, getter_ns=0, getter_errors=0, sleep_calls=0,
                          sleep_ns=0, requested_ns=0, sleep_errors=0, observer_errors=0)
        self.pending_count = 0
        self.usable = True
        self.status = 'new'
        self.snapshot_error = None
        self.metadata_error = None
        self.exit_contexts = []

    def __enter__(self):
        self.status = 'active'
        return self

    def __exit__(self, kind, error, traceback):
        self.exit_contexts.append((kind, error))
        if self.status == 'active':
            self.status = 'complete'
        return False

    def snapshot(self):
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return dict(self.stats, pending_count=self.pending_count,
                    usable=self.usable and self.pending_count == 0)

    def metadata(self):
        if self.metadata_error is not None:
            raise self.metadata_error
        return dict(status=self.status, failure_stage=None, error_type=None,
                    incomplete_spans=0, cleanup_pending=False)


class CaptureWaitIntegrationTests(unittest.TestCase):
    def make_timing(self, directory, *, native=True):
        now = SimpleNamespace(value=0)
        timing = SystemAudioTiming(trace_directory=directory, clock=lambda: now.value)
        timing.thread_clock = lambda: 0
        observer = ScalarWait()
        recorder = SimpleNamespace()
        if native:
            recorder._capture_available_frames = Mock(return_value=480)
            recorder._test_available = recorder._capture_available_frames
            recorder._capture_buffer = Mock(return_value=object())
            recorder._capture_release = Mock()
        with patch('remotevoice.capture_wait_timing.CaptureWaitTiming', return_value=observer) as factory:
            self.assertIs(timing.observe_capture(recorder), recorder)
        factory.assert_called_once_with(recorder, clock=timing.clock)
        self.addCleanup(timing.close_capture_observation)
        return timing, observer, recorder, now

    def finish_packet(self, timing, observer, now, *, getter_ns=0, sleep_ns=0, other_ns=0):
        timing.mark('read_start')
        observer.stats['getter_calls'] += 1
        observer.stats['getter_ns'] += getter_ns
        observer.stats['sleep_calls'] += 1
        observer.stats['sleep_ns'] += sleep_ns
        observer.stats['requested_ns'] += sleep_ns
        now.value += getter_ns + sleep_ns + other_ns
        timing.mark('read_end')
        for phase in ('send_enter', 'send_lock', 'send_done'):
            now.value += 1
            timing.mark(phase)
        with patch('remotevoice.audio_timing.LOGGER.info'):
            timing.finish()

    def export(self, timing):
        timing.close_capture_observation()
        return json.loads(timing.flush_trace().read_text('utf-8'))

    def test_disabled_observation_never_imports_or_starts_wait_monitor(self):
        recorder = SimpleNamespace()
        timing = SystemAudioTiming()
        original_import = builtins.__import__
        imports = []
        def observe_import(name, *args, **kwargs):
            if 'capture_wait_timing' in name:
                imports.append(name)
            return original_import(name, *args, **kwargs)
        with patch.object(builtins, '__import__', observe_import):
            self.assertIs(timing.observe_capture(recorder), recorder)
            timing.close_capture_observation()
        self.assertEqual(imports, [])
        self.assertEqual(vars(recorder), {})

    def test_wait_probe_still_attaches_without_native_wrapper_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory, native=False)
            self.finish_packet(timing, observer, now, getter_ns=10, sleep_ns=20)
            report = self.export(timing)
        self.assertEqual(report['version'], 1)
        self.assertNotIn('capture_native_columns', report)
        self.assertEqual(report['capture_wait_complete'], 1)
        row = dict(zip(report['capture_wait_columns'], report['capture_wait_rows'][0]))
        self.assertEqual(row['getter_ns'], 10)
        self.assertEqual(row['sleep_ns'], 20)
        self.assertEqual(row['complete'], 1)

    def test_native_columns_stay_stable_and_unattributed_excludes_both_wait_and_native_time(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory)
            def available():
                now.value += 200_000_000
                return 480
            recorder._test_available.side_effect = available
            timing.mark('read_start')
            self.assertEqual(recorder._capture_available_frames(), 480)
            observer.stats.update(getter_calls=1, getter_ns=100_000_000,
                                  sleep_calls=1, sleep_ns=150_000_000, requested_ns=100_000_000)
            now.value += 300_000_000
            timing.mark('read_end')
            for phase in ('send_enter', 'send_lock', 'send_done'):
                now.value += 1
                timing.mark(phase)
            with patch('remotevoice.audio_timing.LOGGER.info'):
                timing.finish()
            self.assertEqual(list(Path(directory).iterdir()), [])
            report = self.export(timing)
        self.assertEqual(report['version'], 1)
        self.assertEqual(report['capture_native_columns'], ['frame', 'thread_cpu_ns', 'available_calls', 'available_ns',
            'buffer_calls', 'buffer_ns', 'release_calls', 'release_ns', 'zero_polls', 'unattributed_ns', 'capture_wall_ns'])
        wait_row = dict(zip(report['capture_wait_columns'], report['capture_wait_rows'][0]))
        native_row = dict(zip(report['capture_native_columns'], report['capture_native_rows'][0]))
        self.assertEqual(wait_row['capture_wall_ns'], 500_000_000)
        self.assertEqual(native_row['available_ns'], 200_000_000)
        self.assertEqual(native_row['unattributed_ns'], 300_000_000)
        self.assertEqual(wait_row['unattributed_ns'], 50_000_000)
        self.assertEqual(wait_row['frame'], native_row['frame'])
        self.assertEqual(wait_row['complete'], 1)
        self.assertTrue(all(type(value) is int and value >= 0 for value in wait_row.values()))
        self.assertEqual(report['capture_wait_observer']['status'], 'complete')

    def test_per_read_rows_are_deltas_and_tail_and_early_slow_rows_are_independently_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(SystemAudioTiming, '_TRACE_LIMIT', 1), \
             patch.object(SystemAudioTiming, '_CAPTURE_SLOW_LIMIT', 2):
            timing, observer, recorder, now = self.make_timing(directory)
            for _ in range(3):
                self.finish_packet(timing, observer, now, getter_ns=130_000_000, sleep_ns=10)
            self.finish_packet(timing, observer, now, getter_ns=1, sleep_ns=1)
            report = self.export(timing)
        self.assertEqual(report['capture_wait_complete'], 4)
        self.assertEqual(report['capture_wait_omitted'], 3)
        self.assertEqual([row[0] for row in report['capture_wait_rows']], [4])
        self.assertEqual(report['capture_wait_slow_complete'], 3)
        self.assertEqual(report['capture_wait_slow_omitted'], 1)
        self.assertEqual([row[0] for row in report['capture_wait_slow_rows']], [2, 3])
        for raw in report['capture_wait_rows'] + report['capture_wait_slow_rows']:
            row = dict(zip(report['capture_wait_columns'], raw))
            self.assertEqual(row['getter_calls'], 1)
            self.assertEqual(row['sleep_calls'], 1)

    def test_unsupported_observation_is_exported_without_claiming_complete_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory)
            observer.status = 'unsupported'
            observer.usable = False
            self.finish_packet(timing, observer, now, other_ns=10)
            report = self.export(timing)
        row = dict(zip(report['capture_wait_columns'], report['capture_wait_rows'][0]))
        self.assertEqual(row['complete'], 0)
        self.assertEqual(report['capture_wait_complete'], 1)
        self.assertEqual(report['capture_wait_observer']['status'], 'unsupported')

    def test_pending_or_failed_snapshot_is_never_exported_as_complete(self):
        for kind in ('pending', 'observer_error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                timing, observer, recorder, now = self.make_timing(directory)
                timing.mark('read_start')
                if kind == 'pending':
                    observer.pending_count = 1
                else:
                    observer.stats['observer_errors'] = 1
                    observer.usable = False
                    observer.status = 'failed'
                timing.mark('read_end')
                for phase in ('send_enter', 'send_lock', 'send_done'):
                    timing.mark(phase)
                timing.finish()
                report = self.export(timing)
            row = dict(zip(report['capture_wait_columns'], report['capture_wait_rows'][0]))
            self.assertEqual(row['complete'], 0)
            self.assertEqual(row['pending_count'], int(kind == 'pending'))

    def test_snapshot_exception_cannot_stop_packet_or_turn_partial_data_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory)
            timing.mark('read_start')
            observer.snapshot_error = OSError('PRIVATE SNAPSHOT DETAIL')
            timing.mark('read_end')
            for phase in ('send_enter', 'send_lock', 'send_done'):
                timing.mark(phase)
            timing.finish()
            report = self.export(timing)
        row = dict(zip(report['capture_wait_columns'], report['capture_wait_rows'][0]))
        self.assertEqual(row['complete'], 0)
        self.assertEqual(report['capture_wait_observer']['status'], 'failed')
        self.assertEqual(report['capture_wait_observer']['failure_stage'], 'snapshot')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_metadata_exception_is_sanitized_without_losing_packet_export(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory)
            self.finish_packet(timing, observer, now, getter_ns=10)
            observer.metadata_error = OSError('PRIVATE METADATA DETAIL')
            report = self.export(timing)
        self.assertEqual(len(report['packets']), 1)
        self.assertEqual(report['capture_wait_observer']['status'], 'failed')
        self.assertEqual(report['capture_wait_observer']['failure_stage'], 'metadata')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_finally_close_receives_the_original_propagating_error(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, recorder, now = self.make_timing(directory)
            sentinel = OSError('original target error')
            with self.assertRaises(OSError) as raised:
                try:
                    raise sentinel
                finally:
                    timing.close_capture_observation()
            self.assertIs(raised.exception, sentinel)
            self.assertEqual(observer.exit_contexts[-1], (OSError, sentinel))

    def test_observer_setup_failure_does_not_replace_capture_result(self):
        with tempfile.TemporaryDirectory() as directory:
            timing = SystemAudioTiming(trace_directory=directory)
            recorder = SimpleNamespace(_capture_available_frames=Mock(return_value=480),
                _capture_buffer=Mock(return_value=object()), _capture_release=Mock())
            with patch('remotevoice.capture_wait_timing.CaptureWaitTiming', side_effect=OSError('PRIVATE SETUP DETAIL')):
                self.assertIs(timing.observe_capture(recorder), recorder)
            timing.mark('read_start')
            self.assertEqual(recorder._capture_available_frames(), 480)
            timing.mark('read_end')
            for phase in ('send_enter', 'send_lock', 'send_done'):
                timing.mark(phase)
            timing.finish()
            report = self.export(timing)
        self.assertEqual(report['capture_wait_observer']['status'], 'failed')
        self.assertEqual(report['capture_wait_observer']['failure_stage'], 'setup')
        self.assertNotIn('PRIVATE', json.dumps(report))

    @unittest.skipUnless(MONITORING_AVAILABLE, 'Real cleanup retry requires verified CPython3.14+ monitoring')
    def test_owner_cleanup_automatically_retries_one_transient_slot_release_failure(self):
        monitor = sys.monitoring
        tools = [monitor.get_tool(i) for i in range(6)]
        if any(tools[i] is not None for i in (3, 4)):
            self.skipTest('Capture diagnostic slots occupied')
        with tempfile.TemporaryDirectory() as directory:
            timing = SystemAudioTiming(trace_directory=directory)
            self.addCleanup(timing.close_capture_observation)
            recorder = FakeRecorder()
            timing.observe_capture(recorder)
            timing.mark('read_start')
            self.assertIs(recorder._record_chunk(), recorder.result)
            timing.mark('read_end')
            for phase in ('send_enter', 'send_lock', 'send_done'):
                timing.mark(phase)
            timing.finish()
            original_free = monitor.free_tool_id
            count = [0]
            def fail_once(tool):
                count[0] += 1
                if count[0] == 1:
                    raise OSError('PRIVATE TRANSIENT CLEANUP DETAIL')
                return original_free(tool)
            with patch.object(monitor, 'free_tool_id', side_effect=fail_once):
                timing.close_capture_observation()
            self.assertEqual(count[0], 2)
            report = json.loads(timing.flush_trace().read_text('utf-8'))
        self.assertEqual(report['capture_wait_observer']['status'], 'failed')
        self.assertFalse(report['capture_wait_observer']['cleanup_pending'])
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual([monitor.get_tool(i) for i in range(6)], tools)

    @unittest.skipUnless(MONITORING_AVAILABLE, 'Real cleanup retry requires verified CPython3.14+ monitoring')
    def test_owner_cleanup_is_bounded_and_preserves_target_error_when_slot_release_keeps_failing(self):
        monitor = sys.monitoring
        tools = [monitor.get_tool(i) for i in range(6)]
        if any(tools[i] is not None for i in (3, 4)):
            self.skipTest('Capture diagnostic slots occupied')
        with tempfile.TemporaryDirectory() as directory:
            timing = SystemAudioTiming(trace_directory=directory)
            self.addCleanup(timing.close_capture_observation)
            recorder = FakeRecorder()
            timing.observe_capture(recorder)
            timing.mark('read_start')
            self.assertIs(recorder._record_chunk(), recorder.result)
            timing.mark('read_end')
            for phase in ('send_enter', 'send_lock', 'send_done'):
                timing.mark(phase)
            timing.finish()
            sentinel = OSError('original capture error')
            with patch.object(monitor, 'free_tool_id', side_effect=OSError('PRIVATE PERMANENT CLEANUP DETAIL')) as free:
                with self.assertRaises(OSError) as error:
                    try:
                        raise sentinel
                    finally:
                        timing.close_capture_observation()
                self.assertIs(error.exception, sentinel)
                self.assertEqual(free.call_count, 2)
                report = json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual(report['capture_wait_observer']['status'], 'cleanup_failed')
            self.assertTrue(report['capture_wait_observer']['cleanup_pending'])
            self.assertNotIn('PRIVATE', json.dumps(report))
            timing.close_capture_observation()  # Explicit later cleanup, no silent success claim.
        self.assertEqual([monitor.get_tool(i) for i in range(6)], tools)


class CaptureOwnerFinallyTests(unittest.TestCase):
    def test_real_capture_method_closes_observation_before_recorder_context_exit_on_each_failure_path(self):
        # Execute only the actual Python function body with inert dependencies;
        # do not import remotevoice.audio or SoundCard to reach its lifecycle.
        path = Path(__file__).resolve().parents[1] / 'host/remotevoice/audio.py'
        module = ast.parse(path.read_text('utf-8'))
        cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'AudioBridge')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_capture_system_audio')
        code = compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec')
        for stage in ('success', 'observe', 'guard', 'record', 'send'):
            with self.subTest(stage=stage):
                events = []
                owner = threading.current_thread()
                sentinel = RuntimeError('inert capture failure')
                stopped = threading.Event()
                recorder = Mock()
                context = Mock()
                context.__enter__ = Mock(side_effect=lambda: recorder)
                context.__exit__ = Mock(side_effect=lambda *args: events.append('recorder_exit'))
                def action(name, result=None):
                    events.append(name)
                    if name == stage:
                        raise sentinel
                    return result
                samples = SimpleNamespace(tobytes=lambda: b'synthetic')
                recorder.record.side_effect = lambda **options: action('record', samples)
                timing = Mock()
                timing.observe_capture.side_effect = lambda rec: action('observe')
                def close():
                    self.assertIs(threading.current_thread(), owner)
                    events.append('close')
                timing.close_capture_observation.side_effect = close
                source = SimpleNamespace(recorder=Mock(return_value=context))
                future = SimpleNamespace(result=lambda **options: action('send'))
                namespace = dict(sc=SimpleNamespace(get_microphone=Mock(return_value=source)),
                    SAMPLE_RATE=48000, SYSTEM_CHANNELS=2, SYSTEM_CAPTURE_BUFFER_FRAMES=4800, FRAME_SAMPLES=480,
                    np=SimpleNamespace(asarray=lambda value, **options: value),
                    guard_capture_silence=lambda rec: action('guard'),
                    encode_system_frame=lambda *args, **kwargs: b'synthetic',
                    asyncio=SimpleNamespace(run_coroutine_threadsafe=lambda *args: future),
                    log_worker_failure=Mock(), LOGGER=Mock())
                exec(code, namespace)
                instance = SimpleNamespace(capture_device_id='synthetic', _system_timing=timing, _stop=stopped,
                    on_status=Mock(), _stereo=lambda samples: samples, _system_compression=False, _diagnostics=None,
                    _send=Mock(return_value=object()), loop=object(), _mark_progress=lambda *args: stopped.set(),
                    diagnostic_id='synthetic', _report_failure=Mock())
                namespace['_capture_system_audio'](instance)
                timing.close_capture_observation.assert_called_once_with()
                self.assertEqual(events[-2:], ['close', 'recorder_exit'])
                if stage == 'success':
                    instance._report_failure.assert_not_called()
                else:
                    instance._report_failure.assert_called_once()


if __name__ == '__main__':
    unittest.main()
