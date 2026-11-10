@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  start "" notepad.exe "docs\setup.md"
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -X utf8 -m chattrace.desktop
