@echo off
cd /d "%~dp0"
set PYEXE=
python --version >nul 2>nul && set PYEXE=python
if not defined PYEXE py --version >nul 2>nul && if not defined PYEXE set PYEXE=py
if not defined PYEXE (
  echo Python پیدا نشد - از python.org نصب کنید.
  pause
  exit /b 1
)
%PYEXE% bot.py
pause
