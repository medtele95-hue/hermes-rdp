"""CVD Absorption Detector — B9 pillar for §4.0 Setup Quality Tier.

Detects when cumulative delta (CVD) reaches a new extreme while price fails
to follow — the signature of smart money absorbing the flow at a key level.

  bear_absorption: CVD higher-high  AND  price NOT higher-high
                   → sellers absorbing buy-side pressure (top)
  bull_absorption: CVD lower-low    AND  price NOT lower-low
                   → buyers absorbing sell-side pressure (bottom)

CVD computed via the CLV (close-location value) proxy (Appendix A2/A13):
  clv_t = ((C-L) - (H-C)) / max(H-L, ε)  ∈ [-1, +1]
  delta_t = V_t * clv_t
  CVD_t = cumsum(delta)

A detected absorption is *validated* when at least one of the following
patterns is present in the last closed candle (Appendix A13):
  - Bullish engulfing / Bearish engulfing
  - Bullish pin (hammer): lower_wick ≥ 2×body AND upper_wick ≤ body, high volume
  - Bearish pin (shooting star): upper_wick ≥ 2×body AND lower_wick ≤ body, high volume
  - CVD flattening: |cvd_slope| < flat_eps in the order-flow snapshot

Only a *validated* absorption (valid=True) counts as B9.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

_LOOKBACK: int = 20
_CVD_FLATTEN_THRESHOLD: float = 20.0
_PIN_SHADOW_RATIO: float = 2.0
_PIN_VOL_MULTIPLIER: float = 1.2


def _compute_cvd_series(df: pd.DataFrame) -> np.ndarray | None:
    """Compute cumulative delta series using the CLV proxy (Appendix A2/A13).

    clv_t = ((C-L) - (H-C)) / max(H-L, ε)   ∈ [-1, +1]
    delta_t = V_t * clv_t
    CVD = cumsum(delta)

    More accurate than a plain close>open direction because it captures where
    price closed within the bar's range, not just whether close > open.
    """
    vol_col = (
        "real_volume"
        if "real_volume" in df.columns
        else ("tick_volume" if "tick_volume" in df.columns else None)
    )
    if vol_col is None:
        return None
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        return None

    vol = df[vol_col].values.astype(float)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    c = df["close"].values.astype(float)

    hl = np.maximum(h - lo, 1e-10)
    clv = ((c - lo) - (h - c)) / hl        # close-location value ∈ [-1, 1]
    return np.nancumsum(vol * clv)


def _is_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    c = df.iloc[-2]
    p = df.iloc[-3]
    try:
        co, cc = float(c["open"]), float(c["close"])
        po, pc = float(p["open"]), float(p["close"])
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(v) for v in (co, cc, po, pc)):
        return False
    bull = cc > co and pc < po and cc >= po and co <= pc
    bear = cc < co and pc > po and cc <= po and co >= pc
    return bull or bear


def _is_pin_bar_with_volume(df: pd.DataFrame) -> bool:
    if len(df) < 5:
        return False
    c = df.iloc[-2]
    try:
        h = float(c["high"])
        lo = float(c["low"])
        o = float(c["open"])
        cl = float(c["close"])
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(v) for v in (h, lo, o, cl)):
        return False
    body = abs(cl - o)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - lo
    if (h - lo) <= 0 or body <= 0:
        return False
    # Directional pin bars (Appendix A13):
    # Bullish pin (hammer)      : lower_wick >= 2×body  AND  upper_wick <= body
    # Bearish pin (shooting star): upper_wick >= 2×body  AND  lower_wick <= body
    # +1e-9 relative tolerance guards against floating-point arithmetic differences
    # when wicks and body are computed from the same base price.
    tol = body * 1e-9
    bull_pin = lower_wick >= _PIN_SHADOW_RATIO * body and upper_wick <= body + tol
    bear_pin = upper_wick >= _PIN_SHADOW_RATIO * body and lower_wick <= body + tol
    if not (bull_pin or bear_pin):
        return False
    vol_col = (
        "real_volume"
        if "real_volume" in df.columns
        else ("tick_volume" if "tick_volume" in df.columns else None)
    )
    if vol_col is None:
        return True
    vols = df[vol_col].values.astype(float)
    bar_vol = vols[-2]
    ref = vols[-21:-2] if len(vols) >= 21 else vols[:-2]
    avg = float(np.nanmean(ref)) if len(ref) > 0 else 0.0
    return avg > 0 and math.isfinite(bar_vol) and bar_vol >= _PIN_VOL_MULTIPLIER * avg


def _is_cvd_flattening(of_snapshot: dict) -> bool:
    slope = of_snapshot.get("cvd_slope")
    if slope is None:
        return False
    try:
        return abs(float(slope)) < _CVD_FLATTEN_THRESHOLD
    except (TypeError, ValueError):
        return False


def detect_absorption_context(
    df: pd.DataFrame,
    of_snapshot: dict | None = None,
    lookback: int = _LOOKBACK,
) -> dict:
    """Compute B9 absorption context dict for embedding in the candidate payload.

    Returns:
        bear_absorption — bool: CVD HH + price NOT HH
        bull_absorption — bool: CVD LL + price NOT LL
        pattern        — 'engulf' | 'pin' | 'cvd_flatten' | None
        valid          — bool: absorption detected AND at least one pattern
    """
    result: dict = {
        "bear_absorption": False,
        "bull_absorption": False,
        "pattern": None,
        "valid": False,
    }
    if df is None or df.empty or len(df) < lookback + 3:
        return result

    try:
        cvd = _compute_cvd_series(df)
        if cvd is None or len(cvd) < lookback + 3:
            return result

        if "close" not in df.columns:
            return result

        close = df["close"].values.astype(float)

        # Window: lookback confirmed bars (exclude live candle at index -1)
        cvd_win = cvd[-(lookback + 2):-1]
        price_win = close[-(lookback + 2):-1]

        if len(cvd_win) < 3 or len(price_win) < 3:
            return result

        cvd_cur = float(cvd_win[-1])
        cvd_prev_max = float(np.nanmax(cvd_win[:-1]))
        cvd_prev_min = float(np.nanmin(cvd_win[:-1]))
        price_cur = float(price_win[-1])
        price_prev_max = float(np.nanmax(price_win[:-1]))
        price_prev_min = float(np.nanmin(price_win[:-1]))

        bear = math.isfinite(cvd_cur) and cvd_cur > cvd_prev_max and price_cur <= price_prev_max
        bull = math.isfinite(cvd_cur) and cvd_cur < cvd_prev_min and price_cur >= price_prev_min

        result["bear_absorption"] = bool(bear)
        result["bull_absorption"] = bool(bull)

        if bear or bull:
            snap = of_snapshot or {}
            if _is_engulfing(df):
                result["pattern"] = "engulf"
            elif _is_pin_bar_with_volume(df):
                result["pattern"] = "pin"
            elif _is_cvd_flattening(snap):
                result["pattern"] = "cvd_flatten"
            result["valid"] = result["pattern"] is not None

    except Exception:
        pass

    return result


def absorption_against_position(direction: str, ctx: dict | None) -> bool:
    """Return True when a *validated* absorption is against the open position.

    bear_absorption against a BUY → sellers absorbing → danger
    bull_absorption against a SELL → buyers absorbing → danger
    Requires ctx["valid"] == True to avoid noise from unvalidated detections.
    """
    if not ctx or not ctx.get("valid"):
        return False
    if direction == "BUY" and ctx.get("bear_absorption"):
        return True
    if direction == "SELL" and ctx.get("bull_absorption"):
        return True
    return False
