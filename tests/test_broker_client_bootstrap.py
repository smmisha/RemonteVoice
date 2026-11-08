import threading
import unittest
from unittest.mock import Mock, patch

from remotevoice.broker_client_bootstrap import (
    BootstrapBrokerClient, BrokerClientBootstrapError, open_installed_broker_client,
)
from remotevoice.broker_mic_sink import BrokerMicSink, BrokerSinkStopError
from remotevoice.broker_pipe_client import BrokerClientOpenError, BrokerObservation

STATUS = BrokerObservation(True, False, False, False, 10.0)


class FatalIdentity(BaseException):
    pass


class BootstrapClientTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.installation = object()
        self.manifest = Mock(is_verified=True, cleanup_confirmed=False)
        self.manifest.load.side_effect = lambda: self.installation
        self.manifest.close.side_effect = self.close_manifest
        self.client = Mock(cleanup_confirmed=False)
        self.client.open.side_effect = lambda **kwargs: self.client
        self.client.close.side_effect = self.close_client
        self.client.observe.return_value = STATUS
        self.manifest_type = patch("remotevoice.broker_client_bootstrap.PinnedInstalledManifest", return_value=self.manifest)
        self.client_type = patch("remotevoice.broker_client_bootstrap.InstalledBrokerClient", return_value=self.client)
        self.make_manifest, self.make_client = self.manifest_type.start(), self.client_type.start()
        self.addCleanup(self.manifest_type.stop)
        self.addCleanup(self.client_type.stop)

    def close_manifest(self):
        self.events.append("manifest close")
        self.manifest.cleanup_confirmed = True

    def close_client(self):
        self.events.append("client close")
        self.client.cleanup_confirmed = True

    def test_inert_single_use_open_holds_verified_client_manifest_until_inner_cleanup(self):
        owner = BootstrapBrokerClient()
        self.assertTrue(owner.cleanup_confirmed)
        self.assertIsNone(owner.observation)
        self.make_manifest.assert_not_called()
        self.make_client.assert_not_called()
        self.assertIs(owner.open(0.2), owner)
        self.make_manifest.assert_called_once_with("client")
        self.make_client.assert_called_once_with(self.installation)
        self.client.open.assert_called_once_with(timeout=0.2)
        self.client.set_microphone_enabled.assert_not_called()
        self.manifest.close.assert_not_called()
        self.assertFalse(owner.cleanup_confirmed)
        with self.assertRaises(BrokerClientBootstrapError):
            owner.open()
        owner.close()
        self.assertEqual(self.events, ["client close", "manifest close"])
        self.assertTrue(owner.cleanup_confirmed)
        owner.close()
        self.assertEqual(self.events, ["client close", "manifest close"])

    def test_methods_delegate_on_owner_worker_and_observation_is_cached_only(self):
        owner = open_installed_broker_client()
        self.assertIs(owner.observe(), STATUS)
        self.assertIs(owner.observation, STATUS)
        self.client.observation = property(lambda self: self.fail("Must never inspect inner cache"))
        self.assertIs(owner.observation, STATUS)
        owner.heartbeat()
        owner.submit(bytes(1920))
        owner.set_microphone_enabled(True)
        self.assertIsNone(owner.observation)
        self.client.heartbeat.assert_called_once_with()
        self.client.submit.assert_called_once_with(bytes(1920))
        self.client.set_microphone_enabled.assert_called_once_with(True)
        owner.close()

    def test_unverified_manifest_never_constructs_client_and_failure_exposes_outer_owner(self):
        for verified in (False, None, 1, "verified"):
            with self.subTest(verified=verified):
                self.manifest.is_verified = verified
                owner = BootstrapBrokerClient()
                with self.assertRaises(BrokerClientOpenError) as raised:
                    owner.open()
                self.assertIs(raised.exception.cleanup_owner, owner)
                self.assertTrue(owner.cleanup_confirmed)
        self.make_client.assert_not_called()

    def test_manifest_load_failure_closes_retained_owner_without_constructing_client(self):
        self.manifest.load.side_effect = RuntimeError("Manifest rejected")
        owner = BootstrapBrokerClient()
        with self.assertRaises(BrokerClientOpenError) as raised:
            owner.open()
        self.assertIs(raised.exception.cleanup_owner, owner)
        self.make_client.assert_not_called()
        self.manifest.close.assert_called_once_with()
        self.assertTrue(owner.cleanup_confirmed)

    def test_failed_open_retains_outer_owner_and_manifest_until_inner_close_retry(self):
        self.client.open.side_effect = OSError("Pipe handshake failed")
        self.client.close.side_effect = OSError("Cancelled I/O still pending")
        owner = BootstrapBrokerClient()
        with self.assertRaises(BrokerClientOpenError) as raised:
            owner.open()
        self.assertIs(raised.exception.cleanup_owner, owner)
        self.assertFalse(owner.cleanup_confirmed)
        self.assertIs(owner._client, self.client)
        self.assertIs(owner._manifest, self.manifest)
        self.manifest.close.assert_not_called()
        self.client.close.side_effect = self.close_client
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)
        self.assertEqual(self.events, ["client close", "manifest close"])

    def test_returned_close_without_inner_confirmation_cannot_release_manifest(self):
        owner = open_installed_broker_client()
        self.client.close.side_effect = None
        with self.assertRaises(BrokerClientBootstrapError):
            owner.close()
        self.assertFalse(owner.cleanup_confirmed)
        self.manifest.close.assert_not_called()
        self.client.cleanup_confirmed = True
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)

    def test_manifest_close_failure_retries_without_reclosing_clean_client(self):
        owner = open_installed_broker_client()
        self.manifest.close.side_effect = OSError("Pinned handle remains")
        with self.assertRaises(BrokerClientBootstrapError):
            owner.close()
        self.assertFalse(owner.cleanup_confirmed)
        self.assertEqual(owner._pending, [])
        self.client.close.assert_called_once_with()
        self.manifest.close.side_effect = self.close_manifest
        owner.close()
        self.client.close.assert_called_once_with()
        self.assertTrue(owner.cleanup_confirmed)

    def test_partial_client_constructor_owner_is_retained_before_manifest_release(self):
        partial = Mock(cleanup_confirmed=False)
        partial.close.side_effect = OSError("Partial verifier pending")
        error = RuntimeError("Constructor failed")
        error.cleanup_owner = partial
        self.make_client.side_effect = error
        owner = BootstrapBrokerClient()
        with self.assertRaises(BrokerClientOpenError) as raised:
            owner.open()
        self.assertIs(raised.exception.cleanup_owner, owner)
        self.assertEqual(owner._pending, [partial])
        self.manifest.close.assert_not_called()
        partial.close.side_effect = None
        partial.cleanup_confirmed = True
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)

    def test_partial_manifest_constructor_owner_remains_owned_until_confirmed_retry(self):
        error = RuntimeError("Manifest owner constructor failed")
        error.cleanup_owner = self.manifest
        self.make_manifest.side_effect = error
        self.manifest.close.side_effect = OSError("Partial manifest handles still pinned")
        owner = BootstrapBrokerClient()
        with self.assertRaises(BrokerClientOpenError) as raised:
            owner.open()
        self.assertIs(raised.exception.cleanup_owner, owner)
        self.assertIs(owner._manifest, self.manifest)
        self.assertFalse(owner.cleanup_confirmed)
        self.make_client.assert_not_called()
        self.manifest.close.side_effect = self.close_manifest
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)

    def test_known_delegated_io_failure_invalidates_cached_observation_without_releasing_pins(self):
        owner = open_installed_broker_client()
        owner.observe()
        self.client.heartbeat.side_effect = OSError("Connection rejected")
        with self.assertRaises(OSError):
            owner.heartbeat()
        self.assertIsNone(owner.observation)
        self.manifest.close.assert_not_called()
        owner.observe()
        self.client.submit.side_effect = OSError("Pipe closed")
        with self.assertRaises(OSError):
            owner.submit(bytes(1920))
        self.assertIsNone(owner.observation)
        self.manifest.close.assert_not_called()
        owner.close()

    def test_unknown_constructor_ownership_cannot_claim_cleanup_or_release_manifest(self):
        self.make_client.side_effect = RuntimeError("Constructor failed without ownership evidence")
        owner = BootstrapBrokerClient()
        with self.assertRaises(BrokerClientOpenError) as raised:
            owner.open()
        self.assertIs(raised.exception.cleanup_owner, owner)
        self.assertFalse(owner.cleanup_confirmed)
        self.manifest.close.assert_not_called()
        with self.assertRaises(BrokerClientBootstrapError):
            owner.close()
        self.manifest.close.assert_not_called()

    def test_fatal_open_preserved_over_cleanup_failure_and_outer_owner_attached(self):
        original = FatalIdentity("Fatal identity failure")
        self.client.open.side_effect = original
        self.client.close.side_effect = RuntimeError("Cleanup also failed")
        owner = BootstrapBrokerClient()
        with self.assertRaises(FatalIdentity) as raised:
            owner.open()
        self.assertIs(raised.exception, original)
        self.assertIs(original.cleanup_owner, owner)
        self.assertFalse(owner.cleanup_confirmed)
        self.manifest.close.assert_not_called()
        self.client.close.side_effect = self.close_client
        owner.close()

    def test_fatal_cleanup_is_preserved_when_open_failure_was_ordinary(self):
        self.client.open.side_effect = OSError("Ordinary rejection")
        fatal = FatalIdentity("Fatal cleanup failure")
        self.client.close.side_effect = fatal
        owner = BootstrapBrokerClient()
        with self.assertRaises(FatalIdentity) as raised:
            owner.open()
        self.assertIs(raised.exception, fatal)
        self.assertIs(fatal.cleanup_owner, owner)
        self.manifest.close.assert_not_called()
        self.client.close.side_effect = self.close_client
        owner.close()

    def test_wrong_thread_cannot_use_or_release_owner_but_caches_are_readable(self):
        owner = open_installed_broker_client()
        owner.observe()
        failures = []
        def wrong_worker():
            self.assertIs(owner.observation, STATUS)
            self.assertFalse(owner.cleanup_confirmed)
            for action in (owner.observe, owner.heartbeat, lambda: owner.set_microphone_enabled(True),
                           lambda: owner.submit(bytes(1920)), owner.close):
                try:
                    action()
                except BrokerClientBootstrapError:
                    failures.append(True)
        thread = threading.Thread(target=wrong_worker)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [True] * 5)
        self.client.close.assert_not_called()
        self.manifest.close.assert_not_called()
        owner.close()

    def test_stop_during_manifest_open_keeps_pins_until_client_worker_finishes_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            if not release.wait(1):
                raise RuntimeError("Test failed to release manifest open")
            return self.installation
        self.manifest.load.side_effect = load
        sink = BrokerMicSink(open_installed_broker_client)
        sink.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(BrokerSinkStopError):
                sink.stop(0.01)
            self.assertFalse(sink.snapshot.cleanup_confirmed)
            self.manifest.close.assert_not_called()
            release.set()
            sink.stop()
            self.assertTrue(sink.snapshot.cleanup_confirmed)
            self.assertEqual(self.events, ["client close", "manifest close"])
            self.client.set_microphone_enabled.assert_not_called()
            self.client.observe.assert_not_called()
        finally:
            release.set()
            sink.stop()

    def test_invalid_deadlines_and_close_before_open_never_load_manifest(self):
        owner = BootstrapBrokerClient()
        for value in (0, -1, 0.01, 0.51, True, float("nan"), float("inf"), "0.25"):
            with self.assertRaises(ValueError):
                owner.open(value)
        self.make_manifest.assert_not_called()
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)
        with self.assertRaises(BrokerClientBootstrapError):
            owner.open()
        self.make_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
