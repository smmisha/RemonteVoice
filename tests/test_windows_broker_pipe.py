import ctypes
import gc
import unittest
import weakref

from remotevoice import windows_broker_pipe as pipe


class FakeApi:
    """Models both start status and later completion, including uncancellable I/O."""
    def __init__(self):
        self.now = 10.0
        self.created = []
        self.opened = []
        self.closed = []
        self.disconnects = []
        self.cancelled = []
        self.waits = []
        self.starts = []
        self.steps = []
        self.open_results = []
        self.wait_pipe_results = []
        self.flags = 0
        self.event_count = 0
        self.close_fails = set()
        self.disconnect_error = 0
        self.pipe_handle = (1 << 40) + 1

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.now += duration

    def create_server(self, *arguments):
        self.created.append(arguments)
        return self.pipe_handle

    def open_client(self, *arguments):
        self.opened.append(arguments)
        return self.open_results.pop(0) if self.open_results else (self.pipe_handle, 0)

    def wait_pipe(self, name, milliseconds):
        self.now += milliseconds / 1000
        return self.wait_pipe_results.pop(0) if self.wait_pipe_results else 121

    def pipe_flags(self, handle):
        return self.flags

    def event(self):
        self.event_count += 1
        return (1 << 40) + 100 + self.event_count

    def start(self, operation, handle, pending):
        step = self.steps.pop(0) if self.steps else {}
        pending.step = step
        self.starts.append((operation, handle, pending, pending.buffer.raw))
        payload = step.get("data", b"x" * pending.size)
        if operation == "read":
            ctypes.memmove(pending.buffer, payload, min(len(payload), pending.size))
        if step.get("raise_start"):
            raise RuntimeError("native detail must never reach caller")
        self.now += step.get("elapsed", 0)
        return step.get("error", 0), step.get("count", len(payload) if operation == "read" else pending.size)

    def result(self, handle, pending):
        step = pending.step
        if step.get("raise_result"):
            raise RuntimeError("native detail must never reach caller")
        results = step.get("results", [])
        if len(results) > 1:
            return results.pop(0)
        return results[0] if results else (996, 0)

    def wait(self, event, milliseconds):
        self.waits.append((event, milliseconds))
        pending = next(item[2] for item in self.starts if item[2].overlapped.hEvent == event)
        result = pending.step.get("wait", 0)
        if result == 258:
            self.now += milliseconds / 1000
        return result

    def cancel(self, handle, pending):
        self.cancelled.append((handle, pending))
        return pending.step.get("cancel_error", 0)

    def disconnect(self, handle):
        self.disconnects.append(handle)
        return self.disconnect_error

    def close(self, handle):
        self.closed.append(handle)
        return handle not in self.close_fails


class NativePipeTests(unittest.TestCase):
    def test_failed_completion_query_cannot_release_a_pending_native_buffer(self):
        connection = self.connection()
        self.api.steps = [{'error': 997, 'wait': 0, 'results': [(6, 0)]}]
        with self.assertRaises(pipe.PipeCleanupError):
            connection.read_exact(8, 0.1)
        self.assertTrue(connection.cleanup_pending)
        self.assertEqual(self.api.closed, [])
        with self.assertRaises(pipe.PipeCleanupError):
            connection.close()
        self.api.starts[-1][2].step['results'] = [(995, 0)]
        connection.close()
        self.assertTrue(connection.cleanup_confirmed)

    def setUp(self):
        self.api = FakeApi()
        self.objects = []

    def tearDown(self):
        # Clear fake pending requests only after explicitly completing them.
        for operation in self.api.starts:
            operation[2].step["results"] = [(995, 0)]
            operation[2].step.pop("raise_result", None)
        self.api.close_fails.clear()
        self.api.disconnect_error = 0
        for item in reversed(self.objects):
            item.close()

    def server(self):
        server = pipe.NativePipeServer("RemoteVoice-unit", "S-1-5-5-1-42", api=self.api)
        self.objects.append(server)
        return server

    def connection(self):
        connection = pipe.NativePipeConnection(self.api.pipe_handle, api=self.api)
        self.objects.append(connection)
        return connection

    def test_server_has_first_single_local_byte_instance_and_narrow_exact_logon_acl(self):
        server = self.server()
        name, access, mode, instances, size, sddl = self.api.created[0]
        self.assertEqual(name, "\\\\.\\pipe\\RemoteVoice-unit")
        self.assertEqual(access, pipe.PIPE_ACCESS_DUPLEX | pipe.FILE_FLAG_FIRST_PIPE_INSTANCE | pipe.FILE_FLAG_OVERLAPPED)
        self.assertEqual(mode, pipe.PIPE_REJECT_REMOTE_CLIENTS)  # byte read/type/wait modes are zero.
        self.assertEqual((instances, size), (1, 1928))
        self.assertEqual(sddl, "D:P(A;;GA;;;SY)(A;;0x0012019b;;;S-1-5-5-1-42)")
        self.assertEqual(pipe.CLIENT_ACCESS & 4, 0)  # No FILE_CREATE_PIPE_INSTANCE.
        self.assertNotIn("GW", sddl)
        self.assertNotIn(";;;WD", sddl)
        self.assertGreater(server.handle, 2**32)

    def test_name_and_sid_injection_rejected_before_native_api(self):
        for name in ("other", "RemoteVoice-", "RemoteVoice-a\\nested", "RemoteVoice-a\0b",
                     "\\\\server\\pipe\\RemoteVoice-a", "\\\\.\\pipe\\RemoteVoice-a/b",
                     "RemoteVoice-a.b", "RemoteVoice-" + "a" * 65, None):
            with self.assertRaises(ValueError):
                pipe.NativePipeServer(name, "S-1-5-5-1-42", api=self.api)
            with self.assertRaises(ValueError):
                pipe.NativePipeClient.connect(name, 0.1, api=self.api)
        for sid in ("S-1-5-18", "S-1-5-21-1", "S-1-5-5-01-2", "S-1-5-5-1-4294967296",
                    "S-1-5-5-1-2)(A;;GA;;;WD)", "S-1-5-5-1-2\0", None):
            with self.assertRaises(ValueError):
                pipe.NativePipeServer("RemoteVoice-test", sid, api=self.api)
        self.assertEqual(self.api.created, [])
        self.assertEqual(self.api.opened, [])

    def test_pipe_connected_race_and_connection_disconnect_reuses_original_instance(self):
        server = self.server()
        self.api.steps = [{"error": 535}, {}]
        connection = server.accept(0.2)
        self.objects.append(connection)
        with self.assertRaises(pipe.PipeError):
            server.accept(0.2)
        with self.assertRaises(pipe.PipeCleanupError):
            server.close()
        connection.close()
        self.assertEqual(self.api.disconnects, [server.handle])
        self.assertNotIn(server.handle, self.api.closed)
        second = server.accept(0.2)
        self.objects.append(second)
        self.assertEqual(len(self.api.created), 1)
        self.assertIsNot(second, connection)

    def test_partial_reads_and_writes_share_deadline_and_preserve_bytes(self):
        connection = self.connection()
        self.api.steps = [{"data": b"RV", "count": 2}, {"data": b"MB1234", "count": 6},
                          {"count": 2}, {"count": 6}]
        self.assertEqual(connection.read_exact(8, 0.2), b"RVMB1234")
        connection.write_all(b"RVMB1234", 0.2)
        writes = [item for item in self.api.starts if item[0] == "write"]
        self.assertEqual([item[2].size for item in writes], [8, 6])
        self.assertEqual([item[3] for item in writes], [b"RVMB1234", b"MB1234"])
        self.assertEqual(len(self.api.closed), 4)  # Each distinct event is released.

    def test_pending_completion_reads_data_and_releases_event_after_result(self):
        connection = self.connection()
        self.api.steps = [{"error": 997, "data": b"data", "results": [(0, 4)]}]
        self.assertEqual(connection.read_exact(4, 0.2), b"data")
        self.assertFalse(connection.cleanup_pending)
        self.assertEqual(self.api.cancelled, [])
        self.assertEqual(self.api.closed, [self.api.starts[0][2].overlapped.hEvent])

    def test_partial_transfers_cannot_reset_the_whole_operation_deadline(self):
        connection = self.connection()
        self.api.steps = [{"data": b"RV", "count": 2, "elapsed": 0.11}, {}]
        with self.assertRaises(pipe.PipeTimeoutError):
            connection.read_exact(8, 0.1)
        self.assertTrue(connection.failed)
        self.assertEqual(len(self.api.starts), 1)

    def test_wait_failure_still_joins_cancel_before_releasing_native_memory(self):
        connection = self.connection()
        self.api.steps = [{"error": 997, "wait": 0xFFFFFFFF,
                           "results": [(996, 0), (995, 0)]}]
        with self.assertRaises(pipe.PipeError):
            connection.write_all(b"frame", 0.1)
        self.assertEqual(len(self.api.cancelled), 1)
        self.assertFalse(connection.cleanup_pending)
        self.assertTrue(connection.failed)

    def test_accept_timeout_cancels_and_checks_completion_before_reuse(self):
        server = self.server()
        self.api.steps = [{"error": 997, "wait": 258, "results": [(996, 0), (995, 0)]}, {}]
        self.assertIsNone(server.accept(0.1))
        self.assertFalse(server.failed)
        self.assertFalse(server.cleanup_pending)
        self.assertEqual(len(self.api.cancelled), 1)
        self.assertEqual(self.api.disconnects, [server.handle])
        connection = server.accept(0.2)
        self.objects.append(connection)
        self.assertIsNotNone(connection)

    def test_partial_read_timeout_invalidates_connection_even_when_cancel_completed(self):
        connection = self.connection()
        self.api.steps = [{"data": b"RV", "count": 2},
                          {"error": 997, "wait": 258, "results": [(996, 0), (995, 0)]}]
        with self.assertRaises(pipe.PipeTimeoutError):
            connection.read_exact(8, 0.1)
        self.assertTrue(connection.failed)
        self.assertFalse(connection.cleanup_pending)
        with self.assertRaises(pipe.PipeError):
            connection.read_exact(8, 0.1)
        self.assertEqual(len(self.api.starts), 2)

    def test_unconfirmed_cancel_retains_buffer_event_handle_and_object_until_close_confirms(self):
        connection = self.connection()
        self.api.steps = [{"error": 997, "wait": 258, "results": [(996, 0)], "cancel_error": 1168}]
        with self.assertRaises(pipe.PipeCleanupError):
            connection.read_exact(8, 0.1)
        pending = connection._pending
        self.assertTrue(connection.failed)
        self.assertTrue(connection.cleanup_pending)
        self.assertLessEqual(self.api.waits[-1][1], 50)
        self.assertEqual(self.api.closed, [])
        with self.assertRaises(pipe.PipeCleanupError):
            connection.close()
        self.assertEqual(self.api.closed, [])
        reference = weakref.ref(connection)
        self.objects.remove(connection)
        del connection
        gc.collect()
        self.assertIsNotNone(reference())  # Global ownership survives caller abandonment.
        self.assertIn(reference(), pipe._RETAINED)
        pending.step["results"] = [(995, 0)]
        reference().close()
        self.assertIn(pending.overlapped.hEvent, self.api.closed)
        self.assertIn(self.api.pipe_handle, self.api.closed)
        self.assertFalse(any(item is reference() for item in pipe._RETAINED))

    def test_uncancellable_accept_fails_server_and_prevents_second_instance(self):
        server = self.server()
        self.api.steps = [{"error": 997, "wait": 258, "results": [(996, 0)]}]
        with self.assertRaises(pipe.PipeCleanupError):
            server.accept(0.1)
        with self.assertRaises(pipe.PipeError):
            server.accept(0.1)
        self.assertTrue(server.failed)
        self.assertEqual(len(self.api.starts), 1)
        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.api.closed, [])

    def test_zero_transfer_eof_more_data_and_errors_are_sanitized_and_fail_connection(self):
        for error, count, expected in ((0, 0, pipe.PipeEOFError), (109, 0, pipe.PipeEOFError),
                                       (233, 0, pipe.PipeEOFError), (234, 2, pipe.PipeError),
                                       (5, 0, pipe.PipeError), (0, 9, pipe.PipeError)):
            connection = self.connection()
            self.api.steps = [{"error": error, "count": count}]
            with self.assertRaises(expected) as raised:
                connection.read_exact(8, 0.1)
            self.assertTrue(connection.failed)
            self.assertNotIn("RemoteVoice-unit", str(raised.exception))
            self.assertFalse(connection.cleanup_pending)
            connection.close()

    def test_start_exception_retains_operation_and_close_can_recover(self):
        connection = self.connection()
        self.api.steps = [{"raise_start": True, "results": [(995, 0)]}]
        with self.assertRaises(pipe.PipeCleanupError) as raised:
            connection.write_all(b"frame", 0.1)
        self.assertNotIn("native detail", str(raised.exception))
        self.assertTrue(connection.cleanup_pending)
        self.assertEqual(self.api.closed, [])
        connection.close()
        self.assertFalse(connection.cleanup_pending)
        self.assertTrue(connection.cleanup_confirmed)

    def test_result_adapter_exception_retains_pending_memory_and_sanitizes_error(self):
        connection = self.connection()
        self.api.steps = [{"error": 997, "raise_result": True}]
        with self.assertRaises(pipe.PipeCleanupError) as raised:
            connection.read_exact(8, 0.1)
        self.assertNotIn("native detail", str(raised.exception))
        self.assertTrue(connection.cleanup_pending)
        self.assertFalse(connection.cleanup_confirmed)
        self.assertEqual(self.api.closed, [])
        with self.assertRaises(pipe.PipeCleanupError):
            connection.close()
        self.api.starts[0][2].step.pop("raise_result")
        self.api.starts[0][2].step["results"] = [(995, 0)]
        connection.close()
        self.assertTrue(connection.cleanup_confirmed)

    def test_invalid_sizes_and_deadlines_do_not_issue_io(self):
        connection = self.connection()
        for size in (0, -1, 1929, True, 1.0, None):
            with self.assertRaises(ValueError):
                connection.read_exact(size, 0.1)
        for data in (b"", bytes(1929), bytearray(b"x"), "x", None):
            with self.assertRaises(ValueError):
                connection.write_all(data, 0.1)
        for timeout in (0, -1, 1.01, float("nan"), float("inf"), True, "1"):
            with self.assertRaises(ValueError):
                connection.read_exact(8, timeout)
            with self.assertRaises(ValueError):
                pipe.NativePipeClient.connect("RemoteVoice-unit", timeout, api=self.api)
        self.assertEqual(self.api.starts, [])
        self.assertEqual(self.api.opened, [])

    def test_client_uses_identification_sqos_specific_rights_and_no_create_instance(self):
        connection = pipe.NativePipeClient.connect("\\\\.\\pipe\\RemoteVoice-client", 0.2, api=self.api)
        self.objects.append(connection)
        name, access, flags = self.api.opened[0]
        self.assertEqual(name, "\\\\.\\pipe\\RemoteVoice-client")
        self.assertEqual(access, pipe.CLIENT_ACCESS)
        self.assertEqual(access & 4, 0)
        self.assertEqual(flags, pipe.FILE_FLAG_OVERLAPPED | pipe.SECURITY_SQOS_PRESENT | pipe.SECURITY_IDENTIFICATION)

    def test_client_busy_wait_is_bounded_and_message_pipe_is_rejected_and_closed(self):
        self.api.open_results = [(None, 231)]
        with self.assertRaises(pipe.PipeTimeoutError):
            pipe.NativePipeClient.connect("RemoteVoice-unit", 0.1, api=self.api)
        self.assertEqual(len(self.api.opened), 1)
        self.api.flags = 4
        with self.assertRaises(pipe.PipeError):
            pipe.NativePipeClient.connect("RemoteVoice-unit", 0.1, api=self.api)
        self.assertIn(self.api.pipe_handle, self.api.closed)

    def test_event_and_handle_close_failures_keep_ownership_for_retry(self):
        connection = self.connection()
        event = (1 << 40) + 101
        self.api.close_fails.add(event)
        with self.assertRaises(pipe.PipeCleanupError):
            connection.write_all(b"frame", 0.1)
        self.assertTrue(connection.cleanup_pending)
        self.assertTrue(connection._pending.complete)
        self.api.close_fails.clear()
        self.api.close_fails.add(self.api.pipe_handle)
        with self.assertRaises(pipe.PipeCleanupError):
            connection.close()
        self.assertFalse(connection.cleanup_pending)
        self.assertEqual(connection.handle, self.api.pipe_handle)
        self.api.close_fails.clear()
        connection.close()
        with self.assertRaises(pipe.PipeError):
            _ = connection.handle
        connection.close()  # Idempotent after confirmed cleanup.

    def test_disconnect_failure_does_not_release_server_active_connection(self):
        server = self.server()
        connection = server.accept(0.1)
        self.objects.append(connection)
        self.api.disconnect_error = 5
        with self.assertRaises(pipe.PipeCleanupError):
            connection.close()
        with self.assertRaises(pipe.PipeError):
            server.accept(0.1)
        with self.assertRaises(pipe.PipeCleanupError):
            server.close()
        self.api.disconnect_error = 0
        connection.close()
        server.close()


if __name__ == "__main__":
    unittest.main()
