"""Deterministic recorder guards using synthetic PCM; no audio devices."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from remotevoice.windows_audio_capture import guard_capture_silence


class CaptureSilenceGuardTests(unittest.TestCase):
    def recorder(self, chunks, *, available=0):
        remaining = iter(chunks)
        recorder = SimpleNamespace(
            _idle_start_time=None,
            _capture_available_frames=Mock(return_value=available),
        )

        def record_chunk():
            chunk, idle_start = next(remaining)
            recorder._idle_start_time = idle_start
            return chunk

        recorder._record_chunk = Mock(side_effect=record_chunk)
        return recorder

    def test_native_chunks_are_returned_unchanged_without_native_query(self):
        for chunk in (np.array([0.1, -0.2, 0.3, -0.4], dtype="float32"),
                      np.zeros(4, dtype="float32")):
            with self.subTest(native_silence=not np.any(chunk)):
                recorder = self.recorder([(chunk, None)], available=4)
                original = recorder._record_chunk
                self.assertIs(guard_capture_silence(recorder), recorder)
                original.assert_not_called()
                recorder._capture_available_frames.assert_not_called()
                self.assertIs(recorder._record_chunk(), chunk)
                original.assert_called_once_with()
                recorder._capture_available_frames.assert_not_called()
                self.assertIsNone(recorder._idle_start_time)

    def test_synthetic_silence_without_queued_native_data_is_returned_unchanged(self):
        silence = np.zeros(12, dtype="float32")
        recorder = self.recorder([(silence, 1234)], available=0)
        original = recorder._record_chunk
        guard_capture_silence(recorder)
        self.assertIs(recorder._record_chunk(), silence)
        original.assert_called_once_with()
        recorder._capture_available_frames.assert_called_once_with()
        self.assertEqual(recorder._idle_start_time, 1234)

    def test_synthetic_silence_is_replaced_by_one_native_chunk_when_data_is_queued(self):
        silence = np.zeros(12, dtype="float32")
        native = np.array([0.125, -0.25, 0.375, -0.5], dtype="float32")
        recorder = self.recorder([(silence, 1234), (native, None)], available=2)
        original = recorder._record_chunk
        guard_capture_silence(recorder)
        self.assertIs(recorder._record_chunk(), native)
        self.assertEqual(original.call_count, 2)
        recorder._capture_available_frames.assert_called_once_with()
        self.assertIsNone(recorder._idle_start_time)
        np.testing.assert_array_equal(silence, np.zeros(12, dtype="float32"))
        np.testing.assert_array_equal(native, np.array([0.125, -0.25, 0.375, -0.5], dtype="float32"))

    def test_retry_is_bounded_even_when_second_chunk_is_still_synthetic_silence(self):
        first = np.zeros(12, dtype="float32")
        second = np.zeros(8, dtype="float32")
        recorder = self.recorder([(first, 1234), (second, 2345)], available=2)
        original = recorder._record_chunk
        guard_capture_silence(recorder)
        self.assertIs(recorder._record_chunk(), second)
        self.assertEqual(original.call_count, 2)
        recorder._capture_available_frames.assert_called_once_with()
        self.assertEqual(recorder._idle_start_time, 2345)

    def test_guard_is_idempotent_and_does_not_change_other_recorder_instances(self):
        silence = np.zeros(12, dtype="float32")
        native = np.ones(4, dtype="float32")
        recorder = self.recorder([(silence, 1234), (native, None)], available=2)
        other = self.recorder([(silence, 5678)], available=2)
        original = recorder._record_chunk
        other_original = other._record_chunk
        guard_capture_silence(recorder)
        wrapped = recorder._record_chunk
        guard_capture_silence(recorder)
        self.assertIs(recorder._record_chunk, wrapped)
        self.assertIs(other._record_chunk, other_original)
        self.assertIs(recorder._record_chunk(), native)
        self.assertEqual(original.call_count, 2)
        recorder._capture_available_frames.assert_called_once_with()
        other_original.assert_not_called()
        other._capture_available_frames.assert_not_called()

    def test_unsupported_recorders_are_left_unchanged(self):
        cases = (
            SimpleNamespace(),
            SimpleNamespace(_record_chunk=Mock(), _idle_start_time=None),
            SimpleNamespace(_capture_available_frames=Mock(), _idle_start_time=None),
            SimpleNamespace(_record_chunk=Mock(), _capture_available_frames=Mock()),
            SimpleNamespace(_record_chunk=None, _capture_available_frames=Mock(), _idle_start_time=None),
            SimpleNamespace(_record_chunk=Mock(), _capture_available_frames=None, _idle_start_time=None),
        )
        for recorder in cases:
            with self.subTest(attributes=tuple(vars(recorder))):
                before = vars(recorder).copy()
                self.assertIs(guard_capture_silence(recorder), recorder)
                self.assertEqual(vars(recorder), before)
                for value in before.values():
                    if isinstance(value, Mock):
                        value.assert_not_called()

    def test_original_chunk_failure_propagates_without_native_query(self):
        error = OSError("synthetic capture failure")
        recorder = self.recorder([], available=2)
        original = recorder._record_chunk
        original.side_effect = error
        guard_capture_silence(recorder)
        with self.assertRaises(OSError) as raised:
            recorder._record_chunk()
        self.assertIs(raised.exception, error)
        original.assert_called_once_with()
        recorder._capture_available_frames.assert_not_called()

    def test_native_query_failure_propagates_without_retry(self):
        silence = np.zeros(12, dtype="float32")
        error = OSError("synthetic native query failure")
        recorder = self.recorder([(silence, 1234)])
        original = recorder._record_chunk
        recorder._capture_available_frames.side_effect = error
        guard_capture_silence(recorder)
        with self.assertRaises(OSError) as raised:
            recorder._record_chunk()
        self.assertIs(raised.exception, error)
        original.assert_called_once_with()
        recorder._capture_available_frames.assert_called_once_with()

    def test_retry_failure_propagates_without_further_retry(self):
        silence = np.zeros(12, dtype="float32")
        error = OSError("synthetic retry failure")
        recorder = self.recorder([], available=2)
        original = recorder._record_chunk

        def record_chunk():
            if original.call_count == 1:
                recorder._idle_start_time = 1234
                return silence
            raise error

        original.side_effect = record_chunk
        guard_capture_silence(recorder)
        with self.assertRaises(OSError) as raised:
            recorder._record_chunk()
        self.assertIs(raised.exception, error)
        self.assertEqual(original.call_count, 2)
        recorder._capture_available_frames.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
