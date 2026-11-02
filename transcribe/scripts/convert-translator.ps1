<#
  convert-translator.ps1 — 管理者專用：下載 Google TranslateGemma 4B 並轉成 OpenVINO（int8），供「雙語對照」翻譯成中文。
  模型需要在 Hugging Face 同意條款：先登入並開啟 https://huggingface.co/google/translategemma-4b-it 按同意，
  再到 https://huggingface.co/settings/tokens 建立 Read 類型的 Token。Token 只在本次執行使用，不會寫入任何檔案。
  轉換工具安裝在獨立的 .venv-convert，不影響專案 .venv。轉好後其他電腦複製 D:\models\llm 即可，不需要帳號。
#>
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' })
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo   = 'google/translategemma-4b-it'
$root   = Split-Path $PSScriptRoot -Parent
$cvVenv = Join-Path $root '.venv-convert'
$cvPy   = Join-Path $cvVenv 'Scripts\python.exe'
$cvCli  = Join-Path $cvVenv 'Scripts\optimum-cli.exe'
$dest   = Join-Path $ModelsRoot 'llm\translategemma-4b-it'
$env:HF_HOME = Join-Path $ModelsRoot '.hf-cache'

Write-Host '=== 下載並轉換翻譯模型 TranslateGemma 4B（管理者專用，約需 20–40 分鐘） ===' -ForegroundColor Cyan
Write-Host "來源：$repo（Google，Gemma 使用條款）"
Write-Host "存放：$dest"
Write-Host ''
try {
    if (Test-Path (Join-Path $dest 'prompt_templates.json')) {
        Write-Host '模型已存在，不需重新轉換。' -ForegroundColor Green
        return
    }
    if (-not (Test-Path $cvPy)) {
        Write-Host '建立轉換用虛擬環境 .venv-convert ...' -ForegroundColor Cyan
        & python -m venv $cvVenv
    }
    Write-Host '安裝／更新轉換工具 optimum-intel（第一次約需 5–10 分鐘）...' -ForegroundColor Cyan
    & $cvPy -m pip install --upgrade pip -q
    & $cvPy -m pip install -U "optimum-intel[openvino]" nncf "transformers>=4.50" accelerate pillow -q
    if (-not (Test-Path $cvCli)) { throw '轉換工具安裝失敗。' }

    $sec = Read-Host '請貼上 Hugging Face Token（輸入時不會顯示）' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $env:HF_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if (-not $env:HF_TOKEN) { throw '未輸入 Token。' }

    Write-Host "轉換 $repo → $dest（下載約 8GB 原始模型，轉換約 10–20 分鐘）..." -ForegroundColor Cyan
    & $cvCli export openvino --model $repo --task image-text-to-text --weight-format int8 $dest
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $dest 'openvino_language_model.xml'))) {
        throw '轉換失敗。常見原因：(1) 尚未在模型頁面按同意條款 (2) Token 貼錯或已失效 (3) 網路被擋 (4) 磁碟空間不足（需約 20GB）。'
    }
    Write-Host '產生翻譯提示範本 ...' -ForegroundColor Cyan
    & $cvPy (Join-Path $PSScriptRoot 'translator_prepare.py') $repo $dest
    if (-not (Test-Path (Join-Path $dest 'prompt_templates.json'))) { throw '提示範本產生失敗，請把畫面截圖回報。' }
    @"
本資料夾模型：$repo（OpenVINO int8）
授權：Gemma Terms of Use（https://ai.google.dev/gemma/terms），可於內部重新發佈，需附上使用條款
出處：Google（https://huggingface.co/$repo）
轉換日期：$(Get-Date -Format 'yyyy-MM-dd')
"@ | Set-Content -Path (Join-Path $dest 'ATTRIBUTION.txt') -Encoding UTF8
    Write-Host ''
    Write-Host '[OK] 翻譯模型準備完成。其他電腦複製 D:\models\llm 即可使用。' -ForegroundColor Green
    Write-Host "原始模型下載快取（約 8GB）在 $env:HF_HOME\hub\models--google--translategemma-4b-it，確認可用後可自行刪除。" -ForegroundColor DarkGray
    Write-Host '轉換工具 .venv-convert（約 2–3GB）也可在確認後自行刪除。' -ForegroundColor DarkGray
} catch {
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item Env:\HF_TOKEN -ErrorAction SilentlyContinue
}
