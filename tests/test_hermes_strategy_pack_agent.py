from __future__ import annotations

import unittest

from app.strategies.hermes_strategy_pack_agent import NAME, build_candidate


class HermesStrategyPackAgentTests(unittest.TestCase):
    def test_hermes_pack_wraps_valid_internal_signal(self) -> None:
        out = build_candidate(
            "BTCUSD#",
            "BTCUSD#",
            [
                {
                    "strategy": "BREAKOUT_RETEST",
                    "signal": "BUY",
                    "entry": 100.0,
                    "sl": 98.0,
                    "tp": 104.0,
                    "risk_reward": 2.0,
                    "confidence": 0.82,
                    "grade": "B",
                    "final_confluence_score": 62,
                    "final_confluence_grade": "C",
                    "reason": "VALID_INTERNAL_TEST",
                }
            ],
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["strategy"], NAME)
        self.assertEqual(out["setup_type"], "BREAKOUT_RETEST")
        self.assertEqual(out["metadata"]["internal_strategy"], "BREAKOUT_RETEST")

    def test_hermes_pack_rejects_incomplete_internal_signal(self) -> None:
        out = build_candidate("BTCUSD#", "BTCUSD#", [{"strategy": "BREAKOUT_RETEST", "signal": "BUY", "confidence": 90}])
        self.assertIsNone(out)

    def test_hermes_pack_prefers_highest_scoring_internal_candidate(self) -> None:
        out = build_candidate(
            "BTCUSD#",
            "BTCUSD#",
            [
                {
                    "strategy": "BREAKOUT_RETEST",
                    "signal": "BUY",
                    "entry": 100.0,
                    "sl": 98.0,
                    "tp": 104.0,
                    "risk_reward": 2.0,
                    "confidence": 0.76,
                    "grade": "B",
                    "final_confluence_score": 60,
                    "final_confluence_grade": "C",
                },
                {
                    "strategy": "FIB_OTE_RETEST",
                    "signal": "BUY",
                    "entry": 100.0,
                    "sl": 97.0,
                    "tp": 106.0,
                    "risk_reward": 2.0,
                    "confidence": 0.90,
                    "grade": "A",
                    "final_confluence_score": 70,
                    "final_confluence_grade": "B",
                },
            ],
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["setup_type"], "FIB_OTE_RETEST")


if __name__ == "__main__":
    unittest.main()
