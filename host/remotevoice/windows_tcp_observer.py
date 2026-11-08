"""Read-only SIO_TCP_INFO counters for an already-owned socket; no payload/access changes.

ABI: Microsoft mstcpip.h TCP_INFO_v0 / SIO_TCP_INFO (Windows 10 1703+).
https://learn.microsoft.com/en-us/windows/win32/winsock/sio-tcp-info
"""
import ctypes as ct
from functools import lru_cache

_SIO_TCP_INFO = 0xD8000027
_U32, _U64, _U8 = ct.c_uint32, ct.c_uint64, ct.c_uint8


class _TcpInfo(ct.Structure):
    _fields_ = [('State', _U32), ('Mss', _U32), ('ConnectionTimeMs', _U64),
        ('TimestampsEnabled', _U8), ('RttUs', _U32), ('MinRttUs', _U32),
        ('BytesInFlight', _U32), ('Cwnd', _U32), ('SndWnd', _U32), ('RcvWnd', _U32),
        ('RcvBuf', _U32), ('BytesOut', _U64), ('BytesIn', _U64), ('BytesReordered', _U32),
        ('BytesRetrans', _U32), ('FastRetrans', _U32), ('DupAcksIn', _U32),
        ('TimeoutEpisodes', _U32), ('SynRetrans', _U8)]


_FIELDS = {'rtt_us': 'RttUs', 'min_rtt_us': 'MinRttUs', 'bytes_in_flight': 'BytesInFlight',
    'cwnd': 'Cwnd', 'send_window': 'SndWnd', 'bytes_out': 'BytesOut', 'bytes_in': 'BytesIn',
    'bytes_reordered': 'BytesReordered', 'bytes_retrans': 'BytesRetrans',
    'fast_retrans': 'FastRetrans', 'duplicate_acks': 'DupAcksIn', 'timeout_episodes': 'TimeoutEpisodes'}
TCP_FIELDS = tuple(_FIELDS)


@lru_cache(maxsize=1)
def _query_api():
    query = ct.WinDLL('ws2_32', use_last_error=True).WSAIoctl
    query.argtypes = [ct.c_size_t, _U32, ct.c_void_p, _U32, ct.c_void_p, _U32,
                      ct.POINTER(_U32), ct.c_void_p, ct.c_void_p]
    query.restype = ct.c_int
    return query


def read_tcp_statistics(sock):
    """Return a numeric whitelist, or None if unsupported/closed; never close or write."""
    try:
        handle = sock.fileno()
        if handle < 0:
            return None
        version, returned, info = _U32(0), _U32(), _TcpInfo()
        result = _query_api()(handle, _SIO_TCP_INFO, ct.byref(version), ct.sizeof(version),
            ct.byref(info), ct.sizeof(info), ct.byref(returned), None, None)
        if result != 0 or returned.value != ct.sizeof(info):
            return None
        return {name: int(getattr(info, field)) for name, field in _FIELDS.items()}
    except (OSError, AttributeError, TypeError, ValueError):
        return None
