"""Tests for BTC RANGE mode, ORDER_FLOW_NATIVE clamping, and OF bonus in confluence_engine.

§4.4 / v1.4 behaviour:
- ORDER_FLOW_NATIVE strategies: smc/mtfa clamped to ≥ 0 when strategy_aware=True
- When strategy_aware=False (default): no OF clamping applied
- BTC RANGE mode still reduces smc from -15 → -5 for non-ORDER_FLOW_NATIVE on BTC
- Non-BTC symbols are NOT affected by BTC RANGE mode
- ORDER_FLOW_NATIVE = {ORDER_FLOW_EXECUTION_AGENT, GOLD_LIQUIDITY_HUNTER_PRO, GOLD_ORDER_FLOW_CVD_VWAP}
- BTC_SCALPING_AGENT is NOT ORDER_FLOW_NATIVE (it has its own route bypass)
"""
from __future__ import annotations

import unittest

import pandas as pd

from app.agents.confluence_engine import ConfluenceEngine, _order_flow_bonus


def _flat_frames(n: int = 25, price: float = 50000.0) -> dict:
    df = pd.DataFrame({
        "open":  [price] * n,
        "high":  [price + 50.0] * n,
        "low":   [price - 50.0] * n,
        "close": [price] * n,
    })
    return {"M5": df}


def _range_context(symbol: str = "BTCUSD#") -> dict:
    """Context mimicking analyze_symbol() output when SMC is RANGE + MTFA is NEUTRAL."""
    return {
        "smc_confluence": {
            "smc_h4_direction": "RANGE",
            "smc_h1_trend": "RANGE",
            "smc_confluence_score": 15,
        },
        "mtfa": {
            "h1_bias": "NEUTRAL",
            "mtfa_score": 15,
        },
    }


class TestOrderFlowNativeClamp(unittest.TestCase):
    """§4.4 / v1.4: OF clamping requires strategy_aware=True.

    ORDER_FLOW_NATIVE = {ORDER_FLOW_EXECUTION_AGENT, GOLD_LIQUIDITY_HUNTER_PRO,
                         GOLD_ORDER_FLOW_CVD_VWAP}
    BTC_SCALPING_AGENT is NOT in this set (it has its own route bypass).
    """

    def setUp(self) -> None:
        self.engine = ConfluenceEngine()

    def test_of_native_gold_clamped_when_flag_true(self) -> None:
        """GOLD_LIQUIDITY_HUNTER_PRO smc/mtfa clamped to 0 when strategy_aware=True."""
        ctx = _range_context("GOLD")
        result = self.engine.evaluate(
            "GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames(), ctx, strategy_aware=True,
        )
        self.assertEqual(result["components"]["smc"], 0.0,
                         f"smc must be clamped to 0 when strategy_aware=True, got {result['components']['smc']}")
        self.assertEqual(result["components"]["mtfa"], 0.0,
                         f"mtfa must be clamped to 0 when strategy_aware=True, got {result['components']['mtfa']}")

    def test_of_native_order_flow_exec_clamped_when_flag_true(self) -> None:
        """ORDER_FLOW_EXECUTION_AGENT smc/mtfa clamped when strategy_aware=True."""
        ctx = _range_context("BTCUSD#")
        result = self.engine.evaluate(
            "BTCUSD#", "ORDER_FLOW_EXECUTION_AGENT", _flat_frames(), ctx, strategy_aware=True,
        )
        self.assertEqual(result["components"]["smc"], 0.0)
        self.assertEqual(result["components"]["mtfa"], 0.0)

    def test_of_native_not_clamped_when_flag_false(self) -> None:
        """Without strategy_aware=True, OF clamping is NOT applied (default off)."""
        ctx = _range_context("GOLD")
        result = self.engine.evaluate(
            "GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames(), ctx,
            # strategy_aware defaults to False
        )
        # With smc_confluence_score=15 → STRONG_FAIL → smc_contrib = -15
        self.assertLess(result["components"]["smc"], 0.0,
                        "clamping must be OFF when strategy_aware=False")

    def test_of_native_cvd_vwap_clamped_when_flag_true(self) -> None:
        """GOLD_ORDER_FLOW_CVD_VWAP clamped when strategy_aware=True."""
        ctx = _range_context("GOLD")
        result = self.engine.evaluate(
            "GOLD", "GOLD_ORDER_FLOW_CVD_VWAP", _flat_frames(), ctx, strategy_aware=True,
        )
        self.assertEqual(result["components"]["smc"], 0.0)
        self.assertEqual(result["components"]["mtfa"], 0.0)

    def test_btc_scalping_not_of_native(self) -> None:
        """BTC_SCALPING_AGENT is NOT ORDER_FLOW_NATIVE — no clamping even with strategy_aware."""
        ctx = _range_context("BTCUSD#")
        result = self.engine.evaluate(
            "BTCUSD#", "BTC_SCALPING_AGENT", _flat_frames(), ctx, strategy_aware=True,
        )
        # BTC RANGE mode will mitigate to -5, but clamping to 0 does NOT apply
        self.assertLess(result["components"]["smc"], 0.0,
                        "BTC_SCALPING_AGENT is not OF-native, smc must not be clamped to 0")

    def test_strategy_class_order_flow_native(self) -> None:
        """strategy_class == 'ORDER_FLOW_NATIVE' for OF strategies."""
        for strat in ("ORDER_FLOW_EXECUTION_AGENT", "GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_ORDER_FLOW_CVD_VWAP"):
            result = self.engine.evaluate("GOLD", strat, _flat_frames(), {})
            self.assertEqual(result["strategy_class"], "ORDER_FLOW_NATIVE",
                             f"{strat} must have strategy_class=ORDER_FLOW_NATIVE")

    def test_strategy_class_smc_native(self) -> None:
        """strategy_class == 'SMC_NATIVE' for SMC-aligned strategies."""
        for strat in ("SIMO_ATM_BREAKOUT", "FIB_CONFLUENCE_EXECUTION_AGENT",
                      "AMD_FVG_IFVG_REVERSAL", "CRT_TBS_REVERSAL"):
            result = self.engine.evaluate("GOLD", strat, _flat_frames(), {})
            self.assertEqual(result["strategy_class"], "SMC_NATIVE",
                             f"{strat} must have strategy_class=SMC_NATIVE")

    def test_strategy_class_default_for_unknown(self) -> None:
        result = self.engine.evaluate("BTCUSD#", "NON_OF_STRATEGY", _flat_frames(), {})
        self.assertEqual(result["strategy_class"], "DEFAULT")


class TestBtcRangeModeNonOfNative(unittest.TestCase):
    """BTC RANGE mode mitigation still applies for non-ORDER_FLOW_NATIVE strategies on BTC."""

    def setUp(self) -> None:
        self.engine = ConfluenceEngine()

    def test_btc_range_mode_reduces_to_minus5_for_non_of_native(self) -> None:
        """Non-OF strategy on BTC + RANGE: smc should be mitigated to -5, not -15."""
        ctx = _range_context("BTCUSD#")
        ctx["mtfa"]["h1_bias"] = "BULLISH"
        ctx["mtfa"]["mtfa_score"] = 75  # PASS
        result = self.engine.evaluate("BTCUSD#", "NON_OF_STRATEGY", _flat_frames(), ctx)
        self.assertEqual(result["components"]["smc"], -5.0,
                         f"BTC RANGE mode must mitigate to -5 for non-OF, got {result['components']['smc']}")

    def test_non_btc_not_affected_by_range_mode(self) -> None:
        """GOLD (non-BTC) with RANGE context and non-OF strategy keeps full -15 penalty."""
        ctx = _range_context("GOLD")
        result = self.engine.evaluate("GOLD", "SIMO_ATM_BREAKOUT", _flat_frames(), ctx)
        self.assertEqual(result["components"]["smc"], -15.0,
                         f"GOLD should not get BTC RANGE mitigation, smc={result['components']['smc']}")
        self.assertEqual(result["components"]["mtfa"], -15.0,
                         f"GOLD should not get BTC RANGE mitigation, mtfa={result['components']['mtfa']}")


class TestBtcOrderFlowBonus(unittest.TestCase):

    def test_grade_a_score_95_bonus(self) -> None:
        """grade=A score=95 → bonus = 8 + (95-75)*0.4 = 8 + 8 = 16.0"""
        ctx = {"btc_order_flow_bonus": 8.0 + (95 - 75) * 0.4}
        val = _order_flow_bonus(ctx)
        self.assertAlmostEqual(val, 16.0, places=2)

    def test_grade_b_score_70_bonus(self) -> None:
        """grade=B score=70 → bonus = 4 + (70-65)*0.2 = 4 + 1 = 5.0"""
        ctx = {"btc_order_flow_bonus": 4.0 + (70 - 65) * 0.2}
        val = _order_flow_bonus(ctx)
        self.assertAlmostEqual(val, 5.0, places=2)

    def test_no_btc_of_bonus_falls_back_to_standard(self) -> None:
        """Without btc_order_flow_bonus key, standard order_flow_reader logic applies."""
        ctx = {"order_flow_reader": {"signal": "BUY", "score": 80.0}}
        val = _order_flow_bonus(ctx)
        self.assertEqual(val, 5.0)

    def test_btc_of_bonus_zero_not_overrides(self) -> None:
        """btc_order_flow_bonus=0.0 returns 0.0 (key present but zero)."""
        ctx = {"btc_order_flow_bonus": 0.0}
        val = _order_flow_bonus(ctx)
        self.assertEqual(val, 0.0)

    def test_btc_of_bonus_applied_in_full_evaluate(self) -> None:
        """btc_order_flow_bonus flows through evaluate() into order_flow_bonus component.
        Uses ORDER_FLOW_EXECUTION_AGENT (OF-native) with strategy_aware=True.
        """
        engine = ConfluenceEngine()
        ctx = {
            "btc_order_flow_bonus": 16.0,
            "smc_confluence": {"smc_h4_direction": "RANGE", "smc_h1_trend": "RANGE", "smc_confluence_score": 15},
            "mtfa": {"h1_bias": "NEUTRAL", "mtfa_score": 15},
        }
        result = engine.evaluate(
            "BTCUSD#", "ORDER_FLOW_EXECUTION_AGENT", _flat_frames(), ctx, strategy_aware=True,
        )
        self.assertEqual(result["components"]["order_flow_bonus"], 16.0)
        # OF-native + strategy_aware=True: smc/mtfa clamped to 0
        self.assertEqual(result["components"]["smc"], 0.0)
        self.assertEqual(result["components"]["mtfa"], 0.0)


if __name__ == "__main__":
    unittest.main()
