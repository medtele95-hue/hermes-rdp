from __future__ import annotations

import math

import pandas as pd

from app.config import Settings
from app.utils.indicators import atr


STRATEGY = "AMD_FVG_IFVG_REVERSAL"


def evaluate(symbol: str, frames: dict, context: dict, settings) -> dict:
    settings = settings or Settings()
    if not settings.amd_fvg_ifvg_reversal_enabled:
        return _result(symbol, "WAIT", 0, reason="AMD_FVG_IFVG_REVERSAL_DISABLED")
    if _explicit_not_ok(context, settings):
        return _result(symbol, "WAIT", 0, reason="SAFETY_OR_RISK_NOT_OK")
    m5 = _frame(frames, "M5")
    if m5 is None or len(m5) < 5:
        return _result(symbol, "WAIT", 0, reason="MISSING_AMD_FVG_DATA")
    accumulation = _accumulation(m5, context)
    manipulation, side = _manipulation(m5, context)
    displacement, disp_dir = _displacement(m5, context)
    fvg_context = _fvg_context(m5, context)
    confirmation = bool(context.get("smc_m15_confirmation") or context.get("smc_m5_confirmation") or context.get("m5_cisd"))
    rr = _float(context.get("risk_reward") or context.get("reward_risk")) or 1.8
    risk_safety = context.get("safety_guard_status") in {None, "PASS"} and context.get("risk_diag_status") in {None, "OK"}
    score = 0
    if accumulation:
        score += 15
    if manipulation:
        score += 20
    if displacement:
        score += 20
    if fvg_context != "NONE":
        score += 20
    if confirmation:
        score += 15
    if risk_safety:
        score += 10

    signal = "WAIT"
    if (
        accumulation
        and manipulation
        and displacement
        and fvg_context != "NONE"
        and confirmation
        and rr >= settings.new_strategies_require_rr_min
        and score >= settings.new_strategies_min_score
    ):
        if side == "LOW" and disp_dir in {"BULLISH", "UNKNOWN"}:
            signal = "BUY"
        elif side == "HIGH" and disp_dir in {"BEARISH", "UNKNOWN"}:
            signal = "SELL"
    entry, sl, tp = _levels(m5, signal)
    return _result(
        symbol,
        signal,
        score,
        entry=entry if signal in {"BUY", "SELL"} else None,
        sl=sl if signal in {"BUY", "SELL"} else None,
        tp=tp if signal in {"BUY", "SELL"} else None,
        reason="AMD_FVG_IFVG_REVERSAL_ALIGNED" if signal in {"BUY", "SELL"} else "AMD_FVG_IFVG_CONDITIONS_NOT_MET",
        extra={
            "amd_fvg_score": score,
            "amd_phase_detected": "ACCUMULATION_MANIPULATION_DISPLACEMENT" if accumulation and manipulation and displacement else "INCOMPLETE",
            "manipulation_detected": manipulation,
            "displacement_detected": displacement,
            "fvg_ifvg_context": fvg_context,
            "amd_fvg_reason": "AMD_FVG_IFVG_REVERSAL_ALIGNED" if signal in {"BUY", "SELL"} else "AMD_FVG_IFVG_CONDITIONS_NOT_MET",
        },
    )


def _accumulation(m5: pd.DataFrame, context: dict) -> bool:
    if str(context.get("h4_bias") or "").upper() == "RANGE" or str(context.get("market_phase") or "").upper() in {"ACCUMULATION", "RANGE"}:
        return True
    if len(m5) < 20:
        return False
    recent_range = float(m5["high"].tail(8).max() - m5["low"].tail(8).min())
    recent_atr = _float(atr(m5.tail(20), 14).iloc[-1]) or 0.0
    return recent_atr > 0 and recent_range <= 1.2 * recent_atr


def _manipulation(m5: pd.DataFrame, context: dict) -> tuple[bool, str]:
    if context.get("turtle_soup_ote") or str(context.get("smc_h1_liquidity") or "").upper() in {"EQUAL_HIGHS", "EQUAL_LOWS"}:
        liquidity = str(context.get("smc_h1_liquidity") or "").upper()
        return True, "LOW" if liquidity == "EQUAL_LOWS" else "HIGH"
    prev = m5.iloc[:-1].tail(12)
    last = m5.iloc[-1]
    if prev.empty:
        return False, "NONE"
    if _float(last.get("high")) is not None and _float(last.get("high")) > _float(prev["high"].max()):
        return True, "HIGH"
    if _float(last.get("low")) is not None and _float(last.get("low")) < _float(prev["low"].min()):
        return True, "LOW"
    return False, "NONE"


def _displacement(m5: pd.DataFrame, context: dict) -> tuple[bool, str]:
    shift = str(context.get("m15_structure_shift") or "").upper()
    if context.get("m5_cisd") or shift in {"BULLISH", "BEARISH"}:
        return True, shift if shift in {"BULLISH", "BEARISH"} else "UNKNOWN"
    row = m5.iloc[-1]
    high = _float(row.get("high"))
    low = _float(row.get("low"))
    open_ = _float(row.get("open"))
    close = _float(row.get("close"))
    if None in {high, low, open_, close} or high == low:
        return False, "UNKNOWN"
    body = abs(close - open_)
    candle_range = high - low
    recent_atr = _float(atr(m5.tail(20), 14).iloc[-1]) or 0.0
    ok = body >= 0.6 * candle_range and (recent_atr <= 0 or candle_range >= 1.2 * recent_atr)
    return ok, "BULLISH" if close > open_ else "BEARISH"


def _fvg_context(m5: pd.DataFrame, context: dict) -> str:
    inferred = str(context.get("smc_h1_fvg") or "").upper()
    if inferred != "NONE" and inferred:
        return inferred
    if context.get("ifvg_ote_sniper") or context.get("breaker_fvg_ote") or context.get("amd_bpr_ote"):
        return "INFERRED_IFVG_OB_OTE"
    if len(m5) < 3:
        return "NONE"
    a = m5.iloc[-3]
    c = m5.iloc[-1]
    if _float(c.get("low")) is not None and _float(a.get("high")) is not None and _float(c.get("low")) > _float(a.get("high")):
        return "BULLISH_FVG"
    if _float(c.get("high")) is not None and _float(a.get("low")) is not None and _float(c.get("high")) < _float(a.get("low")):
        return "BEARISH_FVG"
    return "NONE"


def _levels(m5: pd.DataFrame, signal: str) -> tuple[float | None, float | None, float | None]:
    if signal not in {"BUY", "SELL"}:
        return None, None, None
    entry = _float(m5.iloc[-1].get("close"))
    if entry is None:
        return None, None, None
    rng = max(_float(m5["high"].tail(10).max() - m5["low"].tail(10).min()) or abs(entry) * 0.001, abs(entry) * 0.001)
    if signal == "BUY":
        sl = entry - rng * 0.45
        tp = entry + rng * 0.75
    else:
        sl = entry + rng * 0.45
        tp = entry - rng * 0.75
    return entry, sl, tp


def _result(symbol: str, signal: str, score: int, entry=None, sl=None, tp=None, reason="", extra: dict | None = None) -> dict:
    payload = {
        "symbol": symbol,
        "timeframe": "M5",
        "strategy": STRATEGY,
        "signal": signal,
        "confidence": round(min(0.9, 0.45 + score / 200.0), 4),
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "reason": reason,
        "blocked_reason": None if signal in {"BUY", "SELL"} else reason,
        "strategy_status": "ACTIVE",
        "amd_fvg_score": score,
    }
    if extra:
        payload.update(extra)
    return payload


def _explicit_not_ok(context: dict, settings) -> bool:
    if settings.new_strategies_require_safety_pass and context.get("safety_guard_status") not in {None, "PASS"}:
        return True
    if settings.new_strategies_require_risk_ok and context.get("risk_diag_status") not in {None, "OK"}:
        return True
    return False


def _frame(frames: dict, key: str) -> pd.DataFrame | None:
    value = frames.get(key) if isinstance(frames, dict) else None
    return value if isinstance(value, pd.DataFrame) else None


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
