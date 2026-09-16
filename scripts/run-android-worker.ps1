param(
    [Parameter(Mandatory = $true)]
    [string]$AppProjectRoot,
    [string]$WorkerKey = 'android-local-01',
    [string]$PoolKey = 'android-local',
    [string]$QueueName = 'testops.pool.android-local',
    [string]$DisplayName = 'YanJia Android Device Worker',
    [string]$ImageRepository = 'local/yanjia-android-appium',
    [string]$AdapterRoot = ''
)

$ErrorActionPreference = 'Stop'
$platformRoot = Split-Path -Parent $PSScriptRoot
$resolvedAppRoot = [System.IO.Path]::GetFullPath($AppProjectRoot)
$resolvedAdapterRoot = if ($AdapterRoot) {
    [System.IO.Path]::GetFullPath($AdapterRoot)
} else {
    Join-Path $platformRoot 'runners\android_appium\src'
}
if (-not (Test-Path -LiteralPath (Join-Path $resolvedAppRoot 'testops-package.json'))) {
    throw "APP 自动化包无效：$resolvedAppRoot"
}
if (-not (Test-Path -LiteralPath (
    Join-Path $resolvedAdapterRoot 'testops\runners\android_appium\package_digest.py'
))) {
    throw "Android 适配器无效：$resolvedAdapterRoot"
}
if (-not $env:RUNNER_CALLBACK_TOKEN) {
    throw 'RUNNER_CALLBACK_TOKEN 未配置。'
}
foreach ($secretName in @(
    'TESTOPS_SECRET_YANJIA_USERNAME',
    'TESTOPS_SECRET_YANJIA_PASSWORD'
)) {
    if (-not [Environment]::GetEnvironmentVariable($secretName)) {
        throw "$secretName 未配置。"
    }
}

$python = Join-Path $platformRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw '找不到 TOP Python 环境。请先创建 .venv 或将 python 加入 PATH。'
    }
    $python = $pythonCommand.Source
}

$digest = (& $python (Join-Path $PSScriptRoot 'package_android_appium.py') `
    --source $resolvedAppRoot `
    --adapter-root $resolvedAdapterRoot | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Android 自动化包摘要计算失败。'
}

$pythonPaths = @(
    'packages\contracts\src',
    'apps\api\src',
    'runners\web_playwright\src',
    'runners\legacy_pytest\src',
    'services\worker\src'
) | ForEach-Object { Join-Path $platformRoot $_ }
$pythonPaths = @($resolvedAdapterRoot) + $pythonPaths
$env:PYTHONPATH = $pythonPaths -join [System.IO.Path]::PathSeparator
$env:TESTOPS_ANDROID_APPIUM_PROJECT_ROOT = $resolvedAppRoot
$env:RUNNER_WORKER_KEY = $WorkerKey
$env:RUNNER_POOL_KEY = $PoolKey
$env:RUNNER_DISPLAY_NAME = $DisplayName
$env:RUNNER_VERSION = '1.0.0'
$env:RUNNER_MAX_SLOTS = '1'
$env:RUNNER_EXECUTION_MODE = 'IN_PROCESS'
$env:RUNNER_EXECUTOR_TIMEOUT_SECONDS = '86400'
$env:RUNNER_CAPABILITIES = '{"target_types":["APP"],"browsers":[],"labels":{"platform":"windows","framework":"appium"}}'
$catalogEntry = @{
    runner_type = 'ANDROID_APPIUM'
    image_repository = $ImageRepository
    digest = $digest
} | ConvertTo-Json -Compress
$env:RUNNER_PACKAGE_CATALOG = "[$catalogEntry]"

Write-Host "TOP Android Worker：$DisplayName"
Write-Host "Pool/Queue：$PoolKey / $QueueName"
Write-Host "Package：$ImageRepository@$digest"
Write-Host '设备、Appium 与测试进度将保留在当前可见控制台。'
& $python -m celery -A testops.worker.celery_app:celery_app worker `
    --loglevel=INFO `
    --pool=solo `
    --concurrency=1 `
    --queues=$QueueName
exit $LASTEXITCODE
