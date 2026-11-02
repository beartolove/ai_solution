<#
  download-pyannote.ps1 — 管理者專用：一次性下載 pyannote 說話者辨識模型
  一般使用者不需要執行，也不需要 Hugging Face 帳號。
  前置（只做一次）：
    1. 登入 Hugging Face，開啟 https://huggingface.co/pyannote/speaker-diarization-community-1
       按頁面上的同意條款（Agree and access repository）
    2. 到 https://huggingface.co/settings/tokens 建立 Token，類型選「Read」
  Token 只在本次執行使用，不會寫入任何檔案。
#>
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' })
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo = 'pyannote/speaker-diarization-community-1'
$dest = Join-Path $ModelsRoot 'diarization\speaker-diarization-community-1'

Write-Host '=== 下載 pyannote 說話者辨識模型（管理者專用） ===' -ForegroundColor Cyan
Write-Host "來源：$repo"
Write-Host "存放：$dest"
Write-Host ''

try {
    if (Test-Path (Join-Path $dest 'config.yaml')) {
        Write-Host '模型已存在，不需重新下載。' -ForegroundColor Green
        return
    }

    $py = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' } elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { $null }
    if (-not $py) { throw '找不到 Python，請先安裝 Python 3.10 以上。' }
    & $py -c "import huggingface_hub" 2>$null
    if ($LASTEXITCODE -ne 0) { & $py -m pip install -U huggingface_hub }

    $sec = Read-Host '請貼上 Hugging Face Token（輸入時不會顯示）' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $env:HF_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if (-not $env:HF_TOKEN) { throw '未輸入 Token。' }

    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # 注意：Windows PowerShell 5.1 傳參數給外部程式時會吃掉雙引號，所以程式碼內不可含引號。
    # huggingface_hub 會自動讀取環境變數 HF_TOKEN，不需明確傳入。
    $code = 'import sys; from huggingface_hub import snapshot_download as s; s(repo_id=sys.argv[1], local_dir=sys.argv[2])'
    & $py -c $code $repo $dest
    if ($LASTEXITCODE -ne 0) {
        throw '下載失敗。常見原因：(1) 尚未在模型頁面按同意條款 (2) Token 貼錯或已失效 (3) 網路被擋。'
    }

    @"
本資料夾模型：$repo
授權：CC-BY-4.0（可於內部重新發佈，需保留出處）
出處：pyannote（https://huggingface.co/$repo）
下載日期：$(Get-Date -Format 'yyyy-MM-dd')
"@ | Out-File -FilePath (Join-Path $dest 'ATTRIBUTION.txt') -Encoding utf8

    Write-Host ''
    Write-Host '下載完成。' -ForegroundColor Green
    Write-Host "其他電腦只要把 $(Join-Path $ModelsRoot 'diarization') 整個資料夾複製過去即可，不需帳號。"
} catch {
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item Env:HF_TOKEN -ErrorAction SilentlyContinue
}
