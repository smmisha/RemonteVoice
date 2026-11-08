"""Windows output endpoints through Core Audio; importing this module has no side effects.

Reading uses the public IMMDeviceEnumerator / IMMDevice SDK interfaces. Setting
uses the undocumented IPolicyConfig interface, which can be unavailable and must
be treated as a recoverable WindowsOutputError by the caller.

Interface definitions:
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/mmdeviceapi.h
https://github.com/frgnca/AudioDeviceCmdlets/blob/master/SOURCE/IPolicyConfig.cs
https://github.com/frgnca/AudioDeviceCmdlets/blob/master/SOURCE/PolicyConfigClient.cs
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_HRESULT = ctypes.c_int32
_ULONG = ctypes.c_uint32
_RPC_E_CHANGED_MODE = 0x80010106
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_E_RENDER = 0


class WindowsOutputError(RuntimeError):
    """Core Audio or the optional default-output setter could not be used."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_CLSID_MMDEVICE_ENUMERATOR = _GUID.parse("bcde0395-e52f-467c-8e3d-c4579291692e")
_IID_IMMDEVICE_ENUMERATOR = _GUID.parse("a95664d2-9614-4f35-a746-de8db63617e6")
_CLSID_POLICY_CONFIG = _GUID.parse("870af99c-171d-4f9e-af0d-e63df40c2bc9")
_IID_IPOLICY_CONFIG = _GUID.parse("f8679f50-850a-41cf-9c72-430f290290c8")


def _validate_role(role: int) -> None:
    if isinstance(role, bool) or not isinstance(role, int) or role not in (0, 1, 2):
        raise ValueError("Роль вывода должна быть 0, 1 или 2.")


def _validate_endpoint_id(endpoint_id: str) -> None:
    # Endpoint IDs are opaque; do not parse their current Windows-specific format.
    if (
        not isinstance(endpoint_id, str)
        or not endpoint_id.strip()
        or "\0" in endpoint_id
        or len(endpoint_id) > 32767
    ):
        raise ValueError("Нужен корректный идентификатор устройства вывода.")


def _check_hresult(result: int, operation: str) -> None:
    if _HRESULT(result).value < 0:
        raise WindowsOutputError(f"{operation} (HRESULT 0x{result & 0xFFFFFFFF:08X}).")


class _ComAdapter:
    """Small ctypes boundary; the public operations never share COM pointers."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsOutputError("Управление выходом Core Audio доступно только в Windows.")
        try:
            self._ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        except OSError:
            raise WindowsOutputError("Не удалось загрузить Windows Core Audio COM.") from None
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._ole32.CoInitializeEx.restype = _HRESULT
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = _HRESULT
        self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole32.CoTaskMemFree.restype = None

    def initialize(self) -> int:
        return self._ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)

    def uninitialize(self) -> None:
        self._ole32.CoUninitialize()

    @staticmethod
    def _method(pointer: ctypes.c_void_p, index: int, result: Any, *args: Any) -> Any:
        if not pointer:
            raise WindowsOutputError("Core Audio вернул пустой COM-интерфейс.")
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *args)(table[index])

    def release(self, pointer: ctypes.c_void_p) -> None:
        if pointer:
            self._method(pointer, 2, _ULONG)(pointer)

    def create(self, clsid: _GUID, iid: _GUID, operation: str) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        result = self._ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER,
            ctypes.byref(iid), ctypes.byref(pointer),
        )
        try:
            _check_hresult(result, operation)
            if not pointer:
                raise WindowsOutputError(f"{operation}: COM-интерфейс недоступен.")
            return pointer
        except Exception:
            self.release(pointer)
            raise

    def default_device(self, enumerator: ctypes.c_void_p, role: int) -> ctypes.c_void_p:
        device = ctypes.c_void_p()
        result = self._method(
            enumerator, 4, _HRESULT, ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_void_p),
        )(enumerator, _E_RENDER, role, ctypes.byref(device))
        try:
            _check_hresult(result, "Не удалось прочитать устройство вывода Windows")
            if not device:
                raise WindowsOutputError("Windows не вернула устройство вывода.")
            return device
        except Exception:
            self.release(device)
            raise

    def device_id(self, device: ctypes.c_void_p) -> str:
        allocated_id = ctypes.c_void_p()
        try:
            result = self._method(
                device, 5, _HRESULT, ctypes.POINTER(ctypes.c_void_p)
            )(device, ctypes.byref(allocated_id))
            _check_hresult(result, "Не удалось прочитать идентификатор устройства вывода")
            if not allocated_id:
                raise WindowsOutputError("Windows вернула пустой идентификатор устройства.")
            endpoint_id = ctypes.wstring_at(allocated_id)
            try:
                _validate_endpoint_id(endpoint_id)
            except ValueError:
                raise WindowsOutputError("Windows вернула некорректный идентификатор устройства.") from None
            return endpoint_id
        finally:
            if allocated_id:
                self._ole32.CoTaskMemFree(allocated_id)

    def set_default(self, policy: ctypes.c_void_p, endpoint_id: str, role: int) -> None:
        result = self._method(
            policy, 13, _HRESULT, ctypes.c_wchar_p, ctypes.c_int32
        )(policy, endpoint_id, role)
        _check_hresult(result, "Windows не удалось переключить выход через IPolicyConfig")


@contextmanager
def _com_apartment() -> Iterator[_ComAdapter]:
    adapter = _ComAdapter()
    result = adapter.initialize()
    # A changed mode means COM already belongs to this thread's caller. We may
    # use it, but did not acquire a successful initialization to balance.
    initialized_here = result & 0xFFFFFFFF != _RPC_E_CHANGED_MODE
    if initialized_here:
        _check_hresult(result, "Не удалось инициализировать Core Audio COM")
    try:
        yield adapter
    finally:
        if initialized_here:
            adapter.uninitialize()


def get_default_output(role: int) -> str:
    """Return the current rendering endpoint ID for exactly one Windows role."""
    _validate_role(role)
    with _com_apartment() as adapter:
        enumerator = adapter.create(
            _CLSID_MMDEVICE_ENUMERATOR, _IID_IMMDEVICE_ENUMERATOR,
            "Не удалось открыть список устройств Core Audio",
        )
        try:
            device = adapter.default_device(enumerator, role)
            try:
                return adapter.device_id(device)
            finally:
                adapter.release(device)
        finally:
            adapter.release(enumerator)


def set_default_output(endpoint_id: str, role: int) -> None:
    """Set one role through optional IPolicyConfig; failure leaves policy to the caller."""
    _validate_role(role)
    _validate_endpoint_id(endpoint_id)
    with _com_apartment() as adapter:
        policy = adapter.create(
            _CLSID_POLICY_CONFIG, _IID_IPOLICY_CONFIG,
            "Автоматическое переключение вывода недоступно через IPolicyConfig",
        )
        try:
            adapter.set_default(policy, endpoint_id, role)
        finally:
            adapter.release(policy)
