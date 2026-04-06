# ===== paper_reader_v2 定时自动任务脚本 =====
# 功能：扫描新论文 → 更新关键词画像 → 刷新仪表板
# 用法：
#   手动运行：powershell -ExecutionPolicy Bypass -File scheduled_run.ps1
#   定时任务：由 Windows 任务计划程序调用（见下方注册命令）
#
# 注册定时任务（每天早上 9:00 自动运行）：
#   schtasks /create /tn "PaperReader_DailyScan" /tr "powershell -ExecutionPolicy Bypass -File D:\project\inference\research\paper_reader_v2\scheduled_run.ps1" /sc daily /st 09:00 /f
#
# 删除定时任务：
#   schtasks /delete /tn "PaperReader_DailyScan" /f
#
# 查看任务状态：
#   schtasks /query /tn "PaperReader_DailyScan"

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---- 配置 ----
$ProjectDir = "D:\project\inference\research"
$PythonExe = "D:\app\anaconda\python.exe"
$LogDir = Join-Path $PSScriptRoot "logs"
$LogFile = Join-Path $LogDir ("scheduled_$(Get-Date -Format 'yyyyMMdd_HHmmss').log")

# 确保日志目录存在
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ---- 日志函数 ----
function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# ---- 主流程 ----
Write-Log "========== Scheduled Run Start =========="
Set-Location $ProjectDir
$env:PYTHONIOENCODING = "utf-8"

# Step 1: 扫描新论文
Write-Log "[Step 1/3] Scanning arXiv for new papers..."
$scanOutput = & $PythonExe -m paper_reader_v2.main scan 2>&1 | Out-String
Write-Log $scanOutput.Trim()

# Step 2: 更新关键词画像 + 回写 config.yaml
Write-Log "[Step 2/3] Updating keyword profile..."
$kwOutput = & $PythonExe -m paper_reader_v2.main update-keywords 2>&1 | Out-String
Write-Log $kwOutput.Trim()

# Step 3: 刷新仪表板
Write-Log "[Step 3/3] Refreshing dashboard..."
$dashOutput = & $PythonExe -m paper_reader_v2.main dashboard 2>&1 | Out-String
Write-Log $dashOutput.Trim()

Write-Log "========== Scheduled Run Complete =========="

# 清理 30 天前的日志
Get-ChildItem -Path $LogDir -Filter "scheduled_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force
