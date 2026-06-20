# Hermes MT5 Backend

Backend for `MT5 x HERMES`, a 5-minute AI trading analysis agent that reads local MetaTrader 5 data and writes realtime analysis rows through the Lovable Hermes ingest API.

This project does not create a frontend and does not create or rename existing tables.

## Safety Defaults

- `READ_ONLY=true` by default.
- This version does not call the MT5 order placement API.
- This version does not close positions.
- This version does not modify SL or TP.
- Hermes reserves magic number `909001` for future execution support.
- Current output is analysis-only and writes decisions/events through Lovable ingest.

## Existing Tables

The backend writes to the existing tables:

- `hermes_agents`
- `market_states`
- `markov_predictions`
- `kelly_risk`
- `strategy_signals`
- `ai_decisions`
- `execution_events`
- `trades`
- `bot_logs`
- `nightly_reports`
- `account_snapshots`
- `bot_status`
- `market_candles`

`settings` is intentionally not sent every cycle while the ingest endpoint is being stabilized.

## Windows RDP Setup

1. Install Python 3.11 or newer on the Windows RDP.
2. Install MetaTrader 5 and log in to the account you want Hermes to read.
3. Keep MetaTrader 5 running on the same Windows user session.
4. Copy `.env.example` to `.env`.
5. Fill in:

```env
HERMES_INGEST_URL=https://.../api/public/hermes-ingest
HERMES_INGEST_SECRET=your_ingest_secret
```

6. Adjust symbols if needed:

```env
SYMBOLS=BTCUSD,XAUUSD,EURUSD
```

Hermes will resolve broker suffixes such as `BTCUSDm`, `BTCUSD.`, and `BTCUSD.pro`.

## Install

Run PowerShell inside `C:\hermes-mt5-agent`:

```powershell
.\install.ps1
```

If script execution is blocked on the RDP, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install.ps1
```

## Run

Double-click:

```text
run_hermes.bat
```

Or run from a terminal:

```powershell
.\run_hermes.bat
```

Test the Lovable ingest endpoint without connecting to MT5:

```powershell
python -m app.main --test-ingest
```

This sends one minimal `bot_logs` row and prints the endpoint response body.

Generate a Lovable paper trading report without connecting to MT5:

```powershell
python -m app.main --paper-report 1
```

This sends a GET request to `/api/public/hermes-paper-report?hours=1` on the same domain as `HERMES_INGEST_URL` and prints the response cleanly.

## Expected First Successful Output

```text
[INFO] Lovable ingest configured
[INFO] MT5 connected
[INFO] Account detected
[INFO] Requested symbols resolved
[INFO] Heartbeat written
[INFO] Candles pushed
[INFO] Markov prediction written
[INFO] Kelly risk written
[INFO] AI decision written
[INFO] Hermes analysis cycle complete
```

## What It Does Every Cycle

On startup, Hermes sends one ingest test row each to `bot_logs`, `bot_status`, and `hermes_agents`.

Every `POLL_SECONDS` seconds, default `5`, Hermes:

- updates `bot_status`
- inserts `account_snapshots`
- writes `hermes_agents`
- writes `market_candles`
- classifies the M5 market state
- updates in-memory Markov transitions
- writes `market_states` and `markov_predictions`
- evaluates EMA Pullback, Breakout Retest, Second Entry, and Scalping strategies
- writes `strategy_signals`
- evaluates Kelly risk constraints
- writes `kelly_risk`
- writes `ai_decisions`
- writes simulated paper `execution_events` only when paper trading is enabled
- writes `bot_logs`

## Notes

If the Lovable ingest endpoint rejects a row because the existing schema uses different column requirements, the error is logged and the loop continues. The table names are kept exactly as provided.
