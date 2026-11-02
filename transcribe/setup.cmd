@echo off
rem setup.cmd - keeps the window open (-NoExit) so any error stays visible
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
