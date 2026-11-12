param([string]$OutputDirectory)
$ErrorActionPreference='Stop'
$rvProject=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if(!$OutputDirectory) { $OutputDirectory=Join-Path $rvProject 'host/build/native-audio-prototype' }
$rvOutput=[IO.Path]::GetFullPath($OutputDirectory)
if(!$rvOutput.StartsWith($rvProject+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output directory must stay inside the project.'
}
$rvVswhere='C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe'
if(!(Test-Path -LiteralPath $rvVswhere)) { throw 'Existing Visual Studio Build Tools locator not found.' }
$rvInstall=& $rvVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if(!$rvInstall) { throw 'Existing x64 C++ compiler not found; no components were installed.' }
$rvTools=Get-ChildItem -LiteralPath (Join-Path $rvInstall 'VC/Tools/MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
$rvCompiler=Join-Path $rvTools.FullName 'bin/Hostx64/x64/cl.exe'
$rvSdkRoot='C:/Program Files (x86)/Windows Kits/10'
$rvSdk=Get-ChildItem -LiteralPath (Join-Path $rvSdkRoot 'Include') -Directory |
    Where-Object {Test-Path -LiteralPath (Join-Path $_.FullName 'um/Windows.h')} |
    Sort-Object Name -Descending | Select-Object -First 1
if(!$rvSdk -or !(Test-Path -LiteralPath $rvCompiler)) { throw 'Compiler/Windows SDK not available.' }
$rvOldInclude=$env:INCLUDE; $rvOldLib=$env:LIB
New-Item -ItemType Directory -Path $rvOutput -Force | Out-Null
try {
    $env:INCLUDE=@((Join-Path $rvTools.FullName 'include'),(Join-Path $rvSdk.FullName 'ucrt'),(Join-Path $rvSdk.FullName 'shared'),(Join-Path $rvSdk.FullName 'um')) -join ';'
    $env:LIB=@((Join-Path $rvTools.FullName 'lib/x64'),(Join-Path $rvSdkRoot "Lib/$($rvSdk.Name)/ucrt/x64"),(Join-Path $rvSdkRoot "Lib/$($rvSdk.Name)/um/x64")) -join ';'
    & $rvCompiler /nologo /std:c++17 /W4 /WX /O2 /EHsc /LD /MT (Join-Path $PSScriptRoot 'duplex_buffer.cpp') "/Fo$rvOutput/duplex_buffer.obj" /link "/OUT:$rvOutput/remontevoice_audio_buffer.dll" "/IMPLIB:$rvOutput/remontevoice_audio_buffer.lib"
    if($LASTEXITCODE -ne 0) { throw 'Native audio prototype build failed.' }
    Write-Output (Join-Path $rvOutput 'remontevoice_audio_buffer.dll')
} finally { $env:INCLUDE=$rvOldInclude; $env:LIB=$rvOldLib }
