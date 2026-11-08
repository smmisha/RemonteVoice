"""Trusted read-only MMDevice/PnP inventory; no audio activation or defaults.

Construction/import is inert. observe() belongs on a monitor worker and returns
only after COM cleanup succeeds. The configured root instance, hardware ID and
driver service must all match; friendly names never participate. A snapshot is
not an audio/PCM readiness test and must be refreshed by the runtime monitor.

The GetId/SWD fallback supports the Windows layout verified by the driver lab.
Endpoint IDs are otherwise opaque; an absent property with an unknown layout
fails closed rather than guessing a device. No IMMDevice::Activate is called.
"""
from __future__ import annotations

import ctypes as ct
import re
import threading
from dataclasses import dataclass, field

from . import windows_output as _com

_ACTIVE = 1
_MAX_ENDPOINTS = 256
_MAX_DEPTH = 32
_MAX_INSTANCE = 199  # MAX_DEVICE_ID_LEN includes its terminating WCHAR.
_HARDWARE = "root\\remotevoiceaudio"
_SERVICE = "remotevoiceaudio"


class DriverEndpointInventoryError(RuntimeError):
    """A complete, unambiguous driver endpoint pair could not be verified."""


class _MetadataCleanupError(DriverEndpointInventoryError):
    pass


def _text(value: str, limit: int) -> str:
    if type(value) is not str or not value or len(value) > limit or "\0" in value or value != value.strip():
        raise DriverEndpointInventoryError("Invalid Windows device metadata")
    return value


@dataclass(frozen=True)
class DriverEndpointPair:
    # The render ID is used for Windows-to-phone loopback capture; the capture
    # ID is the virtual microphone consumed by ordinary Windows applications.
    capture_endpoint_id: str = field(repr=False)
    microphone_endpoint_id: str = field(repr=False)

    def __post_init__(self) -> None:
        _text(self.capture_endpoint_id, 1024)
        _text(self.microphone_endpoint_id, 1024)
        if self.capture_endpoint_id.casefold() == self.microphone_endpoint_id.casefold():
            raise DriverEndpointInventoryError("Driver directions must use distinct endpoint IDs")


class _PropertyKey(ct.Structure):
    _fields_ = [("format", _com._GUID), ("id", ct.c_uint32)]


class _VariantValue(ct.Union):
    _fields_ = [("pointer", ct.c_void_p), ("words", ct.c_size_t * 2), ("alignment", ct.c_uint64)]


class _PropVariant(ct.Structure):
    _fields_ = [("vt", ct.c_uint16), ("reserved", ct.c_uint16 * 3), ("value", _VariantValue)]


_INSTANCE_KEY = _PropertyKey(_com._GUID.parse("78c34fc8-104a-4aca-9ea4-524d52996e57"), 256)


def _wide_string(pointer: int, maximum: int) -> str:
    if not pointer:
        raise DriverEndpointInventoryError("Missing Windows device metadata")
    characters = []
    for index in range(maximum + 1):
        character = ct.c_wchar.from_address(pointer + index * ct.sizeof(ct.c_wchar)).value
        if character == "\0":
            return _text("".join(characters), maximum)
        characters.append(character)
    raise DriverEndpointInventoryError("Unterminated Windows device metadata")


class _WindowsApi(_com._ComAdapter):
    def __init__(self) -> None:
        super().__init__()  # Windows guard and lazy ole32 binding.
        self._cfg = ct.WinDLL("cfgmgr32", use_last_error=True)
        u32, pointer = ct.c_uint32, ct.c_void_p
        self._ole32.PropVariantClear.argtypes = [ct.POINTER(_PropVariant)]
        self._ole32.PropVariantClear.restype = _com._HRESULT
        bindings = (
            ("CM_Locate_DevNodeW", [ct.POINTER(u32), ct.c_wchar_p, u32]),
            ("CM_Get_Parent", [ct.POINTER(u32), u32, u32]),
            ("CM_Get_Device_IDW", [u32, ct.c_wchar_p, u32, u32]),
            ("CM_Get_DevNode_Registry_PropertyW", [u32, u32, ct.POINTER(u32), pointer, ct.POINTER(u32), u32]),
        )
        for name, arguments in bindings:
            function = getattr(self._cfg, name)
            function.argtypes, function.restype = arguments, u32

    def _interface(self, owner, slot, *arguments):
        pointer = ct.c_void_p()
        argument_types = [kind for kind, _ in arguments] + [ct.POINTER(ct.c_void_p)]
        try:
            result = self._method(owner, slot, _com._HRESULT, *argument_types)(
                owner, *(value for _, value in arguments), ct.byref(pointer))
            _com._check_hresult(result, "Core Audio inventory failed")
            if not pointer:
                raise DriverEndpointInventoryError("Missing Core Audio interface")
            return pointer
        except BaseException:
            self.release(pointer)
            raise

    def enumerator(self):
        return self.create(_com._CLSID_MMDEVICE_ENUMERATOR, _com._IID_IMMDEVICE_ENUMERATOR,
                           "Core Audio inventory failed")

    def endpoints(self, enumerator, flow, state):
        return self._interface(enumerator, 3, (ct.c_int32, flow), (ct.c_uint32, state))

    def count(self, collection):
        count = ct.c_uint32()
        result = self._method(collection, 3, _com._HRESULT, ct.POINTER(ct.c_uint32))(collection, ct.byref(count))
        _com._check_hresult(result, "Core Audio inventory count failed")
        return count.value

    def item(self, collection, index):
        return self._interface(collection, 4, (ct.c_uint32, index))

    def state(self, device):
        state = ct.c_uint32()
        result = self._method(device, 6, _com._HRESULT, ct.POINTER(ct.c_uint32))(device, ct.byref(state))
        _com._check_hresult(result, "Core Audio inventory state failed")
        return state.value

    def device_id(self, device):
        identifier = ct.c_void_p()
        try:
            result = self._method(device, 5, _com._HRESULT, ct.POINTER(ct.c_void_p))(device, ct.byref(identifier))
            _com._check_hresult(result, "Core Audio inventory ID failed")
            return _wide_string(identifier.value, 1024)
        finally:
            if identifier:
                self._ole32.CoTaskMemFree(identifier)

    def instance_id(self, device):
        properties = self._interface(device, 4, (ct.c_uint32, 0))  # STGM_READ.
        value = _PropVariant()  # PropVariantInit is equivalent to zero initialization.
        instance = primary = None
        try:
            result = self._method(properties, 5, _com._HRESULT,
                ct.POINTER(_PropertyKey), ct.POINTER(_PropVariant))(
                    properties, ct.byref(_INSTANCE_KEY), ct.byref(value))
            if _com._HRESULT(result).value >= 0 and value.vt not in (0, 1):  # VT_EMPTY/VT_NULL.
                if value.vt != 31:  # VT_LPWSTR only.
                    raise DriverEndpointInventoryError("Invalid endpoint instance property")
                instance = _wide_string(value.value.pointer, _MAX_INSTANCE)
        except BaseException as error:
            primary = error
        cleanup_errors = []
        for cleanup, retained in (
            (lambda: _com._check_hresult(self._ole32.PropVariantClear(ct.byref(value)), "Property cleanup failed"), value),
            (lambda: self.release(properties), properties),
        ):
            try:
                cleanup()
            except BaseException as error:
                self._metadata_retained = getattr(self, "_metadata_retained", []) + [retained]
                cleanup_errors.append(error)
        if primary is not None and not isinstance(primary, Exception):
            raise primary
        fatal = next((error for error in cleanup_errors if not isinstance(error, Exception)), None)
        if fatal is not None:
            raise fatal
        if cleanup_errors:
            raise _MetadataCleanupError("Property cleanup is unconfirmed") from None
        if primary is not None:
            raise primary
        return instance

    def locate(self, instance):
        node = ct.c_uint32()
        buffer = ct.create_unicode_buffer(instance)
        if self._cfg.CM_Locate_DevNodeW(ct.byref(node), buffer, 0) != 0:
            raise DriverEndpointInventoryError("Endpoint devnode is unavailable")
        return node.value

    def node_id(self, node):
        buffer = ct.create_unicode_buffer(_MAX_INSTANCE + 1)
        if self._cfg.CM_Get_Device_IDW(node, buffer, len(buffer), 0) != 0:
            raise DriverEndpointInventoryError("Endpoint ancestry is unavailable")
        if "\0" not in buffer[:]:
            raise DriverEndpointInventoryError("Invalid endpoint ancestry")
        return _text(buffer.value, _MAX_INSTANCE)

    def parent(self, node):
        parent = ct.c_uint32()
        if self._cfg.CM_Get_Parent(ct.byref(parent), node, 0) != 0:
            raise DriverEndpointInventoryError("Endpoint parent is unavailable")
        return parent.value

    def _property(self, node, property_id, expected_type):
        buffer = ct.create_string_buffer(8192)
        kind, length = ct.c_uint32(), ct.c_uint32(len(buffer))
        if self._cfg.CM_Get_DevNode_Registry_PropertyW(node, property_id,
                ct.byref(kind), buffer, ct.byref(length), 0) != 0:
            raise DriverEndpointInventoryError("Root driver metadata is unavailable")
        if kind.value != expected_type or not 2 <= length.value <= len(buffer) or length.value % 2:
            raise DriverEndpointInventoryError("Invalid root driver metadata")
        text = buffer.raw[:length.value].decode("utf-16-le")
        if expected_type == 7:  # REG_MULTI_SZ.
            if not text.endswith("\0\0"):
                raise DriverEndpointInventoryError("Unterminated root hardware IDs")
            return tuple(_text(part, _MAX_INSTANCE) for part in text[:-2].split("\0"))
        if not text.endswith("\0"):
            raise DriverEndpointInventoryError("Unterminated root service")
        return _text(text[:-1], 256)

    def hardware_ids(self, node):
        return self._property(node, 2, 7)  # CM_DRP_HARDWAREID, REG_MULTI_SZ.

    def service(self, node):
        return self._property(node, 5, 1)  # CM_DRP_SERVICE, REG_SZ.


class WindowsDriverEndpointInventory:
    def __init__(self, expected_instance_id: str, api=None) -> None:
        if (type(expected_instance_id) is not str or len(expected_instance_id) > _MAX_INSTANCE
                or not re.fullmatch(r"ROOT\\[A-Za-z0-9_&+-]+\\[A-Za-z0-9_&+-]+",
                                    expected_instance_id, re.ASCII | re.IGNORECASE)):
            raise ValueError("An exact trusted root instance ID is required")
        self._expected = expected_instance_id.casefold()
        self._api = api
        self._lock = threading.Lock()
        self._poisoned = False
        self._observing = False
        self._unreleased = []

    @property
    def cleanup_confirmed(self) -> bool:
        """Cached ownership only; never release COM from the caller's thread."""
        return not self._observing and not self._poisoned

    def _instance(self, api, device, endpoint_id, flow):
        instance = api.instance_id(device)
        if instance is not None:
            return _text(instance, _MAX_INSTANCE)
        # Compatibility fallback only: never infer identity from the GUID/name.
        prefix = f"{{0.0.{flow}.00000000}}."
        guid = r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}"
        if not endpoint_id.startswith(prefix) or not re.fullmatch(guid, endpoint_id[len(prefix):], re.ASCII):
            raise DriverEndpointInventoryError("Unsupported endpoint devnode mapping")
        return _text("SWD\\MMDEVAPI\\" + endpoint_id, _MAX_INSTANCE)

    def _matches(self, api, instance):
        node = api.locate(instance)
        seen = set()
        for _ in range(_MAX_DEPTH):
            if type(node) is not int or not 0 <= node < 2**32 or node in seen:
                raise DriverEndpointInventoryError("Invalid endpoint ancestry")
            seen.add(node)
            identity = _text(api.node_id(node), _MAX_INSTANCE).casefold()
            if identity == self._expected:
                hardware = api.hardware_ids(node)
                if type(hardware) is not tuple or not 1 <= len(hardware) <= 128:
                    raise DriverEndpointInventoryError("Invalid root hardware IDs")
                ids = tuple(_text(item, _MAX_INSTANCE).casefold() for item in hardware)
                if len(set(ids)) != len(ids) or _HARDWARE not in ids or _text(api.service(node), 256).casefold() != _SERVICE:
                    raise DriverEndpointInventoryError("Configured root driver identity mismatch")
                return True
            if identity == "htree\\root\\0":
                return False
            node = api.parent(node)
        raise DriverEndpointInventoryError("Endpoint ancestry exceeded bound")

    def observe(self) -> DriverEndpointPair:
        with self._lock:
            if self._poisoned:
                raise DriverEndpointInventoryError("Inventory cleanup remains unconfirmed")
            self._observing = True
            if self._api is None:
                try:
                    self._api = _WindowsApi()
                except BaseException as error:
                    self._observing = False
                    if not isinstance(error, Exception):
                        raise
                    raise DriverEndpointInventoryError("Windows inventory is unavailable") from None
            api, references, selected, seen_ids = self._api, [], {}, set()
            initialized = False
            primary = None
            cleanup_errors = []
            pair = None
            try:
                result = api.initialize()
                if result & 0xFFFFFFFF != _com._RPC_E_CHANGED_MODE:
                    _com._check_hresult(result, "Core Audio initialization failed")
                    initialized = True
                enumerator = api.enumerator()
                references.append(enumerator)
                for flow in (0, 1):  # eRender for system loopback, eCapture for virtual mic.
                    collection = api.endpoints(enumerator, flow, _ACTIVE)
                    references.append(collection)
                    count = api.count(collection)
                    if type(count) is not int or not 0 <= count <= _MAX_ENDPOINTS:
                        raise DriverEndpointInventoryError("Endpoint enumeration exceeded bound")
                    for index in range(count):
                        device = api.item(collection, index)
                        references.append(device)
                        state = api.state(device)
                        if type(state) is not int or state != _ACTIVE:
                            raise DriverEndpointInventoryError("Endpoint disappeared or became inactive")
                        identifier = _text(api.device_id(device), 1024)
                        if identifier.casefold() in seen_ids:
                            raise DriverEndpointInventoryError("Duplicate endpoint identity")
                        seen_ids.add(identifier.casefold())
                        instance = self._instance(api, device, identifier, flow)
                        if self._matches(api, instance):
                            if flow in selected:
                                raise DriverEndpointInventoryError("Ambiguous driver endpoint direction")
                            selected[flow] = (identifier, device, instance)
                if set(selected) != {0, 1}:
                    raise DriverEndpointInventoryError("Exactly one active driver endpoint per direction is required")
                # Recheck both retained COM endpoints and current PnP ancestry:
                # disappearing/changing devices cannot yield a successful pair.
                for flow, (identifier, device, instance) in selected.items():
                    state = api.state(device)
                    if (type(state) is not int or state != _ACTIVE or api.device_id(device) != identifier
                            or self._instance(api, device, identifier, flow).casefold() != instance.casefold()
                            or not self._matches(api, instance)):
                        raise DriverEndpointInventoryError("Driver endpoint changed during observation")
                pair = DriverEndpointPair(selected[0][0], selected[1][0])
            except BaseException as error:
                primary = error
            finally:
                for reference in reversed(references):
                    try:
                        api.release(reference)
                    except BaseException as error:
                        self._unreleased.append(reference)
                        cleanup_errors.append(error)
                if initialized:
                    try:
                        api.uninitialize()
                    except BaseException as error:
                        cleanup_errors.append(error)
                self._poisoned = (bool(cleanup_errors) or isinstance(primary, _MetadataCleanupError)
                                  or bool(getattr(api, "_metadata_retained", ())))
                self._observing = False
            if primary is not None and not isinstance(primary, Exception):
                raise primary
            fatal = next((error for error in cleanup_errors if not isinstance(error, Exception)), None)
            if fatal is not None:
                raise fatal
            if cleanup_errors:
                raise DriverEndpointInventoryError("Inventory cleanup is unconfirmed") from None
            if primary is not None:
                raise DriverEndpointInventoryError("Driver endpoint inventory is unavailable") from None
            if pair is None:
                raise DriverEndpointInventoryError("Driver endpoint pair was not verified")
            return pair
