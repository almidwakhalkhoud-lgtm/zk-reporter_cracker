@echo off
chcp 65001 >nul
echo ================================================
echo    نصب ربات گزارش تردد - سیستم مغازه
echo ================================================
echo.

rem --- پیدا کردن پایتون: اول python بعد py ---
set PYEXE=
python --version >nul 2>nul && set PYEXE=python
if not defined PYEXE py --version >nul 2>nul && if not defined PYEXE set PYEXE=py
if not defined PYEXE (
  echo [!] Python پیدا نشد.
  echo     از python.org نصب کنید و تیک "Add python.exe to PATH" را بزنید،
  echo     بعد سیستم را ری‌استارت کنید و دوباره این فایل را اجرا کنید.
  pause
  exit /b 1
)
echo [OK] پایتون پیدا شد: %PYEXE%

echo [1/3] نصب کتابخانه اتصال به دستگاه...
%PYEXE% -m pip install --quiet pyzk
if errorlevel 1 (
  echo [!] نصب pyzk ناموفق بود. اینترنت را چک کنید.
  pause
  exit /b 1
)
echo     نصب شد.

echo [2/3] ثبت اجرای خودکار با روشن شدن ویندوز...
set SCRIPT_DIR=%~dp0
if "%PYEXE%"=="py" (set PYW=pyw) else (set PYW=pythonw)
schtasks /create /f /tn "ZK Attendance Bot" /sc onlogon /rl limited ^
  /tr "cmd /c cd /d \"%SCRIPT_DIR%\" && %PYW% bot.py" >nul
if errorlevel 1 (
  echo [!] ثبت وظیفه ناموفق بود - run.bat را در پوشه Startup کپی کنید.
) else (
  echo     ثبت شد: با هر ورود به ویندوز، ربات خودکار اجرا می‌شود.
)

echo [3/3] اجرای ربات برای تست...
echo.
echo برای توقف: Ctrl+C  یا بستن همین پنجره
echo.
%PYEXE% bot.py
pause
