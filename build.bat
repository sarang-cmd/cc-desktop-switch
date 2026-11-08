@echo off
title CC Desktop Switch - Build Tool
chcp 65001 >nul

:MENU
cls
echo ========================================
echo    CC Desktop Switch v1.0.26 - Build Tool
echo ========================================
echo.
echo  Select a packaging mode:
echo.
echo    [1] Folder build     -- Fast startup, best for daily debugging
echo    [2] Single-file exe  -- One portable file
echo    [3] ZIP archive      -- Extract and run
echo    [4] Setup installer  -- Real installer (requires NSIS)
echo.
echo    [Q] Quit
echo.

choice /c 1234Q /n /m "Enter an option (1/2/3/4/Q): "
set choice=%errorlevel%

if %choice%==5 exit /b 0
if %choice%==1 set MODE=folder
if %choice%==2 set MODE=onefile
if %choice%==3 set MODE=zip
if %choice%==4 set MODE=installer
if not defined MODE goto MENU

cls
echo ========================================
echo  Building (%MODE%)...
echo ========================================

cd /d "%~dp0"

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [Error] Python was not found. Install Python 3.9+ first.
    pause
    exit /b 1
)

REM Install dependencies
echo [1/3] Installing dependencies...
pip install -r requirements.txt >nul 2>&1
if %errorlevel% neq 0 (
    echo [Error] Dependency installation failed.
    pause
    exit /b 1
)
echo  Dependencies installed

REM Clean previous build output
if exist dist\CC-Desktop-Switch rmdir /s /q dist\CC-Desktop-Switch >nul 2>&1
if exist CC-Desktop-Switch.zip del CC-Desktop-Switch.zip >nul 2>&1
if exist CC-Desktop-Switch-Setup-*.exe del CC-Desktop-Switch-Setup-*.exe >nul 2>&1
if exist build rmdir /s /q build >nul 2>&1

REM Run PyInstaller
echo [2/3] Packaging...

if "%MODE%"=="onefile" (
    set CCDS_ONEFILE=1
    python -m PyInstaller --noconfirm --clean build.spec >nul 2>&1
    set CCDS_ONEFILE=
    if %errorlevel% equ 0 (
        echo  Single-file exe built successfully.
    ) else (
        echo [Error] Packaging failed.
        pause
        exit /b 1
    )
)

if "%MODE%"=="folder" (
    set CCDS_ONEFILE=
    python -m PyInstaller --noconfirm --clean build.spec >nul 2>&1
    if %errorlevel% equ 0 (
        echo  Folder build completed successfully.
    ) else (
        echo [Error] Packaging failed.
        pause
        exit /b 1
    )
)

if "%MODE%"=="zip" (
    set CCDS_ONEFILE=
    python -m PyInstaller --noconfirm --clean build.spec >nul 2>&1
    if %errorlevel% equ 0 (
        powershell Compress-Archive -Path "dist\CC-Desktop-Switch\*" -DestinationPath "CC-Desktop-Switch.zip" -Force >nul 2>&1
        echo  ZIP archive built successfully.
    ) else (
        echo [Error] Packaging failed.
        pause
        exit /b 1
    )
)

if "%MODE%"=="installer" (
    REM Build the folder bundle first
    set CCDS_ONEFILE=
    python -m PyInstaller --noconfirm --clean build.spec >nul 2>&1
    if %errorlevel% neq 0 (
        echo [Error] PyInstaller packaging failed.
        pause
        exit /b 1
    )
    echo  PyInstaller finished, building the installer...

    REM Check NSIS
    where makensis >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo [Error] NSIS was not found.
        echo.
        echo  Install NSIS 3.0+ first:
        echo    https://nsis.sourceforge.io/Download
        echo.
        echo  Make sure makensis.exe is on PATH after installing,
        echo  or run it manually: makensis installer.nsi
        echo.
        pause
        exit /b 1
    )

    makensis installer.nsi >nul 2>&1
    if %errorlevel% equ 0 (
        echo  Setup installer built successfully.
    ) else (
        echo [Error] NSIS packaging failed.
        pause
        exit /b 1
    )
)

echo.
echo [3/3] Done.
echo ========================================
echo.
echo  Output:
if "%MODE%"=="folder" echo    dist\CC-Desktop-Switch\
if "%MODE%"=="onefile" echo    dist\CC-Desktop-Switch.exe
if "%MODE%"=="zip" echo    CC-Desktop-Switch.zip
if "%MODE%"=="installer" dir /b CC-Desktop-Switch-Setup-*.exe 2>nul
echo.
echo  Launching the app opens the CC Desktop Switch desktop window.
echo  For browser debugging, run: python main.py --browser
echo ========================================
echo.

pause
goto MENU
