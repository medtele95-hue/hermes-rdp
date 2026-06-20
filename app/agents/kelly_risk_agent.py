from __future__ import annotations

import math
from datetime import datetime, timezone

from app.config import Settings
from app.utils.risk_math import fractional_kelly, reward_risk


class KellyRiskAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.start_equity: float | None = None
        self.daily_start_balance: float | None = None

    def evaluate(
        self,
        strategy_signal: dict,
        markov_prediction: dict,
        account: dict | None,
        open_hermes_trades: int,
        spread: float,
        symbol_specs: dict,
        max_spread: float | None = None,
    ) -> dict:
        signal = strategy_signal.get("signal")
        entry = _to_float(strategy_signal.get("entry"))
        sl = _to_float(strategy_signal.get("sl"))
        tp = _to_float(strategy_signal.get("tp"))
        rr = reward_risk(entry, sl, tp, signal)
        probability = float(markov_prediction.get("probability") or 0.0) * float(strategy_signal.get("confidence") or 0.0)
        kelly = fractional_kelly(probability, rr)
        edge = kelly
        capped_fraction = min(kelly, self.settings.max_risk_per_trade / 100.0)
        effective_max_spread = max_spread if max_spread is not None else self.settings.max_spread
        blocked = []
        lot_blocked_reason = None

        if probability <= 0:
            blocked.append("INVALID_PROBABILITY")
        elif edge <= 0:
            blocked.append("NO_POSITIVE_EDGE")
        if signal not in {"BUY", "SELL"}:
            blocked.append("NO_TRADE_SIGNAL")
        if sl is None:
            blocked.append("MISSING_SL")
            lot_blocked_reason = lot_blocked_reason or "MISSING_SL"
        if tp is None:
            blocked.append("NO_SL_TP")
        if rr < 1.5:
            blocked.append("REWARD_RISK_BELOW_1_5")
        if open_hermes_trades >= self.settings.max_open_hermes_trades:
            blocked.append("MAX_OPEN_HERMES_TRADES")
        if spread > effective_max_spread:
            blocked.append("MAX_SPREAD")
        safe_paper_mode = self.settings.paper_trading and not self.settings.demo_trading and not self.settings.allow_live_trading
        if self.settings.read_only and not safe_paper_mode:
            blocked.append("READ_ONLY")

        equity = _to_float((account or {}).get("equity")) or _to_float((account or {}).get("balance")) or 0.0
        balance = _to_float((account or {}).get("balance")) or 0.0
        if self.start_equity is None and equity > 0:
            self.start_equity = equity
        if self.daily_start_balance is None and balance > 0:
            self.daily_start_balance = balance

        drawdown_pct = self._percent_drop(self.start_equity, equity)
        daily_loss_pct = self._percent_drop(self.daily_start_balance, balance)
        if daily_loss_pct >= self.settings.max_daily_loss:
            blocked.append("MAX_DAILY_LOSS")
        if drawdown_pct >= self.settings.max_drawdown:
            blocked.append("MAX_DRAWDOWN")

        lot = self._calculate_lot(entry, sl, equity, symbol_specs)
        if lot["blocked_reason"]:
            blocked.append(lot["blocked_reason"])
            lot_blocked_reason = lot_blocked_reason or lot["blocked_reason"]
        risk_status = "APPROVED" if not blocked else "BLOCKED"
        approved_lot = lot["lot_size"] if risk_status == "APPROVED" else 0
        final_risk = lot["final_risk"] if risk_status == "APPROVED" else 0

        return {
            "symbol": strategy_signal.get("symbol"),
            "timeframe": strategy_signal.get("timeframe", "M5"),
            "status": risk_status,
            "risk_status": risk_status,
            "equity": equity,
            "risk_amount": lot["risk_amount"],
            "final_risk": final_risk,
            "final_risk_amount": lot.get("final_risk_amount") if risk_status == "APPROVED" else 0,
            "entry": entry,
            "sl": sl,
            "sl_distance": lot["sl_distance"],
            "tick_value": lot["tick_value"],
            "tick_size": lot["tick_size"],
            "contract_size": lot["contract_size"],
            "volume_min": lot["volume_min"],
            "volume_step": lot["volume_step"],
            "volume_max": lot["volume_max"],
            "raw_lot": lot["raw_lot"],
            "calculated_lot": lot["raw_lot"],
            "normalized_lot": lot["normalized_lot"],
            "probability": probability,
            "edge": edge,
            "reward_risk": rr,
            "kelly_fraction": kelly,
            "fractional_kelly": capped_fraction,
            "approved_lot": approved_lot,
            "lot_size": approved_lot,
            "lot_adjustment": lot["lot_adjustment"],
            "lot_blocked_reason": lot_blocked_reason,
            "max_risk_per_trade": self.settings.max_risk_per_trade,
            "max_daily_loss": self.settings.max_daily_loss,
            "max_drawdown": self.settings.max_drawdown,
            "daily_loss_pct": daily_loss_pct,
            "drawdown_pct": drawdown_pct,
            "open_hermes_trades": open_hermes_trades,
            "spread": spread,
            "max_spread": effective_max_spread,
            "blocked_reason": ", ".join(blocked) if blocked else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _calculate_lot(self, entry: float | None, sl: float | None, equity: float, symbol_specs: dict) -> dict:
        tick_value = _positive(symbol_specs.get("tick_value"))
        tick_size = _positive(symbol_specs.get("tick_size"))
        contract_size = _positive(symbol_specs.get("contract_size"))
        volume_min = _positive(symbol_specs.get("volume_min"))
        volume_step = _positive(symbol_specs.get("volume_step"))
        volume_max = _positive(symbol_specs.get("volume_max"))
        max_risk_pct = self.settings.max_risk_per_trade
        risk_amount = equity * (max_risk_pct / 100.0) if equity > 0 else None
        sl_distance = abs(entry - sl) if entry is not None and sl is not None else None
        result = {
            "risk_amount": risk_amount,
            "max_risk_pct": max_risk_pct,
            "sl_distance": sl_distance,
            "tick_value": tick_value,
            "tick_size": tick_size,
            "contract_size": contract_size,
            "volume_min": volume_min,
            "volume_step": volume_step,
            "volume_max": volume_max,
            "raw_lot": None,
            "normalized_lot": None,
            "lot_size": None,
            "final_risk": None,
            "lot_adjustment": None,
            "blocked_reason": None,
            "final_risk_amount": None,
        }

        if equity <= 0 or risk_amount is None or risk_amount <= 0:
            result["blocked_reason"] = "MISSING_EQUITY"
            return result
        if sl is None:
            result["blocked_reason"] = "MISSING_SL"
            return result
        if sl_distance is None or sl_distance <= 0:
            result["blocked_reason"] = "ZERO_SL_DISTANCE"
            return result
        if not symbol_specs.get("symbol_info_available"):
            result["blocked_reason"] = "MISSING_SYMBOL_INFO"
            return result
        if tick_value is None or tick_size is None or tick_value <= 0 or tick_size <= 0:
            result["blocked_reason"] = "INVALID_TICK_VALUE"
            return result
        if volume_min is None or volume_step is None or volume_max is None:
            result["blocked_reason"] = "MISSING_SYMBOL_INFO"
            return result

        risk_per_1_lot = (sl_distance / tick_size) * tick_value
        if not math.isfinite(risk_per_1_lot) or risk_per_1_lot <= 0:
            result["blocked_reason"] = "INVALID_TICK_VALUE"
            return result

        raw_lot = risk_amount / risk_per_1_lot
        result["raw_lot"] = raw_lot
        if not math.isfinite(raw_lot) or raw_lot <= 0:
            result["blocked_reason"] = "RAW_LOT_ZERO"
            return result

        if raw_lot < volume_min:
            min_lot_risk = risk_per_1_lot * volume_min
            if min_lot_risk <= risk_amount:
                normalized = volume_min
                result["lot_adjustment"] = "BROKER_MIN_LOT"
            else:
                result["blocked_reason"] = "MIN_LOT_EXCEEDS_RISK"
                return result
        else:
            normalized = _floor_to_step(raw_lot, volume_step)
            if normalized <= 0:
                result["blocked_reason"] = "RAW_LOT_ZERO"
                return result
            if normalized < volume_min:
                normalized = volume_min
                result["lot_adjustment"] = "BROKER_MIN_LOT"

        if normalized > volume_max:
            normalized = _floor_to_step(volume_max, volume_step)
            result["lot_adjustment"] = "BROKER_MAX_LOT"

        normalized = self._reduce_to_risk_limit(normalized, risk_per_1_lot, equity, max_risk_pct, volume_min, volume_step)
        if normalized is None or normalized <= 0:
            result["blocked_reason"] = "MIN_LOT_EXCEEDS_RISK"
            return result

        final_risk_amount = risk_per_1_lot * normalized
        final_risk_pct = (final_risk_amount / equity) * 100.0
        if final_risk_pct > max_risk_pct:
            result["blocked_reason"] = "MAX_RISK_EXCEEDED"
            return result

        result["normalized_lot"] = round(normalized, 8)
        result["lot_size"] = round(normalized, 8)
        result["final_risk"] = round(final_risk_pct, 8)
        result["final_risk_amount"] = round(final_risk_amount, 8)
        return result

    def _percent_drop(self, baseline: float | None, current: float) -> float:
        if baseline is None or baseline <= 0 or current <= 0:
            return 0.0
        return max(0.0, ((baseline - current) / baseline) * 100.0)

    def _reduce_to_risk_limit(
        self,
        lot: float,
        risk_per_1_lot: float,
        equity: float,
        max_risk_pct: float,
        volume_min: float,
        volume_step: float,
    ) -> float | None:
        current = lot
        while current >= volume_min:
            final_risk_pct = ((risk_per_1_lot * current) / equity) * 100.0
            if final_risk_pct <= max_risk_pct:
                return current
            current = _floor_to_step(current - volume_step, volume_step)
        min_lot_risk_pct = ((risk_per_1_lot * volume_min) / equity) * 100.0
        if min_lot_risk_pct <= max_risk_pct:
            return volume_min
        return None


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _positive(value: object) -> float | None:
    out = _to_float(value)
    return out if out is not None and out > 0 else None


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step
