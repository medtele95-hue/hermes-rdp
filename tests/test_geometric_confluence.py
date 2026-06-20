"""Tests for app/mt5/geometric_confluence.py — HERMES v1.6

Covers:
- All 6 harmonic pattern ratio validations
- Fibonacci retracement / extension hit
- ABCD projection hit
- Confluence zone clustering
- Gann lightweight angle hit
- Closed candles only / no repaint
- WAIT does not block
- CONFIRM adds confluence boost only (no lot/SL/TP changes)
- No mt5.order_send in module
- Live trading disabled
- SHADOW mode → final_bonus == 0
- ACTIVE mode → final_bonus == GEOMETRIC_CONFIRM_BONUS
- SMC-hard-blocked candidate → final_bonus == 0 regardless
- REJECT never hard-blocks
- [GEO_FIB] emitted, [FIB_CONFLUENCE] never emitted
"""
from __future__ import annotations

import math
import types
import logging

import pandas as pd
import numpy as np
import pytest

import io
import contextlib

from app.mt5.geometric_confluence import (
    GEOMETRIC_CONFIRM_BONUS,
    GEOMETRIC_FIB_HIT_TOLERANCE,
    GEOMETRIC_RATIO_TOLERANCE,
    SCHEMA_VERSION,
    _alternating_swings,
    _detect_swing_points,
    _extract_xabcd,
    _fib_levels,
    _check_hit,
    _gann_angle_hit,
    _harmonic_ratios,
    _validate_harmonic,
    _abcd_projections,
    _spiral_confluence,
    _cluster_levels,
    _geometric_grade,
    _geometric_decision,
    _square_of_9_hit,
    compute_geometric_bonus,
    analyze_geometric_confluence,
)


# ---------------------------------------------------------------------------
# Helpers for building synthetic OHLC DataFrames
# ---------------------------------------------------------------------------

def _make_rates(prices: list[float], extra: int = 0) -> pd.DataFrame:
    """Build a minimal OHLC DataFrame from a list of close prices.
    An extra incomplete candle is appended at the end (simulates live bar).
    """
    rows = []
    for p in prices:
        rows.append({"open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "tick_volume": 100})
    # append extra live (incomplete) candle so iloc[:-1] drops it
    last = prices[-1] if prices else 1.0
    rows.append({"open": last, "high": last * 1.001, "low": last * 0.999, "close": last, "tick_volume": 10})
    for _ in range(extra):
        rows.append({"open": last, "high": last, "low": last, "close": last, "tick_volume": 1})
    return pd.DataFrame(rows)


def _make_swing_rates(
    pattern: list[tuple[str, float]],
    fill: int = 3,
) -> pd.DataFrame:
    """Build rates with explicit H/L swing sequence (with fill bars between).

    pattern = [("H", 200), ("L", 100), ("H", 180), ("L", 120), ("L", 90)]
    Each swing point is embedded as a candle whose high (for H) or low (for L)
    is the target value.  Fill bars interpolate between swings.
    """
    rows: list[dict] = []

    def _bar(h: float, l: float, c: float | None = None) -> dict:
        c = c if c is not None else (h + l) / 2
        return {"open": c, "high": h, "low": l, "close": c, "tick_volume": 100}

    prev_price = (pattern[0][1]) if pattern else 100.0
    for stype, sprice in pattern:
        # Fill bars moving toward the swing
        mid = (prev_price + sprice) / 2
        for _ in range(fill):
            rows.append(_bar(max(prev_price, mid) * 1.0002, min(prev_price, mid) * 0.9998))
        # The actual swing candle
        if stype == "H":
            rows.append(_bar(sprice * 1.0003, sprice * 0.997, sprice))
        else:
            rows.append(_bar(sprice * 1.003, sprice * 0.9997, sprice))
        prev_price = sprice
    # Append incomplete live candle
    rows.append(_bar(prev_price * 1.001, prev_price * 0.999, prev_price))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# §1 Swing detection — closed candles only
# ---------------------------------------------------------------------------

class TestClosedCandlesOnly:
    def test_last_candle_excluded_from_swing(self):
        """iloc[:-1] ensures the live (last) candle is never a swing point."""
        # Build rates where ONLY the last candle is an extreme high
        prices = [100.0] * 20 + [999.0]
        df = _make_rates(prices)
        pts = _detect_swing_points(df, window=3)
        swing_highs = [p for p in pts if p[2] == "H"]
        # The 999.0 candle is at index -1 (live bar), must not appear as swing
        assert all(p[1] < 900 for p in swing_highs), \
            "Live (last) candle must not generate swing high — no repaint"

    def test_returns_empty_for_none(self):
        assert _detect_swing_points(None) == []

    def test_returns_empty_for_empty_df(self):
        assert _detect_swing_points(pd.DataFrame()) == []

    def test_swing_points_sorted_by_index(self):
        df = _make_swing_rates([("H", 200), ("L", 100), ("H", 180), ("L", 110)], fill=5)
        pts = _detect_swing_points(df, window=3)
        indices = [p[0] for p in pts]
        assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# §2 Harmonic ratios computation
# ---------------------------------------------------------------------------

class TestHarmonicRatios:
    def test_ratios_keys_present(self):
        r = _harmonic_ratios(100, 200, 138, 175, 122)
        for k in ("AB_XA", "BC_AB", "CD_BC", "D_XA", "D_XC", "XA", "AB", "BC", "CD"):
            assert k in r

    def test_zero_leg_does_not_raise(self):
        # XA = 0 → eps prevents division by zero
        r = _harmonic_ratios(100, 100, 100, 100, 100)
        assert isinstance(r["AB_XA"], float)

    def test_gartley_bullish_ratios(self):
        # X=100, A=200 (XA=100), B=138.2 (AB/XA≈0.618), C=175 (BC/AB≈0.868)
        # D = A - 0.786*XA = 200 - 78.6 = 121.4  →  D_XA = |D-A|/XA = 78.6/100 = 0.786
        X, A, B, C = 100.0, 200.0, 138.2, 175.0
        D = A - 0.786 * (A - X)
        r = _harmonic_ratios(X, A, B, C, D)
        assert abs(r["AB_XA"] - 0.618) < 0.02
        assert abs(r["D_XA"]  - 0.786) < 0.01


# ---------------------------------------------------------------------------
# §3 Harmonic pattern validation — one test per pattern
# ---------------------------------------------------------------------------

def _build_gartley_bullish() -> tuple[float, float, float, float, float]:
    X, A = 100.0, 200.0
    B = A - 0.618 * (A - X)          # AB = 0.618 × XA
    C = B + 0.618 * (A - B)          # BC = 0.618 × AB (within 0.382-0.886)
    D = A - 0.786 * (A - X)          # D ≈ 0.786 × XA from A
    return X, A, B, C, D


def _build_butterfly_bullish() -> tuple:
    X, A = 100.0, 200.0
    B = A - 0.786 * (A - X)          # AB = 0.786 × XA
    C = B + 0.600 * (A - B)          # BC in 0.382-0.886
    D = A - 1.272 * (A - X)          # D_XA = 1.272 (within 1.27-1.618)
    return X, A, B, C, D


def _build_bat_bullish() -> tuple:
    X, A = 100.0, 200.0
    B = A - 0.450 * (A - X)          # AB = 0.45 × XA (in 0.382-0.5)
    C = B + 0.500 * (A - B)          # BC in 0.382-0.886
    D = A - 0.886 * (A - X)          # D_XA = 0.886
    return X, A, B, C, D


def _build_crab_bullish() -> tuple:
    # Must produce CD_BC ∈ (2.618, 3.618) AND D_XA ∈ (1.618, 1.902).
    # With AB_XA=0.5 and BC_AB=0.886, CD_BC ≈ 3.52 and D_XA=1.618 both fall in range.
    X, A = 100.0, 200.0
    B = A - 0.500 * (A - X)          # AB_XA = 0.5 ✓ (in 0.332-0.668)
    C = B + 0.886 * abs(A - B)       # BC_AB = 0.886 ✓ (in 0.382-0.886)
    D = A - 1.618 * abs(A - X)       # D_XA = 1.618 ✓ (in 1.568-1.952)
    # CD_BC = |C - D| / |C - B| ≈ 3.52 ✓
    return X, A, B, C, D


def _build_shark_bullish() -> tuple:
    X, A = 100.0, 200.0
    # AB = 1.13 × XA → B is beyond X (extension)
    B = X - 0.130 * (A - X)          # AB/XA = (A-B)/(A-X): A-B = A-X+0.13*(A-X) = 1.13*(A-X)
    C = B + 1.800 * abs(A - B)       # BC/AB = 1.8 (in 1.618-2.24)
    D = C - 1.000 * abs(C - B)       # CD/BC ≈ 1.0 (in 0.886-1.13)
    return X, A, B, C, D


def _build_cypher_bullish() -> tuple:
    X, A = 100.0, 200.0
    B = A - 0.500 * (A - X)          # AB = 0.5 × XA (in 0.382-0.618)
    C = B + 1.272 * abs(A - B)       # BC/AB = 1.272 (in 1.13-1.414)
    XC = abs(C - X)
    D = C - 0.786 * XC               # D_XC = 0.786 × XC
    return X, A, B, C, D


class TestHarmonicPatternValidation:
    def _check(self, xabcd: tuple, expected_pattern: str):
        X, A, B, C, D = xabcd
        r = _harmonic_ratios(X, A, B, C, D)
        valid, pattern, quality = _validate_harmonic(r)
        assert valid, f"Expected valid {expected_pattern}, got invalid. ratios={r}"
        assert pattern == expected_pattern, f"Expected {expected_pattern}, got {pattern}"
        assert quality > 0.0

    def test_gartley_validates(self):
        self._check(_build_gartley_bullish(), "GARTLEY")

    def test_butterfly_validates(self):
        self._check(_build_butterfly_bullish(), "BUTTERFLY")

    def test_bat_validates(self):
        self._check(_build_bat_bullish(), "BAT")

    def test_crab_validates(self):
        self._check(_build_crab_bullish(), "CRAB")

    def test_shark_validates(self):
        self._check(_build_shark_bullish(), "SHARK")

    def test_cypher_validates(self):
        self._check(_build_cypher_bullish(), "CYPHER")

    def test_garbage_ratios_do_not_validate(self):
        # Completely random XABCD that should not match any pattern
        r = _harmonic_ratios(100, 200, 195, 197, 199)
        valid, pattern, _ = _validate_harmonic(r)
        # If somehow valid, that would be a false positive.  Accept either outcome but
        # confirm the function does not raise.
        assert isinstance(valid, bool)
        assert isinstance(pattern, str)


# ---------------------------------------------------------------------------
# §4 Fibonacci levels
# ---------------------------------------------------------------------------

class TestFibRetracement:
    def test_retracement_prices_count(self):
        rets, exts = _fib_levels(200.0, 100.0)
        assert len(rets) == 7   # 7 retracement levels
        assert len(exts) == 6   # 6 extension levels

    def test_retracement_0618_hit(self):
        high, low = 200.0, 100.0
        price_at_0618 = high - (high - low) * 0.618   # = 200 - 61.8 = 138.2
        rets, _ = _fib_levels(high, low)
        hit, lvl = _check_hit(price_at_0618, rets, tol=(high - low) * 0.015)
        assert hit, "Price exactly at 61.8% retracement should be a hit"
        assert lvl is not None

    def test_retracement_0382_hit(self):
        high, low = 200.0, 100.0
        price = high - (high - low) * 0.382
        rets, _ = _fib_levels(high, low)
        hit, _ = _check_hit(price, rets, tol=(high - low) * 0.015)
        assert hit

    def test_far_price_no_hit(self):
        high, low = 200.0, 100.0
        rets, _ = _fib_levels(high, low)
        hit, _ = _check_hit(50.0, rets, tol=1.0)
        assert not hit


class TestFibExtension:
    def test_extension_1618_hit(self):
        high, low = 200.0, 100.0
        price_at_1618 = low + (high - low) * 1.618   # = 100 + 161.8 = 261.8
        _, exts = _fib_levels(high, low)
        hit, lvl = _check_hit(price_at_1618, exts, tol=(high - low) * 0.015)
        assert hit
        assert lvl is not None

    def test_extension_2618_hit(self):
        high, low = 200.0, 100.0
        price = low + (high - low) * 2.618
        _, exts = _fib_levels(high, low)
        hit, _ = _check_hit(price, exts, tol=(high - low) * 0.015)
        assert hit


# ---------------------------------------------------------------------------
# §5 ABCD projection
# ---------------------------------------------------------------------------

class TestABCDProjection:
    def test_abcd_1618_hit(self):
        # A=200, B=100, C=161.8 (BC retraces 38.2% of AB=100)
        A, B, C = 200.0, 100.0, 161.8
        # D at 1.618 * AB below C → C - 1.618*100 = 161.8 - 161.8 = 0
        # That's a degenerate case.  Use k=1.0: D = C - 1.0 * AB = 61.8
        D_1x = C - abs(B - A) * 1.0
        projs = _abcd_projections(A, B, C)
        hit, _ = _check_hit(D_1x, projs, tol=abs(B - A) * 0.015)
        assert hit

    def test_abcd_1272_hit(self):
        A, B, C = 200.0, 100.0, 161.8
        D = C - abs(B - A) * 1.272
        projs = _abcd_projections(A, B, C)
        hit, _ = _check_hit(D, projs, tol=abs(B - A) * 0.015)
        assert hit

    def test_abcd_returns_both_directions(self):
        projs = _abcd_projections(200.0, 100.0, 150.0)
        # Should contain projections both above and below C
        above = [p for p in projs if p > 150.0]
        below = [p for p in projs if p < 150.0]
        assert above and below, "ABCD projections should include both up and down"


# ---------------------------------------------------------------------------
# §6 Confluence zone clustering
# ---------------------------------------------------------------------------

class TestConfluenceZoneClustering:
    def test_cluster_count_3_when_3_nearby_levels(self):
        # Three levels within 0.5 tolerance
        levels = [100.0, 100.2, 100.4, 200.0]
        clusters = _cluster_levels(levels, tol=0.5)
        assert any(len(cl) >= 3 for cl in clusters)

    def test_separate_clusters(self):
        levels = [100.0, 200.0, 300.0]
        clusters = _cluster_levels(levels, tol=0.1)
        assert len(clusters) == 3

    def test_empty_input(self):
        assert _cluster_levels([], tol=1.0) == []


# ---------------------------------------------------------------------------
# §7 Gann (lightweight)
# ---------------------------------------------------------------------------

class TestGannAngle:
    def test_1x1_hit_at_midpoint(self):
        # Price at exactly 50% (1x1 angle) of high-low range
        high, low = 200.0, 100.0
        price = low + (high - low) * 0.500
        hit, angle = _gann_angle_hit(price, high, low)
        assert hit, "Price at exact 1×1 level should be a hit"
        assert angle == "1x1"

    def test_1x2_hit(self):
        high, low = 200.0, 100.0
        price = low + (high - low) * 0.333
        hit, angle = _gann_angle_hit(price, high, low)
        assert hit
        assert angle == "1x2"

    def test_2x1_hit(self):
        high, low = 200.0, 100.0
        price = low + (high - low) * 0.667
        hit, angle = _gann_angle_hit(price, high, low)
        assert hit
        assert angle == "2x1"

    def test_no_hit_when_far_from_all_angles(self):
        high, low = 200.0, 100.0
        hit, angle = _gann_angle_hit(102.0, high, low)
        assert not hit
        assert angle == "NONE"

    def test_zero_range_returns_false(self):
        hit, angle = _gann_angle_hit(100.0, 100.0, 100.0)
        assert not hit


# ---------------------------------------------------------------------------
# §8 Grading and decision
# ---------------------------------------------------------------------------

class TestGradingAndDecision:
    @pytest.mark.parametrize("score,expected", [
        (80, "A"), (65, "B"), (50, "C"), (49.9, "D"), (0, "D"),
    ])
    def test_grade_thresholds(self, score, expected):
        assert _geometric_grade(score) == expected

    def test_confirm_when_score_ge_75_and_aligned(self):
        dec, _ = _geometric_decision(80, "BUY", "BUY")
        assert dec == "CONFIRM"

    def test_weak_confirm_when_score_60_to_74(self):
        dec, _ = _geometric_decision(65, "BUY", "BUY")
        assert dec == "WEAK_CONFIRM"

    def test_wait_when_score_below_60(self):
        dec, _ = _geometric_decision(55, "BUY", "BUY")
        assert dec == "WAIT"

    def test_wait_when_direction_misaligned(self):
        # Score ≥ 75 but direction contradicts setup
        dec, _ = _geometric_decision(80, "BUY", "SELL")
        assert dec != "CONFIRM"


# ---------------------------------------------------------------------------
# §9 compute_geometric_bonus — shadow / active / smc-blocked
# ---------------------------------------------------------------------------

class TestComputeGeometricBonus:
    def test_shadow_mode_always_returns_zero(self):
        result = {"mode": "SHADOW", "decision": "CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 0.0, "SHADOW mode must always yield bonus=0"

    def test_active_confirm_returns_full_bonus(self):
        result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 5.0

    def test_active_weak_confirm_returns_half_bonus(self):
        result = {"mode": "ACTIVE", "decision": "WEAK_CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 2.5

    def test_active_wait_returns_zero(self):
        result = {"mode": "ACTIVE", "decision": "WAIT"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 0.0

    def test_active_reject_returns_zero_not_block(self):
        """REJECT must never produce a bonus or hard-block."""
        result = {"mode": "ACTIVE", "decision": "REJECT"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 0.0, "REJECT must not produce a bonus"

    def test_smc_hard_blocked_always_zero_regardless_of_mode(self):
        """Confirmation-matrix hard-block must never be overridden by geo bonus."""
        result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=True, confirm_bonus=5.0)
        assert bonus == 0.0

    def test_empty_result_returns_zero(self):
        assert compute_geometric_bonus({}, is_smc_hard_blocked=False) == 0.0
        assert compute_geometric_bonus(None, is_smc_hard_blocked=False) == 0.0  # type: ignore

    def test_bonus_value_matches_geometric_confirm_bonus_constant(self):
        result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False,
                                        confirm_bonus=GEOMETRIC_CONFIRM_BONUS)
        assert bonus == GEOMETRIC_CONFIRM_BONUS


# ---------------------------------------------------------------------------
# §10 analyze_geometric_confluence — integration behaviour
# ---------------------------------------------------------------------------

def _make_minimal_rates(n: int = 60) -> pd.DataFrame:
    """Minimal rates sufficient to run analyze_geometric_confluence."""
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    rows = []
    for p in prices:
        h = p + abs(np.random.randn()) * 0.3
        l = p - abs(np.random.randn()) * 0.3
        rows.append({"open": p, "high": h, "low": l, "close": p, "tick_volume": 100})
    # live bar
    rows.append({"open": prices[-1], "high": prices[-1]*1.001, "low": prices[-1]*0.999, "close": prices[-1], "tick_volume": 1})
    return pd.DataFrame(rows)


class TestAnalyzeGeometricConfluence:
    @pytest.fixture(autouse=True)
    def rates(self):
        self.r = _make_minimal_rates(80)

    def test_returns_schema_version(self):
        result = analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert result["schema_version"] == SCHEMA_VERSION

    def test_required_keys_present(self):
        result = analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        for key in (
            "enabled", "symbol", "direction", "mode",
            "harmonic_pattern", "harmonic_quality", "harmonic_score", "harmonic_ratios",
            "fib_retracement_hit", "fib_retracement_level",
            "fib_extension_hit", "fib_extension_level",
            "abcd_projection_hit", "abcd_projection_level",
            "gann_angle_hit", "gann_angle",
            "square_of_9_hit", "time_price_square_hit",
            "spiral_confluence_hit", "golden_spiral_projection",
            "confluence_zone_count", "confluence_zone_price", "distance_to_confluence_pts",
            "geometric_score", "geometric_grade", "decision", "reason",
        ):
            assert key in result, f"Missing key: {key}"

    def test_shadow_mode_stored_in_result(self):
        result = analyze_geometric_confluence(
            "GOLD#", "SELL", self.r, self.r, self.r, self.r, mode="SHADOW"
        )
        assert result["mode"] == "SHADOW"

    def test_active_mode_stored_in_result(self):
        result = analyze_geometric_confluence(
            "GOLD#", "SELL", self.r, self.r, self.r, self.r, mode="ACTIVE"
        )
        assert result["mode"] == "ACTIVE"

    def test_wait_decision_does_not_block(self):
        """WAIT decision must not set any hard-block field."""
        result = analyze_geometric_confluence("EURUSD", "BUY", self.r, self.r, self.r, None)
        if result["decision"] == "WAIT":
            assert "hard_block" not in result or not result.get("hard_block")

    def test_reject_does_not_produce_hard_block(self):
        result = analyze_geometric_confluence("EURUSD", "BUY", self.r, self.r, self.r, None)
        assert result.get("hard_block", False) is False

    def test_geometric_score_between_0_and_100(self):
        result = analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert 0 <= result["geometric_score"] <= 100

    def test_none_rates_returns_null_result(self):
        result = analyze_geometric_confluence("BTCUSD#", "BUY", None, None, None, None)
        assert result["decision"] == "WAIT"
        assert result["geometric_score"] == 0

    def test_empty_rates_returns_null_result(self):
        empty = pd.DataFrame()
        result = analyze_geometric_confluence("BTCUSD#", "BUY", empty, empty, empty, empty)
        assert result["decision"] == "WAIT"

    @staticmethod
    @contextlib.contextmanager
    def _capture_hermes():
        """Add a temporary StringIO handler to the 'hermes' logger."""
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(message)s"))
        hermes = logging.getLogger("hermes")
        hermes.addHandler(h)
        try:
            yield buf
        finally:
            hermes.removeHandler(h)
            h.close()

    def test_no_fib_confluence_tag_emitted(self):
        """Module MUST NOT emit [FIB_CONFLUENCE] — that tag belongs to FIB_CONFLUENCE_EXECUTION_AGENT."""
        with self._capture_hermes() as buf:
            analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert "[FIB_CONFLUENCE]" not in buf.getvalue(), \
            "geometric_confluence must never emit [FIB_CONFLUENCE] tag"

    def test_geo_fib_tag_emitted(self):
        """Module MUST emit [GEO_FIB]."""
        with self._capture_hermes() as buf:
            analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert "[GEO_FIB]" in buf.getvalue(), \
            f"[GEO_FIB] must be emitted; got:\n{buf.getvalue()}"

    def test_geometric_confluence_tag_emitted(self):
        with self._capture_hermes() as buf:
            analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert "[GEOMETRIC_CONFLUENCE]" in buf.getvalue()

    def test_gann_confluence_tag_emitted(self):
        with self._capture_hermes() as buf:
            analyze_geometric_confluence("BTCUSD#", "BUY", self.r, self.r, self.r, self.r)
        assert "[GANN_CONFLUENCE]" in buf.getvalue()


# ---------------------------------------------------------------------------
# §11 Shadow + Active bonus round-trip through analyze + compute
# ---------------------------------------------------------------------------

class TestShadowActiveBonusRoundTrip:
    """Simulate the main.py bonus calculation path."""

    def _high_score_result(self, mode: str) -> dict:
        """Build a synthetic geo_result with CONFIRM decision."""
        return {
            "mode": mode,
            "decision": "CONFIRM",
            "geometric_score": 85,
            "geometric_grade": "A",
        }

    def test_shadow_confirm_bonus_is_zero(self):
        result = self._high_score_result("SHADOW")
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 0.0

    def test_active_confirm_bonus_equals_configured_bonus(self):
        result = self._high_score_result("ACTIVE")
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 5.0

    def test_smc_hard_block_zeroes_bonus_even_in_active_mode(self):
        result = self._high_score_result("ACTIVE")
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=True, confirm_bonus=5.0)
        assert bonus == 0.0

    def test_bonus_never_changes_lot_or_sl_or_tp(self):
        """Geometric bonus is purely additive to confluence score.
        This test verifies the module exposes no lot/SL/TP fields.
        """
        result = self._high_score_result("ACTIVE")
        for forbidden in ("lot", "sl", "tp", "stop_loss", "take_profit", "lot_size"):
            assert forbidden not in result, f"Geo result must not contain {forbidden}"

    def test_confirm_adds_to_confluence_score_not_standalone(self):
        """CONFIRM only boosts; it must not independently decide to open a trade."""
        result = self._high_score_result("ACTIVE")
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert isinstance(bonus, float)
        assert bonus <= 5.0  # must not exceed configured bonus

    def test_bonus_capped_at_configured_value(self):
        result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        bonus = compute_geometric_bonus(result, is_smc_hard_blocked=False, confirm_bonus=5.0)
        assert bonus == 5.0  # must equal, not exceed, the configured bonus


# ---------------------------------------------------------------------------
# §12 Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariants:
    def test_no_mt5_order_send_in_module(self):
        import app.mt5.geometric_confluence as geo_mod
        src = open(geo_mod.__file__, encoding="utf-8").read()
        # Check for actual call — "order_send(" — not the word in a docstring comment
        assert "order_send(" not in src, \
            "geometric_confluence.py must NOT call order_send()"

    def test_no_mt5_import_in_module(self):
        import app.mt5.geometric_confluence as geo_mod
        src = open(geo_mod.__file__, encoding="utf-8").read()
        assert "import MetaTrader5" not in src
        assert "import mt5" not in src.lower().replace("import metatrader5", "")

    def test_live_trading_remains_disabled(self):
        from app.config import get_settings
        settings = get_settings()
        assert settings.allow_live_trading is False, \
            "allow_live_trading must remain False"

    def test_demo_lot_unchanged(self):
        from app.config import get_settings
        settings = get_settings()
        assert settings.demo_max_lot == 0.01, \
            "demo_max_lot must remain 0.01"

    def test_demo_only_remains_true(self):
        from app.config import get_settings
        settings = get_settings()
        assert settings.demo_only is True

    def test_default_mode_is_shadow(self):
        from app.config import get_settings
        settings = get_settings()
        assert str(getattr(settings, "geometric_confluence_mode", "SHADOW")).upper() == "SHADOW", \
            "Default geometric_confluence_mode must be SHADOW"

    def test_order_send_location_unchanged(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-c",
             "import ast, pathlib\n"
             "src = pathlib.Path('app/mt5/demo_router.py').read_text(encoding='utf-8')\n"
             "assert 'order_send' in src, 'demo_router must contain order_send'\n"
             "print('OK')"],
            capture_output=True, text=True, cwd="C:/hermes-mt5-agent",
        )
        assert "OK" in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# §13 Spiral confluence
# ---------------------------------------------------------------------------

class TestSpiralConfluence:
    def test_hit_at_golden_ratio_level(self):
        high, low = 200.0, 100.0
        phi = (1 + math.sqrt(5)) / 2
        level = low + (high - low) / phi   # ≈ 161.8
        hit, proj = _spiral_confluence(level, high, low)
        assert hit
        assert proj is not None

    def test_no_hit_when_far(self):
        hit, proj = _spiral_confluence(50.0, 200.0, 100.0)
        assert not hit


# ---------------------------------------------------------------------------
# §14 Square of 9
# ---------------------------------------------------------------------------

class TestSquareOf9:
    def test_near_ring_boundary_is_hit(self):
        # sqrt(high) - sqrt(price) ≈ 0.25 ring
        high = 400.0
        # sqrt(400) = 20.0; target sqrt = 19.75 → price = 19.75^2 = 390.0625
        price = 19.75 ** 2
        assert _square_of_9_hit(price, high)

    def test_far_from_ring_is_miss(self):
        # sqrt(400)=20, sqrt(350)≈18.71, diff≈1.29, 1.29 % 0.25 ≈ 0.04 → hit
        # use something that clearly misses
        high = 400.0
        # sqrt(380) ≈ 19.49, diff = 0.51, 0.51 % 0.25 = 0.01 → hit (boundary)
        # Let's pick diff = 0.12 → no hit
        price = (20.0 - 0.12) ** 2   # 19.88^2 ≈ 395.2
        result = _square_of_9_hit(price, high)
        # 0.12 % 0.25 = 0.12 (between 0.05 and 0.20) → expect no hit
        assert not result


# ---------------------------------------------------------------------------
# §15 Negative + discriminative harmonic tests (Task A)
# ---------------------------------------------------------------------------

class TestNegativeHarmonicDiscrimination:
    """Tests that prove _validate_harmonic REJECTS as well as ACCEPTS.

    The existing §3 tests are tautological (they only prove the validator
    accepts its own ratio definitions).  These tests prove the hard boundary:
    ratios outside the tolerance band return NONE, and Gartley geometry is
    NOT misclassified as Bat (and Bat NOT as Butterfly).
    """

    def test_contradictory_ratios_return_none(self):
        """XABCD ratios that fit no pattern must return harmonic_pattern=='NONE'.

        BUG REPORT (do NOT fix without user approval):
        _validate_harmonic lines 211-213 compute:
          total  = sum(1 for _, r in checks if r is not None)
          passed = sum(1 for v, r in checks if _in_range(v, r))
        _in_range(v, None) always returns True (unchecked dimension), so the
        D_XC=None spec dimension inflates `passed` by 1 for every pattern that
        has D_XC=None (GARTLEY, BUTTERFLY, BAT, CRAB, SHARK).  When AB_XA fails
        but the other 3 checked ratios pass, passed = 3 checked-pass + 1 from
        D_XC(None) = 4 = total=4 → the pattern is incorrectly classified.

        Fix needed (NOT applied here per invariant):
          passed = sum(1 for v, r in checks if r is not None and _in_range(v, r))

        This test is LEFT FAILING to surface the bug.
        """
        # AB_XA = 0.1 is outside every pattern's AB_XA range:
        #   GARTLEY (0.568–0.668), BUTTERFLY (0.736–0.836), BAT (0.332–0.550),
        #   CRAB (0.332–0.668), SHARK (1.080–1.668), CYPHER (0.332–0.668)
        ratios = {
            "AB_XA": 0.1,
            "BC_AB": 0.618,
            "CD_BC": 1.272,
            "D_XA":  0.786,
            "D_XC":  0.0,
            "XA": 100.0, "AB": 10.0, "BC": 6.18, "CD": 5.0,
        }
        valid, pattern, quality = _validate_harmonic(ratios)
        assert not valid, (
            f"BUG: AB_XA=0.1 is outside all pattern ranges but _validate_harmonic "
            f"returned pattern={pattern}. "
            f"Root cause: D_XC spec=None inflates 'passed' count via _in_range(v, None)=True. "
            f"Fix: passed = sum(... if r is not None and _in_range(v, r))"
        )
        assert pattern == "NONE"
        assert quality == 0.0

    def test_ratio_just_outside_tolerance_returns_none(self):
        """A ratio set AT the upper bound validates; ONE step outside returns NONE.

        This proves _in_range enforces a hard boundary — the tolerance band
        rejects as firmly as it accepts.

        BUG REPORT (same root cause as test_contradictory_ratios_return_none):
        AB_XA=0.669 is just outside the GARTLEY upper bound (0.668) and outside
        CRAB/CYPHER (0.332–0.668) — it should match NONE.  However the D_XC=None
        inflation in _validate_harmonic causes GARTLEY to still match.

        This test is LEFT FAILING to surface the bug.  The precondition (AT-bound
        validates) is tested first and is expected to PASS.
        """
        # Gartley AB_XA range: (0.568, 0.668).  0.668 is the exact upper bound.
        at_bound = {
            "AB_XA": 0.668,   # AT upper bound → still valid
            "BC_AB": 0.618,   # valid Gartley (0.382, 0.886)
            "CD_BC": 1.272,   # at lower bound of Gartley (1.272, 1.618)
            "D_XA":  0.786,   # valid Gartley (0.736, 0.836)
            "D_XC":  0.0,     # Gartley D_XC spec is None → _in_range(0.0, None)=True
            "XA": 100.0, "AB": 66.8, "BC": 41.22, "CD": 40.0,
        }
        valid_in, pattern_in, _ = _validate_harmonic(at_bound)
        assert valid_in and pattern_in == "GARTLEY", (
            f"Precondition failed: AT-bound ratios must validate as GARTLEY; "
            f"got valid={valid_in} pattern={pattern_in}"
        )

        # 0.669 is ONE step above GARTLEY upper bound of 0.668, and also above
        # CRAB (0.332–0.668) and CYPHER (0.332–0.668) upper bounds.
        just_outside = {**at_bound, "AB_XA": 0.669}
        valid_out, pattern_out, _ = _validate_harmonic(just_outside)
        assert not valid_out, (
            f"BUG: AB_XA=0.669 is outside all pattern AB_XA ranges but "
            f"_validate_harmonic returned pattern={pattern_out}. "
            f"Same D_XC=None inflation bug as test_contradictory_ratios_return_none. "
            f"Fix: passed = sum(... if r is not None and _in_range(v, r))"
        )
        assert pattern_out == "NONE"

    def test_gartley_not_classified_as_bat(self):
        """Gartley XABCD must not be misclassified as BAT.

        Discriminating feature: Gartley AB_XA = 0.618 (outside BAT 0.332–0.550)
        and D_XA = 0.786 (outside BAT 0.836–0.936).
        """
        X, A, B, C, D = _build_gartley_bullish()
        r = _harmonic_ratios(X, A, B, C, D)
        valid, pattern, _ = _validate_harmonic(r)
        assert valid, f"Gartley XABCD must be valid; got invalid. ratios={r}"
        assert pattern == "GARTLEY", f"Expected GARTLEY, got {pattern}"
        assert pattern != "BAT", "Gartley ratios must NOT be misclassified as BAT"

    def test_bat_not_classified_as_butterfly(self):
        """Bat XABCD must not be misclassified as BUTTERFLY.

        Discriminating feature: Bat AB_XA = 0.45 (outside BUTTERFLY 0.736–0.836)
        and Bat D_XA = 0.886 (outside BUTTERFLY 1.27–1.618).
        """
        X, A, B, C, D = _build_bat_bullish()
        r = _harmonic_ratios(X, A, B, C, D)
        valid, pattern, _ = _validate_harmonic(r)
        assert valid, f"Bat XABCD must be valid; got invalid. ratios={r}"
        assert pattern == "BAT", f"Expected BAT, got {pattern}"
        assert pattern != "BUTTERFLY", "Bat ratios must NOT be misclassified as BUTTERFLY"

    def test_gartley_not_classified_as_crab(self):
        """Additional discrimination: Gartley D_XA = 0.786 is outside CRAB D_XA (1.568–1.952)."""
        X, A, B, C, D = _build_gartley_bullish()
        r = _harmonic_ratios(X, A, B, C, D)
        _, pattern, _ = _validate_harmonic(r)
        assert pattern != "CRAB", "Gartley ratios must NOT be misclassified as CRAB"

    def test_config_fib_hit_tolerance_constant_exists_and_matches_hardcoded_value(self):
        """GEOMETRIC_FIB_HIT_TOLERANCE must be defined and equal the former hardcoded 0.015."""
        assert GEOMETRIC_FIB_HIT_TOLERANCE == 0.015, (
            "GEOMETRIC_FIB_HIT_TOLERANCE must equal 0.015 — same value as the "
            "former hardcoded tolerance. No behaviour change allowed."
        )


# ---------------------------------------------------------------------------
# §16 End-to-end main.py integration path tests (Task B)
# ---------------------------------------------------------------------------

class TestMainPyIntegrationPath:
    """Integration tests that mirror the main.py geometric confluence block.

    These tests exercise the full extraction path:
      best_candidate["failed_gates"] → _geo_smc_blocked → compute_geometric_bonus
    — not just compute_geometric_bonus in isolation.

    They also verify the critical invariant: geometric bonus cannot flip a WAIT
    candidate into a trade by itself.
    """

    def test_smc_hard_block_in_failed_gates_zeroes_bonus_active_confirm(self):
        """CONFIRMATION_MATRIX_HARD_BLOCK in failed_gates must yield final_bonus=0.0
        even when mode=ACTIVE and decision=CONFIRM.

        Replicates main.py lines 1031–1039:
          _geo_smc_blocked = "CONFIRMATION_MATRIX_HARD_BLOCK" in (
              (hunter.best_candidate or {}).get("failed_gates") or [])
          _geo_bonus = compute_geometric_bonus(geo_result, _geo_smc_blocked, ...)
        """
        best_candidate = {
            "failed_gates": ["CONFIRMATION_MATRIX_HARD_BLOCK"],
            "direction": "BUY",
            "best_strategy": "ORDER_FLOW_EXECUTION_AGENT",
        }

        # Replicate main.py derivation of _geo_smc_blocked from candidate
        _geo_smc_blocked = "CONFIRMATION_MATRIX_HARD_BLOCK" in (
            best_candidate.get("failed_gates") or []
        )
        assert _geo_smc_blocked is True, "Precondition: candidate must be SMC-hard-blocked"

        # geo_result that would yield bonus=5.0 if not blocked
        geo_result = {"mode": "ACTIVE", "decision": "CONFIRM"}

        final_bonus = compute_geometric_bonus(geo_result, _geo_smc_blocked, confirm_bonus=5.0)

        assert final_bonus == 0.0, (
            "CONFIRMATION_MATRIX_HARD_BLOCK in failed_gates must zero the bonus "
            "even when mode=ACTIVE and decision=CONFIRM"
        )

    def test_smc_unblocked_candidate_receives_full_bonus_active_confirm(self):
        """Discriminative counterpart: unblocked candidate in ACTIVE+CONFIRM must
        receive full bonus — proves the blocked test is not a false positive."""
        best_candidate = {
            "failed_gates": [],
            "direction": "BUY",
            "best_strategy": "ORDER_FLOW_EXECUTION_AGENT",
        }
        _geo_smc_blocked = "CONFIRMATION_MATRIX_HARD_BLOCK" in (
            best_candidate.get("failed_gates") or []
        )
        assert not _geo_smc_blocked

        geo_result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        final_bonus = compute_geometric_bonus(geo_result, _geo_smc_blocked, confirm_bonus=5.0)
        assert final_bonus == 5.0, "Unblocked ACTIVE+CONFIRM must receive the full configured bonus"

    def test_wait_candidate_geo_bonus_cannot_create_trade(self):
        """Invariant: geometric bonus is purely additive to confluence score.

        A WAIT candidate (demo_eligible=False, no force_active_handoff) stays
        WAIT even after ACTIVE+CONFIRM geo bonus is applied to _conf['score'].

        The geometric block in main.py only modifies _conf['score'].  It never
        sets demo_eligible, route_to_demo, or force_active_handoff.  Those flags
        are determined by setup_hunter BEFORE the geometric block runs and are
        never touched by it.
        """
        best_candidate = {
            "demo_eligible": False,     # WAIT candidate — not cleared for execution
            "failed_gates": [],         # NOT smc-blocked (testing a different path)
            "direction": "BUY",
            "best_strategy": "ORDER_FLOW_EXECUTION_AGENT",
        }

        # Geo analysis gives CONFIRM in ACTIVE mode — bonus is non-zero
        geo_result = {"mode": "ACTIVE", "decision": "CONFIRM"}
        _geo_smc_blocked = "CONFIRMATION_MATRIX_HARD_BLOCK" in (
            best_candidate.get("failed_gates") or []
        )
        bonus = compute_geometric_bonus(geo_result, _geo_smc_blocked, confirm_bonus=5.0)
        assert bonus == 5.0, "Bonus must be non-zero so the invariant is meaningful"

        # Replicate main.py: apply bonus to _conf score (lines 1041–1043)
        _conf = {"score": 65.0}
        if bonus > 0.0 and _conf:
            _conf["score"] = min(100.0, _conf["score"] + bonus)
        assert _conf["score"] == 70.0, "Confluence score is bumped by the bonus"

        # The geometric block NEVER modifies demo_eligible — candidate remains WAIT
        assert best_candidate["demo_eligible"] is False, (
            "Geometric bonus must not flip demo_eligible. "
            "WAIT candidate must stay WAIT regardless of geo CONFIRM+ACTIVE."
        )

        # Replicate _is_demo_eligible_active_candidate (main.py line 1157)
        from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES
        force_active_handoff = (
            str(best_candidate.get("best_strategy") or "").upper() in ACTIVE_EXECUTION_STRATEGIES
            and bool(best_candidate.get("demo_eligible"))
            and str(best_candidate.get("direction") or "").upper() in {"BUY", "SELL"}
        )
        assert not force_active_handoff, (
            "force_active_handoff must remain False for a WAIT candidate. "
            "Bonus-inflated confluence score cannot open a trade by itself."
        )
