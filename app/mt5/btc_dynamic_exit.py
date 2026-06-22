"""Dynamic TP/SL/trail calculator for HERMES BTC.

Computes position-management parameters from ATR-14(M5) + confluence_score + CVD slope.
Pure computation — no MT5 imports, no direct trading calls, no side effects.
"""
from __future__ import annotations

import time

from app.logger import log

_LOG_THROTTLE_S: float = 5.0


class BtcDynamicExit:
    FALLBACK: dict = {
        "tp_usd": 1.5,
        "sl_usd": 1.2,
        "lock_usd": 0.8,
        "trail_gap_usd": 0.6,
        "trail_start_usd": 1.0,
    }

    def __init__(self) -> None:
        # Throttle state keyed by _log_key (ticket int or None).
        # Only active when _log_key is provided (daemon passes ticket).
        self._atr_last: dict = {}   # key → {"ts": float, "atr": float}
        self._dyn_last: dict = {}   # key → {"ts": float, "sl": float, "tp": float, "trail": float}

    def compute_atr(self, rates_m5: list[dict], period: int = 14, _log_key=None) -> float | None:
        """Wilder ATR-14 from M5 OHLC dicts. Returns None when candles < period+1."""
        if len(rates_m5) < period + 1:
            return None
        trs: list[float] = []
        for i in range(1, len(rates_m5)):
            h = float(rates_m5[i]["high"])
            lo = float(rates_m5[i]["low"])
            prev_c = float(rates_m5[i - 1]["close"])
            trs.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
        if len(trs) < period:
            return None
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period

        if _log_key is None:
            log.info("[BTC_ATR_COMPUTED] atr=%.4f period=%s candles=%s", atr, period, len(rates_m5))
        else:
            _now = time.monotonic()
            _prev = self._atr_last.get(_log_key, {})
            _prev_atr = float(_prev.get("atr") or 0.0)
            _elapsed = _now - float(_prev.get("ts") or 0.0)
            _material = _prev_atr == 0.0 or abs(atr - _prev_atr) / _prev_atr > 0.02
            if _elapsed >= _LOG_THROTTLE_S or _material:
                log.info("[BTC_ATR_COMPUTED] atr=%.4f period=%s candles=%s", atr, period, len(rates_m5))
                self._atr_last[_log_key] = {"ts": _now, "atr": atr}

        return atr

    def compute(
        self,
        confluence_score: float,
        rates_m5: list[dict],
        cvd_slope: float | None,
        entry_price: float,
        direction: str,
        _log_key=None,
    ) -> dict:
        """Compute dynamic TP/SL/trail params. Returns mode='fallback' if ATR unavailable."""
        atr = self.compute_atr(rates_m5, _log_key=_log_key)
        if atr is None:
            return {**self.FALLBACK, "atr_value": None, "rr_target": None, "mode": "fallback"}

        rr_target = 1.8 + (float(confluence_score) / 100.0) * 1.7
        sl_usd = max(0.8, min(3.0, atr * 1.5))
        tp_usd = max(1.0, min(6.0, sl_usd * rr_target))
        realized_rr = round(tp_usd / sl_usd, 4)
        lock_usd = sl_usd * 0.5
        trail_gap_usd = max(0.3, min(1.5, atr * 0.3))
        trail_start_usd = tp_usd * 0.8

        if str(direction).upper() == "BUY" and cvd_slope is not None and float(cvd_slope) < -100:
            trail_gap_usd = max(0.2, trail_gap_usd * 0.7)
            log.info(
                "[BTC_DYNAMIC_CVD_TIGHTEN] trail_gap=%.3f cvd_slope=%.1f",
                trail_gap_usd, float(cvd_slope),
            )

        if _log_key is None:
            log.info(
                "[BTC_DYNAMIC_EXIT] atr=%.4f rr_target=%.2f realized_rr=%.4f sl=%.3f tp=%.3f trail=%.3f mode=dynamic",
                atr, rr_target, realized_rr, sl_usd, tp_usd, trail_gap_usd,
            )
        else:
            _now = time.monotonic()
            _prev = self._dyn_last.get(_log_key, {})
            _elapsed = _now - float(_prev.get("ts") or 0.0)
            _changed = (
                abs(sl_usd - float(_prev.get("sl") or 0)) > 0.001
                or abs(tp_usd - float(_prev.get("tp") or 0)) > 0.001
                or abs(trail_gap_usd - float(_prev.get("trail") or 0)) > 0.001
            )
            if _elapsed >= _LOG_THROTTLE_S or _changed:
                log.info(
                    "[BTC_DYNAMIC_EXIT] atr=%.4f rr_target=%.2f realized_rr=%.4f sl=%.3f tp=%.3f trail=%.3f mode=dynamic",
                    atr, rr_target, realized_rr, sl_usd, tp_usd, trail_gap_usd,
                )
                self._dyn_last[_log_key] = {"ts": _now, "sl": sl_usd, "tp": tp_usd, "trail": trail_gap_usd}

        return {
            "tp_usd": round(tp_usd, 4),
            "sl_usd": round(sl_usd, 4),
            "lock_usd": round(lock_usd, 4),
            "trail_gap_usd": round(trail_gap_usd, 4),
            "trail_start_usd": round(trail_start_usd, 4),
            "atr_value": round(atr, 4),
            "rr_target": round(rr_target, 4),
            "realized_rr": realized_rr,
            "mode": "dynamic",
        }

    def adjust_on_tick(
        self,
        current_profit: float,
        min_seen_profit: float,
        params: dict,
        cvd_slope: float | None,
        direction: str,
    ) -> dict:
        """Tighten trail_gap when profit exceeds trail_start and CVD diverges on BUY."""
        trail_start = float(params.get("trail_start_usd") or 0.0)
        trail_gap = float(params.get("trail_gap_usd") or 0.0)
        if (
            float(current_profit) >= trail_start
            and str(direction).upper() == "BUY"
            and cvd_slope is not None
            and float(cvd_slope) < -50
        ):
            new_gap = max(0.2, trail_gap * 0.8)
            if new_gap < trail_gap:
                log.info(
                    "[BTC_DYNAMIC_TRAIL_TIGHTEN] profit=%.3f cvd=%.1f trail_gap=%.3f→%.3f",
                    float(current_profit), float(cvd_slope), trail_gap, new_gap,
                )
                return {**params, "trail_gap_usd": round(new_gap, 4)}
        return params
