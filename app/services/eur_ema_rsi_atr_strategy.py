from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.config import Settings
from app.logger import log


STRATEGY = "EUR_EMA_RSI_ATR_CROSSOVER"
SOURCE = "EurRobot_EURUSD(1).mq5"


def evaluate(symbol: str, frames: dict[str, Any], context: dict | None, settings: Settings) -> dict:
    if not bool(getattr(settings, "eur_ema_rsi_atr_enabled", True)):
        return _payload(symbol, "WAIT", "EUR_EMA_RSI_ATR_DISABLED")
    if _canonical_symbol(symbol) != "EURUSD":
        return _payload(symbol, "WAIT", "EUR_EMA_RSI_ATR_SYMBOL_NOT_EURUSD")
    try:
        df = _closed_frame((frames or {}).get("M5"))
        result = evaluate_eur_ema_rsi_atr(symbol, df, context or {}, settings)
    except Exception as exc:
        result = _payload(symbol, "WAIT", "EUR_EMA_RSI_ATR_INTERNAL_ERROR")
        result["eur_ema_rsi_atr"]["error"] = str(exc)
    _log_result(result)
    return result


def evaluate_eur_ema_rsi_atr(symbol: str, candles: pd.DataFrame, context: dict | None, settings: Settings) -> dict:
    df = _normalize_candles(candles)
    fast = int(getattr(settings, "eur_fast_ema", 20))
    slow = int(getattr(settings, "eur_slow_ema", 50))
    rsi_period = int(getattr(settings, "eur_rsi_period", 14))
    atr_period = int(getattr(settings, "eur_atr_period", 14))
    required = max(slow + 2, rsi_period + 2, atr_period + 2)
    if len(df) < required:
        return _payload(symbol, "WAIT", "EUR_EMA_RSI_ATR_NOT_ENOUGH_CLOSED_CANDLES")

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    rsi_values = rsi(close, rsi_period)
    atr_values = atr(df, atr_period)
    ema_fast_1 = _finite(ema_fast.iloc[-1])
    ema_slow_1 = _finite(ema_slow.iloc[-1])
    ema_fast_2 = _finite(ema_fast.iloc[-2])
    ema_slow_2 = _finite(ema_slow.iloc[-2])
    rsi_1 = _finite(rsi_values.iloc[-1])
    atr_1 = _finite(atr_values.iloc[-1])
    if None in {ema_fast_1, ema_slow_1, ema_fast_2, ema_slow_2, rsi_1, atr_1} or atr_1 <= 0:
        return _payload(
            symbol,
            "WAIT",
            "EUR_EMA_RSI_ATR_INDICATOR_INVALID",
            ema_fast_1,
            ema_slow_1,
            ema_fast_2,
            ema_slow_2,
            rsi_1,
            atr_1,
        )

    cross_up = ema_fast_2 <= ema_slow_2 and ema_fast_1 > ema_slow_1
    cross_down = ema_fast_2 >= ema_slow_2 and ema_fast_1 < ema_slow_1
    buy = cross_up and rsi_1 < float(getattr(settings, "eur_rsi_buy_max", 70.0))
    sell = cross_down and rsi_1 > float(getattr(settings, "eur_rsi_sell_min", 30.0))
    context = context or {}
    relaxed = _relaxed_state(settings, context)
    near_cross = False
    near_cross_reason = None
    if not buy and not sell:
        if relaxed["active"] and bool(getattr(settings, "eur_allow_near_cross", True)):
            near_cross = abs(ema_fast_1 - ema_slow_1) <= atr_1 * float(getattr(settings, "eur_near_cross_max_distance_atr", 0.15))
            slope = ema_fast_1 - ema_fast_2
            last_close = float(close.iloc[-1])
            buy = near_cross and slope > 0 and 50 <= rsi_1 <= 68 and last_close > ema_fast_1
            sell = near_cross and slope < 0 and 32 <= rsi_1 <= 50 and last_close < ema_fast_1
            near_cross_reason = "EUR_NEAR_CROSS_RELAXED_DEMO" if (buy or sell) else None
        if not buy and not sell:
            return _payload(
                symbol,
                "WAIT",
                "NO_EMA_CROSS",
                ema_fast_1,
                ema_slow_1,
                ema_fast_2,
                ema_slow_2,
                rsi_1,
                atr_1,
                cross_up,
                cross_down,
                relaxed=relaxed,
                near_cross=near_cross,
            )

    direction = "BUY" if buy else "SELL"
    last_close = float(close.iloc[-1])
    entry = _entry_price(direction, context, last_close)
    sl_distance = atr_1 * float(getattr(settings, "eur_atr_mult", 1.5))
    rr_target = float(getattr(settings, "eur_rr", 2.0))
    if direction == "BUY":
        sl = entry - sl_distance
        tp = entry + sl_distance * rr_target
    else:
        sl = entry + sl_distance
        tp = entry - sl_distance * rr_target
    rr_value = reward_risk(direction, entry, sl, tp)
    if rr_value is None or rr_value < rr_target:
        return _payload(
            symbol,
            "BLOCK",
            "EUR_EMA_RSI_ATR_INVALID_RR",
            ema_fast_1,
            ema_slow_1,
            ema_fast_2,
            ema_slow_2,
            rsi_1,
            atr_1,
            cross_up,
            cross_down,
            entry,
            sl,
            tp,
            relaxed=relaxed,
            near_cross=near_cross,
            near_cross_reason=near_cross_reason,
        )
    return _payload(
        symbol,
        direction,
        near_cross_reason,
        ema_fast_1,
        ema_slow_1,
        ema_fast_2,
        ema_slow_2,
        rsi_1,
        atr_1,
        cross_up,
        cross_down,
        entry,
        sl,
        tp,
        rr_value,
        relaxed=relaxed,
        near_cross=near_cross,
        near_cross_reason=near_cross_reason,
    )


def ema(values: pd.Series, period: int) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").ewm(span=period, adjust=False).mean()


def rsi(values: pd.Series, period: int) -> pd.Series:
    close = pd.to_numeric(values, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100)


def atr(candles: pd.DataFrame, period: int) -> pd.Series:
    high = pd.to_numeric(candles["high"], errors="coerce")
    low = pd.to_numeric(candles["low"], errors="coerce")
    close = pd.to_numeric(candles["close"], errors="coerce")
    previous_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def reward_risk(direction: str, entry: float, sl: float, tp: float) -> float | None:
    if direction == "BUY" and not (sl < entry < tp):
        return None
    if direction == "SELL" and not (tp < entry < sl):
        return None
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    return rr if math.isfinite(rr) else None


def _closed_frame(value: Any) -> pd.DataFrame:
    df = _normalize_candles(value)
    if len(df) > 1:
        return df.iloc[:-1].copy()
    return df


def _normalize_candles(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        out = value.copy()
    elif isinstance(value, dict):
        rows = value.get("candles") or value.get("data") or value.get("rows") or value
        out = pd.DataFrame(rows)
    else:
        out = pd.DataFrame(value)
    if out.empty:
        return pd.DataFrame()
    aliases = {
        "open": ("open", "o"),
        "high": ("high", "h"),
        "low": ("low", "l"),
        "close": ("close", "c"),
        "volume": ("volume", "tick_volume", "real_volume", "v"),
    }
    out = out.loc[:, ~out.columns.duplicated()].copy()
    renamed: dict[str, str] = {}
    lower = {str(col).lower(): col for col in out.columns}
    for standard, names in aliases.items():
        for name in names:
            if name in lower:
                renamed[lower[name]] = standard
                break
    out = out.rename(columns=renamed)
    required = ["open", "high", "low", "close"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()
    for col in required + (["volume"] if "volume" in out.columns else []):
        series = out.loc[:, col]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        out[col] = pd.to_numeric(series, errors="coerce")
    return out.dropna(subset=required).reset_index(drop=True)


def _payload(
    symbol: str,
    decision: str,
    reason: str | None,
    ema_fast_1: float | None = None,
    ema_slow_1: float | None = None,
    ema_fast_2: float | None = None,
    ema_slow_2: float | None = None,
    rsi_1: float | None = None,
    atr_1: float | None = None,
    cross_up: bool = False,
    cross_down: bool = False,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    rr_value: float | None = None,
    relaxed: dict | None = None,
    near_cross: bool = False,
    near_cross_reason: str | None = None,
) -> dict:
    direction = decision if decision in {"BUY", "SELL"} else "WAIT"
    rr = rr_value if rr_value is not None else 2.0
    relaxed = relaxed or {"active": False, "reason": None, "hours_without_setup": None}
    payload = {
        "enabled": True,
        "mode": "ENTRY_STRATEGY",
        "strategy": STRATEGY,
        "source": SOURCE,
        "decision": decision,
        "fast_ema": 20,
        "slow_ema": 50,
        "ema_fast_1": _round(ema_fast_1),
        "ema_slow_1": _round(ema_slow_1),
        "ema_fast_2": _round(ema_fast_2),
        "ema_slow_2": _round(ema_slow_2),
        "rsi_1": _round(rsi_1),
        "atr_1": _round(atr_1),
        "cross_up": bool(cross_up),
        "cross_down": bool(cross_down),
        "near_cross": bool(near_cross),
        "near_cross_reason": near_cross_reason,
        "entry": _round(entry),
        "sl": _round(sl),
        "tp": _round(tp),
        "rr": _round(rr),
        "relaxed_mode_active": bool(relaxed.get("active")),
        "relaxed_reason": relaxed.get("reason"),
        "hours_without_setup": relaxed.get("hours_without_setup"),
        "strict_threshold": "EMA20_50_CROSS",
        "relaxed_threshold": f"NEAR_CROSS_ATR_{0.15}",
        "relaxed_trade_count_today": None,
        "last_relaxed_trade_result": None,
        "block_reason": reason,
        "warnings": [],
    }
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": direction,
        "direction": direction,
        "entry": _round(entry),
        "sl": _round(sl),
        "tp": _round(tp),
        "risk_reward": _round(rr_value) if direction in {"BUY", "SELL"} else None,
        "reward_risk": _round(rr_value) if direction in {"BUY", "SELL"} else None,
        "confidence": 1.0 if direction in {"BUY", "SELL"} else 0.0,
        "m15_confirmation": direction in {"BUY", "SELL"},
        "m1_entry_confirmation": direction in {"BUY", "SELL"},
        "m15_confirmation_status": "PASS" if direction in {"BUY", "SELL"} else "FAIL",
        "m1_trigger_status": "PASS" if direction in {"BUY", "SELL"} else "FAIL",
        "m15_confirmation_reason": "EUR_EMA_RSI_ATR_M5_SIGNAL",
        "m1_trigger_reason": "EUR_EMA_RSI_ATR_M5_SIGNAL",
        "big_setup_grade": "A" if direction in {"BUY", "SELL"} else "D",
        "setup_hunter_grade": "A" if direction in {"BUY", "SELL"} else "D",
        "grade": "A" if direction in {"BUY", "SELL"} else "D",
        "eur_ema_rsi_atr": payload,
        "eur_ema_rsi_atr_decision": decision,
        "eur_ema_rsi_atr_reason": reason,
        "relaxed_mode_active": bool(relaxed.get("active")),
        "relaxed_reason": relaxed.get("reason"),
        "hours_without_setup": relaxed.get("hours_without_setup"),
        "strict_threshold": "EMA20_50_CROSS",
        "relaxed_threshold": payload["relaxed_threshold"],
        "reason": reason or f"{STRATEGY}_{direction}",
    }


def _entry_price(direction: str, context: dict, fallback: float) -> float:
    key = "ask" if direction == "BUY" else "bid"
    tick = context.get("tick") if isinstance(context.get("tick"), dict) else {}
    return _finite(context.get(key)) or _finite(tick.get(key)) or fallback


def _canonical_symbol(symbol: str) -> str:
    text = str(symbol or "").upper().replace("#", "")
    return "GOLD" if text in {"GOLD", "XAUUSD"} else text


def _relaxed_state(settings: Settings, context: dict) -> dict:
    threshold = int(getattr(settings, "eur_relaxed_after_hours_no_setup", 24))
    hours = _finite(context.get("eur_hours_without_setup") or context.get("hours_without_setup"))
    ignore_setup_wait = bool(getattr(settings, "demo_ignore_all_time_blocks", False) or getattr(settings, "demo_ignore_setup_wait_hours", False))
    active = bool(getattr(settings, "eur_ema_rsi_atr_relaxed_demo_mode", True)) and (ignore_setup_wait or (hours is not None and hours >= threshold))
    reason = "DURATION_BLOCKS_DISABLED_BY_USER_ORDER" if active and ignore_setup_wait else "NO_SETUP_24H" if active else None
    if active and ignore_setup_wait:
        log.info("[DURATION_GATE] decision=PASS reason=DURATION_BLOCKS_DISABLED_BY_USER_ORDER")
    return {"active": active, "reason": reason, "hours_without_setup": hours}


def _finite(value: object) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(float(value), 8) if value is not None and math.isfinite(float(value)) else None


def _log_result(result: dict) -> None:
    payload = result.get("eur_ema_rsi_atr") if isinstance(result.get("eur_ema_rsi_atr"), dict) else {}
    decision = payload.get("decision")
    reason = payload.get("block_reason")
    if decision in {"BUY", "SELL"}:
        log.info("[EUR_EMA_RSI_ATR] decision=%s rsi=%s atr=%s rr=%s", decision, payload.get("rsi_1"), payload.get("atr_1"), payload.get("rr"))
    else:
        log.info("[EUR_EMA_RSI_ATR] decision=%s reason=%s", decision, reason)
