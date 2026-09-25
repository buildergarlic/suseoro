@echo off
setlocal
set "SUSEORO_REVIEW_EXE=%~dp0Suseoro.exe"
if not exist "%SUSEORO_REVIEW_EXE%" set "SUSEORO_REVIEW_EXE=%~dp0..\dist\Suseoro\Suseoro.exe"
if not exist "%SUSEORO_REVIEW_EXE%" (
  echo Build the portable application first, then run Start-Review.cmd again.
  pause
  exit /b 1
)
start "" "%SUSEORO_REVIEW_EXE%" --browser --data-dir "%LOCALAPPDATA%\Suseoro-Review-2.2.1"
