"""Tests for app/quant/absorption_detector.py — B9 pillar."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.quant.absorption_detector import (
    _compute_cvd_series,
    _is_cvd_flattening,
    _is_engulfing,
    _is_pin_bar_with_volume,
    absorption_against_position,
    detect_absorption_context,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(
    n: int = 50,
    base_price: float = 2000.0,
    vol: float = 500.0,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prices = base_price + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({
        "open": prices - 0.3,
        "high": prices + 0.6,
        "low": prices - 0.6,
        "close": prices,
        "tick_volume": np.full(n, vol),
    })


def _df_with_bear_absorption(lookback: int = 20) -> pd.DataFrame:
    """Build a DataFrame where CVD makes a higher-high but price does NOT."""
    n = lookback + 10
    prices = np.linspace(2000.0, 2010.0, n)
    # Price ends flat (no new high in last bar)
    prices[-2] = prices[-3] - 0.5  # last confirmed bar lower than previous
    opens = prices - 0.3
    closes = prices.copy()
    closes[-2] = closes[-3] - 0.3  # bearish last bar

    # Volume: make last bar very high bullish volume so CVD spikes up
    vols = np.full(n, 200.0)
    vols[-2] = 5000.0  # massive bullish volume but price doesn't follow

    closes[-1] = closes[-2]  # live candle (excluded from analysis)

    return pd.DataFrame({
        "open": opens,
        "high": prices + 0.5,
        "low": prices - 0.5,
        "close": closes,
        "tick_volume": vols,
    })


# ── _compute_cvd_series ───────────────────────────────────────────────────────

class TestComputeCvdSeries:
    def test_returns_cumulative_array(self):
        df = _make_df(20)
        cvd = _compute_cvd_series(df)
        assert cvd is not None
        assert len(cvd) == 20

    def test_close_at_high_gives_max_positive_delta(self):
        """CLV = +1 when close == high (all buys)."""
        n = 10
        df = pd.DataFrame({
            "open": np.full(n, 1.0),
            "high": np.full(n, 2.0),
            "low": np.full(n, 1.0),
            "close": np.full(n, 2.0),   # close == high → clv = +1
            "tick_volume": np.full(n, 100.0),
        })
        cvd = _compute_cvd_series(df)
        assert cvd is not None
        # Each bar: delta = 100 * 1 = 100; cumulative after n bars = n*100
        assert abs(cvd[-1] - n * 100.0) < 1e-6

    def test_close_at_low_gives_max_negative_delta(self):
        """CLV = -1 when close == low (all sells)."""
        n = 10
        df = pd.DataFrame({
            "open": np.full(n, 2.0),
            "high": np.full(n, 2.0),
            "low": np.full(n, 1.0),
            "close": np.full(n, 1.0),   # close == low → clv = -1
            "tick_volume": np.full(n, 100.0),
        })
        cvd = _compute_cvd_series(df)
        assert cvd is not None
        assert abs(cvd[-1] - (-n * 100.0)) < 1e-6

    def test_midpoint_close_gives_zero_delta(self):
        """CLV ≈ 0 when close == midpoint of range."""
        n = 10
        df = pd.DataFrame({
            "open": np.full(n, 1.5),
            "high": np.full(n, 2.0),
            "low": np.full(n, 1.0),
            "close": np.full(n, 1.5),   # close == mid → clv = 0
            "tick_volume": np.full(n, 100.0),
        })
        cvd = _compute_cvd_series(df)
        assert cvd is not None
        assert abs(cvd[-1]) < 1e-6

    def test_returns_none_without_volume_column(self):
        df = _make_df(20).drop(columns=["tick_volume"])
        assert _compute_cvd_series(df) is None

    def test_returns_none_without_high_low(self):
        df = pd.DataFrame({
            "open": [1.0, 2.0],
            "close": [1.5, 2.5],
            "tick_volume": [100.0, 100.0],
        })
        assert _compute_cvd_series(df) is None


# ── _is_engulfing ─────────────────────────────────────────────────────────────

class TestIsEngulfing:
    def test_bullish_engulfing(self):
        # bar -3: prior bearish (open=2.0, close=1.7)
        # bar -2: bullish engulf (open=1.5, close=2.2 wraps prior body)
        # bar -1: live (ignored)
        df = pd.DataFrame({
            "open":  [2.0, 1.5, 2.0],
            "close": [1.7, 2.2, 2.1],
            "high":  [2.1, 2.3, 2.2],
            "low":   [1.6, 1.4, 2.0],
        })
        assert _is_engulfing(df) is True

    def test_bearish_engulfing(self):
        # bar -3: prior bullish (open=1.7, close=2.0)
        # bar -2: bearish engulf (open=2.2, close=1.4 wraps prior body)
        # bar -1: live (ignored)
        df = pd.DataFrame({
            "open":  [1.7, 2.2, 2.0],
            "close": [2.0, 1.4, 1.5],
            "high":  [2.1, 2.3, 2.0],
            "low":   [1.6, 1.3, 1.5],
        })
        assert _is_engulfing(df) is True

    def test_no_engulfing(self):
        df = _make_df(10)
        # Override last two bars to be same-direction small candles
        df.iloc[-2] = [2000, 2001, 2001.5, 2000.5, 200]
        df.iloc[-3] = [1999, 2000, 2000.5, 1999.5, 200]
        # This is bullish but not engulfing
        assert not _is_engulfing(df)

    def test_insufficient_bars(self):
        assert _is_engulfing(pd.DataFrame()) is False
        assert _is_engulfing(_make_df(2)) is False


# ── _is_pin_bar_with_volume ───────────────────────────────────────────────────

class TestIsPinBar:
    """Directional pin bar tests (Appendix A13 exact formulas)."""

    def _set_bar(self, df: pd.DataFrame, idx: int, o: float, h: float, lo: float, cl: float, v: float) -> None:
        df.iloc[idx, df.columns.get_loc("open")] = o
        df.iloc[idx, df.columns.get_loc("high")] = h
        df.iloc[idx, df.columns.get_loc("low")] = lo
        df.iloc[idx, df.columns.get_loc("close")] = cl
        df.iloc[idx, df.columns.get_loc("tick_volume")] = v

    def _hammer_df(self) -> pd.DataFrame:
        """Bullish hammer: lower_wick >= 2×body AND upper_wick <= body.
        body = 0.1 (2001.0→2001.1), upper=0.1 (≤body), lower=5.0 (≥2×body).
        """
        df = _make_df(30)
        self._set_bar(df, -2, o=2001.0, h=2001.2, lo=1996.0, cl=2001.1, v=1500.0)
        return df

    def _shooting_star_df(self) -> pd.DataFrame:
        """Bearish shooting star: upper_wick >= 2×body AND lower_wick <= body.
        body = 0.1 (2001.1→2001.0), upper=4.9 (≥2×body), lower=0.1 (≤body).
        """
        df = _make_df(30)
        self._set_bar(df, -2, o=2001.1, h=2006.0, lo=2000.9, cl=2001.0, v=1500.0)
        return df

    def test_hammer_passes(self):
        assert _is_pin_bar_with_volume(self._hammer_df())

    def test_shooting_star_passes(self):
        assert _is_pin_bar_with_volume(self._shooting_star_df())

    def test_symmetric_wick_fails(self):
        """Both wicks equal — not a directional pin (fails upper_wick <= body)."""
        df = _make_df(30)
        # body=0.1, lower_wick=3.0, upper_wick=3.0 — symmetric, NOT a hammer
        self._set_bar(df, -2, o=2001.0, h=2004.1, lo=1998.0, cl=2001.1, v=1500.0)
        assert not _is_pin_bar_with_volume(df)

    def test_no_wick_returns_false(self):
        df = _make_df(30)
        assert not _is_pin_bar_with_volume(df)

    def test_insufficient_bars(self):
        assert not _is_pin_bar_with_volume(_make_df(3))


# ── _is_cvd_flattening ────────────────────────────────────────────────────────

class TestIsCvdFlattening:
    def test_flat_slope_returns_true(self):
        assert _is_cvd_flattening({"cvd_slope": 5.0}) is True
        assert _is_cvd_flattening({"cvd_slope": -10.0}) is True

    def test_strong_slope_returns_false(self):
        assert _is_cvd_flattening({"cvd_slope": 80.0}) is False
        assert _is_cvd_flattening({"cvd_slope": -50.0}) is False

    def test_missing_slope_returns_false(self):
        assert _is_cvd_flattening({}) is False
        assert _is_cvd_flattening({"cvd_slope": None}) is False


# ── detect_absorption_context ─────────────────────────────────────────────────

class TestDetectAbsorptionContext:
    def test_returns_safe_defaults_for_empty_df(self):
        ctx = detect_absorption_context(pd.DataFrame())
        assert ctx["bear_absorption"] is False
        assert ctx["bull_absorption"] is False
        assert ctx["pattern"] is None
        assert ctx["valid"] is False

    def test_returns_safe_defaults_for_small_df(self):
        ctx = detect_absorption_context(_make_df(5))
        assert ctx["valid"] is False

    def test_no_volume_column_returns_safe_defaults(self):
        df = _make_df(50).drop(columns=["tick_volume"])
        ctx = detect_absorption_context(df)
        assert ctx["valid"] is False

    def test_bear_absorption_detected(self):
        df = _df_with_bear_absorption(lookback=20)
        ctx = detect_absorption_context(df, lookback=20)
        # Bear absorption: CVD higher-high + price NOT higher-high
        assert ctx["bear_absorption"] is True

    def test_regular_df_no_absorption(self):
        rng = np.random.default_rng(7)
        n = 50
        prices = 2000.0 + np.arange(n) * 0.5  # steady uptrend
        vols = np.full(n, 300.0)
        df = pd.DataFrame({
            "open": prices - 0.2,
            "high": prices + 0.3,
            "low": prices - 0.3,
            "close": prices,
            "tick_volume": vols,
        })
        ctx = detect_absorption_context(df, lookback=20)
        # No absorption expected in a steady trend
        # (CVD and price both make new highs → no divergence)
        assert not ctx["valid"]

    def test_valid_requires_pattern(self):
        df = _df_with_bear_absorption(lookback=20)
        ctx = detect_absorption_context(df, lookback=20)
        # valid=True only when a pattern is confirmed
        if ctx["valid"]:
            assert ctx["pattern"] is not None
        else:
            assert ctx["pattern"] is None


# ── absorption_against_position ───────────────────────────────────────────────

class TestAbsorptionAgainstPosition:
    def test_bear_absorption_against_buy(self):
        ctx = {"bear_absorption": True, "bull_absorption": False, "pattern": "engulf", "valid": True}
        assert absorption_against_position("BUY", ctx) is True

    def test_bull_absorption_against_sell(self):
        ctx = {"bear_absorption": False, "bull_absorption": True, "pattern": "pin", "valid": True}
        assert absorption_against_position("SELL", ctx) is True

    def test_bear_absorption_not_against_sell(self):
        ctx = {"bear_absorption": True, "bull_absorption": False, "pattern": "engulf", "valid": True}
        assert absorption_against_position("SELL", ctx) is False

    def test_bull_absorption_not_against_buy(self):
        ctx = {"bear_absorption": False, "bull_absorption": True, "pattern": "pin", "valid": True}
        assert absorption_against_position("BUY", ctx) is False

    def test_invalid_absorption_not_counted(self):
        ctx = {"bear_absorption": True, "bull_absorption": False, "pattern": None, "valid": False}
        assert absorption_against_position("BUY", ctx) is False

    def test_none_ctx_returns_false(self):
        assert absorption_against_position("BUY", None) is False
        assert absorption_against_position("SELL", {}) is False
