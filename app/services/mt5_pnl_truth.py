from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import MetaTrader5 as mt5

from app.utils.broker_time import broker_day_window, to_mt5_query_bounds


def get_mt5_hermes_pnl_truth(
    hours: int,
    magic_number: int,
    now: datetime | None = None,
    window_start: datetime | None = None,
    broker_utc_offset_hours: float = 3.0,
) -> dict:
    end = _as_utc(now or datetime.now(timezone.utc))
    history_start = _as_utc(window_start) if window_start else end - timedelta(hours=hours)
    # mission/FIX_KILLSWITCH_PNL.md (2026-07-08): "today" MUST be the broker
    # calendar day (same definition app.services.daily_killswitch uses), not
    # a naive UTC-midnight cut — a raw `datetime(end.year, end.month,
    # end.day, tzinfo=utc)` only coincidentally matches the broker day
    # outside the ~21:00-24:00 UTC window, and silently lags a full day
    # behind broker's actual "today" inside it.
    today_start, _ = broker_day_window(end, broker_utc_offset_hours)
    forty_eight_start = end - timedelta(hours=48)

    initialized, init_error = _ensure_mt5_initialized()
    window = _history_deals_pnl(magic_number, history_start, end, broker_utc_offset_hours)
    today = _history_deals_pnl(magic_number, today_start, end, broker_utc_offset_hours)
    forty_eight = _history_deals_pnl(magic_number, forty_eight_start, end, broker_utc_offset_hours)
    available = bool(window.get("available"))
    error = window.get("error") or init_error

    return {
        "available": available,
        "pnl_source": "MT5_HISTORY_DEALS" if available else None,
        "mt5_window_pnl": window.get("pnl"),
        "mt5_today_pnl": today.get("pnl") if today.get("available") else None,
        "mt5_48h_pnl": forty_eight.get("pnl") if forty_eight.get("available") else None,
        "mt5_closed_deals_count": window.get("deals", 0) if available else 0,
        "mt5_gross_profit": window.get("gross_profit") if available else None,
        "mt5_gross_loss": window.get("gross_loss") if available else None,
        "mt5_profit_factor": window.get("profit_factor") if available else None,
        "mt5_history_error": error,
        "mt5_initialized": initialized,
        "mt5_last_error": _safe_last_error(),
        "history_start": history_start.isoformat(),
        "history_end": end.isoformat(),
        "deals_total_before_magic_filter": window.get("deals_total_before_magic_filter", 0),
        "deals_total_after_magic_filter": window.get("deals_total_after_magic_filter", 0),
    }


def _history_deals_pnl(magic_number: int, start: datetime, end: datetime, broker_utc_offset_hours: float = 3.0) -> dict:
    try:
        # mission/FIX_KILLSWITCH_PNL.md (2026-07-08): start/end are TRUE-UTC
        # instants — mt5.history_deals_get() ignores tzinfo and compares raw
        # clock fields directly against deal.time, which the broker stamps
        # in its own wall clock (UTC+3). Passing true-UTC values unconverted
        # silently queries 3h too early, missing the most recent deals and
        # pulling in the tail of the prior broker day. See
        # app.utils.broker_time.to_mt5_query_bounds for the verified fix.
        q_start, q_end = to_mt5_query_bounds(start, end, broker_utc_offset_hours)
        deals = mt5.history_deals_get(q_start, q_end)
    except Exception as exc:
        return _unavailable(str(exc))
    if deals is None:
        return _unavailable("MT5_HISTORY_DEALS_UNAVAILABLE")

    total = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    count = 0
    all_deals = list(deals)
    for deal in all_deals:
        magic = _deal_value(deal, "magic")
        try:
            if int(magic) != int(magic_number):
                continue
        except (TypeError, ValueError):
            continue
        net = (
            (_float_value(_deal_value(deal, "profit")) or 0.0)
            + (_float_value(_deal_value(deal, "commission")) or 0.0)
            + (_float_value(_deal_value(deal, "swap")) or 0.0)
        )
        total += net
        if net >= 0:
            gross_profit += net
        else:
            gross_loss += net
        count += 1
    profit_factor = None
    if gross_loss < 0:
        profit_factor = round(gross_profit / abs(gross_loss), 6)
    elif gross_profit > 0:
        profit_factor = float("inf")
    return {
        "available": True,
        "pnl": round(total, 6),
        "deals": count,
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "profit_factor": profit_factor,
        "error": None,
        "deals_total_before_magic_filter": len(all_deals),
        "deals_total_after_magic_filter": count,
    }


def _ensure_mt5_initialized() -> tuple[bool, str | None]:
    try:
        if mt5.initialize():
            return True, None
        return False, "MT5_INITIALIZE_FAILED"
    except Exception as exc:
        return False, str(exc)


def _safe_last_error() -> Any:
    try:
        return mt5.last_error()
    except Exception as exc:
        return str(exc)


def _unavailable(error: str) -> dict:
    return {
        "available": False,
        "pnl": None,
        "deals": 0,
        "gross_profit": None,
        "gross_loss": None,
        "profit_factor": None,
        "error": error,
        "deals_total_before_magic_filter": 0,
        "deals_total_after_magic_filter": 0,
    }


def _deal_value(deal: Any, key: str) -> Any:
    if isinstance(deal, dict):
        return deal.get(key)
    return getattr(deal, key, None)


def _float_value(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
