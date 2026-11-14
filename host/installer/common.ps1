# Shared helpers for the RemonteVoice setup scripts.
# Dot-source only; nothing here runs at import time.
Set-StrictMode -Version 2.0

$script:RvProductName = 'RemonteVoice'
$script:RvTaskName = 'RemonteVoice'
$script:RvUninstallKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RemonteVoice'
$script:RvAdminSid = 'S-1-5-32-544'
$script:RvSystemSid = 'S-1-5-18'
$script:RvUsersSid = 'S-1-5-32-545'

function Test-RvAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-RvAdministrator([string]$Operation) {
    if (-not (Test-RvAdministrator)) {
        throw "$Operation requires administrator rights. Run the setup elevated (UAC prompt)."
    }
}

function Enter-RvElevatedSelf([string]$ScriptPath, [string[]]$ScriptArguments) {
    # Relaunch this script elevated and wait for it; returns the child exit code.
    $quoted = '"' + $ScriptPath + '"'
    $argumentList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $quoted) + @($ScriptArguments)
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -ArgumentList $argumentList
    $null = $process.WaitForExit()
    return $process.ExitCode
}

function Get-RvDataDirectory {
    $directory = Join-Path $env:ProgramData $script:RvProductName
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    return $directory
}

function Get-RvLogDirectory {
    $directory = Join-Path (Get-RvDataDirectory) 'logs'
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    return $directory
}

function Write-RvReceipt([string]$Name, $Document) {
    $directory = Join-Path (Get-RvDataDirectory) 'receipts'
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $path = Join-Path $directory ($Name + '.json')
    $temporary = $path + '.tmp'
    ($Document | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $path -Force
    return $path
}

function Resolve-RvPackage([string]$Package) {
    if (-not (Test-Path -LiteralPath $Package)) { throw "Package not found: $Package" }
    $item = Get-Item -LiteralPath $Package
    if ($item.PSIsContainer) {
        if (-not (Test-Path -LiteralPath (Join-Path $item.FullName 'package-manifest.json'))) {
            throw "Not a RemonteVoice package (package-manifest.json missing): $($item.FullName)"
        }
        return [ordered]@{ Root = $item.FullName; Temporary = $null }
    }
    if ($item.Extension -ne '.zip') { throw 'Expected a package directory or a .zip package' }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('rv-setup-' + [guid]::NewGuid().ToString('N'))
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($item.FullName, $temporary)
    } catch {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
        Expand-Archive -LiteralPath $item.FullName -DestinationPath $temporary -Force
    }
    $root = $temporary
    if (-not (Test-Path -LiteralPath (Join-Path $root 'package-manifest.json'))) {
        $nested = @(Get-ChildItem -LiteralPath $temporary -Directory |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'package-manifest.json') })
        if ($nested.Count -ne 1) { throw 'Package manifest not found inside the archive' }
        $root = $nested[0].FullName
    }
    return [ordered]@{ Root = $root; Temporary = $temporary }
}

function Close-RvPackage($Resolved) {
    if ($null -ne $Resolved.Temporary -and (Test-Path -LiteralPath $Resolved.Temporary)) {
        Remove-Item -LiteralPath $Resolved.Temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Read-RvPackageManifest([string]$Root) {
    $path = Join-Path $Root 'package-manifest.json'
    if (-not (Test-Path -LiteralPath $path)) { throw "package-manifest.json missing: $Root" }
    return (Get-Content -LiteralPath $path -Raw -Encoding utf8 | ConvertFrom-Json)
}

function Test-RvPackageIntegrity($Manifest, [string]$Root) {
    if ($Manifest.schema_version -ne 1 -or $Manifest.product -ne $script:RvProductName) {
        throw 'Unsupported package manifest'
    }
    $broken = @()
    foreach ($entry in $Manifest.files.PSObject.Properties) {
        $relative = $entry.Name -replace '/', '\'
        $path = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $broken += $relative; continue }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $entry.Value) { $broken += $relative }
    }
    if ($broken.Count -ne 0) {
        throw ('Package integrity check failed: ' + (($broken | Select-Object -First 5) -join ', '))
    }
    return @($Manifest.files.PSObject.Properties).Count
}

function Get-RvInstalledAppPath([string]$InstallRoot) {
    if (-not $InstallRoot) { $InstallRoot = Join-Path $env:ProgramFiles $script:RvProductName }
    return [IO.Path]::GetFullPath($InstallRoot)
}

function Get-RvInstallRootFromState([string]$Default) {
    $state = Read-RvState
    if ($null -ne $state -and $state.install_root) { return [string]$state.install_root }
    if ($Default) { return [IO.Path]::GetFullPath($Default) }
    return [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles $script:RvProductName))
}

function Read-RvState {
    $path = Join-Path (Get-RvDataDirectory) 'install-state.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding utf8 | ConvertFrom-Json) } catch { return $null }
}

function Write-RvState($Document) {
    $path = Join-Path (Get-RvDataDirectory) 'install-state.json'
    $temporary = $path + '.tmp'
    ($Document | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $path -Force
    return $path
}

function Remove-RvState {
    $path = Join-Path (Get-RvDataDirectory) 'install-state.json'
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Stop-RvProcesses {
    param([int]$TimeoutSeconds = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    # Graceful close first: the host restores audio routes and disarms its guard.
    foreach ($process in @(Get-Process -Name $script:RvProductName -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 })) {
        $null = $process.CloseMainWindow()
    }
    do {
        Start-Sleep -Milliseconds 300
        $running = @(Get-Process -Name $script:RvProductName -ErrorAction SilentlyContinue)
    } while ($running.Count -ne 0 -and (Get-Date) -lt $deadline)
    if ($running.Count -ne 0) {
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    # The logon task may own the broker; end it before replacing files.
    & schtasks /end /tn $script:RvTaskName 2>&1 | Out-Null
    Get-Process -Name ($script:RvProductName + 'Broker') -ErrorAction SilentlyContinue | Stop-Process -Force
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        $broker = Get-Process -Name ($script:RvProductName + 'Broker') -ErrorAction SilentlyContinue
        $pipe = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice-Mic-' })
    } while (($broker -or $pipe.Count -ne 0) -and (Get-Date) -lt $deadline)
    if ($broker -or $pipe.Count -ne 0) { throw 'The broker process or pipe did not stop' }
}

function Get-RvBrokerPath([string]$InstallRoot) {
    return Join-Path $InstallRoot ($script:RvProductName + 'Broker.exe')
}

function Get-RvAppPath([string]$InstallRoot) {
    return Join-Path $InstallRoot ($script:RvProductName + '.exe')
}

function Install-RvAppFiles([string]$Source, [string]$Target) {
    # The broker verifies an exact file set, so stale files from a previous build
    # must be removed after copying the new package over the installation.
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Copy-Item -Path (Join-Path $Source '*') -Destination $Target -Recurse -Force
    $manifestPath = Join-Path $Source 'remontevoice-installation.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Application manifest missing in the package' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    $expected = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [void]$expected.Add('remontevoice-installation.json')
    foreach ($name in $manifest.files.PSObject.Properties.Name) {
        [void]$expected.Add(($name -replace '/', '\'))
    }
    $removed = 0
    Get-ChildItem -LiteralPath $Target -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($Target.Length + 1)
        if (-not $expected.Contains($relative)) {
            Remove-Item -LiteralPath $_.FullName -Force
            $removed++
        }
    }
    Get-ChildItem -LiteralPath $Target -Recurse -Directory -Force |
        Sort-Object FullName -Descending | ForEach-Object {
            $relative = $_.FullName.Substring($Target.Length + 1)
            $parts = $relative -split '\\'
            $implied = $false
            foreach ($name in $expected) {
                if ($name.StartsWith($relative + '\', [StringComparison]::OrdinalIgnoreCase)) { $implied = $true; break }
            }
            if (-not $implied -and -not (Get-ChildItem -LiteralPath $_.FullName -Force)) {
                Remove-Item -LiteralPath $_.FullName -Force
            }
        }
    return [ordered]@{ manifest = $manifest; removed = $removed; files = $expected.Count }
}

function Set-RvProtectedAcl([string]$Path) {
    # The driver path only accepts an installation under Program Files with a
    # trusted owner and no write rights for regular users.
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner(([Security.Principal.SecurityIdentifier]::new($script:RvAdminSid)))
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagate = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($entry in @(
            @($script:RvAdminSid, [Security.AccessControl.FileSystemRights]::FullControl),
            @($script:RvSystemSid, [Security.AccessControl.FileSystemRights]::FullControl),
            @($script:RvUsersSid, [Security.AccessControl.FileSystemRights]'ReadAndExecute, Synchronize'))) {
        $sid = [Security.Principal.SecurityIdentifier]::new([string]$entry[0])
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $entry[1], $inherit, $propagate, $allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    $check = Get-Acl -LiteralPath $Path
    $owner = ([Security.Principal.NTAccount]::new($check.Owner)).Translate(
        [Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @($script:RvAdminSid, $script:RvSystemSid)) {
        throw "Installation owner is not trusted: $owner"
    }
    foreach ($rule in @($check.Access)) {
        if ($rule.AccessControlType -ne $allow) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -in @($script:RvAdminSid, $script:RvSystemSid)) { continue }
        # Regular users may read and execute only: any further right is a defect.
        $readOnly = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
            [Security.AccessControl.FileSystemRights]::Synchronize
        $extra = $rule.FileSystemRights -band (-bnot $readOnly)
        if ($extra -ne 0) {
            throw "Installation is writable beyond administrators: $sid ($($rule.FileSystemRights))"
        }
    }
    return $owner
}

function Get-RvDriverDevice {
    $devices = @(Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {
            $_.InstanceId -like 'ROOT\MEDIA\*' -and $_.FriendlyName -like '*RemonteVoice*'
        })
    if ($devices.Count -eq 0) { return $null }
    if ($devices.Count -gt 1) { throw 'Multiple RemonteVoice root devices found; resolve them manually' }
    return $devices[0]
}

function Set-RvManifestDriverInstance([string]$ManifestPath, [string]$Instance) {
    $document = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($document.driver_instance_id -eq $Instance) { return $false }
    $document.driver_instance_id = $Instance
    $json = $document | ConvertTo-Json -Depth 6 -Compress
    [IO.File]::WriteAllText($ManifestPath, $json + "`n", ([Text.UTF8Encoding]::new($false)))
    return $true
}

function Register-RvBrokerTask([string]$TemplatePath, [string]$InstallRoot) {
    $brokerPath = Get-RvBrokerPath $InstallRoot
    if (-not (Test-Path -LiteralPath $TemplatePath)) { throw "Task template missing: $TemplatePath" }
    if (-not (Test-Path -LiteralPath $brokerPath)) { throw "Broker executable missing: $brokerPath" }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $xml = (Get-Content -LiteralPath $TemplatePath -Raw -Encoding utf8).Replace(
        '@@BROKER@@', $brokerPath).Replace('@@INSTALL_ROOT@@', $InstallRoot).Replace('@@USER_SID@@', $sid)
    if ($xml -match '@@') { throw 'Unresolved placeholder in the task template' }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('rv-task-' + [guid]::NewGuid().ToString('N') + '.xml')
    [IO.File]::WriteAllText($temporary, $xml, [Text.Encoding]::Unicode)
    try {
        & schtasks /create /tn $script:RvTaskName /xml $temporary /f | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed with $LASTEXITCODE" }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    $registered = (& schtasks /query /tn $script:RvTaskName /xml) -join "`n"
    if ($registered -notmatch [regex]::Escape($brokerPath)) {
        throw 'Registered task does not start the installed broker'
    }
}

function Remove-RvBrokerTask {
    & schtasks /end /tn $script:RvTaskName 2>&1 | Out-Null
    & schtasks /delete /tn $script:RvTaskName /f 2>&1 | Out-Null
}

function Get-RvShortcutPaths {
    return @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) ($script:RvProductName + '.lnk')),
        (Join-Path ([Environment]::GetFolderPath('Programs')) ($script:RvProductName + '.lnk'))
    )
}

function New-RvShortcuts([string]$InstallRoot) {
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($linkPath in Get-RvShortcutPaths) {
            $link = $shell.CreateShortcut($linkPath)
            $link.TargetPath = Get-RvAppPath $InstallRoot
            $link.Arguments = '--autostart'
            $link.WorkingDirectory = $InstallRoot
            $link.IconLocation = (Get-RvAppPath $InstallRoot) + ',0'
            $link.Description = 'RemonteVoice: phone audio on the laptop'
            $link.Save()
        }
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}

function Remove-RvShortcuts {
    foreach ($linkPath in Get-RvShortcutPaths) {
        Remove-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
    }
}

function Set-RvUninstallEntry([string]$InstallRoot, [string]$Version, [string]$UninstallerPath) {
    New-Item -Path $script:RvUninstallKey -Force | Out-Null
    Set-ItemProperty -Path $script:RvUninstallKey -Name DisplayName -Value $script:RvProductName
    Set-ItemProperty -Path $script:RvUninstallKey -Name DisplayVersion -Value $Version
    Set-ItemProperty -Path $script:RvUninstallKey -Name Publisher -Value 'RemonteVoice'
    Set-ItemProperty -Path $script:RvUninstallKey -Name InstallLocation -Value $InstallRoot
    Set-ItemProperty -Path $script:RvUninstallKey -Name DisplayIcon -Value ((Get-RvAppPath $InstallRoot) + ',0')
    Set-ItemProperty -Path $script:RvUninstallKey -Name UninstallString -Value (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $UninstallerPath)
    Set-ItemProperty -Path $script:RvUninstallKey -Name NoModify -Value 1
    Set-ItemProperty -Path $script:RvUninstallKey -Name NoRepair -Value 1
    Set-ItemProperty -Path $script:RvUninstallKey -Name EstimatedSize -Value (
        [int]((Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Force |
            Measure-Object Length -Sum).Sum / 1KB))
}

function Remove-RvUninstallEntry {
    Remove-Item -LiteralPath $script:RvUninstallKey -Recurse -Force -ErrorAction SilentlyContinue
}

function Install-RvDriver([string]$DriverDirectory, [string]$DriverSetupPath) {
    # Development-mode driver installation: test-signed package, TESTSIGNING and
    # an explicitly trusted development certificate. Requires a reboot when test
    # signing is not active yet.
    Assert-RvAdministrator 'Driver installation'
    foreach ($name in 'RemonteVoiceAudio.sys', 'RemonteVoiceAudio.inf',
            'RemonteVoiceAudio.cat', 'RemonteVoiceDevelopment.cer', 'RemonteVoiceDriverSetup.exe') {
        if (-not (Test-Path -LiteralPath (Join-Path $DriverDirectory $name))) {
            throw "Driver package is incomplete: $name"
        }
    }
    $secureBoot = $null
    try { $secureBoot = Confirm-SecureBootUEFI } catch { $secureBoot = 'unknown' }
    if ($secureBoot -eq $true) {
        throw 'Secure Boot is enabled: a test-signed driver will not load. Disable Secure Boot first.'
    }
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
        (Join-Path $DriverDirectory 'RemonteVoiceDevelopment.cer'))
    if ($certificate.Subject -ne 'CN=RemonteVoice Development Only') {
        throw "Unexpected driver certificate subject: $($certificate.Subject)"
    }
    foreach ($store in 'Root', 'TrustedPublisher') {
        $path = 'Cert:\LocalMachine\' + $store
        $existing = @(Get-ChildItem -Path $path | Where-Object { $_.Thumbprint -eq $certificate.Thumbprint })
        if ($existing.Count -eq 0) {
            Import-Certificate -FilePath (Join-Path $DriverDirectory 'RemonteVoiceDevelopment.cer') `
                -CertStoreLocation $path | Out-Null
        }
    }
    $rebootRequired = $false
    $probe = (& $DriverSetupPath probe) -join "`n"
    if ($probe -match 'testsigning=off') {
        & bcdedit /set testsigning on | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'bcdedit could not enable test signing' }
        $rebootRequired = $true
    }
    & pnputil /add-driver (Join-Path $DriverDirectory 'RemonteVoiceAudio.inf') /install | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "pnputil failed with exit code $LASTEXITCODE" }
    if ($probe -match 'device=absent' -or $rebootRequired) {
        # A reboot is needed before a test-signed driver can bind to a new device.
        if ($rebootRequired) {
            Write-Host 'Test signing was enabled; reboot before the root device is created.'
        } else {
            $install = (& $DriverSetupPath install (Join-Path $DriverDirectory 'RemonteVoiceAudio.inf')) -join "`n"
            Write-Host $install
            if ($LASTEXITCODE -ne 0) { throw 'Root device installation failed' }
            if ($install -match 'restartRequired=True') { $rebootRequired = $true }
        }
    } else {
        Write-Verbose 'The RemonteVoice root device is already present.'
    }
    return [ordered]@{ reboot_required = $rebootRequired; certificate = $certificate.Thumbprint }
}

function Remove-RvDriver([string]$DriverDirectory, [string]$DriverSetupPath, [switch]$KeepTestSigning) {
    Assert-RvAdministrator 'Driver removal'
    if (Test-Path -LiteralPath $DriverSetupPath) { & $DriverSetupPath remove | Out-Host }
    $enumeration = (& pnputil /enum-drivers) -join "`n"
    foreach ($block in ($enumeration -split "`r?`n`r?`n")) {
        if ($block -match 'RemonteVoiceAudio' -and $block -match 'Published Name:\s*(\S+)') {
            & pnputil /delete-driver $Matches[1] /uninstall /force | Out-Host
        }
    }
    $thumbprint = $null
    $candidate = Join-Path $DriverDirectory 'RemonteVoiceDevelopment.cer'
    if (Test-Path -LiteralPath $candidate) {
        $thumbprint = ([Security.Cryptography.X509Certificates.X509Certificate2]::new($candidate)).Thumbprint
    }
    foreach ($store in 'Root', 'TrustedPublisher') {
        $path = 'Cert:\LocalMachine\' + $store
        foreach ($entry in @(Get-ChildItem -Path $path)) {
            if ($thumbprint -and $entry.Thumbprint -eq $thumbprint) {
                Remove-Item -LiteralPath $entry.PSPath -Force
            }
        }
    }
    if (-not $KeepTestSigning) {
        & bcdedit /set testsigning off | Out-Host
    }
    return [ordered]@{ reboot_required = $true }
}

function Wait-RvBrokerReady([int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $process = $null
    $pipes = @()
    do {
        Start-Sleep -Milliseconds 250
        if ($null -eq $process) {
            $process = Get-Process -Name ($script:RvProductName + 'Broker') -ErrorAction SilentlyContinue |
                Sort-Object StartTime -Descending | Select-Object -First 1
        } else {
            $process.Refresh()
            if ($process.HasExited) { throw "Broker exited with code $($process.ExitCode)" }
        }
        $pipes = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'RemonteVoice-Mic-' })
    } while ($pipes.Count -eq 0 -and (Get-Date) -lt $deadline)
    if ($null -eq $process) { throw 'Broker process did not appear' }
    if ($pipes.Count -eq 0) { throw 'Broker pipe did not appear within the timeout' }
    return [ordered]@{ pid = $process.Id; pipe = $pipes[0] }
}

function Start-RvBrokerTaskAndWait([int]$TimeoutSeconds = 60) {
    & schtasks /run /tn $script:RvTaskName 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "schtasks /run failed with $LASTEXITCODE" }
    return Wait-RvBrokerReady -TimeoutSeconds $TimeoutSeconds
}

function Start-RvApplication([string]$InstallRoot) {    $exe = Get-RvAppPath $InstallRoot
    $shell = New-Object -ComObject Shell.Application
    try {
        $desktop = $shell.Windows().Item([uint32]8).Document
        if ($null -eq $desktop) { throw 'Windows desktop shell is unavailable' }
        $desktop.Application.ShellExecute($exe, '--autostart', $InstallRoot, 'open', 1)
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}
