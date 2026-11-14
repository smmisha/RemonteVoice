<#
.SYNOPSIS
  Elevated acceptance of a built RemonteVoice setup package:
  install -> update (repair) -> uninstall -> final install, with checks.
.PARAMETER Package
  Extracted package directory or RemonteVoice-Setup-<version>.zip. Defaults to
  the newest archive under host\build.
.PARAMETER EvidenceDir
  Where acceptance.json and the transcript are written.
#>
param(
    [string]$Package = '',
    [string]$EvidenceDir = 'M:\Projectorium\RemonteVoice\host\build\package-acceptance'
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
if (-not $Package) {
    $archives = @(Get-ChildItem -LiteralPath (Join-Path $repo 'host\build') -Filter 'RemonteVoice-Setup-*.zip' |
        Sort-Object LastWriteTime -Descending)
    if ($archives.Count -eq 0) { throw 'No RemonteVoice-Setup-*.zip archive found under host\build' }
    $Package = $archives[0].FullName
}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'),
        '-Package', ('"' + $Package + '"'), '-EvidenceDir', ('"' + $EvidenceDir + '"'))
    $null = $process.WaitForExit()
    exit $process.ExitCode
}
New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null
Start-Transcript -Path (Join-Path $EvidenceDir 'acceptance.log') -Force | Out-Null

$extracted = $null
$evidence = [ordered]@{
    started_utc = [DateTime]::UtcNow.ToString('o')
    package = $Package
    checks = @()
    steps = @()
    ok = $false
}

function Record-Check([string]$Name, [bool]$Ok, $Detail) {
    $script:evidence.checks += [ordered]@{
        name = $Name; ok = $Ok; detail = $Detail; at_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-Host ('[{0}] {1}: {2}' -f $(if ($Ok) { 'ok' } else { 'FAIL' }), $Name, ($Detail | Out-String).Trim())
    if (-not $Ok) { throw "Acceptance check failed: $Name" }
}

function Record-Step([string]$Name, [string]$Detail) {
    $script:evidence.steps += [ordered]@{ name = $Name; detail = $Detail; at_utc = [DateTime]::UtcNow.ToString('o') }
    Write-Host ('[step] {0}: {1}' -f $Name, $Detail)
}

function Resolve-PackageRoot([string]$Candidate) {
    if (-not (Test-Path -LiteralPath $Candidate)) { throw "Package not found: $Candidate" }
    $item = Get-Item -LiteralPath $Candidate
    if ($item.PSIsContainer) {
        if (-not (Test-Path (Join-Path $item.FullName 'package-manifest.json'))) {
            throw "Not a package directory: $($item.FullName)"
        }
        return [ordered]@{ Root = $item.FullName; Temporary = $null }
    }
    if ($item.Extension -ne '.zip') { throw 'Expected a package directory or .zip archive' }
    $temporary = Join-Path $env:TEMP ('rv-accept-' + [guid]::NewGuid().ToString('N'))
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($item.FullName, $temporary)
    $root = $temporary
    if (-not (Test-Path (Join-Path $root 'package-manifest.json'))) {
        $nested = @(Get-ChildItem $temporary -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName 'package-manifest.json') })
        if ($nested.Count -ne 1) { throw 'Package manifest not found in the archive' }
        $root = $nested[0].FullName
    }
    return [ordered]@{ Root = $root; Temporary = $temporary }
}

function Invoke-SetupScript([string]$Root, [string]$Name, [string[]]$Arguments) {
    $script = Join-Path $Root $Name
    $process = Start-Process -FilePath 'powershell.exe' -PassThru -Wait -ArgumentList (
        @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script + '"')) + $Arguments)
    if ($process.ExitCode -ne 0) { throw "$Name failed with exit code $($process.ExitCode)" }
}

function Get-ProductState {
    $root = 'C:\Program Files\RemonteVoice'
    $broker = Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue
    $manifest = if (Test-Path (Join-Path $root 'remontevoice-installation.json')) {
        (Get-Content (Join-Path $root 'remontevoice-installation.json') -Raw | ConvertFrom-Json).driver_instance_id
    } else { $null }
    $taskCommand = $null
    if (Test-Path 'C:\Windows\System32\Tasks\RemonteVoice') {
        $taskCommand = [string](([xml](Get-Content 'C:\Windows\System32\Tasks\RemonteVoice' -Raw)).Task.Actions.Exec.Command)
    }
    $stateVersion = $null
    $statePath = 'C:\ProgramData\RemonteVoice\install-state.json'
    if (Test-Path $statePath) { $stateVersion = (Get-Content $statePath -Raw | ConvertFrom-Json).version }
    $uninstallVersion = (Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice' -ErrorAction SilentlyContinue).DisplayVersion
    return [ordered]@{
        files = @(Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue).Count
        manifest_instance = $manifest
        owner = if (Test-Path $root) { (Get-Acl -LiteralPath $root).Owner } else { $null }
        owner_sid = if (Test-Path $root) {
            try {
                ([Security.Principal.NTAccount]((Get-Acl -LiteralPath $root).Owner)).Translate(
                    [Security.Principal.SecurityIdentifier]).Value
            } catch { $null }
        } else { $null }
        task_command = $taskCommand
        broker_pid = if ($broker) { $broker.Id } else { $null }
        broker_window = if ($broker) { $broker.MainWindowTitle } else { $null }
        broker_visible_window = if ($broker) { $broker.MainWindowHandle } else { $null }
        pipes = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice-Mic-' })
        shortcuts = @(Get-ChildItem ([Environment]::GetFolderPath('Desktop')) -Filter 'RemonteVoice.lnk' -ErrorAction SilentlyContinue).Count +
            @(Get-ChildItem ([Environment]::GetFolderPath('Programs')) -Filter 'RemonteVoice.lnk' -ErrorAction SilentlyContinue).Count
        uninstall_version = $uninstallVersion
        state_version = $stateVersion
        app_running = [bool](Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
        dev_install = Test-Path 'C:\Program Files\RemonteVoice-Dev'
        config_preserved = Test-Path (Join-Path $env:APPDATA 'RemonteVoice')
    }
}

try {
    $resolved = Resolve-PackageRoot $Package
    $extracted = $resolved.Temporary
    $packageRoot = $resolved.Root
    $manifest = Get-Content (Join-Path $packageRoot 'package-manifest.json') -Raw | ConvertFrom-Json
    $evidence.package_manifest_sha256 = (Get-FileHash (Join-Path $packageRoot 'package-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    Record-Step 'package' ("root={0} version={1}" -f $packageRoot, $manifest.version)

    # 1. Fresh product install over the previous (development) deployment.
    Invoke-SetupScript $packageRoot 'Install-RemonteVoice.ps1' @('-Package', ('"' + $packageRoot + '"'), '-StartApp')
    Record-Step 'install' 'Install-RemonteVoice.ps1'
    $state = Get-ProductState
    $evidence.install = $state
    Record-Check 'install.files' ($state.files -eq 1211) $state.files
    Record-Check 'install.manifest-instance' ($state.manifest_instance -eq 'ROOT\MEDIA\0001') $state.manifest_instance
    Record-Check 'install.owner-trusted' ($state.owner_sid -in @('S-1-5-32-544', 'S-1-5-18')) $state.owner_sid
    Record-Check 'install.task' ($state.task_command -eq 'C:\Program Files\RemonteVoice\RemonteVoiceBroker.exe') $state.task_command
    Record-Check 'install.broker' ($null -ne $state.broker_pid -and $state.pipes.Count -eq 1) ("pid=$($state.broker_pid) pipes=$($state.pipes.Count)")
    Record-Check 'install.broker-windowless' ($state.broker_visible_window -eq 0) $state.broker_visible_window
    Record-Check 'install.shortcuts' ($state.shortcuts -eq 2) $state.shortcuts
    Record-Check 'install.uninstall-entry' ($state.uninstall_version -eq $manifest.version) $state.uninstall_version
    Record-Check 'install.state' ($state.state_version -eq $manifest.version) $state.state_version
    Record-Check 'install.config-preserved' $state.config_preserved $state.config_preserved
    $firstBroker = $state.broker_pid

    # 2. Update/repair in place.
    Invoke-SetupScript $packageRoot 'Update-RemonteVoice.ps1' @('-Package', ('"' + $packageRoot + '"'), '-AllowSameVersion', '-SkipIntegrityCheck')
    Record-Step 'update' 'Update-RemonteVoice.ps1 -AllowSameVersion'
    $updated = Get-ProductState
    $evidence.update = $updated
    Record-Check 'update.files' ($updated.files -eq 1211) $updated.files
    Record-Check 'update.broker-restarted' ($updated.broker_pid -ne $firstBroker -and $updated.pipes.Count -eq 1) ("old=$firstBroker new=$($updated.broker_pid)")
    Record-Check 'update.state' ($updated.state_version -eq $manifest.version) $updated.state_version

    # 3. Uninstall keeps settings and the development deployment.
    Invoke-SetupScript $packageRoot 'Uninstall-RemonteVoice.ps1' @('-KeepLogs')
    Record-Step 'uninstall' 'Uninstall-RemonteVoice.ps1 -KeepLogs'
    $removed = Get-ProductState
    $evidence.uninstall = $removed
    Record-Check 'uninstall.directory' ($removed.files -eq 0) $removed.files
    Record-Check 'uninstall.task' ($null -eq $removed.task_command) $removed.task_command
    Record-Check 'uninstall.shortcuts' ($removed.shortcuts -eq 0) $removed.shortcuts
    Record-Check 'uninstall.registry' ($null -eq $removed.uninstall_version) $removed.uninstall_version
    Record-Check 'uninstall.broker-stopped' ($null -eq $removed.broker_pid -and $removed.pipes.Count -eq 0) ("pid=$($removed.broker_pid) pipes=$($removed.pipes.Count)")
    Record-Check 'uninstall.config-preserved' $removed.config_preserved $removed.config_preserved
    Record-Check 'uninstall.dev-install-untouched' $removed.dev_install $removed.dev_install

    # 4. Final product installation: the delivered state.
    Invoke-SetupScript $packageRoot 'Install-RemonteVoice.ps1' @('-Package', ('"' + $packageRoot + '"'), '-StartApp')
    Record-Step 'reinstall' 'Install-RemonteVoice.ps1'
    $final = Get-ProductState
    $evidence.final = $final
    Record-Check 'final.files' ($final.files -eq 1211) $final.files
    Record-Check 'final.task' ($final.task_command -eq 'C:\Program Files\RemonteVoice\RemonteVoiceBroker.exe') $final.task_command
    Record-Check 'final.broker' ($null -ne $final.broker_pid -and $final.pipes.Count -eq 1 -and $final.broker_visible_window -eq 0) ("pid=$($final.broker_pid) pipes=$($final.pipes.Count)")
    Record-Check 'final.uninstall-entry' ($final.uninstall_version -eq $manifest.version) $final.uninstall_version
    $evidence.ok = $true
    $evidence.finished_utc = [DateTime]::UtcNow.ToString('o')
    ($evidence | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $EvidenceDir 'acceptance.json') -Encoding utf8
    Write-Host 'RESULT: ok'
    $exitCode = 0
}
catch {
    $evidence.errors = @($_.Exception.Message)
    $evidence.finished_utc = [DateTime]::UtcNow.ToString('o')
    ($evidence | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $EvidenceDir 'acceptance.json') -Encoding utf8
    Write-Host ('RESULT: failed - {0}' -f $_.Exception.Message)
    $exitCode = 1
}
finally {
    Stop-Transcript | Out-Null
    if ($null -ne $extracted -and (Test-Path -LiteralPath $extracted)) {
        Remove-Item -LiteralPath $extracted -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit $exitCode
