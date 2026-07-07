from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from app.agents.confirmation_matrix import smc_calibrated_status as _calibrate_smc
from app.config import Settings
from app.logger import log


SMC_FIELDS = [
    "smc_h4_direction",
    "smc_h4_key_level_nearby",
    "smc_h4_supply_demand_zone",
    "smc_h1_trend",
    "smc_h1_break_structure",
    "smc_h1_order_block",
    "smc_h1_fvg",
    "smc_h1_liquidity",
    "smc_m15_confirmation",
    "smc_m5_confirmation",
    "smc_m1_entry_confirmation",
    "ifvg_ote_sniper",
    "turtle_soup_ote",
    "amd_bpr_ote",
    "breaker_fvg_ote",
    "ema50_200_stoch_confirmation",
    "smc_confluence_score",
    "smc_confluence_status",
    "smc_confluence_reason",
]


class SMCConfluenceTagger:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, symbol: str, frames: dict, entry_direction: str | None = None) -> dict:
        direction = str(entry_direction or "").upper()
        try:
            h4 = _frame(frames, "H4")
            h1 = _frame(frames, "H1")
            m15 = _frame(frames, "M15")
            m5 = _frame(frames, "M5")
            m1 = _frame(frames, "M1")
            missing = [name for name, frame in [("H4", h4), ("H1", h1), ("M15", m15), ("M5", m5), ("M1", m1)] if _missing(frame)]

            h4_direction = _trend(h4) if not _missing(h4) else "UNKNOWN"
            h4_key_level = _key_level_nearby(h4) if not _missing(h4) else False
            h4_zone = _supply_demand_zone(h4, h4_direction) if not _missing(h4) else "NONE"
            h1_trend = _trend(h1) if not _missing(h1) else "UNKNOWN"
            h1_break = _break_structure(h1) if not _missing(h1) else "NONE"
            h1_order_block = _order_block(h1) if not _missing(h1) else "NONE"
            h1_fvg = _fair_value_gap(h1) if not _missing(h1) else "NONE"
            h1_liquidity = _liquidity(h1) if not _missing(h1) else "NONE"
            m15_confirmation = _structure_confirmation(m15, direction) if not _missing(m15) else False
            m5_confirmation = _structure_confirmation(m5, direction) if not _missing(m5) else False
            m1_confirmation = _structure_confirmation(m1, direction) if not _missing(m1) else False

            setup_tags = {
                "ifvg_ote_sniper": _ifvg_ote(direction, h1_fvg, h4_zone, m15_confirmation, m1_confirmation),
                "turtle_soup_ote": _turtle_soup(direction, h1_liquidity, h4_key_level, m15_confirmation),
                "amd_bpr_ote": _amd_bpr(h4_direction, h1_break, h1_order_block, m5_confirmation),
                "breaker_fvg_ote": _breaker_fvg(direction, h1_break, h1_fvg, m5_confirmation),
                "ema50_200_stoch_confirmation": _ema_confirmation(m5, direction) if not _missing(m5) else False,
            }
            score = _score(
                direction,
                h4_direction,
                h4_key_level,
                h4_zone,
                h1_trend,
                h1_break,
                h1_order_block,
                h1_fvg,
                h1_liquidity,
                m15_confirmation,
                m5_confirmation,
                m1_confirmation,
                setup_tags,
            )
            weekend_fallback = (
                _is_btc_symbol(symbol)
                and _is_weekend_utc()
                and any(name in missing for name in ("M15", "M5", "M1"))
                and "H4" not in missing
                and "H1" not in missing
            )
            if weekend_fallback:
                score = 50
                status, reason = "NEUTRAL", "SMC_WEEKEND_FALLBACK_H1"
                log.info(
                    "[SMC_WEEKEND_FALLBACK_H1] symbol=%s direction=%s h4=%s h1=%s missing=%s",
                    symbol, direction or "WAIT", h4_direction, h1_trend, ",".join(missing),
                )
            else:
                status, reason = _status_reason(direction, score, missing, m15_confirmation, m5_confirmation, m1_confirmation)
            out = {
                "smc_h4_direction": h4_direction,
                "smc_h4_key_level_nearby": bool(h4_key_level),
                "smc_h4_supply_demand_zone": h4_zone,
                "smc_h1_trend": h1_trend,
                "smc_h1_break_structure": h1_break,
                "smc_h1_order_block": h1_order_block,
                "smc_h1_fvg": h1_fvg,
                "smc_h1_liquidity": h1_liquidity,
                "smc_m15_confirmation": bool(m15_confirmation),
                "smc_m5_confirmation": bool(m5_confirmation),
                "smc_m1_entry_confirmation": bool(m1_confirmation),
                **setup_tags,
                "smc_confluence_score": score,
                "smc_confluence_status": status,
                "smc_confluence_reason": reason,
                "smc_calibrated_status": _calibrate_smc(score),
            }
            self._log(symbol, direction or "WAIT", out)
            return out
        except Exception as exc:
            log.error("[SMC_TAGGER_ERROR] symbol=%s error=%s", symbol, exc)
            status = "NOT_APPLICABLE" if direction in {"", "WAIT", "NONE", "NULL"} else "FAIL"
            out = _default(f"SMC_TAGGER_ERROR:{exc}", status)
            self._log(symbol, direction or "WAIT", out)
            return out

    def _log(self, symbol: str, direction: str, result: dict) -> None:
        log.info(
            "[SMC_TAGGER] symbol=%s direction=%s h4=%s h1=%s m15=%s score=%s status=%s reason=%s",
            symbol,
            direction,
            result.get("smc_h4_direction"),
            result.get("smc_h1_trend"),
            result.get("smc_m15_confirmation"),
            result.get("smc_confluence_score"),
            result.get("smc_confluence_status"),
            result.get("smc_confluence_reason"),
        )


def _default(reason: str, status: str = "NOT_APPLICABLE") -> dict:
    return {
        "smc_h4_direction": "UNKNOWN",
        "smc_h4_key_level_nearby": False,
        "smc_h4_supply_demand_zone": "NONE",
        "smc_h1_trend": "UNKNOWN",
        "smc_h1_break_structure": "NONE",
        "smc_h1_order_block": "NONE",
        "smc_h1_fvg": "NONE",
        "smc_h1_liquidity": "NONE",
        "smc_m15_confirmation": False,
        "smc_m5_confirmation": False,
        "smc_m1_entry_confirmation": False,
        "ifvg_ote_sniper": False,
        "turtle_soup_ote": False,
        "amd_bpr_ote": False,
        "breaker_fvg_ote": False,
        "ema50_200_stoch_confirmation": False,
        "smc_confluence_score": 0,
        "smc_confluence_status": status,
        "smc_confluence_reason": reason,
        "smc_calibrated_status": _calibrate_smc(0),
    }


def _is_btc_symbol(symbol: object) -> bool:
    return str(symbol or "").upper().rstrip("#").startswith("BTCUSD")


def _is_weekend_utc() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def _frame(frames: dict, key: str) -> pd.DataFrame | None:
    value = frames.get(key) if isinstance(frames, dict) else None
    return value if isinstance(value, pd.DataFrame) else None


def _missing(df: pd.DataFrame | None) -> bool:
    return df is None or df.empty or len(df) < 3


def _trend(df: pd.DataFrame | None) -> str:
    if _missing(df):
        return "UNKNOWN"
    closed = df.iloc[:-1]  # exclude live candle
    tail = closed.tail(min(len(closed), 12))
    highs = [_float(v) for v in tail["high"].tail(5)] if "high" in tail else []
    lows = [_float(v) for v in tail["low"].tail(5)] if "low" in tail else []
    closes = [_float(v) for v in tail["close"].tail(5)] if "close" in tail else []
    if len(highs) < 3 or len(lows) < 3 or len(closes) < 3 or any(v is None for v in highs + lows + closes):
        return "UNKNOWN"
    if (highs[-1] > highs[-2] > highs[-3] and lows[-1] > lows[-2] > lows[-3]) or closes[-1] > max(highs[:-1]):
        return "BULLISH"
    if (highs[-1] < highs[-2] < highs[-3] and lows[-1] < lows[-2] < lows[-3]) or closes[-1] < min(lows[:-1]):
        return "BEARISH"
    return "RANGE"


def _zone_tolerance(df: pd.DataFrame | None, close: float) -> float:
    """ATR-relative zone tolerance (k x ATR_H4); pct-of-price fallback.

    The old fixed pct (~$8 on gold) missed zones in high-vol regimes and
    over-triggered in quiet ones.
    """
    atr_val = _atr_value(df)
    if atr_val is not None and atr_val > 0:
        return 0.5 * atr_val
    return max(abs(close) * 0.0025, 0.0001)


def _atr_value(df: pd.DataFrame | None, period: int = 14) -> float | None:
    if df is None or getattr(df, "empty", True) or len(df) < 4:
        return None
    try:
        effective = min(period, len(df) - 1)
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return _float(tr.rolling(effective).mean().iloc[-1])
    except (KeyError, TypeError, ValueError):
        return None


def _key_level_nearby(df: pd.DataFrame | None) -> bool:
    if _missing(df) or len(df) < 6:
        return False
    close = _float(df.iloc[-1].get("close"))
    support = _float(df["low"].iloc[:-1].tail(10).min())
    resistance = _float(df["high"].iloc[:-1].tail(10).max())
    if close is None or support is None or resistance is None:
        return False
    tolerance = _zone_tolerance(df, close)
    return abs(close - support) <= tolerance or abs(close - resistance) <= tolerance


def _supply_demand_zone(df: pd.DataFrame | None, trend: str) -> str:
    if _missing(df) or len(df) < 6:
        return "NONE"
    close = _float(df.iloc[-1].get("close"))
    support = _float(df["low"].iloc[:-1].tail(10).min())
    resistance = _float(df["high"].iloc[:-1].tail(10).max())
    if close is None or support is None or resistance is None:
        return "NONE"
    tolerance = _zone_tolerance(df, close)
    if abs(close - support) <= tolerance:
        return "DEMAND" if trend in {"BULLISH", "RANGE"} else "NONE"
    if abs(close - resistance) <= tolerance:
        return "SUPPLY" if trend in {"BEARISH", "RANGE"} else "NONE"
    return "NONE"


def _break_structure(df: pd.DataFrame | None) -> str:
    if _missing(df) or len(df) < 9:  # +1 for excluded live candle
        return "NONE"
    last = df.iloc[-2]           # last confirmed closed (was live)
    prev = df.iloc[:-2].tail(6)  # 6 candles before that
    close = _float(last.get("close"))
    prior_high = _float(prev["high"].max())
    prior_low = _float(prev["low"].min())
    trend_before = _trend(df)    # _trend() excludes live internally
    if close is None or prior_high is None or prior_low is None:
        return "NONE"
    if close > prior_high:
        return "CHOCH_UP" if trend_before == "BEARISH" else "BOS_UP"
    if close < prior_low:
        return "CHOCH_DOWN" if trend_before == "BULLISH" else "BOS_DOWN"
    return "NONE"


def _order_block(df: pd.DataFrame | None) -> str:
    if _missing(df) or len(df) < 5:
        return "NONE"
    prev = df.iloc[-3]  # n-2 confirmed closed
    last = df.iloc[-2]  # n-1 confirmed closed (was live)
    prev_open = _float(prev.get("open"))
    prev_close = _float(prev.get("close"))
    last_open = _float(last.get("open"))
    last_close = _float(last.get("close"))
    if None in {prev_open, prev_close, last_open, last_close}:
        return "NONE"
    if prev_close < prev_open and last_close > last_open and last_close > _float(prev.get("high")):
        return "BULLISH_OB"
    if prev_close > prev_open and last_close < last_open and last_close < _float(prev.get("low")):
        return "BEARISH_OB"
    return "NONE"


def _fair_value_gap(df: pd.DataFrame | None) -> str:
    if _missing(df) or len(df) < 5:
        return "NONE"
    a = df.iloc[-4]  # flanking candle A (confirmed closed)
    c = df.iloc[-2]  # flanking candle C (confirmed closed, was live)
    a_high = _float(a.get("high"))
    a_low = _float(a.get("low"))
    c_high = _float(c.get("high"))
    c_low = _float(c.get("low"))
    if None in {a_high, a_low, c_high, c_low}:
        return "NONE"
    if c_low > a_high:
        return "BULLISH_FVG"
    if c_high < a_low:
        return "BEARISH_FVG"
    return "NONE"


def _liquidity(df: pd.DataFrame | None) -> str:
    if _missing(df) or len(df) < 9:  # +1 for excluded live candle
        return "NONE"
    closed = df.iloc[:-1]  # exclude live candle
    tail = closed.tail(8)
    highs = [_float(v) for v in tail["high"]]
    lows = [_float(v) for v in tail["low"]]
    close = _float(tail.iloc[-1].get("close"))  # last confirmed close
    if close is None or any(v is None for v in highs + lows):
        return "NONE"
    tolerance = max(abs(close) * 0.001, 0.0001)
    if abs(highs[-2] - highs[-3]) <= tolerance:
        return "EQUAL_HIGHS"
    if abs(lows[-2] - lows[-3]) <= tolerance:
        return "EQUAL_LOWS"
    if highs[-1] > max(highs[:-1]) and close < highs[-1]:
        return "BUY_SIDE"
    if lows[-1] < min(lows[:-1]) and close > lows[-1]:
        return "SELL_SIDE"
    return "NONE"


def _structure_confirmation(df: pd.DataFrame | None, direction: str) -> bool:
    """Breakout of the prior extreme on the last CLOSED candle.

    Accepts either a body close beyond the level OR a wick beyond it —
    requiring a simultaneous body-close breakout on three timeframes at once
    proved unreachable (0/5943 measured).
    """
    if direction not in {"BUY", "SELL"} or _missing(df) or len(df) < 5:
        return False
    closed = df.iloc[:-1]  # exclude live candle
    tail = closed.tail(5)
    last = tail.iloc[-1]   # last confirmed closed candle
    prev = tail.iloc[:-1]
    close = _float(last.get("close"))
    open_ = _float(last.get("open"))
    if close is None or open_ is None:
        return False
    if direction == "BUY":
        prior_high = _float(prev["high"].max())
        if prior_high is None:
            return False
        body = close > prior_high and close > open_
        high = _float(last.get("high"))
        wick = high is not None and high > prior_high
        return bool(body or wick)
    prior_low = _float(prev["low"].min())
    if prior_low is None:
        return False
    body = close < prior_low and close < open_
    low = _float(last.get("low"))
    wick = low is not None and low < prior_low
    return bool(body or wick)


def _ifvg_ote(direction: str, fvg: str, zone: str, m15: bool, m1: bool) -> bool:
    return bool(direction == "BUY" and fvg == "BULLISH_FVG" and zone == "DEMAND" and m15 and m1) or bool(
        direction == "SELL" and fvg == "BEARISH_FVG" and zone == "SUPPLY" and m15 and m1
    )


def _turtle_soup(direction: str, liquidity: str, key_level: bool, m15: bool) -> bool:
    return bool(direction == "BUY" and liquidity in {"SELL_SIDE", "EQUAL_LOWS"} and key_level and m15) or bool(
        direction == "SELL" and liquidity in {"BUY_SIDE", "EQUAL_HIGHS"} and key_level and m15
    )


def _amd_bpr(h4_direction: str, h1_break: str, order_block: str, m5: bool) -> bool:
    return bool(h4_direction == "BULLISH" and h1_break in {"BOS_UP", "CHOCH_UP"} and order_block == "BULLISH_OB" and m5) or bool(
        h4_direction == "BEARISH" and h1_break in {"BOS_DOWN", "CHOCH_DOWN"} and order_block == "BEARISH_OB" and m5
    )


def _breaker_fvg(direction: str, h1_break: str, fvg: str, m5: bool) -> bool:
    return bool(direction == "BUY" and h1_break in {"BOS_UP", "CHOCH_UP"} and fvg == "BULLISH_FVG" and m5) or bool(
        direction == "SELL" and h1_break in {"BOS_DOWN", "CHOCH_DOWN"} and fvg == "BEARISH_FVG" and m5
    )


def _ema_confirmation(df: pd.DataFrame | None, direction: str) -> bool:
    if direction not in {"BUY", "SELL"} or _missing(df) or "close" not in df or len(df) < 20:
        return False
    close = df["close"].astype(float)
    ema_fast = close.ewm(span=min(50, len(close)), adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=min(200, len(close)), adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if direction == "BUY":
        return bool(last >= ema_fast >= ema_slow)
    return bool(last <= ema_fast <= ema_slow)


def _score(
    direction: str,
    h4_direction: str,
    h4_key_level: bool,
    h4_zone: str,
    h1_trend: str,
    h1_break: str,
    h1_order_block: str,
    h1_fvg: str,
    h1_liquidity: str,
    m15_confirmation: bool,
    m5_confirmation: bool,
    m1_confirmation: bool,
    setup_tags: dict,
) -> int:
    score = 0
    if h4_direction in {"BULLISH", "BEARISH"}:
        score += 15
    if h4_key_level:
        score += 10
    if h4_zone in {"SUPPLY", "DEMAND"}:
        score += 10
    if direction == "BUY" and h1_trend == "BULLISH" or direction == "SELL" and h1_trend == "BEARISH":
        score += 10
    if h1_break != "NONE":
        score += 10
    if h1_order_block != "NONE":
        score += 10
    if h1_fvg != "NONE":
        score += 10
    if h1_liquidity != "NONE":
        score += 5
    if m15_confirmation:
        score += 10
    if m5_confirmation:
        score += 5
    if m1_confirmation:
        score += 5
    if any(setup_tags.values()):
        score += 10
    return max(0, min(100, score))


def _status_reason(direction: str, score: int, missing: list[str], m15: bool, m5: bool, m1: bool) -> tuple[str, str]:
    if missing:
        reason = "MISSING_DATA:" + ",".join(missing)
        if direction in {"BUY", "SELL"}:
            return "FAIL", reason
        return "NOT_APPLICABLE", reason
    if direction not in {"BUY", "SELL"}:
        return "NOT_APPLICABLE", "NO_TRADE_DIRECTION"
    confirmed = sum(1 for flag in (m15, m5, m1) if flag)
    # 2-of-3 timeframe confirmations; PASS threshold 60 unchanged.
    if score >= 60 and confirmed >= 2:
        return "PASS", "SMC_CONFLUENCE_ALIGNED"
    missing_factors = []
    if confirmed < 2:
        if not m15:
            missing_factors.append("NO_M15_CONFIRMATION")
        if not m5:
            missing_factors.append("NO_M5_CONFIRMATION")
        if not m1:
            missing_factors.append("NO_M1_ENTRY_CONFIRMATION")
    if score < 60:
        missing_factors.append("LOW_SMC_CONFLUENCE_SCORE")
    return "FAIL", ",".join(missing_factors)


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
