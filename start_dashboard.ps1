# start_dashboard.ps1
# Starts the Vite/React local dashboard frontend on port 5173.
# Backend must be running on port 8000 first (see start_backend_old_btc.ps1).

Set-Location -Path "C:\hermes-mt5-agent\local_dashboard"

Write-Host "[START_DASHBOARD] Launching Vite dev server on http://127.0.0.1:5173" -ForegroundColor Cyan

npm run dev
