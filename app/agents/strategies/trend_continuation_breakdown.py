from __future__ import annotations

import math

import pandas as pd

from app.config import Settings


STRATEGY = "TREND_CONTINUATION_BREAKDOWN"


def evaluate(symbol: str, frames: dict, context: dict, settings) -> dict:
    settings = settings or Settings()
    m5 = _frame(frames, "M5")
    if m5 is None or len(m5) < 30:
        return _result(symbol, "WAIT", 0, reason="MISSING_M5_DATA")

    market_state = str(context.get("market_state") or "").upper()
    h1_bias = _trend(_frame(frames, "H1"))
    h4_bias = _trend(_frame(frames, "H4"))
    m5_structure = _structure(m5)
    m1 = _frame(frames, "M1")
    m15 = _frame(frames, "M15")

    sell = _direction_payload("SELL", symbol, market_state, h1_bias, h4_bias, m5, m5_structure, m1, m15)
    buy = _direction_payload("BUY", symbol, market_state, h1_bias, h4_bias, m5, m5_structure, m1, m15)
    selected = sell if sell["trend_continuation_score"] >= buy["trend_continuation_score"] else buy
    if selected["trend_continuation_score"] < 60 and selected.get("resolved_direction") not in {"BUY", "SELL"}:
        selected["signal"] = "WAIT"
        selected["entry"] = None
        selected["sl"] = None
        selected["tp"] = None
        selected["blocked_reason"] = selected["trend_continuation_reason"]
    return selected


def _direction_payload(
    direction: str,
    symbol: str,
    market_state: str,
    h1_bias: str,
    h4_bias: str,
    m5: pd.DataFrame,
    m5_structure: str,
    m1: pd.DataFrame | None,
    m15: pd.DataFrame | None,
) -> dict:
    close = _float(m5.iloc[-1].get("close")) or 0.0
    atr = max(_atr(m5), abs(close) * 0.001, 0.0001)
    support = _float(m5.iloc[:-1]["low"].tail(12).min())
    resistance = _float(m5.iloc[:-1]["high"].tail(12).max())
    lower_high = _float(m5["high"].tail(8).max())
    higher_low = _float(m5["low"].tail(8).min())

    if direction == "SELL":
        strong_trend = market_state == "STRONG_DOWNTREND"
        aligned = h1_bias == "BEARISH" or h4_bias == "BEARISH"
        structure_ok = m5_structure == "BEARISH"
        break_ok = support is not None and close < support
        retest = support is not None and _float(m5.iloc[-1].get("high")) is not None and abs(float(m5.iloc[-1].get("high")) - support) <= atr * 1.25
        momentum = break_ok and _body(m5.iloc[-1]) >= atr * 0.45
        m1_trigger = _m1_trigger(m1, "SELL", support)
        m1_ok = m1_trigger["m1_entry_confirmation"]
        m15_payload = _m15_confirmation(m15, "SELL", h1_bias, h4_bias)
        m15_ok = m15_payload["m15_confirmation"]
        entry = close
        sl = max(lower_high or close + atr, (support or close) + atr * 0.6)
        tp = entry - max(abs(sl - entry) * 2.0, atr * 2.0)
    else:
        strong_trend = market_state == "STRONG_UPTREND"
        aligned = h1_bias == "BULLISH" or h4_bias == "BULLISH"
        structure_ok = m5_structure == "BULLISH"
        break_ok = resistance is not None and close > resistance
        retest = resistance is not None and _float(m5.iloc[-1].get("low")) is not None and abs(float(m5.iloc[-1].get("low")) - resistance) <= atr * 1.25
        momentum = break_ok and _body(m5.iloc[-1]) >= atr * 0.45
        m1_trigger = _m1_trigger(m1, "BUY", resistance)
        m1_ok = m1_trigger["m1_entry_confirmation"]
        m15_payload = _m15_confirmation(m15, "BUY", h1_bias, h4_bias)
        m15_ok = m15_payload["m15_confirmation"]
        entry = close
        sl = min(higher_low or close - atr, (resistance or close) - atr * 0.6)
        tp = entry + max(abs(entry - sl) * 2.0, atr * 2.0)

    rr = _rr(entry, sl, tp, direction)
    score = 0
    if strong_trend:
        score += 20
    if aligned:
        score += 20
    if break_ok and structure_ok:
        score += 20
    if retest or momentum:
        score += 15
    if m1_ok:
        score += 15
    if m15_ok:
        score += 15
    score += 5
    if rr >= 1.5:
        score += 10
    score = min(100, score)
    direction_payload = _resolve_direction(direction, m1_trigger, m15_payload, h1_bias, h4_bias)
    signal = direction_payload["resolved_direction"] if direction_payload["resolved_direction"] in {"BUY", "SELL"} else "WAIT"
    missing = []
    if m1_ok and m15_ok and signal == "WAIT":
        missing.append("DIRECTION_RESOLVER_FAIL")
    if not m1_ok:
        missing.append("WAITING_FOR_M1_TRIGGER")
    if not m15_ok:
        missing.append("WAITING_FOR_M15_CONFIRMATION")
    if not retest and not momentum:
        missing.append("WAITING_FOR_RETEST")
    if rr < 1.5:
        missing.append("RR_TOO_LOW")
    return _result(
        symbol,
        signal,
        score,
        entry=entry if signal in {"BUY", "SELL"} else None,
        sl=sl if signal in {"BUY", "SELL"} else None,
        tp=tp if signal in {"BUY", "SELL"} else None,
        reason="TREND_CONTINUATION_ALIGNED" if signal in {"BUY", "SELL"} else ",".join(missing) or "TREND_CONTINUATION_NOT_READY",
        extra={
            "trend_continuation_score": score,
            "trend_continuation_direction": direction,
            "trend_continuation_reason": "TREND_CONTINUATION_ALIGNED" if signal in {"BUY", "SELL"} else ",".join(missing) or "TREND_CONTINUATION_NOT_READY",
            "support_break": bool(direction == "SELL" and break_ok),
            "resistance_break": bool(direction == "BUY" and break_ok),
            "trend_continuation_retest": bool(retest),
            "trend_continuation_momentum": bool(momentum),
            "m5_structure_status": m5_structure,
            "m15_confirmation": bool(m15_ok),
            **m15_payload,
            "m1_entry_confirmation": bool(m1_ok),
            **m1_trigger,
            **direction_payload,
            "risk_reward": rr,
            "reward_risk": rr,
            "h1_bias": h1_bias,
            "h4_bias": h4_bias,
        },
    )


def _result(symbol: str, signal: str, score: int, entry=None, sl=None, tp=None, reason="", extra: dict | None = None) -> dict:
    payload = {
        "symbol": symbol,
        "timeframe": "M5",
        "strategy": STRATEGY,
        "signal": signal,
        "confidence": round(min(0.95, 0.45 + score / 200.0), 4),
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "reason": reason,
        "blocked_reason": None if signal in {"BUY", "SELL"} else reason,
        "strategy_status": "ACTIVE",
        "trend_continuation_score": score,
    }
    if extra:
        payload.update(extra)
    return payload


def _structure(df: pd.DataFrame) -> str:
    tail = df.tail(8)
    highs = [_float(v) for v in tail["high"].tail(4)]
    lows = [_float(v) for v in tail["low"].tail(4)]
    if len(highs) < 4 or len(lows) < 4 or any(v is None for v in highs + lows):
        return "RANGE"
    if highs[-1] < highs[-2] < highs[-3] and lows[-1] < lows[-2] < lows[-3]:
        return "BEARISH"
    if highs[-1] > highs[-2] > highs[-3] and lows[-1] > lows[-2] > lows[-3]:
        return "BULLISH"
    return "RANGE"


def _m1_trigger(df: pd.DataFrame | None, direction: str, broken_level: float | None = None) -> dict:
    empty = {
        "m1_trigger_status": "FAIL",
        "m1_trigger_type": None,
        "m1_trigger_price": None,
        "m1_trigger_candle_time": None,
        "m1_trigger_reason": "MISSING_M1_DATA",
        "m1_entry_confirmation": False,
    }
    if df is None or len(df) < 6:
        return empty
    closed = df.tail(5)
    prior = df.iloc[: -len(closed)]
    if len(prior) < 3:
        prior = df.iloc[:-1].tail(5)
    minor_low = _float(prior["low"].tail(5).min()) if "low" in prior else None
    minor_high = _float(prior["high"].tail(5).max()) if "high" in prior else None

    for idx in range(len(closed) - 1, -1, -1):
        candle = closed.iloc[idx]
        before = df.iloc[: df.index.get_loc(candle.name)] if candle.name in df.index else df.iloc[: -1]
        trigger_type = _m1_trigger_type(candle, before.tail(5), direction, broken_level, minor_low, minor_high)
        if trigger_type:
            price = _float(candle.get("close"))
            return {
                "m1_trigger_status": "PASS",
                "m1_trigger_type": trigger_type,
                "m1_trigger_price": price,
                "m1_trigger_candle_time": _candle_time(candle),
            "m1_trigger_reason": trigger_type,
            "m1_trigger_direction": direction,
            "m1_entry_confirmation": True,
            }
    return {
        **empty,
        "m1_trigger_reason": "WAITING_FOR_M1_TRIGGER",
    }


def _m1_trigger_type(
    candle,
    prev: pd.DataFrame,
    direction: str,
    broken_level: float | None,
    minor_low: float | None,
    minor_high: float | None,
) -> str | None:
    close = _float(candle.get("close"))
    open_ = _float(candle.get("open"))
    high = _float(candle.get("high"))
    low = _float(candle.get("low"))
    if None in {close, open_, high, low}:
        return None
    body = abs(close - open_)
    candle_range = max(abs(high - low), 0.0000001)
    strong_body = body >= candle_range * 0.55
    prev_open = _float(prev.iloc[-1].get("open")) if len(prev) else None
    prev_close = _float(prev.iloc[-1].get("close")) if len(prev) else None
    prev_high = _float(prev["high"].tail(4).max()) if len(prev) else None
    prev_low = _float(prev["low"].tail(4).min()) if len(prev) else None
    consolidation_range = max(abs((prev_high or high) - (prev_low or low)), 0.0000001)
    avg_body = _avg_body(prev)

    if direction == "SELL":
        bearish = close < open_
        if bearish and minor_low is not None and close < minor_low:
            return "M1_BEARISH_CLOSE_BELOW_MINOR_LOW"
        if bearish and prev_high is not None and high <= prev_high and close <= (low + candle_range * 0.35):
            return "M1_LOWER_HIGH_REJECTION"
        if bearish and broken_level is not None and high >= broken_level and close < broken_level:
            return "M1_RETEST_CLOSE_BACK_BELOW_SUPPORT"
        if bearish and strong_body and (avg_body is None or body >= avg_body * 1.15):
            return "M1_BEARISH_STRONG_BODY"
        if bearish and prev_low is not None and consolidation_range <= max(avg_body or body, body) * 2.5 and close < prev_low:
            return "M1_BREAK_MICRO_CONSOLIDATION_LOW"
        if bearish and prev_open is not None and prev_close is not None and open_ >= prev_close and close <= prev_open:
            return "M1_BEARISH_ENGULFING"
        return None

    bullish = close > open_
    if bullish and minor_high is not None and close > minor_high:
        return "M1_BULLISH_CLOSE_ABOVE_MINOR_HIGH"
    if bullish and prev_low is not None and low >= prev_low and close >= (high - candle_range * 0.35):
        return "M1_HIGHER_LOW_REJECTION"
    if bullish and broken_level is not None and low <= broken_level and close > broken_level:
        return "M1_RETEST_CLOSE_BACK_ABOVE_RESISTANCE"
    if bullish and strong_body and (avg_body is None or body >= avg_body * 1.15):
        return "M1_BULLISH_STRONG_BODY"
    if bullish and prev_high is not None and consolidation_range <= max(avg_body or body, body) * 2.5 and close > prev_high:
        return "M1_BREAK_MICRO_CONSOLIDATION_HIGH"
    if bullish and prev_open is not None and prev_close is not None and open_ <= prev_close and close >= prev_open:
        return "M1_BULLISH_ENGULFING"
    return None


def _avg_body(df: pd.DataFrame) -> float | None:
    if df is None or len(df) == 0:
        return None
    bodies = [_body(row) for _, row in df.tail(5).iterrows()]
    return sum(bodies) / len(bodies) if bodies else None


def _candle_time(row) -> str | None:
    for key in ("time", "time_utc", "timestamp", "datetime"):
        value = row.get(key)
        if value is not None:
            return str(value)
    name = getattr(row, "name", None)
    return str(name) if name is not None else None


def _resolve_direction(direction: str, m1_trigger: dict, m15_payload: dict, h1_bias: str, h4_bias: str) -> dict:
    hard_block = direction == "SELL" and (h1_bias == "BULLISH" and h4_bias == "BULLISH")
    hard_block = hard_block or (direction == "BUY" and (h1_bias == "BEARISH" and h4_bias == "BEARISH"))
    m1_ok = m1_trigger.get("m1_trigger_status") == "PASS"
    m15_ok = m15_payload.get("m15_confirmation_status") == "PASS"
    m1_direction = str(m1_trigger.get("m1_trigger_direction") or "").upper()
    m15_direction = str(m15_payload.get("m15_confirmation_direction") or "").upper()
    if hard_block:
        return {
            "resolved_direction": "WAIT",
            "direction_source": "HTF_HARD_BLOCK",
            "direction_confidence": 0,
            "direction_block_reason": "HTF_OPPOSES_TREND_CONTINUATION",
        }
    if m1_ok and m15_ok and m1_direction == direction and m15_direction == direction:
        return {
            "resolved_direction": direction,
            "direction_source": "M1_M15_TREND_CONFIRMATION",
            "direction_confidence": 80,
            "direction_block_reason": None,
        }
    if m1_ok and m1_direction == direction:
        return {
            "resolved_direction": direction,
            "direction_source": "M1_TREND_TRIGGER",
            "direction_confidence": 70,
            "direction_block_reason": None,
        }
    return {
        "resolved_direction": "WAIT",
        "direction_source": "UNRESOLVED",
        "direction_confidence": 0,
        "direction_block_reason": "WAITING_FOR_TREND_AND_M1_ALIGNMENT",
    }


def _m15_confirmation(df: pd.DataFrame | None, direction: str, h1_bias: str = "UNKNOWN", h4_bias: str = "UNKNOWN") -> dict:
    empty = {
        "m15_confirmation_status": "FAIL",
        "m15_confirmation_type": None,
        "m15_confirmation_reason": "MISSING_M15_DATA",
        "m15_confirmation_price": None,
        "m15_confirmation_candle_time": None,
        "m15_confirmation": False,
    }
    if df is None or len(df) < 8:
        return empty
    trend = _trend(df)
    last = df.iloc[-1]
    prev = df.iloc[:-1].tail(6)
    close = _float(last.get("close"))
    open_ = _float(last.get("open"))
    high = _float(last.get("high"))
    low = _float(last.get("low"))
    if close is None:
        return {**empty, "m15_confirmation_reason": "MISSING_M15_CLOSE"}
    support = _float(prev["low"].min()) if len(prev) else None
    resistance = _float(prev["high"].max()) if len(prev) else None
    ema = _ema(df["close"].tail(12).tolist()) if "close" in df else None
    candle_range = max(abs((high or close) - (low or close)), 0.0000001)
    body = abs(close - (open_ if open_ is not None else close))
    displacement = body >= candle_range * 0.55
    liquidity_sweep_sell = high is not None and resistance is not None and high > resistance and close < (open_ if open_ is not None else close)
    liquidity_sweep_buy = low is not None and support is not None and low < support and close > (open_ if open_ is not None else close)
    trigger_type = None
    if direction == "SELL":
        if support is not None and close < support:
            trigger_type = "M15_CLOSE_BELOW_SUPPORT"
        elif trend == "BEARISH":
            trigger_type = "M15_BEARISH_STRUCTURE"
        elif liquidity_sweep_sell and displacement:
            trigger_type = "M15_LIQUIDITY_SWEEP_BEARISH_DISPLACEMENT"
        elif ema is not None and close < ema:
            trigger_type = "M15_CLOSE_BELOW_EMA_FILTER"
        elif h1_bias == "BEARISH" or h4_bias == "BEARISH":
            trigger_type = "M15_CONFIRMS_HTF_BEARISH_BIAS"
    else:
        if resistance is not None and close > resistance:
            trigger_type = "M15_CLOSE_ABOVE_RESISTANCE"
        elif trend == "BULLISH":
            trigger_type = "M15_BULLISH_STRUCTURE"
        elif liquidity_sweep_buy and displacement:
            trigger_type = "M15_LIQUIDITY_SWEEP_BULLISH_DISPLACEMENT"
        elif ema is not None and close > ema:
            trigger_type = "M15_CLOSE_ABOVE_EMA_FILTER"
        elif h1_bias == "BULLISH" or h4_bias == "BULLISH":
            trigger_type = "M15_CONFIRMS_HTF_BULLISH_BIAS"
    if trigger_type:
        return {
            "m15_confirmation_status": "PASS",
            "m15_confirmation_type": trigger_type,
            "m15_confirmation_reason": trigger_type,
            "m15_confirmation_price": close,
            "m15_confirmation_candle_time": _candle_time(last),
            "m15_confirmation_direction": direction,
            "m15_confirmation": True,
        }
    return {**empty, "m15_confirmation_reason": "WAITING_FOR_M15_CONFIRMATION", "m15_confirmation_price": close, "m15_confirmation_candle_time": _candle_time(last)}


def _ema(values: list[object]) -> float | None:
    nums = [_float(value) for value in values]
    nums = [value for value in nums if value is not None]
    if not nums:
        return None
    alpha = 2 / (len(nums) + 1)
    out = nums[0]
    for value in nums[1:]:
        out = value * alpha + out * (1 - alpha)
    return out


def _trend(df: pd.DataFrame | None) -> str:
    if df is None or len(df) < 6:
        return "UNKNOWN"
    tail = df.tail(6)
    highs = [_float(v) for v in tail["high"].tail(4)]
    lows = [_float(v) for v in tail["low"].tail(4)]
    closes = [_float(v) for v in tail["close"].tail(4)]
    if any(v is None for v in highs + lows + closes):
        return "UNKNOWN"
    if highs[-1] < highs[-2] < highs[-3] and lows[-1] < lows[-2] < lows[-3]:
        return "BEARISH"
    if highs[-1] > highs[-2] > highs[-3] and lows[-1] > lows[-2] > lows[-3]:
        return "BULLISH"
    if closes[-1] < min(closes[:-1]):
        return "BEARISH"
    if closes[-1] > max(closes[:-1]):
        return "BULLISH"
    return "RANGE"


def _atr(df: pd.DataFrame) -> float:
    tail = df.tail(14)
    ranges = [abs((_float(row.get("high")) or 0.0) - (_float(row.get("low")) or 0.0)) for _, row in tail.iterrows()]
    return sum(ranges) / len(ranges) if ranges else 0.0


def _body(row) -> float:
    return abs((_float(row.get("close")) or 0.0) - (_float(row.get("open")) or 0.0))


def _rr(entry: float, sl: float, tp: float, direction: str) -> float:
    risk = abs(entry - sl)
    reward = (tp - entry) if direction == "BUY" else (entry - tp)
    if risk <= 0:
        return 0.0
    return round(max(0.0, reward / risk), 6)


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
