# Compile private synthetic VM tests using the already approved local toolchain.
param([ValidateSet('pcm_probe','loopback_probe','acl_probe')][string[]]$Names=@('pcm_probe','loopback_probe','acl_probe'))
$ErrorActionPreference='Stop'
$rvRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$rvVc='C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Tools/MSVC/14.44.35207'
$rvSdk=Join-Path $rvRoot 'host/driver/.tools/microsoft.windows.sdk.cpp.10.0.26100.6584/c'
$rvLib=Join-Path $rvRoot 'host/driver/.tools/microsoft.windows.sdk.cpp.x64.10.0.26100.6584/c'
$rvOutput=Join-Path $rvRoot 'host/build/driver-guest-helper'
$rvManifest=[ordered]@{created_utc=[DateTime]::UtcNow.ToString('o');executed=$false;files=@{}}
foreach($rvName in $Names){
    $rvSource=Join-Path $PSScriptRoot ($rvName+'.cpp')
    & "$rvVc/bin/Hostx64/x64/cl.exe" /nologo /std:c++17 /EHsc /MT /W3 /WX /DUNICODE /D_UNICODE "/I$rvVc/include" "/I$rvSdk/Include/10.0.26100.0/um" "/I$rvSdk/Include/10.0.26100.0/shared" "/I$rvSdk/Include/10.0.26100.0/ucrt" $rvSource "/Fo$rvOutput/$rvName.obj" "/Fe$rvOutput/$rvName.exe" /link "/LIBPATH:$rvVc/lib/x64" "/LIBPATH:$rvLib/um/x64" "/LIBPATH:$rvLib/ucrt/x64" ole32.lib setupapi.lib uuid.lib propsys.lib cfgmgr32.lib advapi32.lib
    if($LASTEXITCODE -ne 0){throw ('Probe build failed: '+$rvName)}
    $rvManifest.files[$rvName]=@{source_sha256=(Get-FileHash $rvSource).Hash.ToLowerInvariant();binary_sha256=(Get-FileHash "$rvOutput/$rvName.exe").Hash.ToLowerInvariant()}
}
$rvManifest|ConvertTo-Json -Depth 5|Set-Content "$rvOutput/probe-build-manifest.json" -Encoding utf8
