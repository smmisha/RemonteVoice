"""Identify the launched Host without including user configuration or credentials."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.30"


def build_identity() -> dict:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        return {"kind": "executable", "sha256": digest, "entry_point": str(executable)}
    source = Path(__file__).resolve().parent
    files = sorted(source.glob("*.py"))
    launcher = source.parent / "launcher.py"
    if launcher.is_file():
        files.append(launcher)
    entries = [{"path": str(path), "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"kind": "source_snapshot", "sha256": hashlib.sha256(canonical).hexdigest(),
            "entry_point": str(Path(sys.argv[0]).resolve()), "files": entries}


def write_receipt(path: Path) -> None:
    receipt = {"version": VERSION, "observed_utc": datetime.now(timezone.utc).isoformat(),
               "pid": os.getpid(), "parent_pid": os.getppid(),
               "python": sys.version, "executable": sys.executable, "build": build_identity()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
