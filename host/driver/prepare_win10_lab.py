"""Prepare an UNSIGNED Windows 10 lab variant; never install, sign or fetch tools.

Changing INF matching permits a later lab attempt. It does not prove that the
kernel driver works on Windows 10, or add Windows 10 to supported releases.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD = ROOT / "host/build/driver-development"
OUT = ROOT / "host/build/driver-windows10-lab"
VERSION = "10.0.26100.6584"
KIT = "10.0.26100.0"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    manifest_path = BUILD / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for name, digest in manifest["inputs"].items():
        if sha(ROOT / name) != digest:
            raise RuntimeError(f"Stale build input: {name}")
    binary = ROOT / manifest["binary"]
    if sha(binary) != manifest["binary_sha256"]:
        raise RuntimeError("Kernel binary no longer matches build manifest")
    original = (HERE / "RemonteVoiceAudio.inf").read_bytes()
    if original != (BUILD / "RemonteVoiceAudio.inf").read_bytes():
        raise RuntimeError("Built INF no longer matches source")
    if original.count(b"NTamd64.10.0...22000") != 2:
        raise RuntimeError("Expected exactly two Windows 11 INF decorations")
    derived = original.replace(b"NTamd64.10.0...22000", b"NTamd64.10.0...19041")
    tools = HERE / f".tools/microsoft.windows.wdk.x64.{VERSION}/c"
    verifier = tools / f"tools/{KIT}/x64/infverif.exe"
    catalog_tool = tools / f"bin/{KIT}/x86/Inf2Cat.exe"
    for tool in (verifier, catalog_tool):
        if not tool.is_file():
            raise RuntimeError(f"Missing approved local tool: {tool}")
    OUT.mkdir(exist_ok=False)  # Never overwrite an existing or signed package.
    (OUT / "RemonteVoiceAudio.sys").write_bytes(binary.read_bytes())
    inf = OUT / "RemonteVoiceAudio.inf"
    inf.write_bytes(derived)
    (OUT / "inf-derivation.diff").write_text("".join(difflib.unified_diff(
        original.decode().splitlines(keepends=True), derived.decode().splitlines(keepends=True),
        fromfile="source/RemonteVoiceAudio.inf", tofile="windows10-lab/RemonteVoiceAudio.inf")), "utf-8", newline="")
    commands = {
        "infverif": [str(verifier), "/u", str(inf)],
        "inf2cat": [str(catalog_tool), f"/driver:{OUT}", "/os:10_VB_X64", "/verbose"],
    }
    for name, command in commands.items():
        result = subprocess.run(command, capture_output=True, timeout=120)
        (OUT / f"{name}.log").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{name} exit {result.returncode}; see output log")
    files = ("RemonteVoiceAudio.sys", "RemonteVoiceAudio.inf", "RemonteVoiceAudio.cat")
    receipt = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Experimental Windows 10 >=19041 x64 lab attempt only",
        "derivation": "Only the two manufacturer/model minimum-build decorations changed22000->19041",
        "source_build_manifest_sha256": sha(manifest_path), "source_inf_sha256": sha(HERE / inf.name),
        "generator_sha256": sha(Path(__file__)), "kernel_binary_unchanged": True,
        "catalog_os_identifier": "10_VB_X64", "infverif_exit": 0, "inf2cat_exit": 0,
        "signed": False, "installed": False, "runtime_acceptance": "not_run", "release_ready": False,
        "windows10_supported_release": False,
        "files": {name: sha(OUT / name) for name in files},
    }
    (OUT / "lab-package-manifest.json").write_text(json.dumps(receipt, indent=2) + "\n", "utf-8")
    print("Windows 10 lab INF and unsigned catalog checked. Driver not installed or runtime-tested.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT,
                        help="New output directory under host/build; never overwritten")
    OUT = parser.parse_args().output.resolve()
    build_root = (ROOT / "host/build").resolve()
    if OUT == build_root or not OUT.is_relative_to(build_root):
        raise ValueError("Output must be a new directory below host/build")
    main()
