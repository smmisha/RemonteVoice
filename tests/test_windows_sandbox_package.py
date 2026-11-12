import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from remontevoice.broker_package_manifest import MANIFEST_NAME, build_manifest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_windows_sandbox", ROOT / "host/prepare_windows_sandbox.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class SandboxPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.package, self.output = self.folder / "package & copy", self.folder / "sandbox"
        self.package.mkdir()
        for name in ("RemonteVoiceBroker.exe", "RemonteVoice.exe", "_internal/base_library.zip",
                     "_internal/python314.dll"):
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        self.manifest = build_manifest(self.package, r"ROOT\MEDIA\0000")
        (self.package / MANIFEST_NAME).write_bytes(self.manifest)

    def test_complete_package_and_only_new_reports_are_exposed_with_bounded_resources(self):
        config = builder.prepare_sandbox(self.package, self.output)
        xml = ET.parse(config).getroot()
        self.assertEqual(xml.findtext("MemoryInMB"), "2048")
        for key in ("Networking", "AudioInput", "VideoInput", "ClipboardRedirection", "PrinterRedirection", "vGPU"):
            self.assertEqual(xml.findtext(key), "Disable")
        mappings = [(Path(f.findtext("HostFolder")), f.findtext("SandboxFolder"), f.findtext("ReadOnly"))
                    for f in xml.findall("MappedFolders/MappedFolder")]
        self.assertEqual(mappings, [(self.package.resolve(), r"C:\RemonteVoice-Package", "true"),
                                    (self.output / "control", r"C:\RemonteVoice-Control", "true"),
                                    (self.output / "reports", r"C:\RemonteVoice-Reports", "false")])
        self.assertEqual(list((self.output / "reports").iterdir()), [])
        self.assertEqual((self.output / "control/smoke.ps1").read_bytes(), builder.SCRIPT.read_bytes())
        expected = json.loads((self.output / "control/expected.json").read_text("utf-8"))
        self.assertEqual(set(expected["files"]), {"RemonteVoice.exe", "RemonteVoiceBroker.exe",
                                                "_internal/base_library.zip", "_internal/python314.dll"})
        self.assertEqual((self.package / MANIFEST_NAME).read_bytes(), self.manifest)
        self.assertFalse(json.loads((self.output / "preparation.json").read_text("utf-8"))["started"])

    def test_stale_or_extra_package_files_are_rejected_before_output_creation(self):
        (self.package / "RemonteVoice.exe").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            builder.prepare_sandbox(self.package, self.output)
        self.assertFalse(self.output.exists())
        (self.package / "extra.json").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            builder.prepare_sandbox(self.package, self.output)
        self.assertFalse(self.output.exists())

    def test_never_overwrites_previous_evidence_or_writes_into_package(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("earlier", "utf-8")
        with self.assertRaises(FileExistsError):
            builder.prepare_sandbox(self.package, self.output)
        self.assertEqual(marker.read_text("utf-8"), "earlier")
        with self.assertRaises(ValueError):
            builder.prepare_sandbox(self.package, self.package / "test-output")
        self.assertFalse((self.package / "test-output").exists())

    def test_environment_inspection_exposes_no_package_and_has_no_automatic_logon_command(self):
        config = builder.prepare_environment(self.output)
        xml = ET.parse(config).getroot()
        self.assertIsNone(xml.find("LogonCommand"))
        self.assertEqual(xml.findtext("MemoryInMB"), "2048")
        self.assertEqual([(f.findtext("SandboxFolder"), f.findtext("ReadOnly"))
                          for f in xml.findall("MappedFolders/MappedFolder")],
                         [(r"C:\RemonteVoice-Control", "true"), (r"C:\RemonteVoice-Reports", "false")])
        self.assertEqual((self.output / "control/inspect_environment.ps1").read_bytes(),
                         builder.ENVIRONMENT_SCRIPT.read_bytes())
        marker = json.loads((self.output / "control/probe.json").read_text("utf-8"))
        self.assertEqual(marker["mode"], "environment-read-only")
        self.assertEqual(len(marker["probe_id"]), 36)
        self.assertFalse(json.loads((self.output / "preparation.json").read_text("utf-8"))["started"])
        with self.assertRaises(FileExistsError):
            builder.prepare_environment(self.output)

    def test_environment_cli_rejects_a_package_and_regular_cli_requires_one(self):
        for args in (["--environment-only", "--package", str(self.package), "--output", str(self.output)],
                     ["--output", str(self.output)]):
            with self.assertRaises(SystemExit) as stopped:
                builder.main(args)
            self.assertEqual(stopped.exception.code, 2)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell parser and inert child processes")
    def test_guest_script_parses_and_refuses_to_run_on_host(self):
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {**os.environ, "RV_SANDBOX_SCRIPT": str(builder.SCRIPT), "USERNAME": "Not-Sandbox"}
        parsed = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
            "$tokens=$null; $errors=$null; [void][Management.Automation.Language.Parser]::ParseFile("
            "$env:RV_SANDBOX_SCRIPT,[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}; exit 0"],
            env=env, capture_output=True, timeout=15)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        refused = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(builder.SCRIPT)],
                                 env=env, capture_output=True, timeout=15)
        self.assertEqual(refused.returncode, 1)
        self.assertIn(b"Sandbox-only smoke script", refused.stderr)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "nt", "Read-only native Code Integrity observation")
    def test_environment_probe_parses_rejects_host_and_its_ci_query_is_read_only(self):
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {**os.environ, "RV_SANDBOX_ENV_SCRIPT": str(builder.ENVIRONMENT_SCRIPT)}
        command = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:RV_SANDBOX_ENV_SCRIPT,[ref]$tokens,[ref]$errors)
if($errors.Count){exit 1}
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.StringConstantExpressionAst] -and $node.Value.StartsWith('using System;')},$true)
Add-Type -TypeDefinition $definition.Value
$flags=[RemonteVoiceReadOnlyCI]::ReadFlags()
if($flags -isnot [uint32]){exit 1}
"""
        parsed = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                                env=env, capture_output=True, timeout=15)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        refused = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                  "-File", str(builder.ENVIRONMENT_SCRIPT)],
                                 env=env, capture_output=True, timeout=15)
        self.assertEqual(refused.returncode, 1)
        self.assertIn(b"Sandbox-only environment probe", refused.stderr)

    @unittest.skipUnless(os.name == "nt", "Sanitized BCD failure categories")
    def test_bcd_failures_preserve_access_denial_vs_unavailable_store_and_unknown(self):
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {**os.environ, "RV_SANDBOX_ENV_SCRIPT": str(builder.ENVIRONMENT_SCRIPT)}
        command = r"""
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($env:RV_SANDBOX_ENV_SCRIPT,[ref]$null,[ref]$null)
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-BcdErrorClass'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
if((Get-BcdErrorClass 'The boot configuration data store could not be opened. Access is denied.') -ne 'access_denied'){exit 1}
$localized='"\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e \u0432 \u0434\u043e\u0441\u0442\u0443\u043f\u0435."'|ConvertFrom-Json
if((Get-BcdErrorClass $localized) -ne 'access_denied'){exit 1}
if((Get-BcdErrorClass 'The boot configuration data store could not be opened. The system cannot find the file specified.') -ne 'file_not_found'){exit 1}
if((Get-BcdErrorClass 'The requested system device cannot be found.') -ne 'system_device_missing'){exit 1}
if((Get-BcdErrorClass 'A future undocumented failure.') -ne 'unclassified'){exit 1}
"""
        checked = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                                 env=env, capture_output=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    @unittest.skipUnless(os.name == "nt", "BCD WMI Boolean result contract")
    def test_bcd_openstore_requires_true_boolean_and_an_actual_store(self):
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {**os.environ, "RV_SANDBOX_ENV_SCRIPT": str(builder.ENVIRONMENT_SCRIPT)}
        command = r"""
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$ast=[Management.Automation.Language.Parser]::ParseFile($env:RV_SANDBOX_ENV_SCRIPT,[ref]$null,[ref]$null)
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-BcdOpenStoreObservation'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
$store=[pscustomobject]@{FilePath=''}
foreach($value in @($false,$true)) {
    foreach($present in @($false,$true)) {
        $result=[pscustomobject]@{ReturnValue=$value;Store=$(if($present){$store}else{$null})}
        $observed=Get-BcdOpenStoreObservation $result
        if($observed.return_value -isnot [bool] -or $observed.return_value -ne $value){throw 'Boolean result lost'}
        if($observed.store_present -ne $present){throw 'Store presence lost'}
        if($observed.store_opened -ne ($value -and $present)){throw 'False success or false failure'}
    }
}
foreach($invalid in @(0,1,'true','false',$null)) {
    $rejected=$false
    try { $null=Get-BcdOpenStoreObservation ([pscustomobject]@{ReturnValue=$invalid;Store=$store}) }
    catch { $rejected=$true }
    if(-not $rejected){throw 'Unexpected type treated as an OpenStore Boolean'}
}
"""
        checked = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                                 env=env, capture_output=True, timeout=15)
        self.assertEqual(checked.returncode, 0, (checked.stdout, checked.stderr))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell process helper")
    def test_bounded_process_helper_handles_empty_arguments_and_exit_codes(self):
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {**os.environ, "RV_SANDBOX_SCRIPT": str(builder.SCRIPT), "RV_SANDBOX_TEST_WORK": str(self.folder)}
        command = r"""
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$ast=[Management.Automation.Language.Parser]::ParseFile($env:RV_SANDBOX_SCRIPT,[ref]$null,[ref]$null)
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.StringConstantExpressionAst] -and $node.Value.StartsWith('using System;')},$true)
Add-Type -TypeDefinition $definition.Value
$token=[RemonteVoiceSandboxToken]::Read()
if($token.Length -ne 2 -or $token[0] -notin @(1,2,3) -or $token[1] -notin @(0,1)){throw 'Token observation failed'}
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-BoundedCase'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
$work=$env:RV_SANDBOX_TEST_WORK
$owned=[Collections.Generic.List[Diagnostics.Process]]::new()
$report=@{cases=@();cleanup_confirmed=$true}
try {
    Invoke-BoundedCase 'empty' (Join-Path $env:SystemRoot 'System32\whoami.exe') '' 0
    Invoke-BoundedCase 'arguments' (Join-Path $env:SystemRoot 'System32\cmd.exe') '/d /c exit 7' 7
    if($report.cases.Count -ne 2 -or @($report.cases|Where-Object{-not $_.passed}).Count){$report|ConvertTo-Json -Depth 4;exit 1}
} finally {foreach($child in $owned){if(-not $child.HasExited){$child.Kill()};$child.Dispose()}}
"""
        checked = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                                 env=env, capture_output=True, timeout=40)
        self.assertEqual(checked.returncode, 0, (checked.stdout, checked.stderr))


if __name__ == "__main__":
    unittest.main()
