<#
.SYNOPSIS
  Обновляет установленный RemonteVoice новым пакетом, сохраняя настройки.
.PARAMETER Package
  Папка с новой версией либо zip-пакет (например, RemonteVoice-Windows-0.1.38-local.zip).
.PARAMETER InstallRoot
  Папка установки; по умолчанию %LOCALAPPDATA%\RemoteVoice\app.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RemoteVoice\app')
)
$ErrorActionPreference = 'Stop'

function Resolve-Package([string]$candidate) {
    if (-not (Test-Path -LiteralPath $candidate)) { throw "Пакет не найден: $candidate" }
    $item = Get-Item -LiteralPath $candidate
    if ($item.PSIsContainer) { $source = $item.FullName }
    elseif ($item.Extension -eq '.zip') {
        $temp = Join-Path ([IO.Path]::GetTempPath()) ('remontevoice-update-' + [guid]::NewGuid().ToString('N'))
        Expand-Archive -LiteralPath $item.FullName -DestinationPath $temp -Force
        $exe = Get-ChildItem -LiteralPath $temp -Recurse -Filter 'RemoteVoice.exe' | Select-Object -First 1
        if (-not $exe) { throw "В пакете нет RemoteVoice.exe: $candidate" }
        $source = $exe.Directory.FullName
    }
    else { throw 'Ожидалась папка с приложением или zip-пакет' }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'RemoteVoice.exe'))) {
        throw "В папке нет RemoteVoice.exe: $source"
    }
    return $source
}

$source = Resolve-Package $Package
$wasRunning = [bool](Get-Process -Name RemoteVoice -ErrorAction SilentlyContinue)
Get-Process -Name RemoteVoice -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 700

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $InstallRoot -Recurse -Force
$uninstaller = Join-Path $PSScriptRoot 'Uninstall-RemoteVoice.ps1'
if (Test-Path -LiteralPath $uninstaller) {
    Copy-Item -LiteralPath $uninstaller -Destination (Join-Path $InstallRoot 'Uninstall-RemoteVoice.ps1') -Force
}
Write-Host "RemonteVoice обновлён: $InstallRoot"
if ($wasRunning) {
    Start-Process -FilePath (Join-Path $InstallRoot 'RemoteVoice.exe') -WorkingDirectory $InstallRoot
    Write-Host 'Приложение запущено заново.'
}
