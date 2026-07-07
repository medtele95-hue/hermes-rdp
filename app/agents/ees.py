"""EES — Entry Exhaustion Score (0-100), the extreme-chasing killer.

Scores how exhausted the move already is IN the direction of the proposed
entry, on confirmed M5 candles:
- BUY  : buying into an overextended rally (top-chasing — validated on a
         real -40.86 loss taken at 73.7 EXTREME).
- SELL : selling into an overextended dump (exact mirror via sign flip).

Bands (graduated guard):
- SAIN     : score < 40  -> no effect.
- PRUDENCE : 40 <= score < 65 -> graduated penalty, max -15.
- EXTREME  : score >= 65 -> hard block (EES_EXTREME_BLOCK).

Components (each capped, total clamped 0-100):
- consecutive directional closes (max 30)
- extension of the move vs ATR from the opposing extreme (max 40)
- climax candle: last true range vs ATR (max 15)
- order-flow divergence against the entry (max 15)
"""
from __future__ import annotations

import math

from app.logger import log

BAND_SAIN = "SAIN"
BAND_PRUDENCE = "PRUDENCE"
BAND_EXTREME = "EXTREME"

PRUDENCE_THRESHOLD = 40.0
EXTREME_THRESHOLD = 65.0
MAX_PRUDENCE_PENALTY = -15.0

_LOOKBACK = 20
_CONSECUTIVE_WINDOW = 8


def ees_band(score: float | None) -> str:
    if score is None:
        return BAND_SAIN
    if score >= EXTREME_THRESHOLD:
        return BAND_EXTREME
    if score >= PRUDENCE_THRESHOLD:
        return BAND_PRUDENCE
    return BAND_SAIN


def ees_penalty(score: float | None) -> float:
    """Graduated PRUDENCE penalty: 0 at 40, -15 approaching 65."""
    if score is None or score < PRUDENCE_THRESHOLD or score >= EXTREME_THRESHOLD:
        return 0.0
    span = EXTREME_THRESHOLD - PRUDENCE_THRESHOLD
    return round(MAX_PRUDENCE_PENALTY * (score - PRUDENCE_THRESHOLD) / span, 2)


def compute_ees(direction: str, candles: list[dict] | None, context: dict | None = None) -> dict:
    """Compute the exhaustion score for a BUY or SELL candidate.

    The SELL path and the BUY path share this exact code — the direction only
    flips the sign, so the two guards are symmetric by construction.
    """
    side = str(direction or "").upper()
    if side not in {"BUY", "SELL"}:
        return _result(side, None, "EES_NOT_APPLICABLE")
    rows = [row for row in (candles or []) if isinstance(row, dict)]
    if len(rows) < 10:
        return _result(side, None, "EES_INSUFFICIENT_DATA")

    rows = rows[-(_LOOKBACK + 1):]
    opens = [_to_float(r.get("open")) for r in rows]
    highs = [_to_float(r.get("high")) for r in rows]
    lows = [_to_float(r.get("low")) for r in rows]
    closes = [_to_float(r.get("close")) for r in rows]
    if any(v is None for v in opens + highs + lows + closes):
        return _result(side, None, "EES_INSUFFICIENT_DATA")

    atr = _atr(highs, lows, closes)
    if atr is None or atr <= 0:
        return _result(side, None, "EES_ATR_UNAVAILABLE")

    sign = 1.0 if side == "BUY" else -1.0

    # 1) consecutive directional closes (max 30)
    consecutive = 0
    for i in range(len(closes) - 1, 0, -1):
        if sign * (closes[i] - closes[i - 1]) > 0:
            consecutive += 1
        else:
            break
        if consecutive >= _CONSECUTIVE_WINDOW:
            break
    consecutive_pts = min(30.0, consecutive * (30.0 / 6.0))

    # 2) extension vs ATR from the opposing extreme (max 40)
    if side == "BUY":
        opposing_extreme = min(lows[-_LOOKBACK:])
        extension = (closes[-1] - opposing_extreme) / atr
    else:
        opposing_extreme = max(highs[-_LOOKBACK:])
        extension = (opposing_extreme - closes[-1]) / atr
    extension_pts = min(40.0, max(0.0, (extension - 1.0) * (40.0 / 3.0)))

    # 3) climax candle: last true range vs ATR (max 15)
    last_tr = max(
        highs[-1] - lows[-1],
        abs(highs[-1] - closes[-2]),
        abs(lows[-1] - closes[-2]),
    )
    climax_ratio = last_tr / atr
    climax_pts = min(15.0, max(0.0, (climax_ratio - 1.0) * 30.0))

    # 4) order-flow divergence against the entry (max 15)
    divergence_pts = 0.0
    of_payload = None
    if isinstance(context, dict):
        raw = context.get("order_flow_execution_agent") or context.get("order_flow") or {}
        of_payload = raw if isinstance(raw, dict) else None
    if of_payload:
        divergence = str(of_payload.get("divergence") or "").lower()
        cvd_slope = _to_float(of_payload.get("cvd_slope"))
        against = "bear" if side == "BUY" else "bull"
        if divergence == against:
            divergence_pts += 10.0
        if cvd_slope is not None and sign * cvd_slope < 0:
            divergence_pts += 5.0
    divergence_pts = min(15.0, divergence_pts)

    score = round(max(0.0, min(100.0, consecutive_pts + extension_pts + climax_pts + divergence_pts)), 1)
    result = _result(side, score, "EES_COMPUTED")
    result["components"] = {
        "consecutive_pts": round(consecutive_pts, 1),
        "extension_pts": round(extension_pts, 1),
        "climax_pts": round(climax_pts, 1),
        "divergence_pts": round(divergence_pts, 1),
        "extension_atr": round(extension, 2),
    }
    return result


def _result(side: str, score: float | None, reason: str) -> dict:
    return {
        "side": side or None,
        "score": score,
        "band": ees_band(score),
        "penalty": ees_penalty(score),
        "blocked": bool(score is not None and score >= EXTREME_THRESHOLD),
        "reason": reason,
    }


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if not trs:
        return None
    window = trs[-period:]
    value = sum(window) / len(window)
    return value if math.isfinite(value) and value > 0 else None


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
