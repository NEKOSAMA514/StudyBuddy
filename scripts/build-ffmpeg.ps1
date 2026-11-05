$ErrorActionPreference = 'Stop'
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$ffmpegBin = & (Join-Path $PSScriptRoot 'get-ffmpeg-bin.ps1') $pythonPath
& $pythonPath -X utf8 -B (Join-Path $PSScriptRoot 'ffmpeg/build.py') --reference (Join-Path $ffmpegBin 'ffmpeg.exe')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
