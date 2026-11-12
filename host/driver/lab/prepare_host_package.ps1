# Prepare a signed development driver package for the physical host.
# Signs only locally: no certificate is trusted, no driver is installed, no boot setting changes.
param(
    [string]$Source = 'host/build/driver-development',
    [string]$Output = 'host/build/driver-host-package',
    [string]$Private = '.harness/evidence/driver-host-signing'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$source = [IO.Path]::GetFullPath((Join-Path $root $Source))
$out = [IO.Path]::GetFullPath((Join-Path $root $Output))
$private = [IO.Path]::GetFullPath((Join-Path $root $Private))
$signTool = Join-Path $root 'host/driver/.tools/microsoft.windows.sdk.cpp.10.0.26100.6584/c/bin/10.0.26100.0/x64/signtool.exe'
$inf2cat = Join-Path $root 'host/driver/.tools/microsoft.windows.wdk.x64.10.0.26100.6584/c/bin/10.0.26100.0/x86/Inf2Cat.exe'

foreach ($tool in @($signTool, $inf2cat)) { if (-not (Test-Path -LiteralPath $tool)) { throw "Tool missing: $tool" } }
if (Test-Path -LiteralPath $out) { throw "Package directory already exists: $out" }
if (Test-Path -LiteralPath $private) { throw "Key directory already exists: $private" }
foreach ($name in @('RemonteVoiceAudio.sys', 'RemonteVoiceAudio.inf')) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $name))) { throw "Build is missing $name" }
}

New-Item -ItemType Directory -Path $private -Force | Out-Null
New-Item -ItemType Directory -Path $out -Force | Out-Null

$rsa = [Security.Cryptography.RSA]::Create(3072)
$cert = $null
try {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=RemonteVoice Development Only', $rsa, 'SHA256',
        [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new([Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature, $true))
    $usage = [Security.Cryptography.OidCollection]::new()
    [void]$usage.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usage, $false))
    $cert = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddMonths(12))

    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $entropyBytes = New-Object byte[] 32
        $rng.GetBytes($entropyBytes)
        $password = [Convert]::ToBase64String($entropyBytes)
    } finally { $rng.Dispose() }
    $pfx = Join-Path $private 'development.pfx'
    [IO.File]::WriteAllBytes($pfx, $cert.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password))
    $entropy = [Text.Encoding]::UTF8.GetBytes('RemonteVoice-host-dev-v1')
    [IO.File]::WriteAllBytes((Join-Path $private 'password.dpapi'), [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($password), $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser))
    [IO.File]::WriteAllBytes((Join-Path $out 'RemonteVoiceDevelopment.cer'), $cert.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))

    foreach ($name in @('RemonteVoiceAudio.sys', 'RemonteVoiceAudio.inf')) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $out $name) -Force
    }

    & $signTool sign /fd SHA256 /f $pfx /p $password (Join-Path $out 'RemonteVoiceAudio.sys') *> (Join-Path $private 'sys.sign.log')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to sign .sys' }

    # The catalog must hash the already signed SYS.
    & $inf2cat "/driver:$out" /os:10_VB_X64,10_CO_X64,10_NI_X64,10_GE_X64 *> (Join-Path $out 'inf2cat.log')
    if ($LASTEXITCODE -ne 0) { throw 'Inf2Cat failed: see inf2cat.log' }

    & $signTool sign /fd SHA256 /f $pfx /p $password (Join-Path $out 'RemonteVoiceAudio.cat') *> (Join-Path $private 'catalog.sign.log')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to sign catalog' }

    $files = [ordered]@{}
    foreach ($name in @('RemonteVoiceAudio.sys', 'RemonteVoiceAudio.inf', 'RemonteVoiceAudio.cat', 'RemonteVoiceDevelopment.cer')) {
        $files[$name] = (Get-FileHash -LiteralPath (Join-Path $out $name) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    foreach ($name in @('RemonteVoiceAudio.sys', 'RemonteVoiceAudio.cat')) {
        $sig = Get-AuthenticodeSignature -LiteralPath (Join-Path $out $name)
        if ($sig.SignerCertificate.Thumbprint -ne $cert.Thumbprint) { throw "Signature thumbprint mismatch: $name" }
    }
    [ordered]@{
        created_utc = [DateTime]::UtcNow.ToString('o')
        certificate_thumbprint = $cert.Thumbprint
        certificate_not_after = $cert.NotAfter.ToUniversalTime().ToString('o')
        private_key_directory = $Private
        host_trust_store_changed = $false
        driver_installed = $false
        files = $files
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $out 'package-manifest.json') -Encoding utf8

    "Package prepared: $out"
    "Certificate: $($cert.Thumbprint)"
    $files.Keys | ForEach-Object { "  $_" }
} finally {
    $password = $null
    if ($null -ne $cert) { $cert.Dispose() }
    $rsa.Dispose()
}
