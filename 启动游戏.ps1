# 狼人杀 一键启动脚本
# 先启动 Python 后端，再启动 Tauri 前端

$ErrorActionPreference = "Stop"
$root = Split-Path $MyInvocation.MyCommand.Path -Parent

# ─── 检查 .env ───
$envFile = Join-Path $root "backend\.env"
if (-not (Test-Path $envFile)) {
    Write-Host "⚠️  未找到 backend\.env 配置文件！" -ForegroundColor Yellow
    Write-Host "请先复制 backend\.env.example 为 backend\.env 并填入 Azure Foundry 配置" -ForegroundColor Yellow
    Write-Host ""
    Copy-Item (Join-Path $root "backend\.env.example") $envFile
    Write-Host "已自动复制 .env.example -> .env，请编辑后重新运行此脚本" -ForegroundColor Cyan
    notepad $envFile
    exit 1
}

# ─── 启动 Python 后端 ───
Write-Host "🐍 启动 Python 后端..." -ForegroundColor Green
$venvPython = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "未找到虚拟环境，使用全局 Python..." -ForegroundColor Yellow
    $venvPython = "python"
}

$backendDir = Join-Path $root "backend"
$pythonProc = Start-Process -FilePath $venvPython -ArgumentList "main.py" -WorkingDirectory $backendDir -PassThru -WindowStyle Minimized

# 等待后端启动
Write-Host "等待后端就绪..." -ForegroundColor Gray
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8088/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
}

if (-not $ready) {
    Write-Host "❌ 后端启动失败！" -ForegroundColor Red
    $pythonProc | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "✅ 后端已就绪: http://127.0.0.1:8088" -ForegroundColor Green

# ─── 启动 Tauri 前端 ───
Write-Host "🖥️ 启动狼人杀界面..." -ForegroundColor Green
$exe = Join-Path $root "frontend\src-tauri\target\release\frontend.exe"
if (-not (Test-Path $exe)) {
    Write-Host "❌ 未找到前端 exe: $exe" -ForegroundColor Red
    $pythonProc | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

$frontendProc = Start-Process -FilePath $exe -PassThru

Write-Host "🐺 狼人杀已启动！关闭此窗口将同时关闭后端。" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 退出..." -ForegroundColor Gray

# 等待前端退出
try {
    $frontendProc.WaitForExit()
} catch {}

# 清理
Write-Host "清理后端进程..." -ForegroundColor Gray
$pythonProc | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "已退出。" -ForegroundColor Green
