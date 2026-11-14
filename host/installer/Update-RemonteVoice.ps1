<#
.SYNOPSIS
  Updates an installed RemonteVoice with a newer package, keeping settings and
  pairing in %APPDATA%\RemonteVoice.
.PARAMETER Package
  Extracted package directory or RemonteVoice-Setup-<version>.zip.
.PARAMETER InstallRoot
  Installation directory; taken from the installed state by default.
.PARAMETER AllowSameVersion
  Permit reinstalling the same version (repair).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [string]$InstallRoot = '',
    [switch]$AllowSameVersion,
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
Start-Transcript -Path (Join-Path $logDirectory 'update.log') -Force | Out-Null
$receipt = [ordered]@{
    operation = 'update'
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
    $state = Read-RvState
    if ($null -eq $state) { throw 'RemonteVoice is not installed; run Install-RemonteVoice.ps1' }
    $resolved = Resolve-RvPackage $Package
    $manifest = Read-RvPackageManifest $resolved.Root
    if ($SkipIntegrityCheck) {
        Add-Step 'integrity' 'skipped by request'
    } else {
        $count = Test-RvPackageIntegrity $manifest $resolved.Root
        Add-Step 'integrity' ("verified {0} files" -f $count)
    }
    $installed = [version]$state.version
    $offered = [version]$manifest.version
    if ($offered -lt $installed) { throw "The package ($offered) is older than the installation ($installed)" }
    if ($offered -eq $installed -and -not $AllowSameVersion) {
        throw "The package is the same version ($offered); use -AllowSameVersion to repair"
    }
    $root = Get-RvInstallRootFromState $InstallRoot
    Add-Step 'versions' ("installed={0} package={1} root={2}" -f $installed, $offered, $root)

    $wasRunning = [bool](Get-Process -Name $script:RvProductName -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 })
    Stop-RvProcesses
    Add-Step 'stopped' 'previous application and broker stopped'

    $instance = [string]$state.driver_instance
    if (-not $instance) {
        $device = Get-RvDriverDevice
        if ($device) { $instance = $device.InstanceId }
    }
    if ($instance -notmatch '^ROOT\\MEDIA\\[A-Za-z0-9_-]{1,64}$') {
        $instance = [string]$manifest.driver_instance_id
        Write-Warning "Driver instance is unknown; using the package value: $instance"
    }

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

    Register-RvBrokerTask (Join-Path $resolved.Root 'remontevoice-task.xml.in') $root
    $broker = Start-RvBrokerTaskAndWait
    Add-Step 'broker' ("pid={0} pipe={1}" -f $broker.pid, $broker.pipe)

    Set-RvUninstallEntry $root ([string]$manifest.version) (Join-Path $dataInstaller 'Uninstall-RemonteVoice.ps1')
    $state.version = [string]$manifest.version
    $state.driver_instance = $instance
    $state.updated_utc = [DateTime]::UtcNow.ToString('o')
    $state.broker_pid = $broker.pid
    Write-RvState $state | Out-Null
    Add-Step 'state' ("version={0}" -f $state.version)

    if ($wasRunning) {
        try {
            Start-RvApplication $root
            Add-Step 'app-restarted' 'launched with --autostart'
        } catch {
            Write-Warning ('The application was not restarted automatically: ' + $_.Exception.Message)
        }
    }
    $receipt.ok = $true
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'update' $receipt | Out-Null
    Write-Host 'RESULT: ok'
    $exitCode = 0
}
catch {
    $receipt.errors += $_.Exception.Message
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'update' $receipt | Out-Null
    Write-Host ("RESULT: failed - {0}" -f $_.Exception.Message)
    $exitCode = 1
}
finally {
    if ($null -ne $resolved) { Close-RvPackage $resolved }
    Stop-Transcript | Out-Null
}
exit $exitCode
