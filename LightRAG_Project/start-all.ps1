# ============================================
# LightRAG Project - Start All Services
# Usage:
#   .\start-all.ps1           # start backend + celery + frontend
#   .\start-all.ps1 -Flower   # start all + Celery Flower
#   .\start-all.ps1 -Stop     # stop all services
# ============================================

param(
    [switch]$Flower,
    [switch]$Stop
)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$FrontendDir = Join-Path $ProjectRoot "frontend"

$CondaEnv = "rag"
$CondaPath = "$env:USERPROFILE\.conda\envs\$CondaEnv"
$PythonExe = "$CondaPath\python.exe"

function Stop-AllServices {
    Write-Host ""
    Write-Host "[STOP] Stopping all services..." -ForegroundColor Yellow
    
    $names = @("uvicorn", "celery", "node", "flower")
    foreach ($name in $names) {
        $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
        if ($procs) {
            $procs | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
            Write-Host "  [OK] Stopped $name" -ForegroundColor Green
        }
    }
    
    Write-Host ""
    Write-Host "[DONE] All services stopped." -ForegroundColor Green
    Write-Host ""
    exit
}

# ============== Main ==============

if ($Stop) {
    Stop-AllServices
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  LightRAG Project - Start All" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check environment
if (-not (Test-Path $PythonExe)) {
    Write-Host "[ERROR] Conda env '$CondaEnv' not found" -ForegroundColor Red
    Write-Host "Expected: $PythonExe" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] Conda env: $CondaEnv" -ForegroundColor Green
Write-Host "[OK] Python: $PythonExe" -ForegroundColor Green
Write-Host "[OK] Project: $ProjectRoot" -ForegroundColor Green
Write-Host ""

function Start-ServiceWindow($Title, $Command, $WorkingDir) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    Start-Process powershell -ArgumentList "-NoExit", "-EncodedCommand", $encoded -WorkingDirectory $WorkingDir -WindowStyle Normal
    Write-Host "      $Title started" -ForegroundColor Green
}

# 1. Backend
Write-Host "[1/3] Starting Backend (uvicorn)..." -ForegroundColor Yellow
$backendCmd = @"
`$host.ui.RawUI.WindowTitle = 'LightRAG - Backend'
& '$PythonExe' -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"@
Start-ServiceWindow "Backend (http://localhost:8000)" $backendCmd $ProjectRoot
Start-Sleep -Seconds 2
Write-Host ""

# 2. Celery Worker
Write-Host "[2/3] Starting Celery Worker..." -ForegroundColor Yellow
$celeryCmd = @"
`$host.ui.RawUI.WindowTitle = 'LightRAG - Celery Worker'
& '$PythonExe' -m celery -A app.tasks.celery_app worker --loglevel=info -Q local
"@
Start-ServiceWindow "Celery Worker" $celeryCmd $ProjectRoot
Start-Sleep -Seconds 2
Write-Host ""

# 3. Frontend
Write-Host "[3/3] Starting Frontend (npm run dev)..." -ForegroundColor Yellow
$frontendCmd = @"
`$host.ui.RawUI.WindowTitle = 'LightRAG - Frontend'
npm run dev
"@
Start-ServiceWindow "Frontend (http://localhost:5173)" $frontendCmd $FrontendDir
Start-Sleep -Seconds 2
Write-Host ""

# 4. Optional: Flower
if ($Flower) {
    Write-Host "[BONUS] Starting Celery Flower..." -ForegroundColor Yellow
    $flowerCmd = @"
`$host.ui.RawUI.WindowTitle = 'LightRAG - Flower'
& '$PythonExe' -m celery -A app.tasks.celery_app flower --port=5555
"@
    Start-ServiceWindow "Flower (http://localhost:5555)" $flowerCmd $ProjectRoot
    Start-Sleep -Seconds 1
    Write-Host ""
}

# Summary
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  All services started!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  - Backend API: http://localhost:8000"
Write-Host "  - Frontend:    http://localhost:5173"
if ($Flower) {
    Write-Host "  - Flower:      http://localhost:5555"
}
Write-Host ""
Write-Host "Commands:" -ForegroundColor Yellow
Write-Host "  Stop all: .\start-all.ps1 -Stop"
Write-Host "  +Flower:  .\start-all.ps1 -Flower"
Write-Host ""
Write-Host "Press any key to close this window (services keep running)..."
[void]$Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
