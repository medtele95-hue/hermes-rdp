"""Tests for app/agents/confluence_engine.py."""
from __future__ import annotations

import logging
import unittest

import pandas as pd

from app.agents.confluence_engine import ConfluenceEngine, _order_flow_bonus, evaluate_confluence


def _flat_frames(n: int = 25, price: float = 100.0) -> dict:
    df = pd.DataFrame({
        "open":  [price] * n,
        "high":  [price + 0.5] * n,
        "low":   [price - 0.5] * n,
        "close": [price] * n,
    })
    return {"M5": df}


def _trending_frames(n: int = 25) -> dict:
    closes = [100.0 + i * 0.5 for i in range(n)]
    df = pd.DataFrame({
        "open":  [c - 0.2 for c in closes],
        "high":  [c + 0.3 for c in closes],
        "low":   [c - 0.3 for c in closes],
        "close": closes,
    })
    return {"M5": df}


class TestConfluenceEngineScore(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ConfluenceEngine()

    def test_score_in_range_0_100(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames())
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 100.0)

    def test_grade_is_valid(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames())
        self.assertIn(result["grade"], {"A+", "A", "B", "C", "D"})

    def test_recommendation_is_valid(self) -> None:
        result = self.engine.evaluate("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames())
        self.assertIn(result["recommendation"], {"TRADE", "WATCH", "WAIT"})

    def test_components_dict_present(self) -> None:
        result = self.engine.evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", _flat_frames())
        self.assertIsInstance(result["components"], dict)

    def test_symbol_and_strategy_echoed(self) -> None:
        result = self.engine.evaluate("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames())
        self.assertEqual(result["symbol"], "GOLD")
        self.assertEqual(result["strategy"], "GOLD_LIQUIDITY_HUNTER_PRO")

    def test_empty_frames_returns_valid_result(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", {})
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 100.0)
        self.assertIn(result["grade"], {"A+", "A", "B", "C", "D"})

    def test_none_frames_returns_valid_result(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", None)
        self.assertGreaterEqual(result["score"], 0.0)

    def test_smc_score_increases_total(self) -> None:
        ctx_smc = {"smc_confluence_score": 80.0}
        ctx_none = {}
        with_smc = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames(), ctx_smc)
        without_smc = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames(), ctx_none)
        self.assertGreaterEqual(with_smc["score"], without_smc["score"])

    def test_mtfa_score_increases_total(self) -> None:
        ctx_mtfa = {"mtfa_score": 80.0}
        ctx_none = {}
        with_mtfa = self.engine.evaluate("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames(), ctx_mtfa)
        without_mtfa = self.engine.evaluate("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", _flat_frames(), ctx_none)
        self.assertGreaterEqual(with_mtfa["score"], without_mtfa["score"])

    def test_trending_data_produces_nonzero_score(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _trending_frames())
        self.assertGreater(result["score"], 0.0)

    def test_atr_field_present(self) -> None:
        result = self.engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _trending_frames())
        # atr may be None for flat data but should be present as a key
        self.assertIn("atr", result)


class TestOrderFlowBonus(unittest.TestCase):
    """ORDER_FLOW_READER must never hard-block — only bonus/warning ±5."""

    def test_strong_buy_signal_gives_bonus(self) -> None:
        ctx = {"order_flow_reader": {"signal": "BUY", "score": 80.0}}
        self.assertEqual(_order_flow_bonus(ctx), 5.0)

    def test_strong_sell_signal_gives_bonus(self) -> None:
        ctx = {"order_flow_reader": {"signal": "SELL", "score": 75.0}}
        self.assertEqual(_order_flow_bonus(ctx), 5.0)

    def test_weak_signal_gives_warning(self) -> None:
        ctx = {"order_flow_reader": {"signal": "BUY", "score": 20.0}}
        self.assertEqual(_order_flow_bonus(ctx), -5.0)

    def test_missing_order_flow_gives_zero(self) -> None:
        self.assertEqual(_order_flow_bonus({}), 0.0)

    def test_non_dict_order_flow_gives_zero(self) -> None:
        ctx = {"order_flow_reader": "INVALID"}
        self.assertEqual(_order_flow_bonus(ctx), 0.0)

    def test_bonus_never_exceeds_5(self) -> None:
        ctx = {"order_flow_reader": {"signal": "BUY", "score": 100.0}}
        bonus = _order_flow_bonus(ctx)
        self.assertLessEqual(bonus, 5.0)

    def test_warning_never_below_minus_5(self) -> None:
        ctx = {"order_flow_reader": {"signal": "SELL", "score": 0.0}}
        bonus = _order_flow_bonus(ctx)
        self.assertGreaterEqual(bonus, -5.0)

    def test_order_flow_never_makes_score_negative(self) -> None:
        """Worst-case order flow penalty must not push score below 0."""
        engine = ConfluenceEngine()
        ctx = {"order_flow_reader": {"signal": "BUY", "score": 0.0}}
        result = engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames(), ctx)
        self.assertGreaterEqual(result["score"], 0.0)

    def test_order_flow_never_makes_score_exceed_100(self) -> None:
        engine = ConfluenceEngine()
        ctx = {
            "order_flow_reader": {"signal": "BUY", "score": 100.0},
            "smc_confluence_score": 100.0,
            "mtfa_score": 100.0,
        }
        result = engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _trending_frames(), ctx)
        self.assertLessEqual(result["score"], 100.0)


class TestConfluenceGradeThresholds(unittest.TestCase):
    def _score_to_grade(self, score: float) -> str:
        from app.agents.confluence_engine import _grade
        return _grade(score)

    def test_a_plus_at_85(self) -> None:
        self.assertEqual(self._score_to_grade(85.0), "A+")

    def test_a_at_75(self) -> None:
        self.assertEqual(self._score_to_grade(75.0), "A")

    def test_b_at_65(self) -> None:
        self.assertEqual(self._score_to_grade(65.0), "B")

    def test_c_at_50(self) -> None:
        self.assertEqual(self._score_to_grade(50.0), "C")

    def test_d_at_49(self) -> None:
        self.assertEqual(self._score_to_grade(49.9), "D")

    def test_d_at_zero(self) -> None:
        self.assertEqual(self._score_to_grade(0.0), "D")

    def test_a_plus_at_100(self) -> None:
        self.assertEqual(self._score_to_grade(100.0), "A+")


class TestStratAwareLog(unittest.TestCase):
    """[CONFLUENCE_STRAT_AWARE] must appear for ORDER_FLOW_NATIVE strategies on any symbol."""

    def _run_and_capture(self, symbol: str, strategy: str, ctx: dict) -> list[str]:
        engine = ConfluenceEngine()
        with self.assertLogs("hermes", level=logging.INFO) as cm:
            engine.evaluate(symbol, strategy, _flat_frames(), ctx)
        return [m for m in cm.output if "CONFLUENCE_STRAT_AWARE" in m]

    def test_btcusd_of_agent_passes_when_smc_mtfa_both_positive(self) -> None:
        """BTCUSD + ORDER_FLOW_EXECUTION_AGENT must log strat_aware even when SMC/MTFA are PASS."""
        ctx = {
            "smc_confluence_score": 80.0,   # → PASS → +10 (no penalty to clamp)
            "mtfa_score": 80.0,              # → PASS → +10 (no penalty to clamp)
            "order_flow_reader": {"grade": "A+", "score": 100.0, "signal": "BUY"},
        }
        msgs = self._run_and_capture("BTCUSD", "ORDER_FLOW_EXECUTION_AGENT", ctx)
        self.assertTrue(msgs, "[CONFLUENCE_STRAT_AWARE] not emitted for BTCUSD")
        self.assertIn("strat_aware=True", msgs[0])

    def test_btcusd_of_agent_passes_when_smc_negative(self) -> None:
        """BTCUSD + ORDER_FLOW_EXECUTION_AGENT must log strat_aware when SMC is penalising."""
        ctx = {
            "smc_confluence_score": 30.0,   # → STRONG_FAIL → -15
            "mtfa_score": 80.0,
            "order_flow_reader": {"grade": "A+", "score": 100.0, "signal": "BUY"},
        }
        msgs = self._run_and_capture("BTCUSD", "ORDER_FLOW_EXECUTION_AGENT", ctx)
        self.assertTrue(msgs, "[CONFLUENCE_STRAT_AWARE] not emitted when SMC penalising")

    def test_eurusd_of_agent_logs_strat_aware(self) -> None:
        msgs = self._run_and_capture(
            "EURUSD", "ORDER_FLOW_EXECUTION_AGENT",
            {"smc_confluence_score": 80.0, "mtfa_score": 80.0},
        )
        self.assertTrue(msgs, "[CONFLUENCE_STRAT_AWARE] not emitted for EURUSD")

    def test_non_of_native_strategy_does_not_log(self) -> None:
        engine = ConfluenceEngine()
        with self.assertLogs("hermes", level=logging.INFO) as cm:
            engine.evaluate("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames(), {})
        strat_aware_msgs = [m for m in cm.output if "CONFLUENCE_STRAT_AWARE" in m]
        self.assertFalse(strat_aware_msgs, "strat_aware log must not appear for non-OF_NATIVE strategy")


class TestEvaluateConfluenceConvenienceFunction(unittest.TestCase):
    def test_returns_dict(self) -> None:
        result = evaluate_confluence("BTCUSD", "BTC_SCALPING_AGENT", _flat_frames())
        self.assertIsInstance(result, dict)
        self.assertIn("score", result)

    def test_none_frames_does_not_raise(self) -> None:
        result = evaluate_confluence("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", None)
        self.assertIn("score", result)


if __name__ == "__main__":
    unittest.main()
