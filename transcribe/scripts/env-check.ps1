<#
  env-check.ps1 — 語音轉逐字稿環境初測（Windows PowerShell 5.1 / 7 皆可）
  用法：powershell -ExecutionPolicy Bypass -File .\scripts\env-check.ps1
  輸出：畫面摘要 + <專案>\env-reports\<電腦名稱>.json（報告存在專案內，D:\models 只放模型）
#>
[CmdletBinding()]
param(
    [string]$ModelsRoot = $(if ($env:MODELS_ROOT) { $env:MODELS_ROOT } else { 'D:\models' }),
    [string]$ReportDir  = (Join-Path (Split-Path $PSScriptRoot -Parent) 'env-reports'),
    [switch]$NoPause   # 由其他腳本呼叫時不暫停
)
try {

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$scriptVersion = '0.14.1'
$versionFile = Join-Path (Split-Path $PSScriptRoot -Parent) 'VERSION'
if (Test-Path $versionFile) { $scriptVersion = (Get-Content $versionFile -TotalCount 1).Trim() }

function Test-Cmd([string]$name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

Write-Host "=== 語音轉逐字稿 環境初測 v$scriptVersion ===" -ForegroundColor Cyan

# --- 系統與 CPU ---
$os  = Get-CimInstance Win32_OperatingSystem
$cs  = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$ramGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)

# --- GPU ---
$gpus = @()
foreach ($vc in Get-CimInstance Win32_VideoController) {
    $vendor = 'Other'
    if ($vc.Name -match 'NVIDIA') { $vendor = 'NVIDIA' }
    elseif ($vc.Name -match 'AMD|Radeon') { $vendor = 'AMD' }
    elseif ($vc.Name -match 'Intel') { $vendor = 'Intel' }
    $gpus += [pscustomobject]@{
        Name   = $vc.Name
        Vendor = $vendor
        # AdapterRAM 為 32 位元，超過 4GB 會不準，NVIDIA 以 nvidia-smi 為準
        VramGB = if ($vc.AdapterRAM) { [math]::Round($vc.AdapterRAM / 1GB, 1) } else { $null }
        Driver = $vc.DriverVersion
    }
}

$nvidia = @()
$cudaDriver = $null
if (Test-Cmd 'nvidia-smi') {
    $q = & nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits 2>$null
    foreach ($line in $q) {
        $p = $line -split ',\s*'
        if ($p.Count -ge 4) {
            $nvidia += [pscustomobject]@{
                Name        = $p[0]
                VramTotalGB = [math]::Round([double]$p[1] / 1024, 1)
                VramFreeGB  = [math]::Round([double]$p[2] / 1024, 1)
                Driver      = $p[3]
            }
        }
    }
    $hdr = (& nvidia-smi 2>$null | Select-String 'CUDA Version:\s*([\d\.]+)')
    if ($hdr) { $cudaDriver = $hdr.Matches[0].Groups[1].Value }
}

$vulkan = Test-Path "$env:WINDIR\System32\vulkan-1.dll"

# --- 磁碟 ---
$drive = (Split-Path $ModelsRoot -Qualifier)
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction SilentlyContinue
$diskFreeGB = if ($disk) { [math]::Round($disk.FreeSpace / 1GB, 1) } else { $null }

# --- 軟體 ---
$pyVer = $null
if (Test-Cmd 'python') { $pyVer = (& python --version 2>&1) -replace 'Python\s*', '' }
$torchCuda = $null
if ($pyVer) {
    $torchCuda = & python -c "import torch;print(torch.cuda.is_available())" 2>$null
    if (-not $torchCuda) { $torchCuda = '未安裝 torch' }
}
$ffmpeg = Test-Cmd 'ffmpeg'
$hfCli  = (Test-Cmd 'hf') -or (Test-Cmd 'huggingface-cli')

# --- 等級判斷 ---
$maxVram = 0
if ($nvidia.Count -gt 0) { $maxVram = ($nvidia | Measure-Object VramTotalGB -Maximum).Maximum }
$hasIntelGpu = [bool]($gpus | Where-Object { $_.Vendor -eq 'Intel' })

# 引擎：NVIDIA ≥4GB 用 faster-whisper；其餘一律 OpenVINO（Intel 顯示卡用 GPU，否則用 CPU）
if ($maxVram -ge 8) {
    $tier = 'A'; $engine = 'faster-whisper (CUDA)'; $advice = 'faster-whisper 跑 Breeze-ASR-25；可再用 Whisper large-v3-turbo 交叉比對。'
} elseif ($maxVram -ge 4) {
    $tier = 'B'; $engine = 'faster-whisper (CUDA)'; $advice = 'faster-whisper 跑 Breeze-ASR-25（int8_float16）；備援 Whisper large-v3-turbo。'
} elseif ($hasIntelGpu) {
    $tier = 'C'; $engine = 'OpenVINO (GPU)'; $advice = 'OpenVINO 以 Intel 顯示卡跑 Breeze-ASR-25；失敗時自動改用 CPU。'
} else {
    $tier = 'D'; $engine = 'OpenVINO (CPU)'; $advice = 'OpenVINO 以 CPU 跑 Breeze-ASR-25（較慢，長檔建議離峰執行）；快速草稿可改用 large-v3-turbo。'
}

$warn = @()
if ($ramGB -lt 8)  { $warn += '記憶體低於 8GB，大型模型可能無法載入。' }
if ($diskFreeGB -ne $null -and $diskFreeGB -lt 20) { $warn += "$drive 剩餘空間低於 20GB，全部模型約需 15–20GB。" }
if (-not $disk)    { $warn += "找不到 $drive 磁碟，請將 MODELS_ROOT 指到存在的磁碟。" }
if (-not $ffmpeg)  { $warn += '未偵測到 ffmpeg，影片／m4a 轉檔需要它。' }
if (-not $pyVer)   { $warn += '未偵測到 Python，下載模型需要 Python 3.10 以上（安裝時勾選 Add to PATH）。' }
if ($maxVram -gt 0 -and $torchCuda -eq 'False') { $warn += '有 NVIDIA GPU 但 PyTorch 抓不到 CUDA，請安裝 CUDA 版 torch。' }

$report = [ordered]@{
    CheckedAt   = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    ToolVersion = $scriptVersion
    Computer    = $env:COMPUTERNAME
    OS          = "$($os.Caption) $($os.Version)"
    CPU         = $cpu.Name.Trim()
    Cores       = $cpu.NumberOfCores
    Threads     = $cpu.NumberOfLogicalProcessors
    RamGB       = $ramGB
    Gpus        = $gpus
    Nvidia      = $nvidia
    CudaDriver  = $cudaDriver
    Vulkan      = $vulkan
    ModelsRoot  = $ModelsRoot
    DiskFreeGB  = $diskFreeGB
    Python      = $pyVer
    TorchCuda   = $torchCuda
    Ffmpeg      = $ffmpeg
    HfCli       = $hfCli
    Tier        = $tier
    Engine      = $engine
    Advice      = $advice
    Warnings    = $warn
}

# --- 顯示 ---
Write-Host ""
Write-Host ("電腦：{0}　作業系統：{1}" -f $report.Computer, $report.OS)
Write-Host ("CPU：{0}（{1} 核 / {2} 執行緒）　記憶體：{3} GB" -f $report.CPU, $report.Cores, $report.Threads, $ramGB)
foreach ($g in $gpus) { Write-Host ("顯示卡：{0}" -f $g.Name) }
foreach ($n in $nvidia) { Write-Host ("  NVIDIA VRAM：{0} GB（可用 {1} GB），驅動 {2}，CUDA {3}" -f $n.VramTotalGB, $n.VramFreeGB, $n.Driver, $cudaDriver) }
Write-Host ("Vulkan：{0}　Python：{1}　torch CUDA：{2}　ffmpeg：{3}" -f $vulkan, $pyVer, $torchCuda, $ffmpeg)
Write-Host ("模型庫：{0}（剩餘 {1} GB）" -f $ModelsRoot, $diskFreeGB)
Write-Host ""
Write-Host ("建議等級：{0}　辨識引擎：{1}" -f $tier, $engine) -ForegroundColor Green
Write-Host ("建議作法：{0}" -f $advice) -ForegroundColor Green
foreach ($w in $warn) { Write-Host ("注意：{0}" -f $w) -ForegroundColor Yellow }

# --- 存檔 ---
$envDir = $ReportDir
try {
    New-Item -ItemType Directory -Force -Path $envDir | Out-Null
    $out = Join-Path $envDir "$($env:COMPUTERNAME).json"
    $report | ConvertTo-Json -Depth 5 | Out-File -FilePath $out -Encoding utf8
    Write-Host ""
    Write-Host "報告已存至：$out"
} catch {
    Write-Host "無法寫入 $envDir：$($_.Exception.Message)" -ForegroundColor Yellow
}

} catch {
    Write-Host ''
    Write-Host "[錯誤] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "位置：$($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
} finally {
    if (-not $NoPause) { Write-Host ''; Read-Host '按 Enter 鍵關閉視窗' | Out-Null }
}
