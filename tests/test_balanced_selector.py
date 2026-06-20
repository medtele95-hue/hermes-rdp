from __future__ import annotations

import logging
import unittest

from app.services.balanced_selector import select_best_candidate
from app.strategies.hermes_strategy_pack_agent import NAME, build_candidate
from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES, CONFIRMATION_MODULE_STRATEGIES, strategy_class, StrategyClass


def candidate(strategy: str = "ORDER_FLOW_EXECUTION_AGENT", symbol: str = "EURUSD", **overrides) -> dict:
    data = {
        "strategy": strategy,
        "best_strategy": strategy,
        "symbol": symbol,
        "broker_symbol": symbol,
        "direction": "BUY",
        "entry": 1.1000,
        "sl": 1.0950,
        "tp": 1.1100,
        "rr": 2.0,
        "confidence": 82.0,
        "grade": "B",
        "final_confluence_score": 60.0,
        "final_confluence_grade": "C",
        "time_gate_status": "PASS",
        "safety_guard_status": "PASS",
        "market_open": True,
        "spread_ok": True,
        "mode": "ACTIVE_EXECUTION",
        "route_allowed": True,
        "demo_eligible": True,
    }
    data.update(overrides)
    return data


class BalancedSelectorTests(unittest.TestCase):
    def test_confirmation_modules_do_not_become_active_directly(self) -> None:
        for strategy in ("BREAKOUT_RETEST", "TREND_CONTINUATION_BREAKDOWN", "QUANT_PRO_REGIME_SWITCHING"):
            self.assertIn(strategy, CONFIRMATION_MODULE_STRATEGIES)
            self.assertEqual(strategy_class(strategy), StrategyClass.CONFIRMATION_MODULE)
            self.assertNotIn(strategy, ACTIVE_EXECUTION_STRATEGIES)

    def test_hermes_strategy_pack_wraps_valid_internal_signal(self) -> None:
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

    def test_hermes_strategy_pack_rejects_incomplete_internal_signal(self) -> None:
        out = build_candidate("BTCUSD#", "BTCUSD#", [{"strategy": "BREAKOUT_RETEST", "signal": "BUY", "confidence": 90}])
        self.assertIsNone(out)

    def test_balanced_selector_picks_best_candidate(self) -> None:
        weak = candidate("HERMES_STRATEGY_PACK_AGENT", "BTCUSD#", confidence=78, grade="B", final_confluence_score=58)
        strong = candidate("ORDER_FLOW_EXECUTION_AGENT", "GOLD#", confidence=88, grade="A", final_confluence_score=70)
        result = select_best_candidate([weak, strong])
        self.assertEqual(result.best["strategy"], "ORDER_FLOW_EXECUTION_AGENT")
        self.assertEqual(len(result.accepted), 2)

    def test_balanced_selector_blocks_confluence_d(self) -> None:
        result = select_best_candidate([candidate(final_confluence_score=40, final_confluence_grade="D")])
        self.assertIsNone(result.best)
        self.assertEqual(result.rejected[0]["balanced_selector_reason"], "FINAL_CONFLUENCE_TOO_LOW")

    def test_balanced_selector_prevents_btc_overtrading(self) -> None:
        result = select_best_candidate(
            [candidate("BTC_SCALPING_AGENT", "BTCUSD#", confidence=80)],
            open_trades=[{"symbol": "BTCUSD#", "strategy": "BTC_SCALPING_AGENT", "magic_number": 909002}],
            magic_number=909002,
        )
        self.assertIsNone(result.best)
        self.assertEqual(result.rejected[0]["balanced_selector_reason"], "MAX_BTC_EXPOSURE")

    def test_us100_can_participate_through_simo(self) -> None:
        result = select_best_candidate([candidate("SIMO_ATM_BREAKOUT", "US100Cash#", confidence=80)])
        self.assertEqual(result.best["strategy"], "SIMO_ATM_BREAKOUT")

    def test_balanced_selector_logs_none_when_no_routeable_candidate(self) -> None:
        blocked = candidate(final_confluence_score=40, final_confluence_grade="D", demo_eligible=False)
        with self.assertLogs("hermes", level=logging.INFO) as cm:
            result = select_best_candidate([blocked])
        self.assertIsNone(result.best)
        none_logs = [m for m in cm.output if "BALANCED_SELECTOR_BEST" in m and "symbol=NONE" in m]
        self.assertTrue(len(none_logs) >= 1, "Expected [BALANCED_SELECTOR_BEST] symbol=NONE log")

    def test_balanced_selector_best_logs_ranked_best_when_accepted(self) -> None:
        good = candidate("ORDER_FLOW_EXECUTION_AGENT", "EURUSD", confidence=85, final_confluence_score=68)
        with self.assertLogs("hermes", level=logging.INFO) as cm:
            result = select_best_candidate([good])
        self.assertIsNotNone(result.best)
        best_logs = [m for m in cm.output if "BALANCED_SELECTOR_BEST" in m and "reason=RANKED_BEST" in m]
        self.assertTrue(len(best_logs) >= 1, "Expected [BALANCED_SELECTOR_BEST] reason=RANKED_BEST log")


if __name__ == "__main__":
    unittest.main()
