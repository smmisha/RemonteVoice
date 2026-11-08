"""Opt-in synchronous KS transport for the uninstalled development driver.

Use exclusively through DriverMicWriter's worker. Metadata selects the exact
RemoteVoice root device/filter, not its display name. This is not a privileged
broker: production control authorization and isolated Windows acceptance remain
open. No enumeration, handle or I/O occurs merely by importing this module.
"""
from __future__ import annotations

import ctypes as ct
import os
import re
import threading
import uuid
from dataclasses import dataclass

from .driver_mic import DriverBufferFull, DriverMicError, WIRE_BYTES

_AUDIO_CATEGORY = uuid.UUID("6994ad04-93ef-11d0-a3cc-00a0c9223196")
_MIC_PROPERTY = uuid.UUID("ee47bfaa-9650-41d5-afa0-09ce9f32e71b")
_HARDWARE_ID = "root\\remotevoiceaudio"
_FILTER_REFERENCE = "\\wavemicarray1"
_IOCTL_KS_PROPERTY = 0x002F0003
_INVALID_HANDLE = ct.c_void_p(-1).value


def _instance(value: str | None) -> str | None:
    if value is None:
        return None  # Legacy development probe; composition always supplies a pin.
    if type(value) is not str or not re.fullmatch(r'ROOT\\MEDIA\\[A-Za-z0-9_-]{1,64}', value, re.IGNORECASE):
        raise ValueError('Expected an exact configured root device instance')
    return value.casefold()


class _GUID(ct.Structure):
    _fields_ = [("bytes", ct.c_ubyte * 16)]


class _DeviceInterface(ct.Structure):
    _fields_ = [("cbSize", ct.c_uint32), ("ClassGuid", _GUID),
                ("Flags", ct.c_uint32), ("Reserved", ct.c_void_p)]


class _DeviceInfo(ct.Structure):
    _fields_ = [("cbSize", ct.c_uint32), ("ClassGuid", _GUID),
                ("DevInst", ct.c_uint32), ("Reserved", ct.c_void_p)]


class _KsProperty(ct.Structure):
    _fields_ = [("Set", _GUID), ("Id", ct.c_uint32), ("Flags", ct.c_uint32)]


@dataclass(frozen=True)
class DriverEndpoint:
    path: str


def _guid(value: uuid.UUID) -> _GUID:
    return _GUID.from_buffer_copy(value.bytes_le)


class _WindowsApi:
    def __init__(self) -> None:
        if os.name != "nt" or ct.sizeof(ct.c_void_p) != 8:
            raise DriverMicError("Development driver requires Windows x64")
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.setup = ct.WinDLL("setupapi", use_last_error=True)
        ptr = ct.c_void_p
        u32, boolean = ct.c_uint32, ct.c_int
        self._bind(self.setup.SetupDiGetClassDevsW, [ct.POINTER(_GUID), ct.c_wchar_p, ptr, u32], ptr)
        self._bind(self.setup.SetupDiEnumDeviceInterfaces,
                   [ptr, ptr, ct.POINTER(_GUID), u32, ct.POINTER(_DeviceInterface)], boolean)
        self._bind(self.setup.SetupDiGetDeviceInterfaceDetailW,
                   [ptr, ct.POINTER(_DeviceInterface), ptr, u32, ct.POINTER(u32), ct.POINTER(_DeviceInfo)], boolean)
        self._bind(self.setup.SetupDiGetDeviceRegistryPropertyW,
                   [ptr, ct.POINTER(_DeviceInfo), u32, ct.POINTER(u32), ptr, u32, ct.POINTER(u32)], boolean)
        self._bind(self.setup.SetupDiGetDeviceInstanceIdW,
                   [ptr, ct.POINTER(_DeviceInfo), ct.c_wchar_p, u32, ct.POINTER(u32)], boolean)
        self._bind(self.setup.SetupDiDestroyDeviceInfoList, [ptr], boolean)
        self._bind(self.kernel.CreateFileW, [ct.c_wchar_p, u32, u32, ptr, u32, u32, ptr], ptr)
        self._bind(self.kernel.DeviceIoControl, [ptr, u32, ptr, u32, ptr, u32, ct.POINTER(u32), ptr], boolean)
        self._bind(self.kernel.CloseHandle, [ptr], boolean)

    @staticmethod
    def _bind(function, arguments, result) -> None:
        function.argtypes, function.restype = arguments, result

    def discover(self, expected_instance_id: str | None = None) -> list[DriverEndpoint]:
        expected_instance_id = _instance(expected_instance_id)
        category = _guid(_AUDIO_CATEGORY)
        info_set = self.setup.SetupDiGetClassDevsW(ct.byref(category), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
        if info_set is None or info_set == _INVALID_HANDLE:
            raise DriverMicError("Audio interface enumeration failed")
        matches: list[DriverEndpoint] = []
        try:
            index = 0
            while True:
                if index >= 4096:
                    raise DriverMicError("Audio interface enumeration limit exceeded")
                interface = _DeviceInterface()
                interface.cbSize = ct.sizeof(interface)
                if not self.setup.SetupDiEnumDeviceInterfaces(info_set, None, ct.byref(category), index, ct.byref(interface)):
                    if ct.get_last_error() == 259:  # ERROR_NO_MORE_ITEMS
                        break
                    raise DriverMicError("Audio interface enumeration failed")
                index += 1
                required = ct.c_uint32()
                self.setup.SetupDiGetDeviceInterfaceDetailW(info_set, ct.byref(interface), None, 0, ct.byref(required), None)
                if ct.get_last_error() != 122 or not 8 <= required.value <= 65536:
                    raise DriverMicError("Invalid audio interface metadata")
                detail = ct.create_string_buffer(required.value)
                ct.c_uint32.from_buffer(detail).value = 8  # SP_DEVICE_INTERFACE_DETAIL_DATA_W, x64.
                device_info = _DeviceInfo()
                device_info.cbSize = ct.sizeof(device_info)
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(info_set, ct.byref(interface), detail,
                        required.value, None, ct.byref(device_info)):
                    raise DriverMicError("Audio interface metadata changed")
                raw = detail.raw[4:]
                if len(raw) % 2:
                    raise DriverMicError("Invalid audio device path")
                path_text = raw.decode("utf-16-le")
                if "\0" not in path_text:
                    raise DriverMicError("Unterminated audio device path")
                path = path_text.split("\0", 1)[0]
                if not path.casefold().endswith(_FILTER_REFERENCE):
                    continue
                if expected_instance_id is not None:
                    instance = ct.create_unicode_buffer(200)
                    instance_size = ct.c_uint32()
                    if not self.setup.SetupDiGetDeviceInstanceIdW(info_set, ct.byref(device_info),
                            instance, len(instance), ct.byref(instance_size)):
                        raise DriverMicError('RemoteVoice device instance unavailable')
                    if not 1 < instance_size.value <= len(instance) or instance[instance_size.value - 1] != '\0':
                        raise DriverMicError('Invalid RemoteVoice device instance')
                    if instance[:instance_size.value - 1].casefold() != expected_instance_id:
                        continue
                hardware = ct.create_string_buffer(65536)
                kind, length = ct.c_uint32(), ct.c_uint32()
                if not self.setup.SetupDiGetDeviceRegistryPropertyW(info_set, ct.byref(device_info), 1,
                        ct.byref(kind), hardware, len(hardware), ct.byref(length)):
                    raise DriverMicError("RemoteVoice hardware identity unavailable")
                if kind.value != 7 or not 4 <= length.value <= len(hardware) or length.value % 2:
                    raise DriverMicError("Invalid RemoteVoice hardware identity")
                text = hardware.raw[:length.value].decode("utf-16-le")
                if not text.endswith("\0\0"):
                    raise DriverMicError("Unterminated RemoteVoice hardware identity")
                if _HARDWARE_ID in {item.casefold() for item in text.split("\0") if item}:
                    matches.append(DriverEndpoint(path))
        finally:
            if not self.setup.SetupDiDestroyDeviceInfoList(info_set):
                raise DriverMicError("Audio interface enumeration cleanup unconfirmed")
        return matches


def discover_driver_endpoints() -> list[DriverEndpoint]:
    """Read metadata only; never open the filter or submit microphone commands."""
    return _WindowsApi().discover()


class WindowsDriverMicTransport:
    """Factory must run on the owning writer thread; no automatic elevation.

    DeviceIoControl is synchronous and may hang in a defective driver. The writer
    observes that deadline independently, retains the worker/handle and lets the
    existing Windows route guard recover. No other thread frees pending buffers.
    """

    def __init__(self, expected_instance_id: str | None = None) -> None:
        expected_instance_id = _instance(expected_instance_id)
        self._thread = threading.get_ident()
        self._api = _WindowsApi()
        endpoints = (self._api.discover(expected_instance_id) if expected_instance_id is not None
                     else self._api.discover())
        if len(endpoints) != 1:
            raise DriverMicError("Exactly one RemoteVoice microphone filter is required")
        self._handle = self._api.kernel.CreateFileW(endpoints[0].path, 0xC0000000, 3, None, 3, 0, None)
        if self._handle is None or self._handle == _INVALID_HANDLE:
            raise DriverMicError("RemoteVoice driver filter could not be opened")

    def _require_owner(self) -> None:
        if self._thread != threading.get_ident():
            raise DriverMicError("Driver handle belongs to another thread")
        if self._handle is None:
            raise DriverMicError("Driver handle is closed")

    def send(self, message: bytes) -> None:
        self._require_owner()
        if type(message) is not bytes or len(message) != WIRE_BYTES:
            raise ValueError("Invalid driver message size")
        prop = _KsProperty(_guid(_MIC_PROPERTY), 0, 2)
        data = ct.create_string_buffer(message, WIRE_BYTES)
        returned = ct.c_uint32()
        if not self._api.kernel.DeviceIoControl(self._handle, _IOCTL_KS_PROPERTY, ct.byref(prop), ct.sizeof(prop),
                data, WIRE_BYTES, ct.byref(returned), None):
            if ct.get_last_error() == 170:  # ERROR_BUSY, STATUS_DEVICE_BUSY.
                raise DriverBufferFull("Driver microphone queue full")
            raise DriverMicError("RemoteVoice microphone command failed")

    def close(self) -> None:
        if self._handle is None:
            return
        self._require_owner()
        if not self._api.kernel.CloseHandle(self._handle):
            raise DriverMicError("Driver handle cleanup unconfirmed")
        self._handle = None
