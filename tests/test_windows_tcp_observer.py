"""Read-only socket diagnostic ABI and no-payload native loopback check."""
import ctypes as ct
import os
import socket
import unittest
from unittest.mock import patch

from remotevoice import windows_tcp_observer as observer


class TcpObserverTests(unittest.TestCase):
    def test_sdk_v0_layout_matches_fixed_width_windows_abi(self):
        self.assertEqual(ct.sizeof(observer._TcpInfo), 88)
        self.assertEqual(observer._TcpInfo.BytesOut.offset, 48)
        self.assertEqual(observer._TcpInfo.TimeoutEpisodes.offset, 80)
        self.assertEqual(observer._SIO_TCP_INFO, 0xD8000027)

    def test_only_numeric_whitelist_is_returned_and_query_never_writes_the_socket(self):
        class Socket:
            def fileno(self): return 123
        def read(handle, code, version, size, output, capacity, returned, overlapped, completion):
            self.assertEqual((handle, code, size, capacity, overlapped, completion),
                (123, observer._SIO_TCP_INFO, 4, 88, None, None))
            self.assertEqual(ct.cast(version, ct.POINTER(ct.c_uint32)).contents.value, 0)
            info=ct.cast(output, ct.POINTER(observer._TcpInfo)).contents
            info.RttUs=123000;info.BytesRetrans=400;info.BytesInFlight=2048
            ct.cast(returned, ct.POINTER(ct.c_uint32)).contents.value=88
            return 0
        with patch.object(observer, '_query_api', return_value=read):
            result=observer.read_tcp_statistics(Socket())
        self.assertEqual(result['rtt_us'], 123000)
        self.assertEqual(result['bytes_retrans'], 400)
        self.assertEqual(result['bytes_in_flight'], 2048)
        self.assertEqual(set(result), set(observer.TCP_FIELDS))
        self.assertTrue(all(type(value) is int for value in result.values()))

    def test_unavailable_socket_native_failure_and_short_result_are_ignored(self):
        class Socket:
            def fileno(self): return 123
        for query in (lambda *args: -1, lambda *args: 0):
            with patch.object(observer, '_query_api', return_value=query):
                self.assertIsNone(observer.read_tcp_statistics(Socket()))
        with patch.object(observer, '_query_api', side_effect=OSError('unavailable')):
            self.assertIsNone(observer.read_tcp_statistics(Socket()))
        self.assertIsNone(observer.read_tcp_statistics(None))

    @unittest.skipUnless(os.name=='nt', 'Windows read-only socket observer')
    def test_native_loopback_reads_counters_and_preserves_socket_ownership(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0));listener.listen(1);listener.settimeout(2)
            with socket.create_connection(listener.getsockname(), timeout=2) as client:
                server, _=listener.accept()
                with server:
                    before=observer.read_tcp_statistics(server)
                    self.assertIsNotNone(before)
                    server.sendall(b'scalar-probe')
                    self.assertEqual(client.recv(12), b'scalar-probe')
                    after=observer.read_tcp_statistics(server)
                    self.assertGreaterEqual(after['bytes_out'], before['bytes_out']+12)
                    # The observer did not close, receive from, send on, or reconfigure the socket.
                    client.sendall(b'ok')
                    self.assertEqual(server.recv(2), b'ok')
