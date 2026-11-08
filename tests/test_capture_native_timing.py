"""Opt-in scalar profiling must never change native capture results or errors."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from remotevoice.audio_timing import SystemAudioTiming


class CaptureNativeTimingTests(unittest.TestCase):
    def observe(self,timing,recorder):
        self.assertTrue(callable(getattr(timing,'observe_capture',None)),
                        'Opt-in native capture observation is missing')
        return timing.observe_capture(recorder)

    def recorder(self):
        return SimpleNamespace(_capture_available_frames=Mock(return_value=480),
            _capture_buffer=Mock(return_value=object()),_capture_release=Mock())

    def packet(self,timing,recorder,start=0):
        now=SimpleNamespace(value=start)
        timing.clock=lambda:now.value
        timing.thread_clock=lambda:now.value//10
        timing.mark('read_start')
        now.value+=1_000_000
        original=recorder._capture_available_frames
        self.assertEqual(original(),480)
        now.value+=4_000_000
        recorder._capture_buffer()
        now.value+=3_000_000
        recorder._capture_release(480)
        now.value+=2_000_000
        timing.mark('read_end')
        for phase in ('send_enter','send_lock','send_done'):
            now.value+=1_000_000;timing.mark(phase)
        timing.finish()

    def test_disabled_profile_neither_wraps_nor_queries_recorder(self):
        recorder=self.recorder();before=vars(recorder).copy()
        timing=SystemAudioTiming()
        self.assertIs(self.observe(timing,recorder),recorder)
        self.assertEqual(vars(recorder),before)
        for method in before.values():method.assert_not_called()

    def test_native_results_arguments_and_original_failure_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=self.recorder();original=vars(recorder).copy()
            timing=SystemAudioTiming(trace_directory=directory)
            self.assertIs(self.observe(timing,recorder),recorder)
            self.assertEqual(recorder._capture_available_frames(),480)
            self.assertIs(recorder._capture_buffer(),original['_capture_buffer'].return_value)
            recorder._capture_release(480)
            original['_capture_release'].assert_called_once_with(480)
            error=OSError('original native error')
            original['_capture_available_frames'].side_effect=error
            with self.assertRaises(OSError) as raised:recorder._capture_available_frames()
            self.assertIs(raised.exception,error)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_profile_is_idempotent_and_local_to_one_recorder(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=self.recorder();other=self.recorder();before=vars(other).copy()
            timing=SystemAudioTiming(trace_directory=directory)
            self.observe(timing,recorder)
            methods=[recorder._capture_available_frames,recorder._capture_buffer,recorder._capture_release]
            self.observe(timing,recorder)
            self.assertEqual(methods,[recorder._capture_available_frames,recorder._capture_buffer,recorder._capture_release])
            self.assertEqual(vars(other),before)
            for method in before.values():method.assert_not_called()

    def test_profile_distinguishes_native_wait_from_other_capture_time(self):
        with tempfile.TemporaryDirectory() as directory:
            now=SimpleNamespace(value=0,cpu=0)
            timing=SystemAudioTiming(clock=lambda:now.value,trace_directory=directory)
            timing.thread_clock=lambda:now.cpu
            threads=[]
            def available():
                threads.append(threading.get_ident());now.value+=200_000_000;now.cpu+=1_000_000
                return 0
            recorder=self.recorder();recorder._capture_available_frames.side_effect=available
            self.observe(timing,recorder)
            timing.mark('read_start');self.assertEqual(recorder._capture_available_frames(),0)
            now.value+=300_000_000;now.cpu+=2_000_000
            timing.mark('read_end')
            for phase in ('send_enter','send_lock','send_done'):now.value+=1;timing.mark(phase)
            with patch('remotevoice.audio_timing.LOGGER.info'):timing.finish()
            self.assertEqual(list(Path(directory).iterdir()),[])
            trace=json.loads(timing.flush_trace().read_text('utf-8'))
            columns=trace['capture_native_columns'];row=dict(zip(columns,trace['capture_native_rows'][0]))
            self.assertEqual(row['frame'],1)
            self.assertEqual(row['available_ns'],200_000_000)
            self.assertEqual(row['available_calls'],1);self.assertEqual(row['zero_polls'],1)
            self.assertEqual(row['thread_cpu_ns'],3_000_000)
            self.assertEqual(row['unattributed_ns'],300_000_000)
            self.assertEqual(row['capture_wall_ns'],500_000_000)
            self.assertEqual(threads,[threading.get_ident()])
            self.assertTrue(all(type(value) is int and value>=0 for value in row.values()))

    def test_native_rows_have_a_hard_bound_and_no_native_buffers_are_retained(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(SystemAudioTiming,'_TRACE_LIMIT',2):
            recorder=self.recorder();timing=SystemAudioTiming(trace_directory=directory)
            self.observe(timing,recorder)
            for index in range(3):self.packet(timing,recorder,index*20_000_000)
            trace=json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual(len(trace['capture_native_rows']),2)
            self.assertEqual(trace['capture_native_omitted'],1)
            self.assertEqual([row[0] for row in trace['capture_native_rows']],[2,3])
            self.assertTrue(all(type(value) is int for row in trace['capture_native_rows'] for value in row))

    def test_early_slow_reads_survive_normal_tail_eviction_with_a_separate_bound(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(SystemAudioTiming,'_TRACE_LIMIT',1),patch.object(SystemAudioTiming,'_CAPTURE_SLOW_LIMIT',2):
            now=SimpleNamespace(value=0)
            timing=SystemAudioTiming(trace_directory=directory,clock=lambda:now.value)
            recorder=self.recorder()
            def wait_for_native():now.value+=250_000_000;return 480
            recorder._capture_available_frames.side_effect=wait_for_native
            self.observe(timing,recorder)
            with patch('remotevoice.audio_timing.LOGGER.info'):
                for start in (0,300_000_000,600_000_000):
                    now.value=start;timing.mark('read_start');recorder._capture_available_frames();timing.mark('read_end')
                    for phase in ('send_enter','send_lock','send_done'):timing.mark(phase)
                    timing.finish()
                # A later normal packet evicts every slow read from the tail.
                timing.mark('read_start');timing.mark('read_end')
                for phase in ('send_enter','send_lock','send_done'):timing.mark(phase)
                timing.finish()
            trace=json.loads(timing.flush_trace().read_text('utf-8'))
            self.assertEqual([row[0] for row in trace['capture_native_rows']],[4])
            self.assertEqual([row[0] for row in trace['capture_native_slow_rows']],[2,3])
            self.assertEqual(trace['capture_native_slow_complete'],3)
            self.assertEqual(trace['capture_native_slow_omitted'],1)

    def test_broken_observer_clock_cannot_replace_native_result_or_native_error(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=self.recorder();original=recorder._capture_available_frames
            timing=SystemAudioTiming(trace_directory=directory)
            self.observe(timing,recorder)
            timing.mark('read_start')
            timing.clock=Mock(side_effect=RuntimeError('observer clock'))
            self.assertEqual(recorder._capture_available_frames(),480)
            error=OSError('original API error');original.side_effect=error
            with self.assertRaises(OSError) as raised:recorder._capture_available_frames()
            self.assertIs(raised.exception,error)

    def test_unsupported_recorder_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=SimpleNamespace(_capture_available_frames=Mock())
            before=vars(recorder).copy();timing=SystemAudioTiming(trace_directory=directory)
            self.observe(timing,recorder)
            self.assertEqual(vars(recorder),before)


if __name__=='__main__':unittest.main()
