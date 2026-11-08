import asyncio
import json
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from websockets.asyncio.client import connect

from remotevoice.pairing import PairingAttemptLimiter, PairingManager
from remotevoice.protocol import encode_control
from remotevoice.server import RemoteVoiceServer


class FakeAudioBridge:
    def __init__(self, *args, **kwargs):
        self.microphone_enabled = False
        self.frames = []

    def start(self):
        pass

    def stop(self):
        self.microphone_enabled = False

    def set_microphone_enabled(self, enabled):
        self.microphone_enabled = enabled

    def submit_phone_audio(self, payload):
        self.frames.append(bytes(payload))


class AdmissionSocket:
    def __init__(self, credential=None, *, pending=False, message=None):
        self.request = SimpleNamespace(path="/")
        self.remote_address = ("192.168.1.4", 54321)
        self.close_code = None
        self.sent = []
        self.recv_calls = 0
        self.receiving = asyncio.Event()
        self.incoming = asyncio.Queue()
        if not pending:
            self.incoming.put_nowait(message if message is not None else json.dumps({"type": "auth", "credential": credential}))

    async def recv(self):
        self.recv_calls += 1
        self.receiving.set()
        return await self.incoming.get()

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code=1000, reason=""):
        self.close_code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class ServerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pairing = PairingManager(self.root / "paired.json")
        self.old_token = self.pairing.authenticate(self.pairing.pairing_pin)
        self.pin = self.pairing.begin_pairing()
        self.stored = (self.root / "paired.json").read_bytes()
        self.server = RemoteVoiceServer("127.0.0.1", 8765, "fake-capture", "fake-playback", self.root, self.pairing, lambda status: None)
        self.server._loop = asyncio.get_running_loop()
        self.server._client_lock = asyncio.Lock()
        self.server._stop_event = asyncio.Event()

    def assert_credentials_unchanged(self):
        self.assertEqual(self.pairing.pairing_pin, self.pin)
        self.assertEqual((self.root / "paired.json").read_bytes(), self.stored)
        self.assertEqual(self.pairing.authenticate(self.old_token), self.old_token)

    async def test_retired_audio_worker_cannot_publish_error_during_stop(self):
        statuses = []
        self.server.on_status = statuses.append

        class LateErrorAudio(FakeAudioBridge):
            def __init__(self, *args):
                super().__init__(*args)
                self.callback = args[5]

            def start(self):
                self.callback("audio_active")

            def stop(self):
                self.callback("audio_error: retired worker")

        with patch("remotevoice.server.AudioBridge", LateErrorAudio):
            await self.server._handle_client(AdmissionSocket(self.pin))
        self.assertIn("audio_active", statuses)
        self.assertIn("server_ready", statuses)
        self.assertNotIn("audio_error: retired worker", statuses)

    async def test_connected_notification_uses_owned_audio_status_channel(self):
        observed = []
        def scoped(status, session):
            self.assertTrue(self.server.is_current_audio_session(session))
            observed.append(status)
        def generic(status):
            self.assertNotEqual(status, 'iphone_connected_muted', 'Startup waited for generic UI callback')
        self.server.on_audio_status = scoped
        self.server.on_status = generic
        with patch('remotevoice.server.AudioBridge', FakeAudioBridge):
            await self.server._handle_client(AdmissionSocket(self.pin))
        self.assertEqual(observed, ['iphone_connected_muted'])

    async def test_busy_slot_does_not_read_or_consume_pin(self):
        await self.server._client_lock.acquire()
        websocket = AdmissionSocket(self.pin)
        try:
            with patch("remotevoice.server.AudioBridge") as audio:
                await asyncio.wait_for(self.server._handle_client(websocket), 0.2)
            audio.assert_not_called()
            self.assertEqual(websocket.close_code, 4002)
            self.assertEqual(websocket.recv_calls, 0)
            self.assertEqual(websocket.sent, [])
            self.assert_credentials_unchanged()
        finally:
            self.server._client_lock.release()

    async def test_pending_auth_reserves_slot_without_queuing_another_client(self):
        first = AdmissionSocket(pending=True)
        with patch("remotevoice.server.AudioBridge", FakeAudioBridge):
            task = asyncio.create_task(self.server._handle_client(first))
            try:
                await asyncio.wait_for(first.receiving.wait(), 1)
                second = AdmissionSocket(self.pin)
                await asyncio.wait_for(self.server._handle_client(second), 0.2)
                self.assertEqual(second.close_code, 4002)
                self.assertEqual(second.recv_calls, 0)
                self.assert_credentials_unchanged()
                first.incoming.put_nowait(json.dumps({"type": "auth", "credential": self.pin}))
                await asyncio.wait_for(task, 1)
                self.assertEqual([reply["type"] for reply in first.sent], ["auth_ok"])
                token = first.sent[0]["credential"]
                self.assertEqual(self.pairing.authenticate(token), token)
                self.assertIsNone(self.pairing.pairing_pin)
                self.assertIsNone(self.pairing.authenticate(self.old_token))
                self.assertFalse(self.server._client_lock.locked())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_stop_during_pending_auth_does_not_consume_pin(self):
        websocket = AdmissionSocket(pending=True)
        with patch("remotevoice.server.AudioBridge") as audio:
            task = asyncio.create_task(self.server._handle_client(websocket))
            try:
                await asyncio.wait_for(websocket.receiving.wait(), 1)
                self.server.stop()
                websocket.incoming.put_nowait(json.dumps({"type": "auth", "credential": self.pin}))
                await asyncio.wait_for(task, 1)
                audio.assert_not_called()
                self.assertEqual(websocket.close_code, 1012)
                self.assert_credentials_unchanged()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_malformed_credentials_are_normal_rate_limited_failures(self):
        self.server._attempt_limiter = PairingAttemptLimiter(max_failures=5, window_seconds=60)
        self.server.on_session_start = MagicMock(return_value=True)
        malformed = ("тест", "a" * 129, 12345678, {}, None)
        with patch("remotevoice.server.AudioBridge") as audio:
            for credential in malformed:
                with self.subTest(kind=type(credential).__name__):
                    websocket = AdmissionSocket(credential)
                    await asyncio.wait_for(self.server._handle_client(websocket), 1)
                    self.assertEqual(websocket.close_code, 4001)
                    self.assertEqual(websocket.sent, [{"type": "auth_error"}])
                    self.assert_credentials_unchanged()
            limited = AdmissionSocket(self.pin)
            await self.server._handle_client(limited)
        audio.assert_not_called()
        self.server.on_session_start.assert_not_called()
        self.assertEqual(limited.close_code, 4005)
        self.assertEqual(limited.recv_calls, 0)
        self.assert_credentials_unchanged()

    async def test_malformed_auth_envelope_counts_as_failed_attempt(self):
        for message in (
            b"binary-auth",
            '{"type":"ping"}',
            '["not-an-object"]',
            'invalid-json',
            '{"type":"auth","credential":' + '1' * 4500 + '}',
            '{"type":"auth","credential":' + '[' * 1100 + '0' + ']' * 1100 + '}',
        ):
            with self.subTest(message=message):
                self.server._attempt_limiter = PairingAttemptLimiter(max_failures=1, window_seconds=60)
                websocket = AdmissionSocket(message=message)
                await self.server._handle_client(websocket)
                self.assertEqual(websocket.close_code, 4001)
                self.assertFalse(self.server._attempt_limiter.allowed("192.168.1.4"))
                self.assertFalse(self.server._client_lock.locked())
                self.assert_credentials_unchanged()

    async def test_route_preparation_failure_does_not_consume_pin(self):
        for prepare in (lambda: False, MagicMock(side_effect=RuntimeError("synthetic route failure"))):
            with self.subTest(prepare=prepare):
                self.server.on_session_start = prepare
                websocket = AdmissionSocket(self.pin)
                with patch("remotevoice.server.AudioBridge") as audio:
                    with self.assertLogs("remotevoice.server", level="ERROR"):
                        await asyncio.wait_for(self.server._handle_client(websocket), 1)
                audio.assert_not_called()
                self.assertEqual(websocket.close_code, 1011)
                self.assertEqual(websocket.sent, [])
                self.assert_credentials_unchanged()

    async def test_route_preparation_timeout_does_not_consume_pin(self):
        release = threading.Event()
        finished = threading.Event()

        def prepare():
            try:
                release.wait(3)
                return True
            finally:
                finished.set()

        self.server.on_session_start = prepare
        websocket = AdmissionSocket(self.pin)
        try:
            with patch("remotevoice.server._SESSION_START_TIMEOUT", 0.05):
                with patch("remotevoice.server.AudioBridge") as audio:
                    with self.assertLogs("remotevoice.server", level="ERROR"):
                        await asyncio.wait_for(self.server._handle_client(websocket), 1)
            audio.assert_not_called()
            self.assertEqual(websocket.close_code, 1011)
            self.assertEqual(websocket.sent, [])
            self.assert_credentials_unchanged()
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_stop_during_route_preparation_does_not_consume_pin(self):
        def prepare():
            self.server.stop()
            return True

        self.server.on_session_start = prepare
        websocket = AdmissionSocket(self.pin)
        with patch("remotevoice.server.AudioBridge") as audio:
            await asyncio.wait_for(self.server._handle_client(websocket), 1)
        audio.assert_not_called()
        self.assertEqual(websocket.close_code, 1012)
        self.assertEqual(websocket.sent, [])
        self.assert_credentials_unchanged()

    async def test_pin_replaced_during_prepare_cannot_commit_old_pin(self):
        replacement_pin = None

        def prepare():
            nonlocal replacement_pin
            replacement_pin = self.pairing.begin_pairing()
            while replacement_pin == self.pin:
                replacement_pin = self.pairing.begin_pairing()
            return True

        self.server.on_session_start = prepare
        websocket = AdmissionSocket(self.pin)
        with patch("remotevoice.server.AudioBridge") as audio:
            await asyncio.wait_for(self.server._handle_client(websocket), 1)
        audio.assert_not_called()
        self.assertEqual(websocket.close_code, 4001)
        self.assertEqual(websocket.sent, [{"type": "auth_error"}])
        self.assertEqual(self.pairing.pairing_pin, replacement_pin)
        self.assertEqual((self.root / "paired.json").read_bytes(), self.stored)
        self.assertEqual(self.pairing.authenticate(self.old_token), self.old_token)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_client_can_toggle_mic_and_unpaired_client_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pairing = PairingManager(root / "paired.json")
            pin = pairing.pairing_pin
            port = self._unused_port()
            statuses = []
            ready = asyncio.Event()

            def on_status(value):
                statuses.append(value)
                if value == "server_ready":
                    ready.set()

            server = RemoteVoiceServer(
                "127.0.0.1",
                port,
                "test-capture",
                "test-playback",
                root / "identity",
                pairing,
                on_status,
            )
            client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ssl.check_hostname = False
            client_ssl.verify_mode = ssl.CERT_NONE

            with patch("remotevoice.server.AudioBridge", FakeAudioBridge):
                server_task = asyncio.create_task(server.run())
                try:
                    await asyncio.wait_for(ready.wait(), timeout=5)
                    uri = f"wss://127.0.0.1:{port}"
                    async with connect(uri, ssl=client_ssl) as websocket:
                        await websocket.send(
                            encode_control({"type": "auth", "credential": "wrong-code"})
                        )
                        rejection = json.loads(await websocket.recv())
                        self.assertEqual(rejection["type"], "auth_error")

                    async with connect(uri, ssl=client_ssl) as websocket:
                        await websocket.send(encode_control({"type": "auth", "credential": pin}))
                        authenticated = json.loads(await websocket.recv())
                        self.assertEqual(authenticated["type"], "auth_ok")
                        token = authenticated["credential"]

                        await websocket.send(
                            encode_control({"type": "mic_enabled", "enabled": True})
                        )
                        response = json.loads(await websocket.recv())
                        self.assertEqual(response, {"type": "mic_state", "enabled": True})

                    self.assertEqual(pairing.authenticate(token), token)
                finally:
                    server.stop()
                    await asyncio.wait_for(server_task, timeout=5)

            self.assertIn("iphone_microphone_on", statuses)
            self.assertIn("server_stopped", statuses)

    @staticmethod
    def _unused_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
