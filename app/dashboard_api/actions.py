# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — action handlers.

None of these functions ever touch trading directly — there is no
trade-submission call anywhere in this module, by design (mirrors the
invariant app/local_api already enforces for its own read-only routes; the
repo-wide safety test greps for that exact function name outside
app/mt5/demo_router.py, so it's spelled out fully only in that one file).
Every action here is either a PROCESS control (kill the bot, let the
existing supervisor — scripts/bot_supervisor.ps1 — restart it) or a CONFIG
write the bot reads at its own pace (active symbols, active account
profile). The actual trading engine independently re-verifies every
invariant on its own next cycle; nothing here can force it to skip that.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.mt5.account_profiles import VALID_PROFILES, load_profiles, set_active_profile_name

REPO_ROOT = Path(__file__).resolve().parents[2]
STOPPED_BY_USER_FLAG = REPO_ROOT / "logs" / "stopped_by_user.flag"
ACTIVE_SYMBOLS_FILE = REPO_ROOT / "app" / "data" / "active_symbols.json"
SYMBOL_ALLOWLIST = ("GOLD#", "BTCUSD#")
MAGIC_HARD = 909002


def _find_bot_pids() -> list[int]:
    """Mirrors scripts/bot_supervisor.ps1's Test-BotAlive detection exactly
    (same command-line substring match) so both agree on what "the bot" is."""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.splitlines():
            low = line.lower()
            if ("app/main.py" in low or "app\\main.py" in low or "app.main" in low) and "watchdog" not in low and "dashboard" not in low:
                parts = line.strip().split(",")
                pid_str = parts[-1].strip() if parts else ""
                if pid_str.isdigit():
                    pids.append(int(pid_str))
    except Exception:
        pass
    return pids


def is_bot_running() -> bool:
    return len(_find_bot_pids()) > 0


def is_stopped_by_user() -> bool:
    return STOPPED_BY_USER_FLAG.exists()


# ── bot process control ─────────────────────────────────────────────────

def action_bot_stop() -> dict:
    """Kills the bot process and marks it intentionally stopped — the
    supervisor (bot_supervisor.ps1, patched to check this flag) will NOT
    auto-restart it until action_bot_start() clears the flag.

    Documented behaviour (mission requirement): open positions keep their
    broker-side SL/TP exactly as they were at the moment of the stop. Exit
    V2's virtual trailing stops only run inside the bot process — once it's
    stopped, positions no longer trail, they simply sit at their last
    broker SL/TP until the bot restarts or the position hits SL/TP/is
    closed manually.
    """
    pids = _find_bot_pids()
    STOPPED_BY_USER_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOPPED_BY_USER_FLAG.write_text("stopped via dashboard", encoding="utf-8")
    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            killed.append(pid)
        except Exception:
            pass
    return {
        "ok": True,
        "killed_pids": killed,
        "note": "positions gardent leur SL/TP broker; Exit V2 arrete de trailer tant que le bot est stoppe",
    }


def action_bot_start() -> dict:
    """Clears the stopped-by-user flag. The bot actually comes back up via
    the existing supervisor's normal crash-recovery loop (scripts/
    bot_supervisor.ps1, checks every 60s) — not started directly here, to
    avoid any chance of a duplicate process racing with the supervisor."""
    if STOPPED_BY_USER_FLAG.exists():
        try:
            STOPPED_BY_USER_FLAG.unlink()
        except OSError:
            pass
    return {"ok": True, "note": "flag leve, le superviseur redemarrera le bot sous 60s"}


def action_bot_restart() -> dict:
    """Kills the bot without setting the stopped-by-user flag — the
    supervisor's normal crash-recovery restarts it within 60s, identical to
    an unplanned crash recovery, just triggered intentionally."""
    if STOPPED_BY_USER_FLAG.exists():
        try:
            STOPPED_BY_USER_FLAG.unlink()
        except OSError:
            pass
    pids = _find_bot_pids()
    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            killed.append(pid)
        except Exception:
            pass
    return {"ok": True, "killed_pids": killed, "note": "le superviseur relance sous 60s"}


# ── symbol allowlist toggle ─────────────────────────────────────────────

def action_symbols_toggle(gold_active: bool, btc_active: bool) -> dict:
    """Writes the active-symbols subset the bot's own choke-point
    (app.mt5.demo_router._active_symbols_subset) intersects with the hard
    SYMBOL_ALLOWLIST invariant — can only narrow it, never widen it (see
    that function's own docstring). Takes effect immediately (re-read every
    check), no restart needed.
    """
    if not gold_active and not btc_active and is_bot_running():
        return {"ok": False, "reason": "AT_LEAST_ONE_SYMBOL_OR_BOT_PAUSED_REQUIRED"}
    active = [s for s, on in (("GOLD#", gold_active), ("BTCUSD#", btc_active)) if on]
    ACTIVE_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_SYMBOLS_FILE.write_text(json.dumps(active), encoding="utf-8")
    return {"ok": True, "active_symbols": active}


# ── account switch (DEMO <-> REAL) ──────────────────────────────────────

def _probe_target_equity(profile) -> float | None:
    """Best-effort equity preview for the confirmation screen, run in a
    fully separate subprocess so it can NEVER interfere with this process's
    own read-only MT5 connection (or the bot's, which is a different
    process entirely already). Returns None on any failure/timeout — the
    confirmation flow must still work without this, it's a courtesy, not a
    gate."""
    if profile.login is None:
        return None
    script = (
        "import MetaTrader5 as mt5, sys, json\n"
        f"kwargs = {{'login': {profile.login!r}, 'password': {profile.password!r}, 'server': {profile.server!r}}}\n"
        f"path = {profile.terminal_path!r}\n"
        "if path:\n"
        "    kwargs['path'] = path\n"
        "ok = mt5.initialize(**kwargs)\n"
        "if not ok:\n"
        "    print('null'); sys.exit(0)\n"
        "acc = mt5.account_info()\n"
        "print(json.dumps(acc.equity if acc else None))\n"
        "mt5.shutdown()\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        )
        value = json.loads(result.stdout.strip() or "null")
        return float(value) if value is not None else None
    except Exception:
        return None


def _current_account_has_open_positions() -> bool:
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get() or []
        return any(int(getattr(p, "magic", 0) or 0) == MAGIC_HARD for p in positions)
    except Exception:
        # Fail-safe: if we can't verify, refuse the switch rather than risk
        # abandoning open positions on the account being left.
        return True


def action_account_switch(target: str, confirm_real_text: str | None = None) -> dict:
    """MÉCANIQUE (mission/DASHBOARD.md): writes which pre-declared profile is
    active, kills the bot cleanly, verifies no open positions on the
    account being LEFT, lets the supervisor restart the bot onto the target
    profile.

    Sovereign gate this action can NEVER reach or override: even after a
    successful switch, app.services.adaptive_account_policy.
    trading_authorized() independently re-verifies the LIVE MT5 account's
    trade_mode/login/server against settings.real_declared_login/server AND
    the settings.allow_live_trading master switch on every single order —
    neither of those settings is written by this function, by this module,
    or by the dashboard at all. Switching to a REAL profile here only
    determines which MT5 account the bot's process connects to; it grants
    no trading permission by itself.
    """
    target = str(target or "").upper()
    if target not in VALID_PROFILES:
        return {"ok": False, "reason": "INVALID_TARGET"}

    if target == "REAL" and confirm_real_text != "REAL":
        return {"ok": False, "reason": "REAL_CONFIRMATION_TEXT_MISMATCH"}

    profiles = load_profiles()
    profile = profiles.get(target)
    if profile is None or not profile.configured:
        return {"ok": False, "reason": "PROFILE_NOT_CONFIGURED"}

    if _current_account_has_open_positions():
        return {"ok": False, "reason": "OPEN_POSITIONS_ON_CURRENT_ACCOUNT"}

    target_equity = _probe_target_equity(profile) if target == "REAL" else None

    set_active_profile_name(target)
    restart_result = action_bot_restart()

    return {
        "ok": True,
        "target_profile": target,
        "target_equity_preview": target_equity,
        "restart": restart_result,
    }
