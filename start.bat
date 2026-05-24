@echo off
cd /d "%~dp0"
start "LB-App" venv\Scripts\pythonw.exe -m little_brother
start "CF-Tunnel" cloudflared tunnel run little-brother
