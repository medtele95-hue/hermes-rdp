from __future__ import annotations

import unittest

import pandas as pd

from app.config import Settings
from app.strategies.fib_confluence_agent import STRATEGY, evaluate


def _frame() -> pd.DataFrame:
    rows = []
    for i in range(90):
        base = 118.0 + i * 0.15
        open_ = base
        close = base + 0.10
        high = close + 0.20
        low = open_ - 0.20
        volume = 100
        rows.append([open_, high, low, close, volume, i])

    # Shape the last closed bar so it sits in the golden pocket with confirmation.
    rows[68][2] = 120.0
    rows[68][0] = 120.2
    rows[68][3] = 120.8
    rows[68][1] = 121.0

    rows[74][0] = 129.2
    rows[74][1] = 130.0
    rows[74][2] = 128.9
    rows[74][3] = 129.6

    rows[88][0] = 123.6
    rows[88][1] = 124.9
    rows[88][2] = 121.6
    rows[88][3] = 124.5
    rows[88][4] = 400

    rows[89][0] = 124.3
    rows[89][1] = 124.8
    rows[89][2] = 124.0
    rows[89][3] = 124.5

    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "tick_volume", "time"])


class FibConfluenceAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            fib_confluence_execution_enabled=True,
            fib_confluence_lookback=30,
            fib_confluence_struct_window=15,
            fib_confluence_vol_period=20,
            fib_confluence_vol_mult=1.5,
            fib_confluence_liq_lookback=10,
            fib_confluence_sl_atr_mult=0.5,
            fib_confluence_rr=2.0,
            fib_confluence_max_spread_atr_frac=0.15,
        )
        self.frames = {"M5": _frame()}
        self.context = {
            "spread": 0.05,
            "trade_stops_level": 1,
            "point": 0.01,
            "time_gate_status": "PASS",
            "time_gate_reason": "PASS",
            "market_open": True,
            "symbol_market_open": True,
            "safety_guard_status": "PASS",
            "safety_guard_reason": "PASS",
        }

    def test_disabled_returns_wait(self) -> None:
        out = evaluate("BTCUSD#", self.frames, self.context, Settings())
        self.assertEqual(out["strategy"], STRATEGY)
        self.assertEqual(out["signal"], "WAIT")
        self.assertEqual(out["reason"], "FIB_CONFLUENCE_EXECUTION_DISABLED")

    def test_btc_candidate_is_normalized_and_closed_bar_only(self) -> None:
        out = evaluate("BTCUSD#", self.frames, self.context, self.settings)
        self.assertEqual(out["strategy"], STRATEGY)
        self.assertEqual(out["symbol"], "BTCUSD")
        self.assertEqual(out["broker_symbol"], "BTCUSD#")
        self.assertEqual(out["signal"], "BUY")
        self.assertTrue(out["closed_bar_only"])
        self.assertTrue(out["route_allowed"])
        self.assertTrue(out["demo_eligible"])
        self.assertGreaterEqual(out["final_confluence_score"], 75)
        self.assertGreaterEqual(out["rr"], 1.5)
        self.assertLess(out["sl"], 120.0)
        self.assertGreater(out["tp"], out["entry"])
        self.assertTrue(out["fib_confluence"]["spread_ok"])

    def test_spread_filter_blocks_when_wide_vs_atr(self) -> None:
        context = {**self.context, "spread": 20.0}
        out = evaluate("BTCUSD#", self.frames, context, self.settings)
        self.assertEqual(out["signal"], "WAIT")
        self.assertEqual(out["reason"], "FIB_CONFLUENCE_SPREAD_TOO_WIDE")

    def test_min_stop_distance_influences_sl_buffer(self) -> None:
        context = {**self.context, "trade_stops_level": 2, "point": 1.0}
        settings = Settings(
            fib_confluence_execution_enabled=True,
            fib_confluence_lookback=30,
            fib_confluence_struct_window=15,
            fib_confluence_vol_period=20,
            fib_confluence_vol_mult=1.5,
            fib_confluence_liq_lookback=10,
            fib_confluence_sl_atr_mult=0.01,
            fib_confluence_rr=2.0,
            fib_confluence_max_spread_atr_frac=0.15,
        )
        out = evaluate("BTCUSD#", self.frames, context, settings)
        self.assertEqual(out["signal"], "BUY")
        self.assertAlmostEqual(out["fib_confluence"]["min_stop_distance"], 2.0)
        self.assertGreaterEqual(out["entry"] - out["sl"], 2.0)
        self.assertLess(out["sl"], 120.0)


if __name__ == "__main__":
    unittest.main()
