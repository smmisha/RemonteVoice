"""Read a fixed installed manifest through protected retained Windows handles.

This owner must outlive the composed broker/client. It performs no installation,
elevation or process launch. This is not attestation of libraries already loaded
by a bootloader: protected packaging/loader acceptance remains a separate gate.
"""
from __future__ import annotations

import ctypes as ct
import ntpath
import os

from .broker_package_manifest import MANIFEST_NAME, MAX_MANIFEST_BYTES, parse_manifest
from .windows_broker_bundle import WindowsProtectedBundle
from .windows_broker_identity import (
    _WindowsApi, _FileInfo, _canonical_path, _pin_protected_components, _protected,
)


class InstalledManifestError(RuntimeError):
    pass


class _ManifestApi(_WindowsApi):
    def read_manifest(self, handle):
        chunks, total = [], 0
        while total <= MAX_MANIFEST_BYTES:
            size = min(65536, MAX_MANIFEST_BYTES + 1 - total)
            buffer, count = ct.create_string_buffer(size), ct.c_uint32()
            self._check(self.kernel.ReadFile(handle, buffer, size, ct.byref(count), None))
            if count.value > size:
                raise InstalledManifestError('Invalid installation manifest read')
            if not count.value:
                return b''.join(chunks)
            chunks.append(buffer.raw[:count.value]); total += count.value
        raise InstalledManifestError('Installation manifest exceeds size bound')

    def children(self, directory):
        with os.scandir(directory) as entries:
            result = []
            for entry in entries:
                if len(result) >= 8192:
                    raise InstalledManifestError('Installation directory exceeds bound')
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                    raise InstalledManifestError('Installation reparse entry rejected')
                if not entry.is_dir(follow_symlinks=False) and not entry.is_file(follow_symlinks=False):
                    raise InstalledManifestError('Unexpected installation entry')
                result.append((entry.name, entry.is_dir(follow_symlinks=False)))
            return result


class PinnedInstalledManifest:
    """Single-use native owner. load() is off-UI; installation is cached only."""

    def __init__(self, role: str, *, api=None):
        if type(role) is not str or role not in ('broker', 'client'):
            raise ValueError('Explicit executable role required')
        self._role, self._api = role, api
        self._handles = []
        self._bundle = None
        self._used = self._ready = False
        self.installation = None

    @property
    def is_verified(self):
        return self._ready and self._bundle is not None and self._bundle.is_verified

    @property
    def cleanup_confirmed(self):
        return not self._handles and (self._bundle is None or self._bundle.cleanup_confirmed is True)

    def _tree(self, root, expected):
        allowed_directories = {root}
        for path in expected:
            directory = ntpath.dirname(path)
            while directory != root:
                if not directory.startswith(root + '\\'):
                    raise InstalledManifestError('Installation directory escaped root')
                allowed_directories.add(directory)
                directory = ntpath.dirname(directory)
        pending, files, directories, entries = [root], set(), {root}, 0
        while pending:
            directory = pending.pop()
            handle = self._api.open_image_component(directory, directory=True)
            self._handles.append(handle)
            info = self._api.file_info(handle)
            # Within the install root, ADD_SUBDIRECTORY is mutation too. A new
            # importable package must not be creatable after the inventory read.
            if (type(info) is not _FileInfo or not info.directory or info.reparse
                    or _canonical_path(info.path) != directory
                    or not _protected(self._api.file_security(handle), ancestor=False)):
                raise InstalledManifestError('Installation directory rejected')
            for name, is_directory in self._api.children(directory):
                entries += 1
                if entries > 8192 or type(name) is not str or ntpath.basename(name) != name:
                    raise InstalledManifestError('Invalid installation tree')
                path = _canonical_path(ntpath.join(directory, name))
                if not path.startswith(root + '\\'):
                    raise InstalledManifestError('Installation path escaped root')
                if is_directory is True:
                    # Reject before queueing: on case-sensitive NTFS two physical
                    # names may canonicalize to the same lower-case path. Scanning
                    # that alias twice could otherwise leave one tree unchecked.
                    if path not in allowed_directories or path in directories:
                        raise InstalledManifestError('Unexpected or duplicate installation directory')
                    directories.add(path)
                    pending.append(path)
                elif is_directory is False:
                    if path in files or path not in expected:
                        raise InstalledManifestError('Unexpected installation file')
                    files.add(path)
                else:
                    raise InstalledManifestError('Invalid installation entry type')
        if files != expected or directories != allowed_directories:
            raise InstalledManifestError('Installation file set mismatch')

    def load(self):
        if self._used:
            raise InstalledManifestError('Installation manifest owner is single-use')
        self._used = True
        try:
            if self._api is None:
                self._api = _ManifestApi()
            process = self._api.open_process(os.getpid()); self._handles.append(process)
            image = _canonical_path(self._api.process_image_path(process))
            expected_name = 'remotevoicebroker.exe' if self._role == 'broker' else 'remotevoice.exe'
            if ntpath.basename(image) != expected_name or self._api.process_alive(process) is not True:
                raise InstalledManifestError('Dedicated running executable required')
            root = ntpath.dirname(image)
            manifest_path = _canonical_path(ntpath.join(root, MANIFEST_NAME))
            handle = _pin_protected_components(self._api, manifest_path, self._handles)
            manifest = parse_manifest(self._api.read_manifest(handle))
            pins = {ntpath.join(root, *name.split('/')): digest for name, digest in manifest.files.items()}
            from .broker_composition import BrokerInstallation
            self.installation = BrokerInstallation(ntpath.join(root, manifest.broker),
                ntpath.join(root, manifest.client), pins, manifest.driver_instance_id)
            self._tree(root, set(p.casefold() for p in pins) | {manifest_path})
            self._bundle = WindowsProtectedBundle(pins, api=self._api)
            self._bundle.verify()
            self._bundle.verify_current_executable(image)
            self._ready = True
            return self.installation
        except Exception:
            self._ready = False
            try:
                self.close()
            except Exception:
                error = InstalledManifestError('Installation bootstrap cleanup unconfirmed')
                error.cleanup_owner = self
                raise error from None
            raise InstalledManifestError('Protected installation bootstrap rejected') from None

    def close(self):
        self._ready = False
        errors, failed = [], []
        if self._bundle is not None:
            try: self._bundle.close()
            except BaseException as error: errors.append(error)
        for handle in reversed(self._handles):
            try: self._api.close_handle(handle)
            except BaseException as error:
                failed.append(handle); errors.append(error)
        self._handles = failed
        if errors:
            fatal = next((e for e in errors if not isinstance(e, Exception)), None)
            raise fatal if fatal is not None else InstalledManifestError('Installation bootstrap cleanup unconfirmed')
