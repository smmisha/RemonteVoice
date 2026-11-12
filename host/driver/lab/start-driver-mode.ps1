# Start the installed RemonteVoice in own-driver mode (development backend).
# Requires: driver loaded (after reboot) and the app installed by Install-RemonteVoice.ps1.
param(
    [switch]$Force,
    [int]$WaitSeconds = 20
)
$ErrorActionPreference = 'Stop'

$installRoot = Join-Path $env:LOCALAPPDATA 'RemonteVoice\app'
$exe = Join-Path $installRoot 'RemonteVoice.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Installed app not found: $exe" }

$device = Get-PnpDevice -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -like '*RemonteVoice*' -or $_.FriendlyName -like '*RemonteVoice*' }
if (-not $device) {
    Write-Host 'Driver device is absent. Reboot Windows first, then run this script again.'
    exit 2
}
$device | Select-Object Status, Class, FriendlyName, InstanceId | Format-Table -AutoSize | Out-Host

$running = Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue
if ($running) {
    if (-not $Force) {
        Write-Host "RemonteVoice is already running (PID $($running.Id -join ', ')). Use -Force to restart it in driver mode."
        exit 3
    }
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# Explorer owns the process, so it survives this script and the calling terminal.
& explorer.exe $exe '--driver-backend-lab'
Start-Sleep -Seconds $WaitSeconds

$process = Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue
if ($process) {
    $process | Select-Object Id, StartTime, MainWindowTitle, Responding | Format-Table -AutoSize | Out-Host
} else {
    Write-Host 'Process did not start.'
}

$receipt = Join-Path $env:APPDATA 'RemonteVoice\runtime.json'
if (Test-Path -LiteralPath $receipt) {
    Write-Host 'runtime.json:'
    Get-Content -LiteralPath $receipt -Raw | Out-Host
}

$log = Join-Path $env:APPDATA 'RemonteVoice\host.log'
if (Test-Path -LiteralPath $log) {
    Write-Host 'host.log tail:'
    Get-Content -LiteralPath $log -Tail 15 | Out-Host
}
