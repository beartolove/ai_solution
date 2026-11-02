@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 影片下載器

echo ============================================
echo   影片下載器 啟動中...
echo ============================================

rem ---- 1. 找 Python：需要 3.10 以上（3.9 只裝得到舊版 OpenVINO，語音辨識載入模型會當掉）
rem      依序找 3.11（建議，與語音辨識模型相同）、3.12、3.13、3.10
set "PY="
for %%V in (3.11 3.12 3.13 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [錯誤] 找不到 Python 3.10 以上版本。
  echo        請到 https://www.python.org/downloads/ 安裝 Python 3.11，安裝時請勾選「Add python.exe to PATH」。
  pause
  exit /b 1
)

rem ---- 2. 建立虛擬環境並安裝套件
rem      舊版 Python 建的 .venv 改名保留（不刪除），再用新版 Python 重建
if not exist ".venv\Scripts\python.exe" goto mkvenv
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto venvok
set "OLD=.venv-old"
if exist "%OLD%" set "OLD=.venv-old-%RANDOM%"
echo [注意] 目前的執行環境是用舊版 Python 建立的，改名為 %OLD% 保留（不刪除），改用 %PY% 重建...
ren ".venv" "%OLD%" || (echo [錯誤] 無法改名 .venv，請先關閉其他使用中的視窗再試 & pause & exit /b 1)
:mkvenv
echo [1/3] 建立 Python 虛擬環境（%PY%）...
%PY% -m venv .venv || (echo [錯誤] 建立虛擬環境失敗 & pause & exit /b 1)
:venvok
set "VPY=.venv\Scripts\python.exe"
rem requirements.txt 有變動（例如新增套件）時自動補裝
fc /b requirements.txt ".venv\.installed" >nul 2>nul || del ".venv\.installed" >nul 2>nul
if not exist ".venv\.installed" (
  echo [2/3] 安裝必要套件（約 1 分鐘）...
  "%VPY%" -m pip install --upgrade pip >nul
  "%VPY%" -m pip install -r requirements.txt || (echo [錯誤] 套件安裝失敗 & pause & exit /b 1)
  copy /y requirements.txt ".venv\.installed" >nul
)
rem 語音辨識元件（第一次使用時才安裝）：已裝但版本太舊時自動更新，避免載入新版模型時當掉
"%VPY%" -c "import importlib.metadata as m, sys; v = m.version('openvino-genai'); sys.exit(0 if tuple(int(x) for x in v.split('.')[:2]) < (2026, 4) else 1)" >nul 2>nul
if not errorlevel 1 (
  echo 更新語音辨識元件 openvino-genai 到 2026.4 以上...
  "%VPY%" -m pip install -U "openvino-genai>=2026.4"
)

rem ---- 3. 檢查 ffmpeg
where ffmpeg >nul 2>nul && goto run
if exist "tools\ffmpeg\" (
  for /d %%D in (tools\ffmpeg\*) do if exist "%%D\bin\ffmpeg.exe" goto run
)
echo [3/3] 下載 ffmpeg（影音合併與字幕轉檔需要，約 90MB）...
if not exist tools mkdir tools
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'tools\ffmpeg.zip';" ^
  "Expand-Archive -Path 'tools\ffmpeg.zip' -DestinationPath 'tools\ffmpeg' -Force;" ^
  "Remove-Item 'tools\ffmpeg.zip'"
if errorlevel 1 (
  echo [警告] ffmpeg 自動下載失敗，可改用指令安裝：winget install Gyan.FFmpeg
)

:run
rem ---- 關閉仍在執行的舊版下載器（避免新版無法啟動、畫面卻連到舊版）
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo.
echo 啟動完成後會自動開啟瀏覽器：http://127.0.0.1:8765
echo 要結束程式，請直接關閉這個視窗。
echo.
"%VPY%" run.py
pause
