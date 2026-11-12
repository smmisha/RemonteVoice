import asyncio
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from remontevoice.server import RemonteVoiceServer


_CLOSED = object()


class CleanupSocket:
    def __init__(self, credential="synthetic-pin", *, pending=False):
        self.request = SimpleNamespace(path="/")
        self.remote_address = ("192.168.1.4", 54321)
        self.close_code = None
        self.incoming = asyncio.Queue()
        self.sent = []
        self.recv_calls = 0
        self.receiving = asyncio.Event()
        if not pending:
            self.incoming.put_nowait(json.dumps({"type": "auth", "credential": credential}))

    async def recv(self):
        self.recv_calls += 1
        self.receiving.set()
        return await self.incoming.get()

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.incoming.put_nowait(_CLOSED)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.recv()
        if message is _CLOSED:
            raise StopAsyncIteration
        return message


class CleanupAudio:
    def __init__(self, *args):
        self.microphone_enabled = False

    def start(self):
        pass

    def stop(self):
        pass


class ServerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.statuses = []
        self.connected = asyncio.Event()
        self.restored = asyncio.Event()
        self.state_on_restore = []
        pairing = MagicMock()
        pairing.authenticate.return_value = "synthetic-token"

        def status(value):
            self.statuses.append(value)
            if value == "iphone_connected_muted":
                self.connected.set()
            elif value == "server_ready":
                self.state_on_restore.append((self.server._client_active, self.server._audio_session))
                self.restored.set()

        self.server = RemonteVoiceServer(
            "127.0.0.1", 8765, "fake-capture", "fake-playback", Path("."), pairing, status
        )
        self.server._loop = asyncio.get_running_loop()
        self.server._client_lock = asyncio.Lock()
        self.server._stop_event = asyncio.Event()
        self.socket = CleanupSocket()
        self.task = None

    async def asyncTearDown(self):
        if self.task is not None and not self.task.done():
            await self.socket.close()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def start(self, audio):
        with patch("remontevoice.server.AudioBridge", return_value=audio):
            self.task = asyncio.create_task(self.server._handle_client(self.socket))
            await asyncio.wait_for(self.connected.wait(), 1)

    async def test_remote_close_restores_before_stop_and_revokes_screen(self):
        audio = CleanupAudio()
        state_on_stop = []
        audio.stop = lambda: state_on_stop.append(self.restored.is_set())
        await self.start(audio)
        session = self.server._audio_session
        ticket = self.server._screen_tickets.issue(session, "192.168.1.4", permission=0)
        preview = asyncio.create_task(asyncio.Event().wait())
        self.server._screen_handler = preview
        self.server._screen_session = session
        await self.socket.close()
        await asyncio.wait_for(self.task, 1)
        await asyncio.gather(preview, return_exceptions=True)

        self.assertEqual(state_on_stop, [True])
        self.assertEqual(self.state_on_restore, [(False, None)])
        self.assertIsNone(self.server._audio_socket)
        self.assertIsNone(self.server._audio_ip)
        self.assertIsNone(self.server._screen_session)
        self.assertTrue(preview.cancelled())
        self.assertFalse(self.server._screen_tickets.consume(ticket, session, "192.168.1.4", permission=0))
        self.assertFalse(self.server._stop_requested.is_set())
        self.assertFalse(self.server._client_lock.locked())

    async def test_stalled_audio_restores_laptop_even_while_socket_is_connected(self):
        audio = CleanupAudio()
        audio.assert_progress = MagicMock(side_effect=RuntimeError('stalled native record'))
        await self.start(audio)
        await asyncio.wait_for(self.task, 1)
        self.assertTrue(self.restored.is_set())
        self.assertEqual(self.socket.close_code, 1011)
        self.assertIn('audio_error: Аудиоустройство перестало передавать звук', self.statuses)

    async def test_final_diagnostics_use_bridge_identity_without_authentication_data(self):
        audio = CleanupAudio()
        audio.diagnostic_id = 'b' * 32
        audio.stop = MagicMock()
        await self.start(audio)
        with self.assertLogs('remontevoice.server', level='INFO') as records:
            await self.socket.close()
            await asyncio.wait_for(self.task, 1)
        summary = [r for r in records.output if 'audio_session_end ' in r]
        self.assertEqual(len(summary), 1)
        self.assertIn('session=' + audio.diagnostic_id, summary[0])
        self.assertIn('stage=stream', summary[0])
        self.assertNotIn('synthetic-token', summary[0])
        audio.stop.assert_called_once()
        self.assertTrue(self.restored.is_set())

    async def test_progress_observer_failure_still_closes_session(self):
        original = self.server.on_status

        def status(value):
            if value.startswith('audio_error:'):
                raise RuntimeError('synthetic failed progress observer')
            original(value)

        self.server.on_status = status
        audio = CleanupAudio()
        audio.assert_progress = MagicMock(side_effect=RuntimeError('synthetic stalled audio'))
        await self.start(audio)
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 1)
        except asyncio.TimeoutError:
            self.fail('progress observer failure left the connected audio session open')
        self.assertTrue(self.restored.is_set())
        self.assertEqual(self.socket.close_code, 1011)

    async def test_log_sink_failure_cannot_skip_audio_stop_or_route_restoration(self):
        audio = CleanupAudio()
        audio.stop = MagicMock()
        await self.start(audio)
        with patch('remontevoice.server.LOGGER.info', side_effect=RuntimeError('failed log sink')):
            await self.socket.close()
            await asyncio.wait_for(self.task, 1)
        audio.stop.assert_called_once()
        self.assertTrue(self.restored.is_set())
        self.assertFalse(self.server._client_lock.locked())

    async def test_progress_failure_record_identifies_current_session(self):
        audio = CleanupAudio()
        audio.diagnostic_id = 'c' * 32
        audio.assert_progress = MagicMock(side_effect=RuntimeError('private endpoint'))
        with self.assertLogs('remontevoice.server', level='INFO') as records:
            await self.start(audio)
            await asyncio.wait_for(self.task, 1)
        event = [r for r in records.output if 'audio_session_failure stage=audio_progress' in r]
        self.assertEqual(len(event), 1)
        self.assertIn('session=' + audio.diagnostic_id, event[0])
        self.assertNotIn('private endpoint', event[0])
        self.assertTrue(self.restored.is_set())

    async def test_stalled_start_cannot_block_loop_or_laptop_restoration(self):
        audio = CleanupAudio()
        release = threading.Event()
        audio.start = lambda: release.wait(2)
        self.server.on_session_start = lambda: True
        try:
            with patch('remontevoice.server._AUDIO_START_TIMEOUT', 0.02), patch(
                'remontevoice.server.AudioBridge', return_value=audio
            ), self.assertLogs('remontevoice.server', level='ERROR'):
                await asyncio.wait_for(self.server._handle_client(self.socket), 1)
            self.assertTrue(self.restored.is_set())
            self.assertTrue(self.server._stop_requested.is_set())
        finally:
            release.set()

    async def test_stop_exception_cannot_skip_output_restoration(self):
        audio = CleanupAudio()
        audio.stop = MagicMock(side_effect=RuntimeError("synthetic cleanup failure"))
        await self.start(audio)
        with self.assertLogs("remontevoice.server", level="ERROR"):
            await self.socket.close()
            await asyncio.wait_for(self.task, 1)

        self.assertEqual(self.state_on_restore, [(False, None)])
        self.assertLess(self.statuses.index("server_ready"), len(self.statuses) - 1)
        self.assertTrue(self.statuses[-1].startswith("audio_cleanup_failed:"))
        self.assertTrue(self.server._stop_requested.is_set())
        self.assertTrue(self.server.cleanup_failed)
        self.assertIs(self.server._unclean_audio, audio)
        self.assertFalse(self.server._client_lock.locked())
        replacement = CleanupSocket()
        self.server.pairing.authenticate.reset_mock()
        with patch("remontevoice.server.AudioBridge") as replacement_audio:
            await self.server._handle_client(replacement)
        self.assertEqual(replacement.close_code, 1012)
        self.server.pairing.authenticate.assert_not_called()
        replacement_audio.assert_not_called()

    async def test_stalled_stop_notifies_immediately_then_times_out(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        audio = CleanupAudio()

        def stop():
            entered.set()
            try:
                release.wait(3)
            finally:
                finished.set()

        audio.stop = stop
        await self.start(audio)
        try:
            with patch("remontevoice.server._AUDIO_STOP_TIMEOUT", 0.05):
                with self.assertLogs("remontevoice.server", level="ERROR"):
                    await self.socket.close()
                    await asyncio.wait_for(self.restored.wait(), 0.5)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))
                    self.assertFalse(finished.is_set())
                    self.assertFalse(self.server._client_active)
                    self.assertTrue(self.server._client_lock.locked())
                    replacement = CleanupSocket()
                    self.server.pairing.authenticate.reset_mock()
                    with patch("remontevoice.server.AudioBridge") as replacement_audio:
                        await asyncio.wait_for(self.server._handle_client(replacement), 0.2)
                    self.assertEqual(replacement.close_code, 4002)
                    self.assertEqual(replacement.recv_calls, 0)
                    self.server.pairing.authenticate.assert_not_called()
                    replacement_audio.assert_not_called()
                    await asyncio.wait_for(self.task, 1)
            self.assertEqual(self.state_on_restore, [(False, None)])
            self.assertTrue(self.server._stop_requested.is_set())
            self.assertTrue(self.statuses[-1].startswith("audio_cleanup_failed:"))
            self.assertTrue(self.server.cleanup_failed)
            self.assertIs(self.server._unclean_audio, audio)
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_stop_request_rejects_replacement_before_audio_starts(self):
        self.server.stop()
        with patch("remontevoice.server.AudioBridge") as audio:
            await asyncio.wait_for(self.server._handle_client(self.socket), 1)
        audio.assert_not_called()
        self.server.pairing.authenticate.assert_not_called()
        self.assertEqual(self.socket.close_code, 1012)
        self.assertFalse(self.server._client_active)
        self.assertNotIn("iphone_connected_muted", self.statuses)

    async def test_cleanup_cancellation_keeps_disconnected_state_and_restoration(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        audio = CleanupAudio()

        def stop():
            entered.set()
            try:
                release.wait(3)
            finally:
                finished.set()

        audio.stop = stop
        await self.start(audio)
        try:
            await self.socket.close()
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(self.restored.is_set())
            self.task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self.task
            self.assertEqual(self.state_on_restore, [(False, None)])
            self.assertTrue(self.server._stop_requested.is_set())
            self.assertTrue(self.server.cleanup_failed)
            self.assertIs(self.server._unclean_audio, audio)
            self.assertFalse(self.server._client_lock.locked())
            self.assertIsNone(self.server._screen_session)
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_route_preparation_precedes_audio_and_authentication_reply(self):
        prepared = threading.Event()
        order = []

        def prepare():
            order.append("prepare")
            self.assertEqual(self.socket.sent, [])
            prepared.set()
            return True

        self.server.on_session_start = prepare
        audio = CleanupAudio()
        audio.start = lambda: order.append("start")
        await self.start(audio)
        self.assertTrue(prepared.is_set())
        self.assertEqual(order, ["prepare", "start"])
        self.assertEqual(self.socket.sent[0]["type"], "auth_ok")
        await self.socket.close()
        await asyncio.wait_for(self.task, 1)

    async def test_failed_route_preparation_cannot_start_audio_or_send_auth_ok(self):
        for prepare in (lambda: False, MagicMock(side_effect=RuntimeError("synthetic route error"))):
            with self.subTest(prepare=prepare):
                self.server.on_session_start = prepare
                self.socket = CleanupSocket()
                with patch("remontevoice.server.AudioBridge") as audio:
                    with self.assertLogs("remontevoice.server", level="ERROR"):
                        await asyncio.wait_for(self.server._handle_client(self.socket), 1)
                audio.assert_not_called()
                self.assertEqual(self.socket.close_code, 1011)
                self.assertEqual(self.socket.sent, [])
                self.assertTrue(self.restored.is_set())
                self.assertFalse(self.server._client_lock.locked())

    async def test_stop_during_route_preparation_cannot_start_audio(self):
        def prepare():
            self.server.stop()
            return True

        self.server.on_session_start = prepare
        with patch("remontevoice.server.AudioBridge") as audio:
            await asyncio.wait_for(self.server._handle_client(self.socket), 1)
        audio.assert_not_called()
        self.assertEqual(self.socket.close_code, 1012)
        self.assertEqual(self.socket.sent, [])
        self.assertTrue(self.restored.is_set())

    async def test_route_preparation_timeout_stops_admission_and_restores(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def prepare():
            entered.set()
            try:
                release.wait(3)
                return True
            finally:
                finished.set()

        self.server.on_session_start = prepare
        try:
            with patch("remontevoice.server._SESSION_START_TIMEOUT", 0.05):
                with patch("remontevoice.server.AudioBridge") as audio:
                    with self.assertLogs("remontevoice.server", level="ERROR"):
                        await asyncio.wait_for(self.server._handle_client(self.socket), 1)
            audio.assert_not_called()
            self.assertTrue(entered.is_set())
            self.assertFalse(finished.is_set())
            self.assertTrue(self.restored.is_set())
            self.assertTrue(self.server._stop_requested.is_set())
            self.assertEqual(self.socket.sent, [])
            self.assertEqual(self.socket.close_code, 1011)
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_route_preparation_cancellation_restores_and_stops_admission(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def prepare():
            entered.set()
            try:
                release.wait(3)
                return True
            finally:
                finished.set()

        self.server.on_session_start = prepare
        try:
            with patch("remontevoice.server.AudioBridge") as audio:
                self.task = asyncio.create_task(self.server._handle_client(self.socket))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await self.task
            audio.assert_not_called()
            self.assertTrue(self.restored.is_set())
            self.assertTrue(self.server._stop_requested.is_set())
            self.assertEqual(self.socket.sent, [])
            self.assertFalse(self.server._client_active)
            self.assertFalse(self.server._client_lock.locked())
            self.server.pairing.authenticate.assert_not_called()
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))


if __name__ == "__main__":
    unittest.main()
