<#
  check-tools.ps1 — 準備執行環境（比照 mov_downloader：只裝用得到的、自動完成）
    1. Python 3.10+（唯一需手動安裝的軟體；會檢查版本，舊版 .venv 改名保留後重建）
    2. 專案 .venv + requirements.txt（requirements 有變動才重裝）
       NVIDIA 等級 A/B 另裝 requirements-nvidia.txt
    3. ffmpeg：系統有就沿用；沒有才下載到 tools\ffmpeg\
    4. yt-dlp 更新到最新版（平台常改版）
  JavaScript 執行環境（Deno／Node.js）只在下載 YouTube 時需要，只提示不強制安裝。
  注意：本檔呼叫外部程式的參數不可含雙引號（Windows PowerShell 5.1 會吃掉）。
#>
[CmdletBinding()]
param(
    [string]$Tier = '',
    [switch]$Quiet
)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$root     = Split-Path $PSScriptRoot -Parent
$venv     = Join-Path $root '.venv'
$venvPy   = Join-Path $venv 'Scripts\python.exe'
$marker   = Join-Path $venv '.installed'
$req      = Join-Path $root 'requirements.txt'
$reqNv    = Join-Path $root 'requirements-nvidia.txt'
$toolsDir = Join-Path $root 'tools'
$ok = $true

function Say([string]$mark, [string]$text, [string]$color) { Write-Host ("{0} {1}" -f $mark, $text) -ForegroundColor $color }

Write-Host ''
Write-Host '--- 執行環境 ---' -ForegroundColor Cyan

# 1. Python（3.10 以上；0.13.7 起真的檢查版本）
#    Python 3.9 只能裝到 OpenVINO 2025.3，載入 2026.4 轉出的模型會直接當掉（2026-11-02 HQO302003 實測）
#    有 py 啟動器時依序找 3.11（與母機相同）、3.12、3.13、3.10；內嵌程式碼不可含雙引號（PS 5.1）
$verCode = 'import sys;print(sys.version_info[0]*100+sys.version_info[1])'
function PyNum([string]$exe, [string[]]$pre) {
    try { $o = & $exe @pre -c $verCode 2>$null | Select-Object -Last 1; if ($o -match '^\d+$') { return [int]$o } } catch { }
    return 0
}
$pyExe = $null; $pyPre = @(); $pyNum = 0; $oldNum = 0
$cands = @()
if (Get-Command py -ErrorAction SilentlyContinue) { foreach ($v in '3.11', '3.12', '3.13', '3.10') { $cands += , @('py', "-$v") } ; $cands += , @('py', '-3') }
if (Get-Command python -ErrorAction SilentlyContinue) { $cands += , @('python') }
foreach ($c in $cands) {
    $pre = @($c | Select-Object -Skip 1)
    $n = PyNum $c[0] $pre
    if ($n -ge 310) { $pyExe = $c[0]; $pyPre = $pre; $pyNum = $n; break }
    if ($n -gt $oldNum) { $oldNum = $n }
}
if (-not $pyExe) {
    if ($oldNum -gt 0) {
        Say '[缺]' ("Python 版本太舊（{0}.{1}），需要 3.10 以上。" -f [math]::Floor($oldNum / 100), ($oldNum % 100)) 'Red'
    } else {
        Say '[缺]' '找不到 Python。' 'Red'
    }
    Write-Host '    請到 https://www.python.org/downloads/ 安裝 Python 3.11（勾選 Add python.exe to PATH），再重新執行 setup.cmd' -ForegroundColor Yellow
    Write-Host '    舊版 Python 不用移除，setup 會自動挑新版。' -ForegroundColor DarkGray
    return $false
}
Say '[OK]' ("Python {0}.{1}" -f [math]::Floor($pyNum / 100), ($pyNum % 100)) 'Green'

# 2. 虛擬環境與套件
if (Test-Path $venvPy) {
    $vNum = PyNum $venvPy @()
    if ($vNum -gt 0 -and $vNum -lt 310) {
        # 舊版 Python 建的 .venv：改名保留（不刪除），再用新版重建
        $bak = Join-Path $root ('.venv-old-py{0}{1}' -f [math]::Floor($vNum / 100), ($vNum % 100))
        if (Test-Path $bak) { $bak = '{0}-{1:yyyyMMddHHmmss}' -f $bak, (Get-Date) }
        try {
            Rename-Item -Path $venv -NewName (Split-Path $bak -Leaf) -ErrorAction Stop
            Say '[!]' ("舊的執行環境是 Python {0}.{1}，已改名為 {2}，改用新版重建。確認一切正常後可自行刪除該資料夾。" -f [math]::Floor($vNum / 100), ($vNum % 100), (Split-Path $bak -Leaf)) 'Yellow'
        } catch {
            Say '[缺]' '舊的 .venv 無法改名（可能 start.cmd 還開著）。請關閉所有程式視窗後重新執行 setup.cmd' 'Red'
            return $false
        }
    }
}
if (-not (Test-Path $venvPy)) {
    Write-Host '建立 Python 虛擬環境 .venv ...' -ForegroundColor Cyan
    & $pyExe @pyPre -m venv $venv
}
if (-not (Test-Path $venvPy)) { Say '[缺]' '無法建立 .venv' 'Red'; return $false }

$want = Get-Content $req -Raw -Encoding UTF8
if ($Tier -in 'A', 'B' -and (Test-Path $reqNv)) { $want += "`n" + (Get-Content $reqNv -Raw -Encoding UTF8) }
$have = if (Test-Path $marker) { Get-Content $marker -Raw -Encoding UTF8 } else { '' }
if ($want -ne $have) {
    Write-Host '安裝必要套件（第一次約 2–5 分鐘）...' -ForegroundColor Cyan
    & $venvPy -m pip install --upgrade pip -q | Out-Host
    & $venvPy -m pip install -r $req | Out-Host
    $pipOk = ($LASTEXITCODE -eq 0)
    if ($Tier -in 'A', 'B' -and (Test-Path $reqNv)) { & $venvPy -m pip install -r $reqNv | Out-Host; $pipOk = $pipOk -and ($LASTEXITCODE -eq 0) }
    if ($pipOk) { Set-Content -Path $marker -Value $want -NoNewline -Encoding utf8 }
    else { Say '[缺]' '套件安裝失敗，請確認網路後重新執行' 'Red'; $ok = $false }
} else {
    Write-Host '更新 yt-dlp ...' -ForegroundColor DarkGray
    & $venvPy -m pip install -U yt-dlp -q | Out-Host
}
if ($ok) {
    $ovv = & $venvPy -c 'import openvino as o;print(o.__version__.split(chr(45))[0])' 2>$null | Select-Object -Last 1
    Say '[OK]' "Python 套件（.venv；OpenVINO $ovv）" 'Green'
}

# 3. ffmpeg
$ff = $null
$c = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($c) { $ff = $c.Source }
if (-not $ff) {
    $f = Get-ChildItem (Join-Path $toolsDir 'ffmpeg') -Recurse -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($f) { $ff = $f.FullName }
}
if (-not $ff) {
    Write-Host '下載 ffmpeg（約 90MB）...' -ForegroundColor Cyan
    try {
        New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
        $zip = Join-Path $toolsDir 'ffmpeg.zip'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $toolsDir 'ffmpeg') -Force
        Remove-Item $zip -Force
        $f = Get-ChildItem (Join-Path $toolsDir 'ffmpeg') -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
        if ($f) { $ff = $f.FullName }
    } catch { }
}
if ($ff) { Say '[OK]' "ffmpeg（$ff）" 'Green' }
else { Say '[缺]' 'ffmpeg 自動下載失敗，可改用：winget install Gyan.FFmpeg' 'Red'; $ok = $false }

# 4. JavaScript 執行環境（只提示）
$js = @('deno', 'node', 'bun') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if ($js) { Say '[OK]' "JavaScript 執行環境（$js，下載 YouTube 用）" 'Green' }
else { Say '[－]' 'JavaScript 執行環境：下載 YouTube 時才需要，屆時可執行 winget install DenoLand.Deno' 'DarkGray' }

return $ok
