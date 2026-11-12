# Re-sign a rebuilt helper with the same private lab key; SYS/INF/CAT stay fixed.
$ErrorActionPreference='Stop'
$rvRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$rvPrivate=Join-Path $rvRoot '.harness/evidence/integrated-audio/private-lab-signing'
$rvOutput=Join-Path $rvRoot 'host/build/driver-vm-lab'
$rvHelper=Join-Path $rvRoot 'host/build/driver-guest-helper'
$rvSignTool=Join-Path $rvRoot 'host/driver/.tools/microsoft.windows.sdk.cpp.10.0.26100.6584/c/bin/10.0.26100.0/x64/signtool.exe'
function Get-RvHash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
$rvConfig=Get-Content -LiteralPath (Join-Path $rvOutput 'lab-config.json') -Raw|ConvertFrom-Json
foreach($rvEntry in $rvConfig.files.PSObject.Properties){if((Get-RvHash (Join-Path $rvOutput $rvEntry.Name)) -ne $rvEntry.Value){throw 'Existing signed payload changed; inspect before refresh'}}
$rvManifest=Get-Content -LiteralPath (Join-Path $rvHelper 'build-manifest.json') -Raw|ConvertFrom-Json
foreach($rvEntry in $rvManifest.inputs.PSObject.Properties){if((Get-RvHash (Join-Path $rvRoot $rvEntry.Name)) -ne $rvEntry.Value){throw 'Helper source/build mismatch'}}
if((Get-RvHash (Join-Path $rvRoot $rvManifest.artifact)) -ne $rvManifest.sha256){throw 'Helper artifact mismatch'}
$rvTemporary=Join-Path $rvPrivate 'GuestDriverLab-api.exe'
if(Test-Path -LiteralPath $rvTemporary){throw 'Inspect earlier refresh effects before retrying'}
[IO.File]::WriteAllBytes($rvTemporary,[IO.File]::ReadAllBytes((Join-Path $rvHelper 'GuestDriverLab.exe')))
$rvProtected=[IO.File]::ReadAllBytes((Join-Path $rvPrivate 'password.dpapi'))
$rvEntropy=[Text.Encoding]::UTF8.GetBytes('RemonteVoice-private-lab-v1')
$rvPassword=[Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect($rvProtected,$rvEntropy,[Security.Cryptography.DataProtectionScope]::CurrentUser))
try{
    & $rvSignTool sign /fd SHA256 /f (Join-Path $rvPrivate 'development.pfx') /p $rvPassword $rvTemporary *> (Join-Path $rvPrivate 'helper-api.sign.log')
    if($LASTEXITCODE -ne 0){throw 'Helper re-sign failed; no payload replacement attempted'}
    $rvSignature=Get-AuthenticodeSignature -LiteralPath $rvTemporary
    if($rvSignature.SignerCertificate.Thumbprint -ne $rvConfig.certificate_thumbprint -or $rvSignature.Status -in @('NotSigned','HashMismatch')){throw 'Helper signer mismatch'}
    Copy-Item -LiteralPath $rvTemporary -Destination (Join-Path $rvOutput 'GuestDriverLab.exe') -Force
    $rvConfig.files.'GuestDriverLab.exe'=Get-RvHash (Join-Path $rvOutput 'GuestDriverLab.exe')
    $rvConfig|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $rvOutput 'lab-config.json') -Encoding utf8
    $rvReceipt=Get-Content -LiteralPath (Join-Path $rvOutput 'signing-manifest.json') -Raw|ConvertFrom-Json
    $rvReceipt.helper_manifest_sha256=Get-RvHash (Join-Path $rvHelper 'build-manifest.json')
    $rvReceipt.files.'GuestDriverLab.exe'=$rvConfig.files.'GuestDriverLab.exe'
    $rvReceipt|Add-Member -NotePropertyName helper_refreshed_utc -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
    $rvReceipt|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $rvOutput 'signing-manifest.json') -Encoding utf8
    $rvShare=Join-Path $rvRoot 'host/build/driver-vm-lab-share'
    foreach($rvName in @('GuestDriverLab.exe','lab-config.json')){Copy-Item -LiteralPath (Join-Path $rvOutput $rvName) -Destination (Join-Path $rvShare $rvName) -Force}
    'Signed helper refreshed; private key retained only in protected local directory.'
}finally{$rvPassword=$null}
