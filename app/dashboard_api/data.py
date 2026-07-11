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
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.utils.broker_time import broker_day_window, broker_now_utc, from_mt5_deal_time, to_mt5_query_bounds

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


# mission (2026-07-11): decision_dataset.jsonl grew to ~80MB/8000+ lines.
# Every dashboard read used to re-read+re-parse the WHOLE file on every
# single API call (several times per call, via the two _load_*_index
# helpers below) -- under normal 30s polling this exhausted the request
# thread pool entirely (py-spy dump: every worker thread stuck inside this
# function's old full-file readlines()). Fix: one in-memory cache of ALL
# parsed rows, invalidated on mtime/size change, refreshed via a true tail
# seek -- only the bytes appended since the last read are ever touched.
# Correct because decision_dataset.jsonl is append-only by construction
# (app.services.decision_dataset never rewrites or truncates it, only
# appends) -- this module never writes to it, only reads. A partial
# trailing line (caught mid-write) is carried over and completed on the
# NEXT read rather than dropped, so no row is ever lost or corrupted.
_dataset_cache_lock = threading.Lock()
_dataset_cache: dict = {"path": None, "mtime": None, "size": None, "offset": 0, "rows": [], "carry": b""}


def _refresh_dataset_cache() -> list[dict]:
    with _dataset_cache_lock:
        current_path = str(DATASET_FILE)
        if current_path != _dataset_cache["path"]:
            # DATASET_FILE itself changed (only ever happens in tests, which
            # patch.object() it to a fresh temp file per test) -- the old
            # cache belongs to a DIFFERENT file and must never be reused,
            # even if the new file's mtime/size coincidentally match.
            _dataset_cache.update(path=current_path, mtime=None, size=None, offset=0, rows=[], carry=b"")

        if not DATASET_FILE.exists():
            _dataset_cache.update(mtime=None, size=None, offset=0, rows=[], carry=b"")
            return _dataset_cache["rows"]

        stat = DATASET_FILE.stat()
        if (
            _dataset_cache["size"] is not None
            and stat.st_mtime == _dataset_cache["mtime"]
            and stat.st_size == _dataset_cache["size"]
        ):
            return _dataset_cache["rows"]  # unchanged since last read -- zero I/O

        # cold start, or the file shrank/was replaced (should never happen
        # given append-only, but never trust that blindly): full re-read.
        cold = _dataset_cache["size"] is None or stat.st_size < _dataset_cache["offset"]
        rows: list[dict] = [] if cold else list(_dataset_cache["rows"])
        start_offset = 0 if cold else _dataset_cache["offset"]
        carry = b"" if cold else _dataset_cache["carry"]

        with DATASET_FILE.open("rb") as f:
            f.seek(start_offset)
            chunk = carry + f.read()

        parts = chunk.split(b"\n")
        tail = parts.pop()
        for raw in parts:
            line = raw.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        # the last fragment lacks a trailing \n either because it's the
        # tail of a genuinely complete file (no writer required to end on a
        # newline) or because we caught a write mid-flight -- json.loads()
        # is the only reliable way to tell them apart: if it parses, it's
        # complete and safe to consume now; only an UNPARSEABLE tail is
        # held back as carry and retried once more bytes arrive.
        tail_line = tail.strip()
        if not tail_line:
            carry = b""
        else:
            try:
                rows.append(json.loads(tail_line))
                carry = b""
            except json.JSONDecodeError:
                carry = tail

        _dataset_cache["mtime"] = stat.st_mtime
        _dataset_cache["size"] = stat.st_size
        _dataset_cache["offset"] = stat.st_size
        _dataset_cache["carry"] = carry
        _dataset_cache["rows"] = rows
        return rows


def _iter_dataset_rows(row_type: str | None = None, limit_recent: int | None = None):
    rows = _refresh_dataset_cache()
    if limit_recent:
        rows = rows[-limit_recent:]
    for row in rows:
        if row_type and row.get("row_type") != row_type:
            continue
        yield row


def _exit_v2_state_for(ticket: int) -> dict | None:
    snapshot = _read_json(EXIT_V2_SNAPSHOT_FILE, {}) or {}
    return snapshot.get(str(ticket))


def _duration_seconds(opened_at: str | None, closed_at: str | None) -> float | None:
    if not opened_at or not closed_at:
        return None
    try:
        opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        closed = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((closed - opened).total_seconds(), 1)


def _parse_close_mode_from_comment(comment: str) -> str:
    """Fallback close-mode classifier straight from MT5's own closing-deal
    comment — always available (unlike a dataset outcome match), since the
    broker/our own closers stamp a specific comment on every close. MT5
    truncates comments to 16 chars (verified live: "HERMES_QUICK_EXIT_TP"
    arrives as "HERMES_QUICK_EXI", "HERMES_RESCUE_EXIT" as
    "HERMES_RESCUE_EX") — every substring check below is deliberately short
    enough to survive that truncation. "HERMES_QUICK_EXIT" (no _TP suffix)
    is used ONLY for an SL-modify request in app.mt5.demo_router — it never
    appears as a CLOSING deal's comment, so a truncated "QUICK_EXI" seen
    here is unambiguously the genuine TP-close path."""
    c = str(comment or "").strip()
    upper = c.upper()
    if upper.startswith("[SL"):
        return "SL_HIT"
    if upper.startswith("[TP"):
        return "TP_HIT"
    if "QUICK_EXI" in upper:
        return "TP_HIT"
    if "RESCUE" in upper:
        return "SMART_RESCUE"
    if "EXIT_V2" in upper:
        return "EXIT_V2"
    return c or "INCONNU"


def _load_outcome_index_by_ticket() -> dict[int, dict]:
    """mission/FIX_DASHBOARD_DATA.md (2026-07-08): full-file scan, keyed by
    ticket — outcome rows are diluted by paper-trading "virtual" rows
    (ticket=None), which can push a real trade's outcome far beyond any
    small lookback window (verified live: the last real-ticket outcome row
    sat 921 lines behind a 500-row cap). Used only to ENRICH MT5-sourced
    entries (close_mode/strategy/prices when available) — MT5 deals remain
    the source of truth for WHICH trades exist and their net P&L, per this
    mission's explicit instruction (a dataset-primary design silently
    dropped real trades whose outcome row was never reconciled: verified
    live, 10 dataset-matched entries vs 19 authoritative MT5 closes)."""
    index: dict[int, dict] = {}
    for row in _iter_dataset_rows(row_type="outcome"):
        ticket = row.get("ticket")
        if ticket is None:
            continue
        try:
            index[int(ticket)] = row
        except (TypeError, ValueError):
            continue
    return index


def _load_decision_index_by_setup() -> dict[str, dict]:
    return {row.get("setup_id"): row for row in _iter_dataset_rows(row_type="decision") if row.get("setup_id")}


# mission/FIX_DASHBOARD_DATA.md (2026-07-08): shared source for both
# /api/journal and /api/today's trade list. MT5 closed deals (via the
# now-fixed to_mt5_query_bounds()) are the SOURCE OF TRUTH for which
# trades exist and their net P&L — mission's explicit instruction ("PAS un
# compteur interne, PAS des valeurs périmées"), enriched with the
# reconciled dataset (close_mode label, strategy, sl/tp) where a match
# exists. Every date/time shown is true-UTC (from_mt5_deal_time() inverts
# the broker-wall-clock stamping) or the bot's own datetime.now(utc)
# recording from the dataset — never a raw, unconverted deal.time.
def _mt5_journal_entries(
    start_utc: datetime,
    end_utc: datetime,
    symbol: str | None = None,
    result: str | None = None,
) -> list[dict]:
    entries: list[dict] = []
    try:
        import MetaTrader5 as mt5
        _ensure_mt5_connected(mt5)
        q_start, q_end = to_mt5_query_bounds(start_utc, end_utc, BROKER_UTC_OFFSET_HOURS)
        deals = mt5.history_deals_get(q_start, q_end) or []
    except Exception:
        return entries

    opens = {int(d.position_id): d for d in deals if int(getattr(d, "entry", -1) or 0) == 0}
    outcome_index = _load_outcome_index_by_ticket()
    decision_index = _load_decision_index_by_setup()

    for d in deals:
        if int(getattr(d, "magic", 0) or 0) != MAGIC_HARD or int(getattr(d, "entry", -1) or 0) != 1:
            continue
        d_symbol = str(d.symbol)
        if symbol and d_symbol != symbol:
            continue
        net = round(float(d.profit or 0) + float(d.commission or 0) + float(d.swap or 0), 2)
        is_win = net > 0
        if result == "win" and not is_win:
            continue
        if result == "loss" and is_win:
            continue

        ticket = int(d.position_id)
        open_deal = opens.get(ticket)
        outcome_row = outcome_index.get(ticket)
        decision_row = decision_index.get(outcome_row.get("setup_id")) if outcome_row else None

        closed_at_dt = from_mt5_deal_time(d.time, BROKER_UTC_OFFSET_HOURS)
        if open_deal is not None:
            opened_at_dt = from_mt5_deal_time(open_deal.time, BROKER_UTC_OFFSET_HOURS)
            direction = "BUY" if int(getattr(open_deal, "type", 0) or 0) == 0 else "SELL"
            entry_price = float(open_deal.price) if open_deal.price else None
        elif outcome_row and outcome_row.get("opened_at"):
            try:
                opened_at_dt = datetime.fromisoformat(str(outcome_row["opened_at"]).replace("Z", "+00:00"))
            except ValueError:
                opened_at_dt = None
            direction = outcome_row.get("direction") or ("BUY" if int(getattr(d, "type", 0) or 0) == 1 else "SELL")
            entry_price = outcome_row.get("entry")
        else:
            opened_at_dt = None
            direction = "BUY" if int(getattr(d, "type", 0) or 0) == 1 else "SELL"
            entry_price = None

        opened_at_iso = opened_at_dt.isoformat() if opened_at_dt else None
        closed_at_iso = closed_at_dt.isoformat()

        close_mode = (outcome_row or {}).get("outcome") or _parse_close_mode_from_comment(getattr(d, "comment", ""))

        entries.append({
            "ticket": ticket,
            "symbol": d_symbol,
            "direction": direction,
            "entry": entry_price if entry_price is not None else (outcome_row or {}).get("entry"),
            "exit": float(d.price) if d.price else (outcome_row or {}).get("close_price"),
            "sl": (outcome_row or {}).get("sl"),
            "tp": (outcome_row or {}).get("tp"),
            "pnl_usd": net,
            "opened_at": opened_at_iso,
            "closed_at": closed_at_iso,
            "duration_seconds": _duration_seconds(opened_at_iso, closed_at_iso),
            "close_mode": close_mode,
            "strategy": (decision_row or {}).get("strategy"),
            "confluence_at_entry": (decision_row or {}).get("final_confluence_score") or (decision_row or {}).get("new_confluence"),
            "mae": (outcome_row or {}).get("mae"),
            "mfe": (outcome_row or {}).get("mfe"),
        })

    entries.sort(key=lambda e: e.get("closed_at") or "", reverse=True)
    return entries


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
    """mission/FIX_DASHBOARD_DATA.md (2026-07-08): trades, net_today_usd,
    wins and losses ALL come from the SAME _mt5_journal_entries() call —
    one MT5 query, one source of truth, mathematically identical to
    app.services.daily_killswitch's own daily_pnl (same window, same
    to_mt5_query_bounds conversion, same net formula) by construction.
    Verified live: both showed 16.43 with 0.0000 cross-check divergence."""
    now = _now_utc()
    start, end = _broker_day_window(now)
    trades = _mt5_journal_entries(start, end)
    net_today_usd = round(sum(t["pnl_usd"] for t in trades), 2)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = sum(1 for t in trades if t["pnl_usd"] < 0)

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
        "net_today_usd": net_today_usd,
        "wins": wins,
        "losses": losses,
        "refused_by_reason": dict(refused),
        "generated_at": now.isoformat(),
    }


# ── /api/journal ─────────────────────────────────────────────────────────

def build_journal(page: int = 1, page_size: int = 20, symbol: str | None = None, result: str | None = None) -> dict:
    """result: 'win' | 'loss' | None (all). symbol: 'GOLD#' | 'BTCUSD#' | None
    (all). mission/FIX_DASHBOARD_DATA.md (2026-07-08): defaults to TODAY's
    broker-day window — "le total net affiché en haut du journal doit
    correspondre exactement au P&L réel réconcilié (cohérent avec le
    +13.70 du jour)" only holds by construction when the journal itself is
    scoped to today; the Tous/Gagnants/Perdants/GOLD/BTC filters apply
    within that day, matching a control-room's natural "what happened
    today" framing rather than an unbounded multi-week scroll."""
    now = _now_utc()
    start, end = _broker_day_window(now)
    entries = _mt5_journal_entries(start, end, symbol=symbol, result=result)
    total = len(entries)
    net_total_usd = round(sum(float(e["pnl_usd"]) for e in entries), 2)
    start_idx = max(0, (page - 1) * page_size)
    page_entries = entries[start_idx:start_idx + page_size]
    return {
        "entries": page_entries,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "net_total_usd": net_total_usd,
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

    dataset_lines = len(_refresh_dataset_cache())
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
