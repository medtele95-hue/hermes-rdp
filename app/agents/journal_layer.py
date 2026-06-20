from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from app.agents.big_setup_detector import BIG_SETUP_FIELDS
from app.agents.safety_guard import SAFETY_GUARD_FIELDS
from app.agents.smc_confluence_tagger import SMC_FIELDS


RISK_DIAG_FIELDS = [
    "risk_diag_account_equity",
    "risk_diag_entry",
    "risk_diag_sl",
    "risk_diag_exit_price",
    "risk_diag_lot_size",
    "risk_diag_symbol",
    "risk_diag_broker_symbol",
    "risk_diag_point",
    "risk_diag_digits",
    "risk_diag_tick_value",
    "risk_diag_tick_size",
    "risk_diag_contract_size",
    "risk_diag_sl_distance_price",
    "risk_diag_sl_distance_points",
    "risk_diag_expected_risk_money",
    "risk_diag_expected_risk_percent",
    "risk_diag_realized_pnl",
    "risk_diag_realized_risk_percent",
    "risk_diag_mismatch_percent",
    "risk_diag_status",
]

STRATEGY_REPLACEMENT_FIELDS = [
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
    "relaxed_mode_active",
    "relaxed_reason",
    "hours_without_setup",
    "strict_threshold",
    "relaxed_threshold",
    "relaxed_trade_count_today",
    "last_relaxed_trade_result",
]


PLAN_DEFAULTS = {
    "market_structure": "UNKNOWN",
    "market_phase": "UNKNOWN",
    "support_resistance_nearby": False,
    "supply_demand_zone": "NONE",
    "trendline_channel_context": "NONE",
    "fibonacci_context": "NONE",
    "volume_context": "UNKNOWN",
    "confluence_score": 0,
    "confluence_factors": [],
}

JOURNAL_DEFAULTS = {
    "date_time": None,
    "symbol": None,
    "timeframe": "M5",
    "strategy": None,
    "setup_type": None,
    "direction": None,
    "entry": None,
    "sl": None,
    "tp": None,
    "risk_reward": None,
    "lot_size": None,
    "final_risk": None,
    "confidence": None,
    "reason_for_entry": None,
    "reason_for_skip": None,
    "exit_reason": None,
    "result": None,
    "pnl": None,
    "lesson_tag": None,
    "mistake_tag": None,
    "mtfa_status": None,
    "mtfa_score": None,
    "news_status": None,
    "previous_trade_result_for_symbol": None,
    "previous_trade_result_for_strategy": None,
    "consecutive_losses_symbol": 0,
    "consecutive_losses_symbol_strategy": 0,
    "minutes_since_last_loss": None,
    "reentry_after_sl": False,
    "mtf_structure_strategy": None,
    "mtf_structure_mode": None,
    "mtf_structure_status": None,
    "mtf_structure_direction": None,
    "h4_bias": None,
    "h4_zone": None,
    "m15_confirmation": False,
    "m15_structure_shift": None,
    "m1_entry_confirmation": False,
    "entry_price_suggestion": None,
    "sl_suggestion": None,
    "tp1_suggestion": None,
    "tp2_suggestion": None,
    "risk_reward_suggestion": None,
    "mtf_structure_score": None,
    "mtf_structure_reason": None,
    "h4_recent_support": None,
    "h4_recent_resistance": None,
    "h4_supply_zone": None,
    "h4_demand_zone": None,
    "h4_last_swing_high": None,
    "h4_last_swing_low": None,
    **{field: None for field in SMC_FIELDS},
    **{field: None for field in RISK_DIAG_FIELDS},
    **{field: None for field in SAFETY_GUARD_FIELDS},
    **{field: None for field in BIG_SETUP_FIELDS},
    **{field: None for field in STRATEGY_REPLACEMENT_FIELDS},
}

CHECKLIST_DEFAULTS = {
    "risk_ok": False,
    "spread_ok": False,
    "mtfa_tag_available": False,
    "sl_tp_valid": False,
    "lot_valid": False,
    "confidence_ok": False,
    "max_open_trades_ok": False,
}

JOURNAL_FIELD_NAMES = tuple(JOURNAL_DEFAULTS) + tuple(PLAN_DEFAULTS) + ("execution_checklist",)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_trade_plan(payload: dict) -> dict:
    market_state = str(payload.get("market_state") or payload.get("markov_state") or "").upper()
    direction = str(payload.get("signal") or payload.get("direction") or payload.get("dir") or "").upper()
    h1_bias = str(payload.get("h1_bias") or "").upper()
    m15_liquidity = bool(payload.get("m15_liquidity"))
    m5_cisd = bool(payload.get("m5_cisd"))
    mtfa_score = _int(payload.get("mtfa_score"))
    confidence = _float(payload.get("confidence"))

    factors: list[str] = []
    if payload.get("risk_status") == "APPROVED":
        factors.append("RISK_APPROVED")
    if payload.get("mtfa_status"):
        factors.append(f"MTFA_{payload.get('mtfa_status')}")
    if payload.get("mtf_structure_status"):
        factors.append(f"MTF_STRUCTURE_{payload.get('mtf_structure_status')}")
    if payload.get("h4_bias") in {"BULLISH", "BEARISH"}:
        factors.append(f"H4_{payload.get('h4_bias')}")
    if payload.get("m15_confirmation"):
        factors.append("M15_STRUCTURE_CONFIRMATION")
    if payload.get("m1_entry_confirmation"):
        factors.append("M1_ENTRY_CONFIRMATION")
    if h1_bias and h1_bias != "NEUTRAL":
        factors.append(f"H1_{h1_bias}")
    if m15_liquidity:
        factors.append("M15_LIQUIDITY")
    if m5_cisd:
        factors.append("M5_CISD")
    if confidence is not None and confidence >= 0.65:
        factors.append("CONFIDENCE_OK")

    score_parts = []
    if mtfa_score is not None:
        score_parts.append(mtfa_score)
    if confidence is not None:
        score_parts.append(int(confidence * 100 if 0 <= confidence <= 1 else confidence))
    if payload.get("risk_status") == "APPROVED":
        score_parts.append(75)
    mtf_score = _int(payload.get("mtf_structure_score"))
    if mtf_score is not None:
        score_parts.append(mtf_score)

    return {
        "market_structure": _market_structure(market_state, direction),
        "market_phase": _market_phase(market_state),
        "support_resistance_nearby": bool(payload.get("support_resistance_nearby", False)),
        "supply_demand_zone": str(payload.get("supply_demand_zone") or "NONE").upper(),
        "trendline_channel_context": str(payload.get("trendline_channel_context") or "NONE").upper(),
        "fibonacci_context": str(payload.get("fibonacci_context") or "NONE").upper(),
        "volume_context": str(payload.get("volume_context") or "UNKNOWN").upper(),
        "confluence_score": _clamp_int(round(sum(score_parts) / len(score_parts)) if score_parts else 0, 0, 100),
        "confluence_factors": factors,
    }


def build_execution_checklist(
    decision: dict,
    spread: float,
    max_spread: float | None,
    paper_symbol_max_lot: float,
    max_open_trades_ok: bool,
) -> dict:
    lot = _float(decision.get("lot_size"))
    entry = _float(decision.get("entry"))
    sl = _float(decision.get("sl"))
    tp = _float(decision.get("tp"))
    confidence = _float(decision.get("confidence")) or 0.0
    return {
        "risk_ok": decision.get("risk_status") == "APPROVED",
        "spread_ok": max_spread is None or spread <= max_spread,
        "mtfa_tag_available": decision.get("mtfa_status") is not None,
        "sl_tp_valid": entry is not None and sl is not None and tp is not None and entry != sl and entry != tp,
        "lot_valid": lot is not None and 0 < lot <= paper_symbol_max_lot,
        "confidence_ok": confidence >= 0.65,
        "max_open_trades_ok": max_open_trades_ok,
    }


def build_journal_fields(
    payload: dict,
    sample_type: str,
    reason: str | None = None,
    date_time: str | None = None,
    include_plan: bool = True,
) -> dict:
    raw_payload = payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {}
    source = {**raw_payload, **payload}
    direction = source.get("direction") or source.get("signal") or source.get("dir")
    exit_reason = source.get("exit_reason") or source.get("close_reason") or (reason if sample_type == "PAPER_CLOSE" else None)
    skip_reason = source.get("reason_for_skip") or (reason if sample_type == "PAPER_SKIP" else None)
    entry_reason = source.get("reason_for_entry") or ("PAPER_ENTRY" if sample_type == "PAPER_OPEN" else None)
    pnl = _float(source.get("pnl"))
    fields = {
        "date_time": date_time or source.get("date_time") or source.get("created_at") or now_iso(),
        "symbol": source.get("symbol"),
        "timeframe": source.get("timeframe") or "M5",
        "strategy": source.get("strategy"),
        "setup_type": source.get("setup_type") or source.get("strategy"),
        "direction": direction,
        "entry": source.get("entry"),
        "sl": source.get("sl"),
        "tp": source.get("tp"),
        "risk_reward": source.get("risk_reward") or source.get("reward_risk") or _risk_reward(source),
        "lot_size": source.get("lot_size") or source.get("lot"),
        "final_risk": source.get("final_risk"),
        "confidence": source.get("confidence_decimal") if source.get("confidence_decimal") is not None else source.get("confidence"),
        "reason_for_entry": entry_reason,
        "reason_for_skip": skip_reason,
        "exit_reason": exit_reason,
        "result": source.get("result"),
        "pnl": source.get("pnl"),
        "lesson_tag": source.get("lesson_tag") or _lesson_tag(sample_type, source.get("result"), pnl),
        "mistake_tag": source.get("mistake_tag") or _mistake_tag(skip_reason),
        "mtfa_status": source.get("mtfa_status"),
        "mtfa_score": source.get("mtfa_score"),
        "news_status": source.get("news_status"),
        "previous_trade_result_for_symbol": source.get("previous_trade_result_for_symbol"),
        "previous_trade_result_for_strategy": source.get("previous_trade_result_for_strategy"),
        "consecutive_losses_symbol": source.get("consecutive_losses_symbol", 0),
        "consecutive_losses_symbol_strategy": source.get("consecutive_losses_symbol_strategy", 0),
        "minutes_since_last_loss": source.get("minutes_since_last_loss"),
        "reentry_after_sl": bool(source.get("reentry_after_sl", False)),
        "mtf_structure_strategy": source.get("mtf_structure_strategy"),
        "mtf_structure_mode": source.get("mtf_structure_mode"),
        "mtf_structure_status": source.get("mtf_structure_status"),
        "mtf_structure_direction": source.get("mtf_structure_direction"),
        "h4_bias": source.get("h4_bias"),
        "h4_zone": source.get("h4_zone"),
        "m15_confirmation": bool(source.get("m15_confirmation", False)),
        "m15_structure_shift": source.get("m15_structure_shift"),
        "m1_entry_confirmation": bool(source.get("m1_entry_confirmation", False)),
        "entry_price_suggestion": source.get("entry_price_suggestion"),
        "sl_suggestion": source.get("sl_suggestion"),
        "tp1_suggestion": source.get("tp1_suggestion"),
        "tp2_suggestion": source.get("tp2_suggestion"),
        "risk_reward_suggestion": source.get("risk_reward_suggestion"),
        "mtf_structure_score": source.get("mtf_structure_score"),
        "mtf_structure_reason": source.get("mtf_structure_reason"),
        "h4_recent_support": source.get("h4_recent_support"),
        "h4_recent_resistance": source.get("h4_recent_resistance"),
        "h4_supply_zone": source.get("h4_supply_zone"),
        "h4_demand_zone": source.get("h4_demand_zone"),
        "h4_last_swing_high": source.get("h4_last_swing_high"),
        "h4_last_swing_low": source.get("h4_last_swing_low"),
    }
    for field in SMC_FIELDS + RISK_DIAG_FIELDS + SAFETY_GUARD_FIELDS + BIG_SETUP_FIELDS + STRATEGY_REPLACEMENT_FIELDS:
        fields[field] = source.get(field)
    if include_plan:
        fields.update(build_trade_plan(source))
    checklist = source.get("execution_checklist")
    if isinstance(checklist, dict):
        merged = dict(CHECKLIST_DEFAULTS)
        merged.update(checklist)
        fields["execution_checklist"] = merged
    return fields


def journal_payload(payload: dict) -> dict:
    return {field: payload.get(field) for field in JOURNAL_FIELD_NAMES if field in payload}


def enrich_with_journal(sample: dict, sample_type: str | None = None, reason: str | None = None) -> dict:
    out = dict(sample)
    kind = sample_type or str(out.get("sample_type") or "SETUP")
    fields = build_journal_fields(out, kind, reason or out.get("reason"))
    for key, value in fields.items():
        out.setdefault(key, value)
    return out


def calculate_performance_metrics(samples: Iterable[dict]) -> dict:
    closed = [sample for sample in samples if _is_closed_paper_trade(sample)]
    wins = [sample for sample in closed if _float(sample.get("pnl")) is not None and (_float(sample.get("pnl")) or 0.0) > 0]
    losses = [sample for sample in closed if _float(sample.get("pnl")) is not None and (_float(sample.get("pnl")) or 0.0) < 0]
    pnls = [_float(sample.get("pnl")) or 0.0 for sample in closed]
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    total = len(closed)
    total_pnl = sum(pnls)
    return {
        "trades": total,
        "win_rate": round(len(wins) / total, 4) if total else 0.0,
        "average_win": round(gross_profit / len(wins), 6) if wins else 0.0,
        "average_loss": round(sum((_float(sample.get("pnl")) or 0.0) for sample in losses) / len(losses), 6) if losses else 0.0,
        "risk_reward_avg": _average([_float(sample.get("risk_reward") or sample.get("reward_risk")) for sample in closed]),
        "expectancy": round(total_pnl / total, 6) if total else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "total_pnl": round(total_pnl, 6),
        "max_drawdown": _max_drawdown(pnls),
        "best_strategy": _best_key(closed, "strategy", highest=True),
        "worst_strategy": _best_key(closed, "strategy", highest=False),
        "best_symbol": _best_key(closed, "symbol", highest=True),
        "worst_symbol": _best_key(closed, "symbol", highest=False),
    }


def _market_structure(market_state: str, direction: str) -> str:
    if "RANGE" in market_state:
        return "RANGE"
    if "UP" in market_state or "BULL" in market_state or direction == "BUY" and "DOWN" not in market_state:
        return "UPTREND"
    if "DOWN" in market_state or "BEAR" in market_state or direction == "SELL" and "UP" not in market_state:
        return "DOWNTREND"
    return "UNKNOWN"


def _market_phase(market_state: str) -> str:
    if "BREAKOUT" in market_state or "UP" in market_state:
        return "MARKUP"
    if "DOWN" in market_state:
        return "MARKDOWN"
    if "REVERSAL" in market_state:
        return "DISTRIBUTION"
    if "RANGE" in market_state:
        return "ACCUMULATION"
    return "UNKNOWN"


def _risk_reward(payload: dict) -> float | None:
    entry = _float(payload.get("entry"))
    sl = _float(payload.get("sl"))
    tp = _float(payload.get("tp"))
    if entry is None or sl is None or tp is None:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return round(abs(tp - entry) / risk, 6)


def _lesson_tag(sample_type: str, result: object, pnl: float | None) -> str | None:
    if sample_type != "PAPER_CLOSE":
        return None
    if result == "WIN" or (pnl is not None and pnl > 0):
        return "REPEAT_VALID_SETUP"
    if result == "LOSS" or (pnl is not None and pnl < 0):
        return "REVIEW_LOSS"
    return "BREAKEVEN_REVIEW"


def _mistake_tag(reason: str | None) -> str | None:
    if not reason:
        return None
    return str(reason).upper()


def _is_closed_paper_trade(sample: dict) -> bool:
    if sample.get("sample_type") != "PAPER_CLOSE":
        return False
    if _float(sample.get("pnl")) is None:
        return False
    result = str(sample.get("result") or "").upper()
    return result in {"WIN", "LOSS"} or sample.get("close_reason") or sample.get("exit_reason")


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 6)


def _best_key(samples: list[dict], key: str, highest: bool) -> str | None:
    totals: dict[str, float] = {}
    for sample in samples:
        name = str(sample.get(key) or "")
        if not name:
            continue
        totals[name] = totals.get(name, 0.0) + (_float(sample.get("pnl")) or 0.0)
    if not totals:
        return None
    return max(totals, key=totals.get) if highest else min(totals, key=totals.get)


def _average(values: list[float | None]) -> float:
    clean = [value for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else 0.0


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))
