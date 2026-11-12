import struct
import unittest
from unittest.mock import Mock

from remontevoice.broker_pipe_client import BrokerPipeClient, BrokerClientError, BrokerClientOpenError
from remontevoice.mic_broker import PeerIdentity

PEER = PeerIdentity('S-1-5-21-1000','S-1-5-5-1-42',1,'a'*64)


def response(command, payload):
    return [struct.pack('<4sBBH', b'RVMB', 1, command | 0x80, len(payload)), payload]


class Pipe:
    handle = 51
    def __init__(self, reads):
        self.reads = list(reads)
        self.writes = []
        self.closed = False
    def read_exact(self, size, timeout):
        value = self.reads.pop(0)
        if len(value) != size: raise AssertionError('Wrong read size')
        return value
    def write_all(self, value, timeout): self.writes.append(value)
    def close(self): self.closed = True


class BrokerPipeClientTests(unittest.TestCase):
    def test_pending_native_close_is_retained_and_retryable(self):
        client,pipe,_=self.client(response(4,b'\1'))
        pipe.close=Mock(side_effect=[OSError('pending I/O'),None])
        with self.assertRaises(OSError):client.close()
        self.assertFalse(client.cleanup_confirmed)
        client.close()
        self.assertTrue(client.cleanup_confirmed)
        self.assertEqual(pipe.close.call_count,2)

    def test_fatal_identity_is_preserved_when_constructor_cleanup_also_fails(self):
        class FatalIdentity(BaseException):
            pass
        pipe = Pipe([])
        pipe.close = Mock(side_effect=OSError('pending pipe'))
        verifier = Mock()
        fatal = FatalIdentity()
        verifier.verify_pipe_peer.side_effect = fatal
        verifier.forget_pipe_peer.side_effect = OSError('pending identity')
        client = object.__new__(BrokerPipeClient)
        with self.assertRaises(FatalIdentity) as raised:
            client.__init__(pipe, verifier)
        self.assertIs(raised.exception, fatal)
        self.assertIs(raised.exception.cleanup_owner, client)
        pipe.close.assert_called_once()
        verifier.forget_pipe_peer.assert_called_once_with(51)
        self.assertFalse(client.cleanup_confirmed)

    def test_constructor_handshake_failure_exposes_cleanup_owner_until_worker_retries(self):
        pipe = Pipe([])
        pipe.close = Mock(side_effect=[OSError('pending pipe'), None])
        verifier = Mock()
        verifier.verify_pipe_peer.side_effect = OSError('handshake rejected')
        verifier.forget_pipe_peer.side_effect = [OSError('pending identity'), None]
        with self.assertRaises(BrokerClientOpenError) as raised:
            BrokerPipeClient(pipe, verifier)
        owner = raised.exception.cleanup_owner
        self.assertIs(type(owner), BrokerPipeClient)
        self.assertFalse(owner.cleanup_confirmed)
        self.assertEqual(str(raised.exception), 'Broker setup cleanup is not confirmed')
        self.assertFalse(pipe.writes)
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)
        self.assertEqual(pipe.close.call_count, 2)
        self.assertEqual(verifier.forget_pipe_peer.call_count, 2)

    def test_fatal_request_identity_survives_compound_cleanup_failure_and_can_retry_close(self):
        class FatalIdentity(BaseException):
            pass
        client, pipe, verifier = self.client([])
        fatal = FatalIdentity()
        verifier.validate_pipe_token.side_effect = fatal
        pipe.close = Mock(side_effect=[OSError('pending pipe'), None])
        verifier.forget_pipe_peer.side_effect = [OSError('pending identity'), None]
        with self.assertRaises(FatalIdentity) as raised:
            client.heartbeat()
        self.assertIs(raised.exception, fatal)
        self.assertFalse(client.cleanup_confirmed)
        self.assertEqual(len(pipe.writes), 1)
        self.assertEqual(verifier.forget_pipe_peer.call_count, 1)
        # Only a test retries cleanup after the fatal exception. A real broker
        # process must terminate and must never resume privileged dispatch.
        client.close()
        self.assertTrue(client.cleanup_confirmed)
        self.assertEqual(pipe.close.call_count, 2)

    def test_fatal_identity_cleanup_takes_precedence_over_ordinary_pipe_close_error(self):
        class FatalIdentity(BaseException):
            pass
        client, pipe, verifier = self.client(response(4, b'\1'))
        pipe.close = Mock(side_effect=OSError('pending pipe'))
        verifier.forget_pipe_peer.side_effect = FatalIdentity()
        with self.assertRaises(FatalIdentity):
            client.close()
        self.assertFalse(client.cleanup_confirmed)

    def client(self, reads):
        pipe = Pipe(response(3,b'\1')+reads)
        verifier = Mock()
        verifier.verify_pipe_peer.return_value = PEER
        verifier.validate_pipe_token.return_value = True
        return BrokerPipeClient(pipe,verifier,clock=lambda: 10.0),pipe,verifier

    def test_server_verified_before_first_write_and_connection_starts_muted(self):
        client,pipe,verifier=self.client([])
        verifier.verify_pipe_peer.assert_called_once_with(51,server_side=False)
        self.assertEqual(pipe.writes,[b'RVMB\1\3\0\0'])
        self.assertIsNone(client.observation)

    def test_enable_ack_cannot_imply_writer_readiness(self):
        client,_,_=self.client(response(1,b'\1'))
        self.assertTrue(client.set_microphone_enabled(True))
        self.assertIsNone(client.observation)

    def test_unchanged_poll_counter_cannot_renew_health_timestamp(self):
        status=struct.pack('<BQ',1,4)
        client,_,_=self.client(response(5,status)+response(5,status))
        self.assertEqual(client.observe().observed_at,10.0)
        client._clock=lambda: 11.0
        self.assertEqual(client.observe().observed_at,10.0)

    def test_bad_server_header_or_status_closes_connection(self):
        for reads in (response(5,struct.pack('<BQ',5,1)),
                      response(5,struct.pack('<BQ',1,0)),
                      [b'fake\1\x85\x09\0']):
            with self.subTest(reads=reads):
                client,pipe,_=self.client(reads)
                with self.assertRaises(BrokerClientError):client.observe()
                self.assertTrue(pipe.closed)
                self.assertIsNone(client.observation)

    def test_server_change_prevents_any_further_command(self):
        client,pipe,verifier=self.client([])
        verifier.validate_pipe_token.return_value=False
        with self.assertRaises(PermissionError):client.heartbeat()
        self.assertEqual(len(pipe.writes),1)
        self.assertTrue(pipe.closed)

    def test_close_and_nonfinite_frame_do_not_enable_microphone(self):
        client,pipe,_=self.client(response(4,b'\1'))
        with self.assertRaises(ValueError):client.submit(struct.pack('<480f',*([float('nan')]*480)))
        client.close()
        client.close()
        self.assertTrue(pipe.closed)
        self.assertEqual([value[5] for value in pipe.writes],[3,4])
