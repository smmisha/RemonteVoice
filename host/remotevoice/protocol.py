"""Small, bounded audio protocol shared with the iOS client."""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Mapping
from typing import Any

SAMPLE_RATE = 48_000
FRAME_SAMPLES = 480  # 10 ms at 48 kHz
MIC_CHANNELS = 1
SYSTEM_CHANNELS = 2
MIC_FRAME = 0x01
SYSTEM_FRAME = 0x02
BYTES_PER_SAMPLE = 4  # IEEE-754 float32, little-endian
MAX_AUDIO_PAYLOAD = FRAME_SAMPLES * SYSTEM_CHANNELS * BYTES_PER_SAMPLE


class ProtocolError(ValueError):
    """Raised for malformed or out-of-range protocol messages."""


def encode_control(message: Mapping[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True)


def decode_control(message: str) -> dict[str, Any]:
    try:
        value = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Invalid control message") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ProtocolError("Control message must be an object with a type")
    return value


def decode_audio_frame(message: bytes) -> tuple[int, memoryview]:
    if not isinstance(message, bytes) or len(message) < 1:
        raise ProtocolError("Audio frame is empty")

    frame_type = message[0]
    payload = memoryview(message)[1:]
    validate_audio_payload(frame_type, payload)
    return frame_type, payload


def validate_audio_payload(
    frame_type: int, payload: bytes | bytearray | memoryview
) -> None:
    """Require a complete finite float32 PCM frame without changing its samples.

    Finite headroom outside [-1, 1] remains compatible with the existing float
    PCM protocol. A shared amplitude ceiling/normalization policy is a separate
    protocol change; silently clipping here would alter the sender's signal.
    """
    if frame_type not in (MIC_FRAME, SYSTEM_FRAME):
        raise ProtocolError("Unknown audio frame type")
    if not payload or len(payload) > MAX_AUDIO_PAYLOAD:
        raise ProtocolError("Audio frame size is out of range")
    if len(payload) % BYTES_PER_SAMPLE:
        raise ProtocolError("Audio payload is not aligned to float32 samples")

    channels = MIC_CHANNELS if frame_type == MIC_FRAME else SYSTEM_CHANNELS
    if len(payload) != FRAME_SAMPLES * channels * BYTES_PER_SAMPLE:
        raise ProtocolError("Audio frame has an unexpected number of samples")
    if any(not math.isfinite(sample) for (sample,) in struct.iter_unpack("<f", payload)):
        raise ProtocolError("Audio samples must be finite float32 values")


def encode_audio_frame(frame_type: int, payload: bytes | bytearray | memoryview) -> bytes:
    if frame_type not in (MIC_FRAME, SYSTEM_FRAME):
        raise ProtocolError("Unknown audio frame type")
    encoded = bytes((frame_type,)) + bytes(payload)
    decode_audio_frame(encoded)
    return encoded

