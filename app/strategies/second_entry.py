from __future__ import annotations

import pandas as pd

from app.utils.indicators import enrich_indicators


def evaluate(symbol: str, df: pd.DataFrame) -> dict:
    name = "SECOND_ENTRY"
    enriched = enrich_indicators(df)
    if enriched.empty or len(enriched) < 30:
        return _result(symbol, name, "SKIP", 0.0, reason="Not enough candles", blocked_reason="INSUFFICIENT_DATA")

    tail = enriched.tail(6)
    row = tail.iloc[-1]
    atr = max(float(row["atr"]) if pd.notna(row["atr"]) else 0.0, float(row["close"]) * 0.001)
    close = float(row["close"])
    higher_lows = tail["low"].iloc[-3] > tail["low"].iloc[-5] and tail["low"].iloc[-1] > tail["low"].iloc[-3]
    lower_highs = tail["high"].iloc[-3] < tail["high"].iloc[-5] and tail["high"].iloc[-1] < tail["high"].iloc[-3]

    if close > float(row["ema20"]) and higher_lows:
        return _result(symbol, name, "BUY", 0.64, close, close - atr * 1.3, close + atr * 2.0, "Second-entry long price action")
    if close < float(row["ema20"]) and lower_highs:
        return _result(symbol, name, "SELL", 0.64, close, close + atr * 1.3, close - atr * 2.0, "Second-entry short price action")
    return _result(symbol, name, "WAIT", 0.40, reason="No second-entry pattern")


def _result(symbol: str, strategy: str, signal: str, confidence: float, entry=None, sl=None, tp=None, reason="", blocked_reason=None) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "M5",
        "strategy": strategy,
        "signal": signal,
        "confidence": confidence,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "reason": reason,
        "blocked_reason": blocked_reason,
    }
