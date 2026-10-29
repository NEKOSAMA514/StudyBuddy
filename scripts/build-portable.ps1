param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$')][string]$Version,
    [string]$CondaExe,
    [string]$CondaExplicit
)
$ErrorActionPreference = 'Stop'
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or -not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'This builder requires Windows x64 and 64-bit PowerShell.'
}
if (-not $CondaExe) {
    $command = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($command) { $CondaExe = $command.Source }
    elseif ($env:CONDA_EXE) { $CondaExe = $env:CONDA_EXE }
    else {
        foreach ($directory in ($env:Conda -split ';')) {
            if ($directory -and (Test-Path -LiteralPath (Join-Path $directory 'conda.exe'))) {
                $CondaExe = Join-Path $directory 'conda.exe'; break
            }
        }
    }
}
if (-not $CondaExe -or -not (Test-Path -LiteralPath $CondaExe -PathType Leaf)) { throw 'Conda not found; supply -CondaExe.' }
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$null = Get-Command git, node -ErrorAction Stop
foreach ($file in @('backend/requirements.lock', 'frontend/package-lock.json', 'frontend/package.json', 'LICENSE',
    'scripts/portable/build.py', 'scripts/portable/launcher.py', 'scripts/portable/启动.cmd', 'scripts/portable/使用说明.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $studyRoot $file) -PathType Leaf)) { throw "Missing build input: $file" }
}
$dist = Join-Path $studyRoot 'dist'
$zip = Join-Path $dist "StudyBuddy-v$Version-windows-x64-preview.zip"
if ((Test-Path -LiteralPath $zip) -or (Test-Path -LiteralPath "$zip.sha256")) { throw "Output already exists: $zip" }
$work = Join-Path $dist ('.build/' + [guid]::NewGuid().ToString('N').Substring(0, 10))
$null = New-Item -ItemType Directory -Path $work -Force
$runtime = Join-Path $work 'env'
$python = Join-Path $runtime 'python.exe'
$buildPython = Join-Path $work 'build-tools/Scripts/python.exe'
$saved = @{}
function Set-BuildEnv([string]$Name, [string]$Value) {
    $saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}
function Invoke-Checked([string]$Name, [string]$Command, [string[]]$Arguments) {
    Write-Host ">>> $Name"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command @Arguments 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
            Write-Host $line
            Add-Content -LiteralPath (Join-Path $work "$Name.log") -Value $line -Encoding UTF8
        }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "$Name failed ($code). See $work/$Name.log" }
}
Push-Location $studyRoot
try {
    Set-BuildEnv 'CONDA_PKGS_DIRS' (Join-Path $work 'pkgs')
    Set-BuildEnv 'CONDA_ENVS_PATH' (Join-Path $work 'envs')
    Set-BuildEnv 'CONDA_REGISTER_ENVS' 'false'
    Set-BuildEnv 'CONDA_NO_PLUGINS' 'true'
    Set-BuildEnv 'CONDA_SOLVER' 'classic'
    Set-BuildEnv 'CONDA_REPORT_ERRORS' 'false'
    Set-BuildEnv 'CONDA_NUMBER_CHANNEL_NOTICES' '0'
    Set-BuildEnv 'CONDA_QUIET' 'true'
    Set-BuildEnv 'PIP_CACHE_DIR' (Join-Path $work 'pip-cache')
    Set-BuildEnv 'npm_config_cache' (Join-Path $work 'npm-cache')
    Set-BuildEnv 'PYTHONUTF8' '1'
    Set-BuildEnv 'PYTHONDONTWRITEBYTECODE' '1'
    Set-BuildEnv 'PYTHONNOUSERSITE' '1'
    $null = New-Item -ItemType Directory -Path (Join-Path $work 'tmp')
    Set-BuildEnv 'TEMP' (Join-Path $work 'tmp')
    Set-BuildEnv 'TMP' (Join-Path $work 'tmp')
    Set-BuildEnv 'PATH' "$runtime;$runtime/Library/bin;$env:PATH"
    $create = @('create', '--yes', '--copy', '--no-default-packages', '--prefix', $runtime, '--override-channels', '--channel', 'https://conda.anaconda.org/conda-forge')
    if ($CondaExplicit) { $create += @('--file', (Resolve-Path -LiteralPath $CondaExplicit).Path) }
    else { $create += @('python=3.12.15', 'pip', 'vc14_runtime', 'ucrt') }
    Invoke-Checked 'conda-create' $CondaExe $create
    Invoke-Checked 'build-tools' $python @('-I', '-m', 'venv', (Join-Path $work 'build-tools'))
    Invoke-Checked 'conda-pack-install' $buildPython @('-I', '-m', 'pip', 'install', '--disable-pip-version-check', 'conda-pack==0.9.2')
    Invoke-Checked 'runtime-install' $python @('-I', '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'backend/requirements.lock')
    Invoke-Checked 'pip-check' $python @('-I', '-m', 'pip', 'check')
    & $CondaExe list --prefix $runtime --explicit | Set-Content -LiteralPath (Join-Path $work 'conda-explicit.txt') -Encoding ASCII
    if ($LASTEXITCODE -ne 0) { throw 'Cannot record explicit Conda packages.' }
    $arguments = @('-I', '-B', 'scripts/portable/build.py', '--root', $studyRoot, '--work', $work,
        '--version', $Version, '--npm', $npm, '--build-python', $buildPython, '--conda', $CondaExe)
    Invoke-Checked 'assemble' $python $arguments
    Write-Host "Built local preview: $zip"
} finally {
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
    Pop-Location
    Write-Host "Build logs and inputs retained at: $work"
}
