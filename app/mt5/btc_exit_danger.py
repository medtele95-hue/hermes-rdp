"""BTC position exit danger detector — pure signal evaluation.

Evaluates market context signals to determine if a profitable BTC position
is at risk of turning negative. Requires >= 2 independent signals for danger=True.

No MT5 calls here. Order execution stays exclusively in app/mt5/demo_router.py.
"""
from __future__ import annotations

import math
from typing import Any


_BUY = "BUY"
_SELL = "SELL"
DEFAULT_MIN_SIGNALS = 2

# CVD threshold to consider "strongly trending"
_CVD_STRONG_NEGATIVE = -0.3
_CVD_STRONG_POSITIVE = 0.3

# Delta proxy threshold for "strongly directional"
_DELTA_STRONG_THRESHOLD = 500.0

# Spread utilisation above this fraction of max_spread triggers SPREAD_NEAR_MAX
_SPREAD_NEAR_MAX_RATIO = 0.85

# Price must be within this fraction of POC to count as "POC rejection"
_POC_REJECTION_ZONE = 0.002


def evaluate_btc_exit_danger(
    pos: Any,
    market_context: dict | None,
    min_signals: int = DEFAULT_MIN_SIGNALS,
) -> dict:
    """Return a danger assessment for an open BTC position.

    Returns a dict with keys:
        danger       — bool: True if >= min_signals fired
        signals      — list[str]: individual signal tokens that fired
        signal_count — int
        direction    — "BUY"|"SELL"|"UNKNOWN"
        reason       — str (only when danger=False and no context provided)
    """
    if not market_context:
        return {
            "danger": False,
            "signals": [],
            "signal_count": 0,
            "direction": "UNKNOWN",
            "reason": "NO_MARKET_CONTEXT",
        }

    # Determine position direction from pos.type: 0=BUY, 1=SELL
    _raw_type = getattr(pos, "type", None)
    pos_type = int(_raw_type) if _raw_type is not None else -1
    if pos_type == 0:
        direction = _BUY
    elif pos_type == 1:
        direction = _SELL
    else:
        return {
            "danger": False,
            "signals": [],
            "signal_count": 0,
            "direction": "UNKNOWN",
            "reason": "UNKNOWN_POSITION_TYPE",
        }

    signals = _collect_signals(pos, direction, market_context)
    signal_count = len(signals)
    return {
        "danger": signal_count >= min_signals,
        "signals": signals,
        "signal_count": signal_count,
        "direction": direction,
        "min_signals_required": min_signals,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_signals(pos: Any, direction: str, ctx: dict) -> list[str]:
    price   = _f(ctx.get("price") or ctx.get("last_price") or ctx.get("bid"))
    vwap    = _f(ctx.get("vwap"))
    poc     = _f(ctx.get("poc"))
    vah     = _f(ctx.get("vah"))
    val     = _f(ctx.get("val"))
    cvd     = _f(ctx.get("cvd_slope"))
    delta   = _f(ctx.get("delta_proxy") or ctx.get("latest_delta") or ctx.get("delta"))
    bid     = _f(ctx.get("bid"))
    ask     = _f(ctx.get("ask"))
    spread  = _f(ctx.get("spread"))
    max_sp  = _f(ctx.get("max_spread"))
    m1_mom  = _label(ctx.get("m1_momentum") or ctx.get("m1_signal"))
    m5_mom  = _label(ctx.get("m5_momentum") or ctx.get("m5_signal"))
    m1_close  = _f(ctx.get("m1_close"))
    m5_close  = _f(ctx.get("m5_close"))
    m1_ema_f  = _f(ctx.get("m1_ema_fast") or ctx.get("m1_ema20"))
    m1_ema_s  = _f(ctx.get("m1_ema_slow") or ctx.get("m1_ema50"))
    m5_ema_f  = _f(ctx.get("m5_ema_fast") or ctx.get("m5_ema20"))
    m5_ema_s  = _f(ctx.get("m5_ema_slow") or ctx.get("m5_ema50"))
    of_signal = _label(ctx.get("order_flow_signal") or ctx.get("signal"))
    entry     = _f(getattr(pos, "price_open", None))
    profit    = _f(getattr(pos, "profit", None))

    signals: list[str] = []

    if direction == _BUY:
        # 1. Price falls below VWAP or POC
        if price is not None and vwap is not None and price < vwap:
            signals.append("PRICE_BELOW_VWAP")
        if price is not None and poc is not None and price < poc:
            signals.append("PRICE_BELOW_POC")

        # 2. M1 momentum bearish
        if m1_mom in {"BEARISH", "SELL", "DOWN"}:
            signals.append("M1_BEARISH_MOMENTUM")

        # 3. M5 bearish momentum or close below fast EMA
        if m5_mom in {"BEARISH", "SELL", "DOWN"}:
            signals.append("M5_BEARISH_MOMENTUM")
        if m5_close is not None and m5_ema_f is not None and m5_close < m5_ema_f:
            signals.append("M5_CLOSE_BELOW_EMA")

        # 4. CVD slope strongly negative or delta strongly negative
        if cvd is not None and cvd < _CVD_STRONG_NEGATIVE:
            signals.append("CVD_SLOPE_NEGATIVE")
        if delta is not None and delta < -_DELTA_STRONG_THRESHOLD:
            signals.append("DELTA_STRONGLY_NEGATIVE")

        # 5. Bid retreating toward entry after being in profit
        if bid is not None and entry is not None and profit is not None and profit > 0.05:
            if (bid - entry) < 0.5 * profit:
                signals.append("PRICE_RETREATING_TO_ENTRY")

        # 6. Order flow no longer supports BUY
        if of_signal in {"SELL", "BEARISH", "WAIT"}:
            signals.append("ORDER_FLOW_NOT_BUY")

        # 7. Spread near max
        if spread is not None and max_sp is not None and max_sp > 0 and spread > max_sp * _SPREAD_NEAR_MAX_RATIO:
            signals.append("SPREAD_NEAR_MAX")

        # 8. Fast EMA crosses below slow EMA
        if m1_ema_f is not None and m1_ema_s is not None and m1_ema_f < m1_ema_s:
            signals.append("M1_EMA_BEARISH_CROSS")
        if m5_ema_f is not None and m5_ema_s is not None and m5_ema_f < m5_ema_s:
            signals.append("M5_EMA_BEARISH_CROSS")

        # 9. Price near POC and bearish M1
        if price is not None and poc is not None and m1_mom in {"BEARISH", "SELL", "DOWN"}:
            if abs(price - poc) / poc < _POC_REJECTION_ZONE:
                signals.append("POC_REJECTION_BEARISH")

    else:  # SELL
        # 1. Price rises above VWAP or POC
        if price is not None and vwap is not None and price > vwap:
            signals.append("PRICE_ABOVE_VWAP")
        if price is not None and poc is not None and price > poc:
            signals.append("PRICE_ABOVE_POC")

        # 2. M1 momentum bullish
        if m1_mom in {"BULLISH", "BUY", "UP"}:
            signals.append("M1_BULLISH_MOMENTUM")

        # 3. M5 bullish momentum or close above fast EMA
        if m5_mom in {"BULLISH", "BUY", "UP"}:
            signals.append("M5_BULLISH_MOMENTUM")
        if m5_close is not None and m5_ema_f is not None and m5_close > m5_ema_f:
            signals.append("M5_CLOSE_ABOVE_EMA")

        # 4. CVD slope strongly positive or delta strongly positive
        if cvd is not None and cvd > _CVD_STRONG_POSITIVE:
            signals.append("CVD_SLOPE_POSITIVE")
        if delta is not None and delta > _DELTA_STRONG_THRESHOLD:
            signals.append("DELTA_STRONGLY_POSITIVE")

        # 5. Ask retreating toward entry after being in profit
        if ask is not None and entry is not None and profit is not None and profit > 0.05:
            if (entry - ask) < 0.5 * profit:
                signals.append("PRICE_RETREATING_TO_ENTRY")

        # 6. Order flow no longer supports SELL
        if of_signal in {"BUY", "BULLISH", "WAIT"}:
            signals.append("ORDER_FLOW_NOT_SELL")

        # 7. Spread near max
        if spread is not None and max_sp is not None and max_sp > 0 and spread > max_sp * _SPREAD_NEAR_MAX_RATIO:
            signals.append("SPREAD_NEAR_MAX")

        # 8. Fast EMA crosses above slow EMA
        if m1_ema_f is not None and m1_ema_s is not None and m1_ema_f > m1_ema_s:
            signals.append("M1_EMA_BULLISH_CROSS")
        if m5_ema_f is not None and m5_ema_s is not None and m5_ema_f > m5_ema_s:
            signals.append("M5_EMA_BULLISH_CROSS")

        # 9. Price near POC and bullish M1
        if price is not None and poc is not None and m1_mom in {"BULLISH", "BUY", "UP"}:
            if abs(price - poc) / poc < _POC_REJECTION_ZONE:
                signals.append("POC_REJECTION_BULLISH")

    return list(dict.fromkeys(signals))


def _f(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _label(value: object) -> str:
    return str(value or "").upper().strip()
