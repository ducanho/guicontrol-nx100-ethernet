@echo off
cd /d "%~dp0nx100_web_control"
"..\.venv\Scripts\python.exe" app.py %*
pause
