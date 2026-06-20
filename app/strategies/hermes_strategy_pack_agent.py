from __future__ import annotations

from app.logger import log
from app.strategies.candidate import validate_candidate
from app.strategies.registry import CONFIRMATION_MODULE_STRATEGIES


NAME = "HERMES_STRATEGY_PACK_AGENT"
_MIN_SCORE = 75.0
_MIN_FINAL_CONFLUENCE = 55.0
_MIN_RR = 1.5


def build_candidate(symbol: str, broker_symbol: str, signals: list[dict], context: dict | None = None) -> dict | None:
    """Wrap confirmation-style signals into one dry-run execution candidate.

    The pack never lets internal modules route under their own strategy name.
    It emits one candidate only when the internal signal already has the full
    execution contract and passes conservative thresholds.
    """
    context = context or {}
    accepted: list[dict] = []
    rejected = 0
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        internal = str(signal.get("strategy") or signal.get("setup_type") or "").upper()
        if internal not in CONFIRMATION_MODULE_STRATEGIES:
            continue
        candidate, reason = _normalize(symbol, broker_symbol, internal, signal, context)
        if candidate is None:
            rejected += 1
            log.info("[HERMES_PACK] symbol=%s internal=%s decision=WAIT reason=%s", symbol, internal, reason)
            continue
        accepted.append(candidate)
        log.info(
            "[HERMES_PACK] symbol=%s internal=%s decision=ACCEPT score=%s grade=%s rr=%s",
            symbol,
            internal,
            candidate.get("confidence"),
            candidate.get("grade"),
            candidate.get("rr"),
        )
    if not accepted:
        reason = "NO_VALID_INTERNAL_SIGNAL" if rejected == 0 else "NO_VALID_INTERNAL_SIGNAL"
        log.info("[HERMES_PACK] symbol=%s decision=WAIT reason=%s rejected=%s", symbol, reason, rejected)
        return None
    accepted.sort(key=lambda item: (_grade_rank(item.get("grade")), item.get("confidence") or 0, item.get("final_confluence_score") or 0, item.get("rr") or 0), reverse=True)
    best = accepted[0]
    log.info("[HERMES_PACK_ROUTE] symbol=%s decision=SEND_TO_SETUP_HUNTER internal=%s", symbol, best.get("setup_type"))
    return best


def _normalize(symbol: str, broker_symbol: str, internal: str, signal: dict, context: dict) -> tuple[dict | None, str]:
    direction = str(signal.get("direction") or signal.get("signal") or "WAIT").upper()
    if direction not in {"BUY", "SELL"}:
        return None, "WAIT_SIGNAL"
    score = _num(signal.get("confidence"), signal.get("score"), signal.get("setup_score"), signal.get("edge_score"))
    if score is None and internal == "QUANT_PRO_REGIME_SWITCHING":
        score = _num(signal.get("quant_pro_score"))
    if score is None and internal == "TREND_CONTINUATION_BREAKDOWN":
        score = _num(signal.get("trend_continuation_score"))
    if score is None:
        return None, "MISSING_SCORE"
    if score <= 1.0:
        score *= 100.0
    grade = str(signal.get("grade") or signal.get("big_setup_grade") or _grade(score)).upper()
    final_score = _num(signal.get("final_confluence_score"), context.get("final_confluence_score"), signal.get("smc_confluence_score"))
    final_grade = str(signal.get("final_confluence_grade") or context.get("final_confluence_grade") or _grade(final_score or 0)).upper()
    rr = _num(signal.get("rr"), signal.get("risk_reward"), signal.get("reward_risk"))
    candidate = {
        "strategy": NAME,
        "best_strategy": NAME,
        "setup_type": internal,
        "symbol": symbol,
        "broker_symbol": broker_symbol,
        "direction": direction,
        "signal": direction,
        "entry": _num(signal.get("entry")),
        "sl": _num(signal.get("sl")),
        "tp": _num(signal.get("tp")),
        "rr": rr,
        "risk_reward": rr,
        "confidence": float(score),
        "edge_score": float(score),
        "setup_score": float(score),
        "grade": grade,
        "final_confluence_score": final_score,
        "final_confluence_grade": final_grade,
        "reason": str(signal.get("reason") or signal.get("blocked_reason") or internal),
        "metadata": {
            "internal_strategy": internal,
            "reasons": [value for value in (signal.get("reason"), signal.get("blocked_reason")) if value],
        },
        "mode": "ACTIVE_EXECUTION",
        "route_allowed": True,
        "demo_eligible": True,
    }
    for reason in _threshold_reasons(candidate):
        return None, reason
    valid, reason, field = validate_candidate(candidate)
    if not valid:
        return None, f"{reason}_{field}"
    return candidate, "PASS"


def _threshold_reasons(candidate: dict) -> list[str]:
    reasons: list[str] = []
    if (candidate.get("confidence") or 0) < _MIN_SCORE:
        reasons.append("SCORE_TOO_LOW")
    if _grade_rank(candidate.get("grade")) < _grade_rank("B"):
        reasons.append("GRADE_BELOW_B")
    if (candidate.get("rr") or 0) < _MIN_RR:
        reasons.append("RR_TOO_LOW")
    if (candidate.get("final_confluence_score") or 0) < _MIN_FINAL_CONFLUENCE:
        reasons.append("FINAL_CONFLUENCE_TOO_LOW")
    if _grade_rank(candidate.get("final_confluence_grade")) < _grade_rank("C"):
        reasons.append("FINAL_CONFLUENCE_GRADE_TOO_LOW")
    return reasons


def _num(*values: object) -> float | None:
    for value in values:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def _grade_rank(grade: object) -> int:
    return {"A_PLUS": 4, "A+": 4, "A": 3, "B": 2, "C": 1, "D": 0}.get(str(grade or "").upper(), 0)
