import struct
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from remontevoice.driver_mic import DriverMicStopError, MicWriterState
from remontevoice.mic_broker import BrokerPolicy, MicBroker, PeerIdentity


PEER = PeerIdentity("S-1-5-21-1000", "S-1-5-5-1-42", 2, "a" * 64)
FRAME = struct.pack("<480f", *([0.125] * 480))


class Writer:
    def __init__(self):
        self.state = MicWriterState.IDLE
        self.cleanup_confirmed = True
        self.worker_alive = False
        self.start_calls = self.stop_requests = 0
        self.stop_timeouts = []
        self.payloads = []
        self.stop_fails = False

    @property
    def snapshot(self):
        return SimpleNamespace(state=self.state, cleanup_confirmed=self.cleanup_confirmed,
                               worker_alive=self.worker_alive)

    def start(self):
        self.start_calls += 1
        self.state = MicWriterState.STARTING
        self.worker_alive = True
        self.cleanup_confirmed = False

    def ready(self):
        self.state = MicWriterState.STREAMING

    def submit(self, payload):
        self.payloads.append(payload)
        return self.state == MicWriterState.STREAMING

    def request_stop(self):
        self.stop_requests += 1
        self.state = MicWriterState.STOPPING

    def stop(self, timeout):
        self.stop_timeouts.append(timeout)
        if self.stop_fails:
            raise DriverMicStopError("native call still owns its handle")
        self.state = MicWriterState.CLOSED
        self.worker_alive = False
        self.cleanup_confirmed = True


class MicBrokerTests(unittest.TestCase):
    def test_identity_representation_does_not_expose_account_identifiers(self):
        description = repr(PEER)
        self.assertNotIn(PEER.user_sid, description)
        self.assertNotIn(PEER.logon_sid, description)
        self.assertNotIn(PEER.image_sha256, description)

    def setUp(self):
        self.now = 10.0
        self.writers = []
        self.authenticate = Mock(return_value=PEER)
        self.admission = Mock(return_value=True)
        self.factory = Mock(side_effect=self.new_writer)
        self.broker = self.make_broker()

    def new_writer(self):
        writer = Writer()
        self.writers.append(writer)
        return writer

    def make_broker(self, **kwargs):
        return MicBroker(authenticate=self.authenticate,
                         policy=BrokerPolicy(PEER, admission=self.admission),
                         writer_factory=self.factory, clock=lambda: self.now, **kwargs)

    def enabled(self):
        capability = self.broker.connect()
        self.assertTrue(self.broker.set_microphone_enabled(capability, True))
        self.broker.poll()
        writer = self.writers[-1]
        writer.ready()
        self.broker.poll()
        return capability, writer

    def test_policy_matches_every_verified_identity_field_and_requires_admission(self):
        for peer in (replace(PEER, user_sid="S-1-5-21-2000"),
                     replace(PEER, logon_sid="S-1-5-5-1-99"), replace(PEER, session_id=3),
                     replace(PEER, image_sha256="b" * 64), None, {"trusted": True}):
            self.authenticate.return_value = peer
            self.assertIsNone(self.broker.connect())
        self.admission.assert_not_called()
        self.factory.assert_not_called()
        with self.assertRaises(TypeError):
            BrokerPolicy(PEER)
        self.assertFalse(BrokerPolicy(None, admission=self.admission).allows(PEER))
        self.assertFalse(BrokerPolicy(PEER, admission=lambda peer: "yes").allows(PEER))

    def test_authentication_and_admission_exceptions_deny_without_writer(self):
        self.authenticate.side_effect = OSError("untrusted connection")
        self.assertIsNone(self.broker.connect())
        self.authenticate.side_effect = None
        self.admission.side_effect = RuntimeError("policy unavailable")
        self.assertIsNone(self.broker.connect())
        self.factory.assert_not_called()
        self.assertFalse(self.broker.is_available())
        with self.assertRaises(TypeError):
            self.broker.connect(PEER)  # No caller identity or request body is accepted.

    def test_connected_muted_is_healthy_without_automatic_microphone(self):
        capability = self.broker.connect()
        self.assertIsInstance(capability, bytes)
        self.assertEqual(len(capability), 32)
        self.assertTrue(self.broker.is_available())
        self.assertFalse(self.broker.submit(capability, FRAME))
        self.assertTrue(self.broker.heartbeat(capability))
        self.broker.poll()
        self.factory.assert_not_called()
        self.assertTrue(self.broker.close(capability))
        self.assertFalse(self.broker.is_available())

    def test_capability_replay_and_reconnect_do_not_reenable_or_accept_old_owner(self):
        old = self.broker.connect()
        self.assertIsNone(self.broker.connect())
        for forged in (None, "trusted", bytes(32), b"short", bytearray(old)):
            self.assertFalse(self.broker.set_microphone_enabled(forged, True))
            self.assertFalse(self.broker.heartbeat(forged))
            self.assertFalse(self.broker.close(forged))
        self.assertTrue(self.broker.close(old))
        current = self.broker.connect()
        self.assertNotEqual(current, old)
        self.assertFalse(self.broker.heartbeat(old))
        self.assertFalse(self.broker.submit(old, FRAME))
        self.assertFalse(self.broker.close(old))
        self.assertTrue(self.broker.heartbeat(current))
        self.assertTrue(self.broker.is_available())
        self.factory.assert_not_called()

    def test_enable_creates_fresh_writer_and_waits_for_streaming_readiness(self):
        capability = self.broker.connect()
        self.assertTrue(self.broker.set_microphone_enabled(capability, True))
        self.assertFalse(self.broker.is_available())
        self.factory.assert_not_called()
        self.assertFalse(self.broker.submit(capability, FRAME))
        self.broker.poll()
        first = self.writers[0]
        self.assertEqual(first.start_calls, 1)
        self.assertFalse(self.broker.is_available())
        first.ready()
        self.broker.poll()
        self.assertTrue(self.broker.is_available())
        self.assertTrue(self.broker.set_microphone_enabled(capability, True))
        self.assertTrue(self.broker.is_available())
        self.assertEqual(self.factory.call_count, 1)
        for payload in (bytes(960), bytes(1000), bytes(1919), bytearray(FRAME), "path"):
            self.assertFalse(self.broker.submit(capability, payload))
        self.assertTrue(self.broker.submit(capability, FRAME))
        self.assertEqual(first.payloads, [FRAME])
        self.assertTrue(self.broker.set_microphone_enabled(capability, False))
        self.assertFalse(self.broker.submit(capability, FRAME))
        self.assertFalse(self.broker.is_available())
        self.assertFalse(self.broker.set_microphone_enabled(capability, True))
        self.broker.poll()
        self.assertTrue(first.cleanup_confirmed)
        self.assertTrue(self.broker.is_available())
        self.assertTrue(self.broker.set_microphone_enabled(capability, True))
        self.broker.poll()
        self.assertEqual(len(self.writers), 2)
        self.assertIsNot(first, self.writers[-1])

    def test_heartbeat_loss_closes_ingress_requests_stop_and_invalidates_capability(self):
        capability, writer = self.enabled()
        self.now += 0.75
        self.broker.poll()
        self.assertEqual(writer.stop_requests, 1)
        self.assertEqual(writer.stop_timeouts, [0.05])
        self.assertTrue(writer.cleanup_confirmed)
        self.assertFalse(self.broker.is_available())
        self.assertFalse(self.broker.submit(capability, FRAME))
        self.assertFalse(self.broker.heartbeat(capability))
        self.assertFalse(self.broker.set_microphone_enabled(capability, True))

    def test_late_heartbeat_cannot_resurrect_expired_muted_owner(self):
        capability = self.broker.connect()
        self.now += 0.75
        self.assertFalse(self.broker.heartbeat(capability))
        self.assertFalse(self.broker.is_available())
        self.assertIsNotNone(self.broker.connect())
        self.factory.assert_not_called()

    def test_failed_close_retains_writer_and_blocks_replacement_until_confirmed(self):
        capability, writer = self.enabled()
        writer.stop_fails = True
        self.assertTrue(self.broker.close(capability))
        self.assertFalse(self.broker.is_available())
        self.assertIsNone(self.broker.connect())
        self.broker.poll()
        self.assertFalse(writer.cleanup_confirmed)
        self.assertEqual(writer.stop_timeouts, [0.05])
        self.assertIsNone(self.broker.connect())
        self.assertFalse(self.broker.submit(capability, FRAME))
        writer.stop_fails = False
        self.broker.poll()
        self.assertTrue(writer.cleanup_confirmed)
        self.assertIsNotNone(self.broker.connect())
        self.assertEqual(self.factory.call_count, 1)

    def test_stop_return_without_cleanup_confirmation_does_not_release_owner(self):
        capability, writer = self.enabled()
        writer.stop = Mock()  # A successful return alone is not cleanup evidence.
        self.broker.close(capability)
        self.broker.poll()
        self.assertIsNone(self.broker.connect())
        self.assertFalse(self.broker.is_available())
        writer.cleanup_confirmed, writer.worker_alive = True, False
        self.broker.poll()
        self.assertIsNotNone(self.broker.connect())

    def test_writer_failure_closes_ingress_and_start_failure_keeps_failed_writer(self):
        capability, writer = self.enabled()
        writer.state = MicWriterState.FAILED
        self.broker.poll()
        self.assertFalse(self.broker.is_available())
        self.assertFalse(self.broker.heartbeat(capability))
        self.assertIsNone(self.broker.connect())
        self.broker.poll()
        current = self.broker.connect()
        failed = Writer()
        failed.start = Mock(side_effect=RuntimeError("start failed"))
        failed.stop_fails = True
        self.factory.side_effect = None
        self.factory.return_value = failed
        self.broker.set_microphone_enabled(current, True)
        self.broker.poll()
        self.assertEqual(failed.stop_requests, 1)
        self.broker.poll()
        self.assertIsNone(self.broker.connect())

    def test_availability_is_cached_even_while_service_worker_stop_is_blocked(self):
        capability, writer = self.enabled()
        entered, release = threading.Event(), threading.Event()
        original_stop = writer.stop
        def blocked_stop(timeout):
            entered.set()
            if not release.wait(1):
                raise AssertionError("test did not release worker")
            original_stop(timeout)
        writer.stop = blocked_stop
        self.broker.close(capability)
        worker = threading.Thread(target=self.broker.poll)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.broker.is_available())  # No broker lock acquisition.
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_identity_and_deadline_validation(self):
        for fields in ({"session_id": True}, {"session_id": -1}, {"user_sid": "anonymous"},
                       {"image_sha256": "A" * 64}, {"image_sha256": "a" * 63}):
            with self.assertRaises(ValueError):
                replace(PEER, **fields)
        for value in (0, -1, 1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.make_broker(heartbeat_timeout=value)
        for value in (-1, 2, float("nan")):
            with self.assertRaises(ValueError):
                self.make_broker(stop_timeout=value)


if __name__ == "__main__":
    unittest.main()
