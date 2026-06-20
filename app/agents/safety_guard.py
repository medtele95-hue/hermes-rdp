from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.logger import log
from app.utils.throttle import should_emit


SAFETY_GUARD_FIELDS = [
    "safety_guard_status",
    "safety_guard_reason",
    "safety_guard_rules_triggered",
    "safety_guard_action",
    "safety_guard_local_hour",
    "safety_guard_is_weekend",
    "safety_guard_timezone",
]


class SafetyGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        decision: dict,
        latest_risk_diag: dict | None = None,
        now: datetime | None = None,
    ) -> dict:
        local_dt = _decision_local_dt(decision, self.settings) or (now or datetime.now(timezone.utc)).astimezone(_zone(self.settings.report_timezone))
        symbol = str(decision.get("symbol") or "").upper()
        strategy = str(decision.get("strategy") or "").upper()
        rules: list[str] = []
        reasons: list[str] = []
        caution = False

        if not self.settings.safety_guard_enabled:
            result = _result("PASS", "SAFETY_GUARD_DISABLED", [], local_dt, self.settings.report_timezone)
            _log_result(result)
            return result

        is_btc = symbol.startswith("BTCUSD")
        is_weekend = _btc_weekend_window(local_dt)
        local_hour = local_dt.hour
        time_gate_status = str(decision.get("time_gate_status") or "").upper()
        time_gate_reason = str(decision.get("time_gate_reason") or "")

        if time_gate_status == "BLOCK" and time_gate_reason not in {"", "TIME_GATE_PASS"}:
            rules.append(time_gate_reason)
            reasons.append(time_gate_reason)
        elif not time_gate_status:
            if is_btc and self.settings.btc_weekend_analysis_only and is_weekend:
                rules.append("BTC_WEEKEND_ANALYSIS_ONLY")
                reasons.append("BTC_WEEKEND_ANALYSIS_ONLY")

            if is_btc and self.settings.bad_hour_analysis_only and local_hour in self.settings.int_set(self.settings.btc_bad_hours_local):
                rules.append("BTC_BAD_HOUR_ANALYSIS_ONLY")
                reasons.append("BTC_BAD_HOUR_ANALYSIS_ONLY")

        btc_caution = time_gate_status != "PASS" and is_btc and local_hour in self.settings.int_set(self.settings.btc_caution_hours_local)
        if btc_caution:
            caution = True
            rules.append("BTC_CAUTION_HOUR")

        ema_weak = False
        if strategy == "EMA_PULLBACK" and self.settings.ema_pullback_require_extra_confirmation:
            mtfa_fail = str(decision.get("mtfa_status") or "").upper() == "FAIL"
            mtf_fail = str(decision.get("mtf_structure_status") or "").upper() == "FAIL"
            if self.settings.ema_pullback_block_if_mtfa_and_mtf_fail and mtfa_fail and mtf_fail:
                ema_weak = True
                rules.append("EMA_PULLBACK_WEAK_CONTEXT")
                reasons.append("EMA_PULLBACK_WEAK_CONTEXT")
            smc_score = _float(decision.get("smc_confluence_score"))
            if smc_score is None or smc_score < self.settings.ema_pullback_min_smc_score:
                ema_weak = True
                rules.append("EMA_PULLBACK_LOW_SMC_SCORE")
                reasons.append("EMA_PULLBACK_LOW_SMC_SCORE")
            previous_symbol_strategy = str(
                decision.get("previous_trade_result_for_symbol_strategy")
                or decision.get("previous_trade_result_for_strategy")
                or ""
            ).upper()
            if self.settings.ema_pullback_block_after_symbol_strategy_loss and previous_symbol_strategy == "LOSS":
                ema_weak = True
                rules.append("EMA_PULLBACK_AFTER_LOSS_BLOCKED")
                reasons.append("EMA_PULLBACK_AFTER_LOSS_BLOCKED")
            if not bool(decision.get("m15_confirmation")) and not bool(decision.get("smc_m5_confirmation")):
                ema_weak = True
                rules.append("EMA_PULLBACK_NO_M15_M5_CONFIRMATION")
                reasons.append("EMA_PULLBACK_NO_M15_M5_CONFIRMATION")

        latest_risk_diag = latest_risk_diag or {}
        latest_realized = _float(latest_risk_diag.get("risk_diag_realized_risk_percent"))
        latest_mismatch = _float(latest_risk_diag.get("risk_diag_mismatch_percent"))
        if latest_realized is not None and latest_realized > self.settings.risk_diag_max_realized_risk_percent:
            rules.append("RISK_REALIZED_RISK_TOO_HIGH")
            reasons.append("RISK_REALIZED_RISK_TOO_HIGH")
        if latest_mismatch is not None and abs(latest_mismatch) > self.settings.risk_diag_max_mismatch_abs_percent:
            caution = True
            rules.append("RISK_DIAG_MISMATCH_CAUTION")
            if btc_caution or strategy == "EMA_PULLBACK" or any(rule.startswith("BTC_BAD") for rule in rules):
                rules.append("RISK_DIAG_MISMATCH_BLOCK")
                reasons.append("RISK_DIAG_MISMATCH_BLOCK")

        if reasons:
            result = _result("BLOCK", reasons[0], rules, local_dt, self.settings.report_timezone)
            _log_result(result)
            return result
        if caution:
            result = _result("CAUTION", "CAUTION_CONTEXT", rules, local_dt, self.settings.report_timezone)
            _log_result(result)
            return result
        result = _result("PASS", "SAFETY_GUARD_PASS", rules, local_dt, self.settings.report_timezone)
        _log_result(result)
        return result


def _result(status: str, reason: str, rules: list[str], local_dt: datetime, timezone_name: str) -> dict:
    return {
        "safety_guard_status": status,
        "safety_guard_reason": reason,
        "safety_guard_rules_triggered": list(dict.fromkeys(rules)),
        "safety_guard_action": "SKIP_ANALYSIS_ONLY" if status == "BLOCK" else "ALLOW_PAPER_OPEN",
        "safety_guard_local_hour": local_dt.hour,
        "safety_guard_is_weekend": _btc_weekend_window(local_dt),
        "safety_guard_timezone": timezone_name,
    }


def _btc_weekend_window(local_dt: datetime) -> bool:
    weekday = local_dt.weekday()
    if weekday == 4:
        return local_dt.hour >= 21
    if weekday == 5:
        return True
    if weekday == 6:
        return local_dt.hour < 22
    return False


def _decision_local_dt(decision: dict, settings: Settings) -> datetime | None:
    value = decision.get("casablanca_time") or decision.get("utc_time")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_zone(getattr(settings, "timezone_local", None) or settings.report_timezone))
    except ValueError:
        return None


def _log_result(result: dict) -> None:
    status = result.get("safety_guard_status")
    reason = result.get("safety_guard_reason")
    if status == "PASS" and not should_emit("SAFETY_GUARD_PASS"):
        return
    log.info("[SAFETY_GUARD] status=%s reason=%s", status, reason)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
