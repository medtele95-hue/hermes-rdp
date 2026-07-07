"""EXIT V2 — single exit authority for GOLD, CLOSE-BASED by design.

Replaces the HERMES_QUICK_EXIT parasite (TP money $1.50 / sneaky lock-SL
$0.80) which captured 100% of exits and starved every winner.

Design invariants:
- CLOSE-BASED: this engine NEVER modifies the SL. The protective levels are
  virtual (in-memory) and enforced by closing the position. The broker SL can
  therefore never be widened BY CONSTRUCTION.
- BE armed at +$2.00 profit -> virtual floor at +$0.10 (BE + buffer).
- Trailing armed at +$2.00 peak -> virtual floor at peak - $1.20.
- TP money disabled (tp_usd=0) — winners run.
- Fail-closed: any exception leaves the original SL/TP untouched.
- Account gate: DEMO=ACTIVE (closes execute), REAL=SHADOW (log-only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.logger import log

MODE_ACTIVE = "ACTIVE"
MODE_SHADOW = "SHADOW"


@dataclass(frozen=True)
class ExitV2Config:
    mode: str = MODE_ACTIVE
    tp_usd: float = 0.0            # 0 = money-TP disabled
    be_arm_usd: float = 2.00       # profit that arms the breakeven floor
    be_floor_usd: float = 0.10     # virtual floor once BE armed (BE + buffer)
    trail_start_usd: float = 2.00  # peak that arms the trailing floor
    trail_gap_usd: float = 1.20    # distance kept below the peak


def evaluate_exit_v2(pos: object, tick: object, symbol_info: object, cfg: ExitV2Config, state: dict) -> dict:
    """Pure decision function. Returns an action dict; never touches MT5.

    Actions: NONE / CLOSE (reason EXIT_V2_TP | EXIT_V2_BE_FLOOR |
    EXIT_V2_TRAIL_FLOOR). State per ticket: peak_usd, be_armed.
    """
    ticket = int(getattr(pos, "ticket", 0) or 0)
    profit = _to_float(getattr(pos, "profit", None))
    side = "BUY" if int(getattr(pos, "type", 0) or 0) == 0 else "SELL"
    if ticket <= 0 or profit is None:
        return {"action": "NONE", "reason": "EXIT_V2_UNREADABLE_POSITION", "ticket": ticket, "side": side}

    st = state.setdefault(ticket, {"peak_usd": profit, "be_armed": False})
    st["peak_usd"] = max(_to_float(st.get("peak_usd")) or profit, profit)
    peak = st["peak_usd"]

    if not st["be_armed"] and profit >= cfg.be_arm_usd:
        st["be_armed"] = True
        log.info(
            "[EXIT_V2] action=BE_ARMED ticket=%s profit=%.2f floor=%.2f",
            ticket, profit, cfg.be_floor_usd,
        )

    # Money TP (disabled when tp_usd == 0)
    if cfg.tp_usd and cfg.tp_usd > 0 and profit >= cfg.tp_usd:
        return _close(ticket, "EXIT_V2_TP", profit, peak, st["be_armed"], side=side)

    # Trailing floor: once the peak reached trail_start, protect peak - gap.
    # The trailing floor can only rise (peak is monotonic).
    floors: list[tuple[str, float]] = []
    if peak >= cfg.trail_start_usd:
        floors.append(("EXIT_V2_TRAIL_FLOOR", peak - cfg.trail_gap_usd))
    if st["be_armed"]:
        floors.append(("EXIT_V2_BE_FLOOR", cfg.be_floor_usd))

    if floors:
        reason, floor = max(floors, key=lambda item: item[1])
        if profit <= floor:
            return _close(ticket, reason, profit, peak, st["be_armed"], floor, side=side)

    return {
        "action": "NONE",
        "reason": "EXIT_V2_HOLD",
        "ticket": ticket,
        "side": side,
        "profit_usd": profit,
        "peak_usd": peak,
        "be_armed": st["be_armed"],
        "active_floor": max((f for _, f in floors), default=None),
    }


def _close(
    ticket: int,
    reason: str,
    profit: float,
    peak: float,
    be_armed: bool,
    floor: float | None = None,
    side: str = "BUY",
) -> dict:
    return {
        "action": "CLOSE",
        "reason": reason,
        "ticket": ticket,
        "side": side,
        "profit_usd": profit,
        "peak_usd": peak,
        "be_armed": be_armed,
        "floor_usd": floor,
    }


def is_gold_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().strip()
    return normalized.startswith("GOLD") or normalized.startswith("XAUUSD")


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
