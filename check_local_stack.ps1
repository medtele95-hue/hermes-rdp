# check_local_stack.ps1
# Checks whether the backend (port 8000) and frontend (port 5173) are running,
# then queries /local-api/health and /local-api/readiness.

function Test-Port {
    param([int]$Port)
    $conn = $null
    try {
        $conn = New-Object System.Net.Sockets.TcpClient("127.0.0.1", $Port)
        return $conn.Connected
    } catch {
        return $false
    } finally {
        if ($conn) { $conn.Close() }
    }
}

Write-Host ""
Write-Host "=== HERMES Local Stack Check ===" -ForegroundColor Yellow

# --- Port checks ---
$backend8000  = Test-Port 8000
$frontend5173 = Test-Port 5173

if ($backend8000)  { Write-Host "  [OK]  Backend   port 8000 — OPEN"   -ForegroundColor Green }
else               { Write-Host "  [!!]  Backend   port 8000 — CLOSED"  -ForegroundColor Red   }

if ($frontend5173) { Write-Host "  [OK]  Frontend  port 5173 — OPEN"   -ForegroundColor Green }
else               { Write-Host "  [!!]  Frontend  port 5173 — CLOSED"  -ForegroundColor Red   }

# --- Health endpoint ---
if ($backend8000) {
    Write-Host ""
    Write-Host "--- /local-api/health ---" -ForegroundColor Cyan
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/local-api/health" -Method GET -TimeoutSec 3
        Write-Host "  backend_status : $($health.data.backend_status)"
        Write-Host "  mt5_connected  : $($health.data.mt5_connected)"
        Write-Host "  cycle_status   : $($health.data.cycle_status)"
        Write-Host "  stale          : $($health.data.stale)"
    } catch {
        Write-Host "  ERROR: $_" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "--- /local-api/readiness ---" -ForegroundColor Cyan
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/local-api/readiness" -Method GET -TimeoutSec 3
        Write-Host "  ready          : $($ready.ready)"
        Write-Host "  degraded       : $($ready.degraded)"
        Write-Host "  mt5_connected  : $($ready.mt5_connected)"
        Write-Host "  account_loaded : $($ready.account_loaded)"
        Write-Host "  profile        : $($ready.profile)"
        Write-Host "  demo_only      : $($ready.demo_only)"
        Write-Host "  allow_live_trading: $($ready.allow_live_trading)"
    } catch {
        Write-Host "  ERROR: $_" -ForegroundColor Red
    }
} else {
    Write-Host ""
    Write-Host "  (Skipping HTTP checks — backend not reachable)" -ForegroundColor DarkGray
}

Write-Host ""
