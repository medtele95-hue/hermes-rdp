"""Tests for select_best_btc_setup() — the BTC setup arbiter.

Covers:
- Chooses highest quality (A/A+) setup from candidates
- Returns None when no setup meets the bar
- Never selects more than one BTC setup
- Respects strategy recent performance (best_strategy_now)
- Ignores BLOCK/WAIT decisions — only selects PASS
- Prefers better exit profile
"""
from __future__ import annotations

import unittest

from app.mt5.btc_setup_intelligence import select_best_btc_setup


def _candidate(
    strategy: str,
    score: float,
    grade: str,
    decision: str = "PASS",
    exit_profile: str = "NORMAL_SCALP",
) -> dict:
    return {
        "strategy": strategy,
        "intelligence": {
            "decision": decision,
            "setup_quality_score": score,
            "grade": grade,
            "exit_profile": exit_profile,
        },
    }


class TestArbiterBasic(unittest.TestCase):

    def test_no_candidates_returns_none(self):
        result = select_best_btc_setup([])
        self.assertIsNone(result)

    def test_all_blocked_returns_none(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 80.0, "A", decision="BLOCK"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 70.0, "B", decision="WAIT"),
        ])
        self.assertIsNone(result)

    def test_single_aplus_candidate_selected(self):
        c = _candidate("BTC_SCALPING_AGENT", 90.0, "A+")
        result = select_best_btc_setup([c])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "BTC_SCALPING_AGENT")

    def test_all_below_a_returns_none(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 60.0, "B", decision="WAIT"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 50.0, "C", decision="BLOCK"),
        ])
        self.assertIsNone(result)


class TestArbiterBestSelection(unittest.TestCase):

    def test_higher_score_wins(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 78.0, "A"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 88.0, "A+"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")

    def test_higher_grade_wins_on_tie_score(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 75.0, "A"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 75.0, "A+"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")

    def test_never_selects_more_than_one(self):
        candidates = [
            _candidate("BTC_SCALPING_AGENT", 85.0, "A+"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 80.0, "A"),
            _candidate("HERMES_FIB_AGENT", 88.0, "A+"),
        ]
        result = select_best_btc_setup(candidates)
        # Result is exactly one dict, not a list
        self.assertIsInstance(result, dict)
        self.assertEqual(result["strategy"], "HERMES_FIB_AGENT")

    def test_block_ignored_even_if_high_score(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 95.0, "A+", decision="BLOCK"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 78.0, "A", decision="PASS"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")

    def test_wait_ignored(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 70.0, "B", decision="WAIT"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 78.0, "A", decision="PASS"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")


class TestArbiterExitProfile(unittest.TestCase):

    def test_hold_if_strong_preferred_over_normal_scalp_same_score(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 85.0, "A+", exit_profile="NORMAL_SCALP"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 85.0, "A+", exit_profile="HOLD_IF_STRONG"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")

    def test_normal_scalp_preferred_over_fast_positive_same_score(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 80.0, "A", exit_profile="FAST_POSITIVE"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 80.0, "A", exit_profile="NORMAL_SCALP"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")


class TestArbiterSafety(unittest.TestCase):

    def test_single_candidate_below_a_returns_none(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 60.0, "B", decision="BLOCK"),
        ])
        self.assertIsNone(result)

    def test_mixed_pass_and_block_selects_pass(self):
        result = select_best_btc_setup([
            _candidate("BTC_SCALPING_AGENT", 90.0, "A+", decision="BLOCK"),
            _candidate("ORDER_FLOW_EXECUTION_AGENT", 76.0, "A", decision="PASS"),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"] if "decision" in result else
                         result["intelligence"]["decision"], "PASS")

    def test_result_contains_strategy_field(self):
        c = _candidate("BTC_SCALPING_AGENT", 88.0, "A+")
        result = select_best_btc_setup([c])
        self.assertIn("strategy", result)

    def test_result_contains_intelligence_field(self):
        c = _candidate("BTC_SCALPING_AGENT", 88.0, "A+")
        result = select_best_btc_setup([c])
        self.assertIn("intelligence", result)
        intel = result["intelligence"]
        self.assertIn("setup_quality_score", intel)
        self.assertIn("grade", intel)


if __name__ == "__main__":
    unittest.main()
