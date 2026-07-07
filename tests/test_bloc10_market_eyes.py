"""BLOC 10 — SUPER-EYES tests (pure features, zero decisional effect).

Proves the four senses on injected data:
a) tick-rule CVD on the midprice (+ slopes + divergence),
b) DXY proxy with ICE renormalized weights and inverse-pair signs,
c) D1/W1 levels with SIGNED distances in points and ATR,
d) mining canary fail-soft (NEM.N / B.N absent on this XM server).
"""
from __future__ import annotations

import unittest

from app.services.market_eyes import (
    cvd_from_ticks,
    d1_w1_levels,
    dxy_proxy,
    mining_canary,
)


def _tick(epoch: float, bid: float, ask: float, volume: float = 1.0) -> dict:
    return {"time": epoch, "bid": bid, "ask": ask, "volume": volume}


class TestCvdTickRule(unittest.TestCase):
    def test_uptick_run_gives_positive_cvd(self) -> None:
        ticks = [_tick(i, 100.0 + i * 0.1, 100.2 + i * 0.1) for i in range(10)]
        result = cvd_from_ticks(ticks)
        self.assertTrue(result["available"])
        self.assertGreater(result["cvd_session"], 0)

    def test_flat_tick_inherits_previous_direction(self) -> None:
        # up, flat, flat: the two flat ticks keep adding +volume (tick rule)
        ticks = [_tick(0, 100.0, 100.2), _tick(1, 100.1, 100.3), _tick(2, 100.1, 100.3), _tick(3, 100.1, 100.3)]
        result = cvd_from_ticks(ticks)
        self.assertEqual(result["cvd_session"], 3.0)

    def test_bear_divergence_price_up_cvd_down(self) -> None:
        # price grinds up on tiny uptick volume but dumps on heavy downticks
        ticks = [
            _tick(0, 100.0, 100.2, 1),
            _tick(1, 100.3, 100.5, 1),    # up, +1
            _tick(2, 100.2, 100.4, 50),   # down, -50
            _tick(3, 100.4, 100.6, 1),    # up, +1
            _tick(4, 100.3, 100.5, 40),   # down, -40
            _tick(5, 100.5, 100.7, 1),    # up, +1
        ]
        result = cvd_from_ticks(ticks)
        self.assertLess(result["cvd_session"], 0)
        self.assertEqual(result["cvd_divergence"], "bear")

    def test_slopes_windowed(self) -> None:
        ticks = [_tick(i * 60, 100.0 + i * 0.1, 100.2 + i * 0.1) for i in range(20)]  # one tick/min
        result = cvd_from_ticks(ticks, now_epoch=19 * 60)
        self.assertIsNotNone(result["cvd_slope_m5"])
        self.assertIsNotNone(result["cvd_slope_m15"])
        self.assertLessEqual(result["cvd_slope_m5"], result["cvd_slope_m15"])

    def test_insufficient_ticks_fail_soft(self) -> None:
        result = cvd_from_ticks([_tick(0, 100, 100.2)])
        self.assertFalse(result["available"])


class TestDxyProxy(unittest.TestCase):
    def test_usd_strength_from_falling_eurusd(self) -> None:
        changes = {
            "EURUSD": {"m15": -0.30, "h1": -0.50},  # EUR down = USD up
            "USDJPY": {"m15": 0.10, "h1": 0.20},
            "GBPUSD": {"m15": -0.20, "h1": -0.30},
        }
        result = dxy_proxy(changes)
        self.assertTrue(result["available"])
        self.assertGreater(result["dxy_change_m15"], 0)
        self.assertEqual(result["dxy_trend"], "UP")

    def test_same_direction_divergence_flagged(self) -> None:
        changes = {
            "EURUSD": {"m15": -0.30, "h1": -0.50},
            "USDJPY": {"m15": 0.10, "h1": 0.20},
            "GBPUSD": {"m15": -0.20, "h1": -0.30},
        }
        # DXY up AND gold up -> anomalous
        result = dxy_proxy(changes, gold_change_m15=0.4)
        self.assertEqual(result["gold_dxy_divergence"], "SAME_DIRECTION_ANOMALY")
        result = dxy_proxy(changes, gold_change_m15=-0.4)
        self.assertEqual(result["gold_dxy_divergence"], "NORMAL_INVERSE")

    def test_missing_pairs_fail_soft(self) -> None:
        result = dxy_proxy({"EURUSD": {"m15": None, "h1": None}})
        self.assertFalse(result["available"])


class TestD1W1Levels(unittest.TestCase):
    def test_levels_and_signed_distances(self) -> None:
        d1 = [
            {"open": 3280.0, "high": 3315.0, "low": 3270.0, "close": 3305.0},  # previous day
            {"open": 3306.0, "high": 3320.0, "low": 3300.0, "close": 3310.0},  # today
        ]
        w1 = [{"open": 3290.0, "high": 3325.0, "low": 3265.0, "close": 3310.0}]
        result = d1_w1_levels(d1, w1, price=3310.0, atr=40.0, point=0.01)
        self.assertTrue(result["available"])
        self.assertEqual(result["pdh"], 3315.0)
        self.assertEqual(result["pdl"], 3270.0)
        self.assertEqual(result["pdc"], 3305.0)
        self.assertEqual(result["daily_open"], 3306.0)
        self.assertEqual(result["weekly_open"], 3290.0)
        # signed: price 3310 is 5 BELOW pdh -> -500 points, and ABOVE pdl
        self.assertAlmostEqual(result["dist_pdh_pts"], -500.0)
        self.assertAlmostEqual(result["dist_pdl_pts"], 4000.0)
        self.assertAlmostEqual(result["dist_pdh_atr"], -0.125)

    def test_missing_d1_fail_soft(self) -> None:
        self.assertFalse(d1_w1_levels(None, None, 3300.0, 40.0)["available"])


class TestMiningCanary(unittest.TestCase):
    def test_absent_symbols_fail_soft_documented(self) -> None:
        result = mining_canary(None)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "MINER_SYMBOLS_ABSENT")

    def test_composite_equal_weight(self) -> None:
        result = mining_canary(
            {"NEM.N": {"h1": 1.0, "d1": 2.0}, "B.N": {"h1": 0.5, "d1": 1.0}},
            gold_change_h1=0.2,
        )
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["hui_change_h1"], 0.75)
        self.assertAlmostEqual(result["hui_change_d1"], 1.5)
        self.assertEqual(result["hui_ratio_trend"], "MINERS_LEAD")

    def test_divergence_strict_extremes_only(self) -> None:
        # miners dumping hard while gold rises -> bearish canary
        result = mining_canary(
            {"NEM.N": {"h1": -1.0, "d1": -2.0}, "B.N": {"h1": -0.8, "d1": -1.5}},
            gold_change_h1=0.3,
        )
        self.assertEqual(result["hui_divergence"], "BEARISH_FOR_GOLD")
        # small moves: no divergence call
        result = mining_canary(
            {"NEM.N": {"h1": -0.2, "d1": -0.1}, "B.N": {"h1": -0.1, "d1": -0.1}},
            gold_change_h1=0.05,
        )
        self.assertIsNone(result["hui_divergence"])


if __name__ == "__main__":
    unittest.main()
