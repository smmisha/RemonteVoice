param(
    [Parameter(Mandatory = $true)][string]$ApkPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [switch]$Unsigned
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# An unsigned APK is expected to make apksigner return nonzero. Capture that
# result explicitly, including on PowerShell 7 installations enabling this mode.
$PSNativeCommandUseErrorActionPreference = $false

$sourceApk = (Resolve-Path -LiteralPath $ApkPath -ErrorAction Stop).Path
$sdkRoot = $env:ANDROID_HOME
if (!$sdkRoot) { $sdkRoot = $env:ANDROID_SDK_ROOT }
if (!$sdkRoot) { throw 'Set ANDROID_HOME for the Android Build Tools signature verifier.' }
$signerName = if ($env:OS -eq 'Windows_NT') { 'apksigner.bat' } else { 'apksigner' }
$apkSigner = Join-Path $sdkRoot "build-tools/35.0.0/$signerName"
if (!(Test-Path -LiteralPath $apkSigner -PathType Leaf)) { throw 'Android Build Tools 35.0.0 apksigner is unavailable.' }

# Inspect the container without executing APK contents. Verification output is
# captured: certificate DN, SHA-1/MD5 and tool diagnostics are not printed.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($sourceApk)
try {
    if ($null -eq $archive.GetEntry('AndroidManifest.xml') -or $null -eq $archive.GetEntry('classes.dex')) {
        throw 'The APK container is missing required entries.'
    }
} finally { $archive.Dispose() }
$verification = @(& $apkSigner verify --print-certs $sourceApk 2>&1)
$signatureValid = $LASTEXITCODE -eq 0
$certificateSha256 = $null
if ($Unsigned) {
    if ($signatureValid) { throw 'Expected an unsigned release artifact, but APK signature verification succeeded.' }
    $artifactName = 'RemoteVoice-Android-unsigned-release-not-installable.apk'
    $artifactKind = 'unsigned-release-not-installable'
} else {
    if (!$signatureValid) { throw 'Release APK signature verification failed.' }
    $certificateMatches = [regex]::Matches(($verification -join "`n"), '(?m)^Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F]{64})\s*$')
    if ($certificateMatches.Count -ne 1) { throw 'Expected exactly one verified APK signer certificate.' }
    $certificateSha256 = $certificateMatches[0].Groups[1].Value.ToLowerInvariant()
    $expectedFingerprint = ([string]$env:REMOTEVOICE_RELEASE_CERT_SHA256).Replace(':', '').Trim().ToLowerInvariant()
    if ($expectedFingerprint -notmatch '^[0-9a-f]{64}$' -or $certificateSha256 -ne $expectedFingerprint) {
        throw 'APK signer fingerprint does not match the configured release identity.'
    }
    $artifactName = 'RemoteVoice-Android-signed-release-candidate.apk'
    $artifactKind = 'signed-release-candidate'
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$destination = Join-Path $OutputDirectory $artifactName
Copy-Item -LiteralPath $sourceApk -Destination $destination -Force
$apkSha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    version = 1
    kind = $artifactKind
    artifact = $artifactName
    installable = !$Unsigned
    apk_sha256 = $apkSha256
    certificate_sha256 = $certificateSha256
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'signing-manifest.json') -Encoding utf8
Write-Output "APK SHA-256: $apkSha256"
if ($certificateSha256) { Write-Output "Signer certificate SHA-256: $certificateSha256" }
