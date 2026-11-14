# Elevated: install a build whose broker is a windowed executable and point the
# logon task back at it directly, so no launcher and no console window remain.
#
# The pinned installation file set must match the new manifest exactly: stale
# files from the previous build are removed, missing ones are copied. Any
# verification failure restores the previous build and the previous task.
#
# Usage (elevated):
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy-windowless-broker.ps1 `
#       -Source M:\...\host\build\desktop-064\dist\RemonteVoicePair `
#       -RollbackSource M:\...\host\build\desktop-063\dist\RemonteVoicePair `
#       -RemoveLauncher
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$RollbackSource,
    [switch]$RemoveLauncher
)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$evidence = Join-Path $repo 'host\build\windowless-broker'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
Start-Transcript -Path (Join-Path $evidence 'deploy.log') -Force | Out-Null

$installDir = 'C:\Program Files\RemonteVoice-Dev'
$launcherDir = 'C:\Program Files\RemonteVoice-Launcher'
$manifestName = 'remontevoice-installation.json'
$taskXmlDirect = Join-Path $repo 'host\driver\lab\remontevoice-task.xml'
$receipt = [ordered]@{
    started_utc = [DateTime]::UtcNow.ToString('o')
    ok = $false
    restored_previous = $false
    steps = @()
    errors = @()
}

function Add-Step($name, $detail) {
    $script:receipt.steps += [ordered]@{ name = $name; detail = $detail; at_utc = [DateTime]::UtcNow.ToString('o') }
    Write-Host ("[{0}] {1}" -f $name, $detail)
}

function Write-Receipt {
    $script:receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    ($script:receipt | ConvertTo-Json -Depth 6) |
        Set-Content -LiteralPath (Join-Path $evidence 'deploy-result.json') -Encoding utf8
}

function Get-PeSubsystem([string]$path) {
    $stream = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
        $reader = New-Object IO.BinaryReader($stream)
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4 + 20 + 0x44
        return $reader.ReadUInt16()
    } finally { $stream.Dispose() }
}

function Get-BrokerPipes {
    @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice-Mic-' })
}

function Stop-RunningBroker {
    Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue | Stop-Process -Force
    $deadline = (Get-Date).AddSeconds(15)
    do { Start-Sleep -Milliseconds 200 }
    while (((Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue) -or
            (Get-BrokerPipes).Count -ne 0) -and (Get-Date) -lt $deadline)
    if ((Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue) -or (Get-BrokerPipes).Count -ne 0) {
        throw 'Previous broker did not stop'
    }
}

function Register-DirectTask {
    if (-not (Test-Path -LiteralPath $taskXmlDirect -PathType Leaf)) { throw "Task XML missing: $taskXmlDirect" }
    $content = Get-Content -LiteralPath $taskXmlDirect -Raw -Encoding UTF8
    $temporary = Join-Path $env:TEMP 'remontevoice-task.xml'
    [IO.File]::WriteAllText($temporary, $content, [Text.Encoding]::Unicode)
    & schtasks /create /tn 'RemonteVoice' /xml $temporary /f | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed with $LASTEXITCODE" }
}

function Start-BrokerViaTask {
    & schtasks /run /tn 'RemonteVoice' | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "schtasks /run failed with $LASTEXITCODE" }
    $started = Get-Date
    $deadline = $started.AddSeconds(60)
    $process = $null
    $pipes = @()
    do {
        Start-Sleep -Milliseconds 250
        if ($null -eq $process) {
            $process = Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue |
                Sort-Object StartTime -Descending | Select-Object -First 1
        } else {
            $process.Refresh()
            if ($process.HasExited) { throw "Broker exited with code $($process.ExitCode)" }
        }
        $pipes = Get-BrokerPipes
    } while ($pipes.Count -eq 0 -and (Get-Date) -lt $deadline)
    if ($null -eq $process) { throw 'Broker process did not appear' }
    if ($pipes.Count -eq 0) { throw 'Broker pipe did not appear within 60 seconds' }
    return [ordered]@{
        process = $process
        pipes = $pipes
        ready_seconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    }
}

function Invoke-InstallCopy([string]$package) {
    # Copy first, then drop anything the new manifest does not list: the pinned
    # manifest verifies an exact file set, and the broker refuses to start otherwise.
    Copy-Item -Path (Join-Path $package '*') -Destination $installDir -Recurse -Force
    $encoded = Get-Content -LiteralPath (Join-Path $package $manifestName) -Raw
    $manifest = $encoded | ConvertFrom-Json
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    # The manifest lists every file it covers but cannot list itself; the
    # protected installation legitimately contains it beside the executables.
    [void]$expected.Add($manifestName)
    foreach ($name in $manifest.files.PSObject.Properties.Name) {
        [void]$expected.Add(($name -replace '/', '\'))
    }
    $expectedDirectories = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $expected) {
        $parent = [IO.Path]::GetDirectoryName($relative)
        while ($parent) { [void]$expectedDirectories.Add($parent); $parent = [IO.Path]::GetDirectoryName($parent) }
    }
    $removed = 0
    Get-ChildItem -LiteralPath $installDir -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($installDir.Length + 1)
        if (-not $expected.Contains($relative)) { Remove-Item -LiteralPath $_.FullName -Force; $removed++ }
    }
    Get-ChildItem -LiteralPath $installDir -Recurse -Directory -Force | Sort-Object FullName -Descending | ForEach-Object {
        $relative = $_.FullName.Substring($installDir.Length + 1)
        if (-not $expectedDirectories.Contains($relative) -and
                -not (Get-ChildItem -LiteralPath $_.FullName -Force)) {
            Remove-Item -LiteralPath $_.FullName -Force
        }
    }
    & icacls $installDir /setowner '*S-1-5-32-544' /T /C | Out-Null
    & icacls $installDir /grant '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /T /C | Out-Null
    $installedManifest = Join-Path $installDir $manifestName
    if (-not (Test-Path -LiteralPath $installedManifest -PathType Leaf)) {
        throw 'Installation manifest is missing after the copy'
    }
    return [ordered]@{
        source = $package
        manifest_sha256 = (Get-FileHash -LiteralPath $installedManifest -Algorithm SHA256).Hash.ToLowerInvariant()
        broker_sha256 = (Get-FileHash -LiteralPath (Join-Path $installDir 'RemonteVoiceBroker.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
        removed_stale_files = $removed
        broker_subsystem = (Get-PeSubsystem (Join-Path $installDir 'RemonteVoiceBroker.exe'))
    }
}

function Test-WindowlessBroker($details) {
    if ($details.broker_subsystem -ne 2) { throw "Installed broker subsystem is $($details.broker_subsystem), expected GUI (2)" }
    if ((Get-BrokerPipes).Count -ne 1) { throw "Expected one broker pipe, found $((Get-BrokerPipes).Count)" }
    $log = Join-Path $env:APPDATA 'RemonteVoice\broker.log'
    if (-not (Test-Path -LiteralPath $log)) { throw 'Broker log was not created' }
    $lines = @(Get-Content -LiteralPath $log -ErrorAction SilentlyContinue)
    $start = -1
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        if ($lines[$index] -match ' start ') { $start = $index; break }
    }
    if ($start -lt 0) { throw 'Broker log has no start record' }
    $failures = @($lines[$start..($lines.Count - 1)] | Where-Object { $_ -match ' failure ' })
    if ($failures.Count -ne 0) { throw "Broker log reports a failure: $($failures[0])" }
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script elevated (Run as administrator)'
    }
    if (Get-Process -Name RemonteVoice -ErrorAction SilentlyContinue) {
        throw 'Close the RemonteVoice window first: the host holds broker clients'
    }
    foreach ($package in @($Source, $RollbackSource)) {
        if (-not (Test-Path -LiteralPath (Join-Path $package 'RemonteVoiceBroker.exe'))) {
            throw "Broker missing in $package"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $package $manifestName))) { throw "Manifest missing in $package" }
    }
    $receipt.source = [IO.Path]::GetFullPath($Source)
    $receipt.rollback_source = [IO.Path]::GetFullPath($RollbackSource)
    Add-Step 'precheck' "source=$Source rollback=$RollbackSource"

    Stop-RunningBroker
    $installChanged = $true
    Add-Step 'broker-stopped' 'previous broker terminated'

    $details = Invoke-InstallCopy $Source
    Add-Step 'installed' ("subsystem={0} broker_sha={1} removed={2}" -f $details.broker_subsystem, $details.broker_sha256, $details.removed_stale_files)
    $receipt.install = $details

    Register-DirectTask
    Add-Step 'task-registered' $taskXmlDirect

    $broker = Start-BrokerViaTask
    $visible = @(Get-Process -Id $broker.process.Id | Select-Object -ExpandProperty MainWindowTitle | Where-Object { $_ })
    $receipt.broker = [ordered]@{
        pid = $broker.process.Id
        ready_seconds = $broker.ready_seconds
        pipes = $broker.pipes
        window_titles = $visible
    }
    Add-Step 'broker-started' ("pid={0} ready={1}s pipes={2}" -f $broker.process.Id, $broker.ready_seconds, $broker.pipes.Count)
    Test-WindowlessBroker $details
    Add-Step 'verified' 'windowless broker, one pipe, clean log'

    if ($RemoveLauncher -and (Test-Path -LiteralPath $launcherDir)) {
        Remove-Item -LiteralPath $launcherDir -Recurse -Force
        Add-Step 'launcher-removed' $launcherDir
    }
    $receipt.ok = $true
    Write-Host 'RESULT: ok'
    $exitCode = 0
}
catch {
    $receipt.errors += $_.Exception.Message
    Write-Host ("RESULT: failed - {0}" -f $_.Exception.Message)
    try {
        if ($installChanged) {
            Stop-RunningBroker
            $restore = Invoke-InstallCopy $RollbackSource
            Register-DirectTask
            $null = Start-BrokerViaTask
            $receipt.restored_previous = $true
            $receipt.restore = $restore
            Write-Host 'Restored the previous build and task.'
        } elseif (-not (Get-Process -Name RemonteVoiceBroker -ErrorAction SilentlyContinue)) {
            Register-DirectTask
            $null = Start-BrokerViaTask
            Write-Host 'Broker was not running; restarted it with the existing installation.'
        }
    } catch {
        $receipt.errors += "restore failed: $($_.Exception.Message)"
    }
    $exitCode = 1
}
finally {
    Write-Receipt
    Stop-Transcript | Out-Null
}
exit $exitCode
