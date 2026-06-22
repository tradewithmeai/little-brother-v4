@echo off
cd /d "%~dp0"

REM Kill any stale LB processes regardless of which Python interpreter they use.
REM The old filter (ExecutablePath containing "little-brother") missed processes
REM launched by the system Python, which held ports 47923/5001 and blocked venv ones.
powershell -NoProfile -Command "Get-WmiObject Win32_Process -Filter 'Name = ''pythonw.exe''' | Where-Object { $_.CommandLine -match 'little_brother|watchdog\.py|tray\.py|tunnel_keeper\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Record that start.bat actually ran and when (boot-time evidence).
echo [%date% %time%] start.bat launching LB: app, watchdog, tray, tunnel >> little_brother\logs\boot.log

start "LB-App" venv\Scripts\pythonw.exe -m little_brother
start "LB-Watchdog" venv\Scripts\pythonw.exe tools\watchdog.py
start "LB-Tray" venv\Scripts\pythonw.exe tools\tray.py
start "LB-Tunnel" venv\Scripts\pythonw.exe tools\tunnel_keeper.py
