# Elevated: deploy the current build, register the broker logon task and start the broker.
# Superseded for pair installations by deploy-windowless-broker.ps1: that script verifies
# the windowed broker, drops files outside the new manifest, waits for the broker pipe and
# rolls back to the previous build. This script keeps its original simpler behaviour.
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'rv-broker-setup.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $source = 'M:\Projectorium\RemonteVoice\host\build\desktop-063\dist\RemonteVoicePair'
    $target = 'C:\Program Files\RemonteVoice-Dev'
    if (-not (Test-Path -LiteralPath $source)) { throw "Source missing: $source" }

    Get-Process -Name RemonteVoice,RemonteVoiceBroker -ErrorAction SilentlyContinue | Stop-Process -Force
    & schtasks /end /tn 'RemonteVoice' 2>&1 | Out-Host
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 400
        $busy = $false
        foreach ($probe in @((Join-Path $target 'RemonteVoice.exe'), (Join-Path $target 'RemonteVoiceBroker.exe'))) {
            if (Test-Path -LiteralPath $probe) {
                try { $fs = [IO.File]::Open($probe, 'Open', 'ReadWrite', 'None'); $fs.Dispose() } catch { $busy = $true }
            }
        }
    } while ($busy -and (Get-Date) -lt $deadline)
    if ($busy) { throw 'Installed executables are still locked' }

    Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force -ErrorAction Stop
    $sourceHash = (Get-FileHash -LiteralPath (Join-Path $source 'RemonteVoice.exe') -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath (Join-Path $target 'RemonteVoice.exe') -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash) { throw "Deployed executable hash mismatch: $targetHash vs $sourceHash" }
    Write-Host "verified executable hash: $targetHash"

    & icacls $target /setowner 'BUILTIN\Administrators' /T /C | Out-Null
    & icacls $target /grant 'BUILTIN\Administrators:(OI)(CI)F' 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Users:(OI)(CI)RX' | Out-Null

    $xmlPath = 'M:\Projectorium\RemonteVoice\host\build\driver-host-package\remontevoice-task.xml'
    $content = Get-Content -LiteralPath $xmlPath -Raw -Encoding UTF8
    [IO.File]::WriteAllText('C:\Windows\Temp\remontevoice-task.xml', $content, [Text.Encoding]::Unicode)
    & schtasks /create /tn 'RemonteVoice' /xml 'C:\Windows\Temp\remontevoice-task.xml' /f | Out-Host
    & schtasks /run /tn 'RemonteVoice' | Out-Host

    Start-Sleep -Seconds 8
    Write-Host '=== broker ==='
    Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue | Select-Object Id, StartTime | Format-Table -AutoSize | Out-Host
    Write-Host '=== pipes ==='
    [System.IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice' } | ForEach-Object { Write-Host "  $_" }
} catch {
    Write-Host "BROKER SETUP FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
