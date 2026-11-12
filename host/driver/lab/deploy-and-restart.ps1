# Elevated: deploy the current build, refresh the autostart task and restart the host.
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'rv-deploy-restart.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $source = 'M:\Projectorium\RemonteVoice\host\build\desktop-061\dist\RemonteVoicePair'
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

    if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
    Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force -ErrorAction Stop
    $sourceHash = (Get-FileHash -LiteralPath (Join-Path $source 'RemonteVoice.exe') -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath (Join-Path $target 'RemonteVoice.exe') -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash) { throw "Deployed executable hash mismatch: $targetHash vs $sourceHash" }
    Write-Host "verified executable hash: $targetHash"

    & icacls $target /setowner 'BUILTIN\Administrators' /T /C | Out-Null
    & icacls $target /grant 'BUILTIN\Administrators:(OI)(CI)F' 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Users:(OI)(CI)RX' | Out-Null

    $xmlPath = 'M:\Projectorium\RemonteVoice\host\build\driver-host-package\remontevoice-task.xml'
    $content = Get-Content -LiteralPath $xmlPath -Raw -Encoding UTF8
    $content = $content.Replace('--driver-direct --autostart', '--autostart')
    [IO.File]::WriteAllText('C:\Windows\Temp\remontevoice-task.xml', $content, [Text.Encoding]::Unicode)
    & schtasks /create /tn 'RemonteVoice' /xml 'C:\Windows\Temp\remontevoice-task.xml' /f | Out-Host

    & schtasks /run /tn 'RemonteVoice' | Out-Host
    Start-Sleep -Seconds 18
    Write-Host '=== processes ==='
    Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue | Select-Object Id, StartTime | Format-Table -AutoSize | Out-Host
    Write-Host '=== listening 8765 ==='
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, OwningProcess | Format-Table -AutoSize | Out-Host
} catch {
    Write-Host "DEPLOY/RESTART FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
