from __future__ import annotations

import pandas as pd
import pytest

from app.agents.confluence_engine import ConfluenceEngine


def _dummy_frames() -> dict:
    data = {
        "open":   [100.0, 101.0, 102.0, 101.5, 103.0],
        "high":   [102.0, 103.0, 104.0, 103.5, 105.0],
        "low":    [99.0,  100.0, 101.0, 100.5, 102.0],
        "close":  [101.0, 102.0, 103.0, 102.0, 104.0],
        "volume": [1000,  1100,  1200,  1050,  1300],
    }
    df = pd.DataFrame(data)
    return {"M5": df}


def _ctx_strong_fail(of_score: float = 0.0, of_signal: str = "BUY") -> dict:
    """Context that gives STRONG_FAIL for both SMC and MTFA."""
    return {
        "smc_confluence_score": 10.0,   # STRONG_FAIL (< 40)
        "mtfa_score": 10.0,             # STRONG_FAIL (< 35)
        "order_flow_reader": {"grade": "A", "score": of_score, "signal": of_signal},
    }


def test_strat_aware_clamp_applied_for_of_native():
    """ORDER_FLOW_EXECUTION_AGENT with strategy_aware=True: smc/mtfa clamped to 0."""
    engine = ConfluenceEngine()
    result = engine.evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        frames=_dummy_frames(),
        context=_ctx_strong_fail(of_score=95.0),
        strategy_aware=True,
    )
    assert result["components"]["smc"] == 0.0, "smc should be clamped to 0 for OF_NATIVE"
    assert result["components"]["mtfa"] == 0.0, "mtfa should be clamped to 0 for OF_NATIVE"


def test_strat_aware_not_applied_for_smc_native():
    """SIMO_ATM_BREAKOUT with strategy_aware=True: smc/mtfa NOT clamped (SMC_NATIVE)."""
    engine = ConfluenceEngine()
    result = engine.evaluate(
        symbol="BTCUSD#",
        strategy="SIMO_ATM_BREAKOUT",
        frames=_dummy_frames(),
        context=_ctx_strong_fail(of_score=95.0),
        strategy_aware=True,
    )
    assert result["components"]["smc"] == -15.0, "smc should remain -15 for SMC_NATIVE STRONG_FAIL"
    assert result["components"]["mtfa"] == -15.0, "mtfa should remain -15 for SMC_NATIVE STRONG_FAIL"


def test_strat_aware_false_no_clamp():
    """strategy_aware=False → no clamp even for ORDER_FLOW_NATIVE."""
    engine = ConfluenceEngine()
    result = engine.evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        frames=_dummy_frames(),
        context=_ctx_strong_fail(of_score=95.0),
        strategy_aware=False,
    )
    assert result["components"]["smc"] == -15.0
    assert result["components"]["mtfa"] == -15.0


def test_strat_aware_clamp_gold_of_native():
    """GOLD_LIQUIDITY_HUNTER_PRO with strategy_aware=True: penalties clamped."""
    engine = ConfluenceEngine()
    result = engine.evaluate(
        symbol="GOLD#",
        strategy="GOLD_LIQUIDITY_HUNTER_PRO",
        frames=_dummy_frames(),
        context=_ctx_strong_fail(of_score=91.0, of_signal="SELL"),
        strategy_aware=True,
    )
    assert result["components"]["smc"] == 0.0
    assert result["components"]["mtfa"] == 0.0


def test_strat_aware_graded_of_bonus_applied():
    """With strategy_aware=True and grade=A, of_bonus should use graded path (+15)."""
    engine = ConfluenceEngine()
    ctx = {
        "smc_confluence_score": 10.0,
        "mtfa_score": 10.0,
        "order_flow_reader": {"grade": "A", "score": 95.0, "signal": "BUY"},
    }
    result = engine.evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        frames=_dummy_frames(),
        context=ctx,
        strategy_aware=True,
    )
    assert result["components"]["order_flow_bonus"] == 15.0


def test_strat_aware_grade_b_bonus():
    """With strategy_aware=True and grade=B, of_bonus = +8."""
    engine = ConfluenceEngine()
    ctx = {
        "smc_confluence_score": 10.0,
        "mtfa_score": 10.0,
        "order_flow_reader": {"grade": "B", "score": 83.0, "signal": "SELL"},
    }
    result = engine.evaluate(
        symbol="EURUSD",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        frames=_dummy_frames(),
        context=ctx,
        strategy_aware=True,
    )
    assert result["components"]["order_flow_bonus"] == 8.0
