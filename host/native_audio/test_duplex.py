"""Native DLL acceptance with synthetic PCM only, no Windows audio endpoints."""
import ctypes as c
import json
import sys
import threading
import time
import unittest
from pathlib import Path

DLL_PATH = Path(sys.argv.pop(1)).resolve()
api = c.CDLL(str(DLL_PATH))
api.rv_audio_abi.restype = c.c_uint32
api.rv_audio_create.argtypes = [c.c_uint64]
api.rv_audio_create.restype = c.c_void_p
api.rv_audio_destroy.argtypes = [c.c_void_p]
api.rv_audio_close.argtypes = [c.c_void_p]
api.rv_audio_write.argtypes = [c.c_void_p, c.c_uint32, c.c_uint64, c.c_uint64, c.POINTER(c.c_float), c.c_uint32]
api.rv_audio_read.argtypes = [c.c_void_p, c.c_uint32, c.c_uint64, c.POINTER(c.c_float), c.c_uint32, c.POINTER(c.c_uint64)]


class NativeDuplexTests(unittest.TestCase):
    def setUp(self):
        self.handle = api.rv_audio_create(7)
        self.assertTrue(self.handle)

    def tearDown(self):
        api.rv_audio_close(self.handle)
        api.rv_audio_destroy(self.handle)

    def write(self, direction, value=.25, generation=7, sequence=1):
        size = 960 if direction == 0 else 480
        data = (c.c_float * size)(*([value] * size))
        return api.rv_audio_write(self.handle, direction, generation, sequence, data, size)

    def read(self, direction, generation=7):
        size = 960 if direction == 0 else 480
        data = (c.c_float * size)(*([9] * size))
        sequence = c.c_uint64(99)
        code = api.rv_audio_read(self.handle, direction, generation, data, size, c.byref(sequence))
        return code, list(data), sequence.value

    def test_two_directions_do_not_echo_and_preserve_samples(self):
        self.assertEqual(api.rv_audio_abi(), 1)
        self.assertEqual(self.write(0, .25, sequence=12), 0)
        self.assertEqual(self.read(1), (1, [0.] * 480, 0))
        self.assertEqual(self.write(1, -.5, sequence=13), 0)
        self.assertEqual(self.read(0), (0, [.25] * 960, 12))
        self.assertEqual(self.read(1), (0, [-.5] * 480, 13))

    def test_capacity_is_bounded_and_full_does_not_overwrite_unread_pcm(self):
        for direction, capacity in ((0, 24), (1, 10)):
            for index in range(capacity):
                self.assertEqual(self.write(direction, sequence=index), 0)
            self.assertEqual(self.write(direction, sequence=999), 2)
            for index in range(capacity):
                code, _, sequence = self.read(direction)
                self.assertEqual((code, sequence), (0, index))
            self.assertEqual(self.read(direction)[0], 1)

    def test_stale_generation_and_closed_session_cannot_return_old_audio(self):
        self.assertEqual(self.write(0), 0)
        self.assertEqual(self.write(1, generation=8), -2)
        self.assertEqual(self.read(0, generation=8), (-2, [0.] * 960, 0))
        self.assertEqual(api.rv_audio_close(self.handle), 0)
        self.assertEqual(api.rv_audio_close(self.handle), 0)
        self.assertEqual(self.write(0), -3)
        self.assertEqual(self.read(0), (-3, [0.] * 960, 0))

    def test_invalid_frame_shapes_and_nonfinite_input_are_rejected(self):
        data = (c.c_float * 960)()
        for direction, count in ((0, 480), (1, 960), (2, 960)):
            self.assertEqual(api.rv_audio_write(self.handle, direction, 7, 0, data, count), -1)
        self.assertEqual(self.write(0, float('nan')), -1)
        self.assertEqual(self.write(1, float('inf')), -1)
        self.assertIsNone(api.rv_audio_create(0))
        self.assertEqual(self.read(0)[0], 1)

    def test_concurrent_spsc_wraparound_keeps_all_accepted_packets_in_order(self):
        count = 20_000
        errors = []
        cancelled = threading.Event()
        deadline = time.monotonic() + 20
        def producer():
            data = (c.c_float * 480)()
            for index in range(count):
                data[0] = float(index)
                while not cancelled.is_set():
                    code = api.rv_audio_write(self.handle, 1, 7, index, data, 480)
                    if code == 0:
                        break
                    if code != 2 or time.monotonic() > deadline:
                        errors.append('Producer stalled or failed'); cancelled.set(); return
                    time.sleep(0)
        thread = threading.Thread(target=producer)
        thread.start()
        try:
            data = (c.c_float * 480)()
            sequence = c.c_uint64()
            index = 0
            while index < count and not cancelled.is_set():
                code = api.rv_audio_read(self.handle, 1, 7, data, 480, c.byref(sequence))
                if code == 1:
                    if time.monotonic() > deadline: self.fail('Consumer stalled')
                    time.sleep(0); continue
                self.assertEqual(code, 0)
                self.assertEqual((sequence.value, data[0]), (index, float(index)))
                index += 1
            self.assertEqual(index, count)
        finally:
            cancelled.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
