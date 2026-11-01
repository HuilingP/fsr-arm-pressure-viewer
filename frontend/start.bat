@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto ready
where py >nul 2>nul
if errorlevel 1 (
  python -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 goto fail
:ready
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3,10))"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" launch.py %*
if errorlevel 1 goto fail
exit /b 0
:fail
echo Setup failed. Install Python 3.10 or newer and try again.
pause
exit /b 1
