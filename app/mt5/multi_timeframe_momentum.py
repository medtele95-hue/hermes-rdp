"""Non-repainting EMA/RSI momentum across configurable MT5 timeframes."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import MetaTrader5 as mt5

from app.config import (
    MULTI_TF_DEFAULT_TFS,
    MULTI_TF_EMA_LENGTH,
    MULTI_TF_MIN_CONFLUENCE,
    MULTI_TF_RSI_LENGTH,
)
from app.logger import log


DEFAULT_TFS = list(MULTI_TF_DEFAULT_TFS)
EMA_LENGTH = MULTI_TF_EMA_LENGTH
RSI_LENGTH = MULTI_TF_RSI_LENGTH
MIN_CONFLUENCE_DEFAULT = MULTI_TF_MIN_CONFLUENCE
RSI_MIDPOINT = 50.0
BIAS_BULL_SCORE = 2
BIAS_BEAR_SCORE = -2
COMPOSITE_MIN = -10
COMPOSITE_MAX = 10
ACTIVE_CONFLUENCE_BONUS_MAX = 15.0
INACTIVE_CONFLUENCE_FACTOR = 0.7

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class RepaintingDataError(ValueError):
    """Raised when MT5 bars cannot prove chronological confirmed-bar usage."""


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, None)


def _ema(values: Sequence[float], length: int) -> float:
    if length <= 0 or len(values) < length:
        raise ValueError("EMA_INSUFFICIENT_DATA")
    multiplier = 2.0 / (length + 1.0)
    value = sum(values[:length]) / length
    for close in values[length:]:
        value = (close - value) * multiplier + value
    return value


def _rsi(values: Sequence[float], length: int) -> float:
    if length <= 0 or len(values) < length + 1:
        raise ValueError("RSI_INSUFFICIENT_DATA")
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:length]) / length
    average_loss = sum(losses[:length]) / length
    for index in range(length, len(changes)):
        average_gain = ((average_gain * (length - 1)) + gains[index]) / length
        average_loss = ((average_loss * (length - 1)) + losses[index]) / length
    if average_loss == 0.0:
        return 100.0 if average_gain > 0.0 else RSI_MIDPOINT
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def fetch_tf_data(
    symbol: str,
    timeframe: str,
    ema_length: int = EMA_LENGTH,
    rsi_length: int = RSI_LENGTH,
    bars: int = 3,
) -> dict | None:
    """Fetch MT5 data and calculate indicators using only bars before the forming bar."""
    tf_name = str(timeframe or "").upper()
    if tf_name not in TIMEFRAME_MAP:
        raise ValueError(f"UNSUPPORTED_TIMEFRAME:{tf_name}")
    required_count = max(int(bars), int(ema_length) + 1, int(rsi_length) + 2)
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME_MAP[tf_name], 0, required_count)
    if rates is None or len(rates) < 2:
        return None
    rows = list(rates)
    timestamps = [int(_field(row, "time") or 0) for row in rows]
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise RepaintingDataError("MT5_RATES_NOT_STRICTLY_CHRONOLOGICAL")
    confirmed_rows = rows[:-1]
    closes = [float(_field(row, "close")) for row in confirmed_rows]
    if len(closes) < max(int(ema_length), int(rsi_length) + 1):
        return None
    confirmed = confirmed_rows[-1]
    return {
        "close_prev": closes[-1],
        "ema_prev": round(_ema(closes, int(ema_length)), 8),
        "rsi_prev": round(_rsi(closes, int(rsi_length)), 8),
        "timestamp": int(_field(confirmed, "time") or 0),
    }


def calculate_tf_bias(close_prev: float, ema_prev: float, rsi_prev: float) -> dict:
    ema_bullish = float(close_prev) > float(ema_prev)
    rsi_bullish = float(rsi_prev) > RSI_MIDPOINT
    if ema_bullish and rsi_bullish:
        bias, score = "BULL", BIAS_BULL_SCORE
    elif not ema_bullish and not rsi_bullish:
        bias, score = "BEAR", BIAS_BEAR_SCORE
    else:
        bias, score = "NEUT", 0
    return {
        "bias": bias,
        "score": score,
        "ema_dir": "UP" if ema_bullish else "DOWN",
        "rsi_val": float(rsi_prev),
    }


def momentum_score_to_confluence(
    composite_score: int,
    confluence_active: bool,
    confluence_strength: float,
) -> float:
    bounded_composite = max(COMPOSITE_MIN, min(COMPOSITE_MAX, int(composite_score)))
    bounded_strength = max(0.0, min(1.0, float(confluence_strength)))
    base = (bounded_composite - COMPOSITE_MIN) / (COMPOSITE_MAX - COMPOSITE_MIN) * 100.0
    if confluence_active:
        directional_bonus = bounded_strength * ACTIVE_CONFLUENCE_BONUS_MAX
        if bounded_composite > 0:
            base += directional_bonus
        elif bounded_composite < 0:
            base -= directional_bonus
    else:
        base *= INACTIVE_CONFLUENCE_FACTOR
    return max(0.0, min(100.0, base))


def _grade(score: float) -> str:
    if score >= 90.0:
        return "A+"
    if score >= 80.0:
        return "A"
    if score >= 70.0:
        return "B+"
    if score >= 60.0:
        return "B"
    if score >= 45.0:
        return "C"
    return "D"


def calculate_momentum_confluence(
    symbol: str,
    timeframes: list[str],
    min_confluence: int = MIN_CONFLUENCE_DEFAULT,
    ema_length: int = EMA_LENGTH,
    rsi_length: int = RSI_LENGTH,
) -> dict:
    if not timeframes:
        raise ValueError("MOMENTUM_TIMEFRAMES_EMPTY")
    if min_confluence <= 0 or min_confluence > len(timeframes):
        raise ValueError("MOMENTUM_MIN_CONFLUENCE_INVALID")
    tf_results: list[dict] = []
    for timeframe in timeframes:
        data = fetch_tf_data(symbol, timeframe, ema_length, rsi_length)
        if data is None:
            tf_results.append({"timeframe": timeframe, "bias": "NEUT", "score": 0,
                               "ema_dir": "UNKNOWN", "rsi_val": None, "status": "MISSING_DATA"})
            continue
        bias = calculate_tf_bias(data["close_prev"], data["ema_prev"], data["rsi_prev"])
        tf_results.append({"timeframe": timeframe, **bias, "timestamp": data["timestamp"], "status": "CONFIRMED"})

    composite_score = sum(int(result["score"]) for result in tf_results)
    bull_count = sum(result["bias"] == "BULL" for result in tf_results)
    bear_count = sum(result["bias"] == "BEAR" for result in tf_results)
    neut_count = len(tf_results) - bull_count - bear_count
    bull_confluence = bull_count >= min_confluence
    bear_confluence = bear_count >= min_confluence
    confluence_active = bull_confluence or bear_confluence
    direction = "BULL" if bull_confluence else "BEAR" if bear_confluence else None
    strength = max(bull_count, bear_count) / len(tf_results)
    momentum_score = momentum_score_to_confluence(composite_score, confluence_active, strength)
    result = {
        "symbol": symbol,
        "timeframes": tf_results,
        "composite_score": composite_score,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "neut_count": neut_count,
        "min_confluence": min_confluence,
        "confluence_active": confluence_active,
        "confluence_direction": direction,
        "confluence_strength": round(strength, 2),
        "momentum_score": round(momentum_score, 2),
        "grade": _grade(momentum_score),
    }
    log.info(
        "[MTF_MOMENTUM] symbol=%s score=%.2f confluence=%s/%s direction=%s",
        symbol, momentum_score, max(bull_count, bear_count), len(tf_results), direction or "NEUT",
    )
    return result
