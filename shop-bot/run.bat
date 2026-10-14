@echo off
rem Manual run with visible console - for debugging only.
rem Normal operation does NOT need this file; the bot runs hidden automatically.
cd /d "%~dp0"
set PYEXE=
python --version >nul 2>nul && set PYEXE=python
if not defined PYEXE py --version >nul 2>nul && if not defined PYEXE set PYEXE=py
if not defined PYEXE (
  echo [ERROR] Python was not found. Install it from python.org first.
  pause
  exit /b 1
)
%PYEXE% bot.py
pause
