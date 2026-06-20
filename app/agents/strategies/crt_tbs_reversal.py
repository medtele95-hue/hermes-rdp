from __future__ import annotations

import math

import pandas as pd

from app.config import Settings


STRATEGY = "CRT_TBS_REVERSAL"


def evaluate(symbol: str, frames: dict, context: dict, settings) -> dict:
    settings = settings or Settings()
    if not settings.crt_tbs_reversal_enabled:
        return _result(symbol, "WAIT", 0, reason="CRT_TBS_REVERSAL_DISABLED")
    if _explicit_not_ok(context, settings):
        return _result(symbol, "WAIT", 0, reason="SAFETY_OR_RISK_NOT_OK")
    m5 = _frame(frames, "M5")
    if m5 is None or m5.empty:
        return _result(symbol, "WAIT", 0, reason="MISSING_M5_DATA")
    crt_high, crt_low = _range(frames, context)
    if crt_high is None or crt_low is None or crt_high <= crt_low:
        return _result(symbol, "WAIT", 0, reason="MISSING_CRT_RANGE")

    row = m5.iloc[-1]
    high = _float(row.get("high"))
    low = _float(row.get("low"))
    close = _float(row.get("close"))
    if None in {high, low, close}:
        return _result(symbol, "WAIT", 0, reason="MISSING_OHLC")
    mid = (crt_high + crt_low) / 2.0
    rng = crt_high - crt_low
    price_zone = "PREMIUM" if close > mid else "DISCOUNT" if close < mid else "MID"
    top_extreme = close >= crt_high - 0.15 * rng
    bottom_extreme = close <= crt_low + 0.15 * rng
    sweep_high = high > crt_high and close < crt_high
    sweep_low = low < crt_low and close > crt_low
    turtle = bool(context.get("turtle_soup_ote"))
    confirmation = bool(context.get("smc_m15_confirmation") or context.get("smc_m5_confirmation") or context.get("m5_cisd"))
    rr = _rr_from_context_or_range(context, close, rng)
    risk_safety = _risk_safety_ok(context)

    buy_score = _score(price_zone == "DISCOUNT" or bottom_extreme, sweep_low or turtle, context, confirmation, rr, risk_safety)
    sell_score = _score(price_zone == "PREMIUM" or top_extreme, sweep_high or turtle, context, confirmation, rr, risk_safety)
    if buy_score >= sell_score:
        signal, score, bias = "BUY", buy_score, "BUY"
        sl = crt_low - 0.05 * rng
        tp = close + max(abs(close - sl) * 1.8, rng * 0.3)
        valid = (price_zone == "DISCOUNT" or bottom_extreme) and (sweep_low or turtle)
    else:
        signal, score, bias = "SELL", sell_score, "SELL"
        sl = crt_high + 0.05 * rng
        tp = close - max(abs(sl - close) * 1.8, rng * 0.3)
        valid = (price_zone == "PREMIUM" or top_extreme) and (sweep_high or turtle)
    if not valid or not confirmation or rr < settings.new_strategies_require_rr_min or score < settings.new_strategies_min_score:
        signal = "WAIT"
    return _result(
        symbol,
        signal,
        score,
        entry=close if signal in {"BUY", "SELL"} else None,
        sl=sl if signal in {"BUY", "SELL"} else None,
        tp=tp if signal in {"BUY", "SELL"} else None,
        reason="CRT_TBS_REVERSAL_ALIGNED" if signal in {"BUY", "SELL"} else "CRT_TBS_CONDITIONS_NOT_MET",
        extra={
            "crt_tbs_score": score,
            "crt_tbs_bias": bias,
            "crt_high": round(crt_high, 8),
            "crt_low": round(crt_low, 8),
            "crt_mid_50": round(mid, 8),
            "crt_price_zone": price_zone,
            "crt_tbs_reason": "CRT_TBS_REVERSAL_ALIGNED" if signal in {"BUY", "SELL"} else "CRT_TBS_CONDITIONS_NOT_MET",
        },
    )


def _range(frames: dict, context: dict) -> tuple[float | None, float | None]:
    d1 = _frame(frames, "D1")
    if d1 is not None and len(d1) >= 2:
        row = d1.iloc[-2]
        return _float(row.get("high")), _float(row.get("low"))
    high = _float(context.get("h4_last_swing_high") or context.get("h4_recent_resistance"))
    low = _float(context.get("h4_last_swing_low") or context.get("h4_recent_support"))
    h4 = _frame(frames, "H4")
    if (high is None or low is None) and h4 is not None and len(h4) >= 8:
        high = high if high is not None else _float(h4["high"].tail(12).max())
        low = low if low is not None else _float(h4["low"].tail(12).min())
    return high, low


def _score(zone_ok: bool, sweep: bool, context: dict, confirmation: bool, rr: float, risk_safety: bool) -> int:
    score = 0
    if zone_ok:
        score += 20
    if sweep:
        score += 25
    if context.get("smc_h4_key_level_nearby") or str(context.get("h4_zone") or "").upper() in {"SUPPORT", "RESISTANCE", "SUPPLY", "DEMAND"}:
        score += 15
    if confirmation:
        score += 20
    if rr >= 1.5:
        score += 10
    if risk_safety:
        score += 10
    return score


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
        "crt_tbs_score": score,
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


def _risk_safety_ok(context: dict) -> bool:
    return context.get("safety_guard_status") in {None, "PASS"} and context.get("risk_diag_status") in {None, "OK"}


def _rr_from_context_or_range(context: dict, entry: float, rng: float) -> float:
    rr = _float(context.get("risk_reward") or context.get("reward_risk"))
    if rr is not None:
        return rr
    return 1.8 if entry and rng > 0 else 0.0


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
