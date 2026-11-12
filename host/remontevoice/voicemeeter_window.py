"""Keep the Voicemeeter Banana mixer running without showing its window.

The Host needs the Banana process: it is the audio engine behind the virtual
devices. Its window is not needed while RemonteVoice runs, so the Host hides it
and can bring it back on request. Windows are matched by executable name and
window class (never by title text alone).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as wt

_BANANA_EXES = frozenset({"voicemeeterpro.exe", "voicemeeter8.exe", "voicemeeter.exe"})
_CLASS_MARKER = "voicemeeter"
SW_HIDE = 0
SW_SHOWNORMAL = 1
_SW_RESTORE = 9


def select_window(rows) -> int:
    """Return the mixer main-window handle from (hwnd, exe, class, visible) rows."""
    for hwnd, exe, class_name, _visible in rows:
        if str(exe).casefold() in _BANANA_EXES and _CLASS_MARKER in str(class_name).casefold():
            return int(hwnd)
    return 0


def _enumerate_rows():
    if os.name != "nt":
        return []
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p,
                                                  ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]

    def exe_name(pid):
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            kernel.CloseHandle(handle)

    rows = []

    def callback(hwnd, _):
        pid = wt.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        class_name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, class_name, 256)
        rows.append((hwnd, exe_name(pid.value), class_name.value, True))
        return True

    user.EnumWindows(proc(callback), 0)
    return rows


def window_handle() -> int:
    """Handle of the running mixer's main window, or 0."""
    try:
        return select_window(_enumerate_rows())
    except OSError:
        return 0


def running() -> bool:
    return window_handle() != 0


def _user32():
    if os.name != "nt":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


class _Api:
    """Narrow adapter so tests can assert the window calls without touching the desktop."""

    def __init__(self, user):
        self._user = user
        self.calls = []

    def show_window(self, hwnd: int, command: int) -> bool:
        self.calls.append(("show", int(hwnd), command))
        return bool(self._user.ShowWindow(hwnd, command))

    def foreground(self, hwnd: int) -> bool:
        self.calls.append(("foreground", int(hwnd)))
        return bool(self._user.SetForegroundWindow(hwnd))


def _adapter(api):
    if api is not None:
        return api
    user = _user32()
    return _Api(user) if user is not None else None


def hide(handle: int | None = None, *, api=None) -> bool:
    hwnd = window_handle() if handle is None else int(handle)
    if not hwnd:
        return False
    adapter = _adapter(api)
    if adapter is None:
        return False
    try:
        adapter.show_window(hwnd, SW_HIDE)
    except Exception:
        return False
    return True


def show(handle: int | None = None, *, api=None) -> bool:
    hwnd = window_handle() if handle is None else int(handle)
    if not hwnd:
        return False
    adapter = _adapter(api)
    if adapter is None:
        return False
    try:
        adapter.show_window(hwnd, _SW_RESTORE)
    except Exception:
        return False
    try:
        adapter.foreground(hwnd)
    except Exception:
        pass
    return True
