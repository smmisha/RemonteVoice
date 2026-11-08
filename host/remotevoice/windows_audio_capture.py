"""Instance-local guard for SoundCard's Windows synthetic-silence fallback."""

from __future__ import annotations


def guard_capture_silence(recorder):
    """Prefer retained native PCM over a stale synthetic chunk after an oversleep.

    SoundCard's Windows recorder measures idle time after sleeping. If the
    Python thread was delayed, native PCM may already be available by then.
    Returning synthetic silence as well as that PCM extends the stream and
    eventually overflows the receiver. Retry at most once, without changing
    native PCM, actual idle silence, global classes or installed dependencies.
    Other recorder backends keep their public behavior.
    """
    if getattr(recorder, "_remotevoice_silence_guard", False):
        return recorder
    original = getattr(recorder, "_record_chunk", None)
    available = getattr(recorder, "_capture_available_frames", None)
    if not callable(original) or not callable(available) or not hasattr(recorder, "_idle_start_time"):
        return recorder

    def record_chunk():
        chunk = original()
        # A native chunk resets idle_start_time, even when its PCM is all zero.
        if recorder._idle_start_time is not None and available() > 0:
            return original()
        return chunk

    recorder._record_chunk = record_chunk
    recorder._remotevoice_silence_guard = True
    return recorder
