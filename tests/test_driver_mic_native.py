"""Python writer -> actual driver bridge source, with kernel API test substitutes."""
import ctypes as ct
import hashlib
import json
import os
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from remotevoice.driver_mic import (
    DriverBufferFull, DriverMicError, DriverMicWriter, MicWriterState,
    float_mono_to_pcm16,
)

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Python/native test did not reach expected state")
        time.sleep(0.002)


class DriverMicNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "host/build/driver-bridge-tests/bridge_test.dll"
        if os.name != "nt" or not path.is_file():
            if os.environ.get("RV_REQUIRE_DRIVER_TEST_DLL") == "1":
                raise AssertionError("Required native bridge DLL missing; run host/driver/test.ps1")
            raise unittest.SkipTest("Optional native bridge test DLL not built")
        manifest = json.loads((path.parent / "test-build-manifest.json").read_text("utf-8-sig"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise AssertionError("Native bridge DLL identity changed")
        for name, digest in manifest["inputs"].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
                raise AssertionError("Native bridge DLL is stale; rebuild before acceptance")
        cls.dll = ct.CDLL(str(path))
        cls.dll.rv_test_reset.argtypes = []
        cls.dll.rv_test_reset.restype = None
        cls.dll.rv_test_send.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_uint32]
        cls.dll.rv_test_send.restype = ct.c_int32
        cls.dll.rv_test_read.argtypes = [ct.c_void_p, ct.c_uint32]
        cls.dll.rv_test_read.restype = None
        cls.dll.rv_test_advance.argtypes = [ct.c_uint64]
        cls.dll.rv_test_advance.restype = None

    def setUp(self):
        self.dll.rv_test_reset()
        self.writers = []

    def tearDown(self):
        for writer in self.writers:
            writer.stop()
            self.assertFalse(writer.snapshot.worker_alive)

    def writer(self):
        dll = self.dll
        class NativeTransport:
            def __init__(self):
                self.owner_thread = threading.get_ident()
                self.closed = False
            def send(self, message):
                if self.closed or self.owner_thread != threading.get_ident():
                    raise AssertionError("Native handle lifetime / owner violation")
                data = ct.create_string_buffer(message, len(message))
                status = dll.rv_test_send(data, len(message), 71)
                if status == -4: raise DriverBufferFull()
                if status: raise DriverMicError("Native test bridge rejected command")
            def close(self):
                self.closed = True
        writer = DriverMicWriter(NativeTransport)
        self.writers.append(writer)
        writer.start(); wait_for(writer.is_available)
        return writer

    def read(self, size=960):
        output = ct.create_string_buffer(size)
        self.dll.rv_test_read(output, size)
        return output.raw

    def test_wire_layout_and_converted_voice_cross_real_cpp_boundary(self):
        writer = self.writer()
        samples = np.linspace(-1, 1, 480, dtype="<f4").tobytes()
        self.assertTrue(writer.submit(samples))
        wait_for(lambda: writer.snapshot.sent == 1)
        actual = self.read()
        self.assertEqual(actual, float_mono_to_pcm16(samples))
        self.assertEqual(self.read(), bytes(960))

    def test_end_removes_voice_before_another_recorder_reads(self):
        writer = self.writer()
        writer.submit(np.full(480, 0.5, dtype="<f4").tobytes())
        wait_for(lambda: writer.snapshot.sent == 1)
        writer.stop()
        self.assertEqual(self.read(), bytes(960))

    def test_native_full_queue_drops_packet_and_keeps_writer_streaming(self):
        writer = self.writer()
        for count in range(10):
            writer.submit(np.full(480, (count + 1) / 20, dtype="<f4").tobytes())
            wait_for(lambda: writer.snapshot.sent == count + 1)
        writer.submit(np.zeros(480, dtype="<f4").tobytes())
        wait_for(lambda: writer.snapshot.dropped == 1)
        self.assertTrue(writer.is_available())
        self.assertEqual(writer.snapshot.sent, 10)
        self.assertEqual(self.read(), float_mono_to_pcm16(np.full(480, 0.05, dtype="<f4").tobytes()))
        writer.submit(np.full(480, -0.5, dtype="<f4").tobytes())
        wait_for(lambda: writer.snapshot.sent == 11)

    def test_expired_native_voice_does_not_replay(self):
        writer = self.writer()
        writer.submit(np.full(480, 0.5, dtype="<f4").tobytes())
        wait_for(lambda: writer.snapshot.sent == 1)
        self.dll.rv_test_advance(2_000_001)
        self.assertEqual(self.read(), bytes(960))

    def test_lost_native_lease_fails_closed_without_automatic_reenable(self):
        writer = self.writer()
        self.dll.rv_test_advance(20_000_001)
        writer.submit(np.full(480, 0.5, dtype="<f4").tobytes())
        wait_for(lambda: writer.snapshot.state == MicWriterState.FAILED)
        writer.stop()
        self.assertFalse(writer.is_available())
        self.assertFalse(writer.submit(bytes(1920)))
        self.assertEqual(self.read(), bytes(960))
        fresh = self.writer()
        fresh.submit(np.full(480, -0.5, dtype="<f4").tobytes())
        wait_for(lambda: fresh.snapshot.sent == 1)
        self.assertEqual(self.read(), float_mono_to_pcm16(np.full(480, -0.5, dtype="<f4").tobytes()))
