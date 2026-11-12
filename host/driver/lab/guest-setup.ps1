# Guest-only fallback for the BCD WMI provider; run through authenticated VMware API.
$ErrorActionPreference = 'Stop'
$rvDirectory = $PSScriptRoot
$rvReport = Join-Path $rvDirectory 'api-setup-result.json'
$rvResult = [ordered]@{ success=$false; certificate_trust_changed=$false; boot_changed=$false }
try {
    $rvConfig = Get-Content -LiteralPath (Join-Path $rvDirectory 'lab-config.json') -Raw | ConvertFrom-Json
    $rvManufacturer = (Get-CimInstance Win32_ComputerSystem).Manufacturer
    $rvSerial = (Get-CimInstance Win32_BIOS).SerialNumber -replace '^VMware-', '' -replace '[^0-9a-fA-F]', ''
    $rvExpected = $rvConfig.vm_bios_uuid -replace '^VMware-', '' -replace '[^0-9a-fA-F]', ''
    if ($rvManufacturer -notlike 'VMware*' -or $rvSerial.Length -ne 32 -or $rvSerial -ne $rvExpected -or (Get-CimInstance Win32_OperatingSystem).BuildNumber -ne [string]$rvConfig.guest_build) { throw 'Wrong VM; refusing changes' }
    $rvIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$rvIdentity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Guest administrator required' }
    foreach ($rvFile in $rvConfig.files.PSObject.Properties) {
        if ([IO.Path]::GetFileName($rvFile.Name) -ne $rvFile.Name) { throw 'Invalid package filename' }
        if ((Get-FileHash -LiteralPath (Join-Path $rvDirectory $rvFile.Name) -Algorithm SHA256).Hash -ne $rvFile.Value) { throw 'Package hash mismatch' }
    }
    $rvBcd = Join-Path $env:SystemRoot 'System32\bcdedit.exe'
    $rvBefore = @(& $rvBcd /enum '{current}' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ('Guest BCD read failed: ' + ($rvBefore -join "`n")) }
    $rvResult.bcd_before = $rvBefore -join "`n"
    $rvCertificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2 (Join-Path $rvDirectory 'RemonteVoiceLab.cer')
    if ($rvCertificate.Thumbprint -ne $rvConfig.certificate_thumbprint) { throw 'Unexpected certificate' }
    foreach ($rvStoreName in @('Root','TrustedPublisher')) {
        $rvStore = New-Object Security.Cryptography.X509Certificates.X509Store ($rvStoreName, 'LocalMachine')
        try { $rvStore.Open('ReadWrite'); $rvStore.Add($rvCertificate); $rvResult.certificate_trust_changed=$true } finally { $rvStore.Close() }
    }
    $rvOutput = @(& $rvBcd /set '{current}' testsigning on 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ('Guest TESTSIGNING failed: ' + ($rvOutput -join "`n")) }
    $rvResult.boot_changed=$true
    $rvAfter = @(& $rvBcd /enum '{current}' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Guest BCD readback failed' }
    $rvResult.bcd_after=$rvAfter -join "`n"
    $rvResult.success=$true
} catch { $rvResult.error=$_.Exception.Message }
$rvResult.finished_utc=[DateTime]::UtcNow.ToString('o')
$rvResult | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $rvReport -Encoding utf8
if (-not $rvResult.success) { exit 1 }
