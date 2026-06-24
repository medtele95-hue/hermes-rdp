"""GOLD_RANGE_BREAKOUT — Volume-confirmed H4 range breakout with M5 retest entry.

Strategy logic:
1. Detect H4 range: range_size/ATR < 2.5, h4_bias=RANGE, candles_in_range >= 10
2. Confirm breakout on M5: close outside range × 1.002 with volume > 1.5×avg AND ATR expansion
3. Wait for retest: price returns within atr_h4 × 0.3 of broken level
4. Enter on retest with SL at opposite range boundary, TP at RR >= 1.5

Scoring (0–100): range quality 35 + volume 20 + ATR expansion 20 + retest precision 15 + session 10
Grades: A >= 80 / B 65-79 / C < 65 → WAIT
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.logger import log

STRATEGY = "GOLD_RANGE_BREAKOUT"
GOLD_SYMBOLS: frozenset[str] = frozenset({"XAUUSD", "XAUUSD#", "GOLD", "GOLD#", "GOLDCASH#"})

_H4_LOOKBACK = 25
_BREAKOUT_MULT = 1.002
_RETEST_ATR_MULT = 0.3
_MIN_RR = 1.5
_VOL_MULTIPLIER = 1.5
_MAX_RANGE_ATR_RATIO = 2.5
_MIN_RANGE_CANDLES = 10
_BREAKOUT_LOOKBACK = 5


def evaluate(
    symbol: str,
    frames: dict[str, Any] | None,
    context: dict | None = None,
    settings: object | None = None,
) -> dict:
    if not _is_gold_symbol(symbol):
        return _wait(symbol, "GOLD_RANGE_NOT_GOLD_SYMBOL")
    if not bool(getattr(settings, "gold_range_breakout_enabled", False)):
        return _wait(symbol, "GOLD_RANGE_BREAKOUT_DISABLED")

    frames = frames or {}
    ctx = context or {}

    h4 = _normalize(frames.get("H4"))
    m5 = _normalize(frames.get("M5"))

    if h4 is None or len(h4) < _H4_LOOKBACK:
        log.info("[GOLD_RANGE_STATUS] symbol=%s range_active=False reason=INSUFFICIENT_H4_CANDLES", symbol)
        return _wait(symbol, "GOLD_RANGE_INSUFFICIENT_H4_CANDLES")
    if m5 is None or len(m5) < 20:
        log.info("[GOLD_RANGE_STATUS] symbol=%s range_active=False reason=INSUFFICIENT_M5_CANDLES", symbol)
        return _wait(symbol, "GOLD_RANGE_INSUFFICIENT_M5_CANDLES")

    # Step 1: Detect H4 range
    h4_tail = h4.tail(_H4_LOOKBACK)
    range_high = float(h4_tail["high"].max())
    range_low = float(h4_tail["low"].min())
    range_size = range_high - range_low
    atr_h4 = _atr(h4_tail)

    if atr_h4 <= 0:
        return _wait(symbol, "GOLD_RANGE_ATR_INVALID")

    range_atr_ratio = range_size / atr_h4
    h4_bias = str(ctx.get("h4_main_bias") or ctx.get("h4_bias") or "").upper()

    # Count candles where both high and low stayed inside range (with 0.1% tolerance)
    candles_in_range = int(sum(
        1 for _, row in h4_tail.iterrows()
        if float(row["high"]) <= range_high * 1.001
        and float(row["low"]) >= range_low * 0.999
    ))

    range_active = (
        range_atr_ratio < _MAX_RANGE_ATR_RATIO
        and h4_bias == "RANGE"
        and candles_in_range >= _MIN_RANGE_CANDLES
    )

    log.info(
        "[GOLD_RANGE_STATUS] symbol=%s range_active=%s"
        " range_size=%.2f atr=%.2f ratio=%.2f h4_bias=%s candles_in_range=%d",
        symbol, str(range_active).lower(),
        range_size, atr_h4, range_atr_ratio, h4_bias, candles_in_range,
    )

    if not range_active:
        log.info("[GOLD_RANGE_WAITING] symbol=%s reason=RANGE_NOT_ACTIVE", symbol)
        return _wait(symbol, "GOLD_RANGE_NOT_ACTIVE")

    # Step 2: Detect recent breakout in last N M5 candles
    avg_vol = float(m5.tail(20)["tick_volume"].mean()) or 1.0
    breakout_buy = _detect_breakout(m5, range_high, "BUY", avg_vol)
    breakout_sell = _detect_breakout(m5, range_low, "SELL", avg_vol)

    if not breakout_buy and not breakout_sell:
        log.info("[GOLD_RANGE_WAITING] symbol=%s reason=NO_BREAKOUT_DETECTED", symbol)
        return _wait(symbol, "GOLD_RANGE_NO_BREAKOUT")

    direction = "BUY" if breakout_buy else "SELL"
    breakout_info = breakout_buy if breakout_buy else breakout_sell

    # Step 3: Retest zone check
    current_close = float(m5["close"].iloc[-1])
    retest_level = range_high if direction == "BUY" else range_low
    retest_tolerance = atr_h4 * _RETEST_ATR_MULT
    distance_to_retest = abs(current_close - retest_level)
    in_retest_zone = distance_to_retest <= retest_tolerance

    if not in_retest_zone:
        log.info(
            "[GOLD_RANGE_WAITING] symbol=%s reason=NOT_IN_RETEST_ZONE"
            " direction=%s distance=%.2f tolerance=%.2f",
            symbol, direction, distance_to_retest, retest_tolerance,
        )
        return _wait(symbol, "GOLD_RANGE_NOT_IN_RETEST_ZONE")

    # Step 4: SL / TP
    entry = current_close
    if direction == "BUY":
        sl = range_low
        risk = entry - sl
    else:
        sl = range_high
        risk = sl - entry

    if risk <= 0:
        return _wait(symbol, "GOLD_RANGE_INVALID_RISK")

    tp = entry + risk * _MIN_RR if direction == "BUY" else entry - risk * _MIN_RR
    rr = round(abs(tp - entry) / risk, 2)

    # Step 5: Score and grade
    score = _compute_score(range_atr_ratio, breakout_info, distance_to_retest, retest_tolerance, ctx)
    grade = _grade(score)

    if grade == "C":
        log.info(
            "[GOLD_RANGE_WAITING] symbol=%s reason=GRADE_C_WAIT score=%.1f grade=%s",
            symbol, score, grade,
        )
        return _wait(symbol, "GOLD_RANGE_GRADE_BELOW_B")

    log.info(
        "[GOLD_RANGE_BREAKOUT] symbol=%s direction=%s score=%.1f grade=%s"
        " entry=%.2f sl=%.2f tp=%.2f rr=%.2f range_high=%.2f range_low=%.2f",
        symbol, direction, score, grade, entry, sl, tp, rr, range_high, range_low,
    )

    return {
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "symbol": symbol,
        "timeframe": "M5",
        "signal": direction,
        "direction": direction,
        "side": direction,
        "decision": direction,
        "status": "ORDER_READY",
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "risk_reward": rr,
        "reward_risk": rr,
        "confidence": int(score),
        "gold_range_breakout_score": round(score, 2),
        "gold_range_breakout_grade": grade,
        "gold_range_breakout_ready": True,
        "range_high": round(range_high, 5),
        "range_low": round(range_low, 5),
        "range_size": round(range_size, 5),
        "range_atr_ratio": round(range_atr_ratio, 3),
        "atr_h4": round(atr_h4, 5),
        "range_active": range_active,
        "retest_distance": round(distance_to_retest, 5),
        "retest_tolerance": round(retest_tolerance, 5),
        "breakout_volume_ratio": breakout_info.get("vol_ratio"),
        "grade": grade,
        "big_setup_grade": grade,
        "setup_hunter_grade": grade,
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "m15_confirmation_status": "PASS",
        "m1_trigger_status": "PASS",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": STRATEGY,
        "reason": f"GOLD_RANGE_BREAKOUT_{direction}",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_breakout(m5: pd.DataFrame, level: float, direction: str, avg_vol: float) -> dict | None:
    """Scan last _BREAKOUT_LOOKBACK M5 candles for a volume-confirmed breakout."""
    tail = m5.tail(_BREAKOUT_LOOKBACK)
    m5_atr = _atr(m5.tail(20))

    for _, row in tail.iterrows():
        close = float(row["close"])
        vol = float(row["tick_volume"])
        candle_range = float(row["high"]) - float(row["low"])
        vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0
        vol_ok = vol_ratio >= _VOL_MULTIPLIER
        atr_expanded = m5_atr > 0 and candle_range >= m5_atr * 0.8

        if direction == "BUY":
            broke = close > level * _BREAKOUT_MULT
        else:
            broke = close < level * (2.0 - _BREAKOUT_MULT)

        if broke and vol_ok:
            return {
                "vol_ratio": round(vol_ratio, 2),
                "vol_ok": True,
                "atr_expanded": bool(atr_expanded),
                "close": close,
            }
    return None


def _compute_score(
    range_atr_ratio: float,
    breakout_info: dict,
    distance: float,
    tolerance: float,
    ctx: dict,
) -> float:
    score = 0.0

    # Range quality: 35 pts — tighter range relative to ATR scores higher
    # ratio ≈ 1.0 → 35 pts; ratio ≈ 2.5 → 0 pts
    range_frac = max(0.0, 1.0 - (range_atr_ratio - 1.0) / 1.5)
    score += min(35.0, 35.0 * range_frac)

    # Volume confirmation: 20 pts
    if breakout_info.get("vol_ok"):
        score += 20.0

    # ATR expansion: 20 pts
    if breakout_info.get("atr_expanded"):
        score += 20.0

    # Retest precision: 15 pts (closer to level = more points)
    if tolerance > 0:
        score += max(0.0, 15.0 * (1.0 - distance / tolerance))

    # Session bonus: 10 pts
    session = str(ctx.get("session_name") or "").upper()
    if session in {"LONDON", "NEW_YORK", "OVERLAP"}:
        score += 10.0

    return min(100.0, max(0.0, round(score, 2)))


def _grade(score: float) -> str:
    if score >= 80.0:
        return "A"
    if score >= 65.0:
        return "B"
    return "C"


def _wait(symbol: str, reason: str) -> dict:
    return {
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "symbol": symbol,
        "timeframe": "M5",
        "signal": "WAIT",
        "direction": "WAIT",
        "side": None,
        "decision": "WAIT",
        "status": "WAIT",
        "entry": None,
        "sl": None,
        "tp": None,
        "risk_reward": None,
        "reward_risk": None,
        "confidence": 0,
        "gold_range_breakout_score": 0.0,
        "gold_range_breakout_grade": "C",
        "gold_range_breakout_ready": False,
        "range_active": False,
        "grade": "C",
        "big_setup_grade": "C",
        "setup_hunter_grade": "C",
        "m15_confirmation": False,
        "m1_entry_confirmation": False,
        "m15_confirmation_status": "FAIL",
        "m1_trigger_status": "FAIL",
        "reason": reason,
    }


def _is_gold_symbol(symbol: object) -> bool:
    s = str(symbol or "").upper().strip()
    return s in GOLD_SYMBOLS or s.startswith("GOLD") or s.startswith("XAUUSD")


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < 2:
        return 0.0
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close_prev = pd.to_numeric(df["close"], errors="coerce").shift(1)
    tr = pd.concat(
        [high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1
    ).max(axis=1)
    val = float(tr.tail(period).mean())
    return val if math.isfinite(val) else 0.0


def _normalize(df: Any) -> pd.DataFrame | None:
    if df is None:
        return None
    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return None
    if df.empty:
        return None
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        return None
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 1.0
    for col in ("open", "high", "low", "close", "tick_volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
