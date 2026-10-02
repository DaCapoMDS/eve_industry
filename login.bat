@echo off
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" eve_invention.py --login
start "" "output\report.html"
pause
