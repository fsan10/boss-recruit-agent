@echo off
setlocal
cd /d "%~dp0"
set "RECRUIT_BOOTSTRAP=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined RECRUIT_BOOTSTRAP_PYTHON set "RECRUIT_BOOTSTRAP=%RECRUIT_BOOTSTRAP_PYTHON%"
if not exist ".venv\Scripts\python.exe" (
  if not exist "%RECRUIT_BOOTSTRAP%" (
    echo Python 3.12 not found. Install it or set RECRUIT_BOOTSTRAP_PYTHON to its full path.
    goto failed
  )
  "%RECRUIT_BOOTSTRAP%" -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
  if errorlevel 1 goto failed
  "%RECRUIT_BOOTSTRAP%" -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 ".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.lock
if errorlevel 1 goto failed
echo Recruit workbench: http://127.0.0.1:8765 on this computer.
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m recruit_agent serve %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Check the error above and START_HERE.md.
pause
exit /b 1
