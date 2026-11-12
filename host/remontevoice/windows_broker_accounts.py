"""Read-only account policy for a per-user, linked-UAC broker composition.

No credentials, elevation, process launch, service or device access. Only a
Windows-observed Full/Limited token pair is supported. SYSTEM/service accounts
and session zero are deliberately unsupported. Exact logon SIDs remain separate.
The caller still must authenticate both protected executable/runtime packages.
"""
from __future__ import annotations

import ctypes as ct
import re
from dataclasses import dataclass

from .mic_broker import PeerIdentity
from .windows_broker_identity import _WindowsApi, _SidAndAttributes, _TokenGroups

_HANDLE = ct.c_void_p
_DWORD = ct.c_uint32
_INVALID_HANDLE = ct.c_void_p(-1).value
_ADMINISTRATORS = "S-1-5-32-544"
_SERVICE_USERS = frozenset(("S-1-5-18", "S-1-5-19", "S-1-5-20"))


class BrokerAccountError(RuntimeError):
    """Unsupported account policy or unconfirmed native cleanup; no SID details."""
    def __init__(self, message="Linked Windows broker account policy is unavailable", *, cleanup_owner=None):
        super().__init__(message)
        self.cleanup_owner = cleanup_owner


@dataclass(frozen=True, repr=False)
class AccountIdentity:
    user_sid: str
    logon_sid: str
    session_id: int
    elevation_type: int  # TOKEN_ELEVATION_TYPE: Default=1, Full=2, Limited=3.
    elevated: bool
    administrator_enabled: bool
    administrator_deny_only: bool

    def __post_init__(self):
        PeerIdentity(self.user_sid, self.logon_sid, self.session_id, "0" * 64)
        if (not re.fullmatch(r"S-1-5-5-\d+-\d+", self.logon_sid, re.ASCII)
                or type(self.elevation_type) is not int or self.elevation_type not in (1, 2, 3)
                or any(type(value) is not bool for value in
                       (self.elevated, self.administrator_enabled, self.administrator_deny_only))
                or self.administrator_enabled and self.administrator_deny_only):
            raise ValueError("Invalid Windows account observation")


@dataclass(frozen=True, repr=False)
class BrokerAccountPair:
    broker: AccountIdentity
    client: AccountIdentity
    current_is_broker: bool


class _TokenCleanup:
    def __init__(self, api):
        self.api = api
        self.handles = []

    @property
    def cleanup_confirmed(self) -> bool:
        """Cached ownership only; no Windows/token observation."""
        return not self.handles

    def close(self):
        errors, pending = [], []
        for handle in reversed(self.handles):
            try:
                self.api.close_handle(handle)
            except BaseException as error:
                pending.append(handle)
                errors.append(error)
        self.handles = list(reversed(pending))
        fatal = next((error for error in errors if not isinstance(error, Exception)), None)
        if fatal is not None:
            raise fatal
        if errors:
            raise BrokerAccountError("Windows account token cleanup is unconfirmed", cleanup_owner=self)


def _roles(current: AccountIdentity, linked: AccountIdentity) -> BrokerAccountPair:
    if (type(current) is not AccountIdentity or type(linked) is not AccountIdentity
            or current.user_sid in _SERVICE_USERS or linked.user_sid in _SERVICE_USERS
            or not current.session_id or current.session_id != linked.session_id
            or current.user_sid != linked.user_sid):
        raise BrokerAccountError()

    def full(value):
        return (value.elevation_type == 2 and value.elevated
                and value.administrator_enabled and not value.administrator_deny_only)

    def limited(value):
        return (value.elevation_type == 3 and not value.elevated
                and not value.administrator_enabled and value.administrator_deny_only)

    if full(current) and limited(linked):
        return BrokerAccountPair(current, linked, True)
    if limited(current) and full(linked):
        return BrokerAccountPair(linked, current, False)
    raise BrokerAccountError()


def observe_broker_accounts(*, api=None) -> BrokerAccountPair:
    """Observe current token and its actual Windows-linked counterpart once.

    There are no caller-supplied identity claims. An injected api is a trusted
    testing/native boundary, not a pipe/CLI extension point. Cleanup failures
    expose cleanup_owner.close() for retry; never admit based on a partial result.
    """
    owner = None
    result = None
    primary = None
    try:
        api = api if api is not None else _AccountApi()
        owner = _TokenCleanup(api)
        current = api.open_current_token()
        if type(current) is not int or not 0 < current < (1 << (ct.sizeof(_HANDLE) * 8)) or current == _INVALID_HANDLE:
            raise BrokerAccountError()
        owner.handles.append(current)
        linked = api.linked_token(current)
        if (type(linked) is not int or not 0 < linked < (1 << (ct.sizeof(_HANDLE) * 8))
                or linked == _INVALID_HANDLE or linked == current):
            raise BrokerAccountError()
        owner.handles.append(linked)
        result = _roles(api.read_account(current), api.read_account(linked))
    except BaseException as error:
        primary = error
    cleanup_error = None
    if owner is not None:
        try:
            owner.close()
        except BaseException as error:
            cleanup_error = error
    for error in (primary, cleanup_error):
        if error is not None and not isinstance(error, Exception):
            if owner is not None and owner.handles:
                try:
                    error.cleanup_owner = owner
                except Exception:
                    pass
            raise error
    if cleanup_error is not None:
        raise cleanup_error from None
    if primary is not None or result is None:
        raise BrokerAccountError() from None
    return result


class _AccountApi(_WindowsApi):
    def __init__(self):
        super().__init__()
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentProcess.restype = _HANDLE

    def open_current_token(self):
        token = _HANDLE()
        self._check(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ct.byref(token)))
        return token.value

    def _fixed_token_info(self, token, kind, size):
        # TokenLinkedToken/TokenElevation require their exact fixed-size buffer;
        # a zero-size sizing request can return ERROR_BAD_LENGTH rather than 122.
        information, returned = ct.create_string_buffer(size), _DWORD()
        self._check(self.security.GetTokenInformation(token, kind, information, size, ct.byref(returned)))
        if returned.value != size:
            raise BrokerAccountError()
        return information

    def linked_token(self, token):
        information = self._fixed_token_info(token, 19, ct.sizeof(_HANDLE))
        return _HANDLE.from_buffer(information).value

    def read_account(self, token):
        identity = self._identity_from_token(token)
        kind, elevation = self._fixed_token_info(token, 18, 4), self._fixed_token_info(token, 20, 4)
        elevated = _DWORD.from_buffer(elevation).value
        if elevated not in (0, 1):
            raise BrokerAccountError()
        groups = self._token_info(token, 2)
        count, offset, stride = _DWORD.from_buffer(groups).value, _TokenGroups.groups.offset, ct.sizeof(_SidAndAttributes)
        if count > 4096 or offset + count * stride > len(groups):
            raise BrokerAccountError()
        administrator = []
        for index in range(count):
            group = _SidAndAttributes.from_buffer(groups, offset + index * stride)
            sid = self._sid_text(group.sid, ct.addressof(groups), ct.addressof(groups) + len(groups))
            if sid == _ADMINISTRATORS:
                administrator.append(group.attributes)
        if len(administrator) > 1:
            raise BrokerAccountError()
        attributes = administrator[0] if administrator else 0
        return AccountIdentity(identity.user_sid, identity.logon_sid, identity.session_id,
                               _DWORD.from_buffer(kind).value, bool(elevated),
                               bool(attributes & 0x04), bool(attributes & 0x10))
