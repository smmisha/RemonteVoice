import hashlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from remotevoice.broker_package_manifest import (
    MANIFEST_NAME, MAX_MANIFEST_BYTES, InstallationManifest, PackageManifestError,
    parse_manifest, build_manifest, _regular_metadata,
)


PINS = {"RemoteVoiceBroker.exe": "a" * 64, "RemoteVoice.exe": "b" * 64,
        "_internal/base_library.zip": "c" * 64, "_internal/python314.dll": "d" * 64}
TEMP_ROOT = Path(__file__).resolve().parents[1]


def document(**changes):
    result = dict(schema_version=1, broker="RemoteVoiceBroker.exe", client="RemoteVoice.exe",
                  driver_instance_id=r"ROOT\MEDIA\0000", files=dict(PINS))
    result.update(changes)
    return result


def encoded(**changes):
    return json.dumps(document(**changes)).encode("utf-8")


class ManifestParserTests(unittest.TestCase):
    def test_valid_manifest_is_frozen_and_files_cannot_be_mutated(self):
        result = parse_manifest(encoded())
        self.assertIs(type(result), InstallationManifest)
        self.assertEqual(result.files, PINS)
        self.assertEqual(result.driver_instance_id, r"ROOT\MEDIA\0000")
        with self.assertRaises(FrozenInstanceError):
            result.client = "other.exe"
        with self.assertRaises(TypeError):
            result.files["extra.dll"] = "a" * 64

    def test_schema_types_unknown_keys_and_nonfinite_json_are_rejected(self):
        cases = [encoded(schema_version=True), encoded(schema_version=1.0), encoded(schema_version=2),
                 encoded(extra=1), encoded(broker="remotevoicebroker.exe"), encoded(client=None),
                 encoded(driver_instance_id=r"ROOT\MEDIA\0000\extra"),
                 encoded(driver_instance_id=r"root\media\0000"), encoded(files=[]),
                 b"[]", b"null", b"{}", b'{"schema_version":NaN}', b'{"schema_version":Infinity}',
                 b"\xff", b"\xef\xbb\xbf" + encoded(), b'{"x":1,}']
        for case in cases:
            with self.subTest(case=case[:60]), self.assertRaises(PackageManifestError):
                parse_manifest(case)
        for value in (None, "", bytearray(encoded()), b"", b" " * (MAX_MANIFEST_BYTES + 1)):
            with self.subTest(type=type(value)), self.assertRaises(PackageManifestError):
                parse_manifest(value)

    def test_duplicate_keys_are_rejected_at_top_level_and_in_files(self):
        raw = encoded()
        for case in (raw.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
                     raw.replace(b'"RemoteVoice.exe": "' + b"b" * 64 + b'"',
                                 b'"RemoteVoice.exe": "' + b"b" * 64 + b'", "RemoteVoice.exe": "' + b"b" * 64 + b'"')):
            with self.assertRaises(PackageManifestError):
                parse_manifest(case)

    def test_invalid_windows_paths_names_hashes_and_case_collisions_are_rejected(self):
        bad = ["/absolute.dll", "../x.dll", "a/../x.dll", "./x.dll", "a//b.dll", "a/",
               r"a\b.dll", r"C:\x.dll", "C:/x.dll", "//server/share", "a:b.dll", "a\0.dll",
               "a /b.dll", "a./b.dll", "bad?.dll", "bad|.dll", "bad<.dll", "bad*.dll",
               "a/\x01.dll", "a/\ud800.dll", "CON", "nul.txt", "aux.dll", "COM1.dll", "Lpt9.log",
               "COM¹.txt", "CONIN$", "COM1 .txt", "clock$", "a" * 513, MANIFEST_NAME,
               MANIFEST_NAME.upper(), "remotevoice.exe", "_INTERNAL/base_library.zip"]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(PackageManifestError):
                parse_manifest(encoded(files={**PINS, path: "e" * 64}))
        for digest in ("A" * 64, "a" * 63, "z" * 64, None, 1):
            pins = {**PINS, "extra.dll": digest}
            with self.subTest(digest=digest), self.assertRaises(PackageManifestError):
                parse_manifest(encoded(files=pins))

    def test_prefix_collisions_missing_artifacts_and_limits(self):
        for files in ({**PINS, "_INTERNAL": "a" * 64},
                      {**PINS, "x": "a" * 64, "X/y.dll": "b" * 64},
                      {key: value for key, value in PINS.items() if key != "RemoteVoice.exe"},
                      {key: value for key, value in PINS.items() if not key.endswith(".dll")},
                      {**PINS, **{f"x/{i}.dat": "a" * 64 for i in range(4093)}}):
            with self.subTest(count=len(files)), self.assertRaises(PackageManifestError):
                parse_manifest(encoded(files=files))


class ManifestBuildTests(unittest.TestCase):
    def package(self, root):
        for index, relative in enumerate(PINS):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes((index,)) * (index + 1))

    def test_generator_hashes_streaming_deterministically_and_never_writes_manifest(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            self.package(root)
            result = build_manifest(root, r"ROOT\MEDIA\0000")
            self.assertEqual(result, build_manifest(root, r"ROOT\MEDIA\0000"))
            self.assertFalse((root / MANIFEST_NAME).exists())
            manifest = parse_manifest(result)
            self.assertEqual(set(manifest.files), set(PINS))
            for relative, digest in manifest.files.items():
                self.assertEqual(digest, hashlib.sha256((root / relative).read_bytes()).hexdigest())
            (root / MANIFEST_NAME).write_bytes(b"old manifest excluded")
            self.assertEqual(result, build_manifest(root, r"ROOT\MEDIA\0000"))

    def test_unexpected_inventory_and_metadata_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            self.package(root)
            (root / "NUL.txt").write_bytes(b"x") if os.name != "nt" else None
            if os.name != "nt":
                with self.assertRaises(PackageManifestError):
                    build_manifest(root, r"ROOT\MEDIA\0000")
            with self.assertRaises(PackageManifestError):
                build_manifest(root, "unconfigured")
        for metadata, directory in ((SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0), False),
                                    (SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400), False),
                                    (SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0), False)):
            with self.assertRaises(PackageManifestError):
                _regular_metadata(metadata, directory=directory)

    def test_empty_or_unrepresented_directory_is_rejected_but_nested_runtime_files_pass(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root=Path(temporary);self.package(root)
            extra=root/'_internal'/'pkg'
            extra.mkdir()
            with self.assertRaises(PackageManifestError):
                build_manifest(root,r'ROOT\MEDIA\0000')
            (extra/'helper.py').write_bytes(b'protected build data')
            manifest=parse_manifest(build_manifest(root,r'ROOT\MEDIA\0000'))
            self.assertIn('_internal/pkg/helper.py',manifest.files)
            (root/'empty').mkdir()
            with self.assertRaises(PackageManifestError):
                build_manifest(root,r'ROOT\MEDIA\0000')

    def test_symlink_tree_root_and_file_are_rejected_without_following(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary) / "package"
            root.mkdir()
            self.package(root)
            alias = Path(temporary) / "alias"
            try:
                alias.symlink_to(root, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation unavailable; metadata rejection covered separately")
            with self.assertRaises(PackageManifestError):
                build_manifest(alias, r"ROOT\MEDIA\0000")
            (root / "other.dll").symlink_to(root / "RemoteVoice.exe")
            with self.assertRaises(PackageManifestError):
                build_manifest(root, r"ROOT\MEDIA\0000")

    def test_build_rejects_case_collision_and_unknown_filesystem_node(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            self.package(root)
            (root / "extra.dll").write_bytes(b"a")
            if os.name == "nt":
                # Windows normally prevents creating a second case-only entry;
                # feed the scanner a duplicate entry to exercise that branch.
                original = os.scandir
                class Entries:
                    def __enter__(self):
                        self.handle = original(root)
                        values = list(self.handle)
                        return iter(values + [values[0]])
                    def __exit__(self, *unused):
                        self.handle.close()
                with patch("remotevoice.broker_package_manifest.os.scandir", return_value=Entries()):
                    with self.assertRaises(PackageManifestError):
                        build_manifest(root, r"ROOT\MEDIA\0000")
            else:
                (root / "EXTRA.DLL").write_bytes(b"b")
                with self.assertRaises(PackageManifestError):
                    build_manifest(root, r"ROOT\MEDIA\0000")


if __name__ == "__main__":
    unittest.main()
