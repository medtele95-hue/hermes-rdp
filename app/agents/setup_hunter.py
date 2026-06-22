from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from app.agents.big_setup_detector import BigSetupDetector
from app.agents.confirmation_matrix import evaluate as _confirmation_matrix
from app.agents.hermes_entry_gate import evaluate_entry_gates as _hermes_entry_gates
from app.agents.setup_quality_tier import evaluate_setup_quality as _hermes_tier, tier_to_gate_failures as _tier_to_gate_failures
from app.agents.safety_guard import SafetyGuard
from app.config import Settings
from app.logger import log
from app.mt5.ml_random_forest_confirmator import confirm as _ml_rf_confirm
from app.mt5.geometric_engine_v2 import final_trade_gate as _final_trade_gate_v2
from app.mt5.geometric_engine_v2 import geometric_score
from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence as _calculate_momentum
from app.mt5.mtf_arbiter import arbitrate_mtf as _arbitrate_mtf
from app.mt5.smc_orderblock_liquidity_narrator import narrate as _smc_ob_narrate
from app.profiles.lovable_btc_old_system import is_active as _lovable_btc_is_active
from app.strategies.candidate import validate_candidate
from app.strategies.hermes_strategy_pack_agent import build_candidate as build_strategy_pack_candidate
from app.strategies.registry import (
    ALLOWED_EUR_EXECUTION_STRATEGIES,
    ALLOWED_GOLD_EXECUTION_STRATEGIES,
    ACTIVE_EXECUTION_STRATEGIES,
    CONFIRMATION_STRATEGIES,
    OBSERVATION_STRATEGIES,
    allowed_for_symbol,
)
from app.utils.confidence import normalize_confidence


ENTRY_STRATEGIES = set(ACTIVE_EXECUTION_STRATEGIES)
OBSERVER_STRATEGIES = set(OBSERVATION_STRATEGIES)
_GOLD_CANDIDATE_STRATEGIES = frozenset({
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_M1_M5_EMA_SWEEP_SCALPER",
    "GOLD_ORDER_FLOW_CVD_VWAP",
    "ORDER_FLOW_EXECUTION_AGENT",
})
NEAR_MISS_CATEGORIES = {
    "DIRECTION_RESOLVER_FAIL",
    "WAITING_FOR_M1_TRIGGER",
    "WAITING_FOR_M15_CONFIRMATION",
    "CONFIRMATION_MATRIX_HARD_BLOCK",
    "WAITING_FOR_SESSION",
    "RR_TOO_LOW",
    "SPREAD_TOO_HIGH",
    "STRATEGY_OBSERVER_ONLY",
    "GOLD_GENERIC_STRATEGY_DISABLED",
    "EUR_GENERIC_STRATEGY_DISABLED",
    "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT",
    "BTC_SCALPING_CONFIDENCE_BELOW_MIN",
    "CONFLUENCE_SCORE_TOO_LOW",
    "MOMENTUM_DIVERGENCE",
}

# Hard floor for BTC_SCALPING routing — never route below this confidence regardless of settings.
_BTC_SCALPING_MIN_ROUTE_CONFIDENCE = 75
_OF_MIN_GEOMETRIC_SCORE = 50.0
_PENALTY_FULL_BONUS_SCORE = 80.0
_PENALTY_NEUTRAL_SCORE = 50.0
_PENALTY_SOFT_SCORE = 20.0
_PENALTY_FULL_BONUS = 10.0
_PENALTY_SOFT = -5.0
_PENALTY_STRONG_BASE = -15.0

# Strategy tie-break priority: higher = preferred when grade and score are equal.
_STRATEGY_PRIORITY: dict[str, int] = {
    "ORDER_FLOW_EXECUTION_AGENT": 4,
    "FIB_CONFLUENCE_EXECUTION_AGENT": 3,
    "GOLD_LIQUIDITY_HUNTER_PRO": 3,
    "GOLD_M1_M5_EMA_SWEEP_SCALPER": 3,
    "EUR_EMA_RSI_ATR_CROSSOVER": 2,
    "HERMES_STRATEGY_PACK_AGENT": 2,
    "BTC_SCALPING_AGENT": 1,
}


@dataclass(frozen=True)
class SetupHunterResult:
    candidates: list[dict]
    best_candidate: dict
    decision: dict
    events: list[dict]


class SetupHunter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.big_setup = BigSetupDetector()
        self.safety_guard = SafetyGuard(settings)

    def evaluate(
        self,
        symbol: str,
        broker_symbol: str,
        analysis: dict,
        time_gate: dict,
        spread: float,
        max_spread: float,
        symbol_specs: dict | None = None,
        tick: dict | None = None,
        recent_candles: list[dict] | None = None,
        audit_cycle_id: str | None = None,
    ) -> SetupHunterResult:
        base_decision = dict(analysis.get("ai_decision") or {})
        momentum_result = analysis.get("momentum_result")
        if momentum_result is None and getattr(self.settings, "multi_tf_momentum_enabled", False) is True:
            try:
                configured_tfs = [
                    item.strip().upper()
                    for item in str(getattr(self.settings, "multi_tf_momentum_timeframes", "M1,M5,M15,H1,H4")).split(",")
                    if item.strip()
                ]
                momentum_result = _calculate_momentum(
                    broker_symbol or symbol,
                    configured_tfs,
                    min_confluence=int(getattr(self.settings, "multi_tf_momentum_min_confluence", 4)),
                    ema_length=int(getattr(self.settings, "multi_tf_momentum_ema_length", 20)),
                    rsi_length=int(getattr(self.settings, "multi_tf_momentum_rsi_length", 14)),
                )
            except Exception as exc:
                log.error("[MTF_MOMENTUM_ERROR] symbol=%s error=%s", broker_symbol or symbol, exc)
                momentum_result = None
        if isinstance(momentum_result, dict):
            base_decision["momentum_result"] = momentum_result
            base_decision["momentum_score"] = momentum_result.get("momentum_score")
        signals = list(analysis.get("strategy_signals") or [])
        if getattr(self.settings, "hermes_strategy_pack_enabled", True):
            pack_candidate = build_strategy_pack_candidate(
                symbol,
                broker_symbol,
                signals,
                {
                    "final_confluence_score": base_decision.get("final_confluence_score"),
                    "final_confluence_grade": base_decision.get("final_confluence_grade"),
                },
            )
        else:
            pack_candidate = None
            log.info("[HERMES_PACK] symbol=%s decision=WAIT reason=HERMES_STRATEGY_PACK_DISABLED", symbol)
        gold_symbol = _is_gold_symbol(symbol, broker_symbol)
        skipped_gold_generics = 0
        if gold_symbol:
            filtered_signals = []
            for signal in signals:
                if not isinstance(signal, dict):
                    continue
                strategy = _signal_strategy(signal)
                if strategy not in ALLOWED_GOLD_EXECUTION_STRATEGIES:
                    skipped_gold_generics += 1
                    continue
                filtered_signals.append(signal)
            signals = filtered_signals
        eur_symbol = _is_eur_symbol(symbol, broker_symbol)
        skipped_eur_generics = 0
        if eur_symbol:
            filtered_signals = []
            for signal in signals:
                if not isinstance(signal, dict):
                    continue
                strategy = _signal_strategy(signal)
                if strategy not in ALLOWED_EUR_EXECUTION_STRATEGIES:
                    skipped_eur_generics += 1
                    continue
                filtered_signals.append(signal)
            signals = filtered_signals
        if pack_candidate:
            signals.append(pack_candidate)
        ema = _ema_confirmation(signals)
        candidates: list[dict] = []
        _valid_entry_ids: set[int] = set()
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            candidate = self._candidate(
                symbol, broker_symbol, signal, base_decision, time_gate, spread, max_spread, ema,
                symbol_specs=symbol_specs, tick=tick, recent_candles=recent_candles, audit_cycle_id=audit_cycle_id,
            )
            candidates.append(candidate)
            strategy = str(candidate.get("best_strategy") or "")
            # Symbol-specific candidate visibility logs
            if gold_symbol and strategy in _GOLD_CANDIDATE_STRATEGIES:
                _log_gold_candidate(symbol, strategy, candidate)
            if eur_symbol and strategy == "EUR_EMA_RSI_ATR_CROSSOVER":
                _log_eur_candidate(symbol, candidate)
            # Gate validate_candidate on BUY/SELL only — never pass WAIT
            if candidate.get("strategy_role") == "ENTRY":
                direction = candidate.get("direction")
                if direction not in {"BUY", "SELL"}:
                    wait_reason = _candidate_wait_reason(candidate)
                    log.info(
                        "[CANDIDATE_WAIT] symbol=%s strategy=%s reason=%s",
                        symbol, strategy, wait_reason,
                    )
                    log.info(
                        "[SETUP_HUNTER_REJECT] symbol=%s strategy=%s reason=%s",
                        symbol, strategy, wait_reason,
                    )
                else:
                    valid, v_reason, _v_field = validate_candidate(candidate)
                    if valid:
                        _valid_entry_ids.add(id(candidate))
                        log.debug(
                            "[SETUP_HUNTER_IN] symbol=%s strategy=%s candidate=valid",
                            symbol, strategy,
                        )
                    else:
                        log.info(
                            "[SETUP_HUNTER_IN] symbol=%s strategy=%s candidate=invalid",
                            symbol, strategy,
                        )
                        log.info(
                            "[SETUP_HUNTER_REJECT] symbol=%s strategy=%s reason=%s",
                            symbol, strategy, v_reason,
                        )
        candidates.sort(key=lambda item: (_candidate_sort_key(item, symbol, broker_symbol), item.get("edge_score") or 0, item.get("setup_score") or 0), reverse=True)
        executable_ready = [
            item for item in candidates
            if item.get("execution_candidate") and item.get("demo_eligible")
            and id(item) in _valid_entry_ids
        ]
        executable_ready.sort(key=lambda item: (_candidate_sort_key(item, symbol, broker_symbol), item.get("edge_score") or 0, item.get("setup_score") or 0), reverse=True)
        for _item in executable_ready:
            log.info(
                "[SETUP_HUNTER_RAW_ACCEPT] symbol=%s strategy=%s raw_grade=%s raw_score=%s"
                " accepted_for_analysis=true accepted_for_execution=false",
                symbol, _item.get("best_strategy"), _item.get("grade"), _item.get("confidence"),
            )
        policy_controlled = any(item.get("execution_policy") == "ANALYSIS_ONLY_NOT_EXECUTABLE" for item in candidates)
        if gold_symbol:
            log.info(
                "[GOLD_ROUTER] symbol=%s allowed_candidates=%s generic_candidates=0",
                broker_symbol or symbol, ",".join(sorted(ALLOWED_GOLD_EXECUTION_STRATEGIES)),
            )
            if skipped_gold_generics:
                log.info(
                    "[GOLD_ROUTER] generic_candidates_skipped=%s reason=GOLD_GENERIC_STRATEGY_DISABLED",
                    skipped_gold_generics,
                )
        if eur_symbol and skipped_eur_generics:
            log.info(
                "[EUR_ROUTER] generic_candidates_skipped=%s reason=EUR_GENERIC_STRATEGY_DISABLED",
                skipped_eur_generics,
            )
        if executable_ready:
            best = executable_ready[0]
        elif policy_controlled or gold_symbol or _is_eur_symbol(symbol, broker_symbol):
            best = self._empty_candidate(symbol, broker_symbol, base_decision, time_gate, "NO_ALLOWED_EXECUTION_CANDIDATE")
        else:
            best = candidates[0] if candidates else self._empty_candidate(symbol, broker_symbol, base_decision, time_gate)
        decision = self._execution_decision(best, base_decision) if best.get("entry_candidate") else base_decision
        events = self.events_for_result(best)
        if best.get("best_strategy") == "NONE" and best.get("empty_reason") == "NO_ALLOWED_EXECUTION_CANDIDATE":
            log.info("[SETUP_HUNTER] symbol=%s best=NONE reason=NO_ALLOWED_EXECUTION_CANDIDATE", symbol)
        else:
            log.info(
                "[SETUP_HUNTER] symbol=%s best=%s direction=%s score=%s grade=%s verdict=%s accepted=%s missing=%s",
                symbol,
                best.get("best_strategy"),
                best.get("direction"),
                best.get("edge_score"),
                best.get("grade"),
                best.get("final_verdict") or "PENDING",
                str(bool(best.get("accepted_for_analysis"))).lower(),
                ",".join(best.get("what_is_missing_to_enter") or []),
            )
        if best.get("near_miss_reason"):
            log.info(
                "[NEAR_MISS] symbol=%s strategy=%s direction=%s score=%s missing=%s",
                symbol,
                best.get("best_strategy"),
                best.get("direction"),
                best.get("edge_score"),
                ",".join(best.get("what_is_missing_to_enter") or []),
            )
        return SetupHunterResult(candidates=candidates, best_candidate=best, decision=decision, events=events)

    def _candidate(
        self,
        symbol: str,
        broker_symbol: str,
        signal: dict,
        base_decision: dict,
        time_gate: dict,
        spread: float,
        max_spread: float,
        ema: dict,
        symbol_specs: dict | None = None,
        tick: dict | None = None,
        recent_candles: list[dict] | None = None,
        audit_cycle_id: str | None = None,
    ) -> dict:
        strategy = str(signal.get("strategy") or signal.get("setup_type") or "UNKNOWN").upper()
        role = _strategy_role(strategy)
        raw_signal = str(signal.get("signal") or signal.get("direction") or "WAIT").upper()
        resolved_before_eligibility = str(signal.get("resolved_direction") or "").upper()
        direction = raw_signal if raw_signal in {"BUY", "SELL"} else (resolved_before_eligibility if resolved_before_eligibility in {"BUY", "SELL"} else raw_signal)
        merged = {**base_decision, **signal, **time_gate, "symbol": symbol, "broker_symbol": broker_symbol, "spread": spread}
        merged["strategy"] = strategy
        merged["signal"] = direction
        merged["direction_raw_signal"] = raw_signal
        merged["direction_resolved_before_eligibility"] = resolved_before_eligibility or None
        merged["risk_reward"] = _first_number(signal.get("risk_reward"), signal.get("reward_risk"), base_decision.get("risk_reward"), base_decision.get("reward_risk"))
        merged.setdefault("m15_confirmation", bool(merged.get("m15_confirmation") or merged.get("smc_m15_confirmation")))
        merged.setdefault("m1_entry_confirmation", bool(merged.get("m1_entry_confirmation") or merged.get("smc_m1_entry_confirmation")))
        if not merged.get("safety_guard_status"):
            merged.update(self.safety_guard.evaluate(merged))
        big_setup = self.big_setup.evaluate(merged)
        merged.update(big_setup)
        setup_score = _setup_score(strategy, merged)
        confirmation_boost = int(ema.get("confirmation_boost") or 0) if role == "ENTRY" and ema.get("ema_confirmation") and str(ema.get("ema_direction") or "").upper() == direction else 0
        setup_score = min(100, setup_score + confirmation_boost)
        merged["setup_score"] = setup_score
        merged.update({key: value for key, value in ema.items() if key != "ema_direction"})
        edge_score = setup_score if strategy == "BTC_SCALPING_AGENT" else _edge_score(setup_score, role, merged)
        grade = _candidate_grade(strategy, setup_score, edge_score, self.settings)
        geo_mode = str(getattr(self.settings, "geometric_mode", "SHADOW") or "SHADOW").upper()
        failed = _failed_gates(role, merged, spread, max_spread, self.settings)
        momentum = merged.get("momentum_result") if isinstance(merged.get("momentum_result"), dict) else None
        momentum_adjustment = 0.0
        momentum_aligned = None
        if momentum and momentum.get("confluence_active") and direction in {"BUY", "SELL"}:
            expected_momentum = "BULL" if direction == "BUY" else "BEAR"
            momentum_aligned = str(momentum.get("confluence_direction") or "").upper() == expected_momentum
            if strategy == "ORDER_FLOW_EXECUTION_AGENT" or geo_mode in {"EXECUTION_FILTER", "LIVE"}:
                momentum_adjustment = 5.0 if momentum_aligned else -10.0
            else:
                momentum_adjustment = 0.0
            if not momentum_aligned and (strategy == "ORDER_FLOW_EXECUTION_AGENT" or geo_mode in {"EXECUTION_FILTER", "LIVE"}):
                failed.append("MOMENTUM_DIVERGENCE")
        merged["momentum_aligned"] = momentum_aligned
        merged["momentum_alignment_adjustment"] = momentum_adjustment
        mtf_arbiter = _arbitrate_mtf(
            merged.get("h4_main_bias") or merged.get("smc_h4_direction"),
            merged.get("d1_macro_bias"),
            merged.get("smc_h4_direction") or merged.get("smc_status"),
            (momentum or {}).get("confluence_direction") if momentum else None,
        )
        merged["mtf_arbiter"] = mtf_arbiter
        geometric_v2 = merged.get("geometric_v2") if isinstance(merged.get("geometric_v2"), dict) else _candidate_geometric_v2(merged, geo_mode)
        merged["geometric_v2"] = geometric_v2
        directional_momentum = dict(momentum or {})
        raw_momentum_score = _to_float(directional_momentum.get("momentum_score"))
        if raw_momentum_score is not None:
            directional_momentum["momentum_score"] = raw_momentum_score if direction != "SELL" else 100.0 - raw_momentum_score
        directional_momentum["aligned"] = momentum_aligned
        final_gate = _final_trade_gate_v2(
            geometric_v2,
            {"score": _to_float(merged.get("order_flow_execution_agent_score") or merged.get("order_flow_score")) or 0.0},
            {"status": merged.get("smc_calibrated_status") or merged.get("smc_status"), "score": _to_float(merged.get("smc_confluence_score")) or 0.0},
            {"status": merged.get("mtfa_calibrated_status") or merged.get("mtfa_status"), "score": _to_float(merged.get("mtfa_score")) or 0.0, "trend_strength": _to_float(merged.get("mtfa_trend_strength")) or 0.0},
            float(spread),
            _to_float(merged.get("atr") or merged.get("atr_value")) or 0.0,
            str(merged.get("session_name") or "UNKNOWN"),
            broker_symbol or symbol,
            _to_float(merged.get("capital_risk_pct")) or 0.0,
            geo_mode,
            strategy=strategy,
            spread_points=float(spread),
            point=_to_float((symbol_specs or {}).get("point")) if symbol_specs else None,
            bid=_to_float((tick or {}).get("bid")) if tick else None,
            ask=_to_float((tick or {}).get("ask")) if tick else None,
            max_spread=_to_float(max_spread),
            cycle_id=audit_cycle_id,
            momentum_result=directional_momentum,
            recent_candles=recent_candles,
        )
        merged["final_trade_gate_v2"] = final_gate
        if geo_mode in {"EXECUTION_FILTER", "LIVE"} and final_gate.get("hard_block"):
            failed.extend(str(reason) for reason in final_gate.get("hard_block_reasons") or [])
        policy_reason = _execution_policy_block_reason(symbol, broker_symbol, strategy, role, self.settings)
        if policy_reason is None and strategy == "GOLD_ORDER_FLOW_CVD_VWAP":
            missing_order_flow = _gold_order_flow_missing_required_fields(merged, self.settings)
            if missing_order_flow:
                policy_reason = "ORDER_FLOW_REQUIRED_FIELDS_MISSING"
                merged["missing_required_fields"] = missing_order_flow
        if policy_reason is None and strategy == "ORDER_FLOW_EXECUTION_AGENT":
            if not bool(getattr(self.settings, "order_flow_execution_enabled", False)):
                policy_reason = "ORDER_FLOW_EXECUTION_DISABLED"
        if policy_reason:
            failed.append(policy_reason)
            log.info(
                "[SETUP_POLICY] symbol=%s strategy=%s decision=SKIP_EXEC reason=%s",
                broker_symbol or symbol, strategy, policy_reason,
            )
        near_miss = _near_miss_reason(failed, role)
        executable = role == "ENTRY" and policy_reason is None

        # §v1.5: raw grade + behavior-neutral ML/SMC observability stubs
        _raw_strategy_grade = grade
        _ml_result = _ml_rf_confirm(broker_symbol or symbol, merged, self.settings)
        _ml_status = str(_ml_result.get("ml_status") or "UNAVAILABLE").upper()
        _smc_ob_result = _smc_ob_narrate(broker_symbol or symbol, merged, self.settings)

        # §v1.5: preliminary final_verdict based on internal gates
        # NOTE: main.py FINAL CONFLUENCE GATE may further set demo_eligible=False
        _accepted_for_analysis = not bool(failed)
        _accepted_for_execution = executable and not bool(failed)
        if not executable or failed:
            _final_verdict: str = "BLOCK"
            _verdict_reason: str | None = "GATES_FAILED"
        else:
            # ML UNAVAILABLE/WARMING_UP is strictly neutral — never blocks
            _final_verdict = "PASS"
            _verdict_reason = None

        # §v1.5-fix: NO [FINAL_VERDICT] log here — setup_hunter is pre-confluence.
        # The ONE authoritative verdict is emitted by main.py after evaluate_confluence().
        demo_eligible = _final_verdict == "PASS"
        return {
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "best_strategy": strategy,
            "strategy_role": role,
            "direction": direction,
            "direction_raw_signal": raw_signal,
            "direction_resolved_before_eligibility": resolved_before_eligibility or None,
            "setup_score": setup_score,
            "edge_score": edge_score,
            "grade": grade,
            "normalized_confidence": setup_score if strategy == "BTC_SCALPING_AGENT" else None,
            "entry_candidate": executable and direction in {"BUY", "SELL"},
            "execution_candidate": executable and direction in {"BUY", "SELL"},
            "execution_policy": "EXECUTABLE" if policy_reason is None else "ANALYSIS_ONLY_NOT_EXECUTABLE",
            "execution_policy_reason": policy_reason,
            "analysis_only_reason": "ANALYSIS_ONLY_NOT_EXECUTABLE" if policy_reason else None,
            "demo_eligible": demo_eligible,
            "raw_strategy_grade": _raw_strategy_grade,
            "raw_strategy_score": edge_score,
            "ml_status": _ml_status,
            "final_verdict": _final_verdict,
            "accepted_for_analysis": _accepted_for_analysis,
            "accepted_for_execution": _accepted_for_execution,
            "rr": merged.get("risk_reward"),
            "entry": signal.get("entry") if signal.get("entry") is not None else base_decision.get("entry"),
            "sl": signal.get("sl") if signal.get("sl") is not None else base_decision.get("sl"),
            "tp": signal.get("tp") if signal.get("tp") is not None else base_decision.get("tp"),
            "time_session": time_gate.get("session_name"),
            "time_gate_status": time_gate.get("time_gate_status"),
            "market_open": time_gate.get("symbol_market_open", time_gate.get("market_open")),
            "smc_status": merged.get("smc_confluence_status") or merged.get("smc_status"),
            "smc_score": _to_float(merged.get("smc_confluence_score")),
            "final_confluence_score": _to_float(merged.get("final_confluence_score") or merged.get("confluence_score") or merged.get("smc_confluence_score")),
            "final_confluence_grade": merged.get("final_confluence_grade") or merged.get("confluence_grade") or _grade(int(_to_float(merged.get("final_confluence_score") or merged.get("confluence_score") or merged.get("smc_confluence_score")) or 0)),
            "mtfa_status": merged.get("mtfa_status"),
            "mtfa_score": _to_float(merged.get("mtfa_score")),
            "geometric_v2": merged.get("geometric_v2"),
            "momentum_result": merged.get("momentum_result"),
            "momentum_score": merged.get("momentum_score"),
            "momentum_aligned": merged.get("momentum_aligned"),
            "momentum_alignment_adjustment": merged.get("momentum_alignment_adjustment"),
            "mtf_arbiter": merged.get("mtf_arbiter"),
            "final_trade_gate_v2": merged.get("final_trade_gate_v2"),
            "mtf_structure_status": merged.get("mtf_structure_status"),
            "m15_confirmation": bool(merged.get("m15_confirmation") or merged.get("smc_m15_confirmation")),
            "m15_confirmation_status": merged.get("m15_confirmation_status", "PASS" if bool(merged.get("m15_confirmation") or merged.get("smc_m15_confirmation")) else "FAIL"),
            "m15_confirmation_type": merged.get("m15_confirmation_type"),
            "m15_confirmation_reason": merged.get("m15_confirmation_reason"),
            "m15_confirmation_price": merged.get("m15_confirmation_price"),
            "m15_confirmation_candle_time": merged.get("m15_confirmation_candle_time"),
            "m15_confirmation_direction": merged.get("m15_confirmation_direction"),
            "m1_confirmation": bool(merged.get("m1_entry_confirmation") or merged.get("smc_m1_entry_confirmation")),
            "m1_trigger_status": merged.get("m1_trigger_status", "PASS" if bool(merged.get("m1_entry_confirmation") or merged.get("smc_m1_entry_confirmation")) else "FAIL"),
            "m1_trigger_type": merged.get("m1_trigger_type"),
            "m1_trigger_price": merged.get("m1_trigger_price"),
            "m1_trigger_candle_time": merged.get("m1_trigger_candle_time"),
            "m1_trigger_reason": merged.get("m1_trigger_reason"),
            "m1_trigger_direction": merged.get("m1_trigger_direction"),
            "safety_guard_status": merged.get("safety_guard_status"),
            "big_setup_grade": merged.get("big_setup_grade"),
            "support_break": merged.get("support_break"),
            "resistance_break": merged.get("resistance_break"),
            "trend_continuation_score": merged.get("trend_continuation_score"),
            "trend_continuation_reason": merged.get("trend_continuation_reason"),
            "trend_continuation_retest": merged.get("trend_continuation_retest"),
            "trend_continuation_momentum": merged.get("trend_continuation_momentum"),
            "resolved_direction": merged.get("resolved_direction"),
            "direction_source": merged.get("direction_source"),
            "direction_confidence": merged.get("direction_confidence"),
            "direction_block_reason": merged.get("direction_block_reason"),
            "ema_confirmation": bool(ema.get("ema_confirmation") and str(ema.get("ema_direction") or "").upper() == direction) if role == "ENTRY" else bool(strategy == "EMA_PULLBACK"),
            "ema_score": ema.get("ema_score"),
            "ema_reason": ema.get("ema_reason"),
            "confirmation_boost": confirmation_boost,
            "quant_slope": merged.get("quant_slope"),
            "quant_r2": merged.get("quant_r2"),
            "quant_z_score": merged.get("quant_z_score"),
            "quant_mean": merged.get("quant_mean"),
            "quant_stdev": merged.get("quant_stdev"),
            "quant_signal": merged.get("quant_signal"),
            "quant_score": merged.get("quant_score"),
            "quant_reason": merged.get("quant_reason"),
            "quant_pro_regime": merged.get("quant_pro_regime"),
            "quant_pro_score": merged.get("quant_pro_score"),
            "quant_pro_grade": merged.get("quant_pro_grade"),
            "quant_pro_ols_slope": merged.get("quant_pro_ols_slope"),
            "quant_pro_ols_r2": merged.get("quant_pro_ols_r2"),
            "quant_pro_ols_tstat": merged.get("quant_pro_ols_tstat"),
            "quant_pro_kalman_velocity": merged.get("quant_pro_kalman_velocity"),
            "quant_pro_kalman_z": merged.get("quant_pro_kalman_z"),
            "quant_pro_ou_beta": merged.get("quant_pro_ou_beta"),
            "quant_pro_ou_tstat": merged.get("quant_pro_ou_tstat"),
            "quant_pro_ou_half_life": merged.get("quant_pro_ou_half_life"),
            "quant_pro_hurst": merged.get("quant_pro_hurst"),
            "quant_pro_hurst_filter": merged.get("quant_pro_hurst_filter"),
            "quant_pro_min_trend_hurst": merged.get("quant_pro_min_trend_hurst"),
            "quant_pro_trend_strength": merged.get("quant_pro_trend_strength"),
            "quant_pro_hurst_filter_status": merged.get("quant_pro_hurst_filter_status"),
            "quant_pro_hurst_block_reason": merged.get("quant_pro_hurst_block_reason"),
            "quant_pro_ewma_vol": merged.get("quant_pro_ewma_vol"),
            "quant_pro_signal": merged.get("quant_pro_signal"),
            "quant_pro_reason": merged.get("quant_pro_reason"),
            "quant_pro_no_lookahead": merged.get("quant_pro_no_lookahead"),
            "btc_scalping_agent": merged.get("btc_scalping_agent"),
            "btc_scalping_decision": merged.get("btc_scalping_decision") or merged.get("decision"),
            "btc_scalping_signal": direction if strategy == "BTC_SCALPING_AGENT" else merged.get("btc_scalping_signal"),
            "btc_scalping_reason": merged.get("btc_scalping_reason") or merged.get("reason"),
            "btc_scalping_trigger_type": merged.get("btc_scalping_trigger_type") or merged.get("trigger_type"),
            "btc_scalping_confidence": setup_score if strategy == "BTC_SCALPING_AGENT" else merged.get("btc_scalping_confidence"),
            "btc_scalping_route_status": "PASS" if strategy == "BTC_SCALPING_AGENT" and not failed and policy_reason is None else None,
            "btc_scalping_route_reason": "SCALP_SIGNAL_VALID" if strategy == "BTC_SCALPING_AGENT" and not failed and policy_reason is None else None,
            "gold_liquidity_hunter": merged.get("gold_liquidity_hunter"),
            "gold_liquidity_signal": merged.get("gold_liquidity_signal"),
            "gold_liquidity_score": merged.get("gold_liquidity_score"),
            "gold_liquidity_reason": merged.get("gold_liquidity_reason"),
            "gold_m1m5_scalper": merged.get("gold_m1m5_scalper"),
            "gold_m1m5_scalper_decision": merged.get("gold_m1m5_scalper_decision"),
            "gold_m1m5_scalper_score": merged.get("gold_m1m5_scalper_score"),
            "gold_m1m5_scalper_reason": merged.get("gold_m1m5_scalper_reason"),
            "gold_order_flow_cvd_vwap": merged.get("gold_order_flow_cvd_vwap"),
            "gold_order_flow_signal": merged.get("gold_order_flow_signal"),
            "gold_order_flow_score": merged.get("gold_order_flow_score"),
            "order_flow_execution_agent": merged.get("order_flow_execution_agent"),
            "order_flow_execution_agent_score": merged.get("order_flow_execution_agent_score"),
            "order_flow_execution_agent_signal": merged.get("order_flow_execution_agent_signal"),
            "order_flow_execution_agent_reason": merged.get("order_flow_execution_agent_reason"),
            "gold_order_flow_reason": merged.get("gold_order_flow_reason"),
            "simo_atm_breakout": merged.get("simo_atm_breakout"),
            "simo_atm_signal": merged.get("simo_atm_signal"),
            "simo_atm_score": merged.get("simo_atm_score"),
            "simo_atm_reason": merged.get("simo_atm_reason"),
            "eur_ema_rsi_atr": merged.get("eur_ema_rsi_atr"),
            "eur_ema_rsi_atr_decision": merged.get("eur_ema_rsi_atr_decision"),
            "eur_ema_rsi_atr_reason": merged.get("eur_ema_rsi_atr_reason"),
            "relaxed_mode_active": merged.get("relaxed_mode_active"),
            "relaxed_reason": merged.get("relaxed_reason"),
            "hours_without_setup": merged.get("hours_without_setup"),
            "strict_threshold": merged.get("strict_threshold"),
            "relaxed_threshold": merged.get("relaxed_threshold"),
            "relaxed_trade_count_today": merged.get("relaxed_trade_count_today"),
            "last_relaxed_trade_result": merged.get("last_relaxed_trade_result"),
            "top_down_reader": merged.get("top_down_reader") if isinstance(merged.get("top_down_reader"), dict) else None,
            "top_down_status": merged.get("top_down_status"),
            "top_down_decision": merged.get("top_down_decision")
            or ((merged.get("top_down_reader") or {}).get("top_down_decision") if isinstance(merged.get("top_down_reader"), dict) else None)
            or ((merged.get("top_down_reader") or {}).get("decision") if isinstance(merged.get("top_down_reader"), dict) else None),
            "entry_readiness_score": merged.get("entry_readiness_score"),
            "market_narrative": merged.get("market_narrative"),
            "missing_confirmations": merged.get("missing_confirmations"),
            "score_breakdown": merged.get("score_breakdown"),
            "d1_macro_bias": merged.get("d1_macro_bias"),
            "h4_main_bias": merged.get("h4_main_bias"),
            "h1_internal_structure": merged.get("h1_internal_structure"),
            "m5_context_status": merged.get("m5_context_status"),
            "metadata": merged.get("metadata") or {},
            "failed_gates": failed,
            "eligibility_block_reasons": failed,
            "missing_required_fields": merged.get("missing_required_fields") or [],
            "near_miss_reason": near_miss,
            "what_is_missing_to_enter": failed,
            "raw_decision": merged,
            # Canonical contract fields (Phase 2)
            "strategy": strategy,   # alias of best_strategy for validate_candidate
            "mode": "ACTIVE_EXECUTION" if role == "ENTRY" else "OBSERVATION",
            "route_allowed": executable and direction in {"BUY", "SELL"},
            "confidence": edge_score,
        }

    def events_for_result(self, best: dict) -> list[dict]:
        if not best:
            return []
        setup = _event_payload("SETUP_HUNTER", best)
        events = [setup]
        if best.get("near_miss_reason"):
            events.append(_event_payload("NEAR_MISS", best))
        return events

    def _execution_decision(self, candidate: dict, base_decision: dict) -> dict:
        raw = dict(candidate.get("raw_decision") or {})
        quant_payload = {
            key: candidate.get(key)
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
                "btc_scalping_agent",
                "btc_scalping_decision",
                "btc_scalping_signal",
                "btc_scalping_reason",
                "btc_scalping_trigger_type",
                "btc_scalping_confidence",
                "btc_scalping_route_status",
                "btc_scalping_route_reason",
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
                "simo_atm_breakout",
                "simo_atm_signal",
                "simo_atm_score",
                "simo_atm_reason",
                "eur_ema_rsi_atr",
                "eur_ema_rsi_atr_decision",
                "eur_ema_rsi_atr_reason",
                "relaxed_mode_active",
                "relaxed_reason",
                "hours_without_setup",
                "strict_threshold",
                "relaxed_threshold",
                "relaxed_trade_count_today",
                "last_relaxed_trade_result",
            )
            if candidate.get(key) is not None
        }
        if candidate.get("best_strategy") == "QUANT_STATISTICAL_PULLBACK" and "quant_signal" not in quant_payload:
            quant_payload["quant_signal"] = candidate.get("direction")
        if candidate.get("best_strategy") == "QUANT_PRO_REGIME_SWITCHING" and "quant_pro_signal" not in quant_payload:
            quant_payload["quant_pro_signal"] = candidate.get("direction")
        existing_raw_payload = raw.get("raw_payload") if isinstance(raw.get("raw_payload"), dict) else {}
        top_down = candidate.get("top_down_reader") if isinstance(candidate.get("top_down_reader"), dict) else {}
        top_down_payload = {
            "top_down_reader": top_down,
            "top_down_status": candidate.get("top_down_status") or top_down.get("top_down_status"),
            "top_down_decision": candidate.get("top_down_decision") or top_down.get("top_down_decision") or top_down.get("decision"),
            "entry_readiness_score": candidate.get("entry_readiness_score") or top_down.get("entry_readiness_score"),
            "market_narrative": candidate.get("market_narrative") or top_down.get("market_narrative"),
            "missing_confirmations": candidate.get("missing_confirmations") or top_down.get("missing_confirmations") or top_down.get("timeframe_missing_confirmations") or [],
            "score_breakdown": candidate.get("score_breakdown") or top_down.get("score_breakdown") or {},
            "d1_macro_bias": candidate.get("d1_macro_bias") or top_down.get("d1_macro_bias"),
            "h4_main_bias": candidate.get("h4_main_bias") or top_down.get("h4_main_bias"),
            "h1_internal_structure": candidate.get("h1_internal_structure") or top_down.get("h1_internal_structure"),
            "m15_confirmation_status": candidate.get("m15_confirmation_status") or top_down.get("m15_confirmation_status"),
            "m5_context_status": candidate.get("m5_context_status") or top_down.get("m5_context_status"),
            "m1_trigger_status": candidate.get("m1_trigger_status") or top_down.get("m1_trigger_status"),
        }
        raw["raw_payload"] = {**existing_raw_payload, **quant_payload, **top_down_payload}
        raw.update(
            {
                "symbol": candidate.get("symbol"),
                "broker_symbol": candidate.get("broker_symbol"),
                "strategy": candidate.get("best_strategy"),
                "signal": candidate.get("direction"),
                "direction": candidate.get("direction"),
                "entry": candidate.get("entry"),
                "sl": candidate.get("sl"),
                "tp": candidate.get("tp"),
                "risk_reward": candidate.get("rr"),
                "reward_risk": candidate.get("rr"),
                "decision": "ENTER_ANALYSIS_ONLY",
                "setup_hunter_selected": True,
                "setup_hunter_score": candidate.get("edge_score"),
                "setup_hunter_grade": candidate.get("grade"),
                **{key: value for key, value in top_down_payload.items() if key != "top_down_reader"},
                "top_down_reader": top_down,
            }
        )
        return {**base_decision, **raw}

    def _empty_candidate(self, symbol: str, broker_symbol: str, base_decision: dict, time_gate: dict, reason: str = "NO_VALID_SIGNAL") -> dict:
        return {
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "best_strategy": "NONE",
            "strategy_role": "NONE",
            "direction": str(base_decision.get("signal") or "WAIT"),
            "setup_score": 0,
            "edge_score": 0,
            "grade": "D",
            "entry_candidate": False,
            "execution_candidate": False,
            "execution_policy": "NONE",
            "execution_policy_reason": reason,
            "demo_eligible": False,
            "rr": None,
            "entry": None,
            "sl": None,
            "tp": None,
            "time_session": time_gate.get("session_name"),
            "time_gate_status": time_gate.get("time_gate_status"),
            "failed_gates": [reason],
            "empty_reason": reason,
            "near_miss_reason": None,
            "what_is_missing_to_enter": [reason],
            "raw_decision": base_decision,
        }


def summarize_setup_hunter(candidates: Iterable[dict]) -> dict:
    items = list(candidates)
    near = [item for item in items if item.get("near_miss_reason")]
    ready = [item for item in items if item.get("demo_eligible")]
    best = max(items, key=lambda item: item.get("edge_score") or 0, default=None)
    return {
        "edge_ready_candidates": len(ready),
        "near_miss_candidates": len(near),
        "best_near_miss": max(near, key=lambda item: item.get("edge_score") or 0, default=None),
        "best_candidate_now": best,
        "current_session_quality": _session_quality(items),
        "top_missing_confirmation": dict(Counter(missing for item in near for missing in item.get("what_is_missing_to_enter", [])).most_common(10)),
        "setup_hunter_last_decision": best,
    }


def _strategy_role(strategy: str) -> str:
    if strategy in ENTRY_STRATEGIES:
        return "ENTRY"
    if strategy in CONFIRMATION_STRATEGIES:
        return "CONFIRMATION"
    if strategy in OBSERVER_STRATEGIES:
        return "OBSERVER"
    return "UNKNOWN"


def _signal_strategy(signal: dict) -> str:
    return str(signal.get("strategy") or signal.get("setup_type") or "UNKNOWN").upper()


def _execution_policy_block_reason(symbol: str, broker_symbol: str, strategy: str, role: str, settings: Settings) -> str | None:
    if role != "ENTRY":
        return None
    if not allowed_for_symbol(strategy, symbol, broker_symbol):
        if _is_gold_symbol(symbol, broker_symbol):
            return "GOLD_GENERIC_STRATEGY_DISABLED"
        if _is_eur_symbol(symbol, broker_symbol):
            return "EUR_GENERIC_STRATEGY_DISABLED"
        return "SYMBOL_STRATEGY_NOT_ALLOWED"
    if _is_gold_symbol(symbol, broker_symbol) and strategy not in ALLOWED_GOLD_EXECUTION_STRATEGIES:
        return "GOLD_GENERIC_STRATEGY_DISABLED"
    if _is_eur_symbol(symbol, broker_symbol) and strategy not in ALLOWED_EUR_EXECUTION_STRATEGIES:
        return "EUR_GENERIC_STRATEGY_DISABLED"
    if strategy == "BTC_SCALPING_AGENT" and not _is_btc_symbol(symbol, broker_symbol):
        return "BTC_SCALPING_AGENT_SYMBOL_NOT_BTC"
    if _is_btc_symbol(symbol, broker_symbol) and settings.btc_disable_quant_statistical_pullback and strategy == "QUANT_STATISTICAL_PULLBACK":
        return "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT"
    return None


def _candidate_sort_key(candidate: dict, symbol: str, broker_symbol: str) -> tuple:
    # Quality-first: role > grade > strategy priority. Edge/setup score appended by callers.
    role_rank = _role_rank(candidate.get("strategy_role"))
    grade_rank = _grade_rank(str(candidate.get("grade") or "D"))
    strategy_priority = _STRATEGY_PRIORITY.get(str(candidate.get("best_strategy") or "").upper(), 0)
    return (role_rank, grade_rank, strategy_priority)


def _candidate_grade(strategy: str, setup_score: int, edge_score: int, settings: Settings) -> str:
    if strategy == "BTC_SCALPING_AGENT" and setup_score >= max(int(getattr(settings, "btc_scalping_min_confidence", _BTC_SCALPING_MIN_ROUTE_CONFIDENCE)), _BTC_SCALPING_MIN_ROUTE_CONFIDENCE):
        return "B" if setup_score < 85 else _grade(setup_score)
    return _grade(edge_score)


def _btc_scalping_ready(payload: dict, settings: Settings) -> bool:
    if str(payload.get("strategy") or "").upper() != "BTC_SCALPING_AGENT":
        return False
    if str(payload.get("signal") or "").upper() not in {"BUY", "SELL"}:
        return False
    if not _is_btc_symbol(payload.get("symbol"), payload.get("broker_symbol")):
        return False
    confidence = _setup_score("BTC_SCALPING_AGENT", payload)
    min_conf = max(int(getattr(settings, "btc_scalping_min_confidence", _BTC_SCALPING_MIN_ROUTE_CONFIDENCE)), _BTC_SCALPING_MIN_ROUTE_CONFIDENCE)
    return confidence >= min_conf


def _gold_order_flow_missing_required_fields(payload: dict, settings: Settings) -> list[str]:
    if str(payload.get("strategy") or "").upper() != "GOLD_ORDER_FLOW_CVD_VWAP":
        return []
    data = payload.get("gold_order_flow_cvd_vwap") if isinstance(payload.get("gold_order_flow_cvd_vwap"), dict) else {}
    required = {
        "price": data.get("price") or data.get("entry") or payload.get("entry"),
        "vwap": data.get("vwap"),
        "poc": data.get("poc"),
        "vah": data.get("vah"),
        "val": data.get("val"),
        "cvd_proxy": data.get("cvd_proxy") if data.get("cvd_proxy") is not None else data.get("cvd"),
        "delta_proxy": data.get("delta_proxy") if data.get("delta_proxy") is not None else data.get("latest_delta"),
        "entry": data.get("entry") or payload.get("entry"),
        "sl": data.get("sl") or payload.get("sl"),
        "tp": data.get("tp") or payload.get("tp"),
        "rr": data.get("rr") or payload.get("risk_reward") or payload.get("reward_risk"),
    }
    missing = [key for key, value in required.items() if _to_float(value) is None]
    return list(dict.fromkeys(missing))


def _demo_ignore_time_blocks(settings: Settings) -> bool:
    return bool(
        getattr(settings, "demo_ignore_all_time_blocks", False)
        or getattr(settings, "demo_ignore_session_blocks", False)
        or getattr(settings, "demo_ignore_bad_hour_blocks", False)
    )


def _is_gold_symbol(*symbols: object) -> bool:
    return any(str(symbol or "").upper().startswith(("GOLD", "XAUUSD")) for symbol in symbols)


def _is_eur_symbol(*symbols: object) -> bool:
    return any(str(symbol or "").upper().startswith("EURUSD") for symbol in symbols)


def _is_btc_symbol(*symbols: object) -> bool:
    return any(str(symbol or "").upper().startswith("BTCUSD") for symbol in symbols)


def _role_rank(role: object) -> int:
    return {"ENTRY": 3, "CONFIRMATION": 2, "OBSERVER": 1}.get(str(role or "").upper(), 0)


def _ema_confirmation(signals: list[dict]) -> dict:
    ema = next((item for item in signals if isinstance(item, dict) and str(item.get("strategy") or item.get("setup_type") or "").upper() == "EMA_PULLBACK"), None)
    if not ema:
        return {"ema_confirmation": False, "ema_score": None, "ema_reason": "EMA_NOT_PRESENT", "confirmation_boost": 0, "ema_direction": None}
    direction = str(ema.get("signal") or ema.get("direction") or "WAIT").upper()
    score = _setup_score("EMA_PULLBACK", ema)
    confirmed = direction in {"BUY", "SELL"} and score >= 60
    return {
        "ema_confirmation": confirmed,
        "ema_score": score,
        "ema_reason": "EMA_CONFIRMS_DIRECTION" if confirmed else "EMA_NOT_CONFIRMING",
        "confirmation_boost": 5 if confirmed else 0,
        "ema_direction": direction,
    }


def _event_payload(event_type: str, candidate: dict) -> dict:
    missing = candidate.get("what_is_missing_to_enter") or candidate.get("failed_gates") or []
    return {
        "event_type": event_type,
        "symbol": candidate.get("symbol"),
        "broker_symbol": candidate.get("broker_symbol"),
        "strategy": candidate.get("best_strategy"),
        "strategy_role": candidate.get("strategy_role"),
        "direction": candidate.get("direction"),
        "direction_raw_signal": candidate.get("direction_raw_signal"),
        "direction_resolved_before_eligibility": candidate.get("direction_resolved_before_eligibility"),
        "resolved_direction": candidate.get("resolved_direction"),
        "direction_source": candidate.get("direction_source"),
        "direction_confidence": candidate.get("direction_confidence"),
        "direction_block_reason": candidate.get("direction_block_reason"),
        "setup_score": candidate.get("setup_score"),
        "edge_score": candidate.get("edge_score"),
        "grade": candidate.get("grade"),
        "demo_eligible": bool(candidate.get("demo_eligible")),
        "execution_candidate": bool(candidate.get("execution_candidate")),
        "execution_policy": candidate.get("execution_policy"),
        "execution_policy_reason": candidate.get("execution_policy_reason"),
        "analysis_only_reason": candidate.get("analysis_only_reason"),
        "eligibility_block_reasons": candidate.get("eligibility_block_reasons") or [],
        "missing": missing,
        "near_miss_reason": candidate.get("near_miss_reason"),
        "time_session": candidate.get("time_session"),
        "time_gate_status": candidate.get("time_gate_status"),
        "smc_status": candidate.get("smc_status"),
        "smc_score": candidate.get("smc_score"),
        "mtfa_status": candidate.get("mtfa_status"),
        "mtfa_score": candidate.get("mtfa_score"),
        "m15_confirmation": candidate.get("m15_confirmation"),
        "m15_confirmation_status": candidate.get("m15_confirmation_status"),
        "m15_confirmation_type": candidate.get("m15_confirmation_type"),
        "m15_confirmation_reason": candidate.get("m15_confirmation_reason"),
        "m15_confirmation_price": candidate.get("m15_confirmation_price"),
        "m15_confirmation_candle_time": candidate.get("m15_confirmation_candle_time"),
        "m15_confirmation_direction": candidate.get("m15_confirmation_direction"),
        "m1_confirmation": candidate.get("m1_confirmation"),
        "m1_entry_confirmation": candidate.get("m1_confirmation"),
        "m1_trigger_status": candidate.get("m1_trigger_status"),
        "m1_trigger_type": candidate.get("m1_trigger_type"),
        "m1_trigger_price": candidate.get("m1_trigger_price"),
        "m1_trigger_candle_time": candidate.get("m1_trigger_candle_time"),
        "m1_trigger_reason": candidate.get("m1_trigger_reason"),
        "m1_trigger_direction": candidate.get("m1_trigger_direction"),
        "rr": candidate.get("rr"),
        "entry": candidate.get("entry"),
        "sl": candidate.get("sl"),
        "tp": candidate.get("tp"),
        "support_break": candidate.get("support_break"),
        "resistance_break": candidate.get("resistance_break"),
        "trend_continuation_score": candidate.get("trend_continuation_score"),
        "trend_continuation_reason": candidate.get("trend_continuation_reason"),
        "trend_continuation_retest": candidate.get("trend_continuation_retest"),
        "trend_continuation_momentum": candidate.get("trend_continuation_momentum"),
        "ema_confirmation": candidate.get("ema_confirmation"),
        "ema_score": candidate.get("ema_score"),
        "ema_reason": candidate.get("ema_reason"),
        "confirmation_boost": candidate.get("confirmation_boost"),
        "quant_slope": candidate.get("quant_slope"),
        "quant_r2": candidate.get("quant_r2"),
        "quant_z_score": candidate.get("quant_z_score"),
        "quant_mean": candidate.get("quant_mean"),
        "quant_stdev": candidate.get("quant_stdev"),
        "quant_signal": candidate.get("quant_signal"),
        "quant_score": candidate.get("quant_score"),
        "quant_reason": candidate.get("quant_reason"),
        "quant_pro_regime": candidate.get("quant_pro_regime"),
        "quant_pro_score": candidate.get("quant_pro_score"),
        "quant_pro_grade": candidate.get("quant_pro_grade"),
        "quant_pro_ols_slope": candidate.get("quant_pro_ols_slope"),
        "quant_pro_ols_r2": candidate.get("quant_pro_ols_r2"),
        "quant_pro_ols_tstat": candidate.get("quant_pro_ols_tstat"),
        "quant_pro_kalman_velocity": candidate.get("quant_pro_kalman_velocity"),
        "quant_pro_kalman_z": candidate.get("quant_pro_kalman_z"),
        "quant_pro_ou_beta": candidate.get("quant_pro_ou_beta"),
        "quant_pro_ou_tstat": candidate.get("quant_pro_ou_tstat"),
        "quant_pro_ou_half_life": candidate.get("quant_pro_ou_half_life"),
        "quant_pro_hurst": candidate.get("quant_pro_hurst"),
        "quant_pro_hurst_filter": candidate.get("quant_pro_hurst_filter"),
        "quant_pro_min_trend_hurst": candidate.get("quant_pro_min_trend_hurst"),
        "quant_pro_trend_strength": candidate.get("quant_pro_trend_strength"),
        "quant_pro_hurst_filter_status": candidate.get("quant_pro_hurst_filter_status"),
        "quant_pro_hurst_block_reason": candidate.get("quant_pro_hurst_block_reason"),
        "quant_pro_ewma_vol": candidate.get("quant_pro_ewma_vol"),
        "quant_pro_signal": candidate.get("quant_pro_signal"),
        "quant_pro_reason": candidate.get("quant_pro_reason"),
        "quant_pro_no_lookahead": candidate.get("quant_pro_no_lookahead"),
        "btc_scalping_agent": candidate.get("btc_scalping_agent"),
        "btc_scalping_decision": candidate.get("btc_scalping_decision"),
        "btc_scalping_signal": candidate.get("btc_scalping_signal"),
        "btc_scalping_reason": candidate.get("btc_scalping_reason"),
        "btc_scalping_trigger_type": candidate.get("btc_scalping_trigger_type"),
        "btc_scalping_confidence": candidate.get("btc_scalping_confidence"),
        "btc_scalping_route_status": candidate.get("btc_scalping_route_status"),
        "btc_scalping_route_reason": candidate.get("btc_scalping_route_reason"),
        "gold_liquidity_hunter": candidate.get("gold_liquidity_hunter"),
        "gold_liquidity_signal": candidate.get("gold_liquidity_signal"),
        "gold_liquidity_score": candidate.get("gold_liquidity_score"),
        "gold_liquidity_reason": candidate.get("gold_liquidity_reason"),
        "gold_m1m5_scalper": candidate.get("gold_m1m5_scalper"),
        "gold_m1m5_scalper_decision": candidate.get("gold_m1m5_scalper_decision"),
        "gold_m1m5_scalper_score": candidate.get("gold_m1m5_scalper_score"),
        "gold_m1m5_scalper_reason": candidate.get("gold_m1m5_scalper_reason"),
        "gold_order_flow_cvd_vwap": candidate.get("gold_order_flow_cvd_vwap"),
        "gold_order_flow_signal": candidate.get("gold_order_flow_signal"),
        "gold_order_flow_score": candidate.get("gold_order_flow_score"),
        "gold_order_flow_reason": candidate.get("gold_order_flow_reason"),
        "order_flow_execution_agent": candidate.get("order_flow_execution_agent"),
        "order_flow_execution_agent_score": candidate.get("order_flow_execution_agent_score"),
        "order_flow_execution_agent_signal": candidate.get("order_flow_execution_agent_signal"),
        "order_flow_execution_agent_reason": candidate.get("order_flow_execution_agent_reason"),
        "simo_atm_breakout": candidate.get("simo_atm_breakout"),
        "simo_atm_signal": candidate.get("simo_atm_signal"),
        "simo_atm_score": candidate.get("simo_atm_score"),
        "simo_atm_reason": candidate.get("simo_atm_reason"),
        "eur_ema_rsi_atr": candidate.get("eur_ema_rsi_atr"),
        "eur_ema_rsi_atr_decision": candidate.get("eur_ema_rsi_atr_decision"),
        "eur_ema_rsi_atr_reason": candidate.get("eur_ema_rsi_atr_reason"),
        "relaxed_mode_active": candidate.get("relaxed_mode_active"),
        "relaxed_reason": candidate.get("relaxed_reason"),
        "hours_without_setup": candidate.get("hours_without_setup"),
        "strict_threshold": candidate.get("strict_threshold"),
        "relaxed_threshold": candidate.get("relaxed_threshold"),
        "relaxed_trade_count_today": candidate.get("relaxed_trade_count_today"),
        "last_relaxed_trade_result": candidate.get("last_relaxed_trade_result"),
        "top_down_status": candidate.get("top_down_status"),
        "top_down_decision": candidate.get("top_down_decision"),
        "entry_readiness_score": candidate.get("entry_readiness_score"),
        "market_narrative": candidate.get("market_narrative"),
        "missing_confirmations": candidate.get("missing_confirmations") or [],
        "score_breakdown": candidate.get("score_breakdown") or {},
        "d1_macro_bias": candidate.get("d1_macro_bias"),
        "h4_main_bias": candidate.get("h4_main_bias"),
        "h1_internal_structure": candidate.get("h1_internal_structure"),
        "m5_context_status": candidate.get("m5_context_status"),
        "top_down_reader": candidate.get("top_down_reader"),
    }


def _setup_score(strategy: str, payload: dict) -> int:
    fields = {
        "BREAKOUT_RETEST": "confidence",
        "CRT_TBS_REVERSAL": "crt_tbs_score",
        "AMD_FVG_IFVG_REVERSAL": "amd_fvg_score",
        "FIB_OTE_RETEST": "fib_ote_score",
        "FIB_CONFLUENCE_EXECUTION_AGENT": "confidence",
        "TREND_CONTINUATION_BREAKDOWN": "trend_continuation_score",
            "QUANT_STATISTICAL_PULLBACK": "quant_score",
            "QUANT_PRO_REGIME_SWITCHING": "quant_pro_score",
            "GOLD_LIQUIDITY_HUNTER_PRO": "gold_liquidity_score",
            "GOLD_M1_M5_EMA_SWEEP_SCALPER": "gold_m1m5_scalper_score",
            "GOLD_ORDER_FLOW_CVD_VWAP": "gold_order_flow_score",
            "ORDER_FLOW_EXECUTION_AGENT": "order_flow_execution_agent_score",
            "EUR_EMA_RSI_ATR_CROSSOVER": "confidence",
            "BTC_SCALPING_AGENT": "confidence",
            "SIMO_ATM_BREAKOUT": "simo_atm_score",
            "HERMES_STRATEGY_PACK_AGENT": "confidence",
            "EMA_PULLBACK": "confidence",
    }
    raw = _to_float(payload.get(fields.get(strategy, "confidence")))
    if raw is None:
        raw = _to_float(payload.get("big_setup_score")) or 0.0
    if strategy == "BTC_SCALPING_AGENT":
        raw = normalize_confidence(raw)
    elif raw <= 1.0:
        raw *= 100.0
    return int(max(0, min(100, round(raw))))


def _compute_penalty(raw_score: float) -> float:
    """Return the graduated confirmation adjustment for a raw 0-100 score."""
    score = max(0.0, min(100.0, float(raw_score)))
    if score >= _PENALTY_FULL_BONUS_SCORE:
        return _PENALTY_FULL_BONUS
    if score >= _PENALTY_NEUTRAL_SCORE:
        return 0.0
    if score >= _PENALTY_SOFT_SCORE:
        return _PENALTY_SOFT
    return _PENALTY_STRONG_BASE * (1.0 + (_PENALTY_SOFT_SCORE - score) / _PENALTY_SOFT_SCORE)


def _candidate_geometric_v2(payload: dict, mode: str) -> dict:
    pattern = {
        "quality": _to_float(payload.get("harmonic_score") or payload.get("harmonic_quality_score")) or 0.0,
    }
    prz = {
        "prz_strength": _to_float(payload.get("prz_strength")) or 0.0,
        "distance_atr": _to_float(payload.get("prz_distance_atr")),
    }
    if prz["distance_atr"] is None:
        prz["distance_atr"] = float("inf")
    return geometric_score(
        pattern,
        prz,
        _to_float(payload.get("htf_alignment_score")) or 0.0,
        _to_float(payload.get("gann_confluence")) or 0.0,
        _to_float(payload.get("vwap_score")) or 0.0,
        mode,
    )


def _edge_score(setup_score: int, role: str, payload: dict) -> int:
    score = setup_score
    score += min(15, int((_to_float(payload.get("smc_confluence_score")) or 0) / 10))
    score += min(10, int((_to_float(payload.get("mtfa_score")) or 0) / 12))
    if payload.get("m15_confirmation") or payload.get("smc_m15_confirmation"):
        score += 5
    if payload.get("m1_entry_confirmation") or payload.get("smc_m1_entry_confirmation"):
        score += 5
    if role != "ENTRY":
        score = min(score, 69)
    return int(max(0, min(100, score)))


def _failed_gates(role: str, payload: dict, spread: float, max_spread: float, settings: Settings) -> list[str]:
    failed: list[str] = []
    if role != "ENTRY":
        failed.append("STRATEGY_OBSERVER_ONLY" if role == "OBSERVER" else "STRATEGY_CONFIRMATION_ONLY")
    if str(payload.get("signal") or "").upper() not in {"BUY", "SELL"}:
        failed.append("NO_TRADE_DIRECTION")
    ignore_time_blocks = _demo_ignore_time_blocks(settings)
    if not ignore_time_blocks and str(payload.get("session_name") or "").upper() == "OFF_HOURS":
        failed.append("WAITING_FOR_SESSION")
    if not ignore_time_blocks and payload.get("time_gate_status") != "PASS":
        failed.append(str(payload.get("time_gate_reason") or "TIME_GATE_BLOCK"))
    if payload.get("symbol_market_open", payload.get("market_open")) is not True:
        failed.append("MARKET_CLOSED")
    if spread > max_spread:
        failed.append("SPREAD_TOO_HIGH")
    rr = _to_float(payload.get("risk_reward") or payload.get("reward_risk"))
    if rr is None or rr < 1.5:
        failed.append("RR_TOO_LOW")
    smc_score = _to_float(payload.get("smc_confluence_score")) or 0.0
    mtfa_score = _to_float(payload.get("mtfa_score")) or 0.0
    strategy = str(payload.get("strategy") or "").upper()
    quant_strategy = strategy == "QUANT_STATISTICAL_PULLBACK"
    quant_ready = quant_strategy and (_to_float(payload.get("quant_score")) or 0.0) >= settings.hermes_quant_min_score and (rr or 0.0) >= settings.hermes_quant_min_rr
    quant_pro_strategy = strategy == "QUANT_PRO_REGIME_SWITCHING"
    quant_pro_ready = quant_pro_strategy and (_to_float(payload.get("quant_pro_score")) or 0.0) >= settings.hermes_quant_pro_min_score and (rr or 0.0) >= settings.hermes_quant_pro_min_rr
    gold_strategy = strategy == "GOLD_LIQUIDITY_HUNTER_PRO"
    gold_ready = (
        gold_strategy
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("gold_liquidity_score")) or 0.0) >= settings.gold_min_liquidity_score
        and (rr or 0.0) >= settings.gold_min_rr
    )
    gold_m1m5_strategy = strategy == "GOLD_M1_M5_EMA_SWEEP_SCALPER"
    gold_m1m5_min_score = int(getattr(settings, "gold_m1m5_min_score_relaxed", 65)) if payload.get("relaxed_mode_active") else int(getattr(settings, "gold_m1m5_min_score_strict", 75))
    gold_m1m5_ready = (
        gold_m1m5_strategy
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("gold_m1m5_scalper_score")) or _to_float(payload.get("confidence")) or 0.0) >= gold_m1m5_min_score
        and (rr or 0.0) >= 1.5
    )
    gold_order_flow_strategy = strategy == "GOLD_ORDER_FLOW_CVD_VWAP"
    gold_order_flow_ready = (
        gold_order_flow_strategy
        and bool(getattr(settings, "gold_order_flow_execution_enabled", False))
        and not _gold_order_flow_missing_required_fields(payload, settings)
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("gold_order_flow_score")) or _to_float(payload.get("confidence")) or 0.0) >= int(getattr(settings, "gold_order_flow_min_confidence", 70))
        and (rr or 0.0) >= 1.5
    )
    order_flow_exec_strategy = strategy == "ORDER_FLOW_EXECUTION_AGENT"
    order_flow_exec_ready = (
        order_flow_exec_strategy
        and bool(getattr(settings, "order_flow_execution_enabled", False))
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("order_flow_execution_agent_score")) or _to_float(payload.get("confidence")) or 0.0) >= int(getattr(settings, "order_flow_min_score", 75))
        and (rr or 0.0) >= float(getattr(settings, "order_flow_min_rr", 1.5))
        and _to_float(payload.get("entry")) is not None
        and _to_float(payload.get("sl")) is not None
        and _to_float(payload.get("tp")) is not None
    )
    if order_flow_exec_ready:
        _of_setup_score = float(_setup_score("ORDER_FLOW_EXECUTION_AGENT", payload))
        _of_symbol_raw = str(payload.get("broker_symbol") or payload.get("symbol") or "").upper()
        _is_btc_of = _of_symbol_raw.startswith("BTCUSD")
        if _is_btc_of:
            _of_cm = _confirmation_matrix(
                str(payload.get("symbol") or ""),
                "ORDER_FLOW_EXECUTION_AGENT",
                smc_score,
                mtfa_score,
                _of_setup_score,
                rr,
                of_score=_of_setup_score,
            )
            if _of_cm["hard_block"]:
                failed.append("CONFIRMATION_MATRIX_HARD_BLOCK")
                log.info(
                    "[SETUP_HUNTER_REJECT] symbol=%s strategy=ORDER_FLOW_EXECUTION_AGENT "
                    "reason=CONFIRMATION_MATRIX_HARD_BLOCK",
                    str(payload.get("symbol") or ""),
                )
                log.info(
                    "[BTC_ENTRY_GUARD] status=BLOCK reason=CONFIRMATION_MATRIX_HARD_BLOCK"
                    " strategy=ORDER_FLOW_EXECUTION_AGENT exits_allowed=true",
                )
            _of_grade = str(payload.get("final_confluence_grade") or "D").upper()
            _of_cscore = _to_float(payload.get("final_confluence_score")) or 0.0
            _of_high_quality_legacy_bypass = _of_setup_score >= 90.0 and _of_grade == "A"
            if not _of_high_quality_legacy_bypass and _grade_rank(_of_grade) < _grade_rank("B"):
                failed.append("ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B")
                log.info(
                    "[BTC_ENTRY_GUARD] status=BLOCK reason=ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B"
                    " strategy=ORDER_FLOW_EXECUTION_AGENT grade=%s exits_allowed=true",
                    _of_grade,
                )
            if not _of_high_quality_legacy_bypass and _of_cscore < 65.0:
                failed.append("ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65")
                log.info(
                    "[BTC_ENTRY_GUARD] status=BLOCK reason=ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65"
                    " strategy=ORDER_FLOW_EXECUTION_AGENT score=%.1f exits_allowed=true",
                    _of_cscore,
                )
            if _of_high_quality_legacy_bypass:
                log.info(
                    "[BTC_ENTRY_GUARD] status=PASS reason=OF_BYPASS_SAFETY_CHECK "
                    "strategy=ORDER_FLOW_EXECUTION_AGENT grade=%s score=%.1f",
                    _of_grade, _of_setup_score,
                )
        _geo_mode = str(getattr(settings, "geometric_mode", "SHADOW") or "SHADOW").upper()
        _of_geo = _candidate_geometric_v2(payload, _geo_mode)
        payload["geometric_v2"] = _of_geo
        _of_grade_for_geo = str(payload.get("order_flow_grade") or payload.get("grade") or "D").upper()
        if (
            _geo_mode in {"EXECUTION_FILTER", "LIVE"}
            and float(_of_geo.get("score") or 0.0) < _OF_MIN_GEOMETRIC_SCORE
            and _of_grade_for_geo != "A+"
        ):
            failed.append("OF_GEO_INSUFFICIENT")
            order_flow_exec_ready = False
            log.info(
                "[SETUP_HUNTER_REJECT] symbol=%s strategy=ORDER_FLOW_EXECUTION_AGENT "
                "reason=OF_GEO_INSUFFICIENT geometric_score=%.1f mode=%s",
                payload.get("symbol"), float(_of_geo.get("score") or 0.0), _geo_mode,
            )
    fib_strategy = strategy == "FIB_CONFLUENCE_EXECUTION_AGENT"
    fib_ready = (
        fib_strategy
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("confidence")) or 0.0) >= 75.0
        and (_to_float(payload.get("final_confluence_score")) or 0.0) >= 55.0
        and _grade_rank(str(payload.get("grade") or "D")) >= _grade_rank("B")
        and _grade_rank(str(payload.get("final_confluence_grade") or "D")) >= _grade_rank("C")
        and (rr or 0.0) >= 1.5
        and _to_float(payload.get("entry")) is not None
        and _to_float(payload.get("sl")) is not None
        and _to_float(payload.get("tp")) is not None
    )
    btc_scalping_strategy = strategy == "BTC_SCALPING_AGENT"
    btc_scalping_ready = _btc_scalping_ready(payload, settings)
    _btc_lovable_bypass = (
        btc_scalping_strategy
        and _lovable_btc_is_active(settings)
        and str(payload.get("safety_guard_status") or "").upper() == "PASS"
    )
    if _btc_lovable_bypass:
        log.info(
            "[BTC_ENTRY_GATE_DEMO_ELIGIBLE] strategy=BTC_SCALPING_AGENT"
            " lovable_bypass=True confidence_gate=SKIPPED",
        )
    elif btc_scalping_strategy and str(payload.get("signal") or "").upper() in {"BUY", "SELL"} and not btc_scalping_ready:
        failed.append("BTC_SCALPING_CONFIDENCE_BELOW_MIN")
    if btc_scalping_ready or _btc_lovable_bypass:
        _btc_setup = float(_setup_score("BTC_SCALPING_AGENT", payload))
        _btc_cm = _confirmation_matrix(
            str(payload.get("symbol") or ""),
            "BTC_SCALPING_AGENT",
            smc_score,
            mtfa_score,
            _btc_setup,
            rr,
            of_score=_btc_setup,
        )
        _btc_confluence = _btc_setup + _compute_penalty(smc_score) + _compute_penalty(mtfa_score)
        _btc_safety_pass = str(payload.get("safety_guard_status") or "").upper() == "PASS"
        _old_btc_cm_bypass = _lovable_btc_is_active(settings) and _btc_safety_pass
        if _old_btc_cm_bypass:
            log.info(
                "[OLD_BTC_ROUTE] strategy=BTC_SCALPING_AGENT setup_hunter=ACCEPT safety=PASS confirmation_matrix_bypass=true",
            )
            log.info(
                "[BTC_SCALPING_ROUTE] decision=PASS reason=SCALP_SIGNAL_VALID normalized_confidence=%s",
                int(_btc_setup),
            )
        elif _btc_cm["hard_block"]:
            failed.append("CONFIRMATION_MATRIX_HARD_BLOCK")
            log.info(
                "[SETUP_HUNTER_REJECT] symbol=%s strategy=BTC_SCALPING_AGENT reason=CONFIRMATION_MATRIX_HARD_BLOCK",
                payload.get("symbol"),
            )
        elif _btc_confluence < 55.0:
            failed.append("CONFLUENCE_SCORE_TOO_LOW")
            log.info(
                "[SETUP_HUNTER_REJECT] symbol=%s strategy=BTC_SCALPING_AGENT reason=CONFLUENCE_SCORE_TOO_LOW",
                payload.get("symbol"),
            )
        else:
            log.info(
                "[BTC_SCALPING_ROUTE] decision=PASS reason=SCALP_SIGNAL_VALID normalized_confidence=%s",
                int(_btc_setup),
            )
    simo_strategy = strategy == "SIMO_ATM_BREAKOUT"
    simo_ready = (
        simo_strategy
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and _setup_score("SIMO_ATM_BREAKOUT", payload) >= int(getattr(settings, "new_strategies_min_score", 70))
        and (rr or 0.0) >= 1.5
        and _to_float(payload.get("entry")) is not None
        and _to_float(payload.get("sl")) is not None
        and _to_float(payload.get("tp")) is not None
    )
    strategy_pack = strategy == "HERMES_STRATEGY_PACK_AGENT"
    strategy_pack_ready = (
        strategy_pack
        and str(payload.get("signal") or "").upper() in {"BUY", "SELL"}
        and (_to_float(payload.get("confidence")) or 0.0) >= 75.0
        and (rr or 0.0) >= 1.5
        and (_to_float(payload.get("final_confluence_score")) or 0.0) >= 55.0
        and _grade_rank(str(payload.get("final_confluence_grade") or "D")) >= _grade_rank("C")
        and _grade_rank(str(payload.get("grade") or "D")) >= _grade_rank("B")
        and _to_float(payload.get("entry")) is not None
        and _to_float(payload.get("sl")) is not None
        and _to_float(payload.get("tp")) is not None
    )
    statistical_quant_ready = quant_ready or quant_pro_ready
    strategy_ready_without_confluence = statistical_quant_ready or gold_ready or gold_m1m5_ready or gold_order_flow_ready or btc_scalping_ready or _btc_lovable_bypass or simo_ready or strategy_pack_ready or fib_ready
    if not strategy_ready_without_confluence and not order_flow_exec_ready:
        # Task 4: skip confirmation matrix for symbol-mismatched strategies to avoid noisy logs.
        # order_flow_exec_ready has its own dedicated CM check above — skip the general check.
        _sym_check = str(payload.get("symbol") or "")
        _bs_check = str(payload.get("broker_symbol") or "")
        if role == "ENTRY" and allowed_for_symbol(strategy, _sym_check, _bs_check):
            _setup_score_for_matrix = _to_float(payload.get("setup_score")) or 0.0
            _cm = _confirmation_matrix(
                _sym_check,
                strategy,
                smc_score,
                mtfa_score,
                _setup_score_for_matrix,
                rr,
            )
            if _cm["hard_block"]:
                failed.append("CONFIRMATION_MATRIX_HARD_BLOCK")
                log.info(
                    "[SETUP_HUNTER_REJECT] symbol=%s strategy=%s reason=CONFIRMATION_MATRIX_HARD_BLOCK",
                    _sym_check, strategy,
                )
    trend_strategy = strategy == "TREND_CONTINUATION_BREAKDOWN"
    m15_pass = bool(payload.get("m15_confirmation") or payload.get("smc_m15_confirmation") or payload.get("m15_confirmation_status") == "PASS")
    m1_pass = bool(payload.get("m1_entry_confirmation") or payload.get("smc_m1_entry_confirmation") or payload.get("m1_trigger_status") == "PASS")
    if trend_strategy and m1_pass and m15_pass and _same_confirmed_direction(payload) and str(payload.get("resolved_direction") or payload.get("signal") or "").upper() not in {"BUY", "SELL"}:
        failed.append("DIRECTION_RESOLVER_FAIL")
    if not m15_pass and not strategy_ready_without_confluence and not order_flow_exec_ready:
        failed.append("WAITING_FOR_M15_CONFIRMATION")
    if not m1_pass and not strategy_ready_without_confluence and not order_flow_exec_ready:
        failed.append("WAITING_FOR_M1_TRIGGER")
    if str(payload.get("safety_guard_status") or "").upper() != "PASS":
        failed.append(str(payload.get("safety_guard_reason") or "SAFETY_GUARD_BLOCK"))
    if trend_strategy:
        if (_to_float(payload.get("trend_continuation_score")) or 0.0) < 80:
            failed.append("LOW_SETUP_SCORE")
    grade_ok = _grade_rank(str(payload.get("big_setup_grade") or "")) >= _grade_rank("B")
    setup_score = _to_float(payload.get("big_setup_score")) or _to_float(payload.get("setup_score")) or 0.0
    strict = bool(payload.get("m15_confirmation") or payload.get("smc_m15_confirmation")) and bool(payload.get("m1_entry_confirmation") or payload.get("smc_m1_entry_confirmation"))
    if not (strategy_ready_without_confluence or order_flow_exec_ready or grade_ok or (setup_score >= 75 and strict)):
        failed.append("BIG_SETUP_GRADE_BELOW_B")
    # §4.1–4.3: HERMES entry gates (feature-flagged; default off)
    # Use `is True` to guard against MagicMock auto-attributes in tests.
    if getattr(settings, "hermes_entry_gates_enabled", False) is True:
        _direction = str(payload.get("signal") or "").upper()
        if _direction in {"BUY", "SELL"}:
            _gate_fails = _hermes_entry_gates(strategy, _direction, payload)
            if _gate_fails:
                failed.extend(_gate_fails)
                log.info(
                    "[HERMES_ENTRY_GATE_BLOCK] symbol=%s strategy=%s direction=%s gates=%s",
                    payload.get("symbol"), strategy, _direction, ",".join(_gate_fails),
                )
    # §4.0: Setup Quality Tier (feature-flagged; default off)
    if getattr(settings, "hermes_setup_tier_enabled", False) is True:
        _direction = str(payload.get("signal") or "").upper()
        if _direction in {"BUY", "SELL"}:
            _tier_result = _hermes_tier(strategy, _direction, payload)
            _tier_fails = _tier_to_gate_failures(_tier_result)
            if _tier_fails:
                failed.extend(_tier_fails)
                log.info(
                    "[HERMES_TIER_BLOCK] symbol=%s strategy=%s direction=%s tier=%s reason=%s",
                    payload.get("symbol"), strategy, _direction,
                    _tier_result.get("tier"), _tier_result.get("reason"),
                )
            else:
                payload["hermes_setup_tier"] = _tier_result.get("tier")
                payload["hermes_setup_bonus_count"] = _tier_result.get("bonus_count")
    return list(dict.fromkeys(failed))


def _same_confirmed_direction(payload: dict) -> bool:
    m1_direction = str(payload.get("m1_trigger_direction") or _direction_from_reason(payload.get("m1_trigger_type") or payload.get("m1_trigger_reason")) or "").upper()
    m15_direction = str(payload.get("m15_confirmation_direction") or _direction_from_reason(payload.get("m15_confirmation_type") or payload.get("m15_confirmation_reason")) or "").upper()
    return m1_direction in {"BUY", "SELL"} and m1_direction == m15_direction


def _direction_from_reason(reason: object) -> str | None:
    text = str(reason or "").upper()
    if any(part in text for part in ("BULLISH", "ABOVE", "HIGHER_HIGH", "HIGHER_LOW", "RESISTANCE")):
        return "BUY"
    if any(part in text for part in ("BEARISH", "BELOW", "LOWER_LOW", "LOWER_HIGH", "SUPPORT")):
        return "SELL"
    return None


def _near_miss_reason(failed: list[str], role: str) -> str | None:
    if not failed:
        return None
    for reason in failed:
        if reason in NEAR_MISS_CATEGORIES:
            return reason
    if role != "ENTRY":
        return "STRATEGY_OBSERVER_ONLY"
    return failed[0]


def _candidate_wait_reason(candidate: dict) -> str:
    strategy = str(candidate.get("best_strategy") or "").upper()
    field_map = {
        "GOLD_LIQUIDITY_HUNTER_PRO": "gold_liquidity_reason",
        "GOLD_M1_M5_EMA_SWEEP_SCALPER": "gold_m1m5_scalper_reason",
        "GOLD_ORDER_FLOW_CVD_VWAP": "gold_order_flow_reason",
        "ORDER_FLOW_EXECUTION_AGENT": "order_flow_execution_agent_reason",
        "EUR_EMA_RSI_ATR_CROSSOVER": "eur_ema_rsi_atr_reason",
    }
    field = field_map.get(strategy)
    if field:
        reason = candidate.get(field)
        if reason:
            return str(reason)
    failed = candidate.get("failed_gates") or []
    return str(failed[0]) if failed else "WAIT"


def _log_gold_candidate(symbol: str, strategy: str, candidate: dict) -> None:
    direction = candidate.get("direction")
    if direction in {"BUY", "SELL"}:
        log.info(
            "[GOLD_CANDIDATE] strategy=%s direction=%s entry=%s sl=%s tp=%s rr=%s score=%s grade=%s",
            strategy, direction,
            candidate.get("entry"), candidate.get("sl"), candidate.get("tp"), candidate.get("rr"),
            candidate.get("edge_score"), candidate.get("grade"),
        )
    else:
        reason = _candidate_wait_reason(candidate)
        log.info("[GOLD_CANDIDATE] strategy=%s decision=WAIT reason=%s", strategy, reason)


def _log_eur_candidate(symbol: str, candidate: dict) -> None:
    direction = candidate.get("direction")
    if direction in {"BUY", "SELL"}:
        log.info(
            "[EUR_CANDIDATE] strategy=EUR_EMA_RSI_ATR_CROSSOVER direction=%s entry=%s sl=%s tp=%s rr=%s score=%s grade=%s",
            direction,
            candidate.get("entry"), candidate.get("sl"), candidate.get("tp"), candidate.get("rr"),
            candidate.get("edge_score"), candidate.get("grade"),
        )
    else:
        reason = candidate.get("eur_ema_rsi_atr_reason") or candidate.get("near_miss_reason") or "WAIT"
        log.info("[EUR_CANDIDATE] strategy=EUR_EMA_RSI_ATR_CROSSOVER decision=WAIT reason=%s", reason)


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def _grade_rank(grade: str) -> int:
    return {"A_PLUS": 4, "A+": 4, "A": 3, "B": 2, "C": 1, "D": 0, "UNKNOWN": 0, "": 0}.get(str(grade).upper(), 0)


def _session_quality(items: list[dict]) -> str:
    if any(item.get("demo_eligible") for item in items):
        return "EDGE_READY"
    if any(item.get("near_miss_reason") for item in items):
        return "NEAR_MISS"
    return "NO_EDGE"


def _first_number(*values: object) -> float | None:
    for value in values:
        out = _to_float(value)
        if out is not None:
            return out
    return None


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
