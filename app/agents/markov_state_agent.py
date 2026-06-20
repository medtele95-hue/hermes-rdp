from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, Tuple

import pandas as pd

from app.utils.indicators import enrich_indicators
from app.utils.sessions import trading_session


class MarkovStateAgent:
    def __init__(self) -> None:
        self.previous_state: Dict[str, str] = {}
        self.transitions: Dict[str, Counter] = defaultdict(Counter)

    def analyze(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Tuple[dict, dict]:
        enriched = enrich_indicators(df)
        state = self.classify(enriched)
        previous = self.previous_state.get(symbol)
        if previous:
            self.transitions[previous][state] += 1
        self.previous_state[symbol] = state

        next_state, probability, transition_count = self.predict_next(state)
        persistence = self._persistence(state)
        confidence = min(0.99, max(0.10, (probability * 0.7) + (persistence * 0.3)))
        signal = "ENTER" if confidence >= 0.70 and state not in {"LOW_LIQUIDITY", "REVERSAL_RISK"} else "WAIT"
        if state in {"LOW_LIQUIDITY", "HIGH_VOLATILITY"} and confidence < 0.80:
            signal = "SKIP"

        now = datetime.now(timezone.utc).isoformat()
        latest = enriched.iloc[-1]
        market_state = {
            "symbol": symbol,
            "timeframe": timeframe,
            "state": state,
            "session": trading_session(latest["candle_time"].to_pydatetime()),
            "ema20": _clean(latest.get("ema20")),
            "ema50": _clean(latest.get("ema50")),
            "ema200": _clean(latest.get("ema200")),
            "atr": _clean(latest.get("atr")),
            "rsi": _clean(latest.get("rsi")),
            "volatility": _clean(latest.get("volatility")),
            "created_at": now,
        }
        prediction = {
            "symbol": symbol,
            "timeframe": timeframe,
            "current_state": state,
            "predicted_next_state": next_state,
            "predicted_state": next_state or state or "UNKNOWN",
            "probability": probability,
            "persistence": persistence,
            "transition_count": transition_count,
            "confidence": confidence,
            "signal": signal,
            "created_at": now,
        }
        return market_state, prediction

    def classify(self, df: pd.DataFrame) -> str:
        if df.empty or len(df) < 50:
            return "LOW_LIQUIDITY"
        row = df.iloc[-1]
        close = float(row["close"])
        ema20 = float(row["ema20"])
        ema50 = float(row["ema50"])
        ema200 = float(row["ema200"]) if pd.notna(row["ema200"]) else ema50
        atr = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        volatility = float(row["volatility"]) if pd.notna(row["volatility"]) else 0.0
        rsi = float(row["rsi"]) if pd.notna(row["rsi"]) else 50.0
        recent = df.tail(20)
        recent_range = float(recent["high"].max() - recent["low"].min())

        if volatility > 0.015:
            return "HIGH_VOLATILITY"
        if recent["tick_volume"].tail(10).mean() <= 0:
            return "LOW_LIQUIDITY"
        if atr > 0 and recent_range > atr * 4 and abs(close - recent["high"].max()) < atr:
            return "BREAKOUT"
        if atr > 0 and recent_range < atr * 2:
            return "CONSOLIDATION"
        if rsi > 74 or rsi < 26:
            return "REVERSAL_RISK"
        if close > ema20 > ema50 > ema200:
            return "STRONG_UPTREND"
        if close > ema20 > ema50:
            return "WEAK_UPTREND"
        if close < ema20 < ema50 < ema200:
            return "STRONG_DOWNTREND"
        if close < ema20 < ema50:
            return "WEAK_DOWNTREND"
        if close > ema50:
            return "UP"
        if close < ema50:
            return "DOWN"
        return "RANGE"

    def predict_next(self, state: str) -> Tuple[str, float, int]:
        counts = self.transitions.get(state, Counter())
        total = sum(counts.values())
        if total == 0:
            return state, 0.55, 0
        next_state, count = counts.most_common(1)[0]
        return next_state, count / total, total

    def _persistence(self, state: str) -> float:
        counts = self.transitions.get(state, Counter())
        total = sum(counts.values())
        if total == 0:
            return 0.50
        return counts[state] / total


def _clean(value: object) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
