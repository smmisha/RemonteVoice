# Elevated: disable unused Voicemeeter capture buses, keeping B1 and B2.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-disable-buses.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $ids = @(
        'SWD\MMDEVAPI\{0.0.1.00000000}.{9D14A5A0-222A-478E-AAA9-AC8603DE071F}',  # Out A1
        'SWD\MMDEVAPI\{0.0.1.00000000}.{5FCDD824-9218-4486-B1A4-60A1BA089545}',  # Out A2
        'SWD\MMDEVAPI\{0.0.1.00000000}.{C3039A19-D0ED-4225-A694-FB8FC675075E}',  # Out A3
        'SWD\MMDEVAPI\{0.0.1.00000000}.{1614D3D4-09B8-4988-ACD6-4674460505F0}',  # Out A4
        'SWD\MMDEVAPI\{0.0.1.00000000}.{4F9CCDB2-F803-4861-A34E-49DEF3A7E493}',  # Out A5
        'SWD\MMDEVAPI\{0.0.1.00000000}.{F76C37A0-2C48-4C40-90C0-43BDC3E7A609}'   # Out B3
    )
    foreach ($id in $ids) {
        try {
            Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
            Write-Host "disabled: $id"
        } catch {
            Write-Host "FAILED: $id -> $($_.Exception.Message)"
        }
    }
    Write-Host '=== remaining capture endpoints ==='
    Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like '*0.0.1*' } |
        Select-Object Status, FriendlyName | Format-Table -AutoSize | Out-Host
} catch {
    Write-Host "DISABLE TASK FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
