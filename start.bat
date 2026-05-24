@echo off
cd /d "%~dp0"
start "LB-App" venv\Scripts\pythonw.exe -m little_brother
start "LB-Watchdog" venv\Scripts\pythonw.exe tools\watchdog.py
