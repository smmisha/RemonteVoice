"""Synthetic Win32 tests: no test loads a DLL or captures the desktop."""

import ctypes
import importlib.util
import types
import unittest
from unittest.mock import Mock, call, patch

from remotevoice import windows_screen


class FakeWin32:
    SCREEN_DC = 101
    MEMORY_DC = 102
    BITMAP = 103
    OLD_BITMAP = 104
    OLD_DPI = ctypes.c_void_p(-1).value

    def __init__(self, size=(4, 2), *, failures=None, pixels=None, bits=None):
        self.size = size
        self.failures = failures or {}
        self.pixels = pixels
        self.bits = bits
        self.events = []
        self.counts = {}
        self.buffer = None
        self.header = None
        self.user32 = types.SimpleNamespace(
            GetDC=self.function("GetDC", lambda *_: self.SCREEN_DC),
            ReleaseDC=self.function("ReleaseDC", lambda *_: 1),
            GetSystemMetrics=self.function("GetSystemMetrics", lambda index: self.size[index]),
            SetThreadDpiAwarenessContext=self.function("SetThreadDpiAwarenessContext", lambda *_: self.OLD_DPI),
        )
        self.gdi32 = types.SimpleNamespace(
            CreateCompatibleDC=self.function("CreateCompatibleDC", lambda *_: self.MEMORY_DC),
            CreateDIBSection=self.function("CreateDIBSection", self.create_bitmap),
            SelectObject=self.function("SelectObject", lambda *_: self.OLD_BITMAP),
            BitBlt=self.function("BitBlt", lambda *_: 1),
            StretchBlt=self.function("StretchBlt", lambda *_: 1),
            SetStretchBltMode=self.function("SetStretchBltMode", lambda *_: 1),
            SetBrushOrgEx=self.function("SetBrushOrgEx", lambda *_: 1),
            GdiFlush=self.function("GdiFlush", lambda: 1),
            DeleteObject=self.function("DeleteObject", lambda *_: 1),
            DeleteDC=self.function("DeleteDC", lambda *_: 1),
        )

    def function(self, name, implementation):
        def invoke(*args):
            self.counts[name] = self.counts.get(name, 0) + 1
            key = name
            if name in {"SetThreadDpiAwarenessContext", "SelectObject"}:
                key += ":" + str(self.counts[name])
            elif name == "GetSystemMetrics":
                key += ":" + str(args[0])
            self.events.append(key)
            if key in self.failures:
                result = self.failures[key]
                if isinstance(result, Exception):
                    raise result
                return result
            return implementation(*args)

        return Mock(name=name, side_effect=invoke)

    def create_bitmap(self, screen_dc, info, usage, bits, section, offset):
        self.header = windows_screen._BitmapInfoHeader.from_buffer_copy(
            ctypes.cast(info, ctypes.POINTER(windows_screen._BitmapInfo)).contents.bmiHeader
        )
        byte_count = self.header.biWidth * -self.header.biHeight * 4
        if not 0 < byte_count <= windows_screen._MAX_BITMAP_BYTES:
            raise AssertionError("DIB allocation exceeded the test hard bound")
        self.buffer = ctypes.create_string_buffer(byte_count)
        if self.pixels is not None:
            if len(self.pixels) != byte_count:
                raise AssertionError("Synthetic pixel count does not match the DIB")
            ctypes.memmove(self.buffer, self.pixels, byte_count)
        address = ctypes.addressof(self.buffer) if self.bits is None else self.bits
        ctypes.cast(bits, ctypes.POINTER(ctypes.c_void_p))[0] = address
        return self.BITMAP


class WindowsCaptureTests(unittest.TestCase):
    def capture(self, fake, max_size=(4, 2), **options):
        with patch.object(windows_screen, "_load_apis", return_value=(fake.user32, fake.gdi32)):
            return windows_screen.capture_primary_image(max_size, **options)

    def test_video_fast_scale_uses_srccopy_and_skips_halftone_brush(self):
        fake = FakeWin32((8, 4))
        with self.capture(fake, (4, 2), fast=True) as image:
            self.assertEqual(image.size, (4, 2))
        fake.gdi32.SetStretchBltMode.assert_called_once_with(fake.MEMORY_DC, 3)
        fake.gdi32.SetBrushOrgEx.assert_not_called()
        self.assertEqual(fake.gdi32.StretchBlt.call_args.args[-1], 0x00CC0020)
        self.assert_cleanup(fake, ["SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def test_fast_scale_failure_releases_native_handles(self):
        for stage in ('SetStretchBltMode', 'StretchBlt'):
            fake = FakeWin32((8, 4), failures={stage: 0})
            with self.assertRaisesRegex(RuntimeError, 'capture_failed'):
                self.capture(fake, (4, 2), fast=True)
            self.assert_cleanup(fake, ["SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def assert_failed(self, fake, max_size=(4, 2)):
        with self.assertRaises(RuntimeError) as raised:
            self.capture(fake, max_size)
        self.assertEqual(str(raised.exception), "capture_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def assert_cleanup(self, fake, expected):
        names = {"SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"}
        self.assertEqual([event for event in fake.events if event in names], expected)

    def test_fit_preserves_aspect_bounds_and_never_upscales(self):
        cases = (
            ((1920, 1080), (960, 540), (960, 540)),
            ((1920, 1200), (960, 540), (864, 540)),
            ((1080, 1920), (960, 540), (304, 540)),
            ((120, 80), (960, 540), (120, 80)),
            ((7680, 4320), (1920, 1200), (1920, 1080)),
            ((1, 2**31 - 1), (960, 540), (1, 540)),
            ((2**31 - 1, 1), (960, 540), (960, 1)),
        )
        for source, maximum, expected in cases:
            with self.subTest(source=source, maximum=maximum):
                fake = FakeWin32(source)
                with self.capture(fake, maximum) as image:
                    self.assertEqual(image.size, expected)
                    self.assertLessEqual(image.width, min(source[0], maximum[0]))
                    self.assertLessEqual(image.height, min(source[1], maximum[1]))
                    self.assertLessEqual(fake.header.biSizeImage, windows_screen._MAX_BITMAP_BYTES)

    def test_primary_only_coordinates_and_temporary_thread_dpi(self):
        fake = FakeWin32((8, 4))
        with self.capture(fake, (4, 2)):
            pass
        self.assertEqual(fake.user32.GetSystemMetrics.call_args_list, [call(0), call(1)])
        fake.user32.GetDC.assert_called_once_with(None)
        self.assertEqual(fake.user32.SetThreadDpiAwarenessContext.call_args_list,
                         [call(windows_screen._PER_MONITOR_AWARE), call(fake.OLD_DPI)])
        self.assertEqual(fake.events[0], "SetThreadDpiAwarenessContext:1")
        self.assertEqual(fake.events[-1], "SetThreadDpiAwarenessContext:2")
        fake.gdi32.SetStretchBltMode.assert_called_once_with(fake.MEMORY_DC, 4)
        fake.gdi32.SetBrushOrgEx.assert_called_once_with(fake.MEMORY_DC, 0, 0, None)
        fake.gdi32.StretchBlt.assert_called_once_with(fake.MEMORY_DC, 0, 0, 4, 2,
                                                    fake.SCREEN_DC, 0, 0, 8, 4, 0x00CC0020)
        fake.gdi32.BitBlt.assert_not_called()

    def test_native_size_uses_bitblt_and_top_down_bgrx_decodes_rgb(self):
        pixels = bytes((0, 0, 255, 5, 0, 255, 0, 99, 255, 0, 0, 0, 30, 20, 10, 255))
        fake = FakeWin32((2, 2), pixels=pixels)
        read_pixels = ctypes.string_at

        def after_flush(*args):
            self.assertEqual(fake.events[-1], "GdiFlush")
            fake.events.append("read_pixels")
            return read_pixels(*args)

        with patch.object(windows_screen.ctypes, "string_at", side_effect=after_flush):
            image = self.capture(fake, (4, 2))
        with image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual([image.getpixel((x, y)) for y in range(2) for x in range(2)],
                             [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)])
            # The returned image owns its pixels after native cleanup.
            ctypes.memset(fake.buffer, 0, len(fake.buffer))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        header = fake.header
        self.assertEqual(ctypes.sizeof(windows_screen._BitmapInfoHeader), 40)
        self.assertEqual((header.biSize, header.biWidth, header.biHeight), (40, 2, -2))
        self.assertEqual((header.biPlanes, header.biBitCount, header.biCompression, header.biSizeImage), (1, 32, 0, 16))
        args = fake.gdi32.CreateDIBSection.call_args.args
        self.assertEqual((args[0], args[2], args[4], args[5]), (fake.SCREEN_DC, 0, None, 0))
        fake.gdi32.BitBlt.assert_called_once_with(fake.MEMORY_DC, 0, 0, 2, 2,
                                               fake.SCREEN_DC, 0, 0, 0x00CC0020)
        fake.gdi32.StretchBlt.assert_not_called()
        fake.gdi32.SetStretchBltMode.assert_not_called()
        self.assert_cleanup(fake, ["SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])
        fake.gdi32.SelectObject.assert_has_calls([call(fake.MEMORY_DC, fake.BITMAP), call(fake.MEMORY_DC, fake.OLD_BITMAP)])
        fake.gdi32.DeleteObject.assert_called_once_with(fake.BITMAP)
        fake.gdi32.DeleteDC.assert_called_once_with(fake.MEMORY_DC)
        fake.user32.ReleaseDC.assert_called_once_with(None, fake.SCREEN_DC)

    def test_invalid_caller_bounds_fail_before_native_loading(self):
        for maximum in (None, [], (1,), (1, 2, 3), (0, 2), (-1, 2), (True, 2), (1.0, 2),
                        (1, "2"), (2**31, 1), (1920, 1201), (100000, 100000)):
            with self.subTest(maximum=maximum), patch.object(windows_screen, "_load_apis") as load:
                with self.assertRaisesRegex(RuntimeError, "^capture_failed$"):
                    windows_screen.capture_primary_image(maximum)
                load.assert_not_called()

    def test_invalid_primary_metrics_fail_before_bitmap_allocation_and_restore_dpi(self):
        for source in ((0, 2), (-1, 2), (4, 0), (4, -1), (2**31, 2), (4, 2**31), (True, 2)):
            with self.subTest(source=source):
                fake = FakeWin32(source)
                self.assert_failed(fake)
                fake.user32.GetDC.assert_not_called()
                fake.gdi32.CreateDIBSection.assert_not_called()
                self.assert_cleanup(fake, ["SetThreadDpiAwarenessContext:2"])

    def test_all_capture_api_failures_restore_only_resources_already_acquired(self):
        full = ["SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"]
        cases = (
            ("SetThreadDpiAwarenessContext:1", []),
            ("GetSystemMetrics:0", ["SetThreadDpiAwarenessContext:2"]),
            ("GetSystemMetrics:1", ["SetThreadDpiAwarenessContext:2"]),
            ("GetDC", ["SetThreadDpiAwarenessContext:2"]),
            ("CreateCompatibleDC", ["ReleaseDC", "SetThreadDpiAwarenessContext:2"]),
            ("CreateDIBSection", ["DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"]),
            ("SelectObject:1", full[1:]),
            ("SetStretchBltMode", full),
            ("SetBrushOrgEx", full),
            ("StretchBlt", full),
            ("GdiFlush", full),
        )
        for stage, cleanup in cases:
            for failure in (0, OSError("PRIVATE CAPTURE DATA C:/secret/path")):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    fake = FakeWin32((8, 4), failures={stage: failure})
                    self.assert_failed(fake)
                    self.assert_cleanup(fake, cleanup)
        for failure in (0, OSError("PRIVATE CAPTURE DATA")):
            fake = FakeWin32(failures={"BitBlt": failure})
            self.assert_failed(fake)
            self.assert_cleanup(fake, full)

    def test_invalid_handle_sentinels_are_never_used_or_deleted(self):
        for stage in ("GetDC", "CreateCompatibleDC", "CreateDIBSection", "SelectObject:1"):
            for invalid in (None, 0, -1, windows_screen._HGDI_ERROR):
                with self.subTest(stage=stage, invalid=invalid):
                    fake = FakeWin32(failures={stage: invalid})
                    self.assert_failed(fake)
                    fake.gdi32.BitBlt.assert_not_called()
                    if stage == "GetDC":
                        fake.user32.ReleaseDC.assert_not_called()
                    if stage in {"GetDC", "CreateCompatibleDC"}:
                        fake.gdi32.DeleteDC.assert_not_called()
                    if stage != "SelectObject:1":
                        fake.gdi32.DeleteObject.assert_not_called()
                    self.assertEqual(fake.events[-1], "SetThreadDpiAwarenessContext:2")

    def test_invalid_dib_pointer_cleans_bitmap_without_reading_pixels(self):
        for invalid in (0, -1, windows_screen._HGDI_ERROR):
            with self.subTest(invalid=invalid), patch.object(windows_screen.ctypes, "string_at") as read:
                fake = FakeWin32(bits=invalid)
                self.assert_failed(fake)
                read.assert_not_called()
                fake.gdi32.SelectObject.assert_not_called()
                self.assert_cleanup(fake, ["DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def test_pixel_copy_and_pillow_failures_are_sanitized_and_release_every_resource(self):
        for target in ("remotevoice.windows_screen.ctypes.string_at", "remotevoice.windows_screen.Image.frombytes"):
            with self.subTest(target=target), patch(target, side_effect=ValueError("PRIVATE PIXELS")):
                fake = FakeWin32()
                self.assert_failed(fake)
                self.assert_cleanup(fake, ["SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def test_cleanup_failure_attempts_remaining_resources_and_closes_result(self):
        for stage in ("SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"):
            for failure in (0, OSError("PRIVATE CLEANUP DATA")):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    fake = FakeWin32(failures={stage: failure})
                    result = Mock()
                    with patch.object(windows_screen.Image, "frombytes", return_value=result):
                        self.assert_failed(fake)
                    result.close.assert_called_once_with()
                    fake.gdi32.DeleteObject.assert_called_once_with(fake.BITMAP)
                    fake.gdi32.DeleteDC.assert_called_once_with(fake.MEMORY_DC)
                    fake.user32.ReleaseDC.assert_called_once_with(None, fake.SCREEN_DC)
                    self.assertEqual(fake.events[-1], "SetThreadDpiAwarenessContext:2")
                    if stage == "SelectObject:2":
                        self.assertLess(fake.events.index("DeleteDC"), fake.events.index("DeleteObject"))

    def test_multiple_cleanup_failures_still_attempt_dpi_restore(self):
        fake = FakeWin32(failures={stage: OSError("PRIVATE") for stage in
                                  ("SelectObject:2", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2")})
        self.assert_failed(fake)
        self.assert_cleanup(fake, ["SelectObject:2", "DeleteDC", "DeleteObject", "DeleteDC", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def test_failed_bitmap_restore_handles_hgdi_error_before_deleting_bitmap(self):
        for invalid in (-1, windows_screen._HGDI_ERROR):
            with self.subTest(invalid=invalid):
                fake = FakeWin32(failures={"SelectObject:2": invalid})
                self.assert_failed(fake)
                self.assert_cleanup(fake, ["SelectObject:2", "DeleteDC", "DeleteObject", "ReleaseDC", "SetThreadDpiAwarenessContext:2"])

    def test_native_loader_failure_is_sanitized(self):
        with patch.object(windows_screen, "_load_apis", side_effect=OSError("PRIVATE DLL PATH")):
            with self.assertRaisesRegex(RuntimeError, "^capture_failed$"):
                windows_screen.capture_primary_image((4, 2))


class WindowsCaptureLoadingTests(unittest.TestCase):
    def test_module_import_does_not_load_windows_dlls(self):
        spec = importlib.util.spec_from_file_location("_windows_screen_import_test", windows_screen.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(ctypes, "WinDLL", create=True, side_effect=AssertionError("Import loaded a DLL")) as load:
            spec.loader.exec_module(module)
        load.assert_not_called()
        self.assertTrue(callable(module.capture_primary_image))

    def test_lazy_loader_uses_windll_and_declares_pointer_safe_signatures(self):
        fake = FakeWin32()
        windows_screen._load_apis.cache_clear()
        self.addCleanup(windows_screen._load_apis.cache_clear)
        with patch.object(ctypes, "WinDLL", create=True, side_effect=[fake.user32, fake.gdi32]) as load:
            self.assertEqual(windows_screen._load_apis(), (fake.user32, fake.gdi32))
            self.assertEqual(windows_screen._load_apis(), (fake.user32, fake.gdi32))
        self.assertEqual(load.call_args_list, [call("user32", use_last_error=True), call("gdi32", use_last_error=True)])
        for dll in (fake.user32, fake.gdi32):
            for function in vars(dll).values():
                self.assertIsInstance(function.argtypes, list)
                self.assertIsNotNone(function.restype)
        self.assertEqual(fake.user32.GetDC.argtypes, [ctypes.c_void_p])
        self.assertIs(fake.user32.GetDC.restype, ctypes.c_void_p)
        self.assertEqual(fake.user32.SetThreadDpiAwarenessContext.argtypes, [ctypes.c_void_p])
        self.assertIs(fake.user32.SetThreadDpiAwarenessContext.restype, ctypes.c_void_p)
        self.assertIs(fake.gdi32.CreateCompatibleDC.restype, ctypes.c_void_p)
        self.assertIs(fake.gdi32.CreateDIBSection.restype, ctypes.c_void_p)
        self.assertIs(fake.gdi32.SelectObject.restype, ctypes.c_void_p)
        self.assertEqual(fake.gdi32.CreateDIBSection.argtypes,
                         [ctypes.c_void_p, ctypes.POINTER(windows_screen._BitmapInfo), ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32])
        self.assertEqual(fake.gdi32.GdiFlush.argtypes, [])
        self.assertEqual(fake.events, [])
