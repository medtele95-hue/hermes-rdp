from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.config import Settings
from app.logger import log


STRATEGY = "GOLD_M1_M5_EMA_SWEEP_SCALPER"
SOURCE = "docs/strategies/gold_m1m5_scalper"

EMA_TREND = (20, 50, 200)
EMA_FAST = 9
EMA_SLOW = 21
PULLBACK_ATR = 0.5
CROSS_LOOKBACK = 3
SETUP_WINDOW = 8
RSI_PERIOD = 14
RSI_BUY = (52, 68)
RSI_SELL = (32, 48)
RSI_BUY_REJECT = 70
RSI_SELL_REJECT = 30
ATR_PERIOD = 14
ATR_BAND = (0.25, 0.85)
ATR_BAND_WINDOW = 200
FRACTAL = 2
W_TREND, W_CROSS, W_RSI, W_ATR, W_STRUCT, W_SWEEP, W_SPREAD, W_SESSION = 20, 15, 10, 10, 15, 20, 5, 5
MIN_SCORE_TRADE = 75
MIN_SCORE_WAIT = 60
SL_ATR_MULT = 0.3
TP1_R = 1.0
TP2_R = 1.5
MAX_SPREAD_POINTS = 30
LONDON = (7, 12)
NEWYORK = (12, 21)


def evaluate(symbol: str, frames: dict[str, Any], context: dict | None, settings: Settings) -> dict:
    if not _is_gold_symbol(symbol):
        return _signal_from_result(symbol, _decision("WAIT", 0, blocked="SYMBOL_NOT_GOLD", symbol=symbol))
    try:
        result = evaluate_gold_m1m5_scalper(symbol, (frames or {}).get("M1"), (frames or {}).get("M5"), context or {}, settings)
    except Exception as exc:
        result = _decision("WAIT", 0, blocked="GOLD_M1_M5_INTERNAL_ERROR", symbol=symbol)
        result["error"] = str(exc)
    _log_result(result)
    return _signal_from_result(symbol, result)


def evaluate_gold_m1m5_scalper(symbol: str, m1: pd.DataFrame, m5: pd.DataFrame, context: dict | None, settings: Settings) -> dict:
    m1_raw = _normalize_candles(m1)
    m5_df = _closed_frame(m5)
    if m1_raw.empty:
        return _decision("WAIT", 0, blocked="NO_M1_CANDLES", symbol=symbol)
    if m5_df.empty:
        return _decision("WAIT", 0, blocked="NO_M5_CANDLES", symbol=symbol)
    m1_df = _prepare_entry(_closed_frame(m1_raw))
    required_m1 = max(ATR_BAND_WINDOW + ATR_PERIOD + 2, EMA_SLOW + CROSS_LOOKBACK + 2, FRACTAL * 2 + 2)
    required_m5 = max(EMA_TREND) + 2
    if len(m1_df) < required_m1 or len(m5_df) < required_m5:
        return _decision("WAIT", 0, blocked="NOT_ENOUGH_CANDLES", symbol=symbol)
    trend_dir, trend_reason = _trend_context(m5_df)
    spread_points = _spread_points(context or {}, settings)
    hour = _hour(context or {})
    relaxed = _relaxed_state(settings, context or {})
    return _analyze(symbol, m1_df, m5_df, trend_dir, spread_points, hour, trend_reason, settings, relaxed)


def ema(values: pd.Series, period: int) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").ewm(span=period, adjust=False).mean()


def rsi(values: pd.Series, period: int) -> pd.Series:
    close = pd.to_numeric(values, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr(candles: pd.DataFrame, period: int) -> pd.Series:
    high = pd.to_numeric(candles["high"], errors="coerce")
    low = pd.to_numeric(candles["low"], errors="coerce")
    close = pd.to_numeric(candles["close"], errors="coerce")
    previous_close = close.shift(1)
    tr = pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _prepare_entry(m1: pd.DataFrame) -> pd.DataFrame:
    df = m1.copy()
    if df.empty:
        return df
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    df["atr_lo"] = df["atr"].rolling(ATR_BAND_WINDOW).quantile(ATR_BAND[0])
    df["atr_hi"] = df["atr"].rolling(ATR_BAND_WINDOW).quantile(ATR_BAND[1])
    swing_highs = []
    swing_lows = []
    last_high = math.nan
    last_low = math.nan
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    for i in range(len(df)):
        j = i - FRACTAL
        if j >= FRACTAL and j + FRACTAL < len(df):
            high_window = highs[j - FRACTAL : j + FRACTAL + 1]
            low_window = lows[j - FRACTAL : j + FRACTAL + 1]
            if highs[j] == max(high_window):
                last_high = highs[j]
            if lows[j] == min(low_window):
                last_low = lows[j]
        swing_highs.append(last_high)
        swing_lows.append(last_low)
    df["swing_high"] = swing_highs
    df["swing_low"] = swing_lows
    return df


def _trend_context(m5: pd.DataFrame) -> tuple[int, str]:
    e1, e2, e3 = (ema(m5["close"], period) for period in EMA_TREND)
    if bool(e1.iloc[-1] > e2.iloc[-1] > e3.iloc[-1]):
        return 1, "M5_EMA20_50_200_BULL"
    if bool(e1.iloc[-1] < e2.iloc[-1] < e3.iloc[-1]):
        return -1, "M5_EMA20_50_200_BEAR"
    return 0, "M5_EMA_MIXED"


def _setup_detection(m1: pd.DataFrame, direction: int, i: int) -> bool:
    if direction == 0:
        return False
    close = m1["close"].to_numpy()
    slow = m1["ema_slow"].to_numpy()
    atr_values = m1["atr"].to_numpy()
    for k in range(max(0, i - SETUP_WINDOW), i + 1):
        if math.isfinite(atr_values[k]) and abs(close[k] - slow[k]) < PULLBACK_ATR * atr_values[k]:
            return True
    return False


def _cross(m1: pd.DataFrame, i: int, direction: int) -> bool:
    if i - CROSS_LOOKBACK < 0:
        return False
    fast = m1["ema_fast"].to_numpy()
    slow = m1["ema_slow"].to_numpy()
    if direction == 1:
        return bool(fast[i] > slow[i] and fast[i - CROSS_LOOKBACK] <= slow[i - CROSS_LOOKBACK])
    return bool(fast[i] < slow[i] and fast[i - CROSS_LOOKBACK] >= slow[i - CROSS_LOOKBACK])


def _entry_confirmation(m1: pd.DataFrame, direction: int, i: int) -> dict:
    fast = m1["ema_fast"].to_numpy()
    close = m1["close"].to_numpy()
    high = m1["high"].to_numpy()
    low = m1["low"].to_numpy()
    swing_high = float(m1["swing_high"].iloc[i])
    swing_low = float(m1["swing_low"].iloc[i])
    cross = _cross(m1, i, direction)
    beyond = close[i] > fast[i] if direction == 1 else close[i] < fast[i]
    if direction == 1:
        struct = math.isfinite(swing_high) and close[i] > swing_high
        sweep = math.isfinite(swing_low) and low[i] < swing_low and close[i] > swing_low
    else:
        struct = math.isfinite(swing_low) and close[i] < swing_low
        sweep = math.isfinite(swing_high) and high[i] > swing_high and close[i] < swing_high
    return {"cross": cross, "beyond": beyond, "struct": struct, "sweep": sweep, "confirmed": cross and beyond and (struct or sweep)}


def _analyze(symbol: str, m1: pd.DataFrame, m5: pd.DataFrame, trend_dir: int, spread_points: float, hour: int, trend_reason: str, settings: Settings, relaxed: dict | None = None) -> dict:
    max_spread = _to_float(getattr(settings, "max_spread_gold", None)) or MAX_SPREAD_POINTS
    relaxed = relaxed or {"active": False, "reason": None, "hours_without_setup": None}
    min_score_trade = int(getattr(settings, "gold_m1m5_min_score_relaxed", 65) if relaxed["active"] else getattr(settings, "gold_m1m5_min_score_strict", MIN_SCORE_TRADE))
    i = len(m1) - 1
    row = m1.iloc[i]
    atr_value = _to_float(row.get("atr"))
    atr_lo = _to_float(row.get("atr_lo"))
    atr_hi = _to_float(row.get("atr_hi"))
    pullback_ok = False
    if all(_to_float(row.get(name)) is not None for name in ("atr", "atr_lo", "atr_hi", "rsi")) and trend_dir != 0:
        pullback_ok = _setup_detection(m1, trend_dir, i)
    m5_snapshot = _m5_snapshot(m5, trend_dir)
    base = {
        **m5_snapshot,
        "ema9_m1": _to_float(row.get("ema_fast")),
        "ema21_m1": _to_float(row.get("ema_slow")),
        "rsi": _to_float(row.get("rsi")),
        "atr": atr_value,
        "atr_lo": atr_lo,
        "atr_hi": atr_hi,
        "atr_percentile": _atr_band_position(atr_value, atr_lo, atr_hi),
        "swing_high": _to_float(row.get("swing_high")),
        "swing_low": _to_float(row.get("swing_low")),
        "spread": spread_points,
        "spread_points": spread_points,
        "max_spread_points": max_spread,
        "pullback_ok": pullback_ok,
        "min_score_trade": min_score_trade,
        "relaxed_mode_active": relaxed["active"],
        "relaxed_reason": relaxed["reason"],
        "hours_without_setup": relaxed["hours_without_setup"],
        "strict_threshold": int(getattr(settings, "gold_m1m5_min_score_strict", MIN_SCORE_TRADE)),
        "relaxed_threshold": int(getattr(settings, "gold_m1m5_min_score_relaxed", 65)),
        "atr_relax_factor": float(getattr(settings, "gold_m1m5_atr_low_relax_factor", 0.80)),
        "score_components": {},
    }
    if any(_to_float(row.get(name)) is None for name in ("atr", "atr_lo", "atr_hi", "swing_high", "swing_low", "rsi")):
        return _decision("WAIT", 0, blocked="WARMUP", symbol=symbol, **base)
    if spread_points > max_spread:
        return _decision("WAIT", 0, reasons=[f"spread {spread_points:.0f}>{max_spread}"], blocked="SPREAD_HIGH", symbol=symbol, **base)
    atr_low_threshold = float(row["atr_lo"])
    if relaxed["active"]:
        atr_low_threshold *= float(getattr(settings, "gold_m1m5_atr_low_relax_factor", 0.80))
    if atr_value is None or atr_value < atr_low_threshold:
        return _decision("WAIT", 0, blocked="ATR_TOO_LOW", symbol=symbol, **base)
    if atr_value > float(row["atr_hi"]):
        return _decision("WAIT", 0, blocked="ATR_TOO_HIGH", symbol=symbol, **base)
    if trend_dir == 0:
        return _decision("WAIT", 0, reasons=[trend_reason], blocked="NO_M5_TREND", symbol=symbol, **base)

    rsi_value = float(row["rsi"])
    if trend_dir == 1 and rsi_value > RSI_BUY_REJECT:
        return _decision("WAIT", 0, blocked="RSI_OVERBOUGHT", symbol=symbol, **base)
    if trend_dir == -1 and rsi_value < RSI_SELL_REJECT:
        return _decision("WAIT", 0, blocked="RSI_OVERSOLD", symbol=symbol, **base)

    setup_ok = pullback_ok
    comps = _entry_confirmation(m1, trend_dir, i)
    components = {"trend": W_TREND, "ema_cross": 0, "rsi_zone": 0, "atr_band": W_ATR, "structure_break": 0, "liquidity_sweep": 0, "spread": 0, "session": 0}
    score = W_TREND + W_ATR
    reasons = [f"{trend_reason} (+{W_TREND})", f"ATR in band (+{W_ATR})"]
    if comps["cross"]:
        components["ema_cross"] = W_CROSS
        score += W_CROSS
        reasons.append(f"EMA9/21 cross (+{W_CROSS})")
    rsi_zone = RSI_BUY[0] <= rsi_value <= RSI_BUY[1] if trend_dir == 1 else RSI_SELL[0] <= rsi_value <= RSI_SELL[1]
    if rsi_zone:
        components["rsi_zone"] = W_RSI
        score += W_RSI
        reasons.append(f"RSI zone (+{W_RSI})")
    if comps["struct"]:
        components["structure_break"] = W_STRUCT
        score += W_STRUCT
        reasons.append(f"structure break (+{W_STRUCT})")
    if comps["sweep"]:
        components["liquidity_sweep"] = W_SWEEP
        score += W_SWEEP
        reasons.append(f"liquidity sweep (+{W_SWEEP})")
    if spread_points <= max_spread:
        components["spread"] = W_SPREAD
        score += W_SPREAD
    if LONDON[0] <= hour < NEWYORK[1]:
        components["session"] = W_SESSION
        score += W_SESSION
        reasons.append(f"session ok (+{W_SESSION})")
    base.update(
        {
            "pullback_ok": setup_ok,
            "ema_cross": "BUY" if comps["cross"] and trend_dir == 1 else "SELL" if comps["cross"] and trend_dir == -1 else None,
            "close_beyond_ema9": comps["beyond"],
            "structure_break": comps["struct"],
            "liquidity_sweep": comps["sweep"],
            "score_components": components,
        }
    )

    entry = float(row["close"])
    if trend_dir == 1:
        sl = float(row["swing_low"]) - SL_ATR_MULT * atr_value
        risk = entry - sl
        tp1 = entry + TP1_R * risk
        tp2 = entry + TP2_R * risk
    else:
        sl = float(row["swing_high"]) + SL_ATR_MULT * atr_value
        risk = sl - entry
        tp1 = entry - TP1_R * risk
        tp2 = entry - TP2_R * risk
    if not setup_ok:
        return _decision("WAIT", score, entry, sl, tp1, tp2, reasons, blocked="NO_PULLBACK", symbol=symbol, **base)
    if not comps["cross"]:
        return _decision("WAIT", score, entry, sl, tp1, tp2, reasons, blocked="NO_EMA_CROSS", symbol=symbol, **base)
    if not comps["beyond"] or not (comps["struct"] or comps["sweep"]):
        return _decision("WAIT", score, entry, sl, tp1, tp2, reasons, blocked="NO_ENTRY_CONFIRMATION", symbol=symbol, **base)
    if setup_ok and comps["confirmed"] and score >= min_score_trade and risk > 0:
        return _decision("BUY" if trend_dir == 1 else "SELL", score, entry, sl, tp1, tp2, reasons, symbol=symbol, **base)
    if score >= MIN_SCORE_WAIT:
        return _decision("WAIT", score, entry, sl, tp1, tp2, reasons + ["awaiting full confirmation"], blocked="PARTIAL_SETUP", symbol=symbol, **base)
    return _decision("WAIT", score, entry, sl, tp1, tp2, reasons, blocked="LOW_SCORE", symbol=symbol, **base)


def _m5_snapshot(m5: pd.DataFrame, trend_dir: int) -> dict:
    if m5 is None or m5.empty or "close" not in m5.columns:
        return {"trend_m5": "UNKNOWN", "ema20_m5": None, "ema50_m5": None, "ema200_m5": None}
    ema20 = _to_float(ema(m5["close"], 20).iloc[-1])
    ema50 = _to_float(ema(m5["close"], 50).iloc[-1])
    ema200 = _to_float(ema(m5["close"], 200).iloc[-1])
    trend = "BULLISH" if trend_dir == 1 else "BEARISH" if trend_dir == -1 else "MIXED"
    return {"trend_m5": trend, "ema20_m5": ema20, "ema50_m5": ema50, "ema200_m5": ema200}


def _atr_band_position(atr: float | None, atr_lo: float | None, atr_hi: float | None) -> float | None:
    if atr is None or atr_lo is None or atr_hi is None:
        return None
    width = atr_hi - atr_lo
    if width <= 0:
        return None
    return max(0.0, min(1.0, (atr - atr_lo) / width))


def _decision(
    decision: str,
    confidence: int,
    entry: float | None = None,
    sl: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    reasons: list[str] | None = None,
    blocked: str | None = None,
    symbol: str = "GOLD",
    **diagnostics: object,
) -> dict:
    rr_to_tp1 = None
    rr_to_tp2 = None
    if entry is not None and sl is not None and tp1 is not None:
        risk = abs(entry - sl)
        rr_to_tp1 = abs(tp1 - entry) / risk if risk > 0 else None
        rr_to_tp2 = abs(tp2 - entry) / risk if risk > 0 and tp2 is not None else None
    raw = {
        "enabled": True,
        "mode": "ENTRY_STRATEGY",
        "strategy": STRATEGY,
        "source": SOURCE,
        "decision": decision,
        "trend_m5": diagnostics.get("trend_m5"),
        "ema20_m5": _round(diagnostics.get("ema20_m5")),
        "ema50_m5": _round(diagnostics.get("ema50_m5")),
        "ema200_m5": _round(diagnostics.get("ema200_m5")),
        "ema9_m1": _round(diagnostics.get("ema9_m1")),
        "ema21_m1": _round(diagnostics.get("ema21_m1")),
        "rsi": _round(diagnostics.get("rsi")),
        "atr": _round(diagnostics.get("atr")),
        "atr_lo": _round(diagnostics.get("atr_lo")),
        "atr_hi": _round(diagnostics.get("atr_hi")),
        "atr_percentile": _round(diagnostics.get("atr_percentile")),
        "spread": _round(diagnostics.get("spread")),
        "spread_points": _round(diagnostics.get("spread_points")),
        "pullback_ok": bool(diagnostics.get("pullback_ok", False)),
        "ema_cross": diagnostics.get("ema_cross"),
        "close_beyond_ema9": bool(diagnostics.get("close_beyond_ema9", False)),
        "structure_break": bool(diagnostics.get("structure_break", False)),
        "liquidity_sweep": bool(diagnostics.get("liquidity_sweep", False)),
        "swing_high": _round(diagnostics.get("swing_high")),
        "swing_low": _round(diagnostics.get("swing_low")),
        "score": int(confidence),
        "score_components": dict(diagnostics.get("score_components") or {}),
        "min_score_trade": diagnostics.get("min_score_trade") or MIN_SCORE_TRADE,
        "relaxed_mode_active": bool(diagnostics.get("relaxed_mode_active", False)),
        "relaxed_reason": diagnostics.get("relaxed_reason"),
        "hours_without_setup": diagnostics.get("hours_without_setup"),
        "strict_threshold": diagnostics.get("strict_threshold"),
        "relaxed_threshold": diagnostics.get("relaxed_threshold"),
        "atr_relax_factor": diagnostics.get("atr_relax_factor"),
        "relaxed_trade_count_today": diagnostics.get("relaxed_trade_count_today"),
        "last_relaxed_trade_result": diagnostics.get("last_relaxed_trade_result"),
        "entry": _round(entry),
        "sl": _round(sl),
        "tp1": _round(tp1),
        "tp2": _round(tp2),
        "rr_to_tp1": _round(rr_to_tp1),
        "rr": _round(rr_to_tp2),
        "final_lot": None,
        "block_reason": blocked,
        "warnings": [],
    }
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "source": SOURCE,
        "decision": decision,
        "confidence": int(confidence),
        "entry": _round(entry),
        "sl": _round(sl),
        "tp": _round(tp2),
        "tp1": _round(tp1),
        "tp2": _round(tp2),
        "rr": _round(rr_to_tp2),
        "rr_to_tp1": _round(rr_to_tp1),
        "score": int(confidence),
        "score_components": raw["score_components"],
        "reasons": reasons or [],
        "block_reason": blocked,
        "gold_m1_m5_ema_sweep_scalper": raw,
    }


def _signal_from_result(symbol: str, result: dict) -> dict:
    decision = str(result.get("decision") or "WAIT").upper()
    signal = decision if decision in {"BUY", "SELL"} else "WAIT"
    confidence = int(result.get("confidence") or 0)
    rr = result.get("rr")
    raw = result.get("gold_m1_m5_ema_sweep_scalper") if isinstance(result.get("gold_m1_m5_ema_sweep_scalper"), dict) else result
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": signal,
        "direction": signal,
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "risk_reward": rr if signal in {"BUY", "SELL"} else None,
        "reward_risk": rr if signal in {"BUY", "SELL"} else None,
        "confidence": round(confidence / 100.0, 4),
        "m15_confirmation": signal in {"BUY", "SELL"},
        "m1_entry_confirmation": signal in {"BUY", "SELL"},
        "m15_confirmation_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m1_trigger_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": STRATEGY,
        "big_setup_grade": _grade(confidence),
        "setup_hunter_grade": _grade(confidence),
        "grade": _grade(confidence),
        "gold_m1m5_scalper": result,
        "gold_m1_m5_ema_sweep_scalper": raw,
        "gold_m1m5_scalper_decision": decision,
        "gold_m1m5_scalper_score": confidence,
        "gold_m1m5_scalper_reason": result.get("block_reason") or f"GOLD_M1_M5_{signal}",
        "relaxed_mode_active": bool(raw.get("relaxed_mode_active")),
        "relaxed_reason": raw.get("relaxed_reason"),
        "hours_without_setup": raw.get("hours_without_setup"),
        "strict_threshold": raw.get("strict_threshold"),
        "relaxed_threshold": raw.get("relaxed_threshold"),
        "reason": result.get("block_reason") or f"GOLD_M1_M5_{signal}",
    }


def _normalize_candles(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        df = value.copy()
    elif isinstance(value, dict):
        df = pd.DataFrame(value.get("candles") or value.get("data") or value.get("rows") or value)
    elif isinstance(value, (list, tuple)):
        df = pd.DataFrame(value)
    else:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    aliases = {
        "open": "open",
        "o": "open",
        "high": "high",
        "h": "high",
        "low": "low",
        "l": "low",
        "close": "close",
        "c": "close",
        "volume": "volume",
        "tick_volume": "volume",
        "real_volume": "volume",
        "v": "volume",
        "spread": "spread",
    }
    df = df.rename(columns={col: aliases.get(str(col).strip().lower(), col) for col in df.columns})
    df = df.loc[:, ~df.columns.duplicated()].copy()
    required = ["open", "high", "low", "close"]
    if any(col not in df.columns for col in required):
        return pd.DataFrame()
    if "volume" not in df.columns:
        df["volume"] = 0.0
    out = df[required + ["volume"]].copy()
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna().reset_index(drop=True)


def _closed_frame(value: Any) -> pd.DataFrame:
    df = _normalize_candles(value)
    if len(df) > 1:
        return df.iloc[:-1].copy().reset_index(drop=True)
    return df


def _spread_points(context: dict, settings: Settings) -> float:
    spread = _to_float(context.get("spread"))
    if spread is not None:
        return spread
    tick = context.get("tick") if isinstance(context.get("tick"), dict) else {}
    bid = _to_float(tick.get("bid") or context.get("bid"))
    ask = _to_float(tick.get("ask") or context.get("ask"))
    point = _to_float(context.get("point")) or 0.01
    if bid is not None and ask is not None and point > 0:
        return abs(ask - bid) / point
    return _to_float(getattr(settings, "max_spread_gold", None)) or 0.0


def _hour(context: dict) -> int:
    for key in ("utc_hour", "hour"):
        value = context.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 12


def _relaxed_state(settings: Settings, context: dict) -> dict:
    threshold = int(getattr(settings, "gold_m1m5_relaxed_after_hours_no_setup", 24))
    hours = _to_float(context.get("gold_m1m5_hours_without_setup") or context.get("hours_without_setup"))
    ignore_setup_wait = bool(getattr(settings, "demo_ignore_all_time_blocks", False) or getattr(settings, "demo_ignore_setup_wait_hours", False))
    active = bool(getattr(settings, "gold_m1m5_relaxed_demo_mode", True)) and (ignore_setup_wait or (hours is not None and hours >= threshold))
    if active:
        reason = "DURATION_BLOCKS_DISABLED_BY_USER_ORDER" if ignore_setup_wait else "NO_SETUP_24H"
        if ignore_setup_wait:
            log.info("[DURATION_GATE] decision=PASS reason=DURATION_BLOCKS_DISABLED_BY_USER_ORDER")
        log.info(
            "[GOLD_M1M5_RELAXED] active=true reason=%s min_score=%s atr_relax=%.2f",
            reason,
            int(getattr(settings, "gold_m1m5_min_score_relaxed", 65)),
            float(getattr(settings, "gold_m1m5_atr_low_relax_factor", 0.80)),
        )
    return {"active": active, "reason": reason if active else None, "hours_without_setup": hours}


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def _is_gold_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().replace("#", "")
    return normalized.startswith("GOLD") or normalized.startswith("XAUUSD")


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(float(value), 8) if value is not None and math.isfinite(float(value)) else None


def _log_result(result: dict) -> None:
    decision = result.get("decision")
    if decision in {"BUY", "SELL"}:
        log.info("[GOLD_M1_M5] mode=ENTRY_STRATEGY decision=%s score=%s rr=%s", decision, result.get("confidence"), result.get("rr"))
    else:
        log.info("[GOLD_M1_M5] mode=ENTRY_STRATEGY decision=%s reason=%s", decision, result.get("block_reason") or "NONE")
