import asyncio
import struct
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from remotevoice.audio import AudioBridge, AudioStopError
from remotevoice.audio_backend import DriverAudioBackend, DriverBackendSnapshot
from remotevoice.driver_audio_bridge import DriverAudioBridge


FRAME = struct.pack("<480f", *([0.25] * 480))
STATUS = DriverBackendSnapshot(1, True, True, "exact-render-id", "exact-mic-id", 10.0)


def wait_for(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Bridge did not reach expected state")
        time.sleep(0.002)


class Sink:
    def __init__(self):
        self.started = False
        self.available = False
        self.controls = []
        self.payloads = []
        self.stop_calls = []
        self.stop_fails = False
        self.control_result = True
        self.control_ready = True
        self.cleanup_confirmed = False
        self.on_control = self.on_submit = None

    @property
    def snapshot(self):
        return SimpleNamespace(cleanup_confirmed=self.cleanup_confirmed)

    def start(self):
        self.started = self.available = True

    def is_available(self):
        return self.available

    def set_enabled(self, enabled, timeout):
        self.controls.append(enabled)
        if self.on_control:
            self.on_control(enabled)
        self.available = self.control_ready
        return self.control_result

    def submit(self, payload):
        self.payloads.append(payload)
        if self.on_submit:
            self.on_submit()
        return self.available

    def stop(self, timeout):
        self.stop_calls.append(timeout)
        self.available = False
        if self.stop_fails:
            raise RuntimeError("native handle still owned")
        self.cleanup_confirmed = True


class DriverAudioBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridges = []
        self.releases = []
        self.status = STATUS
        self.observe = Mock(side_effect=lambda: self.status)
        self.backend = DriverAudioBackend(self.observe, clock=lambda: 10.0)
        self.sink = Sink()
        self.factory = Mock(return_value=self.sink)

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.sink.stop_fails = False
        for bridge in self.bridges:
            bridge.stop()
            self.assertEqual(bridge._threads, [])

    def bridge(self, capture="exact-render-id", microphone="exact-mic-id"):
        bridge = DriverAudioBridge(capture, microphone,
            MagicMock(spec=asyncio.AbstractEventLoop),
            SimpleNamespace(close=AsyncMock(), send=AsyncMock()),
            MagicMock(spec=asyncio.Lock), Mock(),
            driver_backend=self.backend, sink_factory=self.factory)
        bridge._STOP_JOIN_TIMEOUT_SECONDS = 0.02
        self.bridges.append(bridge)
        return bridge

    def validate(self, bridge):
        with patch("remotevoice.driver_audio_bridge.sc.get_microphone", return_value=SimpleNamespace(
                id="exact-render-id", name="RemoteVoice Render", isloopback=True)):
            bridge._validate_audio_devices()

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def test_constructor_is_inert_and_startup_confirms_muted_sink_without_speaker(self):
        with patch("remotevoice.driver_audio_bridge.sc.get_microphone") as capture, patch(
                "remotevoice.driver_audio_bridge.sc.get_speaker") as speaker:
            bridge = self.bridge()
            self.factory.assert_not_called()
            self.observe.assert_not_called()
            capture.assert_not_called()
            speaker.assert_not_called()
            capture.return_value = SimpleNamespace(id="exact-render-id", name="RemoteVoice Render")
            with patch.object(bridge, "_launch_audio_workers") as launch:
                bridge.start()
            launch.assert_called_once()
            capture.assert_called_once_with("exact-render-id", include_loopback=True)
            speaker.assert_not_called()
        self.assertTrue(self.sink.started)
        self.assertEqual(self.sink.controls, [False])
        self.assertFalse(bridge.microphone_enabled)
        bridge.submit_phone_audio(FRAME)
        self.assertTrue(bridge._phone_frames.empty())

    def test_untrusted_or_mismatched_ids_fail_before_any_device_or_sink(self):
        for status, ids in ((replace(STATUS, broker_authenticated=False), ("exact-render-id", "exact-mic-id")),
                            (replace(STATUS, endpoints_verified=False), ("exact-render-id", "exact-mic-id")),
                            (replace(STATUS, observed_at=8.0), ("exact-render-id", "exact-mic-id")),
                            (STATUS, ("forged-render", "exact-mic-id")),
                            (STATUS, ("exact-render-id", "forged-mic"))):
            with self.subTest(status=status, ids=ids):
                self.status = status
                bridge = self.bridge(*ids)
                with patch("remotevoice.driver_audio_bridge.sc.get_microphone") as capture, patch(
                        "remotevoice.driver_audio_bridge.sc.get_speaker") as speaker:
                    with self.assertRaises(RuntimeError):
                        bridge._validate_audio_devices()
                    capture.assert_not_called()
                    speaker.assert_not_called()
        self.factory.assert_not_called()

    def test_friendly_virtual_name_cannot_substitute_for_exact_returned_capture_id(self):
        bridge = self.bridge()
        with patch("remotevoice.driver_audio_bridge.sc.get_microphone", return_value=SimpleNamespace(
                id="physical-id", name="RemoteVoice Render")), patch(
                "remotevoice.driver_audio_bridge.sc.get_speaker") as speaker:
            with self.assertRaises(RuntimeError):
                bridge._validate_audio_devices()
            speaker.assert_not_called()
        self.factory.assert_not_called()

    def test_capture_worker_rechecks_backend_before_shared_exact_id_capture(self):
        bridge = self.bridge()
        self.status = replace(STATUS, endpoints_verified=False)
        with patch.object(AudioBridge, "_capture_system_audio") as capture, patch.object(
                bridge, "_report_failure") as failed:
            bridge._capture_system_audio()
        capture.assert_not_called()
        failed.assert_called_once()
        self.status = STATUS
        with patch.object(AudioBridge, "_capture_system_audio") as capture:
            bridge._capture_system_audio()
        capture.assert_called_once_with()

    def test_enable_requires_sink_readiness_and_failure_keeps_phone_ingress_muted(self):
        bridge = self.bridge()
        self.validate(bridge)
        self.sink.control_ready = False  # ACK True alone must not enable phone input.
        with self.assertRaisesRegex(RuntimeError, "готовность"):
            bridge.set_microphone_enabled(True)
        self.assertFalse(bridge.microphone_enabled)
        bridge.submit_phone_audio(FRAME)
        self.assertTrue(bridge._phone_frames.empty())
        self.sink.control_ready = True
        self.sink.control_result = False
        with self.assertRaises(RuntimeError):
            bridge.set_microphone_enabled(True)
        self.assertFalse(bridge.microphone_enabled)

    def test_enable_disable_clears_queue_and_only_forwards_exact_finite_mono_pcm(self):
        bridge = self.bridge()
        self.validate(bridge)
        bridge.set_microphone_enabled(True)
        self.assertTrue(bridge.microphone_enabled)
        bridge.submit_phone_audio(FRAME)
        self.assertFalse(bridge._phone_frames.empty())
        bridge.set_microphone_enabled(False)
        self.assertTrue(bridge._phone_frames.empty())
        self.assertFalse(bridge.microphone_enabled)
        bridge.set_microphone_enabled(True)
        bridge.submit_phone_audio(struct.pack("<480f", *([float("nan")] * 480)))
        self.assertTrue(bridge._phone_frames.empty())
        bridge.submit_phone_audio(FRAME)
        self.sink.on_submit = bridge._stop.set
        with patch("remotevoice.driver_audio_bridge.sc.get_speaker") as speaker:
            bridge._play_phone_audio()
        speaker.assert_not_called()
        self.assertEqual(self.sink.payloads, [FRAME])
        self.assertIn("playback", bridge._progress)

    def test_stop_during_enable_cannot_apply_a_late_ready_result(self):
        bridge = self.bridge()
        self.validate(bridge)
        entered, release = self.event(), self.event()
        self.sink.on_control = lambda enabled: (entered.set(), release.wait(1))
        errors = []
        def enable():
            try:
                bridge.set_microphone_enabled(True)
            except RuntimeError as error:
                errors.append(str(error))
        worker = threading.Thread(target=enable)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            bridge.stop()
            release.set()
            worker.join(1)
            self.assertEqual(len(errors), 1)
            self.assertFalse(bridge.microphone_enabled)
            bridge.submit_phone_audio(FRAME)
            self.assertTrue(bridge._phone_frames.empty())
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_enable_transition_has_bounded_health_grace_then_reports_audio_failure(self):
        bridge = self.bridge()
        self.validate(bridge)
        self.sink.available = False
        bridge._transition_deadline = time.monotonic() + 0.05
        failed = threading.Event()
        with patch.object(bridge, "_report_failure", side_effect=lambda message: (
                bridge._stop.set(), failed.set())) as report:
            worker = threading.Thread(target=bridge._play_phone_audio)
            worker.start()
            try:
                self.assertFalse(failed.wait(0.01))
                self.assertTrue(failed.wait(0.3))
            finally:
                bridge._stop.set()
                worker.join(1)
        report.assert_called_once()
        self.assertFalse(worker.is_alive())

    def test_telemetry_failure_uses_existing_audio_error_and_websocket_close(self):
        bridge = self.bridge()
        self.validate(bridge)
        self.sink.available = False
        dispatched = []
        def dispatch(coroutine, loop):
            dispatched.append(loop)
            # AsyncMock completes without suspension; no native event loop or
            # socketpair is needed for this inert websocket-close assertion.
            try:
                coroutine.send(None)
            except StopIteration:
                pass
            return Mock()
        with patch("remotevoice.audio.asyncio.run_coroutine_threadsafe", side_effect=dispatch):
            bridge._play_phone_audio()
        self.assertTrue(bridge._stop.is_set())
        self.assertFalse(bridge.microphone_enabled)
        self.assertEqual(dispatched, [bridge.loop])
        self.assertTrue(bridge.on_status.call_args[0][0].startswith("audio_error: "))
        bridge.websocket.close.assert_awaited_once_with(code=1011, reason="Audio device error")
        self.assertIn(0, self.sink.stop_calls)

    def test_stop_attempts_both_paths_retains_failed_sink_and_live_capture_workers(self):
        bridge = self.bridge()
        self.validate(bridge)
        release = self.event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        bridge._threads.append(worker)
        self.sink.stop_fails = True
        try:
            with self.assertRaises(AudioStopError):
                bridge.stop()
            self.assertIs(bridge._sink, self.sink)
            self.assertEqual(bridge._threads, [worker])
            self.assertTrue(worker.is_alive())
            self.assertTrue(self.sink.stop_calls)
            self.assertFalse(bridge.microphone_enabled)
        finally:
            self.sink.stop_fails = False
            release.set()
            worker.join(1)
        bridge.stop()
        self.assertTrue(self.sink.cleanup_confirmed)
        self.assertEqual(bridge._threads, [])

    def test_startup_failure_retains_sink_and_attempts_cleanup(self):
        bridge = self.bridge()
        self.sink.control_result = False
        self.sink.stop_fails = True
        with patch("remotevoice.driver_audio_bridge.sc.get_microphone", return_value=SimpleNamespace(
                id="exact-render-id", name="RemoteVoice Render")):
            with self.assertRaises(AudioStopError):
                bridge._validate_audio_devices()
        self.assertIs(bridge._sink, self.sink)
        self.assertTrue(bridge._stop.is_set())
        self.assertFalse(bridge.microphone_enabled)
        self.assertTrue(self.sink.stop_calls)


if __name__ == "__main__":
    unittest.main()
