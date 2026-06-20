from __future__ import annotations

import pandas as pd

from app.utils.indicators import enrich_indicators


def evaluate(symbol: str, df: pd.DataFrame) -> dict:
    name = "EMA_PULLBACK"
    enriched = enrich_indicators(df)
    if enriched.empty or len(enriched) < 60:
        return _result(symbol, name, "SKIP", 0.0, reason="Not enough candles", blocked_reason="INSUFFICIENT_DATA")

    row = enriched.iloc[-1]
    atr = _atr(row)
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    ema200 = float(row["ema200"])

    if close > ema20 > ema50 > ema200 and abs(close - ema20) <= atr * 1.2:
        return _result(symbol, name, "BUY", 0.74, close, close - atr * 1.5, close + atr * 2.5, "Bullish EMA pullback")
    if close < ema20 < ema50 < ema200 and abs(close - ema20) <= atr * 1.2:
        return _result(symbol, name, "SELL", 0.74, close, close + atr * 1.5, close - atr * 2.5, "Bearish EMA pullback")
    return _result(symbol, name, "WAIT", 0.45, reason="No EMA pullback setup")


def _atr(row: pd.Series) -> float:
    return max(float(row["atr"]) if pd.notna(row["atr"]) else 0.0, float(row["close"]) * 0.001)


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
