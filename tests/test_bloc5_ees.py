"""BLOC 5 — EES (Entry Exhaustion Score) tests.

Proves:
- Graduated bands: SAIN < 40 (no effect), PRUDENCE 40-65 (max -15 penalty),
  EXTREME >= 65 (EES_EXTREME_BLOCK).
- BUY and SELL guards are exact mirrors (same code, sign flip): a mirrored
  price series produces the identical score.
- Plumbing evaluate -> _candidate: candidates carry side / ees_score /
  ees_band / ees_buy / ees_sell and EXTREME appends EES_EXTREME_BLOCK.
"""
from __future__ import annotations

import unittest

from app.agents.ees import (
    BAND_EXTREME,
    BAND_PRUDENCE,
    BAND_SAIN,
    compute_ees,
    ees_band,
    ees_penalty,
)
from app.agents.setup_hunter import SetupHunter
from app.config import Settings


def _flat_candles(n: int = 25) -> list[dict]:
    rows = []
    for i in range(n):
        drift = 0.1 if i % 2 == 0 else -0.1
        open_ = 100.0
        close = 100.0 + drift
        rows.append({"open": open_, "high": max(open_, close) + 0.4, "low": min(open_, close) - 0.4, "close": close})
    return rows


def _parabolic_up(n: int = 25) -> list[dict]:
    rows = []
    price = 100.0
    for i in range(n - 8):
        open_ = price
        close = open_ + 0.2
        rows.append({"open": open_, "high": close + 0.2, "low": open_ - 0.2, "close": close})
        price = close
    for i in range(8):
        open_ = price
        close = open_ + 2.0
        rows.append({"open": open_, "high": close + 0.3, "low": open_ - 0.1, "close": close})
        price = close
    return rows


def _mirror(rows: list[dict], pivot: float = 200.0) -> list[dict]:
    return [
        {
            "open": pivot - r["open"],
            "high": pivot - r["low"],
            "low": pivot - r["high"],
            "close": pivot - r["close"],
        }
        for r in rows
    ]


class TestEesEngine(unittest.TestCase):
    def test_flat_market_is_sain(self) -> None:
        result = compute_ees("BUY", _flat_candles())
        self.assertEqual(result["band"], BAND_SAIN)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["penalty"], 0.0)

    def test_parabolic_buy_is_extreme_blocked(self) -> None:
        result = compute_ees("BUY", _parabolic_up())
        self.assertGreaterEqual(result["score"], 65.0)
        self.assertEqual(result["band"], BAND_EXTREME)
        self.assertTrue(result["blocked"])

    def test_sell_mirror_is_byte_identical(self) -> None:
        buy = compute_ees("BUY", _parabolic_up())
        sell = compute_ees("SELL", _mirror(_parabolic_up()))
        self.assertEqual(buy["score"], sell["score"])
        self.assertEqual(buy["band"], sell["band"])
        self.assertEqual(buy["penalty"], sell["penalty"])
        self.assertEqual(buy["blocked"], sell["blocked"])

    def test_buy_not_penalized_for_a_dump(self) -> None:
        # a parabolic DUMP is exhaustion for SELL entries, not BUY entries
        dump = _mirror(_parabolic_up())
        buy = compute_ees("BUY", dump)
        self.assertEqual(buy["band"], BAND_SAIN)

    def test_insufficient_data_is_neutral(self) -> None:
        result = compute_ees("BUY", _flat_candles(5))
        self.assertIsNone(result["score"])
        self.assertEqual(result["band"], BAND_SAIN)
        self.assertFalse(result["blocked"])

    def test_wait_direction_not_applicable(self) -> None:
        result = compute_ees("WAIT", _parabolic_up())
        self.assertIsNone(result["score"])

    def test_of_divergence_raises_score(self) -> None:
        base = compute_ees("BUY", _parabolic_up())
        with_div = compute_ees(
            "BUY", _parabolic_up(),
            {"order_flow_execution_agent": {"divergence": "bear", "cvd_slope": -1.0}},
        )
        self.assertGreaterEqual(with_div["score"], base["score"])


class TestEesBands(unittest.TestCase):
    def test_band_thresholds(self) -> None:
        self.assertEqual(ees_band(39.9), BAND_SAIN)
        self.assertEqual(ees_band(40.0), BAND_PRUDENCE)
        self.assertEqual(ees_band(64.9), BAND_PRUDENCE)
        self.assertEqual(ees_band(65.0), BAND_EXTREME)
        self.assertEqual(ees_band(None), BAND_SAIN)

    def test_penalty_graduated_max_15(self) -> None:
        self.assertEqual(ees_penalty(39.0), 0.0)
        self.assertEqual(ees_penalty(40.0), 0.0)
        self.assertAlmostEqual(ees_penalty(52.5), -7.5)
        self.assertGreaterEqual(ees_penalty(64.9), -15.0)
        self.assertEqual(ees_penalty(65.0), 0.0)  # EXTREME blocks instead
        self.assertEqual(ees_penalty(None), 0.0)


class TestSetupHunterPlumbing(unittest.TestCase):
    def _signal(self, direction: str) -> dict:
        return {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "setup_type": "ORDER_FLOW_EXECUTION_AGENT",
            "symbol": "GOLD",
            "signal": direction,
            "direction": direction,
            "confidence": 80.0,
            "entry": 100.0,
            "sl": 98.0 if direction == "BUY" else 102.0,
            "tp": 104.0 if direction == "BUY" else 96.0,
            "risk_reward": 2.0,
            "reward_risk": 2.0,
            "safety_guard_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
        }

    def _time_gate(self) -> dict:
        return {
            "time_gate_status": "PASS",
            "session_name": "LONDON",
            "symbol_market_open": True,
            "market_open": True,
        }

    def _run(self, direction: str, candles: list[dict]):
        hunter = SetupHunter(Settings(order_flow_execution_enabled=True))
        analysis = {"ai_decision": {}, "strategy_signals": [self._signal(direction)]}
        result = hunter.evaluate("GOLD", "GOLD#", analysis, self._time_gate(), 10, 50, recent_candles=candles)
        return next(
            c for c in result.candidates
            if str(c.get("best_strategy") or c.get("strategy") or "") == "ORDER_FLOW_EXECUTION_AGENT"
        )

    def test_buy_candidate_extreme_blocked_with_observability(self) -> None:
        candidate = self._run("BUY", _parabolic_up())
        self.assertEqual(candidate["side"], "BUY")
        self.assertEqual(candidate["ees_band"], BAND_EXTREME)
        self.assertIsNotNone(candidate["ees_buy"])
        self.assertIsNone(candidate["ees_sell"])
        self.assertIn("EES_EXTREME_BLOCK", candidate.get("failed_gates") or [])

    def test_sell_candidate_mirror_blocked(self) -> None:
        candidate = self._run("SELL", _mirror(_parabolic_up()))
        self.assertEqual(candidate["side"], "SELL")
        self.assertEqual(candidate["ees_band"], BAND_EXTREME)
        self.assertIsNotNone(candidate["ees_sell"])
        self.assertIsNone(candidate["ees_buy"])
        self.assertIn("EES_EXTREME_BLOCK", candidate.get("failed_gates") or [])

    def test_sain_candidate_not_blocked(self) -> None:
        candidate = self._run("BUY", _flat_candles())
        self.assertEqual(candidate["ees_band"], BAND_SAIN)
        self.assertNotIn("EES_EXTREME_BLOCK", candidate.get("failed_gates") or [])

    def test_buy_and_sell_scores_symmetric_through_hunter(self) -> None:
        buy = self._run("BUY", _parabolic_up())
        sell = self._run("SELL", _mirror(_parabolic_up()))
        self.assertEqual(buy["ees_score"], sell["ees_score"])


if __name__ == "__main__":
    unittest.main()
