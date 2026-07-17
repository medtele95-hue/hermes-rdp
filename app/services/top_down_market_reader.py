from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from app.utils.candles import closed_frame

_log = logging.getLogger(__name__)

REQUIRED_TIMEFRAMES = ("D1", "H4", "H1", "M15", "M5", "M1")
MIN_CLOSED_CANDLES = 100

# Internal → user-visible mapping for missing timeframe names
_TF_RATES_MISSING: dict[str, str] = {
    "D1_MISSING_OR_INSUFFICIENT":  "D1_RATES_MISSING",
    "H4_MISSING_OR_INSUFFICIENT":  "H4_RATES_MISSING",
    "H1_MISSING_OR_INSUFFICIENT":  "H1_RATES_MISSING",
    "M15_MISSING_OR_INSUFFICIENT": "M15_RATES_MISSING",
    "M5_MISSING_OR_INSUFFICIENT":  "M5_RATES_MISSING",
    "M1_MISSING_OR_INSUFFICIENT":  "M1_RATES_MISSING",
}


@dataclass(frozen=True)
class ReaderInputs:
    symbol: str
    frames: dict[str, Any] | None
    direction: str | None
    entry: float | None
    sl: float | None
    tp: float | None
    spread_points: float | None
    max_spread_points: float | None
    decision_time: datetime | None = None
    smc_score: float | None = None
    timeframe: str = "M5"


class TopDownMarketReader:
    @staticmethod
    def unavailable(symbol: str, timeframe: str = "M5", reason: str = "TOP_DOWN_DATA_MISSING") -> dict:
        narrative = f"{symbol} top-down read unavailable: {reason}."
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "top_down_status": "FAIL",
            "decision": "WAIT_FOR_CONFIRMATION",
            "top_down_decision": "WAIT_FOR_CONFIRMATION",
            "entry_readiness_score": 0,
            "market_narrative": narrative,
            "d1_macro_bias": "UNKNOWN",
            "h4_main_bias": "UNKNOWN",
            "h1_internal_structure": "UNKNOWN",
            "m15_confirmation_status": "FAIL",
            "m5_context_status": "UNKNOWN",
            "m1_trigger_status": "FAIL",
            "timeframe_alignment_score": 0,
            "timeframe_alignment_status": "UNKNOWN",
            "timeframe_missing_confirmations": [reason],
            "timeframe_narrative": narrative,
            "h4_bias": "UNKNOWN",
            "h1_bias": "UNKNOWN",
            "m15_confirmation": False,
            "m5_context": "UNKNOWN",
            "m1_trigger": False,
            "liquidity_sweep": False,
            "bos_choch": False,
            "ob_fvg_present": False,
            "premium_discount_zone": "UNKNOWN",
            "price_location": "UNKNOWN",
            "trade_plan_quality": "WAIT",
            "missing_confirmations": [reason],
            "score_breakdown": {},
            "reason": reason,
        }

    def evaluate(
        self,
        symbol: str,
        frames: dict[str, Any] | None,
        direction: str | None,
        entry: float | None,
        sl: float | None,
        tp: float | None,
        spread_points: float | None,
        max_spread_points: float | None,
        decision_time: datetime | None = None,
        smc_score: float | None = None,
        timeframe: str = "M5",
    ) -> dict:
        inputs = ReaderInputs(
            symbol=symbol,
            frames=frames,
            direction=str(direction or "").upper() or None,
            entry=_to_float(entry),
            sl=_to_float(sl),
            tp=_to_float(tp),
            spread_points=_to_float(spread_points),
            max_spread_points=_to_float(max_spread_points),
            decision_time=decision_time,
            smc_score=_to_float(smc_score),
            timeframe=timeframe,
        )
        return self.read(inputs)

    def read(self, inputs: ReaderInputs) -> dict:
        frames, missing = self._prepare_frames(inputs.frames, inputs.decision_time)
        if missing:
            mapped = [_TF_RATES_MISSING.get(m, m) for m in missing]
            reason = ",".join(mapped) if mapped else "TOP_DOWN_SNAPSHOT_NOT_READY"
            _log.warning(
                "[TOP_DOWN_DATA_MISSING] symbol=%s missing=%s",
                inputs.symbol,
                ",".join(mapped),
            )
            out = self.unavailable(inputs.symbol, inputs.timeframe, reason)
            out["missing_confirmations"] = mapped
            out["timeframe_missing_confirmations"] = mapped
            return out
        direction = str(inputs.direction or "").upper()
        missing_confirmations = list(missing)
        hard_invalid: list[str] = []
        scores: dict[str, float] = {
            "d1_macro_bias": 0.0,
            "h4_main_bias": 0.0,
            "h1_internal_structure": 0.0,
            "timeframe_alignment": 0.0,
            "htf_alignment": 0.0,
            "price_location": 0.0,
            "liquidity_sweep": 0.0,
            "bos_choch": 0.0,
            "ob_fvg_ifvg": 0.0,
            "m15_confirmation": 0.0,
            "m5_context": 0.0,
            "m1_trigger": 0.0,
            "rr_sl_tp": 0.0,
            "spread": 0.0,
            "data_completeness_penalty": 0.0,
        }

        if direction not in {"BUY", "SELL"}:
            hard_invalid.append("NO_DIRECTION")
            missing_confirmations.append("DIRECTION")

        d1 = self._analyze_trend(frames.get("D1"))
        h4 = self._analyze_trend(frames.get("H4"))
        h1 = self._analyze_trend(frames.get("H1"))
        d1_bias = d1["bias"]
        h4_bias = h4["bias"]
        h1_bias = h1["bias"]
        wanted_bias = "BULLISH" if direction == "BUY" else ("BEARISH" if direction == "SELL" else None)
        opposite_bias = "BEARISH" if direction == "BUY" else ("BULLISH" if direction == "SELL" else None)
        d1_h4_conflict = bool(
            wanted_bias
            and {d1_bias, h4_bias} == {"BULLISH", "BEARISH"}
        )
        h4_h1_conflict = bool(
            wanted_bias
            and {h4_bias, h1_bias} == {"BULLISH", "BEARISH"}
        )
        h4_direction_conflict = bool(wanted_bias and h4_bias == opposite_bias)
        current_price = _last_close(frames.get("M5")) or _last_close(frames.get("M1")) or inputs.entry
        atr_h4 = h4.get("atr14") or _safe_last(atr14(frames.get("H4"))) or 0.0
        h4_swings = _confirmed_swings(frames.get("H4"))
        range_high = h4_swings["highs"][-1][1] if h4_swings["highs"] else None
        range_low = h4_swings["lows"][-1][1] if h4_swings["lows"] else None
        price_location = "UNKNOWN"
        premium_discount = "UNKNOWN"
        near_demand = False
        near_supply = False

        if direction in {"BUY", "SELL"}:
            scores["htf_alignment"] = float(self._alignment_score(h4_bias, h1_bias, direction))
            scores["d1_macro_bias"] = float(self._d1_macro_score(d1_bias, h4_bias, direction))
            scores["h4_main_bias"] = float(self._h4_main_bias_score(h4_bias, direction))
            if h4_bias == "RANGE":
                missing_confirmations.append("H4_DIRECTION_CLEAR")
            if h1_bias == "RANGE":
                missing_confirmations.append("H1_DIRECTION_CLEAR")
            if h4_direction_conflict:
                missing_confirmations.append("H4_MAIN_BIAS_CONFLICT")
            if h4_h1_conflict:
                missing_confirmations.append("H1_CONFLICT")
            if d1_h4_conflict:
                missing_confirmations.append("D1_H4_CONFLICT")

        if current_price is not None and range_high is not None and range_low is not None and range_high > range_low:
            ratio = (current_price - range_low) / (range_high - range_low)
            premium_discount = premium_discount_zone(ratio)
            demand_low = range_low
            demand_high = range_low + atr_h4 * 0.5
            supply_high = range_high
            supply_low = range_high - atr_h4 * 0.5
            near_demand = _near_zone(current_price, demand_low, demand_high, atr_h4)
            near_supply = _near_zone(current_price, supply_low, supply_high, atr_h4)
            if near_demand:
                price_location = "H4_DEMAND"
            elif near_supply:
                price_location = "H4_SUPPLY"
            elif current_price > range_high or current_price < range_low:
                price_location = "BREAKOUT_ZONE"
            else:
                price_location = "MID_RANGE"
            scores["price_location"] = float(self._price_location_score(direction, premium_discount, near_demand, near_supply))
        else:
            missing_confirmations.append("H4_DEALING_RANGE")

        m15 = frames.get("M15")
        m5 = frames.get("M5")
        m1 = frames.get("M1")
        sweep = self._liquidity_sweep(m15, direction)
        scores["liquidity_sweep"] = float(sweep["score"])
        h1_structure = self._bos_choch(frames.get("H1"), direction)
        m15_bos = self._bos_choch(m15, direction)
        bos = {
            "present": bool(h1_structure["present"] or m15_bos["present"]),
            "score": max(float(h1_structure["score"]), float(m15_bos["score"])),
        }
        scores["bos_choch"] = float(bos["score"])
        scores["h1_internal_structure"] = float(self._h1_internal_structure_score(h1_bias, h1_structure["present"], direction))
        fvg = self._fvg_score(m15, direction)
        ob = self._order_block_score(m15, direction)
        scores["ob_fvg_ifvg"] = float(min(10, max(fvg["score"], ob["score"])))
        m15_confirmation = self._m15_confirmation(m15, direction, m15_bos["present"], fvg["retest"] or ob["retest"])
        scores["m15_confirmation"] = 20.0 if m15_confirmation else 0.0
        m5_context = self._m5_context(m5, direction)
        scores["m5_context"] = float(self._m5_hierarchy_score(m5_context["context"]))
        m1_trigger = self._m1_trigger(m1, direction)
        scores["m1_trigger"] = float(m1_trigger["score"])
        scores["timeframe_alignment"] = float(
            self._timeframe_alignment_score(
                direction,
                d1_bias,
                h4_bias,
                h1_bias,
                m15_confirmation,
                m1_trigger["fresh"],
                d1_h4_conflict,
                h4_h1_conflict,
            )
        )

        if not sweep["present"]:
            missing_confirmations.append("LIQUIDITY_SWEEP")
        if not bos["present"]:
            missing_confirmations.append("BOS_CHOCH")
        if not (fvg["present"] or ob["present"]):
            missing_confirmations.append("OB_FVG")
        if not m15_confirmation:
            missing_confirmations.append("M15_CONFIRMATION")
        if not m1_trigger["fresh"]:
            missing_confirmations.append("M1_ENTRY")

        rr_result = validate_rr(direction, inputs.entry, inputs.sl, inputs.tp)
        scores["rr_sl_tp"] = float(rr_result["score"])
        if not rr_result["valid"]:
            hard_invalid.append("INVALID_SL_TP")
            missing_confirmations.append("RR_VALID")
        spread_result = spread_score(inputs.spread_points, inputs.max_spread_points)
        scores["spread"] = float(spread_result["score"])
        if not spread_result["ok"]:
            hard_invalid.append("SPREAD_FAIL")
            missing_confirmations.append("SPREAD_OK")
        if inputs.smc_score is not None and inputs.smc_score < 35:
            hard_invalid.append("SMC_SCORE_LT_35")

        data_penalty = -5.0 * len(missing)
        scores["data_completeness_penalty"] = data_penalty
        hierarchy_total = (
            scores["d1_macro_bias"]
            + scores["h4_main_bias"]
            + scores["h1_internal_structure"]
            + scores["m15_confirmation"]
            + scores["m5_context"]
            + scores["m1_trigger"]
            + data_penalty
        )
        total = max(0, min(100, hierarchy_total))
        h4_range_confirmed = bool(h4_bias == "RANGE" and sweep["present"] and bos["present"] and m15_confirmation and m1_trigger["fresh"])

        required_confirmations = (
            direction in {"BUY", "SELL"}
            and not h4_direction_conflict
            and not h4_h1_conflict
            and m15_confirmation
            and m1_trigger["fresh"]
            and rr_result["valid"]
            and spread_result["ok"]
            and not hard_invalid
            and not (h4_bias == "RANGE" and not h4_range_confirmed)
        )
        if "M1_MISSING_OR_INSUFFICIENT" in missing or "M15_MISSING_OR_INSUFFICIENT" in missing:
            required_confirmations = False

        if hard_invalid:
            decision = "AVOID"
        elif h4_h1_conflict or h4_direction_conflict:
            decision = "AVOID"
        elif d1_h4_conflict and not (m15_confirmation and m1_trigger["fresh"]):
            decision = "AVOID"
        elif h4_bias == "RANGE" and not h4_range_confirmed:
            decision = "WAIT_FOR_CONFIRMATION"
        elif total >= 75 and required_confirmations and (bos["present"] or fvg["present"] or ob["present"]):
            decision = "ALLOW_DEMO"
        elif total >= 45 or near_demand or near_supply or sweep["present"]:
            decision = "WAIT_FOR_CONFIRMATION"
        else:
            decision = "AVOID"

        if decision == "ALLOW_DEMO":
            status = "PASS"
        elif decision == "WAIT_FOR_CONFIRMATION":
            status = "WAIT"
        else:
            status = "FAIL" if total < 45 else "WAIT"

        if spread_result["ok"] is False or not rr_result["valid"] or "SMC_SCORE_LT_35" in hard_invalid:
            status = "FAIL"

        h1_internal_structure = self._h1_internal_structure_label(h1_bias, h1_structure["present"], direction)
        m15_confirmation_status = "PASS" if m15_confirmation else "FAIL"
        m5_context_status = m5_context["context"]
        m1_trigger_status = "PASS" if m1_trigger["fresh"] else ("STALE" if m1_trigger["age"] is not None else "FAIL")
        timeframe_alignment_score = round(total, 2)
        timeframe_alignment_status = self._timeframe_alignment_status(
            direction,
            d1_bias,
            h4_bias,
            h1_bias,
            m15_confirmation,
            m1_trigger["fresh"],
            h4_h1_conflict,
            d1_h4_conflict,
        )

        narrative = _timeframe_narrative(
            inputs.symbol,
            d1_bias,
            h4_bias,
            h1_bias,
            h1_internal_structure,
            m15_confirmation,
            m5_context_status,
            m1_trigger["fresh"],
            bos["present"],
            sweep["present"],
            spread_result["ok"],
            inputs.smc_score,
        )
        missing_confirmations = _unique(missing_confirmations)
        reason = "PASS" if decision == "ALLOW_DEMO" else ",".join(missing_confirmations or hard_invalid or ["LOW_TOP_DOWN_SCORE"])
        _log.info(
            "[TOP_DOWN_DATA_BUILD] symbol=%s status=%s decision=%s score=%s missing=%s",
            inputs.symbol,
            status,
            decision,
            round(total, 1),
            ",".join(missing_confirmations) if missing_confirmations else "NONE",
        )
        return {
            "symbol": inputs.symbol,
            "timeframe": inputs.timeframe,
            "top_down_status": status,
            "decision": decision,
            "top_down_decision": decision,
            "entry_readiness_score": round(total, 2),
            "market_narrative": narrative,
            "d1_macro_bias": d1_bias,
            "h4_main_bias": h4_bias,
            "h1_internal_structure": h1_internal_structure,
            "m15_confirmation_status": m15_confirmation_status,
            "m5_context_status": m5_context_status,
            "m1_trigger_status": m1_trigger_status,
            "timeframe_alignment_score": timeframe_alignment_score,
            "timeframe_alignment_status": timeframe_alignment_status,
            "timeframe_missing_confirmations": missing_confirmations,
            "timeframe_narrative": narrative,
            "h4_bias": h4_bias,
            "h1_bias": h1_bias,
            "m15_confirmation": bool(m15_confirmation),
            "m5_context": m5_context["context"],
            "m1_trigger": bool(m1_trigger["fresh"]),
            "liquidity_sweep": bool(sweep["present"]),
            "bos_choch": bool(bos["present"]),
            "ob_fvg_present": bool(fvg["present"] or ob["present"]),
            "premium_discount_zone": premium_discount,
            "price_location": price_location,
            "trade_plan_quality": "GOOD" if decision == "ALLOW_DEMO" else ("WAIT" if decision == "WAIT_FOR_CONFIRMATION" else "BAD"),
            "missing_confirmations": missing_confirmations,
            "score_breakdown": scores,
            "reason": reason,
            "rr": rr_result["rr"],
            "spread_ratio": spread_result["ratio"],
            "d1_bias_confidence": d1["confidence"],
            "h4_bias_confidence": h4["confidence"],
            "h1_bias_confidence": h1["confidence"],
            "liquidity_sweep_strength": sweep["strength"],
            "m1_trigger_age": m1_trigger["age"],
        }

    def _prepare_frames(self, frames: dict[str, Any] | None, decision_time: datetime | None) -> tuple[dict[str, pd.DataFrame], list[str]]:
        """Prepare les frames du verdict — LE point de passage unique.

        P0-TER (2026-07-14) : la bougie EN COURS est retiree ici, une fois pour
        toutes. Tous les calculs en aval (sweep, BOS, FVG, order blocks,
        m15_confirmation, m1_trigger, trend, current_price) indexent `.iloc[-1]`
        : cette ligne designe desormais la derniere bougie CLOTUREE, plus la
        bougie en formation. Corriger ici plutot qu'aux 20 sites .iloc[-1]
        evite qu'un futur ajout de site ne reintroduise le repaint.

        `decision_time` reste un cutoff "as-of" OPTIONNEL (outil de replay ;
        None en production). Il n'a JAMAIS filtre la bougie en cours, contrairement
        a ce que son existence laissait croire — c'etait le code zombie pointe par
        l'audit. L'ordre des deux operations est volontaire : le cutoff garde la
        bougie qui etait OUVERTE a cet instant (candle_time = heure d'OUVERTURE
        <= cutoff), c'est-a-dire exactement la bougie en cours de ce moment-la.
        Retirer la derniere ligne APRES le cutoff est donc correct dans les deux
        cas — avec cutoff comme sans — et ne peut jamais retirer deux bougies.
        """
        prepared: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for timeframe in REQUIRED_TIMEFRAMES:
            df = _to_frame((frames or {}).get(timeframe))
            if not df.empty and "candle_time" in df.columns:
                df = df.copy()
                df["candle_time"] = pd.to_datetime(df["candle_time"], utc=True, errors="coerce")
                df = df[df["candle_time"].notna()]
                if decision_time is not None:
                    cutoff = pd.Timestamp(decision_time)
                    if cutoff.tzinfo is None:
                        cutoff = cutoff.tz_localize("UTC")
                    else:
                        cutoff = cutoff.tz_convert("UTC")
                    df = df[df["candle_time"] <= cutoff]
                df = df.sort_values("candle_time")
            df = closed_frame(df)
            # Le controle de suffisance vient APRES le retrait : sinon un frame
            # de MIN_CLOSED_CANDLES pile passerait le test puis tomberait a MIN-1.
            if len(df) < MIN_CLOSED_CANDLES:
                missing.append(f"{timeframe}_MISSING_OR_INSUFFICIENT")
            prepared[timeframe] = df
        return prepared, missing

    def _analyze_trend(self, df: pd.DataFrame | None) -> dict:
        if df is None or len(df) < 60:
            return {"bias": "RANGE", "confidence": 0, "score": 0, "atr14": None}
        close = _series(df, "close")
        high = _series(df, "high")
        low = _series(df, "low")
        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        ema200 = ema(close, 200)
        atr = atr14(df)
        swings = _confirmed_swings(df)
        highs = swings["highs"]
        lows = swings["lows"]
        score = 0
        last_close = close.iloc[-1]
        last_atr = _safe_last(atr) or 1.0
        if last_close > ema200.iloc[-1]:
            score += 25
        if last_close < ema200.iloc[-1]:
            score -= 25
        if ema20.iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1]:
            score += 20
        if ema20.iloc[-1] < ema50.iloc[-1] < ema200.iloc[-1]:
            score -= 20
        if len(highs) >= 2:
            if highs[-1][1] > highs[-2][1]:
                score += 20
            elif highs[-1][1] < highs[-2][1]:
                score -= 20
        if len(lows) >= 2:
            if lows[-1][1] > lows[-2][1]:
                score += 20
            elif lows[-1][1] < lows[-2][1]:
                score -= 20
        if len(ema50) > 10 and last_atr > 0:
            slope = (ema50.iloc[-1] - ema50.iloc[-11]) / last_atr
            if slope > 0:
                score += 15
            elif slope < 0:
                score -= 15
        bias = "BULLISH" if score >= 45 else ("BEARISH" if score <= -45 else "RANGE")
        return {"bias": bias, "confidence": min(abs(score), 100), "score": score, "atr14": _safe_last(atr)}

    def _alignment_score(self, h4_bias: str, h1_bias: str, direction: str) -> int:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite = "BEARISH" if direction == "BUY" else "BULLISH"
        if h4_bias == wanted and h1_bias == wanted:
            return 15
        if (h4_bias == wanted and h1_bias == "RANGE") or (h1_bias == wanted and h4_bias == "RANGE"):
            return 8
        if h4_bias == opposite and h1_bias == opposite:
            return -15
        if {h4_bias, h1_bias} == {"BULLISH", "BEARISH"}:
            return -15
        return 0

    def _d1_macro_score(self, d1_bias: str, h4_bias: str, direction: str) -> int:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite = "BEARISH" if direction == "BUY" else "BULLISH"
        if d1_bias == wanted and h4_bias == wanted:
            return 10
        if d1_bias == "RANGE":
            return 4
        if d1_bias == opposite and h4_bias == wanted:
            return -6
        if d1_bias == wanted and h4_bias == "RANGE":
            return 6
        if d1_bias == opposite and h4_bias == opposite:
            return 0
        return 0

    def _h4_main_bias_score(self, h4_bias: str, direction: str) -> int:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite = "BEARISH" if direction == "BUY" else "BULLISH"
        if h4_bias == wanted:
            return 25
        if h4_bias == "RANGE":
            return 10
        if h4_bias == opposite:
            return -25
        return 0

    def _h1_internal_structure_score(self, h1_bias: str, bos_choch_present: bool, direction: str) -> int:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite = "BEARISH" if direction == "BUY" else "BULLISH"
        if h1_bias == opposite:
            return -20
        if h1_bias == wanted and bos_choch_present:
            return 20
        if h1_bias == wanted:
            return 14
        if h1_bias == "RANGE" and bos_choch_present:
            return 10
        if h1_bias == "RANGE":
            return 4
        return 0

    def _h1_internal_structure_label(self, h1_bias: str, bos_choch_present: bool, direction: str) -> str:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite = "BEARISH" if direction == "BUY" else "BULLISH"
        if h1_bias == opposite:
            return "CONFLICT"
        if bos_choch_present:
            return "BOS_CHOCH"
        if h1_bias == wanted:
            return "CONTINUATION"
        if h1_bias == "RANGE":
            return "RANGE"
        return "UNKNOWN"

    def _m5_hierarchy_score(self, context: str) -> int:
        if context in {"BULLISH_CONTEXT", "BEARISH_CONTEXT"}:
            return 10
        if context == "NEUTRAL":
            return 5
        return 0

    def _timeframe_alignment_score(
        self,
        direction: str,
        d1_bias: str,
        h4_bias: str,
        h1_bias: str,
        m15_confirmation: bool,
        m1_trigger: bool,
        d1_h4_conflict: bool,
        h4_h1_conflict: bool,
    ) -> int:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        if d1_h4_conflict or h4_h1_conflict:
            return -10
        aligned_count = sum(
            [
                d1_bias == wanted,
                h4_bias == wanted,
                h1_bias == wanted,
                bool(m15_confirmation),
                bool(m1_trigger),
            ]
        )
        return min(10, aligned_count * 2)

    def _timeframe_alignment_status(
        self,
        direction: str,
        d1_bias: str,
        h4_bias: str,
        h1_bias: str,
        m15_confirmation: bool,
        m1_trigger: bool,
        h4_h1_conflict: bool,
        d1_h4_conflict: bool,
    ) -> str:
        wanted = "BULLISH" if direction == "BUY" else "BEARISH"
        if h4_h1_conflict or d1_h4_conflict:
            return "CONFLICT"
        if h4_bias == wanted and h1_bias == wanted and m15_confirmation and m1_trigger:
            return "ALIGNED" if d1_bias in {wanted, "RANGE"} else "PARTIAL"
        return "PARTIAL"

    def _price_location_score(self, direction: str, zone: str, near_demand: bool, near_supply: bool) -> int:
        if direction == "BUY":
            return 10 if zone == "DISCOUNT" else (5 if near_demand else (-10 if zone == "PREMIUM" else 0))
        if direction == "SELL":
            return 10 if zone == "PREMIUM" else (5 if near_supply else (-10 if zone == "DISCOUNT" else 0))
        return 0

    def _liquidity_sweep(self, df: pd.DataFrame | None, direction: str) -> dict:
        if df is None or len(df) < 20:
            return {"present": False, "score": 0, "strength": 0.0}
        swings = _confirmed_swings(df)
        atr = _safe_last(atr14(df)) or 0.0
        last = df.iloc[-1]
        if atr <= 0:
            return {"present": False, "score": 0, "strength": 0.0}
        if direction == "BUY" and swings["lows"]:
            level = swings["lows"][-1][1]
            distance = abs(float(last["low"]) - level) / atr
            if float(last["low"]) < level and float(last["close"]) > level:
                return {"present": True, "score": 10 if distance >= 0.10 else 5, "strength": round(distance, 4)}
        if direction == "SELL" and swings["highs"]:
            level = swings["highs"][-1][1]
            distance = abs(float(last["high"]) - level) / atr
            if float(last["high"]) > level and float(last["close"]) < level:
                return {"present": True, "score": 10 if distance >= 0.10 else 5, "strength": round(distance, 4)}
        return {"present": False, "score": 0, "strength": 0.0}

    def _bos_choch(self, df: pd.DataFrame | None, direction: str) -> dict:
        if df is None or len(df) < 20:
            return {"present": False, "score": 0}
        swings = _confirmed_swings(df)
        atr = _safe_last(atr14(df)) or 0.0
        if atr <= 0:
            return {"present": False, "score": 0}
        close = float(df.iloc[-1]["close"])
        if direction == "BUY" and swings["highs"]:
            return {"present": close > swings["highs"][-1][1] + 0.1 * atr, "score": 12 if close > swings["highs"][-1][1] + 0.1 * atr else 0}
        if direction == "SELL" and swings["lows"]:
            return {"present": close < swings["lows"][-1][1] - 0.1 * atr, "score": 12 if close < swings["lows"][-1][1] - 0.1 * atr else 0}
        return {"present": False, "score": 0}

    def _fvg_score(self, df: pd.DataFrame | None, direction: str) -> dict:
        zones = detect_fvg(df)
        if df is None or df.empty or not zones:
            return {"present": False, "score": 0, "retest": False}
        last = df.iloc[-1]
        for zone in reversed(zones[-10:]):
            if direction == "BUY" and zone["type"] in {"BULLISH_FVG", "BULLISH_IFVG"}:
                retest = float(last["low"]) <= zone["high"] and float(last["close"]) > zone["high"]
                return {"present": True, "score": 10 if retest else 5, "retest": retest}
            if direction == "SELL" and zone["type"] in {"BEARISH_FVG", "BEARISH_IFVG"}:
                retest = float(last["high"]) >= zone["low"] and float(last["close"]) < zone["low"]
                return {"present": True, "score": 10 if retest else 5, "retest": retest}
        return {"present": bool(zones), "score": 0, "retest": False}

    def _order_block_score(self, df: pd.DataFrame | None, direction: str) -> dict:
        ob = detect_order_block(df, direction)
        if not ob:
            return {"present": False, "score": 0, "retest": False}
        last = df.iloc[-1]
        if direction == "BUY":
            retest = float(last["low"]) <= ob["high"] and float(last["close"]) > ob["high"]
        else:
            retest = float(last["high"]) >= ob["low"] and float(last["close"]) < ob["low"]
        return {"present": True, "score": 8 if retest else 4, "retest": retest}

    def _m15_confirmation(self, df: pd.DataFrame | None, direction: str, bos_present: bool, zone_retest: bool) -> bool:
        if df is None or len(df) < 20:
            return False
        close = _series(df, "close")
        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        last = df.iloc[-1]
        swings = _confirmed_swings(df)
        if direction == "BUY":
            swing_break = bool(swings["highs"] and float(last["close"]) > swings["highs"][-1][1])
            ema_confirm = float(last["close"]) > ema20.iloc[-1] and float(last["close"]) > ema50.iloc[-1]
            return bool(swing_break or bos_present or ema_confirm or zone_retest)
        if direction == "SELL":
            swing_break = bool(swings["lows"] and float(last["close"]) < swings["lows"][-1][1])
            ema_confirm = float(last["close"]) < ema20.iloc[-1] and float(last["close"]) < ema50.iloc[-1]
            return bool(swing_break or bos_present or ema_confirm or zone_retest)
        return False

    def _m5_context(self, df: pd.DataFrame | None, direction: str) -> dict:
        if df is None or len(df) < 20:
            return {"context": "MISSING", "score": -8}
        last = df.iloc[-1]
        prev = df.iloc[-2]
        ema20 = ema(_series(df, "close"), 20).iloc[-1]
        candle_range = max(float(last["high"]) - float(last["low"]), 0.0)
        body = abs(float(last["close"]) - float(last["open"]))
        body_ratio = body / candle_range if candle_range > 0 else 0
        if direction == "BUY":
            top_close = (float(last["high"]) - float(last["close"])) <= candle_range * 0.35 if candle_range > 0 else False
            confirms = (float(last["close"]) > ema20 or (float(prev["close"]) < ema20 <= float(last["close"]))) and body_ratio >= 0.40 and top_close
            opposite = float(last["close"]) < ema20 and float(last["close"]) < float(last["open"])
            return {"context": "BULLISH_CONTEXT" if confirms else ("OPPOSITE" if opposite else "NEUTRAL"), "score": 8 if confirms else (-8 if opposite else 4)}
        if direction == "SELL":
            bottom_close = (float(last["close"]) - float(last["low"])) <= candle_range * 0.35 if candle_range > 0 else False
            confirms = (float(last["close"]) < ema20 or (float(prev["close"]) > ema20 >= float(last["close"]))) and body_ratio >= 0.40 and bottom_close
            opposite = float(last["close"]) > ema20 and float(last["close"]) > float(last["open"])
            return {"context": "BEARISH_CONTEXT" if confirms else ("OPPOSITE" if opposite else "NEUTRAL"), "score": 8 if confirms else (-8 if opposite else 4)}
        return {"context": "UNKNOWN", "score": -8}

    def _m1_trigger(self, df: pd.DataFrame | None, direction: str) -> dict:
        if df is None or len(df) < 20:
            return {"fresh": False, "score": 0, "age": None}
        ema20 = ema(_series(df, "close"), 20)
        latest_trigger_age: int | None = None
        for age, idx in enumerate(range(len(df) - 1, max(-1, len(df) - 15), -1)):
            if idx <= 0:
                continue
            cur = df.iloc[idx]
            prev = df.iloc[idx - 1]
            lookback = df.iloc[max(0, idx - 5) : idx]
            minor_high = _series(lookback, "high").max() if not lookback.empty else float(prev["high"])
            minor_low = _series(lookback, "low").min() if not lookback.empty else float(prev["low"])
            trigger = False
            if direction == "BUY":
                engulf = float(cur["close"]) > float(cur["open"]) and float(prev["close"]) < float(prev["open"]) and float(cur["close"]) > float(prev["open"]) and float(cur["open"]) <= float(prev["close"])
                close_above_minor = float(cur["close"]) > minor_high
                reclaim = float(prev["close"]) < ema20.iloc[idx - 1] and float(cur["close"]) > ema20.iloc[idx]
                lower_wick = min(float(cur["open"]), float(cur["close"])) - float(cur["low"])
                body = abs(float(cur["close"]) - float(cur["open"]))
                rejection = float(cur["close"]) > float(cur["open"]) and body > 0 and lower_wick >= body * 1.5
                trigger = engulf or close_above_minor or reclaim or rejection
            elif direction == "SELL":
                engulf = float(cur["close"]) < float(cur["open"]) and float(prev["close"]) > float(prev["open"]) and float(cur["close"]) < float(prev["open"]) and float(cur["open"]) >= float(prev["close"])
                close_below_minor = float(cur["close"]) < minor_low
                reject = float(prev["close"]) > ema20.iloc[idx - 1] and float(cur["close"]) < ema20.iloc[idx]
                upper_wick = float(cur["high"]) - max(float(cur["open"]), float(cur["close"]))
                body = abs(float(cur["close"]) - float(cur["open"]))
                rejection = float(cur["close"]) < float(cur["open"]) and body > 0 and upper_wick >= body * 1.5
                trigger = engulf or close_below_minor or reject or rejection
            if trigger:
                latest_trigger_age = age
                break
        if latest_trigger_age is None:
            return {"fresh": False, "score": 0, "age": None}
        return {"fresh": latest_trigger_age < 5, "score": 15 if latest_trigger_age < 5 else 8, "age": latest_trigger_age}


def ema(values: pd.Series, period: int) -> pd.Series:
    alpha = 2 / (period + 1)
    return values.astype(float).ewm(alpha=alpha, adjust=False).mean()


def atr14(df: pd.DataFrame | None) -> pd.Series:
    """Wilder RMA, graceful expanding-mean for the leading bars (COEUR_V2
    chantier 2, was SMA/min_periods=1 — see COEUR_V2_REPORT.md)."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    from app.utils.indicators import atr_series_graceful
    frame = pd.DataFrame({"high": _series(df, "high"), "low": _series(df, "low"), "close": _series(df, "close")})
    return atr_series_graceful(frame, period=14)


def swing_points(df: pd.DataFrame | None, length: int = 2) -> dict[str, list[tuple[int, float]]]:
    # A2 (2026-07-17) — implementation vectorisee, equivalente BIT-A-BIT a l'originale
    # (8074 comparaisons differentielles / 0 divergence ; 2277 tests anti-look-ahead).
    # swing_points ne fait AUCUNE arithmetique -> uniquement des comparaisons >/< entre
    # valeurs STOCKEES -> equivalence exacte, aucune tolerance. Semantique preservee :
    # index POSITIONNEL, strict >/<, NaN (centre ou voisin) supprime le swing, valeur =
    # bougie centrale, meme garde d'historique. Original fige dans
    # scratchpad/.../a2_swing_points_vectorization/oracle_swing_points.py (SHA 7ECFF250).
    if df is None or len(df) < length * 2 + 1:
        return {"highs": [], "lows": []}
    import numpy as np
    h = np.asarray(df["high"].to_numpy(), dtype=float)
    lo = np.asarray(df["low"].to_numpy(), dtype=float)
    n = len(df)
    c0, c1 = length, n - length
    centers = np.arange(c0, c1)
    hc = h[c0:c1]
    lc = lo[c0:c1]
    hi_mask = np.ones(centers.shape, dtype=bool)
    lo_mask = np.ones(centers.shape, dtype=bool)
    for i in range(1, length + 1):
        hi_mask &= (hc > h[c0 - i:c1 - i]) & (hc > h[c0 + i:c1 + i])
        lo_mask &= (lc < lo[c0 - i:c1 - i]) & (lc < lo[c0 + i:c1 + i])
    highs = [(int(idx), float(h[idx])) for idx in centers[hi_mask]]
    lows = [(int(idx), float(lo[idx])) for idx in centers[lo_mask]]
    return {"highs": highs, "lows": lows}


def premium_discount_zone(ratio: float | None) -> str:
    if ratio is None or not math.isfinite(ratio):
        return "UNKNOWN"
    if ratio <= 0.382:
        return "DISCOUNT"
    if ratio >= 0.618:
        return "PREMIUM"
    return "MID"


def detect_fvg(df: pd.DataFrame | None) -> list[dict]:
    if df is None or len(df) < 3:
        return []
    atr = atr14(df)
    zones: list[dict] = []
    for idx in range(2, len(df)):
        c1 = df.iloc[idx - 2]
        c3 = df.iloc[idx]
        min_gap = (_safe_value(atr.iloc[idx]) or 0.0) * 0.1
        if float(c1["high"]) < float(c3["low"]):
            low = float(c1["high"])
            high = float(c3["low"])
            if high - low >= min_gap:
                inverted = any(float(row["close"]) < low for _, row in df.iloc[idx + 1 :].iterrows())
                zones.append({"type": "BULLISH_IFVG" if inverted else "BULLISH_FVG", "low": low, "high": high, "index": idx})
        if float(c1["low"]) > float(c3["high"]):
            low = float(c3["high"])
            high = float(c1["low"])
            if high - low >= min_gap:
                inverted = any(float(row["close"]) > high for _, row in df.iloc[idx + 1 :].iterrows())
                zones.append({"type": "BEARISH_IFVG" if inverted else "BEARISH_FVG", "low": low, "high": high, "index": idx})
    return zones


def detect_order_block(df: pd.DataFrame | None, direction: str) -> dict | None:
    if df is None or len(df) < 5:
        return None
    atr = _safe_last(atr14(df)) or 0.0
    if atr <= 0:
        return None
    for idx in range(len(df) - 2, 1, -1):
        candle = df.iloc[idx - 1]
        after = df.iloc[idx]
        if direction == "BUY" and float(candle["close"]) < float(candle["open"]):
            if float(after["close"]) - float(candle["high"]) >= atr * 0.8:
                return {"type": "BULLISH_OB", "low": float(candle["low"]), "high": float(candle["high"]), "index": idx - 1}
        if direction == "SELL" and float(candle["close"]) > float(candle["open"]):
            if float(candle["low"]) - float(after["close"]) >= atr * 0.8:
                return {"type": "BEARISH_OB", "low": float(candle["low"]), "high": float(candle["high"]), "index": idx - 1}
    return None


def validate_rr(direction: str, entry: float | None, sl: float | None, tp: float | None) -> dict:
    if direction not in {"BUY", "SELL"} or entry is None or sl is None or tp is None:
        return {"valid": False, "rr": None, "score": -20}
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = reward / risk if risk > 0 else math.inf
    valid = risk > 0 and reward > 0 and math.isfinite(rr)
    if direction == "BUY":
        valid = valid and sl < entry < tp
    if direction == "SELL":
        valid = valid and tp < entry < sl
    if not valid:
        return {"valid": False, "rr": rr if math.isfinite(rr) else None, "score": -20}
    score = 10 if rr >= 2.0 else (6 if rr >= 1.5 else 0)
    return {"valid": True, "rr": rr, "score": score}


def spread_score(spread_points: float | None, max_spread_points: float | None) -> dict:
    if spread_points is None or max_spread_points is None or max_spread_points <= 0:
        return {"ok": False, "ratio": None, "score": -20}
    ratio = spread_points / max_spread_points
    if ratio <= 0.5:
        return {"ok": True, "ratio": ratio, "score": 5}
    if ratio <= 1.0:
        return {"ok": True, "ratio": ratio, "score": 2}
    return {"ok": False, "ratio": ratio, "score": -20}


def _confirmed_swings(df: pd.DataFrame | None) -> dict[str, list[tuple[int, float]]]:
    return swing_points(df, length=2)


def _to_frame(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        return value.copy()
    try:
        return pd.DataFrame(value)
    except ValueError:
        return pd.DataFrame()


def _series(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(df[column], errors="coerce").astype(float)


def _safe_last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    return _safe_value(series.iloc[-1])


def _safe_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_float(value: Any) -> float | None:
    return _safe_value(value)


def _last_close(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty or "close" not in df:
        return None
    return _safe_value(df.iloc[-1]["close"])


def _near_zone(price: float, zone_low: float, zone_high: float, atr: float) -> bool:
    if atr <= 0:
        return zone_low <= price <= zone_high
    return zone_low - atr * 0.35 <= price <= zone_high + atr * 0.35


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _timeframe_narrative(
    symbol: str,
    d1_bias: str,
    h4_bias: str,
    h1_bias: str,
    h1_structure: str,
    m15: bool,
    m5_context: str,
    m1: bool,
    bos: bool,
    sweep: bool,
    spread_ok: bool,
    smc_score: float | None,
) -> str:
    parts = [
        f"{symbol} top-down read: D1 is {d1_bias.lower()}, H4 is {h4_bias.lower()}, H1 is {h1_bias.lower()} with {h1_structure.lower()} structure."
    ]
    if m15:
        parts.append("M15 confirms direction.")
    else:
        parts.append("M15 confirmation is missing.")
    parts.append(f"M5 context is {m5_context.lower()}.")
    missing = []
    if h4_bias == "RANGE":
        missing.append("H4 direction is range")
    if h1_structure in {"RANGE", "UNKNOWN"}:
        missing.append("H1 internal structure is not decisive")
    if smc_score is not None and smc_score < 35:
        missing.append("SMC score is low")
    if not sweep:
        missing.append("liquidity sweep is missing")
    if not bos:
        missing.append("BOS/CHoCH is missing")
    if not m1:
        missing.append("M1 trigger is missing")
    if not spread_ok:
        missing.append("spread is not confirmed")
    if missing:
        parts.append(", ".join(missing) + ". Wait for stronger confirmation.")
    else:
        parts.append("H4/H1/M15/M1 are aligned with a fresh trigger.")
    return " ".join(parts)


def _narrative(symbol: str, h4_bias: str, h1_bias: str, price_location: str, m15: bool, m1: bool, bos: bool, ob_fvg: bool, spread_ok: bool, smc_score: float | None) -> str:
    parts = [f"{symbol} is at {price_location.lower()} with H4 {h4_bias.lower()} and H1 {h1_bias.lower()}."]
    if m15:
        parts.append("M15 has confirmation.")
    else:
        parts.append("M15 confirmation is missing.")
    missing = []
    if h4_bias == "RANGE":
        missing.append("H4 direction is unclear")
    if h1_bias == "RANGE":
        missing.append("H1 direction is unclear")
    if smc_score is not None and smc_score < 35:
        missing.append("SMC score is low")
    if not bos:
        missing.append("BOS/CHoCH is missing")
    if not ob_fvg:
        missing.append("OB/FVG is missing")
    if not m1:
        missing.append("M1 entry is missing")
    if not spread_ok:
        missing.append("spread is not confirmed")
    if missing:
        parts.append(", ".join(missing) + ". Wait for stronger confirmation.")
    return " ".join(parts)
