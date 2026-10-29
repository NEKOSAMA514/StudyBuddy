$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$toolRoot = Join-Path $studyRoot 'tools/ffmpeg'
$archive = Join-Path $toolRoot 'ffmpeg-9.0.2-essentials_build.zip'
$expectedHash = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
New-Item -ItemType Directory -Force -Path (Join-Path $toolRoot 'bin') | Out-Null
if (-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip' -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'FFmpeg 下载校验失败，请检查 tools/ffmpeg 中的压缩包。'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
try {
    foreach ($name in @('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')) {
        $entry = $zip.GetEntry("ffmpeg-9.0.2-essentials_build/bin/$name")
        if (-not $entry) { throw "FFmpeg 压缩包缺少 $name。" }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $toolRoot "bin/$name"), $true)
    }
    foreach ($name in @('LICENSE', 'README.txt')) {
        $entry = $zip.GetEntry("ffmpeg-9.0.2-essentials_build/$name")
        if ($entry) { [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $toolRoot $name), $true) }
    }
} finally { $zip.Dispose() }
Write-Host 'FFmpeg 9.0.2 已安装到项目 tools/ffmpeg；启动和测试脚本会自动使用。'
