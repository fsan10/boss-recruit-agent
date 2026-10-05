@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 1
if not exist "runtime\logs" mkdir "runtime\logs"
".venv\Scripts\python.exe" -m recruit_agent serve >> "runtime\logs\service.log" 2>&1
exit /b %errorlevel%
