import struct
import threading
import time
import unittest
from unittest.mock import Mock

from remontevoice.broker_mic_sink import BrokerMicSink, BrokerSinkError, BrokerSinkStopError
from remontevoice.broker_pipe_client import BrokerObservation


def frame(value=0.125):
    return struct.pack("<480f", *([value] * 480))


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Sink did not reach expected state")
        time.sleep(0.002)


class Client:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.owner = None
        self.calls = []
        self.requested = self.ready = self.cleanup_pending = False
        self.endpoint_available = True
        self.auto_ready = True
        self.heartbeat_ok = self.submit_ok = True
        self.cleanup_confirmed = False
        self.close_calls = 0
        self.close_fails = False
        self.fixed_status_time = None
        self.payloads = []
        self.on_submit = self.on_close = self.on_heartbeat = None

    def record(self, operation):
        identity = threading.get_ident()
        if self.owner is None:
            self.owner = identity
        if self.owner != identity:
            raise AssertionError("Client used by a non-owning thread")
        self.calls.append((operation, self.clock(), identity))

    def heartbeat(self):
        self.record("heartbeat")
        if self.on_heartbeat:
            self.on_heartbeat()
        return self.heartbeat_ok

    def observe(self):
        self.record("observe")
        available = self.endpoint_available and not self.cleanup_pending and (not self.requested or self.ready)
        return BrokerObservation(available, self.requested, self.ready, self.cleanup_pending,
            self.clock() if self.fixed_status_time is None else self.fixed_status_time)

    def set_microphone_enabled(self, enabled):
        self.record(("enable", enabled))
        self.requested = enabled
        self.ready = enabled and self.auto_ready
        return True  # ACK remains independent of observation readiness.

    def submit(self, payload):
        self.record("submit")
        if self.on_submit:
            self.on_submit()
        self.payloads.append(payload)
        return self.submit_ok

    def close(self):
        self.record("close")
        self.close_calls += 1
        if self.on_close:
            self.on_close()
        if self.close_fails:
            raise OSError("unconfirmed native close")
        self.cleanup_confirmed = True


class BrokerMicSinkTests(unittest.TestCase):
    def test_constructor_failure_retains_explicit_cleanup_owner_on_same_worker(self):
        sink, client, factory = self.make()
        client.close_fails = True
        error = OSError('Handshake failed with native cancellation pending')
        error.cleanup_owner = client
        factory.side_effect = error
        sink.start()
        wait_for(lambda: client.close_calls >= 1)
        self.assertFalse(sink.snapshot.cleanup_confirmed)
        self.assertTrue(sink.snapshot.worker_alive)
        self.assertFalse(sink.is_available())
        client.close_fails = False
        sink.stop()
        self.assertTrue(sink.snapshot.cleanup_confirmed)
        self.assertEqual(factory.call_count, 1)

    def setUp(self):
        self.sinks = []
        self.releases = []
        self.clients = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for client in self.clients:
            client.close_fails = False
        for sink in self.sinks:
            sink.stop()
            self.assertTrue(sink.snapshot.cleanup_confirmed)
            self.assertFalse(sink.snapshot.worker_alive)

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def make(self, *, clock=time.monotonic):
        client = Client(clock)
        self.clients.append(client)
        factory = Mock(return_value=client)
        sink = BrokerMicSink(factory, clock=clock)
        self.sinks.append(sink)
        return sink, client, factory

    def start(self, sink):
        sink.start()
        wait_for(sink.is_available)

    def test_inert_constructor_muted_handshake_and_all_io_owned_by_worker(self):
        sink, client, factory = self.make()
        factory.assert_not_called()
        self.assertFalse(sink.is_available())
        self.assertFalse(sink.submit(frame()))
        self.start(sink)
        self.assertTrue(sink.is_available())
        self.assertFalse(sink.snapshot.writer_ready)
        self.assertFalse(sink.submit(frame()))
        self.assertFalse(any(isinstance(call[0], tuple) for call in client.calls))
        sink.stop()
        self.assertTrue(client.cleanup_confirmed)
        self.assertNotEqual(client.owner, threading.get_ident())
        self.assertEqual({call[2] for call in client.calls}, {client.owner})
        with self.assertRaises(BrokerSinkError):
            sink.start()

    def test_enable_ack_waits_for_matching_ready_observation(self):
        sink, client, _ = self.make()
        client.auto_ready = False
        self.start(sink)
        result = []
        waiter = threading.Thread(target=lambda: result.append(sink.set_enabled(True, 0.8)))
        waiter.start()
        try:
            wait_for(lambda: client.requested)
            self.assertTrue(waiter.is_alive())
            self.assertFalse(sink.is_available())
            self.assertFalse(sink.submit(frame()))
            client.ready = True
            waiter.join(1)
            self.assertEqual(result, [True])
            self.assertTrue(sink.is_available())
            self.assertTrue(sink.snapshot.writer_ready)
            self.assertTrue(sink.submit(frame()))
            wait_for(lambda: len(client.payloads) == 1)
        finally:
            client.ready = True
            waiter.join(1)
        self.assertFalse(waiter.is_alive())

    def test_confirmed_enabled_session_fails_closed_when_broker_unexpectedly_mutes(self):
        sink, client, _ = self.make()
        self.start(sink)
        self.assertTrue(sink.set_enabled(True))
        # A fresh, internally consistent status can still contradict the last
        # confirmed control. It must not renew the previous ready/healthy flags.
        confirmed_at = []
        def unexpected_muted_status():
            client.record("observe")
            confirmed_at.append(sink.snapshot.observed_at)
            return BrokerObservation(True, False, False, False, time.monotonic())
        client.observe = unexpected_muted_status
        wait_for(lambda: sink.snapshot.error is not None)
        self.assertFalse(sink.is_available())
        self.assertFalse(sink.snapshot.writer_ready)
        self.assertFalse(sink.submit(frame()))
        self.assertEqual(sink.snapshot.observed_at, confirmed_at[-1])
        wait_for(lambda: not sink.snapshot.worker_alive)
        self.assertTrue(client.cleanup_confirmed)

    def test_confirmed_muted_session_fails_closed_when_broker_unexpectedly_enables(self):
        sink, client, _ = self.make()
        self.start(sink)
        self.assertTrue(sink.set_enabled(False))
        confirmed_at = []
        def unexpected_enabled_status():
            client.record("observe")
            confirmed_at.append(sink.snapshot.observed_at)
            return BrokerObservation(True, True, True, False, time.monotonic())
        client.observe = unexpected_enabled_status
        wait_for(lambda: sink.snapshot.error is not None)
        self.assertFalse(sink.is_available())
        self.assertFalse(sink.snapshot.writer_ready)
        self.assertFalse(sink.submit(frame()))
        self.assertEqual(sink.snapshot.observed_at, confirmed_at[-1])
        self.assertEqual(client.calls[0][0], "observe")
        self.assertFalse(any(isinstance(call[0], tuple) and call[0] == ("enable", True)
                             for call in client.calls))
        wait_for(lambda: not sink.snapshot.worker_alive)
        self.assertTrue(client.cleanup_confirmed)

    def test_unconfirmed_enable_allows_multiple_not_ready_observations_before_confirmation(self):
        sink, client, _ = self.make()
        client.auto_ready = False
        self.start(sink)
        result = []
        waiter = threading.Thread(target=lambda: result.append(sink.set_enabled(True, 0.8)))
        waiter.start()
        try:
            wait_for(lambda: client.requested)
            initial_observations = len([call for call in client.calls if call[0] == "observe"])
            wait_for(lambda: len([call for call in client.calls if call[0] == "observe"]) >= initial_observations + 2)
            self.assertTrue(waiter.is_alive())
            self.assertIsNone(sink.snapshot.error)
            self.assertFalse(sink.is_available())
            client.ready = True
            waiter.join(1)
            self.assertEqual(result, [True])
            self.assertTrue(sink.is_available())
            self.assertTrue(sink.snapshot.writer_ready)
        finally:
            client.ready = True
            waiter.join(1)
        self.assertFalse(waiter.is_alive())

    def test_enable_timeout_closes_ingress_and_late_readiness_cannot_reenable(self):
        sink, client, _ = self.make()
        client.auto_ready = False
        self.start(sink)
        self.assertFalse(sink.set_enabled(True, 0.08))
        client.ready = True
        self.assertFalse(sink.is_available())
        self.assertFalse(sink.submit(frame()))
        self.assertFalse(sink.set_enabled(True, 0.1))
        sink.stop()
        self.assertEqual(sink.snapshot.error, "activation_timeout")

    def test_bounded_queue_drops_old_voice_and_disable_discards_waiting_pcm(self):
        sink, client, _ = self.make()
        self.start(sink)
        self.assertTrue(sink.set_enabled(True))
        entered, release = self.event(), self.event()
        client.on_submit = lambda: (entered.set(), release.wait(1))
        self.assertTrue(sink.submit(frame(0)))
        self.assertTrue(entered.wait(1))
        for value in range(20):
            self.assertTrue(sink.submit(frame(value / 100)))
        self.assertEqual(sink.snapshot.queued, 10)
        self.assertEqual(sink.snapshot.dropped, 10)
        disabled = []
        waiter = threading.Thread(target=lambda: disabled.append(sink.set_enabled(False)))
        waiter.start()
        try:
            wait_for(lambda: not sink.snapshot.enabled)
            self.assertEqual(sink.snapshot.queued, 0)
            self.assertFalse(sink.submit(frame()))
            release.set()
            waiter.join(1)
            self.assertEqual(disabled, [True])
            self.assertEqual(len(client.payloads), 1)
            self.assertTrue(sink.is_available())  # Healthy muted without a writer.
        finally:
            release.set()
            waiter.join(1)

    def test_queued_voice_expires_at_100ms_before_sending(self):
        now = [10.0]
        sink, client, _ = self.make(clock=lambda: now[0])
        self.start(sink)
        self.assertTrue(sink.set_enabled(True))
        entered, release = self.event(), self.event()
        client.on_submit = lambda: (entered.set(), release.wait(1))
        self.assertTrue(sink.submit(frame(0)))
        self.assertTrue(entered.wait(1))
        self.assertTrue(sink.submit(frame(0.5)))
        now[0] += 0.11
        release.set()
        wait_for(lambda: sink.snapshot.dropped == 1)
        self.assertEqual(len(client.payloads), 1)
        self.assertTrue(sink.is_available())

    def test_false_submit_is_backpressure_not_an_implicit_disable(self):
        sink, client, _ = self.make()
        self.start(sink)
        self.assertTrue(sink.set_enabled(True))
        client.submit_ok = False
        self.assertTrue(sink.submit(frame()))
        wait_for(lambda: sink.snapshot.dropped == 1)
        self.assertTrue(sink.is_available())
        self.assertTrue(sink.snapshot.writer_ready)
        client.submit_ok = True
        self.assertTrue(sink.submit(frame()))
        wait_for(lambda: sink.snapshot.sent == 1)

    def test_endpoint_failure_and_heartbeat_rejection_fail_closed(self):
        for failure in ("endpoint", "heartbeat"):
            with self.subTest(failure=failure):
                sink, client, _ = self.make()
                self.start(sink)
                self.assertTrue(sink.set_enabled(True))
                if failure == "endpoint":
                    client.endpoint_available = False
                else:
                    client.heartbeat_ok = False
                wait_for(lambda: not sink.is_available())
                wait_for(lambda: not sink.snapshot.worker_alive)
                self.assertFalse(sink.submit(frame()))
                self.assertTrue(client.cleanup_confirmed)

    def test_stalled_worker_health_expires_without_ui_lock_or_native_calls(self):
        now = [10.0]
        sink, client, _ = self.make(clock=lambda: now[0])
        self.start(sink)
        entered, release = self.event(), self.event()
        client.on_heartbeat = lambda: (entered.set(), release.wait(1))
        now[0] += 0.08
        self.assertTrue(entered.wait(1))
        now[0] += 0.51
        before = time.monotonic()
        self.assertFalse(sink.is_available())
        self.assertLess(time.monotonic() - before, 0.05)
        self.assertFalse(sink.submit(frame()))
        with self.assertRaises(BrokerSinkStopError):
            sink.stop(0.01)
        self.assertTrue(sink.snapshot.worker_alive)
        release.set()
        sink.stop()
        self.assertTrue(sink.snapshot.cleanup_confirmed)

    def test_unchanged_server_progress_fails_even_when_pipe_calls_keep_completing(self):
        now = [10.0]
        sink, client, _ = self.make(clock=lambda: now[0])
        client.fixed_status_time = now[0]
        self.start(sink)
        now[0] += 0.49
        wait_for(lambda: len([call for call in client.calls if call[0] == "observe"]) >= 2)
        self.assertTrue(sink.is_available())
        now[0] += 0.02
        self.assertFalse(sink.is_available())
        wait_for(lambda: not sink.snapshot.worker_alive)
        self.assertTrue(client.cleanup_confirmed)

    def test_stalled_close_retains_worker_and_client_until_actual_completion(self):
        sink, client, _ = self.make()
        self.start(sink)
        entered, release = self.event(), self.event()
        client.on_close = lambda: (entered.set(), release.wait(1))
        with self.assertRaises(BrokerSinkStopError):
            sink.stop(0.02)
        self.assertTrue(entered.is_set())
        self.assertFalse(sink.is_available())
        self.assertTrue(sink.snapshot.worker_alive)
        self.assertFalse(sink.snapshot.cleanup_confirmed)
        self.assertIs(sink._client, client)
        with self.assertRaises(BrokerSinkError):
            sink.start()
        release.set()
        sink.stop()
        self.assertTrue(sink.snapshot.cleanup_confirmed)

    def test_close_failure_retries_on_original_worker_and_success_return_is_not_confirmation(self):
        sink, client, _ = self.make()
        self.start(sink)
        client.close_fails = True
        with self.assertRaises(BrokerSinkStopError):
            sink.stop(0.02)
        self.assertFalse(sink.snapshot.cleanup_confirmed)
        self.assertIs(sink._client, client)
        client.close_fails = False
        sink.stop()
        self.assertTrue(client.cleanup_confirmed)
        self.assertEqual({call[2] for call in client.calls}, {client.owner})
        second, unconfirmed, _ = self.make()
        self.start(second)
        original_close = unconfirmed.close
        unconfirmed.close = lambda: unconfirmed.record("unconfirmed close return")
        with self.assertRaises(BrokerSinkStopError):
            second.stop(0.02)
        self.assertFalse(second.snapshot.cleanup_confirmed)
        unconfirmed.close = original_close
        second.stop()

    def test_invalid_pcm_and_controls_never_reach_client(self):
        sink, client, factory = self.make()
        for value in (b"", bytes(1919), bytes(1921), bytearray(frame()), frame(float("nan")), frame(float("inf"))):
            self.assertFalse(sink.submit(value))
        for timeout in (-1, 1.01, True, float("nan"), float("inf"), "1"):
            with self.assertRaises(ValueError):
                sink.set_enabled(True, timeout)
            with self.assertRaises(ValueError):
                sink.stop(timeout)
        with self.assertRaises(ValueError):
            sink.set_enabled(1)
        self.assertFalse(sink.set_enabled(True, 0.01))
        factory.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_already_enabled_or_inconsistent_initial_session_is_never_adopted(self):
        for status in (BrokerObservation(True, True, True, False, time.monotonic()),
                       BrokerObservation(True, False, True, False, time.monotonic()),
                       BrokerObservation(True, False, False, True, time.monotonic()),
                       BrokerObservation("yes", False, False, False, time.monotonic())):
            with self.subTest(status=status):
                sink, client, _ = self.make()
                client.observe = lambda: status
                sink.start()
                wait_for(lambda: not sink.snapshot.worker_alive)
                self.assertFalse(sink.is_available())
                self.assertTrue(client.cleanup_confirmed)
                self.assertFalse(sink.submit(frame()))

    def test_factory_failure_and_stop_during_open_do_not_enable_or_replace(self):
        sink, _, factory = self.make()
        factory.side_effect = OSError("no authenticated broker")
        sink.start()
        wait_for(lambda: not sink.snapshot.worker_alive)
        self.assertFalse(sink.is_available())
        self.assertTrue(sink.snapshot.cleanup_confirmed)
        second, client, second_factory = self.make()
        entered, release = self.event(), self.event()
        def delayed_factory():
            entered.set()
            release.wait(1)
            return client
        second_factory.side_effect = delayed_factory
        second.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(BrokerSinkStopError):
            second.stop(0.01)
        release.set()
        second.stop()
        self.assertEqual([call[0] for call in client.calls], ["close"])
        self.assertTrue(client.cleanup_confirmed)


if __name__ == "__main__":
    unittest.main()
