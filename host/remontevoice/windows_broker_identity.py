"""Windows pipe peer verification; inert until explicitly called.

The pipe owner must reject remote clients and serialize read, verification and
disconnect. Server-side calls MUST follow a successful read: pipe impersonation
describes the last message read, not a claimed identity in that message.

Pins must name a dedicated protected executable/runtime. Pinning python.exe does
not authenticate its script, arguments, DLL search path or imported modules.
Same-user/logon handle borrowing is not distinguished by Windows pipe tokens.
This module alone therefore does not certify the current Python Host application.

The ACL policy checks every ancestor, including the volume root. Specific
add-subdirectory rights on an ancestor are permitted: they cannot replace the
existing next component, whose own trusted owner/ACL is independently checked.
No ACL is changed. A binding retains process/image/directory handles until
forget_pipe_peer() or close(). Reversion failure is fatal to the dedicated
process: its owner must terminate rather than resume serving under that token.
"""
from __future__ import annotations

import ctypes as ct
import hashlib
import hmac
import ntpath
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from .mic_broker import PeerIdentity

_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_TRUSTED_INSTALLER = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
_TRUSTED_OWNERS = frozenset((_SYSTEM, _ADMINISTRATORS, _TRUSTED_INSTALLER))
_MUTATION = 0x10000000 | 0x40000000 | 0x000D0000 | 0x00000156
_INVALID_HANDLE = ct.c_void_p(-1).value
_DWORD, _BOOL, _HANDLE = ct.c_uint32, ct.c_int32, ct.c_void_p


class PeerVerificationError(RuntimeError):
    """Authentication rejected without exposing native SID/path/error details."""


class BrokerIdentityFatalError(BaseException):
    """Reversion failed; terminate the dedicated process, do not catch as auth."""


@dataclass(frozen=True, repr=False)
class _TokenIdentity:
    user_sid: str
    logon_sid: str
    session_id: int


@dataclass(frozen=True, repr=False)
class _Ace:
    kind: int
    flags: int
    mask: int
    sid: str


@dataclass(frozen=True, repr=False)
class _Security:
    owner: str
    dacl_present: bool
    aces: tuple[_Ace, ...]


@dataclass(frozen=True, repr=False)
class _FileInfo:
    path: str
    directory: bool
    reparse: bool


@dataclass(repr=False)
class _Binding:
    pid: int
    identity: PeerIdentity
    process: int
    handles: list[int]
    server_side: bool


def _canonical_path(path: str) -> str:
    # Reject aliases, device/UNC paths, relative paths, ADS and ambiguous Win32
    # trailing-dot/space normalization. Native final paths undergo the same check.
    if type(path) is not str or not path or "/" in path or "\0" in path:
        raise ValueError("Expected canonical absolute executable path")
    drive, tail = ntpath.splitdrive(path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
        raise ValueError("Expected canonical absolute executable path")
    parts = tail[1:].split("\\")
    if any(not part or part in (".", "..") or part[-1:] in (" ", ".")
           or ":" in part for part in parts):
        # The volume root is valid for ancestor comparison only.
        if tail != "\\":
            raise ValueError("Expected canonical absolute executable path")
    return (drive + tail).casefold()


def _components(path: str) -> list[str]:
    drive, tail = ntpath.splitdrive(path)
    result = [drive + "\\"]
    for part in tail[1:].split("\\"):
        result.append(ntpath.join(result[-1], part))
    return result


def _protected(security: _Security, *, ancestor: bool = False) -> bool:
    if type(security) is not _Security or security.owner not in _TRUSTED_OWNERS or not security.dacl_present:
        return False
    for ace in security.aces:
        if type(ace) is not _Ace:
            return False
        # Inherit-only does not apply to this object; descendants are checked.
        if ace.flags & 0x08:
            continue
        if ace.kind == 1:  # ACCESS_DENIED_ACE; never use a deny to cancel an allow.
            continue
        if ace.kind != 0:  # Object/callback/unknown ACE formats fail closed.
            return False
        mutation = ace.mask & _MUTATION
        if ancestor:
            # FILE_ADD_SUBDIRECTORY only creates a new sibling. Every existing
            # next component is pinned by a deny-delete-shared handle and checked
            # independently. DELETE_CHILD and DELETE remain forbidden. On the
            # executable leaf this same bit means FILE_APPEND_DATA and is denied.
            # Generic WRITE/ALL retain their mutation bits and are never relaxed.
            mutation &= ~0x04
        if mutation and ace.sid not in _TRUSTED_OWNERS:
            return False
    return True


def _pin_protected_components(api, path: str, handles: list[int]) -> int:
    """Pin a protected regular file before reading its contents or hash."""
    roots = tuple(_canonical_path(root) for root in api.installation_roots())
    if not any(path.startswith(root.rstrip("\\") + "\\") for root in roots):
        raise PeerVerificationError("Protected installation verification rejected")
    components = _components(path)
    for index, component in enumerate(components):
        directory = index < len(components) - 1
        handle = api.open_image_component(component, directory=directory)
        handles.append(handle)
        info = api.file_info(handle)
        if (type(info) is not _FileInfo or info.reparse or info.directory != directory
                or _canonical_path(info.path) != component
                or not _protected(api.file_security(handle), ancestor=directory)):
            raise PeerVerificationError("Protected installation verification rejected")
    return handles[-1]


def _pin_protected_file(api, path: str, digest: str, handles: list[int]) -> None:
    """Shared image/runtime pinning; caller retains handles even on failure."""
    _pin_protected_components(api, path, handles)
    actual_digest = api.hash_image(handles[-1])
    if type(actual_digest) is not str or not hmac.compare_digest(actual_digest, digest):
        raise PeerVerificationError("Protected installation verification rejected")


class WindowsPeerVerifier:
    def __init__(self, expected_user_sid: str, expected_logon_sid: str,
                 expected_session_id: int, trusted_images: Mapping[str, str], api=None) -> None:
        expected = PeerIdentity(expected_user_sid, expected_logon_sid, expected_session_id, "0" * 64)
        if not re.fullmatch(r"S-1-5-5-\d+-\d+", expected.logon_sid, re.ASCII):
            raise ValueError("Expected an explicit logon SID")
        if not isinstance(trusted_images, Mapping) or not trusted_images:
            raise ValueError("Explicit protected executable pins are required")
        images = {}
        for path, digest in trusted_images.items():
            canonical = _canonical_path(path)
            if canonical.endswith("\\") or canonical in images or type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid protected executable pin")
            images[canonical] = digest
        self._expected = _TokenIdentity(expected.user_sid, expected.logon_sid, expected.session_id)
        self._images, self._api = images, api
        self._bindings: dict[int, _Binding] = {}
        self._pending_close: list[int] = []
        self._poisoned = False
        self._lock = threading.RLock()

    def _native(self):
        if self._api is None:
            try:
                self._api = _WindowsApi()
            except Exception:
                raise PeerVerificationError("Windows peer verification is unavailable") from None
        return self._api

    @staticmethod
    def _handle(handle: int) -> None:
        if type(handle) is not int or not 0 < handle < (1 << (ct.sizeof(_HANDLE) * 8)) or handle == _INVALID_HANDLE:
            raise PeerVerificationError("Peer authentication rejected")

    def _revert(self, api) -> None:
        try:
            if api.revert_to_self() is not True:
                raise RuntimeError()
        except BaseException:
            self._poisoned = True
            raise BrokerIdentityFatalError("Windows impersonation cleanup failed; terminate process") from None

    def _observe(self, api, pipe_handle: int, process: int, server_side: bool) -> _TokenIdentity:
        # Always revert on server-side failure, including failure before or within
        # impersonation. Never inspect an image while still impersonating a client.
        try:
            actual = api.process_token_identity(process)
            if type(actual) is not _TokenIdentity or actual != self._expected:
                raise PeerVerificationError("Peer authentication rejected")
            if server_side:
                api.impersonate_pipe_client(pipe_handle)
                if api.thread_token_identity() != actual:
                    raise PeerVerificationError("Peer authentication rejected")
            return actual
        finally:
            if server_side:
                self._revert(api)

    def _close_handles(self, api, handles: list[int]) -> None:
        failed = []
        for handle in reversed(handles):
            try:
                api.close_handle(handle)
            except Exception:
                failed.append(handle)
        handles.clear()
        if failed:
            self._pending_close.extend(failed)
            raise PeerVerificationError("Windows authentication cleanup unconfirmed")

    def _verify_image(self, api, process: int, handles: list[int]) -> str:
        path = _canonical_path(api.process_image_path(process))
        digest = self._images.get(path)
        if digest is None:
            raise PeerVerificationError("Peer authentication rejected")
        _pin_protected_file(api, path, digest, handles)
        return digest

    def verify_pipe_peer(self, pipe_handle: int, *, server_side: bool) -> PeerIdentity:
        """Authenticate after initial read; retain binding until forget_pipe_peer."""
        self._handle(pipe_handle)
        if type(server_side) is not bool:
            raise PeerVerificationError("Peer authentication rejected")
        with self._lock:
            if self._poisoned:
                raise BrokerIdentityFatalError("Windows identity verifier is unusable")
            if self._pending_close:
                raise PeerVerificationError("Windows authentication cleanup unconfirmed")
            api = self._native()
            existing = self._bindings.get(pipe_handle)
            if existing is not None:
                if not self.validate_pipe_token(pipe_handle, existing.identity, server_side=server_side):
                    raise PeerVerificationError("Peer authentication rejected")
                return existing.identity
            handles: list[int] = []
            observed = False
            try:
                pid = api.pipe_peer_pid(pipe_handle, server_side=server_side)
                if type(pid) is not int or not 0 < pid < 2**32:
                    raise PeerVerificationError("Peer authentication rejected")
                process = api.open_process(pid)
                handles.append(process)
                observed = True
                actual = self._observe(api, pipe_handle, process, server_side)
                digest = self._verify_image(api, process, handles)
                if (not api.process_alive(process)
                        or api.pipe_peer_pid(pipe_handle, server_side=server_side) != pid):
                    raise PeerVerificationError("Peer authentication rejected")
                identity = PeerIdentity(actual.user_sid, actual.logon_sid, actual.session_id, digest)
                self._bindings[pipe_handle] = _Binding(pid, identity, process, handles, server_side)
                handles = []
                return identity
            except BrokerIdentityFatalError:
                raise
            except Exception:
                raise PeerVerificationError("Peer authentication rejected") from None
            finally:
                try:
                    if server_side and not observed:
                        self._revert(api)
                finally:
                    try:
                        self._close_handles(api, handles)
                    except PeerVerificationError:
                        if self._poisoned:
                            raise BrokerIdentityFatalError("Windows impersonation cleanup failed; terminate process") from None
                        raise

    def validate_pipe_token(self, pipe_handle: int, expected: PeerIdentity, *, server_side: bool = True) -> bool:
        """After EACH read, validate the retained PID/token binding without hashing."""
        self._handle(pipe_handle)
        with self._lock:
            if self._poisoned:
                raise BrokerIdentityFatalError("Windows identity verifier is unusable")
            api = self._native()
            observed = False
            try:
                binding = self._bindings.get(pipe_handle)
                if (type(expected) is not PeerIdentity or binding is None or type(server_side) is not bool
                        or binding.identity != expected or binding.server_side != server_side):
                    return False
                if (api.pipe_peer_pid(pipe_handle, server_side=server_side) != binding.pid
                        or not api.process_alive(binding.process)):
                    return False
                observed = True
                self._observe(api, pipe_handle, binding.process, server_side)
                return api.pipe_peer_pid(pipe_handle, server_side=server_side) == binding.pid
            except BrokerIdentityFatalError:
                raise
            except Exception:
                return False
            finally:
                if server_side and not observed:
                    self._revert(api)

    def forget_pipe_peer(self, pipe_handle: int) -> None:
        """The pipe owner must call this before closing/reusing a pipe handle."""
        with self._lock:
            binding = self._bindings.pop(pipe_handle, None)
            handles, self._pending_close = self._pending_close, []
            if binding is not None:
                handles.extend(binding.handles)
            if handles:
                self._close_handles(self._native(), handles)

    def close(self) -> None:
        with self._lock:
            api = self._api
            if api is None:
                return
            handles, self._pending_close = self._pending_close, []
            for binding in self._bindings.values():
                handles.extend(binding.handles)
            self._bindings.clear()
            self._close_handles(api, handles)


class _SidAndAttributes(ct.Structure):
    _fields_ = [("sid", ct.c_void_p), ("attributes", _DWORD)]


class _TokenGroups(ct.Structure):
    _fields_ = [("count", _DWORD), ("groups", _SidAndAttributes * 1)]


class _FileInformation(ct.Structure):
    _fields_ = [("attributes", _DWORD), ("creation", _DWORD * 2), ("access", _DWORD * 2),
                ("write", _DWORD * 2), ("volume", _DWORD), ("size_high", _DWORD),
                ("size_low", _DWORD), ("links", _DWORD), ("index_high", _DWORD), ("index_low", _DWORD)]


class _AclSizeInformation(ct.Structure):
    _fields_ = [("count", _DWORD), ("used", _DWORD), ("free", _DWORD)]


class _WindowsApi:
    """Small pointer-width-correct Win32 adapter, loaded only on explicit use."""
    def __init__(self) -> None:
        if os.name != "nt":
            raise PeerVerificationError("Windows peer verification is unavailable")
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.security = ct.WinDLL("advapi32", use_last_error=True)
        ptr, u32p, handlep = ct.c_void_p, ct.POINTER(_DWORD), ct.POINTER(_HANDLE)
        bindings = (
            (self.kernel, "GetNamedPipeClientProcessId", [_HANDLE, u32p], _BOOL),
            (self.kernel, "GetNamedPipeServerProcessId", [_HANDLE, u32p], _BOOL),
            (self.kernel, "OpenProcess", [_DWORD, _BOOL, _DWORD], _HANDLE),
            (self.kernel, "GetCurrentThread", [], _HANDLE),
            (self.kernel, "CloseHandle", [_HANDLE], _BOOL),
            (self.kernel, "LocalFree", [ptr], ptr),
            (self.kernel, "GetExitCodeProcess", [_HANDLE, u32p], _BOOL),
            (self.kernel, "WaitForSingleObject", [_HANDLE, _DWORD], _DWORD),
            (self.kernel, "QueryFullProcessImageNameW", [_HANDLE, _DWORD, ct.c_wchar_p, u32p], _BOOL),
            (self.kernel, "CreateFileW", [ct.c_wchar_p, _DWORD, _DWORD, ptr, _DWORD, _DWORD, _HANDLE], _HANDLE),
            (self.kernel, "GetFileInformationByHandle", [_HANDLE, ct.POINTER(_FileInformation)], _BOOL),
            (self.kernel, "GetFinalPathNameByHandleW", [_HANDLE, ct.c_wchar_p, _DWORD, _DWORD], _DWORD),
            (self.kernel, "ReadFile", [_HANDLE, ptr, _DWORD, u32p, ptr], _BOOL),
            (self.kernel, "GetWindowsDirectoryW", [ct.c_wchar_p, _DWORD], _DWORD),
            (self.security, "OpenProcessToken", [_HANDLE, _DWORD, handlep], _BOOL),
            (self.security, "OpenThreadToken", [_HANDLE, _DWORD, _BOOL, handlep], _BOOL),
            (self.security, "GetTokenInformation", [_HANDLE, _DWORD, ptr, _DWORD, u32p], _BOOL),
            (self.security, "ImpersonateNamedPipeClient", [_HANDLE], _BOOL),
            (self.security, "RevertToSelf", [], _BOOL),
            (self.security, "IsValidSid", [ptr], _BOOL),
            (self.security, "ConvertSidToStringSidW", [ptr, ct.POINTER(ct.c_wchar_p)], _BOOL),
            (self.security, "GetSecurityInfo", [_HANDLE, _DWORD, _DWORD, ct.POINTER(ptr), ct.POINTER(ptr),
                                               ct.POINTER(ptr), ct.POINTER(ptr), ct.POINTER(ptr)], _DWORD),
            (self.security, "GetSecurityDescriptorDacl", [ptr, ct.POINTER(_BOOL), ct.POINTER(ptr), ct.POINTER(_BOOL)], _BOOL),
            (self.security, "GetAclInformation", [ptr, ptr, _DWORD, _DWORD], _BOOL),
            (self.security, "GetAce", [ptr, _DWORD, ct.POINTER(ptr)], _BOOL),
        )
        for library, name, arguments, result in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _check(value) -> None:
        if not value:
            raise PeerVerificationError("Windows identity observation failed")

    def close_handle(self, handle: int) -> None:
        self._check(self.kernel.CloseHandle(handle))

    def pipe_peer_pid(self, handle: int, *, server_side: bool) -> int:
        pid = _DWORD()
        function = (self.kernel.GetNamedPipeClientProcessId if server_side
                    else self.kernel.GetNamedPipeServerProcessId)
        self._check(function(handle, ct.byref(pid)))
        return pid.value

    def open_process(self, pid: int) -> int:
        process = self.kernel.OpenProcess(0x101000, False, pid)  # QUERY_LIMITED_INFORMATION|SYNCHRONIZE.
        if process is None or process == _INVALID_HANDLE:
            raise PeerVerificationError("Windows identity observation failed")
        return process

    def process_alive(self, process: int) -> bool:
        state = self.kernel.WaitForSingleObject(process, 0)
        if state not in (0, 258):
            raise PeerVerificationError("Windows process liveness observation failed")
        return state == 258  # Exit code259 must not make a dead process look alive.

    def _token_info(self, token: int, kind: int):
        required = _DWORD()
        result = self.security.GetTokenInformation(token, kind, None, 0, ct.byref(required))
        if result or ct.get_last_error() != 122 or not 4 <= required.value <= 1024 * 1024:
            raise PeerVerificationError("Windows identity observation failed")
        buffer = ct.create_string_buffer(required.value)
        self._check(self.security.GetTokenInformation(token, kind, buffer, len(buffer), ct.byref(required)))
        if required.value > len(buffer):
            raise PeerVerificationError("Windows identity observation failed")
        return buffer

    def _sid_text(self, sid: int, start: int = 0, end: int = 0) -> str:
        if not sid or (end and not start <= sid <= end - 8):
            raise PeerVerificationError("Windows identity observation failed")
        if end:
            length = 8 + 4 * ct.c_ubyte.from_address(sid + 1).value
            if sid + length > end:
                raise PeerVerificationError("Windows identity observation failed")
        self._check(self.security.IsValidSid(sid))
        text = ct.c_wchar_p()
        self._check(self.security.ConvertSidToStringSidW(sid, ct.byref(text)))
        try:
            return text.value
        finally:
            if self.kernel.LocalFree(ct.cast(text, ct.c_void_p)):
                raise PeerVerificationError("Windows identity cleanup unconfirmed")

    def _identity_from_token(self, token: int) -> _TokenIdentity:
        user = self._token_info(token, 1)  # TokenUser.
        if len(user) < ct.sizeof(_SidAndAttributes):
            raise PeerVerificationError("Windows identity observation failed")
        user_sid = self._sid_text(_SidAndAttributes.from_buffer(user).sid,
                                  ct.addressof(user), ct.addressof(user) + len(user))
        groups = self._token_info(token, 2)  # TokenGroups.
        count = _DWORD.from_buffer(groups).value
        offset, stride = _TokenGroups.groups.offset, ct.sizeof(_SidAndAttributes)
        if count > 4096 or offset + count * stride > len(groups):
            raise PeerVerificationError("Windows identity observation failed")
        logons = []
        for index in range(count):
            group = _SidAndAttributes.from_buffer(groups, offset + index * stride)
            if group.attributes & 0xC0000000 == 0xC0000000:  # SE_GROUP_LOGON_ID.
                logons.append(self._sid_text(group.sid, ct.addressof(groups), ct.addressof(groups) + len(groups)))
        session = self._token_info(token, 12)  # TokenSessionId.
        if len(logons) != 1 or len(session) != 4 or not re.fullmatch(r"S-1-5-5-\d+-\d+", logons[0], re.ASCII):
            raise PeerVerificationError("Windows identity observation failed")
        return _TokenIdentity(user_sid, logons[0], _DWORD.from_buffer(session).value)

    def process_token_identity(self, process: int) -> _TokenIdentity:
        token = _HANDLE()
        self._check(self.security.OpenProcessToken(process, 0x0008, ct.byref(token)))
        try:
            return self._identity_from_token(token.value)
        finally:
            self.close_handle(token.value)

    def impersonate_pipe_client(self, pipe: int) -> None:
        self._check(self.security.ImpersonateNamedPipeClient(pipe))

    def thread_token_identity(self) -> _TokenIdentity:
        token = _HANDLE()
        # OpenAsSelf permits querying an Identification-level client token.
        self._check(self.security.OpenThreadToken(self.kernel.GetCurrentThread(), 0x0008, True, ct.byref(token)))
        try:
            return self._identity_from_token(token.value)
        finally:
            self.close_handle(token.value)

    def revert_to_self(self) -> bool:
        return bool(self.security.RevertToSelf())

    def process_image_path(self, process: int) -> str:
        text = ct.create_unicode_buffer(32768)
        length = _DWORD(len(text))
        self._check(self.kernel.QueryFullProcessImageNameW(process, 0, text, ct.byref(length)))
        if not 0 < length.value < len(text):
            raise PeerVerificationError("Windows identity observation failed")
        return text[:length.value]

    def installation_roots(self) -> tuple[str, ...]:
        import winreg  # Lazy Windows-only registry reads; never trust environment.
        text = ct.create_unicode_buffer(32768)
        length = self.kernel.GetWindowsDirectoryW(text, len(text))
        if not 0 < length < len(text):
            raise PeerVerificationError("Windows identity observation failed")
        roots = [text.value]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion",
                            0, winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
            for name in ("ProgramFilesDir", "ProgramFilesDir (x86)"):
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if kind != winreg.REG_SZ:
                    raise PeerVerificationError("Windows identity observation failed")
                roots.append(value)
        return tuple(roots)

    def open_image_component(self, path: str, *, directory: bool) -> int:
        # File handle denies both WRITE and DELETE sharing. Directories deny
        # DELETE sharing to pin the checked path while normal system reads run.
        access = 0x00020080 | (0 if directory else 0x80000000)  # READ_CONTROL|ATTRIBUTES|READ.
        sharing = 3 if directory else 1
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT|BACKUP_SEMANTICS.
        handle = self.kernel.CreateFileW(path, access, sharing, None, 3, flags, None)
        if handle is None or handle == _INVALID_HANDLE:
            raise PeerVerificationError("Windows image verification failed")
        return handle

    def file_info(self, handle: int) -> _FileInfo:
        info = _FileInformation()
        self._check(self.kernel.GetFileInformationByHandle(handle, ct.byref(info)))
        text = ct.create_unicode_buffer(32768)
        length = self.kernel.GetFinalPathNameByHandleW(handle, text, len(text), 0)
        if not 0 < length < len(text) or not text.value.startswith("\\\\?\\"):
            raise PeerVerificationError("Windows image verification failed")
        return _FileInfo(text.value[4:], bool(info.attributes & 0x10), bool(info.attributes & 0x400))

    def file_security(self, handle: int) -> _Security:
        owner, dacl, descriptor = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
        result = self.security.GetSecurityInfo(handle, 1, 0x00000005, ct.byref(owner), None,
                                               ct.byref(dacl), None, ct.byref(descriptor))
        if result or not descriptor.value:
            raise PeerVerificationError("Windows image verification failed")
        try:
            owner_sid = self._sid_text(owner.value)
            present, defaulted, actual_dacl = _BOOL(), _BOOL(), ct.c_void_p()
            self._check(self.security.GetSecurityDescriptorDacl(descriptor, ct.byref(present),
                                                                 ct.byref(actual_dacl), ct.byref(defaulted)))
            if not present.value or not actual_dacl.value:
                return _Security(owner_sid, False, ())
            size = _AclSizeInformation()
            self._check(self.security.GetAclInformation(actual_dacl, ct.byref(size), ct.sizeof(size), 2))
            if size.count > 65535 or size.used < 8 or size.used > 65535:
                raise PeerVerificationError("Windows image verification failed")
            aces = []
            for index in range(size.count):
                ace = ct.c_void_p()
                self._check(self.security.GetAce(actual_dacl, index, ct.byref(ace)))
                start, end = actual_dacl.value, actual_dacl.value + size.used
                if not start <= ace.value <= end - 4:
                    raise PeerVerificationError("Windows image verification failed")
                kind, flags = ct.c_ubyte.from_address(ace.value).value, ct.c_ubyte.from_address(ace.value + 1).value
                length = ct.c_uint16.from_address(ace.value + 2).value
                if length < 4 or ace.value + length > end:
                    raise PeerVerificationError("Windows image verification failed")
                if kind not in (0, 1):
                    aces.append(_Ace(kind, flags, 0, ""))
                    continue
                if length < 16:
                    raise PeerVerificationError("Windows image verification failed")
                mask = _DWORD.from_address(ace.value + 4).value
                sid = self._sid_text(ace.value + 8, ace.value + 8, ace.value + length)
                aces.append(_Ace(kind, flags, mask, sid))
            return _Security(owner_sid, True, tuple(aces))
        finally:
            if self.kernel.LocalFree(descriptor):
                raise PeerVerificationError("Windows identity cleanup unconfirmed")

    def hash_image(self, handle: int) -> str:
        digest = hashlib.sha256()
        buffer, count = ct.create_string_buffer(65536), _DWORD()
        while True:
            self._check(self.kernel.ReadFile(handle, buffer, len(buffer), ct.byref(count), None))
            if count.value > len(buffer):
                raise PeerVerificationError("Windows image verification failed")
            if not count.value:
                return digest.hexdigest()
            digest.update(buffer.raw[:count.value])


def read_current_process_token() -> _TokenIdentity:
    """Read-only native smoke helper; does not admit an image or access a pipe."""
    api = _WindowsApi()
    process = api.open_process(os.getpid())
    try:
        return api.process_token_identity(process)
    finally:
        api.close_handle(process)
