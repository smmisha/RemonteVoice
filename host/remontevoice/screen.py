"""Bounded, in-memory screen preview and one-use audio-session tickets."""

from __future__ import annotations

import asyncio
import hmac
import io
import secrets
import threading
import time
from collections.abc import Callable

from PIL import Image

from .windows_screen import capture_primary_image

MAX_SCREEN_BYTES = 128 * 1024
SCREEN_SIZE = (960, 540)
DETAIL_SCREEN_BYTES = 256 * 1024
DETAIL_SCREEN_SIZE = (1920, 1200)
# A full detail frame must fit within one 15fps interval on the LAN. The
# one-request/one-frame protocol still waits for Android to display each frame,
# so slow receivers apply backpressure instead of accumulating stale pictures.
SCREEN_BYTES_PER_SECOND = 4 * 1024 * 1024
SCREEN_FRAME_INTERVAL = 1 / 15
# Avoid dozens of sub-millisecond sleeps per JPEG on Windows: timer rounding
# would turn the old 4KiB fragments into a second, much slower frame limiter.
SCREEN_FRAGMENT_BYTES = 64 * 1024
SCREEN_TICKET_LIFETIME = 15.0
_NATIVE_CAPTURE_LOCK = threading.Lock()


class ScreenError(RuntimeError):
    """A fixed protocol code, never a capture exception or a screen payload."""

    def __init__(self, code: str) -> None:
        if code not in {"disabled", "unavailable", "busy", "capture_failed", "timeout", "invalid_request"}:
            code = "capture_failed"
        super().__init__(code)
        self.code = code


def encode_screen_jpeg(image: Image.Image, *, detail: bool = False) -> bytes:
    """Fit one image to the preview bounds without changing its aspect ratio."""
    if image.width < 1 or image.height < 1:
        raise ScreenError("capture_failed")
    with image.convert("RGB") as preview:
        preview.thumbnail(DETAIL_SCREEN_SIZE if detail else SCREEN_SIZE, Image.Resampling.LANCZOS)
        # Preserve text edges and chroma in detail mode. Very complex scenes
        # reduce resolution before falling to the old unreadable quality floor.
        sizes = (1.0, 0.8, 0.6, 0.45, 0.35) if detail else (1.0,)
        for scale in sizes:
            with preview.resize((max(1, round(preview.width * scale)),
                                 max(1, round(preview.height * scale))), Image.Resampling.LANCZOS) as candidate:
                for quality in ((90, 80, 70) if detail else (65, 50, 35, 20)):
                    with io.BytesIO() as output:
                        options = {"subsampling": 0} if detail else {}
                        candidate.save(output, format="JPEG", quality=quality, **options)
                        if output.tell() <= (DETAIL_SCREEN_BYTES if detail else MAX_SCREEN_BYTES):
                            return output.getvalue()
    raise ScreenError("capture_failed")


def capture_primary_jpeg(*, detail: bool = False, capture_size: tuple[int, int] | None = None) -> bytes:
    """Capture the primary Windows monitor only; never write an image to disk."""
    try:
        with capture_primary_image(capture_size or (DETAIL_SCREEN_SIZE if detail else SCREEN_SIZE)) as image:
            return encode_screen_jpeg(image, detail=detail)
    except ScreenError:
        raise
    except Exception:
        raise ScreenError("capture_failed") from None


class ScreenTicketStore:
    """Loop-owned single ticket, bound to an exact audio session and source IP."""

    def __init__(self) -> None:
        self._ticket: str | None = None
        self._session: object | None = None
        self._source_ip: str | None = None
        self._expires_at = 0.0
        self._permission = 0
        self._detail = False

    def issue(self, session: object, source_ip: str, *, permission: int = 0, detail: bool | str = False) -> str:
        self._ticket = secrets.token_urlsafe(32)
        self._session = session
        self._source_ip = source_ip
        self._expires_at = time.monotonic() + SCREEN_TICKET_LIFETIME
        self._permission = permission
        self._detail = detail
        return self._ticket

    def consume_profile(self, ticket: object, session: object | None, source_ip: str,
                        *, permission: int = 0) -> bool | str | None:
        detail = self._detail
        return detail if self.consume(ticket, session, source_ip, permission=permission) else None

    def consume(self, ticket: object, session: object | None, source_ip: str, *, permission: int = 0) -> bool:
        if self._ticket is None:
            return False
        if time.monotonic() >= self._expires_at:
            self.revoke()
            return False
        if (
            not isinstance(ticket, str)
            or not 32 <= len(ticket) <= 256
            or not ticket.isascii()
            or session is None
            or session is not self._session
            or source_ip != self._source_ip
            or permission != self._permission
            or not hmac.compare_digest(ticket, self._ticket)
        ):
            return False
        self.revoke()
        return True

    def revoke(self) -> None:
        self._ticket = None
        self._session = None
        self._source_ip = None
        self._expires_at = 0.0
        self._permission = 0
        self._detail = False


class ScreenFrameSource:
    """One native capture globally, including when a cancelled caller timed out.

    Cancelling asyncio.to_thread cannot stop an already running native grab. Its
    task is retained until completion so replacement previews cannot queue more
    captures. Revoked callers discard the result; waiting for the frame interval
    is cancellable and never queues a future native grab.
    """

    def __init__(self, capture: Callable[[], bytes] | None = None) -> None:
        self._capture = capture
        self._pending = False
        self._worker: asyncio.Task[bytes] | None = None
        self._next_frame_at = 0.0
        self._detail_size = DETAIL_SCREEN_SIZE

    async def next_frame(self, authorized: Callable[[], bool], *, detail: bool = False) -> bytes:
        if self._pending or self._worker is not None:
            raise ScreenError("busy")
        self._pending = True
        try:
            delay = self._next_frame_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not authorized():
                raise ScreenError("unavailable")
            started = time.monotonic()
            self._next_frame_at = started + SCREEN_FRAME_INTERVAL
            worker = asyncio.create_task(asyncio.to_thread(self._capture_authorized, authorized, detail))
            self._worker = worker
            worker.add_done_callback(self._finished)
            frame = await asyncio.shield(worker)
            if not authorized():
                raise ScreenError("unavailable")
            if detail:
                # Charge the capture interval from its start. Encoding time is
                # already elapsed time, not an extra debt before the next grab.
                # Network fragments enforce the send budget independently.
                self._next_frame_at = max(self._next_frame_at, started + len(frame) / SCREEN_BYTES_PER_SECOND)
            return frame
        finally:
            self._pending = False

    def _capture_authorized(self, authorized: Callable[[], bool], detail: bool = False) -> bytes:
        if not _NATIVE_CAPTURE_LOCK.acquire(blocking=False):
            raise ScreenError("busy")
        try:
            if not authorized():
                raise ScreenError("unavailable")
            if self._capture:
                frame = self._capture()
            elif detail:
                frame = capture_primary_jpeg(detail=True, capture_size=self._detail_size)
                # If a desktop needed a smaller JPEG to fit the byte limit,
                # capture at that useful size next time. Avoid re-encoding the
                # same oversized full-resolution surface at several qualities
                # on every frame. Keep at least the 80% capture surface so a
                # complex scene cannot progressively collapse the resolution.
                with Image.open(io.BytesIO(frame)) as image:
                    self._detail_size = (max(round(DETAIL_SCREEN_SIZE[0] * .8), image.width),
                                         max(round(DETAIL_SCREEN_SIZE[1] * .8), image.height))
            else:
                frame = capture_primary_jpeg()
            if (
                not isinstance(frame, bytes)
                or not 4 <= len(frame) <= (DETAIL_SCREEN_BYTES if detail else MAX_SCREEN_BYTES)
                or not frame.startswith(b"\xff\xd8")
                or not frame.endswith(b"\xff\xd9")
            ):
                raise ScreenError("capture_failed")
            return frame
        except ScreenError:
            raise
        except Exception:
            raise ScreenError("capture_failed") from None
        finally:
            _NATIVE_CAPTURE_LOCK.release()

    def _finished(self, worker: asyncio.Task[bytes]) -> None:
        if self._worker is worker:
            self._worker = None
        if not worker.cancelled():
            worker.exception()  # Consume failures after a cancelled preview discarded the task.


async def paced_screen_fragments(frame: bytes, authorized: Callable[[], bool]):
    """One bounded JPEG message, paced separately from the audio connection."""
    started = time.monotonic()
    for offset in range(0, len(frame), SCREEN_FRAGMENT_BYTES):
        delay = started + offset / SCREEN_BYTES_PER_SECOND - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if not authorized():
            raise ScreenError("unavailable")
        yield frame[offset:offset + SCREEN_FRAGMENT_BYTES]
