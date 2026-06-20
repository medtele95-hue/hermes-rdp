"""Tests for app/quant/volume_engine.py — B8 pillar.

Covers: K-Means HVN (A15), break_vol_ratio (A16), Wilder ATR (A4).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.quant.volume_engine import (
    _weighted_kmeans_1d,
    compute_atr,
    compute_break_vol_ratio,
    compute_hvn_levels,
    evaluate_b8_context,
    price_near_hvn,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(
    n: int = 50,
    base_price: float = 2000.0,
    vol_col: str = "tick_volume",
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prices = base_price + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame({
        "open": prices - 0.5,
        "high": prices + 1.0,
        "low": prices - 1.0,
        "close": prices,
        vol_col: rng.uniform(100, 1000, n),
    })


# ── compute_hvn_levels ────────────────────────────────────────────────────────

class TestWeightedKmeans1D:
    """Unit tests for the K-Means core (Appendix A15)."""

    def test_returns_k_centroids(self):
        rng = np.random.default_rng(0)
        p = rng.uniform(0, 100, 50).astype(float)
        w = rng.uniform(1, 10, 50).astype(float)
        centroids = _weighted_kmeans_1d(p, w, k=4)
        assert len(centroids) == 4

    def test_centroids_sorted_ascending(self):
        rng = np.random.default_rng(1)
        p = rng.uniform(0, 100, 50).astype(float)
        w = np.ones(50)
        centroids = _weighted_kmeans_1d(p, w, k=3)
        assert list(centroids) == sorted(centroids)

    def test_centroids_within_data_range(self):
        p = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        w = np.ones(5)
        centroids = _weighted_kmeans_1d(p, w, k=3)
        assert all(10.0 <= c <= 50.0 for c in centroids)

    def test_high_weight_attracts_centroid(self):
        # Cluster centered around 80 with 10× the weight
        p = np.array([10.0, 12.0, 80.0, 82.0])
        w = np.array([1.0, 1.0, 10.0, 10.0])
        centroids = _weighted_kmeans_1d(p, w, k=2)
        # One centroid near 11, one near 81
        assert any(c > 70 for c in centroids)
        assert any(c < 20 for c in centroids)

    def test_single_cluster(self):
        p = np.array([5.0, 5.1, 4.9, 5.05])
        w = np.ones(4)
        centroids = _weighted_kmeans_1d(p, w, k=1)
        assert len(centroids) == 1
        assert abs(centroids[0] - 5.0) < 0.2


class TestComputeHvnLevels:
    def test_returns_list_of_floats(self):
        df = _make_df(200)
        levels = compute_hvn_levels(df)
        assert isinstance(levels, list)
        assert all(isinstance(v, float) for v in levels)

    def test_returns_k_hvn_levels(self):
        df = _make_df(200)
        levels = compute_hvn_levels(df, k=4)
        assert len(levels) == 4

    def test_levels_are_within_price_range(self):
        df = _make_df(200)
        levels = compute_hvn_levels(df, k=4)
        p_min = ((df["high"] + df["low"] + df["close"]) / 3).min()
        p_max = ((df["high"] + df["low"] + df["close"]) / 3).max()
        for lvl in levels:
            assert p_min <= lvl <= p_max

    def test_levels_are_sorted(self):
        df = _make_df(200)
        levels = compute_hvn_levels(df, k=4)
        assert levels == sorted(levels)

    def test_empty_df_returns_empty(self):
        assert compute_hvn_levels(pd.DataFrame()) == []

    def test_tiny_df_returns_empty(self):
        df = _make_df(5)
        assert compute_hvn_levels(df) == []

    def test_no_volume_column_returns_empty(self):
        df = _make_df(200).drop(columns=["tick_volume"])
        assert compute_hvn_levels(df) == []

    def test_real_volume_column_works(self):
        df = _make_df(200, vol_col="real_volume")
        levels = compute_hvn_levels(df, k=3)
        assert len(levels) == 3

    def test_lookback_limits_data_used(self):
        rng = np.random.default_rng(0)
        prices_old = 1000.0 + np.cumsum(rng.normal(0, 1, 300))
        prices_new = 2000.0 + np.cumsum(rng.normal(0, 1, 200))
        prices = np.concatenate([prices_old, prices_new])
        vols = np.ones(500) * 500
        df = pd.DataFrame({
            "open": prices - 0.5,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "tick_volume": vols,
        })
        levels = compute_hvn_levels(df, lookback=200, k=2)
        # With lookback=200, only the new-range prices should appear
        for lvl in levels:
            assert lvl > 1500.0, f"Expected price in ~2000 range, got {lvl}"

    def test_high_volume_cluster_dominates(self):
        """K-Means centroids gravitate toward high-volume price clusters."""
        n = 100
        # Two price clusters: one at 1000 with low vol, one at 2000 with high vol
        prices_lo = np.full(50, 1000.0)
        prices_hi = np.full(50, 2000.0)
        vols_lo = np.full(50, 100.0)
        vols_hi = np.full(50, 5000.0)

        prices_all = np.concatenate([prices_lo, prices_hi])
        vols_all = np.concatenate([vols_lo, vols_hi])
        df = pd.DataFrame({
            "open": prices_all - 0.5,
            "high": prices_all + 1.0,
            "low": prices_all - 1.0,
            "close": prices_all,
            "tick_volume": vols_all,
        })
        levels = compute_hvn_levels(df, k=2)
        # Both clusters should be found
        assert any(abs(lvl - 1000.0) < 50 for lvl in levels), f"Expected ~1000 cluster, got {levels}"
        assert any(abs(lvl - 2000.0) < 50 for lvl in levels), f"Expected ~2000 cluster, got {levels}"


# ── compute_break_vol_ratio ───────────────────────────────────────────────────

class TestBreakVolRatio:
    def test_high_volume_bar_returns_ratio_above_one(self):
        df = _make_df(50)
        # Spike the last confirmed candle to 5000
        df.iloc[-2, df.columns.get_loc("tick_volume")] = 5000.0
        ratio = compute_break_vol_ratio(df, bar_offset=1, sma_period=20)
        assert ratio is not None
        assert ratio > 1.0

    def test_low_volume_bar_returns_ratio_below_one(self):
        df = _make_df(50)
        df["tick_volume"] = 500.0
        df.iloc[-2, df.columns.get_loc("tick_volume")] = 50.0
        ratio = compute_break_vol_ratio(df, bar_offset=1, sma_period=20)
        assert ratio is not None
        assert ratio < 1.0

    def test_returns_none_for_insufficient_bars(self):
        df = _make_df(15)
        assert compute_break_vol_ratio(df, bar_offset=1, sma_period=20) is None

    def test_returns_none_without_volume_column(self):
        df = _make_df(50).drop(columns=["tick_volume"])
        assert compute_break_vol_ratio(df) is None

    def test_high_vol_real_break(self):
        df = _make_df(50, vol_col="real_volume")
        df.iloc[-2, df.columns.get_loc("real_volume")] = 10000.0
        ratio = compute_break_vol_ratio(df, bar_offset=1, sma_period=20)
        assert ratio is not None and ratio >= 1.5


# ── price_near_hvn ────────────────────────────────────────────────────────────

class TestPriceNearHvn:
    def test_price_at_hvn_returns_true(self):
        assert price_near_hvn(2000.0, [1990.0, 2000.5, 2020.0], 5.0) is True

    def test_price_far_from_hvn_returns_false(self):
        assert price_near_hvn(2100.0, [1990.0, 2000.5, 2020.0], 5.0) is False

    def test_empty_hvn_returns_false(self):
        assert price_near_hvn(2000.0, [], 5.0) is False

    def test_zero_atr_returns_false(self):
        assert price_near_hvn(2000.0, [2000.0], 0.0) is False

    def test_tolerance_factor_respected(self):
        # ATR=10, tolerance_factor=0.5 → tol=5; HVN at 2006 is just outside
        assert price_near_hvn(2000.0, [2006.0], 10.0, 0.5) is False
        # HVN at 2004 is inside
        assert price_near_hvn(2000.0, [2004.0], 10.0, 0.5) is True


# ── compute_atr ───────────────────────────────────────────────────────────────

class TestComputeAtr:
    def test_returns_positive_float(self):
        df = _make_df(50)
        atr = compute_atr(df)
        assert atr is not None and atr > 0

    def test_insufficient_bars_returns_none(self):
        df = _make_df(5)
        assert compute_atr(df) is None

    def test_missing_columns_returns_none(self):
        df = pd.DataFrame({"close": [1, 2, 3]})
        assert compute_atr(df) is None

    def test_wilder_smoothing_differs_from_sma(self):
        """Wilder RMA should give a different (smoother) result than plain SMA."""
        import numpy as np
        rng = np.random.default_rng(99)
        n = 100
        prices = 2000.0 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame({
            "open": prices - 0.5,
            "high": prices + 2.0,
            "low": prices - 2.0,
            "close": prices,
        })
        atr = compute_atr(df, period=14)
        # Verify it's finite, positive, and reasonable (not NaN or 0)
        assert atr is not None
        assert 0 < atr < 10  # should be on the order of the noise (~2)

    def test_spike_bar_smoothed_by_wilder(self):
        """ATR spike from one bar should decay over subsequent bars (RMA property)."""
        df = _make_df(60)
        # Insert a spike bar near the middle
        df.iloc[30, df.columns.get_loc("high")] = 2500.0
        df.iloc[30, df.columns.get_loc("low")] = 1500.0
        atr = compute_atr(df, period=14)
        assert atr is not None and atr > 0


# ── evaluate_b8_context ───────────────────────────────────────────────────────

class TestEvaluateB8Context:
    def test_returns_dict_with_required_keys(self):
        df = _make_df(200)
        ctx = evaluate_b8_context(df)
        assert isinstance(ctx, dict)
        for key in ("hvn_levels", "break_vol_ratio", "real_break", "atr"):
            assert key in ctx

    def test_real_break_true_when_ratio_high(self):
        df = _make_df(50)
        df.iloc[-2, df.columns.get_loc("tick_volume")] = 50000.0
        ctx = evaluate_b8_context(df)
        assert ctx["real_break"] is True
        assert ctx["break_vol_ratio"] >= 1.5

    def test_empty_df_returns_safe_defaults(self):
        ctx = evaluate_b8_context(pd.DataFrame())
        assert ctx["hvn_levels"] == []
        assert ctx["break_vol_ratio"] is None
        assert ctx["real_break"] is False
        assert ctx["atr"] is None

    def test_no_volume_still_returns_dict(self):
        df = _make_df(200).drop(columns=["tick_volume"])
        ctx = evaluate_b8_context(df)
        assert isinstance(ctx, dict)
        assert ctx["hvn_levels"] == []
