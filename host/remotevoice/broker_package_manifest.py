"""Pure strict package inventory; no installation, trust or loader attestation.

The generator reads a trusted, quiescent build directory. Runtime must separately
verify manifest provenance, protected ACLs, pinned handles and the loaded runtime.
Build scanning is limited to 8192 entries and 2 GiB of regular-file contents.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

MANIFEST_NAME = "remotevoice-installation.json"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_BROKER, _CLIENT = "RemoteVoiceBroker.exe", "RemoteVoice.exe"
_KEYS = frozenset(("schema_version", "broker", "client", "driver_instance_id", "files"))
_RESERVED = frozenset(("con", "prn", "aux", "nul", "conin$", "conout$", "clock$")) | frozenset(
    prefix + suffix for prefix in ("com", "lpt") for suffix in "123456789¹²³")


class PackageManifestError(ValueError):
    """Malformed or inconsistent inventory, without exposing paths or file data."""


@dataclass(frozen=True)
class InstallationManifest:
    broker: str
    client: str
    driver_instance_id: str
    files: Mapping[str, str]


def _path(value):
    if (type(value) is not str or not 1 <= len(value) <= 512
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   or char in '\\:<>"|?*' for char in value)):
        raise PackageManifestError("Invalid package relative path")
    parts = value.split("/")
    if any(not part or part in (".", "..") or part[-1] in " ."
           or part.split(".", 1)[0].rstrip(" .").casefold() in _RESERVED for part in parts):
        raise PackageManifestError("Invalid package relative path")
    return value


def _instance(value):
    if type(value) is not str or not re.fullmatch(r"ROOT\\MEDIA\\[A-Za-z0-9_-]{1,64}", value, re.ASCII):
        raise PackageManifestError("Invalid exact driver instance")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageManifestError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise PackageManifestError("Invalid JSON numeric constant")


def _inventory(files):
    if type(files) is not dict or not 1 <= len(files) <= 4096:
        raise PackageManifestError("Invalid package inventory size")
    canonical, result = {}, {}
    for path, digest in files.items():
        path = _path(path)
        folded = path.casefold()
        if (folded == MANIFEST_NAME or folded in canonical or type(digest) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", digest, re.ASCII)):
            raise PackageManifestError("Invalid or duplicate package file")
        canonical[folded], result[path] = path, digest
    for path in canonical:
        parts = path.split("/")
        if any("/".join(parts[:index]) in canonical for index in range(1, len(parts))):
            raise PackageManifestError("Package file and directory collide")
    required = {_BROKER, _CLIENT, "_internal/base_library.zip"}
    if not required <= result.keys() or not any(
            re.fullmatch(r"_internal/python3[^/]*\.dll", path, re.ASCII) for path in result):
        raise PackageManifestError("Required shared onedir artifacts are missing")
    return MappingProxyType(result)


def parse_manifest(data: bytes) -> InstallationManifest:
    if type(data) is not bytes or not 1 <= len(data) <= MAX_MANIFEST_BYTES:
        raise PackageManifestError("Invalid package manifest size")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if (type(document) is not dict or document.keys() != _KEYS
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or document["broker"] != _BROKER or type(document["broker"]) is not str
                or document["client"] != _CLIENT or type(document["client"]) is not str):
            raise PackageManifestError("Invalid package manifest schema")
        return InstallationManifest(_BROKER, _CLIENT, _instance(document["driver_instance_id"]),
                                    _inventory(document["files"]))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PackageManifestError("Package manifest rejected") from None


def _regular_metadata(metadata, *, directory):
    if (stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))):
        raise PackageManifestError("Unsupported package filesystem entry")


def build_manifest(root, driver_instance_id: str) -> bytes:
    """Return canonical JSON bytes; never write the manifest or execute a file."""
    instance = _instance(driver_instance_id)
    files, names, directories, stack, entries, total = {}, set(), set(), [(Path(root), "")], 0, 0
    try:
        _regular_metadata(stack[0][0].lstat(), directory=True)
        while stack:
            directory, prefix = stack.pop()
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > 8192:
                        raise PackageManifestError("Package scan entry limit exceeded")
                    relative = _path(prefix + child.name)
                    folded = relative.casefold()
                    if folded in names:
                        raise PackageManifestError("Package names collide")
                    names.add(folded)
                    # Windows DirEntry.stat may leave st_ino/st_dev zero; use a
                    # full path stat for the later open-handle identity check.
                    before = os.stat(child.path, follow_symlinks=False)
                    is_directory = stat.S_ISDIR(before.st_mode)
                    _regular_metadata(before, directory=is_directory)
                    if is_directory:
                        directories.add(folded)
                        stack.append((Path(child.path), relative + "/"))
                        continue
                    if folded == MANIFEST_NAME:
                        continue
                    if len(files) >= 4096 or before.st_size > 2 * 1024**3 - total:
                        raise PackageManifestError("Package scan content limit exceeded")
                    digest = hashlib.sha256()
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(child.path, flags)
                    with os.fdopen(descriptor, "rb") as source:
                        opened = os.fstat(source.fileno())
                        _regular_metadata(opened, directory=False)
                        if not os.path.samestat(before, opened):
                            raise PackageManifestError("Package changed during scanning")
                        while chunk := source.read(65536):
                            total += len(chunk)
                            if total > 2 * 1024**3:
                                raise PackageManifestError("Package scan content limit exceeded")
                            digest.update(chunk)
                        after = os.fstat(source.fileno())
                    current = os.stat(child.path, follow_symlinks=False)
                    _regular_metadata(current, directory=False)
                    if (not os.path.samestat(before, current) or not os.path.samestat(opened, after)
                            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_size != current.st_size or before.st_mtime_ns != current.st_mtime_ns):
                        raise PackageManifestError("Package changed during scanning")
                    files[relative] = digest.hexdigest()
        represented = set()
        for relative in files:
            parts = relative.casefold().split("/")
            represented.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if directories != represented:
            raise PackageManifestError("Unrepresented package directory")
        document = dict(schema_version=1, broker=_BROKER, client=_CLIENT,
                        driver_instance_id=instance, files=dict(sorted(files.items())))
        encoded = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        parse_manifest(encoded)
        return encoded
    except (OSError, ValueError, TypeError, RecursionError):
        raise PackageManifestError("Package build inventory rejected") from None
