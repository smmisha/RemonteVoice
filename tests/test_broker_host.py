import threading
import unittest
from unittest.mock import Mock

from remotevoice.broker_host import BrokerHost, BrokerHostError
from remotevoice.broker_pipe_session import BrokerPipeSession, BrokerSessionError
from remotevoice.mic_broker import BrokerPolicy, PeerIdentity

PEER = PeerIdentity("S-1-5-21-1000", "S-1-5-5-1-42", 1, "a" * 64)
HELLO = b"RVMB\x01\x03\x00\x00"
CLOSE = b"RVMB\x01\x04\x00\x00"


class FatalIdentity(BaseException):
    pass


class Pipe:
    def __init__(self, reads=()):
        self.handle = 51
        self.reads = list(reads)
        self.writes = []
        self.cleanup_confirmed = False
        self.close_calls = 0

    def read_exact(self, size, timeout):
        if not self.reads:
            raise EOFError("Peer consumed close ACK")
        value = self.reads.pop(0)
        if len(value) != size:
            raise AssertionError("Unexpected frame read")
        return value

    def write_all(self, payload, timeout):
        self.writes.append(payload)

    def close(self):
        self.close_calls += 1
        self.cleanup_confirmed = True


class Listener:
    def __init__(self, connections, stop):
        self.connections = list(connections)
        self.stop = stop
        self.accept_timeouts = []
        self.close_calls = 0
        self.close_error = None
        self.cleanup_confirmed = False
        self.accept_error = None

    def accept(self, timeout):
        self.accept_timeouts.append(timeout)
        if self.accept_error is not None:
            raise self.accept_error
        if self.connections:
            return self.connections.pop(0)
        self.stop.set()
        return None

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.cleanup_confirmed = True


class Verifier:
    def __init__(self):
        self.verified = []
        self.forgotten = []
        self.close_calls = 0
        self.close_error = None

    def verify_pipe_peer(self, handle, *, server_side):
        self.verified.append(handle)
        return PEER

    def validate_pipe_token(self, handle, peer, *, server_side):
        return peer is PEER

    def forget_pipe_peer(self, handle):
        self.forgotten.append(handle)

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class Session:
    def __init__(self, *, clean=True, error=None, on_serve=None):
        self.cleanup_confirmed = clean
        self.error, self.on_serve = error, on_serve
        self.calls = 0

    def serve(self, stop):
        self.calls += 1
        if self.on_serve:
            self.on_serve()
        if self.error is not None:
            raise self.error


class BrokerHostTests(unittest.TestCase):
    def make(self, connections=(), *, session_factory=BrokerPipeSession):
        stop = threading.Event()
        listener = Listener(connections, stop)
        verifier = Verifier()
        writer = Mock(side_effect=AssertionError("No automatic microphone"))
        policy = BrokerPolicy(PEER, admission=lambda peer: True)
        host = BrokerHost(listener, verifier, policy, writer, session_factory=session_factory)
        return host, stop, listener, verifier, writer

    def test_two_clean_real_sessions_reconnect_sequentially_without_microphone(self):
        first, second = Pipe([HELLO, CLOSE]), Pipe([HELLO, CLOSE])
        first.handle, second.handle = 51, 52
        host, stop, listener, verifier, writer = self.make([first, second])
        host.serve(stop)
        self.assertEqual(listener.accept_timeouts, [0.1, 0.1, 0.1])
        self.assertEqual(verifier.verified, [51, 52])
        self.assertEqual(verifier.forgotten, [51, 52])
        self.assertEqual((first.close_calls, second.close_calls), (1, 1))
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertTrue(host.cleanup_confirmed)
        self.assertEqual(host.cleanup_errors, ())
        writer.assert_not_called()
        with self.assertRaises(BrokerHostError):
            host.serve(threading.Event())

    def test_bad_handshake_clean_rejection_allows_next_verified_connection(self):
        bad = Pipe([b"RVMB\x01\x01\x01\x00", b"\1"])
        good = Pipe([HELLO, CLOSE])
        host, stop, listener, verifier, writer = self.make([bad, good])
        host.serve(stop)
        self.assertTrue(bad.cleanup_confirmed)
        self.assertEqual(bad.writes, [])
        self.assertEqual(verifier.verified, [51])
        self.assertEqual(len(good.writes), 2)
        self.assertEqual(len(listener.accept_timeouts), 3)
        self.assertTrue(host.cleanup_confirmed)
        writer.assert_not_called()

    def test_unknown_cleanup_never_accepts_replacement_even_after_normal_return(self):
        for clean in (False, None, 1):
            with self.subTest(clean=clean):
                session = Session(clean=clean)
                factory = Mock(return_value=session)
                host, stop, listener, verifier, writer = self.make([Pipe(), Pipe()], session_factory=factory)
                with self.assertRaises(BrokerHostError):
                    host.serve(stop)
                self.assertEqual(len(listener.accept_timeouts), 1)
                self.assertIs(host._session, session)
                self.assertIsNotNone(host._connection)
                self.assertIs(host._listener, listener)
                self.assertIs(host._verifier, verifier)
                self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
                self.assertFalse(host.cleanup_confirmed)
                writer.assert_not_called()

    def test_client_exception_is_recoverable_only_after_explicit_cleanup(self):
        first = Session(error=BrokerSessionError("Rejected malformed client"))
        second = Session()
        host, stop, listener, _, _ = self.make([Pipe(), Pipe()], session_factory=Mock(side_effect=[first, second]))
        host.serve(stop)
        self.assertEqual((first.calls, second.calls), (1, 1))
        self.assertTrue(host.cleanup_confirmed)
        self.assertEqual(len(listener.accept_timeouts), 3)
        unclean = Session(clean=False, error=BrokerSessionError("Rejected but still owns writer"))
        host, stop, listener, _, _ = self.make([Pipe(), Pipe()], session_factory=Mock(return_value=unclean))
        with self.assertRaises(BrokerHostError) as raised:
            host.serve(stop)
        self.assertIs(raised.exception.__cause__, unclean.error)
        self.assertEqual(len(listener.accept_timeouts), 1)

    def test_accept_failure_stops_admission_without_retry_spin(self):
        host, stop, listener, verifier, writer = self.make()
        failure = OSError("Listener pending I/O failed")
        listener.accept_error = failure
        with self.assertRaises(BrokerHostError) as raised:
            host.serve(stop)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(listener.accept_timeouts, [0.1])
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertTrue(host.cleanup_confirmed)  # Handles closed, although host cannot be reused.
        writer.assert_not_called()

    def test_stop_idle_closes_both_resources_and_never_accepts(self):
        host, stop, listener, verifier, writer = self.make([Pipe()])
        stop.set()
        host.serve(stop)
        self.assertEqual(listener.accept_timeouts, [])
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertTrue(host.cleanup_confirmed)
        writer.assert_not_called()

    def test_stop_racing_accept_closes_unclaimed_pipe_without_creating_session(self):
        factory = Mock()
        connection = Pipe()
        host, stop, listener, _, _ = self.make(session_factory=factory)
        def accepted(timeout):
            stop.set()
            return connection
        listener.accept = accepted
        host.serve(stop)
        factory.assert_not_called()
        self.assertTrue(connection.cleanup_confirmed)
        self.assertTrue(host.cleanup_confirmed)

    def test_fatal_session_preserved_despite_both_cleanup_errors_no_next_accept(self):
        original = FatalIdentity("Compromised impersonation thread")
        session = Session(clean=False, error=original)
        host, stop, listener, verifier, writer = self.make([Pipe(), Pipe()], session_factory=Mock(return_value=session))
        listener_error, verifier_error = OSError("Pipe still owns buffers"), OSError("Pinned image handle pending")
        listener.close_error, verifier.close_error = listener_error, verifier_error
        with self.assertRaises(FatalIdentity) as raised:
            host.serve(stop)
        self.assertIs(raised.exception, original)
        self.assertEqual(host.cleanup_errors, (listener_error, verifier_error))
        self.assertEqual(len(listener.accept_timeouts), 1)
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertIs(host._session, session)
        self.assertFalse(host.cleanup_confirmed)
        writer.assert_not_called()

    def test_falsy_fatal_is_preserved_instead_of_being_replaced_by_cleanup_fatal(self):
        class FalsyFatal(FatalIdentity):
            def __bool__(self):
                return False
        original = FalsyFatal()
        session = Session(clean=False, error=original)
        host, stop, listener, verifier, _ = self.make([Pipe()], session_factory=Mock(return_value=session))
        listener.close_error = FatalIdentity("Later cleanup failure")
        verifier.close_error = OSError("Another cleanup failure")
        with self.assertRaises(FalsyFatal) as raised:
            host.serve(stop)
        self.assertIs(raised.exception, original)
        self.assertFalse(host.cleanup_confirmed)

    def test_fatal_accept_or_cleanup_is_never_reduced_to_ordinary_host_error(self):
        host, stop, listener, verifier, _ = self.make()
        fatal = FatalIdentity()
        listener.accept_error = fatal
        with self.assertRaises(FatalIdentity) as raised:
            host.serve(stop)
        self.assertIs(raised.exception, fatal)
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertFalse(host.cleanup_confirmed)
        host, stop, listener, verifier, _ = self.make()
        stop.set()
        listener.close_error = OSError("Ordinary cleanup failure")
        verifier.close_error = fatal
        with self.assertRaises(FatalIdentity) as raised:
            host.serve(stop)
        self.assertIs(raised.exception, fatal)
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertFalse(host.cleanup_confirmed)

    def test_constructor_failure_closes_unclaimed_pipe_but_does_not_admit_next(self):
        connection = Pipe()
        failure = RuntimeError("Invalid trusted composition")
        host, stop, listener, _, _ = self.make([connection, Pipe()], session_factory=Mock(side_effect=failure))
        with self.assertRaises(RuntimeError) as raised:
            host.serve(stop)
        self.assertIs(raised.exception, failure)
        self.assertEqual(connection.close_calls, 1)
        self.assertEqual(len(listener.accept_timeouts), 1)
        self.assertTrue(host.cleanup_confirmed)

    def test_ordinary_close_failure_still_attempts_other_resource_and_is_unconfirmed(self):
        host, stop, listener, verifier, _ = self.make()
        stop.set()
        listener.close_error = OSError("Pipe handle cleanup pending")
        with self.assertRaises(BrokerHostError):
            host.serve(stop)
        self.assertEqual((listener.close_calls, verifier.close_calls), (1, 1))
        self.assertFalse(host.cleanup_confirmed)
        host, stop, listener, _, _ = self.make()
        stop.set()
        listener.close = Mock()  # Returning is insufficient when explicit evidence stays False.
        with self.assertRaises(BrokerHostError):
            host.serve(stop)
        self.assertFalse(host.cleanup_confirmed)

    def test_deadline_validation_is_inert(self):
        listener, verifier, writer = Mock(), Mock(), Mock()
        policy = BrokerPolicy(PEER, admission=lambda peer: True)
        for value in (0, -1, 1.1, True, float("nan"), float("inf"), "0.1"):
            with self.assertRaises(ValueError):
                BrokerHost(listener, verifier, policy, writer, accept_timeout=value)
        listener.accept.assert_not_called()
        listener.close.assert_not_called()
        verifier.close.assert_not_called()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
