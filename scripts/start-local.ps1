param(
    [ValidateRange(1024, 65535)][int]$Port = 18000,
    [switch]$Reload
)
$ErrorActionPreference = 'Stop'
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $env:CONDA_PREFIX) { throw '请先执行 conda activate studybuddy。' }
$localTools = Join-Path $studyRoot 'tools/ffmpeg/bin'
if (Test-Path -LiteralPath $localTools) { $env:PATH = "$localTools;$env:PATH" }
$pythonPath = (Get-Command python -ErrorAction Stop).Source
& $pythonPath -c "import sys; assert sys.version_info[:2] == (3, 12), 'StudyBuddy requires Python 3.12'"
if ($LASTEXITCODE -ne 0) { throw '请使用 studybuddy 的 Python 3.12 环境。' }
foreach ($tool in @('ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "缺少 $tool，请在 Conda 环境安装 ffmpeg。" }
}
Push-Location $studyRoot
try {
    # 沿用已有非路径设置，不打印凭据；原有私有 JSON 仍由后端加载。
    foreach ($name in @('.env.example', '.env')) {
        if (Test-Path -LiteralPath $name) {
            foreach ($line in Get-Content -LiteralPath $name -Encoding UTF8) {
                if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
                    $key, $value = $Matches[1], $Matches[2].Trim()
                    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                        $value = $value.Substring(1, $value.Length - 2)
                    }
                    [Environment]::SetEnvironmentVariable($key, $value, 'Process')
                }
            }
        }
    }
    $env:DATA_DIR = Join-Path $studyRoot 'data'
    $env:MODEL_DIR = Join-Path $studyRoot 'models'
    $env:LOG_DIR = Join-Path $studyRoot 'log'
    $env:STATIC_DIR = Join-Path $studyRoot 'frontend/dist'
    $env:HF_HOME = $env:MODEL_DIR
    $env:PYTHONUTF8 = '1'
    if (-not (Test-Path -LiteralPath (Join-Path $env:STATIC_DIR 'index.html'))) {
        throw '请先进入 frontend 运行 npm ci 和 npm run build。'
    }
    $arguments = @('-m', 'uvicorn', 'app.main:create_app', '--factory', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', "$Port")
    if ($Reload) { $arguments += @('--reload', '--reload-dir', 'backend/app') }
    else { $arguments += @('--workers', '1') }
    Write-Host "StudyBuddy: http://localhost:$Port  (Ctrl+C 停止)"
    & $pythonPath @arguments
    exit $LASTEXITCODE
} finally { Pop-Location }
