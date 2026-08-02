@echo off
setlocal enabledelayedexpansion
title Link2Lead - LinkedIn Lead Generator
cd /d "%~dp0"

echo ============================================================
echo   Link2Lead - B2B LinkedIn Lead Generator
echo   Launching application...
echo ============================================================
echo.

REM ------------------------------------------------------------------
REM 1. Locate Python
REM ------------------------------------------------------------------
set "PYTHON_CMD="
where python >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>nul
    if !errorlevel!==0 (
        set "PYTHON_CMD=py -3"
    ) else (
        echo [ERROR] Python 3 was not found on this system.
        echo.
        echo Please install Python 3.10 or newer from https://www.python.org/downloads/
        echo and ensure "Add Python to PATH" is checked during installation.
        echo.
        pause
        exit /b 1
    )
)

echo [1/4] Python detected: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

REM ------------------------------------------------------------------
REM 2. Create virtual environment if it does not exist
REM ------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment (.venv^)...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo.
) else (
    echo [2/4] Virtual environment (.venv^) already exists.
    echo.
)

set "VENV_PYTHON=.venv\Scripts\python.exe"

REM ------------------------------------------------------------------
REM 3. Install / verify dependencies
REM ------------------------------------------------------------------
echo [3/4] Installing dependencies from requirements.txt...
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>nul
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo.

REM ------------------------------------------------------------------
REM 4. Verify .env configuration
REM ------------------------------------------------------------------
if not exist ".env" (
    echo [WARNING] No .env file found.
    echo.
    echo   A .env file is required to run searches.
    echo   Copy .env.example to .env and fill in your Serper.dev API key:
    echo.
    echo     copy .env.example .env
    echo.
    echo   Then edit .env and set SERPER_API_KEY.
    echo.
    choice /c YN /m "Continue without .env (app will load but searches will fail)"
    if errorlevel 2 (
        echo.
        echo Setup cancelled. Create your .env file and run this script again.
        pause
        exit /b 1
    )
    echo.
)

REM ------------------------------------------------------------------
REM 5. Launch the Streamlit application
REM ------------------------------------------------------------------
echo [4/4] Starting Link2Lead...
echo.
echo   The application will open in your default browser.
echo   Press Ctrl+C in this window to stop the server.
echo.
"%VENV_PYTHON%" -m streamlit run app.py

echo.
echo Application stopped.
pause