@echo off
rem admin-only: download Google TranslateGemma 4B and convert to OpenVINO (for bilingual subtitles)
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\convert-translator.ps1"
