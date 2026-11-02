<#
  setup.ps1 — 由 setup.cmd 以 -NoExit 啟動，視窗不會自動關閉。
  流程：設定模型位置與媒體檔位置 → 環境初測 → 執行環境（自動）→ 辨識模型 →
        這台的別名與功能（第一次才問）→ 只準備需要的模型 → 模型配置方案（多餘的只告知、不刪除）
  可重複執行；已完成的步驟會自動略過。
#>
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' }),
    [string]$Source = $env:MODELS_SOURCE     # 共用模型來源（網芳／隨身碟），選填
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path $PSScriptRoot -Parent
$ver  = (Get-Content (Join-Path $root 'VERSION') -TotalCount 1).Trim()
# 網頁「參數設定」改過模型位置時，以它為準（沒有手動指定 -ModelsRoot 才套用）
$setFile0 = Join-Path $root 'data\settings.json'
if (-not $PSBoundParameters.ContainsKey('ModelsRoot') -and (Test-Path $setFile0)) {
    try { $mr = (Get-Content $setFile0 -Raw -Encoding UTF8 | ConvertFrom-Json).models_root } catch { $mr = $null }
    if ($mr) { $ModelsRoot = $mr; Write-Host "模型位置採用網頁「參數設定」：$mr" -ForegroundColor DarkGray }
}
Write-Host "=== 影音轉逐字稿 v$ver：環境設定 ===" -ForegroundColor Cyan
Write-Host ("PowerShell {0}　執行原則 {1}" -f $PSVersionTable.PSVersion, (Get-ExecutionPolicy)) -ForegroundColor DarkGray
Write-Host ''

try {
    # 1. 模型位置
    & (Join-Path $PSScriptRoot 'download-models.ps1') -SetEnv -NoPause -ModelsRoot $ModelsRoot
    $env:MODELS_ROOT = $ModelsRoot

    # 1-1. 媒體檔位置（上傳檔、轉檔結果、效能實測暫存；不放在專案內）
    $media = 'D:\mediafiles'; $mediaSrc = '預設'
    if ($env:MEDIA_ROOT) { $media = $env:MEDIA_ROOT; $mediaSrc = '環境變數 MEDIA_ROOT' }
    $setFile = Join-Path $root 'data\settings.json'
    if (Test-Path $setFile) {
        try { $sv = (Get-Content $setFile -Raw -Encoding UTF8 | ConvertFrom-Json).media_root } catch { $sv = $null }
        if ($sv) { $media = $sv; $mediaSrc = '網頁「參數設定」' }
    }
    Write-Host ''
    Write-Host '--- 檔案位置 ---' -ForegroundColor Cyan
    Write-Host "模型位置：$ModelsRoot"
    $drv = [System.IO.Path]::GetPathRoot($media)
    if ($drv -and -not (Test-Path $drv)) {
        Write-Host "[注意] 找不到 $drv 磁碟。媒體檔會暫時放在專案的 mediafiles 資料夾；建議啟動網頁後到「參數設定」改到其他磁碟。" -ForegroundColor Yellow
    } else {
        foreach ($sub in 'uploads', 'benchmark', 'downloads') {
            $d = Join-Path $media $sub
            if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        }
        Write-Host "媒體檔位置：$media（$mediaSrc；可在網頁「參數設定」修改）"
        Write-Host "  uploads   上傳的檔案，每個任務一個資料夾（原始檔、字幕、純文字、MP3）" -ForegroundColor DarkGray
        Write-Host "  benchmark 效能實測暫存" -ForegroundColor DarkGray
        Write-Host "  downloads 從網路下載的影音，每個任務一個資料夾" -ForegroundColor DarkGray
    }
    $legacy = Join-Path $root 'jobs'
    if (Test-Path $legacy) {
        $n = @(Get-ChildItem $legacy -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike 'benchmark_*' -and $_.Name -notlike '_*' }).Count
        if ($n -gt 0) { Write-Host "提示：專案內舊的 jobs 資料夾還有 $n 個任務（0.6.7 起改放媒體檔位置）。確認已複製後可自行刪除。" -ForegroundColor Cyan }
    }

    # 2. 環境初測（決定等級與引擎）
    & (Join-Path $PSScriptRoot 'env-check.ps1') -NoPause -ModelsRoot $ModelsRoot
    $rep  = Join-Path $root "env-reports\$($env:COMPUTERNAME).json"
    $tier = if (Test-Path $rep) { (Get-Content $rep -Raw -Encoding UTF8 | ConvertFrom-Json).Tier } else { 'D' }

    # 3. 執行環境（自動完成，不詢問）
    $envOk = & (Join-Path $PSScriptRoot 'check-tools.ps1') -Tier $tier | Select-Object -Last 1

    # 4. 辨識模型（轉逐字稿必備：從本機 keyboard_free／共用來源複製，都沒有時請管理者轉換）
    Write-Host ''
    Write-Host '--- 辨識模型 ---' -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'prepare-ov-models.ps1') -ModelsRoot $ModelsRoot -Source $Source

    # 5. 這台的功能與模型配置方案（0.14.0）：依 models.json＋功能需求＋電腦環境，只準備需要的模型
    #    NVIDIA 專用模型（faster-whisper）等引擎完成後才會列入方案，現在不下載
    $planMiss = -1
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    if ($envOk -and (Test-Path $venvPy)) {
        if ($Source) { $env:MODELS_SOURCE = $Source }
        Push-Location $root
        try {
            Write-Host ''
            & $venvPy -m app.modelplan configure
            Write-Host ''
            Write-Host '--- 準備這台需要的模型 ---' -ForegroundColor Cyan
            & $venvPy -m app.modelplan prepare
            & $venvPy -m app.modelplan report
            $planMiss = $LASTEXITCODE
        } finally { Pop-Location }
    } else {
        Write-Host '[注意] 執行環境還沒就緒，略過功能選擇與模型方案。' -ForegroundColor Yellow
    }

    $ovMiss = [int](& (Join-Path $PSScriptRoot 'prepare-ov-models.ps1') -ModelsRoot $ModelsRoot -Status | Select-Object -Last 1)
    Write-Host ''
    if (-not (Test-Path (Join-Path $root "env-reports\$($env:COMPUTERNAME).benchmark.json"))) {
        Write-Host '提示：這台電腦還沒做效能實測。請雙擊 start.cmd，到網頁「本機效能」按「開始實測」（約 5–10 分鐘），找出這台的最佳配置。' -ForegroundColor Cyan
    }
    if ($envOk -and $ovMiss -eq 0 -and $planMiss -eq 0) { Write-Host '全部就緒，可以關閉這個視窗。' -ForegroundColor Green }
    else { Write-Host '仍有項目未完成，請依上方黃色／紅色訊息處理後再執行一次 setup.cmd。' -ForegroundColor Yellow }
} catch {
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "位置：$($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
}
