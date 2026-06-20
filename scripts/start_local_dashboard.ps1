# HERMES Local Dashboard Startup Script
# Starts the Python backend and the Vite frontend dev server.
# Run from: C:\hermes-mt5-agent

param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "  HERMES Local Dashboard Launcher" -ForegroundColor Cyan
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host ""

# ── Verify paths
$BackendMain = Join-Path $Root "app\main.py"
$DashDir     = Join-Path $Root "local_dashboard"

if (-not (Test-Path $BackendMain)) {
    Write-Error "Backend not found at $BackendMain. Run from C:\hermes-mt5-agent."
    exit 1
}
if (-not (Test-Path $DashDir)) {
    Write-Error "Frontend not found at $DashDir. Run 'npm install' first."
    exit 1
}

# ── Check node_modules
$NodeModules = Join-Path $DashDir "node_modules"
if (-not (Test-Path $NodeModules)) {
    Write-Host "  [1/3] Installing frontend dependencies..." -ForegroundColor Yellow
    Set-Location $DashDir
    npm install
    Set-Location $Root
} else {
    Write-Host "  [1/3] Frontend dependencies: OK" -ForegroundColor Green
}

Write-Host ""
Write-Host "  [2/3] Starting HERMES backend (python -m app.main)..." -ForegroundColor Yellow
Write-Host "         Backend API will be at: http://127.0.0.1:$BackendPort/local-api" -ForegroundColor Gray
Write-Host "         API docs:                http://127.0.0.1:$BackendPort/local-api/docs" -ForegroundColor Gray
Write-Host ""

# Start backend in a new window
$BackendJob = Start-Process -PassThru -FilePath "cmd.exe" -ArgumentList @(
    "/k",
    "title HERMES Backend && cd /d `"$Root`" && python -m app.main"
) -WindowStyle Normal

Write-Host "  Backend PID: $($BackendJob.Id)" -ForegroundColor Gray
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "  [3/3] Starting frontend dev server (npm run dev)..." -ForegroundColor Yellow
Write-Host "         Dashboard URL: http://127.0.0.1:$FrontendPort" -ForegroundColor Green
Write-Host ""

# Start frontend in a new window
$FrontendJob = Start-Process -PassThru -FilePath "cmd.exe" -ArgumentList @(
    "/k",
    "title HERMES Dashboard && cd /d `"$DashDir`" && npm run dev"
) -WindowStyle Normal

Write-Host "  Frontend PID: $($FrontendJob.Id)" -ForegroundColor Gray
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "  ─────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Dashboard:     http://127.0.0.1:$FrontendPort" -ForegroundColor Green
Write-Host "  Backend API:   http://127.0.0.1:$BackendPort/local-api/health" -ForegroundColor Cyan
Write-Host "  API Docs:      http://127.0.0.1:$BackendPort/local-api/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "  SAFETY:  DEMO_ONLY=true  |  ALLOW_LIVE_TRADING=false  |  READ-ONLY DASHBOARD" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Press Ctrl+C to stop this launcher." -ForegroundColor DarkGray
Write-Host ""

# Optionally open browser
if (-not $NoBrowser) {
    Start-Sleep -Seconds 2
    try {
        Start-Process "http://127.0.0.1:$FrontendPort"
    } catch {}
}

# Keep script alive to show status
while ($true) {
    Start-Sleep -Seconds 30
    $be = Get-Process -Id $BackendJob.Id -ErrorAction SilentlyContinue
    $fe = Get-Process -Id $FrontendJob.Id -ErrorAction SilentlyContinue
    if (-not $be) { Write-Host "  [!] Backend process exited." -ForegroundColor Red }
    if (-not $fe) { Write-Host "  [!] Frontend process exited." -ForegroundColor Red }
}
