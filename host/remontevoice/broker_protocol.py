"""Bounded private control frames; Windows pipe authentication is a separate gate.

The capability stays server-side, bound to one authenticated connection. An ACK
means request acceptance, never microphone/route readiness. No identities, file
paths, raw KS commands or driver session tokens are accepted from this wire.
"""
from __future__ import annotations

import math
import struct
from enum import IntEnum

from .mic_broker import MicBroker

_HEADER = struct.Struct('<4sBBH')
_MAGIC = b'RVMB'
_VERSION = 1
MAX_CONTROL_FRAME = _HEADER.size + 1920


class BrokerCommand(IntEnum):
    ENABLE = 1
    PCM = 2
    HEARTBEAT = 3
    CLOSE = 4
    STATUS = 5


class BrokerProtocolError(ValueError):
    pass


def control_body_size(header: bytes) -> int:
    """Validate the fixed header before a native transport allocates/reads a body."""
    if type(header) is not bytes or len(header) != _HEADER.size:
        raise BrokerProtocolError('Invalid control header size')
    magic, version, command, size = _HEADER.unpack(header)
    expected = {1: 1, 2: 1920, 3: 0, 4: 0, 5: 0}
    if magic != _MAGIC or version != _VERSION or command not in expected or size != expected[command]:
        raise BrokerProtocolError('Invalid control frame header')
    return size


class BrokerConnection:
    """Dispatcher for a single, already OS-authenticated local pipe connection.

    The transport reads exactly eight header bytes, rejects a declared
    length above 1920 before reading a body, and calls this with one full frame.
    It must call close on EOF/error and run broker.poll on its service worker.
    """

    def __init__(self, broker: MicBroker, *, status_payload=None) -> None:
        self._broker = broker
        self._capability = broker.connect()
        if self._capability is None:
            raise PermissionError('Broker connection denied')
        self._closed = False
        self._status_payload = status_payload

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._broker.close(self._capability)

    def handle_frame(self, frame: bytes) -> bytes:
        if self._closed:
            raise BrokerProtocolError('Connection closed')
        try:
            if type(frame) is not bytes or not _HEADER.size <= len(frame) <= MAX_CONTROL_FRAME:
                raise BrokerProtocolError('Invalid control frame size')
            magic, version, command, size = _HEADER.unpack_from(frame)
            if size != control_body_size(frame[:_HEADER.size]) or len(frame) != _HEADER.size + size:
                raise BrokerProtocolError('Invalid control frame header')
            try:
                kind = BrokerCommand(command)
            except ValueError as error:
                raise BrokerProtocolError('Unknown broker command') from error
            payload = frame[_HEADER.size:]
            if kind == BrokerCommand.STATUS:
                if self._status_payload is None:
                    raise BrokerProtocolError('Status observation unavailable')
                status = self._status_payload()
                if type(status) is not bytes or len(status) != 9:
                    raise BrokerProtocolError('Invalid trusted status')
                return _HEADER.pack(_MAGIC, _VERSION, command | 0x80, len(status)) + status
            if kind == BrokerCommand.ENABLE:
                if payload not in (b'\0', b'\1'):
                    raise BrokerProtocolError('Invalid enable request')
                accepted = self._broker.set_microphone_enabled(self._capability, payload == b'\1')
            elif kind == BrokerCommand.PCM:
                if len(payload) != 1920 or not all(math.isfinite(v) for v in struct.unpack('<480f', payload)):
                    raise BrokerProtocolError('Invalid microphone frame')
                accepted = self._broker.submit(self._capability, payload)
            else:
                if payload:
                    raise BrokerProtocolError('Unexpected control payload')
                if kind == BrokerCommand.HEARTBEAT:
                    accepted = self._broker.heartbeat(self._capability)
                else:
                    accepted = self._broker.close(self._capability)
                    self._closed = True
            return _HEADER.pack(_MAGIC, _VERSION, command | 0x80, 1) + bytes((bool(accepted),))
        except Exception:
            self.close()
            raise
