from __future__ import annotations

import pandas as pd

from app.agents.confirmation_matrix import mtfa_calibrated_status as _calibrate_mtfa
from app.config import Settings
from app.logger import log


class MTFAFilter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, symbol: str, frames: dict, direction: str | None) -> dict:
        mode = (self.settings.mtfa_mode or "TAG_ONLY").upper()
        if not self.settings.mtfa_enabled:
            return _result("NEUTRAL", False, None, False, "PASS", "MTFA_DISABLED", 0, mode)

        try:
            if _no_trade_direction(direction):
                out = _result("NEUTRAL", False, None, False, "NOT_APPLICABLE", "NO_TRADE_DIRECTION", 0, mode)
                self._log(symbol, direction, out)
                return out
            h1 = frames.get("H1")
            m15 = frames.get("M15")
            m5 = frames.get("M5")
            if _missing(h1) or _missing(m15) or _missing(m5):
                out = _result("NEUTRAL", False, None, False, "FAIL", "MISSING_TIMEFRAME_DATA", 0, mode)
                self._log(symbol, direction, out)
                return out

            h1_bias = self._h1_bias(h1)
            liquidity, liquidity_type, liquidity_strength, level_side = self._m15_liquidity(m15)
            cisd = self._m5_cisd(m5, direction, level_side)
            score = self._score(h1_bias, liquidity, liquidity_strength, cisd, direction)
            status, reason = self._status(h1_bias, liquidity, liquidity_strength, cisd, direction)
            out = _result(h1_bias, liquidity, liquidity_type, cisd, status, reason, score, mode)
            self._log(symbol, direction, out)
            return out
        except Exception as exc:
            out = _result("NEUTRAL", False, None, False, "FAIL", f"MTFA_ERROR:{exc}", 0, mode)
            self._log(symbol, direction, out)
            return out

    def _h1_bias(self, df: pd.DataFrame) -> str:
        closed = df.iloc[:-1]  # exclude live H1 candle
        tail = closed.tail(12)
        if len(tail) < 6:
            return "NEUTRAL"
        prev = tail.iloc[:-1]
        last = tail.iloc[-1]  # last confirmed closed H1
        prev_high = float(prev["high"].tail(5).max())
        prev_low = float(prev["low"].tail(5).min())
        close = float(last["close"])
        if close > prev_high:
            return "BULLISH"
        if close < prev_low:
            return "BEARISH"
        return "NEUTRAL"

    def _m15_liquidity(self, df: pd.DataFrame) -> tuple[bool, str | None, bool, str | None]:
        closed = df.iloc[:-1]  # exclude live M15 candle
        tail = closed.tail(20)
        if len(tail) < 6:
            return False, None, False, None
        last = tail.iloc[-1]  # last confirmed closed M15
        prev = tail.iloc[:-1]
        high = float(prev["high"].tail(10).max())
        low = float(prev["low"].tail(10).min())
        close = float(last["close"])
        last_high = float(last["high"])
        last_low = float(last["low"])
        body = abs(float(last["close"]) - float(last["open"]))
        upper_reject = last_high - max(float(last["close"]), float(last["open"]))
        lower_reject = min(float(last["close"]), float(last["open"])) - last_low

        if last_low < low and close > low:
            return True, "SELL_SIDE_SWEEP", lower_reject > max(body, 0), "LOW"
        if last_high > high and close < high:
            return True, "BUY_SIDE_SWEEP", upper_reject > max(body, 0), "HIGH"
        recent_highs = prev["high"].tail(6)
        recent_lows = prev["low"].tail(6)
        tolerance = max(close * 0.0005, 0.00001)
        if float(recent_highs.max() - recent_highs.min()) <= tolerance:
            return True, "EQUAL_HIGHS", False, "HIGH"
        if float(recent_lows.max() - recent_lows.min()) <= tolerance:
            return True, "EQUAL_LOWS", False, "LOW"
        return False, None, False, None

    def _m5_cisd(self, df: pd.DataFrame, direction: str | None, level_side: str | None) -> bool:
        if direction not in {"BUY", "SELL"} or len(df) < 9:  # +1 for excluded live candle
            return False
        closed = df.iloc[:-1]  # exclude live M5 candle
        tail = closed.tail(8)
        last = tail.iloc[-1]  # last confirmed closed M5
        prev = tail.iloc[:-1]
        prev_high = float(prev["high"].tail(5).max())
        prev_low = float(prev["low"].tail(5).min())
        close = float(last["close"])
        open_ = float(last["open"])
        if direction == "BUY":
            swept_low = float(tail["low"].min()) <= prev_low or level_side == "LOW"
            displacement_high = float(prev["high"].tail(3).max())
            return swept_low and close > displacement_high and close > open_
        swept_high = float(tail["high"].max()) >= prev_high or level_side == "HIGH"
        displacement_low = float(prev["low"].tail(3).min())
        return swept_high and close < displacement_low and close < open_

    def _score(self, h1_bias: str, liquidity: bool, strong_liquidity: bool, cisd: bool, direction: str | None) -> int:
        score = 0
        if _bias_agrees(h1_bias, direction):
            score += 35
        elif h1_bias == "NEUTRAL":
            score += 15
        if liquidity:
            score += 25
        if strong_liquidity:
            score += 15
        if cisd:
            score += 25
        return min(100, score)

    def _status(
        self,
        h1_bias: str,
        liquidity: bool,
        strong_liquidity: bool,
        cisd: bool,
        direction: str | None,
    ) -> tuple[str, str]:
        checks = []
        if self.settings.mtfa_require_h1_bias:
            checks.append(_bias_agrees(h1_bias, direction) or (h1_bias == "NEUTRAL" and strong_liquidity))
        if self.settings.mtfa_require_m15_liquidity:
            checks.append(liquidity)
        if self.settings.mtfa_require_m5_cisd:
            checks.append(cisd)
        if all(checks):
            return "PASS", "MTFA_ALIGNED"
        reasons = []
        if self.settings.mtfa_require_h1_bias and not (_bias_agrees(h1_bias, direction) or (h1_bias == "NEUTRAL" and strong_liquidity)):
            reasons.append("H1_BIAS_MISMATCH")
        if self.settings.mtfa_require_m15_liquidity and not liquidity:
            reasons.append("NO_M15_LIQUIDITY")
        if self.settings.mtfa_require_m5_cisd and not cisd:
            reasons.append("NO_M5_CISD")
        return "FAIL", ",".join(reasons) if reasons else "MTFA_FAIL"

    def _log(self, symbol: str, direction: str | None, result: dict) -> None:
        log.info(
            "[MTFA] symbol=%s h1_bias=%s m15_liquidity=%s m5_cisd=%s direction=%s mode=%s status=%s reason=%s score=%s",
            symbol,
            result.get("h1_bias"),
            result.get("m15_liquidity"),
            result.get("m5_cisd"),
            direction,
            result.get("mtfa_mode"),
            result.get("mtfa_status"),
            result.get("mtfa_reason"),
            result.get("mtfa_score"),
        )


def _missing(df: object) -> bool:
    return df is None or getattr(df, "empty", True)


def _bias_agrees(h1_bias: str, direction: str | None) -> bool:
    return (direction == "BUY" and h1_bias == "BULLISH") or (direction == "SELL" and h1_bias == "BEARISH")


def _no_trade_direction(direction: str | None) -> bool:
    return str(direction or "").upper() in {"", "WAIT", "NONE", "NULL"}


def _result(
    h1_bias: str,
    m15_liquidity: bool,
    m15_liquidity_type: str | None,
    m5_cisd: bool,
    status: str,
    reason: str,
    score: int,
    mode: str,
) -> dict:
    return {
        "h1_bias": h1_bias,
        "m15_liquidity": bool(m15_liquidity),
        "m15_liquidity_type": m15_liquidity_type,
        "m5_cisd": bool(m5_cisd),
        "mtfa_status": status,
        "mtfa_reason": reason,
        "mtfa_score": score,
        "mtfa_mode": mode,
        "mtfa_calibrated_status": _calibrate_mtfa(score),
    }
