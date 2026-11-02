@echo off
rem admin-only: one-time download of pyannote speaker diarization model
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\download-pyannote.ps1"
