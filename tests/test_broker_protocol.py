import struct
import unittest
from unittest.mock import Mock

from remotevoice.broker_protocol import BrokerConnection, BrokerProtocolError, control_body_size


def frame(command, body=b'', magic=b'RVMB', version=1):
    return struct.pack('<4sBBH', magic, version, command, len(body)) + body


class BrokerProtocolTests(unittest.TestCase):
    def test_transport_rejects_declared_lengths_before_reading_a_body(self):
        for command, size in ((1, 1), (2, 1920), (3, 0), (4, 0), (5, 0)):
            self.assertEqual(control_body_size(struct.pack('<4sBBH', b'RVMB', 1, command, size)), size)
            with self.assertRaises(BrokerProtocolError):
                control_body_size(struct.pack('<4sBBH', b'RVMB', 1, command, 65535))
        for header in (b'', b'RVMB', struct.pack('<4sBBH', b'RVMB', 1, 0x81, 1)):
            with self.assertRaises(BrokerProtocolError):
                control_body_size(header)

    def setUp(self):
        self.broker = Mock()
        self.broker.connect.return_value = b'private server capability'
        self.connection = BrokerConnection(self.broker)

    def test_connect_authenticates_without_accepting_client_claims_or_starting_mic(self):
        self.broker.connect.assert_called_once_with()
        self.broker.set_microphone_enabled.assert_not_called()
        self.broker.submit.assert_not_called()

    def test_denied_connection_cannot_dispatch(self):
        self.broker.connect.return_value = None
        with self.assertRaises(PermissionError):
            BrokerConnection(self.broker)

    def test_explicit_enable_uses_server_side_capability_and_ack_is_only_acceptance(self):
        self.broker.set_microphone_enabled.return_value = True
        self.assertEqual(self.connection.handle_frame(frame(1, b'\1')), frame(0x81, b'\1'))
        self.broker.set_microphone_enabled.assert_called_once_with(b'private server capability', True)

    def test_muted_pcm_can_be_rejected_without_claiming_readiness(self):
        self.broker.submit.return_value = False
        self.assertEqual(self.connection.handle_frame(frame(2, b'\0' * 1920)), frame(0x82, b'\0'))

    def test_malformed_or_oversized_frames_close_ingress(self):
        for data in (b'', b'x' * 1929, frame(6), frame(1, b'yes'), frame(3, b'identity'),
                     frame(2, b'\0' * 4), frame(2, struct.pack('<480f', *([float('nan')] * 480))),
                     frame(1, b'\0', version=2), frame(1, b'\0', magic=b'fake'), frame(3)+b'extra'):
            with self.subTest(size=len(data)):
                broker = Mock()
                broker.connect.return_value = b'capability'
                connection = BrokerConnection(broker)
                with self.assertRaises(BrokerProtocolError):
                    connection.handle_frame(data)
                broker.close.assert_called_once_with(b'capability')
                broker.submit.assert_not_called()

    def test_transport_disconnect_closes_once_and_rejects_replay(self):
        self.connection.close()
        self.connection.close()
        with self.assertRaises(BrokerProtocolError):
            self.connection.handle_frame(frame(3))
        self.broker.close.assert_called_once_with(b'private server capability')
