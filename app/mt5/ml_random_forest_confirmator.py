from __future__ import annotations

from app.logger import log


def confirm(symbol: str, candidate: dict, settings: object) -> dict:
    """Behavior-neutral ML confirmator stub.

    Always returns UNAVAILABLE. UNAVAILABLE and WARMING_UP are strictly neutral:
    they must NEVER set demo_eligible=False on their own and must never block.
    """
    log.info(
        "[ML_RF_CONFIRMATOR] symbol=%s ml_status=UNAVAILABLE reason=NOT_IMPLEMENTED",
        symbol,
    )
    # §v1.5-fix: unified ML verdict tag for all symbols (consistent schema)
    log.info(
        "[FINAL_VERDICT_ML] symbol=%s ml_status=UNAVAILABLE decision=NEUTRAL",
        symbol,
    )
    return {
        "ml_status": "UNAVAILABLE",
        "ml_reason": "NOT_IMPLEMENTED",
        "ml_confidence": None,
    }
