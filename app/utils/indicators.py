from __future__ import annotations

import numpy as np
import pandas as pd


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["atr"] = atr(out)
    out["rsi"] = rsi(out["close"])
    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["volatility"] = out["atr"] / out["close"].replace(0, np.nan)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr_sma(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Legacy ATR — simple moving average of True Range. NOT the Wilder RMA
    standard (COEUR_V2 chantier 2, MATH_CORE_AUDIT.md RÉSERVÉ SIMO n°2): kept
    only for traceability/comparison. `atr()` no longer means this."""
    return true_range(df).rolling(period).mean()


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's RMA-smoothed ATR — the standard Welles Wilder introduced ATR
    with (`ATR_t = (ATR_{t-1}*(period-1) + TR_t) / period`, seeded by the
    simple average of the first `period` True Range values). This is now the
    canonical, system-wide `atr()` (COEUR_V2 chantier 2, 2026-07-08)."""
    tr = true_range(df)
    out = pd.Series(index=tr.index, dtype=float)
    n = len(tr)
    if n < period:
        return out
    seed = tr.iloc[:period].mean()
    out.iloc[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + tr.iloc[i]) / period
        out.iloc[i] = prev
    return out


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder RMA ATR (alias, COEUR_V2 chantier 2). Every call-site in this
    repo that reads `atr()`/`enrich_indicators()["atr"]` now receives the
    standard Wilder-smoothed value. See `atr_sma()` for the pre-migration
    (simple moving average) behaviour, kept for comparison."""
    return atr_wilder(df, period)


def atr_series_graceful(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Series version of `atr_last`'s graceful fallback: Wilder RMA from the
    point a full seed is available (index `period-1` onward), expanding-mean
    of True Range for the leading bars that don't have `period` bars of
    history yet (mirrors the pre-migration `tr.rolling(period, min_periods=1)`
    behaviour some call-sites relied on for a value on every bar, not just the
    last). Used where a caller indexes the ATR series per-bar, not just
    `.iloc[-1]` (see `atr_last` for the scalar case).
    """
    tr = true_range(df)
    leading = tr.expanding(min_periods=1).mean()
    wilder = atr_wilder(df, period)
    return wilder.combine_first(leading)


def atr_last(df: pd.DataFrame, period: int = 14) -> float | None:
    """Scalar convenience wrapper — the last Wilder ATR value, with a graceful
    SMA-of-available-bars fallback when there isn't enough history for a full
    Wilder seed (< `period` bars). Consolidates ~9 near-duplicate local ATR
    helpers found across the repo during COEUR_V2 chantier 2 (all of them were
    the pre-migration SMA formula) onto one shared, tested implementation.
    Returns None on empty/unusable input.
    """
    if df is None or len(df) < 2:
        return None
    tr = true_range(df)
    n = len(tr)
    if n >= period:
        wilder = atr_wilder(df, period)
        val = wilder.iloc[-1]
        if pd.notna(val):
            return float(val)
    val = tr.tail(period).mean()
    return float(val) if pd.notna(val) else None


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))
