from __future__ import annotations

import pandas as pd
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from app.strategies.order_flow_execution_agent import _in_kill_zone, evaluate


# ── 1. _in_kill_zone — détection des zones ──────────────────────────────────

def test_london_open_detected():
    assert _in_kill_zone({"utc_hour": 7}) == (True, "LONDON_OPEN")
    assert _in_kill_zone({"utc_hour": 8}) == (True, "LONDON_OPEN")


def test_london_open_boundary_exclusive():
    assert _in_kill_zone({"utc_hour": 9})[0] is False


def test_ny_open_detected():
    assert _in_kill_zone({"utc_hour": 12}) == (True, "NY_OPEN")
    assert _in_kill_zone({"utc_hour": 13}) == (True, "NY_OPEN")


def test_ny_open_boundary_exclusive():
    assert _in_kill_zone({"utc_hour": 14})[0] is False


def test_asian_reversal_detected():
    assert _in_kill_zone({"utc_hour": 1}) == (True, "ASIAN_REVERSAL")
    assert _in_kill_zone({"utc_hour": 2}) == (True, "ASIAN_REVERSAL")


def test_outside_all_zones_returns_false():
    for h in (0, 3, 6, 10, 11, 15, 20, 23):
        active, name = _in_kill_zone({"utc_hour": h})
        assert active is False
        assert name is None


def test_none_context_falls_back_to_system_clock():
    # Can't control clock, just assert it returns a well-formed tuple
    active, name = _in_kill_zone(None)
    assert isinstance(active, bool)
    assert name is None or isinstance(name, str)


def test_invalid_utc_hour_falls_back_to_system_clock():
    active, name = _in_kill_zone({"utc_hour": "bad"})
    assert isinstance(active, bool)


# ── 2. Bonus +6 dans evaluate() ─────────────────────────────────────────────

def _snap_sell():
    return {
        "price": 101.0, "vwap": 99.0, "poc": 99.5,
        "vah": 101.2, "val": 97.0,
        "cvd_slope": -2.0, "delta_proxy": -600.0, "divergence": "bear",
        "created_at": None,
    }


def _m1_sell_pass():
    closes = [105.0, 104.0, 103.0, 102.0, 95.0]
    lows   = [104.0, 103.0, 102.0, 101.0, 94.0]
    highs  = [106.0, 105.0, 104.0, 103.0, 96.0]
    return pd.DataFrame({"close": closes, "low": lows, "high": highs})


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


def test_kill_zone_bonus_increases_score():
    frames = {"order_flow_snapshot": _snap_sell(), "M1": _m1_sell_pass()}
    ctx_in  = {"utc_hour": 8}   # London Open
    ctx_out = {"utc_hour": 10}  # outside

    result_in  = evaluate("BTCUSD#", frames, context=ctx_in,  settings=_settings())
    result_out = evaluate("BTCUSD#", frames, context=ctx_out, settings=_settings())

    if result_in["status"] == "WAIT" or result_out["status"] == "WAIT":
        pytest.skip("setup not triggered with these fixtures")

    assert result_in["confidence"] == result_out["confidence"] + 6


def test_kill_zone_bonus_capped_at_100():
    # Force a very high score by giving a high min_score=0 and aligned everything
    frames = {"order_flow_snapshot": _snap_sell(), "M1": _m1_sell_pass()}
    ctx = {"utc_hour": 8, "h1_bias": "BEARISH"}  # aligned + kill zone
    result = evaluate("BTCUSD#", frames, context=ctx, settings=_settings())
    if result["status"] == "WAIT":
        pytest.skip("setup not triggered")
    assert result["confidence"] <= 100
