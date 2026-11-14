"""Dedicated broker bootstrap for an installed protected onedir executable.

The packaged broker is a windowed (GUI-subsystem) executable: Windows gives it
no console and PyInstaller leaves sys.stderr as None. Its only stdio-free record
of a refused protected installation is therefore the per-user broker log
installed below; every write is best-effort and must never change broker
behaviour or mask the original failure.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .broker_composition import InstalledBrokerApplication
from .broker_process import run_broker_process
from .windows_broker_manifest import PinnedInstalledManifest

_LAB_STOP_SECONDS = 60.0
_LAB_EXIT_SECONDS = 90.0
_LOG_NAME = "broker.log"
_LOG_LIMIT_BYTES = 256 * 1024

_DIAGNOSTICS: _Diagnostics | None = None


def _data_dir() -> Path:
    base = os.environ.get("APPDATA")
    return Path(base) / "RemonteVoice" if base else Path.home() / ".remontevoice"


class _Diagnostics:
    """Append-only local record for the packaged windowless broker."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self._lock = threading.Lock()

    def _write(self, text: str) -> None:
        try:
            with self._lock:
                self.stream.write(text)
                self.stream.flush()
        except (OSError, ValueError):
            return  # Diagnostics must never raise into broker startup or exit.

    def note(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._write(f"{stamp} {message}\n")

    def failure(self, error: BaseException) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            kind, detail = type(error).__name__, str(error)
        except BaseException:
            kind, detail = "BaseException", ""
        self._write(f"{stamp} failure {kind}: {detail}\n")
        try:
            with self._lock:
                traceback.print_exception(type(error), error, error.__traceback__, file=self.stream)
                self.stream.flush()
        except (OSError, ValueError):
            return


def _open_diagnostics(path: Path) -> _Diagnostics | None:
    """Open the single-generation log; return None when the profile is unusable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LOG_LIMIT_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        stream = path.open("a", encoding="utf-8", buffering=1)
    except (OSError, ValueError):
        return None
    diagnostics = _Diagnostics(stream)
    try:
        faulthandler.enable(stream)
    except (OSError, ValueError, RuntimeError):
        pass
    return diagnostics


def _install_diagnostics() -> _Diagnostics | None:
    # A source run keeps its console; tests must not write to the real profile.
    if not getattr(sys, "frozen", False):
        return None
    return _open_diagnostics(_data_dir() / _LOG_NAME)


def _record_failure(error: BaseException) -> None:
    if _DIAGNOSTICS is not None:
        _DIAGNOSTICS.failure(error)


def _install_hooks(diagnostics: _Diagnostics) -> None:
    def excepthook(kind, error, trace):
        diagnostics.failure(error)

    def threadhook(args):
        if args.exc_value is not None and args.exc_type is not SystemExit:
            diagnostics.failure(args.exc_value)

    sys.excepthook = excepthook
    threading.excepthook = threadhook


class BrokerBootstrapHost:
    """Retain the protected manifest owner until application cleanup confirms."""

    def __init__(self, *, lab_muted_only=False):
        if type(lab_muted_only) is not bool:
            raise TypeError('Explicit laboratory mode required')
        self._lab_muted_only = lab_muted_only
        self._manifest = self._application = None
        self._finished = self._fatal = False
        self._cleanup_error = None

    @property
    def cleanup_confirmed(self):
        return (self._finished and not self._fatal and self._cleanup_error is None
                and self._manifest is not None and self._manifest.cleanup_confirmed is True
                and (self._application is None or self._application.cleanup_confirmed is True))

    def serve(self, stop):
        if self._manifest is not None or self._finished:
            raise RuntimeError('Broker bootstrap is single-use')
        original = None
        try:
            self._manifest = PinnedInstalledManifest('broker')
            installation = self._manifest.load()
            if self._manifest.is_verified is not True:
                raise RuntimeError('Installed broker policy is not verified')
            self._application = InstalledBrokerApplication(installation, lab_muted_only=self._lab_muted_only)
            self._application.serve(stop)
        except BaseException as error:
            original = error
            self._fatal = not isinstance(error, Exception)
            _record_failure(error)
        finally:
            if not self._fatal and self._manifest is not None:
                try:
                    if self._application is None or self._application.cleanup_confirmed is True:
                        self._manifest.close()
                except BaseException as error:
                    self._cleanup_error = error
            self._finished = True
        if original is not None and not isinstance(original, Exception):
            raise original
        if self._cleanup_error is not None and not isinstance(self._cleanup_error, Exception):
            self._fatal = True
            raise self._cleanup_error
        if original is not None:
            raise original
        if self._cleanup_error is not None:
            raise self._cleanup_error


def _run_muted_lab(stop):
    # Fixed deadlines and a disabled writer; no caller-supplied path/policy/audio.
    # Hard exit also bounds a blocked bootstrap or native cleanup operation.
    graceful = threading.Timer(_LAB_STOP_SECONDS, stop.set)
    watchdog = threading.Timer(_LAB_EXIT_SECONDS, lambda: os._exit(70))
    graceful.daemon = watchdog.daemon = True
    try:
        watchdog.start()
        graceful.start()
        return run_broker_process(lambda: BrokerBootstrapHost(lab_muted_only=True), stop)
    finally:
        graceful.cancel()
        watchdog.cancel()


def main():
    global _DIAGNOSTICS
    # The fixed installation beside the actual executable is the only policy
    # source. There is no CLI route for SID, image pins, driver paths or scripts.
    laboratory = sys.argv[1:] == ['--lab-muted-only']
    if len(sys.argv) != 1 and not laboratory:
        return 64
    _DIAGNOSTICS = _install_diagnostics()
    if _DIAGNOSTICS is not None:
        _install_hooks(_DIAGNOSTICS)
        _DIAGNOSTICS.note(f"start pid={os.getpid()} laboratory={laboratory}")
    stop = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stop.set())
    status = _run_muted_lab(stop) if laboratory else run_broker_process(BrokerBootstrapHost, stop)
    if _DIAGNOSTICS is not None:
        _DIAGNOSTICS.note(f"exit status={status}")
    return status
