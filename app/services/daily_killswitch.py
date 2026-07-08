"""DAILY_KILLSWITCH — armored daily loss guard.

Four armor plates (each one fixed a production incident):
1. BROKER-day window: [broker midnight, now + 2h]. The broker (XM) stamps
   deals in UTC+3, so a deal can look like it lives "in the future" relative
   to UTC — the +2h tail guarantees it is never missed (the historical
   blind spot). Broker midnight == 21:00 UTC of the previous day.
2. Nothing in memory: the counter re-reads the broker deals history at
   EVERY evaluation — it survives any restart by construction.
3. Quotas per account policy (first threshold reached stops trading):
   DEMO 6 losses/day + 3% daily drawdown; REAL_DECLARED 3/day;
   REAL_UNKNOWN 1/day.
4. mission/FIX_KILLSWITCH_DATE.md (2026-07-08): the window is now computed
   through app.utils.broker_time — a SINGLE, centralized "now" source
   shared with every other broker-day-window consumer in the codebase
   (was previously duplicated locally here), with a built-in anti-
   regression guard that logs CRITICAL if the computed window is ever
   found to be more than 48h stale — the exact signature of this bug
   family, caught the moment it happens instead of discovered later.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.logger import log
from app.utils.broker_time import broker_day_window as _centralized_broker_day_window
from app.utils.broker_time import broker_now_utc


def broker_day_window(now_utc: datetime, broker_utc_offset_hours: float) -> tuple[datetime, datetime]:
    """[broker midnight, now + 2h], both expressed in UTC. Thin wrapper kept
    for backward compatibility — delegates to the centralized
    app.utils.broker_time implementation."""
    return _centralized_broker_day_window(now_utc, broker_utc_offset_hours)


def evaluate_daily_killswitch(
    policy,
    settings,
    magic: int,
    account: object = None,
    now_utc: datetime | None = None,
    history_fn=None,
) -> dict:
    """Stateless daily kill-switch. Re-reads broker deals on every call."""
    now_utc = now_utc if now_utc is not None else broker_now_utc()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    offset_hours = float(getattr(settings, "broker_utc_offset_hours", 3.0))
    start_utc, end_utc = broker_day_window(now_utc, offset_hours)

    if history_fn is None:
        history_fn = _mt5_history_deals

    try:
        deals = list(history_fn(start_utc, end_utc) or [])
    except Exception as exc:
        # Fail-closed: unreadable history blocks trading rather than
        # trading blind past the quota.
        log.warning("[DAILY_KILLSWITCH] history_unreadable error=%s -> FAIL_CLOSED", str(exc)[:200])
        return {
            "triggered": True,
            "reason": "DAILY_KILLSWITCH_HISTORY_UNREADABLE",
            "losses_today": None,
            "daily_pnl": None,
            "drawdown_pct": None,
            "max_losses_per_day": getattr(policy, "max_losses_per_day", None),
            "window_start_utc": start_utc.isoformat(),
            "window_end_utc": end_utc.isoformat(),
            "account_policy": getattr(policy, "level", None),
        }

    losses_today = 0
    daily_pnl = 0.0
    counted = 0
    for deal in deals:
        if int(getattr(deal, "magic", -1) or -1) != int(magic):
            continue
        if int(getattr(deal, "entry", -1) or 0) != 1:  # DEAL_ENTRY_OUT only
            continue
        net = _to_float(getattr(deal, "profit", 0)) or 0.0
        net += _to_float(getattr(deal, "commission", 0)) or 0.0
        net += _to_float(getattr(deal, "swap", 0)) or 0.0
        counted += 1
        daily_pnl += net
        if net < 0:
            losses_today += 1

    balance = None
    if account is not None:
        source = account if isinstance(account, dict) else vars(account) if hasattr(account, "__dict__") else {}
        balance = _to_float(source.get("balance")) or _to_float(source.get("equity"))
    drawdown_pct = None
    if balance and balance > 0:
        drawdown_pct = abs(min(0.0, daily_pnl)) / balance * 100.0

    max_losses = int(getattr(policy, "max_losses_per_day", 1) or 1)
    max_dd = getattr(policy, "max_daily_drawdown_percent", None)

    triggered = False
    reason = None
    if losses_today >= max_losses:
        triggered = True
        reason = "DAILY_KILLSWITCH_MAX_LOSSES"
    elif max_dd is not None and drawdown_pct is not None and drawdown_pct >= float(max_dd):
        triggered = True
        reason = "DAILY_KILLSWITCH_DRAWDOWN"

    result = {
        "triggered": triggered,
        "reason": reason,
        "losses_today": losses_today,
        "deals_counted": counted,
        "daily_pnl": round(daily_pnl, 2),
        "drawdown_pct": round(drawdown_pct, 3) if drawdown_pct is not None else None,
        "max_losses_per_day": max_losses,
        "max_daily_drawdown_percent": max_dd,
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "broker_utc_offset_hours": offset_hours,
        "account_policy": getattr(policy, "level", None),
        "now_utc_detected": now_utc.isoformat(),
    }
    # mission/FIX_KILLSWITCH_DATE.md: now_utc_detected loggé explicitement
    # à côté de la fenêtre — une dérive future (fenêtre qui ne colle plus à
    # "aujourd'hui") est visible en un coup d'oeil sans avoir à recalculer.
    log.info(
        "[DAILY_KILLSWITCH] now_utc_detected=%s triggered=%s reason=%s losses=%s/%s daily_pnl=%.2f dd_pct=%s policy=%s window=[%s -> %s]",
        result["now_utc_detected"], triggered, reason, losses_today, max_losses, daily_pnl,
        result["drawdown_pct"], result["account_policy"],
        result["window_start_utc"], result["window_end_utc"],
    )
    return result


def _mt5_history_deals(start_utc: datetime, end_utc: datetime):  # pragma: no cover - live MT5 only
    import MetaTrader5 as mt5
    return mt5.history_deals_get(start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
