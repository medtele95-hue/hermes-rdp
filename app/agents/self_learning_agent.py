from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable


STRATEGY_PRIORITY = [
    "BREAKOUT_RETEST",
    "TREND_CONTINUATION_BREAKDOWN",
    "CRT_TBS_REVERSAL",
    "AMD_FVG_IFVG_REVERSAL",
    "FIB_OTE_RETEST",
    "QUANT_STATISTICAL_PULLBACK",
    "QUANT_PRO_REGIME_SWITCHING",
    "EMA_PULLBACK",
]


class SelfLearningAgent:
    def __init__(self) -> None:
        self.strategy_params: dict[str, dict[str, float]] = defaultdict(self._default_params)
        self.performance: dict[str, dict[str, float]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    def choose_strategy(self, signals: Iterable[dict], markov_prediction: dict) -> dict:
        adjusted = list(signals)
        actionable = [item for item in adjusted if item.get("signal") in {"BUY", "SELL"}]
        if not actionable:
            return max(adjusted, key=lambda item: item.get("confidence", 0.0))

        state_signal = markov_prediction.get("signal")
        if state_signal == "SKIP":
            best = max(actionable, key=lambda item: item.get("confidence", 0.0))
            best = dict(best)
            best["signal"] = "SKIP"
            best["blocked_reason"] = "MARKOV_SKIP"
            return best
        return sorted(actionable, key=lambda item: (_priority(item), -float(item.get("confidence") or 0.0)))[0]

    def adjusted_signals(self, signals: Iterable[dict]) -> list[dict]:
        return [self.apply_parameters(item) for item in signals]

    def apply_parameters(self, signal: dict) -> dict:
        strategy = str(signal.get("strategy") or "UNKNOWN")
        params = self.strategy_params[strategy]
        out = dict(signal)
        out["confidence"] = _clamp(float(out.get("confidence") or 0.0) + params["confidence_bias"], 0.0, 0.95)
        out["optimizer_params"] = dict(params)

        if out.get("signal") not in {"BUY", "SELL"}:
            return out
        entry = _to_float(out.get("entry"))
        sl = _to_float(out.get("sl"))
        tp = _to_float(out.get("tp"))
        if entry is None or sl is None or tp is None:
            return out

        sl_distance = abs(entry - sl) * params["sl_multiplier"]
        tp_distance = abs(tp - entry) * params["tp_multiplier"]
        if out["signal"] == "BUY":
            out["sl"] = entry - sl_distance
            out["tp"] = entry + tp_distance
        else:
            out["sl"] = entry + sl_distance
            out["tp"] = entry - tp_distance
        return out

    def record_paper_close(self, trade: dict) -> dict:
        strategy = str(trade.get("strategy") or "UNKNOWN")
        result = str(trade.get("result") or "").upper()
        pnl = _to_float(trade.get("pnl")) or 0.0
        params = self.strategy_params[strategy]
        perf = self.performance[strategy]

        if result == "WIN":
            perf["wins"] += 1
            params["confidence_bias"] = _clamp(params["confidence_bias"] + 0.015, -0.20, 0.15)
            params["tp_multiplier"] = _clamp(params["tp_multiplier"] + 0.025, 0.80, 1.50)
        elif result == "LOSS":
            perf["losses"] += 1
            params["confidence_bias"] = _clamp(params["confidence_bias"] - 0.030, -0.20, 0.15)
            params["sl_multiplier"] = _clamp(params["sl_multiplier"] - 0.025, 0.70, 1.00)
            params["tp_multiplier"] = _clamp(params["tp_multiplier"] - 0.015, 0.80, 1.50)

        perf["pnl"] += pnl
        total = perf["wins"] + perf["losses"]
        win_rate = perf["wins"] / total if total else 0.0
        return {
            "strategy": strategy,
            "result": result,
            "pnl": pnl,
            "wins": int(perf["wins"]),
            "losses": int(perf["losses"]),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(perf["pnl"], 6),
            "confidence_bias": params["confidence_bias"],
            "sl_multiplier": params["sl_multiplier"],
            "tp_multiplier": params["tp_multiplier"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _default_params(self) -> dict[str, float]:
        return {"confidence_bias": 0.0, "sl_multiplier": 1.0, "tp_multiplier": 1.0}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _priority(item: dict) -> int:
    strategy = str(item.get("strategy") or "")
    try:
        return STRATEGY_PRIORITY.index(strategy)
    except ValueError:
        return len(STRATEGY_PRIORITY)
