param([switch]$SkipChecks)
$ErrorActionPreference = 'Stop'
$androidRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolsRoot = Join-Path $androidRoot '.tools'
$gradle = Join-Path $toolsRoot 'gradle-8.13/bin/gradle.bat'
if (!(Test-Path -LiteralPath $gradle)) { throw 'Run scripts/setup-tools.ps1 first (JDK 17 required).' }
$env:ANDROID_HOME = Join-Path $toolsRoot 'sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:GRADLE_USER_HOME = Join-Path $toolsRoot 'gradle-user-home'
$env:ANDROID_USER_HOME = Join-Path $toolsRoot 'android-user-home'
Get-Command javac -ErrorAction Stop | Out-Null
if (!$env:JAVA_HOME) {
    $javaSettings = & java -XshowSettings:properties -version 2>&1 | Out-String
    $homeMatch = [regex]::Match($javaSettings, '(?m)^\s*java\.home\s*=\s*(.+)$')
    if (!$homeMatch.Success) { throw 'Cannot locate JDK: set JAVA_HOME.' }
    $env:JAVA_HOME = $homeMatch.Groups[1].Value.Trim()
}
if (!(Test-Path -LiteralPath (Join-Path $env:JAVA_HOME 'bin/javac.exe'))) {
    throw 'Set JAVA_HOME to an installed JDK 17.'
}
Push-Location $androidRoot
try {
    $tasks = @(':app:assembleDebug')
    if (!$SkipChecks) { $tasks += @(':app:testDebugUnitTest', ':app:lintDebug') }
    & $gradle --no-daemon --console=plain @tasks
    if ($LASTEXITCODE -ne 0) { throw "Gradle failed ($LASTEXITCODE)" }
    New-Item -ItemType Directory -Path 'preview' -Force | Out-Null
    Copy-Item -LiteralPath 'app/build/outputs/apk/debug/app-debug.apk' -Destination 'preview/RemonteVoice-Android-debug.apk' -Force
    Get-FileHash -LiteralPath 'preview/RemonteVoice-Android-debug.apk' -Algorithm SHA256
} finally { Pop-Location }
