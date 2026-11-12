# Elevated, single prompt: deploy build 054 to the protected folder and start it in direct driver mode.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-deploy-launch.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $source = 'M:\Projectorium\RemonteVoice\host\build\desktop-055\dist\RemonteVoicePair'
    $target = 'C:\Program Files\RemonteVoice-Dev'
    if (-not (Test-Path -LiteralPath $source)) { throw "Source missing: $source" }

    Write-Host 'Stopping running copies...'
    Get-Process -Name RemonteVoice,RemonteVoiceBroker -ErrorAction SilentlyContinue | Stop-Process -Force
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
    Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force
    & icacls $target /setowner 'BUILTIN\Administrators' /T /C | Out-Host
    & icacls $target /grant 'BUILTIN\Administrators:(OI)(CI)F' 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Users:(OI)(CI)RX' | Out-Host
    Write-Host "deployed: $((Get-Item (Join-Path $target 'RemonteVoice.exe')).LastWriteTime)"

    Write-Host 'Starting RemonteVoice in direct driver mode (elevated)...'
    $process = Start-Process -FilePath (Join-Path $target 'RemonteVoice.exe') `
        -ArgumentList '--driver-direct', '--autostart' -WorkingDirectory $target -PassThru
    Start-Sleep -Seconds 20
    $process.Refresh()
    if ($process.HasExited) {
        Write-Host "app exited: $($process.ExitCode)"
    } else {
        Write-Host "app running: PID $($process.Id)"
    }
    Write-Host '=== listening 8765 ==='
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize | Out-Host
    Write-Host '=== driver start errors ==='
    $err = Join-Path $env:APPDATA 'RemonteVoice\driver-start-error.log'
    if (Test-Path -LiteralPath $err) { Get-Content -LiteralPath $err -Tail 12 | Out-Host } else { Write-Host '  (none)' }
} catch {
    Write-Host "FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
