<#
  backup-github.ps1 — 把本專案備份到 GitHub：beartolove/ai_solution 的 mov_downloader 資料夾（1.27.0，照 transcribe 專案的做法）
  由 admin-backup-github.cmd 啟動，視窗不會自動關閉。
  做法：在專案旁邊放一份 ai_solution 的工作複本（預設 D:\code_projects\ai_solution），
        把專案複製到它的 mov_downloader 資料夾 → 提交 → 推送。專案本身不動（檔案是硬連結，不在專案內執行 git）。
  不上傳（與 .gitignore 相同）：.env（AI 金鑰）、data（設定、Cookie 登入資料、隱藏影片密碼）、download（影片）、
        .venv、.venv-old*、.cache、tools（ffmpeg）、__pycache__
  注意：ai_solution 是公開儲存庫。複製完會再檢查一次，萬一 .env 或 data 被複製過去就停止、不提交。
#>
param(
    [string]$Repo = 'https://github.com/beartolove/ai_solution.git',
    [string]$Clone = '',
    [string]$Sub = 'mov_downloader'
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = Split-Path $PSScriptRoot -Parent
if (-not $Clone) { $Clone = Join-Path (Split-Path $root -Parent) 'ai_solution' }
$ver = (Get-Content (Join-Path $root 'VERSION') -TotalCount 1).Trim()
Write-Host "=== 備份到 GitHub：mov_downloader v$ver ===" -ForegroundColor Cyan
Write-Host "目的地：$Repo 的 $Sub 資料夾（公開儲存庫）" -ForegroundColor DarkGray
Write-Host "工作複本：$Clone" -ForegroundColor DarkGray
Write-Host ''

function Fail([string]$msg) { Write-Host "[錯誤] $msg" -ForegroundColor Red; Write-Host '處理後再雙擊一次 admin-backup-github.cmd。' -ForegroundColor Yellow; exit 1 }

# 1. Git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host '[缺] 這台電腦沒有 Git。' -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $go = Read-Host '現在用 winget 安裝 Git for Windows？(Y/N)'
        if ($go -match '^[Yy]') {
            winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
            Write-Host '安裝完成後請關閉這個視窗，再雙擊一次 admin-backup-github.cmd（新視窗才找得到 git）。' -ForegroundColor Cyan
            exit 0
        }
    }
    Fail '請到 https://git-scm.com/download/win 安裝 Git for Windows（全部用預設選項即可）。'
}
Write-Host ("[OK] {0}" -f (git --version)) -ForegroundColor Green

# 2. 工作複本（第一次 clone，之後 pull）
if (-not (Test-Path (Join-Path $Clone '.git'))) {
    if ((Test-Path $Clone) -and (Get-ChildItem $Clone -Force | Select-Object -First 1)) {
        Fail "$Clone 已經存在，但不是 Git 工作複本。請換個位置（admin-backup-github.cmd -Clone 其他路徑）或自行確認那個資料夾。"
    }
    Write-Host '第一次備份：下載 ai_solution 工作複本…' -ForegroundColor Cyan
    git clone $Repo $Clone
    if ($LASTEXITCODE -ne 0) { Fail '下載 ai_solution 失敗，請確認網路與 GitHub 帳號權限。' }
} else {
    Write-Host '更新工作複本…' -ForegroundColor Cyan
    git -C $Clone pull --ff-only
    if ($LASTEXITCODE -ne 0) { Fail "工作複本無法更新（可能在 $Clone 有手動修改）。請自行處理後再試。" }
}
git -C $Clone config core.quotepath false

# 3. 提交者名稱（只設定在這個工作複本）
if (-not (git -C $Clone config user.name)) {
    $n = Read-Host '提交者名稱（顯示在 GitHub 紀錄，例：beartolove）'
    if (-not $n) { $n = 'beartolove' }
    git -C $Clone config user.name $n
}
if (-not (git -C $Clone config user.email)) {
    $e = Read-Host '提交者 email（建議用 GitHub 的 noreply 地址；直接按 Enter 用 beartolove@users.noreply.github.com）'
    if (-not $e) { $e = 'beartolove@users.noreply.github.com' }
    git -C $Clone config user.email $e
}

# 4. 複製專案（鏡像：專案刪掉的檔案，備份也跟著移除；排除的資料夾不會被動到）
$dst = Join-Path $Clone $Sub
Write-Host ''
Write-Host "複製專案到 $dst …" -ForegroundColor Cyan
$xd = @('.git', '.venv', '.cache', 'tools', 'data', 'download') | ForEach-Object { Join-Path $root $_ }
$xf = @('.env') | ForEach-Object { Join-Path $root $_ }
robocopy $root $dst /MIR /XD $xd '.venv-old*' '__pycache__' /XF $xf '*.pyc' '.*.tmp' /R:1 /W:1 /NFL /NDL /NJH /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "複製失敗（robocopy 代碼 $LASTEXITCODE）。" }
# 公開儲存庫：再確認一次金鑰與登入資料沒有被複製過去
foreach ($bad in @('.env', 'data', 'download')) {
    if (Test-Path (Join-Path $dst $bad)) { Fail "備份資料夾裡出現 $bad（含金鑰、登入資料或影片），已停止、沒有提交。請刪除 $(Join-Path $dst $bad) 後再試。" }
}
Write-Host '[OK] 複製完成（不含 .env 金鑰、data 設定與 Cookie、下載的影片、執行環境）' -ForegroundColor Green

# 5. 提交與推送
git -C $Clone add -A -- $Sub
git -C $Clone diff --cached --quiet -- $Sub
if ($LASTEXITCODE -eq 0) {
    Write-Host '這一版和 GitHub 上的備份相同，不需要提交。' -ForegroundColor Green
} else {
    $cnt = @(git -C $Clone diff --cached --name-only -- $Sub).Count
    Write-Host "有 $cnt 個檔案變更，提交中…"
    $msgFile = Join-Path $env:TEMP 'mov_downloader-backup-msg.txt'
    $msg = "mov_downloader v$ver 備份（$(Get-Date -Format 'yyyy-MM-dd HH:mm')，$env:COMPUTERNAME）`n`n詳見 mov_downloader/CHANGELOG.md"
    [System.IO.File]::WriteAllText($msgFile, $msg, (New-Object System.Text.UTF8Encoding $false))
    git -C $Clone commit -q -F $msgFile
    Remove-Item $msgFile -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) { Fail '提交失敗。' }
}
$ahead = [int](git -C $Clone rev-list --count '@{u}..HEAD' 2>$null)
if ($ahead -gt 0) {
    Write-Host '推送到 GitHub…（第一次會開瀏覽器請你登入 GitHub）' -ForegroundColor Cyan
    git -C $Clone push
    if ($LASTEXITCODE -ne 0) {
        Write-Host '推送被拒絕，先合併 GitHub 上的新內容再推送一次…' -ForegroundColor Yellow
        git -C $Clone pull --rebase
        git -C $Clone push
        if ($LASTEXITCODE -ne 0) { Fail '推送失敗。請確認已登入有寫入權限的 GitHub 帳號。' }
    }
    Write-Host ''
    Write-Host "[OK] 已備份 v$ver：https://github.com/beartolove/ai_solution/tree/main/$Sub" -ForegroundColor Green
} else {
    Write-Host "[OK] GitHub 上已是最新：https://github.com/beartolove/ai_solution/tree/main/$Sub" -ForegroundColor Green
}
