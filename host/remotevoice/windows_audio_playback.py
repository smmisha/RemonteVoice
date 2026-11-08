"""Instance-local guard for SoundCard's Windows render frame accounting."""

from __future__ import annotations

import numpy as np


def guard_playback_buffer(player, *, stop_event=None):
    """Limit each native write to PCM remaining in one single-writer play call.

    Keep SoundCard's original normalization, channel handling and retry loop.
    Only this player's methods are wrapped; other backends are left untouched.
    Native errors propagate, and a failed call never leaks its frame budget.
    An optional owner stop flag also cancels SoundCard's zero-space polling;
    a native API call already in progress still owns its normal cleanup path.
    """
    if getattr(player, "_remotevoice_playback_frames_guard", False):
        return player
    original_play = getattr(player, "play", None)
    original_available = getattr(player, "_render_available_frames", None)
    original_release = getattr(player, "_render_release", None)
    if not all(callable(method) for method in (
        original_play, original_available, original_release, getattr(player, "_render_buffer", None)
    )):
        return player

    remaining_frames = None

    def available_frames():
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("Audio playback stopped")
        available = original_available()
        return available if remaining_frames is None else min(available, remaining_frames)

    def release_frames(frames):
        nonlocal remaining_frames
        result = original_release(frames)
        if remaining_frames is not None:
            remaining_frames -= frames
        return result

    def play(data):
        nonlocal remaining_frames
        # Materialize array-like inputs once; original_play still owns validation
        # and channel conversion. The extra float32 copy never changes gain.
        samples = np.array(data, dtype="float32", order="C")
        previous_budget = remaining_frames
        remaining_frames = samples.shape[0] if samples.ndim in (1, 2) else 0
        try:
            return original_play(samples)
        finally:
            remaining_frames = previous_budget

    player.play = play
    player._render_available_frames = available_frames
    player._render_release = release_frames
    player._remotevoice_playback_frames_guard = True
    return player
