@echo off
setlocal
cd /d "%~dp0"
echo ================================================
echo    ZK Attendance Bot - Installer
echo ================================================
echo.

rem --- find Python: try "python" first, then "py" ---
set PYEXE=
python --version >nul 2>nul && set PYEXE=python
if not defined PYEXE py --version >nul 2>nul && if not defined PYEXE set PYEXE=py
if not defined PYEXE (
  echo [ERROR] Python was not found.
  echo         Install it from python.org and CHECK "Add python.exe to PATH",
  echo         then restart the PC and run this file again.
  pause
  exit /b 1
)
echo [OK] Python found: %PYEXE%

if "%PYEXE%"=="py" (set PYWEXE=pyw) else (set PYWEXE=pythonw)

echo [0/6] Stopping any old running copies of the bot...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo        done

echo [1/6] Installing the device library (pyzk)...
%PYEXE% -m pip install --quiet pyzk
if errorlevel 1 (
  echo [WARN] pip install failed. The bot will retry automatically on startup.
)

echo [2/6] Creating auto-start in the Windows Startup folder...
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
> "%STARTUP%\zk-attendance-bot.vbs" echo CreateObject("WScript.Shell").Run "%PYWEXE% ""%~dp0bot.py""", 0, False
if exist "%STARTUP%\zk-attendance-bot.vbs" (echo        done - starts at EVERY Windows logon) else (echo [WARN] could not create startup file)

echo [3/6] Registering auto-start at Windows logon (extra layer)...
schtasks /create /f /tn "ZK Attendance Bot" /sc onlogon ^
  /tr "%PYWEXE% \"%~dp0bot.py\"" >nul
if errorlevel 1 (echo [SKIP] needs admin - not required, startup folder covers it) else (echo        done)

echo [4/6] Registering watchdog (restarts the bot within 1 minute if it stops)...
schtasks /create /f /tn "ZK Attendance Bot Watchdog" /sc minute /mo 1 ^
  /tr "%PYWEXE% \"%~dp0bot.py\"" >nul
if errorlevel 1 (echo [WARN] could not register watchdog task) else (echo        done)

echo [5/6] Disabling Windows sleep so the bot never pauses...
powercfg /change standby-timeout-ac 0 >nul 2>nul
powercfg /change hibernate-timeout-ac 0 >nul 2>nul
echo        done (monitor may still turn off - that is fine)

echo [6/6] Starting the bot now (hidden, no window)...
start "" %PYWEXE% "%~dp0bot.py"

echo.
echo ================================================
echo  All done! The bot is running in the background.
echo  - It starts automatically when Windows starts.
echo  - It restarts itself within 5 minutes if it stops.
echo  - You can safely CLOSE this window now.
echo  - Log file: %~dp0bot.log
echo ================================================
pause
