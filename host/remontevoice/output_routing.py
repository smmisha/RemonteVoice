"""Return managed Windows playback roles to a physical output on session loss."""

from __future__ import annotations

from collections.abc import Callable

from .windows_output import get_default_output, set_default_output


class OutputRoutingError(RuntimeError):
    """At least one requested Windows output change could not be confirmed."""


class OutputRoute:
    ROLES = (0, 1, 2)  # Console, multimedia, communications.

    def __init__(
        self,
        remote_endpoint: str,
        fallback_endpoint: str,
        *,
        get_default: Callable[[int], str] | None = None,
        set_default: Callable[[str, int], None] | None = None,
    ) -> None:
        if not remote_endpoint or not fallback_endpoint or remote_endpoint == fallback_endpoint:
            raise ValueError("Choose distinct remote and physical playback endpoints")
        self.remote_endpoint = remote_endpoint
        self.fallback_endpoint = fallback_endpoint
        self._get = get_default or get_default_output
        self._set = set_default or set_default_output

    def _set_confirmed(self, endpoint: str, role: int) -> None:
        self._set(endpoint, role)
        if self._get(role) != endpoint:
            raise OutputRoutingError("Windows не подтвердил переключение устройства вывода.")

    def activate(self) -> None:
        """Select the phone route; roll back a partially applied change on error."""
        before: dict[int, str] = {}
        attempted: list[int] = []
        try:
            # Read the complete snapshot before making any change.
            before = {role: self._get(role) for role in self.ROLES}
            for role in self.ROLES:
                if before[role] != self.remote_endpoint:
                    attempted.append(role)
                    self._set_confirmed(self.remote_endpoint, role)
        except Exception as exc:
            rollback_failed = False
            for role in reversed(attempted):
                try:
                    # Preserve an intervening manual device selection.
                    if self._get(role) == self.remote_endpoint:
                        self._set_confirmed(before[role], role)
                except Exception:
                    rollback_failed = True
            message = "Не удалось переключить звук на телефон."
            if rollback_failed:
                message += " Проверьте устройство вывода в Windows."
            raise OutputRoutingError(message) from exc

    def restore(self) -> int:
        """Restore every still-managed role, preserving other manual selections."""
        restored = 0
        failures: list[Exception] = []
        for role in self.ROLES:
            try:
                if self._get(role) == self.remote_endpoint:
                    self._set_confirmed(self.fallback_endpoint, role)
                    restored += 1
            except Exception as exc:
                # A failed communications role must not prevent normal playback recovery.
                failures.append(exc)
        if failures:
            raise OutputRoutingError(
                "Не удалось вернуть все каналы звука на ноутбук. "
                "Выберите динамики в настройках вывода Windows."
            ) from failures[0]
        return restored
