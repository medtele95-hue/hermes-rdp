# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — HERMES control-room dashboard, standalone process.

Run: python -m app.dashboard_api.server
Binds 127.0.0.1 only — reachable from a phone exclusively via `tailscale
serve` on the same machine (mission Tailscale step), never a public port.

Read routes (/api/status, /api/today, /api/journal, /api/system,
/api/senses) are open on the Tailscale-only network — no PIN, matching
mission spec (read access = anyone already on the tailnet; only ACTIONS are
PIN-gated). Every /api/action/* route requires a correct PIN in the request
body, is rate-limited to 1 per 5 seconds, and locks out for 15 minutes after
5 wrong PINs — see app.dashboard_api.security. Every action, successful or
refused, is written to dashboard/actions_audit.log — see
app.dashboard_api.audit.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.dashboard_api import actions, audit, data, security
from app.logger import log

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    pin = security.ensure_pin_configured()
    if pin:
        log.warning("=" * 60)
        log.warning("[DASHBOARD] PREMIER DEMARRAGE — PIN genere: %s", pin)
        log.warning("[DASHBOARD] Notez-le maintenant, il ne sera plus jamais affiche.")
        log.warning("[DASHBOARD] Changez-le en editant dashboard/.env si besoin.")
        log.warning("=" * 60)
    # dashboard_api is its own process (mission requirement) — it needs its
    # OWN MT5 handle to read account/positions/deals. This is the same
    # pattern watchdog/hermes_watchdog.py and scripts/daily_report.py
    # already use successfully alongside the live bot process: the MT5
    # Python API connects to the local terminal over IPC, and multiple
    # processes can each hold their own read handle to it concurrently.
    # Read-only here: no trade-submission call exists anywhere in this
    # package (see app/dashboard_api/actions.py's module docstring).
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            log.info("[DASHBOARD] MT5 connecte (lecture seule)")
        else:
            log.warning("[DASHBOARD] MT5 non connecte au demarrage: %s", mt5.last_error())
    except Exception as exc:
        log.warning("[DASHBOARD] MT5 indisponible au demarrage: %s", exc)
    log.info("[DASHBOARD] demarrage OK, port 127.0.0.1")
    yield
    try:
        import MetaTrader5 as mt5
        mt5.shutdown()
    except Exception:
        pass


app = FastAPI(title="HERMES Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)


# ── read routes (no PIN) ─────────────────────────────────────────────────

@app.get("/api/status")
def get_status() -> dict:
    return data.build_status()


@app.get("/api/today")
def get_today() -> dict:
    return data.build_today()


@app.get("/api/journal")
def get_journal(page: int = 1, page_size: int = 20, symbol: str | None = None, result: str | None = None) -> dict:
    return data.build_journal(page=page, page_size=page_size, symbol=symbol, result=result)


@app.get("/api/system")
def get_system() -> dict:
    return data.build_system()


@app.get("/api/senses")
def get_senses(symbol: str = "GOLD#") -> dict:
    return data.build_senses(symbol=symbol)


@app.get("/api/audit")
def get_audit(limit: int = 50) -> dict:
    return {"entries": audit.read_recent_audit(limit=limit)}


# ── action routes (PIN + rate limit + lockout) ──────────────────────────

def _pin_gate(pin: str) -> JSONResponse | None:
    allowed, retry_after = security.check_rate_limit()
    if not allowed:
        return JSONResponse({"ok": False, "reason": "RATE_LIMITED", "retry_after": retry_after}, status_code=429)
    ok, reason = security.check_and_record_attempt(pin)
    if not ok:
        return JSONResponse({"ok": False, "reason": reason}, status_code=401)
    return None


@app.post("/api/action/bot/stop")
async def post_bot_stop(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _pin_gate(str(body.get("pin", "")))
    if gate:
        audit.record_action("bot_stop", "REFUSED", {"gate": gate.body.decode()})
        return gate
    result = actions.action_bot_stop()
    audit.record_action("bot_stop", "OK" if result["ok"] else "FAILED", result)
    return JSONResponse(result)


@app.post("/api/action/bot/start")
async def post_bot_start(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _pin_gate(str(body.get("pin", "")))
    if gate:
        audit.record_action("bot_start", "REFUSED", {"gate": gate.body.decode()})
        return gate
    result = actions.action_bot_start()
    audit.record_action("bot_start", "OK" if result["ok"] else "FAILED", result)
    return JSONResponse(result)


@app.post("/api/action/bot/restart")
async def post_bot_restart(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _pin_gate(str(body.get("pin", "")))
    if gate:
        audit.record_action("bot_restart", "REFUSED", {"gate": gate.body.decode()})
        return gate
    result = actions.action_bot_restart()
    audit.record_action("bot_restart", "OK" if result["ok"] else "FAILED", result)
    return JSONResponse(result)


@app.post("/api/action/symbols")
async def post_symbols_toggle(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _pin_gate(str(body.get("pin", "")))
    if gate:
        audit.record_action("symbols_toggle", "REFUSED", {"gate": gate.body.decode()})
        return gate
    result = actions.action_symbols_toggle(bool(body.get("gold_active")), bool(body.get("btc_active")))
    audit.record_action("symbols_toggle", "OK" if result["ok"] else "FAILED", result)
    return JSONResponse(result)


@app.post("/api/action/account/switch")
async def post_account_switch(request: Request) -> JSONResponse:
    body = await request.json()
    gate = _pin_gate(str(body.get("pin", "")))
    if gate:
        audit.record_action("account_switch", "REFUSED", {"gate": gate.body.decode(), "target": body.get("target")})
        return gate
    target = str(body.get("target", ""))
    result = actions.action_account_switch(target, confirm_real_text=body.get("confirm_real_text"))
    audit.record_action("account_switch", "OK" if result["ok"] else "FAILED", {**result, "target": target}, notify=True)
    return JSONResponse(result)


# ── frontend (single mobile page) ────────────────────────────────────────

@app.get("/")
def get_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def run(host: str = "127.0.0.1", port: int = 8010) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    run()
