"""Private IPv4 addresses the phone can plausibly reach.

Windows hostname resolution lists every adapter, including VMware/VirtualBox
host-only networks that a phone can never reach.  GetAdaptersInfo reports the
adapter type, description, gateway and addresses, so physical LAN/Wi-Fi
adapters (and the Windows mobile hotspot adapter) can be offered while
host-only virtual networks are skipped.  Adapters that already have a default
gateway are listed first; hotspot/secondary adapters follow.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
from ctypes import wintypes

_PHYSICAL_TYPES = {6, 71}  # IF_TYPE_ETHERNET_CSMACD, IF_TYPE_IEEE80211
_EXCLUDED_DESCRIPTIONS = (
    "vmware", "virtualbox", "hyper-v", "loopback", "tap-windows", "teredo",
    "bluetooth", "wan miniport", "npcap", "wireguard", "tailscale", "zerotier",
    "vpn", "pseudo-interface",
)


class _IpAddressString(ctypes.Structure):
    pass


_IpAddressString._fields_ = [
    ("Next", ctypes.POINTER(_IpAddressString)),
    ("IpAddress", ctypes.c_char * 16),
    ("IpMask", ctypes.c_char * 16),
    ("Context", wintypes.DWORD),
]


class _IpAdapterInfo(ctypes.Structure):
    pass


_IpAdapterInfo._fields_ = [
    ("Next", ctypes.POINTER(_IpAdapterInfo)),
    ("ComboIndex", wintypes.DWORD),
    ("AdapterName", ctypes.c_char * 260),
    ("Description", ctypes.c_char * 132),
    ("AddressLength", wintypes.UINT),
    ("Address", ctypes.c_ubyte * 8),
    ("Index", wintypes.DWORD),
    ("Type", wintypes.UINT),
    ("DhcpEnabled", wintypes.UINT),
    ("CurrentIpAddress", ctypes.POINTER(_IpAddressString)),
    ("IpAddressList", _IpAddressString),
    ("GatewayList", _IpAddressString),
    ("DhcpServer", _IpAddressString),
    ("HaveWins", wintypes.BOOL),
    ("PrimaryWinsServer", _IpAddressString),
    ("SecondaryWinsServer", _IpAddressString),
    ("LeaseObtained", wintypes.DWORD),
    ("LeaseExpires", wintypes.DWORD),
]


def _iphlpapi():
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows IP helper API is unavailable")
    return loader("iphlpapi")


def _text(raw) -> str:
    data = bytes(raw).split(b"\x00", 1)[0]
    try:
        return data.decode("mbcs")
    except (LookupError, UnicodeDecodeError):
        return data.decode("latin-1", "replace")


def _adapter_rows() -> list[dict]:
    """Read IP_ADAPTER_INFO rows with description, type, gateway and addresses."""
    function = _iphlpapi().GetAdaptersInfo
    size = wintypes.ULONG()
    function(None, ctypes.byref(size))
    if not size.value:
        return []
    for _attempt in range(2):
        buffer = ctypes.create_string_buffer(size.value)
        result = function(buffer, ctypes.byref(size))
        if result == 0:
            break
        if result != 111:  # ERROR_BUFFER_OVERFLOW: adapters changed between calls.
            raise OSError("Windows adapter table request failed")
    else:
        raise OSError("Windows adapter table changed repeatedly")
    rows: list[dict] = []
    node = ctypes.cast(buffer, ctypes.POINTER(_IpAdapterInfo))
    while node:
        info = node.contents
        addresses: list[str] = []
        address = info.IpAddressList
        while True:
            addresses.append(_text(address.IpAddress))
            if not address.Next:
                break
            address = address.Next.contents
        rows.append(dict(
            description=_text(info.Description),
            type=info.Type,
            gateway=_text(info.GatewayList.IpAddress),
            addresses=addresses,
        ))
        node = info.Next
    return rows


def _private_address(text: str) -> str | None:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if (address.is_private and not address.is_loopback and not address.is_link_local
            and not address.is_unspecified and not address.is_multicast):
        return str(address)
    return None


def physical_private_ipv4_addresses() -> list[str]:
    """Usable addresses of physical adapters, dependent ones after routed ones."""
    preferred: list[str] = []
    dependent: list[str] = []
    for row in _adapter_rows():
        if row["type"] not in _PHYSICAL_TYPES:
            continue
        description = row["description"].casefold()
        if any(key in description for key in _EXCLUDED_DESCRIPTIONS):
            continue
        target = preferred if row["gateway"] not in ("", "0.0.0.0") else dependent
        for text in row["addresses"]:
            address = _private_address(text)
            if address and address not in preferred and address not in dependent:
                target.append(address)
    return preferred + dependent


def hostname_private_ipv4_addresses() -> list[str]:
    """Fallback: every private address the hostname resolves to."""
    found: set[str] = set()
    for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = ipaddress.ip_address(result[4][0])
        if address.is_private and not address.is_loopback and not address.is_link_local:
            found.add(str(address))
    return sorted(found)


def private_ipv4_addresses() -> list[str]:
    """Addresses offered in the Host UI; physical adapters first, old list as fallback."""
    try:
        physical = physical_private_ipv4_addresses()
    except (AttributeError, OSError):
        physical = []
    return physical or hostname_private_ipv4_addresses()
