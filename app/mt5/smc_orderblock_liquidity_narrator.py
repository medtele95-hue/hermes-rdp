from __future__ import annotations

from app.logger import log
from app.utils.throttle import log_event_throttled


def narrate(symbol: str, candidate: dict, settings: object) -> dict:
    """Behavior-neutral SMC order block / liquidity narrator stub.

    Observational mode only. MUST NOT block any candidate that currently passes.
    Emits required log tags for observability.
    """
    log_event_throttled(
        f"SMC_OB_NARRATOR:{symbol}",
        "[SMC_OB_NARRATOR] symbol=%s status=NOT_IMPLEMENTED reason=NOT_IMPLEMENTED" % symbol,
        state=(symbol, "NOT_IMPLEMENTED"),
    )
    log_event_throttled(
        f"SMC_OB_AVOID:{symbol}",
        "[SMC_OB_AVOID] symbol=%s avoid=False reason=NOT_IMPLEMENTED" % symbol,
        state=(symbol, False, "NOT_IMPLEMENTED"),
    )
    log_event_throttled(
        f"SMC_OB_ENTRY_WINDOW:{symbol}",
        "[SMC_OB_ENTRY_WINDOW] symbol=%s window=None reason=NOT_IMPLEMENTED" % symbol,
        state=(symbol, None, "NOT_IMPLEMENTED"),
    )
    return {
        "smc_ob_status": "NOT_IMPLEMENTED",
        "smc_ob_avoid": False,
        "smc_ob_entry_window": None,
        "smc_ob_reason": "NOT_IMPLEMENTED",
    }
