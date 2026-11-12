<#
.SYNOPSIS
  Удаляет RemonteVoice, установленный Install-RemoteVoice.ps1.
.PARAMETER InstallRoot
  Папка установки; по умолчанию %LOCALAPPDATA%\RemoteVoice\app.
.PARAMETER RemoveConfig
  Удалить также настройки и сопряжение в %APPDATA%\RemoteVoice.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RemoteVoice\app'),
    [switch]$RemoveConfig
)
$ErrorActionPreference = 'Stop'

Get-Process -Name RemoteVoice -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 700

$links = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'RemonteVoice.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'RemonteVoice.lnk')
)
foreach ($linkPath in $links) {
    Remove-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice' `
    -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}
if ($RemoveConfig) {
    $config = Join-Path $env:APPDATA 'RemoteVoice'
    if (Test-Path -LiteralPath $config) { Remove-Item -LiteralPath $config -Recurse -Force }
    Write-Host 'Настройки и сопряжение удалены.'
} else {
    Write-Host 'Настройки в %APPDATA%\RemoteVoice сохранены (удалить: -RemoveConfig).'
}
Write-Host 'RemonteVoice удалён.'
