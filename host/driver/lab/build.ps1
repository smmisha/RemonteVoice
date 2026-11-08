$ErrorActionPreference='Stop'
$rvRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$rvOutput=Join-Path $rvRoot 'host/build/driver-guest-helper'
$rvCompiler='C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if(!(Test-Path -LiteralPath $rvCompiler)){throw 'Existing Windows .NET Framework compiler required; no installation attempted.'}
New-Item -ItemType Directory -Path $rvOutput -Force|Out-Null
& $rvCompiler /nologo /target:winexe /platform:x64 /optimize+ /warn:4 /warnaserror+ /codepage:65001 `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Management.dll `
    /reference:System.Web.Extensions.dll "/out:$rvOutput/GuestDriverLab.exe" `
    (Join-Path $PSScriptRoot 'GuestDriverLab.cs') (Join-Path $PSScriptRoot 'LabNative.cs')
if($LASTEXITCODE -ne 0){throw 'Guest lab helper compile failed'}
$rvInputs=@{}
foreach($rvName in @('GuestDriverLab.cs','LabNative.cs','build.ps1')){
    $rvInputs['host/driver/lab/'+$rvName]=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $rvName) -Algorithm SHA256).Hash.ToLowerInvariant()
}
@{created_utc=[DateTime]::UtcNow.ToString('o'); artifact='host/build/driver-guest-helper/GuestDriverLab.exe'; sha256=(Get-FileHash -LiteralPath (Join-Path $rvOutput 'GuestDriverLab.exe') -Algorithm SHA256).Hash.ToLowerInvariant();inputs=$rvInputs;scope='Private guest-only GUI helper; not executed by build'}|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $rvOutput 'build-manifest.json') -Encoding utf8
