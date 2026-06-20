# start_backend_old_btc.ps1
# Starts the HERMES local API backend with the LOVABLE_BTC_OLD_SYSTEM execution profile.
# Safety: ALLOW_LIVE_TRADING is always false. DEMO_ONLY is always true.

$env:HERMES_EXECUTION_PROFILE = "LOVABLE_BTC_OLD_SYSTEM"
$env:ALLOW_LIVE_TRADING        = "false"
$env:DEMO_ONLY                 = "true"
$env:DEMO_MAX_LOT              = "0.01"
$env:DEMO_MAGIC_NUMBER         = "909002"

Write-Host "[START_BACKEND] Profile=LOVABLE_BTC_OLD_SYSTEM  ALLOW_LIVE_TRADING=false  DEMO_ONLY=true" -ForegroundColor Cyan

Set-Location -Path "C:\hermes-mt5-agent"

python -m uvicorn app.local_api.server:app --host 127.0.0.1 --port 8000 --reload
