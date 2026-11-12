# Elevated: remove the Voicemeeter VAIO audio device and its driver package.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-remove-voicemeeter.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    Get-Process -Name voicemeeter,voicemeeterpro,voicemeeter8x64 -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1

    $device = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -eq 'ROOT\MEDIA\0000' }
    if ($device) {
        Write-Host "removing device: $($device.InstanceId) ($($device.FriendlyName))"
        & pnputil /remove-device 'ROOT\MEDIA\0000' | Out-Host
        Write-Host "pnputil remove-device exit: $LASTEXITCODE"
    } else {
        Write-Host 'Voicemeeter device ROOT\MEDIA\0000 not present'
    }

    Write-Host 'deleting driver package oem1.inf'
    & pnputil /delete-driver 'oem1.inf' /uninstall /force | Out-Host
    Write-Host "pnputil delete-driver exit: $LASTEXITCODE"

    Start-Sleep -Seconds 2
    Write-Host '=== remaining Voicemeeter devices ==='
    $left = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -like '*Voicemeeter*' }
    if ($left) { $left | Select-Object Status, Class, FriendlyName, InstanceId | Format-Table -AutoSize | Out-Host } else { Write-Host '  (none)' }

    Write-Host '=== remaining driver packages ==='
    $drivers = pnputil /enum-drivers 2>&1
    $blocks = ($drivers -join "`n") -split "`r?`n`r?`n"
    $found = $false
    foreach ($block in $blocks) {
        if ($block -match 'vbvoicemeeter') { $found = $true; Write-Host $block }
    }
    if (-not $found) { Write-Host '  (none)' }
} catch {
    Write-Host "REMOVAL FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
