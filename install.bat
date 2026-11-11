@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo  ==========================================
echo   Douyin Spark - Setup
echo  ==========================================
echo.

set PYEXE=
where python >nul 2>nul && set PYEXE=python
if not defined PYEXE (
    where py >nul 2>nul && set PYEXE=py
)
if not defined PYEXE (
    echo  [X] Python not found.
    echo      Install Python 3.9+ from https://www.python.org/downloads/
    echo      Tick "Add python.exe to PATH" during setup, then run install.bat again.
    echo.
    pause
    exit /b 1
)

echo  [1/3] Creating local venv (.venv) ...
if not exist ".venv\Scripts\python.exe" (
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo  [X] Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo       already exists, skip
)

set VPY=.venv\Scripts\python.exe

echo  [2/3] Installing dependencies ...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [X] pip install failed. Check network and retry.
    pause
    exit /b 1
)

echo  [3/3] Done.
echo.
echo  Next steps:
echo    1. double click start.bat
echo    2. a setup page opens in your browser:
echo         scan QR to log in  -^>  tick the friends  -^>  set the time
echo    3. that's it. it will send automatically every day.
echo.
echo  Note: this tool drives your installed Microsoft Edge.
echo        No extra browser download needed.
echo.
pause
