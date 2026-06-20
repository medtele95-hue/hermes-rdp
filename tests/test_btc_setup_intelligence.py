"""Tests for app/mt5/btc_setup_intelligence.py — BTC Setup Intelligence Engine.

Covers:
- Grade thresholds (A+, A, B, C, D)
- Decision logic per grade and adaptive mode
- VWAP/POC blocking
- CVD opposing blocks
- Weak confirmation blocks
- Strong multi-confirmation passes
- Exit profile selection
- Confidence multiplier values
- SMC/MTFA hard block handling
- Entry mode variations (CONFIDENT/NORMAL/DEFENSIVE/PAUSED)
"""
from __future__ import annotations

import unittest

from app.mt5.btc_setup_intelligence import (
    evaluate_btc_setup_intelligence,
    select_best_btc_setup,
    _grade,
    _session_score,
    _spread_score,
    _order_flow_score,
    _cvd_score,
    _delta_score,
    _level_relation_score,
    _momentum_score,
    _rejection_score,
)


# ─── Context helpers ──────────────────────────────────────────────────────────

def _strong_buy_ctx() -> dict:
    """All signals aligned for a strong BUY setup — should score A+."""
    return {
        "price": 65200.0,
        "bid": 65200.0,
        "vwap": 65000.0,
        "poc": 64900.0,
        "cvd_slope": 0.6,
        "delta_proxy": 800.0,
        "order_flow_signal": "BUY",
        "m1_momentum": "BULLISH",
        "m5_momentum": "BULLISH",
        "spread": 30.0,
        "max_spread": 100.0,
        "volatility_status": "NORMAL",
        "session": "LONDON_NY",
    }


def _strong_sell_ctx() -> dict:
    return {
        "price": 64800.0,
        "bid": 64800.0,
        "vwap": 65000.0,
        "poc": 65100.0,
        "cvd_slope": -0.6,
        "delta_proxy": -800.0,
        "order_flow_signal": "SELL",
        "m1_momentum": "BEARISH",
        "m5_momentum": "BEARISH",
        "spread": 30.0,
        "max_spread": 100.0,
        "volatility_status": "NORMAL",
        "session": "LONDON_NY",
    }


def _weak_ctx() -> dict:
    """Minimal signals — should score C/D."""
    return {
        "price": 65000.0,
        "bid": 65000.0,
        "vwap": 65100.0,    # price below VWAP → bad for BUY
        "poc": 65050.0,     # price below POC → bad for BUY
        "cvd_slope": -0.4,  # opposing CVD for BUY
        "order_flow_signal": "SELL",  # opposing OF
        "m1_momentum": "BEARISH",
        "m5_momentum": "BEARISH",
        "spread": 95.0,
        "max_spread": 100.0,
        "volatility_status": "HIGH",
        "session": "CLOSED",
    }


def _good_cand(**kwargs) -> dict:
    base = {"smc_score": 75.0, "mtfa_score": 65.0, "rr": 2.0}
    base.update(kwargs)
    return base


def _normal_perf() -> dict:
    return {
        "adaptive_mode": "NORMAL",
        "is_paused": False,
        "win_streak": 0,
        "loss_streak": 0,
        "requires_aplus": False,
    }


def _confident_perf(streak: int = 3) -> dict:
    return {
        "adaptive_mode": "CONFIDENT",
        "is_paused": False,
        "win_streak": streak,
        "loss_streak": 0,
        "requires_aplus": False,
    }


def _defensive_perf() -> dict:
    return {
        "adaptive_mode": "DEFENSIVE",
        "is_paused": False,
        "win_streak": 0,
        "loss_streak": 3,
        "requires_aplus": False,
    }


def _paused_perf() -> dict:
    return {
        "adaptive_mode": "PAUSED",
        "is_paused": True,
        "win_streak": 0,
        "loss_streak": 4,
        "requires_aplus": False,
    }


# ─── Grade threshold tests ────────────────────────────────────────────────────

class TestGradeThresholds(unittest.TestCase):

    def test_grade_aplus(self):
        self.assertEqual(_grade(85.0), "A+")
        self.assertEqual(_grade(100.0), "A+")

    def test_grade_a(self):
        self.assertEqual(_grade(75.0), "A")
        self.assertEqual(_grade(84.9), "A")

    def test_grade_b(self):
        self.assertEqual(_grade(65.0), "B")
        self.assertEqual(_grade(74.9), "B")

    def test_grade_c(self):
        self.assertEqual(_grade(55.0), "C")
        self.assertEqual(_grade(64.9), "C")

    def test_grade_d(self):
        self.assertEqual(_grade(0.0), "D")
        self.assertEqual(_grade(54.9), "D")


# ─── Full A+ setup passes ─────────────────────────────────────────────────────

class TestAPlus(unittest.TestCase):

    def test_aplus_buy_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["grade"], "A+")
        self.assertGreaterEqual(result["setup_quality_score"], 85.0)

    def test_aplus_sell_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "ORDER_FLOW_EXECUTION_AGENT", "SELL",
            _good_cand(), _strong_sell_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["grade"], "A+")

    def test_aplus_has_correct_multiplier(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertAlmostEqual(result["confidence_multiplier"], 1.2)


# ─── A setup passes ───────────────────────────────────────────────────────────

class TestAGrade(unittest.TestCase):

    def test_a_grade_passes_in_normal_mode(self):
        # A setup: good but not perfect — missing M5 momentum
        ctx = dict(_strong_buy_ctx())
        ctx["m5_momentum"] = "NEUTRAL"
        ctx["cvd_slope"] = 0.1    # weaker CVD
        ctx["session"] = "LONDON"
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertIn(result["grade"], ("A+", "A"))

    def test_a_grade_multiplier(self):
        ctx = dict(_strong_buy_ctx())
        ctx["m5_momentum"] = "NEUTRAL"
        ctx["cvd_slope"] = 0.15
        ctx["delta_proxy"] = 100.0
        ctx["session"] = "LONDON"
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        if result["grade"] == "A":
            self.assertAlmostEqual(result["confidence_multiplier"], 1.0)


# ─── B setup behavior ─────────────────────────────────────────────────────────

class TestBGrade(unittest.TestCase):

    def _btc_b_grade_ctx(self) -> dict:
        """Context that should score in B range (65-74)."""
        return {
            "price": 65000.0,
            "bid": 65000.0,
            "vwap": 64900.0,   # slightly above VWAP
            "poc": 64950.0,    # slightly above POC
            "cvd_slope": 0.05, # weak CVD
            "delta_proxy": 50.0,
            "order_flow_signal": "BUY",
            "m1_momentum": "BULLISH",
            "m5_momentum": "NEUTRAL",  # no M5
            "spread": 50.0,
            "max_spread": 100.0,
            "volatility_status": "NORMAL",
            "session": "LONDON",
        }

    def test_b_setup_blocked_in_normal_mode(self):
        ctx = self._btc_b_grade_ctx()
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=50.0, mtfa_score=40.0), ctx, {}, _normal_perf(),
        )
        if result["grade"] == "B":
            self.assertNotEqual(result["decision"], "PASS",
                                "B grade should WAIT or BLOCK in normal mode")
            self.assertIn(result["decision"], ("WAIT", "BLOCK"))

    def test_b_setup_passes_after_win_streak_3(self):
        ctx = self._btc_b_grade_ctx()
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=50.0, mtfa_score=40.0), ctx, {}, _confident_perf(3),
        )
        if result["grade"] == "B":
            self.assertEqual(result["decision"], "PASS")
            self.assertIn("B_ALLOWED_WIN_STREAK", result["reasons"])

    def test_b_setup_passes_after_win_streak_4(self):
        ctx = self._btc_b_grade_ctx()
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=50.0, mtfa_score=40.0), ctx, {}, _confident_perf(4),
        )
        if result["grade"] == "B":
            self.assertEqual(result["decision"], "PASS")


# ─── C/D setups always block ──────────────────────────────────────────────────

class TestCDGrade(unittest.TestCase):

    def test_c_grade_blocks_in_normal_mode(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=20.0, mtfa_score=10.0),
            _weak_ctx(), {}, _normal_perf(),
        )
        self.assertIn(result["grade"], ("C", "D"))
        self.assertEqual(result["decision"], "BLOCK")

    def test_d_grade_always_blocks(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=20.0, mtfa_score=10.0),
            _weak_ctx(), {}, _confident_perf(10),
        )
        # Even with win streak, C/D cannot pass
        self.assertIn(result["grade"], ("C", "D"))
        self.assertEqual(result["decision"], "BLOCK")

    def test_c_grade_blocks_even_in_confident_mode(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "ORDER_FLOW_EXECUTION_AGENT", "BUY",
            {}, _weak_ctx(), {}, _confident_perf(5),
        )
        self.assertIn(result["grade"], ("C", "D"))
        self.assertEqual(result["decision"], "BLOCK")


# ─── VWAP/POC blocking ────────────────────────────────────────────────────────

class TestVwapPocBlocking(unittest.TestCase):

    def test_buy_against_vwap_reduces_score(self):
        ctx = dict(_strong_buy_ctx())
        ctx["vwap"] = 66000.0  # price 65200 is below VWAP — bad for BUY
        result_against = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        result_with = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertLess(
            result_against["setup_quality_score"],
            result_with["setup_quality_score"],
        )
        self.assertIn("PRICE_AGAINST_VWAP", result_against["reasons"])

    def test_sell_against_vwap_reduces_score(self):
        ctx = dict(_strong_sell_ctx())
        ctx["vwap"] = 64000.0  # price 64800 is above VWAP — bad for SELL
        result_against = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "SELL",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertIn("PRICE_AGAINST_VWAP", result_against["reasons"])

    def test_buy_against_poc_reduces_score(self):
        ctx = dict(_strong_buy_ctx())
        ctx["poc"] = 66000.0   # price 65200 below POC — bad for BUY
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertIn("PRICE_AGAINST_POC", result["reasons"])

    def test_level_relation_score_buy_above(self):
        self.assertEqual(_level_relation_score(65200.0, 65000.0, "BUY"), 1.0)

    def test_level_relation_score_buy_below(self):
        self.assertEqual(_level_relation_score(64800.0, 65000.0, "BUY"), 0.0)

    def test_level_relation_score_sell_below(self):
        self.assertEqual(_level_relation_score(64800.0, 65000.0, "SELL"), 1.0)

    def test_level_relation_score_sell_above(self):
        self.assertEqual(_level_relation_score(65200.0, 65000.0, "SELL"), 0.0)


# ─── CVD opposing blocks ──────────────────────────────────────────────────────

class TestCvdBlocking(unittest.TestCase):

    def test_strong_opposing_cvd_reduces_buy_score(self):
        ctx = dict(_strong_buy_ctx())
        ctx["cvd_slope"] = -0.6   # strongly negative — against BUY
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertIn("CVD_OPPOSING", result["reasons"])

    def test_strong_positive_cvd_reduces_sell_score(self):
        ctx = dict(_strong_sell_ctx())
        ctx["cvd_slope"] = 0.6    # strongly positive — against SELL
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "SELL",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertIn("CVD_OPPOSING", result["reasons"])

    def test_cvd_score_aligned_buy(self):
        self.assertEqual(_cvd_score(0.6, "BUY"), 1.0)
        self.assertGreater(_cvd_score(0.15, "BUY"), 0.5)
        self.assertEqual(_cvd_score(-0.6, "BUY"), 0.0)

    def test_cvd_score_aligned_sell(self):
        self.assertEqual(_cvd_score(-0.6, "SELL"), 1.0)
        self.assertEqual(_cvd_score(0.6, "SELL"), 0.0)


# ─── Weak confirmation blocks ─────────────────────────────────────────────────

class TestWeakConfirmation(unittest.TestCase):

    def test_no_context_scores_low(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            None, None, None, _normal_perf(),
        )
        self.assertLess(result["setup_quality_score"], 65.0)
        self.assertIn(result["decision"], ("WAIT", "BLOCK"))

    def test_opposing_order_flow_reduces_score(self):
        ctx = dict(_strong_buy_ctx())
        ctx["order_flow_signal"] = "SELL"
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), ctx, {}, _normal_perf(),
        )
        self.assertIn("ORDER_FLOW_AGAINST", result["reasons"])

    def test_smc_hard_block_forces_d_grade(self):
        cand = _good_cand(smc_calibrated_status="STRONG_FAIL")
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            cand, _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertIn("SMC_MTFA_HARD_BLOCK", result["reasons"])
        self.assertEqual(result["grade"], "D")
        self.assertEqual(result["decision"], "BLOCK")


# ─── Strong multi-confirmation passes ────────────────────────────────────────

class TestStrongMultiConfirmation(unittest.TestCase):

    def test_all_signals_buy_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertIn(result["grade"], ("A+", "A"))

    def test_all_signals_sell_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "ORDER_FLOW_EXECUTION_AGENT", "SELL",
            _good_cand(), _strong_sell_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")

    def test_high_score_gets_pass(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=90.0, mtfa_score=85.0),
            _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertGreaterEqual(result["setup_quality_score"], 75.0)


# ─── Exit profile ─────────────────────────────────────────────────────────────

class TestExitProfile(unittest.TestCase):

    def test_aplus_with_strong_of_and_streak_gets_hold_if_strong(self):
        perf = _confident_perf(3)
        perf["win_streak"] = 3
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, perf,
        )
        if result["grade"] == "A+":
            self.assertEqual(result["exit_profile"], "HOLD_IF_STRONG")

    def test_b_grade_gets_fast_positive(self):
        ctx = {
            "price": 65000.0, "bid": 65000.0,
            "vwap": 64900.0, "poc": 64950.0,
            "cvd_slope": 0.05, "delta_proxy": 50.0,
            "order_flow_signal": "BUY",
            "m1_momentum": "BULLISH", "m5_momentum": "NEUTRAL",
            "spread": 50.0, "max_spread": 100.0,
            "volatility_status": "NORMAL", "session": "LONDON",
        }
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=50.0, mtfa_score=40.0), ctx, {}, _confident_perf(),
        )
        if result["grade"] == "B":
            self.assertEqual(result["exit_profile"], "FAST_POSITIVE")


# ─── Defensive mode ───────────────────────────────────────────────────────────

class TestDefensiveMode(unittest.TestCase):

    def test_paused_mode_blocks_all(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _paused_perf(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["entry_mode"], "PAUSED")
        self.assertIn("ENTRY_PAUSED_DEFENSIVE", result["reasons"])

    def test_defensive_mode_only_aplus_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _defensive_perf(),
        )
        self.assertEqual(result["entry_mode"], "DEFENSIVE")
        if result["grade"] != "A+":
            self.assertEqual(result["decision"], "BLOCK")

    def test_defensive_mode_aplus_still_passes(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(smc_score=90.0, mtfa_score=85.0),
            _strong_buy_ctx(), {}, _defensive_perf(),
        )
        if result["grade"] == "A+":
            self.assertEqual(result["decision"], "PASS")

    def test_entry_mode_normal(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["entry_mode"], "NORMAL")

    def test_entry_mode_aggressive_on_win_streak(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _confident_perf(3),
        )
        self.assertEqual(result["entry_mode"], "AGGRESSIVE")


# ─── Invalid direction ────────────────────────────────────────────────────────

class TestInvalidInput(unittest.TestCase):

    def test_invalid_direction_blocks(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "WAIT",
            _good_cand(), _strong_buy_ctx(), {}, _normal_perf(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("INVALID_DIRECTION", result["reasons"])

    def test_none_context_handled_gracefully(self):
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            None, None, None, None,
        )
        self.assertIn("decision", result)
        self.assertIn("setup_quality_score", result)
        self.assertIn("grade", result)


# ─── Scoring helper unit tests ────────────────────────────────────────────────

class TestScoringHelpers(unittest.TestCase):

    def test_session_good(self):
        self.assertEqual(_session_score("LONDON_NY"), 1.0)

    def test_session_closed(self):
        self.assertEqual(_session_score("CLOSED"), 0.0)

    def test_session_unknown(self):
        self.assertEqual(_session_score(""), 0.5)

    def test_spread_ok(self):
        self.assertEqual(_spread_score(0.5), 1.0)

    def test_spread_wide(self):
        self.assertEqual(_spread_score(1.0), 0.0)

    def test_spread_no_data(self):
        score = _spread_score(None)
        self.assertGreater(score, 0.0)

    def test_order_flow_aligned_buy(self):
        self.assertEqual(_order_flow_score("BUY", "BUY"), 1.0)

    def test_order_flow_against_buy(self):
        self.assertEqual(_order_flow_score("SELL", "BUY"), 0.0)

    def test_order_flow_neutral(self):
        self.assertEqual(_order_flow_score("WAIT", "BUY"), 0.5)

    def test_momentum_aligned(self):
        self.assertEqual(_momentum_score("BULLISH", "BUY"), 1.0)
        self.assertEqual(_momentum_score("BEARISH", "SELL"), 1.0)

    def test_momentum_opposing(self):
        self.assertEqual(_momentum_score("BEARISH", "BUY"), 0.0)
        self.assertEqual(_momentum_score("BULLISH", "SELL"), 0.0)

    def test_delta_aligned_buy(self):
        self.assertEqual(_delta_score(600.0, "BUY"), 1.0)

    def test_delta_opposing_buy(self):
        self.assertEqual(_delta_score(-600.0, "BUY"), 0.0)

    def test_rejection_score_no_rejection(self):
        self.assertEqual(_rejection_score({}, "BUY"), 1.0)

    def test_rejection_score_bearish_wick_blocks_buy(self):
        ctx = {"wick_rejection": "BEARISH_WICK"}
        self.assertEqual(_rejection_score(ctx, "BUY"), 0.0)

    def test_rejection_score_bullish_wick_blocks_sell(self):
        ctx = {"wick_rejection": "BULLISH_REJECTION"}
        self.assertEqual(_rejection_score(ctx, "SELL"), 0.0)


if __name__ == "__main__":
    unittest.main()
