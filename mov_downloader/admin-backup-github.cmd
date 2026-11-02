@echo off
rem admin: back up this project to GitHub beartolove/ai_solution (folder mov_downloader)
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\backup-github.ps1" %*
