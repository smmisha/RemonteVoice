import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from websockets.exceptions import ConnectionClosedOK

from remotevoice.server import RemoteVoiceServer


class ReadySocket:
    def __init__(self, auth, events, *, ready=None, messages=()):
        self.request = SimpleNamespace(path="/")
        self.remote_address = ("192.168.1.4", 54321)
        self.close_code = None
        self.close_reason = None
        self.sent = []
        self.recv_calls = 0
        self.events = events
        self.auth_sent = asyncio.Event()
        self.waiting_ready = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps(auth))
        if ready is not None:
            self.incoming.put_nowait(ready)
        self.messages = iter(messages)

    async def recv(self):
        self.recv_calls += 1
        if self.recv_calls == 2:
            self.waiting_ready.set()
        message = await self.incoming.get()
        if isinstance(message, Exception):
            self.close_code = 1000
            raise message
        if self.recv_calls == 2:
            self.events.append("ready_received")
        return message

    async def send(self, message):
        if isinstance(message, str):
            message = json.loads(message)
            self.events.append(message["type"])
            if message["type"] == "auth_ok":
                self.auth_sent.set()
        else:
            self.events.append("frame")
        self.sent.append(message)

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.close_code is not None:
            raise StopAsyncIteration
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None


class ClientAudioReadyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.pairing = MagicMock()
        self.pairing.is_valid_credential.side_effect = lambda credential: credential == "valid"
        self.pairing.authenticate.return_value = "saved-token"
        self.server = self.make_server()
        self.audio_instances = []
        events = self.events
        instances = self.audio_instances

        class FakeAudio:
            def __init__(self, capture, playback, loop, websocket, send_lock, callback):
                self.loop = loop
                self.websocket = websocket
                self.microphone_enabled = False
                self.started = False
                self.stopped = False
                instances.append(self)

            def start(self):
                self.started = True
                events.append("start")
                asyncio.run_coroutine_threadsafe(
                    self.websocket.send(b"synthetic-system-frame"), self.loop
                ).result(timeout=1)

            def stop(self):
                self.stopped = True
                events.append("stop")

            def set_microphone_enabled(self, enabled):
                self.microphone_enabled = enabled
                events.append("microphone")

            def submit_phone_audio(self, payload):
                events.append("microphone_frame")

        self.audio_patch = patch("remotevoice.server.AudioBridge", FakeAudio)
        self.audio_patch.start()
        self.addCleanup(self.audio_patch.stop)

    def make_server(self):
        def prepare():
            self.events.append("prepare")
            return True

        server = RemoteVoiceServer(
            "127.0.0.1", 8765, "fake-capture", "fake-playback", Path("unused"),
            self.pairing, self.events.append, on_session_start=MagicMock(side_effect=prepare),
        )
        server._loop = asyncio.get_running_loop()
        server._client_lock = asyncio.Lock()
        server._stop_event = asyncio.Event()
        return server

    def auth(self, **fields):
        return {"type": "auth", "credential": "valid", **fields}

    def assert_released(self):
        self.assertFalse(self.server._client_lock.locked())
        self.assertFalse(self.server._client_active)
        self.assertIsNone(self.server._audio_session)
        self.assertIsNone(self.server._audio_socket)
        self.assertIsNone(self.server._audio_ip)
        self.assertIsNone(self.server._unclean_audio)
        self.assertFalse(self.server.cleanup_failed)

    def assert_restored_before_stop(self):
        self.assertLess(self.events.index("server_ready"), self.events.index("stop"))
        self.assertTrue(self.audio_instances[-1].stopped)
        self.assert_released()

    async def pending_session(self):
        websocket = ReadySocket(self.auth(audio_ready=True), self.events)
        task = asyncio.create_task(self.server._handle_client(websocket))
        self.addAsyncCleanup(self.finish_task, task)
        await asyncio.wait_for(websocket.auth_sent.wait(), 1)
        return websocket, task

    @staticmethod
    async def finish_task(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_negotiated_client_receives_ack_before_ready_and_first_audio_frame(self):
        websocket, task = await self.pending_session()
        self.assertEqual(websocket.sent, [{
            "type": "auth_ok", "credential": "saved-token",
            "microphone_enabled": False, "audio_ready": True,
        }])
        self.assertNotIn("start", self.events)
        self.assertLess(self.events.index("prepare"), self.events.index("auth_ok"))
        websocket.incoming.put_nowait('{"type":"audio_ready"}')
        await asyncio.wait_for(task, 1)
        self.assertLess(self.events.index("auth_ok"), self.events.index("ready_received"))
        self.assertLess(self.events.index("ready_received"), self.events.index("start"))
        self.assertLess(self.events.index("start"), self.events.index("frame"))
        self.assert_restored_before_stop()

    async def test_readiness_timeout_restores_routes_before_audio_cleanup(self):
        websocket = ReadySocket(self.auth(audio_ready=True), self.events)
        with patch("remotevoice.server._AUDIO_READY_TIMEOUT", 0.02, create=True):
            await asyncio.wait_for(self.server._handle_client(websocket), 1)
        self.assertEqual(websocket.close_code, 4004)
        self.assertNotIn("start", self.events)
        self.assertNotIn("frame", self.events)
        self.assert_restored_before_stop()

    async def test_malformed_or_premature_messages_cannot_start_audio(self):
        for ready in (
            b"unexpected-binary", b"\x01" + bytes(480 * 4), "invalid-json", "[]", "{}",
            '{"type":"mic_enabled","enabled":true}', '{"type":"ping"}',
            '{"type":"audio_ready","extra":true}', '{"type":"audio_ready"}' + " " * 128,
            '{"type":"audio_ready","nested":' + "[" * 1100 + "0" + "]" * 1100 + "}",
        ):
            with self.subTest(ready=ready[:80]):
                self.events.clear()
                self.server = self.make_server()
                websocket = ReadySocket(self.auth(audio_ready=True), self.events, ready=ready)
                await asyncio.wait_for(self.server._handle_client(websocket), 1)
                self.assertEqual(websocket.close_code, 4004)
                self.assertNotIn("start", self.events)
                self.assertNotIn("microphone", self.events)
                self.assertNotIn("microphone_frame", self.events)
                self.assert_restored_before_stop()

    async def test_negotiation_requires_exact_true_before_routes_or_credential_commit(self):
        for flag in (False, None, 0, 1, "true", {}, []):
            with self.subTest(flag=flag):
                self.events.clear()
                self.pairing.reset_mock()
                self.server = self.make_server()
                websocket = ReadySocket(self.auth(audio_ready=flag), self.events)
                await asyncio.wait_for(self.server._handle_client(websocket), 1)
                self.assertEqual(websocket.close_code, 4001)
                self.assertEqual(websocket.sent, [{"type": "auth_error"}])
                self.server.on_session_start.assert_not_called()
                self.pairing.authenticate.assert_not_called()
                self.assertNotIn("start", self.events)
                self.assert_released()

    async def test_legacy_and_negotiated_ack_shapes_with_screen_detail(self):
        for negotiated in (False, True):
            for screen_detail in (False, True):
                with self.subTest(negotiated=negotiated, screen_detail=screen_detail):
                    self.events.clear()
                    self.server = self.make_server()
                    fields = {"screen_detail": screen_detail}
                    if negotiated:
                        fields["audio_ready"] = True
                    websocket = ReadySocket(
                        self.auth(**fields), self.events,
                        ready='{"type":"audio_ready"}' if negotiated else None,
                    )
                    await asyncio.wait_for(self.server._handle_client(websocket), 1)
                    expected = {
                        "type": "auth_ok", "credential": "saved-token", "microphone_enabled": False,
                    }
                    if screen_detail:
                        expected["screen_detail"] = True
                    if negotiated:
                        expected["audio_ready"] = True
                    self.assertEqual(websocket.sent[0], expected)
                    self.assertEqual(websocket.recv_calls, 2 if negotiated else 1)
                    self.assertIn("start", self.events)
                    self.assert_restored_before_stop()

    async def test_duplicate_ready_is_rejected_without_processing_following_controls(self):
        websocket = ReadySocket(
            self.auth(audio_ready=True), self.events, ready='{"type":"audio_ready"}',
            messages=['{"type":"audio_ready"}', '{"type":"mic_enabled","enabled":true}'],
        )
        await asyncio.wait_for(self.server._handle_client(websocket), 1)
        self.assertEqual(websocket.close_code, 4004)
        self.assertNotIn("microphone", self.events)
        self.assert_restored_before_stop()

    async def test_ready_without_negotiation_is_rejected(self):
        websocket = ReadySocket(
            self.auth(), self.events, messages=['{"type":"audio_ready"}'],
        )
        await asyncio.wait_for(self.server._handle_client(websocket), 1)
        self.assertEqual(websocket.close_code, 4004)
        self.assert_restored_before_stop()

    async def test_pending_readiness_keeps_single_client_slot(self):
        websocket, task = await self.pending_session()
        second = ReadySocket(self.auth(audio_ready=True), self.events)
        await asyncio.wait_for(self.server._handle_client(second), 1)
        self.assertEqual(second.close_code, 4002)
        self.assertEqual(second.recv_calls, 0)
        self.assertEqual(self.pairing.authenticate.call_count, 1)
        self.assertEqual(self.server.on_session_start.call_count, 1)
        websocket.incoming.put_nowait('{"type":"audio_ready"}')
        await asyncio.wait_for(task, 1)
        self.assert_restored_before_stop()

    async def test_stop_while_waiting_ready_cannot_start_audio(self):
        websocket, task = await self.pending_session()
        self.server.stop()
        websocket.incoming.put_nowait('{"type":"audio_ready"}')
        await asyncio.wait_for(task, 1)
        self.assertEqual(websocket.close_code, 1012)
        self.assertNotIn("start", self.events)
        self.assert_restored_before_stop()

    async def test_connection_close_while_waiting_ready_restores_routes(self):
        websocket, task = await self.pending_session()
        websocket.incoming.put_nowait(ConnectionClosedOK(None, None))
        await asyncio.wait_for(task, 1)
        self.assertNotIn("start", self.events)
        self.assert_restored_before_stop()

    async def test_cancellation_while_waiting_ready_restores_routes(self):
        websocket, task = await self.pending_session()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.server._stop_requested.is_set())
        self.assertNotIn("start", self.events)
        self.assert_restored_before_stop()


if __name__ == "__main__":
    unittest.main()
