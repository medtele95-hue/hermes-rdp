from __future__ import annotations

import pandas as pd

from app.utils.indicators import enrich_indicators


def evaluate(symbol: str, df: pd.DataFrame) -> dict:
    name = "BREAKOUT_RETEST"
    enriched = enrich_indicators(df)
    if enriched.empty or len(enriched) < 40:
        return _result(symbol, name, "SKIP", 0.0, reason="Not enough candles", blocked_reason="INSUFFICIENT_DATA")

    row = enriched.iloc[-1]
    prev = enriched.iloc[-21:-1]
    atr = max(float(row["atr"]) if pd.notna(row["atr"]) else 0.0, float(row["close"]) * 0.001)
    high = float(prev["high"].max())
    low = float(prev["low"].min())
    close = float(row["close"])

    # COEUR_V2 chantier 2 (2026-07-08): ATR multipliers rescaled by 1/1.025855
    # (measured Wilder/SMA ratio, GOLD#+BTCUSD# M5 30j blended) to preserve the
    # same effective distances now that `atr` means Wilder RMA, not SMA. Wick
    # filter and SL buffer were implicit *1.0, TP was *2.0 — see
    # COEUR_V2_REPORT.md chantier 2 for the measurement.
    if close > high and float(row["lower_wick"]) <= atr * 0.9748:
        return _result(symbol, name, "BUY", 0.68, close, high - atr * 0.9748, close + atr * 1.9497, "Breakout above 20-candle range")
    if close < low and float(row["upper_wick"]) <= atr * 0.9748:
        return _result(symbol, name, "SELL", 0.68, close, low + atr * 0.9748, close - atr * 1.9497, "Breakdown below 20-candle range")
    return _result(symbol, name, "WAIT", 0.42, reason="No breakout retest")


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
