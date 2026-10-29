param(
    [ValidateSet('All', 'Backend', 'Frontend', 'Browser', 'Docs', 'Smoke', 'Recognition', 'Cloud')]
    [string]$Suite = 'All',
    [string]$VideoPath,
    [string]$AsrOption = 'paraformer',
    [ValidateSet('rapidocr')][string]$OcrOption = 'rapidocr',
    [ValidateSet('zh', 'en', 'auto')][string]$Language = 'zh',
    [string]$BaseUrl = 'http://127.0.0.1:18000'
)

$ErrorActionPreference = 'Stop'
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$output = Join-Path $studyRoot "test-results/$runId"
$container = "studybuddy-tests-$runId"
$image = 'studybuddy-tests:local'
$results = [System.Collections.Generic.List[object]]::new()
$exitCode = 0
$current = 'preparation'
$started = Get-Date
$savedEnv = @{}
New-Item -ItemType Directory -Force -Path $output | Out-Null

# Run native tools without losing their exit code when output is saved to a file.
function Invoke-Checked {
    param([string]$Name, [string]$Command, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command @Arguments 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
            Write-Host $line
            Add-Content -LiteralPath (Join-Path $output "$Name.log") -Value $line -Encoding UTF8
        }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "$Name failed (exit $code). See $Name.log." }
}

function Invoke-Suite {
    param([string]$Name, [scriptblock]$Action)
    $script:current = $Name
    Write-Host "`n>>> $Name"
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    & $Action
    $results.Add([ordered]@{ suite = $Name; status = 'passed'; seconds = [math]::Round($clock.Elapsed.TotalSeconds, 2) })
}

function Set-TestEnv {
    param([string]$Name, [string]$Value)
    if (-not $savedEnv.ContainsKey($Name)) { $savedEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process') }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

Push-Location $studyRoot
try {
    if ($Suite -in @('Recognition', 'Cloud')) {
        if (-not $VideoPath) { throw 'Recognition/Cloud requires -VideoPath with a real teaching video.' }
        $VideoPath = (Resolve-Path -LiteralPath $VideoPath).Path
        if (-not (Test-Path -LiteralPath $VideoPath -PathType Leaf)) { throw 'VideoPath must be a file.' }
    }
    if ($Suite -in @('All', 'Frontend', 'Browser', 'Smoke')) {
        $null = Get-Command node, npm.cmd -ErrorAction Stop
        Push-Location frontend
        try {
            if (-not (Test-Path node_modules/.package-lock.json)) {
                Invoke-Checked 'npm-install' 'npm.cmd' @('ci', '--no-audit', '--no-fund')
            }
            Invoke-Checked 'npm-dependencies' 'npm.cmd' @('ls', '--depth=0')
        } finally { Pop-Location }
    }
    if ($Suite -ne 'Frontend' -and $Suite -ne 'Smoke') {
        $null = Get-Command docker -ErrorAction Stop
        Invoke-Checked 'docker-engine' 'docker' @('info', '--format', '{{.ServerVersion}}')
        $fingerprint = ((Get-FileHash -LiteralPath 'Dockerfile', 'backend/requirements.lock', 'frontend/package-lock.json').Hash -join ':')
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $imageInfo = & docker image inspect $image 2>$null | ConvertFrom-Json
        $ErrorActionPreference = $previous
        if ($LASTEXITCODE -ne 0 -or $imageInfo.Config.Labels.'studybuddy.test-dependencies' -ne $fingerprint) {
            Invoke-Checked 'docker-build' 'docker' @('build', '--target', 'runtime', '--label', "studybuddy.test-dependencies=$fingerprint", '-t', $image, '.')
        } else { Write-Host 'Reusing test image: dependency fingerprints match.' }
    }
    $dockerArgs = @('run', '--rm', '--pull', 'never', '--network', 'none',
        '--mount', "type=bind,source=$studyRoot/backend,target=/app,readonly",
        '--mount', "type=bind,source=$studyRoot,target=/workspace,readonly",
        '--mount', "type=bind,source=$output,target=/test-output",
        '-e', 'DATA_DIR=/test-output/data', '-e', 'LOG_DIR=/test-output/log', '-e', 'MODEL_DIR=/test-output/models',
        '-e', 'PYTHONDONTWRITEBYTECODE=1')

    if ($Suite -in @('All', 'Backend')) {
        Invoke-Suite 'backend' {
            Invoke-Checked 'backend-dependencies' 'docker' ($dockerArgs + @($image, 'python', '-m', 'pip', 'check'))
            Invoke-Checked 'backend' 'docker' ($dockerArgs + @($image, 'python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--junitxml=/test-output/backend.xml'))
        }
    }
    if ($Suite -in @('All', 'Frontend', 'Browser')) {
        Invoke-Suite 'frontend' {
            Push-Location frontend
            try { Invoke-Checked 'frontend' 'npm.cmd' @('run', 'build') } finally { Pop-Location }
        }
    }
    if ($Suite -in @('All', 'Docs')) {
        Invoke-Suite 'docs' { Invoke-Checked 'docs' 'docker' ($dockerArgs + @($image, 'python', '/workspace/scripts/check_docs.py', '--report', '/test-output/docs.json')) }
    }
    if ($Suite -in @('All', 'Browser')) {
        Invoke-Suite 'browser' {
            Invoke-Checked 'browser-sample' 'docker' ($dockerArgs + @($image, 'python', '-m', 'tests.make_browser_sample', '/test-output/sample.mp4'))
            New-Item -ItemType Directory -Force -Path (Join-Path $output 'data') | Out-Null
            Set-Content -LiteralPath (Join-Path $output 'data/.e2e-isolated') -Value 'Test data only' -Encoding ASCII
            Invoke-Checked 'browser-server' 'docker' @('run', '-d', '--pull', 'never', '--name', $container,
                '-p', '127.0.0.1::8000',
                '--mount', "type=bind,source=$studyRoot/backend,target=/app,readonly",
                '--mount', "type=bind,source=$studyRoot/frontend/dist,target=/web,readonly",
                '--mount', "type=bind,source=$output,target=/test-output",
                '-e', 'DATA_DIR=/test-output/data', '-e', 'MODEL_DIR=/test-output/data/models',
                '-e', 'STATIC_DIR=/web', '-e', 'LOG_DIR=/test-output/log', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                $image, 'uvicorn', 'tests.e2e_app:factory', '--factory', '--host', '0.0.0.0', '--port', '8000')
            # Consume all output: Select-Object -First can report exit -1 on Windows PowerShell 5.1.
            $bindings = @(& docker port $container 8000/tcp)
            $portExit = $LASTEXITCODE
            if ($portExit -ne 0 -or $bindings.Count -eq 0) { throw 'Cannot discover isolated test server port.' }
            $binding = $bindings[0].Trim()
            if ($binding -notmatch '^127\.0\.0\.1:\d+$') { throw 'Unexpected isolated test server binding.' }
            $testUrl = "http://$binding"
            $ready = $false
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                try {
                    $identity = Invoke-RestMethod "$testUrl/__test__/identity" -TimeoutSec 2
                    if ($identity.fake_services_used -eq $true) { $ready = $true; break }
                } catch { Start-Sleep -Seconds 1 }
            }
            if (-not $ready) { throw 'Isolated test server did not become ready; see browser-server.log.' }
            Set-TestEnv 'E2E_BASE_URL' $testUrl
            Set-TestEnv 'E2E_VIDEO_PATH' (Join-Path $output 'sample.mp4')
            Set-TestEnv 'E2E_LOG_PATH' (Join-Path $output 'log/studybuddy.log')
            Set-TestEnv 'E2E_REPORT_DIR' (Join-Path $output 'browser')
            Push-Location frontend
            try {
                Invoke-Checked 'browser-install' 'npx.cmd' @('--no-install', 'playwright', 'install', 'chromium')
                Invoke-Checked 'browser' 'npm.cmd' @('run', 'test:e2e')
            } finally { Pop-Location }
        }
    }
    if ($Suite -eq 'Smoke') {
        Invoke-Suite 'smoke' {
            Invoke-Checked 'browser-install' 'npx.cmd' @('--prefix', 'frontend', '--no-install', 'playwright', 'install', 'chromium')
            Invoke-Checked 'smoke' 'node' @('frontend/scripts/check-live.mjs', $BaseUrl, $output)
        }
    }
    if ($Suite -in @('Recognition', 'Cloud')) {
        Invoke-Suite $Suite.ToLower() {
            $realArgs = @('run', '--rm', '--pull', 'never',
                '--mount', "type=bind,source=$studyRoot/backend,target=/app,readonly",
                '--mount', "type=bind,source=$output,target=/test-output",
                '--mount', "type=bind,source=$studyRoot/models,target=/models,readonly",
                '--mount', "type=bind,source=$VideoPath,target=/input.mp4,readonly",
                '-e', 'DATA_DIR=/test-output/data', '-e', 'MODEL_DIR=/models', '-e', 'LOG_DIR=/test-output/log',
                '-e', 'HF_HUB_OFFLINE=1', '-e', 'PYTHONDONTWRITEBYTECODE=1')
            if ($Suite -eq 'Cloud') {
                $private = Join-Path $studyRoot 'data/_private'
                if (-not (Test-Path (Join-Path $private 'model-settings.json'))) { throw 'Save the ModelScope Token in the app settings first.' }
                $realArgs += @('--mount', "type=bind,source=$private,target=/saved-settings/_private,readonly")
            } else { $realArgs += @('--network', 'none') }
            $realArgs += @($image, 'python', 'scripts/check_real.py', '--asr', $AsrOption, '--ocr', $OcrOption, '--language', $Language)
            if ($Suite -eq 'Cloud') { $realArgs += '--cloud' }
            Invoke-Checked $Suite.ToLower() 'docker' $realArgs
        }
    }
} catch {
    $exitCode = 1
    $results.Add([ordered]@{ suite = $current; status = 'failed'; message = $_.Exception.Message })
    Write-Host "`nFAILED: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    # Only the uniquely named container created by this run is stopped. Production is untouched.
    if ($Suite -in @('All', 'Browser')) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $existing = & docker ps -aq --filter "name=^/$container$" 2>$null
        if ($existing) {
            & docker logs $container 2>&1 | Out-File -LiteralPath (Join-Path $output 'browser-server.log') -Append -Encoding UTF8
            & docker stop --time 10 $container 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { $exitCode = 1; Write-Host "Cleanup failed; stop container $container manually." }
            & docker rm $container 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { $exitCode = 1; Write-Host "Cleanup failed; remove container $container manually." }
        }
        $ErrorActionPreference = $previous
    }
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
    $report = [ordered]@{ run_id = $runId; suite = $Suite; status = $(if ($exitCode -eq 0) { 'passed' } else { 'failed' });
        seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 2); cloud_requested = ($Suite -eq 'Cloud'); results = @($results.ToArray()) }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'summary.json') -Encoding UTF8
    Pop-Location
    Write-Host "`nReports: $output"
}
exit $exitCode
