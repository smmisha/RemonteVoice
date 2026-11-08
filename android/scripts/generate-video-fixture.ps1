$ErrorActionPreference='Stop'
# Optional developer fixture. Uses an already installed FFmpeg; never downloads one.
$rvFfmpeg=(Get-Command ffmpeg -ErrorAction Stop).Source
$rvAndroid=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$rvAssets=Join-Path $rvAndroid 'app/src/debug/assets'
New-Item -ItemType Directory -Path $rvAssets -Force|Out-Null
& $rvFfmpeg -hide_banner -loglevel error -f lavfi -i 'smptebars=size=1280x800:rate=30' -frames:v 90 -an -c:v libx264 -preset ultrafast -tune zerolatency -threads 2 -pix_fmt yuv420p -profile:v baseline -b:v 4M -g 30 -bf 0 -x264-params 'aud=1:repeat-headers=1' -f h264 -y (Join-Path $rvAssets 'synthetic-video.h264')
if($LASTEXITCODE -ne 0){throw 'Synthetic video fixture generation failed'}
