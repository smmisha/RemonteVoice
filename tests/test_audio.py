import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import numpy as np

from remotevoice.audio import AudioBridge, AudioStopError, is_virtual_audio_device, virtual_device_family
from remotevoice.protocol import FRAME_SAMPLES, SAMPLE_RATE, SYSTEM_CHANNELS, SYSTEM_FRAME


class AudioConversionTests(unittest.TestCase):
    def test_mono_capture_is_duplicated_to_stereo(self):
        mono = np.array([[0.1], [-0.2]], dtype=np.float32)
        stereo = AudioBridge._stereo(mono)

        self.assertEqual(stereo.shape, (2, 2))
        np.testing.assert_array_equal(stereo[:, 0], mono[:, 0])
        np.testing.assert_array_equal(stereo[:, 1], mono[:, 0])

    def test_multichannel_capture_keeps_first_stereo_pair(self):
        multichannel = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype=np.float32)
        stereo = AudioBridge._stereo(multichannel)

        self.assertEqual(stereo.shape, (2, 2))
        np.testing.assert_array_equal(stereo, multichannel[:, :2])

    def test_physical_microphone_is_not_accepted_as_audio_route(self):
        self.assertTrue(is_virtual_audio_device("CABLE Output (VB-Audio Virtual Cable)"))
        self.assertTrue(is_virtual_audio_device("VoiceMeeter AUX Input"))
        self.assertFalse(is_virtual_audio_device("Microphone Array (Realtek Audio)"))

    def test_two_directions_must_use_different_virtual_pairs(self):
        cable_input = "CABLE Input (VB-Audio Virtual Cable)"
        cable_output = "CABLE Output (VB-Audio Virtual Cable)"
        cable_a = "CABLE-A Input (VB-Audio Virtual Cable A)"
        self.assertEqual(virtual_device_family(cable_input), virtual_device_family(cable_output))
        self.assertNotEqual(virtual_device_family(cable_input), virtual_device_family(cable_a))


class AudioSystemCaptureBufferTests(unittest.TestCase):
    def test_native_capture_buffer_is_larger_than_reads_and_wire_frames(self):
        websocket = SimpleNamespace(send=AsyncMock())
        bridge = AudioBridge(
            "selected-capture", "selected-playback", MagicMock(spec=asyncio.AbstractEventLoop),
            websocket, asyncio.Lock(), MagicMock(),
        )
        frames = [
            np.linspace(-0.75, 0.75, FRAME_SAMPLES * SYSTEM_CHANNELS, dtype="<f4").reshape(
                FRAME_SAMPLES, SYSTEM_CHANNELS
            ),
            np.full((FRAME_SAMPLES, SYSTEM_CHANNELS), 0.25, dtype="<f4"),
        ]
        completed = MagicMock()

        def complete_send(timeout):
            if websocket.send.await_count == len(frames):
                bridge._stop.set()

        def dispatch_send(coroutine, loop):
            self.assertIs(loop, bridge.loop)
            asyncio.run(coroutine)
            return completed

        completed.result.side_effect = complete_send
        try:
            with patch("remotevoice.audio.sc.get_microphone") as get_microphone, patch(
                "remotevoice.audio.sc.get_speaker"
            ) as get_speaker, patch(
                "remotevoice.audio.asyncio.run_coroutine_threadsafe", side_effect=dispatch_send
            ) as dispatch, patch.object(bridge, "_report_failure") as report_failure:
                source = get_microphone.return_value
                recorder = source.recorder.return_value.__enter__.return_value
                recorder.record.side_effect = frames
                self.assertFalse(bridge.microphone_enabled)
                bridge._capture_system_audio()

            report_failure.assert_not_called()
            get_microphone.assert_called_once_with("selected-capture", include_loopback=True)
            get_speaker.assert_not_called()
            recorder.record.assert_has_calls([call(numframes=480), call(numframes=480)])
            self.assertEqual(recorder.record.call_count, 2)
            expected_packets = [bytes((SYSTEM_FRAME,)) + frame.tobytes() for frame in frames]
            self.assertEqual(websocket.send.await_args_list, [call(packet) for packet in expected_packets])
            self.assertTrue(all(len(packet) == 1 + 480 * 2 * 4 for packet in expected_packets))
            self.assertEqual((FRAME_SAMPLES, SYSTEM_CHANNELS, FRAME_SAMPLES / SAMPLE_RATE), (480, 2, 0.01))
            self.assertEqual(dispatch.call_count, 2)
            completed.result.assert_has_calls([call(timeout=1.0), call(timeout=1.0)])
            self.assertFalse(bridge.microphone_enabled)
            self.assertTrue(bridge._phone_frames.empty())
            self.assertEqual((bridge.capture_device_id, bridge.playback_device_id),
                             ("selected-capture", "selected-playback"))
            bridge.on_status.assert_called_once_with("audio_active")
            source.recorder.assert_called_once_with(
                samplerate=SAMPLE_RATE, channels=SYSTEM_CHANNELS, blocksize=4800,
            )
        finally:
            bridge.stop()


class AudioWorkerFailureDiagnosticsTests(unittest.TestCase):
    def bridge(self):
        return AudioBridge(
            "capture", "playback", MagicMock(spec=asyncio.AbstractEventLoop),
            MagicMock(), MagicMock(spec=asyncio.Lock), MagicMock(),
        )

    def test_send_timeout_is_identified_without_logging_exception_contents(self):
        bridge = self.bridge()
        pending = MagicMock()
        pending.result.side_effect = TimeoutError("private-device-or-credential")

        def dispatch(coroutine, loop):
            coroutine.close()
            return pending

        with patch("remotevoice.audio.sc.get_microphone") as source, patch(
            "remotevoice.audio.asyncio.run_coroutine_threadsafe", side_effect=dispatch
        ), patch.object(bridge, "_report_failure") as failure, self.assertLogs(
            "remotevoice.audio", level="WARNING"
        ) as logs:
            recorder = source.return_value.recorder.return_value.__enter__.return_value
            recorder.record.return_value = np.zeros((FRAME_SAMPLES, SYSTEM_CHANNELS), dtype="<f4")
            bridge._capture_system_audio()

        self.assertEqual(len(logs.output), 1)
        self.assertIn("audio_worker_failure stage=system_send error_type=TimeoutError", logs.output[0])
        self.assertNotIn("private-device-or-credential", logs.output[0])
        self.assertIn("session=" + bridge.diagnostic_id, logs.output[0])
        pending.result.assert_called_once_with(timeout=1.0)
        failure.assert_called_once()
        bridge.stop()

    def test_each_bridge_has_distinct_identity_shared_with_its_trace(self):
        first, second = self.bridge(), self.bridge()
        self.assertRegex(first.diagnostic_id, r'^[0-9a-f]{32}$')
        self.assertNotEqual(first.diagnostic_id, second.diagnostic_id)
        self.assertEqual(first.diagnostic_id, first._system_timing.session_id)
        first.stop()
        second.stop()

    def test_native_failures_identify_direction_and_preserve_stop(self):
        cases = (
            ("sc.get_microphone", "_capture_system_audio", "system_capture"),
            ("sc.get_speaker", "_play_phone_audio", "phone_playback"),
        )
        for lookup, method, stage in cases:
            with self.subTest(stage=stage):
                bridge = self.bridge()
                with patch("remotevoice.audio." + lookup, side_effect=RuntimeError("private-endpoint")), patch.object(
                    bridge, "_report_failure"
                ) as failure, self.assertLogs("remotevoice.audio", level="WARNING") as logs:
                    getattr(bridge, method)()
                self.assertEqual(len(logs.output), 1)
                self.assertIn(f"audio_worker_failure stage={stage} error_type=RuntimeError", logs.output[0])
                self.assertNotIn("private-endpoint", logs.output[0])
                failure.assert_called_once()
                bridge.stop()

    def test_shutdown_does_not_report_expected_native_exception(self):
        bridge = self.bridge()
        bridge._stop.set()
        with patch("remotevoice.audio.sc.get_microphone", side_effect=RuntimeError("closed")), patch.object(
            bridge, "_report_failure"
        ) as failure, self.assertNoLogs("remotevoice.audio", level="WARNING"):
            bridge._capture_system_audio()
        failure.assert_not_called()
        bridge.stop()


class AudioLifecycleTests(unittest.TestCase):
    def bridge(self):
        bridge = AudioBridge(
            "capture", "playback", MagicMock(spec=asyncio.AbstractEventLoop),
            MagicMock(), MagicMock(spec=asyncio.Lock), MagicMock(),
        )
        bridge._STOP_JOIN_TIMEOUT_SECONDS = 0.02
        return bridge

    def test_incomplete_stop_retains_workers_and_repeated_stop_confirms_exit(self):
        bridge = self.bridge()
        release = threading.Event()
        started = threading.Event()

        def wait():
            started.set()
            release.wait()

        worker = threading.Thread(target=wait, name="inert-audio-worker", daemon=True)
        worker.start()
        bridge._threads.append(worker)
        try:
            self.assertTrue(started.wait(timeout=1.0))
            bridge.set_microphone_enabled(True)
            bridge.submit_phone_audio(bytes(FRAME_SAMPLES * 4))
            with self.assertRaises(AudioStopError):
                bridge.stop()
            self.assertTrue(worker.is_alive())
            self.assertEqual(bridge._threads, [worker])
            self.assertTrue(bridge._stop.is_set())
            self.assertFalse(bridge.microphone_enabled)
            self.assertTrue(bridge._phone_frames.empty())
            with patch("remotevoice.audio.sc.get_microphone") as get_microphone:
                with self.assertRaises(RuntimeError):
                    bridge.start()
                get_microphone.assert_not_called()
        finally:
            release.set()
            worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        bridge.stop()
        self.assertEqual(bridge._threads, [])
        bridge.stop()  # Idempotent once every worker has exited.

    def test_start_cannot_replace_running_worker_handles(self):
        bridge = self.bridge()
        release = threading.Event()
        with patch("remotevoice.audio.sc.get_microphone", return_value=SimpleNamespace(
            name="VoiceMeeter Output"
        )) as get_microphone, patch("remotevoice.audio.sc.get_speaker", return_value=SimpleNamespace(
            name="CABLE Input"
        )), patch.object(bridge, "_capture_system_audio", side_effect=release.wait), patch.object(
            bridge, "_play_phone_audio", side_effect=release.wait
        ):
            bridge.start()
            workers = list(bridge._threads)
            try:
                self.assertEqual(len(workers), 2)
                with self.assertRaises(RuntimeError):
                    bridge.start()
                self.assertEqual(bridge._threads, workers)
                get_microphone.assert_called_once()
                with self.assertRaises(AudioStopError):
                    bridge.stop()
                self.assertEqual(bridge._threads, workers)
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=1.0)
            bridge.stop()
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            with self.assertRaises(RuntimeError):
                bridge.start()  # A bridge belongs to exactly one session.

    def test_partial_startup_preserves_started_worker_for_cleanup(self):
        bridge = self.bridge()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, name="inert-partial-start", daemon=True)
        unstarted = threading.Thread(target=lambda: None, daemon=True)
        with patch("remotevoice.audio.sc.get_microphone", return_value=SimpleNamespace(
            name="VoiceMeeter Output"
        )), patch("remotevoice.audio.sc.get_speaker", return_value=SimpleNamespace(
            name="CABLE Input"
        )), patch("remotevoice.audio.threading.Thread", side_effect=[worker, unstarted]), patch.object(
            unstarted, "start", side_effect=RuntimeError("controlled startup failure")
        ):
            try:
                with self.assertRaisesRegex(RuntimeError, "controlled startup failure"):
                    bridge.start()
                self.assertEqual(bridge._threads, [worker])
                self.assertTrue(bridge._stop.is_set())
                with self.assertRaises(AudioStopError):
                    bridge.stop()
            finally:
                release.set()
                worker.join(timeout=1.0)
            bridge.stop()
            self.assertFalse(worker.is_alive())
            self.assertIsNone(unstarted.ident)
            self.assertEqual(bridge._threads, [])

    def test_each_native_direction_requires_progress_even_without_phone_speech(self):
        bridge = self.bridge()
        bridge._progress = {'capture': 0.0, 'playback': 0.0}
        with patch('remotevoice.audio.time.monotonic', return_value=1.0):
            bridge.assert_progress()
        with patch('remotevoice.audio.time.monotonic', return_value=2.1):
            bridge._mark_progress('capture')
            with self.assertRaisesRegex(RuntimeError, 'Аудиоустройство'):
                bridge.assert_progress()
            bridge._mark_progress('playback')
            bridge.assert_progress()
        bridge.stop()

    def test_cancelled_native_lookup_cannot_start_workers_later(self):
        bridge = self.bridge()
        def lookup(*args, **kwargs):
            bridge._stop.set()
            return SimpleNamespace(name='VoiceMeeter Output')
        with patch('remotevoice.audio.sc.get_microphone', side_effect=lookup), patch(
            'remotevoice.audio.sc.get_speaker', return_value=SimpleNamespace(name='CABLE Input')
        ), patch('remotevoice.audio.threading.Thread') as thread:
            with self.assertRaisesRegex(RuntimeError, 'отменён'):
                bridge.start()
        thread.assert_not_called()


class AudioInputValidationTests(unittest.TestCase):
    def bridge(self):
        return AudioBridge(
            "capture", "playback", MagicMock(spec=asyncio.AbstractEventLoop),
            MagicMock(), MagicMock(spec=asyncio.Lock), MagicMock(),
        )

    def test_nonfinite_microphone_frames_never_reach_queue_or_player(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), patch("remotevoice.audio.sc.get_speaker") as get_speaker:
                bridge = self.bridge()
                bridge.set_microphone_enabled(True)
                samples = np.zeros(FRAME_SAMPLES, dtype="<f4")
                samples[FRAME_SAMPLES // 2] = value
                bridge.submit_phone_audio(samples.tobytes())
                self.assertTrue(bridge._phone_frames.empty())
                player = get_speaker.return_value.player.return_value.__enter__.return_value
                played = []

                def play(frame):
                    played.append(frame.copy())
                    bridge._stop.set()

                player.play.side_effect = play
                bridge._play_phone_audio()
                self.assertEqual(len(played), 1)
                np.testing.assert_array_equal(played[0], np.zeros((FRAME_SAMPLES, SYSTEM_CHANNELS)))
                bridge.stop()

    def test_finite_headroom_is_preserved_without_clipping(self):
        bridge = self.bridge()
        bridge.set_microphone_enabled(True)
        samples = np.linspace(-2.0, 2.0, FRAME_SAMPLES, dtype="<f4")
        payload = samples.tobytes()
        bridge.submit_phone_audio(memoryview(payload))
        self.assertEqual(bridge._phone_frames.get_nowait(), payload)
        bridge.stop()


if __name__ == "__main__":
    unittest.main()
