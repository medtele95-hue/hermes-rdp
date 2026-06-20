from __future__ import annotations

import importlib
import importlib.util
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np


STRATEGY = "SIMO_ATM_BREAKOUT"
SOURCE_MODULES = (
    "simo_atm_breakout",
    "app.strategies.simo_atm_breakout_source",
    "docs.strategies.simo_atm_breakout",
)
SOURCE_FILES = (
    Path.cwd() / "simo_atm_breakout.py",
    Path.cwd() / "docs" / "strategies" / "simo_atm_breakout.py",
)


def evaluate(symbol: str, frames: dict[str, Any] | None, context: dict | None = None, settings: object | None = None) -> dict:
    source = _source_callable()
    if source is None:
        # Use built-in ATM breakout implementation as fallback
        return _builtin_evaluate(symbol, frames or {}, context or {}, settings)
    try:
        raw = _call_source(source, symbol, frames or {}, context or {}, settings)
    except Exception as exc:
        return _payload(symbol, "WAIT", "SIMO_INTERNAL_ERROR", raw_payload={"error": str(exc)})
    return _normalize_source_result(symbol, raw)


def _source_callable() -> Callable | None:
    for module_name in SOURCE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        if module.__name__ == __name__:
            continue
        for name in ("evaluate", "generate_signal", "analyze", "strategy", "run_strategy"):
            fn = getattr(module, name, None)
            if callable(fn):
                return fn
    for path in SOURCE_FILES:
        module = _load_source_file(path)
        if module is None:
            continue
        for name in ("evaluate", "generate_signal", "analyze", "strategy", "run_strategy"):
            fn = getattr(module, name, None)
            if callable(fn):
                return fn
    return None


def _load_source_file(path: Path) -> Any | None:
    try:
        if not path.exists() or path.resolve() == Path(__file__).resolve():
            return None
        spec = importlib.util.spec_from_file_location("hermes_uploaded_simo_atm_breakout", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _call_source(fn: Callable, symbol: str, frames: dict[str, Any], context: dict, settings: object | None) -> Any:
    attempts = (
        lambda: fn(symbol=symbol, frames=frames, context=context, settings=settings),
        lambda: fn(symbol, frames, context, settings),
        lambda: fn(symbol, frames),
        lambda: fn(frames.get("M5") if isinstance(frames, dict) else frames),
    )
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return None


def _normalize_source_result(symbol: str, raw: Any) -> dict:
    if raw is None:
        return _payload(symbol, "WAIT", "SIMO_NO_SIGNAL")
    if isinstance(raw, str):
        return _payload(symbol, raw.upper(), None if raw.upper() in {"BUY", "SELL"} else "SIMO_NO_SIGNAL")
    if not isinstance(raw, dict):
        return _payload(symbol, "WAIT", "SIMO_UNSUPPORTED_SOURCE_OUTPUT", raw_payload={"source_output": str(raw)})
    direction = str(raw.get("signal") or raw.get("decision") or raw.get("direction") or "WAIT").upper()
    if direction not in {"BUY", "SELL"}:
        return _payload(symbol, "WAIT", str(raw.get("reason") or raw.get("block_reason") or "SIMO_NO_SIGNAL"), raw_payload=raw)
    entry = _first_float(raw, "entry", "entry_price", "price")
    sl = _first_float(raw, "sl", "stop_loss", "stop")
    tp = _first_float(raw, "tp", "take_profit", "target")
    rr = _first_float(raw, "risk_reward", "reward_risk", "rr")
    if rr is None and None not in {entry, sl, tp}:
        rr = _reward_risk(entry, sl, tp, direction)
    missing = [field for field, value in {"entry": entry, "sl": sl, "tp": tp, "rr": rr}.items() if value is None]
    if missing:
        return _payload(symbol, "WAIT", "SIMO_MISSING_ENTRY_SL_TP", raw_payload={**raw, "missing": missing})
    confidence = _first_float(raw, "confidence", "score", "setup_score") or 0.0
    if confidence <= 1.0:
        confidence *= 100.0
    return _payload(
        symbol,
        direction,
        None,
        confidence=int(max(0, min(100, round(confidence)))),
        entry=entry,
        sl=sl,
        tp=tp,
        rr=rr,
        raw_payload=raw,
    )


def _builtin_evaluate(
    symbol: str,
    frames: dict[str, Any],
    context: dict,
    settings: object | None,
) -> dict:
    """Built-in ATM impulse-breakout implementation.

    Detects when price closes above a recent swing high (BUY) or below a
    recent swing low (SELL) with sufficient ATR impulse.  Used when no
    external SIMO source module is present.
    """
    tf = getattr(settings, "simo_atm_timeframe", "M5") if settings else "M5"
    rates = frames.get(tf) if isinstance(frames, dict) else None

    # Minimal data guard
    try:
        import pandas as pd
        if rates is None or not isinstance(rates, pd.DataFrame) or rates.empty or len(rates) < 20:
            return _payload(symbol, "WAIT", "SIMO_INSUFFICIENT_DATA")
    except ImportError:
        return _payload(symbol, "WAIT", "SIMO_PANDAS_UNAVAILABLE")

    atr_period = int(getattr(settings, "simo_atm_atr_period", 14)) if settings else 14
    impulse_mult = float(getattr(settings, "simo_atm_impulse_atr_mult", 1.6)) if settings else 1.6
    close_zone = float(getattr(settings, "simo_atm_close_zone", 0.70)) if settings else 0.70
    swing_bars = int(getattr(settings, "simo_atm_swing_bars", 10)) if settings else 10
    entry_buf = int(getattr(settings, "simo_atm_entry_buffer_points", 20)) if settings else 20
    sl_buf = int(getattr(settings, "simo_atm_sl_buffer_points", 30)) if settings else 30
    target_rr = float(getattr(settings, "simo_atm_rr", 2.0)) if settings else 2.0

    atr_val = _calc_atr(rates, atr_period)
    if not atr_val or atr_val <= 0:
        return _payload(symbol, "WAIT", "SIMO_ATR_UNAVAILABLE")

    n = len(rates)
    if n < swing_bars + 2:
        return _payload(symbol, "WAIT", "SIMO_INSUFFICIENT_DATA")

    # Estimate price point size from the price magnitude
    close_price = float(rates["close"].iloc[-1])
    point = 0.1 if close_price > 1000 else (0.01 if close_price > 10 else 0.0001)

    # Swing high/low from the lookback window (exclude last bar)
    window = rates.iloc[max(0, n - swing_bars - 2) : n - 1]
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())

    cur_close = float(rates["close"].iloc[-1])
    cur_high = float(rates["high"].iloc[-1])
    cur_low = float(rates["low"].iloc[-1])
    bar_range = cur_high - cur_low

    # BUY: close above swing high with ATR impulse and close in upper zone of bar
    buy_signal = (
        cur_close > swing_high
        and (cur_close - cur_low) >= atr_val * impulse_mult
        and (bar_range > 0 and (cur_close - cur_low) / bar_range >= close_zone)
    )

    # SELL: close below swing low with ATR impulse and close in lower zone of bar
    sell_signal = (
        cur_close < swing_low
        and (cur_high - cur_close) >= atr_val * impulse_mult
        and (bar_range > 0 and (cur_high - cur_close) / bar_range >= close_zone)
    )

    if buy_signal:
        entry = cur_close + entry_buf * point
        sl = swing_low - sl_buf * point
        risk = entry - sl
        if risk <= 0:
            return _payload(symbol, "WAIT", "SIMO_INVALID_RISK_BUY")
        tp = entry + target_rr * risk
        rr_val = _reward_risk(entry, sl, tp, "BUY")
        confidence = min(100, 65 + int((cur_close - swing_high) / atr_val * 20))
        return _payload(
            symbol, "BUY", None,
            confidence=confidence,
            entry=round(entry, 5),
            sl=round(sl, 5),
            tp=round(tp, 5),
            rr=rr_val,
            raw_payload={
                "builtin": True,
                "swing_high": round(swing_high, 5),
                "swing_low": round(swing_low, 5),
                "atr": round(atr_val, 5),
                "impulse_mult": impulse_mult,
            },
        )

    if sell_signal:
        entry = cur_close - entry_buf * point
        sl = swing_high + sl_buf * point
        risk = sl - entry
        if risk <= 0:
            return _payload(symbol, "WAIT", "SIMO_INVALID_RISK_SELL")
        tp = entry - target_rr * risk
        rr_val = _reward_risk(entry, sl, tp, "SELL")
        confidence = min(100, 65 + int((swing_low - cur_close) / atr_val * 20))
        return _payload(
            symbol, "SELL", None,
            confidence=confidence,
            entry=round(entry, 5),
            sl=round(sl, 5),
            tp=round(tp, 5),
            rr=rr_val,
            raw_payload={
                "builtin": True,
                "swing_high": round(swing_high, 5),
                "swing_low": round(swing_low, 5),
                "atr": round(atr_val, 5),
                "impulse_mult": impulse_mult,
            },
        )

    return _payload(symbol, "WAIT", "SIMO_NO_ATM_SETUP")


def _calc_atr(rates: Any, period: int = 14) -> float | None:
    """Wilder ATR calculation using numpy."""
    try:
        high = rates["high"].to_numpy(dtype=float)
        low = rates["low"].to_numpy(dtype=float)
        close = rates["close"].to_numpy(dtype=float)
    except Exception:
        return None
    n = len(high)
    if n < 2:
        return None
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    if n < period:
        val = float(tr.mean())
        return val if val > 0 and math.isfinite(val) else None
    atr = np.empty(n)
    atr[period - 1] = float(tr[:period].mean())
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    val = float(atr[n - 1])
    return val if val > 0 and math.isfinite(val) else None


def _payload(
    symbol: str,
    decision: str,
    reason: str | None,
    *,
    confidence: int = 0,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    rr: float | None = None,
    raw_payload: dict | None = None,
) -> dict:
    signal = decision if decision in {"BUY", "SELL"} else "WAIT"
    payload = {
        "symbol": symbol,
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "timeframe": "M5",
        "signal": signal,
        "direction": signal,
        "decision": signal,
        "status": "ORDER_READY" if signal in {"BUY", "SELL"} else "WAIT",
        "confidence": confidence,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk_reward": rr,
        "reward_risk": rr,
        "simo_atm_breakout": {
            "enabled": True,
            "mode": "ACTIVE_EXECUTION",
            "decision": signal,
            "confidence": confidence,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "block_reason": reason,
            "source_loaded": _source_callable() is not None,
            "raw_source_payload": raw_payload or {},
        },
        "simo_atm_signal": signal,
        "simo_atm_score": confidence,
        "simo_atm_reason": reason or f"SIMO_{signal}",
        "reason": reason or f"SIMO_{signal}",
        "raw_payload": raw_payload or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return payload


def _first_float(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = _to_float(payload.get(key))
        if value is not None:
            return value
    return None


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _reward_risk(entry: float, sl: float, tp: float, direction: str) -> float | None:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or reward <= 0:
        return None
    if direction == "BUY" and not (sl < entry < tp):
        return None
    if direction == "SELL" and not (tp < entry < sl):
        return None
    return round(reward / risk, 6)
