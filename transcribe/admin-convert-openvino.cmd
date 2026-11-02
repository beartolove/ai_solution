@echo off
rem admin-only: convert Breeze-ASR-25 / whisper-large-v3-turbo to OpenVINO (only if no machine has them yet)
cd /d "%~dp0"
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\convert-openvino.ps1"
