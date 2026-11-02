<#
  model-report.ps1 — 顯示這台的模型配置方案（0.14.0 起改由 app\modelplan.py 依 models.json 判斷）
  只告知，不刪除任何檔案。setup.cmd 會自動執行；也可手動：
    powershell -ExecutionPolicy Bypass -File .\scripts\model-report.ps1
#>
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = Split-Path $PSScriptRoot -Parent
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Write-Host '找不到執行環境，請先執行 setup.cmd' -ForegroundColor Yellow; return }
Push-Location $root
try { & $venvPy -m app.modelplan report } finally { Pop-Location }
