@echo off
setlocal
set ROOT=%~dp0
set EXE=%ROOT%SuseoroAI.exe

if exist "%EXE%" (
  start "" "%EXE%"
  exit /b 0
)

if exist "%ROOT%\.venv\Scripts\python.exe" (
  start "" "%ROOT%\.venv\Scripts\python.exe" "%ROOT%launcher.py"
  exit /b 0
)

python "%ROOT%launcher.py"
if errorlevel 1 pause
