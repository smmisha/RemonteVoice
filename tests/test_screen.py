import asyncio
import io
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
from PIL import Image

from remontevoice import screen


def synthetic_frame():
    with Image.new("RGB", (64, 36), "navy") as image:
        return screen.encode_screen_jpeg(image)


class ScreenEncodingTests(unittest.TestCase):
    def test_fit_preserves_aspect_ratio_and_never_upscales(self):
        for source_size, expected_size in (((1920, 1080), (960, 540)), ((1920, 1200), (864, 540)),
                                           ((1080, 1920), (304, 540)), ((120, 80), (120, 80))):
            with self.subTest(source_size=source_size), Image.new("RGBA", source_size, (12, 34, 56, 120)) as source:
                encoded = screen.encode_screen_jpeg(source)
                self.assertEqual(source.size, source_size)
                self.assertLessEqual(len(encoded), 128 * 1024)
                with Image.open(io.BytesIO(encoded)) as decoded:
                    self.assertEqual(decoded.format, "JPEG")
                    self.assertEqual(decoded.mode, "RGB")
                    self.assertEqual(decoded.size, expected_size)

    def test_noisy_frame_stays_inside_byte_bound(self):
        noise = np.random.default_rng(37).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
        with Image.fromarray(noise) as image:
            frame = screen.encode_screen_jpeg(image)
        self.assertLessEqual(len(frame), screen.MAX_SCREEN_BYTES)
        self.assertTrue(frame.startswith(b"\xff\xd8"))

    def test_oversized_encoding_fails_without_returning_frame(self):
        def oversized(image, output, **kwargs):
            output.write(b"x" * (screen.MAX_SCREEN_BYTES + 1))

        with Image.new("RGB", (1, 1)) as image, patch.object(Image.Image, "save", oversized):
            with self.assertRaisesRegex(screen.ScreenError, "capture_failed"):
                screen.encode_screen_jpeg(image)

    def test_primary_grab_is_mocked_and_capture_errors_are_fixed_codes(self):
        with patch("remontevoice.screen.capture_primary_image", return_value=Image.new("RGB", (32, 18))) as grab:
            self.assertTrue(screen.capture_primary_jpeg().startswith(b"\xff\xd8"))
            grab.assert_called_once_with(screen.SCREEN_SIZE)
        with patch("remontevoice.screen.capture_primary_image", side_effect=RuntimeError("PRIVATE WINDOW CONTENT")):
            with self.assertRaises(screen.ScreenError) as raised:
                screen.capture_primary_jpeg()
            self.assertEqual(str(raised.exception), "capture_failed")


    def test_detail_capture_uses_bounded_native_surface_and_never_pillow_grab(self):
        with patch("remontevoice.screen.capture_primary_image", return_value=Image.new("RGB",(1920,1200))) as grab:
            frame=screen.capture_primary_jpeg(detail=True)
            grab.assert_called_once_with(screen.DETAIL_SCREEN_SIZE)
            with Image.open(io.BytesIO(frame)) as image:
                self.assertEqual(image.size,(1920,1200))



class ScreenTicketTests(unittest.TestCase):
    def test_ticket_is_single_use_bound_to_session_ip_and_permission(self):
        store = screen.ScreenTicketStore()
        session = object()
        ticket = store.issue(session, "192.168.1.4", permission=7)
        self.assertGreaterEqual(len(ticket), 32)
        for value, owner, ip, permission in ((ticket, object(), "192.168.1.4", 7),
                                            (ticket, session, "192.168.1.5", 7),
                                            (ticket, session, "192.168.1.4", 8),
                                            ("wrong", session, "192.168.1.4", 7),
                                            (None, session, "192.168.1.4", 7)):
            self.assertFalse(store.consume(value, owner, ip, permission=permission))
        self.assertTrue(store.consume(ticket, session, "192.168.1.4", permission=7))
        self.assertFalse(store.consume(ticket, session, "192.168.1.4", permission=7))

    def test_ticket_expires_at_fifteen_seconds_and_replacement_revokes_previous(self):
        store = screen.ScreenTicketStore()
        session = object()
        with patch("remontevoice.screen.time.monotonic", return_value=10):
            old_ticket = store.issue(session, "10.0.0.1")
            ticket = store.issue(session, "10.0.0.1")
        self.assertNotEqual(old_ticket, ticket)
        with patch("remontevoice.screen.time.monotonic", return_value=24.99):
            self.assertFalse(store.consume(old_ticket, session, "10.0.0.1"))
        with patch("remontevoice.screen.time.monotonic", return_value=25):
            self.assertFalse(store.consume(ticket, session, "10.0.0.1"))
        ticket = store.issue(session, "10.0.0.1")
        store.revoke()
        self.assertFalse(store.consume(ticket, session, "10.0.0.1"))


class ScreenFrameSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_capture_does_not_add_a_second_bandwidth_wait(self):
        clock = [0.0]
        frame = b"\xff\xd8" + b"x" * (200 * 1024) + b"\xff\xd9"

        def capture():
            clock[0] += 1.0  # Capture/encoding has already used the frame budget.
            return frame

        source = screen.ScreenFrameSource(capture)
        with patch.object(screen, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            await source.next_frame(lambda: True, detail=True)
            self.assertLessEqual(source._next_frame_at, clock[0])
            await source.next_frame(lambda: True, detail=True)
            self.assertLessEqual(source._next_frame_at, clock[0])

    async def test_detail_reuses_fitting_surface_without_progressive_resolution_collapse(self):
        for size, expected in (((1536, 960), (1536, 960)), ((384, 240), (1536, 960))):
            with Image.new("RGB", size, "navy") as image:
                frame = screen.encode_screen_jpeg(image, detail=True)
            source = screen.ScreenFrameSource()
            with patch.object(screen, "capture_primary_jpeg", return_value=frame) as capture:
                await source.next_frame(lambda: True, detail=True)
                await source.next_frame(lambda: True, detail=True)
            self.assertEqual(capture.call_args_list[0].kwargs,
                             {'detail': True, 'capture_size': screen.DETAIL_SCREEN_SIZE})
            self.assertEqual(capture.call_args_list[1].kwargs,
                             {'detail': True, 'capture_size': expected})

    async def test_capture_uses_worker_and_paces_fifteen_frames_per_second(self):
        starts = []
        main_thread = threading.get_ident()

        def capture():
            self.assertNotEqual(threading.get_ident(), main_thread)
            starts.append(time.monotonic())
            return synthetic_frame()

        source = screen.ScreenFrameSource(capture)
        await source.next_frame(lambda: True)
        deadline = source._next_frame_at
        await source.next_frame(lambda: True)
        # Worker threads start a little after the pacing deadline, and that
        # dispatch latency is subtracted from the gap between the two capture
        # timestamps. Compare against the source's own deadline instead of
        # requiring two thread timestamps to differ by the whole frame interval.
        self.assertGreaterEqual(starts[1], deadline - .02)
        self.assertLess(starts[1] - starts[0], .4)  # Regression: old fixed 500ms wait.

    async def test_cancelled_native_capture_cannot_queue_replacement_in_any_source(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def capture():
            calls.append(1)
            entered.set()
            release.wait(timeout=3)
            return synthetic_frame()

        source = screen.ScreenFrameSource(capture)
        task = asyncio.create_task(source.next_frame(lambda: True))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaisesRegex(screen.ScreenError, "busy"):
                await source.next_frame(lambda: True)
            other = screen.ScreenFrameSource(lambda: self.fail("A native capture must not be queued"))
            with self.assertRaisesRegex(screen.ScreenError, "busy"):
                await other.next_frame(lambda: True)
            self.assertEqual(calls, [1])
        finally:
            release.set()
            if source._worker is not None:
                await asyncio.wait_for(asyncio.shield(source._worker), 2)

    async def test_revoked_request_never_captures_or_returns_frame(self):
        source = screen.ScreenFrameSource(lambda: self.fail("Revoked capture must not start"))
        with self.assertRaisesRegex(screen.ScreenError, "unavailable"):
            await source.next_frame(lambda: False)
        allowed = threading.Event()
        allowed.set()

        def capture():
            allowed.clear()
            return synthetic_frame()

        source = screen.ScreenFrameSource(capture)
        with self.assertRaisesRegex(screen.ScreenError, "unavailable"):
            await source.next_frame(allowed.is_set)

    async def test_bad_capture_result_and_exception_are_fixed_failures(self):
        for frame in (b"invalid", b"\xff\xd8" + b"x" * screen.MAX_SCREEN_BYTES + b"\xff\xd9", "not bytes"):
            source = screen.ScreenFrameSource(lambda: frame)
            with self.assertRaisesRegex(screen.ScreenError, "capture_failed"):
                await source.next_frame(lambda: True)

        def capture():
            raise RuntimeError("PRIVATE SCREEN CONTENT")

        with self.assertRaises(screen.ScreenError) as raised:
            await screen.ScreenFrameSource(capture).next_frame(lambda: True)
        self.assertEqual(str(raised.exception), "capture_failed")


class DetailScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_keeps_native_laptop_resolution_and_full_chroma(self):
        from PIL import JpegImagePlugin, ImageDraw
        with Image.new("RGB", (1920,1200), "white") as image:
            draw=ImageDraw.Draw(image)
            for y in range(0,1200,24):
                draw.text((20,y), "RemonteVoice source code readable at native resolution 0123456789", fill="navy")
            encoded=screen.encode_screen_jpeg(image, detail=True)
        self.assertLessEqual(len(encoded),256*1024)
        with Image.open(io.BytesIO(encoded)) as decoded:
            self.assertEqual(decoded.size,(1920,1200))
            self.assertEqual(JpegImagePlugin.get_sampling(decoded),0)

    async def test_detail_noise_adapts_resolution_inside_hard_byte_bound(self):
        noise=np.random.default_rng(44).integers(0,256,(1200,1920,3),dtype=np.uint8)
        with Image.fromarray(noise) as image:
            frame=screen.encode_screen_jpeg(image,detail=True)
        self.assertLessEqual(len(frame),256*1024)
        with Image.open(io.BytesIO(frame)) as decoded:
            self.assertLessEqual(decoded.width,1920)
            self.assertLessEqual(decoded.height,1200)

    async def test_ticket_consumption_carries_exact_profile_and_is_still_single_use(self):
        store=screen.ScreenTicketStore();session=object()
        ticket=store.issue(session,"10.0.0.2",detail=True)
        self.assertIsNone(store.consume_profile(ticket,object(),"10.0.0.2"))
        self.assertIs(store.consume_profile(ticket,session,"10.0.0.2"),True)
        self.assertIsNone(store.consume_profile(ticket,session,"10.0.0.2"))
        ticket=store.issue(session,"10.0.0.2")
        self.assertIs(store.consume_profile(ticket,session,"10.0.0.2"),False)

    async def test_detail_fragments_are_paced_bounded_and_revocation_discards_rest(self):
        frame=b"x"*(192*1024);times=[];parts=[];started=time.monotonic()
        async for part in screen.paced_screen_fragments(frame,lambda:True):
            times.append(time.monotonic());parts.append(part)
        self.assertEqual(b"".join(parts),frame)
        self.assertTrue(all(len(p)<=screen.SCREEN_FRAGMENT_BYTES for p in parts))
        self.assertGreaterEqual(times[-1]-started,
                                (len(frame)-screen.SCREEN_FRAGMENT_BYTES)/screen.SCREEN_BYTES_PER_SECOND*.9)
        iterator=screen.paced_screen_fragments(frame,lambda:False)
        with self.assertRaisesRegex(screen.ScreenError,"unavailable"):
            await anext(iterator)


class DetailTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_source_carries_larger_frame_and_paces_next_capture(self):
        frame=b"\xff\xd8"+b"x"*(200*1024)+b"\xff\xd9"
        starts=[]
        def capture():
            starts.append(time.monotonic());return frame
        source=screen.ScreenFrameSource(capture)
        self.assertEqual(await source.next_frame(lambda:True,detail=True),frame)
        self.assertEqual(await source.next_frame(lambda:True,detail=True),frame)
        self.assertGreaterEqual(starts[1]-starts[0],len(frame)/screen.SCREEN_BYTES_PER_SECOND*.95)

    async def test_detail_revocation_after_first_fragment_stops_remaining_payload(self):
        allowed=True
        iterator=screen.paced_screen_fragments(b"x"*(screen.SCREEN_FRAGMENT_BYTES+10000),lambda:allowed)
        self.assertEqual(len(await anext(iterator)),screen.SCREEN_FRAGMENT_BYTES)
        allowed=False
        with self.assertRaisesRegex(screen.ScreenError,"unavailable"):
            await anext(iterator)
