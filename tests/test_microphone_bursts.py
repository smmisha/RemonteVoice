"""Run the actual bridge class with synthetic PCM and no SoundCard/COM import."""

import ast
import __future__
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from remotevoice import protocol
from remotevoice.audio_timing import SystemAudioTiming
from remotevoice.windows_audio_playback import guard_playback_buffer


SOURCE = Path(__file__).resolve().parents[1] / 'host/remotevoice/audio.py'


def bridge_class(clock, output=None):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    # Compile the current production class, without module-level native imports.
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AudioBridge']
    namespace = dict(vars(protocol), np=np, queue=queue, threading=threading, SystemAudioTiming=SystemAudioTiming,
                     os=SimpleNamespace(environ={}), time=SimpleNamespace(monotonic=lambda: clock.now),
                     sc=SimpleNamespace(get_speaker=Mock(return_value=output)),
                     guard_playback_buffer=Mock(side_effect=guard_playback_buffer))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PHONE_QUEUE_FRAMES' for t in node.targets):
            namespace['PHONE_QUEUE_FRAMES'] = ast.literal_eval(node.value)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec',
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace['AudioBridge'], namespace


def payload(index):
    return np.full(protocol.FRAME_SAMPLES, (index+1)/128, dtype='<f4').tobytes()


class MicrophoneBurstTests(unittest.TestCase):
    def bridge(self):
        cls, namespace = bridge_class(SimpleNamespace(now=0.0))
        return cls('capture', 'playback', None, None, None, Mock()), namespace

    def test_sender_permitted_ten_packet_burst_is_retained_in_order(self):
        bridge, namespace = self.bridge()
        bridge.set_microphone_enabled(True)
        for index in range(10):
            bridge.submit_phone_audio(payload(index))
        self.assertEqual(bridge._phone_frames.qsize(), 10)
        self.assertEqual(namespace['PHONE_QUEUE_FRAMES'], 10)
        self.assertEqual(bridge._phone_frames.maxsize, 10)
        self.assertEqual([bridge._phone_frames.get_nowait() for _ in range(10)],
                         [payload(index) for index in range(10)])

    def test_larger_burst_stays_bounded_and_discards_oldest_packets(self):
        bridge, _ = self.bridge()
        bridge.set_microphone_enabled(True)
        for index in range(15):
            bridge.submit_phone_audio(payload(index))
            self.assertLessEqual(bridge._phone_frames.qsize(), 10)
        self.assertEqual(bridge._phone_frames.qsize(), 10)
        self.assertEqual([bridge._phone_frames.get_nowait() for _ in range(10)],
                         [payload(index) for index in range(5, 15)])

    def test_mute_clears_burst_and_admits_no_more_packets(self):
        bridge, _ = self.bridge()
        bridge.submit_phone_audio(payload(0))
        self.assertTrue(bridge._phone_frames.empty())
        bridge.set_microphone_enabled(True)
        for index in range(10):
            bridge.submit_phone_audio(payload(index))
        bridge.set_microphone_enabled(False)
        self.assertTrue(bridge._phone_frames.empty())
        bridge.submit_phone_audio(payload(10))
        self.assertTrue(bridge._phone_frames.empty())
        bridge.set_microphone_enabled(True)
        bridge.submit_phone_audio(payload(11))
        self.assertEqual(bridge._phone_frames.get_nowait(), payload(11))

    def test_actual_play_loop_handles_repeated_100ms_bursts_without_loss_or_zero_fill(self):
        clock = SimpleNamespace(now=0.0)
        played = []

        class StopEvent:
            def __init__(self): self.stopped = False
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, delay):
                if not self.stopped: clock.now += delay
                return self.stopped

        class Native480Player:
            def __init__(self): self.queued = 0.0; self.updated = 0.0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def play(self, samples):
                # Fake a 48kHz device with 1056-frame capacity and correct writes.
                self.queued = max(0.0, self.queued-(clock.now-self.updated)*48000)
                if 1056-self.queued < 480-1e-6:
                    clock.now += (480-(1056-self.queued))/48000
                    self.queued = 576
                self.updated = clock.now
                self.queued += len(samples)
                played.append(samples.copy())
                if len(played) == 60: bridge._stop.set()

        player = Native480Player()
        output = SimpleNamespace(player=Mock(return_value=player))
        cls, namespace = bridge_class(clock, output)
        bridge = cls('capture', 'playback', None, None, None, Mock())
        bridge._stop = StopEvent()
        bridge.set_microphone_enabled(True)
        received = 0
        drops = 0
        next_burst = 0.0

        class TimedQueue(queue.Queue):
            def deliver_due(self):
                nonlocal received, drops, next_burst
                while next_burst <= clock.now+1e-9:
                    for _ in range(10):
                        if self.full(): drops += 1
                        bridge.submit_phone_audio(payload(received))
                        received += 1
                    next_burst += .1

            def get(self, block=True, timeout=None):
                if not block: return super().get(block=False)
                self.deliver_due()
                if self.empty():
                    if next_burst <= clock.now+timeout+1e-9:
                        clock.now = next_burst
                        self.deliver_due()
                    else:
                        clock.now += timeout
                        raise queue.Empty
                return super().get(block=False)

        bridge._phone_frames = TimedQueue(maxsize=bridge._phone_frames.maxsize)
        bridge._report_failure = Mock()
        bridge._play_phone_audio()
        bridge._report_failure.assert_not_called()
        namespace['guard_playback_buffer'].assert_called_once_with(player, stop_event=bridge._stop)
        self.assertEqual(drops, 0)
        self.assertEqual(received, 60)
        self.assertTrue(bridge._phone_frames.empty())
        self.assertEqual(len(played), 60)
        self.assertAlmostEqual(clock.now, .59, places=7)
        for index, samples in enumerate(played):
            expected = np.frombuffer(payload(index), dtype='<f4')
            np.testing.assert_array_equal(samples, np.repeat(expected[:, None], 2, axis=1))


if __name__ == '__main__':
    unittest.main()
