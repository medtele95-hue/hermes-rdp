"""Tests for app/quant/geometry_engine.py — all pure functions."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from app.quant.geometry_engine import (
    _atr_last,
    breakout_box,
    fibonacci_ote_zone,
    geometric_confluence_score,
    impulse_score,
    linear_regression_channel,
    premium_discount_zone,
    range_compression_score,
    support_resistance_zones,
    swing_high_low,
    trendline_slope,
    triangle_or_wedge_detection,
    volatility_expansion_score,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_flat(n: int = 20, price: float = 100.0) -> pd.DataFrame:
    """Flat OHLC with minimal spread."""
    return pd.DataFrame({
        "open": [price] * n,
        "high": [price + 0.5] * n,
        "low": [price - 0.5] * n,
        "close": [price] * n,
    })


def _make_trending_up(n: int = 20) -> pd.DataFrame:
    """Steadily rising close prices."""
    closes = [100.0 + i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "open": [c - 0.2 for c in closes],
        "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
    })


def _make_trending_down(n: int = 20) -> pd.DataFrame:
    closes = [100.0 - i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "open": [c + 0.2 for c in closes],
        "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
    })


def _make_pivot_data() -> pd.DataFrame:
    """Data with a clear swing high at index 2 and swing low at index 7."""
    highs =  [10, 11, 15, 12, 11, 10,  9,  7,  9, 10, 11]
    lows =   [ 9, 10, 13, 10,  9,  8,  7,  5,  7,  8,  9]
    closes = [ 9, 10, 14, 11, 10,  9,  8,  6,  8,  9, 10]
    opens =  [ 9, 10, 13, 12, 10,  9,  8,  6,  7,  9, 10]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})


# ---------------------------------------------------------------------------
# Tests: swing_high_low
# ---------------------------------------------------------------------------

class TestSwingHighLow(unittest.TestCase):
    def test_finds_swing_high(self) -> None:
        df = _make_pivot_data()
        result = swing_high_low(df, lookback=10)
        self.assertIsNotNone(result["swing_high"])
        self.assertGreaterEqual(result["swing_high"], 13.0)

    def test_finds_swing_low(self) -> None:
        df = _make_pivot_data()
        result = swing_high_low(df, lookback=10)
        self.assertIsNotNone(result["swing_low"])
        self.assertLessEqual(result["swing_low"], 6.0)

    def test_returns_none_on_empty(self) -> None:
        result = swing_high_low(pd.DataFrame(), lookback=5)
        self.assertIsNone(result["swing_high"])
        self.assertIsNone(result["swing_low"])

    def test_fallback_on_no_strict_pivot(self) -> None:
        # Monotonic: no strict pivot exists, should fallback to extremes
        df = _make_trending_up(15)
        result = swing_high_low(df, lookback=10)
        self.assertIsNotNone(result["swing_high"])
        self.assertIsNotNone(result["swing_low"])

    def test_indices_within_dataframe_bounds(self) -> None:
        df = _make_pivot_data()
        result = swing_high_low(df, lookback=10)
        n = len(df)
        if result["swing_high_idx"] is not None:
            self.assertGreaterEqual(result["swing_high_idx"], 0)
            self.assertLess(result["swing_high_idx"], n)
        if result["swing_low_idx"] is not None:
            self.assertGreaterEqual(result["swing_low_idx"], 0)
            self.assertLess(result["swing_low_idx"], n)


# ---------------------------------------------------------------------------
# Tests: fibonacci_ote_zone
# ---------------------------------------------------------------------------

class TestFibonacciOteZone(unittest.TestCase):
    def test_standard_zone(self) -> None:
        result = fibonacci_ote_zone(110.0, 100.0)
        self.assertAlmostEqual(result["ote_low"], 106.2, places=10)
        self.assertAlmostEqual(result["ote_high"], 107.9, places=10)
        self.assertAlmostEqual(result["midpoint"], 107.05, places=10)
        self.assertAlmostEqual(result["range"], 10.0, places=10)

    def test_invalid_inputs_return_none(self) -> None:
        result = fibonacci_ote_zone(100.0, 110.0)   # high < low
        self.assertIsNone(result["ote_low"])
        self.assertIsNone(result["ote_high"])

    def test_equal_high_low_returns_none(self) -> None:
        result = fibonacci_ote_zone(100.0, 100.0)
        self.assertIsNone(result["ote_low"])

    def test_none_inputs_return_none(self) -> None:
        result = fibonacci_ote_zone(None, 100.0)
        self.assertIsNone(result["ote_low"])

    def test_zone_is_between_high_and_low(self) -> None:
        result = fibonacci_ote_zone(200.0, 100.0)
        self.assertGreater(result["ote_low"], 100.0)
        self.assertLess(result["ote_high"], 200.0)
        self.assertLess(result["ote_low"], result["ote_high"])


# ---------------------------------------------------------------------------
# Tests: premium_discount_zone
# ---------------------------------------------------------------------------

class TestPremiumDiscountZone(unittest.TestCase):
    def test_premium_zone(self) -> None:
        result = premium_discount_zone(110.0, 100.0, 108.0)
        self.assertEqual(result["zone"], "PREMIUM")
        self.assertGreater(result["pct"], 0.60)

    def test_discount_zone(self) -> None:
        result = premium_discount_zone(110.0, 100.0, 102.0)
        self.assertEqual(result["zone"], "DISCOUNT")
        self.assertLess(result["pct"], 0.40)

    def test_equilibrium_zone(self) -> None:
        result = premium_discount_zone(110.0, 100.0, 105.0)
        self.assertEqual(result["zone"], "EQUILIBRIUM")

    def test_price_at_high_is_premium(self) -> None:
        result = premium_discount_zone(110.0, 100.0, 110.0)
        self.assertEqual(result["zone"], "PREMIUM")

    def test_price_at_low_is_discount(self) -> None:
        result = premium_discount_zone(110.0, 100.0, 100.0)
        self.assertEqual(result["zone"], "DISCOUNT")

    def test_invalid_range_returns_unknown(self) -> None:
        result = premium_discount_zone(100.0, 110.0, 105.0)
        self.assertEqual(result["zone"], "UNKNOWN")

    def test_none_input_returns_unknown(self) -> None:
        result = premium_discount_zone(None, 100.0, 105.0)
        self.assertEqual(result["zone"], "UNKNOWN")


# ---------------------------------------------------------------------------
# Tests: linear_regression_channel
# ---------------------------------------------------------------------------

class TestLinearRegressionChannel(unittest.TestCase):
    def test_bullish_trend(self) -> None:
        df = _make_trending_up(30)
        result = linear_regression_channel(df, lookback=20)
        self.assertEqual(result["direction"], "BULLISH")
        self.assertIsNotNone(result["slope"])
        self.assertGreater(result["slope"], 0)

    def test_bearish_trend(self) -> None:
        df = _make_trending_down(30)
        result = linear_regression_channel(df, lookback=20)
        self.assertEqual(result["direction"], "BEARISH")
        self.assertIsNotNone(result["slope"])
        self.assertLess(result["slope"], 0)

    def test_empty_data_returns_flat(self) -> None:
        result = linear_regression_channel(pd.DataFrame())
        self.assertEqual(result["direction"], "FLAT")
        self.assertIsNone(result["slope"])

    def test_r_squared_between_0_and_1(self) -> None:
        df = _make_trending_up(30)
        result = linear_regression_channel(df, lookback=20)
        self.assertGreaterEqual(result["r_squared"], 0.0)
        self.assertLessEqual(result["r_squared"], 1.0)

    def test_channel_bounds_make_sense(self) -> None:
        df = _make_trending_up(30)
        result = linear_regression_channel(df, lookback=20)
        self.assertGreater(result["upper_channel"], result["lower_channel"])


# ---------------------------------------------------------------------------
# Tests: breakout_box
# ---------------------------------------------------------------------------

class TestBreakoutBox(unittest.TestCase):
    def test_returns_box_bounds(self) -> None:
        df = _make_pivot_data()
        result = breakout_box(df, lookback=8)
        self.assertIsNotNone(result["box_high"])
        self.assertIsNotNone(result["box_low"])
        self.assertGreater(result["box_high"], result["box_low"])

    def test_box_range_is_positive(self) -> None:
        df = _make_flat(20, 100.0)
        result = breakout_box(df, lookback=10)
        self.assertGreaterEqual(result["box_range"], 0.0)

    def test_midpoint_is_average(self) -> None:
        df = _make_flat(20, 100.0)
        result = breakout_box(df, lookback=10)
        self.assertAlmostEqual(
            result["midpoint"],
            (result["box_high"] + result["box_low"]) / 2,
            places=5,
        )

    def test_insufficient_data_returns_none(self) -> None:
        result = breakout_box(pd.DataFrame({"high": [1], "low": [0], "close": [0.5]}), lookback=5)
        self.assertIsNone(result["box_high"])


# ---------------------------------------------------------------------------
# Tests: range_compression_score
# ---------------------------------------------------------------------------

class TestRangeCompressionScore(unittest.TestCase):
    def test_score_between_0_and_1(self) -> None:
        df = _make_flat(20)
        score = range_compression_score(df)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_compressed_flat_market_scores_high(self) -> None:
        df = _make_flat(20)
        score = range_compression_score(df)
        self.assertGreater(score, 0.5)

    def test_empty_data_returns_zero(self) -> None:
        self.assertEqual(range_compression_score(pd.DataFrame()), 0.0)


# ---------------------------------------------------------------------------
# Tests: impulse_score
# ---------------------------------------------------------------------------

class TestImpulseScore(unittest.TestCase):
    def test_score_range(self) -> None:
        df = _make_trending_up(20)
        score = impulse_score(df)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_flat_market_has_low_impulse(self) -> None:
        df = _make_flat(20)
        score = impulse_score(df)
        self.assertLessEqual(score, 10.0)

    def test_empty_data_returns_zero(self) -> None:
        self.assertEqual(impulse_score(pd.DataFrame()), 0.0)


# ---------------------------------------------------------------------------
# Tests: volatility_expansion_score
# ---------------------------------------------------------------------------

class TestVolatilityExpansionScore(unittest.TestCase):
    def test_score_range(self) -> None:
        df = _make_trending_up(20)
        score = volatility_expansion_score(df)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(volatility_expansion_score(pd.DataFrame()), 0.0)


# ---------------------------------------------------------------------------
# Tests: trendline_slope
# ---------------------------------------------------------------------------

class TestTrendlineSlope(unittest.TestCase):
    def test_positive_slope(self) -> None:
        pts = [(0, 10.0), (5, 15.0), (10, 20.0)]
        slope = trendline_slope(pts)
        self.assertGreater(slope, 0)

    def test_negative_slope(self) -> None:
        pts = [(0, 20.0), (5, 15.0), (10, 10.0)]
        slope = trendline_slope(pts)
        self.assertLess(slope, 0)

    def test_single_point_returns_zero(self) -> None:
        self.assertEqual(trendline_slope([(0, 100.0)]), 0.0)

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(trendline_slope([]), 0.0)


# ---------------------------------------------------------------------------
# Tests: support_resistance_zones
# ---------------------------------------------------------------------------

class TestSupportResistanceZones(unittest.TestCase):
    def test_returns_lists(self) -> None:
        df = _make_pivot_data()
        result = support_resistance_zones(df, lookback=10)
        self.assertIsInstance(result["resistance_zones"], list)
        self.assertIsInstance(result["support_zones"], list)

    def test_empty_data_returns_empty_lists(self) -> None:
        result = support_resistance_zones(pd.DataFrame())
        self.assertEqual(result["resistance_zones"], [])
        self.assertEqual(result["support_zones"], [])

    def test_zone_fields_present(self) -> None:
        df = _make_pivot_data()
        result = support_resistance_zones(df, lookback=10)
        for zone in result["resistance_zones"] + result["support_zones"]:
            self.assertIn("level", zone)
            self.assertIn("strength", zone)
            self.assertIn("type", zone)
            self.assertGreater(zone["strength"], 0)


# ---------------------------------------------------------------------------
# Tests: triangle_or_wedge_detection
# ---------------------------------------------------------------------------

class TestTriangleOrWedgeDetection(unittest.TestCase):
    def test_returns_pattern_field(self) -> None:
        df = _make_trending_up(20)
        result = triangle_or_wedge_detection(df)
        self.assertIn("pattern", result)
        self.assertIn(result["pattern"], {
            "ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE",
            "SYMMETRICAL_TRIANGLE", "RISING_WEDGE", "FALLING_WEDGE", "NONE",
        })

    def test_insufficient_data_returns_none(self) -> None:
        result = triangle_or_wedge_detection(pd.DataFrame())
        self.assertEqual(result["pattern"], "NONE")
        self.assertFalse(result["convergence"])


# ---------------------------------------------------------------------------
# Tests: geometric_confluence_score
# ---------------------------------------------------------------------------

class TestGeometricConfluenceScore(unittest.TestCase):
    def test_score_range(self) -> None:
        result = geometric_confluence_score()
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 100.0)

    def test_components_present(self) -> None:
        result = geometric_confluence_score()
        self.assertIn("components", result)
        self.assertIn("dominant_pattern", result)

    def test_ote_zone_hit_adds_score(self) -> None:
        ote = {"ote_low": 104.0, "ote_high": 108.0, "midpoint": 106.0, "range": 10.0}
        with_ote = geometric_confluence_score(ote=ote, current_price=106.0)
        without_ote = geometric_confluence_score(ote=ote, current_price=90.0)
        self.assertGreater(with_ote["score"], without_ote["score"])

    def test_breakout_above_box_adds_score(self) -> None:
        box = {"box_high": 100.0, "box_low": 90.0, "box_range": 10.0, "midpoint": 95.0}
        above_box = geometric_confluence_score(box=box, current_price=102.0)
        inside_box = geometric_confluence_score(box=box, current_price=95.0)
        self.assertGreater(above_box["score"], inside_box["score"])

    def test_pd_zone_premium_discount_adds_score(self) -> None:
        pd_premium = {"zone": "PREMIUM", "pct": 0.8, "distance_to_50pct": 3.0}
        pd_neutral = {"zone": "EQUILIBRIUM", "pct": 0.5, "distance_to_50pct": 0.0}
        with_pd = geometric_confluence_score(pd_zone=pd_premium)
        without_pd = geometric_confluence_score(pd_zone=pd_neutral)
        self.assertGreater(with_pd["score"], without_pd["score"])

    def test_high_impulse_increases_score(self) -> None:
        high = geometric_confluence_score(impulse=80.0)
        low = geometric_confluence_score(impulse=0.0)
        self.assertGreater(high["score"], low["score"])


# ---------------------------------------------------------------------------
# Tests: _atr_last (internal helper)
# ---------------------------------------------------------------------------

class TestAtrLast(unittest.TestCase):
    def test_atr_is_positive(self) -> None:
        df = _make_trending_up(20)
        atr = _atr_last(df, period=5)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.0)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_atr_last(pd.DataFrame()))

    def test_short_series_uses_mean(self) -> None:
        df = _make_trending_up(5)
        atr = _atr_last(df, period=14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.0)


if __name__ == "__main__":
    unittest.main()
