param()
$ErrorActionPreference = 'Stop'
$androidRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolsRoot = Join-Path $androidRoot '.tools'
$downloads = Join-Path $toolsRoot 'downloads'
New-Item -ItemType Directory -Path $downloads -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-VerifiedArchive([string]$Url, [string]$Name, [string]$Hash, [string]$Algorithm) {
    $dest = Join-Path $downloads $Name
    if (!(Test-Path -LiteralPath $dest) -or (Get-FileHash -LiteralPath $dest -Algorithm $Algorithm).Hash -ne $Hash) {
        Write-Host "Downloading $Name"
        Invoke-WebRequest -Uri $Url -OutFile $dest -TimeoutSec 300
    }
    if ((Get-FileHash -LiteralPath $dest -Algorithm $Algorithm).Hash -ne $Hash) { throw "Checksum mismatch: $Name" }
    return $dest
}

function Expand-Checked([string]$Archive, [string]$Destination) {
    $dest = [IO.Path]::GetFullPath($Destination)
    if (!$dest.StartsWith($toolsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid extraction directory' }
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $dest $entry.FullName))
            if (!$target.StartsWith($dest + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe ZIP entry' }
        }
    } finally { $zip.Dispose() }
    Expand-Archive -LiteralPath $Archive -DestinationPath $dest -Force
}

$gradleHash = '20f1b1176237254a6fc204d8434196fa11a4cfb387567519c61556e8710aed78'
if (!(Test-Path -LiteralPath (Join-Path $toolsRoot 'gradle-8.13/bin/gradle.bat'))) {
    $archive = Get-VerifiedArchive 'https://services.gradle.org/distributions/gradle-8.13-bin.zip' 'gradle-8.13-bin.zip' $gradleHash 'SHA256'
    Expand-Checked $archive (Join-Path $toolsRoot 'gradle-stage')
    Copy-Item -LiteralPath (Join-Path $toolsRoot 'gradle-stage/gradle-8.13') -Destination $toolsRoot -Recurse -Force
}

$catalog = Join-Path $downloads 'repository.xml'
if (!(Test-Path -LiteralPath $catalog)) {
    Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/repository2-3.xml' -OutFile $catalog -TimeoutSec 60
}
[xml]$repo = Get-Content -LiteralPath $catalog -Raw
$sdkRoot = Join-Path $toolsRoot 'sdk'
$packages = @(
    @{ Id='platforms;android-36'; Destination='platforms/android-36' },
    @{ Id='build-tools;35.0.0'; Destination='build-tools/35.0.0' },
    @{ Id='platform-tools'; Destination='platform-tools' }
)
foreach ($item in $packages) {
    $destination = Join-Path $sdkRoot $item.Destination
    if (Test-Path -LiteralPath (Join-Path $destination 'source.properties')) { continue }
    $pkg = @($repo.SelectNodes('//*[local-name()="remotePackage"]') | Where-Object { $_.path -eq $item.Id })[0]
    if (!$pkg) { throw "Package unavailable: $($item.Id)" }
    $archiveInfo = @($pkg.archives.archive | Where-Object { !$_.('host-os') -or $_.('host-os') -eq 'windows' })[0].complete
    $name = [string]$archiveInfo.url
    $hash = $archiveInfo.checksum.InnerText
    $algorithm = if ($archiveInfo.checksum.type -eq 'sha256') { 'SHA256' } else { 'SHA1' }
    $archive = Get-VerifiedArchive ("https://dl.google.com/android/repository/" + $name) $name $hash $algorithm
    $stage = Join-Path $toolsRoot ('stage-' + $item.Id.Replace(';','-'))
    Expand-Checked $archive $stage
    $source = @(Get-ChildItem -LiteralPath $stage -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'source.properties') })
    if ($source.Count -ne 1) { throw "Unexpected package layout: $name" }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Get-ChildItem -LiteralPath $source[0].FullName -Force | Copy-Item -Destination $destination -Recurse -Force
    Write-Output "Prepared $($item.Id)"
}
Write-Output "Project-local SDK: $sdkRoot"
