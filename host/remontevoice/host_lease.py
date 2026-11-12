"""One Windows Host owns shared audio routes, pairing and recovery at a time."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


class HostLeaseError(RuntimeError):
    """Another Host owns the application, or ownership cannot be established."""


class HostLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.held:
            return
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open('a+b')
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception as exc:
            if handle is not None:
                handle.close()
            raise HostLeaseError(
                'RemonteVoice уже открыт или не удалось получить управление аудиоустройствами. '
                'Используйте существующее окно Host.'
            ) from exc
        self._handle = handle

    def release(self) -> None:
        # The kernel also releases the byte-range lock if the process exits.
        if self._handle is not None:
            self._handle.close()
            self._handle = None
