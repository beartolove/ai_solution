<#
  convert-openvino.ps1 — 管理者專用：把 Whisper 系列模型轉成 OpenVINO 格式（int8）
  只在「沒有任何電腦有現成 OpenVINO 模型」時才需要；轉好後其他電腦複製 D:\models\asr\ov 即可。
  轉換工具（optimum-intel、PyTorch，約 2GB）安裝在獨立的暫存虛擬環境，不影響專案 .venv。
  Breeze-ASR-25 與 Whisper 為公開模型，不需要 Hugging Face 帳號。
#>
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' })
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$jobs = @(
    @{ Name = 'breeze-asr-25';  Repo = 'MediaTek-Research/Breeze-ASR-25' },
    @{ Name = 'large-v3-turbo'; Repo = 'openai/whisper-large-v3-turbo' }
)
$root    = Split-Path $PSScriptRoot -Parent
$cvVenv  = Join-Path $root '.venv-convert'
$cvPy    = Join-Path $cvVenv 'Scripts\python.exe'
$cvCli   = Join-Path $cvVenv 'Scripts\optimum-cli.exe'
$env:HF_HOME = Join-Path $ModelsRoot '.hf-cache'

Write-Host '=== 轉換 OpenVINO 模型（管理者專用，約需 20–40 分鐘） ===' -ForegroundColor Cyan
try {
    if (-not (Test-Path $cvPy)) {
        Write-Host '建立轉換用虛擬環境 .venv-convert ...' -ForegroundColor Cyan
        & python -m venv $cvVenv
    }
    if (-not (Test-Path $cvCli)) {
        Write-Host '安裝轉換工具 optimum-intel（第一次約需 5–10 分鐘）...' -ForegroundColor Cyan
        & $cvPy -m pip install --upgrade pip -q
        & $cvPy -m pip install optimum-intel[openvino] nncf
    }
    if (-not (Test-Path $cvCli)) { throw '轉換工具安裝失敗。' }

    foreach ($j in $jobs) {
        $dest = Join-Path $ModelsRoot "asr\ov\$($j.Name)"
        if (Test-Path (Join-Path $dest 'openvino_encoder_model.xml')) {
            Write-Host "[略過] $($j.Name) 已存在" -ForegroundColor Green; continue
        }
        Write-Host "轉換 $($j.Repo) → $dest ..." -ForegroundColor Cyan
        & $cvCli export openvino --model $j.Repo --task automatic-speech-recognition-with-past --weight-format int8 $dest
        if (Test-Path (Join-Path $dest 'openvino_encoder_model.xml')) { Write-Host "[OK] $($j.Name)" -ForegroundColor Green }
        else { Write-Host "[失敗] $($j.Name)，請把畫面截圖回報。" -ForegroundColor Red }
    }
    Write-Host ''
    Write-Host '完成後可刪除 .venv-convert 資料夾釋放空間（約 2GB）。' -ForegroundColor DarkGray
} catch {
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
}
