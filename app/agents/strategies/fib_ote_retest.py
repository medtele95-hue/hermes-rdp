from __future__ import annotations

import math

import pandas as pd

from app.config import Settings


STRATEGY = "FIB_OTE_RETEST"


def evaluate(symbol: str, frames: dict, context: dict, settings) -> dict:
    settings = settings or Settings()
    if not settings.fib_ote_retest_enabled:
        return _result(symbol, "WAIT", 0, reason="FIB_OTE_RETEST_DISABLED")
    if _explicit_not_ok(context, settings):
        return _result(symbol, "WAIT", 0, reason="SAFETY_OR_RISK_NOT_OK")
    m5 = _frame(frames, "M5")
    if m5 is None or m5.empty:
        return _result(symbol, "WAIT", 0, reason="MISSING_M5_DATA")
    swing_high = _float(context.get("h4_last_swing_high") or context.get("h4_recent_resistance"))
    swing_low = _float(context.get("h4_last_swing_low") or context.get("h4_recent_support"))
    h4 = _frame(frames, "H4")
    if (swing_high is None or swing_low is None) and h4 is not None and len(h4) >= 8:
        swing_high = swing_high if swing_high is not None else _float(h4["high"].tail(20).max())
        swing_low = swing_low if swing_low is not None else _float(h4["low"].tail(20).min())
    if swing_high is None or swing_low is None or swing_high <= swing_low:
        return _result(symbol, "WAIT", 0, reason="MISSING_FIB_SWINGS")
    price = _float(m5.iloc[-1].get("close"))
    if price is None:
        return _result(symbol, "WAIT", 0, reason="MISSING_PRICE")

    impulse = _impulse(context)
    buy_low = swing_high - 0.786 * (swing_high - swing_low)
    buy_high = swing_high - 0.618 * (swing_high - swing_low)
    sell_low = swing_low + 0.618 * (swing_high - swing_low)
    sell_high = swing_low + 0.786 * (swing_high - swing_low)
    buy_ote = buy_low <= price <= buy_high
    sell_ote = sell_low <= price <= sell_high
    retest = _retest(context)
    confirmation = bool(context.get("smc_m15_confirmation") or context.get("smc_m5_confirmation") or context.get("m15_confirmation"))
    rr = _float(context.get("risk_reward") or context.get("reward_risk")) or 1.8
    risk_safety = context.get("safety_guard_status") in {None, "PASS"} and context.get("risk_diag_status") in {None, "OK"}

    buy_score = _score(impulse == "BUY", buy_ote, retest, confirmation, rr, risk_safety)
    sell_score = _score(impulse == "SELL", sell_ote, retest, confirmation, rr, risk_safety)
    if buy_score >= sell_score:
        signal, score, bias, in_ote = "BUY", buy_score, "BUY", buy_ote
        sl = swing_low
        tp = price + abs(price - sl) * 1.8
        fib_618, fib_786 = buy_high, buy_low
    else:
        signal, score, bias, in_ote = "SELL", sell_score, "SELL", sell_ote
        sl = swing_high
        tp = price - abs(sl - price) * 1.8
        fib_618, fib_786 = sell_low, sell_high
    if not in_ote or not retest or not confirmation or rr < settings.new_strategies_require_rr_min or score < settings.new_strategies_min_score:
        signal = "WAIT"
    return _result(
        symbol,
        signal,
        score,
        entry=price if signal in {"BUY", "SELL"} else None,
        sl=sl if signal in {"BUY", "SELL"} else None,
        tp=tp if signal in {"BUY", "SELL"} else None,
        reason="FIB_OTE_RETEST_ALIGNED" if signal in {"BUY", "SELL"} else "FIB_OTE_CONDITIONS_NOT_MET",
        extra={
            "fib_ote_score": score,
            "fib_ote_zone": "OTE" if in_ote else "OUTSIDE_OTE",
            "fib_ote_bias": bias,
            "fib_ote_reason": "FIB_OTE_RETEST_ALIGNED" if signal in {"BUY", "SELL"} else "FIB_OTE_CONDITIONS_NOT_MET",
            "fib_ote_618": round(fib_618, 8),
            "fib_ote_786": round(fib_786, 8),
        },
    )


def _impulse(context: dict) -> str:
    structure = str(context.get("smc_h1_break_structure") or "").upper()
    market_structure = str(context.get("market_structure") or "").upper()
    if structure in {"BOS_UP", "CHOCH_UP"} or market_structure == "UPTREND":
        return "BUY"
    if structure in {"BOS_DOWN", "CHOCH_DOWN"} or market_structure == "DOWNTREND":
        return "SELL"
    return "NONE"


def _retest(context: dict) -> bool:
    return (
        str(context.get("smc_h1_order_block") or "").upper() != "NONE"
        or str(context.get("smc_h1_fvg") or "").upper() != "NONE"
        or bool(context.get("breaker_fvg_ote"))
        or bool(context.get("ifvg_ote_sniper"))
        or bool(context.get("support_resistance_nearby"))
    )


def _score(impulse: bool, ote: bool, retest: bool, confirmation: bool, rr: float, risk_safety: bool) -> int:
    score = 0
    if impulse:
        score += 20
    if ote:
        score += 25
    if retest:
        score += 20
    if confirmation:
        score += 15
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
        "fib_ote_score": score,
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
