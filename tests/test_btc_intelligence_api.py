"""Tests for the /local-api/btc-intelligence endpoint and BTC intelligence state.

Covers:
- Endpoint returns expected fields
- State update and retrieval works
- Safety proof present
- No order_send in server.py for this endpoint
- No execution logic in btc_setup_intelligence or btc_performance_memory
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.local_api.state import _LocalState


class TestBtcIntelligenceState(unittest.TestCase):

    def setUp(self):
        self.state = _LocalState()

    def test_initial_btc_intelligence_has_expected_keys(self):
        intel = self.state.get_btc_intelligence()
        expected_keys = {
            "current_mode", "win_streak", "loss_streak", "positive_exit_rate",
            "best_strategy_now", "worst_strategy_now", "last_setup_score",
            "last_setup_grade", "last_block_reason", "current_exit_profile",
            "temporary_block_rules", "smart_exit_status", "fast_exit_status",
            "pause_remaining_minutes", "requires_aplus",
        }
        self.assertTrue(expected_keys.issubset(set(intel.keys())))

    def test_update_btc_intelligence_merges(self):
        self.state.update_btc_intelligence({"current_mode": "CONFIDENT", "win_streak": 3})
        intel = self.state.get_btc_intelligence()
        self.assertEqual(intel["current_mode"], "CONFIDENT")
        self.assertEqual(intel["win_streak"], 3)
        # Other fields still present
        self.assertIn("loss_streak", intel)

    def test_update_btc_intelligence_partial_update(self):
        self.state.update_btc_intelligence({"last_setup_grade": "A+"})
        intel = self.state.get_btc_intelligence()
        self.assertEqual(intel["last_setup_grade"], "A+")
        self.assertIn("win_streak", intel)   # not overwritten

    def test_btc_intelligence_returns_copy(self):
        intel1 = self.state.get_btc_intelligence()
        intel1["win_streak"] = 999
        intel2 = self.state.get_btc_intelligence()
        self.assertNotEqual(intel2["win_streak"], 999)


class TestBtcIntelligenceEndpoint(unittest.TestCase):

    def test_endpoint_exists_in_server(self):
        server_src = Path("app/local_api/server.py").read_text(encoding="utf-8")
        self.assertIn("/local-api/btc-intelligence", server_src)
        self.assertIn("def btc_intelligence(", server_src)

    def test_endpoint_reads_state_no_mt5_calls(self):
        """The btc-intelligence endpoint must not call mt5 directly."""
        import re
        server_src = Path("app/local_api/server.py").read_text(encoding="utf-8")
        # Isolate the btc_intelligence function
        match = re.search(
            r"def btc_intelligence\(\).*?(?=\n@app\.|\nclass |\Z)",
            server_src, re.DOTALL,
        )
        self.assertIsNotNone(match, "btc_intelligence function not found")
        fn_body = match.group(0)
        self.assertNotIn("mt5.order_send", fn_body)
        self.assertNotIn("mt5.positions_get", fn_body)

    def test_btc_intelligence_endpoint_has_safety_proof(self):
        """Endpoint must expose safety_proof dict."""
        server_src = Path("app/local_api/server.py").read_text(encoding="utf-8")
        self.assertIn("safety_proof", server_src)
        self.assertIn("allow_live_trading", server_src)
        self.assertIn("max_lot", server_src)

    def test_no_order_send_in_btc_intelligence_engine(self):
        src = Path("app/mt5/btc_setup_intelligence.py").read_text(encoding="utf-8")
        self.assertNotIn("order_send", src)

    def test_no_order_send_in_performance_memory(self):
        src = Path("app/mt5/btc_performance_memory.py").read_text(encoding="utf-8")
        self.assertNotIn("order_send", src)

    def test_no_mt5_calls_in_setup_intelligence(self):
        src = Path("app/mt5/btc_setup_intelligence.py").read_text(encoding="utf-8")
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("mt5.positions_get", src)
        self.assertNotIn("mt5.order_send", src)

    def test_no_mt5_calls_in_performance_memory(self):
        src = Path("app/mt5/btc_performance_memory.py").read_text(encoding="utf-8")
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("mt5.order_send", src)


class TestBtcIntelligenceFields(unittest.TestCase):
    """Verify intelligence result structure from the engine."""

    def _eval(self, perf=None, ctx=None, cand=None):
        from app.mt5.btc_setup_intelligence import evaluate_btc_setup_intelligence
        return evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            cand or {"smc_score": 80.0, "mtfa_score": 75.0, "rr": 2.0},
            ctx or {
                "price": 65200.0, "bid": 65200.0,
                "vwap": 65000.0, "poc": 64900.0,
                "cvd_slope": 0.6, "delta_proxy": 800.0,
                "order_flow_signal": "BUY",
                "m1_momentum": "BULLISH", "m5_momentum": "BULLISH",
                "spread": 30.0, "max_spread": 100.0,
                "volatility_status": "NORMAL", "session": "LONDON_NY",
            },
            {},
            perf or {"adaptive_mode": "NORMAL", "is_paused": False,
                     "win_streak": 0, "loss_streak": 0, "requires_aplus": False},
        )

    def test_result_has_decision(self):
        r = self._eval()
        self.assertIn("decision", r)
        self.assertIn(r["decision"], ("PASS", "BLOCK", "WAIT"))

    def test_result_has_setup_quality_score(self):
        r = self._eval()
        self.assertIn("setup_quality_score", r)
        score = r["setup_quality_score"]
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_result_has_grade(self):
        r = self._eval()
        self.assertIn("grade", r)
        self.assertIn(r["grade"], ("A+", "A", "B", "C", "D"))

    def test_result_has_reasons_list(self):
        r = self._eval()
        self.assertIn("reasons", r)
        self.assertIsInstance(r["reasons"], list)

    def test_result_has_entry_mode(self):
        r = self._eval()
        self.assertIn("entry_mode", r)
        self.assertIn(r["entry_mode"], ("AGGRESSIVE", "NORMAL", "DEFENSIVE", "PAUSED"))

    def test_result_has_confidence_multiplier(self):
        r = self._eval()
        self.assertIn("confidence_multiplier", r)
        self.assertIsInstance(r["confidence_multiplier"], float)

    def test_result_has_exit_profile(self):
        r = self._eval()
        self.assertIn("exit_profile", r)
        self.assertIn(r["exit_profile"], ("FAST_POSITIVE", "NORMAL_SCALP", "HOLD_IF_STRONG"))

    def test_aplus_multiplier_is_1_2(self):
        r = self._eval()
        if r["grade"] == "A+":
            self.assertAlmostEqual(r["confidence_multiplier"], 1.2)

    def test_a_multiplier_is_1_0(self):
        from app.mt5.btc_setup_intelligence import evaluate_btc_setup_intelligence
        ctx = {
            "price": 65000.0, "bid": 65000.0,
            "vwap": 64900.0, "poc": 64950.0,
            "cvd_slope": 0.2, "delta_proxy": 200.0,
            "order_flow_signal": "BUY",
            "m1_momentum": "BULLISH", "m5_momentum": "NEUTRAL",
            "spread": 40.0, "max_spread": 100.0,
            "volatility_status": "NORMAL", "session": "LONDON",
        }
        r = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            {"smc_score": 75.0, "mtfa_score": 65.0, "rr": 2.0},
            ctx, {},
            {"adaptive_mode": "NORMAL", "is_paused": False,
             "win_streak": 0, "loss_streak": 0, "requires_aplus": False},
        )
        if r["grade"] == "A":
            self.assertAlmostEqual(r["confidence_multiplier"], 1.0)


if __name__ == "__main__":
    unittest.main()
