from __future__ import annotations

import pytest

from app.agents.confirmation_matrix import evaluate


def test_of_native_strong_fail_bypassed_when_of_score_high():
    """ORDER_FLOW_EXECUTION_AGENT + STRONG_FAIL (smc=10, mtfa=10) + of_score=95 → hard_block=False."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=95.0,
        rr=2.0,
        of_score=95.0,
    )
    assert result["hard_block"] is False
    assert result["hard_block_reason"] == "OF_OVERRIDE_APPLIED"
    assert result["smc_calibrated_status"] == "STRONG_FAIL"
    assert result["mtfa_calibrated_status"] == "STRONG_FAIL"


def test_of_native_strong_fail_kept_when_of_score_low():
    """ORDER_FLOW_EXECUTION_AGENT + STRONG_FAIL + of_score=60 → hard_block=True (no override)."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=60.0,
        rr=2.0,
        of_score=60.0,
    )
    assert result["hard_block"] is True
    assert "SMC_STRONG_FAIL" in result["hard_block_reason"]


def test_of_native_strong_fail_at_boundary_89():
    """of_score=89.9 (just under 90) → hard_block=True."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=89.0,
        rr=2.0,
        of_score=89.9,
    )
    assert result["hard_block"] is True


def test_of_native_strong_fail_at_boundary_90():
    """of_score=90.0 (exactly at threshold) → hard_block=False."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=90.0,
        rr=2.0,
        of_score=90.0,
    )
    assert result["hard_block"] is False
    assert result["hard_block_reason"] == "OF_OVERRIDE_APPLIED"


def test_btc_scalping_agent_override_at_high_score():
    """BTC_SCALPING_AGENT + STRONG_FAIL + of_score=92 → hard_block=False."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="BTC_SCALPING_AGENT",
        smc_score=5.0,
        mtfa_score=5.0,
        setup_score=92.0,
        rr=2.0,
        of_score=92.0,
    )
    assert result["hard_block"] is False
    assert result["hard_block_reason"] == "OF_OVERRIDE_APPLIED"


def test_smc_native_strong_fail_not_overridden():
    """SIMO_ATM_BREAKOUT + STRONG_FAIL + low setup_score → hard_block=True (not OF_NATIVE, no override)."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="SIMO_ATM_BREAKOUT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=60.0,   # below 75 → calibrated waiver doesn't apply
        rr=2.0,
        of_score=95.0,      # high OF score irrelevant — strategy not in ORDER_FLOW_NATIVE
    )
    assert result["hard_block"] is True
    assert "SMC_STRONG_FAIL" in result["hard_block_reason"]
    assert result["hard_block_reason"] != "OF_OVERRIDE_APPLIED"


def test_soft_fail_behavior_unchanged_no_hard_block():
    """SOFT_FAIL on any strategy → hard_block=False regardless of of_score."""
    for strategy in ("ORDER_FLOW_EXECUTION_AGENT", "SIMO_ATM_BREAKOUT", "BTC_SCALPING_AGENT"):
        result = evaluate(
            symbol="GOLD#",
            strategy=strategy,
            smc_score=50.0,  # SOFT_FAIL (40–69)
            mtfa_score=45.0,  # SOFT_FAIL (35–59)
            setup_score=70.0,
            rr=2.0,
            of_score=0.0,
        )
        assert result["hard_block"] is False, f"strategy={strategy} should not hard-block on SOFT_FAIL"


def test_default_of_score_zero_preserves_old_behavior():
    """Calling evaluate() without of_score (default 0.0) keeps ORDER_FLOW hard-block intact."""
    result = evaluate(
        symbol="BTCUSD#",
        strategy="ORDER_FLOW_EXECUTION_AGENT",
        smc_score=10.0,
        mtfa_score=10.0,
        setup_score=95.0,
        rr=2.0,
    )
    assert result["hard_block"] is True
