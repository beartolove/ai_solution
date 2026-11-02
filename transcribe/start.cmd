@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Transcribe
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Please run setup.cmd first.
  pause
  exit /b 1
)
rem Close an older instance still listening on the same port
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo Starting... the browser will open http://127.0.0.1:8766
echo Close this window to stop the program.
".venv\Scripts\python.exe" -m app.web
pause
