"""Installer helper behaviour: exact file-set install, manifest rewrite, integrity.

These checks run the shipped PowerShell helpers as an ordinary user against
temporary directories; no elevation, no real installation and no registry or
device access.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "host" / "installer" / "common.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _ps_string(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell helpers")
class InstallerHelperTests(unittest.TestCase):
    def run_script(self, body: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "installer-helper-test.ps1"
            script.write_text(body, encoding="utf-8")
            return subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)

    def assert_success(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)

    def build_package(self, directory: Path) -> None:
        payload = directory / "data"
        payload.mkdir(parents=True)
        (payload / "a.txt").write_text("alpha", encoding="utf-8")
        (payload / "b.txt").write_text("beta", encoding="utf-8")
        files = {"data/" + name: hashlib.sha256((payload / name).read_bytes()).hexdigest()
                 for name in ("a.txt", "b.txt")}
        manifest = {
            "schema_version": 1,
            "product": "RemonteVoice",
            "version": "0.0.1",
            "files": files,
        }
        (directory / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_package_integrity_accepts_exact_bytes_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            self.build_package(package)
            body = f"""
$ErrorActionPreference = 'Stop'
. {_ps_string(COMMON)}
function Assert([bool]$Condition, [string]$Message) {{ if (-not $Condition) {{ throw "ASSERT: $Message" }} }}
$manifest = Read-RvPackageManifest {_ps_string(package)}
$count = Test-RvPackageIntegrity $manifest {_ps_string(package)}
Assert ($count -eq 2) "expected 2 verified files, got $count"
[IO.File]::WriteAllText((Join-Path {_ps_string(package)} 'data\\a.txt'), 'changed')
$threw = $false
try {{ $null = Test-RvPackageIntegrity $manifest {_ps_string(package)} }} catch {{ $threw = $true }}
Assert $threw 'a modified package file was accepted'
Write-Host 'OK'
"""
            self.assert_success(self.run_script(body))

    def test_app_install_removes_stale_files_and_rewrites_driver_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app"
            (source / "sub").mkdir(parents=True)
            (source / "keep.txt").write_text("new", encoding="utf-8")
            (source / "sub" / "keep2.txt").write_text("nested", encoding="utf-8")
            files = {"keep.txt": hashlib.sha256(b"new").hexdigest(),
                     "sub/keep2.txt": hashlib.sha256(b"nested").hexdigest()}
            manifest = {"schema_version": 1, "broker": "RemonteVoiceBroker.exe",
                        "client": "RemonteVoice.exe", "driver_instance_id": "ROOT\\MEDIA\\0001",
                        "files": files}
            (source / "remontevoice-installation.json").write_text(json.dumps(manifest), encoding="utf-8")
            target = Path(temporary) / "target"
            target.mkdir()
            (target / "keep.txt").write_text("old", encoding="utf-8")
            (target / "stale.bin").write_text("stale", encoding="utf-8")
            body = f"""
$ErrorActionPreference = 'Stop'
. {_ps_string(COMMON)}
function Assert([bool]$Condition, [string]$Message) {{ if (-not $Condition) {{ throw "ASSERT: $Message" }} }}
$result = Install-RvAppFiles {_ps_string(source)} {_ps_string(target)}
Assert (-not (Test-Path -LiteralPath (Join-Path {_ps_string(target)} 'stale.bin'))) 'stale file survived'
Assert ((Get-Content -LiteralPath (Join-Path {_ps_string(target)} 'keep.txt') -Raw) -eq 'new') 'file was not replaced'
Assert (Test-Path -LiteralPath (Join-Path {_ps_string(target)} 'sub\\keep2.txt')) 'nested file missing'
Assert ($result.removed -eq 1) "expected one removed file, got $($result.removed)"
$rewritten = Set-RvManifestDriverInstance (Join-Path {_ps_string(target)} 'remontevoice-installation.json') 'ROOT\\MEDIA\\0002'
Assert $rewritten 'instance was not rewritten'
$document = Get-Content -LiteralPath (Join-Path {_ps_string(target)} 'remontevoice-installation.json') -Raw | ConvertFrom-Json
Assert ($document.driver_instance_id -eq 'ROOT\\MEDIA\\0002') 'instance value wrong'
$again = Set-RvManifestDriverInstance (Join-Path {_ps_string(target)} 'remontevoice-installation.json') 'ROOT\\MEDIA\\0002'
Assert (-not $again) 'an unchanged instance was rewritten'
Write-Host 'OK'
"""
            self.assert_success(self.run_script(body))


if __name__ == "__main__":
    unittest.main()
