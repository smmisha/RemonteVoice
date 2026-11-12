import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from remontevoice.driver_mic import (
    DriverBufferFull, DriverMicError, DriverMicStopError, DriverMicWriter,
    MicCommand, MicWriterState, encode_command, float_mono_to_pcm16,
)


def frame(value=0.25):
    return np.full(480, value, dtype="<f4").tobytes()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Writer did not reach the expected state")
        time.sleep(0.002)


class Transport:
    def __init__(self, action=None):
        self.messages = []
        self.closed = False
        self.action = action
        self.threads = []

    def send(self, message):
        self.threads.append(threading.get_ident())
        self.messages.append(message)
        if self.action:
            self.action(MicCommand(struct.unpack_from("<I", message, 4)[0]))

    def close(self):
        self.threads.append(threading.get_ident())
        self.closed = True

    @property
    def commands(self):
        return [MicCommand(struct.unpack_from("<I", m, 4)[0]) for m in self.messages]


class DriverMicProtocolTests(unittest.TestCase):
    def test_exact_native_layout_and_zero_control_payload(self):
        for command in (MicCommand.BEGIN, MicCommand.END, MicCommand.KEEPALIVE):
            packet = encode_command(command, 0x1122334455667788, b"n" * 16)
            self.assertEqual(len(packet), 1000)
            self.assertEqual(struct.unpack_from("<IIQ16sII", packet),
                             (1, command, 0x1122334455667788, b"n" * 16, 0, 0))
            self.assertEqual(packet[40:], bytes(960))
        pcm = b"\x01\x80" * 480
        packet = encode_command(MicCommand.WRITE, 1, b"n" * 16, pcm)
        self.assertEqual(packet[40:], pcm)
        self.assertEqual(struct.unpack_from("<I", packet, 32)[0], 960)

    def test_invalid_wire_inputs_are_rejected(self):
        for session in (0, -1, 2**64, True):
            with self.assertRaises(ValueError):
                encode_command(MicCommand.BEGIN, session, b"n" * 16)
        for nonce in (bytes(16), b"n" * 15, b"n" * 17, bytearray(b"n" * 16)):
            with self.assertRaises(ValueError):
                encode_command(MicCommand.BEGIN, 1, nonce)
        for command, pcm in ((1, b""), (MicCommand.WRITE, b""), (MicCommand.BEGIN, bytes(960)),
                             (MicCommand.WRITE, bytes(959)), (MicCommand.WRITE, bytes(961))):
            with self.assertRaises(ValueError):
                encode_command(command, 1, b"n" * 16, pcm)

    def test_pcm_endianness_clipping_and_signed_range(self):
        source = np.resize(np.array([-2, -1, -0.5, 0, 0.5, 1, 2], dtype="<f4"), 480)
        output = float_mono_to_pcm16(source.tobytes())
        self.assertEqual(len(output), 960)
        self.assertEqual(np.frombuffer(output, dtype="<i2")[:7].tolist(),
                         [-32768, -32768, -16384, 0, 16384, 32767, 32767])

    def test_nonfinite_and_wrong_frames_are_rejected(self):
        for value in (np.nan, np.inf, -np.inf):
            with self.assertRaises(ValueError):
                float_mono_to_pcm16(frame(value))
        for payload in (b"", bytes(1919), bytes(1921), bytearray(1920)):
            with self.assertRaises(ValueError):
                float_mono_to_pcm16(payload)


class DriverMicWorkerTests(unittest.TestCase):
    def setUp(self):
        self.writers = []
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for writer in self.writers:
            writer.stop()
            self.assertFalse(writer.snapshot.worker_alive)

    def writer(self, factory, **kwargs):
        writer = DriverMicWriter(factory, **kwargs)
        self.writers.append(writer)
        return writer

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def stream(self, transport=None, **kwargs):
        transport = transport or Transport()
        writer = self.writer(lambda: transport, **kwargs)
        writer.start()
        wait_for(lambda: writer.is_available())
        return writer, transport

    def test_constructor_and_idle_stop_never_open_or_enable(self):
        factory = Mock()
        writer = self.writer(factory)
        self.assertFalse(writer.is_available())
        self.assertFalse(writer.submit(frame()))
        writer.stop()
        factory.assert_not_called()
        self.assertEqual(writer.snapshot.state, MicWriterState.CLOSED)
        with self.assertRaises(DriverMicError):
            writer.start()

    def test_roundtrip_order_and_all_native_calls_on_one_worker(self):
        transport = Transport()
        factory_threads = []
        def factory():
            factory_threads.append(threading.get_ident())
            return transport
        writer = self.writer(factory)
        writer.start(); wait_for(writer.is_available)
        self.assertTrue(writer.submit(frame()))
        wait_for(lambda: writer.snapshot.sent == 1)
        writer.stop()
        self.assertEqual(transport.commands, [MicCommand.BEGIN, MicCommand.WRITE, MicCommand.END])
        self.assertTrue(transport.closed)
        self.assertEqual(set(transport.threads), set(factory_threads))
        self.assertNotEqual(factory_threads[0], threading.get_ident())
        self.assertEqual(writer.snapshot.state, MicWriterState.CLOSED)

    def test_begin_is_not_readiness_until_acknowledged(self):
        entered, release = self.event(), self.event()
        def action(command):
            if command == MicCommand.BEGIN:
                entered.set(); release.wait(2)
        writer = self.writer(lambda: Transport(action))
        writer.start(); self.assertTrue(entered.wait(1))
        self.assertFalse(writer.is_available())
        self.assertFalse(writer.submit(frame()))
        release.set(); wait_for(writer.is_available)

    def test_fresh_activation_has_new_identity_old_writer_stays_closed(self):
        first, a = self.stream(); first.stop()
        second, b = self.stream()
        self.assertNotEqual(a.messages[0][8:32], b.messages[0][8:32])
        self.assertFalse(first.submit(frame()))
        with self.assertRaises(DriverMicError):
            first.start()
        self.assertTrue(second.is_available())

    def test_queue_overrun_discards_old_voice_without_disabling(self):
        entered, release = self.event(), self.event()
        calls = 0
        def action(command):
            nonlocal calls
            if command == MicCommand.WRITE:
                calls += 1
                if calls == 1:
                    entered.set(); release.wait(2)
        writer, transport = self.stream(Transport(action))
        writer.submit(frame(0)); self.assertTrue(entered.wait(1))
        for i in range(30):
            self.assertTrue(writer.submit(frame(i / 100)))
        self.assertEqual(writer.snapshot.queued, 10)
        self.assertEqual(writer.snapshot.dropped, 20)
        self.assertTrue(writer.is_available())
        release.set(); wait_for(lambda: writer.snapshot.sent == 11)
        packets = [m[40:] for m in transport.messages if struct.unpack_from("<I", m, 4)[0] == 2]
        self.assertEqual(packets[1], float_mono_to_pcm16(frame(0.20)))
        self.assertEqual(packets[-1], float_mono_to_pcm16(frame(0.29)))

    def test_kernel_queue_full_is_a_drop_not_session_failure(self):
        attempts = 0
        def action(command):
            nonlocal attempts
            if command == MicCommand.WRITE:
                attempts += 1
                if attempts <= 2:
                    raise DriverBufferFull()
        writer, _ = self.stream(Transport(action))
        for _ in range(3): writer.submit(frame())
        wait_for(lambda: writer.snapshot.sent == 1)
        self.assertEqual(writer.snapshot.dropped, 2)
        self.assertTrue(writer.is_available())

    def test_invalid_samples_do_not_damage_an_active_lease(self):
        writer, _ = self.stream()
        for data in (bytes(1919), frame(np.nan), frame(np.inf), 1920, memoryview(bytes(4096))):
            self.assertFalse(writer.submit(data))
        self.assertTrue(writer.is_available())
        data = bytearray(frame(0.5))
        self.assertTrue(writer.submit(data)); data[:] = bytes(1920)
        wait_for(lambda: writer.snapshot.sent == 1)

    def test_queued_pcm_expires_instead_of_replaying_late(self):
        entered, release = self.event(), self.event()
        now = [10.0]
        def action(command):
            if command == MicCommand.WRITE and not entered.is_set():
                entered.set(); release.wait(2)
        writer, transport = self.stream(Transport(action), clock=lambda: now[0])
        writer.submit(frame()); self.assertTrue(entered.wait(1))
        writer.submit(frame()); now[0] += 0.11; release.set()
        wait_for(lambda: writer.snapshot.dropped == 1)
        self.assertEqual(transport.commands.count(MicCommand.WRITE), 1)

    def test_stop_discards_queued_voice_and_late_ack_cannot_reenable(self):
        entered, release = self.event(), self.event()
        def action(command):
            if command == MicCommand.BEGIN:
                entered.set(); release.wait(2)
        transport = Transport(action); writer = self.writer(lambda: transport)
        writer.start(); self.assertTrue(entered.wait(1)); writer.request_stop()
        self.assertFalse(writer.submit(frame()))
        release.set(); writer.stop()
        self.assertEqual(transport.commands, [MicCommand.BEGIN, MicCommand.END])
        self.assertFalse(writer.is_available())

    def test_hung_native_call_reports_failure_and_retains_handle_until_return(self):
        entered, release = self.event(), self.event()
        now = [10.0]
        def action(command):
            if command == MicCommand.BEGIN:
                entered.set(); release.wait(2)
        transport = Transport(action)
        writer = self.writer(lambda: transport, clock=lambda: now[0])
        writer.start(); self.assertTrue(entered.wait(1)); now[0] += 0.6
        wait_for(lambda: writer.snapshot.state == MicWriterState.FAILED)
        self.assertEqual(writer.snapshot.error, "driver_call_timeout")
        before = time.monotonic()
        with self.assertRaises(DriverMicStopError): writer.stop(0.01)
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertFalse(transport.closed)
        self.assertTrue(writer.snapshot.worker_alive)
        release.set(); writer.stop()
        self.assertEqual(transport.commands, [MicCommand.BEGIN, MicCommand.END])
        self.assertTrue(transport.closed)
        self.assertEqual(writer.snapshot.state, MicWriterState.FAILED)

    def test_elapsed_deadline_detected_even_without_watchdog_scheduling(self):
        now = [10.0]
        def action(command):
            if command == MicCommand.BEGIN: now[0] += 0.6
        writer = self.writer(lambda: Transport(action), clock=lambda: now[0])
        writer.start(); wait_for(lambda: writer.snapshot.state == MicWriterState.FAILED)
        writer.stop()
        self.assertEqual(writer.snapshot.error, "driver_call_timeout")

    def test_stop_during_open_does_not_send_begin_after_open_returns(self):
        entered, release = self.event(), self.event(); transport = Transport()
        def factory():
            entered.set(); release.wait(2); return transport
        writer = self.writer(factory); writer.start(); self.assertTrue(entered.wait(1))
        writer.request_stop(); release.set(); writer.stop()
        self.assertEqual(transport.commands, [])
        self.assertTrue(transport.closed)

    def test_idle_enabled_lease_has_keepalive(self):
        writer, transport = self.stream()
        wait_for(lambda: MicCommand.KEEPALIVE in transport.commands)
        self.assertTrue(writer.is_available())
        self.assertEqual(writer.snapshot.sent, 0)

    def test_factory_failure_is_not_ready_and_error_has_no_private_details(self):
        writer = self.writer(Mock(side_effect=OSError("private path / token")))
        writer.start(); wait_for(lambda: writer.snapshot.state == MicWriterState.FAILED)
        self.assertFalse(writer.is_available())
        self.assertEqual(writer.snapshot.error, "driver_transport_failed")

    def test_lost_begin_ack_still_attempts_end_and_closes(self):
        def action(command):
            if command == MicCommand.BEGIN: raise OSError("ack lost")
        transport = Transport(action); writer = self.writer(lambda: transport)
        writer.start(); wait_for(lambda: transport.closed)
        self.assertEqual(transport.commands, [MicCommand.BEGIN, MicCommand.END])
        self.assertEqual(writer.snapshot.state, MicWriterState.FAILED)

    def test_failed_close_retains_transport_and_never_claims_cleanup(self):
        # A fake handle only; this failure scenario allocates no OS resources.
        transport = Transport()
        transport.close = Mock(side_effect=OSError("close failed"))
        writer = DriverMicWriter(lambda: transport)
        writer.start(); wait_for(writer.is_available); writer.request_stop()
        wait_for(lambda: not writer.snapshot.worker_alive)
        with self.assertRaises(DriverMicStopError): writer.stop()
        self.assertFalse(writer.snapshot.cleanup_confirmed)
        self.assertIs(writer._transport, transport)
        self.assertEqual(writer.snapshot.state, MicWriterState.FAILED)
        self.assertEqual(writer.snapshot.error, "driver_close_failed")

    def test_bad_deadlines_are_rejected(self):
        for value in (-1, 0, 2, np.nan, np.inf):
            with self.assertRaises(ValueError): DriverMicWriter(Transport, call_timeout=value)
        writer = self.writer(Transport)
        for value in (-1, 6, np.nan, np.inf):
            with self.assertRaises(ValueError): writer.stop(value)


class WindowsDriverTransportTests(unittest.TestCase):
    def test_no_device_or_ambiguous_metadata_never_opens_a_filter(self):
        from remontevoice.windows_driver_transport import DriverEndpoint, WindowsDriverMicTransport
        for endpoints in ([], [DriverEndpoint("a"), DriverEndpoint("b")]):
            api = Mock(); api.discover.return_value = endpoints
            with patch("remontevoice.windows_driver_transport._WindowsApi", return_value=api):
                with self.assertRaises(DriverMicError): WindowsDriverMicTransport()
            api.kernel.CreateFileW.assert_not_called()

    def test_real_ioctl_header_buffer_and_close_are_formed_correctly(self):
        import ctypes
        from remontevoice.windows_driver_transport import DriverEndpoint, WindowsDriverMicTransport
        api = Mock(); api.discover.return_value = [DriverEndpoint("verified root filter")]
        api.kernel.CreateFileW.return_value = 123
        packet = encode_command(MicCommand.BEGIN, 1, b"n" * 16)
        def check(handle, code, prop, prop_size, data, data_size, returned, overlapped):
            self.assertEqual((handle, code, prop_size, data_size, overlapped), (123, 0x2f0003, 24, 1000, None))
            self.assertEqual(ctypes.string_at(prop, 24),
                             bytes.fromhex("aabf47ee5096d541afa009ce9f32e71b") + struct.pack("<II", 0, 2))
            self.assertEqual(ctypes.string_at(data, data_size), packet)
            return 1
        api.kernel.DeviceIoControl.side_effect = check
        with patch("remontevoice.windows_driver_transport._WindowsApi", return_value=api):
            transport = WindowsDriverMicTransport(); transport.send(packet); transport.close(); transport.close()
        api.kernel.CloseHandle.assert_called_once_with(123)

    def test_only_queue_busy_is_classified_as_packet_drop(self):
        from remontevoice.windows_driver_transport import DriverEndpoint, WindowsDriverMicTransport
        api = Mock(); api.discover.return_value = [DriverEndpoint("verified root filter")]
        api.kernel.CreateFileW.return_value = 123; api.kernel.DeviceIoControl.return_value = 0
        with patch("remontevoice.windows_driver_transport._WindowsApi", return_value=api):
            transport = WindowsDriverMicTransport()
            for code, error in ((170, DriverBufferFull), (32, DriverMicError), (5, DriverMicError), (6, DriverMicError)):
                with patch("remontevoice.windows_driver_transport.ct.get_last_error", return_value=code, create=True):
                    with self.assertRaises(error): transport.send(encode_command(MicCommand.BEGIN, 1, b"n" * 16))
            transport.close()

    def test_wrong_thread_cannot_send_or_close_pending_handle(self):
        from remontevoice.windows_driver_transport import DriverEndpoint, WindowsDriverMicTransport
        api = Mock(); api.discover.return_value = [DriverEndpoint("verified root filter")]
        api.kernel.CreateFileW.return_value = 123
        with patch("remontevoice.windows_driver_transport._WindowsApi", return_value=api):
            transport = WindowsDriverMicTransport()
        errors = []
        def wrong_thread():
            for action in (lambda: transport.send(bytes(1000)), transport.close):
                try: action()
                except DriverMicError: errors.append(True)
        worker = threading.Thread(target=wrong_thread); worker.start(); worker.join()
        self.assertEqual(len(errors), 2)
        api.kernel.DeviceIoControl.assert_not_called(); api.kernel.CloseHandle.assert_not_called()
        transport.close()
