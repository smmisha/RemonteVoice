<#
.SYNOPSIS
  Устанавливает RemonteVoice в профиль пользователя (без прав администратора).
.PARAMETER Package
  Папка с RemonteVoice.exe и _internal либо zip-пакет.
.PARAMETER InstallRoot
  Куда ставить; по умолчанию %LOCALAPPDATA%\RemonteVoice\app.
.PARAMETER NoShortcuts
  Не создавать ярлыки на рабочем столе и в меню «Пуск».
.PARAMETER NoRegistry
  Не писать запись об удалении в реестр.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RemonteVoice\app'),
    [switch]$NoShortcuts,
    [switch]$NoRegistry
)
$ErrorActionPreference = 'Stop'

function Resolve-Package([string]$candidate) {
    if (-not (Test-Path -LiteralPath $candidate)) { throw "Пакет не найден: $candidate" }
    $item = Get-Item -LiteralPath $candidate
    if ($item.PSIsContainer) { $source = $item.FullName }
    elseif ($item.Extension -eq '.zip') {
        $temp = Join-Path ([IO.Path]::GetTempPath()) ('remontevoice-install-' + [guid]::NewGuid().ToString('N'))
        Expand-Archive -LiteralPath $item.FullName -DestinationPath $temp -Force
        $exe = Get-ChildItem -LiteralPath $temp -Recurse -Filter 'RemonteVoice.exe' | Select-Object -First 1
        if (-not $exe) { throw "В пакете нет RemonteVoice.exe: $candidate" }
        $source = $exe.Directory.FullName
    }
    else { throw 'Ожидалась папка с приложением или zip-пакет' }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'RemonteVoice.exe'))) {
        throw "В папке нет RemonteVoice.exe: $source"
    }
    return $source
}

$source = Resolve-Package $Package
$running = Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 700
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $InstallRoot -Recurse -Force
$exe = Join-Path $InstallRoot 'RemonteVoice.exe'

if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $links = @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'RemonteVoice.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Programs')) 'RemonteVoice.lnk')
    )
    foreach ($linkPath in $links) {
        $link = $shell.CreateShortcut($linkPath)
        $link.TargetPath = $exe
        $link.WorkingDirectory = $InstallRoot
        $link.IconLocation = "$exe,0"
        $link.Save()
    }
}

if (-not $NoRegistry) {
    $uninstaller = Join-Path $InstallRoot 'Uninstall-RemonteVoice.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Uninstall-RemonteVoice.ps1') -Destination $uninstaller -Force
    $version = '0.0.0'
    $installation = Join-Path $InstallRoot 'remontevoice-installation.json'
    if (Test-Path -LiteralPath $installation) {
        try { $version = (Get-Content -LiteralPath $installation -Raw -Encoding utf8 | ConvertFrom-Json).version } catch { }
    }
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice'
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty -Path $key -Name DisplayName -Value 'RemonteVoice'
    Set-ItemProperty -Path $key -Name DisplayVersion -Value $version
    Set-ItemProperty -Path $key -Name InstallLocation -Value $InstallRoot
    Set-ItemProperty -Path $key -Name DisplayIcon -Value "$exe,0"
    Set-ItemProperty -Path $key -Name UninstallString -Value (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f $uninstaller, $InstallRoot)
    Set-ItemProperty -Path $key -Name NoModify -Value 1
    Set-ItemProperty -Path $key -Name NoRepair -Value 1
}

Write-Host "RemonteVoice установлен: $exe"
if (-not $NoShortcuts) { Write-Host 'Ярлыки: рабочий стол и меню «Пуск».' }
if (-not $NoRegistry) { Write-Host 'Удаление: «Программы и компоненты» → RemonteVoice.' }
