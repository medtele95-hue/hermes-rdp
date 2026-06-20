from __future__ import annotations

import time
from typing import Dict, List

import pandas as pd

from app.logger import log
from app.agents.execution_agent import ExecutionAgent
from app.agents.kelly_risk_agent import KellyRiskAgent
from app.agents.markov_state_agent import MarkovStateAgent
from app.agents.mtfa_filter import MTFAFilter
from app.agents.mtf_structure_detector import MTFStructureDetector
from app.agents.smc_confluence_tagger import SMCConfluenceTagger
from app.agents.self_learning_agent import SelfLearningAgent
from app.config import Settings
from app.agents.strategies import amd_fvg_ifvg_reversal, crt_tbs_reversal, fib_ote_retest, quant_pro_regime_switching, quant_statistical_pullback, trend_continuation_breakdown
from app.quant.geometry_engine import impulse_score as _impulse_score, swing_high_low as _swing_high_low
from app.quant.volume_engine import evaluate_b8_context as _evaluate_b8_context
from app.quant.absorption_detector import detect_absorption_context as _detect_absorption_context
from app.services import eur_ema_rsi_atr_strategy, gold_liquidity_hunter_strategy, gold_m1m5_ema_sweep_scalper_strategy
from app.services.wsp_intelligence_overlay import evaluate_wsp_intelligence
from app.services.top_down_market_reader import TopDownMarketReader
from app.strategies import breakout_retest, ema_pullback, fib_confluence_agent, gold_order_flow_cvd_vwap, order_flow_execution_agent, scalping, second_entry, simo_atm_breakout
from app.mt5.btc_market_narrator import BtcNarratorInput

_BTC_SYMBOLS: frozenset[str] = frozenset({"BTCUSD#", "BTCUSD"})
_BTC_AGENT_STRATEGIES: frozenset[str] = frozenset({"BTC_SCALPING_AGENT", "ORDER_FLOW_EXECUTION_AGENT"})


OPTIONAL_DASHBOARD_MODULE_PLACEHOLDERS = {
    "acceleration_bands_htf": {
        "enabled": False,
        "status": "OPTIONAL_NOT_ENABLED",
        "signal": "UNKNOWN",
        "score": None,
        "reason": "Acceleration Bands HTF module not enabled or no payload yet",
    },
    "volume_profile": {
        "enabled": False,
        "status": "OPTIONAL_NOT_ENABLED",
        "poc": None,
        "vah": None,
        "val": None,
        "score": None,
        "reason": "Volume Profile module not enabled or no payload yet",
    },
}


class Hermes5MinAgent:
    def __init__(self, settings: Settings, learning_optimizer=None) -> None:
        self.settings = settings
        self.markov = MarkovStateAgent()
        self.kelly = KellyRiskAgent(settings)
        self.learning = SelfLearningAgent()
        self.execution = ExecutionAgent(settings)
        self.mtfa = MTFAFilter(settings)
        self.mtf_structure = MTFStructureDetector(settings)
        self.smc_tagger = SMCConfluenceTagger(settings)
        self.top_down_reader = TopDownMarketReader()
        self.learning_optimizer = learning_optimizer
        self._top_down_logged_symbols: set[str] = set()

    def analyze_symbol(
        self,
        symbol: str,
        frames: Dict[str, object],
        account: dict | None,
        open_hermes_trades: int,
        symbol_specs: dict | None = None,
        max_spread: float | None = None,
        strategy_context: dict | None = None,
    ) -> dict:
        self._top_down_logged_symbols = set()
        m5 = frames.get("M5")
        market_state, markov_prediction = self.markov.analyze(symbol, "M5", m5)
        base_context = _frame_context(frames)
        base_context.update(strategy_context or {})
        base_context["market_state"] = market_state.get("state")
        order_flow_snapshot = gold_order_flow_cvd_vwap.evaluate_reader(symbol, frames, base_context, self.settings)
        base_context["order_flow_snapshot"] = order_flow_snapshot
        signals: List[dict] = [
            simo_atm_breakout.evaluate(symbol, frames, base_context, self.settings),
            _active_signal(breakout_retest.evaluate(symbol, m5)),
            trend_continuation_breakdown.evaluate(symbol, frames, base_context, self.settings),
            crt_tbs_reversal.evaluate(symbol, frames, base_context, self.settings),
            amd_fvg_ifvg_reversal.evaluate(symbol, frames, base_context, self.settings),
            fib_ote_retest.evaluate(symbol, frames, base_context, self.settings),
            quant_statistical_pullback.evaluate(symbol, frames, base_context, self.settings),
            quant_pro_regime_switching.evaluate(symbol, frames, base_context, self.settings),
            gold_liquidity_hunter_strategy.evaluate(symbol, frames, base_context, self.settings),
            gold_m1m5_ema_sweep_scalper_strategy.evaluate(symbol, frames, base_context, self.settings),
            gold_order_flow_cvd_vwap.evaluate(symbol, frames, base_context, self.settings),
            order_flow_execution_agent.evaluate(symbol, frames, base_context, self.settings),
            fib_confluence_agent.evaluate(symbol, frames, base_context, self.settings),
            eur_ema_rsi_atr_strategy.evaluate(symbol, frames, base_context, self.settings),
            scalping.evaluate_btc_entry(symbol, m5, frames, self.settings, max_spread),
            _active_signal(ema_pullback.evaluate(symbol, m5)),
            self._legacy_signal(second_entry.evaluate(symbol, m5), "SECOND_ENTRY"),
            self._legacy_signal(scalping.evaluate(symbol, m5), "SCALPING_AGENT"),
        ]
        latest_spread = float(m5.iloc[-1]["spread"]) if not m5.empty else 0.0
        signals = [
            self._with_top_down(symbol, frames, signal, latest_spread, max_spread)
            for signal in signals
            if isinstance(signal, dict)
        ]
        if self.learning_optimizer is not None:
            signals = self.learning_optimizer.apply_to_signals(symbol, signals)
        chosen = self.learning.choose_strategy(signals, markov_prediction)
        risk = self.kelly.evaluate(
            chosen,
            markov_prediction,
            account,
            open_hermes_trades,
            latest_spread,
            symbol_specs or {},
            max_spread,
        )
        decision = self.execution.decision(market_state, markov_prediction, chosen, risk)
        for field in [
            "strategy_status",
            "crt_tbs_score",
            "crt_tbs_bias",
            "crt_high",
            "crt_low",
            "crt_mid_50",
            "crt_price_zone",
            "crt_tbs_reason",
            "amd_fvg_score",
            "amd_phase_detected",
            "manipulation_detected",
            "displacement_detected",
            "fvg_ifvg_context",
            "amd_fvg_reason",
            "fib_ote_score",
            "fib_ote_zone",
            "fib_ote_bias",
            "fib_ote_reason",
            "fib_ote_618",
            "fib_ote_786",
            "trend_continuation_score",
            "trend_continuation_direction",
            "trend_continuation_reason",
            "support_break",
            "resistance_break",
            "trend_continuation_retest",
            "trend_continuation_momentum",
            "m5_structure_status",
            "quant_slope",
            "quant_r2",
            "quant_z_score",
            "quant_mean",
            "quant_stdev",
            "quant_signal",
            "quant_score",
            "quant_reason",
            "quant_pro_regime",
            "quant_pro_score",
            "quant_pro_grade",
            "quant_pro_ols_slope",
            "quant_pro_ols_r2",
            "quant_pro_ols_tstat",
            "quant_pro_kalman_velocity",
            "quant_pro_kalman_z",
            "quant_pro_ou_beta",
            "quant_pro_ou_tstat",
            "quant_pro_ou_half_life",
            "quant_pro_hurst",
            "quant_pro_hurst_filter",
            "quant_pro_min_trend_hurst",
            "quant_pro_trend_strength",
            "quant_pro_hurst_filter_status",
            "quant_pro_hurst_block_reason",
            "quant_pro_ewma_vol",
            "quant_pro_signal",
            "quant_pro_reason",
            "quant_pro_no_lookahead",
            "gold_liquidity_hunter",
            "gold_liquidity_signal",
            "gold_liquidity_score",
            "gold_liquidity_reason",
            "gold_m1m5_scalper",
            "gold_m1m5_scalper_decision",
            "gold_m1m5_scalper_score",
            "gold_m1m5_scalper_reason",
            "gold_order_flow_cvd_vwap",
            "gold_order_flow_signal",
            "gold_order_flow_score",
            "gold_order_flow_reason",
            "eur_ema_rsi_atr",
            "eur_ema_rsi_atr_decision",
            "eur_ema_rsi_atr_reason",
            "btc_scalping_agent",
            "btc_scalping_decision",
            "btc_scalping_reason",
            "btc_scalping_trigger_type",
            "btc_scalping_confidence",
            "simo_atm_breakout",
            "simo_atm_signal",
            "simo_atm_score",
            "simo_atm_reason",
            "relaxed_mode_active",
            "relaxed_reason",
            "hours_without_setup",
            "strict_threshold",
            "relaxed_threshold",
            "relaxed_trade_count_today",
            "last_relaxed_trade_result",
        ]:
            if field in chosen:
                decision[field] = chosen.get(field)
        mtfa = self.mtfa.evaluate(symbol, frames, decision.get("signal"))
        mtf_structure = self.mtf_structure.evaluate(symbol, frames, decision.get("signal"))
        smc_confluence = self.smc_tagger.evaluate(symbol, frames, decision.get("signal"))
        if not isinstance(decision.get("top_down_reader"), dict):
            decision = self._with_top_down(symbol, frames, decision, latest_spread, max_spread)
        top_down_context = decision.get("top_down_reader") if isinstance(decision.get("top_down_reader"), dict) else {}
        wsp = evaluate_wsp_intelligence(symbol, str(decision.get("timeframe") or "M5"), frames.get("M5"), top_down_context)
        decision.update(mtfa)
        decision.update(mtf_structure)
        decision.update(smc_confluence)
        decision["wsp_intelligence"] = wsp

        # §4.0 B4/B5/B8/B9: pre-compute quant context so tier evaluator can use
        # it from the merged payload without needing frame access.
        if m5 is not None and not m5.empty:
            try:
                decision["impulse_score_m5"] = float(_impulse_score(m5))
            except Exception:
                pass
            try:
                decision["volume_engine"] = _evaluate_b8_context(m5)
            except Exception:
                pass
            try:
                decision["cvd_absorption"] = _detect_absorption_context(
                    m5, base_context.get("order_flow_snapshot") or {}
                )
            except Exception:
                pass
            # B5: swing range for exact A10 premium/discount check
            try:
                _swings = _swing_high_low(m5, lookback=20)
                if _swings.get("swing_high") and _swings.get("swing_low"):
                    decision["pd_swing_high"] = float(_swings["swing_high"])
                    decision["pd_swing_low"] = float(_swings["swing_low"])
            except Exception:
                pass

        # ── BTC narrative intelligence — build narrator_input for BTC strategies ──
        _btc_strat = str(chosen.get("strategy") or "").upper()
        if symbol.upper() in _BTC_SYMBOLS and _btc_strat in _BTC_AGENT_STRATEGIES:
            _btc_dir = str(chosen.get("signal") or "").upper()
            if _btc_dir in {"BUY", "SELL"}:
                _narrator_input = _build_narrator_input_from_context(
                    symbol=symbol,
                    direction=_btc_dir,
                    frames=frames,
                    smc_confluence=smc_confluence,
                    mtfa=mtfa,
                    base_context=base_context,
                )
                if _narrator_input is not None:
                    decision["btc_narrator_input"] = _narrator_input
                    log.info(
                        "[BTC_NARRATOR_READY] symbol=%s strategy=%s direction=%s "
                        "smc_direction=%s mtfa_bias=%s coherence_inputs_available=True",
                        symbol, _btc_strat, _btc_dir,
                        _narrator_input.smc_direction,
                        _narrator_input.mtfa_bias,
                    )
        top_down_payload = _top_down_payload(top_down_context)
        decision.update(top_down_payload)
        quant_payload = {
            key: decision.get(key)
            for key in (
                "quant_slope",
                "quant_r2",
                "quant_z_score",
                "quant_mean",
                "quant_stdev",
                "quant_signal",
                "quant_score",
                "quant_reason",
                "quant_pro_regime",
                "quant_pro_score",
                "quant_pro_grade",
                "quant_pro_ols_slope",
                "quant_pro_ols_r2",
                "quant_pro_ols_tstat",
                "quant_pro_kalman_velocity",
                "quant_pro_kalman_z",
                "quant_pro_ou_beta",
                "quant_pro_ou_tstat",
                "quant_pro_ou_half_life",
                "quant_pro_hurst",
                "quant_pro_hurst_filter",
                "quant_pro_min_trend_hurst",
                "quant_pro_trend_strength",
                "quant_pro_hurst_filter_status",
                "quant_pro_hurst_block_reason",
                "quant_pro_ewma_vol",
                "quant_pro_signal",
                "quant_pro_reason",
                "quant_pro_no_lookahead",
                "gold_liquidity_hunter",
                "gold_liquidity_signal",
                "gold_liquidity_score",
                "gold_liquidity_reason",
                "gold_m1m5_scalper",
                "gold_m1m5_scalper_decision",
                "gold_m1m5_scalper_score",
                "gold_m1m5_scalper_reason",
                "gold_order_flow_cvd_vwap",
                "gold_order_flow_signal",
                "gold_order_flow_score",
                "gold_order_flow_reason",
                "eur_ema_rsi_atr",
                "eur_ema_rsi_atr_decision",
                "eur_ema_rsi_atr_reason",
                "simo_atm_breakout",
                "simo_atm_signal",
                "simo_atm_score",
                "simo_atm_reason",
                "relaxed_mode_active",
                "relaxed_reason",
                "hours_without_setup",
                "strict_threshold",
                "relaxed_threshold",
                "relaxed_trade_count_today",
                "last_relaxed_trade_result",
            )
            if decision.get(key) is not None
        }
        decision["raw_payload"] = {
            "mtfa": mtfa,
            "mtf_structure": mtf_structure,
            "smc_confluence": smc_confluence,
            "wsp_intelligence": wsp,
            "top_down_reader": top_down_context,
            **_optional_dashboard_module_placeholders(),
            **top_down_payload,
            **quant_payload,
        }
        return {
            "market_state": market_state,
            "markov_prediction": markov_prediction,
            "strategy_signals": signals,
            "chosen_strategy": chosen,
            "kelly_risk": risk,
            "mtfa": mtfa,
            "mtf_structure": mtf_structure,
            "smc_confluence": smc_confluence,
            "wsp_intelligence": wsp,
            "ai_decision": decision,
            "order_flow_snapshots": [order_flow_snapshot] if isinstance(order_flow_snapshot, dict) else [],
        }

    def _with_top_down(
        self,
        symbol: str,
        frames: Dict[str, object],
        payload: dict,
        spread: float,
        max_spread: float | None,
    ) -> dict:
        out = dict(payload)
        direction = str(out.get("signal") or out.get("resolved_direction") or out.get("direction") or "").upper()
        smc_score = out.get("smc_confluence_score") or out.get("smc_score")
        mtfa_score = out.get("mtfa_score")
        if direction not in {"BUY", "SELL"}:
            top_down = TopDownMarketReader.unavailable(symbol, str(out.get("timeframe") or "M5"), "NO_TRADE_DIRECTION")
        else:
            try:
                top_down = self.top_down_reader.evaluate(
                    symbol,
                    frames,
                    direction,
                    out.get("entry"),
                    out.get("sl"),
                    out.get("tp"),
                    spread,
                    max_spread,
                    None,
                    smc_score,
                    str(out.get("timeframe") or "M5"),
                )
            except Exception as exc:
                missing_str = "TOP_DOWN_SNAPSHOT_NOT_READY"
                log.warning(
                    "[TOP_DOWN_DATA_MISSING] symbol=%s missing=%s error=%s",
                    symbol, missing_str, str(exc)[:120],
                )
                top_down = TopDownMarketReader.unavailable(symbol, str(out.get("timeframe") or "M5"), missing_str)
                top_down["missing_confirmations"] = [missing_str]
                top_down["error"] = str(exc)
        top_down.setdefault("top_down_decision", top_down.get("decision"))
        td_missing = top_down.get("missing_confirmations") or []
        has_top_down = bool(top_down) and top_down.get("reason") not in {
            "TOP_DOWN_DATA_MISSING", "TOP_DOWN_SNAPSHOT_NOT_READY", "NO_TRADE_DIRECTION"
        } and td_missing == []
        log.info(
            "[ROUTER_CONTEXT] symbol=%s strategy=%s direction=%s has_top_down=%s smc_score=%s mtfa_score=%s td_status=%s",
            symbol,
            out.get("strategy"),
            direction,
            str(has_top_down).lower(),
            smc_score,
            mtfa_score,
            top_down.get("top_down_status"),
        )
        if symbol not in self._top_down_logged_symbols:
            self._top_down_logged_symbols.add(symbol)
            score = top_down.get("entry_readiness_score")
            try:
                score_value = float(score)
            except (TypeError, ValueError):
                score_value = None
            if score_value == 0 and td_missing:
                log.warning(
                    "[TOP_DOWN_DATA_MISSING] symbol=%s missing=%s",
                    symbol,
                    ",".join(str(m) for m in td_missing),
                )
            else:
                log.info(
                    "[TOP_DOWN_READER] symbol=%s status=%s decision=%s score=%s",
                    symbol,
                    top_down.get("top_down_status"),
                    top_down.get("top_down_decision") or top_down.get("decision"),
                    score,
                )
        out["top_down_reader"] = top_down
        out.update(_top_down_payload(top_down))
        raw_payload = out.get("raw_payload") if isinstance(out.get("raw_payload"), dict) else {}
        out["raw_payload"] = {**raw_payload, "top_down_reader": top_down, **_top_down_payload(top_down)}
        return out

    def _legacy_signal(self, signal: dict, strategy: str) -> dict:
        enabled = self.settings.second_entry_enabled if strategy == "SECOND_ENTRY" else self.settings.scalping_agent_enabled
        observer = self.settings.second_entry_legacy_observer if strategy == "SECOND_ENTRY" else self.settings.scalping_agent_legacy_observer
        if enabled:
            return _active_signal(signal)
        out = dict(signal)
        out["signal"] = "WAIT"
        out["entry"] = None
        out["sl"] = None
        out["tp"] = None
        out["strategy_status"] = "LEGACY_OBSERVER" if observer else "DISABLED"
        out["reason"] = f"{strategy}_DISABLED_LEGACY_OBSERVER" if observer else f"{strategy}_DISABLED"
        out["blocked_reason"] = out["reason"]
        return out


def _active_signal(signal: dict) -> dict:
    out = dict(signal)
    out.setdefault("strategy_status", "ACTIVE")
    return out


def _optional_dashboard_module_placeholders() -> dict:
    return {key: dict(value) for key, value in OPTIONAL_DASHBOARD_MODULE_PLACEHOLDERS.items()}


def _top_down_payload(top_down: dict) -> dict:
    return {
        "top_down_status": top_down.get("top_down_status"),
        "top_down_decision": top_down.get("top_down_decision") or top_down.get("decision"),
        "entry_readiness_score": top_down.get("entry_readiness_score"),
        "market_narrative": top_down.get("market_narrative"),
        "missing_confirmations": top_down.get("missing_confirmations") or top_down.get("timeframe_missing_confirmations") or [],
        "score_breakdown": top_down.get("score_breakdown") or {},
        "d1_macro_bias": top_down.get("d1_macro_bias"),
        "h4_main_bias": top_down.get("h4_main_bias"),
        "h1_internal_structure": top_down.get("h1_internal_structure"),
        "m15_confirmation_status": top_down.get("m15_confirmation_status"),
        "m5_context_status": top_down.get("m5_context_status"),
        "m1_trigger_status": top_down.get("m1_trigger_status"),
    }


def _frame_context(frames: dict) -> dict:
    h4 = frames.get("H4")
    out = {}
    if h4 is not None and not getattr(h4, "empty", True):
        tail = h4.tail(20)
        out["h4_recent_support"] = _float(tail["low"].tail(10).min())
        out["h4_recent_resistance"] = _float(tail["high"].tail(10).max())
        out["h4_last_swing_high"] = _float(tail["high"].tail(7).max())
        out["h4_last_swing_low"] = _float(tail["low"].tail(7).min())
        out["h4_bias"] = "BULLISH" if _float(tail.iloc[-1].get("close")) and _float(tail.iloc[-1].get("close")) > _float(tail["close"].tail(5).mean()) else "RANGE"
    return out


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# BTC Narrative Intelligence — helper (Pièce 4)
# ---------------------------------------------------------------------------

def _build_narrator_input_from_context(
    symbol: str,
    direction: str,
    frames: dict,
    smc_confluence: dict,
    mtfa: dict,
    base_context: dict,
) -> BtcNarratorInput | None:
    """Build BtcNarratorInput from data already computed in the pipeline.

    All inputs come from smc_confluence_tagger, mtfa_filter, order_flow_snapshot,
    and frame data — zero new MT5 calls.  Returns None on any mapping error.
    Only called for BTCUSD# + BTC strategies with a live BUY/SELL direction.
    """
    try:
        # ── SMC fields ────────────────────────────────────────────────────────
        h1_trend = str(smc_confluence.get("smc_h1_trend") or "UNKNOWN").upper()
        smc_direction = (
            "BULLISH" if h1_trend == "BULLISH"
            else "BEARISH" if h1_trend == "BEARISH"
            else "NEUTRAL"
        )
        smc_score_raw = float(smc_confluence.get("smc_confluence_score") or 0.0)
        smc_score = min(1.0, max(0.0, smc_score_raw / 100.0))

        h1_ob = str(smc_confluence.get("smc_h1_order_block") or "NONE").upper()
        smc_order_block_bull = "BULLISH_OB" in h1_ob
        smc_order_block_bear = "BEARISH_OB" in h1_ob

        h1_fvg = str(smc_confluence.get("smc_h1_fvg") or "NONE").upper()
        smc_fvg_bull = "BULLISH_FVG" in h1_fvg
        smc_fvg_bear = "BEARISH_FVG" in h1_fvg

        h1_liq = str(smc_confluence.get("smc_h1_liquidity") or "NONE").upper()
        smc_liquidity_above = h1_liq in {"EQUAL_HIGHS", "BUY_SIDE"}
        smc_liquidity_below = h1_liq in {"EQUAL_LOWS", "SELL_SIDE"}
        smc_inducement = bool(smc_confluence.get("turtle_soup_ote")) or h1_liq in {"BUY_SIDE", "SELL_SIDE"}

        # ── MTFA fields ───────────────────────────────────────────────────────
        h1_bias_raw = str(mtfa.get("h1_bias") or "NEUTRAL").upper()
        mtfa_bias = (
            "BULL" if h1_bias_raw == "BULLISH"
            else "BEAR" if h1_bias_raw == "BEARISH"
            else "NEUTRAL"
        )
        mtfa_h1_direction = (
            "BULLISH" if h1_bias_raw == "BULLISH"
            else "BEARISH" if h1_bias_raw == "BEARISH"
            else "NEUTRAL"
        )
        mtfa_score = float(mtfa.get("mtfa_score") or 0.0)
        mtfa_m15_liquidity_ok = bool(mtfa.get("m15_liquidity"))
        mtfa_cisd_m5 = bool(mtfa.get("m5_cisd"))

        # ── Order flow + CVD ─────────────────────────────────────────────────
        of_snap = base_context.get("order_flow_snapshot") or {}
        cvd_slope_m5 = _safe_float(of_snap.get("cvd_slope") or base_context.get("cvd_slope"))
        delta_last = _safe_float(
            of_snap.get("delta_proxy")
            or of_snap.get("latest_delta")
            or base_context.get("delta_proxy")
        )
        of_signal = str(of_snap.get("signal") or base_context.get("order_flow_signal") or "WAIT").upper()
        if direction == "BUY":
            order_flow_bonus = 5.0 if of_signal in {"BUY", "BULLISH"} else -2.0 if of_signal in {"SELL", "BEARISH"} else 0.0
        else:
            order_flow_bonus = 5.0 if of_signal in {"SELL", "BEARISH"} else -2.0 if of_signal in {"BUY", "BULLISH"} else 0.0

        # ── Confluence grade ──────────────────────────────────────────────────
        confluence_score = float(
            base_context.get("final_confluence_score")
            or base_context.get("confluence_score")
            or 0.0
        )
        confluence_grade = str(
            base_context.get("final_confluence_grade")
            or base_context.get("confluence_grade")
            or "C"
        )

        # ── Session ───────────────────────────────────────────────────────────
        session_name = str(
            base_context.get("session_name")
            or base_context.get("session")
            or of_snap.get("session_name")
            or "OFF"
        ).upper()
        session_quality = _session_quality_from_name(session_name)

        # ── Danger ───────────────────────────────────────────────────────────
        danger_signal_count = int(base_context.get("danger_signal_count") or 0)

        # ── Price zone (from M5 frame or base_context) ────────────────────────
        m5 = frames.get("M5")
        price = _safe_float(base_context.get("price") or base_context.get("bid"))
        vwap = _safe_float(base_context.get("vwap"))
        if price is not None and vwap is not None:
            price_in_premium = price > vwap
            price_in_discount = price < vwap
        else:
            price_in_premium = False
            price_in_discount = False

        # ── M5 EMA stack + ATR ────────────────────────────────────────────────
        m5_ema_stack, m5_last_close_vs_open, atr_m5 = _m5_derived(m5, price, vwap)

        # ── M1 candle body/wick pcts ─────────────────────────────────────────
        m1 = frames.get("M1")
        m1_last_body_pct, m1_upper_wick_pct, m1_lower_wick_pct = _m1_candle_pcts(m1)

        # ── Geometry (impulse, compression) — from base_context or defaults ───
        from app.quant.geometry_engine import impulse_score as _impulse_score, range_compression_score as _rng_comp
        _m5_rates = m5 if (m5 is not None and not getattr(m5, "empty", True)) else None
        if _m5_rates is not None and len(_m5_rates) >= 5:
            _atr_val = atr_m5 if atr_m5 else None
            impulse_score_val = _impulse_score(_m5_rates, _atr_val)
            range_compression = _rng_comp(_m5_rates, _atr_val)
        else:
            impulse_score_val = float(base_context.get("impulse_score") or 0.3)
            range_compression = float(base_context.get("range_compression") or 0.5)

        return BtcNarratorInput(
            smc_direction=smc_direction,
            smc_score=smc_score,
            smc_order_block_bull=smc_order_block_bull,
            smc_order_block_bear=smc_order_block_bear,
            smc_fvg_bull=smc_fvg_bull,
            smc_fvg_bear=smc_fvg_bear,
            smc_liquidity_above=smc_liquidity_above,
            smc_liquidity_below=smc_liquidity_below,
            smc_inducement=smc_inducement,
            mtfa_bias=mtfa_bias,
            mtfa_score=mtfa_score,
            mtfa_h1_direction=mtfa_h1_direction,
            mtfa_m15_liquidity_ok=mtfa_m15_liquidity_ok,
            mtfa_cisd_m5=mtfa_cisd_m5,
            confluence_score=confluence_score,
            confluence_grade=confluence_grade,
            order_flow_bonus=order_flow_bonus,
            danger_signal_count=danger_signal_count,
            price_in_premium=price_in_premium,
            price_in_discount=price_in_discount,
            atr_m5=atr_m5 if atr_m5 else 150.0,
            impulse_score=min(1.0, max(0.0, impulse_score_val)),
            range_compression=min(1.0, max(0.0, range_compression)),
            m1_last_body_pct=m1_last_body_pct,
            m1_upper_wick_pct=m1_upper_wick_pct,
            m1_lower_wick_pct=m1_lower_wick_pct,
            m5_ema_stack=m5_ema_stack,
            m5_last_close_vs_open=m5_last_close_vs_open,
            cvd_slope_m5=cvd_slope_m5,
            delta_last=delta_last,
            dominant_side=None,
            session_name=session_name,
            session_quality=session_quality,
            direction=direction,
            server_time_utc=int(time.time()),
        )
    except Exception as exc:
        log.warning("[BTC_NARRATOR_BUILD] mapping failed symbol=%s error=%s", symbol, exc)
        return None


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        import math
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _session_quality_from_name(session_name: str) -> float:
    s = session_name.upper()
    if s in {"LONDON_NY", "OVERLAP", "LONDON_NEW_YORK"}:
        return 0.95
    if s in {"LONDON", "NEW_YORK", "NEW YORK"}:
        return 0.90
    if s in {"ASIA", "TOKYO", "SYDNEY"}:
        return 0.50
    if s in {"OFF", "OFF_HOURS", "CLOSED"}:
        return 0.20
    return 0.65


def _m5_derived(
    m5: pd.DataFrame | None,
    price: float | None,
    vwap: float | None,
) -> tuple[str, float, float | None]:
    """Return (m5_ema_stack, m5_last_close_vs_open, atr_m5) from M5 data."""
    try:
        if m5 is None or getattr(m5, "empty", True) or len(m5) < 3:
            ema_stack = (
                "BULL" if (price and vwap and price > vwap)
                else "BEAR" if (price and vwap and price < vwap)
                else "MIXED"
            )
            return ema_stack, 0.0, None

        last = m5.iloc[-1]
        close = _float(last.get("close"))
        open_ = _float(last.get("open"))
        close_vs_open = (close - open_) if (close is not None and open_ is not None) else 0.0

        # EMA stack: prefer ema20 column, else fall back to VWAP proxy
        if "ema20" in m5.columns:
            ema20 = _float(last.get("ema20"))
            if close is not None and ema20 is not None:
                ema_stack = "BULL" if close > ema20 else "BEAR"
            elif price and vwap:
                ema_stack = "BULL" if price > vwap else "BEAR"
            else:
                ema_stack = "MIXED"
        elif price and vwap:
            ema_stack = "BULL" if price > vwap else "BEAR"
        else:
            ema_stack = "MIXED"

        # ATR from geometry engine
        from app.quant.geometry_engine import _atr_last
        atr_m5 = _atr_last(m5)
        return ema_stack, float(close_vs_open), atr_m5
    except Exception:
        return "MIXED", 0.0, None


def _m1_candle_pcts(m1: pd.DataFrame | None) -> tuple[float, float, float]:
    """Return (body_pct, upper_wick_pct, lower_wick_pct) for last M1 candle."""
    try:
        if m1 is None or getattr(m1, "empty", True):
            return 0.5, 0.25, 0.25
        last = m1.iloc[-1]
        high = _float(last.get("high"))
        low = _float(last.get("low"))
        close = _float(last.get("close"))
        open_ = _float(last.get("open"))
        if None in {high, low, close, open_}:
            return 0.5, 0.25, 0.25
        candle_range = high - low
        if candle_range <= 0:
            return 0.5, 0.25, 0.25
        body = abs(close - open_)
        upper_wick = high - max(close, open_)
        lower_wick = min(close, open_) - low
        return (
            round(min(1.0, body / candle_range), 4),
            round(min(1.0, max(0.0, upper_wick / candle_range)), 4),
            round(min(1.0, max(0.0, lower_wick / candle_range)), 4),
        )
    except Exception:
        return 0.5, 0.25, 0.25
