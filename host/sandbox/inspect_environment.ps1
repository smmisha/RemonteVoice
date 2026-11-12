# Read-only guest feasibility check. No driver, certificate, registry or BCD writes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSScriptRoot -cne 'C:\RemonteVoice-Control' -or -not (Test-Path -LiteralPath 'C:\RemonteVoice-Reports')) {
    throw 'Sandbox-only environment probe'
}
$marker = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'probe.json') -Raw | ConvertFrom-Json
if ($marker.mode -cne 'environment-read-only') { throw 'Unexpected probe scope' }
[void][guid]::Parse($marker.probe_id)
$resultPath = 'C:\RemonteVoice-Reports\environment.json'
if (Test-Path -LiteralPath $resultPath) { throw 'Refusing to overwrite an earlier report' }
$report = [ordered]@{schema_version=1;probe_id=$marker.probe_id;success=$false;error=$null
    cleanup_confirmed=$true;run_as_system=$false;os_build=$null;enable_lua=$null
    ci_query_succeeded=$false;ci_flags=$null;ci_enabled=$null;testsigning_active=$null;hvci_active=$null
    bcd_read_exit_code=$null;bcd_store_accessible=$false;bcd_testsigning_declared=$null
    bcd_error_class=$null;bcd_registry_visible=$null;bcd_wmi_provider_accessible=$false
    bcd_wmi_store_opened=$null;bcd_wmi_return_code=$null;bcd_wmi_error_code=$null
    bcd_wmi_return_value=$null;bcd_wmi_store_present=$null
    audio_service_state=$null;audio_endpoint_service_state=$null;portcls_present=$false
    pnputil_present=$false;settings_changed=$false;driver_installed=$false;phase='identity'}
function Get-BcdErrorClass([string]$Text) {
    if ($Text -match '(?i)access is denied|\u043e\u0442\u043a\u0430\u0437\u0430\u043d\u043e \u0432 \u0434\u043e\u0441\u0442\u0443\u043f\u0435') { return 'access_denied' }
    if ($Text -match '(?i)requested system device cannot be found|\u043d\u0435 \u0443\u0434\u0430\u0435\u0442\u0441\u044f \u043d\u0430\u0439\u0442\u0438 \u0437\u0430\u043f\u0440\u043e\u0448\u0435\u043d\u043d\u043e\u0435 \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u043e\u0435 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e') { return 'system_device_missing' }
    if ($Text -match '(?i)cannot find the file|\u043d\u0435 \u0443\u0434\u0430\u0435\u0442\u0441\u044f \u043d\u0430\u0439\u0442\u0438 \u0443\u043a\u0430\u0437\u0430\u043d\u043d\u044b\u0439 \u0444\u0430\u0439\u043b') { return 'file_not_found' }
    if ($Text -match '(?i)could not be opened|\u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043a\u0440\u044b\u0442\u044c') { return 'store_open_failed' }
    return 'unclassified'
}
function Get-BcdOpenStoreObservation($Result) {
    # OpenStore returns a Boolean, not an HRESULT: false must never mean success.
    # https://learn.microsoft.com/en-us/previous-versions/windows/desktop/bcd/openstore-bcdstore
    if ($null -eq $Result -or $Result.ReturnValue -isnot [bool]) {
        throw 'Unexpected BCD OpenStore result type'
    }
    $present = ($null -ne $Result.Store)
    return [pscustomobject]@{return_value=$Result.ReturnValue;store_present=$present
        store_opened=($Result.ReturnValue -and $present)}
}
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $report.run_as_system = ($identity.User.Value -ceq 'S-1-5-18') } finally { $identity.Dispose() }
    if (-not $report.run_as_system) { throw 'This inspection requires the explicit guest System context' }
    $report.phase = 'os_and_uac'
    $report.os_build = [string](Get-CimInstance Win32_OperatingSystem).BuildNumber
    $policy = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    $report.enable_lua = [int]$policy.EnableLUA
    $report.phase = 'live_code_integrity'
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RemonteVoiceReadOnlyCI {
    [StructLayout(LayoutKind.Sequential)]
    struct CodeIntegrity { public uint Length; public uint Options; }
    [DllImport("ntdll.dll")]
    static extern int NtQuerySystemInformation(uint kind, ref CodeIntegrity value, uint size, out uint returned);
    public static uint ReadFlags() {
        CodeIntegrity value = new CodeIntegrity {Length=8, Options=0};
        uint returned;
        int status = NtQuerySystemInformation(103, ref value, 8, out returned);
        if (status != 0 || returned != 8 || value.Length != 8) throw new InvalidOperationException();
        return value.Options;
    }
}
'@
    $flags = [RemonteVoiceReadOnlyCI]::ReadFlags()
    $report.ci_query_succeeded = $true
    $report.ci_flags = [long]$flags
    $report.ci_enabled = (($flags -band 1) -ne 0)
    $report.testsigning_active = (($flags -band 2) -ne 0)
    $report.hvci_active = (($flags -band 0x400) -ne 0)
    $report.phase = 'bcd_cli_read'
    $bcd = [Diagnostics.Process]::new()
    try {
        $bcd.StartInfo = [Diagnostics.ProcessStartInfo]::new()
        $bcd.StartInfo.FileName = Join-Path $env:SystemRoot 'System32\bcdedit.exe'
        $bcd.StartInfo.Arguments = '/enum {current}'
        $bcd.StartInfo.UseShellExecute = $false
        $bcd.StartInfo.CreateNoWindow = $true
        $bcd.StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
        $bcd.StartInfo.RedirectStandardOutput = $true
        $bcd.StartInfo.RedirectStandardError = $true
        if (-not $bcd.Start()) { throw 'BCD inspection did not start' }
        $stdout, $stderr = $bcd.StandardOutput.ReadToEndAsync(), $bcd.StandardError.ReadToEndAsync()
        if (-not $bcd.WaitForExit(10000)) { throw 'BCD inspection timed out' }
        if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) { throw 'BCD streams did not complete' }
        $report.bcd_read_exit_code = $bcd.ExitCode
        $report.bcd_store_accessible = ($bcd.ExitCode -eq 0)
        if ($bcd.ExitCode -ne 0) { $report.bcd_error_class = Get-BcdErrorClass ($stdout.Result + $stderr.Result) }
        if ($bcd.ExitCode -eq 0 -and $stdout.Result -match '(?im)^\s*testsigning\s+(Yes|No|On|Off)\s*$') {
            $report.bcd_testsigning_declared = ($matches[1] -in @('Yes','On'))
        }
    } finally {
        try {
            if (-not $bcd.HasExited) { $bcd.Kill(); [void]$bcd.WaitForExit(2000) }
            if (-not $bcd.HasExited) { $report.cleanup_confirmed = $false }
        } catch { $report.cleanup_confirmed = $false }
        $bcd.Dispose()
    }
    $report.phase = 'bcd_wmi_read'
    $report.bcd_registry_visible = Test-Path -LiteralPath 'Registry::HKEY_LOCAL_MACHINE\BCD00000000'
    $class = $opened = $null
    try {
        $class = [wmiclass]'\\.\root\WMI:BcdStore'
        $opened = $class.OpenStore('') # Read the default store; never create or set one.
        $report.bcd_wmi_provider_accessible = $true
        $observation = Get-BcdOpenStoreObservation $opened
        $report.bcd_wmi_return_value = $observation.return_value
        $report.bcd_wmi_store_present = $observation.store_present
        $report.bcd_wmi_store_opened = $observation.store_opened
        # Keep the legacy numeric field null; this API has no numeric return code.
    } catch {
        $report.bcd_wmi_error_code = ('0x{0:x8}' -f $_.Exception.HResult)
    } finally {
        try {
            if ($null -ne $opened) { $opened.Dispose() }
            if ($null -ne $class) { $class.Dispose() }
        } catch { $report.cleanup_confirmed = $false }
    }
    $report.phase = 'audio_metadata'
    $report.audio_service_state = [string](Get-Service -Name Audiosrv).Status
    $report.audio_endpoint_service_state = [string](Get-Service -Name AudioEndpointBuilder).Status
    $report.portcls_present = Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\drivers\portcls.sys')
    $report.pnputil_present = Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\pnputil.exe')
    $report.success = $report.cleanup_confirmed
    $report.phase = 'complete'
} catch { $report.error = 'environment_inspection_failed' }
$bytes = [Text.UTF8Encoding]::new($false).GetBytes(($report | ConvertTo-Json -Depth 3))
$stream = [IO.File]::Open($resultPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try { $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
if (-not $report.success) { exit 1 }
exit 0
