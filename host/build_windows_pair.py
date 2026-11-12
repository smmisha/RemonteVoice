"""Build a new, unsigned shared-onedir pair; never install or launch its output.

Run with the existing project .venv interpreter. This utility never fetches tools,
overwrites an output directory, cleans old builds, signs files or changes trust.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

from remotevoice.broker_package_manifest import MANIFEST_NAME, build_manifest, parse_manifest

HOST = Path(__file__).resolve().parent
SPEC = HOST / "RemoteVoicePair.spec"


def _hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _sources():
    sources = [SPEC, Path(__file__).resolve(), HOST / "broker_launcher.py",
               HOST / "launcher.py", HOST / "requirements.txt", HOST / "RemoteVoice.ico"]
    sources += sorted((HOST / "remotevoice").rglob("*.py"))
    return {path.relative_to(HOST).as_posix(): _hash(path) for path in sources}


def build_pair(output, driver_instance_id=r"ROOT\MEDIA\0000", *, runner=None) -> Path:
    """Create output/dist/RemoteVoicePair and report files; runner is test-only."""
    # Validate exact driver policy before any filesystem mutation.
    from remotevoice.broker_package_manifest import _instance
    _instance(driver_instance_id)
    version = importlib.metadata.version("pyinstaller")  # Existing installation only.
    sources = _sources()
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise FileExistsError("Pair output directory must not already exist")
    output.mkdir()  # Parent must already exist; never replace/clean an old tree.
    directories = {name: output / name for name in ("config", "cache", "work", "dist")}
    for directory in directories.values():
        directory.mkdir()
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(directories["config"])
    env["PYTHONPYCACHEPREFIX"] = str(directories["cache"] / "pycache")
    command = [sys.executable, "-m", "PyInstaller", "--workpath", str(directories["work"]),
               "--distpath", str(directories["dist"]), str(SPEC)]
    run = subprocess.run if runner is None else runner
    with (output / "pyinstaller.log").open("xb") as log:
        run(command, cwd=str(HOST), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
    if _sources() != sources:
        raise RuntimeError("Build sources changed; preserve this output and use a new directory")
    package = directories["dist"] / "RemoteVoicePair"
    encoded = build_manifest(package, driver_instance_id)
    manifest = parse_manifest(encoded)
    with (package / MANIFEST_NAME).open("xb") as target:
        target.write(encoded)
    report = {
        "schema_version": 1, "pyinstaller_version": version,
        "python_version": sys.version, "source_sha256": sources,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "components": {name: manifest.files[name] for name in (manifest.broker, manifest.client)},
        "runtime_files": {name: digest for name, digest in manifest.files.items()
                          if name not in (manifest.broker, manifest.client)},
        "driver_instance_id": manifest.driver_instance_id,
        "packaging_only": True, "installed": False, "signed": False, "launched": False,
    }
    with (output / "build-report.json").open("x", encoding="utf-8") as target:
        json.dump(report, target, sort_keys=True, indent=2)
        target.write("\n")
    return package


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New output directory; parent must exist")
    parser.add_argument("--driver-instance", default=r"ROOT\MEDIA\0000")
    args = parser.parse_args(argv)
    package = build_pair(args.output, args.driver_instance)
    print(f"Pair packaged: {package}")
    print(f"Build report: {package.parents[1] / 'build-report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
