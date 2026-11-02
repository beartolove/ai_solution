@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "LOG=%~dp0env-reports\debug-benchmark.log"
echo [admin] Benchmark diagnostics (logs native crashes too), please wait (5-10 min)...
echo Log: %LOG%
> "%LOG%" echo === %DATE% %TIME% ===
".venv\Scripts\python.exe" -c "import sys,platform;print('python',sys.version);import openvino as ov,openvino_genai as og;print('openvino',ov.__version__);print('genai',og.__version__ if hasattr(og,'__version__') else '?');print('devices',ov.Core().available_devices)" >> "%LOG%" 2>&1
echo exit=%ERRORLEVEL% >> "%LOG%"
echo N| ".venv\Scripts\python.exe" -X faulthandler -m app.benchmark >> "%LOG%" 2>&1
echo benchmark exit=%ERRORLEVEL% >> "%LOG%"
echo Done. Exit code %ERRORLEVEL%
pause
