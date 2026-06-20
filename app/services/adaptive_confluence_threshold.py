from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# §4.4 / v1.4 — strategy class sets for per-strategy pass thresholds
# BTC_SCALPING_AGENT is excluded: it has its own route bypass (_old_btc_route_bypass)
_ORDER_FLOW_NATIVE = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_ORDER_FLOW_CVD_VWAP",
})
_SMC_NATIVE = frozenset({
    "SIMO_ATM_BREAKOUT",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
})
_TH_OF = 58.0       # ORDER_FLOW_NATIVE pass threshold
_TH_SMC = 65.0      # SMC_NATIVE pass threshold


@dataclass(frozen=True)
class SymbolConfluencePolicy:
    min_confluence: float
    topdown_min: float
    m15_required: bool
    m1_required: bool
    reason: str


def evaluate_adaptive_confluence(symbol: str, decision: dict, gates: dict, settings: Any) -> dict:
    enabled = bool(getattr(settings, "hermes_adaptive_confluence_enabled", False))
    policy = symbol_policy(symbol, settings)
    # §4.4: per-strategy threshold overrides the symbol policy
    strategy = str(decision.get("strategy_id") or decision.get("strategy") or decision.get("name") or "").upper()
    effective_min = _strategy_min_confluence(strategy, policy)
    components = confluence_components(decision, gates)
    computed_score = weighted_confluence_score(components)
    explicit_score = _number(decision.get("final_confluence_score") or decision.get("adaptive_confluence_score"))
    final_score = explicit_score if explicit_score is not None else computed_score
    hard_avoid = float(getattr(settings, "hermes_hard_avoid_confluence", 45.0))

    top_down_decision = str(gates.get("top_down_decision") or "").upper()
    top_down_score = _number(gates.get("top_down_score")) or components["top_down_score"]["value"]
    rr_valid = bool(components["rr_valid"]["passed"])
    spread_ok = bool(components["spread_ok"]["passed"])
    sl_tp_valid = bool(gates.get("sl_tp_valid"))
    m15_pass = bool(components["m15_confirmation"]["passed"])
    m1_pass = bool(components["m1_trigger"]["passed"])
    fallback_mode = bool(getattr(settings, "hermes_demo_topdown_fallback_mode", False))

    status = "PASS"
    reason = policy.reason
    threshold_pass = final_score >= effective_min

    if not enabled:
        status = "DISABLED"
        reason = "ADAPTIVE_CONFLUENCE_DISABLED"
        threshold_pass = True
    elif final_score < hard_avoid:
        status = "BLOCK"
        reason = "ADAPTIVE_CONFLUENCE_TOO_LOW"
    elif top_down_decision == "AVOID":
        status = "BLOCK"
        reason = "TOP_DOWN_READER_BLOCK"
    elif not rr_valid:
        status = "BLOCK"
        reason = "INVALID_RR"
    elif not spread_ok:
        status = "BLOCK"
        reason = "SPREAD_FAIL"
    elif not sl_tp_valid:
        status = "BLOCK"
        reason = "INVALID_SL_TP"
    elif top_down_score < policy.topdown_min and not fallback_mode:
        status = "BLOCK"
        reason = "TOPDOWN_BELOW_SYMBOL_MIN"
    elif policy.m15_required and not m15_pass and not fallback_mode:
        status = "BLOCK"
        reason = "M15_REQUIRED"
    elif policy.m1_required and not m1_pass and not fallback_mode:
        status = "BLOCK"
        reason = "M1_REQUIRED"
    elif not policy.m1_required and not m1_pass and final_score < 70 and not fallback_mode:
        status = "BLOCK"
        reason = "GOLD_M1_WAIT_REQUIRES_70"
    elif not threshold_pass:
        status = "BLOCK"
        reason = "BELOW_SYMBOL_THRESHOLD"

    return {
        "adaptive_confluence_enabled": enabled,
        "final_confluence_score": round(final_score, 4),
        "computed_confluence_score": round(computed_score, 4),
        "symbol_min_confluence": effective_min,
        "symbol_topdown_min": policy.topdown_min,
        "m15_required": policy.m15_required,
        "m1_required": policy.m1_required,
        "confluence_threshold_pass": bool(status == "PASS" and threshold_pass),
        "confluence_threshold_reason": reason,
        "confluence_components": components,
        "confluence_mode": "ADAPTIVE",
        "status": status,
        "block_reason": reason if status == "BLOCK" else None,
    }


def symbol_policy(symbol: str, settings: Any) -> SymbolConfluencePolicy:
    normalized = _normalize_symbol(symbol)
    default_min = float(getattr(settings, "hermes_default_min_confluence", 60.0))
    if normalized.startswith("BTCUSD"):
        return SymbolConfluencePolicy(
            float(getattr(settings, "btcusd_min_confluence", 65.0)),
            float(getattr(settings, "btcusd_topdown_min", 65.0)),
            bool(getattr(settings, "btcusd_m15_required", True)),
            bool(getattr(settings, "btcusd_m1_required", True)),
            "BTCUSD_ADAPTIVE_THRESHOLD",
        )
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return SymbolConfluencePolicy(
            float(getattr(settings, "gold_min_confluence", 55.0)),
            float(getattr(settings, "gold_topdown_min", 55.0)),
            bool(getattr(settings, "gold_m15_required", True)),
            bool(getattr(settings, "gold_m1_required", False)),
            "GOLD_ADAPTIVE_THRESHOLD",
        )
    if normalized.startswith("EURUSD"):
        return SymbolConfluencePolicy(
            float(getattr(settings, "eurusd_min_confluence", 60.0)),
            float(getattr(settings, "eurusd_topdown_min", 60.0)),
            bool(getattr(settings, "eurusd_m15_required", True)),
            bool(getattr(settings, "eurusd_m1_required", True)),
            "EURUSD_ADAPTIVE_THRESHOLD",
        )
    return SymbolConfluencePolicy(default_min, default_min, True, True, "DEFAULT_ADAPTIVE_THRESHOLD")


def confluence_components(decision: dict, gates: dict) -> dict[str, dict]:
    top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
    smc_status = str(decision.get("smc_confluence_status") or decision.get("smc_status") or gates.get("smc_status") or "").upper()
    mtfa_status = str(decision.get("mtfa_status") or gates.get("mtfa_status") or "").upper()
    quant_score = _number(decision.get("quant_score"))
    quant_pro_score = _number(decision.get("quant_pro_score"))
    acceleration_status = str(decision.get("acceleration_bands_confirmation") or decision.get("acceleration_bands_status") or "").upper()
    volume_status = str(decision.get("volume_profile_confirmation") or decision.get("volume_profile_status") or "").upper()
    rr = _number(gates.get("rr") if gates.get("rr") is not None else decision.get("reward_risk") or decision.get("risk_reward"))
    spread_ok = bool(gates.get("spread_ok"))
    m15_pass = bool(gates.get("m15_confirmation_pass"))
    m1_pass = bool(gates.get("m1_trigger_pass"))
    return {
        "top_down_score": _component(_score_number(top_down.get("entry_readiness_score") or gates.get("top_down_score")), True),
        "smc_score": _component(_score_number(decision.get("smc_score") or decision.get("smc_confluence_score"), smc_status), smc_status != "FAIL"),
        "mtfa_score": _component(_score_number(decision.get("mtfa_score"), mtfa_status), mtfa_status != "FAIL"),
        "m15_confirmation": _component(100.0 if m15_pass else 0.0, m15_pass),
        "m1_trigger": _component(100.0 if m1_pass else 0.0, m1_pass),
        "rr_valid": _component(100.0 if rr is not None and rr >= 2.0 else (75.0 if rr is not None and rr >= 1.5 else 0.0), bool(rr is not None and rr >= 1.5)),
        "spread_ok": _component(100.0 if spread_ok else 0.0, spread_ok),
        "acceleration_bands_confirmation": _component(_optional_confirmation_score(acceleration_status), acceleration_status == "PASS"),
        "volume_profile_confirmation": _component(_optional_confirmation_score(volume_status), volume_status == "PASS"),
        "quant_confirmation": _component(max(quant_score or 0.0, quant_pro_score or 0.0), bool((quant_score or 0.0) >= 75 or (quant_pro_score or 0.0) >= 75)),
    }


def weighted_confluence_score(components: dict[str, dict]) -> float:
    weights = {
        "top_down_score": 25.0,
        "smc_score": 15.0,
        "mtfa_score": 15.0,
        "m15_confirmation": 10.0,
        "m1_trigger": 10.0,
        "rr_valid": 15.0,
        "spread_ok": 10.0,
    }
    score = sum((components[key]["value"] / 100.0) * weight for key, weight in weights.items())
    for optional_key in ("acceleration_bands_confirmation", "volume_profile_confirmation"):
        value = components.get(optional_key, {}).get("value")
        if value is not None:
            score += (float(value) / 100.0) * 2.5
    quant_value = components.get("quant_confirmation", {}).get("value") or 0.0
    if quant_value > 0:
        score += (float(quant_value) / 100.0) * 5.0
    return max(0.0, min(100.0, score))


def _component(value: float | None, passed: bool) -> dict:
    return {"value": round(float(value or 0.0), 4), "passed": bool(passed)}


def _score_number(value: Any, status: str = "") -> float:
    number = _number(value)
    if number is not None:
        return max(0.0, min(100.0, number))
    if status == "PASS":
        return 75.0
    if status == "FAIL":
        return 35.0
    return 50.0


def _optional_confirmation_score(status: str) -> float | None:
    if not status:
        return None
    if status == "PASS":
        return 100.0
    if status in {"FAIL", "BLOCK"}:
        return 0.0
    return 50.0


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("#", "")


def _strategy_min_confluence(strategy: str, policy: SymbolConfluencePolicy) -> float:
    """§4.4: Return the effective minimum confluence threshold for a strategy.

    ORDER_FLOW_NATIVE uses TH_OF (58) — lower bound since SMC/MTFA are clamped to ≥0.
    SMC_NATIVE uses TH_SMC (65) — higher bar since structure gates are meaningful.
    Everything else falls back to the symbol policy threshold.
    """
    if strategy in _ORDER_FLOW_NATIVE:
        return _TH_OF
    if strategy in _SMC_NATIVE:
        return _TH_SMC
    return policy.min_confluence
