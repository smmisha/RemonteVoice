# Elevated: place the built pair into a protected Program Files location and start the broker.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-protected-install.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $source = 'M:\Projectorium\RemonteVoice\host\build\desktop-054\dist\RemonteVoicePair'
    $target = 'C:\Program Files\RemonteVoice-Dev'
    if (-not (Test-Path -LiteralPath $source)) { throw "Source missing: $source" }

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
    if ($busy) { throw 'Installed executables are still locked by a running process' }
    Start-Sleep -Milliseconds 500

    if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
    Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force

    Write-Host '=== owner before ==='
    (Get-Acl -LiteralPath $target).Owner

    & icacls $target /setowner 'BUILTIN\Administrators' /T /C | Out-Host
    & icacls $target /grant 'BUILTIN\Administrators:(OI)(CI)F' 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Users:(OI)(CI)RX' | Out-Host

    Write-Host '=== owner after ==='
    (Get-Acl -LiteralPath $target).Owner
    (Get-Acl -LiteralPath (Join-Path $target 'RemonteVoiceBroker.exe')).Owner

    Write-Host '=== start broker from protected location ==='
    $broker = Join-Path $target 'RemonteVoiceBroker.exe'
    $process = Start-Process -FilePath $broker -WorkingDirectory $target -PassThru
    Start-Sleep -Seconds 8
    $process.Refresh()
    if ($process.HasExited) {
        Write-Host "broker exited: $($process.ExitCode)"
    } else {
        Write-Host "broker alive: PID $($process.Id)"
    }

    Write-Host '=== pipes ==='
    [System.IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'remonte|rv' } | ForEach-Object { Write-Host "  $_" }

    Write-Host '=== broker error log ==='
    $errorLog = Join-Path $env:APPDATA 'RemonteVoice\broker-error.log'
    if (Test-Path -LiteralPath $errorLog) { Get-Content -LiteralPath $errorLog -Tail 25 | Out-Host } else { Write-Host '  (none)' }
} catch {
    Write-Host "PROTECTED INSTALL FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
