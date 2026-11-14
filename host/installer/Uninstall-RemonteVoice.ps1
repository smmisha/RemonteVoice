<#
.SYNOPSIS
  Removes an installed RemonteVoice: application files, broker logon task,
  shortcuts and the Programs and Features entry. Settings and pairing are kept
  unless -RemoveConfig is given.
.PARAMETER InstallRoot
  Installation directory; taken from the installed state by default.
.PARAMETER RemoveDriver
  Also remove the development audio driver, its trusted certificate and test
  signing. Requires a reboot.
.PARAMETER RemoveConfig
  Also remove %APPDATA%\RemonteVoice (settings and pairing).
.PARAMETER KeepLogs
  Keep installation receipts and logs in %ProgramData%\RemonteVoice.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [switch]$RemoveDriver,
    [switch]$RemoveConfig,
    [switch]$KeepLogs,
    [switch]$KeepTestSigning
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
Start-Transcript -Path (Join-Path $logDirectory 'uninstall.log') -Force | Out-Null
$receipt = [ordered]@{
    operation = 'uninstall'
    started_utc = [DateTime]::UtcNow.ToString('o')
    ok = $false
    steps = @()
    errors = @()
}
function Add-Step([string]$Name, [string]$Detail) {
    $script:receipt.steps += [ordered]@{ name = $Name; detail = $Detail; at_utc = [DateTime]::UtcNow.ToString('o') }
    Write-Host ('[{0}] {1}' -f $Name, $Detail)
}

try {
    $state = Read-RvState
    $root = Get-RvInstallRootFromState $InstallRoot
    Add-Step 'target' ("install_root={0} recorded={1}" -f $root, [bool]$state)

    Stop-RvProcesses
    Add-Step 'stopped' 'application and broker stopped'
    Remove-RvBrokerTask
    Add-Step 'task-removed' $script:RvTaskName
    Remove-RvShortcuts
    Add-Step 'shortcuts-removed' 'desktop and Start menu'
    Remove-RvUninstallEntry
    Add-Step 'uninstall-entry-removed' 'Programs and Features'

    if ($RemoveDriver) {
        $driverStorage = Join-Path (Join-Path (Get-RvDataDirectory) 'installer') 'driver'
        $driverSetup = Join-Path $driverStorage 'RemonteVoiceDriverSetup.exe'
        if (Test-Path -LiteralPath $driverSetup) {
            $driver = Remove-RvDriver $driverStorage $driverSetup -KeepTestSigning:$KeepTestSigning
            Add-Step 'driver-removed' ("reboot_required={0}" -f [bool]$driver.reboot_required)
        } else {
            Write-Warning 'The stored driver package is missing; driver removal was skipped.'
        }
    }

    if (Test-Path -LiteralPath $root) {
        $normalized = [IO.Path]::GetFullPath($root)
        $programFiles = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\')
        $recorded = if ($state -and $state.install_root) { [IO.Path]::GetFullPath([string]$state.install_root) } else { $null }
        $allowed = ($normalized -like ($programFiles + '\RemonteVoice')) -or ($recorded -and $normalized -eq $recorded)
        if (-not $allowed) {
            throw "Refusing to remove an unexpected directory: $normalized"
        }
        Remove-Item -LiteralPath $normalized -Recurse -Force
        Add-Step 'files-removed' $normalized
    }
    Remove-RvState

    if ($RemoveConfig) {
        $configDirectory = Join-Path $env:APPDATA $script:RvProductName
        if (Test-Path -LiteralPath $configDirectory) {
            Remove-Item -LiteralPath $configDirectory -Recurse -Force
            Add-Step 'config-removed' $configDirectory
        }
    }
    $receipt.ok = $true
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'uninstall' $receipt | Out-Null
    Write-Host 'RESULT: ok'
    $exitCode = 0
}
catch {
    $receipt.errors += $_.Exception.Message
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    Write-RvReceipt 'uninstall' $receipt | Out-Null
    Write-Host ("RESULT: failed - {0}" -f $_.Exception.Message)
    $exitCode = 1
}
finally {
    Stop-Transcript | Out-Null
    if (-not $KeepLogs) {
        $dataDirectory = Join-Path $env:ProgramData $script:RvProductName
        if (Test-Path -LiteralPath $dataDirectory) {
            Remove-Item -LiteralPath $dataDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
exit $exitCode
