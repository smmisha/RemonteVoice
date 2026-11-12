# Elevated: remove the retired per-user install and all Voicemeeter leftovers.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-remove-leftovers.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    Get-Process -Name voicemeeter,voicemeeterpro,voicemeeter8x64 -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1

    $userApp = Join-Path $env:LOCALAPPDATA 'RemonteVoice\app'
    if (Test-Path -LiteralPath $userApp) {
        Remove-Item -LiteralPath $userApp -Recurse -Force
        Write-Host "removed per-user app: $userApp"
    }

    $uninstallKeys = @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\VB:Voicemeeter {17359A74-1236-5467}'
    )
    foreach ($key in $uninstallKeys) {
        if (Test-Path -LiteralPath $key) {
            Remove-Item -LiteralPath $key -Recurse -Force
            Write-Host "removed registry key: $key"
        }
    }

    foreach ($folder in 'C:\Program Files\VB', 'C:\Program Files (x86)\VB') {
        if (Test-Path -LiteralPath $folder) {
            Remove-Item -LiteralPath $folder -Recurse -Force
            Write-Host "removed folder: $folder"
        }
    }

    $startMenu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
    Get-ChildItem -LiteralPath $startMenu -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '*Voicemeeter*' } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force; Write-Host "removed start-menu entry: $($_.FullName)" }

    Write-Host '=== remaining Voicemeeter devices ==='
    $devices = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -like '*Voicemeeter*' }
    if ($devices) { $devices | Select-Object Status, FriendlyName | Format-Table -AutoSize | Out-Host } else { Write-Host '  (none)' }

    Write-Host '=== remaining driver packages ==='
    $drivers = pnputil /enum-drivers 2>&1
    $blocks = ($drivers -join "`n") -split "`r?`n`r?`n"
    $found = $false
    foreach ($block in $blocks) { if ($block -match 'vbvoicemeeter') { $found = $true; Write-Host $block } }
    if (-not $found) { Write-Host '  (none)' }

    Write-Host '=== app still running ==='
    Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue | Select-Object Id, StartTime | Format-Table -AutoSize | Out-Host
} catch {
    Write-Host "LEFTOVER REMOVAL FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
