"""Single-use, sequential broker host for a dedicated process/worker only.

Composition supplies trusted listener, verifier, policy and inert writer factory.
This module creates no service, account policy, pipe, driver or microphone. A new
client is admitted only after the previous session confirms all cleanup. Fatal
BaseException failures must reach the dedicated process boundary; they never
become recoverable client rejections. Retain a failed host until process exit.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable

from .broker_pipe_session import BrokerPipeSession
from .mic_broker import BrokerPolicy


class BrokerHostError(RuntimeError):
    """Admission has stopped; the host must not be restarted or replaced."""


class BrokerHost:
    def __init__(self, listener, verifier, policy: BrokerPolicy, writer_factory: Callable, *,
                 session_factory: Callable = BrokerPipeSession, accept_timeout: float = 0.1) -> None:
        if (type(accept_timeout) not in (int, float) or not math.isfinite(accept_timeout)
                or not 0 < accept_timeout <= 1):
            raise ValueError("Invalid bounded accept deadline")
        if (type(policy) is not BrokerPolicy or not callable(writer_factory) or not callable(session_factory)
                or not callable(getattr(listener, "accept", None))
                or not callable(getattr(listener, "close", None))
                or not callable(getattr(verifier, "close", None))):
            raise TypeError("Trusted broker composition is required")
        self._listener, self._verifier = listener, verifier
        self._policy, self._writer_factory = policy, writer_factory
        self._session_factory, self._accept_timeout = session_factory, accept_timeout
        self._session = self._connection = None
        self._used = self._finished = self._listener_closed = self._verifier_closed = False
        self._fatal: BaseException | None = None
        self._cleanup_errors: tuple[BaseException, ...] = ()

    @property
    def cleanup_confirmed(self) -> bool:
        return (self._finished and self._listener_closed and self._verifier_closed
                and self._session is None and self._connection is None
                and self._fatal is None and not self._cleanup_errors)

    @property
    def cleanup_errors(self) -> tuple[BaseException, ...]:
        """Retained cleanup evidence for the process boundary; never send on wire."""
        return self._cleanup_errors

    def _close_unclaimed_connection(self) -> None:
        self._connection.close()
        if self._connection.cleanup_confirmed is not True:
            raise BrokerHostError("Accepted connection cleanup is unconfirmed")
        self._connection = None

    def _accept_sessions(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._connection = self._listener.accept(self._accept_timeout)
            except Exception as error:
                raise BrokerHostError("Broker listener failed; admission stopped") from error
            if self._connection is None:
                continue
            if stop.is_set():
                self._close_unclaimed_connection()
                break
            # Constructor failure is a host/composition fault, not a recoverable
            # handshake rejection. The accepted connection stays owned for cleanup.
            self._session = self._session_factory(self._connection, self._verifier,
                                                  self._policy, self._writer_factory)
            session_error = None
            try:
                self._session.serve(stop)
            except Exception as error:
                session_error = error
            # BaseException deliberately bypasses recovery and reaches serve's
            # resource-only finalizer and then the dedicated process boundary.
            if self._session.cleanup_confirmed is not True:
                raise BrokerHostError("Session cleanup is unconfirmed; admission stopped") from session_error
            self._session = self._connection = None

    def serve(self, stop: threading.Event) -> None:
        if self._used:
            raise BrokerHostError("Broker host is single-use")
        self._used = True
        primary: BaseException | None = None
        try:
            self._accept_sessions(stop)
        except BaseException as error:
            primary = error
            if not isinstance(error, Exception):
                self._fatal = error
        finally:
            errors: list[BaseException] = []
            # An accepted pipe whose constructor failed has no session owner.
            # On a fatal thread only resource cleanup is permitted, never driver
            # dispatch, new authentication or another session/admission attempt.
            if self._connection is not None and self._session is None:
                try:
                    self._close_unclaimed_connection()
                except BaseException as error:
                    errors.append(error)
            for resource, attribute in ((self._listener, "_listener_closed"),
                                         (self._verifier, "_verifier_closed")):
                try:
                    resource.close()
                    # NativePipeServer exposes explicit confirmation. The
                    # current verifier's successful close() confirms its pinned
                    # handles; honor explicit confirmation if an adapter adds it.
                    default_confirmation = attribute == "_verifier_closed"
                    if getattr(resource, "cleanup_confirmed", default_confirmation) is not True:
                        raise BrokerHostError("Broker resource cleanup is unconfirmed")
                    setattr(self, attribute, True)
                except BaseException as error:
                    errors.append(error)
            self._cleanup_errors = tuple(errors)
            self._finished = True
        fatal = self._fatal if self._fatal is not None else next(
            (error for error in self._cleanup_errors if not isinstance(error, Exception)), None)
        if fatal is not None:
            self._fatal = fatal
            raise fatal
        if primary is not None:
            raise primary
        if self._cleanup_errors:
            raise BrokerHostError("Broker host cleanup is unconfirmed") from self._cleanup_errors[0]
