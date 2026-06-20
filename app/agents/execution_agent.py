from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings


class ExecutionAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decision(self, market_state: dict, markov_prediction: dict, strategy_signal: dict, risk: dict) -> dict:
        approved = risk.get("status") == "APPROVED" and strategy_signal.get("signal") in {"BUY", "SELL"}
        decision = "ENTER_ANALYSIS_ONLY" if approved else "WAIT_ANALYSIS_ONLY"
        if self.settings.read_only:
            decision = "ENTER_ANALYSIS_ONLY" if strategy_signal.get("signal") in {"BUY", "SELL"} else "WAIT_ANALYSIS_ONLY"

        return {
            "symbol": strategy_signal.get("symbol"),
            "timeframe": strategy_signal.get("timeframe", "M5"),
            "market_state": market_state.get("state"),
            "markov_probability": markov_prediction.get("probability"),
            "strategy": strategy_signal.get("strategy"),
            "signal": strategy_signal.get("signal"),
            "confidence": strategy_signal.get("confidence"),
            "risk_status": risk.get("status"),
            "kelly_fraction": risk.get("fractional_kelly"),
            "raw_lot": risk.get("raw_lot"),
            "calculated_lot": risk.get("calculated_lot"),
            "approved_lot": risk.get("approved_lot"),
            "lot_size": risk.get("lot_size"),
            "final_risk": risk.get("final_risk"),
            "reward_risk": risk.get("reward_risk"),
            "learning_params": strategy_signal.get("learning_params"),
            "lot_blocked_reason": risk.get("lot_blocked_reason"),
            "lot_adjustment": risk.get("lot_adjustment"),
            "entry": strategy_signal.get("entry"),
            "sl": strategy_signal.get("sl"),
            "tp": strategy_signal.get("tp"),
            "decision": decision,
            "reason": "Real analysis based on MT5 candles",
            "blocked_reason": risk.get("blocked_reason") or strategy_signal.get("blocked_reason"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
