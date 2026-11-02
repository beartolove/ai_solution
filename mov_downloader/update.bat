@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 更新 yt-dlp 與相關套件...
".venv\Scripts\python.exe" -m pip install -U -r requirements.txt
rem 有用過語音辨識（已安裝 openvino-genai）才一起更新
".venv\Scripts\python.exe" -m pip show openvino-genai >nul 2>nul && ".venv\Scripts\python.exe" -m pip install -U "openvino-genai>=2026.4"
echo 完成！
pause
