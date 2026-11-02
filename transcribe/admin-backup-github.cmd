@echo off
rem admin: back up this project to GitHub beartolove/ai_solution (folder transcribe)
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\backup-github.ps1" %*
