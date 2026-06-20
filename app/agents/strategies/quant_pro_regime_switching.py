from __future__ import annotations

from typing import Any

from app.config import Settings
from app.services.quant_pro_regime_switching import QuantProRegimeSwitching, STRATEGY


def evaluate(symbol: str, frames: dict[str, Any], context: dict | None, settings: Settings) -> dict:
    if not settings.hermes_quant_pro_enabled:
        return _wait(symbol, "QUANT_PRO_DISABLED")
    if str(settings.hermes_quant_pro_role or "").upper() != "ENTRY_STRATEGY":
        return _wait(symbol, "QUANT_PRO_ROLE_NOT_ENTRY")
    result = QuantProRegimeSwitching(settings).evaluate(symbol, frames.get("M5"))
    direction = str(result.get("direction") or "WAIT").upper()
    score = int(result.get("score") or 0)
    rr = result.get("rr")
    reason = str(result.get("reason") or "QUANT_PRO_WAIT")
    hurst_filter = result.get("quant_pro_hurst_filter") if isinstance(result.get("quant_pro_hurst_filter"), dict) else {}
    payload = {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": direction,
        "direction": direction,
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "risk_reward": rr,
        "reward_risk": rr,
        "confidence": round(score / 100.0, 4),
        "m15_confirmation": direction in {"BUY", "SELL"},
        "m1_entry_confirmation": direction in {"BUY", "SELL"},
        "m15_confirmation_status": "PASS" if direction in {"BUY", "SELL"} else "FAIL",
        "m1_trigger_status": "PASS" if direction in {"BUY", "SELL"} else "FAIL",
        "m15_confirmation_reason": "QUANT_PRO_48H_EXPLORATION",
        "m1_trigger_reason": "QUANT_PRO_48H_EXPLORATION",
        "big_setup_grade": result.get("grade"),
        "setup_hunter_grade": result.get("grade"),
        "grade": result.get("grade"),
        "quant_pro_regime": result.get("regime"),
        "quant_pro_score": score,
        "quant_pro_grade": result.get("grade"),
        "quant_pro_ols_slope": result.get("ols_slope"),
        "quant_pro_ols_r2": result.get("ols_r2"),
        "quant_pro_ols_tstat": result.get("ols_tstat"),
        "quant_pro_kalman_velocity": result.get("kalman_velocity"),
        "quant_pro_kalman_z": result.get("kalman_z"),
        "quant_pro_ou_beta": result.get("ou_beta"),
        "quant_pro_ou_tstat": result.get("ou_tstat"),
        "quant_pro_ou_half_life": result.get("ou_half_life"),
        "quant_pro_hurst": result.get("hurst"),
        "quant_pro_hurst_filter": hurst_filter,
        "quant_pro_min_trend_hurst": hurst_filter.get("min_trend_hurst"),
        "quant_pro_trend_strength": hurst_filter.get("trend_strength"),
        "quant_pro_hurst_filter_status": "PASS" if hurst_filter.get("passed") else ("BLOCK" if hurst_filter.get("block_reason") else "UNKNOWN"),
        "quant_pro_hurst_block_reason": hurst_filter.get("block_reason"),
        "quant_pro_ewma_vol": result.get("ewma_vol"),
        "quant_pro_reason": reason,
        "quant_pro_signal": direction,
        "quant_pro_no_lookahead": bool(result.get("no_lookahead")),
        "reason": reason,
    }
    if direction not in {"BUY", "SELL"}:
        payload["entry"] = None
        payload["sl"] = None
        payload["tp"] = None
        payload["risk_reward"] = None
        payload["reward_risk"] = None
        payload["confidence"] = 0.0
    return payload


def _wait(symbol: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": "WAIT",
        "direction": "WAIT",
        "entry": None,
        "sl": None,
        "tp": None,
        "risk_reward": None,
        "confidence": 0.0,
        "quant_pro_regime": "FLAT",
        "quant_pro_score": 0,
        "quant_pro_grade": "D",
        "quant_pro_signal": "WAIT",
        "quant_pro_hurst_filter": {},
        "quant_pro_hurst_filter_status": "UNKNOWN",
        "quant_pro_reason": reason,
        "quant_pro_no_lookahead": True,
        "reason": reason,
    }
