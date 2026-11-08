"""Negotiated, lossless, independently bounded 10 ms system-audio frames."""
import zlib

from .protocol import SYSTEM_FRAME, encode_audio_frame

SYSTEM_DEFLATE_FRAME = 0x03


def encode_system_frame(payload, *, compressed=False):
    """Preserve float bits, grouping byte lanes before per-frame raw DEFLATE.

    A fresh dictionary bounds memory and doesn't depend on preceding packets.
    Unhelpful compression falls back to the existing raw PCM frame.
    """
    raw = encode_audio_frame(SYSTEM_FRAME, payload)
    if not compressed:
        return raw
    pcm = raw[1:]
    lanes = b''.join(pcm[offset::4] for offset in range(4))
    encoded = zlib.compress(lanes, level=1, wbits=-15)
    return bytes((SYSTEM_DEFLATE_FRAME,)) + encoded if len(encoded) < len(pcm) else raw
