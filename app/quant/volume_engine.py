"""Dynamic Volume Engine — B8 pillar for §4.0 Setup Quality Tier.

Identifies High-Volume Nodes (HVNs) via volume-weighted 1-D K-Means
(Rehankhanani, Appendix A15) and computes the break-vol-ratio for
structure-break quality assessment (A16).

ATR uses Wilder's RMA (Appendix A4, α = 1/n, seeded with SMA).
Volume: `real_volume` if available and > 0, else `tick_volume` (A2 caveat).

All functions are pure (no MT5 calls, no side effects).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

_DEFAULT_LOOKBACK: int = 200
_DEFAULT_N_HVN: int = 4
_K_MEANS_MAX_ITER: int = 100
_K_MEANS_EPS: float = 1e-6
_DEFAULT_VOL_SMA: int = 20
_BREAK_VOL_MIN_RATIO: float = 1.5


# ── Internal helpers ──────────────────────────────────────────────────────────

def _vol_col(df: pd.DataFrame) -> str | None:
    if "real_volume" in df.columns:
        return "real_volume"
    if "tick_volume" in df.columns:
        return "tick_volume"
    return None


def _weighted_kmeans_1d(
    prices: np.ndarray,
    weights: np.ndarray,
    k: int,
    max_iter: int = _K_MEANS_MAX_ITER,
    eps: float = _K_MEANS_EPS,
) -> np.ndarray:
    """Volume-weighted 1-D K-Means (Appendix A15).

    Init: centroids at volume-weighted quantiles of price distribution.
    Update: volume-weighted mean of assigned points.
    Convergence: max centroid shift < eps.
    Returns centroid array sorted ascending by price.
    """
    n = len(prices)
    k = min(k, n)

    # Init: volume-weighted quantile positions
    sorted_idx = np.argsort(prices)
    p_sorted = prices[sorted_idx]
    w_sorted = weights[sorted_idx]
    w_cum = np.cumsum(w_sorted)
    total_w = w_cum[-1]
    if total_w <= 0:
        return np.linspace(prices.min(), prices.max(), k)

    centroids = np.empty(k)
    for j in range(k):
        q = (j + 0.5) / k * total_w
        idx = int(np.searchsorted(w_cum, q))
        idx = min(idx, n - 1)
        centroids[j] = p_sorted[idx]

    for _ in range(max_iter):
        # Assign each price to nearest centroid (1-D)
        dists = np.abs(prices[:, None] - centroids[None, :])   # (n, k)
        assignments = np.argmin(dists, axis=1)

        # Update: volume-weighted mean per cluster
        new_centroids = centroids.copy()
        for m in range(k):
            mask = assignments == m
            if mask.any():
                wm = weights[mask]
                new_centroids[m] = np.dot(wm, prices[mask]) / wm.sum()

        if np.max(np.abs(new_centroids - centroids)) < eps:
            break
        centroids = new_centroids

    return np.sort(centroids)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_hvn_levels(
    df: pd.DataFrame,
    k: int = _DEFAULT_N_HVN,
    lookback: int = _DEFAULT_LOOKBACK,
) -> list[float]:
    """Return k HVN price levels via volume-weighted K-Means (Appendix A15).

    Uses typical price (HLC/3) as the price coordinate, bar volume as weight.
    Returns centroids sorted ascending; fewer than k returned if data is scarce.
    """
    data = df.tail(lookback)
    vc = _vol_col(data)
    if vc is None or len(data) < max(k * 3, 10):
        return []
    required = {"high", "low", "close"}
    if not required.issubset(data.columns):
        return []

    price = ((data["high"] + data["low"] + data["close"]) / 3).values.astype(float)
    vol = data[vc].values.astype(float)

    mask = ~(np.isnan(price) | np.isnan(vol) | (vol <= 0))
    price, vol = price[mask], vol[mask]
    if len(price) < k:
        return []

    p_min, p_max = float(np.nanmin(price)), float(np.nanmax(price))
    if p_max <= p_min or not math.isfinite(p_min) or not math.isfinite(p_max):
        return []

    try:
        centroids = _weighted_kmeans_1d(price, vol, k)
        return [float(c) for c in centroids if math.isfinite(c)]
    except Exception:
        return []


def compute_break_vol_ratio(
    df: pd.DataFrame,
    bar_offset: int = 1,
    sma_period: int = _DEFAULT_VOL_SMA,
) -> float | None:
    """Return volume[bar_offset] / SMA(volume, sma_period).  (Appendix A16)

    bar_offset=1 → last fully closed candle.
    Returns None when volume data or sufficient bars are unavailable.
    """
    vc = _vol_col(df)
    if vc is None:
        return None

    min_bars = sma_period + bar_offset + 2
    if len(df) < min_bars:
        return None

    series = df[vc].values.astype(float)
    bar_vol = series[-(bar_offset + 1)]
    sma_slice = series[-(sma_period + bar_offset + 1):-(bar_offset + 1)]

    if len(sma_slice) == 0:
        return None
    sma_vol = float(np.nanmean(sma_slice))
    if not sma_vol or sma_vol <= 0 or not math.isfinite(bar_vol) or bar_vol < 0:
        return None

    return float(bar_vol / sma_vol)


def price_near_hvn(
    price: float,
    hvn_levels: list[float],
    atr: float,
    tolerance_factor: float = 0.5,
) -> bool:
    """Return True when price is within tolerance_factor × ATR of any HVN."""
    if not hvn_levels or atr <= 0 or not math.isfinite(price):
        return False
    tol = tolerance_factor * atr
    return any(abs(price - hvn) <= tol for hvn in hvn_levels)


def compute_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder-smoothed ATR (Appendix A4): RMA(TR, period), seeded with SMA.

    α = 1/period.  Returns None when data is insufficient.
    """
    if len(df) < period + 1:
        return None
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        return None

    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(h)

    # True Range series (A4)
    tr = np.empty(n)
    tr[0] = h[0] - lo[0]
    for i in range(1, n):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))

    # Seed Wilder ATR with SMA of first `period` TR values
    atr_val = float(np.mean(tr[:period]))
    alpha = 1.0 / period
    for i in range(period, n):
        atr_val = atr_val * (1.0 - alpha) + tr[i] * alpha

    return atr_val if math.isfinite(atr_val) and atr_val > 0 else None


def evaluate_b8_context(
    df: pd.DataFrame,
    lookback: int = _DEFAULT_LOOKBACK,
    k: int = _DEFAULT_N_HVN,
    vol_sma_period: int = _DEFAULT_VOL_SMA,
) -> dict:
    """Compute B8 context dict for embedding in the candidate payload.

    Keys:
        hvn_levels      — list[float] of HVN price levels (K-Means centroids)
        break_vol_ratio — float | None; last-bar vol / SMA(vol, sma_period)
        real_break      — bool; True when break_vol_ratio ≥ 1.5
        atr             — float | None; Wilder ATR(14)

    Caller checks entry_near_hvn using price_near_hvn() once the entry price
    is known from the merged candidate payload.
    """
    result: dict = {
        "hvn_levels": [],
        "break_vol_ratio": None,
        "real_break": False,
        "atr": None,
    }
    if df is None or df.empty:
        return result
    try:
        result["hvn_levels"] = compute_hvn_levels(df, k=k, lookback=lookback)
        ratio = compute_break_vol_ratio(df, bar_offset=1, sma_period=vol_sma_period)
        result["break_vol_ratio"] = ratio
        result["real_break"] = ratio is not None and ratio >= _BREAK_VOL_MIN_RATIO
        result["atr"] = compute_atr(df)
    except Exception:
        pass
    return result
