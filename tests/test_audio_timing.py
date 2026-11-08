import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from remotevoice.audio_timing import SystemAudioTiming


class TimingTests(unittest.TestCase):
    def packet(self, timing, points):
        timing.clock = iter(points).__next__
        for phase in ('read_start', 'read_end', 'send_enter', 'send_lock', 'send_done'):
            timing.mark(phase)
        return timing.finish()

    def test_capture_dispatch_lock_and_send_stalls_are_not_conflated(self):
        for selected in ('capture_ms', 'dispatch_ms', 'lock_ms', 'send_ms'):
            durations = [200_000_000 if name == selected else 1_000_000
                         for name in ('capture_ms', 'dispatch_ms', 'lock_ms', 'send_ms')]
            points = [1_000_000_000]
            for duration in durations:
                points.append(points[-1]+duration)
            with self.subTest(selected=selected), self.assertLogs('remotevoice.audio', level='INFO'):
                result = self.packet(SystemAudioTiming(), points)
            self.assertEqual(result[selected], 200)
            for other in ('capture_ms', 'dispatch_ms', 'lock_ms', 'send_ms'):
                if other != selected:
                    self.assertEqual(result[other], 1)
            self.assertEqual(result['gap_ms'], 0)

    def test_normal_capture_batches_are_quiet_but_gap_between_sent_packets_is_visible(self):
        timing = SystemAudioTiming()
        self.assertIsNone(self.packet(timing, [0,100_000_000,101_000_000,102_000_000,103_000_000]))
        with self.assertLogs('remotevoice.audio', level='INFO'):
            result = self.packet(timing, [400_000_000,401_000_000,402_000_000,403_000_000,404_000_000])
        self.assertEqual(result['frame'], 2)
        self.assertEqual(result['gap_ms'], 301)
        self.assertEqual(result['capture_ms'], 1)

    def test_log_rate_is_bounded_and_data_contains_only_scalars(self):
        timing = SystemAudioTiming()
        with patch('remotevoice.audio_timing.LOGGER.info') as log:
            first = self.packet(timing,[0,200_000_000,201_000_000,202_000_000,203_000_000])
            self.assertIsNone(self.packet(timing,[300_000_000,500_000_000,501_000_000,502_000_000,503_000_000]))
            self.packet(timing,[1_400_000_000,1_600_000_000,1_601_000_000,1_602_000_000,1_603_000_000])
            self.assertEqual(log.call_count, 2)
        self.assertTrue(all(isinstance(value,(int,float)) for value in first.values()))
        self.assertEqual(timing.points,{})

    def test_incomplete_observation_and_logger_failure_do_not_abort_audio(self):
        timing = SystemAudioTiming(clock=lambda: 1)
        timing.mark('read_start')
        self.assertIsNone(timing.finish())
        self.assertEqual(timing.points,{})
        with patch('remotevoice.audio_timing.LOGGER.info',side_effect=RuntimeError('test logger')):
            self.assertIsNotNone(self.packet(timing,[0,200_000_000,201_000_000,202_000_000,203_000_000]))
        with self.assertRaises(ValueError):
            timing.mark('private-frame-data')

    def test_trace_is_off_by_default_and_normal_packets_are_retained_only_on_opt_in(self):
        normal = [0, 2_000_000, 3_000_000, 4_000_000, 5_000_000]
        disabled = SystemAudioTiming()
        self.assertIsNone(self.packet(disabled, normal))
        self.assertIsNone(disabled.flush_trace())
        with tempfile.TemporaryDirectory() as directory:
            timing = SystemAudioTiming(trace_directory=Path(directory))
            self.assertIsNone(self.packet(timing, normal))
            self.assertEqual(list(Path(directory).iterdir()), [])  # No I/O on the audio path.
            target = timing.flush_trace()
            trace = json.loads(target.read_text('utf-8'))
            self.assertEqual(trace['columns'], ['frame', 'read_start_ns', 'read_end_ns',
                'send_enter_ns', 'send_lock_ns', 'send_done_ns'])
            self.assertEqual(trace['packets'], [[1] + normal])
            self.assertEqual(trace['complete_packets'], 1)
            self.assertEqual(trace['omitted_packets'], 0)
            self.assertEqual(set(trace), {'version', 'columns', 'complete_packets', 'omitted_packets', 'packets', 'session_id', 'trace_retention'})
            self.assertEqual(trace['session_id'], timing.session_id)
            self.assertIsNone(timing.flush_trace())  # A repeated stop cannot duplicate the report.
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_trace_has_a_hard_memory_bound_and_identifies_omitted_packets(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(SystemAudioTiming, '_TRACE_LIMIT', 2):
            timing = SystemAudioTiming(trace_directory=Path(directory))
            for start in (0, 10_000_000, 20_000_000):
                self.packet(timing, [start + offset for offset in range(5)])
            trace = json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual(trace['complete_packets'], 3)
            self.assertEqual(trace['omitted_packets'], 1)
            self.assertEqual([row[0] for row in trace['packets']], [2, 3])

    def test_trace_io_failure_and_incomplete_packet_do_not_abort_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            regular_file = Path(directory) / 'unavailable-directory'
            regular_file.write_text('preserve', encoding='utf-8')
            timing = SystemAudioTiming(trace_directory=regular_file)
            self.packet(timing, [0, 1, 2, 3, 4])
            with self.assertLogs('remotevoice.audio', level='WARNING'):
                self.assertIsNone(timing.flush_trace())
            self.assertEqual(regular_file.read_text('utf-8'), 'preserve')
            incomplete = SystemAudioTiming(clock=lambda: 1, trace_directory=Path(directory))
            incomplete.mark('read_start')
            self.assertIsNone(incomplete.finish())
            self.assertIsNone(incomplete.flush_trace())

    def test_transport_statistics_require_trace_opt_in_and_are_rate_and_memory_bounded(self):
        observer = unittest.mock.Mock(return_value={'rtt_us': 500, 'bytes_retrans': 4, 'secret': 'never-save'})
        timing = SystemAudioTiming(tcp_observer=observer)
        self.packet(timing, [0, 1, 2, 3, 4])
        observer.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch.object(SystemAudioTiming, '_TCP_LIMIT', 2):
            timing = SystemAudioTiming(trace_directory=directory, tcp_observer=observer)
            for start in (0, 10_000_000, 150_000_000, 300_000_000):
                timing.clock = iter([start+v for v in range(7)]).__next__
                for phase in timing._PHASES: timing.mark(phase)
                timing.finish()
            trace=json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual(observer.call_count,3)
            self.assertEqual(trace['tcp_omitted'],1)
            self.assertEqual(trace['tcp_retention'],{'sample_limit':2,'history_complete':False})
            self.assertEqual([row['frame'] for row in trace['tcp_samples']],[3,4])
            self.assertTrue(all('secret' not in row for row in trace['tcp_samples']))

    def test_explicit_long_history_keeps_transport_statistics_for_the_same_span(self):
        observer = unittest.mock.Mock(return_value={'rtt_us': 500})
        with tempfile.TemporaryDirectory() as directory, patch.object(SystemAudioTiming, '_TCP_LIMIT', 2):
            default = SystemAudioTiming(trace_directory=directory, tcp_observer=observer)
            self.assertEqual(default._tcp_samples.maxlen, 2)
            short = SystemAudioTiming(trace_directory=directory, tcp_observer=observer, trace_packet_limit='15')
            self.assertEqual(short._tcp_samples.maxlen, 2)
            invalid = SystemAudioTiming(trace_directory=directory, tcp_observer=observer, trace_packet_limit='90001')
            self.assertEqual(invalid._tcp_samples.maxlen, 2)
            self.assertEqual(SystemAudioTiming(trace_directory=directory, tcp_observer=observer,
                trace_packet_limit='90000')._tcp_samples.maxlen, 9000)
            timing = SystemAudioTiming(trace_directory=directory, tcp_observer=observer, trace_packet_limit='31')
            self.assertEqual(timing._tcp_samples.maxlen, 4)
            for start in (0, 150_000_000, 300_000_000, 450_000_000):
                timing.clock = iter([start+v for v in range(7)]).__next__
                for phase in timing._PHASES: timing.mark(phase)
                timing.finish()
            trace = json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual(trace['tcp_omitted'], 0)
            self.assertEqual([row['frame'] for row in trace['tcp_samples']], [1, 2, 3, 4])
            self.assertEqual(trace['tcp_retention'], {'sample_limit': 4, 'history_complete': True})

    def test_failed_transport_statistics_do_not_abort_packet_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = unittest.mock.Mock(side_effect=OSError('read-only probe unavailable'))
            timing=SystemAudioTiming(trace_directory=directory,tcp_observer=observer)
            timing.clock=iter(range(7)).__next__
            for phase in timing._PHASES: timing.mark(phase)
            self.assertIsNone(timing.finish())
            self.assertEqual(json.loads(timing.flush_trace().read_text('utf-8'))['packets'], [[1,0,1,2,3,4]])
