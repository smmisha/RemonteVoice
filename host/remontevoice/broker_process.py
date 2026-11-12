"""Dedicated broker process exit boundary; no service installation or CLI policy.

The executable's trusted composition supplies a host factory. This is deliberately
not an entry point accepting image pins, SIDs, writer factories or paths from IPC
or command-line input. Production packaging/account policy is a separate gate.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from enum import IntEnum


class BrokerProcessExit(IntEnum):
    CLEAN = 0
    FAILED = 1
    TERMINATION_REQUIRED = 70


class BrokerProcessTerminationRequired(BaseException):
    """The hard-exit adapter did not terminate; never resume privileged dispatch."""

    def __init__(self, owner) -> None:
        super().__init__('Dedicated broker process termination required')
        self.cleanup_owner = owner


def run_broker_process(host_factory: Callable, stop: threading.Event, *,
                       hard_exit: Callable[[int], object] = os._exit) -> int:
    """Run once in the dedicated process's main thread, never in the Host UI.

    A normal return is allowed only after exact cached cleanup confirmation.
    Fatal token reversion errors, failed construction and unknown cleanup must
    terminate the process, including any surviving worker/native handles. Do not
    inspect cleanup or invoke arbitrary callbacks on a compromised fatal thread.
    The owning executable must pass the returned status directly to process exit.
    """
    if not callable(host_factory) or not callable(hard_exit):
        raise TypeError('Trusted broker factory and process exit adapter required')
    owner = None
    failed = False
    fatal = False
    try:
        owner = host_factory()
        owner.serve(stop)
    except Exception:
        failed = True
    except BaseException:
        fatal = True

    clean = False
    if not fatal and owner is not None:
        try:
            clean = owner.cleanup_confirmed is True
        except BaseException:
            # A failing ownership observation is not proof that resources closed.
            pass
    if not fatal and clean:
        return int(BrokerProcessExit.FAILED if failed else BrokerProcessExit.CLEAN)

    # Outside the try above: an exit adapter failure must not reenter the boundary
    # or initiate a second cleanup/exit attempt under an unknown Windows token.
    try:
        hard_exit(int(BrokerProcessExit.TERMINATION_REQUIRED))
    except BaseException:
        raise BrokerProcessTerminationRequired(owner) from None
    raise BrokerProcessTerminationRequired(owner) from None
