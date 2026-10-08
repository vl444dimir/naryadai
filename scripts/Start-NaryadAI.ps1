param(
    [ValidateSet('Dev', 'Preview')][string]$Mode = 'Dev'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot 'backend'
$frontendDir = Join-Path $projectRoot 'frontend'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimeDir = Join-Path $projectRoot '.local\runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Test-Api {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 3
        $spec = Invoke-RestMethod 'http://127.0.0.1:8000/openapi.json' -TimeoutSec 3
        return $health.status -eq 'ok' -and $spec.info.title -eq 'НарядAI Backend'
    } catch { return $false }
}

function Test-Port([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $Port)
        return $pending.Wait(1000) -and $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}

function Start-ProjectProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$Directory) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outPath = Join-Path $runtimeDir "$Name-$stamp.out.log"
    $errPath = Join-Path $runtimeDir "$Name-$stamp.err.log"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory `
        -WindowStyle Hidden -RedirectStandardOutput $outPath -RedirectStandardError $errPath -PassThru
    Write-Host "$Name запущен: PID $($process.Id). Журнал: $errPath"
    return $process
}

function Wait-Ready([scriptblock]$Check, [string]$Name) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (& $Check) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name не готов. Проверьте журналы в $runtimeDir."
}

if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Не найден .venv. Подготовьте backend по README.md.' }
if (-not (Test-Path -LiteralPath (Join-Path $backendDir '.env'))) { throw 'Не найден backend/.env. Подготовьте подключение к базе по README.md.' }
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not $npmCommand) { throw 'Установите Node.js 24 и откройте новый PowerShell.' }
$vitePath = Join-Path $frontendDir 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $vitePath)) {
    Push-Location $frontendDir
    try { & $npmCommand.Source ci; if ($LASTEXITCODE -ne 0) { throw 'npm ci завершился с ошибкой.' } }
    finally { Pop-Location }
}

if (-not (Test-Api)) {
    if (Test-Port 8000) { throw 'Порт 8000 занят другим или неготовым сервером. Существующий процесс сохранён.' }
    $dataDir = Join-Path $projectRoot '.local\postgres\data'
    $pgCtlPath = Join-Path $env:ProgramFiles 'PostgreSQL\16\bin\pg_ctl.exe'
    if ((Test-Path -LiteralPath (Join-Path $dataDir 'PG_VERSION')) -and (Test-Path -LiteralPath $pgCtlPath)) {
        & $pgCtlPath status -D $dataDir *> $null
        if ($LASTEXITCODE -ne 0) {
            $dbLog = Join-Path $runtimeDir 'postgres.log'
            & $pgCtlPath start -D $dataDir -l $dbLog -w
            if ($LASTEXITCODE -ne 0) { throw "PostgreSQL не запущен. Журнал: $dbLog" }
        }
    }
    $null = Start-ProjectProcess 'api' $pythonPath @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') $backendDir
    Wait-Ready { Test-Api } 'API и база'
} else { Write-Host 'API и база уже работают; повторный процесс не создаётся.' }

$workers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($pythonPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 `
        -and $_.CommandLine -match '(?:^|\s)-m\s+app\.worker(?:\s|$)'
})
if ($workers.Count -eq 0) {
    $worker = Start-ProjectProcess 'worker' $pythonPath @('-m', 'app.worker') $backendDir
    Start-Sleep -Milliseconds 500
    $worker.Refresh()
    if ($worker.HasExited) { throw 'Worker завершился. Проверьте его журнал.' }
} else { Write-Host 'Worker проекта уже работает; повторный процесс не создаётся.' }

$port = if ($Mode -eq 'Dev') { 5173 } else { 4173 }
$url = "http://127.0.0.1:$port"
function Test-Frontend {
    try {
        $page = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3
        return $page.StatusCode -eq 200 -and $page.Content -match '<title>НарядAI'
    } catch { return $false }
}
if (Test-Frontend) { Write-Host "Интерфейс уже работает: $url"; return }
if (Test-Port $port) { throw "Порт $port занят другим процессом. Существующий процесс сохранён." }
if ($Mode -eq 'Preview') {
    Push-Location $frontendDir
    try { & $npmCommand.Source run build; if ($LASTEXITCODE -ne 0) { throw 'Сборка frontend завершилась с ошибкой.' } }
    finally { Pop-Location }
}
$quotedVitePath = '"' + $vitePath + '"'
$arguments = if ($Mode -eq 'Preview') { @($quotedVitePath, 'preview', '--host', '127.0.0.1') } else { @($quotedVitePath, '--host', '127.0.0.1') }
$null = Start-ProjectProcess 'frontend' $nodeCommand.Source $arguments $frontendDir
Wait-Ready { Test-Frontend } 'Интерфейс'
Write-Host "Готово: $url. Процессы работают в фоне; журналы — $runtimeDir."
Write-Host 'Скрипт не выполняет миграции, загрузку тестовых данных и настройку внешних сервисов.'
