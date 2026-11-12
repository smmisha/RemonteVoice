"""Bounded primary-monitor capture using GDI calls that release the Python GIL.

The caller owns the screen-sharing consent check. Native resources and pixels
remain local to one call; no full-size intermediate bitmap or file is created.
"""

from __future__ import annotations

import ctypes
from functools import lru_cache

from PIL import Image

_MAX_BITMAP_BYTES = 1920 * 1200 * 4
_INT_MAX = 2**31 - 1
_HGDI_ERROR = ctypes.c_void_p(-1).value
_PER_MONITOR_AWARE = ctypes.c_void_p(-3).value
_SRCCOPY = 0x00CC0020
_HALFTONE = 4
_COLORONCOLOR = 3


class _BitmapInfoHeader(ctypes.Structure):
    # Win32 LONG/DWORD stay 32 bit even when imported on a non-Windows host.
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 1)]


@lru_cache(maxsize=1)
def _load_apis():
    # WinDLL, unlike PyDLL and Pillow's grab wrapper, releases the GIL during
    # potentially slow driver calls. Loading is lazy for non-Windows imports.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle = ctypes.c_void_p
    integer = ctypes.c_int
    boolean = ctypes.c_int
    dword = ctypes.c_uint32
    signatures = (
        (user32.GetDC, [handle], handle),
        (user32.ReleaseDC, [handle, handle], integer),
        (user32.GetSystemMetrics, [integer], integer),
        (user32.SetThreadDpiAwarenessContext, [handle], handle),
        (gdi32.CreateCompatibleDC, [handle], handle),
        (gdi32.CreateDIBSection,
         [handle, ctypes.POINTER(_BitmapInfo), dword, ctypes.POINTER(handle), handle, dword], handle),
        (gdi32.SelectObject, [handle, handle], handle),
        (gdi32.BitBlt, [handle, integer, integer, integer, integer, handle, integer, integer, dword], boolean),
        (gdi32.StretchBlt,
         [handle, integer, integer, integer, integer, handle, integer, integer, integer, integer, dword], boolean),
        (gdi32.SetStretchBltMode, [handle, integer], integer),
        (gdi32.SetBrushOrgEx, [handle, integer, integer, handle], boolean),
        (gdi32.GdiFlush, [], boolean),
        (gdi32.DeleteObject, [handle], boolean),
        (gdi32.DeleteDC, [handle], boolean),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return user32, gdi32


def _valid_handle(value) -> bool:
    return value not in (None, 0, -1, _HGDI_ERROR)


def _fit_size(width: int, height: int, max_size: tuple[int, int]) -> tuple[int, int]:
    if not (isinstance(max_size, tuple) and len(max_size) == 2):
        raise RuntimeError("capture_failed")
    if any(type(value) is not int or not 1 <= value <= _INT_MAX for value in (*max_size, width, height)):
        raise RuntimeError("capture_failed")
    max_width, max_height = max_size
    if max_width * max_height * 4 > _MAX_BITMAP_BYTES:
        raise RuntimeError("capture_failed")
    if width <= max_width and height <= max_height:
        return width, height
    # Integer rounding avoids floating-point overflow for invalid OS metrics.
    if max_width * height <= max_height * width:
        return max_width, max(1, (height * max_width + width // 2) // width)
    return max(1, (width * max_height + height // 2) // height), max_height


def _cleanup(user32, gdi32, screen_dc, memory_dc, bitmap, old_bitmap, old_dpi) -> bool:
    succeeded = True

    def attempt(function, *arguments, handle_result=False):
        nonlocal succeeded
        try:
            value = function(*arguments)
            ok = _valid_handle(value) if handle_result else bool(value)
        except Exception:
            ok = False
        succeeded = succeeded and ok
        return ok

    restored = True
    if old_bitmap is not None:
        restored = attempt(gdi32.SelectObject, memory_dc, old_bitmap, handle_result=True)
    if not restored:
        # Deleting the memory DC first releases a bitmap if re-selection failed.
        # Every resource still gets a cleanup attempt, including the DPI context.
        if attempt(gdi32.DeleteDC, memory_dc):
            memory_dc = None
    if bitmap is not None:
        attempt(gdi32.DeleteObject, bitmap)
    if memory_dc is not None:
        attempt(gdi32.DeleteDC, memory_dc)
    if screen_dc is not None:
        attempt(user32.ReleaseDC, None, screen_dc)
    if old_dpi is not None:
        # Negative pseudo-handles are valid DPI contexts, including -1.
        attempt(user32.SetThreadDpiAwarenessContext, old_dpi)
    return succeeded


def capture_primary_image(max_size: tuple[int, int], *, fast: bool = False) -> Image.Image:
    """Return an RGB primary-screen image within max_size, without upscaling.

    Failures expose a fixed code only. This function never falls back to a
    different capture API or changes the process-wide DPI awareness.
    """
    image = None
    try:
        # Validate caller bounds before loading native libraries or changing DPI.
        _fit_size(1, 1, max_size)
        user32, gdi32 = _load_apis()
        screen_dc = memory_dc = bitmap = old_bitmap = old_dpi = None
        try:
            old_dpi = user32.SetThreadDpiAwarenessContext(_PER_MONITOR_AWARE)
            if not old_dpi:
                old_dpi = None
                raise RuntimeError("capture_failed")
            # SM_CXSCREEN/SM_CYSCREEN describe only the primary monitor. Its
            # desktop origin is (0, 0); virtual-screen dimensions are not used.
            source_width = user32.GetSystemMetrics(0)
            source_height = user32.GetSystemMetrics(1)
            width, height = _fit_size(source_width, source_height, max_size)
            byte_count = width * height * 4
            if byte_count > _MAX_BITMAP_BYTES:
                raise RuntimeError("capture_failed")
            screen_dc = user32.GetDC(None)
            if not _valid_handle(screen_dc):
                screen_dc = None
                raise RuntimeError("capture_failed")
            memory_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not _valid_handle(memory_dc):
                memory_dc = None
                raise RuntimeError("capture_failed")
            info = _BitmapInfo()
            info.bmiHeader = _BitmapInfoHeader(
                biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width, biHeight=-height,
                biPlanes=1, biBitCount=32, biCompression=0, biSizeImage=byte_count,
            )
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            if not _valid_handle(bitmap):
                bitmap = None
                raise RuntimeError("capture_failed")
            if not _valid_handle(bits.value):
                raise RuntimeError("capture_failed")
            old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
            if not _valid_handle(old_bitmap):
                old_bitmap = None
                raise RuntimeError("capture_failed")
            if (width, height) == (source_width, source_height):
                # CAPTUREBLT can hide/redraw the local software cursor on each
                # grab. Plain desktop SRCCOPY avoids that layered-window pass.
                copied = gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, 0, 0, _SRCCOPY)
            else:
                if not gdi32.SetStretchBltMode(memory_dc, _COLORONCOLOR if fast else _HALFTONE):
                    raise RuntimeError("capture_failed")
                if not fast and not gdi32.SetBrushOrgEx(memory_dc, 0, 0, None):
                    raise RuntimeError("capture_failed")
                copied = gdi32.StretchBlt(memory_dc, 0, 0, width, height, screen_dc, 0, 0,
                                         source_width, source_height, _SRCCOPY)
            if not copied or not gdi32.GdiFlush():
                raise RuntimeError("capture_failed")
            pixels = ctypes.string_at(bits, byte_count)
            image = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX", width * 4, 1)
        finally:
            if not _cleanup(user32, gdi32, screen_dc, memory_dc, bitmap, old_bitmap, old_dpi):
                raise RuntimeError("capture_failed")
        return image
    except Exception:
        if image is not None:
            image.close()
        raise RuntimeError("capture_failed") from None
