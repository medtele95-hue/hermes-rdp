"""Resolve MTF structure disagreements with explicit HTF priority."""
from __future__ import annotations


_BULL_VALUES = {"BULL", "BULLISH", "BUY", "UP"}
_BEAR_VALUES = {"BEAR", "BEARISH", "SELL", "DOWN"}
_RANGE_VALUES = {"RANGE", "NEUTRAL", "NEUT", "SIDEWAYS"}


def _direction(value: object) -> str:
    normalized = str(value or "").upper()
    if normalized in _BULL_VALUES:
        return "BULLISH"
    if normalized in _BEAR_VALUES:
        return "BEARISH"
    if normalized in _RANGE_VALUES:
        return "RANGE"
    return "UNKNOWN"


def arbitrate_mtf(
    h4_direction: object,
    d1_direction: object,
    smc_direction: object,
    momentum_direction: object,
) -> dict:
    h4 = _direction(h4_direction)
    d1 = _direction(d1_direction)
    smc = _direction(smc_direction)
    momentum = _direction(momentum_direction)
    directional = {"BULLISH", "BEARISH"}
    conflict = h4 in directional and d1 in directional and h4 != d1

    if d1 in directional:
        chosen, source, reason = d1, "D1", "D1_HTF_PRIORITY"
    elif h4 in directional:
        chosen, source, reason = h4, "H4", "H4_HTF_PRIORITY"
    elif momentum in directional:
        chosen, source, reason = momentum, "MOMENTUM", "HTF_RANGE_MOMENTUM_FALLBACK"
    elif smc in directional:
        chosen, source, reason = smc, "SMC", "HTF_RANGE_SMC_FALLBACK"
    else:
        chosen, source, reason = "NEUTRAL", "NONE", "NO_DIRECTIONAL_EVIDENCE"
    return {
        "direction": chosen,
        "source": source,
        "reason": reason,
        "conflict": conflict,
        "inputs": {"h4": h4, "d1": d1, "smc": smc, "momentum": momentum},
    }
