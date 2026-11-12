"""Dedicated broker bootstrap for an installed protected onedir executable."""
from __future__ import annotations

import os
import signal
import sys
import threading

from .broker_composition import InstalledBrokerApplication
from .broker_process import run_broker_process
from .windows_broker_manifest import PinnedInstalledManifest

_LAB_STOP_SECONDS = 60.0
_LAB_EXIT_SECONDS = 90.0


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
    # The fixed installation beside the actual executable is the only policy
    # source. There is no CLI route for SID, image pins, driver paths or scripts.
    laboratory = sys.argv[1:] == ['--lab-muted-only']
    if len(sys.argv) != 1 and not laboratory:
        return 64
    stop = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stop.set())
    return _run_muted_lab(stop) if laboratory else run_broker_process(BrokerBootstrapHost, stop)
