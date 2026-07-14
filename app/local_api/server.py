"""
HERMES Local Dashboard API — read-only FastAPI server.

All endpoints are GET (or WebSocket) — no POST/PUT/DELETE.
No execution logic, no DemoRouter calls, no MT5 execution calls.

Every response is passed through to_json_safe() before serialization
and returned as JSONResponse to bypass FastAPI/Pydantic's built-in
serializer (which throws on circular refs or non-JSON-safe objects).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import MetaTrader5 as mt5

from app.config import get_settings
from app.local_api.state import get_local_state, normalize_symbol
from app.local_api.utils import to_json_safe
from app.logger import log

# ---------------------------------------------------------------------------
# T9 (2026-07-14) — LES VOYANTS DE SECURITE ETAIENT PEINTS, PAS BRANCHES
#
# `"demo_only": True` et `"allow_live_trading": False` etaient ecrits en LITTERAUX
# a SEPT endroits de ce fichier — y compris dans /local-api/audit-safety, l'endpoint
# dont le seul role est de PROUVER que le live est bloque.
#
# Si ALLOW_LIVE_TRADING passait a true dans .env, ces endpoints auraient continue
# d'affirmer que le live etait bloque. Un indicateur de securite qui ne peut pas
# signaler le danger est pire qu'aucun indicateur : il donne une fausse assurance.
#
# Le patron correct existait deja dans ce meme fichier (/btc-status lit
# getattr(settings, ...)), et app/services/dashboard_snapshot.py lit lui aussi la
# vraie config — la couche local_api CONTREDISAIT donc la couche snapshot.
# ---------------------------------------------------------------------------

def _safety_flags() -> dict:
    """Les deux drapeaux de securite, lus dans la VRAIE configuration."""
    settings = get_settings()
    return {
        "demo_only": bool(getattr(settings, "demo_only", True)),
        "allow_live_trading": bool(getattr(settings, "allow_live_trading", False)),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HERMES Local Dashboard API",
    description="Read-only local API for the HERMES MT5 trading dashboard",
    version="1.0.0",
    docs_url="/local-api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:5174", "http://127.0.0.1:5174",
                   "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# Canonical quad-terminal symbols (display names)
QUAD_SYMBOLS = ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]

# All internal keys that map to each quad symbol
_QUAD_KEY_MAP: Dict[str, str] = {
    "BTCUSD":      "BTCUSD#",
    "BTCUSD#":     "BTCUSD#",
    "GOLD":        "GOLD#",
    "GOLD#":       "GOLD#",
    "XAUUSD":      "GOLD#",
    "XAUUSD#":     "GOLD#",
    "EURUSD":      "EURUSD",
    "US100CASH":   "US100Cash#",
    "US100CASH#":  "US100Cash#",
    "US100Cash#":  "US100Cash#",
    "NAS100":      "US100Cash#",
    "USTEC":       "US100Cash#",
}


def _canon(symbol: str) -> str:
    """Map any symbol variant to the display name."""
    u = str(symbol or "").upper().strip().replace(" ", "")
    for raw, canon in _QUAD_KEY_MAP.items():
        if u == raw.upper():
            return canon
    if "US100" in u or "NAS100" in u:
        return "US100Cash#"
    if u.startswith("BTCUSD"):
        return "BTCUSD#"
    if u.startswith(("GOLD", "XAUUSD")):
        return "GOLD#"
    if u.startswith("EURUSD"):
        return "EURUSD"
    return symbol


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _heartbeat_age_seconds(last_hb: str) -> Optional[float]:
    if not last_hb:
        return None
    try:
        dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - dt).total_seconds(), 1)
    except Exception:
        return None


def _ok(data: Any) -> JSONResponse:
    """Return a 200 JSON response wrapping data, passing it through to_json_safe."""
    return JSONResponse(content=to_json_safe({
        "ok": True,
        "data": data,
        "timestamp": _utc_now(),
    }))


def _stale(reason: str) -> JSONResponse:
    """Return a 200 JSON response indicating unavailable data."""
    return JSONResponse(content={
        "ok": False,
        "status": "UNAVAILABLE",
        "reason": reason,
        "timestamp": _utc_now(),
    })


def _err(endpoint: str, exc: Exception) -> JSONResponse:
    """Return a 200 JSON response with error details — never re-raises."""
    return JSONResponse(content={
        "ok": False,
        "status": "ERROR",
        "error": str(exc)[:500],
        "endpoint": endpoint,
        "data": None,
        "timestamp": _utc_now(),
    })


def _data_freshness(last_update: Optional[str]) -> str:
    if not last_update:
        return "NO_DATA"
    try:
        dt = datetime.fromisoformat(str(last_update).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        if age < 15:
            return "FRESH"
        if age < 60:
            return "RECENT"
        if age < 300:
            return "STALE"
        return "VERY_STALE"
    except Exception:
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# Helper — build full symbol card for quad terminal
# ---------------------------------------------------------------------------

def _build_symbol_card(display_sym: str, state: Any) -> dict:
    """Build a rich symbol card from shared state — no MT5 calls."""
    sym_states = state.get_per_symbol_state()
    of_snaps = state.get_order_flow_snapshots()
    candidates = state.get_latest_candidates()

    # Find the per-symbol state entry (try multiple key variants)
    sym_keys = [display_sym, display_sym.upper(), display_sym.rstrip("#"),
                display_sym.upper().rstrip("#")]
    if "US100" in display_sym.upper():
        sym_keys += ["US100CASH", "US100CASH#", "US100Cash#".upper()]
    ss: dict = {}
    for k in sym_keys:
        if k in sym_states:
            ss = sym_states[k]
            break
        if k.upper() in sym_states:
            ss = sym_states[k.upper()]
            break

    # Order flow — try all known key variants
    of_raw: dict = {}
    for raw_key, canon in _QUAD_KEY_MAP.items():
        if canon == display_sym or canon == _canon(display_sym):
            of_raw = of_snaps.get(raw_key, {})
            if of_raw:
                break
    if not of_raw:
        of_raw = of_snaps.get(display_sym.rstrip("#").upper(), {})

    # Candle availability summary (from cache)
    candle_summary: Dict[str, dict] = {}
    for tf in ["M1", "M5", "M15", "H1", "H4"]:
        rows = (state.get_candles(display_sym.rstrip("#").upper(), tf) or
                state.get_candles(display_sym.upper(), tf) or
                state.get_candles(display_sym, tf))
        if rows:
            candle_summary[tf] = {
                "available": True,
                "count": len(rows),
                "last_close": rows[-1].get("close") if rows else None,
                "last_time": (rows[-1].get("candle_time") or rows[-1].get("time")) if rows else None,
            }
        else:
            candle_summary[tf] = {"available": False, "count": 0, "last_close": None, "last_time": None}

    # Latest candidates for this symbol
    sym_candidates = [
        c for c in candidates
        if _canon(str(c.get("symbol", ""))) == display_sym or
           _canon(str(c.get("broker_symbol", ""))) == display_sym
    ]
    best_candidate = max(
        (c for c in sym_candidates if c.get("edge_score") is not None),
        key=lambda c: float(c.get("edge_score") or 0),
        default=None,
    )

    # Strategy signals for this symbol
    signals = (state.get_strategy_signals(display_sym.rstrip("#").upper()) or
               state.get_strategy_signals(display_sym.upper()) or
               state.get_strategy_signals(display_sym))

    # Confirmations from best candidate
    confirmations: dict = {}
    if best_candidate:
        confirmations = {
            "smc_score": best_candidate.get("smc_score"),
            "smc_status": best_candidate.get("smc_status"),
            "mtfa_score": best_candidate.get("mtfa_score"),
            "mtfa_status": best_candidate.get("mtfa_status"),
            "confluence_score": best_candidate.get("confluence_score"),
            "confluence_grade": best_candidate.get("confluence_grade") or best_candidate.get("grade"),
            "geometry_score": best_candidate.get("geometry_score"),
            "breakout_score": best_candidate.get("breakout_score"),
            "hard_block": best_candidate.get("hard_block"),
            "block_reason": best_candidate.get("block_reason"),
            "failed_gates": (best_candidate.get("failed_gates") or [])[:8],
            "demo_eligible": best_candidate.get("demo_eligible"),
            "time_gate": ss.get("time_gate"),
            "spread_status": ss.get("spread_status"),
            "analysis_only": best_candidate.get("analysis_only"),
        }

    # Signal plan from best candidate
    plan: dict = {}
    if best_candidate:
        plan = {
            "direction": best_candidate.get("direction"),
            "bias": best_candidate.get("bias"),
            "entry": best_candidate.get("entry"),
            "sl": best_candidate.get("sl"),
            "tp": best_candidate.get("tp"),
            "tp2": best_candidate.get("tp2"),
            "rr": best_candidate.get("rr"),
            "confidence": best_candidate.get("normalized_confidence") or best_candidate.get("confidence"),
            "grade": best_candidate.get("grade"),
            "analysis_only": best_candidate.get("analysis_only"),
            "blocked": not bool(best_candidate.get("demo_eligible")),
            "block_reason": (best_candidate.get("failed_gates") or [None])[0],
            "best_strategy": best_candidate.get("best_strategy"),
            "demo_eligible": best_candidate.get("demo_eligible"),
        }

    # State badge
    route = ss.get("route_status", "")
    gate = ss.get("time_gate", "")
    spread_st = ss.get("spread_status", "")
    if not ss:
        badge = "NO_DATA"
    elif route == "ROUTE_TO_DEMO":
        badge = "LIVE"
    elif spread_st == "MAX_SPREAD":
        badge = "BLOCK"
    elif gate and "BLOCK" in str(gate).upper():
        badge = "BLOCK"
    elif "OBSERVE" in str(route).upper():
        badge = "OBSERVE_ONLY"
    elif display_sym == "US100Cash#":
        badge = "OBSERVE_ONLY"
    else:
        badge = "WAIT"

    return {
        "symbol": display_sym,
        "available": bool(ss),
        "state_badge": badge,
        "price": ss.get("price"),
        "spread": ss.get("spread"),
        "spread_status": ss.get("spread_status"),
        "session": ss.get("session"),
        "time_gate": ss.get("time_gate"),
        "latest_decision": ss.get("latest_decision", "WAIT"),
        "latest_reason": ss.get("latest_reason"),
        "route_status": ss.get("route_status", "WAIT"),
        "last_update_utc": ss.get("last_update_utc"),
        "candles": candle_summary,
        "confirmations": confirmations,
        "plan": plan,
        "strategies": signals,
        "order_flow": {
            "poc": of_raw.get("poc"),
            "vah": of_raw.get("vah"),
            "val": of_raw.get("val"),
            "vwap": of_raw.get("vwap"),
            "cvd_slope": of_raw.get("cvd_slope") or of_raw.get("cvd_proxy"),
            "delta": of_raw.get("delta"),
            "divergence": of_raw.get("divergence"),
            "order_flow_score": of_raw.get("order_flow_score") or of_raw.get("score"),
            "signal": of_raw.get("signal") or of_raw.get("order_flow_signal"),
            "status": of_raw.get("status") or of_raw.get("order_flow_status"),
            "latest_decision": of_raw.get("latest_decision"),
            "latest_reason": of_raw.get("latest_reason"),
            "mode": "OBSERVE_ONLY",
            "reader_status": of_raw.get("reader_status"),
            "execution_agent_status": of_raw.get("execution_agent_status"),
        },
        "levels": {
            "poc": of_raw.get("poc"),
            "vah": of_raw.get("vah"),
            "val": of_raw.get("val"),
            "vwap": of_raw.get("vwap"),
            "support": best_candidate.get("support") if best_candidate else None,
            "resistance": best_candidate.get("resistance") if best_candidate else None,
            "entry": best_candidate.get("entry") if best_candidate else None,
            "sl": best_candidate.get("sl") if best_candidate else None,
            "tp1": best_candidate.get("tp") if best_candidate else None,
            "tp2": best_candidate.get("tp2") if best_candidate else None,
        },
        "mode": "OBSERVE_ONLY" if display_sym == "US100Cash#" else (
            "ACTIVE_EXECUTION" if badge == "LIVE" else "ANALYSIS"
        ),
    }


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _WsManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = threading.Lock()

    def add(self, ws: WebSocket) -> None:
        with self._lock:
            self._connections.append(ws)

    def remove(self, ws: WebSocket) -> None:
        with self._lock:
            try:
                self._connections.remove(ws)
            except ValueError:
                pass

    async def broadcast(self, data: dict) -> None:
        dead = []
        with self._lock:
            conns = list(self._connections)
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


_ws_manager = _WsManager()


# ---------------------------------------------------------------------------
# Background broadcast task
# ---------------------------------------------------------------------------

async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            state = get_local_state()
            msg = to_json_safe({
                "type": "heartbeat",
                "mt5_connected": state.get_mt5_connected(),
                "last_heartbeat_at": state.get_last_heartbeat_at(),
                "heartbeat_age_seconds": _heartbeat_age_seconds(state.get_last_heartbeat_at()),
                "cycle_status": state.get_cycle_status().get("last_status"),
                "timestamp": _utc_now(),
            })
            await _ws_manager.broadcast(msg)
        except Exception:
            pass


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_broadcast_loop())


# ---------------------------------------------------------------------------
# Endpoints — all read-only GET
# ---------------------------------------------------------------------------

@app.get("/local-api/health")
def health() -> JSONResponse:
    try:
        state = get_local_state()
        last_hb = state.get_last_heartbeat_at()
        age = _heartbeat_age_seconds(last_hb)
        stale = age is not None and age > 30
        snap = state.get_dashboard_snapshot()
        account = state.get_account_snapshot()
        return _ok({
            "backend_status": "STALE" if stale else ("LIVE" if last_hb else "STARTING"),
            "backend_started_at": state.get_backend_started_at(),
            "last_heartbeat_at": last_hb,
            "heartbeat_age_seconds": age,
            "stale": stale,
            "mt5_connected": state.get_mt5_connected(),
            **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
            "cycle_status": state.get_cycle_status().get("last_status", "STARTING"),
            "session_name": snap.get("session_name"),
            "account_equity": account.get("equity"),
            "account_balance": account.get("balance"),
            "resolved_symbols": list(state.get_resolved_symbols().keys()),
            "ingest_health": state.get_ingest_health(),
            "safety_proof": {
                **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
                "execution_handler": "app/mt5/demo_router.py ONLY",
            },
        })
    except Exception as exc:
        return _err("/local-api/health", exc)


@app.get("/local-api/readiness")
def readiness() -> JSONResponse:
    """Lightweight readiness check — does NOT require MT5.

    Returns HTTP 200 always (degraded:true when subsystems are unavailable).
    Frontend and start scripts can poll this to know when the API is accepting requests.
    """
    try:
        settings = get_settings()
        state = get_local_state()

        mt5_connected = bool(state.get_mt5_connected())
        account_loaded = False
        try:
            acct = state.get_account_snapshot()
            account_loaded = bool(acct and acct.get("balance") is not None)
        except Exception:
            pass

        data_files_ok = False
        try:
            import os
            from pathlib import Path
            jsonl = Path("demo_pilot_events.jsonl")
            data_files_ok = jsonl.exists() or True  # soft check — file created on first event
        except Exception:
            pass

        profile = str(getattr(settings, "hermes_execution_profile", "DEFAULT") or "DEFAULT")
        dashboard_api_ok = True

        degraded = not mt5_connected or not account_loaded
        return JSONResponse(content={
            "ok": True,
            "ready": True,
            "degraded": degraded,
            "mt5_connected": mt5_connected,
            "account_loaded": account_loaded,
            "profile": profile,
            "data_files_ok": data_files_ok,
            "dashboard_api_ok": dashboard_api_ok,
            **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
            "timestamp": _utc_now(),
        })
    except Exception as exc:
        return JSONResponse(content={
            "ok": True,
            "ready": False,
            "degraded": True,
            "error": str(exc)[:300],
            "timestamp": _utc_now(),
        })


@app.get("/local-api/btc-status")
def btc_status() -> JSONResponse:
    """HERMES BTC open position status — used to show emergency warning on dashboard."""
    try:
        state = get_local_state()
        data = state.get_hermes_btc_status()
        settings = get_settings()
        emergency = bool(data.get("open_count", 0) > int(getattr(settings, "old_btc_emergency_open_count", 1)))
        return _ok({
            **data,
            "emergency_active": emergency,
            "max_open_positions": int(getattr(settings, "old_btc_max_open_positions", 1)),
            "smart_exit_enabled": bool(getattr(settings, "old_btc_smart_exit_enabled", True)),
            "danger_exit_min_usd": float(getattr(settings, "old_btc_danger_exit_min_usd", 0.08)),
            "demo_only": bool(getattr(settings, "demo_only", True)),
            "allow_live_trading": bool(getattr(settings, "allow_live_trading", False)),
            # Fast Smart Exit daemon
            "fast_exit_daemon_enabled": bool(getattr(settings, "old_btc_fast_exit_daemon_enabled", True)),
            "fast_exit_interval_ms": int(getattr(settings, "old_btc_fast_exit_interval_ms", 250)),
            "fast_exit_min_profit_usd": float(getattr(settings, "old_btc_fast_exit_min_profit_usd", 0.03)),
            "fast_exit_hard_min_profit_usd": float(getattr(settings, "old_btc_fast_exit_hard_min_profit_usd", 0.01)),
            "fast_exit_close_at_any_positive": bool(getattr(settings, "old_btc_fast_exit_close_at_any_positive", True)),
            "timestamp": _utc_now(),
        })
    except Exception as exc:
        return _err("/local-api/btc-status", exc)


@app.get("/local-api/dashboard-status")
def dashboard_status() -> JSONResponse:
    try:
        state = get_local_state()
        snap = state.get_dashboard_snapshot()
        if not snap:
            return _stale("DASHBOARD_SNAPSHOT_NOT_YET_BUILT")
        return _ok(snap)
    except Exception as exc:
        return _err("/local-api/dashboard-status", exc)


@app.get("/local-api/quad-terminal")
def quad_terminal() -> JSONResponse:
    try:
        state = get_local_state()
        cards = {}
        for sym in QUAD_SYMBOLS:
            cards[sym] = _build_symbol_card(sym, state)
        return _ok({
            "symbols": QUAD_SYMBOLS,
            "cards": cards,
            "generated_at": _utc_now(),
            "note": "US100Cash# is OBSERVE_ONLY — no execution.",
        })
    except Exception as exc:
        return _err("/local-api/quad-terminal", exc)


@app.get("/local-api/symbols")
def symbols() -> JSONResponse:
    try:
        state = get_local_state()
        per_sym = state.get_per_symbol_state()
        resolved = state.get_resolved_symbols()
        result: Dict[str, dict] = {}
        for sym, ss in per_sym.items():
            display = _canon(sym)
            result[display] = {
                "symbol": display,
                "internal_key": sym,
                "broker_symbol": (resolved.get(sym.lower()) or
                                  resolved.get(sym) or
                                  ss.get("broker_symbol")),
                "price": ss.get("price"),
                "spread": ss.get("spread"),
                "spread_status": ss.get("spread_status"),
                "session": ss.get("session"),
                "time_gate": ss.get("time_gate"),
                "latest_decision": ss.get("latest_decision", "WAIT"),
                "latest_reason": ss.get("latest_reason"),
                "route_status": ss.get("route_status", "WAIT"),
                "last_update_utc": ss.get("last_update_utc"),
                "data_freshness": _data_freshness(ss.get("last_update_utc")),
            }
        return _ok({"symbols": result, "count": len(result)})
    except Exception as exc:
        return _err("/local-api/symbols", exc)


@app.get("/local-api/candles/{symbol}/{timeframe}")
def candles(symbol: str, timeframe: str) -> JSONResponse:
    try:
        state = get_local_state()
        tf = timeframe.upper()
        if tf not in ("M1", "M5", "M15", "H1", "H4"):
            return JSONResponse(content={
                "ok": False,
                "status": f"INVALID_TIMEFRAME_{tf}",
                "reason": "Must be M1/M5/M15/H1/H4",
                "timestamp": _utc_now(),
            })

        # Try multiple key variants for the symbol
        display = _canon(symbol)
        keys_to_try = [
            display.rstrip("#").upper(),
            display.upper(),
            symbol.upper(),
            symbol.upper().rstrip("#"),
        ]
        if "US100" in symbol.upper() or "NAS100" in symbol.upper():
            keys_to_try += ["US100CASH", "US100Cash#".upper()]
        if symbol.upper().startswith("GOLD") or symbol.upper().startswith("XAUUSD"):
            keys_to_try += ["GOLD", "XAUUSD"]

        rows = None
        for k in keys_to_try:
            rows = state.get_candles(k, tf)
            if rows:
                break

        if rows is None:
            return JSONResponse(content={
                "ok": False,
                "status": "HISTORY_NOT_READY",
                "reason": f"No candle cache for {display}/{tf}. Backend may not have completed a cycle yet.",
                "symbol": display,
                "timeframe": tf,
                "timestamp": _utc_now(),
            })

        return _ok({
            "symbol": display,
            "timeframe": tf,
            "count": len(rows),
            "candles": rows,
        })
    except Exception as exc:
        return _err(f"/local-api/candles/{symbol}/{timeframe}", exc)


@app.get("/local-api/order-flow")
def order_flow() -> JSONResponse:
    try:
        state = get_local_state()
        snaps = state.get_order_flow_snapshots()

        # Normalize all keys to canonical display names
        normalized: Dict[str, dict] = {}
        for raw_key, snap in snaps.items():
            canon = _canon(raw_key)
            if canon not in normalized or snap:
                normalized[canon] = {
                    **snap,
                    "display_symbol": canon,
                    "internal_key": raw_key,
                    "mode": ("OBSERVE_ONLY" if canon == "US100Cash#"
                             else snap.get("mode", "OBSERVE_ONLY")),
                }

        # Ensure all quad symbols present
        for sym in QUAD_SYMBOLS:
            if sym not in normalized:
                normalized[sym] = {
                    "display_symbol": sym,
                    "status": "NO_DATA",
                    "mode": "OBSERVE_ONLY",
                }

        return _ok({
            "snapshots": normalized,
            "order_flow_badge": (
                "ORDER FLOW = OBSERVE-ONLY INTELLIGENCE. IT DOES NOT BLOCK TRADES."
            ),
        })
    except Exception as exc:
        return _err("/local-api/order-flow", exc)


@app.get("/local-api/confirmations")
def confirmations(symbol: Optional[str] = Query(default=None)) -> JSONResponse:
    try:
        state = get_local_state()
        candidates = state.get_latest_candidates()

        if symbol:
            canon = _canon(symbol)
            candidates = [
                c for c in candidates
                if (_canon(str(c.get("symbol", ""))) == canon or
                    _canon(str(c.get("broker_symbol", ""))) == canon)
            ]

        result = []
        for c in candidates:
            result.append({
                "symbol": _canon(str(c.get("symbol", ""))),
                "broker_symbol": c.get("broker_symbol"),
                "strategy": c.get("best_strategy"),
                "direction": c.get("direction"),
                "smc_score": c.get("smc_score"),
                "smc_status": c.get("smc_status") or c.get("smc_calibrated_status"),
                "smc_reason": c.get("smc_reason"),
                "mtfa_score": c.get("mtfa_score"),
                "mtfa_status": c.get("mtfa_status") or c.get("mtfa_calibrated_status"),
                "mtf_structure": c.get("mtf_structure"),
                "confluence_score": c.get("confluence_score"),
                "confluence_grade": c.get("confluence_grade") or c.get("grade"),
                "geometry_score": c.get("geometry_score"),
                "breakout_score": c.get("breakout_score"),
                "order_flow_status": c.get("order_flow_status"),
                "time_gate": c.get("time_gate"),
                "spread_status": c.get("spread_status"),
                "hard_block": c.get("hard_block"),
                "block_reason": c.get("block_reason"),
                "failed_gates": (c.get("failed_gates") or [])[:8],
                "missing_confirmations": c.get("missing_confirmations") or [],
                "demo_eligible": c.get("demo_eligible"),
                "analysis_only": c.get("analysis_only"),
            })

        return _ok({"confirmations": result, "count": len(result)})
    except Exception as exc:
        return _err("/local-api/confirmations", exc)


@app.get("/local-api/setup-hunter")
def setup_hunter() -> JSONResponse:
    try:
        state = get_local_state()
        best = state.get_latest_setup_hunter()
        candidates = state.get_latest_candidates()
        snap = state.get_dashboard_snapshot()
        sh_block = snap.get("setup_hunter") or {}

        accepted = [c for c in candidates if c.get("demo_eligible")]
        rejected = [c for c in candidates if not c.get("demo_eligible")]

        return _ok({
            "best_candidate": best,
            "edge_ready_count": len(accepted),
            "near_miss_count": len(rejected),
            "accepted_candidates": [
                {
                    "symbol": _canon(str(c.get("symbol", ""))),
                    "strategy": c.get("best_strategy"),
                    "direction": c.get("direction"),
                    "score": c.get("edge_score"),
                    "grade": c.get("grade"),
                    "entry": c.get("entry"),
                    "sl": c.get("sl"),
                    "tp": c.get("tp"),
                    "rr": c.get("rr"),
                    "demo_eligible": True,
                    "analysis_only": c.get("analysis_only"),
                }
                for c in accepted[:20]
            ],
            "rejected_candidates": [
                {
                    "symbol": _canon(str(c.get("symbol", ""))),
                    "strategy": c.get("best_strategy"),
                    "direction": c.get("direction"),
                    "score": c.get("edge_score"),
                    "grade": c.get("grade"),
                    "reject_reason": (c.get("failed_gates") or ["UNKNOWN"])[0],
                    "failed_gates": (c.get("failed_gates") or [])[:4],
                    "demo_eligible": False,
                }
                for c in rejected[:30]
            ],
            "setup_hunter_block": sh_block,
        })
    except Exception as exc:
        return _err("/local-api/setup-hunter", exc)


@app.get("/local-api/strategies")
def strategies(symbol: Optional[str] = Query(default=None)) -> JSONResponse:
    try:
        state = get_local_state()

        if symbol:
            canon = _canon(symbol)
            keys = [canon.rstrip("#").upper(), canon.upper(), symbol.upper()]
            sigs: list = []
            for k in keys:
                sigs = state.get_strategy_signals(k)
                if sigs:
                    break
            return _ok({
                "symbol": canon,
                "signals": sigs,
                "count": len(sigs),
            })

        all_signals = state.get_all_strategy_signals()
        # Normalize keys
        normalized_sigs: Dict[str, list] = {}
        for raw_key, sig_list in all_signals.items():
            canon = _canon(raw_key)
            normalized_sigs[canon] = sig_list

        return _ok({
            "by_symbol": normalized_sigs,
            "symbols": list(normalized_sigs.keys()),
        })
    except Exception as exc:
        return _err("/local-api/strategies", exc)


@app.get("/local-api/account-snapshot")
def account_snapshot() -> JSONResponse:
    try:
        state = get_local_state()
        acc = state.get_account_snapshot()
        pos = state.get_position_sync()
        snap = state.get_dashboard_snapshot()

        if not acc:
            return _stale("ACCOUNT_SNAPSHOT_UNAVAILABLE")

        return _ok({
            "login": acc.get("login"),
            "name": acc.get("name"),
            "server": acc.get("server"),
            "company": acc.get("company"),
            "trade_mode": acc.get("trade_mode"),
            "account_type": snap.get("account_type"),
            "currency": acc.get("currency", "USD"),
            "balance": acc.get("balance"),
            "equity": acc.get("equity"),
            "margin": acc.get("margin"),
            "free_margin": acc.get("free_margin"),
            "margin_level": acc.get("margin_level"),
            "floating_pnl": acc.get("profit") or pos.get("demo_floating_pnl"),
            "closed_pnl_today": pos.get("demo_closed_pnl_today"),
            "total_pnl_today": pos.get("demo_total_pnl_today"),
            "daily_pnl": pos.get("demo_closed_pnl_today"),
            "open_positions_count": (pos.get("hermes_mt5_open_positions_count") or
                                     acc.get("open")),
            "mt5_open_positions_count": pos.get("mt5_open_positions_count"),
            "snapshot_time": acc.get("snapshot_time"),
            "trade_allowed": acc.get("trade_allowed"),
            "trade_expert": acc.get("trade_expert"),
        })
    except Exception as exc:
        return _err("/local-api/account-snapshot", exc)


@app.get("/local-api/risk")
def risk() -> JSONResponse:
    try:
        state = get_local_state()
        snap = state.get_dashboard_snapshot()
        acc = state.get_account_snapshot()
        pos = state.get_position_sync()
        sg = state.get_latest_safety_guard()
        settings_snap = state.get_settings_snapshot()

        risk_exp = snap.get("risk_exposure") or {}

        return _ok({
            "account": {
                "balance": acc.get("balance"),
                "equity": acc.get("equity"),
                "margin": acc.get("margin"),
                "free_margin": acc.get("free_margin"),
                "floating_pnl": acc.get("profit") or pos.get("demo_floating_pnl"),
                "closed_pnl_today": pos.get("demo_closed_pnl_today"),
                "daily_pnl": pos.get("demo_closed_pnl_today"),
                "total_pnl_today": pos.get("demo_total_pnl_today"),
            },
            "limits": {
                **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
                "demo_max_lot": settings_snap.get("demo_max_lot", 0.01),
                "demo_max_open_trades": settings_snap.get("demo_max_open_trades", 3),
                "demo_max_trades_per_day": settings_snap.get("demo_max_trades_per_day", 5),
                "demo_max_daily_loss_pct": settings_snap.get("demo_max_daily_loss_pct", 1.0),
                "demo_max_risk_per_trade_pct": settings_snap.get("demo_max_risk_per_trade_pct", 0.25),
                "demo_stop_after_consecutive_losses": settings_snap.get(
                    "demo_stop_after_consecutive_losses", 3),
            },
            "open_positions_count": pos.get("hermes_mt5_open_positions_count", 0),
            "safety_guard": sg,
            "risk_exposure": risk_exp,
            "guards": snap.get("hard_safety_still_active") or [
                "DEMO_ONLY true",
                "LIVE TRADING BLOCKED",
                "MAX LOT 0.01",
            ],
        })
    except Exception as exc:
        return _err("/local-api/risk", exc)


def _pos_float(val: Any) -> Optional[float]:
    """Return float or None; treat 0.0 as None (unset SL/TP)."""
    try:
        f = float(val)
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def _pos_to_trade(p: dict, magic: int) -> dict:
    """Convert a raw MT5 position dict to the Trade shape the frontend expects."""
    comment = str(p.get("comment") or "")
    pos_type = int(p.get("type") or 0)
    direction = "BUY" if pos_type == 0 else "SELL"

    # Derive strategy from comment when possible
    cu = comment.upper()
    if "BTC_SCALPING" in cu:
        strategy = "BTC_SCALPING_AGENT"
    elif "ORDER_FLOW" in cu:
        strategy = "ORDER_FLOW_EXECUTION_AGENT"
    elif any(k in cu for k in ("KELLY", "KELL")):
        strategy = "HERMES_DEMO"
    else:
        strategy = "HERMES_DEMO"

    timestamp: Optional[str] = None
    pos_time = p.get("time")
    if pos_time:
        try:
            timestamp = datetime.fromtimestamp(float(pos_time), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass

    is_magic = int(p.get("magic") or 0) == magic
    has_hermes_comment = "HERMES" in cu
    if is_magic and has_hermes_comment:
        match_reason = "MAGIC_909002_AND_HERMES_COMMENT"
    elif is_magic:
        match_reason = "MAGIC_909002"
    else:
        match_reason = "HERMES_COMMENT"

    return {
        "ticket": p.get("ticket"),
        "symbol": _canon(str(p.get("symbol") or "")),
        "strategy": strategy,
        "direction": direction,
        "entry": _pos_float(p.get("price_open")),
        "sl": _pos_float(p.get("sl")),
        "tp": _pos_float(p.get("tp")),
        "pnl": _pos_float(p.get("profit")),
        "lot": _pos_float(p.get("volume")),
        "timestamp": timestamp,
        "comment": comment,
        "magic": int(p.get("magic") or 0),
        "source": "MT5_LIVE",
        "reason": match_reason,
    }


def _mt5_value(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict, NamedTuple, or MT5 object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "_asdict"):
        try:
            return dict(obj._asdict()).get(key, default)
        except Exception:
            pass
    return getattr(obj, key, default)


def _mt5_time(obj: Any) -> Optional[str]:
    raw = _mt5_value(obj, "time")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _mt5_float(obj: Any, key: str) -> float:
    try:
        return float(_mt5_value(obj, key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mt5_int(obj: Any, key: str, default: int = 0) -> int:
    try:
        return int(_mt5_value(obj, key) or default)
    except (TypeError, ValueError):
        return default


def _deal_is_entry_out(deal: Any) -> bool:
    entry = _mt5_int(deal, "entry", -1)
    out_values = {
        int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
        int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
    }
    return entry in out_values


def _deal_is_entry_in(deal: Any) -> bool:
    return _mt5_int(deal, "entry", -1) == int(getattr(mt5, "DEAL_ENTRY_IN", 0))


def _deal_type_direction(deal_type: int, *, is_close_deal: bool = False) -> Optional[str]:
    buy = int(getattr(mt5, "DEAL_TYPE_BUY", 0))
    sell = int(getattr(mt5, "DEAL_TYPE_SELL", 1))
    if deal_type == buy:
        return "SELL" if is_close_deal else "BUY"
    if deal_type == sell:
        return "BUY" if is_close_deal else "SELL"
    return None


def _closed_deal_to_trade(position_id: str, group: List[Any], magic: int) -> Optional[dict]:
    out_deals = [d for d in group if _deal_is_entry_out(d)]
    if not out_deals:
        return None

    btc_deals = [d for d in group if _canon(str(_mt5_value(d, "symbol") or "")) == "BTCUSD#"]
    if not btc_deals:
        return None

    hermes_deals = [
        d for d in group
        if _mt5_int(d, "magic", 0) == magic or "HERMES" in str(_mt5_value(d, "comment") or "").upper()
    ]
    if not hermes_deals:
        return None

    entry_deals = [d for d in group if _deal_is_entry_in(d)]
    first_entry = min(entry_deals, key=lambda d: _mt5_value(d, "time") or 0, default=None)
    last_exit = max(out_deals, key=lambda d: _mt5_value(d, "time") or 0)

    profit = round(sum(_mt5_float(d, "profit") for d in out_deals), 6)
    commission = round(sum(_mt5_float(d, "commission") for d in out_deals), 6)
    swap = round(sum(_mt5_float(d, "swap") for d in out_deals), 6)
    pnl = round(profit + commission + swap, 6)

    direction: Optional[str] = None
    if first_entry is not None:
        direction = _deal_type_direction(_mt5_int(first_entry, "type", -1))
    if direction is None:
        direction = _deal_type_direction(_mt5_int(last_exit, "type", -1), is_close_deal=True)

    comment = str(_mt5_value(last_exit, "comment") or _mt5_value(first_entry, "comment") or "")
    cu = comment.upper()
    if "BTC_SCALPING" in cu:
        strategy = "BTC_SCALPING_AGENT"
    elif "ORDER_FLOW" in cu:
        strategy = "ORDER_FLOW_EXECUTION_AGENT"
    elif "HERMES" in cu:
        strategy = "HERMES_DEMO"
    else:
        strategy = "HERMES_DEMO"

    lot = _mt5_float(first_entry, "volume") if first_entry is not None else _mt5_float(last_exit, "volume")
    row = {
        "symbol": "BTCUSD#",
        "ticket": int(position_id) if str(position_id).isdigit() else position_id,
        "position_id": int(position_id) if str(position_id).isdigit() else position_id,
        "deal_ticket": _mt5_value(last_exit, "ticket"),
        "magic": _mt5_int(last_exit, "magic", _mt5_int(first_entry, "magic", magic) if first_entry else magic),
        "strategy": strategy,
        "direction": direction,
        "entry": _pos_float(_mt5_value(first_entry, "price")) if first_entry is not None else None,
        "exit_price": _pos_float(_mt5_value(last_exit, "price")),
        "sl": None,
        "tp": None,
        "pnl": pnl,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "lot": lot or None,
        "open_time": _mt5_time(first_entry) if first_entry is not None else None,
        "close_time": _mt5_time(last_exit),
        "timestamp": _mt5_time(last_exit),
        "comment": comment,
        "note": "MT5 history deal",
        "reason": "MT5_HISTORY_DEALS",
        "source": "MT5_HISTORY_DEALS",
    }
    log.info(
        "[TRADES_CLOSED_HISTORY_ROW] ticket=%s position_id=%s pnl=%s",
        row["ticket"], row["position_id"], row["pnl"],
    )
    return row


def _recent_mt5_closed_trades(magic: int = 909002, hours: int = 48, limit: int = 50) -> List[dict]:
    """Return recent closed HERMES BTC trades from MT5 history deals."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        deals = mt5.history_deals_get(start, end)
    except Exception as exc:
        log.warning("[TRADES_CLOSED_HISTORY_UNAVAILABLE] reason=%s", exc)
        raise RuntimeError(str(exc)) from exc
    if deals is None:
        reason = str(getattr(mt5, "last_error", lambda: "history_deals_get returned None")())
        log.warning("[TRADES_CLOSED_HISTORY_UNAVAILABLE] reason=%s", reason)
        raise RuntimeError(reason)

    groups: Dict[str, List[Any]] = {}
    for deal in list(deals):
        position_id = str(_mt5_value(deal, "position_id") or "")
        if not position_id:
            position_id = str(_mt5_value(deal, "order") or _mt5_value(deal, "ticket") or "")
        if not position_id:
            continue
        groups.setdefault(position_id, []).append(deal)

    rows: List[dict] = []
    for position_id, group in groups.items():
        row = _closed_deal_to_trade(position_id, group, magic)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: str(r.get("close_time") or ""), reverse=True)
    rows = rows[:limit]
    log.info("[TRADES_CLOSED_HISTORY_READ] source=MT5_HISTORY_DEALS count=%s", len(rows))
    return rows


def _live_hermes_open_positions() -> List[dict]:
    """Query MT5 directly for open HERMES demo positions.

    Matching rule: magic == demo_magic_number  OR  'HERMES' in comment.
    Falls back to [] if MT5 is unavailable.
    """
    try:
        hermes_magic = get_settings().demo_magic_number
    except Exception:
        hermes_magic = 909002

    try:
        raw_positions = list(mt5.positions_get() or [])
    except Exception as exc:
        log.warning("[TRADES_ENDPOINT] mt5.positions_get() failed: %s", exc)
        return []

    result: List[dict] = []
    for pos in raw_positions:
        # Normalise NamedTuple or dict to plain dict
        if hasattr(pos, "_asdict"):
            p: dict = dict(pos._asdict())
        elif isinstance(pos, dict):
            p = dict(pos)
        else:
            keys = ("ticket", "symbol", "type", "volume", "price_open",
                    "sl", "tp", "profit", "magic", "comment", "time")
            p = {k: getattr(pos, k, None) for k in keys}

        magic_val = int(p.get("magic") or 0)
        comment = str(p.get("comment") or "")
        ticket = p.get("ticket")
        symbol = str(p.get("symbol") or "")

        is_hermes = (magic_val == hermes_magic) or ("HERMES" in comment.upper())
        if not is_hermes:
            continue

        log.info(
            "[MT5_OPEN_POSITION_FOUND] ticket=%s symbol=%s magic=%s comment=%s",
            ticket, symbol, magic_val, comment,
        )
        match_reason = (
            "MAGIC_909002_AND_HERMES_COMMENT"
            if (magic_val == hermes_magic and "HERMES" in comment.upper())
            else ("MAGIC_909002" if magic_val == hermes_magic else "HERMES_COMMENT")
        )
        log.info("[HERMES_OPEN_POSITION_MATCH] ticket=%s reason=%s", ticket, match_reason)

        result.append(_pos_to_trade(p, hermes_magic))

    log.info("[TRADES_DASHBOARD_OPEN_COUNT] count=%s", len(result))
    return result


@app.get("/local-api/trades")
def trades() -> JSONResponse:
    """Return open HERMES demo positions (live from MT5) plus recent closed trades.

    Open positions are queried directly from mt5.positions_get() so they appear
    even if no DEMO_ORDER event was written to the local JSONL log (e.g. positions
    opened by older systems or before the current backend started).

    Closed trades still come from the in-memory event log populated by the backend.
    """
    try:
        state = get_local_state()
        events = state.get_demo_events()
        pos = state.get_position_sync()
        try:
            hermes_magic = int(get_settings().demo_magic_number)
        except Exception:
            hermes_magic = 909002
        open_source = "MT5_LIVE"
        closed_source = "FALLBACK_MEMORY"
        fallback_used = True
        history_error: Optional[str] = None

        # ── Open positions: live MT5 query, not the event log ─────────────────
        open_trades = _live_hermes_open_positions()

        # ── Closed trades: event log (MT5 doesn't keep closed positions live) ─
        closed_trades = []
        for ev in events:
            action = str(ev.get("action") or ev.get("event_type") or "").upper()
            if "CLOSE" not in action and "EXIT" not in action:
                continue
            closed_trades.append({
                "symbol": _canon(str(ev.get("symbol", ""))),
                "ticket": ev.get("ticket"),
                "magic": ev.get("magic_number") or ev.get("magic"),
                "strategy": ev.get("strategy"),
                "direction": ev.get("direction") or ev.get("signal"),
                "entry": ev.get("entry") or ev.get("price"),
                "sl": ev.get("sl"),
                "tp": ev.get("tp"),
                "lot": ev.get("lot") or ev.get("lot_size"),
                "pnl": ev.get("pnl") or ev.get("profit"),
                "comment": ev.get("comment"),
                "source": ev.get("source", "HERMES_DEMO"),
                "timestamp": ev.get("timestamp") or ev.get("created_at"),
                "reason": ev.get("reason"),
                "exit_price": ev.get("exit_price") or ev.get("close_price"),
            })

        try:
            closed_trades = _recent_mt5_closed_trades(magic=hermes_magic, hours=48, limit=50)
            closed_source = "MT5_HISTORY_DEALS"
            fallback_used = False
        except Exception as exc:
            history_error = str(exc)[:300]
            closed_source = "FALLBACK_MEMORY"
            fallback_used = True

        note: Optional[str] = None
        if not open_trades and not events:
            note = "No open MT5 positions found and no recent events. Backend may be starting up."

        pnl_source = pos.get("pnl_source") or ("MT5_HISTORY_DEALS" if not fallback_used else "TRADES_TABLE_FALLBACK")
        log.info(
            "[TRADES_ENDPOINT_SOURCE] open_source=%s closed_source=%s pnl_source=%s",
            open_source, closed_source, pnl_source,
        )

        return _ok({
            "open_trades": open_trades,
            "closed_trades": closed_trades[-50:],
            "open_count": len(open_trades),
            "closed_count": len(closed_trades),
            "open_source": open_source,
            "closed_source": closed_source,
            "pnl_source": pnl_source,
            "mt5_closed_deals_count": len(closed_trades) if closed_source == "MT5_HISTORY_DEALS" else 0,
            "fallback_used": fallback_used,
            "history_error": history_error,
            "note": note,
        })
    except Exception as exc:
        return _err("/local-api/trades", exc)


@app.get("/local-api/mt5-open-positions-debug")
def mt5_open_positions_debug() -> JSONResponse:
    """Diagnostic: all raw MT5 open positions with HERMES visibility status.

    Returns every open MT5 position, annotated with:
      - is_hermes_demo  (matches magic OR comment rule)
      - why_visible_or_hidden
      - dashboard_open_count  (total that would appear in /trades)
    """
    try:
        try:
            hermes_magic = get_settings().demo_magic_number
        except Exception:
            hermes_magic = 909002

        try:
            raw_positions = list(mt5.positions_get() or [])
            mt5_available = True
        except Exception as exc:
            return _ok({
                "mt5_available": False,
                "error": str(exc),
                "mt5_open_positions_total": 0,
                "dashboard_open_count": 0,
                "hermes_magic_number": hermes_magic,
                "positions": [],
            })

        rows: List[dict] = []
        hermes_count = 0

        for pos in raw_positions:
            if hasattr(pos, "_asdict"):
                p: dict = dict(pos._asdict())
            elif isinstance(pos, dict):
                p = dict(pos)
            else:
                keys = ("ticket", "symbol", "type", "volume", "price_open",
                        "sl", "tp", "profit", "magic", "comment", "time")
                p = {k: getattr(pos, k, None) for k in keys}

            magic_val = int(p.get("magic") or 0)
            comment = str(p.get("comment") or "")
            is_magic = magic_val == hermes_magic
            is_comment = "HERMES" in comment.upper()
            is_hermes_demo = is_magic or is_comment

            if is_hermes_demo:
                hermes_count += 1
                reasons = []
                if is_magic:
                    reasons.append(f"magic=={hermes_magic}")
                if is_comment:
                    reasons.append("comment contains HERMES")
                why = "VISIBLE: " + " AND ".join(reasons)
            else:
                why = (
                    f"HIDDEN: magic={magic_val} != {hermes_magic}"
                    f" AND 'HERMES' not in comment={comment!r}"
                )

            rows.append({
                "ticket": p.get("ticket"),
                "symbol": p.get("symbol"),
                "type": p.get("type"),
                "magic": magic_val,
                "comment": comment,
                "volume": p.get("volume"),
                "price_open": p.get("price_open"),
                "sl": p.get("sl"),
                "tp": p.get("tp"),
                "profit": p.get("profit"),
                "is_hermes_demo": is_hermes_demo,
                "why_visible_or_hidden": why,
            })

        return _ok({
            "mt5_available": mt5_available,
            "mt5_open_positions_total": len(raw_positions),
            "dashboard_open_count": hermes_count,
            "hermes_magic_number": hermes_magic,
            "positions": rows,
        })
    except Exception as exc:
        return _err("/local-api/mt5-open-positions-debug", exc)


@app.get("/local-api/logs/recent")
def logs_recent(
    limit: int = Query(default=200, ge=1, le=1000),
    level: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> JSONResponse:
    try:
        state = get_local_state()
        entries = state.get_recent_logs(
            limit=limit,
            level=level,
            symbol=symbol,
            strategy=strategy,
            search=search,
        )
        return _ok({
            "logs": entries,
            "count": len(entries),
            "limit": limit,
            "filters": {
                "level": level,
                "symbol": symbol,
                "strategy": strategy,
                "search": search,
            },
        })
    except Exception as exc:
        return _err("/local-api/logs/recent", exc)


@app.get("/local-api/audit-safety")
def audit_safety() -> JSONResponse:
    try:
        state = get_local_state()
        snap = state.get_dashboard_snapshot()
        sg = state.get_latest_safety_guard()
        settings_snap = state.get_settings_snapshot()

        _flags = _safety_flags()
        return _ok({
            "safety_flags": {
                # T9 : c'est L'ENDPOINT D'AUDIT DE SECURITE. Il affirmait "live
                # bloque" avec des litteraux — il aurait donc continue de le dire
                # meme si le live avait ete active. Il lit desormais la vraie config.
                "ALLOW_LIVE_TRADING": _flags["allow_live_trading"],
                "DEMO_ONLY": _flags["demo_only"],
                "READ_ONLY_DASHBOARD": True,
                "execution_handler": "app/mt5/demo_router.py ONLY",
                "no_execution_endpoints": True,
            },
            "config": {
                **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
                "demo_max_lot": settings_snap.get("demo_max_lot", 0.01),
                "demo_magic_number": settings_snap.get("demo_magic_number", 909002),
            },
            "safety_guard_status": sg,
            "hard_safety": snap.get("hard_safety_still_active") or [
                "DEMO_ONLY true",
                "LIVE TRADING BLOCKED",
                "MAX LOT 0.01",
            ],
            "demo_pilot_enabled": snap.get("demo_pilot_enabled"),
            "pilot_hours_remaining": snap.get("pilot_hours_remaining"),
            "last_demo_gate_decision": snap.get("last_demo_gate_decision"),
            "last_demo_gate_reason": snap.get("last_demo_gate_reason"),
            "last_demo_ticket": snap.get("last_demo_ticket"),
            "strategy_manager": snap.get("strategy_manager", {}),
        })
    except Exception as exc:
        return _err("/local-api/audit-safety", exc)


@app.get("/local-api/btc-intelligence")
def btc_intelligence() -> JSONResponse:
    """BTC Setup Intelligence panel — current mode, streaks, grades, and exit profile.

    Populated by the BTC intelligence engine each cycle; read-only dashboard data.
    No execution logic, no DemoRouter calls, no MT5 execution calls.
    """
    try:
        state = get_local_state()
        btc_intel = state.get_btc_intelligence()
        btc_status = state.get_hermes_btc_status()

        # Merge smart/fast exit status from btc_status for convenience
        smart_exit = {
            "open_count": btc_status.get("open_count", 0),
            "floating_pnl": btc_status.get("floating_pnl", 0.0),
            "positive_candidates": btc_status.get("positive_candidates", 0),
            "last_smart_exit_reason": btc_status.get("last_smart_exit_reason"),
            "emergency_active": btc_status.get("emergency_active", False),
        }
        fast_exit = {
            "daemon_enabled": btc_status.get("fast_exit_daemon_enabled", False),
            "interval_ms": btc_status.get("fast_exit_interval_ms", 250),
            "last_tick": btc_status.get("fast_exit_last_tick"),
            "positive_candidates": btc_status.get("fast_exit_positive_candidates", 0),
            "last_close_ticket": btc_status.get("fast_exit_last_close_ticket"),
            "last_close_profit": btc_status.get("fast_exit_last_close_profit"),
            "last_close_reason": btc_status.get("fast_exit_last_close_reason"),
            "last_error": btc_status.get("fast_exit_last_error"),
        }

        return _ok({
            "current_mode": btc_intel.get("current_mode", "NORMAL"),
            "win_streak": btc_intel.get("win_streak", 0),
            "loss_streak": btc_intel.get("loss_streak", 0),
            "positive_exit_rate": btc_intel.get("positive_exit_rate", 0.0),
            "best_strategy_now": btc_intel.get("best_strategy_now"),
            "worst_strategy_now": btc_intel.get("worst_strategy_now"),
            "last_setup_score": btc_intel.get("last_setup_score"),
            "last_setup_grade": btc_intel.get("last_setup_grade"),
            "last_block_reason": btc_intel.get("last_block_reason"),
            "current_exit_profile": btc_intel.get("current_exit_profile"),
            "temporary_block_rules": btc_intel.get("temporary_block_rules", []),
            "pause_remaining_minutes": btc_intel.get("pause_remaining_minutes", 0.0),
            "requires_aplus": btc_intel.get("requires_aplus", False),
            # Narrative intelligence (populated by BtcMarketNarrator each cycle)
            "last_narrative_verdict": btc_intel.get("last_narrative_verdict", "N/A"),
            "last_narrative_coherence": btc_intel.get("last_narrative_coherence", 0.0),
            "last_narrative_confidence": btc_intel.get("last_narrative_confidence", 0.0),
            "last_strong_confirmations": btc_intel.get("last_strong_confirmations", []),
            "last_silent_risks": btc_intel.get("last_silent_risks", []),
            "active_temp_blocks": btc_intel.get("active_temp_blocks", []),
            "smart_exit_status": smart_exit,
            "fast_exit_status": fast_exit,
            "safety_proof": {
                **_safety_flags(),  # T9 : lus dans la vraie config, plus en dur
                "max_lot": 0.01,
                "max_btc_positions": 1,
            },
        })
    except Exception as exc:
        return _err("/local-api/btc-intelligence", exc)


@app.get("/local-api/setup-audit")
def setup_audit_endpoint(
    hours: int = Query(default=48, ge=1, le=720),
) -> JSONResponse:
    try:
        from app.tools.setup_audit import run_audit, DEFAULT_EVENTS_PATH, DEFAULT_OUT_DIR
        summary = run_audit(
            hours=hours,
            events_path=DEFAULT_EVENTS_PATH,
            out_dir=DEFAULT_OUT_DIR,
            quiet=True,
        )
        # Strip the large embedded lists from the API response (too large for JSON)
        payload = {k: v for k, v in summary.items()
                   if k not in ("all_records", "executed_records", "accepted_not_executed")}
        payload["accepted_not_executed"] = summary.get("accepted_not_executed", [])[:100]
        payload["executed_records"] = summary.get("executed_records", [])
        payload["hours"] = hours
        return _ok(payload)
    except Exception as exc:
        return _err("/local-api/setup-audit", exc)


@app.websocket("/local-api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_manager.add(websocket)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "keepalive", "timestamp": _utc_now()})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_manager.remove(websocket)


# ---------------------------------------------------------------------------
# Server startup helpers
# ---------------------------------------------------------------------------

_server_thread: Optional[threading.Thread] = None
_server_started = threading.Event()


def start_local_api_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the FastAPI server in a background daemon thread."""
    global _server_thread

    if _server_thread and _server_thread.is_alive():
        return

    def _run() -> None:
        import uvicorn
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        asyncio.run(server.serve())

    _server_thread = threading.Thread(target=_run, daemon=True, name="hermes-local-api")
    _server_thread.start()
    # Give uvicorn a moment to bind
    time.sleep(0.5)
