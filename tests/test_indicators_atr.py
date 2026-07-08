# -*- coding: utf-8 -*-
"""COEUR_V2 chantier 2 — app/utils/indicators.py:atr() migre de SMA vers Wilder
RMA (MATH_CORE_AUDIT.md RÉSERVÉ SIMO n°2). Valeurs de référence vérifiables à
la main : True Range calculé barre par barre, puis SMA(14) vs Wilder RMA(14)
recalculés indépendamment (voir docstring de chaque test pour la dérivation)."""
from __future__ import annotations

import unittest

import pandas as pd

from app.utils.indicators import atr, atr_last, atr_series_graceful, atr_sma, atr_wilder, true_range

# 15 barres OHLC de référence — assez pour produire UNE valeur ATR(14) SMA et
# UNE valeur ATR(14) Wilder, et voir la 15e barre diverger entre les deux
# méthodes (Wilder pondère la nouvelle barre 1/14, SMA la pondère 1/14 aussi
# mais fait glisser la fenêtre entière — les deux ne coïncident qu'à la toute
# première valeur, ligne 14, où seed_wilder == mean_sma par construction).
_REF_DATA = {
    "high":  [48.70, 48.72, 48.90, 48.87, 48.82, 49.05, 49.20, 49.35, 49.92, 50.19, 50.12, 49.66, 49.88, 50.19, 50.36],
    "low":   [47.79, 48.14, 48.39, 48.37, 48.24, 48.64, 48.94, 48.86, 49.50, 49.87, 49.20, 48.90, 49.43, 49.73, 49.26],
    "close": [48.16, 48.61, 48.75, 48.63, 48.74, 49.03, 49.07, 49.32, 49.91, 50.13, 49.53, 49.50, 49.75, 50.03, 50.31],
}


def _ref_df() -> pd.DataFrame:
    return pd.DataFrame(_REF_DATA)


def _hand_true_range() -> list[float]:
    """True Range computed by hand from _REF_DATA, bar by bar (no pandas)."""
    highs, lows, closes = _REF_DATA["high"], _REF_DATA["low"], _REF_DATA["close"]
    tr = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return tr


class TestTrueRange(unittest.TestCase):
    def test_matches_hand_computation(self) -> None:
        tr = true_range(_ref_df())
        expected = _hand_true_range()
        for i, exp in enumerate(expected):
            self.assertAlmostEqual(tr.iloc[i], exp, places=6, msg=f"bar {i}")


class TestAtrSmaReferenceValue(unittest.TestCase):
    def test_sma_atr14_matches_hand_average(self) -> None:
        """SMA(14) of the first 14 True Range values, hand-averaged."""
        tr = _hand_true_range()
        expected_seed = sum(tr[:14]) / 14
        result = atr_sma(_ref_df(), period=14)
        self.assertAlmostEqual(result.iloc[13], expected_seed, places=6)

    def test_sma_atr14_last_value_is_rolling_mean_of_last_14(self) -> None:
        tr = _hand_true_range()
        expected_last = sum(tr[1:15]) / 14  # bars 1..14 (last 14 of 15 bars)
        result = atr_sma(_ref_df(), period=14)
        self.assertAlmostEqual(result.iloc[14], expected_last, places=6)


class TestAtrWilderReferenceValue(unittest.TestCase):
    def test_wilder_seed_equals_sma_of_first_period(self) -> None:
        """Wilder's own definition: seed = SMA of the first `period` TR values —
        so index [period-1] must be IDENTICAL between atr_sma and atr_wilder."""
        sma = atr_sma(_ref_df(), period=14)
        wilder = atr_wilder(_ref_df(), period=14)
        self.assertAlmostEqual(sma.iloc[13], wilder.iloc[13], places=6)

    def test_wilder_atr14_last_value_hand_recursion(self) -> None:
        """ATR_15 = (ATR_14*(14-1) + TR_15) / 14, hand-computed from the same
        True Range series used above — this is THE regression value: 0.59327
        (vs SMA's 0.56786 on the same window, ~4.3% apart on this sample)."""
        tr = _hand_true_range()
        seed = sum(tr[:14]) / 14
        expected = (seed * 13 + tr[14]) / 14
        result = atr_wilder(_ref_df(), period=14)
        self.assertAlmostEqual(result.iloc[14], expected, places=6)
        self.assertAlmostEqual(result.iloc[14], 0.593265, places=5)

    def test_wilder_diverges_from_sma_after_seed(self) -> None:
        sma = atr_sma(_ref_df(), period=14)
        wilder = atr_wilder(_ref_df(), period=14)
        self.assertNotAlmostEqual(sma.iloc[14], wilder.iloc[14], places=3)

    def test_insufficient_bars_returns_all_nan(self) -> None:
        short_df = _ref_df().iloc[:10]
        result = atr_wilder(short_df, period=14)
        self.assertTrue(result.isna().all())


class TestAtrIsNowWilderAlias(unittest.TestCase):
    def test_atr_equals_atr_wilder(self) -> None:
        a = atr(_ref_df(), period=14)
        w = atr_wilder(_ref_df(), period=14)
        pd.testing.assert_series_equal(a, w)

    def test_atr_no_longer_equals_atr_sma_after_seed(self) -> None:
        a = atr(_ref_df(), period=14)
        s = atr_sma(_ref_df(), period=14)
        self.assertNotAlmostEqual(a.iloc[14], s.iloc[14], places=3)


class TestAtrLastConsolidatedHelper(unittest.TestCase):
    """COEUR_V2 chantier 2: ~9 near-duplicate local ATR helpers found across the
    repo consolidate onto this one shared, tested function."""

    def test_matches_wilder_last_value_when_enough_bars(self) -> None:
        result = atr_last(_ref_df(), period=14)
        wilder = atr_wilder(_ref_df(), period=14)
        self.assertAlmostEqual(result, float(wilder.iloc[-1]), places=6)

    def test_falls_back_to_sma_when_too_few_bars_for_wilder_seed(self) -> None:
        short_df = _ref_df().iloc[:10]  # < period=14, Wilder can't seed
        result = atr_last(short_df, period=14)
        tr = _hand_true_range()[:10]
        expected = sum(tr) / len(tr)
        self.assertAlmostEqual(result, expected, places=6)

    def test_none_on_empty_or_single_row(self) -> None:
        self.assertIsNone(atr_last(_ref_df().iloc[:1], period=14))
        self.assertIsNone(atr_last(None, period=14))

    def test_exact_period_bars_matches_sma_seed_continuity(self) -> None:
        """At exactly `period` bars, Wilder's seed value IS the SMA — no
        discontinuity at the fallback boundary."""
        exact_df = _ref_df().iloc[:14]
        result = atr_last(exact_df, period=14)
        tr = _hand_true_range()[:14]
        expected = sum(tr) / 14
        self.assertAlmostEqual(result, expected, places=6)


class TestAtrSeriesGraceful(unittest.TestCase):
    def test_matches_wilder_from_seed_onward(self) -> None:
        series = atr_series_graceful(_ref_df(), period=14)
        wilder = atr_wilder(_ref_df(), period=14)
        self.assertAlmostEqual(series.iloc[13], wilder.iloc[13], places=6)
        self.assertAlmostEqual(series.iloc[14], wilder.iloc[14], places=6)

    def test_leading_bars_have_values_not_nan(self) -> None:
        """Mirrors the pre-migration `rolling(period, min_periods=1)` — every
        bar gets a value, not just bars >= period."""
        series = atr_series_graceful(_ref_df(), period=14)
        self.assertFalse(series.iloc[:13].isna().any())

    def test_leading_bar_is_expanding_mean_of_true_range(self) -> None:
        tr = _hand_true_range()
        series = atr_series_graceful(_ref_df(), period=14)
        expected_bar2 = sum(tr[:3]) / 3
        self.assertAlmostEqual(series.iloc[2], expected_bar2, places=6)


if __name__ == "__main__":
    unittest.main()
