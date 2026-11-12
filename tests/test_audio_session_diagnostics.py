import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from websockets.asyncio.server import serve
from websockets.frames import Close

from remontevoice.audio_session_diagnostics import close_summary, log_session_end, log_session_error, log_worker_failure


SESSION = "a" * 32


def socket(sent=None, received=None, received_first=None, code=1006):
    return SimpleNamespace(close_code=code, protocol=SimpleNamespace(
        close_sent=sent, close_rcvd=received, close_rcvd_then_sent=received_first))


class CloseDiagnosticsTests(unittest.TestCase):
    def test_local_heartbeat_is_distinct_even_without_peer_close(self):
        result = close_summary(socket(sent=Close(1011, "keepalive ping timeout")))
        self.assertEqual(result, dict(origin="local", cause="heartbeat_timeout", sent_code=1011,
                                      received_code=0, close_code=1006))

    def test_local_audio_and_progress_errors_remain_distinct(self):
        for reason, expected in (("Audio device error", "audio_worker_error"),
                                 ("Audio progress stopped", "audio_progress_stopped"),
                                 ("Host audio error", "host_error")):
            with self.subTest(reason=reason):
                result = close_summary(socket(Close(1011, reason), Close(1011, reason), False, 1011))
                self.assertEqual((result["origin"], result["cause"]), ("local", expected))

    def test_peer_cannot_impersonate_local_heartbeat_with_echoed_reason(self):
        result = close_summary(socket(Close(1011, "keepalive ping timeout"),
                                      Close(1011, "keepalive ping timeout"), True, 1011))
        self.assertEqual((result["origin"], result["cause"]), ("peer", "peer_error"))

    def test_normal_peer_close_and_missing_transport_close_frames(self):
        normal = close_summary(socket(Close(1000, "private"), Close(1000, "private"), True, 1000))
        self.assertEqual(normal["cause"], "peer_normal")
        self.assertEqual(close_summary(socket())["cause"], "transport_lost")
        unknown = close_summary(SimpleNamespace(close_code=None))
        self.assertEqual(unknown["cause"], "unclassified")

    def test_unknown_order_or_reason_is_not_guessed(self):
        result = close_summary(socket(Close(1011, "keepalive ping timeout"), Close(1011, "same")))
        self.assertEqual((result["origin"], result["cause"]), ("unknown", "unclassified"))
        result = close_summary(socket(sent=Close(1011, "private-device-id")))
        self.assertEqual(result["cause"], "local_internal_unclassified")

    def test_records_are_bounded_and_never_include_peer_text_or_exception_message(self):
        secret = "token=PRIVATE user@example.test " * 500
        logger = Mock()
        log_session_end(logger, socket(Close(1011, secret), Close(1011, secret), True, 1011),
                        session_id=SESSION, stage="stream", failure="none")
        log_session_error(logger, session_id=SESSION, stage="audio_start", error=RuntimeError(secret))
        log_worker_failure(logger, session_id=SESSION, stage="system_send", error=TimeoutError(secret))
        for called in (logger.info.call_args, logger.error.call_args, logger.warning.call_args):
            rendered = called.args[0] % called.args[1:]
            self.assertLess(len(rendered), 450)
            self.assertNotIn("PRIVATE", rendered)
            self.assertNotIn("example.test", rendered)
            self.assertIn("session=" + SESSION, rendered)

    def test_invalid_tags_and_logging_failures_do_not_escape(self):
        logger = Mock()
        log_session_end(logger, socket(), session_id="PRIVATE\n" * 100, stage="PRIVATE", failure="PRIVATE")
        rendered = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertNotIn("PRIVATE", rendered)
        logger.info.side_effect = RuntimeError("broken log sink")
        logger.error.side_effect = RuntimeError("broken log sink")
        logger.warning.side_effect = RuntimeError("broken log sink")
        log_session_end(logger, socket(), session_id=SESSION, stage="stream", failure="none")
        log_session_error(logger, session_id=SESSION, stage="stream", error=RuntimeError())
        log_worker_failure(logger, session_id=SESSION, stage="system_send", error=TimeoutError())


class RealHeartbeatDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websockets_heartbeat_close_is_identified_without_audio_devices(self):
        logger = Mock()
        finished = asyncio.Event()

        async def handler(connection):
            try:
                await connection.wait_closed()
            finally:
                log_session_end(logger, connection, session_id=SESSION, stage="stream", failure="none")
                finished.set()

        async with serve(handler, "127.0.0.1", 0, ping_interval=.03, ping_timeout=.03, close_timeout=.05) as server:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                              "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n").encode("ascii"))
                await writer.drain()
                self.assertIn(b"101", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
                # Deliberately never answer Ping; no phone, recorder or NIC changes.
                await asyncio.wait_for(finished.wait(), 3)
                rendered = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
                self.assertIn("cause=heartbeat_timeout", rendered)
                self.assertIn("origin=local", rendered)
                self.assertIn("sent_code=1011", rendered)
            finally:
                writer.close()
                await writer.wait_closed()
