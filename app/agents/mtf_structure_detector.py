from __future__ import annotations

import math

import pandas as pd

from app.config import Settings
from app.logger import log


STRATEGY_NAME = "MTF_STRUCTURE_4H_15M_1M"


class MTFStructureDetector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, symbol: str, frames: dict, entry_direction: str | None = None) -> dict:
        mode = (self.settings.mtf_structure_mode or "TAG_ONLY").upper()
        if not self.settings.mtf_structure_enabled:
            out = _result("NOT_APPLICABLE", "WAIT", "RANGE", "NONE", False, "NONE", False, None, None, None, None, None, 0, "MTF_STRUCTURE_DISABLED", mode)
            self._log(symbol, out)
            return out
        try:
            if _no_trade_direction(entry_direction):
                out = _result("NOT_APPLICABLE", "WAIT", "RANGE", "NONE", False, "NONE", False, None, None, None, None, None, 0, "NO_TRADE_DIRECTION", mode)
                self._log(symbol, out)
                return out
            h4 = frames.get("H4")
            m15 = frames.get("M15")
            m1 = frames.get("M1")
            if _missing(h4) or _missing(m15) or _missing(m1):
                out = _result("NOT_APPLICABLE", "WAIT", "RANGE", "NONE", False, "NONE", False, None, None, None, None, None, 0, "MISSING_TIMEFRAME_DATA", mode)
                self._log(symbol, out)
                return out
            if len(h4) < 12 or len(m15) < 10 or len(m1) < 8:
                out = _result("NOT_APPLICABLE", "WAIT", "RANGE", "NONE", False, "NONE", False, None, None, None, None, None, 0, "INSUFFICIENT_TIMEFRAME_DATA", mode)
                self._log(symbol, out)
                return out

            h4_context = self._h4_context(h4)
            h4_zone = self._active_h4_zone(m15, h4_context)
            direction = self._direction(h4_context["h4_bias"], h4_zone)
            m15_confirmation, m15_shift = self._m15_confirmation(m15, direction)
            m1_confirmation = self._m1_entry_confirmation(m1, direction)
            levels = self._suggest_levels(m1, h4_context, direction)
            score = self._score(h4_context["h4_bias"], h4_zone, m15_confirmation, m1_confirmation, direction)
            status = "PASS" if direction in {"BUY", "SELL"} and m15_confirmation and m1_confirmation else "FAIL"
            reason = self._reason(direction, h4_zone, m15_confirmation, m1_confirmation)
            out = _result(
                status,
                direction,
                h4_context["h4_bias"],
                h4_zone,
                m15_confirmation,
                m15_shift,
                m1_confirmation,
                levels["entry_price_suggestion"],
                levels["sl_suggestion"],
                levels["tp1_suggestion"],
                levels["tp2_suggestion"],
                levels["risk_reward_suggestion"],
                score,
                reason,
                mode,
                h4_context,
            )
            self._log(symbol, out)
            return out
        except Exception as exc:
            out = _result("FAIL", "WAIT", "RANGE", "NONE", False, "NONE", False, None, None, None, None, None, 0, f"MTF_STRUCTURE_ERROR:{exc}", mode)
            self._log(symbol, out)
            return out

    def _h4_context(self, h4: pd.DataFrame) -> dict:
        tail = h4.tail(20)
        last = tail.iloc[-1]
        prev = tail.iloc[:-1]
        recent = prev.tail(8)
        prev_high = _float(recent["high"].max())
        prev_low = _float(recent["low"].min())
        last_close = _float(last["close"])
        highs = [_float(value) for value in tail["high"].tail(5)]
        lows = [_float(value) for value in tail["low"].tail(5)]

        higher_highs = highs[-1] > highs[-2] > highs[-3] if len(highs) >= 3 else False
        higher_lows = lows[-1] > lows[-2] > lows[-3] if len(lows) >= 3 else False
        lower_highs = highs[-1] < highs[-2] < highs[-3] if len(highs) >= 3 else False
        lower_lows = lows[-1] < lows[-2] < lows[-3] if len(lows) >= 3 else False

        if (higher_highs and higher_lows) or (prev_high is not None and last_close is not None and last_close > prev_high):
            bias = "BULLISH"
        elif (lower_highs and lower_lows) or (prev_low is not None and last_close is not None and last_close < prev_low):
            bias = "BEARISH"
        else:
            bias = "RANGE"

        support = _float(prev["low"].tail(10).min())
        resistance = _float(prev["high"].tail(10).max())
        last_swing_high = _last_swing(tail, "high")
        last_swing_low = _last_swing(tail, "low")
        return {
            "h4_bias": bias,
            "h4_recent_support": support,
            "h4_recent_resistance": resistance,
            "h4_supply_zone": resistance,
            "h4_demand_zone": support,
            "h4_last_swing_high": last_swing_high,
            "h4_last_swing_low": last_swing_low,
        }

    def _active_h4_zone(self, m15: pd.DataFrame, h4_context: dict) -> str:
        price = _float(m15.iloc[-1].get("close"))
        if price is None:
            return "NONE"
        support = h4_context.get("h4_recent_support")
        resistance = h4_context.get("h4_recent_resistance")
        tolerance = max(abs(price) * 0.002, 0.0001)
        if support is not None and abs(price - support) <= tolerance:
            return "DEMAND" if h4_context.get("h4_bias") == "BULLISH" else "SUPPORT"
        if resistance is not None and abs(price - resistance) <= tolerance:
            return "SUPPLY" if h4_context.get("h4_bias") == "BEARISH" else "RESISTANCE"
        return "NONE"

    def _direction(self, h4_bias: str, h4_zone: str) -> str:
        if h4_bias == "BULLISH" and h4_zone in {"SUPPORT", "DEMAND"}:
            return "BUY"
        if h4_bias == "BEARISH" and h4_zone in {"RESISTANCE", "SUPPLY"}:
            return "SELL"
        return "WAIT"

    def _m15_confirmation(self, m15: pd.DataFrame, direction: str) -> tuple[bool, str]:
        if direction not in {"BUY", "SELL"} or len(m15) < 8:
            return False, "NONE"
        tail = m15.tail(8)
        last = tail.iloc[-1]
        prev = tail.iloc[:-1]
        close = _float(last["close"])
        open_ = _float(last["open"])
        minor_high = _float(prev["high"].tail(4).max())
        minor_low = _float(prev["low"].tail(4).min())
        if direction == "BUY":
            correction = _float(prev["close"].iloc[-1]) < _float(prev["close"].iloc[-2])
            confirmed = bool(correction and close is not None and open_ is not None and minor_high is not None and close > minor_high and close > open_)
            return confirmed, "BULLISH" if confirmed else "NONE"
        correction = _float(prev["close"].iloc[-1]) > _float(prev["close"].iloc[-2])
        confirmed = bool(correction and close is not None and open_ is not None and minor_low is not None and close < minor_low and close < open_)
        return confirmed, "BEARISH" if confirmed else "NONE"

    def _m1_entry_confirmation(self, m1: pd.DataFrame, direction: str) -> bool:
        if direction not in {"BUY", "SELL"} or len(m1) < 6:
            return False
        tail = m1.tail(6)
        last = tail.iloc[-1]
        prev = tail.iloc[:-1]
        close = _float(last["close"])
        open_ = _float(last["open"])
        if direction == "BUY":
            pullback = _float(prev["close"].iloc[-1]) < _float(prev["close"].iloc[-2])
            minor_high = _float(prev["high"].tail(3).max())
            return bool(pullback and close is not None and open_ is not None and minor_high is not None and close > minor_high and close > open_)
        pullback = _float(prev["close"].iloc[-1]) > _float(prev["close"].iloc[-2])
        minor_low = _float(prev["low"].tail(3).min())
        return bool(pullback and close is not None and open_ is not None and minor_low is not None and close < minor_low and close < open_)

    def _suggest_levels(self, m1: pd.DataFrame, h4_context: dict, direction: str) -> dict:
        if direction not in {"BUY", "SELL"} or m1.empty:
            return _levels()
        entry = _float(m1.iloc[-1].get("close"))
        if entry is None:
            return _levels()
        if direction == "BUY":
            sl = _float(m1["low"].tail(6).min()) or h4_context.get("h4_last_swing_low")
            tp1 = h4_context.get("h4_last_swing_high") or h4_context.get("h4_recent_resistance")
            risk = entry - sl if sl is not None else None
            tp2 = entry + (risk * 2.0) if risk is not None and risk > 0 else None
        else:
            sl = _float(m1["high"].tail(6).max()) or h4_context.get("h4_last_swing_high")
            tp1 = h4_context.get("h4_last_swing_low") or h4_context.get("h4_recent_support")
            risk = sl - entry if sl is not None else None
            tp2 = entry - (risk * 2.0) if risk is not None and risk > 0 else None
        rr = None
        if risk is not None and risk > 0 and tp1 is not None:
            rr = abs(tp1 - entry) / risk
        return _levels(entry, sl, tp1, tp2, rr)

    def _score(self, h4_bias: str, h4_zone: str, m15_confirmation: bool, m1_confirmation: bool, direction: str) -> int:
        score = 0
        if h4_bias in {"BULLISH", "BEARISH"}:
            score += 25
        if h4_zone != "NONE":
            score += 25
        if direction in {"BUY", "SELL"}:
            score += 10
        if m15_confirmation:
            score += 20
        if m1_confirmation:
            score += 20
        return min(100, score)

    def _reason(self, direction: str, h4_zone: str, m15_confirmation: bool, m1_confirmation: bool) -> str:
        missing = []
        if direction == "WAIT":
            missing.append("NO_H4_DIRECTION_ZONE_ALIGNMENT" if h4_zone != "NONE" else "PRICE_NOT_AT_H4_ZONE")
        if not m15_confirmation:
            missing.append("NO_M15_STRUCTURE_SHIFT")
        if not m1_confirmation:
            missing.append("NO_M1_ENTRY_CONFIRMATION")
        return "MTF_STRUCTURE_ALIGNED" if not missing else ",".join(missing)

    def _log(self, symbol: str, result: dict) -> None:
        log.info(
            "[MTF_STRUCTURE] symbol=%s h4_bias=%s h4_zone=%s m15_confirmation=%s m1_entry=%s direction=%s status=%s score=%s reason=%s",
            symbol,
            result.get("h4_bias"),
            result.get("h4_zone"),
            result.get("m15_confirmation"),
            result.get("m1_entry_confirmation"),
            result.get("mtf_structure_direction"),
            result.get("mtf_structure_status"),
            result.get("mtf_structure_score"),
            result.get("mtf_structure_reason"),
        )


def _result(
    status: str,
    direction: str,
    h4_bias: str,
    h4_zone: str,
    m15_confirmation: bool,
    m15_shift: str,
    m1_confirmation: bool,
    entry: float | None,
    sl: float | None,
    tp1: float | None,
    tp2: float | None,
    rr: float | None,
    score: int,
    reason: str,
    mode: str,
    h4_context: dict | None = None,
) -> dict:
    h4_context = h4_context or {}
    return {
        "mtf_structure_strategy": STRATEGY_NAME,
        "mtf_structure_mode": mode,
        "mtf_structure_status": status,
        "mtf_structure_direction": direction,
        "h4_bias": h4_bias,
        "h4_zone": h4_zone,
        "m15_confirmation": bool(m15_confirmation),
        "m15_structure_shift": m15_shift,
        "m1_entry_confirmation": bool(m1_confirmation),
        "entry_price_suggestion": entry,
        "sl_suggestion": sl,
        "tp1_suggestion": tp1,
        "tp2_suggestion": tp2,
        "risk_reward_suggestion": rr,
        "mtf_structure_score": max(0, min(100, int(score))),
        "mtf_structure_reason": reason,
        "h4_recent_support": h4_context.get("h4_recent_support"),
        "h4_recent_resistance": h4_context.get("h4_recent_resistance"),
        "h4_supply_zone": h4_context.get("h4_supply_zone"),
        "h4_demand_zone": h4_context.get("h4_demand_zone"),
        "h4_last_swing_high": h4_context.get("h4_last_swing_high"),
        "h4_last_swing_low": h4_context.get("h4_last_swing_low"),
    }


def _levels(
    entry: float | None = None,
    sl: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    rr: float | None = None,
) -> dict:
    return {
        "entry_price_suggestion": _round(entry),
        "sl_suggestion": _round(sl),
        "tp1_suggestion": _round(tp1),
        "tp2_suggestion": _round(tp2),
        "risk_reward_suggestion": _round(rr),
    }


def _missing(df: object) -> bool:
    return df is None or getattr(df, "empty", True)


def _no_trade_direction(direction: str | None) -> bool:
    return str(direction or "").upper() in {"", "WAIT", "NONE", "NULL"}


def _last_swing(df: pd.DataFrame, field: str) -> float | None:
    values = [_float(value) for value in df[field].tail(7)]
    if len(values) < 5:
        return None
    middle = values[-3]
    if field == "high" and middle == max(values[-5:]):
        return middle
    if field == "low" and middle == min(values[-5:]):
        return middle
    return _float(df[field].tail(5).max() if field == "high" else df[field].tail(5).min())


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None
