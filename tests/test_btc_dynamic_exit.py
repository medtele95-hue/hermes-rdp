"""Tests for Intelligence Upgrade v6 — HERMES Dynamic TP/SL (BtcDynamicExit).

Covers:
- compute_atr: normal (≥15 candles) and insufficient data (<15 candles)
- compute: dynamic mode (high/low confluence score), fallback (empty rates)
- compute: caps respected (tp_usd ≤ 6.0, sl_usd ≤ 3.0)
- compute: rr_target = tp_usd / sl_usd when caps not hit
- compute: CVD tighten on BUY + cvd_slope < -100
- adjust_on_tick: tighten trail_gap when profit > trail_start and CVD negative
- adjust_on_tick: no change when CVD is positive
"""
from __future__ import annotations

import unittest

from app.mt5.btc_dynamic_exit import BtcDynamicExit


def _make_rates(n: int = 20, base: float = 100.0, swing: float = 0.5) -> list[dict]:
    """Generate n OHLC dicts. swing controls high-low range → ATR ≈ swing*2."""
    rates = []
    for i in range(n):
        close = base + (i % 2) * swing
        rates.append({"high": close + swing, "low": close - swing, "close": close})
    return rates


def _make_rates_large(n: int = 20) -> list[dict]:
    """Candles with very large swings to force cap hits (ATR >> 2.0)."""
    rates = []
    for i in range(n):
        price = 60000.0 + (i % 2) * 500.0
        rates.append({"high": price + 200.0, "low": price - 200.0, "close": price})
    return rates


class TestComputeAtr(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcDynamicExit()

    def test_atr_normal(self) -> None:
        """15 candles → ATR is not None and > 0."""
        rates = _make_rates(15, 100.0, 1.0)
        atr = self.engine.compute_atr(rates)
        self.assertIsNotNone(atr, "ATR must not be None with 15 candles (period+1=15)")
        self.assertGreater(atr, 0.0, "ATR must be positive")

    def test_atr_insufficient_data(self) -> None:
        """5 candles < period+1 (15) → returns None."""
        rates = _make_rates(5)
        atr = self.engine.compute_atr(rates)
        self.assertIsNone(atr, "ATR must be None when candles < period+1")

    def test_atr_exactly_period_plus_one(self) -> None:
        """Exactly 15 candles (period=14 → needs 15) → ATR computed."""
        rates = _make_rates(15)
        atr = self.engine.compute_atr(rates)
        self.assertIsNotNone(atr)

    def test_atr_empty_returns_none(self) -> None:
        atr = self.engine.compute_atr([])
        self.assertIsNone(atr)


class TestComputeDynamic(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcDynamicExit()

    def test_compute_dynamic_high_score(self) -> None:
        """confluence=90 → mode=dynamic, rr_target > 2.5."""
        rates = _make_rates(20, 100.0, 0.5)
        result = self.engine.compute(90, rates, None, 100.0, "BUY")
        self.assertEqual(result["mode"], "dynamic")
        self.assertGreater(result["rr_target"], 2.5,
                           f"rr_target={result['rr_target']} expected > 2.5 for confluence=90")

    def test_compute_low_score(self) -> None:
        """confluence=30 → mode=dynamic, rr_target < rr for confluence=90."""
        rates = _make_rates(20, 100.0, 0.5)
        high = self.engine.compute(90, rates, None, 100.0, "BUY")
        low = self.engine.compute(30, rates, None, 100.0, "BUY")
        self.assertEqual(low["mode"], "dynamic")
        self.assertLess(low["rr_target"], high["rr_target"],
                        "Low confluence must yield lower rr_target than high confluence")
        # Formula: 1.8 + (30/100)*1.7 = 2.31
        self.assertAlmostEqual(low["rr_target"], 2.31, places=2)

    def test_compute_fallback_empty_rates(self) -> None:
        """Empty rates → mode=fallback, values equal FALLBACK dict."""
        result = self.engine.compute(70, [], None, 100.0, "BUY")
        self.assertEqual(result["mode"], "fallback")
        self.assertIsNone(result["atr_value"])
        self.assertIsNone(result["rr_target"])
        for key, val in BtcDynamicExit.FALLBACK.items():
            self.assertAlmostEqual(result[key], val, places=4,
                                   msg=f"Fallback key {key} mismatch")

    def test_caps_respected_high_confluence(self) -> None:
        """Large-swing candles + confluence=100 → tp_usd ≤ 6.0 and sl_usd ≤ 3.0."""
        rates = _make_rates_large(20)
        result = self.engine.compute(100, rates, None, 60000.0, "BUY")
        self.assertEqual(result["mode"], "dynamic")
        self.assertLessEqual(result["tp_usd"], 6.0, "tp_usd must not exceed 6.0")
        self.assertLessEqual(result["sl_usd"], 3.0, "sl_usd must not exceed 3.0")
        self.assertGreaterEqual(result["tp_usd"], 1.0, "tp_usd must be at least 1.0")
        self.assertGreaterEqual(result["sl_usd"], 0.8, "sl_usd must be at least 0.8")

    def test_rr_formula_no_caps(self) -> None:
        """When caps are not hit, tp_usd / sl_usd ≈ rr_target within 1%."""
        # ATR ≈ 1.0, confluence=50 → sl=1.5, rr=2.65, tp=3.975 — no caps hit
        rates = _make_rates(20, 100.0, 0.5)
        result = self.engine.compute(50, rates, None, 100.0, "BUY")
        self.assertEqual(result["mode"], "dynamic")
        tp = result["tp_usd"]
        sl = result["sl_usd"]
        rr = result["rr_target"]
        actual_rr = tp / sl
        self.assertAlmostEqual(actual_rr, rr, delta=rr * 0.01,
                               msg=f"tp/sl={actual_rr:.4f} should ≈ rr_target={rr:.4f}")

    def test_cvd_tighten_buy(self) -> None:
        """BUY + cvd_slope=-200 → trail_gap_usd reduced vs no-CVD baseline."""
        rates = _make_rates(20, 100.0, 0.5)
        base = self.engine.compute(70, rates, None, 100.0, "BUY")
        tightened = self.engine.compute(70, rates, -200.0, 100.0, "BUY")
        self.assertEqual(tightened["mode"], "dynamic")
        self.assertLess(tightened["trail_gap_usd"], base["trail_gap_usd"],
                        "trail_gap_usd must decrease when cvd_slope < -100 on BUY")

    def test_cvd_tighten_not_applied_on_sell(self) -> None:
        """SELL + cvd_slope=-200 → trail_gap unchanged (CVD tighten only on BUY)."""
        rates = _make_rates(20, 100.0, 0.5)
        base = self.engine.compute(70, rates, None, 100.0, "SELL")
        sell = self.engine.compute(70, rates, -200.0, 100.0, "SELL")
        self.assertEqual(sell["trail_gap_usd"], base["trail_gap_usd"],
                         "SELL position must not be affected by CVD tighten")

    def test_all_keys_present_in_dynamic_result(self) -> None:
        """Dynamic result must contain all required keys."""
        rates = _make_rates(20)
        result = self.engine.compute(70, rates, None, 100.0, "BUY")
        for key in ("tp_usd", "sl_usd", "lock_usd", "trail_gap_usd",
                    "trail_start_usd", "atr_value", "rr_target", "mode"):
            self.assertIn(key, result, f"Key '{key}' missing from compute() result")


class TestAdjustOnTick(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcDynamicExit()

    def _base_params(self) -> dict:
        return {
            "tp_usd": 3.0,
            "sl_usd": 1.5,
            "lock_usd": 0.75,
            "trail_gap_usd": 0.6,
            "trail_start_usd": 2.4,
            "atr_value": 1.0,
            "rr_target": 2.0,
            "mode": "dynamic",
        }

    def test_adjust_on_tick_tighten(self) -> None:
        """profit > trail_start + cvd_slope=-200 on BUY → trail_gap reduced."""
        params = self._base_params()
        updated = self.engine.adjust_on_tick(
            current_profit=2.5,
            min_seen_profit=-0.1,
            params=params,
            cvd_slope=-200.0,
            direction="BUY",
        )
        self.assertLess(updated["trail_gap_usd"], params["trail_gap_usd"],
                        "trail_gap_usd must decrease when profit > trail_start and CVD < -50")
        self.assertGreaterEqual(updated["trail_gap_usd"], 0.2, "floor is 0.2")

    def test_adjust_on_tick_no_change_positive_cvd(self) -> None:
        """cvd_slope=+100 → params returned unchanged."""
        params = self._base_params()
        updated = self.engine.adjust_on_tick(
            current_profit=2.5,
            min_seen_profit=0.0,
            params=params,
            cvd_slope=100.0,
            direction="BUY",
        )
        self.assertEqual(updated["trail_gap_usd"], params["trail_gap_usd"],
                         "Positive CVD must not tighten trail")
        self.assertIs(updated, params, "Should return the same params dict object unchanged")

    def test_adjust_on_tick_no_change_below_trail_start(self) -> None:
        """profit < trail_start → no tightening even with negative CVD."""
        params = self._base_params()
        updated = self.engine.adjust_on_tick(
            current_profit=1.0,  # below trail_start=2.4
            min_seen_profit=0.0,
            params=params,
            cvd_slope=-200.0,
            direction="BUY",
        )
        self.assertEqual(updated["trail_gap_usd"], params["trail_gap_usd"])

    def test_adjust_on_tick_floor_respected(self) -> None:
        """Repeated tightening must not push trail_gap below 0.2."""
        params = self._base_params()
        params["trail_gap_usd"] = 0.25
        updated = self.engine.adjust_on_tick(
            current_profit=2.5,
            min_seen_profit=0.0,
            params=params,
            cvd_slope=-300.0,
            direction="BUY",
        )
        self.assertGreaterEqual(updated["trail_gap_usd"], 0.2,
                                "trail_gap_usd must never go below 0.2")


if __name__ == "__main__":
    unittest.main()
