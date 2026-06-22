from __future__ import annotations

from app.logger import log
from app.utils.throttle import log_event_throttled


def confirm(symbol: str, candidate: dict, settings: object) -> dict:
    """Behavior-neutral ML confirmator stub.

    Always returns UNAVAILABLE. UNAVAILABLE and WARMING_UP are strictly neutral:
    they must NEVER set demo_eligible=False on their own and must never block.
    """
    _state = (symbol, "UNAVAILABLE", "NOT_IMPLEMENTED")
    log_event_throttled(
        f"ML_RF_CONFIRMATOR:{symbol}",
        "[ML_RF_CONFIRMATOR] symbol=%s ml_status=UNAVAILABLE reason=NOT_IMPLEMENTED" % symbol,
        state=_state,
    )
    # §v1.5-fix: unified ML verdict tag for all symbols (consistent schema)
    log_event_throttled(
        f"FINAL_VERDICT_ML_UNAVAILABLE:{symbol}",
        "[FINAL_VERDICT_ML] symbol=%s ml_status=UNAVAILABLE decision=NEUTRAL" % symbol,
        state=(symbol, "UNAVAILABLE", "NEUTRAL"),
    )
    return {
        "ml_status": "UNAVAILABLE",
        "ml_reason": "NOT_IMPLEMENTED",
        "ml_confidence": None,
    }
