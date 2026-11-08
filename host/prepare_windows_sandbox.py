"""Prepare a bounded Windows Sandbox smoke test; never start or install anything."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from remotevoice.broker_package_manifest import MANIFEST_NAME, MAX_MANIFEST_BYTES, build_manifest, parse_manifest

SCRIPT = Path(__file__).resolve().parent / "sandbox" / "smoke.ps1"
ENVIRONMENT_SCRIPT = SCRIPT.with_name("inspect_environment.ps1")


def _configuration(path, mappings, command=None):
    config = ET.Element("Configuration")
    for key in ("Networking", "AudioInput", "VideoInput", "ClipboardRedirection",
                "PrinterRedirection", "vGPU"):
        ET.SubElement(config, key).text = "Disable"
    ET.SubElement(config, "MemoryInMB").text = "2048"
    folders = ET.SubElement(config, "MappedFolders")
    for host, guest, readonly in mappings:
        folder = ET.SubElement(folders, "MappedFolder")
        ET.SubElement(folder, "HostFolder").text = str(host)
        ET.SubElement(folder, "SandboxFolder").text = guest
        ET.SubElement(folder, "ReadOnly").text = str(readonly).lower()
    if command is not None:
        ET.SubElement(ET.SubElement(config, "LogonCommand"), "Command").text = command
    ET.indent(config)
    ET.ElementTree(config).write(path, encoding="utf-8", xml_declaration=True)


def prepare_sandbox(package: Path, output: Path) -> Path:
    package, output = Path(package).resolve(strict=True), Path(output).absolute()
    if os.path.lexists(output):
        raise FileExistsError("Sandbox output must be a new directory")
    output = output.resolve()
    # Do not certify a stale inventory, extra files or a replacement output tree.
    with (package / MANIFEST_NAME).open("rb") as source:
        encoded = source.read(MAX_MANIFEST_BYTES + 1)
    manifest = parse_manifest(encoded)
    observed = parse_manifest(build_manifest(package, manifest.driver_instance_id))
    if observed != manifest:
        raise ValueError("Package contents differ from the installation manifest")
    script = SCRIPT.read_bytes()
    if os.path.lexists(output):
        raise FileExistsError("Sandbox output must be a new directory")
    if output.is_relative_to(package) or package.is_relative_to(output):
        raise ValueError("Sandbox output must be separate from the package")
    output.mkdir()  # Parent must exist. Never clean or replace earlier evidence.
    control, reports = output / "control", output / "reports"
    control.mkdir()
    reports.mkdir()
    (control / "smoke.ps1").write_bytes(script)
    (control / "expected.json").write_text(json.dumps({
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": dict(manifest.files),
    }, sort_keys=True), "utf-8")
    path = output / "RemoteVoice-Smoke.wsb"
    _configuration(path, ((package, r"C:\RemoteVoice-Package", True),
                          (control, r"C:\RemoteVoice-Control", True),
                          (reports, r"C:\RemoteVoice-Reports", False)),
                   r'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass '
                   r'-File "C:\RemoteVoice-Control\smoke.ps1"')
    (output / "preparation.json").write_text(json.dumps({
        "schema_version": 1, "prepared_only": True, "started": False,
        "memory_in_mb": 2048, "package": str(package),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "script_sha256": hashlib.sha256(script).hexdigest(),
        "scope": "Workspace rejection and muted cleanup only; no protected installation or driver acceptance",
    }, indent=2) + "\n", "utf-8")
    return path


def prepare_environment(output: Path) -> Path:
    """Prepare read-only guest metadata inspection, without any app/driver files."""
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise FileExistsError("Sandbox output must be a new directory")
    output = output.resolve()
    script = ENVIRONMENT_SCRIPT.read_bytes()
    output.mkdir()
    control, reports = output / "control", output / "reports"
    control.mkdir()
    reports.mkdir()
    (control / ENVIRONMENT_SCRIPT.name).write_bytes(script)
    marker = dict(mode="environment-read-only", probe_id=str(uuid.uuid4()))
    (control / "probe.json").write_text(json.dumps(marker), "utf-8")
    path = output / "RemoteVoice-Environment.wsb"
    _configuration(path, ((control, r"C:\RemoteVoice-Control", True),
                          (reports, r"C:\RemoteVoice-Reports", False)))
    (output / "preparation.json").write_text(json.dumps(dict(
        schema_version=1, prepared_only=True, started=False, memory_in_mb=2048,
        mode=marker["mode"], probe_id=marker["probe_id"],
        script_sha256=hashlib.sha256(script).hexdigest(),
        scope="Read-only guest CI/UAC/BCD/audio-service metadata; no settings or installation changes"
    ), indent=2) + "\n", "utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--environment-only", action="store_true", help="Prepare read-only guest metadata inspection")
    parser.add_argument("--output", type=Path, required=True, help="New directory with an existing parent")
    args = parser.parse_args(argv)
    if args.environment_only:
        if args.package is not None:
            parser.error("Environment inspection must not include an application package")
        print(prepare_environment(args.output))
    else:
        if args.package is None:
            parser.error("Package smoke test requires --package")
        print(prepare_sandbox(args.package, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
