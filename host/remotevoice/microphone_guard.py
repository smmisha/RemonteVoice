"""Mute one selected laptop capture endpoint and recover the state we changed."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .windows_microphone import get_microphone_mute, set_microphone_mute


class MicrophoneGuardError(RuntimeError):
    """A local microphone state change or its recovery could not be confirmed."""


class _Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
            try:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except Exception:
                handle.close()
                raise
            self._handle = handle
        except Exception as exc:
            raise MicrophoneGuardError(
                "Не удалось открыть запись восстановления микрофона. "
                "Закройте другое окно RemoteVoice и попробуйте снова."
            ) from exc

    def release(self) -> None:
        # Closing the handle releases its byte-range lock, including after a crash.
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def read(self) -> tuple[str, bool] | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        data = json.loads(text)
        if (not isinstance(data, dict) or set(data) != {"version", "endpoint_id", "previous_mute"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["endpoint_id"], str) or not data["endpoint_id"].strip()
                or "\0" in data["endpoint_id"] or type(data["previous_mute"]) is not bool):
            raise MicrophoneGuardError("Запись восстановления микрофона повреждена.")
        return data["endpoint_id"], data["previous_mute"]

    def write(self, endpoint: str, previous: bool) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "endpoint_id": endpoint,
                                         "previous_mute": previous}) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _restore_state(endpoint: str, previous: bool, get_mute: Callable[[str], bool],
                   set_mute: Callable[[str, bool], None]) -> None:
    # We do not own a pre-existing mute, and do not override a later manual unmute.
    if previous is False:
        current = get_mute(endpoint)
        if type(current) is not bool:
            raise MicrophoneGuardError("Не удалось прочитать состояние микрофона ноутбука.")
        if current:
            set_mute(endpoint, False)
            if get_mute(endpoint) is not False:
                raise MicrophoneGuardError("Windows не подтвердил восстановление микрофона ноутбука.")


class MicrophoneMuteGuard:
    def __init__(self, endpoint_id: str, *, journal_path: Path,
                 get_mute: Callable[[str], bool] | None = None,
                 set_mute: Callable[[str, bool], None] | None = None) -> None:
        if not isinstance(endpoint_id, str) or not endpoint_id.strip() or "\0" in endpoint_id:
            raise ValueError("Choose a physical laptop microphone")
        self.endpoint_id = endpoint_id
        self._journal = _Journal(journal_path)
        self._get = get_mute or get_microphone_mute
        self._set = set_mute or set_microphone_mute
        self._previous: bool | None = None
        self._active = False

    def activate(self) -> None:
        if self._previous is not None:
            if self._active:
                return
            raise MicrophoneGuardError("Сначала восстановите микрофон предыдущего сеанса.")
        self._journal.acquire()
        try:
            if self._journal.read() is not None:
                raise MicrophoneGuardError("Сначала восстановите микрофон предыдущего сеанса.")
            previous = self._get(self.endpoint_id)
            if type(previous) is not bool:
                raise MicrophoneGuardError("Не удалось прочитать состояние микрофона ноутбука.")
            # Persist before mutation; recovery survives termination of the Host process.
            self._journal.write(self.endpoint_id, previous)
            self._previous = previous
            if not previous:
                self._set(self.endpoint_id, True)
                if self._get(self.endpoint_id) is not True:
                    raise MicrophoneGuardError("Windows не подтвердил выключение микрофона ноутбука.")
            self._active = True
        except Exception as exc:
            try:
                self.restore()
            except MicrophoneGuardError:
                raise MicrophoneGuardError(
                    "Не удалось выключить микрофон ноутбука и восстановить его состояние. "
                    "Проверьте микрофон в Windows."
                ) from exc
            raise MicrophoneGuardError("Не удалось выключить микрофон ноутбука.") from exc
        finally:
            if self._previous is None:
                self._journal.release()

    def restore(self) -> None:
        # Even a failed restoration can already have unmuted the physical endpoint.
        self._active = False
        if self._previous is None:
            return
        try:
            _restore_state(self.endpoint_id, self._previous, self._get, self._set)
            self._journal.clear()
        except Exception as exc:
            # Keep the original state and journal so a retry can still recover it.
            raise MicrophoneGuardError(
                "Не удалось восстановить микрофон ноутбука. Проверьте его состояние в Windows."
            ) from exc
        self._previous = None
        self._journal.release()

    @classmethod
    def recover(cls, journal_path: Path) -> None:
        journal = _Journal(journal_path)
        journal.acquire()
        try:
            saved = journal.read()
            if saved is not None:
                _restore_state(*saved, get_microphone_mute, set_microphone_mute)
                journal.clear()
        except Exception as exc:
            raise MicrophoneGuardError(
                "Не удалось восстановить микрофон предыдущего сеанса. "
                "Проверьте микрофон в Windows."
            ) from exc
        finally:
            journal.release()
