@echo off
cd /d "%~dp0"

REM Kill any stale LB processes from a previous session before starting fresh.
REM This prevents the single-instance lock (port 47923) from blocking startup
REM after an unclean shutdown or desktop restart.
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
    wmic process where "ProcessId=%%~p" get ExecutablePath 2>nul | findstr /i "little-brother" >nul 2>&1
    if not errorlevel 1 taskkill /pid %%~p /f >nul 2>&1
)

REM Record that start.bat actually ran and when (boot-time evidence).
echo [%date% %time%] start.bat launching LB: app, watchdog, tray, tunnel >> little_brother\logs\boot.log

start "LB-App" venv\Scripts\pythonw.exe -m little_brother
start "LB-Watchdog" venv\Scripts\pythonw.exe tools\watchdog.py
start "LB-Tray" venv\Scripts\pythonw.exe tools\tray.py
start "LB-Tunnel" venv\Scripts\pythonw.exe tools\tunnel_keeper.py
