"""Bounded opt-in scalar history; no audio imports, monitoring hooks or devices."""
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remotevoice.audio_timing import SystemAudioTiming


class ScalarObserver:
    def __init__(self):
        self.values = dict(getter_calls=0, getter_ns=0, getter_errors=0,
                           sleep_calls=0, sleep_ns=0, requested_ns=0,
                           sleep_errors=0, observer_errors=0)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.closed = True
        return False

    def snapshot(self):
        return dict(self.values, pending_count=0, usable=True)

    def metadata(self):
        return dict(status='complete' if self.closed else 'active', failure_stage=None,
                    error_type=None, incomplete_spans=0, cleanup_pending=False)


class PrivateValue:
    def __str__(self):
        raise AssertionError('Configuration must not be stringified')

    __repr__ = __str__


class TraceRetentionTests(unittest.TestCase):
    def make_timing(self, directory, limit=None):
        clock = SimpleNamespace(value=0)
        timing = SystemAudioTiming(clock=lambda: clock.value, trace_directory=directory,
                                   trace_packet_limit=limit)
        timing.thread_clock = lambda: 0
        observer = ScalarObserver()
        recorder = SimpleNamespace(_capture_available_frames=lambda: 480,
                                   _capture_buffer=lambda: object(), _capture_release=lambda frames: None)
        with patch('remotevoice.capture_wait_timing.CaptureWaitTiming', return_value=observer):
            self.assertIs(timing.observe_capture(recorder), recorder)
        self.addCleanup(timing.close_capture_observation)
        return timing, observer, clock

    def packet(self, timing, observer, clock, *, duration_ns=1_000_000, frame=None):
        if frame is not None:
            timing.frames = frame - 1
        timing.mark('read_start')
        observer.values['getter_calls'] += 1
        observer.values['sleep_calls'] += 1
        clock.value += duration_ns
        timing.mark('read_end')
        for phase in ('send_enter', 'send_lock', 'send_done'):
            clock.value += 1
            timing.mark(phase)
        timing.finish()

    def export(self, timing):
        timing.close_capture_observation()
        path = timing.flush_trace()
        self.assertIsNotNone(path)
        return json.loads(path.read_text('utf-8'))

    def assert_aligned_ordinals(self, report):
        ordinals = [row[0] for row in report['packets']]
        self.assertEqual([row[0] for row in report['capture_native_rows']], ordinals)
        self.assertEqual([row[0] for row in report['capture_wait_rows']], ordinals)
        complete_index = report['capture_wait_columns'].index('complete')
        self.assertTrue(all(row[complete_index] == 1 for row in report['capture_wait_rows']))
        self.assertTrue(all(type(value) is int and value >= 0
                            for key in ('packets', 'capture_native_rows', 'capture_wait_rows')
                            for row in report[key] for value in row))

    def test_default_real_crossing_of_6000_packets_preserves_a_bounded_aligned_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            timing, observer, clock = self.make_timing(directory)
            for _ in range(6001):
                self.packet(timing, observer, clock)
            self.assertEqual(list(Path(directory).iterdir()), [])
            report = self.export(timing)
        self.assert_aligned_ordinals(report)
        self.assertEqual(report['complete_packets'], 6001)
        self.assertEqual(len(report['packets']), 6000)
        self.assertEqual(report['trace_retention'], dict(packet_limit=6000, max_packet_limit=90000,
            selection='default', history_complete=False, first_retained_frame=2,
            last_retained_frame=6001, omitted_packets=1))
        self.assertEqual(report['capture_native_omitted'], 1)
        self.assertEqual(report['capture_wait_omitted'], 1)

    def test_562_early_victim_ordinals_survive_explicit_history_but_not_default_tail(self):
        # Script the five old ordinals, then emit a genuine final6000-packet
        # tail. This is6005 observed packets, not a fabricated62067-packet run.
        victims = (10426, 40803, 41033, 42694, 48751)
        reports = {}
        for limit in (None, 90000):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                timing, observer, clock = self.make_timing(directory, limit)
                for frame in victims:
                    self.packet(timing, observer, clock, frame=frame)
                for frame in range(56068, 62068):
                    self.packet(timing, observer, clock, frame=frame)
                report = self.export(timing)
                self.assert_aligned_ordinals(report)
                self.assertEqual(report['complete_packets'], 6005)
                reports[limit] = report
        default_ordinals = {row[0] for row in reports[None]['packets']}
        explicit_ordinals = {row[0] for row in reports[90000]['packets']}
        self.assertTrue(all(frame not in default_ordinals for frame in victims))
        self.assertTrue(set(victims).issubset(explicit_ordinals))
        self.assertFalse(reports[None]['trace_retention']['history_complete'])
        self.assertTrue(reports[90000]['trace_retention']['history_complete'])
        self.assertEqual(reports[90000]['trace_retention']['packet_limit'], 90000)
        self.assertEqual(reports[90000]['trace_retention']['first_retained_frame'], victims[0])
        self.assertEqual(reports[90000]['trace_retention']['last_retained_frame'], 62067)
        self.assertEqual(reports[90000]['trace_retention']['omitted_packets'], 0)
        for key in ('capture_native_columns', 'capture_wait_columns', 'columns', 'version'):
            self.assertEqual(reports[None][key], reports[90000][key])
        self.assertEqual(reports[90000]['version'], 1)

    def test_tiny_explicit_capacity_bounds_all_three_tails_without_reducing_slow_history(self):
        with tempfile.TemporaryDirectory() as directory, patch('remotevoice.audio_timing.LOGGER.info'):
            timing, observer, clock = self.make_timing(directory, 2)
            self.assertEqual(timing._capture_slow_rows.maxlen, 128)
            self.assertEqual(timing._capture_wait_slow_rows.maxlen, 128)
            self.assertEqual(timing._tcp_samples.maxlen, 600)
            for _ in range(3):
                self.packet(timing, observer, clock, duration_ns=130_000_000)
            self.packet(timing, observer, clock)
            report = self.export(timing)
        self.assert_aligned_ordinals(report)
        self.assertEqual([row[0] for row in report['packets']], [3, 4])
        self.assertEqual([row[0] for row in report['capture_native_slow_rows']], [1, 2, 3])
        self.assertEqual([row[0] for row in report['capture_wait_slow_rows']], [1, 2, 3])
        self.assertEqual(report['capture_native_omitted'], 2)
        self.assertEqual(report['capture_wait_omitted'], 2)
        self.assertEqual(report['trace_retention'], dict(packet_limit=2, max_packet_limit=90000,
            selection='explicit', history_complete=False, first_retained_frame=3,
            last_retained_frame=4, omitted_packets=2))

    def test_explicit_integer_and_stripped_ascii_decimal_boundaries_are_accepted(self):
        for limit, expected in ((1, 1), (90000, 90000), (' 2 ', 2), ('90000', 90000), ('000002', 2)):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                timing, observer, clock = self.make_timing(directory, limit)
                self.packet(timing, observer, clock)
                report = self.export(timing)
            retention = report['trace_retention']
            self.assertEqual(retention['selection'], 'explicit')
            self.assertEqual(retention['packet_limit'], expected)
            self.assertTrue(retention['history_complete'])

    def test_invalid_values_use_default_without_echoing_or_stringifying_raw_configuration(self):
        invalid = (True, False, 2.0, 0, -1, 90001, '', ' ', '+2', '-2', '2.0', '1_000', '2e3',
                   '\u0662', 'PRIVATE CONFIGURATION VALUE', {}, [], PrivateValue())
        for index, limit in enumerate(invalid):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                timing, observer, clock = self.make_timing(directory, limit)
                self.packet(timing, observer, clock)
                report = self.export(timing)
            self.assertEqual(report['trace_retention']['selection'], 'invalid_default')
            self.assertEqual(report['trace_retention']['packet_limit'], 6000)
            self.assertNotIn('PRIVATE', json.dumps(report))
            self.assertNotIn('raw_value', report['trace_retention'])

    def test_disabled_trace_ignores_explicit_value_without_allocating_history_or_hooks(self):
        for limit in (90000, 'PRIVATE DISABLED VALUE', PrivateValue()):
            timing = SystemAudioTiming(trace_packet_limit=limit)
            recorder = SimpleNamespace()
            with patch('remotevoice.capture_wait_timing.CaptureWaitTiming') as monitoring:
                self.assertIs(timing.observe_capture(recorder), recorder)
                timing.close_capture_observation()
            self.assertIsNone(timing._trace)
            self.assertIsNone(timing._capture_native_rows)
            self.assertIsNone(timing._capture_wait_rows)
            self.assertIsNone(timing.flush_trace())
            self.assertEqual(vars(recorder), {})
            monitoring.assert_not_called()

    def test_bridge_forwards_retention_environment_only_with_trace_directory(self):
        # Inspect and execute only the actual constructor expression; avoid
        # importing audio.py/SoundCard just to check opt-in configuration flow.
        path = Path(__file__).resolve().parents[1] / 'host/remotevoice/audio.py'
        tree = ast.parse(path.read_text('utf-8'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'SystemAudioTiming']
        self.assertEqual(len(calls), 1)
        expression = ast.Expression(body=calls[0])
        code = compile(expression, str(path), 'eval')
        for trace_directory in (None, 'diagnostic-trace'):
            parameters = {}
            def timing_factory(*args, **kwargs):
                parameters.update(kwargs)
                return object()
            namespace = dict(SystemAudioTiming=timing_factory, trace_directory=trace_directory,
                os=SimpleNamespace(environ={'REMOTEVOICE_AUDIO_TRACE_PACKETS': '90000'}),
                self=SimpleNamespace(_tcp_observer=None), tcp_observer=None)
            eval(code, namespace)
            self.assertEqual(parameters['trace_packet_limit'], '90000' if trace_directory else None)


if __name__ == '__main__':
    unittest.main()
