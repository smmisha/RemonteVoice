"""Manage the Windows microphone route for the lifetime of a phone session."""

from __future__ import annotations

from collections.abc import Callable

from .output_routing import OutputRoute, OutputRoutingError
from .windows_microphone import get_default_microphone, set_default_microphone


class InputRoutingError(OutputRoutingError):
    """A requested Windows microphone route could not be confirmed."""


class InputRoute(OutputRoute):
    """Share the role snapshot, readback, rollback and manual-override policy."""

    def __init__(
        self,
        remote_endpoint: str,
        fallback_endpoint: str,
        *,
        get_default: Callable[[int], str] | None = None,
        set_default: Callable[[str, int], None] | None = None,
    ) -> None:
        super().__init__(
            remote_endpoint, fallback_endpoint,
            get_default=get_default or get_default_microphone,
            set_default=set_default or set_default_microphone,
        )

    def _set_confirmed(self, endpoint: str, role: int) -> None:
        self._set(endpoint, role)
        if self._get(role) != endpoint:
            raise InputRoutingError("Windows не подтвердил переключение микрофона.")

    def activate(self) -> None:
        try:
            super().activate()
        except OutputRoutingError as exc:
            raise InputRoutingError(
                "Не удалось переключить микрофон на телефон. "
                "Проверьте устройство ввода в настройках Windows."
            ) from exc

    def restore(self) -> int:
        try:
            return super().restore()
        except OutputRoutingError as exc:
            raise InputRoutingError(
                "Не удалось вернуть все каналы микрофона на ноутбук. "
                "Выберите микрофон в настройках ввода Windows."
            ) from exc
