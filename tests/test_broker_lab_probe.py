import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from remotevoice import broker_lab_probe as probe
from remotevoice.broker_pipe_client import BrokerClientOpenError, BrokerObservation
from remotevoice.windows_driver_endpoints import DriverEndpointPair


class Client:
    def __init__(self, events):
        self.events = events
        self.enabled = self.cleanup_confirmed = False
        self.controls = []
        self.frames = []
        self.close_error = None
        self.status_override = None

    def observe(self):
        return self.status_override or BrokerObservation(True, self.enabled, self.enabled, False, time.monotonic())

    def heartbeat(self):
        return True

    def set_microphone_enabled(self, enabled):
        self.controls.append(enabled)
        self.enabled = enabled
        return True

    def submit(self, payload):
        self.frames.append(payload)
        return True

    def close(self):
        self.events.append("client close")
        if self.close_error:
            raise self.close_error
        self.cleanup_confirmed = True
        self.enabled = False


class Recorder:
    def __init__(self, client):
        self.client = client
        self.frames = 0
        self.entered = self.exited = False
        self.malformed = False
        self.enter_error = self.exit_error = None
        self.block = None
        self.ignore_mute = False
        self.tail_seconds = 0.0
        self.disabled_at = None

    def __enter__(self):
        if self.enter_error:
            raise self.enter_error
        self.entered = True
        return self

    def record(self, numframes):
        if self.block:
            self.block.wait(3)
        time.sleep(numframes / 48000)
        if self.malformed:
            return np.full((numframes, 1), np.nan)
        if not self.client.enabled:
            if self.disabled_at is None:
                self.disabled_at = time.monotonic()
            if not self.ignore_mute and time.monotonic() - self.disabled_at >= self.tail_seconds:
                return np.zeros((numframes, 1))
        samples = 0.18 * np.sin(2 * np.pi * 900 * np.arange(self.frames, self.frames + numframes) / 48000)
        self.frames += numframes
        return samples.reshape(numframes, 1)

    def __exit__(self, *args):
        if self.exit_error:
            raise self.exit_error
        self.exited = True


class BrokerLabProbeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.client = Client(self.events)
        self.manifest = Mock(is_verified=True, cleanup_confirmed=False)
        self.manifest.load.return_value = SimpleNamespace(driver_instance_id=r"ROOT\MEDIA\0000")
        self.manifest.close.side_effect = self.close_manifest
        self.inventory = Mock(cleanup_confirmed=True)
        self.inventory.observe.return_value = DriverEndpointPair("verified-render", "verified-virtual-mic")
        self.recorder = Recorder(self.client)
        self.source = SimpleNamespace(id="verified-virtual-mic", isloopback=False,
                                      recorder=Mock(return_value=self.recorder))
        self.patches = [
            patch.object(probe, "open_installed_broker_client", return_value=self.client),
            patch.object(probe, "PinnedInstalledManifest", return_value=self.manifest),
            patch.object(probe, "WindowsDriverEndpointInventory", return_value=self.inventory),
            patch.object(probe, "_capture_source", return_value=self.source),
        ]
        self.open_client, self.make_manifest, self.make_inventory, self.get_capture = [
            item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.assertEqual(probe._RETAINED, [])

    def close_manifest(self):
        self.events.append("manifest close")
        self.manifest.cleanup_confirmed = True

    def tearDown(self):
        if self.recorder.block:
            self.recorder.block.set()
        for client, manifest, inventory, capture in probe._RETAINED:
            if capture:
                capture.stop_requested.set()
                if capture.thread.ident is not None:
                    capture.thread.join(1)
                self.assertFalse(capture.thread.is_alive())
        probe._RETAINED.clear()  # Only inert fake owners are used in this test module.

    def test_default_is_muted_ipc_only_and_report_contains_only_sanitized_scalars(self):
        report = probe.run_probe()
        self.assertTrue(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertIsNone(report["error"])
        self.assertEqual((report["observations"], report["heartbeats"]), (3, 3))
        self.assertEqual(self.client.controls, [])
        self.assertEqual(self.client.frames, [])
        self.assertFalse(report["silence_verified"])
        self.get_capture.assert_not_called()
        self.make_manifest.assert_not_called()
        self.make_inventory.assert_not_called()
        self.assertTrue(self.client.cleanup_confirmed)
        self.assertTrue(all(type(value) in (str, bool, int, float, type(None)) for value in report.values()))
        self.assertNotIn("verified-virtual-mic", repr(report))

    def test_synthetic_uses_only_verified_capture_and_reduces_tone_to_scalar_evidence(self):
        report = probe.run_probe(synthetic_pcm=True)
        self.assertTrue(report["success"], report)
        self.assertTrue(report["cleanup_confirmed"])
        self.make_manifest.assert_called_once_with("client")
        self.make_inventory.assert_called_once_with(r"ROOT\MEDIA\0000")
        self.get_capture.assert_called_once_with("verified-virtual-mic")
        self.source.recorder.assert_called_once_with(samplerate=48000, channels=1, blocksize=4800)
        self.assertEqual(self.client.controls, [True, False])
        self.assertEqual(len(self.client.frames), 50)
        samples = np.frombuffer(b"".join(self.client.frames), dtype="<f4")
        expected = 0.18 * np.sin(2 * np.pi * 900 * np.arange(len(samples)) / 48000)
        np.testing.assert_allclose(samples, expected, atol=1e-7)
        self.assertGreaterEqual(report["captured_frames"], 12000)
        self.assertGreater(report["tone_fraction"], 0.9)
        self.assertTrue(report["silence_verified"])
        self.assertGreaterEqual(report["silence_frames"], 9600)
        self.assertEqual((report["silence_rms"], report["silence_peak"]), (0.0, 0.0))
        self.assertTrue(self.recorder.exited)
        self.assertEqual(self.events[-2:], ["client close", "manifest close"])

    def test_inventory_rejection_and_unverified_manifest_prevent_any_audio_open(self):
        self.manifest.is_verified = False
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "manifest_unverified")
        self.get_capture.assert_not_called()
        self.open_client.assert_not_called()
        self.manifest.is_verified = True
        self.inventory.observe.side_effect = OSError("Endpoint not trusted")
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.get_capture.assert_not_called()
        self.open_client.assert_not_called()

    def test_getter_fallback_to_other_or_loopback_device_never_opens_recorder(self):
        self.source.id = "physical-microphone"
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "virtual_capture_identity_mismatch")
        self.assertTrue(report["cleanup_confirmed"])
        self.source.recorder.assert_not_called()
        self.assertEqual(self.client.controls, [True])  # CLOSE releases the explicit lease.
        self.assertTrue(self.client.cleanup_confirmed)

    def test_malformed_or_silent_capture_cannot_pass_synthetic_acceptance(self):
        self.recorder.malformed = True
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "capture_data_invalid")
        self.assertTrue(report["cleanup_confirmed"])
        self.assertTrue(self.recorder.exited)

    def test_silent_capture_is_not_evidence_of_synthetic_pcm(self):
        def silent(numframes):
            time.sleep(numframes / 48000)
            return np.zeros((numframes, 1))
        self.recorder.record = silent
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "synthetic_signal_rejected")
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(report["rms"], 0.0)
        self.assertEqual(report["tone_fraction"], 0.0)

    def test_muted_status_with_continuing_pcm_fails_post_disable_acceptance(self):
        self.recorder.ignore_mute = True
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "post_disable_not_silent")
        self.assertFalse(report["silence_verified"])
        self.assertGreater(report["silence_rms"], 0.1)
        self.assertTrue(self.recorder.exited)

    def test_bounded_tail_is_drained_before_silence_measurement(self):
        self.recorder.tail_seconds = 0.1
        report = probe.run_probe(synthetic_pcm=True)
        self.assertTrue(report["success"], report)
        self.assertTrue(report["silence_verified"])
        self.assertEqual(report["silence_peak"], 0.0)

    def test_recorder_that_finishes_before_silence_cannot_pass(self):
        original = self.recorder.record
        def premature(numframes):
            if not self.client.enabled:
                raise OSError("virtual recorder ended before silence")
            return original(numframes)
        self.recorder.record = premature
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "virtual_capture_failed")
        self.assertFalse(report["silence_verified"])

    def test_stalled_native_capture_is_retained_with_manifest_for_process_exit(self):
        self.recorder.block = threading.Event()
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertFalse(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "silence_capture_timeout")
        self.assertEqual(len(probe._RETAINED), 1)
        self.manifest.close.assert_not_called()
        self.assertTrue(self.client.cleanup_confirmed)
        previous = self.open_client.call_count
        second = probe.run_probe()
        self.assertEqual(second["error"], "prior_cleanup_unconfirmed")
        self.assertEqual(self.open_client.call_count, previous)

    def test_failed_client_close_or_unknown_open_never_releases_manifest_or_claims_clean(self):
        self.client.close_error = OSError("Private device details must not leak")
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["cleanup_confirmed"])
        self.assertFalse(report["success"])
        self.manifest.close.assert_not_called()
        self.assertNotIn("Private", repr(report))

    def test_failed_open_with_explicit_clean_owner_can_report_failed_but_clean(self):
        self.open_client.side_effect = BrokerClientOpenError(self.client)
        report = probe.run_probe()
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "broker_open_failed")
        self.assertTrue(self.client.cleanup_confirmed)

    def test_unknown_open_owner_requires_process_exit_without_replacement(self):
        self.open_client.side_effect = OSError("Unknown partial construction")
        report = probe.run_probe()
        self.assertFalse(report["success"])
        self.assertFalse(report["cleanup_confirmed"])
        self.assertEqual(len(probe._RETAINED), 1)

    def test_capture_enter_or_exit_failure_cannot_claim_native_cleanup(self):
        self.recorder.enter_error = OSError("Partial native recorder")
        report = probe.run_probe(synthetic_pcm=True)
        self.assertFalse(report["success"])
        self.assertFalse(report["cleanup_confirmed"])
        self.assertFalse(self.recorder.exited)
        self.manifest.close.assert_not_called()

    def test_fatal_client_failure_is_sanitized_and_retained_without_more_client_calls(self):
        class Fatal(BaseException):
            pass
        self.client.observe = Mock(side_effect=Fatal("Private token failure"))
        self.client.close = Mock()
        report = probe.run_probe()
        self.assertFalse(report["success"])
        self.assertFalse(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "fatal_probe_error")
        self.assertNotIn("Private", repr(report))
        self.client.close.assert_not_called()

    def test_bad_or_stale_broker_observation_fails_without_microphone_enable(self):
        self.client.status_override = BrokerObservation(True, False, False, False, 0.0)
        report = probe.run_probe()
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "broker_status_invalid")
        self.assertEqual(self.client.controls, [])
        self.assertTrue(report["cleanup_confirmed"])

    def test_invalid_opt_in_argument_has_no_factory_or_audio_effects(self):
        report = probe.run_probe(synthetic_pcm=1)
        self.assertFalse(report["success"])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(report["error"], "invalid_probe_argument")
        self.open_client.assert_not_called()
        self.get_capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
