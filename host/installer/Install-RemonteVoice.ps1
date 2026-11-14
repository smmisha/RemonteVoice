<#
.SYNOPSIS
  Installs RemonteVoice as a machine-wide product: protected Program Files copy,
  privileged broker logon task, Start menu and desktop shortcuts and the
  Programs and Features entry. Optionally installs the development audio driver.
.PARAMETER Package
  Extracted package directory or RemonteVoice-Setup-<version>.zip. Defaults to
  the directory this script was started from.
.PARAMETER InstallRoot
  Installation directory; default %ProgramFiles%\RemonteVoice.
.PARAMETER DriverInstance
  Exact ROOT\MEDIA\NNNN device instance; detected from the installed driver by default.
.PARAMETER InstallDriver
  Development-mode driver installation (test signing, trusted development
  certificate, root device). Requires Secure Boot off and a reboot.
.PARAMETER StartApp
  Start the application after a successful installation.
.PARAMETER NoShortcuts / NoTask
  Skip shortcuts or the broker logon task (diagnostics and tests).
.PARAMETER SkipIntegrityCheck
  Skip package hash verification (diagnostics only; never for a real install).
#>
[CmdletBinding()]
param(
    [string]$Package = '',
    [string]$InstallRoot = '',
    [string]$DriverInstance = '',
    [switch]$InstallDriver,
    [switch]$StartApp,
    [switch]$NoShortcuts,
    [switch]$NoTask,
    [switch]$SkipIntegrityCheck
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not (Test-RvAdministrator)) {
    $forward = @()
    foreach ($key in $PSBoundParameters.Keys) {
        $value = $PSBoundParameters[$key]
        if ($value -is [switch]) {
            if ($value.IsPresent) { $forward += ('-' + $key) }
        } else {
            $forward += @(('-' + $key), ('"' + [string]$value + '"'))
        }
    }
    $code = Enter-RvElevatedSelf -ScriptPath $PSCommandPath -ScriptArguments $forward
    exit $code
}

$logDirectory = Get-RvLogDirectory
Start-Transcript -Path (Join-Path $logDirectory 'install.log') -Force | Out-Null
$receipt = [ordered]@{
    operation = 'install'
    started_utc = [DateTime]::UtcNow.ToString('o')
    ok = $false
    steps = @()
    errors = @()
}
function Add-Step([string]$Name, [string]$Detail) {
    $script:receipt.steps += [ordered]@{ name = $Name; detail = $Detail; at_utc = [DateTime]::UtcNow.ToString('o') }
    Write-Host ('[{0}] {1}' -f $Name, $Detail)
}

$resolved = $null
try {
    if (-not $Package) { $Package = $PSScriptRoot }
    $resolved = Resolve-RvPackage $Package
    $manifest = Read-RvPackageManifest $resolved.Root
    Add-Step 'package' ("source={0} version={1}" -f $resolved.Root, $manifest.version)
    if ($SkipIntegrityCheck) {
        Add-Step 'integrity' 'skipped by request'
    } else {
        $count = Test-RvPackageIntegrity $manifest $resolved.Root
        Add-Step 'integrity' ("verified {0} files" -f $count)
    }
    $root = Get-RvInstalledAppPath $InstallRoot
    if (-not $root) { throw 'Install root is not defined' }
    if ($root -notmatch '^[A-Za-z]:\\') { throw "Install root must be an absolute local path: $root" }
    Add-Step 'target' $root

    $previous = Read-RvState
    Stop-RvProcesses
    Add-Step 'stopped' 'previous application and broker stopped'

    $driverDevice = Get-RvDriverDevice
    $rebootRequired = $false
    if ($InstallDriver) {
        $driver = Install-RvDriver (Join-Path $resolved.Root 'driver') `
            (Join-Path $resolved.Root 'driver\RemonteVoiceDriverSetup.exe')
        $rebootRequired = [bool]$driver.reboot_required
        $driverDevice = Get-RvDriverDevice
        Add-Step 'driver' ("installed; reboot_required={0}" -f $rebootRequired)
    }
    $instance = $DriverInstance
    if (-not $instance -and $driverDevice) { $instance = $driverDevice.InstanceId }
    if (-not $instance) {
        $instance = [string]$manifest.driver_instance_id
        Write-Warning 'No RemonteVoice audio device is installed; the microphone path will not work until it is.'
    }
    if ($instance -notmatch '^ROOT\\MEDIA\\[A-Za-z0-9_-]{1,64}$') { throw "Invalid driver instance: $instance" }
    $receipt.driver_instance = $instance

    $copied = Install-RvAppFiles (Join-Path $resolved.Root 'app') $root
    $rewritten = Set-RvManifestDriverInstance (Join-Path $root 'remontevoice-installation.json') $instance
    $owner = Set-RvProtectedAcl $root
    Add-Step 'app-files' ("files={0} removed_stale={1} manifest_rewritten={2} owner={3}" -f
        $copied.files, $copied.removed, $rewritten, $owner)

    $dataInstaller = Join-Path (Get-RvDataDirectory) 'installer'
    New-Item -ItemType Directory -Path $dataInstaller -Force | Out-Null
    foreach ($file in @('common.ps1', 'Install-RemonteVoice.ps1', 'Update-RemonteVoice.ps1',
            'Uninstall-RemonteVoice.ps1', 'remontevoice-task.xml.in')) {
        $source = Join-Path $resolved.Root $file
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $dataInstaller -Force }
    }
    $driverStorage = Join-Path $dataInstaller 'driver'
    if (Test-Path -LiteralPath (Join-Path $resolved.Root 'driver')) {
        New-Item -ItemType Directory -Path $driverStorage -Force | Out-Null
        Copy-Item -Path (Join-Path $resolved.Root 'driver\*') -Destination $driverStorage -Recurse -Force
    }
    Add-Step 'maintenance-scripts' $dataInstaller

    $broker = $null
    if (-not $NoTask) {
        Register-RvBrokerTask (Join-Path $resolved.Root 'remontevoice-task.xml.in') $root
        $broker = Start-RvBrokerTaskAndWait
        Add-Step 'broker' ("pid={0} pipe={1}" -f $broker.pid, $broker.pipe)
    }
    if (-not $NoShortcuts) {
        Remove-RvShortcuts
        New-RvShortcuts $root
        Add-Step 'shortcuts' 'desktop and Start menu'
    }
    Set-RvUninstallEntry $root ([string]$manifest.version) (Join-Path $dataInstaller 'Uninstall-RemonteVoice.ps1')
    Add-Step 'uninstall-entry' 'Programs and Features'

    $state = [ordered]@{
        product = $script:RvProductName
        version = [string]$manifest.version
        install_root = $root
        driver_instance = $instance
        driver_available = [bool]$driverDevice
        package = (Resolve-Path -LiteralPath $Package).Path
        installed_utc = [DateTime]::UtcNow.ToString('o')
        broker_pid = if ($broker) { $broker.pid } else { $null }
    }
    Write-RvState $state | Out-Null
    $receipt.state = $state
    if ($previous) { $receipt.previous_version = $previous.version }

    if ($StartApp) {
        if ($rebootRequired) {
            Write-Warning 'A reboot is required before the driver can load; the application was not started.'
        } else {
            try {
                Start-RvApplication $root
                Add-Step 'app-started' 'launched with --autostart'
            } catch {
                Write-Warning ('The application was not started automatically: ' + $_.Exception.Message)
                Write-Warning 'Start it from the created shortcut.'
            }
        }
    }
    $receipt.ok = $true
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'install' $receipt | Out-Null
    Write-Host 'RESULT: ok'
    $exitCode = 0
}
catch {
    $receipt.errors += $_.Exception.Message
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'install' $receipt | Out-Null
    Write-Host ("RESULT: failed - {0}" -f $_.Exception.Message)
    $exitCode = 1
}
finally {
    if ($null -ne $resolved) { Close-RvPackage $resolved }
    Stop-Transcript | Out-Null
}
exit $exitCode
