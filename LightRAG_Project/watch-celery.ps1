# ============================================
# Celery Worker 看门狗脚本
# 检测到 Worker 死亡/假死后自动重启
# Usage: powershell -File .\watch-celery.ps1
# ============================================

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$CondaEnv = "rag"
$PythonExe = "$env:USERPROFILE\.conda\envs\$CondaEnv\python.exe"
$CheckInterval = 30  # 每 30 秒检查一次

function Test-CeleryAlive {
    # 检查是否有监听 local 队列的 celery worker 进程
    $procs = Get-WmiObject Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -like "*celery*worker*" -and $_.CommandLine -like "*-Q local*"
    }
    return ($procs -ne $null -and $procs.Count -gt 0)
}

function Start-CeleryWorker {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 🚀 启动 Celery Worker..." -ForegroundColor Green
    $cmd = @"
`$host.ui.RawUI.WindowTitle = 'LightRAG - Celery Worker'
& '$PythonExe' -m celery -A app.tasks.celery_app worker --loglevel=info -Q local
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    Start-Process powershell -ArgumentList "-NoExit", "-EncodedCommand", $encoded -WorkingDirectory $ProjectRoot -WindowStyle Normal
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Celery Worker Watchdog" -ForegroundColor Cyan
Write-Host "  检查间隔: ${CheckInterval}s" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 首次启动
if (-not (Test-CeleryAlive)) {
    Start-CeleryWorker
    Start-Sleep -Seconds 5
}

while ($true) {
    Start-Sleep -Seconds $CheckInterval

    if (Test-CeleryAlive) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ✅ Celery Worker 运行正常" -ForegroundColor DarkGray
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ⚠️ Celery Worker 未检测到，准备重启..." -ForegroundColor Yellow
        Start-CeleryWorker
        Start-Sleep -Seconds 5
    }
}
