param(
    [ValidateSet('All', 'Backend', 'Frontend', 'Browser', 'BrowserSource', 'Docs', 'Smoke')][string]$Suite = 'All',
    [string[]]$BackendTests = @(),
    [string[]]$BrowserTests = @(),
    [string]$BaseUrl = 'http://127.0.0.1:18000'
)
$ErrorActionPreference = 'Stop'
$BackendTests = @($BackendTests | ForEach-Object { $_ -split ',' })
$BrowserTests = @($BrowserTests | ForEach-Object { $_ -split ',' })
if (-not $env:CONDA_PREFIX) { throw '请先执行 conda activate studybuddy。' }
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$output = Join-Path $studyRoot "test-results/local-$runId"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$server = $null
$sourceServer = $null
$saved = @{}
function Set-TestEnv([string]$Name, [string]$Value) {
    if (-not $saved.ContainsKey($Name)) { $saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process') }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}
function Invoke-Checked([string]$Name, [string]$Command, [string[]]$Arguments) {
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
    if ($code -ne 0) { throw "$Name failed; see $output/$Name.log" }
}
$summary = [ordered]@{ status = 'failed'; cloud_model_called = $false; suites = @() }
Push-Location $studyRoot
try {
    $localTools = Join-Path $studyRoot 'tools/ffmpeg/bin'
    if (Test-Path -LiteralPath $localTools) { Set-TestEnv 'PATH' "$localTools;$env:PATH" }
    Set-TestEnv 'DATA_DIR' (Join-Path $output 'data')
    Set-TestEnv 'MODEL_DIR' (Join-Path $output 'data/models')
    Set-TestEnv 'LOG_DIR' (Join-Path $output 'log')
    Set-TestEnv 'STATIC_DIR' (Join-Path $output 'web')
    Set-TestEnv 'PYTHONDONTWRITEBYTECODE' '1'
    Set-TestEnv 'PYTHONUTF8' '1'
    Set-TestEnv 'MODELSCOPE_TOKEN' ''
    Set-TestEnv 'LLM_API_KEY' ''
    Set-TestEnv 'LLM_FREE_TIER_CONFIRMED' 'false'
    if ($Suite -in @('All', 'Backend')) {
        Invoke-Checked 'dependencies' $pythonPath @('-m', 'pip', 'check')
        Push-Location backend
        try {
            Invoke-Checked 'backend' $pythonPath (@('-m', 'pytest', '-q', '-p', 'no:cacheprovider', "--basetemp=$output/backend-temp", "--junitxml=$output/backend.xml") + $BackendTests)
        } finally { Pop-Location }
        $summary.suites += 'Backend'
    }
    if ($Suite -in @('All', 'Frontend', 'Browser', 'BrowserSource')) {
        Push-Location frontend
        try {
            if (-not (Test-Path 'node_modules/.package-lock.json')) { Invoke-Checked 'npm-install' 'npm.cmd' @('ci', '--no-audit', '--no-fund') }
            Invoke-Checked 'frontend' 'npm.cmd' @('run', 'build', '--', '--outDir', $env:STATIC_DIR)
        } finally { Pop-Location }
        $summary.suites += 'Frontend'
    }
    if ($Suite -in @('All', 'Docs')) {
        Invoke-Checked 'docs' $pythonPath @('scripts/check_docs.py', '--report', "$output/docs.json")
        $summary.suites += 'Docs'
    }
    if ($Suite -in @('All', 'Browser', 'BrowserSource')) {
        New-Item -ItemType Directory -Force -Path $env:DATA_DIR | Out-Null
        Set-Content -LiteralPath (Join-Path $env:DATA_DIR '.e2e-isolated') -Value 'Isolated local tests' -Encoding ASCII
        Push-Location backend
        try { Invoke-Checked 'sample' $pythonPath @('-m', 'tests.make_browser_sample', "$output/sample.mp4", "$output/media") }
        finally { Pop-Location }
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $listener.Stop()
        $server = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'tests.e2e_app:factory', '--factory', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', "$port") -WorkingDirectory $studyRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$output/server.log" -RedirectStandardError "$output/server-errors.log"
        $testUrl = "http://127.0.0.1:$port"
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if ($server.HasExited) { throw "Test server exited; see $output/server-errors.log" }
            try {
                $identity = Invoke-RestMethod "$testUrl/__test__/identity" -TimeoutSec 1
                if ($identity.fake_services_used) { $ready = $true; break }
            } catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $ready) { throw 'Isolated test server did not become ready.' }
        Set-TestEnv 'E2E_BASE_URL' $testUrl
        Set-TestEnv 'E2E_REPORT_DIR' "$output/browser"
        Set-TestEnv 'E2E_VIDEO_PATH' "$output/sample.mp4"
        Set-TestEnv 'E2E_MEDIA_DIR' "$output/media"
        Set-TestEnv 'E2E_LOG_PATH' "$output/log/studybuddy.log"
        if ($Suite -ne 'BrowserSource') {
            Set-TestEnv 'E2E_SOURCE_TESTS' ''
            Push-Location frontend
            try { Invoke-Checked 'browser' 'npx.cmd' (@('--no-install', 'playwright', 'test') + $BrowserTests) }
            finally { Pop-Location }
            $summary.suites += 'Browser'
        }
        if ($Suite -eq 'BrowserSource' -or $BrowserTests.Count -eq 0) {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
            $listener.Start()
            $sourcePort = $listener.LocalEndpoint.Port
            $listener.Stop()
            Set-TestEnv 'E2E_API_PROXY' $testUrl
            $sourceServer = Start-Process -FilePath (Get-Command node).Source -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$sourcePort", '--strictPort') -WorkingDirectory (Join-Path $studyRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput "$output/vite.log" -RedirectStandardError "$output/vite-errors.log"
            $sourceUrl = "http://127.0.0.1:$sourcePort"
            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                if ($sourceServer.HasExited) { throw "Test Vite exited; see $output/vite-errors.log" }
                try {
                    $identity = Invoke-RestMethod "$sourceUrl/__test__/identity" -TimeoutSec 1
                    if ($identity.fake_services_used) { $ready = $true; break }
                } catch { Start-Sleep -Milliseconds 500 }
            }
            if (-not $ready) { throw 'Isolated Vite did not become ready.' }
            Set-TestEnv 'E2E_BASE_URL' $sourceUrl
            Set-TestEnv 'E2E_SOURCE_TESTS' '1'
            Set-TestEnv 'E2E_REPORT_DIR' "$output/browser-source"
            Push-Location frontend
            $sourceTests = if ($BrowserTests.Count) { $BrowserTests } else { @('error-reporting.spec.ts') }
            try { Invoke-Checked 'browser-source' 'npx.cmd' (@('--no-install', 'playwright', 'test') + $sourceTests) }
            finally { Pop-Location }
            $summary.suites += 'BrowserSource'
        }
    }
    if ($Suite -eq 'Smoke') {
        Invoke-Checked 'smoke' 'node' @('frontend/scripts/check-live.mjs', $BaseUrl, $output)
        $summary.suites += 'Smoke'
    }
    $summary.status = 'passed'
} catch {
    $summary.error = $_.Exception.Message
    Write-Error $_ -ErrorAction Continue
} finally {
    if ($sourceServer -and -not $sourceServer.HasExited) { Stop-Process -Id $sourceServer.Id -Force }
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
    $summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$output/summary.json" -Encoding UTF8
    Pop-Location
    Write-Host "Reports: $output"
}
if ($summary.status -ne 'passed') { exit 1 }
