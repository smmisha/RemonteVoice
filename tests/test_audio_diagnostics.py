import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from remotevoice.audio import AudioBridge, _AudioDiagnostics
from remotevoice.protocol import FRAME_SAMPLES, MIC_CHANNELS, SYSTEM_CHANNELS


class AudioDiagnosticsTests(unittest.TestCase):
    def bridge(self):
        return AudioBridge(
            "capture-PRIVATE-ENDPOINT",
            "playback-PRIVATE-ENDPOINT",
            MagicMock(spec=asyncio.AbstractEventLoop),
            MagicMock(),
            MagicMock(spec=asyncio.Lock),
            MagicMock(),
        )

    def test_disabled_by_default_and_requires_exact_opt_in(self):
        for setting in (None, "", "0", "true", "01"):
            with self.subTest(setting=setting), patch.dict(os.environ, {}, clear=False):
                if setting is None:
                    os.environ.pop("REMOTEVOICE_AUDIO_DIAGNOSTICS", None)
                else:
                    os.environ["REMOTEVOICE_AUDIO_DIAGNOSTICS"] = setting
                with self.assertNoLogs("remotevoice.audio", level="INFO"):
                    bridge = self.bridge()
                    self.assertIsNone(bridge._diagnostics)
                    bridge.set_microphone_enabled(True)
                    bridge.submit_phone_audio(np.ones(FRAME_SAMPLES, dtype="<f4").tobytes())
                    bridge.stop()

    def test_enabled_aggregates_interval_and_flushes_remaining_counters_once(self):
        with patch("remotevoice.audio.time.monotonic", return_value=10.0) as clock:
            diagnostics = _AudioDiagnostics()
            system = np.array([[0.375, -0.625], [0.1234567, 0.25]], dtype="<f4")
            received = np.array([0.25, -0.875, 0.3333333], dtype="<f4").tobytes()
            played = np.array([[0.125, -0.75]], dtype="<f4")
            with self.assertNoLogs("remotevoice.audio", level="INFO"):
                diagnostics.record_system(system)
                diagnostics.record_received(received)
                diagnostics.record_played(played, zero_fill=False)
            clock.return_value = 13.01
            with self.assertLogs("remotevoice.audio", level="INFO") as logs:
                diagnostics.record_played(np.zeros((1, 2)), zero_fill=True)
            self.assertEqual(len(logs.records), 1)
            message = logs.records[0].getMessage()
            for metric in (
                "final=False", "interval_s=3.010", "system_frames=1", "system_peak=0.625000",
                "mic_frames_rx=1", "rx_peak=0.875000", "played_nonempty=1",
                "play_peak=0.750000", "zero_fill=1",
            ):
                self.assertIn(metric, message)
            self.assertNotIn("0.1234567", message)
            self.assertNotIn("0.3333333", message)
            self.assertNotIn(repr(received), message)
            self.assertNotIn(received.hex(), message)
            clock.return_value = 14.0
            diagnostics.record_system(np.array([0.5], dtype="<f4"))
            with self.assertLogs("remotevoice.audio", level="INFO") as final_logs:
                diagnostics.flush()
                diagnostics.flush()
                diagnostics.record_received(received)
            self.assertEqual(len(final_logs.records), 1)
            final_message = final_logs.records[0].getMessage()
            self.assertIn("final=True", final_message)
            self.assertIn("system_frames=1 system_peak=0.500000", final_message)
            self.assertIn("mic_frames_rx=0 rx_peak=0.000000", final_message)
            self.assertIn("played_nonempty=0 play_peak=0.000000 zero_fill=0", final_message)

    def test_bridge_counts_only_admitted_microphone_frames_and_stop_flushes_without_payload(self):
        with patch.dict(os.environ, {"REMOTEVOICE_AUDIO_DIAGNOSTICS": "1"}), patch(
            "remotevoice.audio.time.monotonic", return_value=1.0
        ):
            bridge = self.bridge()
            payload = np.full((FRAME_SAMPLES, MIC_CHANNELS), 0.25, dtype="<f4").tobytes()
            bridge.submit_phone_audio(payload)  # Muted: not admitted.
            bridge.set_microphone_enabled(True)
            bridge.submit_phone_audio(b"TOKEN-NEVER-LOG-THIS")  # Wrong frame length.
            bridge.submit_phone_audio(memoryview(payload))
            bridge.submit_phone_audio(payload)
            with self.assertLogs("remotevoice.audio", level="INFO") as logs:
                bridge.stop()
                bridge.submit_phone_audio(payload)  # Stopped: not admitted.
                bridge.stop()
            self.assertEqual(len(logs.records), 1)
            message = logs.records[0].getMessage()
            self.assertIn("mic_frames_rx=2 rx_peak=0.250000", message)
            for private_value in ("PRIVATE-ENDPOINT", "TOKEN-NEVER-LOG-THIS", repr(payload), payload.hex()):
                self.assertNotIn(private_value, message)

    def test_play_metrics_follow_successful_player_calls_and_distinguish_zero_fill(self):
        with patch.dict(os.environ, {"REMOTEVOICE_AUDIO_DIAGNOSTICS": "1"}), patch(
            "remotevoice.audio.sc.get_speaker"
        ) as get_speaker:
            bridge = self.bridge()
            bridge.set_microphone_enabled(True)
            bridge.submit_phone_audio(np.full(FRAME_SAMPLES, 0.5, dtype="<f4").tobytes())
            played = []
            player = get_speaker.return_value.player.return_value.__enter__.return_value

            def play(samples):
                played.append(samples.copy())
                if len(played) == 2:
                    bridge._stop.set()

            player.play.side_effect = play
            bridge._play_phone_audio()
            self.assertEqual(len(played), 2)
            np.testing.assert_array_equal(played[0], np.full((FRAME_SAMPLES, SYSTEM_CHANNELS), 0.5))
            np.testing.assert_array_equal(played[1], np.zeros((FRAME_SAMPLES, SYSTEM_CHANNELS)))
            with self.assertLogs("remotevoice.audio", level="INFO") as logs:
                bridge.stop()
            message = logs.records[0].getMessage()
            self.assertIn("played_nonempty=1 play_peak=0.500000 zero_fill=1", message)

    def test_capture_metrics_are_recorded_before_network_dispatch(self):
        with patch.dict(os.environ, {"REMOTEVOICE_AUDIO_DIAGNOSTICS": "1"}), patch(
            "remotevoice.audio.sc.get_microphone"
        ) as get_microphone, patch("remotevoice.audio.asyncio.run_coroutine_threadsafe") as dispatch:
            bridge = self.bridge()
            recorder = get_microphone.return_value.recorder.return_value.__enter__.return_value
            recorder.record.return_value = np.full((FRAME_SAMPLES, SYSTEM_CHANNELS), 0.75, dtype="<f4")

            def send(coroutine, loop):
                self.assertEqual(bridge._diagnostics._system_frames, 1)
                self.assertEqual(bridge._diagnostics._system_peak, 0.75)
                coroutine.close()
                bridge._stop.set()
                return MagicMock()

            dispatch.side_effect = send
            bridge._capture_system_audio()
            dispatch.assert_called_once()
            with self.assertLogs("remotevoice.audio", level="INFO") as logs:
                bridge.stop()
            self.assertIn("system_frames=1 system_peak=0.750000", logs.records[0].getMessage())

    def test_failed_player_call_is_not_reported_as_played(self):
        with patch.dict(os.environ, {"REMOTEVOICE_AUDIO_DIAGNOSTICS": "1"}), patch(
            "remotevoice.audio.sc.get_speaker"
        ) as get_speaker:
            bridge = self.bridge()
            bridge.set_microphone_enabled(True)
            bridge.submit_phone_audio(np.full(FRAME_SAMPLES, 0.5, dtype="<f4").tobytes())
            player = get_speaker.return_value.player.return_value.__enter__.return_value
            player.play.side_effect = RuntimeError("test player failed")
            with patch.object(bridge, "_report_failure"):
                bridge._play_phone_audio()
            with self.assertLogs("remotevoice.audio", level="INFO") as logs:
                bridge.stop()
            self.assertIn("played_nonempty=0 play_peak=0.000000 zero_fill=0", logs.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
