import asyncio
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from websockets.exceptions import ConnectionClosed

from remotevoice import screen
from remotevoice.server import RemoteVoiceServer
from remotevoice.h264_stream import StreamError


_CLOSED = object()
_JPEG = b"\xff\xd8synthetic-test-frame\xff\xd9"


class FakeSocket:
    def __init__(self, path="/", ip="192.168.1.4"):
        self.request = SimpleNamespace(path=path)
        self.remote_address = (ip, 54321)
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.closed = asyncio.Event()
        self.close_code = None
        self.block_binary = False

    async def recv(self):
        message = await self.incoming.get()
        if message is _CLOSED:
            raise ConnectionClosed(None, None)
        return message

    async def send(self, message):
        if hasattr(message,"__aiter__"):
            message=b"".join([part async for part in message])
        if self.block_binary and isinstance(message, bytes):
            await asyncio.Event().wait()
        if self.closed.is_set():
            raise ConnectionClosed(None, None)
        await self.sent.put(message)

    async def close(self, code=1000, reason=""):
        if not self.closed.is_set():
            self.close_code = code
            self.closed.set()
            self.incoming.put_nowait(_CLOSED)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except ConnectionClosed:
            raise StopAsyncIteration

    def control(self, value):
        self.incoming.put_nowait(json.dumps(value))

    async def response(self):
        value = await asyncio.wait_for(self.sent.get(), 2)
        return json.loads(value) if isinstance(value, str) else value


class FakeAudio:
    def __init__(self, *args):
        self.microphone_enabled = False
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def set_microphone_enabled(self, enabled):
        self.microphone_enabled = enabled

    def submit_phone_audio(self, payload):
        pass


class FakeEncoder:
    def __init__(self):
        self.started = False
        self.stopped = threading.Event()
        self.frames = 0

    def start(self):
        self.started = True

    async def next_frame(self):
        self.frames += 1
        return b'synthetic-h264-access-unit'

    def stop(self):
        self.stopped.set()


class ScreenServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_encoder_or_explicit_optout_keeps_jpeg_capability(self):
        for available, setting, expected in [(False, '1', False), (True, '0', False), (True, '', True)]:
            with patch('remotevoice.server.encoder_available', return_value=available), \
                    patch.dict('os.environ', {'REMOTEVOICE_VIDEO_EXPERIMENTAL': setting}):
                server = RemoteVoiceServer('127.0.0.1', 8765, 'fake', 'fake', Path('.'), self.pairing, lambda _: None)
            self.assertEqual(server._screen_video, expected)
            self.assertFalse(server._screen_video_peer)
            self.assertFalse(server._screen_permission_snapshot()[0])

    async def asyncSetUp(self):
        self.pairing = MagicMock()
        self.pairing.authenticate.return_value = "SYNTHETIC_TOKEN_FOR_LOCAL_TEST_ONLY_123456"
        self.audio_statuses = []
        self.screen_statuses = []
        self.server = RemoteVoiceServer("127.0.0.1", 8765, "fake-capture", "fake-playback", Path("."),
                                        self.pairing, self.audio_statuses.append, self.screen_statuses.append)
        self.server._loop = asyncio.get_running_loop()
        self.server._client_lock = asyncio.Lock()
        self.server._screen_source = screen.ScreenFrameSource(lambda: _JPEG)
        self.audio_patch = patch("remotevoice.server.AudioBridge", FakeAudio)
        self.audio_patch.start()
        self.tasks = []
        self.sockets = []
        self.main = self.start_socket()
        self.main.control({"type": "auth", "credential": "SYNTHETIC-PIN"})
        response = await self.main.response()
        self.assertEqual(response, {"type": "auth_ok", "credential": self.pairing.authenticate.return_value,
                                    "microphone_enabled": False})

    async def asyncTearDown(self):
        self.server._revoke_screen()
        for websocket in self.sockets:
            await websocket.close()
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 3)
        if self.server._screen_source._worker is not None:
            await asyncio.wait_for(asyncio.shield(self.server._screen_source._worker), 3)
        self.audio_patch.stop()

    def start_socket(self, path="/", ip="192.168.1.4"):
        websocket = FakeSocket(path, ip)
        self.sockets.append(websocket)
        self.tasks.append(asyncio.create_task(self.server._handle_client(websocket)))
        return websocket

    async def ticket(self):
        self.main.control({"type": "screen_request"})
        offer = await self.main.response()
        self.assertEqual(set(offer), {"type", "ticket"})
        self.assertEqual(offer["type"], "screen_offer")
        return offer["ticket"]

    async def preview(self):
        ticket = await self.ticket()
        websocket = self.start_socket("/screen")
        websocket.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await websocket.response(), {"type": "screen_ok"})
        return websocket, ticket

    async def assert_audio_alive(self):
        self.main.control({"type": "mic_enabled", "enabled": True})
        self.assertEqual(await self.main.response(), {"type": "mic_state", "enabled": True})
        self.main.control({"type": "ping"})
        self.assertEqual(await self.main.response(), {"type": "pong"})
        self.assertFalse(self.main.closed.is_set())

    async def test_detail_negotiates_only_with_requesting_audio_peer_and_binds_ticket(self):
        await self.main.close()
        await asyncio.wait_for(self.tasks[0],2)
        self.main=self.start_socket()
        self.main.control({"type":"auth","credential":"SYNTHETIC-PIN","screen_detail":True})
        response=await self.main.response()
        self.assertIs(response.get("screen_detail"),True)
        self.server.set_screen_allowed(True)
        self.main.control({"type":"screen_request","request_id":"HD1","profile":"detail"})
        offer=await self.main.response();self.assertEqual(offer["type"],"screen_offer")
        preview=self.start_socket("/screen")
        preview.control({"type":"screen_auth","ticket":offer["ticket"]})
        self.assertEqual(await preview.response(),{"type":"screen_ok"})
        with patch.object(self.server._screen_source,"next_frame",return_value=_JPEG) as capture:
            preview.control({"type":"screen_next"})
            self.assertEqual(await preview.response(),_JPEG)
            self.assertIs(capture.call_args.kwargs.get("detail"),True)
        await self.assert_audio_alive()

    async def test_unnegotiated_or_unknown_profile_is_preview_only_refusal(self):
        self.server.set_screen_allowed(True)
        for profile in ("detail","unknown",True,None):
            self.main.control({"type":"screen_request","request_id":"HD1","profile":profile})
            self.assertEqual(await self.main.response(),{"type":"screen_error","request_id":"HD1","message":"invalid_request"})
        await self.assert_audio_alive()

    async def test_unfinished_video_profile_stays_disabled_for_normal_clients(self):
        self.server.set_screen_allowed(True)
        self.server._screen_detail = True
        self.server._screen_video = False
        with patch('remotevoice.server.create_encoder') as create:
            self.main.control({'type': 'screen_request', 'request_id': 'v1', 'profile': 'video'})
            self.assertEqual(await self.main.response(),
                             {'type': 'screen_error', 'message': 'invalid_request', 'request_id': 'v1'})
            create.assert_not_called()
        await self.assert_audio_alive()

    async def video_preview(self):
        self.server.set_screen_allowed(True)
        self.server._screen_detail = self.server._screen_video = True
        self.server._screen_video_peer = True
        self.main.control({'type': 'screen_request', 'profile': 'video', 'request_id': 'video1'})
        offer = await self.main.response()
        self.assertEqual(offer['type'], 'screen_offer')
        preview = self.start_socket('/screen')
        preview.control({'type': 'screen_auth', 'ticket': offer['ticket']})
        return preview

    async def test_video_capability_requires_both_peers_and_detail_negotiation(self):
        for enabled, requested, detail in [(True, True, True), (False, True, True),
                                           (True, False, True), (True, True, False)]:
            await self.main.close()
            await asyncio.wait_for(self.tasks[-1], 2)
            self.server._screen_video = enabled
            self.main = self.start_socket()
            self.main.control({'type': 'auth', 'credential': 'SYNTHETIC-PIN',
                               'screen_detail': detail, 'screen_video': requested})
            response = await self.main.response()
            negotiated = enabled and requested and detail
            self.assertEqual(response.get('screen_video'), True if negotiated else None)
            self.server.set_screen_allowed(True)
            self.main.control({'type': 'screen_request', 'profile': 'video', 'request_id': 'v1'})
            offer = await self.main.response()
            self.assertEqual(offer['type'], 'screen_offer' if negotiated else 'screen_error')
            if not negotiated:
                self.assertEqual(offer['message'], 'invalid_request')
        await self.assert_audio_alive()

    async def test_video_pulls_one_access_unit_and_revocation_releases_encoder_only(self):
        encoder = FakeEncoder()
        with patch('remotevoice.server.create_encoder', return_value=encoder):
            preview = await self.video_preview()
            self.assertEqual(await preview.response(), {'type': 'screen_ok', 'codec': 'h264',
                                                       'width': 1280, 'height': 800, 'fps': 30})
            self.assertTrue(encoder.started)
            self.assertEqual(encoder.frames, 0)
            preview.control({'type': 'screen_next'})
            self.assertEqual(await preview.response(), b'synthetic-h264-access-unit')
            await asyncio.sleep(.02)
            self.assertEqual(encoder.frames, 1)
            self.server.set_screen_allowed(False)
            await asyncio.wait_for(preview.closed.wait(), 1)
            self.assertTrue(encoder.stopped.is_set())
        await self.assert_audio_alive()

    async def test_video_blocked_writer_is_cancelled_on_audio_disconnect(self):
        encoder = FakeEncoder()
        with patch('remotevoice.server.create_encoder', return_value=encoder):
            preview = await self.video_preview()
            await preview.response()
            preview.block_binary = True
            preview.control({'type': 'screen_next'})
            while not encoder.frames:
                await asyncio.sleep(.001)
            await self.main.close()
            await asyncio.wait_for(preview.closed.wait(), 1)
            self.assertTrue(encoder.stopped.is_set())
            self.assertIsNone(self.server._screen_socket)

    async def test_video_start_failure_stops_owned_encoder_without_ending_audio(self):
        for code, expected in [('encoder_unavailable', 'capture_failed'), ('busy', 'busy')]:
            encoder = FakeEncoder()
            encoder.start = MagicMock(side_effect=StreamError(code))
            with patch('remotevoice.server.create_encoder', return_value=encoder):
                preview = await self.video_preview()
                self.assertEqual(await preview.response(), {'type': 'screen_error', 'message': expected})
                await asyncio.wait_for(preview.closed.wait(), 1)
                self.assertTrue(encoder.stopped.is_set())
            await self.assert_audio_alive()

    async def test_video_revocation_during_native_start_waits_for_owned_cleanup(self):
        encoder = FakeEncoder()
        entered, release = threading.Event(), threading.Event()
        lifecycle = threading.Lock()

        def start():
            with lifecycle:
                entered.set()
                release.wait(2)
                encoder.started = True

        def stop():
            with lifecycle:
                encoder.stopped.set()

        encoder.start, encoder.stop = start, stop
        with patch('remotevoice.server.create_encoder', return_value=encoder):
            preview = await self.video_preview()
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.server.set_screen_allowed(False)
                await asyncio.sleep(.02)
                self.assertFalse(preview.closed.is_set())
                self.assertIs(self.server._screen_socket, preview)
            finally:
                release.set()
            await asyncio.wait_for(preview.closed.wait(), 1)
            self.assertTrue(encoder.stopped.is_set())
            self.assertEqual(encoder.frames, 0)
        await self.assert_audio_alive()

    async def test_video_unknown_cleanup_blocks_new_preview_but_preserves_audio(self):
        encoder = FakeEncoder()
        encoder.stop = MagicMock(side_effect=StreamError('cleanup_unconfirmed'))
        with patch('remotevoice.server.create_encoder', return_value=encoder):
            preview = await self.video_preview()
            await preview.response()
            preview.control({'type': 'unexpected'})
            with self.assertLogs('remotevoice.server', level='WARNING'):
                self.assertEqual(await preview.response(), {'type': 'screen_error', 'message': 'invalid_request'})
                await asyncio.wait_for(preview.closed.wait(), 1)
            self.assertTrue(self.server._video_cleanup_failed)
            self.main.control({'type': 'screen_request'})
            self.assertEqual(await self.main.response(), {'type': 'screen_error', 'message': 'busy'})
        await self.assert_audio_alive()

    async def test_disabled_by_default_and_rejects_credentials_on_screen_channel(self):
        self.main.control({"type": "screen_request"})
        self.assertEqual(await self.main.response(), {"type": "screen_error", "message": "disabled"})
        preview = self.start_socket("/screen")
        preview.control({"type": "auth", "credential": "SYNTHETIC-PIN"})
        self.assertEqual(await preview.response(), {"type": "screen_error", "message": "disabled"})
        self.pairing.authenticate.assert_called_once_with("SYNTHETIC-PIN")
        await self.assert_audio_alive()

    async def test_correlated_offer_echoes_exact_request_id(self):
        self.server.set_screen_allowed(True)
        for request_id in ("00010_x-Y", "x" * 64):
            with self.subTest(request_id=request_id):
                self.main.control({"type": "screen_request", "request_id": request_id})
                offer = await self.main.response()
                self.assertEqual(set(offer), {"type", "ticket", "request_id"})
                self.assertEqual(offer["type"], "screen_offer")
                self.assertEqual(offer["request_id"], request_id)
                self.assertTrue(self.server._screen_tickets.consume(
                    offer["ticket"], self.server._audio_session, "192.168.1.4", permission=0
                ))
        await self.assert_audio_alive()

    async def test_correlated_errors_echo_the_request_id_without_ending_audio(self):
        async def request(expected):
            self.main.control({"type": "screen_request", "request_id": "42"})
            self.assertEqual(await self.main.response(), {
                "type": "screen_error", "message": expected, "request_id": "42"
            })

        await request("disabled")
        self.server.set_screen_allowed(True)
        with patch.object(self.server, "_screen_socket", object()):
            await request("busy")
        with patch.object(self.server, "_audio_session", None):
            await request("unavailable")
        await self.assert_audio_alive()

    async def test_invalid_screen_request_ids_never_issue_tickets_or_end_audio(self):
        self.server.set_screen_allowed(True)
        invalid_ids = (None, True, 42, {}, [], "", "тест", "123/456", "x y", "line\n", "x" * 65)
        with patch.object(self.server._screen_tickets, "issue") as issue:
            for request_id in invalid_ids:
                with self.subTest(kind=type(request_id).__name__):
                    self.main.control({"type": "screen_request", "request_id": request_id})
                    self.assertEqual(await self.main.response(), {
                        "type": "screen_error", "message": "invalid_request"
                    })
            issue.assert_not_called()
        await self.assert_audio_alive()

    async def test_screen_extra_fields_refuse_only_preview_and_preserve_valid_correlation(self):
        self.server.set_screen_allowed(True)
        with patch.object(self.server._screen_tickets, "issue") as issue:
            self.main.control({"type": "screen_request", "request_id": "123", "extra": True})
            self.assertEqual(await self.main.response(), {
                "type": "screen_error", "message": "invalid_request", "request_id": "123"
            })
            self.main.control({"type": "screen_request", "extra": True})
            self.assertEqual(await self.main.response(), {
                "type": "screen_error", "message": "invalid_request"
            })
            issue.assert_not_called()
        await self.assert_audio_alive()

    async def test_consecutive_offers_keep_their_own_request_ids(self):
        self.server.set_screen_allowed(True)
        self.main.control({"type": "screen_request", "request_id": "101"})
        self.main.control({"type": "screen_request", "request_id": "102"})
        first = await self.main.response()
        second = await self.main.response()
        self.assertEqual(first["request_id"], "101")
        self.assertEqual(second["request_id"], "102")
        self.assertNotEqual(first["ticket"], second["ticket"])
        self.assertFalse(self.server._screen_tickets.consume(
            first["ticket"], self.server._audio_session, "192.168.1.4", permission=0
        ))
        self.assertTrue(self.server._screen_tickets.consume(
            second["ticket"], self.server._audio_session, "192.168.1.4", permission=0
        ))
        await self.assert_audio_alive()

    async def test_separate_screen_path_auth_pull_and_independent_audio(self):
        self.server.set_screen_allowed(True)
        preview, _ = await self.preview()
        preview.control({"type": "screen_next"})
        self.assertEqual(await preview.response(), _JPEG)
        self.assertIn("screen_active", self.screen_statuses)
        await self.assert_audio_alive()
        self.assertNotIn("screen_active", self.audio_statuses)

    async def test_streaming_state_requires_current_audio_owner_and_permission(self):
        self.assertFalse(self.server.screen_streaming)
        self.server.set_screen_allowed(True)
        preview, _ = await self.preview()
        self.assertTrue(self.server.screen_streaming)
        owner = self.server._audio_session
        self.server._audio_session = object()
        self.assertFalse(self.server.screen_streaming)
        self.server._audio_session = owner
        self.assertTrue(self.server.screen_streaming)
        self.server.set_screen_allowed(False)
        self.assertFalse(self.server.screen_streaming)
        await asyncio.wait_for(preview.closed.wait(), 1)
        self.assertFalse(self.server.screen_streaming)
        await self.assert_audio_alive()

    async def test_ticket_is_one_use_only_and_only_one_preview_is_active(self):
        self.server.set_screen_allowed(True)
        preview, ticket = await self.preview()
        other = self.start_socket("/screen")
        other.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await other.response(), {"type": "screen_error", "message": "busy"})
        await preview.close()
        await asyncio.wait_for(preview.closed.wait(), 1)
        await asyncio.sleep(0)
        while self.server._screen_socket is not None:
            await asyncio.sleep(0)
        replay = self.start_socket("/screen")
        replay.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await replay.response(), {"type": "screen_error", "message": "unavailable"})

    async def test_ticket_rejects_another_ip_expiry_and_unknown_payload(self):
        self.server.set_screen_allowed(True)
        ticket = await self.ticket()
        other = self.start_socket("/screen", "192.168.1.5")
        other.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await other.response(), {"type": "screen_error", "message": "unavailable"})
        self.server._screen_tickets._expires_at = 0
        expired = self.start_socket("/screen")
        expired.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await expired.response(), {"type": "screen_error", "message": "unavailable"})
        invalid = self.start_socket("/screen")
        invalid.control({"type": "auth", "credential": self.pairing.authenticate.return_value})
        self.assertEqual(await invalid.response(), {"type": "screen_error", "message": "invalid_request"})
        self.pairing.authenticate.assert_called_once()

    async def test_main_disconnect_and_permission_off_revoke_preview(self):
        self.server.set_screen_allowed(True)
        preview, _ = await self.preview()
        self.server.set_screen_allowed(False)
        await asyncio.wait_for(preview.closed.wait(), 1)
        self.assertIn("screen_stopped", self.screen_statuses)
        await self.assert_audio_alive()
        self.server.set_screen_allowed(True)
        preview, _ = await self.preview()
        await self.main.close()
        await asyncio.wait_for(preview.closed.wait(), 1)
        self.assertIsNone(self.server._audio_session)

    async def test_permission_off_then_on_invalidates_ticket_before_async_revoke_runs(self):
        self.server.set_screen_allowed(True)
        ticket = await self.ticket()
        queued = []
        with patch.object(self.server._loop, "call_soon_threadsafe", side_effect=lambda callback: queued.append(callback)):
            self.server.set_screen_allowed(False)
            self.server.set_screen_allowed(True)
        self.assertEqual(len(queued), 1)
        preview = self.start_socket("/screen")
        preview.control({"type": "screen_auth", "ticket": ticket})
        self.assertEqual(await preview.response(), {"type": "screen_error", "message": "unavailable"})
        queued[0]()
        await self.assert_audio_alive()

    async def test_capture_failure_and_invalid_request_close_only_preview(self):
        self.server.set_screen_allowed(True)

        def fail_capture():
            raise RuntimeError("PRIVATE WINDOW CONTENT")

        self.server._screen_source = screen.ScreenFrameSource(fail_capture)
        preview, _ = await self.preview()
        preview.control({"type": "screen_next"})
        self.assertEqual(await preview.response(), {"type": "screen_error", "message": "capture_failed"})
        await self.assert_audio_alive()
        preview, _ = await self.preview()
        preview.control({"type": "screen_next", "remote_control": "forbidden"})
        self.assertEqual(await preview.response(), {"type": "screen_error", "message": "invalid_request"})
        await self.assert_audio_alive()

    async def test_capture_is_independent_and_revoked_native_work_blocks_replacement(self):
        self.server.set_screen_allowed(True)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def capture():
            calls.append(1)
            entered.set()
            release.wait(3)
            return _JPEG

        self.server._screen_source = screen.ScreenFrameSource(capture)
        preview, _ = await self.preview()
        preview.control({"type": "screen_next"})
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await self.assert_audio_alive()
            self.server.set_screen_allowed(False)
            await asyncio.wait_for(preview.closed.wait(), 1)
            self.server.set_screen_allowed(True)
            replacement, _ = await self.preview()
            replacement.control({"type": "screen_next"})
            self.assertEqual(await replacement.response(), {"type": "screen_error", "message": "busy"})
            self.assertEqual(calls, [1])
            await self.assert_audio_alive()
        finally:
            release.set()

    async def test_auth_capture_and_send_deadlines_only_end_screen(self):
        self.server.set_screen_allowed(True)
        with patch("remotevoice.server._SCREEN_AUTH_TIMEOUT", 0.01):
            unauthenticated = self.start_socket("/screen")
            self.assertEqual(await unauthenticated.response(), {"type": "screen_error", "message": "timeout"})
        with patch("remotevoice.server._SCREEN_REQUEST_TIMEOUT", 0.01):
            idle, _ = await self.preview()
            self.assertEqual(await idle.response(), {"type": "screen_error", "message": "timeout"})
        preview, _ = await self.preview()
        preview.block_binary = True
        with patch("remotevoice.server._SCREEN_SEND_TIMEOUT", 0.01):
            preview.control({"type": "screen_next"})
            self.assertEqual(await preview.response(), {"type": "screen_error", "message": "timeout"})
        await self.assert_audio_alive()

    async def test_one_prefetch_overlaps_send_but_never_grows_queue_and_revocation_stops_it(self):
        self.server.set_screen_allowed(True)
        calls = []
        second = threading.Event()

        def capture():
            calls.append(1)
            if len(calls) == 2:
                second.set()
            return _JPEG

        self.server._screen_source = screen.ScreenFrameSource(capture)
        preview, _ = await self.preview()
        self.assertEqual(calls, [])
        preview.block_binary = True
        preview.control({'type': 'screen_next'})
        self.assertTrue(await asyncio.to_thread(second.wait, 1))
        await asyncio.sleep(.15)
        self.assertEqual(len(calls), 2)
        self.server.set_screen_allowed(False)
        await asyncio.wait_for(preview.closed.wait(), 1)
        await asyncio.sleep(.1)
        self.assertEqual(len(calls), 2)
        await self.assert_audio_alive()

    async def test_stop_revokes_live_preview(self):
        self.server.set_screen_allowed(True)
        self.server._stop_event = asyncio.Event()
        preview, _ = await self.preview()
        self.server.stop()
        await asyncio.wait_for(preview.closed.wait(), 1)
        self.assertTrue(self.server._stop_requested.is_set())

    async def test_unknown_path_never_reaches_pairing(self):
        websocket = self.start_socket("/screen?credential=forbidden")
        await asyncio.wait_for(websocket.closed.wait(), 1)
        self.assertEqual(websocket.close_code, 4004)
        self.pairing.authenticate.assert_called_once()
