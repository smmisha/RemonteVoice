# After reboot: confirm the development driver loaded and exposed a capture device.
$ErrorActionPreference = 'Continue'

Write-Host '=== Устройства PnP с именем RemonteVoice ==='
$devices = Get-PnpDevice -Class MEDIA -ErrorAction SilentlyContinue |
    Where-Object { $_.FriendlyName -like '*RemonteVoice*' -or $_.InstanceId -like '*RemonteVoice*' }
if ($devices) {
    $devices | Select-Object Status, Class, FriendlyName, InstanceId | Format-List
} else {
    Write-Host 'Устройства не найдено.'
}

Write-Host '=== Все устройства с HWID ROOT\\RemonteVoiceAudio ==='
Get-PnpDevice -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -like '*RemonteVoice*' } |
    Select-Object Status, Present, Problem, Class, FriendlyName, InstanceId | Format-List

Write-Host '=== Служба/драйвер в системе ==='
Get-CimInstance Win32_SystemDriver -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '*Remonte*' } |
    Select-Object Name, State, Started, PathName | Format-List

Write-Host '=== Устройства записи (soundcard) ==='
$env:PYTHONPATH = 'M:\Projectorium\RemonteVoice\host'
& 'M:\Projectorium\RemonteVoice\.venv\Scripts\python.exe' -c "import soundcard as sc; [print(' ', d.name, '|', d.id) for d in sc.all_microphones(include_loopback=False)]" 2>&1

Write-Host '=== Подпись установленного драйвера ==='
$driverFile = Get-ChildItem 'C:\Windows\System32\drivers' -Filter 'RemonteVoiceAudio.sys' -ErrorAction SilentlyContinue
if ($driverFile) {
    $signature = Get-AuthenticodeSignature -LiteralPath $driverFile.FullName
    Write-Host "  $($driverFile.FullName)"
    Write-Host "  status=$($signature.Status) signer=$($signature.SignerCertificate.Subject)"
} else {
    Write-Host '  RemonteVoiceAudio.sys в System32\drivers не найден.'
}
