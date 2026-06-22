from __future__ import annotations

import pandas as pd
import pytest
from types import SimpleNamespace

from app.strategies.order_flow_execution_agent import evaluate


def _snap(direction: str = "SELL"):
    if direction == "SELL":
        return {
            "price": 101.0, "vwap": 99.0, "poc": 99.5,
            "vah": 101.2, "val": 97.0,
            "cvd_slope": -2.0, "delta_proxy": -600.0, "divergence": "bear",
            "created_at": None,
        }
    return {
        "price": 97.0, "vwap": 99.0, "poc": 99.5,
        "vah": 102.0, "val": 96.8,
        "cvd_slope": 2.0, "delta_proxy": 600.0, "divergence": "bull",
        "created_at": None,
    }


def _settings(**extra):
    ns = SimpleNamespace(
        order_flow_execution_enabled=True,
        order_flow_min_score=0,
        order_flow_min_rr=1.0,
        order_flow_cooldown_minutes=0,
        order_flow_allowed_symbols="",
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def _m1_pass(direction: str = "SELL"):
    """M1 data that passes the MSS gate for the given direction."""
    if direction == "SELL":
        # last close < recent_low → mss=True
        closes = [105.0, 104.0, 103.0, 102.0, 95.0]
        lows   = [104.0, 103.0, 102.0, 101.0, 94.0]
        highs  = [106.0, 105.0, 104.0, 103.0, 96.0]
    else:
        closes = [100.0, 101.0, 102.0, 103.0, 110.0]
        lows   = [ 99.0, 100.0, 101.0, 102.0, 109.0]
        highs  = [101.0, 102.0, 103.0, 104.0, 111.0]
    return pd.DataFrame({"close": closes, "low": lows, "high": highs})


# ── 1. Conflit → WAIT ───────────────────────────────────────────────────────

def test_bearish_bias_blocks_buy():
    frames = {"order_flow_snapshot": _snap("BUY"), "M1": _m1_pass("BUY")}
    result = evaluate("BTCUSD#", frames, context={"h1_bias": "BEARISH"}, settings=_settings())
    assert result["status"] == "WAIT"
    assert result["reason"] == "ORDER_FLOW_H1_BIAS_CONFLICT"


def test_bullish_bias_blocks_sell():
    frames = {"order_flow_snapshot": _snap("SELL"), "M1": _m1_pass("SELL")}
    result = evaluate("BTCUSD#", frames, context={"h1_bias": "BULLISH"}, settings=_settings())
    assert result["status"] == "WAIT"
    assert result["reason"] == "ORDER_FLOW_H1_BIAS_CONFLICT"


# ── 2. Biais aligné → bonus +5 ─────────────────────────────────────────────

def test_bearish_bias_aligned_with_sell_adds_bonus():
    frames = {"order_flow_snapshot": _snap("SELL"), "M1": _m1_pass("SELL")}
    result = evaluate("BTCUSD#", frames, context={"h1_bias": "BEARISH"}, settings=_settings())
    assert result.get("status") == "ORDER_READY"
    # score should be >= base (already good setup) + 5 alignment bonus
    assert result["confidence"] >= 5


def test_bullish_bias_aligned_with_buy_adds_bonus():
    frames = {"order_flow_snapshot": _snap("BUY"), "M1": _m1_pass("BUY")}
    result = evaluate("BTCUSD#", frames, context={"h1_bias": "BULLISH"}, settings=_settings())
    assert result.get("status") == "ORDER_READY"
    assert result["confidence"] >= 5


# ── 3. NEUTRAL / absent → passe-through sans modifier le score ─────────────

def test_neutral_bias_does_not_block():
    frames = {"order_flow_snapshot": _snap("SELL"), "M1": _m1_pass("SELL")}
    result_neutral = evaluate("BTCUSD#", frames, context={"h1_bias": "NEUTRAL"}, settings=_settings())
    result_absent  = evaluate("BTCUSD#", frames, context={}, settings=_settings())
    # Neither should be blocked by H1 bias
    for r in (result_neutral, result_absent):
        assert r.get("reason") != "ORDER_FLOW_H1_BIAS_CONFLICT"


def test_missing_context_does_not_block():
    frames = {"order_flow_snapshot": _snap("SELL"), "M1": _m1_pass("SELL")}
    result = evaluate("BTCUSD#", frames, context=None, settings=_settings())
    assert result.get("reason") != "ORDER_FLOW_H1_BIAS_CONFLICT"


# ── 4. Casse insensible ─────────────────────────────────────────────────────

def test_lowercase_bias_respected():
    frames = {"order_flow_snapshot": _snap("BUY"), "M1": _m1_pass("BUY")}
    result = evaluate("BTCUSD#", frames, context={"h1_bias": "bearish"}, settings=_settings())
    assert result["status"] == "WAIT"
    assert result["reason"] == "ORDER_FLOW_H1_BIAS_CONFLICT"
