"""Real event-loop/worker interaction under controlled application backpressure.

Native audio is synthetic; no Windows routes, microphones or NICs are touched.
"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from remotevoice.audio import AudioBridge
from remotevoice.protocol import FRAME_SAMPLES, SYSTEM_CHANNELS


class AudioBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def blocked_capture(self, *, failing_observer=False):
        closed = asyncio.Event()
        worker_errors = []
        statuses = []

        async def close(**kwargs):
            closed.set()

        def status(value):
            statuses.append(value)
            if failing_observer and value.startswith('audio_error:'):
                raise RuntimeError('synthetic failed status observer')

        websocket = SimpleNamespace(send=AsyncMock(), close=AsyncMock(side_effect=close))
        lock = asyncio.Lock()
        await lock.acquire()
        bridge = AudioBridge('synthetic-capture', 'unused-playback', asyncio.get_running_loop(),
                             websocket, lock, status)

        def capture():
            try:
                bridge._capture_system_audio()
            except Exception as exc:
                worker_errors.append(type(exc).__name__)

        worker = threading.Thread(target=capture, name='rv-test-blocked-capture', daemon=True)
        bridge._threads.append(worker)
        try:
            with patch('remotevoice.audio.sc.get_microphone') as source:
                recorder = source.return_value.recorder.return_value.__enter__.return_value
                recorder.record.return_value = np.zeros((FRAME_SAMPLES, SYSTEM_CHANNELS), dtype='<f4')
                worker.start()
                await asyncio.to_thread(worker.join, 2)
                self.assertFalse(worker.is_alive(), 'capture must exit after its bounded send wait')
                # Let the thread-safe close callback run without unblocking send.
                await asyncio.sleep(0)
                stopped_before_release = bridge._stop.is_set()
                closed_before_release = closed.is_set()
                bridge.stop()
                lock.release()
                await asyncio.sleep(0.02)
                return dict(stopped=stopped_before_release, closed=closed_before_release,
                            sends=websocket.send.await_count, errors=worker_errors,
                            close_calls=websocket.close.await_args_list, statuses=statuses)
        finally:
            bridge._stop.set()
            if lock.locked():
                lock.release()
            await asyncio.to_thread(worker.join, 2)
            bridge.stop()

    async def test_timed_out_send_cannot_deliver_after_bridge_has_stopped(self):
        result = await self.blocked_capture()
        self.assertTrue(result['stopped'])
        self.assertTrue(result['closed'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['sends'], 0, 'stale PCM must not be sent when a held lock is released')
        self.assertEqual(len(result['close_calls']), 1)
        self.assertEqual(result['close_calls'][0].kwargs, dict(code=1011, reason='Audio device error'))

    async def test_failed_status_observer_cannot_prevent_stop_and_socket_close(self):
        result = await self.blocked_capture(failing_observer=True)
        self.assertTrue(result['stopped'], 'failure handling must stop workers before notifying the UI')
        self.assertTrue(result['closed'], 'the socket close must not depend on a working UI callback')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['sends'], 0)
        self.assertEqual(len(result['close_calls']), 1)
