@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :setup
goto :run

:setup
echo First run: installing dependencies, this may take a minute...
python -m venv .venv
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto :error
goto :run

:run
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
exit /b 0

:error
echo.
echo Setup failed. Install Python 3.12 or newer, with "Add python.exe to PATH" checked.
pause
exit /b 1
