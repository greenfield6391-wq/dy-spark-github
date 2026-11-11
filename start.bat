@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set PYEXE=
if exist "%~dp0.venv\Scripts\python.exe" set PYEXE=%~dp0.venv\Scripts\python.exe
if not defined PYEXE if defined PYTHON set PYEXE=%PYTHON%
if not defined PYEXE (
    where python >nul 2>nul && set PYEXE=python
)
if not defined PYEXE (
    where py >nul 2>nul && set PYEXE=py
)
if not defined PYEXE (
    echo.
    echo   Python not found.
    echo   Run install.bat first, or install Python 3.9+ and tick "Add to PATH".
    echo.
    pause
    exit /b 1
)

"%PYEXE%" app.py %*
if errorlevel 1 pause
