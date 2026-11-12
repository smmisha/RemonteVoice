"""Build an unsigned development .sys locally; never install, sign or fetch tools."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / "host/build/driver-development"
TOOLS = HERE / ".tools"
PACKAGE_VERSION = "10.0.26100.6584"
KIT_VERSION = "10.0.26100.0"
VC = Path("C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Tools/MSVC/14.44.35207")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(name: str, args: list[str]) -> None:
    result = subprocess.run(args, cwd=OUT, capture_output=True, timeout=300)
    (OUT / f"{name}.log").write_bytes(result.stdout + result.stderr)
    print(f"{name}: exit {result.returncode}", flush=True)
    if result.returncode:
        raise RuntimeError(f"See {OUT / (name + '.log')}")


def main() -> None:
    wdk = TOOLS / f"microsoft.windows.wdk.x64.{PACKAGE_VERSION}/c"
    sdk = TOOLS / f"microsoft.windows.sdk.cpp.{PACKAGE_VERSION}/c"
    compiler = VC / "bin/Hostx64/x64/cl.exe"
    linker = compiler.with_name("link.exe")
    verifier = wdk / f"tools/{KIT_VERSION}/x64/infverif.exe"
    for path in (compiler, linker, verifier, wdk / f"Include/{KIT_VERSION}/km/portcls.h"):
        if not path.is_file():
            raise RuntimeError(f"Missing existing tool: {path}; no installation attempted")
    OUT.mkdir(parents=True, exist_ok=True)
    source = HERE / "src"
    # Explicit objects prevent leftovers from a prior build adding a file sink.
    sources = sorted(p for p in source.rglob("*.cpp") if p.name.lower() not in {"savedata.cpp", "tonegenerator.cpp"})
    if len({p.stem.lower() for p in sources}) != len(sources):
        raise RuntimeError("Duplicate object names")
    includes = [wdk / f"Include/{KIT_VERSION}/km", wdk / "Include/wdf/kmdf/1.15",
                sdk / f"Include/{KIT_VERSION}/shared", sdk / f"Include/{KIT_VERSION}/um",
                sdk / f"Include/{KIT_VERSION}/ucrt", VC / "include"]
    includes += [source / p for p in ("Inc", "Main", "Utilities", "Filters")]
    flags = ["/nologo", "/c", "/kernel", "/Zl", "/GR-", "/GS", "/W4", "/WX",
             "/wd4324", "/O2", "/Zi", "/EHs-c-", "/Zc:wchar_t-", "/std:c++17"]
    # C4324 is WDF's intentional cache alignment; all other warnings stay errors.
    defines = ["_AMD64_", "AMD64", "_WIN64", "_WIN32", "UNICODE", "_UNICODE",
               "PC_IMPLEMENTATION", "_USE_WAVERT_",
               "_NEW_DELETE_OPERATORS_", "POOL_NX_OPTIN=1", "NTDDI_VERSION=0x0A000008",
               "_WIN32_WINNT=0x0A00", "WINVER=0x0A00", "DEBUG_LEVEL=0"]
    run("compile", [str(compiler), *flags, *("/D" + d for d in defines),
                    *("/I" + str(p) for p in includes), f"/Fo{OUT}/", f"/Fd{OUT}/compiler.pdb",
                    *map(str, sources)])
    libs = wdk / f"Lib/{KIT_VERSION}/km/x64"
    wdf = wdk / "Lib/wdf/kmdf/x64/1.15"
    binary = OUT / "RemonteVoiceAudio.sys"
    run("link", [str(linker), "/nologo", "/driver", "/subsystem:native,10.00", "/machine:x64",
                 "/entry:FxDriverEntry", "/nodefaultlib", "/integritycheck", "/debug", "/opt:ref", "/opt:icf",
                 f"/out:{binary}", f"/pdb:{OUT}/RemonteVoiceAudio.pdb",
                 *(str(OUT / (p.stem + ".obj")) for p in sources),
                 *(str(libs / (name + ".lib")) for name in
                   ("ntoskrnl", "hal", "wmilib", "portcls", "stdunk", "libcntpr", "ksguid", "BufferOverflowK")),
                 *(str(wdf / (name + ".lib")) for name in ("WdfDriverEntry", "WdfLdr"))])
    inf = OUT / "RemonteVoiceAudio.inf"
    inf.write_bytes((HERE / inf.name).read_bytes())
    run("infverif", [str(verifier), "/u", str(inf)])
    run("imports", [str(compiler.with_name("dumpbin.exe")), "/imports", str(binary)])
    imports = (OUT / "imports.log").read_text("utf-8", errors="replace")
    if any(name in imports for name in ("ZwCreateFile", "ZwWriteFile", "__stdio_common", "ucrtbase.dll")):
        raise RuntimeError("Unexpected file sink / user CRT import")
    inputs = [p for p in source.rglob("*") if p.is_file()] + [Path(__file__), HERE / inf.name]
    manifest = {"executed_at": datetime.now(timezone.utc).isoformat(), "package_version": PACKAGE_VERSION,
                "compiler": str(compiler), "compiler_sha256": sha256(compiler),
                "binary": str(binary.relative_to(ROOT)), "binary_bytes": binary.stat().st_size,
                "binary_sha256": sha256(binary), "unsigned": True, "installed": False,
                "signed": False, "inf_universal_validation": "passed",
                "inputs": {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(inputs)},
                "runtime_acceptance": "not_run", "release_ready": False}
    (OUT / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    print("Unsigned development driver built and INF checked. Not installed; not release-ready.")


if __name__ == "__main__":
    main()
