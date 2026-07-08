# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — read-endpoint data builders.

All pure read: MT5 (positions_get/account_info/history_deals_get only — no
trade-submission call anywhere in this module, by design), plus
decision_dataset.jsonl, demo_pilot_events.jsonl, exit_v2_state.json (written
by app/mt5/demo_router.py's Exit V2 snapshot, mission/DASHBOARD.md commit),
watchdog/heartbeat.txt, logs/supervisor_state.json, news_calendar_cache.json.

dashboard_api is a genuinely separate OS process from the bot (mission
requirement), so it deliberately does NOT use
app.local_api.state.get_local_state() — that accessor is an in-process cache
the bot's own thread populates (local_api's server runs as a daemon thread
inside app.main.HermesBackend, not a separate process) and is empty/stale
when read from any other process. Every MT5 field here is read directly,
the same pattern watchdog/hermes_watchdog.py and scripts/daily_report.py
already use.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.utils.broker_time import broker_day_window, broker_now_utc

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
EVENTS_FILE = REPO_ROOT / "app" / "data" / "demo_pilot_events.jsonl"
EXIT_V2_SNAPSHOT_FILE = REPO_ROOT / "app" / "data" / "exit_v2_state.json"
NEWS_CACHE_FILE = REPO_ROOT / "app" / "data" / "news_calendar_cache.json"
WATCHDOG_HEARTBEAT_FILE = REPO_ROOT / "watchdog" / "heartbeat.txt"
WATCHDOG_ALERTS_FILE = REPO_ROOT / "watchdog" / "WATCHDOG_ALERTS.log"
SUPERVISOR_STATE_FILE = REPO_ROOT / "logs" / "supervisor_state.json"
BACKUPS_DIR = REPO_ROOT / "backups"
MAGIC_HARD = 909002
BROKER_UTC_OFFSET_HOURS = 3.0
REFUSED_REASONS = ("EES_EXTREME_BLOCK", "NEWS_BLACKOUT", "ORDER_ABORT", "SYMBOL_BLOCKED", "DAILY_KILLSWITCH")


# mission/FIX_KILLSWITCH_DATE.md (2026-07-08): "now" and the broker-day
# window come exclusively from app.utils.broker_time — the single,
# centralized, anti-regression-guarded source shared with
# app.services.daily_killswitch. No local duplication of this arithmetic.
def _now_utc() -> datetime:
    return broker_now_utc()


def _broker_day_window(now_utc: datetime) -> tuple[datetime, datetime]:
    return broker_day_window(now_utc, BROKER_UTC_OFFSET_HOURS)


def _ensure_mt5_connected(mt5_module) -> bool:
    """Self-healing connection check, called at the top of every MT5-reading
    function below. dashboard_api connects once at server startup (see
    server.py's lifespan handler) — but a long-running process can see that
    single connection drop for reasons unrelated to "too many connections"
    (terminal restart, brief network blip, Windows session change, etc.).
    Multiple concurrent Python processes holding independent MT5 handles is
    NOT the limiting factor here — watchdog/hermes_watchdog.py and the bot
    itself (app.main) both hold their own live connections at the same time
    this function runs, proving the terminal accepts more than one handle.
    Without this check, a single dropped connection at any point in the
    process's lifetime would silently return None from every MT5 call
    forever after, with no way to self-recover short of a manual restart.
    terminal_info() is used as the cheap liveness probe (no side effects);
    initialize() is idempotent and safe to call when already connected."""
    try:
        if mt5_module.terminal_info() is not None:
            return True
    except Exception:
        pass
    try:
        return bool(mt5_module.initialize())
    except Exception:
        return False


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _iter_dataset_rows(row_type: str | None = None, limit_recent: int | None = None):
    if not DATASET_FILE.exists():
        return
    with DATASET_FILE.open(encoding="utf-8") as f:
        lines = f.readlines()
    if limit_recent:
        lines = lines[-limit_recent:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row_type and row.get("row_type") != row_type:
            continue
        yield row


def _exit_v2_state_for(ticket: int) -> dict | None:
    snapshot = _read_json(EXIT_V2_SNAPSHOT_FILE, {}) or {}
    return snapshot.get(str(ticket))


# ── /api/status ──────────────────────────────────────────────────────────

def build_status() -> dict:
    # dashboard_api is a genuinely separate OS process from the bot (mission
    # requirement) — app.local_api.state.get_local_state() is an IN-PROCESS
    # cache the bot's own thread populates (local_api's FastAPI server runs
    # as a daemon thread INSIDE app.main.HermesBackend, not a separate
    # process); calling it from here would silently return an empty,
    # never-populated singleton. Read MT5 directly instead, exactly like
    # watchdog/hermes_watchdog.py and scripts/daily_report.py already do.
    acc_obj = None
    positions = []
    try:
        import MetaTrader5 as mt5
        _ensure_mt5_connected(mt5)
        acc_obj = mt5.account_info()
        raw_positions = mt5.positions_get() or []
        for p in raw_positions:
            if int(getattr(p, "magic", 0) or 0) != MAGIC_HARD:
                continue
            ticket = int(getattr(p, "ticket", 0) or 0)
            exit_state = _exit_v2_state_for(ticket)
            positions.append({
                "ticket": ticket,
                "symbol": getattr(p, "symbol", None),
                "direction": "BUY" if int(getattr(p, "type", 0) or 0) == 0 else "SELL",
                "entry": getattr(p, "price_open", None),
                "current": getattr(p, "price_current", None),
                "floating_usd": getattr(p, "profit", None),
                "sl": getattr(p, "sl", None) or None,
                "tp": getattr(p, "tp", None) or None,
                "exit_v2": exit_state,
            })
    except Exception:
        positions = []

    from app.mt5.account_mode import is_mt5_demo_account
    from app.mt5.account_profiles import get_active_profile_name

    floating_pnl = sum(p["floating_usd"] for p in positions if p["floating_usd"] is not None) if positions else None

    return {
        "equity": getattr(acc_obj, "equity", None) if acc_obj else None,
        "balance": getattr(acc_obj, "balance", None) if acc_obj else None,
        "floating_pnl": floating_pnl,
        "mode": "DEMO" if (acc_obj is not None and is_mt5_demo_account(acc_obj)) else ("REAL" if acc_obj is not None else "UNKNOWN"),
        "active_profile": get_active_profile_name(),
        "account_login": getattr(acc_obj, "login", None) if acc_obj else None,
        "account_server": getattr(acc_obj, "server", None) if acc_obj else None,
        "positions": positions,
        "mt5_connected": acc_obj is not None,
        "generated_at": _now_utc().isoformat(),
    }


# ── /api/today ────────────────────────────────────────────────────────────

def build_today() -> dict:
    now = _now_utc()
    trades = []
    try:
        import MetaTrader5 as mt5
        _ensure_mt5_connected(mt5)
        start, end = _broker_day_window(now)
        deals = mt5.history_deals_get(start.replace(tzinfo=None), end.replace(tzinfo=None)) or []
        opens = {d.position_id: d for d in deals if int(getattr(d, "entry", -1) or 0) == 0}
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != MAGIC_HARD or int(getattr(d, "entry", -1) or 0) != 1:
                continue
            net = float(d.profit or 0) + float(d.commission or 0) + float(d.swap or 0)
            open_deal = opens.get(d.position_id)
            trades.append({
                "ticket": int(d.position_id),
                "symbol": d.symbol,
                "direction": "BUY" if int(getattr(open_deal, "type", d.type) or 0) == 0 else "SELL",
                "net_usd": round(net, 2),
                "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                "close_mode": _outcome_for_ticket(int(d.position_id)),
            })
    except Exception:
        trades = []

    today_broker = (now + timedelta(hours=BROKER_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")
    refused = Counter()
    for row in _iter_dataset_rows(row_type="decision", limit_recent=2000):
        created = str(row.get("created_at") or "")
        if not created.startswith(today_broker):
            continue
        reason = str(row.get("reason") or "")
        for code in REFUSED_REASONS:
            if code in reason:
                refused[code] += 1

    return {
        "trades": trades,
        "net_today_usd": round(sum(t["net_usd"] for t in trades), 2),
        "wins": sum(1 for t in trades if t["net_usd"] > 0),
        "losses": sum(1 for t in trades if t["net_usd"] < 0),
        "refused_by_reason": dict(refused),
        "generated_at": now.isoformat(),
    }


def _outcome_for_ticket(ticket: int) -> str:
    for row in _iter_dataset_rows(row_type="outcome", limit_recent=500):
        if row.get("ticket") == ticket:
            return str(row.get("outcome") or "INCONNU")
    return "INCONNU"


# ── /api/journal ─────────────────────────────────────────────────────────

def build_journal(page: int = 1, page_size: int = 20, symbol: str | None = None, result: str | None = None) -> dict:
    """result: 'win' | 'loss' | None (all)."""
    outcomes = list(_iter_dataset_rows(row_type="outcome"))
    decisions_by_setup = {row.get("setup_id"): row for row in _iter_dataset_rows(row_type="decision") if row.get("setup_id")}

    entries = []
    for o in outcomes:
        if symbol and o.get("symbol") != symbol:
            continue
        pnl = o.get("pnl_reconciled")
        if pnl is None:
            continue
        is_win = float(pnl) > 0
        if result == "win" and not is_win:
            continue
        if result == "loss" and is_win:
            continue
        decision = decisions_by_setup.get(o.get("setup_id")) or {}
        entries.append({
            "ticket": o.get("ticket"),
            "symbol": o.get("symbol"),
            "direction": o.get("direction"),
            "entry": o.get("entry"),
            "exit": o.get("close_price"),
            "sl": o.get("sl"),
            "tp": o.get("tp"),
            "pnl_usd": pnl,
            "opened_at": o.get("opened_at"),
            "closed_at": o.get("closed_at"),
            "close_mode": o.get("outcome"),
            "strategy": decision.get("strategy"),
            "confluence_at_entry": decision.get("final_confluence_score") or decision.get("new_confluence"),
            "mae": o.get("mae"),
            "mfe": o.get("mfe"),
        })

    entries.sort(key=lambda e: e.get("closed_at") or "", reverse=True)
    total = len(entries)
    start = max(0, (page - 1) * page_size)
    page_entries = entries[start:start + page_size]
    return {
        "entries": page_entries,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


# ── /api/system ──────────────────────────────────────────────────────────

def build_system() -> dict:
    now = _now_utc()
    killswitch = {}
    for line in reversed(_read_lines(EVENTS_FILE)):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ks = row.get("daily_killswitch") or {}
        if ks.get("losses_today") is not None:
            killswitch = {
                "losses_today": ks.get("losses_today"),
                "max_losses_per_day": ks.get("max_losses_per_day"),
                "drawdown_pct": ks.get("drawdown_pct"),
                "triggered": ks.get("triggered"),
            }
            break

    watchdog_alive = _heartbeat_age_seconds(WATCHDOG_HEARTBEAT_FILE)
    supervisor_state = _read_json(SUPERVISOR_STATE_FILE, {}) or {}

    last_backup = "AUCUN"
    if BACKUPS_DIR.exists():
        dated = sorted(
            (p.name for p in BACKUPS_DIR.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-"),
            reverse=True,
        )
        last_backup = dated[0] if dated else "AUCUN"

    alerts = []
    if WATCHDOG_ALERTS_FILE.exists():
        today = now.strftime("%Y-%m-%d")
        try:
            for line in WATCHDOG_ALERTS_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith(today):
                    alerts.append(line)
        except OSError:
            pass

    dataset_lines = sum(1 for _ in _read_lines(DATASET_FILE))
    core_version = None
    for row in _iter_dataset_rows(row_type="decision", limit_recent=1):
        core_version = row.get("core_version")

    from app.mt5.account_profiles import get_active_profile_name
    return {
        "kill_switch": killswitch,
        "watchdog_heartbeat_age_seconds": watchdog_alive,
        "supervisor_bot_restarts_last_hour": len(supervisor_state.get("bot_restarts", [])),
        "core_version": core_version,
        "dataset_lines": dataset_lines,
        "last_backup": last_backup,
        "open_alerts_today": alerts[-10:],
        "active_profile": get_active_profile_name(),
        "generated_at": now.isoformat(),
    }


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _heartbeat_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        stamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return round((_now_utc() - stamp).total_seconds(), 1)
    except (OSError, ValueError):
        return None


# ── /api/senses ──────────────────────────────────────────────────────────

def build_senses(symbol: str = "GOLD#") -> dict:
    now = _now_utc()
    ees_buy = None
    ees_sell = None
    try:
        import MetaTrader5 as mt5
        from app.agents.ees import compute_ees
        _ensure_mt5_connected(mt5)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 25)
        if rates is not None and len(rates) >= 10:
            candles = [
                {"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
                for r in rates
            ]
            ees_buy = compute_ees("BUY", candles)
            ees_sell = compute_ees("SELL", candles)
    except Exception:
        pass

    dxy = {}
    session = None
    atr_percentile = None
    for row in _iter_dataset_rows(row_type="decision", limit_recent=1):
        dxy = {
            "trend": row.get("extra.eyes_dxy_trend"),
            "change_h1": row.get("extra.eyes_dxy_change_h1"),
            "change_m15": row.get("extra.eyes_dxy_change_m15"),
            "gold_divergence": row.get("extra.eyes_dxy_gold_dxy_divergence"),
        }
        session = row.get("session")
        atr_percentile = row.get("regime.atr_percentile")

    next_high_news = None
    news = _read_json(NEWS_CACHE_FILE, {}) or {}
    for event in news.get("events", []):
        try:
            event_time = datetime.fromisoformat(str(event.get("time_utc")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if event.get("impact") == "High" and event_time > now:
            next_high_news = {"title": event.get("title"), "country": event.get("country"), "time_utc": event.get("time_utc")}
            break

    return {
        "ees_buy": ees_buy,
        "ees_sell": ees_sell,
        "dxy": dxy,
        "atr_percentile": atr_percentile,
        "session": session,
        "next_high_news": next_high_news,
        "generated_at": now.isoformat(),
    }
