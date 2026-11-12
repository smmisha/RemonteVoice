"""Local Windows byte pipes; worker-only, source-only transport, no service setup.

This module does not authenticate a process or implement the RVMB protocol. A
trusted server must verify each peer separately. Client identification SQOS lets
the server inspect identity without impersonating it to access other resources.
No native library or pipe is opened on import. Do not call blocking I/O on UI.

Each object serializes I/O. Timeouts allow up to 50 ms to confirm cancellation;
if completion is unconfirmed, buffers/events/handles remain owned, the object
fails, and close() must be retried by its worker. A process-wide retention list
also protects outstanding native buffers when a caller drops a failed object.
"""
from __future__ import annotations

import ctypes as ct
import math
import os
import re
import threading
import time
from dataclasses import dataclass

MAX_IO_BYTES = 1928
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_ACCESS_DUPLEX = 3
PIPE_REJECT_REMOTE_CLIENTS = 8
SECURITY_IDENTIFICATION = 0x00010000
SECURITY_SQOS_PRESENT = 0x00100000
# FILE_GENERIC_READ|FILE_GENERIC_WRITE, excluding FILE_APPEND_DATA (also the
# dangerous FILE_CREATE_PIPE_INSTANCE right). CreateFile requests the same mask.
CLIENT_ACCESS = 0x0012019B
_INVALID_HANDLE = ct.c_void_p(-1).value
_PENDING = (996, 997)  # ERROR_IO_INCOMPLETE, ERROR_IO_PENDING
_EOF = (38, 109, 232, 233)
_TERMINAL_COMPLETION = (0, 995, 38, 109, 232, 233, 234)
_RETAINED: list[object] = []


class PipeError(RuntimeError):
    """Sanitized pipe failure, without endpoint or Windows identity details."""


class PipeTimeoutError(PipeError):
    pass


class PipeEOFError(PipeError):
    pass


class PipeCleanupError(PipeError):
    """Native ownership remains unconfirmed; retain this endpoint and retry close."""


class _Overlapped(ct.Structure):
    _fields_ = [("Internal", ct.c_size_t), ("InternalHigh", ct.c_size_t),
                ("Offset", ct.c_uint32), ("OffsetHigh", ct.c_uint32), ("hEvent", ct.c_void_p)]


class _SecurityAttributes(ct.Structure):
    _fields_ = [("nLength", ct.c_uint32), ("lpSecurityDescriptor", ct.c_void_p),
                ("bInheritHandle", ct.c_int)]


def _name(name: str) -> str:
    prefix = "\\\\.\\pipe\\"
    if type(name) is not str:
        raise ValueError("Invalid local broker pipe name")
    suffix = name[len(prefix):] if name.startswith(prefix) else name
    if not re.fullmatch(r"RemonteVoice-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", suffix, re.ASCII):
        raise ValueError("Invalid local broker pipe name")
    return prefix + suffix


def _sddl(logon_sid: str) -> str:
    if type(logon_sid) is not str or not re.fullmatch(
            r"S-1-5-5-(?:0|[1-9][0-9]{0,9})-(?:0|[1-9][0-9]{0,9})", logon_sid, re.ASCII):
        raise ValueError("Expected an exact logon SID")
    if any(int(part) > 0xFFFFFFFF for part in logon_sid.split("-")[-2:]):
        raise ValueError("Invalid logon SID subauthority")
    return f"D:P(A;;GA;;;SY)(A;;0x{CLIENT_ACCESS:08x};;;{logon_sid})"


def _deadline(timeout: float, api) -> float:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 1:
        raise ValueError("Pipe deadline must be greater than zero and at most one second")
    return api.clock() + timeout


def _milliseconds(deadline: float, api) -> int:
    return max(0, min(1000, math.ceil((deadline - api.clock()) * 1000)))


class _WindowsApi:
    clock = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def __init__(self) -> None:
        if os.name != "nt":
            raise PipeError("Windows broker pipes require Windows")
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.security = ct.WinDLL("advapi32", use_last_error=True)
        ptr, u32, boolean = ct.c_void_p, ct.c_uint32, ct.c_int
        overlap = ct.POINTER(_Overlapped)
        self._bind(self.kernel.CreateNamedPipeW,
                   [ct.c_wchar_p, u32, u32, u32, u32, u32, u32, ct.POINTER(_SecurityAttributes)], ptr)
        self._bind(self.kernel.CreateFileW, [ct.c_wchar_p, u32, u32, ptr, u32, u32, ptr], ptr)
        self._bind(self.kernel.WaitNamedPipeW, [ct.c_wchar_p, u32], boolean)
        self._bind(self.kernel.GetNamedPipeInfo, [ptr, ct.POINTER(u32), ptr, ptr, ptr], boolean)
        self._bind(self.kernel.ConnectNamedPipe, [ptr, overlap], boolean)
        for function in (self.kernel.ReadFile, self.kernel.WriteFile):
            self._bind(function, [ptr, ptr, u32, ct.POINTER(u32), overlap], boolean)
        self._bind(self.kernel.CreateEventW, [ptr, boolean, boolean, ct.c_wchar_p], ptr)
        self._bind(self.kernel.WaitForSingleObject, [ptr, u32], u32)
        self._bind(self.kernel.GetOverlappedResult, [ptr, overlap, ct.POINTER(u32), boolean], boolean)
        self._bind(self.kernel.CancelIoEx, [ptr, overlap], boolean)
        self._bind(self.kernel.DisconnectNamedPipe, [ptr], boolean)
        self._bind(self.kernel.CloseHandle, [ptr], boolean)
        self._bind(self.kernel.LocalFree, [ptr], ptr)
        self._bind(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW,
                   [ct.c_wchar_p, u32, ct.POINTER(ptr), ct.POINTER(u32)], boolean)

    @staticmethod
    def _bind(function, arguments, result) -> None:
        function.argtypes, function.restype = arguments, result

    def create_server(self, name, open_mode, pipe_mode, instances, buffer_bytes, sddl):
        descriptor = ct.c_void_p()
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ct.byref(descriptor), None):
            raise PipeError("Broker pipe security descriptor failed")
        try:
            attributes = _SecurityAttributes(ct.sizeof(_SecurityAttributes), descriptor, False)
            handle = self.kernel.CreateNamedPipeW(name, open_mode, pipe_mode, instances,
                buffer_bytes, buffer_bytes, 0, ct.byref(attributes))
            if handle is None or handle == _INVALID_HANDLE:
                raise PipeError("Broker pipe creation failed")
            return handle
        finally:
            self.kernel.LocalFree(descriptor)

    def open_client(self, name, access, flags):
        handle = self.kernel.CreateFileW(name, access, 0, None, 3, flags, None)
        return (None, ct.get_last_error()) if handle is None or handle == _INVALID_HANDLE else (handle, 0)

    def wait_pipe(self, name, milliseconds):
        return 0 if self.kernel.WaitNamedPipeW(name, milliseconds) else ct.get_last_error()

    def pipe_flags(self, handle):
        flags = ct.c_uint32()
        if not self.kernel.GetNamedPipeInfo(handle, ct.byref(flags), None, None, None):
            raise PipeError("Broker pipe metadata failed")
        return flags.value

    def event(self):
        handle = self.kernel.CreateEventW(None, True, False, None)
        if not handle:
            raise PipeError("Broker pipe event creation failed")
        return handle

    def start(self, operation, handle, pending):
        count = ct.c_uint32()
        if operation == "connect":
            ok = self.kernel.ConnectNamedPipe(handle, ct.byref(pending.overlapped))
        else:
            function = self.kernel.ReadFile if operation == "read" else self.kernel.WriteFile
            ok = function(handle, pending.buffer, pending.size, ct.byref(count), ct.byref(pending.overlapped))
        return (0, count.value) if ok else (ct.get_last_error(), count.value)

    def result(self, handle, pending):
        count = ct.c_uint32()
        ok = self.kernel.GetOverlappedResult(handle, ct.byref(pending.overlapped), ct.byref(count), False)
        return (0, count.value) if ok else (ct.get_last_error(), count.value)

    def wait(self, event, milliseconds):
        return self.kernel.WaitForSingleObject(event, milliseconds)

    def cancel(self, handle, pending):
        return 0 if self.kernel.CancelIoEx(handle, ct.byref(pending.overlapped)) else ct.get_last_error()

    def disconnect(self, handle):
        return 0 if self.kernel.DisconnectNamedPipe(handle) else ct.get_last_error()

    def close(self, handle):
        return bool(self.kernel.CloseHandle(handle))


@dataclass
class _Pending:
    overlapped: _Overlapped
    buffer: object
    size: int
    complete: bool = False


class _Endpoint:
    def __init__(self, handle: int, api) -> None:
        self._handle, self._api = handle, api
        self._lock = threading.RLock()
        self._pending: _Pending | None = None
        self._failed = False

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise PipeError("Broker pipe is closed")
        return self._handle

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def cleanup_pending(self) -> bool:
        return self._pending is not None

    @property
    def cleanup_confirmed(self) -> bool:
        return self._handle is None

    def _retain(self) -> None:
        self._failed = True
        if not any(owner is self for owner in _RETAINED):
            _RETAINED.append(self)

    def _forget(self) -> None:
        _RETAINED[:] = [owner for owner in _RETAINED if owner is not self]

    def _release_operation(self) -> None:
        pending = self._pending
        if pending is not None:
            try:
                closed = pending.complete and self._api.close(pending.overlapped.hEvent)
            except Exception:
                closed = False
            if not closed:
                self._retain()
                raise PipeCleanupError("Broker pipe event cleanup unconfirmed")
            self._pending = None

    def _cancel_pending(self) -> None:
        pending = self._pending
        if pending is None:
            return
        if not pending.complete:
            # Cancellation success, ERROR_NOT_FOUND, or a signaled event alone
            # never proves completion. Always ask GetOverlappedResult(FALSE).
            try:
                self._api.cancel(self.handle, pending)
                error, _ = self._api.result(self.handle, pending)
                if error in _PENDING:
                    self._api.wait(pending.overlapped.hEvent, 50)
                    error, _ = self._api.result(self.handle, pending)
                # Query failure (for example INVALID_HANDLE/ACCESS_DENIED) is
                # not evidence that the kernel released the OVERLAPPED buffer.
                pending.complete = error in _TERMINAL_COMPLETION
            except Exception:
                pending.complete = False
            if not pending.complete:
                self._retain()
                raise PipeCleanupError("Broker pipe cancellation completion unconfirmed")
        self._release_operation()

    def _io(self, operation: str, size: int, deadline: float, data: bytes | None = None) -> tuple[int, bytes]:
        if self._handle is None or self._failed or self._pending is not None:
            raise PipeError("Broker pipe is unavailable")
        if self._api.clock() >= deadline:
            raise PipeTimeoutError("Broker pipe deadline expired")
        buffer = ct.create_string_buffer(data, size) if data is not None else ct.create_string_buffer(size)
        pending = _Pending(_Overlapped(), buffer, size)
        event = self._api.event()
        pending.overlapped.hEvent = event
        self._pending = pending
        try:
            error, count = self._api.start(operation, self.handle, pending)
        except Exception:
            self._retain()  # An adapter exception cannot prove I/O was not issued.
            raise PipeCleanupError("Broker pipe operation ownership unconfirmed") from None
        if operation == "connect" and error == 535:  # ERROR_PIPE_CONNECTED: valid connection race.
            error = 0
        if error in _PENDING:
            try:
                waited = self._api.wait(event, _milliseconds(deadline, self._api))
                error, count = self._api.result(self.handle, pending)
            except Exception:
                self._retain()
                raise PipeCleanupError("Broker pipe completion ownership unconfirmed") from None
            if error not in _PENDING and error not in _TERMINAL_COMPLETION:
                self._retain()
                raise PipeCleanupError("Broker pipe completion status unconfirmed")
            if error in _PENDING or waited != 0:
                self._cancel_pending()
                if waited in (0, 258):
                    raise PipeTimeoutError("Broker pipe deadline expired")
                raise PipeError("Broker pipe completion wait failed")
        pending.complete = True
        payload = buffer.raw[:count] if operation == "read" and 0 <= count <= size else b""
        self._release_operation()
        if error in _EOF:
            raise PipeEOFError("Broker pipe closed")
        if error == 234:  # ERROR_MORE_DATA is impossible for the required byte pipe.
            raise PipeError("Broker pipe byte framing rejected")
        if error:
            raise PipeError("Broker pipe I/O failed")
        if operation != "connect" and not 0 < count <= size:
            if count == 0:
                raise PipeEOFError("Broker pipe closed")
            raise PipeError("Broker pipe transfer size rejected")
        return count, payload


class NativePipeConnection(_Endpoint):
    def __init__(self, handle: int, *, api=None, _server=None) -> None:
        if type(handle) is not int or handle <= 0 or handle == _INVALID_HANDLE:
            raise ValueError("Invalid broker pipe handle")
        super().__init__(handle, api if api is not None else _WindowsApi())
        self._server = _server

    def read_exact(self, size: int, timeout: float) -> bytes:
        if type(size) is not int or not 1 <= size <= MAX_IO_BYTES:
            raise ValueError("Invalid broker pipe read size")
        deadline = _deadline(timeout, self._api)
        with self._lock:
            received = bytearray()
            try:
                while len(received) < size:
                    _, data = self._io("read", size - len(received), deadline)
                    received.extend(data)
                return bytes(received)
            except PipeError:
                self._failed = True
                raise

    def write_all(self, data: bytes, timeout: float) -> None:
        if type(data) is not bytes or not 1 <= len(data) <= MAX_IO_BYTES:
            raise ValueError("Invalid broker pipe write size")
        deadline = _deadline(timeout, self._api)
        with self._lock:
            sent = 0
            try:
                while sent < len(data):
                    count, _ = self._io("write", len(data) - sent, deadline, data[sent:])
                    sent += count
            except PipeError:
                self._failed = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._cancel_pending()
            if self._server is not None:
                # Completed writes prove buffering, not peer receipt. Disconnect
                # discards unread output; the protocol must synchronize receipt
                # or wait for peer EOF before closing this server connection.
                self._server._release_connection(self)
            elif not self._api.close(self._handle):
                self._retain()
                raise PipeCleanupError("Broker pipe handle cleanup unconfirmed")
            self._handle = None
            self._forget()


class NativePipeServer(_Endpoint):
    def __init__(self, name: str, logon_sid: str, *, api=None) -> None:
        name, descriptor = _name(name), _sddl(logon_sid)
        api = api if api is not None else _WindowsApi()
        handle = api.create_server(name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                                   PIPE_REJECT_REMOTE_CLIENTS, 1, MAX_IO_BYTES, descriptor)
        super().__init__(handle, api)
        self._active: NativePipeConnection | None = None

    def accept(self, timeout: float) -> NativePipeConnection | None:
        deadline = _deadline(timeout, self._api)
        with self._lock:
            if self._active is not None:
                raise PipeError("Broker pipe already has an active connection")
            try:
                self._io("connect", 0, deadline)
            except PipeTimeoutError:
                # A client may have connected in the timeout/cancellation race.
                if self._api.disconnect(self.handle) not in (0, 233):
                    self._retain()
                    raise PipeCleanupError("Broker pipe disconnect cleanup unconfirmed") from None
                return None
            except PipeError:
                self._failed = True
                raise
            self._active = NativePipeConnection(self.handle, api=self._api, _server=self)
            return self._active

    def _release_connection(self, connection: NativePipeConnection) -> None:
        with self._lock:
            if self._active is not connection:
                raise PipeCleanupError("Broker pipe connection ownership mismatch")
            if self._api.disconnect(self.handle) not in (0, 233):
                connection._retain()
                raise PipeCleanupError("Broker pipe disconnect cleanup unconfirmed")
            self._active = None

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            if self._active is not None:
                raise PipeCleanupError("Close the active broker connection first")
            self._cancel_pending()
            if not self._api.close(self._handle):
                self._retain()
                raise PipeCleanupError("Broker pipe handle cleanup unconfirmed")
            self._handle = None
            self._forget()


class NativePipeClient:
    @staticmethod
    def connect(name: str, timeout: float, *, api=None) -> NativePipeConnection:
        name = _name(name)
        api = api if api is not None else _WindowsApi()
        deadline = _deadline(timeout, api)
        while api.clock() < deadline:
            handle, error = api.open_client(name, CLIENT_ACCESS,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION)
            if handle is not None:
                connection = NativePipeConnection(handle, api=api)
                try:
                    if api.pipe_flags(handle) & 4:  # PIPE_TYPE_MESSAGE
                        raise PipeError("Broker pipe byte framing rejected")
                except Exception:
                    try:
                        connection.close()
                    except PipeCleanupError as error:
                        error.cleanup_owner = connection
                        raise
                    raise PipeError("Broker pipe metadata rejected") from None
                return connection
            if error not in (2, 231):  # Not yet created, or ERROR_PIPE_BUSY.
                raise PipeError("Broker pipe connection failed")
            remaining = _milliseconds(deadline, api)
            if not remaining:
                break
            waited = api.wait_pipe(name, remaining)
            if waited not in (0, 2, 121, 231):
                raise PipeError("Broker pipe connection wait failed")
            if waited == 2:
                api.sleep(min(0.005, max(0, deadline - api.clock())))
        raise PipeTimeoutError("Broker pipe connection deadline expired")
