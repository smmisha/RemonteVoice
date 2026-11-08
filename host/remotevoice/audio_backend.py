"""Audio component lifecycle boundary; Windows route ownership stays in HostWindow.

The legacy mixer adapter is temporary. Selecting ordinary virtual pairs preserves
their existing externally configured behavior; it does not certify a new driver.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .voicemeeter_routes import (
    VoicemeeterRoutingError, close_standard_mixer_session, configure_standard_routes, standard_mixer_running,
)


class AudioBackendError(RuntimeError):
    """The selected audio component could not be prepared."""


def close_audio_backend() -> None:
    """Final SDK release; session stop/reconnect must retain the shared login."""
    try:
        close_standard_mixer_session()
    except VoicemeeterRoutingError as exc:
        raise AudioBackendError(str(exc)) from exc


@dataclass(frozen=True)
class AudioBackend:
    uses_external_mixer: bool

    @property
    def unavailable_message(self) -> str:
        return "Аудиомикшер завершился — подключение остановлено"

    def is_available(self) -> bool:
        # Generic selected pairs never start or poll a mixer. Endpoint/PCM
        # liveness is still monitored by the existing capture/playback workers.
        if not self.uses_external_mixer:
            return True
        try:
            return standard_mixer_running()
        except Exception:
            return False  # A failed health observation must trigger recovery.


@dataclass(frozen=True)
class DriverBackendSnapshot:
    """Trusted local monitor output, never an unauthenticated IPC payload.

    The Windows runtime must authenticate the broker and bind both
    endpoint identities before publishing this snapshot. UI polling only reads
    the cache; it must not refresh its timestamp or call native driver I/O.
    """

    protocol_version: int
    broker_authenticated: bool
    endpoints_verified: bool
    capture_endpoint_id: str = field(repr=False)
    microphone_endpoint_id: str = field(repr=False)
    observed_at: float
    mic_requested: bool = False
    mic_writer_ready: bool = False
    cleanup_pending: bool = False


class DriverAudioBackend:
    """Readiness seam only; selecting/deploying the driver remains gated."""

    uses_external_mixer = False
    unavailable_message = "Аудиокомпонент недоступен — подключение остановлено"

    def __init__(self, observe: Callable[[], DriverBackendSnapshot], *,
                 clock: Callable[[], float] = time.monotonic, max_age: float = 0.75) -> None:
        if not math.isfinite(max_age) or not 0.05 <= max_age <= 1.0:
            raise ValueError("Invalid component observation deadline")
        self._observe, self._clock, self._max_age = observe, clock, max_age

    def is_available(self) -> bool:
        return self._ready_snapshot() is not None

    def matches_endpoints(self, capture_endpoint_id: str, microphone_endpoint_id: str) -> bool:
        """Exact previously verified Windows identities; no friendly-name pairing."""
        status = self._ready_snapshot()
        return bool(status is not None and type(capture_endpoint_id) is str
                    and type(microphone_endpoint_id) is str
                    and status.capture_endpoint_id.casefold() == capture_endpoint_id.casefold()
                    and status.microphone_endpoint_id.casefold() == microphone_endpoint_id.casefold())

    def _ready_snapshot(self) -> DriverBackendSnapshot | None:
        try:
            status = self._observe()
            if type(status) is not DriverBackendSnapshot:
                return None
            if type(status.protocol_version) is not int or status.protocol_version != 1:
                return None
            if status.broker_authenticated is not True or status.endpoints_verified is not True:
                return None
            if (type(status.mic_requested) is not bool or type(status.mic_writer_ready) is not bool
                    or status.cleanup_pending is not False):
                return None
            ids = (status.capture_endpoint_id, status.microphone_endpoint_id)
            if any(type(value) is not str or not value.strip() or len(value) > 1024 for value in ids):
                return None
            if ids[0].casefold() == ids[1].casefold():
                return None
            if type(status.observed_at) not in (float, int) or not math.isfinite(status.observed_at):
                return None
            age = self._clock() - status.observed_at
            if not math.isfinite(age) or not 0 <= age <= self._max_age:
                return None
            # An explicitly muted microphone is healthy without an active writer.
            return status if not status.mic_requested or status.mic_writer_ready else None
        except Exception:
            return None


def prepare_driver_audio_backend(observe: Callable[[], DriverBackendSnapshot], **options) -> DriverAudioBackend:
    """Require authenticated readiness before any route/mute change; no mixer."""
    backend = DriverAudioBackend(observe, **options)
    if not backend.is_available():
        raise AudioBackendError(backend.unavailable_message)
    return backend


def prepare_audio_backend(capture_name: str, playback_name: str) -> AudioBackend:
    """Prepare only the selected known route; do not alter Windows defaults."""
    try:
        external = configure_standard_routes(capture_name, playback_name)
    except VoicemeeterRoutingError as exc:
        raise AudioBackendError(str(exc)) from exc
    return AudioBackend(uses_external_mixer=external)
