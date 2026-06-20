"""Pure broker-independent geometric and mathematical analysis utilities.

All functions are side-effect-free and operate on pandas DataFrames
with at least the columns: high, low, close (standard OHLC).
No MT5 calls, no side effects, fully testable in isolation.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atr_last(rates: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder-smoothed ATR(period) — returns the most recent value."""
    if rates is None or len(rates) < 2:
        return None
    high = rates["high"].to_numpy(dtype=float)
    low = rates["low"].to_numpy(dtype=float)
    close = rates["close"].to_numpy(dtype=float)
    n = len(high)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    if n < period:
        val = float(tr.mean())
        return val if val > 0 else None
    atr = np.empty(n)
    atr[period - 1] = float(tr[:period].mean())
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    val = float(atr[n - 1])
    return val if math.isfinite(val) and val > 0 else None


def _cluster_levels(levels: list[float], tolerance: float, zone_type: str) -> list[dict]:
    """Group nearby price levels into zones by proximity."""
    if not levels:
        return []
    sorted_levels = sorted(levels)
    clusters: list[dict] = []
    current: list[float] = [sorted_levels[0]]
    for level in sorted_levels[1:]:
        if level - current[-1] <= tolerance:
            current.append(level)
        else:
            clusters.append({
                "level": float(np.mean(current)),
                "strength": len(current),
                "type": zone_type,
            })
            current = [level]
    clusters.append({
        "level": float(np.mean(current)),
        "strength": len(current),
        "type": zone_type,
    })
    return sorted(clusters, key=lambda z: z["strength"], reverse=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def swing_high_low(rates: pd.DataFrame, lookback: int = 10) -> dict:
    """Find most recent swing high and low within the lookback window.

    A swing high is a bar where high >= both immediate neighbours.
    A swing low is a bar where low <= both immediate neighbours.
    If no strict pivot is found the window extremes are used as fallback.

    Returns:
        swing_high, swing_low (float or None)
        swing_high_idx, swing_low_idx (int or None)
    """
    if rates is None or len(rates) < 3:
        return {"swing_high": None, "swing_low": None,
                "swing_high_idx": None, "swing_low_idx": None}

    highs = rates["high"].to_numpy(dtype=float)
    lows = rates["low"].to_numpy(dtype=float)
    n = len(highs)
    end = n - 2          # exclude the current (possibly open) bar
    start = max(1, end - lookback)

    swing_high: float | None = None
    swing_high_idx: int | None = None
    swing_low: float | None = None
    swing_low_idx: int | None = None

    for i in range(end, start - 1, -1):
        if swing_high is None and highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            swing_high = float(highs[i])
            swing_high_idx = i
        if swing_low is None and lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            swing_low = float(lows[i])
            swing_low_idx = i
        if swing_high is not None and swing_low is not None:
            break

    # Fallback: use range extremes if no pivot found
    if swing_high is None and end >= start:
        rel = int(np.argmax(highs[start : end + 1]))
        swing_high_idx = start + rel
        swing_high = float(highs[swing_high_idx])
    if swing_low is None and end >= start:
        rel = int(np.argmin(lows[start : end + 1]))
        swing_low_idx = start + rel
        swing_low = float(lows[swing_low_idx])

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "swing_high_idx": swing_high_idx,
        "swing_low_idx": swing_low_idx,
    }


def support_resistance_zones(
    rates: pd.DataFrame,
    lookback: int = 50,
    tolerance_atr: float = 0.5,
) -> dict:
    """Identify support and resistance zones from recent pivot clusters.

    Groups nearby swing highs into resistance clusters and swing lows
    into support clusters.

    Returns:
        resistance_zones: list[{"level", "strength", "type"}]
        support_zones:    list[{"level", "strength", "type"}]
    """
    if rates is None or len(rates) < 5:
        return {"resistance_zones": [], "support_zones": []}

    recent = rates.tail(min(lookback, len(rates)))
    atr_val = _atr_last(recent) or 1.0
    tol = atr_val * tolerance_atr

    highs = recent["high"].to_numpy(dtype=float)
    lows = recent["low"].to_numpy(dtype=float)
    n = len(highs)

    pivot_highs: list[float] = []
    pivot_lows: list[float] = []

    for i in range(1, n - 1):
        if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            pivot_highs.append(highs[i])
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            pivot_lows.append(lows[i])

    return {
        "resistance_zones": _cluster_levels(pivot_highs, tol, "resistance"),
        "support_zones": _cluster_levels(pivot_lows, tol, "support"),
    }


def trendline_slope(points: list[tuple[float, float]]) -> float:
    """Compute OLS slope through a list of (x, price) points.

    Args:
        points: list of (bar_index, price) tuples.

    Returns:
        Slope (price change per bar). Returns 0.0 for < 2 points.
    """
    if len(points) < 2:
        return 0.0
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if float(np.std(xs)) == 0.0:
        return 0.0
    coeffs = np.polyfit(xs, ys, 1)
    slope = float(coeffs[0])
    return slope if math.isfinite(slope) else 0.0


def linear_regression_channel(rates: pd.DataFrame, lookback: int = 20) -> dict:
    """Fit OLS linear regression to close prices.

    Returns:
        slope, intercept, upper_channel, lower_channel, stdev,
        r_squared, current_value, direction (BULLISH|BEARISH|FLAT)
    """
    if rates is None or len(rates) < 3:
        return _empty_channel()

    recent = rates.tail(min(lookback, len(rates)))
    close = recent["close"].to_numpy(dtype=float)
    n = len(close)
    if float(np.std(close)) == 0.0:
        return _empty_channel()

    xs = np.arange(n, dtype=float)
    coeffs = np.polyfit(xs, close, 1)
    slope, intercept = float(coeffs[0]), float(coeffs[1])
    predicted = slope * xs + intercept
    residuals = close - predicted
    stdev = float(np.std(residuals))
    var_close = float(np.var(close))
    r_squared = max(0.0, min(1.0, 1.0 - float(np.var(residuals)) / var_close)) if var_close > 0 else 0.0
    current_value = slope * (n - 1) + intercept

    atr_val = _atr_last(rates) or (stdev * 2) or 1.0
    flat_threshold = atr_val * 0.005
    if slope > flat_threshold:
        direction = "BULLISH"
    elif slope < -flat_threshold:
        direction = "BEARISH"
    else:
        direction = "FLAT"

    return {
        "slope": slope,
        "intercept": intercept,
        "upper_channel": current_value + 2.0 * stdev,
        "lower_channel": current_value - 2.0 * stdev,
        "stdev": stdev,
        "r_squared": r_squared,
        "current_value": float(current_value),
        "direction": direction,
    }


def _empty_channel() -> dict:
    return {
        "slope": None,
        "intercept": None,
        "upper_channel": None,
        "lower_channel": None,
        "stdev": None,
        "r_squared": None,
        "current_value": None,
        "direction": "FLAT",
    }


def breakout_box(rates: pd.DataFrame, lookback: int = 20) -> dict:
    """Calculate the consolidation box (high/low) over the lookback window.

    The current (last) bar is excluded to avoid look-ahead.

    Returns:
        box_high, box_low, box_range, midpoint
    """
    if rates is None or len(rates) < 2:
        return {"box_high": None, "box_low": None, "box_range": None, "midpoint": None}

    # Exclude last bar
    window = rates.iloc[max(0, len(rates) - lookback - 1) : len(rates) - 1]
    if window.empty:
        return {"box_high": None, "box_low": None, "box_range": None, "midpoint": None}

    box_high = float(window["high"].max())
    box_low = float(window["low"].min())
    box_range = box_high - box_low
    midpoint = (box_high + box_low) / 2.0

    return {
        "box_high": box_high,
        "box_low": box_low,
        "box_range": box_range,
        "midpoint": midpoint,
    }


def range_compression_score(rates: pd.DataFrame, atr: float | None = None) -> float:
    """Score how compressed (tight) the recent price range is relative to ATR.

    1.0 = maximally compressed, 0.0 = fully expanded.
    """
    if rates is None or len(rates) < 5:
        return 0.0
    atr_val = atr or _atr_last(rates)
    if not atr_val or atr_val <= 0:
        return 0.0
    tail = rates.tail(5)
    recent_range = float(tail["high"].max() - tail["low"].min())
    expected_range = atr_val * 5.0
    compression = 1.0 - (recent_range / expected_range)
    return float(max(0.0, min(1.0, compression)))


def impulse_score(rates: pd.DataFrame, atr: float | None = None) -> float:
    """Measure momentum/impulse strength of the recent 3-bar move.

    0 = no impulse, 100 = strong impulse (≥ 3× ATR net move).
    """
    if rates is None or len(rates) < 3:
        return 0.0
    atr_val = atr or _atr_last(rates)
    if not atr_val or atr_val <= 0:
        return 0.0
    recent = rates.tail(3)
    net_move = abs(float(recent["close"].iloc[-1]) - float(recent["open"].iloc[0]))
    score = (net_move / atr_val) * 33.33
    return float(max(0.0, min(100.0, round(score, 2))))


def fibonacci_ote_zone(high: float, low: float) -> dict:
    """Calculate the Fibonacci Optimal Trade Entry (OTE) zone.

    The OTE golden zone is the 0.62–0.79 retracement level.
    For BUY setups price should return to this discount zone.
    For SELL setups price should return to the mirror premium zone.

    Returns:
        ote_low, ote_high, midpoint (0.705), range
    """
    if high is None or low is None or not math.isfinite(high) or not math.isfinite(low) or high <= low:
        return {"ote_low": None, "ote_high": None, "midpoint": None, "range": None}

    rng = high - low
    return {
        "ote_low": float(low + rng * 0.62),
        "ote_high": float(low + rng * 0.79),
        "midpoint": float(low + rng * 0.705),
        "range": float(rng),
    }


def premium_discount_zone(high: float, low: float, price: float) -> dict:
    """Classify current price as PREMIUM, DISCOUNT, or EQUILIBRIUM.

    Premium  = price above 60% of the high–low range (sell consideration).
    Discount = price below 40% of the range (buy consideration).
    Equilibrium = 40%–60%.

    Returns:
        zone ("PREMIUM"|"DISCOUNT"|"EQUILIBRIUM"|"UNKNOWN"),
        pct (0.0 at low, 1.0 at high),
        distance_to_50pct (signed)
    """
    if (
        high is None or low is None or price is None
        or not math.isfinite(high) or not math.isfinite(low) or not math.isfinite(price)
        or high <= low
    ):
        return {"zone": "UNKNOWN", "pct": None, "distance_to_50pct": None}

    rng = high - low
    pct = (price - low) / rng
    midpoint = low + rng * 0.5
    distance = price - midpoint

    if pct >= 0.60:
        zone = "PREMIUM"
    elif pct <= 0.40:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"

    return {"zone": zone, "pct": float(pct), "distance_to_50pct": float(distance)}


def triangle_or_wedge_detection(rates: pd.DataFrame) -> dict:
    """Detect converging/wedge structures by fitting trendlines to pivot highs and lows.

    Returns:
        pattern: ASCENDING_TRIANGLE | DESCENDING_TRIANGLE |
                 SYMMETRICAL_TRIANGLE | RISING_WEDGE | FALLING_WEDGE | NONE
        high_slope, low_slope, convergence (bool)
    """
    if rates is None or len(rates) < 10:
        return {"pattern": "NONE", "high_slope": None, "low_slope": None, "convergence": False}

    highs = rates["high"].to_numpy(dtype=float)
    lows = rates["low"].to_numpy(dtype=float)
    n = len(highs)
    atr_val = _atr_last(rates) or 1.0

    pivot_high_pts: list[tuple[float, float]] = []
    pivot_low_pts: list[tuple[float, float]] = []

    for i in range(1, n - 1):
        if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            pivot_high_pts.append((float(i), float(highs[i])))
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            pivot_low_pts.append((float(i), float(lows[i])))

    if len(pivot_high_pts) < 2 or len(pivot_low_pts) < 2:
        return {"pattern": "NONE", "high_slope": None, "low_slope": None, "convergence": False}

    high_slope = trendline_slope(pivot_high_pts)
    low_slope = trendline_slope(pivot_low_pts)
    flat = atr_val * 0.001

    converging = (high_slope < -flat and low_slope > flat) or (
        abs(high_slope - low_slope) < abs(high_slope) * 0.5
    )

    if not converging:
        pattern = "NONE"
    elif abs(high_slope) <= flat and low_slope > flat:
        pattern = "ASCENDING_TRIANGLE"
    elif high_slope < -flat and abs(low_slope) <= flat:
        pattern = "DESCENDING_TRIANGLE"
    elif high_slope < -flat and low_slope > flat:
        pattern = "SYMMETRICAL_TRIANGLE"
    elif high_slope > flat and low_slope > flat:
        pattern = "RISING_WEDGE"
    elif high_slope < -flat and low_slope < -flat:
        pattern = "FALLING_WEDGE"
    else:
        pattern = "NONE"
        converging = False

    return {
        "pattern": pattern,
        "high_slope": float(high_slope),
        "low_slope": float(low_slope),
        "convergence": converging,
    }


def volatility_expansion_score(rates: pd.DataFrame, atr: float | None = None) -> float:
    """Score recent volatility expansion relative to baseline ATR.

    0 = flat/contracting, 100 = very high expansion.
    """
    if rates is None or len(rates) < 5:
        return 0.0
    atr_val = atr or _atr_last(rates)
    if not atr_val or atr_val <= 0:
        return 0.0
    recent = rates.tail(3)
    if len(recent) < 1:
        return 0.0
    rh = recent["high"].to_numpy(dtype=float)
    rl = recent["low"].to_numpy(dtype=float)
    avg_range = float((rh - rl).mean())
    ratio = avg_range / atr_val
    # 0.5x ATR → score 0, 1.5x ATR → score 100
    score = (ratio - 0.5) * 100.0
    return float(max(0.0, min(100.0, round(score, 2))))


def geometric_confluence_score(
    swing: dict | None = None,
    channel: dict | None = None,
    box: dict | None = None,
    ote: dict | None = None,
    pd_zone: dict | None = None,
    wedge: dict | None = None,
    compression: float = 0.0,
    impulse: float = 0.0,
    volatility: float = 0.0,
    current_price: float | None = None,
) -> dict:
    """Compute a composite geometric confluence score (0–100).

    Component weights:
      trend (channel r² × 15)     up to 15
      impulse (×0.15)             up to 15
      OTE zone hit                    15
      premium/discount zone           10
      compression (×10)           up to 10
      volatility expansion (×0.10)  up to 10
      breakout box position           10
      triangle/wedge pattern          15
      ─────────────────────────────────
      maximum                        100

    Returns:
        score (0–100), components (dict), dominant_pattern (str)
    """
    score = 0.0
    components: dict[str, float] = {}

    # Trend from channel
    ch = channel or {}
    ch_dir = ch.get("direction", "FLAT")
    ch_r2 = float(ch.get("r_squared") or 0.0)
    trend_score = ch_r2 * 15.0 if ch_dir in {"BULLISH", "BEARISH"} else 0.0
    score += trend_score
    components["trend"] = round(trend_score, 2)

    # Impulse
    imp_contrib = float(impulse or 0.0) * 0.15
    score += imp_contrib
    components["impulse"] = round(imp_contrib, 2)

    # OTE zone
    ote_score = 0.0
    if ote and current_price is not None:
        ol = ote.get("ote_low")
        oh = ote.get("ote_high")
        if ol is not None and oh is not None and ol <= current_price <= oh:
            ote_score = 15.0
    score += ote_score
    components["ote_zone"] = ote_score

    # Premium/discount zone
    pd_score = 10.0 if (pd_zone or {}).get("zone") in {"PREMIUM", "DISCOUNT"} else 0.0
    score += pd_score
    components["pd_zone"] = pd_score

    # Range compression
    comp_score = float(compression or 0.0) * 10.0
    score += comp_score
    components["compression"] = round(comp_score, 2)

    # Volatility expansion
    vol_score = min(10.0, float(volatility or 0.0) * 0.10)
    score += vol_score
    components["volatility"] = round(vol_score, 2)

    # Breakout box position
    box_score = 0.0
    bx = box or {}
    if current_price is not None:
        bh = bx.get("box_high")
        bl = bx.get("box_low")
        if bh is not None and current_price > bh:
            box_score = 10.0
        elif bl is not None and current_price < bl:
            box_score = 10.0
    score += box_score
    components["breakout_box"] = box_score

    # Triangle/wedge pattern
    wdg = wedge or {}
    dominant = wdg.get("pattern", "NONE") or "NONE"
    pat_score = 15.0 if (dominant != "NONE" and wdg.get("convergence")) else 0.0
    score += pat_score
    components["pattern"] = pat_score

    total = float(max(0.0, min(100.0, round(score, 2))))
    return {"score": total, "components": components, "dominant_pattern": dominant}
