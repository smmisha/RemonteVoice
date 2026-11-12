# Elevated: register the driver-mode autostart task and record its definition.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'rv-task-create.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $exe = 'C:\Program Files\RemonteVoice-Dev\RemonteVoice.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "Missing executable: $exe" }
    $xml = 'M:\Projectorium\RemonteVoice\host\build\driver-host-package\remontevoice-task.xml'
    $content = Get-Content -LiteralPath $xml -Raw -Encoding UTF8
    [IO.File]::WriteAllText('C:\Windows\Temp\remontevoice-task.xml', $content, [Text.Encoding]::Unicode)
    & schtasks /create /tn 'RemonteVoice' /xml 'C:\Windows\Temp\remontevoice-task.xml' /f | Out-Host
    Write-Host "schtasks exit: $LASTEXITCODE"
    & schtasks /query /tn 'RemonteVoice' /xml | Out-File -FilePath 'M:\Projectorium\RemonteVoice\host\build\remontevoice-task.xml' -Encoding utf8
    Write-Host '=== task query ==='
    & schtasks /query /tn 'RemonteVoice' /fo LIST | Out-Host
} catch {
    Write-Host "TASK CREATE FAILED: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
