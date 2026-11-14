<#
.SYNOPSIS
  Builds a reproducible RemonteVoice setup package: application pair, development
  driver payload, setup scripts, hash manifest and a zip archive.
.PARAMETER Version
  Package version; read from host/remontevoice/deployment.py by default.
.PARAMETER Pair
  Existing RemonteVoicePair directory. When omitted (and -SkipPairBuild is not
  given) a new pair is built with host/build_windows_pair.py.
.PARAMETER DriverInstance
  Driver device instance recorded in the application manifest; detected from the
  installed driver by default, otherwise ROOT\MEDIA\0000.
.PARAMETER SkipPairBuild
  Fail instead of building when -Pair is not given.
.PARAMETER Output
  New package directory; default host/build/setup-<version>.
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Pair = '',
    [string]$DriverInstance = '',
    [string]$DriverPackage = '',
    [string]$Output = '',
    [string]$Archive = '',
    [switch]$SkipPairBuild
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$buildRoot = Join-Path $repo 'host\build'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

function Write-Step([string]$Name, [string]$Detail) {
    Write-Host ('[{0}] {1}' -f $Name, $Detail)
}

function Get-NextTraceDirectory([string]$Root) {
    $highest = 63
    foreach ($directory in @(Get-ChildItem -LiteralPath $Root -Directory -Filter 'desktop-*')) {
        if ($directory.Name -match '^desktop-(\d{3,4})$') { $highest = [Math]::Max($highest, [int]$Matches[1]) }
    }
    return (Join-Path $Root ('desktop-{0:D3}' -f ($highest + 1)))
}

function Read-DeploymentVersion([string]$Path) {
    $match = [regex]::Match((Get-Content -LiteralPath $Path -Raw -Encoding utf8), 'VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
    if (-not $match.Success) { throw "Version not found in $Path" }
    return $match.Groups[1].Value
}

function Get-DriverDeviceInstance {
    $device = Get-RvDriverDevice
    if ($null -eq $device) { return $null }
    return $device.InstanceId
}

try {
    if (-not $Version) { $Version = Read-DeploymentVersion (Join-Path $repo 'host\remontevoice\deployment.py') }
    if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw "Invalid version: $Version" }
    if (-not $DriverInstance) {
        $DriverInstance = Get-DriverDeviceInstance
        if (-not $DriverInstance) {
            $DriverInstance = 'ROOT\MEDIA\0000'
            Write-Warning "No RemonteVoice audio device installed; recording $DriverInstance (rewritten at install time)."
        }
    }
    Write-Step 'version' $Version
    Write-Step 'driver-instance' $DriverInstance

    if (-not $Pair) {
        if ($SkipPairBuild) { throw '-Pair is required with -SkipPairBuild' }
        if (-not (Test-Path -LiteralPath $python)) { throw "Project interpreter not found: $python" }
        $trace = Get-NextTraceDirectory $buildRoot
        Write-Step 'pair-build' $trace
        & $python (Join-Path $repo 'host\build_windows_pair.py') --output $trace --driver-instance $DriverInstance
        if ($LASTEXITCODE -ne 0) { throw "Pair build failed with $LASTEXITCODE" }
        $Pair = Join-Path $trace 'dist\RemonteVoicePair'
    }
    $Pair = [IO.Path]::GetFullPath($Pair)
    if (-not (Test-Path -LiteralPath (Join-Path $Pair 'RemonteVoice.exe'))) { throw "Pair not found: $Pair" }
    $appManifest = Get-Content -LiteralPath (Join-Path $Pair 'remontevoice-installation.json') -Raw -Encoding utf8 | ConvertFrom-Json
    if ($appManifest.driver_instance_id -ne $DriverInstance) {
        throw "Pair is built for $($appManifest.driver_instance_id), requested $DriverInstance"
    }
    $pairBuildReport = Join-Path (Split-Path -Parent $Pair) '..\build-report.json'
    $pairBuildReport = [IO.Path]::GetFullPath((Join-Path $Pair '..\..\build-report.json'))
    if (-not (Test-Path -LiteralPath $pairBuildReport)) { throw "Pair build report missing: $pairBuildReport" }

    if (-not $DriverPackage) { $DriverPackage = Join-Path $buildRoot 'driver-host-package' }
    $DriverPackage = [IO.Path]::GetFullPath($DriverPackage)
    $driverManifestPath = Join-Path $DriverPackage 'package-manifest.json'
    if (-not (Test-Path -LiteralPath $driverManifestPath)) { throw "Driver package manifest missing: $DriverPackage" }
    $driverManifest = Get-Content -LiteralPath $driverManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    foreach ($name in 'RemonteVoiceAudio.sys', 'RemonteVoiceAudio.inf', 'RemonteVoiceAudio.cat', 'RemonteVoiceDevelopment.cer') {
        $path = Join-Path $DriverPackage $name
        if (-not (Test-Path -LiteralPath $path)) { throw "Driver file missing: $path" }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($driverManifest.files.$name -ne $actual) { throw "Driver file differs from its manifest: $name" }
    }
    $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2(
        (Join-Path $DriverPackage 'RemonteVoiceDevelopment.cer'))
    Write-Step 'driver' ("certificate={0} thumbprint={1}" -f $certificate.Subject, $certificate.Thumbprint)

    if (-not $Output) { $Output = Join-Path $buildRoot ('setup-' + $Version) }
    $Output = [IO.Path]::GetFullPath($Output)
    if (Test-Path -LiteralPath $Output) { throw "Output directory already exists: $Output" }
    if (-not $Archive) { $Archive = Join-Path $buildRoot ('RemonteVoice-Setup-' + $Version + '.zip') }
    $zip = [IO.Path]::GetFullPath($Archive)
    if (Test-Path -LiteralPath $zip) { throw "Package archive already exists: $zip" }

    New-Item -ItemType Directory -Path $Output -Force | Out-Null
    foreach ($directory in 'app', 'driver') { New-Item -ItemType Directory -Path (Join-Path $Output $directory) -Force | Out-Null }

    Write-Step 'copy-app' $Pair
    Copy-Item -Path (Join-Path $Pair '*') -Destination (Join-Path $Output 'app') -Recurse -Force
    Copy-Item -Path (Join-Path $DriverPackage 'RemonteVoiceAudio.sys'), (Join-Path $DriverPackage 'RemonteVoiceAudio.inf'),
        (Join-Path $DriverPackage 'RemonteVoiceAudio.cat'), (Join-Path $DriverPackage 'RemonteVoiceDevelopment.cer'),
        (Join-Path $DriverPackage 'package-manifest.json') -Destination (Join-Path $Output 'driver') -Force

    if (-not (Test-Path -LiteralPath $csc)) { throw "Windows .NET Framework compiler not found: $csc" }
    Write-Step 'compile-driver-helper'
    $helperOutput = Join-Path $Output 'driver\RemonteVoiceDriverSetup.exe'
    & $csc /nologo /target:exe /platform:x64 /optimize+ /warn:4 /warnaserror+ /codepage:65001 `
        ('/out:' + $helperOutput) (Join-Path $PSScriptRoot 'HostDriverSetup.cs') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Driver helper compilation failed' }

    foreach ($script in 'common.ps1', 'Install-RemonteVoice.ps1', 'Update-RemonteVoice.ps1',
            'Uninstall-RemonteVoice.ps1') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $script) -Destination $Output -Force
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'remontevoice-task.xml.in') -Destination $Output -Force

    $installCmd = @'
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-RemonteVoice.ps1" -Package "%~dp0" -StartApp
if not errorlevel 1 exit /b 0
echo.
echo Installation failed. See %ProgramData%\RemonteVoice\logs\install.log
pause
exit /b 1
'@
    $uninstallCmd = @'
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-RemonteVoice.ps1"
if not errorlevel 1 exit /b 0
echo.
echo Removal failed. See %ProgramData%\RemonteVoice\logs\uninstall.log
pause
exit /b 1
'@
    [IO.File]::WriteAllText((Join-Path $Output 'install.cmd'), $installCmd, (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText((Join-Path $Output 'uninstall.cmd'), $uninstallCmd, (New-Object Text.UTF8Encoding($false)))

    $readme = @"
RemonteVoice $Version — установка для Windows 10/11 x64

Что делает установщик:
  * копирует приложение в $env:ProgramFiles\RemonteVoice и запрещает запись обычному пользователю;
  * регистрирует задачу входа "RemonteVoice": привилегированный broker поднимается вместе с сеансом,
    без консольного окна;
  * создаёт ярлыки на рабочем столе и в меню "Пуск";
  * добавляет запись "RemonteVoice" в "Программы и компоненты";
  * запускает приложение с сохранённой конфигурацией (--autostart).

Требования:
  * Windows 10/11 x64, права администратора (UAC);
  * для собственного аудиодрайвера: тестовая подпись (testsigning), Secure Boot выключен;
  * драйвер уже установлен на этой машине — установщик находит устройство и использует его instance ID.

Установка:
  распакуйте архив и запустите install.cmd (UAC), либо:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-RemonteVoice.ps1 -Package . -StartApp

Драйвер (только при необходимости, dev-режим):
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-RemonteVoice.ps1 -Package . -InstallDriver
  Требуется перезагрузка. Это тестовая подпись, не production-подпись.

Обновление:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Update-RemonteVoice.ps1 -Package <пакет.zip>

Удаление:
  uninstall.cmd или "Программы и компоненты"; настройки в %APPDATA%\RemonteVoice сохраняются
  (-RemoveConfig удаляет и их; -RemoveDriver удаляет драйвер и отключает testsigning).

Проверка целостности:
  установщик сверяет SHA-256 каждого файла с package-manifest.json и отказывается ставить
  повреждённый пакет. Подписи кода у пакета нет: это development-сборка.

Журналы и квитанции: %ProgramData%\RemonteVoice\logs и %ProgramData%\RemonteVoice\receipts
"@
    [IO.File]::WriteAllText((Join-Path $Output 'README.txt'), $readme, (New-Object Text.UTF8Encoding($true)))

    Write-Step 'package-manifest'
    $files = [ordered]@{}
    Get-ChildItem -LiteralPath $Output -Recurse -File -Force |
        Sort-Object FullName | ForEach-Object {
            $relative = $_.FullName.Substring($Output.Length + 1) -replace '\\', '/'
            if ($relative -eq 'package-manifest.json') { return }
            $files[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    $packageManifest = [ordered]@{
        schema_version = 1
        product = $script:RvProductName
        version = $Version
        created_utc = [DateTime]::UtcNow.ToString('o')
        driver_instance_id = $DriverInstance
        driver = [ordered]@{
            certificate_subject = $certificate.Subject
            certificate_thumbprint = $certificate.Thumbprint
            development_signing = $true
        }
        entry = [ordered]@{
            install = 'Install-RemonteVoice.ps1'
            update = 'Update-RemonteVoice.ps1'
            uninstall = 'Uninstall-RemonteVoice.ps1'
            task_template = 'remontevoice-task.xml.in'
        }
        pair_build_report_sha256 = (Get-FileHash -LiteralPath $pairBuildReport -Algorithm SHA256).Hash.ToLowerInvariant()
        files = $files
    }
    $packageManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'package-manifest.json') -Encoding utf8

    Write-Step 'archive' $zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($Output, $zip,
        [IO.Compression.CompressionLevel]::Optimal, $true)

    $archiveHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $receipt = [ordered]@{
        operation = 'build-setup'
        version = $Version
        driver_instance_id = $DriverInstance
        package_directory = $Output
        archive = $zip
        archive_sha256 = $archiveHash
        archive_bytes = (Get-Item -LiteralPath $zip).Length
        files = $files.Count
        pair = $Pair
        pair_build_report = $pairBuildReport
        driver_package = $DriverPackage
        driver_certificate_thumbprint = $certificate.Thumbprint
        created_utc = [DateTime]::UtcNow.ToString('o')
    }
    $receiptPath = $zip + '.build.json'
    $receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $receiptPath -Encoding utf8
    Write-Step 'receipt' $receiptPath
    Write-Host ('RESULT: ok version={0} archive={1} sha256={2} files={3}' -f
        $Version, $zip, $archiveHash, $files.Count)
    exit 0
}
catch {
    Write-Host ('RESULT: failed - {0}' -f $_.Exception.Message)
    exit 1
}
