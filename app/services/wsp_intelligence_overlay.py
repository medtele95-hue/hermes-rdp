from __future__ import annotations

import math
from typing import Any

import pandas as pd


def evaluate_wsp_intelligence(
    symbol: str,
    timeframe: str,
    candles: Any,
    top_down_context: dict | None = None,
) -> dict:
    df = _closed_frame(candles)
    if len(df) < 20:
        return _empty(symbol, timeframe, "WSP_NOT_ENOUGH_CLOSED_CANDLES")

    close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    high = pd.to_numeric(df.get("high", close), errors="coerce").astype(float)
    low = pd.to_numeric(df.get("low", close), errors="coerce").astype(float)
    volume = _volume_series(df)
    if close.dropna().empty:
        return _empty(symbol, timeframe, "WSP_NO_CLOSED_CANDLES")

    atr = _atr(high, low, close, 14)
    adx = _adx(high, low, close, 14)
    rsi = _rsi(close, 14)
    trend_ma = float(close.tail(50).mean() if len(close) >= 50 else close.mean())
    last = float(close.iloc[-1])
    z_score = _z_score(close.tail(20))
    market_state = _market_state(last, trend_ma, adx, rsi)
    htf_alignment = _htf_alignment(market_state, top_down_context or {})
    pressure_pct, pressure_label = _volume_pressure(close, volume)
    whale_activity = _whale_activity(volume)
    trap_check = _trap_check(market_state, z_score, rsi)
    mm_sentiment = _mm_sentiment(market_state, trap_check, pressure_label)
    sm_action = _sm_action(mm_sentiment, pressure_label, trap_check)
    crash_warning = bool(z_score <= -2.0 and market_state == "BEARISH")
    rocket_warning = bool(z_score >= 2.0 and market_state == "BULLISH")
    exhaustion_warning = bool(rsi >= 75 or rsi <= 25 or abs(z_score) >= 2.5)
    risk_score = _risk_score(adx, rsi, z_score, whale_activity, trap_check, htf_alignment)
    safety_guard_visual = "DANGER" if risk_score >= 8 else ("CAUTION" if risk_score >= 5 else "SECURE")
    labels = _labels(market_state, pressure_label, trap_check, safety_guard_visual, crash_warning, rocket_warning, exhaustion_warning)
    return {
        "wsp_enabled": True,
        "role": "OBSERVER_ONLY",
        "visual_role": "VISUAL_CONFIRMATION",
        "symbol": symbol,
        "timeframe": timeframe,
        "atr": _round(atr),
        "adx": _round(adx),
        "rsi": _round(rsi),
        "trend_ma": _round(trend_ma),
        "market_state": market_state,
        "htf_trend_alignment": htf_alignment,
        "volume_pressure_pct": _round(pressure_pct),
        "volume_pressure_label": pressure_label,
        "risk_score": risk_score,
        "whale_activity": whale_activity,
        "mm_sentiment": mm_sentiment,
        "z_score": _round(z_score),
        "trap_check": trap_check,
        "sm_action": sm_action,
        "crash_warning": crash_warning,
        "rocket_warning": rocket_warning,
        "exhaustion_warning": exhaustion_warning,
        "safety_guard_visual": safety_guard_visual,
        "labels": labels,
        "reason": "WSP_OBSERVER_READY",
    }


def _empty(symbol: str, timeframe: str, reason: str) -> dict:
    return {
        "wsp_enabled": True,
        "role": "OBSERVER_ONLY",
        "visual_role": "VISUAL_CONFIRMATION",
        "symbol": symbol,
        "timeframe": timeframe,
        "market_state": "RANGE",
        "htf_trend_alignment": "UNKNOWN",
        "volume_pressure_pct": 0.0,
        "volume_pressure_label": "BULLISH",
        "risk_score": 5,
        "whale_activity": "STABLE",
        "mm_sentiment": "NEUTRAL",
        "z_score": 0.0,
        "trap_check": "VALID",
        "sm_action": "RETAIL_FLOW",
        "crash_warning": False,
        "rocket_warning": False,
        "exhaustion_warning": False,
        "safety_guard_visual": "CAUTION",
        "labels": [],
        "reason": reason,
    }


def _closed_frame(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    df = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if df.empty or "close" not in df:
        return pd.DataFrame()
    return df.iloc[:-1].copy() if len(df) > 1 else df.copy()


def _volume_series(df: pd.DataFrame) -> pd.Series:
    for name in ("volume", "tick_volume", "real_volume"):
        if name in df:
            return pd.to_numeric(df[name], errors="coerce").fillna(0).astype(float)
    return pd.Series([0.0] * len(df), index=df.index)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    value = tr.tail(period).mean()
    return float(value) if math.isfinite(float(value)) else 0.0


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().replace(0, math.nan)
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, math.nan)) * 100
    value = dx.tail(period).mean()
    return float(value) if value is not None and math.isfinite(float(value)) else 0.0


def _rsi(close: pd.Series, period: int) -> float:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).tail(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).tail(period).mean()
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return float(100 - (100 / (1 + gain / loss)))


def _z_score(values: pd.Series) -> float:
    if len(values) < 2:
        return 0.0
    stdev = float(values.std(ddof=0))
    if stdev <= 0:
        return 0.0
    return float((values.iloc[-1] - values.mean()) / stdev)


def _market_state(last: float, trend_ma: float, adx: float, rsi: float) -> str:
    if adx < 18:
        return "RANGE"
    if last > trend_ma and rsi >= 52:
        return "BULLISH"
    if last < trend_ma and rsi <= 48:
        return "BEARISH"
    return "RANGE"


def _htf_alignment(market_state: str, top_down: dict) -> str:
    h4 = str(top_down.get("h4_bias") or "").upper()
    h1 = str(top_down.get("h1_bias") or "").upper()
    decision = str(top_down.get("decision") or "").upper()
    if not any((h4, h1, decision)):
        return "UNKNOWN"
    bullish = market_state == "BULLISH" and ("BULLISH" in {h4, h1} or decision == "ALLOW_DEMO")
    bearish = market_state == "BEARISH" and ("BEARISH" in {h4, h1} or decision == "ALLOW_DEMO")
    if bullish or bearish:
        return "ALIGNED"
    if market_state in {"BULLISH", "BEARISH"} and ("BULLISH" in {h4, h1} or "BEARISH" in {h4, h1}):
        return "CONFLICT"
    return "UNKNOWN"


def _volume_pressure(close: pd.Series, volume: pd.Series) -> tuple[float, str]:
    delta = close.diff().fillna(0)
    buy_volume = float(volume.where(delta >= 0, 0.0).tail(20).sum())
    sell_volume = float(volume.where(delta < 0, 0.0).tail(20).sum())
    total = buy_volume + sell_volume
    if total <= 0:
        return 0.0, "BULLISH"
    pct = ((buy_volume - sell_volume) / total) * 100
    return pct, "BULLISH" if pct >= 0 else "BEARISH"


def _whale_activity(volume: pd.Series) -> str:
    if len(volume) < 20:
        return "STABLE"
    recent = float(volume.tail(5).mean())
    baseline = float(volume.tail(50).mean() if len(volume) >= 50 else volume.mean())
    if baseline <= 0:
        return "STABLE"
    ratio = recent / baseline
    if ratio >= 2.0:
        return "AGGRESSIVE"
    if ratio >= 1.25:
        return "RISING"
    return "STABLE"


def _trap_check(market_state: str, z_score: float, rsi: float) -> str:
    if market_state == "BULLISH" and z_score < -1.5 and rsi < 45:
        return "BULL_TRAP"
    if market_state == "BEARISH" and z_score > 1.5 and rsi > 55:
        return "BEAR_TRAP"
    return "VALID"


def _mm_sentiment(market_state: str, trap_check: str, pressure_label: str) -> str:
    if trap_check in {"BULL_TRAP", "BEAR_TRAP"}:
        return "HUNTING"
    if market_state == "BULLISH" and pressure_label == "BULLISH":
        return "ACCUMULATION"
    if market_state == "BEARISH" and pressure_label == "BEARISH":
        return "DISTRIBUTION"
    return "NEUTRAL"


def _sm_action(mm_sentiment: str, pressure_label: str, trap_check: str) -> str:
    if trap_check in {"BULL_TRAP", "BEAR_TRAP"} or mm_sentiment == "HUNTING":
        return "INST_EXIT"
    if mm_sentiment in {"ACCUMULATION", "DISTRIBUTION"} and pressure_label in {"BULLISH", "BEARISH"}:
        return "INST_LOGIN"
    return "RETAIL_FLOW"


def _risk_score(adx: float, rsi: float, z_score: float, whale_activity: str, trap_check: str, htf_alignment: str) -> int:
    score = 3
    if adx < 15:
        score += 1
    if rsi >= 75 or rsi <= 25:
        score += 2
    if abs(z_score) >= 2.0:
        score += 2
    if whale_activity == "AGGRESSIVE":
        score += 2
    elif whale_activity == "RISING":
        score += 1
    if trap_check in {"BULL_TRAP", "BEAR_TRAP"}:
        score += 2
    if htf_alignment == "CONFLICT":
        score += 1
    return max(1, min(10, score))


def _labels(market_state: str, pressure_label: str, trap_check: str, safety: str, crash: bool, rocket: bool, exhaustion: bool) -> list[str]:
    labels = [f"WSP_{market_state}", f"VOL_{pressure_label}", f"WSP_{safety}"]
    if trap_check != "VALID":
        labels.append(trap_check)
    if crash:
        labels.append("CRASH_WARNING")
    if rocket:
        labels.append("ROCKET_WARNING")
    if exhaustion:
        labels.append("EXHAUSTION_WARNING")
    return labels


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6) if math.isfinite(float(value)) else None
