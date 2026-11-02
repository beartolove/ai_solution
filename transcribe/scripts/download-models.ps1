<#
  download-models.ps1 — 下載語音轉逐字稿模型到集中模型庫（預設 D:\models）
  用法：
    powershell -ExecutionPolicy Bypass -File .\scripts\download-models.ps1 -SetEnv  # 設定 MODELS_ROOT / HF_HOME（每台電腦一次）
    .\scripts\download-models.ps1 -Tier auto         # 依環境初測結果下載
    .\scripts\download-models.ps1 -Tier B            # 指定等級 A/B/C/D
    .\scripts\download-models.ps1 -Tier all          # 全部下載（建議在主力機做集中庫）
    .\scripts\download-models.ps1 -List              # 只列出清單與狀態
  需求：Python 3.10 以上（huggingface_hub 會自動安裝）
  也可在檔案總管按右鍵「用 PowerShell 執行」，結束時會停住讓你看結果。
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'A', 'B', 'C', 'D', 'all')]
    [string]$Tier = 'auto',
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' }),
    [string]$ReportDir = (Join-Path (Split-Path $PSScriptRoot -Parent) 'env-reports'),
    [switch]$SetEnv,
    [switch]$List,
    [switch]$Status,   # 只回傳此等級尚缺幾個模型，不下載
    [switch]$NoPause   # 由 setup.cmd 或其他腳本呼叫時不暫停
)

try {
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ---------- 環境變數 ----------
if ($SetEnv) {
    [Environment]::SetEnvironmentVariable('MODELS_ROOT', $ModelsRoot, 'User')
    [Environment]::SetEnvironmentVariable('HF_HOME', (Join-Path $ModelsRoot '.hf-cache'), 'User')
    Write-Host "已設定 MODELS_ROOT=$ModelsRoot、HF_HOME=$(Join-Path $ModelsRoot '.hf-cache')（重開終端機後生效）" -ForegroundColor Green
    if (-not $List -and $PSBoundParameters.Keys -notcontains 'Tier') { return }
}
$env:HF_HOME = Join-Path $ModelsRoot '.hf-cache'

# ---------- 模型清單 ----------
# 原則：不使用中國廠商開發的模型（已排除 Qwen3-ASR、SenseVoice 等）
# 等級 C/D 改用 OpenVINO 模型，由 prepare-ov-models.ps1 處理（不從這裡下載）
# Check：用來判斷是否已下載的相對路徑
$catalog = @(
    @{ Key='breeze-ct2';   Name='Breeze-ASR-25（CT2 int8_float16，faster-whisper）'; Repo='shdennlin/breeze-asr-25-ct2';  Include='int8_float16/*'; Files=@();                                Dest='asr\ct2\breeze-asr-25';        Check='int8_float16'; Tiers='A','B' }
    @{ Key='turbo-ct2';    Name='Whisper large-v3-turbo（CT2，faster-whisper）';    Repo='deepdml/faster-whisper-large-v3-turbo-ct2'; Include=''; Files=@();                         Dest='asr\ct2\whisper-large-v3-turbo'; Check='model.bin';   Tiers='A','B' }
)

function Test-Downloaded($m) {
    $p = Join-Path (Join-Path $ModelsRoot $m.Dest) $m.Check
    if (Test-Path $p -PathType Leaf) { return $true }
    if (Test-Path $p -PathType Container) { return [bool](Get-ChildItem $p -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1) }
    return $false
}

if ($List) {
    foreach ($m in $catalog) {
        $s = if (Test-Downloaded $m) { '已下載' } else { '未下載' }
        Write-Host ("[{0}] 等級 {1,-8} {2}" -f $s, ($m.Tiers -join ','), $m.Name)
    }
    return
}

# ---------- 決定等級 ----------
if ($Tier -eq 'auto') {
    $envFile = Join-Path $ReportDir "$($env:COMPUTERNAME).json"
    if (-not (Test-Path $envFile)) {
        Write-Host '尚無環境初測報告，先執行 env-check.ps1 ...' -ForegroundColor Cyan
        & (Join-Path $PSScriptRoot 'env-check.ps1') -ModelsRoot $ModelsRoot -ReportDir $ReportDir -NoPause | Out-Host
    }
    if (Test-Path $envFile) {
        $Tier = (Get-Content $envFile -Raw -Encoding UTF8 | ConvertFrom-Json).Tier
        Write-Host "依環境初測結果，使用等級 $Tier" -ForegroundColor Green
    } else {
        $Tier = 'D'; Write-Host '無法取得初測結果，改用最保守的等級 D' -ForegroundColor Yellow
    }
}

$targets = if ($Tier -eq 'all') { $catalog } else { $catalog | Where-Object { $_.Tiers -contains $Tier } }

if ($Status) {
    $missing = @($targets | Where-Object { -not (Test-Downloaded $_) })
    return $missing.Count
}

# ---------- 下載工具（直接用 Python 的 huggingface_hub，避免 PATH 問題） ----------
$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $py = 'python' }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = 'py' }
if (-not $py) {
    throw '找不到 Python。請到 https://www.python.org 安裝 Python 3.10 以上，安裝時勾選「Add python.exe to PATH」，再重新執行。'
}
& $py -c "import huggingface_hub" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '未安裝 huggingface_hub，自動安裝中...' -ForegroundColor Cyan
    & $py -m pip install -U huggingface_hub
    & $py -c "import huggingface_hub" 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'huggingface_hub 安裝失敗，請手動執行：python -m pip install -U huggingface_hub' }
}
$dlCode = 'import sys; from huggingface_hub import snapshot_download as s; s(repo_id=sys.argv[1], local_dir=sys.argv[2], allow_patterns=(sys.argv[3:] or None))'

# ---------- 執行 ----------
$ok = @(); $skip = @(); $fail = @()
foreach ($m in $targets) {
    if (Test-Downloaded $m) { $skip += $m.Name; Write-Host "略過（已存在）：$($m.Name)"; continue }
    $dest = Join-Path $ModelsRoot $m.Dest
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Write-Host "下載中：$($m.Name) → $dest" -ForegroundColor Cyan

    $patterns = @($m.Files)
    if ($m.Include) { $patterns += $m.Include }
    & $py -c $dlCode $m.Repo $dest @patterns
    if ($LASTEXITCODE -eq 0 -and (Test-Downloaded $m)) { $ok += $m.Name }
    else { $fail += $m.Name; Write-Host "失敗，已略過：$($m.Name)（請確認儲存庫名稱或授權條款）" -ForegroundColor Yellow }
}

Write-Host ''
Write-Host "完成：$($ok.Count) 項　略過：$($skip.Count) 項　失敗：$($fail.Count) 項" -ForegroundColor Green
foreach ($f in $fail) { Write-Host "  失敗：$f" -ForegroundColor Yellow }

} catch {
    Write-Host ''
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "位置：$($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
} finally {
    if (-not $NoPause) { Write-Host ''; Read-Host '按 Enter 鍵關閉視窗' | Out-Null }
}
