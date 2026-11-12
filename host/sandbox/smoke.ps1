# Runs inside Windows Sandbox only. No installation, ACL/trust/UAC/driver changes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:USERNAME -cne 'WDAGUtilityAccount') { throw 'Sandbox-only smoke script' }
$resultPath = 'C:\RemonteVoice-Reports\result.json'
if (Test-Path -LiteralPath $resultPath) { throw 'Refusing to overwrite an earlier report' }
$report = [ordered]@{
    schema_version = 1; success = $false; error = $null
    package_verified = $false; cleanup_confirmed = $true
    account_token_type = 0; linked_token_present = $false
    cases = @(); driver_tested = $false; audio_opened = $false
}
$owned = [Collections.Generic.List[Diagnostics.Process]]::new()
function Invoke-BoundedCase([string]$Name, [string]$Executable, [string]$Arguments, [int]$Expected) {
    # Start-Process in Windows PowerShell can lose ExitCode for quick children.
    # Own the Process before launch and asynchronously discard both log streams.
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $child.StartInfo.FileName = $Executable
    $child.StartInfo.Arguments = $Arguments
    $child.StartInfo.WorkingDirectory = $work
    $child.StartInfo.UseShellExecute = $false
    $child.StartInfo.CreateNoWindow = $true
    $child.StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $child.StartInfo.RedirectStandardOutput = $true
    $child.StartInfo.RedirectStandardError = $true
    if (-not $child.Start()) { $child.Dispose(); throw 'Child did not start' }
    $owned.Add($child)
    $child.BeginOutputReadLine()
    $child.BeginErrorReadLine()
    $timedOut = -not $child.WaitForExit(15000)
    if ($timedOut) { $child.Kill(); [void]$child.WaitForExit(2000) }
    $clean = $child.HasExited
    $code = if ($clean) { $child.ExitCode } else { -1 }
    $report.cases += [ordered]@{name=$Name; exit_code=$code; expected_exit=$Expected
        timed_out=$timedOut; cleanup_confirmed=$clean; passed=($clean -and -not $timedOut -and $code -eq $Expected)}
    if (-not $clean) { $report.cleanup_confirmed = $false }
}
try {
    $expected = Get-Content -LiteralPath 'C:\RemonteVoice-Control\expected.json' -Raw | ConvertFrom-Json
    $work = Join-Path $env:LOCALAPPDATA ('RemonteVoice-Smoke-' + [guid]::NewGuid().ToString('N'))
    [void](New-Item -ItemType Directory -Path $work)
    Copy-Item -LiteralPath 'C:\RemonteVoice-Package' -Destination (Join-Path $work 'package') -Recurse
    $package = Join-Path $work 'package'
    $manifest = Join-Path $package 'remontevoice-installation.json'
    if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expected.manifest_sha256) {
        throw 'Manifest mismatch'
    }
    $files = @(Get-ChildItem -LiteralPath $package -Recurse -File)
    if ($files.Count -ne (@($expected.files.psobject.Properties).Count + 1)) { throw 'File set mismatch' }
    foreach ($entry in $expected.files.psobject.Properties) {
        $path = Join-Path $package $entry.Name.Replace('/', '\')
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value) {
            throw 'Package hash mismatch'
        }
    }
    $report.package_verified = $true
    # Read-only, scalar token facts. Do not change Sandbox's account/UAC policy.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RemonteVoiceSandboxToken {
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr t, int k, IntPtr p, int n, out int used);
    public static int[] Read() {
        IntPtr token = IntPtr.Zero, linked = IntPtr.Zero, data = Marshal.AllocHGlobal(IntPtr.Size);
        int kind = 0, present = 0, used;
        bool clean = true;
        try {
            if (!OpenProcessToken(GetCurrentProcess(), 8, out token)) throw new InvalidOperationException();
            if (!GetTokenInformation(token, 18, data, 4, out used) || used != 4) throw new InvalidOperationException();
            kind = Marshal.ReadInt32(data);
            if (GetTokenInformation(token, 19, data, IntPtr.Size, out used) && used == IntPtr.Size) {
                linked = Marshal.ReadIntPtr(data);
                present = linked != IntPtr.Zero ? 1 : 0;
            }
        } finally {
            if (linked != IntPtr.Zero) clean = CloseHandle(linked) && clean;
            if (token != IntPtr.Zero) clean = CloseHandle(token) && clean;
            Marshal.FreeHGlobal(data);
        }
        if (!clean) throw new InvalidOperationException();
        return new int[] {kind, present};
    }
}
'@
    $report.cleanup_confirmed = $false
    $token = [RemonteVoiceSandboxToken]::Read()
    $report.cleanup_confirmed = $true
    $report.account_token_type = $token[0]
    $report.linked_token_present = ($token[1] -eq 1)
    Invoke-BoundedCase 'broker_workspace' (Join-Path $package 'RemonteVoiceBroker.exe') '' 1
    Invoke-BoundedCase 'broker_policy' (Join-Path $package 'RemonteVoiceBroker.exe') '--policy untrusted.json' 64
    $probePath = Join-Path $work 'muted.json'
    Invoke-BoundedCase 'client_muted' (Join-Path $package 'RemonteVoice.exe') `
        ('--broker-lab-probe --broker-lab-report "' + $probePath + '"') 1
    $probe = Get-Content -LiteralPath $probePath -Raw | ConvertFrom-Json
    if ($probe.success -isnot [bool] -or $probe.success -or
        $probe.cleanup_confirmed -isnot [bool] -or -not $probe.cleanup_confirmed -or
        $probe.sent_frames -ne 0 -or $probe.synthetic_pcm -isnot [bool] -or $probe.synthetic_pcm) {
        throw 'Muted probe did not fail cleanly'
    }
    $report.success = ($report.cases.Count -eq 3 -and @($report.cases | Where-Object { -not $_.passed }).Count -eq 0)
    if (-not $report.success) { $report.error = 'negative_case_failed' }
} catch {
    $report.success = $false
    $report.error = 'sandbox_smoke_failed' # Never export raw exception paths or logs.
} finally {
    foreach ($child in $owned) {
        try {
            if (-not $child.HasExited) { $child.Kill(); [void]$child.WaitForExit(2000) }
            if (-not $child.HasExited) { $report.cleanup_confirmed = $false }
            $child.Dispose()
        } catch { $report.cleanup_confirmed = $false }
    }
    if (-not $report.cleanup_confirmed) { $report.success = $false }
    $json = $report | ConvertTo-Json -Depth 5
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $stream = [IO.File]::Open($resultPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
}
if (-not $report.success) { exit 1 }
exit 0
