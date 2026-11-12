param([Parameter(Mandatory=$true)][string]$ReportPath,[switch]$AllowStopped)
$ErrorActionPreference = 'Stop'
# Run by the desktop shell, so MSIX app-data redirection of the calling editor
# cannot substitute the editor's private RemonteVoice profile.
$rvData = Join-Path $env:APPDATA 'RemonteVoice'
$rvDeadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    $rvRuntime = Get-Content -LiteralPath (Join-Path $rvData 'runtime.json') -Raw | ConvertFrom-Json
    $rvProcess = Get-Process -Id $rvRuntime.pid -ErrorAction SilentlyContinue
    if ($AllowStopped -or ($rvProcess -and $rvProcess.Path -eq $rvRuntime.build.entry_point)) {break}
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $rvDeadline)
if (!$AllowStopped -and !$rvProcess) {throw 'No active desktop Host receipt'}
$rvState = @{runtime=$rvRuntime;data_directory=$rvData;observed_utc=[DateTime]::UtcNow.ToString('o')}
$rvWsh = New-Object -ComObject WScript.Shell
$rvDesktopPath = [string]$rvWsh.SpecialFolders.Item('Desktop')
$rvShortcutPath = Join-Path $rvDesktopPath 'RemonteVoice.lnk'
if (Test-Path -LiteralPath $rvShortcutPath) {
    $rvLink = $rvWsh.CreateShortcut($rvShortcutPath)
    $rvState.desktop_shortcut = @{path=$rvShortcutPath;target=$rvLink.TargetPath;arguments=$rvLink.Arguments}
}
[Runtime.InteropServices.Marshal]::FinalReleaseComObject($rvWsh) | Out-Null
$rvGuardFile = Join-Path $rvData 'safety-guard-recovery.json'
if(Test-Path -LiteralPath $rvGuardFile) {
    $rvState.guard_recovery = Get-Content -LiteralPath $rvGuardFile -Raw | ConvertFrom-Json
}
$rvLogFile = Join-Path $rvData 'host.log'
if(Test-Path -LiteralPath $rvLogFile) {
    # Export only our scalar timing rows, never arbitrary app logs/configuration.
    $rvLogStream = [IO.File]::Open($rvLogFile,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
    try {
        $rvLogReader = New-Object IO.StreamReader($rvLogStream)
        $rvLogText = $rvLogReader.ReadToEnd()
        $rvState.audio_timing = @($rvLogText -split "`n" |
            Where-Object {$_ -match 'system_audio_timing frame=\d+ clock_ns=\d+'} | Select-Object -Last 200)
    } finally { $rvLogStream.Dispose() }
}
$rvTemporary = $ReportPath + '.tmp'
$rvState | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $rvTemporary -Encoding utf8
Move-Item -LiteralPath $rvTemporary -Destination $ReportPath -Force
