"""Separate the verified Banana virtual audio paths without changing hardware."""

from __future__ import annotations

import atexit
import ctypes
import logging
import math
import os
import threading
import time
from pathlib import Path


_READBACK_TIMEOUT = 3.0
_POLL_INTERVAL = 0.03
# A measured cold start of the installed Banana (SDK launch accepted, process
# started, server still reporting -2) took 11.2 s on this machine, so the old
# 8 s budget failed a launch that was actually in progress. Keep a margin for
# slower disks and first runs; a genuinely absent mixer still ends in the same
# fixed user-facing error.
_START_TIMEOUT = 20.0
_ERROR = "Не удалось настроить раздельные аудиоканалы Voicemeeter."
_NOT_RUNNING_ERROR = (
    "Не удалось автоматически запустить установленный Voicemeeter Banana. "
    "Проверьте его установку и повторите подключение."
)
_WRONG_PRODUCT_ERROR = (
    "Для выбранных аудиоканалов нужен Voicemeeter Banana. "
    "Закройте другую версию Voicemeeter, запустите Banana и нажмите «Начать подключение»."
)
_ROLLBACK_ERROR = " Исходные настройки Voicemeeter не подтверждены."
_CLOSE_ERROR = "Не удалось подтвердить завершение соединения с Voicemeeter."
LOGGER = logging.getLogger(__name__)
_ROUTES = {
    "Strip[3].A1": 0.0,
    "Strip[3].A2": 0.0,
    "Strip[3].A3": 0.0,
    "Strip[3].B1": 1.0,
    "Strip[3].B2": 0.0,
    "Strip[3].Mute": 0.0,
    "Strip[3].Solo": 0.0,
    "Strip[3].Gain": 0.0,
    "Strip[4].A1": 0.0,
    "Strip[4].A2": 0.0,
    "Strip[4].A3": 0.0,
    "Strip[4].B1": 0.0,
    "Strip[4].B2": 1.0,
    "Strip[4].Mute": 0.0,
    "Strip[4].Solo": 0.0,
    "Strip[4].Gain": 0.0,
    "Bus[3].Mute": 0.0,
    "Bus[3].Gain": 0.0,
    "Bus[4].Mute": 0.0,
    "Bus[4].Gain": 0.0,
}


class VoicemeeterRoutingError(RuntimeError):
    """The selected virtual route could not be configured and confirmed."""


def _base_name(name: str) -> str:
    return name.partition("(")[0].strip().casefold() if isinstance(name, str) else ""


def _load_api():
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    path = Path(program_files) / "VB" / "Voicemeeter" / "VoicemeeterRemote64.dll"
    api = ctypes.WinDLL(str(path))
    for name in ("VBVMR_Login", "VBVMR_Logout", "VBVMR_IsParametersDirty"):
        function = getattr(api, name)
        function.argtypes = []
        function.restype = ctypes.c_int32
    api.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
    api.VBVMR_GetParameterFloat.restype = ctypes.c_int32
    api.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
    api.VBVMR_SetParameterFloat.restype = ctypes.c_int32
    api.VBVMR_GetVoicemeeterType.argtypes = [ctypes.POINTER(ctypes.c_int32)]
    api.VBVMR_GetVoicemeeterType.restype = ctypes.c_int32
    api.VBVMR_RunVoicemeeter.argtypes = [ctypes.c_int32]
    api.VBVMR_RunVoicemeeter.restype = ctypes.c_int32
    return api


def _start_installed_banana(api) -> None:
    # SDK product 2 is Banana. A successful request is asynchronous; no route
    # may change until the running mixer confirms both communication and type.
    started = time.monotonic()
    try:
        result = api.VBVMR_RunVoicemeeter(2)
    except Exception as exc:
        _log_sdk("launch_error", started, error_type=type(exc).__name__)
        raise
    _log_sdk("launch", started, result=result)
    if result != 0:
        raise VoicemeeterRoutingError(_NOT_RUNNING_ERROR)
    deadline = time.monotonic() + _START_TIMEOUT
    last_type_result = None
    while True:
        product = ctypes.c_int32()
        try:
            dirty = api.VBVMR_IsParametersDirty()
            if dirty in (0, 1):
                last_type_result = api.VBVMR_GetVoicemeeterType(ctypes.byref(product))
                if last_type_result == 0:
                    if product.value == 2:
                        _log_sdk("ready", started, result=last_type_result, dirty=dirty, product=product.value)
                        return
                    if product.value in (1, 3):
                        _log_sdk("wrong_product", started, result=last_type_result, dirty=dirty, product=product.value)
                        raise VoicemeeterRoutingError(_WRONG_PRODUCT_ERROR)
            elif dirty != -2:
                _log_sdk("readiness_error", started, result=dirty)
                raise VoicemeeterRoutingError(_ERROR)
        except VoicemeeterRoutingError:
            raise
        except Exception as exc:
            _log_sdk("readiness_exception", started, error_type=type(exc).__name__)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _log_sdk("readiness_timeout", started, result=last_type_result, dirty=dirty, product=product.value)
            raise VoicemeeterRoutingError(_NOT_RUNNING_ERROR)
        time.sleep(min(0.1, remaining))


def _log_sdk(stage, started, *, result=None, dirty=None, product=None, error_type="none") -> None:
    # Log only fixed operation names, scalar return values and exception type;
    # one launch/readiness result, never the full poll stream or native details.
    try:
        LOGGER.info("mixer_sdk stage=%s elapsed_ms=%.3f result=%s dirty=%s product=%s error_type=%s",
                    stage, max(0.0, time.monotonic() - started) * 1000,
                    result, dirty, product, error_type)
    except Exception:
        # Observation must never replace an SDK outcome or lose login ownership.
        pass


def _refresh(api) -> None:
    if api.VBVMR_IsParametersDirty() not in (0, 1):
        raise VoicemeeterRoutingError(_ERROR)


def _read(api, name: str) -> float:
    value = ctypes.c_float()
    if api.VBVMR_GetParameterFloat(name.encode("ascii"), ctypes.byref(value)) != 0:
        raise VoicemeeterRoutingError(_ERROR)
    if not math.isfinite(value.value):
        raise VoicemeeterRoutingError(_ERROR)
    return value.value


def _write(api, name: str, value: float) -> None:
    if api.VBVMR_SetParameterFloat(name.encode("ascii"), ctypes.c_float(value)) != 0:
        raise VoicemeeterRoutingError(_ERROR)


def _confirm(api, expected: dict[str, float]) -> None:
    deadline = time.monotonic() + _READBACK_TIMEOUT
    while True:
        _refresh(api)
        actual = {name: _read(api, name) for name in expected}
        remaining = deadline - time.monotonic()
        if remaining >= 0 and all(abs(actual[name] - value) < 0.001 for name, value in expected.items()):
            return
        if remaining <= 0:
            raise VoicemeeterRoutingError(_ERROR)
        time.sleep(min(_POLL_INTERVAL, remaining))


def _restore(api, original: dict[str, float]) -> bool:
    restored = True
    for name, value in original.items():
        try:
            _write(api, name, value)
        except Exception:
            restored = False  # Still attempt every remaining original setting.
    try:
        _confirm(api, original)
    except Exception:
        restored = False
    return restored


def _configure_routes(api) -> None:
    """Configure only the twenty verified virtual controls on an owned login."""
    original = {}
    mutating = False
    failed = False
    failure_message = _ERROR
    rollback_ok = True
    try:
        # SDK: type 2 is Banana. Shared endpoint names do not establish the
        # running product's strip indices; other layouts must remain untouched.
        product = ctypes.c_int32()
        if api.VBVMR_GetVoicemeeterType(ctypes.byref(product)) != 0:
            raise VoicemeeterRoutingError(_ERROR)
        if product.value != 2:
            failure_message = _WRONG_PRODUCT_ERROR
            raise VoicemeeterRoutingError(failure_message)
        _refresh(api)
        original = {name: _read(api, name) for name in _ROUTES}
        mutating = True
        for name, value in _ROUTES.items():
            _write(api, name, value)
        _confirm(api, _ROUTES)
    except Exception as exc:
        failed = True
        if isinstance(exc, VoicemeeterRoutingError):
            failure_message = str(exc)
        if mutating:
            rollback_ok = _restore(api, original)
    if failed:
        raise VoicemeeterRoutingError(failure_message + ("" if rollback_ok else _ROLLBACK_ERROR)) from None


class _MixerSession:
    """One lazy SDK client for this process; availability never re-logins.

    The SDK preserves its client connection across mixer restarts. Calls that
    refresh its parameter cache must be serialized, including full readback and
    rollback operations. Final close is terminal even when Logout fails.
    """

    def __init__(self, api_loader=None):
        self._api_loader = api_loader
        self._api = None
        self._logged_in = False
        self._owner_thread = None
        self._login_failed = False
        self._closed = False
        self._close_failed = False
        self._lock = threading.RLock()

    def _open(self):
        if self._closed or self._login_failed:
            raise VoicemeeterRoutingError(_ERROR)
        # IsParametersDirty's cache must be refreshed from one owner thread,
        # not merely by non-overlapping callers on different threads. Holding
        # the Thread object also avoids accepting a reused numeric thread id.
        if self._owner_thread is not None and self._owner_thread is not threading.current_thread():
            raise VoicemeeterRoutingError(_ERROR)
        if self._api is None:
            try:
                self._api = (self._api_loader or _load_api)()
            except Exception as exc:
                # No Login was attempted; a later explicit attempt is safe.
                _log_sdk("load_error", time.monotonic(), error_type=type(exc).__name__)
                raise VoicemeeterRoutingError(_ERROR) from None
        if not self._logged_in:
            started = time.monotonic()
            try:
                result = self._api.VBVMR_Login()
            except Exception as exc:
                self._login_failed = True
                _log_sdk("login_error", started, error_type=type(exc).__name__)
                raise VoicemeeterRoutingError(_ERROR) from None
            _log_sdk("login", started, result=result)
            if result not in (0, 1):
                # Do not Logout a client whose ownership was not confirmed.
                self._login_failed = True
                raise VoicemeeterRoutingError(_ERROR)
            self._logged_in = True
            self._owner_thread = threading.current_thread()
        return self._api

    def configure_routes(self) -> None:
        with self._lock:
            try:
                api = self._open()
                dirty = api.VBVMR_IsParametersDirty()
                if dirty == -2:
                    _start_installed_banana(api)
                elif dirty not in (0, 1):
                    raise VoicemeeterRoutingError(_ERROR)
                _configure_routes(api)
            except VoicemeeterRoutingError:
                raise
            except Exception:
                raise VoicemeeterRoutingError(_ERROR) from None

    def is_running(self) -> bool:
        with self._lock:
            try:
                api = self._open()
                if api.VBVMR_IsParametersDirty() not in (0, 1):
                    return False
                product = ctypes.c_int32()
                return api.VBVMR_GetVoicemeeterType(ctypes.byref(product)) == 0 and product.value == 2
            except Exception:
                return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                if self._close_failed:
                    raise VoicemeeterRoutingError(_CLOSE_ERROR)
                return
            self._closed = True
            if not self._logged_in:
                return
            self._logged_in = False
            started = time.monotonic()
            try:
                result = self._api.VBVMR_Logout()
                _log_sdk("logout", started, result=result)
                if result != 0:
                    self._close_failed = True
            except Exception as exc:
                self._close_failed = True
                _log_sdk("logout_error", started, error_type=type(exc).__name__)
            if self._close_failed:
                raise VoicemeeterRoutingError(_CLOSE_ERROR)


_session = _MixerSession()


def configure_standard_routes(capture_name: str, playback_name: str) -> bool:
    """Prepare the known pair without changing Windows defaults or hardware."""
    if _base_name(capture_name) not in {"voicemeeter out b1", "voicemeeter output"}:
        return False
    if _base_name(playback_name) != "voicemeeter aux input":
        return False
    _session.configure_routes()
    return True


def standard_mixer_running() -> bool:
    """Read existing mixer availability without launching or rewriting routes."""
    return _session.is_running()


def close_standard_mixer_session() -> None:
    """Release the one process-owned SDK login only at final application exit."""
    _session.close()


def _close_at_exit() -> None:
    try:
        close_standard_mixer_session()
    except VoicemeeterRoutingError:
        LOGGER.warning("Mixer SDK logout was not confirmed")


atexit.register(_close_at_exit)
