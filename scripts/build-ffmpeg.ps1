$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& python -X utf8 -B (Join-Path $root 'scripts/ffmpeg/build.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
