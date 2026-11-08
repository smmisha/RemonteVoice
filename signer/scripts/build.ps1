param([switch]$DebugBuild)
$ErrorActionPreference = 'Stop'
$rvSignerRoot = Split-Path -Parent $PSScriptRoot
$rvAppRoot = Join-Path $rvSignerRoot 'app'
$rvToolRoot = Join-Path $rvSignerRoot '.tools'
if (Test-Path -LiteralPath (Join-Path $rvToolRoot 'cargo\bin\cargo.exe')) {
    $env:CARGO_HOME = Join-Path $rvToolRoot 'cargo'
    $env:RUSTUP_HOME = Join-Path $rvToolRoot 'rustup'
    $env:PATH = (Join-Path $env:CARGO_HOME 'bin') + ';' + $env:PATH
}
$rvVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path -LiteralPath $rvVsWhere)) { throw 'Install Visual Studio C++ Build Tools before building.' }
$rvVsRoot = & $rvVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$rvVsRoot) { throw 'MSVC x64 toolchain not found.' }
Import-Module (Join-Path $rvVsRoot 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $rvVsRoot -SkipAutomaticLocation -Arch amd64 -HostArch amd64 | Out-Null
$rvCmake = Join-Path $rvVsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'
if (Test-Path -LiteralPath $rvCmake) { $env:PATH = $rvCmake + ';' + $env:PATH }
$rvGitPerl = Join-Path $env:ProgramFiles 'Git\usr\bin'
if (Test-Path -LiteralPath (Join-Path $rvGitPerl 'perl.exe')) { $env:PATH += ';' + $rvGitPerl }
$env:CARGO_BUILD_JOBS = '2'
Push-Location $rvAppRoot
try {
    npm.cmd run build
    if ($LASTEXITCODE) { throw 'Frontend build failed.' }
    if ($DebugBuild) { cargo build --manifest-path src-tauri/Cargo.toml --locked }
    else { cargo build --manifest-path src-tauri/Cargo.toml --locked --release }
    if ($LASTEXITCODE) { throw 'Rust build failed.' }
} finally { Pop-Location }
