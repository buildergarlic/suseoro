@echo off
set ROOT=%~dp0
set PIDFILE=%ROOT%run\server.pid

if exist "%PIDFILE%" (
  set /p PID=<"%PIDFILE%"
  taskkill /PID %PID% /F >nul 2>&1
  del "%PIDFILE%" >nul 2>&1
  echo stopped
  exit /b 0
)

echo server pid not found

