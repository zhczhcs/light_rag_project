@echo off
chcp 65001 >nul
echo ========================================
echo   LightRAG Project 一键启动（含监控）
echo ========================================
echo.

powershell -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" -Flower
