"""Control verified Windows capture endpoints without recording.

Reading and mute use public Core Audio interfaces; changing the default uses the
optional IPolicyConfig adapter shared with windows_output. Importing this module
neither opens an endpoint nor changes mute. COM initialization and calling conventions
are shared with windows_output; references remain local to each function call.

SDK definitions:
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/mmdeviceapi.h
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/endpointvolume.h
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from . import windows_output as _com

_IID_IMM_ENDPOINT = _com._GUID.parse("1be09788-6894-4089-8586-9a2a6c265ac5")
_IID_AUDIO_ENDPOINT_VOLUME = _com._GUID.parse("5cdf2c82-841e-4546-9722-0cf74078229a")
_BOOL = ctypes.c_int32
_E_CAPTURE = 1


class WindowsMicrophoneError(RuntimeError):
    """The capture endpoint could not be verified or its mute could not be used."""


def _validate_role(role: int) -> None:
    try:
        _com._validate_role(role)
    except ValueError:
        raise ValueError("Роль микрофона должна быть 0, 1 или 2.") from None


def _validate_endpoint_id(endpoint_id: str) -> None:
    try:
        _com._validate_endpoint_id(endpoint_id)
    except ValueError:
        raise ValueError("Нужен корректный идентификатор микрофона Windows.") from None


def _check_hresult(result: int, operation: str) -> None:
    if _com._HRESULT(result).value < 0:
        raise WindowsMicrophoneError(f"{operation} (HRESULT 0x{result & 0xFFFFFFFF:08X}).")


def _interface_result(
    adapter: _com._ComAdapter, method: Callable[..., int], operation: str, *arguments: Any
) -> ctypes.c_void_p:
    pointer = ctypes.c_void_p()
    try:
        result = method(*arguments, ctypes.byref(pointer))
        _check_hresult(result, operation)
        if not pointer:
            raise WindowsMicrophoneError(f"{operation}: COM-интерфейс недоступен.")
        return pointer
    except Exception:
        adapter.release(pointer)
        raise


@contextmanager
def _capture_device(endpoint_id: str) -> Iterator[tuple[_com._ComAdapter, ctypes.c_void_p]]:
    try:
        with _com._com_apartment() as adapter, ExitStack() as references:
            enumerator = adapter.create(
                _com._CLSID_MMDEVICE_ENUMERATOR, _com._IID_IMMDEVICE_ENUMERATOR,
                "Не удалось открыть список микрофонов Core Audio",
            )
            references.callback(adapter.release, enumerator)
            device = _interface_result(
                adapter,
                adapter._method(
                    enumerator, 5, _com._HRESULT, ctypes.c_wchar_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
                "Не удалось открыть выбранный микрофон", enumerator, endpoint_id,
            )
            references.callback(adapter.release, device)
            endpoint = _interface_result(
                adapter,
                adapter._method(
                    device, 0, _com._HRESULT, ctypes.POINTER(_com._GUID),
                    ctypes.POINTER(ctypes.c_void_p),
                ),
                "Не удалось проверить направление аудиоустройства",
                device, ctypes.byref(_IID_IMM_ENDPOINT),
            )
            references.callback(adapter.release, endpoint)
            direction = ctypes.c_int32(-1)
            result = adapter._method(
                endpoint, 3, _com._HRESULT, ctypes.POINTER(ctypes.c_int32)
            )(endpoint, ctypes.byref(direction))
            _check_hresult(result, "Не удалось проверить направление аудиоустройства")
            if direction.value != _E_CAPTURE:
                raise WindowsMicrophoneError("Выбранное устройство не является микрофоном Windows.")
            yield adapter, device
    except _com.WindowsOutputError as exc:
        raise WindowsMicrophoneError(f"Управление микрофоном недоступно: {exc}") from None


@contextmanager
def _capture_volume(endpoint_id: str) -> Iterator[tuple[_com._ComAdapter, ctypes.c_void_p]]:
    with _capture_device(endpoint_id) as (adapter, device), ExitStack() as references:
        volume = _interface_result(
            adapter,
            adapter._method(
                device, 3, _com._HRESULT, ctypes.POINTER(_com._GUID),
                ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ),
            "Не удалось открыть управление mute микрофона",
            device, ctypes.byref(_IID_AUDIO_ENDPOINT_VOLUME),
            _com._CLSCTX_INPROC_SERVER, None,
        )
        references.callback(adapter.release, volume)
        yield adapter, volume


def get_default_microphone(role: int) -> str:
    """Return the capture endpoint assigned to exactly one Windows role."""
    _validate_role(role)
    try:
        with _com._com_apartment() as adapter, ExitStack() as references:
            enumerator = adapter.create(
                _com._CLSID_MMDEVICE_ENUMERATOR, _com._IID_IMMDEVICE_ENUMERATOR,
                "Не удалось открыть список микрофонов Core Audio",
            )
            references.callback(adapter.release, enumerator)
            device = _interface_result(
                adapter,
                adapter._method(
                    enumerator, 4, _com._HRESULT, ctypes.c_int32, ctypes.c_int32,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
                "Не удалось прочитать микрофон Windows",
                enumerator, _E_CAPTURE, role,
            )
            references.callback(adapter.release, device)
            return adapter.device_id(device)
    except _com.WindowsOutputError as exc:
        raise WindowsMicrophoneError(f"Управление микрофоном недоступно: {exc}") from None


def set_default_microphone(endpoint_id: str, role: int) -> None:
    """Assign a verified capture endpoint to one role through optional IPolicyConfig."""
    _validate_role(role)
    _validate_endpoint_id(endpoint_id)
    with _capture_device(endpoint_id) as (adapter, _device):
        policy = adapter.create(
            _com._CLSID_POLICY_CONFIG, _com._IID_IPOLICY_CONFIG,
            "Автоматическое переключение микрофона недоступно через IPolicyConfig",
        )
        try:
            adapter.set_default(policy, endpoint_id, role)
        finally:
            adapter.release(policy)


def get_microphone_mute(endpoint_id: str) -> bool:
    """Return mute for an explicitly identified capture endpoint, without recording."""
    _validate_endpoint_id(endpoint_id)
    with _capture_volume(endpoint_id) as (adapter, volume):
        muted = _BOOL()
        result = adapter._method(
            volume, 15, _com._HRESULT, ctypes.POINTER(_BOOL)
        )(volume, ctypes.byref(muted))
        _check_hresult(result, "Не удалось прочитать mute микрофона")
        return bool(muted.value)


def set_microphone_mute(endpoint_id: str, muted: bool) -> None:
    """Set capture endpoint mute; the caller owns any previous-state restoration."""
    _validate_endpoint_id(endpoint_id)
    if type(muted) is not bool:
        raise ValueError("Состояние mute микрофона должно быть bool.")
    with _capture_volume(endpoint_id) as (adapter, volume):
        result = adapter._method(
            volume, 14, _com._HRESULT, _BOOL, ctypes.POINTER(_com._GUID)
        )(volume, int(muted), None)
        _check_hresult(result, "Не удалось изменить mute микрофона")
