"""Pinned TLS WebSocket server for the Windows host."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .audio import AudioBridge
from .audio_session_diagnostics import log_session_end, log_session_error, session_id_for
from .certificates import load_or_create_identity
from .pairing import PairingAttemptLimiter, PairingManager
from .protocol import MIC_FRAME, ProtocolError, decode_audio_frame, decode_control
from .screen import ScreenError, ScreenFrameSource, ScreenTicketStore, paced_screen_fragments
from .h264_stream import create_encoder, encoder_available, StreamError, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS

LOGGER = logging.getLogger("remontevoice.server")
_SCREEN_AUTH_TIMEOUT = 8.0
_SCREEN_REQUEST_TIMEOUT = 15.0
_SCREEN_SEND_TIMEOUT = 2.0
_AUDIO_STOP_TIMEOUT = 3.0
_AUDIO_START_TIMEOUT = 3.0
_SESSION_START_TIMEOUT = 5.0
_AUTH_MESSAGE_MAX_LENGTH = 1024
_AUDIO_READY_TIMEOUT = 8.0
_AUDIO_READY_MESSAGE_MAX_LENGTH = 128


class RemonteVoiceServer:
    def __init__(
        self,
        bind_host: str,
        port: int,
        capture_device_id: str,
        playback_device_id: str,
        identity_dir: Path,
        pairing: PairingManager,
        on_status: Callable[[str], None],
        on_screen_status: Callable[[str], None] | None = None,
        on_session_start: Callable[[], bool] | None = None,
        on_audio_status: Callable[[str, object], None] | None = None,
        audio_factory: Callable[..., AudioBridge] | None = None,
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.capture_device_id = capture_device_id
        self.playback_device_id = playback_device_id
        self.identity_dir = identity_dir
        self.pairing = pairing
        self.on_status = on_status
        self.on_screen_status = on_screen_status
        self.on_session_start = on_session_start
        self.on_audio_status = on_audio_status
        self._audio_factory = audio_factory
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested = threading.Event()
        self._client_lock: asyncio.Lock | None = None
        self._client_active = False
        self._cleanup_failed = threading.Event()
        self._unclean_audio: AudioBridge | None = None
        self._audio_session: object | None = None
        self._audio_socket: ServerConnection | None = None
        self._audio_ip: str | None = None
        self._screen_allowed = threading.Event()
        self._screen_permission_lock = threading.Lock()
        self._screen_permission = 0
        self._screen_tickets = ScreenTicketStore()
        self._screen_source = ScreenFrameSource()
        self._screen_detail = False
        # Older peers keep JPEG. A missing installed encoder also keeps JPEG;
        # capture starts only after an explicit, permission-bound screen ticket.
        self._screen_video = os.environ.get('REMONTEVOICE_VIDEO_EXPERIMENTAL') != '0' and encoder_available()
        self._screen_video_peer = False
        self._video_cleanup_failed = False
        self._screen_socket: ServerConnection | None = None
        self._screen_handler: asyncio.Task[Any] | None = None
        self._screen_session: object | None = None
        self._attempt_limiter = PairingAttemptLimiter()
        self.fingerprint: str | None = None

    @property
    def cleanup_failed(self) -> bool:
        """A previous audio engine may still own devices; restarting is unsafe."""
        return self._cleanup_failed.is_set()

    def is_current_audio_session(self, session: object) -> bool:
        return session is not None and self._audio_session is session

    @property
    def screen_streaming(self) -> bool:
        """Current preview ownership, including synchronous permission revocation."""
        return (
            self._screen_permission_snapshot()[0]
            and not self._stop_requested.is_set()
            and self._screen_socket is not None
            and self._screen_session is not None
            and self._screen_session is self._audio_session
        )

    def _report_audio_status(self, session: object, status: str) -> None:
        if not self.is_current_audio_session(session):
            return
        if self.on_audio_status is not None:
            self.on_audio_status(status, session)
        else:
            self.on_status(status)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        self._client_lock = asyncio.Lock()
        tls_context, self.fingerprint = load_or_create_identity(self.identity_dir)
        self.on_status("server_starting")
        try:
            async with serve(
                self._handle_client,
                self.bind_host,
                self.port,
                ssl=tls_context,
                max_size=16 * 1024,
                max_queue=8,
                write_limit=16 * 1024,
                ping_interval=1,
                ping_timeout=3,
                close_timeout=2,
                compression=None,
                server_header="RemonteVoice",
            ):
                self.on_status("server_ready")
                await self._stop_event.wait()
        except OSError as exc:
            self.on_status(f"server_error: {exc}")
            raise
        finally:
            self._revoke_screen()
            self.on_status("server_stopped")

    def stop(self) -> None:
        self._stop_requested.set()
        if self._stop_event is not None and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)
            self._loop.call_soon_threadsafe(self._revoke_screen)

    def set_screen_allowed(self, allowed: bool) -> None:
        """Host permission, safe to update from the UI thread; disabled by default."""
        if type(allowed) is not bool:
            raise ValueError("Screen permission must be bool")
        with self._screen_permission_lock:
            if allowed:
                self._screen_allowed.set()
            else:
                self._screen_allowed.clear()
                self._screen_permission += 1
        if not allowed:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._revoke_screen)

    def _screen_permission_snapshot(self) -> tuple[bool, int]:
        with self._screen_permission_lock:
            return self._screen_allowed.is_set(), self._screen_permission

    async def _handle_client(self, websocket: ServerConnection) -> None:
        client_ip = self._client_ip(websocket)
        if not client_ip or not ipaddress.ip_address(client_ip).is_private:
            await websocket.close(code=4003, reason="Local network only")
            return
        path = websocket.request.path
        if path == "/screen":
            await self._handle_screen(websocket, client_ip)
            return
        if path != "/":
            await websocket.close(code=4004, reason="Unknown channel")
            return
        if not self._attempt_limiter.allowed(client_ip):
            await websocket.close(code=4005, reason="Pairing temporarily rate limited")
            return

        assert self._client_lock is not None
        if self._stop_requested.is_set():
            await websocket.close(code=1012, reason="Host stopping")
            return
        if self._client_lock.locked():
            await websocket.close(code=4002, reason="Another phone is connected")
            return

        # No await between checking and taking this loop-owned lock: a second
        # connection is rejected immediately, even while the first authenticates
        # or its native audio engine is still stopping. Credentials are untouched
        # until this connection owns the slot.
        async with self._client_lock:
            try:
                first = await asyncio.wait_for(websocket.recv(), timeout=8)
                if self._stop_requested.is_set():
                    await websocket.close(code=1012, reason="Host stopping")
                    return
                if not isinstance(first, str):
                    raise ProtocolError("Authentication must be a text message")
                if len(first) > _AUTH_MESSAGE_MAX_LENGTH:
                    raise ProtocolError("Authentication message is too large")
                auth = decode_control(first)
                if auth.get("type") != "auth":
                    raise ProtocolError("Authentication must be the first message")
                if "audio_ready" in auth and auth["audio_ready"] is not True:
                    raise ProtocolError("Audio readiness negotiation must be true")
                if "system_deflate" in auth and auth["system_deflate"] is not True:
                    raise ProtocolError("System audio compression negotiation must be true")
                credential = auth.get("credential")
                if not self.pairing.is_valid_credential(credential):
                    raise ProtocolError("Invalid credential")
            except (asyncio.TimeoutError, ValueError, RecursionError, ConnectionClosed):
                self._attempt_limiter.failed(client_ip)
                try:
                    await websocket.send(json.dumps({"type": "auth_error"}))
                except ConnectionClosed:
                    pass
                await websocket.close(code=4001, reason="Invalid authentication")
                return
            self._client_active = True
            self._audio_session = object()
            session = self._audio_session
            self._audio_socket = websocket
            self._audio_ip = client_ip
            self._screen_detail = auth.get("screen_detail") is True
            self._screen_video_peer = self._screen_video and self._screen_detail and auth.get("screen_video") is True
            loop = asyncio.get_running_loop()
            send_lock = asyncio.Lock()
            audio: AudioBridge | None = None
            progress_task: asyncio.Task[Any] | None = None
            diagnostic_id = uuid.uuid4().hex
            diagnostic_stage = "route_prepare"
            diagnostic_failure = "none"
            try:
                if self.on_session_start is not None:
                    try:
                        prepared = await asyncio.wait_for(
                            asyncio.to_thread(self.on_session_start),
                            timeout=_SESSION_START_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        diagnostic_failure = "route_prepare_timeout"
                        # A thread cannot be cancelled by wait_for. The Host
                        # callback must also discard its late UI work, and this
                        # server cannot admit another session in the meantime.
                        self.stop()
                        raise RuntimeError("Не удалось вовремя подготовить аудиоустройства") from None
                    if prepared is not True:
                        raise RuntimeError("Не удалось подготовить аудиоустройства")
                if self._stop_requested.is_set():
                    await websocket.close(code=1012, reason="Host stopping")
                    return
                # A PIN may have been replaced from the UI while route
                # preparation ran. Commit only after readiness, and revalidate
                # under the pairing lock rather than trusting the preflight.
                token = self.pairing.authenticate(credential)
                if token is None:
                    self._attempt_limiter.failed(client_ip)
                    await websocket.send(json.dumps({"type": "auth_error"}))
                    await websocket.close(code=4001, reason="Pairing required")
                    return
                self._attempt_limiter.succeeded(client_ip)
                diagnostic_stage = "audio_setup"
                audio = (self._audio_factory or AudioBridge)(
                    self.capture_device_id,
                    self.playback_device_id,
                    loop,
                    websocket,
                    send_lock,
                    lambda status: self._report_audio_status(session, status),
                )
                diagnostic_id = session_id_for(audio, diagnostic_id)
                configure_compression = getattr(audio, "enable_system_compression", None)
                system_deflate = (auth.get("system_deflate") is True
                    and os.environ.get("REMONTEVOICE_AUDIO_COMPRESSION_EXPERIMENTAL") == "1"
                    and callable(configure_compression))
                if system_deflate:
                    configure_compression()
                diagnostic_stage = "auth_reply"
                await websocket.send(
                    json.dumps(
                        {
                            "type": "auth_ok",
                            "credential": token,
                            "microphone_enabled": False,
                            **({"system_deflate": True} if system_deflate else {}),
                            **({"screen_detail": True} if self._screen_detail else {}),
                            **({"screen_video": True} if self._screen_video_peer else {}),
                            **({"audio_ready": True} if auth.get("audio_ready") is True else {}),
                        }
                    )
                )
                if auth.get("audio_ready") is True:
                    diagnostic_stage = "audio_ready"
                    try:
                        ready = await asyncio.wait_for(websocket.recv(), timeout=_AUDIO_READY_TIMEOUT)
                        if (
                            not isinstance(ready, str)
                            or len(ready) > _AUDIO_READY_MESSAGE_MAX_LENGTH
                            or decode_control(ready) != {"type": "audio_ready"}
                        ):
                            raise ProtocolError("Expected audio readiness message")
                    except asyncio.TimeoutError:
                        await websocket.close(code=4004, reason="Audio readiness timeout")
                        return
                    except (ValueError, RecursionError):
                        await websocket.close(code=4004, reason="Invalid audio readiness message")
                        return
                if self._stop_requested.is_set():
                    await websocket.close(code=1012, reason="Host stopping")
                    return
                diagnostic_stage = "audio_start"
                try:
                    await asyncio.wait_for(asyncio.to_thread(audio.start), timeout=_AUDIO_START_TIMEOUT)
                except asyncio.TimeoutError:
                    diagnostic_failure = "audio_start_timeout"
                    self.stop()
                    raise RuntimeError("Аудиодвижок не запустился вовремя") from None
                diagnostic_stage = "stream"
                progress_task = asyncio.create_task(self._watch_audio_progress(audio, websocket, diagnostic_id))
                self._report_audio_status(session, "iphone_connected_muted")
                async for message in websocket:
                    await self._handle_message(message, websocket, audio, send_lock)
            except asyncio.CancelledError:
                diagnostic_failure = "cancelled"
                self.stop()
                raise
            except ConnectionClosed:
                pass
            except Exception as exc:
                if diagnostic_failure == "none":
                    diagnostic_failure = "session_error"
                log_session_error(LOGGER, session_id=diagnostic_id, stage=diagnostic_stage, error=exc)
                self.on_status(f"client_error: {exc}")
                try:
                    await websocket.close(code=1011, reason="Host audio error")
                except Exception:
                    pass
            finally:
                if progress_task is not None:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                self._audio_session = None
                self._audio_socket = None
                self._audio_ip = None
                self._client_active = False
                try:
                    self._revoke_screen()
                finally:
                    try:
                        # Restore the laptop devices before native audio cleanup can
                        # block, fail or be cancelled. The client lock still keeps
                        # a replacement session out until cleanup has completed.
                        self.on_status("server_ready")
                    finally:
                        log_session_end(LOGGER, websocket, session_id=diagnostic_id,
                                        stage=diagnostic_stage, failure=diagnostic_failure)
                        if audio is not None:
                            try:
                                await asyncio.wait_for(
                                    asyncio.to_thread(audio.stop), timeout=_AUDIO_STOP_TIMEOUT
                                )
                            except asyncio.CancelledError:
                                self._unclean_audio = audio
                                self._cleanup_failed.set()
                                self.stop()
                                self.on_status("audio_cleanup_failed: Не удалось подтвердить остановку аудиопотока; хост остановлен")
                                raise
                            except Exception as exc:
                                log_session_error(LOGGER, session_id=diagnostic_id, stage="audio_cleanup", error=exc)
                                # A timed out worker may still be running; don't
                                # accept a new session against the same devices.
                                self._unclean_audio = audio
                                self._cleanup_failed.set()
                                self.stop()
                                self.on_status("audio_cleanup_failed: Не удалось завершить аудиопоток; хост остановлен")

    async def _watch_audio_progress(self, audio: AudioBridge, websocket: ServerConnection, diagnostic_id=None) -> None:
        checker = getattr(audio, "assert_progress", None)
        if checker is None:
            return  # Allows protocol-only test adapters without native workers.
        while True:
            await asyncio.sleep(0.25)
            try:
                checker()
            except Exception as exc:
                log_session_error(LOGGER, session_id=diagnostic_id, stage="audio_progress", error=exc)
                try:
                    self.on_status("audio_error: Аудиоустройство перестало передавать звук")
                finally:
                    # A failed UI observer cannot leave a stalled session open.
                    await websocket.close(code=1011, reason="Audio progress stopped")
                return

    async def _handle_message(
        self,
        message: str | bytes,
        websocket: ServerConnection,
        audio: AudioBridge,
        send_lock: asyncio.Lock,
    ) -> None:
        if isinstance(message, bytes):
            try:
                frame_type, payload = decode_audio_frame(message)
            except ProtocolError:
                await websocket.close(code=4004, reason="Malformed audio frame")
                return
            if frame_type == MIC_FRAME and audio.microphone_enabled:
                audio.submit_phone_audio(payload)
            return

        try:
            control = decode_control(message)
        except ProtocolError:
            await websocket.close(code=4004, reason="Malformed control message")
            return

        if control.get("type") == "mic_enabled" and isinstance(
            control.get("enabled"), bool
        ):
            enabled = control["enabled"]
            # Own-driver readiness may await the broker/BEGIN acknowledgement;
            # never block the WebSocket event loop or ACK a request prematurely.
            try:
                await asyncio.wait_for(asyncio.to_thread(audio.set_microphone_enabled, enabled), timeout=1.5)
            except asyncio.TimeoutError:
                self.stop()  # A timed-out thread can still complete late.
                raise RuntimeError("Микрофон не подтвердил готовность вовремя") from None
            self.on_status("iphone_microphone_on" if enabled else "iphone_connected_muted")
            async with send_lock:
                await websocket.send(
                    json.dumps({"type": "mic_state", "enabled": enabled})
                )
        elif control.get("type") == "ping":
            async with send_lock:
                await websocket.send(json.dumps({"type": "pong"}))
        elif control.get("type") == "screen_request":
            await self._offer_screen(websocket, send_lock, control)
        elif control.get("type") == "audio_ready":
            await websocket.close(code=4004, reason="Unexpected audio readiness message")

    async def _offer_screen(
        self,
        websocket: ServerConnection,
        send_lock: asyncio.Lock,
        request: dict[str, Any],
    ) -> None:
        request_id = request.get("request_id")
        valid_request_id = (
            isinstance(request_id, str)
            and 0 < len(request_id) <= 64
            and request_id.isascii()
            and all(character.isalnum() or character in "-_" for character in request_id)
        )
        # Requests from older clients retain their original response shape.
        # An invalid ID is never reflected; a valid ID still correlates an
        # invalid extra-field request with its preview-only refusal.
        fields = set(request) - {"profile"}
        valid_request = (fields == {"type"} or (
            fields == {"type", "request_id"} and valid_request_id
        )) and (
            "profile" not in request or (self._screen_detail and (
                request["profile"] == "detail" or (self._screen_video and self._screen_video_peer
                                                    and request["profile"] == "video")))
        )
        allowed, permission = self._screen_permission_snapshot()
        if not valid_request:
            response = {"type": "screen_error", "message": "invalid_request"}
        elif not allowed:
            response = {"type": "screen_error", "message": "disabled"}
        elif websocket is not self._audio_socket or self._audio_session is None or self._audio_ip is None:
            response = {"type": "screen_error", "message": "unavailable"}
        elif self._screen_socket is not None or self._video_cleanup_failed:
            response = {"type": "screen_error", "message": "busy"}
        else:
            ticket = self._screen_tickets.issue(self._audio_session, self._audio_ip, permission=permission,
                                                detail=("video" if request.get("profile") == "video"
                                                        else request.get("profile") == "detail"))
            response = {"type": "screen_offer", "ticket": ticket}
        if valid_request_id:
            response["request_id"] = request_id
        try:
            async with send_lock:
                await asyncio.wait_for(websocket.send(json.dumps(response)), timeout=_SCREEN_SEND_TIMEOUT)
        except Exception:
            self._screen_tickets.revoke()
            self._notify_screen_status("screen_error")

    def _revoke_screen(self) -> None:
        """Loop-owned revocation; cancelling preview never cancels the audio task."""
        self._screen_tickets.revoke()
        self._screen_session = None
        handler = self._screen_handler
        self._screen_handler = None
        if handler is not None and not handler.done():
            handler.cancel()

    def _notify_screen_status(self, status: str) -> None:
        if self.on_screen_status is not None:
            try:
                self.on_screen_status(status)
            except Exception:
                LOGGER.warning("Screen status observer failed")

    async def _screen_reply(self, websocket: ServerConnection, message: dict[str, str]) -> None:
        await asyncio.wait_for(websocket.send(json.dumps(message)), timeout=_SCREEN_SEND_TIMEOUT)

    async def _close_screen(self, websocket: ServerConnection, code: str | None) -> None:
        if code is not None:
            try:
                await self._screen_reply(websocket, {"type": "screen_error", "message": code})
            except (ConnectionClosed, asyncio.TimeoutError):
                pass
        try:
            await asyncio.wait_for(websocket.close(code=1000, reason="Screen preview ended"), timeout=_SCREEN_SEND_TIMEOUT)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass

    async def _handle_screen(self, websocket: ServerConnection, client_ip: str) -> None:
        session: object | None = None
        active = False
        error: str | None = None
        next_frame: asyncio.Task[bytes] | None = None
        try:
            if not self._screen_permission_snapshot()[0]:
                raise ScreenError("disabled")
            first = await asyncio.wait_for(websocket.recv(), timeout=_SCREEN_AUTH_TIMEOUT)
            if not isinstance(first, str):
                raise ScreenError("invalid_request")
            auth = decode_control(first)
            if set(auth) != {"type", "ticket"} or auth["type"] != "screen_auth":
                raise ScreenError("invalid_request")
            allowed, permission = self._screen_permission_snapshot()
            if not allowed:
                raise ScreenError("disabled")
            if self._screen_socket is not None or self._video_cleanup_failed:
                raise ScreenError("busy")
            session = self._audio_session
            detail = (self._screen_tickets.consume_profile(auth["ticket"], session, client_ip,
                                                          permission=permission)
                      if self._client_active else None)
            if detail is None:
                raise ScreenError("unavailable")
            self._screen_socket = websocket
            self._screen_session = session
            self._screen_handler = asyncio.current_task()

            def authorized() -> bool:
                allowed_now, current_permission = self._screen_permission_snapshot()
                return (
                    allowed_now
                    and current_permission == permission
                    and not self._stop_requested.is_set()
                    and session is not None
                    and self._audio_session is session
                    and self._screen_session is session
                    and self._screen_socket is websocket
                )

            if detail == "video":
                active = True
                await self._stream_video(websocket, authorized)
                return
            await self._screen_reply(websocket, {"type": "screen_ok"})
            active = True
            self._notify_screen_status("screen_active")
            diagnostics = os.environ.get("REMONTEVOICE_SCREEN_DIAGNOSTICS") == "1"
            measured_at = time.monotonic()
            measured_frames = measured_bytes = 0
            while authorized():
                request = await asyncio.wait_for(websocket.recv(), timeout=_SCREEN_REQUEST_TIMEOUT)
                if not isinstance(request, str) or decode_control(request) != {"type": "screen_next"}:
                    raise ScreenError("invalid_request")
                if next_frame is None:
                    next_frame = asyncio.create_task(self._screen_source.next_frame(authorized, detail=detail))
                frame = await asyncio.wait_for(next_frame, timeout=_SCREEN_REQUEST_TIMEOUT)
                next_frame = None
                if not authorized():
                    raise ScreenError("unavailable")
                # Prepare at most one following frame while this one is being
                # sent and decoded. A slow receiver cannot grow the queue.
                next_frame = asyncio.create_task(self._screen_source.next_frame(authorized, detail=detail))
                payload = paced_screen_fragments(frame, authorized) if detail else frame
                await asyncio.wait_for(websocket.send(payload), timeout=_SCREEN_SEND_TIMEOUT)
                if diagnostics:
                    measured_frames += 1
                    measured_bytes += len(frame)
                    elapsed = time.monotonic() - measured_at
                    if elapsed >= 3:
                        # Only scalar counters; never log pixels or session tickets.
                        LOGGER.info("screen_diagnostics frames=%d interval_s=%.3f fps=%.2f bytes=%d",
                                    measured_frames, elapsed, measured_frames / elapsed, measured_bytes)
                        measured_at = time.monotonic()
                        measured_frames = measured_bytes = 0
        except ScreenError as exc:
            error = exc.code
        except ProtocolError:
            error = "invalid_request"
        except asyncio.TimeoutError:
            error = "timeout"
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass  # Revocation is an ordinary preview-only shutdown.
        except Exception:
            error = "capture_failed"
        finally:
            if next_frame is not None:
                next_frame.cancel()
                await asyncio.gather(next_frame, return_exceptions=True)
            if self._screen_socket is websocket:
                self._screen_socket = None
                self._screen_session = None
                self._screen_handler = None
            if error is not None:
                self._notify_screen_status("screen_error")
            await self._close_screen(websocket, error)
            if active:
                self._notify_screen_status("screen_stopped")

    async def _stream_video(self, websocket, authorized) -> None:
        encoder = None
        try:
            encoder = await asyncio.to_thread(create_encoder, authorized)
            await asyncio.to_thread(encoder.start)
            await self._screen_reply(websocket, {'type': 'screen_ok', 'codec': 'h264',
                                                 'width': VIDEO_WIDTH, 'height': VIDEO_HEIGHT, 'fps': VIDEO_FPS})
            self._notify_screen_status('screen_active')
            while authorized():
                request = await asyncio.wait_for(websocket.recv(), timeout=_SCREEN_REQUEST_TIMEOUT)
                if not isinstance(request, str) or decode_control(request) != {'type': 'screen_next'}:
                    raise ScreenError('invalid_request')
                frame = await encoder.next_frame()
                if not authorized():
                    raise ScreenError('unavailable')
                await asyncio.wait_for(websocket.send(frame), timeout=_SCREEN_SEND_TIMEOUT)
        except StreamError as error:
            code = str(error)
            raise ScreenError(code if code in {'unavailable', 'busy'} else 'capture_failed') from None
        finally:
            if encoder is not None:
                try:
                    await asyncio.to_thread(encoder.stop)
                except Exception:
                    # Retain admission protection when native cleanup is unknown.
                    self._video_cleanup_failed = True
                    LOGGER.warning('Video encoder cleanup was not confirmed')

    @staticmethod
    def _client_ip(websocket: ServerConnection) -> str | None:
        address: Any = websocket.remote_address
        if isinstance(address, tuple) and address:
            return str(address[0])
        return None
