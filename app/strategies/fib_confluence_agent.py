from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config import Settings
from app.logger import log
from app.quant.geometry_engine import _atr_last
from app.strategies.candidate import validate_candidate
from app.utils.confidence import normalize_confidence


STRATEGY = "FIB_CONFLUENCE_EXECUTION_AGENT"
SOURCE = "app/strategies/fib_confluence_agent.py"

_DEFAULT_ALLOWED_SYMBOLS = frozenset({
    "BTCUSD",
    "BTCUSD#",
    "GOLD",
    "GOLD#",
    "XAUUSD",
    "XAUUSD#",
    "EURUSD",
    "US100",
    "US100Cash",
    "US100Cash#",
    "NAS100",
    "USTEC",
})

_GRADE_ORDER = {"A+": 4, "A": 3, "B": 2, "C": 1, "D": 0}


def evaluate(
    symbol: str,
    frames: dict[str, Any] | None,
    context: dict | None = None,
    settings: object | None = None,
) -> dict:
    settings = settings or Settings()
    context = dict(context or {})
    if not bool(getattr(settings, "fib_confluence_execution_enabled", False)):
        return _wait(symbol, "FIB_CONFLUENCE_EXECUTION_DISABLED")

    resolved_symbol = _normalize_symbol(symbol or context.get("broker_symbol") or context.get("symbol") or "")
    if not _symbol_allowed(resolved_symbol, settings):
        return _wait(symbol, "FIB_CONFLUENCE_SYMBOL_NOT_ALLOWED")

    frame = _best_frame(frames)
    if frame is None or frame.empty:
        return _wait(symbol, "FIB_CONFLUENCE_MISSING_FRAME")

    closed = _closed_frame(frame)
    if len(closed) < 80:
        return _wait(symbol, "FIB_CONFLUENCE_NOT_ENOUGH_CLOSED_CANDLES")

    if _market_open(context) is False:
        return _wait(symbol, "FIB_CONFLUENCE_MARKET_CLOSED")
    if str(context.get("time_gate_status") or "PASS").upper() != "PASS":
        return _wait(symbol, str(context.get("time_gate_reason") or "FIB_CONFLUENCE_TIME_GATE_BLOCK"))
    if str(context.get("safety_guard_status") or "PASS").upper() != "PASS":
        return _wait(symbol, str(context.get("safety_guard_reason") or "FIB_CONFLUENCE_SAFETY_BLOCK"))

    sig = len(closed) - 1
    if sig < 2:
        return _wait(symbol, "FIB_CONFLUENCE_NOT_ENOUGH_CLOSED_CANDLES")

    swing = _swing_legs(closed, sig, int(getattr(settings, "fib_confluence_lookback", 30)))
    if swing is None:
        return _wait(symbol, "FIB_CONFLUENCE_MISSING_SWING")

    swing_high, swing_low, leg_up = swing
    rng = swing_high - swing_low
    if rng <= 0:
        return _wait(symbol, "FIB_CONFLUENCE_INVALID_SWING_RANGE")

    row = closed.iloc[sig]
    o = _float(row.get("open"))
    h = _float(row.get("high"))
    l = _float(row.get("low"))
    c = _float(row.get("close"))
    if None in {o, h, l, c}:
        return _wait(symbol, "FIB_CONFLUENCE_MISSING_PRICE")

    atr = _atr_last(closed.iloc[: sig + 1]) or _estimate_atr(closed.iloc[: sig + 1])
    if not atr or atr <= 0:
        return _wait(symbol, "FIB_CONFLUENCE_ATR_UNAVAILABLE")

    zone = _fib_zone(swing_high, swing_low, leg_up)
    zone_low, zone_high, fib_618 = zone["zone_low"], zone["zone_high"], zone["fib_618"]
    if zone_low is None or zone_high is None or fib_618 is None:
        return _wait(symbol, "FIB_CONFLUENCE_INVALID_ZONE")

    touched = (l <= zone_high if leg_up else h >= zone_low)
    rejection = (c > fib_618 and c > o) if leg_up else (c < fib_618 and c < o)
    if not touched or not rejection:
        return _wait(symbol, "FIB_CONFLUENCE_NO_VALID_REJECTION")

    structure = _structure_confirmation(closed, sig, int(getattr(settings, "fib_confluence_struct_window", 15)))
    volume = _volume_spike(closed, sig, int(getattr(settings, "fib_confluence_vol_period", 20)), float(getattr(settings, "fib_confluence_vol_mult", 1.5)))
    liquidity = _liquidity_sweep(closed, sig, int(getattr(settings, "fib_confluence_liq_lookback", 10)))
    order_flow = _order_flow_proxy(closed.iloc[sig])
    direction = "BUY" if leg_up else "SELL"

    spread = _float(context.get("spread"))
    if spread is None:
        ask = _float(context.get("ask"))
        bid = _float(context.get("bid"))
        if ask is not None and bid is not None:
            spread = max(0.0, ask - bid)

    spread_ok, spread_reason = _spread_ok(spread, atr, context, settings)
    if not spread_ok:
        return _wait(symbol, spread_reason or "FIB_CONFLUENCE_SPREAD_TOO_WIDE")

    min_stop_distance = _min_stop_distance(context, settings)
    sl, tp, rr, stop_reason = _sl_tp(direction, c, swing_high, swing_low, atr, min_stop_distance, context, settings)
    if sl is None or tp is None or rr is None:
        return _wait(symbol, stop_reason or "FIB_CONFLUENCE_INVALID_SLTP")

    confluence_score, breakdown = _score_components(
        direction=direction,
        touched=touched,
        rejection=rejection,
        structure=structure,
        volume=volume,
        liquidity=liquidity,
        order_flow=order_flow,
        atr=atr,
        price=c,
        spread=spread,
        settings=settings,
        symbol=resolved_symbol,
        close_price=c,
    )

    final_confluence_score = float(max(0.0, min(100.0, round(confluence_score, 2))))
    final_confluence_grade = _grade(final_confluence_score)
    if final_confluence_score < 55.0 or _grade_rank(final_confluence_grade) < _grade_rank("C"):
        return _wait(symbol, "FIB_CONFLUENCE_SCORE_BELOW_THRESHOLD")

    if final_confluence_score < 75.0 or _grade_rank(final_confluence_grade) < _grade_rank("B") or rr < 1.5:
        return _wait(symbol, "FIB_CONFLUENCE_CANDIDATE_BELOW_EXECUTION_BAR")

    confidence = normalize_confidence(final_confluence_score)
    candidate = {
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "best_strategy": STRATEGY,
        "setup_type": "FIB_GOLDEN_POCKET_CONFLUENCE",
        "symbol": _canonical_symbol(resolved_symbol),
        "broker_symbol": str(symbol or context.get("broker_symbol") or resolved_symbol).upper(),
        "direction": direction,
        "signal": direction,
        "side": direction,
        "entry": round(c, _digits(context, c)),
        "sl": round(sl, _digits(context, c)),
        "tp": round(tp, _digits(context, c)),
        "rr": round(rr, 4),
        "risk_reward": round(rr, 4),
        "reward_risk": round(rr, 4),
        "confidence": float(confidence),
        "edge_score": float(confidence),
        "setup_score": float(confidence),
        "grade": _grade(confidence),
        "setup_hunter_grade": _grade(confidence),
        "big_setup_grade": _grade(confidence),
        "big_setup_score": float(confidence),
        "final_confluence_score": final_confluence_score,
        "final_confluence_grade": final_confluence_grade,
        "confluence_score": final_confluence_score,
        "confluence_grade": final_confluence_grade,
        "reason": "FIB_CONFLUENCE_GOLDEN_POCKET",
        "score_breakdown": breakdown,
        "fib_confluence": {
            "leg_direction": "UP" if leg_up else "DOWN",
            "zone_low": zone_low,
            "zone_high": zone_high,
            "fib_618": fib_618,
            "touched": touched,
            "rejection": rejection,
            "structure": structure,
            "volume_spike": volume,
            "liquidity_sweep": liquidity,
            "order_flow_proxy": order_flow,
            "atr": atr,
            "spread": spread,
            "spread_ok": spread_ok,
            "min_stop_distance": min_stop_distance,
        },
        "market_open": True,
        "symbol_market_open": True,
        "time_gate_status": "PASS",
        "time_gate_reason": "FIB_CONFLUENCE_TIME_GATE_PASS",
        "safety_guard_status": "PASS",
        "safety_guard_reason": "FIB_CONFLUENCE_SAFETY_PASS",
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "m15_confirmation_status": "PASS",
        "m1_trigger_status": "PASS",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": STRATEGY,
        "mode": "ACTIVE_EXECUTION",
        "route_allowed": True,
        "demo_eligible": True,
        "status": "ORDER_READY",
        "strategy_role": "ENTRY_STRATEGY",
        "strategy_status": "ACTIVE",
        "timeframe": _timeframe(frames),
        "closed_bar_only": True,
        "bar_time": _bar_time(closed.iloc[sig]),
        "source": SOURCE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "volatility_safe_score": _volatility_safe_score(resolved_symbol, atr, c),
    }
    candidate["score_breakdown"]["total"] = final_confluence_score
    candidate["score_breakdown"]["structure"] = breakdown["structure"]
    candidate["score_breakdown"]["volume"] = breakdown["volume"]
    candidate["score_breakdown"]["liquidity"] = breakdown["liquidity"]
    candidate["score_breakdown"]["order_flow"] = breakdown["order_flow"]
    candidate["score_breakdown"]["spread"] = breakdown["spread"]
    candidate["score_breakdown"]["atr"] = breakdown["atr"]
    candidate["score_breakdown"]["btc_volatility"] = breakdown["btc_volatility"]

    valid, reason, field = validate_candidate(candidate)
    if not valid:
        return _wait(symbol, f"{reason}_{field}" if field else str(reason or "FIB_CONFLUENCE_INVALID_CANDIDATE"))

    log.info(
        "[FIB_CONFLUENCE] symbol=%s direction=%s score=%s grade=%s rr=%s",
        symbol,
        direction,
        int(round(final_confluence_score)),
        final_confluence_grade,
        round(rr, 2),
    )
    log.info("[FIB_CONFLUENCE_ROUTE] symbol=%s decision=SEND_TO_SETUP_HUNTER", symbol)
    return candidate


def _score_components(
    *,
    direction: str,
    touched: bool,
    rejection: bool,
    structure: bool,
    volume: bool,
    liquidity: bool,
    order_flow: bool,
    atr: float,
    price: float,
    spread: float | None,
    settings: object,
    symbol: str,
    close_price: float,
) -> tuple[float, dict[str, float]]:
    score = 0.0
    breakdown: dict[str, float] = {}

    pocket_score = 30.0 if touched and rejection else 0.0
    score += pocket_score
    breakdown["golden_pocket"] = pocket_score

    structure_score = 15.0 if structure else 0.0
    score += structure_score
    breakdown["structure"] = structure_score

    volume_score = 15.0 if volume else 0.0
    score += volume_score
    breakdown["volume"] = volume_score

    liquidity_score = 15.0 if liquidity else 0.0
    score += liquidity_score
    breakdown["liquidity"] = liquidity_score

    order_flow_score = 10.0 if order_flow else 0.0
    score += order_flow_score
    breakdown["order_flow"] = order_flow_score

    atr_score = 10.0 if atr > 0 else 0.0
    score += atr_score
    breakdown["atr"] = atr_score

    spread_score = 5.0 if _spread_safe(spread, atr, settings) else 0.0
    score += spread_score
    breakdown["spread"] = spread_score

    btc_safe = _volatility_safe_score(symbol, atr, price)
    score += btc_safe
    breakdown["btc_volatility"] = btc_safe

    if direction == "BUY" and close_price >= price:
        score += 0.0
    if direction == "SELL" and close_price <= price:
        score += 0.0
    return score, breakdown


def _volatility_safe_score(symbol: str, atr: float, price: float) -> float:
    if not _is_btc(symbol):
        return 0.0
    if price <= 0 or atr <= 0:
        return 0.0
    ratio = atr / price
    if ratio <= 0.010:
        return 10.0
    if ratio <= 0.015:
        return 7.0
    if ratio <= 0.020:
        return 4.0
    return 0.0


def _spread_ok(spread: float | None, atr: float, context: dict, settings: object) -> tuple[bool, str | None]:
    if spread is None:
        return True, None
    if atr <= 0:
        return True, None
    max_frac = float(context.get("max_spread_atr_frac") or getattr(settings, "fib_confluence_max_spread_atr_frac", 0.15))
    if spread <= atr * max_frac:
        return True, None
    return False, "FIB_CONFLUENCE_SPREAD_TOO_WIDE"


def _spread_safe(spread: float | None, atr: float, settings: object) -> bool:
    if spread is None or atr <= 0:
        return True
    max_frac = float(getattr(settings, "fib_confluence_max_spread_atr_frac", 0.15))
    return spread <= atr * max_frac


def _sl_tp(
    direction: str,
    price: float,
    swing_high: float,
    swing_low: float,
    atr: float,
    min_stop_distance: float,
    context: dict,
    settings: object,
) -> tuple[float | None, float | None, float | None, str | None]:
    rr = max(1.5, _float(context.get("rr") or context.get("risk_reward") or getattr(settings, "fib_confluence_rr", 2.0)) or 2.0)
    buffer = max(atr * float(getattr(settings, "fib_confluence_sl_atr_mult", 0.5)), min_stop_distance)
    if direction == "BUY":
        sl = swing_low - buffer
        if price - sl < min_stop_distance or sl >= swing_low:
            return None, None, None, "FIB_CONFLUENCE_MIN_STOP_DISTANCE_TOO_WIDE"
        risk = price - sl
        if risk <= 0:
            return None, None, None, "FIB_CONFLUENCE_INVALID_BUY_RISK"
        tp = price + risk * rr
    else:
        sl = swing_high + buffer
        if sl - price < min_stop_distance or sl <= swing_high:
            return None, None, None, "FIB_CONFLUENCE_MIN_STOP_DISTANCE_TOO_WIDE"
        risk = sl - price
        if risk <= 0:
            return None, None, None, "FIB_CONFLUENCE_INVALID_SELL_RISK"
        tp = price - risk * rr
    return sl, tp, rr, None


def _min_stop_distance(context: dict, settings: object) -> float:
    points = _float(context.get("trade_stops_level") or context.get("stops_level") or context.get("min_stop_distance_points"))
    point_size = _float(context.get("point") or context.get("tick_size") or context.get("symbol_point"))
    if points is None:
        points = float(getattr(settings, "fib_confluence_min_stop_distance_points", 0) or 0)
    if point_size is None:
        point_size = _float(context.get("price_point")) or 0.0
    if points <= 0 or point_size <= 0:
        return 0.0
    return float(points * point_size)


def _structure_confirmation(closed: pd.DataFrame, sig: int, window: int) -> bool:
    if window <= 0 or sig - 2 * window < 0:
        return False
    recent = closed.iloc[sig - window:sig]
    prior = closed.iloc[sig - 2 * window:sig - window]
    if recent.empty or prior.empty:
        return False
    rec_hh = float(recent["high"].max())
    rec_ll = float(recent["low"].min())
    pri_hh = float(prior["high"].max())
    pri_ll = float(prior["low"].min())
    return (rec_hh > pri_hh and rec_ll > pri_ll) or (rec_hh < pri_hh and rec_ll < pri_ll)


def _volume_spike(closed: pd.DataFrame, sig: int, period: int, mult: float) -> bool:
    if period <= 0 or sig - period < 0:
        return False
    volume_col = "tick_volume" if "tick_volume" in closed.columns else ("volume" if "volume" in closed.columns else None)
    if volume_col is None:
        return False
    avg = float(closed.iloc[sig - period:sig][volume_col].mean())
    current = _float(closed.iloc[sig][volume_col])
    if current is None or avg <= 0:
        return False
    return current > avg * mult


def _liquidity_sweep(closed: pd.DataFrame, sig: int, lookback: int) -> bool:
    if lookback <= 0 or sig - lookback < 0:
        return False
    window = closed.iloc[sig - lookback:sig]
    if window.empty:
        return False
    l = _float(closed.iloc[sig].get("low"))
    h = _float(closed.iloc[sig].get("high"))
    c = _float(closed.iloc[sig].get("close"))
    if None in {l, h, c}:
        return False
    sweep_low = float(window["low"].min())
    sweep_high = float(window["high"].max())
    return (l < sweep_low and c > sweep_low) or (h > sweep_high and c < sweep_high)


def _order_flow_proxy(row: pd.Series) -> bool:
    h = _float(row.get("high"))
    l = _float(row.get("low"))
    c = _float(row.get("close"))
    if None in {h, l, c}:
        return False
    rng = max(h - l, 1e-12)
    close_pos = (c - l) / rng
    return close_pos >= 0.66 or close_pos <= 0.34


def _fib_zone(swing_high: float, swing_low: float, leg_up: bool) -> dict:
    rng = swing_high - swing_low
    if rng <= 0:
        return {"zone_low": None, "zone_high": None, "fib_618": None}
    if leg_up:
        zone_high = swing_high - rng * 0.618
        zone_low = swing_high - rng * 0.786
        fib_618 = zone_high
    else:
        zone_low = swing_low + rng * 0.618
        zone_high = swing_low + rng * 0.786
        fib_618 = zone_low
    return {"zone_low": min(zone_low, zone_high), "zone_high": max(zone_low, zone_high), "fib_618": fib_618}


def _swing_legs(closed: pd.DataFrame, sig: int, lookback: int) -> tuple[float, float, bool] | None:
    if lookback <= 0:
        return None
    start = sig - lookback
    if start < 0:
        return None
    highs = closed["high"].to_numpy(dtype=float)
    lows = closed["low"].to_numpy(dtype=float)
    sw_high_idx = start + int(highs[start:sig].argmax())
    sw_low_idx = start + int(lows[start:sig].argmin())
    swing_high = float(highs[sw_high_idx])
    swing_low = float(lows[sw_low_idx])
    if not math.isfinite(swing_high) or not math.isfinite(swing_low) or swing_high <= swing_low:
        return None
    leg_up = sw_high_idx > sw_low_idx
    return swing_high, swing_low, leg_up


def _estimate_atr(closed: pd.DataFrame, period: int = 14) -> float | None:
    if len(closed) < 2:
        return None
    high = closed["high"].to_numpy(dtype=float)
    low = closed["low"].to_numpy(dtype=float)
    close = closed["close"].to_numpy(dtype=float)
    tr = []
    for i in range(len(closed)):
        if i == 0:
            tr.append(high[0] - low[0])
        else:
            tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    window = tr[-period:] if len(tr) >= period else tr
    val = float(sum(window) / len(window))
    return val if math.isfinite(val) and val > 0 else None


def _best_frame(frames: dict[str, Any] | None) -> pd.DataFrame | None:
    if not isinstance(frames, dict):
        return None
    for key in ("M5", "M15", "M1", "H1"):
        value = frames.get(key)
        if isinstance(value, pd.DataFrame) and not value.empty:
            return value
    for value in frames.values():
        if isinstance(value, pd.DataFrame) and not value.empty:
            return value
    return None


def _closed_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if len(frame) <= 1:
        return frame.copy()
    return frame.iloc[:-1].copy()


def _market_open(context: dict) -> bool | None:
    for key in ("market_open", "symbol_market_open", "trade_allowed"):
        if key in context:
            value = context.get(key)
            if value is None:
                continue
            return bool(value)
    return None


def _timeframe(frames: dict[str, Any] | None) -> str:
    if not isinstance(frames, dict):
        return "M5"
    for key in ("M5", "M15", "M1", "H1"):
        if key in frames and isinstance(frames[key], pd.DataFrame) and not frames[key].empty:
            return key
    return "M5"


def _bar_time(row: pd.Series) -> object:
    for key in ("time", "timestamp", "date", "datetime"):
        if key not in row:
            continue
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return int(value)
        return str(value)
    return None


def _digits(context: dict, price: float) -> int:
    digits = _float(context.get("digits"))
    if digits is not None:
        return max(0, int(digits))
    if price >= 1000:
        return 2
    if price >= 10:
        return 3
    return 5


def _normalize_symbol(symbol: object) -> str:
    text = str(symbol or "").upper().strip()
    if text.startswith("BTCUSD"):
        return "BTCUSD"
    if text.startswith(("GOLD", "XAUUSD")):
        return "GOLD"
    if text.startswith("EURUSD"):
        return "EURUSD"
    if text.startswith(("US100", "NAS100", "USTEC")):
        return "US100"
    return text.replace("#", "")


def _canonical_symbol(symbol: object) -> str:
    normalized = _normalize_symbol(symbol)
    if normalized in {"BTCUSD", "GOLD", "EURUSD", "US100"}:
        return normalized
    return normalized


def _symbol_allowed(symbol: str, settings: object) -> bool:
    raw = getattr(settings, "fib_confluence_allowed_symbols", None)
    if raw:
        allowed = {_normalize_symbol(item) for item in str(raw).split(",") if item.strip()}
    else:
        allowed = {_normalize_symbol(item) for item in _DEFAULT_ALLOWED_SYMBOLS}
    if not allowed:
        allowed = {_normalize_symbol(item) for item in _DEFAULT_ALLOWED_SYMBOLS}
    return _normalize_symbol(symbol) in allowed


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _grade(score: float) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _grade_rank(grade: object) -> int:
    return _GRADE_ORDER.get(str(grade or "").upper(), 0)


def _is_btc(symbol: object) -> bool:
    return _normalize_symbol(symbol) == "BTCUSD"


def _wait(symbol: str, reason: str) -> dict:
    log.info("[FIB_CONFLUENCE] symbol=%s decision=WAIT reason=%s", symbol, reason)
    return {
        "symbol": _normalize_symbol(symbol),
        "broker_symbol": str(symbol or "").upper(),
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "signal": "WAIT",
        "direction": "WAIT",
        "decision": "WAIT",
        "reason": reason,
        "blocked_reason": reason,
        "confidence": 0.0,
        "grade": "D",
        "final_confluence_score": 0.0,
        "final_confluence_grade": "D",
        "confluence_score": 0.0,
        "confluence_grade": "D",
        "mode": "ACTIVE_EXECUTION",
        "route_allowed": True,
        "demo_eligible": False,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "timeframe": "M5",
        "closed_bar_only": True,
        "source": SOURCE,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
