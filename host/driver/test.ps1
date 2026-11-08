$ErrorActionPreference='Stop'
$rvProject=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$rvOutput=Join-Path $rvProject 'host/build/driver-bridge-tests'
$rvVswhere='C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe'
$rvInstall=& $rvVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if(!$rvInstall) { throw 'Existing MSVC required; no installation attempted.' }
$rvTools=Get-ChildItem -LiteralPath (Join-Path $rvInstall 'VC/Tools/MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
$rvSdkRoot='C:/Program Files (x86)/Windows Kits/10'
$rvSdk=Get-ChildItem -LiteralPath (Join-Path $rvSdkRoot 'Include') -Directory |
    Where-Object {Test-Path -LiteralPath (Join-Path $_.FullName 'um/Windows.h')} | Sort-Object Name -Descending | Select-Object -First 1
if(!$rvSdk) { throw 'Existing user-mode Windows SDK required.' }
$rvOldInclude=$env:INCLUDE; $rvOldLib=$env:LIB
New-Item -ItemType Directory -Path $rvOutput -Force | Out-Null
try {
    $env:INCLUDE=@((Join-Path $rvTools.FullName 'include'),(Join-Path $rvSdk.FullName 'ucrt'),(Join-Path $rvSdk.FullName 'shared'),(Join-Path $rvSdk.FullName 'um')) -join ';'
    $env:LIB=@((Join-Path $rvTools.FullName 'lib/x64'),(Join-Path $rvSdkRoot "Lib/$($rvSdk.Name)/ucrt/x64"),(Join-Path $rvSdkRoot "Lib/$($rvSdk.Name)/um/x64")) -join ';'
    & (Join-Path $rvTools.FullName 'bin/Hostx64/x64/cl.exe') /nologo /std:c++17 /W4 /WX /O2 /EHsc /MT "/I$PSScriptRoot/tests/shim" "/I$PSScriptRoot/src/Main" "/I$PSScriptRoot/src/Inc" (Join-Path $PSScriptRoot 'tests/bridge_tests.cpp') (Join-Path $PSScriptRoot 'src/Main/rv_pcm_bridge.cpp') "/Fo$rvOutput/" /link "/OUT:$rvOutput/bridge_tests.exe"
    if($LASTEXITCODE -ne 0) { throw 'Bridge test compile failed.' }
    & (Join-Path $rvOutput 'bridge_tests.exe')
    if($LASTEXITCODE -ne 0) { throw 'Bridge tests failed.' }
    $rvDllOutput=Join-Path $rvOutput 'dll'
    New-Item -ItemType Directory -Path $rvDllOutput -Force | Out-Null
    & (Join-Path $rvTools.FullName 'bin/Hostx64/x64/cl.exe') /nologo /std:c++17 /W4 /WX /O2 /EHsc /MT /LD "/I$PSScriptRoot/tests/shim" "/I$PSScriptRoot/src/Main" "/I$PSScriptRoot/src/Inc" (Join-Path $PSScriptRoot 'tests/bridge_test_dll.cpp') (Join-Path $PSScriptRoot 'src/Main/rv_pcm_bridge.cpp') "/Fo$rvDllOutput/" /link "/OUT:$rvOutput/bridge_test.dll" "/IMPLIB:$rvDllOutput/bridge_test.lib"
    if($LASTEXITCODE -ne 0) { throw 'Python/native bridge test DLL build failed.' }
    $rvInputs=@{}
    foreach($rvInput in @('test.ps1','tests/bridge_tests.cpp','tests/bridge_test_dll.cpp','tests/shim/portcls.h','tests/shim/ntifs.h','src/Main/rv_pcm_bridge.cpp','src/Main/rv_pcm_bridge.h','src/Inc/rv_mic_protocol.h')) {
        $rvInputs['host/driver/'+$rvInput]=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $rvInput) -Algorithm SHA256).Hash.ToLower()
    }
    @{executed_at=[DateTime]::UtcNow.ToString('o'); artifact='host/build/driver-bridge-tests/bridge_test.dll'; sha256=(Get-FileHash -LiteralPath (Join-Path $rvOutput 'bridge_test.dll') -Algorithm SHA256).Hash.ToLower(); inputs=$rvInputs; kernel_test=$false} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $rvOutput 'test-build-manifest.json') -Encoding utf8
} finally { $env:INCLUDE=$rvOldInclude; $env:LIB=$rvOldLib }
