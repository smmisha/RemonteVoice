import unittest
import asyncio
import ctypes
import os
import subprocess
import sys
from unittest.mock import patch
from PIL import Image
from remotevoice.h264_stream import AccessUnits, Encoder, StreamError, MAX_ACCESS_UNIT, create_encoder, _capture_rgb, encoder_available
from remotevoice.screen import _NATIVE_CAPTURE_LOCK

AUD = b'\x00\x00\x00\x01\x09\xf0'
IDR = AUD + b'\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65first'
P = AUD + b'\x00\x00\x01\x41following'


class FramingTests(unittest.TestCase):
    def test_every_chunk_boundary_preserves_exact_access_units(self):
        data = IDR + P + P
        for cut in range(1, len(data)):
            parser = AccessUnits()
            frames = parser.feed(data[:cut]) + parser.feed(data[cut:]) + parser.finish()
            self.assertEqual(frames, [IDR, P, P])

    def test_single_byte_pipe_reads(self):
        parser = AccessUnits()
        frames = []
        for byte in IDR + P:
            frames.extend(parser.feed(bytes([byte])))
        self.assertEqual(frames + parser.finish(), [IDR, P])

    def test_missing_keyframe_is_rejected(self):
        parser = AccessUnits()
        parser.feed(P)
        with self.assertRaisesRegex(StreamError, 'missing_keyframe'):
            parser.finish()

    def test_no_vcl_or_invalid_prefix_cannot_be_delivered(self):
        for data in (AUD, b'junk' + IDR):
            parser = AccessUnits()
            with self.assertRaises(StreamError):
                parser.feed(data)
                parser.finish()

    def test_unterminated_oversized_frame_is_bounded(self):
        parser = AccessUnits()
        parser.feed(IDR)
        with self.assertRaisesRegex(StreamError, 'frame_too_large'):
            for _ in range(MAX_ACCESS_UNIT // 8192 + 1):
                parser.feed(b'x' * 8192)


class NativePipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_capture_pipe_has_bounded_producer_and_stops_all_workers(self):
        frame = bytes(1280 * 800 * 3)
        command = [sys._base_executable, '-c',
                   'import sys,time;sys.stdin.buffer.read(' + str(len(frame)) + ');'
                   'sys.stdout.buffer.write(' + repr(IDR + P) + ');'
                   'sys.stdout.buffer.flush();time.sleep(20)']
        encoder = Encoder(command, lambda: True, capture=lambda: frame)
        try:
            await asyncio.to_thread(encoder.start)
            self.assertEqual(await asyncio.wait_for(encoder.next_frame(), 8), IDR)
            self.assertTrue(encoder.producer.is_alive())
            self.assertLessEqual(encoder.pending.qsize(), 2)
        finally:
            await asyncio.to_thread(encoder.stop)
        self.assertFalse(encoder.producer.is_alive())
        self.assertFalse(encoder.reader.is_alive())
        self.assertFalse(encoder.errors.is_alive())
        self.assertIsNotNone(encoder.process.poll())

    @unittest.skipUnless(os.name == 'nt', 'Windows encoder job containment')
    async def test_host_process_exit_terminates_its_owned_encoder_job(self):
        # A disposable Python parent stands in for the Host. No desktop data,
        # FFmpeg, audio engine, or installed RemoteVoice process is touched.
        script = (
            'import sys;from remotevoice.h264_stream import Encoder;'
            'e=Encoder([sys._base_executable,"-c","import time;time.sleep(60)"],lambda:True);'
            'e.start();print(e.process.pid,flush=True);sys.stdin.buffer.read(1)'
        )
        parent = subprocess.Popen([sys._base_executable, '-c', script], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = None
        try:
            line = await asyncio.wait_for(asyncio.to_thread(parent.stdout.readline), 10)
            self.assertTrue(line.strip().isdigit(), 'Disposable Host did not start its encoder')
            handle = api.OpenProcess(0x100000, False, int(line))  # Wait only, no termination rights.
            self.assertTrue(handle)
            parent.kill()
            await asyncio.to_thread(parent.wait, 5)
            self.assertEqual(await asyncio.to_thread(api.WaitForSingleObject, handle, 3000), 0,
                             'Encoder survived its Host process')
        finally:
            if parent.poll() is None:
                parent.kill()
                await asyncio.to_thread(parent.wait, 5)
            if handle:
                api.CloseHandle(handle)
            parent.stdin.close(); parent.stdout.close(); parent.stderr.close()

    async def test_small_live_pipe_frame_is_delivered_before_process_eof_and_cleanup_releases_owner(self):
        # Synthetic NALs only. Keep the writer alive with much less than 8192
        # bytes written: read(n) would delay delivery until process termination.
        command = [sys._base_executable, '-c',
                   'import sys,time;sys.stdout.buffer.write(' + repr(IDR + P) +
                   ');sys.stdout.buffer.flush();time.sleep(20)']
        for _ in range(2):
            encoder = Encoder(command, lambda: True)
            try:
                await asyncio.to_thread(encoder.start)
                self.assertEqual(await asyncio.wait_for(encoder.next_frame(), 8), IDR)
                self.assertIsNone(encoder.process.poll())
                self.assertLessEqual(encoder.pending.qsize(), 2)
            finally:
                await asyncio.to_thread(encoder.stop)
            self.assertIsNotNone(encoder.process.poll())
            self.assertFalse(encoder.reader.is_alive())
            self.assertFalse(encoder.errors.is_alive())
            self.assertFalse(encoder.held)

    async def test_revoked_consent_discards_queued_units_and_stops_native_reader(self):
        allowed = True
        command = [sys._base_executable, '-c',
                   'import sys,time;sys.stdout.buffer.write(' + repr(IDR + P * 8) +
                   ');sys.stdout.buffer.flush();time.sleep(20)']
        encoder = Encoder(command, lambda: allowed)
        try:
            await asyncio.to_thread(encoder.start)
            self.assertEqual(await asyncio.wait_for(encoder.next_frame(), 8), IDR)
            allowed = False
            with self.assertRaisesRegex(StreamError, 'unavailable'):
                await encoder.next_frame()
        finally:
            await asyncio.to_thread(encoder.stop)
        self.assertEqual(encoder.pending.qsize(), 0)
        self.assertIsNotNone(encoder.process.poll())


class RawCaptureTests(unittest.TestCase):
    def test_missing_encoder_capability_never_starts_a_process_or_installs_anything(self):
        for executable, expected in [(None, False), ('test-encoder', True)]:
            with patch('remotevoice.h264_stream.shutil.which', return_value=executable), \
                    patch('remotevoice.h264_stream.subprocess.Popen') as spawn:
                self.assertEqual(encoder_available(), expected)
                spawn.assert_not_called()
    def test_encoder_uses_owned_raw_pipe_without_gdigrab_or_capture_at_construction(self):
        with patch('remotevoice.h264_stream.shutil.which', return_value='test-encoder'), \
                patch('remotevoice.windows_screen.capture_primary_image') as capture:
            encoder = create_encoder(lambda: True)
        self.assertIn('rawvideo', encoder.command)
        self.assertIn('pipe:0', encoder.command)
        self.assertNotIn('gdigrab', encoder.command)
        self.assertNotIn('desktop', encoder.command)
        self.assertIsNotNone(encoder.capture)
        capture.assert_not_called()

    def test_synthetic_image_is_letterboxed_without_stretching(self):
        with patch('remotevoice.windows_screen.capture_primary_image',
                   return_value=Image.new('RGB', (1280, 720), (0, 255, 0))) as capture:
            payload = _capture_rgb(lambda: True)
        self.assertEqual(len(payload), 1280 * 800 * 3)
        capture.assert_called_once_with((1280, 800), fast=True)
        with Image.frombytes('RGB', (1280, 800), payload) as image:
            self.assertEqual(image.getpixel((640, 0)), (0, 0, 0))
            self.assertEqual(image.getpixel((640, 40)), (0, 255, 0))
            self.assertEqual(image.getpixel((640, 759)), (0, 255, 0))
            self.assertEqual(image.getpixel((640, 799)), (0, 0, 0))

    def test_denied_or_revoked_capture_never_returns_pixels_and_releases_shared_owner(self):
        with patch('remotevoice.windows_screen.capture_primary_image') as capture:
            with self.assertRaisesRegex(StreamError, 'unavailable'):
                _capture_rgb(lambda: False)
            capture.assert_not_called()
        answers = iter([True, False])
        with patch('remotevoice.windows_screen.capture_primary_image', return_value=Image.new('RGB', (16, 16))):
            with self.assertRaisesRegex(StreamError, 'unavailable'):
                _capture_rgb(lambda: next(answers))
        self.assertTrue(_NATIVE_CAPTURE_LOCK.acquire(blocking=False))
        _NATIVE_CAPTURE_LOCK.release()

    def test_pending_jpeg_native_capture_prevents_parallel_video_grab(self):
        self.assertTrue(_NATIVE_CAPTURE_LOCK.acquire(blocking=False))
        try:
            with patch('remotevoice.windows_screen.capture_primary_image') as capture:
                with self.assertRaisesRegex(StreamError, 'busy'):
                    _capture_rgb(lambda: True)
                capture.assert_not_called()
        finally:
            _NATIVE_CAPTURE_LOCK.release()


if __name__ == '__main__':
    unittest.main()
