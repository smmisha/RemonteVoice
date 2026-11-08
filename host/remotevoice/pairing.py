"""One-time local pairing PIN and persistent per-phone credential."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import TypeGuard


_MAX_CREDENTIAL_LENGTH = 128


def _valid_credential(credential: object) -> TypeGuard[str]:
    """Bound untrusted input before the ASCII-only constant-time comparison."""
    return (
        isinstance(credential, str)
        and 0 < len(credential) <= _MAX_CREDENTIAL_LENGTH
        and credential.isascii()
        and all(character.isalnum() or character in "-_" for character in credential)
    )


class PairingManager:
    def __init__(self, token_path: Path) -> None:
        self._token_path = token_path
        self._lock = threading.Lock()
        self._paired_token = self._read_token()
        self._pairing_pin: str | None = None
        self._pin_available = False
        if self._paired_token is None:
            self.begin_pairing()

    @property
    def pairing_pin(self) -> str | None:
        with self._lock:
            return self._pairing_pin if self._pin_available else None

    @property
    def is_paired(self) -> bool:
        with self._lock:
            return self._paired_token is not None

    def begin_pairing(self) -> str:
        pin = f"{secrets.randbelow(100_000_000):08d}"
        with self._lock:
            self._pairing_pin = pin
            self._pin_available = True
        return pin

    def is_valid_credential(self, credential: object) -> bool:
        """Check admission without consuming a PIN or replacing its token."""
        if not _valid_credential(credential):
            return False
        with self._lock:
            return bool(
                (self._paired_token and hmac.compare_digest(credential, self._paired_token))
                or (
                    self._pin_available
                    and self._pairing_pin is not None
                    and hmac.compare_digest(credential, self._pairing_pin)
                )
            )

    def authenticate(self, credential: object) -> str | None:
        if not _valid_credential(credential):
            return None

        with self._lock:
            if self._paired_token and hmac.compare_digest(credential, self._paired_token):
                return self._paired_token

            if not self._pin_available or self._pairing_pin is None:
                return None
            if not hmac.compare_digest(credential, self._pairing_pin):
                return None

            token = secrets.token_urlsafe(32)
            self._write_token(token)
            self._paired_token = token
            self._pairing_pin = None
            self._pin_available = False
            return token

    def _read_token(self) -> str | None:
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = data.get("token") if isinstance(data, dict) else None
        return token if _valid_credential(token) and len(token) >= 32 else None

    def _write_token(self, token: str) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix="pairing-", suffix=".tmp", dir=self._token_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump({"token": token}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._token_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class PairingAttemptLimiter:
    """Apply a short per-address backoff after repeated local pairing failures."""

    def __init__(self, max_failures: int = 5, window_seconds: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.get(address)
            if attempts is None:
                return True
            self._discard_expired(attempts, now)
            if not attempts:
                self._attempts.pop(address, None)
                return True
            return len(attempts) < self.max_failures

    def failed(self, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.setdefault(address, deque())
            self._discard_expired(attempts, now)
            attempts.append(now)
            if len(self._attempts) > 256:
                oldest_address = min(
                    self._attempts,
                    key=lambda key: self._attempts[key][0] if self._attempts[key] else now,
                )
                self._attempts.pop(oldest_address, None)

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)

    def _discard_expired(self, attempts: deque[float], now: float) -> None:
        while attempts and now - attempts[0] >= self.window_seconds:
            attempts.popleft()
