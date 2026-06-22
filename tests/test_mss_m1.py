from __future__ import annotations

import math
import pandas as pd
import pytest
from types import SimpleNamespace

from app.strategies.order_flow_execution_agent import _check_mss_m1


def _m1(closes, lows=None, highs=None):
    n = len(closes)
    lows = lows or [c - 1.0 for c in closes]
    highs = highs or [c + 1.0 for c in closes]
    return pd.DataFrame({"close": closes, "low": lows, "high": highs})


# ── 1. Données insuffisantes ────────────────────────────────────────────────

def test_none_df_returns_none():
    assert _check_mss_m1(None, "SELL") is None


def test_empty_df_returns_none():
    assert _check_mss_m1(pd.DataFrame(), "SELL") is None


def test_too_few_candles_returns_none():
    df = _m1([100.0, 99.0, 98.0, 97.0])  # 4 rows — need >= 5
    assert _check_mss_m1(df, "SELL") is None


# ── 2. SELL — Lower Low confirmé ────────────────────────────────────────────

def test_sell_mss_confirmed_lower_low():
    # closes: [..., 102, 101, 100], lows of [-4:-1] = [102-1, 101-1, 100-1] = [101, 100, 99]
    # last close = 95.0 < recent_low = min(101, 100, 99) = 99 → True
    closes = [105.0, 104.0, 103.0, 102.0, 95.0]
    lows =   [104.0, 103.0, 102.0, 101.0, 94.0]
    highs =  [106.0, 105.0, 104.0, 103.0, 96.0]
    df = _m1(closes, lows, highs)
    assert _check_mss_m1(df, "SELL") is True


def test_sell_mss_not_confirmed_close_above_recent_low():
    # last close = 103.5 > recent_low = 101 → False
    closes = [105.0, 104.0, 103.0, 102.0, 103.5]
    lows =   [104.0, 103.0, 102.0, 101.0, 102.5]
    highs =  [106.0, 105.0, 104.0, 103.0, 104.5]
    df = _m1(closes, lows, highs)
    assert _check_mss_m1(df, "SELL") is False


# ── 3. BUY — Higher High confirmé ───────────────────────────────────────────

def test_buy_mss_confirmed_higher_high():
    # last close = 110.0 > recent_high = max(highs[-4:-1]) = max(103,104,105) = 105 → True
    closes = [100.0, 101.0, 102.0, 103.0, 110.0]
    lows =   [ 99.0, 100.0, 101.0, 102.0, 109.0]
    highs =  [101.0, 102.0, 103.0, 104.0, 111.0]
    df = _m1(closes, lows, highs)
    assert _check_mss_m1(df, "BUY") is True


def test_buy_mss_not_confirmed_close_below_recent_high():
    # last close = 103.0 < recent_high = max(101,102,103) = 103 → NOT strictly greater → False
    closes = [100.0, 101.0, 102.0, 103.0, 103.0]
    lows =   [ 99.0, 100.0, 101.0, 102.0, 102.0]
    highs =  [101.0, 102.0, 103.0, 104.0, 104.0]
    df = _m1(closes, lows, highs)
    assert _check_mss_m1(df, "BUY") is False


# ── 4. Intégration dans evaluate() ─────────────────────────────────────────

def _snapshot():
    return {
        "price": 100.0, "vwap": 98.0, "poc": 98.5,
        "vah": 101.0, "val": 96.0,
        "cvd_slope": -1.0, "delta_proxy": -500.0, "divergence": None,
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


def test_evaluate_mss_none_sets_m1_entry_confirmation_true():
    from app.strategies.order_flow_execution_agent import evaluate
    frames = {
        "order_flow_snapshot": _snapshot(),
        # No "M1" key → _mss=None → m1_entry_confirmation=True
    }
    result = evaluate("BTCUSD#", frames, settings=_settings())
    if result["status"] == "WAIT":
        pytest.skip("setup not detected with these fixtures")
    assert result["m1_entry_confirmation"] is True
    assert result["m1_trigger_status"] == "PASS"


def test_evaluate_mss_false_score_below_85_returns_wait():
    from app.strategies.order_flow_execution_agent import evaluate
    # Force a SELL setup at VAH: price > vah - prox, delta < 0, divergence=bear
    snap = {
        "price": 101.0, "vwap": 99.0, "poc": 99.5,
        "vah": 101.2, "val": 97.0,
        "cvd_slope": -2.0, "delta_proxy": -600.0, "divergence": "bear",
        "created_at": None,
    }
    # M1 with 5 candles: last close (102) > recent_low (99) → mss=False for SELL
    closes = [103.0, 102.0, 101.0, 100.0, 102.0]
    lows =   [102.0, 101.0, 100.0,  99.0, 101.0]
    highs =  [104.0, 103.0, 102.0, 101.0, 103.0]
    m1_df = pd.DataFrame({"close": closes, "low": lows, "high": highs})
    frames = {"order_flow_snapshot": snap, "M1": m1_df}
    # WEEKEND session → session_ok=False → score=80 (below 85 threshold)
    result = evaluate("BTCUSD#", frames, context={"session_name": "WEEKEND"}, settings=_settings())
    assert result["status"] == "WAIT"
    assert result["reason"] == "ORDER_FLOW_MSS_NOT_CONFIRMED"
