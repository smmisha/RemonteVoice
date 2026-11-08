"""Recover managed laptop routes after the owning Windows Host process dies.

The helper never treats a broken pipe or an unresponsive UI as owner death.
It opens the owner's kernel process handle before acknowledging readiness and
requires both confirmed process exit and the exclusive Host lease to recover.
No credentials, audio frames or microphone recordings enter this protocol.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .host_lease import HostLease, HostLeaseError

_MAX_MESSAGE_BYTES = 16 * 1024
_READY_TIMEOUT_SECONDS = 5.0
_REAP_TIMEOUT_SECONDS = 2.0
_PROTOCOL_VERSION = 1


class SafetyGuardError(RuntimeError):
    """The independent recovery helper could not be armed or stopped safely."""


@dataclass(frozen=True)
class _Snapshot:
    data_dir: Path
    owner_pid: int
    output_remote: str
    output_fallback: str
    input_remote: str
    input_fallback: str

    @classmethod
    def parse(cls, message: Any) -> _Snapshot:
        expected = {"type", "version", "data_dir", "owner_pid", "output_remote",
                    "output_fallback", "input_remote", "input_fallback"}
        if not isinstance(message, dict) or set(message) != expected:
            raise SafetyGuardError("Invalid recovery snapshot fields")
        if message["type"] != "arm" or type(message["version"]) is not int or message["version"] != 1:
            raise SafetyGuardError("Invalid recovery snapshot version")
        owner_pid = message["owner_pid"]
        if type(owner_pid) is not int or not 0 < owner_pid <= 0xFFFFFFFF:
            raise SafetyGuardError("Invalid recovery owner")
        directory = message["data_dir"]
        if (not isinstance(directory, str) or not directory.strip() or "\0" in directory
                or len(directory) > 4096 or not Path(directory).is_absolute()):
            raise SafetyGuardError("Invalid recovery directory")
        endpoints = [message[name] for name in (
            "output_remote", "output_fallback", "input_remote", "input_fallback"
        )]
        if any(not isinstance(value, str) or not value.strip() or "\0" in value
               or len(value) > 1024 for value in endpoints):
            raise SafetyGuardError("Invalid recovery endpoint")
        if endpoints[0] == endpoints[1] or endpoints[2] == endpoints[3]:
            raise SafetyGuardError("Recovery routes must have distinct endpoints")
        return cls(Path(directory), owner_pid, *endpoints)

    def message(self) -> dict[str, Any]:
        return {"type": "arm", "version": _PROTOCOL_VERSION,
                "data_dir": str(self.data_dir), "owner_pid": self.owner_pid,
                "output_remote": self.output_remote, "output_fallback": self.output_fallback,
                "input_remote": self.input_remote, "input_fallback": self.input_fallback}


def _write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise SafetyGuardError("Recovery message is too large")
    stream.write(encoded)
    stream.flush()


def _read_message(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline(_MAX_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) > _MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
        raise SafetyGuardError("Invalid recovery message size")
    try:
        message = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyGuardError("Invalid recovery message") from exc
    if not isinstance(message, dict):
        raise SafetyGuardError("Invalid recovery message")
    return message


class _WindowsOwner:
    """A kernel handle pins process identity, including across PID reuse."""

    def __init__(self, owner_pid: int) -> None:
        if os.name != "nt":
            raise SafetyGuardError("The recovery helper requires Windows")
        from ctypes import wintypes

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.OpenProcess(0x00100000, False, owner_pid)  # SYNCHRONIZE only.
        if not self._handle:
            raise SafetyGuardError("Could not open the recovery owner process")

    def alive(self) -> bool:
        result = self._kernel.WaitForSingleObject(self._handle, 0)
        if result not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT.
            raise SafetyGuardError("Could not confirm recovery owner state")
        return result == 258

    def wait_for_exit(self) -> None:
        if self._kernel.WaitForSingleObject(self._handle, 0xFFFFFFFF) != 0:
            raise SafetyGuardError("Could not confirm recovery owner exit")

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _recover(snapshot: _Snapshot) -> dict[str, bool]:
    # Lazy imports keep module import, handshake and disarming free of native
    # endpoint access. Each action runs even when another one fails.
    from .input_routing import InputRoute
    from .microphone_guard import MicrophoneMuteGuard
    from .output_routing import OutputRoute

    actions = (
        ("output_restored", lambda: OutputRoute(snapshot.output_remote, snapshot.output_fallback).restore()),
        ("input_restored", lambda: InputRoute(snapshot.input_remote, snapshot.input_fallback).restore()),
        ("microphone_restored", lambda: MicrophoneMuteGuard.recover(snapshot.data_dir / "microphone-restore.json")),
    )
    result = {}
    for name, action in actions:
        try:
            action()
            result[name] = True
        except Exception:
            # Only scalar outcomes leave this process; no endpoint IDs or raw
            # exception messages can accidentally disclose local account data.
            result[name] = False
    return result


def _run_guard(
    snapshot: _Snapshot,
    commands: BinaryIO,
    replies: BinaryIO,
    *,
    owner_factory: Callable[[int], Any] = _WindowsOwner,
    lease_factory: Callable[[Path], Any] = HostLease,
    recover: Callable[[_Snapshot], dict[str, bool]] = _recover,
    persist_receipt: Callable[[_Snapshot, dict[str, Any]], bool] | None = None,
) -> int:
    owner = owner_factory(snapshot.owner_pid)
    try:
        if not owner.alive():
            # No acknowledgement means the parent may never change routes.
            raise SafetyGuardError("The recovery owner exited before readiness")
        _write_message(replies, {"type": "ready", "version": _PROTOCOL_VERSION})
        try:
            command = _read_message(commands)
        except (SafetyGuardError, OSError):
            command = None  # Invalid input never disarms a recovery helper.
        if (command == {"type": "disarm", "version": _PROTOCOL_VERSION}
                and type(command["version"]) is int):
            return 0
        # EOF alone grants no recovery permission. This kernel wait does not
        # poll the UI, network, PID table or Host lease while the owner is alive.
        owner.wait_for_exit()
        lease = lease_factory(snapshot.data_dir / "host.lock")
        try:
            lease.acquire()
        except HostLeaseError:
            _finish(replies, {"status": "lease_unavailable"})
            return 0
        try:
            result = recover(snapshot)
            status = "restored" if all(result.values()) else "recovery_failed"
            receipt = {"status": status, **result}
            if persist_receipt is not None:
                receipt["receipt_persisted"] = persist_receipt(snapshot, receipt)
            _finish(replies, receipt)
            return 0 if status == "restored" else 1
        finally:
            lease.release()
    finally:
        owner.close()


def _finish(replies: BinaryIO, result: dict[str, Any]) -> None:
    try:
        _write_message(replies, {"type": "finished", "version": _PROTOCOL_VERSION, **result})
    except (OSError, ValueError):
        pass  # The original Host normally exited, so its stdout reader is gone.


def _persist_receipt(snapshot: _Snapshot, result: dict[str, Any]) -> bool:
    """Write only scalar recovery results while this helper holds HostLease."""
    fields = ("output_restored", "input_restored", "microphone_restored")
    receipt = {"version": _PROTOCOL_VERSION, "owner_pid": snapshot.owner_pid,
               "completed_at": datetime.now(timezone.utc).isoformat(),
               "status": result["status"],
               **{name: result[name] is True for name in fields}}
    target = snapshot.data_dir / "safety-guard-recovery.json"
    temporary = target.with_suffix(".json.tmp")
    try:
        snapshot.data_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(receipt, ensure_ascii=True) + "\n", encoding="ascii")
        temporary.replace(target)
        return True
    except OSError:
        return False  # A receipt failure must never undo completed device recovery.


class SafetyGuard:
    """An acknowledged independent process, armed for one immutable route set."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._disarmed = False

    @property
    def armed(self) -> bool:
        return not self._disarmed and self._process.poll() is None

    @classmethod
    def start(
        cls, data_dir: Path, output_remote: str, output_fallback: str,
        input_remote: str, input_fallback: str,
    ) -> SafetyGuard:
        snapshot = _Snapshot.parse({
            "type": "arm", "version": _PROTOCOL_VERSION, "data_dir": str(data_dir.resolve()),
            "owner_pid": os.getpid(), "output_remote": output_remote,
            "output_fallback": output_fallback, "input_remote": input_remote,
            "input_fallback": input_fallback,
        })
        environment = os.environ.copy()
        source_root = Path(__file__).resolve().parents[1]
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--safety-guard"]
            # Recovery must outlive the Host. A onefile worker otherwise reuses
            # the Host's extraction directory, removed when that Host exits.
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            working_directory = None
        else:
            executable = Path(getattr(sys, "_base_executable", sys.executable))
            if os.name == "nt":
                # The venv executable can be a redirector that launches another
                # process. Terminating only that redirector would orphan the
                # armed runtime; use the direct console interpreter instead.
                executable = executable.with_name("python.exe")
            command = [str(executable), "-m", "remotevoice.safety_guard"]
            # Replace, rather than append untrusted inherited search entries.
            environment["PYTHONPATH"] = str(source_root)
            working_directory = str(source_root)
        process = None
        reader = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=working_directory, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdin is not None and process.stdout is not None
            complete = threading.Event()
            result: list[Any] = []

            def read_ready() -> None:
                try:
                    # Include the initial write in the deadline too: an early
                    # child startup failure must not block on a full stdin pipe.
                    _write_message(process.stdin, snapshot.message())
                    result.append(_read_message(process.stdout))
                except Exception:
                    result.append(None)
                finally:
                    complete.set()

            reader = threading.Thread(target=read_ready, name="remotevoice-guard-handshake", daemon=True)
            reader.start()
            if not complete.wait(_READY_TIMEOUT_SECONDS):
                raise SafetyGuardError("Recovery helper did not become ready in time")
            if result != [{"type": "ready", "version": _PROTOCOL_VERSION}] or process.poll() is not None:
                raise SafetyGuardError("Recovery helper did not confirm readiness")
            return cls(process)
        except Exception as exc:
            if process is not None:
                _terminate(process)
            if isinstance(exc, SafetyGuardError):
                raise
            raise SafetyGuardError("Could not start the independent recovery helper") from exc
        finally:
            if reader is not None:
                reader.join(timeout=_REAP_TIMEOUT_SECONDS)

    def disarm(self) -> None:
        """Call only after confirmed restoration and a clean audio shutdown."""
        if self._disarmed:
            return
        process = self._process
        error = None
        try:
            if process.poll() is None:
                assert process.stdin is not None
                _write_message(process.stdin, {"type": "disarm", "version": _PROTOCOL_VERSION})
                process.stdin.close()
            process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except Exception as exc:
            error = exc
            _terminate(process)
        finally:
            self._disarmed = True
            _close_pipes(process)
        if error is not None:
            raise SafetyGuardError("Recovery helper did not stop normally") from error


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        raise SafetyGuardError("Could not confirm recovery helper termination") from None
    finally:
        _close_pipes(process)


def _binary_standard_stream(name: str) -> tuple[BinaryIO, bool]:
    stream = getattr(sys, name, None)
    if stream is not None:
        return getattr(stream, "buffer", stream), False
    if os.name != "nt" or name not in ("stdin", "stdout"):
        raise SafetyGuardError("Recovery pipe is unavailable")

    # Windowed Python/PyInstaller may set sys.stdin/stdout to None even when
    # CreateProcess received redirected pipes. Duplicate only those inherited
    # PIPE handles; the fd below owns its duplicate, never the original handle.
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                       wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    standard = kernel.GetStdHandle(-10 if name == "stdin" else -11)
    if not standard or kernel.GetFileType(standard) != 3:  # FILE_TYPE_PIPE.
        raise SafetyGuardError("Recovery requires redirected binary pipes")
    current = kernel.GetCurrentProcess()
    duplicated = wintypes.HANDLE()
    if not kernel.DuplicateHandle(current, standard, current, ctypes.byref(duplicated),
                                  0, False, 2):  # DUPLICATE_SAME_ACCESS.
        raise SafetyGuardError("Could not duplicate the recovery pipe")
    descriptor = None
    try:
        flags = os.O_BINARY | (os.O_RDONLY if name == "stdin" else os.O_WRONLY)
        descriptor = msvcrt.open_osfhandle(duplicated.value, flags)
        return os.fdopen(descriptor, "rb" if name == "stdin" else "wb", buffering=0), True
    except Exception:
        if descriptor is None:
            kernel.CloseHandle(duplicated)
        else:
            os.close(descriptor)
        raise SafetyGuardError("Could not open the recovery pipe") from None


def main() -> int:
    owned_streams = []
    try:
        commands, owned = _binary_standard_stream("stdin")
        if owned:
            owned_streams.append(commands)
        replies, owned = _binary_standard_stream("stdout")
        if owned:
            owned_streams.append(replies)
        message = _read_message(commands)
        snapshot = _Snapshot.parse(message)
        # Windows venv launchers may insert an intermediate process, so getppid
        # is not a reliable owner identity. The private stdin snapshot names the
        # Host; its already-open kernel handle pins the actual process identity.
        if snapshot.owner_pid == os.getpid():
            raise SafetyGuardError("Recovery helper cannot own itself")
        return _run_guard(snapshot, commands, replies, persist_receipt=_persist_receipt)
    except Exception:
        # No native changes run after malformed startup or an unconfirmed owner.
        return 2
    finally:
        for stream in owned_streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
