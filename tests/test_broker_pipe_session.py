import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remotevoice.broker_pipe_session import BrokerPipeSession, BrokerSessionError
from remotevoice.driver_mic import MicWriterState
from remotevoice.mic_broker import BrokerPolicy, MicBroker, PeerIdentity

HELLO = b'RVMB\x01\x03\x00\x00'
CLOSE = b'RVMB\x01\x04\x00\x00'
ENABLE = b'RVMB\x01\x01\x01\x00'
PEER = PeerIdentity('S-1-5-21-1000', 'S-1-5-5-1-42', 1, 'a'*64)


class Pipe:
    def __init__(self, reads):
        self.handle = 51
        self.reads = list(reads)
        self.writes = []
        self.closed = False

    def read_exact(self, size, timeout):
        if not self.reads:
            raise TimeoutError('closed/idle')
        value = self.reads.pop(0)
        if len(value) != size:
            raise AssertionError('Unexpected read size')
        return value

    def write_all(self, payload, timeout):
        self.writes.append(payload)

    def close(self):
        self.closed = True


class BrokerPipeSessionTests(unittest.TestCase):
    def session(self, pipe, identity=PEER):
        verifier = Mock()
        verifier.verify_pipe_peer.return_value = identity
        verifier.validate_pipe_token.return_value = True
        factory = Mock(side_effect=AssertionError('No automatic microphone'))
        session = BrokerPipeSession(pipe, verifier, BrokerPolicy(PEER, lambda _: True), factory)
        return session, verifier, factory

    def test_muted_handshake_authenticates_then_closes_without_writer(self):
        pipe = Pipe([HELLO, CLOSE])
        session, verifier, factory = self.session(pipe)
        session.serve(threading.Event())
        factory.assert_not_called()
        verifier.verify_pipe_peer.assert_called_once_with(51, server_side=True)
        verifier.validate_pipe_token.assert_called_once_with(51, PEER, server_side=True)
        self.assertEqual(len(pipe.writes), 2)
        self.assertTrue(session.cleanup_confirmed)
        self.assertFalse(session.is_available())

    def test_status_stays_coherent_when_worker_completes_enable_or_mute_during_reply(self):
        for enabling in (True, False):
            with self.subTest(enabling=enabling):
                session, _, _ = self.session(Pipe([]))
                writer = Mock(snapshot=SimpleNamespace(
                    state=MicWriterState.STREAMING, cleanup_confirmed=True, worker_alive=False))
                broker = MicBroker(authenticate=lambda: PEER, policy=session._policy,
                                   writer_factory=lambda: writer)
                capability = broker.connect()
                broker.set_microphone_enabled(capability, True)
                if not enabling:
                    broker.poll()
                    broker.set_microphone_enabled(capability, False)
                session._broker = broker
                session._last_poll = time.monotonic()
                session._poll_sequence = 7
                snapshot = broker.status

                def complete_transition_after_snapshot():
                    status = snapshot()
                    broker.poll()  # Deterministic worker interleaving, no real audio.
                    return status

                with patch.object(broker, 'status', side_effect=complete_transition_after_snapshot):
                    flags, sequence = struct.unpack('<BQ', session._status_payload())
                self.assertEqual(sequence, 7)
                # Pending BEGIN/END must not borrow availability from a later poll.
                self.assertEqual(flags, 2 if enabling else 8)
                self.assertTrue(broker.is_available())

    def test_status_never_reports_available_with_stale_or_failed_worker(self):
        for condition in ('fresh', 'missing', 'stale', 'future', 'failed', 'finished'):
            with self.subTest(condition=condition):
                session, _, factory = self.session(Pipe([]))
                session._broker = MicBroker(authenticate=lambda: PEER, policy=session._policy,
                                            writer_factory=factory)
                session._broker.connect()
                session._last_poll = {'missing': None, 'stale': 9.7, 'future': 10.1}.get(condition, 10.0)
                if condition == 'failed':
                    session._worker_failed.set()
                if condition == 'finished':
                    session._finish.set()
                with patch('remotevoice.broker_pipe_session.time.monotonic', return_value=10.0):
                    flags, _ = struct.unpack('<BQ', session._status_payload())
                    self.assertEqual(session.is_available(), condition == 'fresh')
                self.assertEqual(flags, 1 if condition == 'fresh' else 0)
                factory.assert_not_called()

    def test_enable_before_handshake_is_rejected_without_auth_or_writer(self):
        pipe = Pipe([ENABLE, b'\1'])
        session, verifier, factory = self.session(pipe)
        with self.assertRaises(BrokerSessionError):
            session.serve(threading.Event())
        verifier.verify_pipe_peer.assert_not_called()
        factory.assert_not_called()
        self.assertTrue(pipe.closed)

    def test_wrong_peer_and_per_message_token_change_close_before_dispatch(self):
        for changed in (False, True):
            pipe = Pipe([HELLO, ENABLE, b'\1'])
            session, verifier, factory = self.session(pipe, identity=PEER if changed else None)
            verifier.validate_pipe_token.return_value = False
            with self.assertRaises(BrokerSessionError):
                session.serve(threading.Event())
            factory.assert_not_called()
            self.assertTrue(pipe.closed)

    def test_idle_or_partial_read_failure_closes_connection(self):
        session, _, factory = self.session(Pipe([HELLO]))
        with self.assertRaises(BrokerSessionError):
            session.serve(threading.Event())
        factory.assert_not_called()
        self.assertTrue(session.cleanup_confirmed)

    def test_fatal_token_reversion_failure_is_not_swallowed(self):
        class FatalIdentity(BaseException):
            pass
        pipe = Pipe([HELLO])
        session, verifier, factory = self.session(pipe)
        verifier.verify_pipe_peer.side_effect = FatalIdentity()
        with self.assertRaises(FatalIdentity):
            session.serve(threading.Event())
        factory.assert_not_called()
        self.assertTrue(pipe.closed)

    def test_connection_and_cleanup_failures_cannot_be_claimed_clean(self):
        pipe = Pipe([HELLO, CLOSE])
        pipe.close = Mock(side_effect=OSError('pending native I/O'))
        session, _, _ = self.session(pipe)
        with self.assertRaises(OSError):
            session.serve(threading.Event())
        self.assertFalse(session.cleanup_confirmed)

    def test_fatal_identity_is_preserved_when_both_cleanup_operations_fail(self):
        class FatalIdentity(BaseException):
            pass
        for at_handshake in (True, False):
            with self.subTest(at_handshake=at_handshake):
                pipe = Pipe([HELLO, CLOSE])
                pipe.close = Mock(side_effect=OSError('pending native I/O'))
                session, verifier, factory = self.session(pipe)
                fatal = FatalIdentity()
                if at_handshake:
                    verifier.verify_pipe_peer.side_effect = fatal
                else:
                    verifier.validate_pipe_token.side_effect = fatal
                verifier.forget_pipe_peer.side_effect = OSError('pending identity handles')
                with self.assertRaises(FatalIdentity) as raised:
                    session.serve(threading.Event())
                self.assertIs(raised.exception, fatal)
                pipe.close.assert_called_once()
                verifier.forget_pipe_peer.assert_called_once_with(51)
                self.assertFalse(session.cleanup_confirmed)
                factory.assert_not_called()
                if session._worker:
                    session._worker.join(1)
                    self.assertFalse(session._worker.is_alive())

    def test_identity_cleanup_failure_does_not_claim_closed_session_clean(self):
        pipe = Pipe([HELLO, CLOSE])
        session, verifier, _ = self.session(pipe)
        verifier.forget_pipe_peer.side_effect = OSError('pending identity handles')
        with self.assertRaises(OSError):
            session.serve(threading.Event())
        self.assertTrue(pipe.closed)
        self.assertFalse(session.cleanup_confirmed)

    def test_initial_poll_fatal_survives_both_cleanup_failures_without_shutdown_or_ack(self):
        class FatalPoll(BaseException):
            pass
        fatal = FatalPoll()
        pipe = Pipe([HELLO])
        pipe.close = Mock(side_effect=OSError('pending pipe'))
        session, verifier, factory = self.session(pipe)
        verifier.forget_pipe_peer.side_effect = OSError('pending identity')
        broker = Mock(cleanup_confirmed=True)
        broker.poll.side_effect = fatal
        with patch('remotevoice.broker_pipe_session.MicBroker', return_value=broker):
            with self.assertRaises(FatalPoll) as raised:
                session.serve(threading.Event())
        self.assertIs(raised.exception, fatal)
        self.assertFalse(pipe.writes)
        broker.shutdown.assert_not_called()
        self.assertFalse(session.cleanup_confirmed)
        verifier.forget_pipe_peer.assert_called_once_with(51)
        factory.assert_not_called()
        session._worker.join(1)
        self.assertFalse(session._worker.is_alive())

    def test_poll_fatal_overrides_concurrent_ordinary_read_and_cleanup_errors(self):
        class FatalPoll(BaseException):
            pass
        fatal = FatalPoll()
        triggered = threading.Event()
        pipe = Pipe([HELLO])
        original_read = pipe.read_exact
        def read(size, timeout):
            if pipe.reads:
                return original_read(size, timeout)
            self.assertTrue(triggered.wait(1))
            raise OSError('concurrent read failure')
        pipe.read_exact = read
        pipe.close = Mock(side_effect=OSError('pending pipe'))
        session, verifier, _ = self.session(pipe)
        verifier.forget_pipe_peer.side_effect = OSError('pending identity')
        broker = Mock(cleanup_confirmed=True)
        polls = []
        def poll():
            polls.append(True)
            if len(polls) > 1:
                triggered.set()
                raise fatal
        broker.poll.side_effect = poll
        with patch('remotevoice.broker_pipe_session.MicBroker', return_value=broker):
            with self.assertRaises(FatalPoll) as raised:
                session.serve(threading.Event())
        self.assertIs(raised.exception, fatal)
        self.assertEqual(len(pipe.writes), 1)
        broker.shutdown.assert_not_called()
        self.assertFalse(session.cleanup_confirmed)
        session._worker.join(1)
        self.assertFalse(session._worker.is_alive())

    def test_worker_shutdown_fatal_during_join_reaches_main_once(self):
        class FatalShutdown(BaseException):
            pass
        fatal = FatalShutdown()
        pipe = Pipe([HELLO, CLOSE])
        session, verifier, _ = self.session(pipe)
        verifier.forget_pipe_peer.side_effect = OSError('pending identity')
        broker = Mock(cleanup_confirmed=True)
        broker.shutdown.side_effect = fatal
        with patch('remotevoice.broker_pipe_session.MicBroker', return_value=broker):
            with self.assertRaises(FatalShutdown) as raised:
                session.serve(threading.Event())
        self.assertIs(raised.exception, fatal)
        broker.shutdown.assert_called_once()
        self.assertTrue(pipe.closed)
        self.assertFalse(session.cleanup_confirmed)
        self.assertFalse(session._worker.is_alive())
