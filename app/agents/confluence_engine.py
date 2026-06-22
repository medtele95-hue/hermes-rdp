"""Unified confluence scoring engine (Phase 4).

Combines geometric structure, SMC tags, MTFA tags, and order-flow
signals into a single 0–100 confluence score with letter grades.

Rules:
- Does NOT execute trades.
- Only enriches candidates.
- SafetyGuard / DemoRouter remain the final execution gates.
- ORDER_FLOW_READER is bonus/warning only — never hard-blocks.
- Spread, session, and safety are external gates tracked but NOT applied here.
"""
from __future__ import annotations

import pandas as pd

from app.agents.confirmation_matrix import (
    mtfa_calibrated_status as _mtfa_cal,
    mtfa_confluence_adjustment as _mtfa_adj,
    smc_calibrated_status as _smc_cal,
    smc_confluence_adjustment as _smc_adj,
)
from app.logger import log
from app.quant.geometry_engine import (
    _atr_last,
    breakout_box,
    fibonacci_ote_zone,
    geometric_confluence_score,
    impulse_score,
    linear_regression_channel,
    premium_discount_zone,
    range_compression_score,
    swing_high_low,
    triangle_or_wedge_detection,
    volatility_expansion_score,
)

_GRADE_THRESHOLDS = [
    (85, "A+"),
    (75, "A"),
    (65, "B"),
    (50, "C"),
]

_BTC_SYMBOLS = frozenset({"BTCUSD#", "BTCUSD"})

# §4.4 — strategies that run on order-flow signals, not SMC/MTFA structure
_ORDER_FLOW_NATIVE = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_ORDER_FLOW_CVD_VWAP",
})

# Strategies that require full SMC structural alignment
_SMC_NATIVE = frozenset({
    "SIMO_ATM_BREAKOUT",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
})


def _grade(score: float) -> str:
    for threshold, letter in _GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "D"


class ConfluenceEngine:
    """Compute a unified confluence score for a symbol/strategy pair.

    Usage::

        engine = ConfluenceEngine()
        result = engine.evaluate("GOLD", "GOLD_LIQUIDITY_HUNTER_PRO", frames, context)
        # result["score"] -> 0-100
        # result["grade"] -> "A+" / "A" / "B" / "C" / "D"
    """

    def evaluate(
        self,
        symbol: str,
        strategy: str,
        frames: dict[str, pd.DataFrame] | None,
        context: dict | None = None,
        strategy_aware: bool = False,
    ) -> dict:
        """Evaluate geometric + contextual confluence.

        Args:
            symbol:   instrument symbol (e.g. "GOLD", "BTCUSD")
            strategy: strategy name (e.g. "GOLD_LIQUIDITY_HUNTER_PRO")
            frames:   dict of timeframe -> OHLC DataFrame
            context:  optional dict with smc_confluence_score, mtfa_score,
                      order_flow_reader, tick_price, entry, etc.

        Returns a dict with keys:
            score, grade, components, recommendation,
            dominant_pattern, trend_direction, ote_zone, pd_zone,
            swing_high, swing_low, atr
        """
        ctx = dict(context or {})
        rates = _best_frame(frames)

        atr_val = _atr_last(rates) if (rates is not None and not rates.empty) else None
        current_price = _current_price(rates, ctx)

        # --- Geometry ---
        if rates is not None and not rates.empty:
            swing = swing_high_low(rates, lookback=10)
            channel = linear_regression_channel(rates, lookback=20)
            box = breakout_box(rates, lookback=20)
            wedge = triangle_or_wedge_detection(rates)
            comp = range_compression_score(rates, atr_val)
            imp = impulse_score(rates, atr_val)
            vol = volatility_expansion_score(rates, atr_val)
        else:
            swing = channel = box = wedge = {}
            comp = imp = vol = 0.0

        sw_high = swing.get("swing_high") if isinstance(swing, dict) else None
        sw_low = swing.get("swing_low") if isinstance(swing, dict) else None

        ote = fibonacci_ote_zone(sw_high, sw_low) if (sw_high is not None and sw_low is not None) else {}
        pd_zone = (
            premium_discount_zone(sw_high, sw_low, current_price)
            if (sw_high is not None and sw_low is not None and current_price is not None)
            else {}
        )

        geo = geometric_confluence_score(
            swing=swing,
            channel=channel,
            box=box,
            ote=ote,
            pd_zone=pd_zone,
            wedge=wedge,
            compression=comp,
            impulse=imp,
            volatility=vol,
            current_price=current_price,
        )
        geo_score = geo.get("score", 0.0)

        # --- SMC contribution: PASS +10, SOFT_FAIL -5, STRONG_FAIL -15 ---
        _smc_ctx = ctx.get("smc_confluence") or {}
        smc_raw = float(ctx.get("smc_confluence_score") or _smc_ctx.get("smc_confluence_score") or 0.0)
        smc_cal_status = str(ctx.get("smc_calibrated_status") or _smc_ctx.get("smc_calibrated_status") or _smc_cal(smc_raw))
        smc_contrib = _smc_adj(smc_cal_status)

        # §4.5 SMC arbitrator: OF score >= 90 + SOFT_FAIL → halve penalty (-2.5 instead of -5)
        if smc_cal_status == "SOFT_FAIL" and smc_contrib < 0.0:
            _arb_score = float((ctx.get("order_flow_reader") or {}).get("score") or 0.0)
            if _arb_score >= 90.0:
                log.info(
                    "[SMC_ARBITRATOR] symbol=%s smc_penalty=%.1f→%.1f of_score=%.1f",
                    symbol, smc_contrib, smc_contrib / 2, _arb_score,
                )
                smc_contrib /= 2

        # --- MTFA contribution: PASS +10, SOFT_FAIL -5, STRONG_FAIL -15 ---
        _mtfa_ctx = ctx.get("mtfa") or {}
        mtfa_raw = float(ctx.get("mtfa_score") or _mtfa_ctx.get("mtfa_score") or 0.0)
        mtfa_cal_status = str(ctx.get("mtfa_calibrated_status") or _mtfa_ctx.get("mtfa_calibrated_status") or _mtfa_cal(mtfa_raw))
        mtfa_contrib = _mtfa_adj(mtfa_cal_status)

        # --- BTC RANGE mode: reduce penalties when SMC+MTFA are neutral, not STRONG_FAIL ---
        _btc_sym = str(symbol or "").upper() in _BTC_SYMBOLS
        h4_dir = str(ctx.get("smc_h4_direction") or _smc_ctx.get("smc_h4_direction") or "").upper()
        h1_trend = str(ctx.get("smc_h1_trend") or _smc_ctx.get("smc_h1_trend") or "").upper()
        h1_bias = str(ctx.get("h1_bias") or _mtfa_ctx.get("h1_bias") or "").upper()
        if _btc_sym and h4_dir == "RANGE" and h1_trend == "RANGE" and smc_contrib < -5.0:
            log.info(
                "[CONFLUENCE_BTC_RANGE_MODE] symbol=%s smc_contrib=%.1f→-5 h4=%s h1=%s",
                symbol, smc_contrib, h4_dir, h1_trend,
            )
            smc_contrib = -5.0
        if _btc_sym and h1_bias == "NEUTRAL" and mtfa_contrib < -5.0:
            log.info(
                "[CONFLUENCE_BTC_RANGE_MODE] symbol=%s mtfa_contrib=%.1f→-5 h1_bias=%s",
                symbol, mtfa_contrib, h1_bias,
            )
            mtfa_contrib = -5.0

        # --- §4.4 Strategy-aware confluence: ORDER_FLOW_NATIVE never penalized by SMC/MTFA ---
        # Gated by strategy_aware=True (controlled by hermes_confluence_strategy_aware setting).
        _of_native = str(strategy or "").upper() in _ORDER_FLOW_NATIVE
        if strategy_aware and _of_native:
            if smc_contrib < 0.0 or mtfa_contrib < 0.0:
                log.info(
                    "[CONFLUENCE_OF_NATIVE_CLAMP] strategy=%s smc=%.1f→%.1f mtfa=%.1f→%.1f",
                    strategy, smc_contrib, max(0.0, smc_contrib), mtfa_contrib, max(0.0, mtfa_contrib),
                )
            smc_contrib = max(0.0, smc_contrib)
            mtfa_contrib = max(0.0, mtfa_contrib)
            of_bonus = _order_flow_bonus_graded(ctx)
        else:
            # --- Order-flow: bonus/warning only, never hard-block (-5 to +5) ---
            of_bonus = _order_flow_bonus(ctx)

        raw_score = geo_score + smc_contrib + mtfa_contrib + of_bonus
        final_score = float(max(0.0, min(100.0, round(raw_score, 2))))
        grade = _grade(final_score)

        components = {
            **geo.get("components", {}),
            "smc": round(smc_contrib, 2),
            "mtfa": round(mtfa_contrib, 2),
            "order_flow_bonus": round(of_bonus, 2),
        }

        recommendation = "TRADE" if final_score >= 65 else ("WATCH" if final_score >= 50 else "WAIT")

        log.info(
            "[CONFLUENCE] symbol=%s strategy=%s score=%s grade=%s components=%s",
            symbol,
            strategy,
            final_score,
            grade,
            ",".join(f"{k}={v}" for k, v in components.items()),
        )

        _smc_native = str(strategy or "").upper() in _SMC_NATIVE
        _strategy_class = (
            "ORDER_FLOW_NATIVE" if _of_native
            else ("SMC_NATIVE" if _smc_native else "DEFAULT")
        )
        return {
            "symbol": symbol,
            "strategy": strategy,
            "strategy_class": _strategy_class,
            "score": final_score,
            "grade": grade,
            "components": components,
            "recommendation": recommendation,
            "dominant_pattern": geo.get("dominant_pattern", "NONE"),
            "trend_direction": (channel or {}).get("direction", "FLAT"),
            "ote_zone": ote,
            "pd_zone": pd_zone,
            "swing_high": sw_high,
            "swing_low": sw_low,
            "atr": atr_val,
        }


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

_engine = ConfluenceEngine()


def evaluate_confluence(
    symbol: str,
    strategy: str,
    frames: dict | None,
    context: dict | None = None,
    strategy_aware: bool = False,
) -> dict:
    """Module-level convenience wrapper around ConfluenceEngine.evaluate."""
    return _engine.evaluate(symbol, strategy, frames, context, strategy_aware)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _best_frame(frames: dict | None) -> pd.DataFrame | None:
    """Return the best available OHLC DataFrame from the frames dict."""
    if not frames:
        return None
    for tf in ("M5", "M15", "M1", "H1"):
        df = frames.get(tf)
        if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
            return df
    return None


def _current_price(rates: pd.DataFrame | None, ctx: dict) -> float | None:
    """Extract current price from context or the last close of rates."""
    for key in ("tick_price", "entry", "current_price"):
        val = ctx.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    if rates is not None and not rates.empty and "close" in rates.columns:
        return float(rates["close"].iloc[-1])
    return None


def _order_flow_bonus(ctx: dict) -> float:
    """Extract order-flow signal as a small bonus/warning.

    Returns a value in [-5, +20]. Never hard-blocks execution.
    BTC ORDER_FLOW_EXECUTION_AGENT bonus takes priority when present.
    """
    btc_bonus = ctx.get("btc_order_flow_bonus")
    if btc_bonus is not None:
        val = float(btc_bonus)
        log.debug("[CONFLUENCE_BTC_OF_BONUS] btc_order_flow_bonus=%.2f", val)
        return val

    of_data = (
        ctx.get("order_flow_reader")
        or ctx.get("gold_order_flow_cvd_vwap")
        or {}
    )
    if not isinstance(of_data, dict):
        return 0.0
    score = float(
        of_data.get("score")
        or of_data.get("cvd_score")
        or of_data.get("confidence")
        or 0.0
    )
    signal = str(of_data.get("signal") or of_data.get("decision") or "").upper()
    if signal in {"BUY", "SELL"} and score >= 60.0:
        return 5.0
    if signal in {"BUY", "SELL"} and score < 30.0:
        return -5.0
    return 0.0


def _order_flow_bonus_graded(ctx: dict) -> float:
    """Grade-based bonus for ORDER_FLOW_NATIVE strategies: A→+15, B→+8, else 0.

    Used instead of _order_flow_bonus when strategy is ORDER_FLOW_NATIVE.
    Never returns negative (SMC/MTFA clamping already handles downside).
    """
    btc_bonus = ctx.get("btc_order_flow_bonus")
    if btc_bonus is not None:
        return max(0.0, float(btc_bonus))

    of_data = (
        ctx.get("order_flow_reader")
        or ctx.get("gold_order_flow_cvd_vwap")
        or {}
    )
    if not isinstance(of_data, dict):
        return 0.0
    grade = str(of_data.get("grade") or of_data.get("of_grade") or "").upper()
    score = float(
        of_data.get("score")
        or of_data.get("cvd_score")
        or of_data.get("confidence")
        or 0.0
    )
    if grade == "A" or grade == "A+" or (not grade and score >= 85.0):
        return 15.0
    if grade == "B" or (not grade and score >= 60.0):
        return 8.0
    return 0.0
