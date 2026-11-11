@echo off
rem Auto run: called by Windows Task Scheduler every day. No pause, no menu.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs"

set PYEXE=
if exist "%~dp0.venv\Scripts\python.exe" set PYEXE=%~dp0.venv\Scripts\python.exe
if not defined PYEXE (
    where python >nul 2>nul && set PYEXE=python
)
if not defined PYEXE (
    where py >nul 2>nul && set PYEXE=py
)

echo. >> "logs\auto_run.log"
echo ===== %date% %time% start ===== >> "logs\auto_run.log"
if not defined PYEXE (
    echo [ERROR] python not found >> "logs\auto_run.log"
    exit /b 1
)

"%PYEXE%" app.py --auto >> "logs\auto_run.log" 2>&1
echo ===== %date% %time% end (code %errorlevel%) ===== >> "logs\auto_run.log"
exit /b %errorlevel%
