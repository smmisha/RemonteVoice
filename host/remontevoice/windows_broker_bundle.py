"""Pin explicitly trusted installed executable/runtime/configuration files.

This is an integrity/ACL building block, not a manifest trust source or loader
attestation. The protected composition must supply a complete file inventory from
its trusted build/install policy. Workspace scripts, arbitrary plugins and paths
from a client are never a substitute. No ACL changes or file writes occur here.
"""
from __future__ import annotations

import re
import os
from collections.abc import Mapping

from .windows_broker_identity import (
    PeerVerificationError, _WindowsApi, _canonical_path, _pin_protected_file,
)


class ProtectedBundleError(RuntimeError):
    pass


class WindowsProtectedBundle:
    """Worker-affine lifetime owner for protected immutable installation pins."""

    def __init__(self, trusted_files: Mapping[str, str], *, api=None) -> None:
        if not isinstance(trusted_files, Mapping) or not 1 <= len(trusted_files) <= 4096:
            raise ValueError('Explicit bounded installed-file pins required')
        files = {}
        for path, digest in trusted_files.items():
            path = _canonical_path(path)
            if (path.endswith('\\') or path in files or type(digest) is not str
                    or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('Invalid installed-file pin')
            files[path] = digest
        self._files, self._api = files, api
        self._handles: list[int] = []
        self._used = self._verified = False

    @property
    def is_verified(self) -> bool:
        return self._verified

    @property
    def cleanup_confirmed(self) -> bool:
        return not self._handles

    def verify(self) -> None:
        if self._used:
            raise ProtectedBundleError('Installation pin owner is single-use')
        self._used = True
        try:
            if self._api is None:
                self._api = _WindowsApi()
            for path, digest in self._files.items():
                _pin_protected_file(self._api, path, digest, self._handles)
            self._verified = True
        except Exception:
            self._verified = False
            # Retain failed cleanup on this explicit owner. Do not hide a live
            # file handle behind a constructor that never returned its owner.
            try:
                self.close()
            except Exception:
                error = ProtectedBundleError('Installation cleanup unconfirmed')
                error.cleanup_owner = self
                raise error from None
            raise ProtectedBundleError('Protected installation rejected') from None

    def close(self) -> None:
        self._verified = False
        failed = []
        for handle in reversed(self._handles):
            try:
                self._api.close_handle(handle)
            except Exception:
                failed.append(handle)
        self._handles = failed
        if failed:
            raise ProtectedBundleError('Installation cleanup unconfirmed') from None

    def verify_current_executable(self, expected_path: str) -> None:
        """Bind this owner to the running dedicated executable, not its caller's claim."""
        expected_path = _canonical_path(expected_path)
        if not self._verified or expected_path not in self._files:
            raise ProtectedBundleError('Running executable verification rejected')
        try:
            process = self._api.open_process(os.getpid())
            self._handles.append(process)
            if (_canonical_path(self._api.process_image_path(process)) != expected_path
                    or self._api.process_alive(process) is not True):
                raise ProtectedBundleError('Running executable verification rejected')
        except Exception:
            self._verified = False
            raise ProtectedBundleError('Running executable verification rejected') from None
