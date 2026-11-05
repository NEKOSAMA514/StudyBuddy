param([Parameter(Mandatory = $true)][string]$PythonPath)
$ErrorActionPreference = 'Stop'
$resolveBin = @'
from contextlib import redirect_stdout
from importlib.util import find_spec
from io import StringIO
import json

from portable_ffmpeg import get_ffmpeg

if find_spec('portable_ffmpeg_full') is None:
    raise ModuleNotFoundError('The portable-ffmpeg-full wheel is required.')

with redirect_stdout(StringIO()):
    ffmpeg, _ = get_ffmpeg()
print(json.dumps({'directory': str(ffmpeg.parent)}))
'@

# Serialize first extraction for callers that share a Python environment.
$mutex = [Threading.Mutex]::new($false, 'Local\StudyBuddy.FFmpegWheel')
try {
    try { $null = $mutex.WaitOne() }
    catch [Threading.AbandonedMutexException] { }
    try {
        $result = & $PythonPath -I -B -c $resolveBin
        if ($LASTEXITCODE -ne 0) { throw 'FFmpeg setup failed. Install portable-ffmpeg-full in the selected Python environment.' }
        (ConvertFrom-Json -InputObject $result).directory
    } finally { $mutex.ReleaseMutex() }
} finally { $mutex.Dispose() }
