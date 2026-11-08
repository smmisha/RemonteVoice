# Offline private-lab signing only. No certificate is installed/trusted on the host.
param([string]$SourceDirectory='host/build/driver-windows10-lab-startupfix',
      [string]$OutputDirectory='host/build/driver-vm-lab',
      [string]$PrivateDirectory='.harness/evidence/integrated-audio/private-lab-signing')
$ErrorActionPreference='Stop'
$rvRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$rvSource=[IO.Path]::GetFullPath((Join-Path $rvRoot $SourceDirectory))
$rvHelper=Join-Path $rvRoot 'host/build/driver-guest-helper'
$rvOutput=[IO.Path]::GetFullPath((Join-Path $rvRoot $OutputDirectory))
$rvPrivate=[IO.Path]::GetFullPath((Join-Path $rvRoot $PrivateDirectory))
$rvBuildPrefix=[IO.Path]::GetFullPath((Join-Path $rvRoot 'host/build'))+[IO.Path]::DirectorySeparatorChar
$rvEvidencePrefix=[IO.Path]::GetFullPath((Join-Path $rvRoot '.harness/evidence/integrated-audio'))+[IO.Path]::DirectorySeparatorChar
if(-not $rvSource.StartsWith($rvBuildPrefix,[StringComparison]::OrdinalIgnoreCase) -or -not $rvOutput.StartsWith($rvBuildPrefix,[StringComparison]::OrdinalIgnoreCase) -or -not $rvPrivate.StartsWith($rvEvidencePrefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Lab paths must stay inside ignored project build/evidence directories'}
$rvVm='C:/Users/User/Projects/Delo/app/test-output/windows10-vm/vmware/Delo-Win10-22H2.vmx'
$rvSignTool=Join-Path $rvRoot 'host/driver/.tools/microsoft.windows.sdk.cpp.10.0.26100.6584/c/bin/10.0.26100.0/x64/signtool.exe'
$rvInf2Cat=Join-Path $rvRoot 'host/driver/.tools/microsoft.windows.wdk.x64.10.0.26100.6584/c/bin/10.0.26100.0/x86/Inf2Cat.exe'
function Get-RvHash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
if((Test-Path -LiteralPath $rvOutput) -or (Test-Path -LiteralPath $rvPrivate)){throw 'Signing output/key directory already exists. Inspect previous effects before retrying.'}
$rvLab=Get-Content -LiteralPath (Join-Path $rvSource 'lab-package-manifest.json') -Raw|ConvertFrom-Json
foreach($rvProperty in $rvLab.files.PSObject.Properties){if((Get-RvHash (Join-Path $rvSource $rvProperty.Name)) -ne $rvProperty.Value){throw 'Stale lab package'}}
$rvBuilt=Get-Content -LiteralPath (Join-Path $rvHelper 'build-manifest.json') -Raw|ConvertFrom-Json
foreach($rvProperty in $rvBuilt.inputs.PSObject.Properties){if((Get-RvHash (Join-Path $rvRoot $rvProperty.Name)) -ne $rvProperty.Value){throw 'Stale guest helper source'}}
if((Get-RvHash (Join-Path $rvRoot $rvBuilt.artifact)) -ne $rvBuilt.sha256){throw 'Stale guest helper binary'}
$rvVmText=Get-Content -LiteralPath $rvVm -Raw
$rvUuid=[regex]::Match($rvVmText,'(?m)^uuid\.bios\s*=\s*"([0-9a-fA-F -]+)"').Groups[1].Value
if(($rvUuid -replace '[^0-9a-fA-F]','').Length -ne 32){throw 'Expected primary VM BIOS identity unavailable'}
foreach($rvTool in @($rvSignTool,$rvInf2Cat)){if(!(Test-Path -LiteralPath $rvTool)){throw 'Approved local signing/catalog tool missing'}}
New-Item -ItemType Directory -Path $rvPrivate|Out-Null
$rvAcl=Get-Acl -LiteralPath $rvPrivate
$rvAcl.SetAccessRuleProtection($true,$false)
$rvSid=[Security.Principal.WindowsIdentity]::GetCurrent().User
foreach($rvIdentity in @($rvSid,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'))){
    $rvRule=[Security.AccessControl.FileSystemAccessRule]::new($rvIdentity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $rvAcl.AddAccessRule($rvRule)
}
Set-Acl -LiteralPath $rvPrivate -AclObject $rvAcl
New-Item -ItemType Directory -Path $rvOutput|Out-Null
$rvRsa=[Security.Cryptography.RSA]::Create(3072)
$rvCertificate=$null
try{
    $rvRequest=[Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=RemoteVoice Private VM Development Only',$rvRsa,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $rvRequest.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false,$false,0,$true))
    $rvRequest.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new([Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,$true))
    $rvUsage=[Security.Cryptography.OidCollection]::new()
    [void]$rvUsage.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
    $rvRequest.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($rvUsage,$false))
    $rvCertificate=$rvRequest.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-7),[DateTimeOffset]::UtcNow.AddDays(30))
    $rvPassword=[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $rvPfx=Join-Path $rvPrivate 'development.pfx'
    [IO.File]::WriteAllBytes($rvPfx,$rvCertificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx,$rvPassword))
    $rvEntropy=[Text.Encoding]::UTF8.GetBytes('RemoteVoice-private-lab-v1')
    [IO.File]::WriteAllBytes((Join-Path $rvPrivate 'password.dpapi'),[Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($rvPassword),$rvEntropy,[Security.Cryptography.DataProtectionScope]::CurrentUser))
    [IO.File]::WriteAllBytes((Join-Path $rvOutput 'RemoteVoiceLab.cer'),$rvCertificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
    foreach($rvName in @('RemoteVoiceAudio.sys','RemoteVoiceAudio.inf')){[IO.File]::WriteAllBytes((Join-Path $rvOutput $rvName),[IO.File]::ReadAllBytes((Join-Path $rvSource $rvName)))}
    [IO.File]::WriteAllBytes((Join-Path $rvOutput 'GuestDriverLab.exe'),[IO.File]::ReadAllBytes((Join-Path $rvHelper 'GuestDriverLab.exe')))
    foreach($rvName in @('RemoteVoiceAudio.sys','GuestDriverLab.exe')){
        & $rvSignTool sign /fd SHA256 /f $rvPfx /p $rvPassword (Join-Path $rvOutput $rvName) *> (Join-Path $rvPrivate ($rvName+'.sign.log'))
        if($LASTEXITCODE -ne 0){throw ('Offline signing failed for '+$rvName+'; inspect private log without printing arguments')}
    }
    # The catalog MUST hash the already signed SYS, not the original unsigned SYS.
    & $rvInf2Cat "/driver:$rvOutput" /os:10_VB_X64 /verbose *> (Join-Path $rvOutput 'inf2cat.log')
    if($LASTEXITCODE -ne 0){throw 'Signed-SYS catalog generation failed'}
    & $rvSignTool sign /fd SHA256 /f $rvPfx /p $rvPassword (Join-Path $rvOutput 'RemoteVoiceAudio.cat') *> (Join-Path $rvPrivate 'catalog.sign.log')
    if($LASTEXITCODE -ne 0){throw 'Offline catalog signing failed'}
    $rvFiles=[ordered]@{}
    foreach($rvName in @('RemoteVoiceAudio.sys','RemoteVoiceAudio.inf','RemoteVoiceAudio.cat','RemoteVoiceLab.cer','GuestDriverLab.exe')){$rvFiles[$rvName]=Get-RvHash (Join-Path $rvOutput $rvName)}
    $rvConfig=[ordered]@{vm_bios_uuid=$rvUuid;guest_build='19045';certificate_thumbprint=$rvCertificate.Thumbprint;files=$rvFiles}
    $rvConfig|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $rvOutput 'lab-config.json') -Encoding utf8
    $rvSignatures=@()
    foreach($rvName in @('RemoteVoiceAudio.sys','RemoteVoiceAudio.cat','GuestDriverLab.exe')){
        $rvSignature=Get-AuthenticodeSignature -LiteralPath (Join-Path $rvOutput $rvName)
        if($rvSignature.SignerCertificate.Thumbprint -ne $rvCertificate.Thumbprint -or $rvSignature.Status -in @('NotSigned','HashMismatch')){throw ('Signed-file identity check failed: '+$rvName)}
        $rvSignatures += [ordered]@{file=$rvName;status=[string]$rvSignature.Status;signer_thumbprint=$rvSignature.SignerCertificate.Thumbprint}
    }
    [ordered]@{created_utc=[DateTime]::UtcNow.ToString('o');source_manifest_sha256=Get-RvHash (Join-Path $rvSource 'lab-package-manifest.json');helper_manifest_sha256=Get-RvHash (Join-Path $rvHelper 'build-manifest.json');certificate_thumbprint=$rvCertificate.Thumbprint;certificate_not_after=$rvCertificate.NotAfter.ToUniversalTime().ToString('o');private_key_directory=$PrivateDirectory;private_key_in_payload=$false;host_trust_store_changed=$false;catalog_generated_after_sys_signature=$true;guest_installed=$false;files=$rvFiles;signatures=$rvSignatures}|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $rvOutput 'signing-manifest.json') -Encoding utf8
    'Private lab payload signed. Host trust unchanged; private key excluded from payload.'
}finally{
    $rvPassword=$null
    if($null -ne $rvCertificate){$rvCertificate.Dispose()}
    $rvRsa.Dispose()
}
