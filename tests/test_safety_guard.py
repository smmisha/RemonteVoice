import io
import json
import os
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from remontevoice.host_lease import HostLeaseError
from remontevoice.input_routing import InputRoute as MemoryInputRoute
from remontevoice.output_routing import OutputRoute as MemoryOutputRoute
from remontevoice.safety_guard import (
    SafetyGuard, SafetyGuardError, _Snapshot, _WindowsOwner,
    _MAX_MESSAGE_BYTES, _persist_receipt, _read_message, _recover, _run_guard,
)


def snapshot():
    return _Snapshot.parse({
        "type": "arm", "version": 1,
        "data_dir": str(Path.cwd().resolve() / "unused-guard-data"),
        "owner_pid": 1234, "output_remote": "PRIVATE-virtual-output",
        "output_fallback": "PRIVATE-physical-output", "input_remote": "PRIVATE-virtual-input",
        "input_fallback": "PRIVATE-physical-input",
    })


def messages(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


class FakeOwner:
    def __init__(self, *, alive=True, exit_allowed=None):
        self.is_alive = alive
        self.exit_allowed = exit_allowed
        self.waiting = threading.Event()
        self.exited = False
        self.closed = False

    def alive(self):
        return self.is_alive

    def wait_for_exit(self):
        self.waiting.set()
        if self.exit_allowed is not None and not self.exit_allowed.wait(1):
            raise AssertionError("Controlled owner did not exit")
        self.exited = True

    def close(self):
        self.closed = True


class FakeLease:
    def __init__(self, *, unavailable=False):
        self.held = False
        self.unavailable = unavailable
        self.acquire_count = 0
        self.release_count = 0

    def acquire(self):
        self.acquire_count += 1
        if self.unavailable:
            raise HostLeaseError("controlled lease conflict")
        self.held = True

    def release(self):
        self.release_count += 1
        self.held = False


class SafetyGuardProtocolTests(unittest.TestCase):
    def test_snapshot_allowlists_fields_and_rejects_invalid_values(self):
        valid = snapshot().message()
        invalid = [
            {**valid, "credential": "never-accepted"},
            {**valid, "data_dir": "relative"},
            {**valid, "owner_pid": True},
            {**valid, "owner_pid": 0},
            {**valid, "version": True},
            {**valid, "output_remote": ""},
            {**valid, "input_fallback": "bad\0id"},
            {**valid, "input_fallback": valid["input_remote"]},
            {**valid, "output_fallback": valid["output_remote"]},
            {**valid, "output_fallback": "x" * 1025},
        ]
        self.assertEqual(_Snapshot.parse(valid), snapshot())
        for message in invalid:
            with self.subTest(fields=list(message)), self.assertRaises(SafetyGuardError):
                _Snapshot.parse(message)

    def test_framing_is_bounded_and_requires_one_json_object(self):
        self.assertIsNone(_read_message(io.BytesIO()))
        for value in (b"{}", b"[]\n", b"not-json\n", b"x" * (_MAX_MESSAGE_BYTES + 1)):
            with self.subTest(length=len(value)), self.assertRaises(SafetyGuardError):
                _read_message(io.BytesIO(value))

    def test_owner_must_be_alive_before_ready(self):
        owner = FakeOwner(alive=False)
        replies = io.BytesIO()
        lease_factory, recover = MagicMock(), MagicMock()
        with self.assertRaises(SafetyGuardError):
            _run_guard(snapshot(), io.BytesIO(), replies, owner_factory=lambda pid: owner,
                       lease_factory=lease_factory, recover=recover)
        self.assertEqual(replies.getvalue(), b"")
        self.assertTrue(owner.closed)
        lease_factory.assert_not_called()
        recover.assert_not_called()

    def test_clean_disarm_never_waits_for_exit_or_touches_routes(self):
        owner, replies = FakeOwner(), io.BytesIO()
        lease_factory, recover = MagicMock(), MagicMock()
        result = _run_guard(snapshot(), io.BytesIO(b'{"type":"disarm","version":1}\n'), replies,
                            owner_factory=lambda pid: owner, lease_factory=lease_factory, recover=recover)
        self.assertEqual(result, 0)
        self.assertEqual(messages(replies), [{"type": "ready", "version": 1}])
        self.assertFalse(owner.waiting.is_set())
        self.assertTrue(owner.closed)
        lease_factory.assert_not_called()
        recover.assert_not_called()

    def test_eof_with_live_owner_cannot_acquire_lease_or_recover(self):
        exited = threading.Event()
        owner, lease, replies = FakeOwner(exit_allowed=exited), FakeLease(), io.BytesIO()
        results = []

        def recover(value):
            self.assertTrue(owner.exited)
            self.assertTrue(lease.held)
            self.assertEqual(value, snapshot())
            return {"output_restored": True, "input_restored": True, "microphone_restored": True}

        worker = threading.Thread(target=lambda: results.append(_run_guard(
            snapshot(), io.BytesIO(), replies, owner_factory=lambda pid: owner,
            lease_factory=lambda path: lease, recover=recover,
        )), name="controlled-guard-test")
        worker.start()
        try:
            self.assertTrue(owner.waiting.wait(1))
            self.assertEqual(lease.acquire_count, 0)
            self.assertEqual(results, [])
        finally:
            exited.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [0])
        self.assertEqual(lease.acquire_count, 1)
        self.assertEqual(lease.release_count, 1)
        self.assertFalse(lease.held)
        self.assertTrue(owner.closed)
        self.assertEqual(messages(replies)[1]["status"], "restored")
        self.assertNotIn(b"PRIVATE", replies.getvalue())

    def test_new_host_lease_prevents_all_recovery_with_one_attempt(self):
        owner, lease, replies = FakeOwner(), FakeLease(unavailable=True), io.BytesIO()
        recover = MagicMock()
        self.assertEqual(_run_guard(snapshot(), io.BytesIO(), replies, owner_factory=lambda pid: owner,
                                    lease_factory=lambda path: lease, recover=recover), 0)
        self.assertTrue(owner.exited)
        self.assertEqual(lease.acquire_count, 1)
        self.assertEqual(lease.release_count, 0)
        self.assertTrue(owner.closed)
        recover.assert_not_called()
        self.assertEqual(messages(replies)[1]["status"], "lease_unavailable")

    def test_invalid_disarm_does_not_disable_crash_recovery(self):
        for command in (b'not-json\n', b'{"type":"disarm","version":true}\n',
                        b'{"type":"disarm","version":1,"extra":1}\n'):
            with self.subTest(command=command):
                owner, lease = FakeOwner(), FakeLease()
                recover = MagicMock(return_value={"output_restored": True})
                _run_guard(snapshot(), io.BytesIO(command), io.BytesIO(), owner_factory=lambda pid: owner,
                           lease_factory=lambda path: lease, recover=recover)
                self.assertTrue(owner.exited)
                recover.assert_called_once()
                self.assertEqual(lease.release_count, 1)

    def test_failed_recovery_reports_scalar_failure_and_releases_lease(self):
        owner, lease, replies = FakeOwner(), FakeLease(), io.BytesIO()
        outcome = {"output_restored": False, "input_restored": True, "microphone_restored": False}
        self.assertEqual(_run_guard(snapshot(), io.BytesIO(), replies, owner_factory=lambda pid: owner,
                                    lease_factory=lambda path: lease, recover=lambda value: outcome), 1)
        self.assertEqual(messages(replies)[1], {"type": "finished", "version": 1,
                                              "status": "recovery_failed", **outcome})
        self.assertEqual(lease.release_count, 1)
        self.assertTrue(owner.closed)

    def test_receipt_persistence_runs_under_lease_after_all_recovery_actions(self):
        owner, lease, replies = FakeOwner(), FakeLease(), io.BytesIO()
        outcome = {"output_restored": True, "input_restored": True, "microphone_restored": True}
        order = []

        def recover(value):
            order.append("recover")
            self.assertTrue(lease.held)
            return outcome

        def persist(value, receipt):
            order.append("persist")
            self.assertTrue(lease.held)
            self.assertEqual(receipt, {"status": "restored", **outcome})
            return True

        _run_guard(snapshot(), io.BytesIO(), replies, owner_factory=lambda pid: owner,
                   lease_factory=lambda path: lease, recover=recover, persist_receipt=persist)
        self.assertEqual(order, ["recover", "persist"])
        self.assertFalse(lease.held)
        self.assertTrue(messages(replies)[1]["receipt_persisted"])


class SafetyGuardRecoveryTests(unittest.TestCase):
    def test_persisted_receipt_contains_only_scalar_outcome_and_atomic_replace(self):
        directory = MagicMock(spec=Path)
        value = replace(snapshot(), data_dir=directory)
        outcome = {"status": "recovery_failed", "output_restored": False,
                   "input_restored": True, "microphone_restored": False,
                   "credential": "PRIVATE-NEVER-SAVE", "endpoint": "PRIVATE-NEVER-SAVE"}
        self.assertTrue(_persist_receipt(value, outcome))
        target = directory.__truediv__.return_value
        temporary = target.with_suffix.return_value
        receipt = json.loads(temporary.write_text.call_args.args[0])
        self.assertEqual(set(receipt), {"version", "owner_pid", "completed_at", "status",
                                        "output_restored", "input_restored", "microphone_restored"})
        self.assertEqual(receipt["status"], "recovery_failed")
        self.assertFalse(receipt["output_restored"])
        self.assertTrue(receipt["input_restored"])
        self.assertNotIn("PRIVATE", temporary.write_text.call_args.args[0])
        temporary.replace.assert_called_once_with(target)

    def test_receipt_write_failure_does_not_raise_after_recovery(self):
        directory = MagicMock(spec=Path)
        directory.mkdir.side_effect = PermissionError("controlled receipt failure")
        self.assertFalse(_persist_receipt(replace(snapshot(), data_dir=directory), {
            "status": "restored", "output_restored": True,
            "input_restored": True, "microphone_restored": True,
        }))

    def test_every_recovery_runs_independently_after_failures(self):
        with patch("remontevoice.output_routing.OutputRoute") as output, patch(
            "remontevoice.input_routing.InputRoute"
        ) as input_route, patch("remontevoice.microphone_guard.MicrophoneMuteGuard.recover") as microphone:
            output.return_value.restore.side_effect = RuntimeError("PRIVATE-output-error")
            microphone.side_effect = RuntimeError("PRIVATE-mic-error")
            outcome = _recover(snapshot())
            self.assertEqual(outcome, {"output_restored": False, "input_restored": True,
                                       "microphone_restored": False})
            output.return_value.restore.assert_called_once()
            input_route.return_value.restore.assert_called_once()
            microphone.assert_called_once_with(snapshot().data_dir / "microphone-restore.json")

    def test_real_route_policy_preserves_manual_device_changes(self):
        value = snapshot()
        outputs = {0: "manual-output", 1: value.output_remote, 2: value.output_remote}
        inputs = {0: value.input_remote, 1: "manual-input", 2: value.input_remote}
        output = MemoryOutputRoute(value.output_remote, value.output_fallback, get_default=outputs.__getitem__,
                             set_default=lambda endpoint, role: outputs.__setitem__(role, endpoint))
        input_route = MemoryInputRoute(value.input_remote, value.input_fallback, get_default=inputs.__getitem__,
                                 set_default=lambda endpoint, role: inputs.__setitem__(role, endpoint))
        with patch("remontevoice.output_routing.OutputRoute", return_value=output), patch(
            "remontevoice.input_routing.InputRoute", return_value=input_route
        ), patch("remontevoice.microphone_guard.MicrophoneMuteGuard.recover"):
            self.assertTrue(all(_recover(value).values()))
        self.assertEqual(outputs, {0: "manual-output", 1: value.output_fallback, 2: value.output_fallback})
        self.assertEqual(inputs, {0: value.input_fallback, 1: "manual-input", 2: value.input_fallback})


class FakeProcess:
    def __init__(self, response=b'{"type":"ready","version":1}\n'):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(response)
        self.returncode = None
        self.killed = False
        self.waits = []
        self.on_kill = lambda: None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.on_kill()

    def wait(self, timeout):
        self.waits.append(timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class SafetyGuardParentTests(unittest.TestCase):
    def start(self):
        value = snapshot()
        return SafetyGuard.start(value.data_dir, value.output_remote, value.output_fallback,
                                 value.input_remote, value.input_fallback)

    def test_source_launch_replaces_pythonpath_and_waits_for_ready(self):
        process = FakeProcess()
        with patch("remontevoice.safety_guard.subprocess.Popen", return_value=process) as popen, patch.dict(
            os.environ, {"PYTHONPATH": "untrusted-inherited-path"}
        ):
            guard = self.start()
        self.assertTrue(guard.armed)
        args, kwargs = popen.call_args
        executable = Path(getattr(sys, "_base_executable", sys.executable))
        if os.name == "nt":
            executable = executable.with_name("python.exe")
        self.assertEqual(args[0], [str(executable), "-m", "remontevoice.safety_guard"])
        self.assertEqual(kwargs["env"]["PYTHONPATH"], str(Path(__file__).resolve().parents[1] / "host"))
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sent = json.loads(process.stdin.getvalue())
        self.assertEqual(sent["owner_pid"], os.getpid())
        self.assertEqual(set(sent), set(snapshot().message()))
        guard.disarm()
        guard.disarm()
        self.assertFalse(guard.armed)
        self.assertFalse(process.killed)
        self.assertEqual(len(process.waits), 1)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_frozen_launch_uses_main_dispatch_without_pythonpath_override(self):
        process = FakeProcess()
        with patch("remontevoice.safety_guard.subprocess.Popen", return_value=process) as popen, patch.object(
            sys, "frozen", True, create=True
        ):
            guard = self.start()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "--safety-guard"])
        self.assertEqual(popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        guard.disarm()

    def test_missing_or_malformed_ready_is_fail_closed_and_reaped(self):
        for response in (b"", b'{"type":"wrong","version":1}\n', b"[]\n"):
            with self.subTest(response=response):
                process = FakeProcess(response)
                with patch("remontevoice.safety_guard.subprocess.Popen", return_value=process):
                    with self.assertRaises(SafetyGuardError):
                        self.start()
                self.assertTrue(process.killed)
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)

    def test_startup_timeout_covers_stalled_initial_write_and_reaps_reader(self):
        process, release = FakeProcess(), threading.Event()
        process.on_kill = release.set

        class BlockedInput(io.BytesIO):
            def write(self, value):
                if not release.wait(1):
                    raise AssertionError("Controlled startup write did not unblock")
                raise BrokenPipeError("controlled child termination")

        process.stdin = BlockedInput()
        with patch("remontevoice.safety_guard.subprocess.Popen", return_value=process), patch(
            "remontevoice.safety_guard._READY_TIMEOUT_SECONDS", 0.02
        ):
            with self.assertRaisesRegex(SafetyGuardError, "ready in time"):
                self.start()
        self.assertTrue(process.killed)
        self.assertFalse(any(thread.name == "remontevoice-guard-handshake" and thread.is_alive()
                             for thread in threading.enumerate()))

    def test_unexpected_helper_exit_is_observable(self):
        process = FakeProcess()
        with patch("remontevoice.safety_guard.subprocess.Popen", return_value=process):
            guard = self.start()
        process.returncode = 2
        self.assertFalse(guard.armed)
        guard.disarm()


@unittest.skipUnless(os.name == "nt", "Windows process handles")
class WindowsOwnerHandleTests(unittest.TestCase):
    def pythonw(self):
        executable = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
        if not executable.is_file():
            self.skipTest("Adjacent base pythonw.exe is unavailable")
        return str(executable)

    def test_pythonw_owner_can_start_and_disarm_direct_console_helper(self):
        value = snapshot()
        code = (
            "from pathlib import Path; from remontevoice.safety_guard import SafetyGuard; "
            "import sys; guard=SafetyGuard.start(Path(sys.argv[1]), 'ro', 'fo', 'ri', 'fi'); "
            "assert guard.armed; guard.disarm(); assert not guard.armed"
        )
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "host"))
        process = subprocess.Popen([self.pythonw(), "-c", code, str(value.data_dir)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertFalse(value.data_dir.exists())

    def test_pythonw_helper_main_recovers_inherited_pipes_when_streams_are_none(self):
        value = replace(snapshot(), owner_pid=os.getpid())
        code = ("import sys; from remontevoice.safety_guard import main; "
                "sys.stdin=None; sys.stdout=None; raise SystemExit(main())")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "host"))
        process = subprocess.Popen([self.pythonw(), "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        payload = (json.dumps(value.message()) + '\n{"type":"disarm","version":1}\n').encode("ascii")
        try:
            stdout, stderr = process.communicate(input=payload, timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(json.loads(stdout), {"type": "ready", "version": 1})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertFalse(value.data_dir.exists())

    def test_real_source_helper_acknowledges_and_disarms_without_recovery(self):
        value = snapshot()
        guard = SafetyGuard.start(value.data_dir, value.output_remote, value.output_fallback,
                                 value.input_remote, value.input_fallback)
        try:
            self.assertTrue(guard.armed)
        finally:
            guard.disarm()
        self.assertFalse(guard.armed)
        self.assertIsNotNone(guard._process.poll())
        # Disarming while this owner lives must not create even the lease file.
        self.assertFalse(value.data_dir.exists())

    def test_handle_confirms_dummy_process_exit_without_audio_or_routes(self):
        # This child only waits for stdin EOF. No Host, audio API, endpoint or
        # actual recovery helper is launched by this process-handle check.
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        owner = None
        try:
            owner = _WindowsOwner(child.pid)
            self.assertTrue(owner.alive())
            child.stdin.close()
            child.wait(timeout=5)
            owner.wait_for_exit()
            self.assertFalse(owner.alive())
        finally:
            if owner is not None:
                owner.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if child.stdin is not None:
                child.stdin.close()


if __name__ == "__main__":
    unittest.main()
