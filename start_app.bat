@echo off
rem Launch the desktop console. Self-minimize to avoid a cmd console flash,
rem then hand off to pythonw (which never opens a console).
if not "%~1"=="hidden" (
  start /min "" "%~f0" hidden
  exit /b
)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "app_platform.py"
exit /b
