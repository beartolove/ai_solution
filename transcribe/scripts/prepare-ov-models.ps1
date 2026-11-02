<#
  prepare-ov-models.ps1 — 準備 OpenVINO 格式辨識模型到 $MODELS_ROOT\asr\ov\
  取得順序（找到就停）：
    1. 已存在於 D:\models\asr\ov\<模型>      → 略過
    2. 本機 keyboard_free 已轉好的模型       → 複製（%APPDATA%\KeyboardFree\models\ov\<模型>）
    3. 共用來源 -Source（網芳／隨身碟）      → 複製（<Source>\asr\ov\<模型>）
    4. 都沒有 → 提示管理者執行 admin-convert-openvino.cmd 轉換一次
  一般使用者不需要 Hugging Face 帳號。
#>
[CmdletBinding()]
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' }),
    [string]$Source = $env:MODELS_SOURCE,
    [switch]$Status    # 只回傳缺少的模型數
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$models   = @('breeze-asr-25', 'large-v3-turbo')
$must     = 'openvino_encoder_model.xml'
$ovRoot   = Join-Path $ModelsRoot 'asr\ov'
$appdata  = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $env:USERPROFILE 'AppData\Roaming' }
$kbfRoot  = Join-Path $appdata 'KeyboardFree\models\ov'

function Test-Model([string]$dir) { Test-Path (Join-Path $dir $must) }

$missing = @($models | Where-Object { -not (Test-Model (Join-Path $ovRoot $_)) })
if ($Status) { return $missing.Count }

foreach ($m in $models) {
    $dest = Join-Path $ovRoot $m
    if (Test-Model $dest) { Write-Host "[OK] OpenVINO 模型 $m" -ForegroundColor Green; continue }

    $candidates = @()
    $candidates += [pscustomobject]@{ Label = '本機 keyboard_free'; Path = (Join-Path $kbfRoot $m) }
    if ($Source) { $candidates += [pscustomobject]@{ Label = "共用來源 $Source"; Path = (Join-Path $Source "asr\ov\$m") } }

    $done = $false
    foreach ($c in $candidates) {
        if (Test-Model $c.Path) {
            Write-Host "複製 $m（來源：$($c.Label)）→ $dest ..." -ForegroundColor Cyan
            New-Item -ItemType Directory -Force -Path $dest | Out-Null
            # robocopy：大檔較穩，可續傳；結束碼 < 8 都算成功
            & robocopy $c.Path $dest /E /Z /NFL /NDL /NJH /NP | Out-Null
            if ($LASTEXITCODE -lt 8 -and (Test-Model $dest)) {
                Write-Host "[OK] OpenVINO 模型 $m 已就緒" -ForegroundColor Green
                $done = $true; break
            } else {
                Write-Host "複製失敗（robocopy 代碼 $LASTEXITCODE）" -ForegroundColor Yellow
            }
        }
    }
    if (-not $done) {
        Write-Host "[缺] OpenVINO 模型 $m：本機找不到來源。" -ForegroundColor Yellow
        Write-Host '     請從已裝好的電腦複製 D:\models\asr\ov 整個資料夾過來，' -ForegroundColor Yellow
        Write-Host '     或由管理者雙擊 admin-convert-openvino.cmd 轉換一次。' -ForegroundColor Yellow
    }
}
