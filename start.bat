@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title CC Desktop Switch
echo ========================================
echo    CC Desktop Switch v1.0.27
echo    Starting the admin console...
echo ========================================

cd /d "%~dp0"

if not defined CCDS_CONFIG_DIR (
    set "CCDS_CONFIG_DIR=%~dp0.tmp\dev-config"
    echo [Info] Development startup uses a separate config directory:
    echo        !CCDS_CONFIG_DIR!
)

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [Error] Python was not found. Install Python 3.9+ first.
    pause
    exit /b 1
)

REM Check dependencies
pip show fastapi >nul 2>&1
if %errorlevel% neq 0 (
    echo [Info] First run detected, installing dependencies...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [Error] Dependency installation failed.
        pause
        exit /b 1
    )
)

python main.py
if %errorlevel% neq 0 (
    echo [Error] The app exited unexpectedly with code %errorlevel%.
    pause
)
