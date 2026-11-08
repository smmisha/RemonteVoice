"""Exercise SoundCard's actual play loop with synthetic PCM and fake native I/O."""

import ast
import asyncio
from contextlib import contextmanager
from importlib.metadata import distribution
from itertools import chain, repeat
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import time
from unittest.mock import Mock, patch

import numpy as np

from remotevoice.windows_audio_playback import guard_playback_buffer


def _original_play():
    # Extract only this method: importing mediafoundation would initialize COM.
    path = Path(distribution("SoundCard").locate_file("soundcard/mediafoundation.py"))
    module = ast.parse(path.read_text(encoding="utf-8"))
    player = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "_Player")
    method = next(node for node in player.body if isinstance(node, ast.FunctionDef) and node.name == "play")
    sleep = Mock()
    namespace = {
        "numpy": np,
        "time": SimpleNamespace(sleep=sleep),
        "_ffi": SimpleNamespace(memmove=lambda target, source, count: target.__setitem__(slice(0, count), source[:count])),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["play"], sleep


_PLAY, _SLEEP = _original_play()


class FakePlayer:
    play = _PLAY

    def __init__(self, available=(1056,), channelmap=(0, 1)):
        self.channelmap = channelmap
        self.writes = []
        self.released = []
        self._native_buffer = None
        self._render_available_frames = Mock(side_effect=chain(available, repeat(available[-1])))
        self._render_buffer = Mock(side_effect=self.make_buffer)
        self._render_release = Mock(side_effect=self.release_buffer)

    def make_buffer(self, frames):
        self.writes.append(frames)
        self._native_buffer = bytearray(b"\xa5" * (frames * len(set(self.channelmap)) * 4))
        return [self._native_buffer]

    def release_buffer(self, frames):
        self.released.append((frames, bytes(self._native_buffer)))

    def played(self):
        return np.concatenate([
            np.frombuffer(payload, dtype="float32").reshape(frames, len(set(self.channelmap)))
            for frames, payload in self.released
        ])


class PlaybackFramesGuardTests(unittest.TestCase):
    def setUp(self):
        _SLEEP.reset_mock()

    def test_bridge_stop_cancels_soundcard_render_poll_without_new_native_frames(self):
        from remotevoice.audio import AudioBridge, AudioStopError
        player=FakePlayer();native_release=player._render_release
        polling=threading.Event();release=threading.Event();closed=threading.Event()
        def available():
            polling.set()
            return 480 if release.is_set() else 0
        player._render_available_frames.side_effect=available
        @contextmanager
        def player_context(**options):
            try: yield player
            finally: closed.set()
        bridge=AudioBridge('synthetic-capture','synthetic-playback',Mock(),Mock(),asyncio.Lock(),Mock())
        worker=threading.Thread(target=bridge._play_phone_audio,name='test-stalled-wasapi-poll',daemon=True)
        bridge._threads.append(worker)
        stopped_error=None
        try:
            with patch('remotevoice.audio.sc.get_speaker',return_value=SimpleNamespace(player=player_context)), patch.object(_SLEEP,'side_effect',time.sleep):
                worker.start();self.assertTrue(polling.wait(1))
                try: bridge.stop()
                except AudioStopError as error: stopped_error=error
                self.assertIsNone(stopped_error,'stop must reach SoundCard polling without requiring native frame availability')
                self.assertFalse(worker.is_alive());self.assertTrue(closed.is_set())
                native_release.assert_not_called()
        finally:
            bridge._stop.set();release.set();worker.join(2)
            self.assertFalse(worker.is_alive(),'synthetic stalled worker leaked')
            bridge.stop()

    def test_480_pcm_frames_never_release_1056_available_native_frames(self):
        player = FakePlayer()
        samples = np.linspace(-0.75, 0.75, 960, dtype="float32").reshape(480, 2)
        self.assertIs(guard_playback_buffer(player), player)
        self.assertIsNone(player.play(samples))
        self.assertEqual(player.writes, [480])
        self.assertEqual([frames for frames, _ in player.released], [480])
        np.testing.assert_array_equal(player.played(), samples)

    def test_partial_native_writes_consume_only_the_remaining_pcm(self):
        player = FakePlayer(available=(128, 192, 1056))
        samples = np.arange(960, dtype="float32").reshape(480, 2)
        guard_playback_buffer(player).play(samples)
        self.assertEqual(player.writes, [128, 192, 160])
        self.assertEqual([frames for frames, _ in player.released], [128, 192, 160])
        np.testing.assert_array_equal(player.played(), samples)

    def test_zero_native_availability_keeps_the_original_retry_without_spending_budget(self):
        player = FakePlayer(available=(0, 0, 1056))
        guard_playback_buffer(player).play(np.ones((480, 2), dtype="float32"))
        self.assertEqual(player.writes, [480])
        self.assertEqual(_SLEEP.call_count, 2)
        _SLEEP.assert_called_with(0.001)

    def test_1d_and_single_channel_pcm_keep_original_channel_expansion(self):
        mono = np.array([0.125, -0.25, 0.375, -0.5], dtype="float32")
        for samples in (mono, mono[:, None]):
            with self.subTest(shape=samples.shape):
                player = FakePlayer()
                guard_playback_buffer(player).play(samples)
                self.assertEqual(player.writes, [4])
                np.testing.assert_array_equal(player.played(), np.repeat(mono[:, None], 2, axis=1))

    def test_float_conversion_channel_order_and_input_contents_are_preserved(self):
        player = FakePlayer(channelmap=(1, 0))
        samples = np.asfortranarray([[0.125, -0.25], [2.0, -2.0]], dtype="float64")
        original = samples.copy()
        guard_playback_buffer(player).play(samples)
        np.testing.assert_array_equal(player.played(), samples.astype("float32")[:, [1, 0]])
        np.testing.assert_array_equal(samples, original)

    def test_original_validation_errors_remain_unchanged_and_never_call_native_io(self):
        cases = ((1.0, (0, 1)), (np.ones((2, 2, 2)), (0, 1)),
                 (np.ones((2, 0)), (0, 1)), (np.ones((2, 2)), (0, 0)),
                 (["not-pcm"], (0, 1)))
        for samples, channelmap in cases:
            with self.subTest(shape=np.shape(samples), channelmap=channelmap):
                original = FakePlayer(channelmap=channelmap)
                guarded = FakePlayer(channelmap=channelmap)
                native_release = guarded._render_release
                try:
                    original.play(samples)
                except Exception as error:
                    expected = error
                else:
                    self.fail("Fixture must fail original SoundCard validation")
                guard_playback_buffer(guarded)
                with self.assertRaises(type(expected)) as raised:
                    guarded.play(samples)
                self.assertEqual(str(raised.exception), str(expected))
                guarded._render_buffer.assert_not_called()
                native_release.assert_not_called()
                self.assertEqual(guarded._render_available_frames(), 1056)

    def test_empty_input_does_not_write_and_later_calls_get_a_fresh_budget(self):
        player = FakePlayer()
        native_available = player._render_available_frames
        guard_playback_buffer(player).play(np.empty((0, 2), dtype="float32"))
        native_available.assert_not_called()
        self.assertEqual(player.writes, [])
        for frames in (480, 240):
            player.play(np.zeros((frames, 2), dtype="float32"))
            self.assertEqual(player._render_available_frames(), 1056)
        self.assertEqual(player.writes, [480, 240])

    def test_input_array_provider_is_materialized_only_once(self):
        provider = SimpleNamespace(calls=0)

        class ArrayInput:
            def __array__(self, dtype=None, copy=None):
                provider.calls += 1
                return np.array([[0.25, -0.5]], dtype=dtype)

        player = FakePlayer()
        guard_playback_buffer(player).play(ArrayInput())
        self.assertEqual(provider.calls, 1)
        np.testing.assert_array_equal(player.played(), [[0.25, -0.5]])

    def test_idempotence_does_not_change_the_class_or_another_player(self):
        player, other = FakePlayer(), FakePlayer()
        original_other = vars(other).copy()
        guard_playback_buffer(player)
        wrappers = (player.play, player._render_available_frames, player._render_release)
        self.assertIs(guard_playback_buffer(player), player)
        for expected, actual in zip(wrappers, (player.play, player._render_available_frames, player._render_release)):
            self.assertIs(actual, expected)
        self.assertIs(FakePlayer.play, _PLAY)
        self.assertEqual(vars(other), original_other)
        self.assertEqual(other._render_available_frames(), 1056)

    def test_unsupported_backends_are_left_unchanged_without_calls(self):
        cases = (SimpleNamespace(), SimpleNamespace(play=Mock()),
                 SimpleNamespace(play=Mock(), _render_available_frames=Mock()),
                 SimpleNamespace(play=Mock(), _render_release=Mock()),
                 SimpleNamespace(play=None, _render_available_frames=Mock(), _render_release=Mock()))
        for player in cases:
            with self.subTest(attributes=tuple(vars(player))):
                before = vars(player).copy()
                self.assertIs(guard_playback_buffer(player), player)
                self.assertEqual(vars(player), before)
                for value in before.values():
                    if isinstance(value, Mock):
                        value.assert_not_called()

    def test_native_query_failure_propagates_and_budget_is_restored(self):
        player = FakePlayer()
        native_available = player._render_available_frames
        error = OSError("synthetic availability failure")
        native_available.side_effect = error
        guard_playback_buffer(player)
        with self.assertRaises(OSError) as raised:
            player.play(np.ones((480, 2), dtype="float32"))
        self.assertIs(raised.exception, error)
        native_available.side_effect = lambda: 1056
        self.assertEqual(player._render_available_frames(), 1056)
        player.play(np.ones((240, 2), dtype="float32"))
        self.assertEqual(player.writes, [240])

    def test_native_buffer_failure_propagates_without_release_and_resets_budget(self):
        player = FakePlayer()
        error = OSError("synthetic GetBuffer failure")
        native_release = player._render_release
        player._render_buffer.side_effect = error
        guard_playback_buffer(player)
        with self.assertRaises(OSError) as raised:
            player.play(np.ones((480, 2), dtype="float32"))
        self.assertIs(raised.exception, error)
        native_release.assert_not_called()
        self.assertEqual(player._render_available_frames(), 1056)
        player._render_buffer.side_effect = player.make_buffer
        player.play(np.ones((240, 2), dtype="float32"))
        self.assertEqual(player.writes, [240])

    def test_release_failure_does_not_spend_budget_and_next_call_is_independent(self):
        player = FakePlayer()
        native_release = player._render_release
        remaining_at_failure = []
        error = OSError("synthetic ReleaseBuffer failure")

        def fail_release(frames):
            remaining_at_failure.append(player._render_available_frames())
            raise error

        native_release.side_effect = fail_release
        guard_playback_buffer(player)
        with self.assertRaises(OSError) as raised:
            player.play(np.ones((480, 2), dtype="float32"))
        self.assertIs(raised.exception, error)
        self.assertEqual(remaining_at_failure, [480])
        self.assertEqual(player._render_available_frames(), 1056)
        native_release.side_effect = player.release_buffer
        player.play(np.ones((240, 2), dtype="float32"))
        self.assertEqual(player.writes, [480, 240])
        self.assertEqual([frames for frames, _ in player.released], [240])


if __name__ == "__main__":
    unittest.main()
