@echo off
chcp 65001 >nul
echo ========================================
echo   LightRAG Project 一键启动
echo ========================================
echo.

REM 检查 PowerShell 执行策略，如果需要则提示
powershell -Command "Get-ExecutionPolicy" | findstr "Restricted" >nul
if %errorlevel% == 0 (
    echo [WARN] PowerShell 执行策略为 Restricted，脚本可能无法运行。
    echo        请以管理员身份运行 PowerShell，执行：
    echo        Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
    echo.
    pause
    exit /b 1
)

REM 启动脚本
powershell -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
