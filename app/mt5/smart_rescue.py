"""Smart Rescue Quick Exit — pure state-tracking logic for LOVABLE_BTC_OLD_SYSTEM.

No MT5 calls here. Order execution remains exclusively inside app/mt5/demo_router.py.

Three close rules:
  1. NEGATIVE_THEN_SMALL_POSITIVE — position dipped ≤ ARM_DRAWDOWN_USD then recovered ≥ MIN_PROFIT_USD.
  2. EMERGENCY_MULTIPLE_POSITIONS — multiple HERMES BTC positions open and profit ≥ EMERGENCY_POSITIVE_USD.
  3. AGE_EXCEEDED — position older than MAX_HOLD_SECONDS and profit ≥ MIN_PROFIT_USD.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class SmartRescueConfig:
    enabled: bool = True
    rescue_min_profit_usd: float = 0.08
    rescue_arm_drawdown_usd: float = -0.20
    emergency_open_count_gt: int = 1
    emergency_positive_usd: float = 0.08
    max_hold_seconds: int = 180
    protect_existing: bool = True
    magic_number: int = 909002
    max_open_positions: int = 1


_BTC_SYMBOLS: frozenset[str] = frozenset({"BTCUSD#", "BTCUSD"})


def is_hermes_btc_pos(pos: Any, magic: int = 909002) -> bool:
    """Return True if position is a HERMES BTC demo position."""
    symbol = str(getattr(pos, "symbol", "") or "").upper().strip()
    pos_magic = int(getattr(pos, "magic", -1) or -1)
    comment = str(getattr(pos, "comment", "") or "").upper()
    return symbol in _BTC_SYMBOLS and (pos_magic == magic or "HERMES" in comment)


def count_hermes_btc_open(positions: list[Any], magic: int = 909002) -> int:
    """Count how many open positions are HERMES BTC demo positions."""
    return sum(1 for p in positions if is_hermes_btc_pos(p, magic))


def evaluate_rescue(
    pos: Any,
    rescue_states: dict[int, dict],
    cfg: SmartRescueConfig,
    open_btc_count: int,
    now: datetime | None = None,
) -> dict | None:
    """Update per-position rescue state and return a close action when one of the rescue rules fires.

    Returns:
        dict(action="ARMED", ...)         — rescue just armed this tick (for logging only, no close).
        dict(action="RESCUE_CLOSE", ...)  — position should be closed now.
        None                              — nothing to do this tick.
    """
    now_ = now or datetime.now(timezone.utc)
    ticket = int(getattr(pos, "ticket", 0) or 0)
    profit = float(getattr(pos, "profit", 0) or 0)
    pos_time = int(getattr(pos, "time", 0) or 0)

    try:
        age_seconds: float | None = (
            (now_ - datetime.fromtimestamp(pos_time, tz=timezone.utc)).total_seconds()
            if pos_time
            else None
        )
    except (OSError, OverflowError, ValueError):
        age_seconds = None

    # Track whether this position was already armed before this tick
    was_armed_before = ticket in rescue_states and bool(rescue_states[ticket].get("rescue_armed"))

    # Seed state on first sighting of this ticket
    state = rescue_states.setdefault(ticket, {
        "ticket": ticket,
        "symbol": str(getattr(pos, "symbol", "") or ""),
        "magic": int(getattr(pos, "magic", -1) or -1),
        "comment": str(getattr(pos, "comment", "") or ""),
        "opened_at": pos_time,
        "min_seen_profit": profit,
        "max_seen_profit": profit,
        "was_negative": profit < 0,
        "rescue_armed": profit <= cfg.rescue_arm_drawdown_usd,
        "last_seen_profit": profit,
    })

    # Update running metrics
    state["last_seen_profit"] = profit
    if profit < state["min_seen_profit"]:
        state["min_seen_profit"] = profit
    if profit > state["max_seen_profit"]:
        state["max_seen_profit"] = profit

    # Arm if profit dips to / below the drawdown threshold
    if profit <= cfg.rescue_arm_drawdown_usd:
        state["rescue_armed"] = True
        state["was_negative"] = True

    rescue_armed = bool(state["rescue_armed"])
    min_seen = float(state["min_seen_profit"])

    # Notify caller that rescue was just armed this tick (caller logs [OLD_BTC_RESCUE_ARMED])
    if rescue_armed and not was_armed_before:
        return {
            "action": "ARMED",
            "ticket": ticket,
            "profit": profit,
            "min_seen_profit": min_seen,
        }

    # Rule 1 — armed position recovered past rescue_min_profit_usd
    if rescue_armed and profit >= cfg.rescue_min_profit_usd:
        return {
            "action": "RESCUE_CLOSE",
            "reason": "NEGATIVE_THEN_SMALL_POSITIVE",
            "ticket": ticket,
            "profit": profit,
            "min_seen_profit": min_seen,
            "rescue_armed": True,
        }

    # Rule 2 — emergency: multiple HERMES BTC open positions + any positive
    if open_btc_count > cfg.emergency_open_count_gt and profit >= cfg.emergency_positive_usd:
        return {
            "action": "RESCUE_CLOSE",
            "reason": "EMERGENCY_MULTIPLE_POSITIONS",
            "ticket": ticket,
            "profit": profit,
            "open_count": open_btc_count,
            "rescue_armed": rescue_armed,
        }

    # Rule 3 — position held too long + any small positive
    if (
        age_seconds is not None
        and age_seconds >= cfg.max_hold_seconds
        and profit >= cfg.rescue_min_profit_usd
    ):
        return {
            "action": "RESCUE_CLOSE",
            "reason": "AGE_EXCEEDED",
            "ticket": ticket,
            "profit": profit,
            "age_seconds": age_seconds,
            "rescue_armed": rescue_armed,
        }

    return None
