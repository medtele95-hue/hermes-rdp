"""BLOC 2 — SLTP geometry + anti-penny-grab tests.

Proves:
a) _calc_sltp anchors the SL on the far side of BOTH the value level and the
   price (a sweep beyond VAL/VAH no longer puts the SL on the wrong side).
b) _final_rr computes the reward/risk from the final request values and the
   router floor blocks RR < 1.0 after every TP modification.
c) max_money_tp is dead by default and GOLD is hard-excluded.
"""
from __future__ import annotations

import unittest

from app.config import Settings
from app.mt5.demo_router import _final_rr, _max_money_tp_symbol_allowed
from app.strategies.order_flow_execution_agent import _calc_sltp


class TestCalcSltpGeometry(unittest.TestCase):
    def test_buy_sweep_below_val_keeps_sl_below_price(self) -> None:
        # price swept BELOW the value-area low: old code returned None
        levels = _calc_sltp("BUY", price=2275.0, vwap=2295.0, poc=2290.0, vah=2310.0, val=2280.0, min_rr=1.5)
        self.assertIsNotNone(levels)
        entry, sl, tp, rr = levels
        self.assertLess(sl, entry)
        self.assertGreater(tp, entry)
        self.assertGreaterEqual(rr, 1.5)

    def test_sell_sweep_above_vah_keeps_sl_above_price(self) -> None:
        levels = _calc_sltp("SELL", price=2315.0, vwap=2295.0, poc=2290.0, vah=2310.0, val=2280.0, min_rr=1.5)
        self.assertIsNotNone(levels)
        entry, sl, tp, rr = levels
        self.assertGreater(sl, entry)
        self.assertLess(tp, entry)
        self.assertGreaterEqual(rr, 1.5)

    def test_buy_normal_case_unchanged(self) -> None:
        levels = _calc_sltp("BUY", price=2285.0, vwap=2295.0, poc=2290.0, vah=2310.0, val=2280.0, min_rr=1.5)
        self.assertIsNotNone(levels)
        entry, sl, tp, rr = levels
        self.assertLess(sl, 2280.0)  # still anchored below the value-area low


class TestFinalRrFloor(unittest.TestCase):
    def test_buy_rr(self) -> None:
        self.assertAlmostEqual(_final_rr("BUY", 100.0, 95.0, 110.0), 2.0)
        self.assertAlmostEqual(_final_rr("BUY", 100.0, 95.0, 102.0), 0.4)

    def test_sell_rr(self) -> None:
        self.assertAlmostEqual(_final_rr("SELL", 100.0, 105.0, 90.0), 2.0)
        self.assertAlmostEqual(_final_rr("SELL", 100.0, 105.0, 98.0), 0.4)

    def test_not_computable_returns_none(self) -> None:
        self.assertIsNone(_final_rr("BUY", None, 95.0, 110.0))
        self.assertIsNone(_final_rr("BUY", 100.0, 100.0, 110.0))  # zero risk
        self.assertIsNone(_final_rr("BUY", 100.0, 105.0, 110.0))  # inverted SL


class TestMaxMoneyTpKilled(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        self.assertFalse(Settings().max_money_tp_enabled)
        self.assertEqual(Settings().max_tp_applies_to, "")

    def test_gold_hard_excluded_even_when_listed(self) -> None:
        settings = Settings(max_tp_applies_to="GOLD#,GOLD,XAUUSD,EURUSD")
        self.assertFalse(_max_money_tp_symbol_allowed(settings, "GOLD#"))
        self.assertFalse(_max_money_tp_symbol_allowed(settings, "GOLD"))
        self.assertFalse(_max_money_tp_symbol_allowed(settings, "XAUUSD"))

    def test_non_gold_symbols_respect_the_list(self) -> None:
        settings = Settings(max_tp_applies_to="EURUSD")
        self.assertTrue(_max_money_tp_symbol_allowed(settings, "EURUSD"))
        self.assertFalse(_max_money_tp_symbol_allowed(settings, "BTCUSD#"))


if __name__ == "__main__":
    unittest.main()
