"""Select exactly one BTC exit authority for a position."""
from __future__ import annotations

import math

from app.config import BTC_EXIT_DYNAMIC_ATR_MAX, BTC_EXIT_QUICK_ATR_MAX


def select_exit_manager(atr: float | None, grade: str | None = None) -> dict:
    try:
        atr_value = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        atr_value = 0.0
    if not math.isfinite(atr_value) or atr_value <= 0.0:
        manager, reason = "SWING", "ATR_UNAVAILABLE_FAIL_SAFE"
    elif atr_value < BTC_EXIT_QUICK_ATR_MAX:
        manager, reason = "QUICK", "ATR_BELOW_QUICK_MAX"
    elif atr_value <= BTC_EXIT_DYNAMIC_ATR_MAX:
        manager, reason = "DYNAMIC", "ATR_WITHIN_DYNAMIC_RANGE"
    else:
        manager, reason = "SWING", "ATR_ABOVE_DYNAMIC_MAX"
    return {
        "manager": manager,
        "enabled_managers": [manager],
        "atr": atr_value if atr_value > 0.0 else None,
        "grade": str(grade or "UNKNOWN").upper(),
        "reason": reason,
    }
