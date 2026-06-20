from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

import pandas as pd

from app.agents.big_setup_detector import BigSetupDetector
from app.agents.journal_layer import RISK_DIAG_FIELDS, build_execution_checklist, build_journal_fields, journal_payload
from app.agents.safety_guard import SafetyGuard
from app.config import Settings
from app.logger import log


class PaperTradingAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.open_trades: Dict[str, list[dict]] = {}
        self.paper_open_duplicate_count = 0
        self.open_duplicates_by_symbol: dict[str, int] = {}
        self.last_result_by_symbol: Dict[str, str | None] = {}
        self.last_result_by_strategy: Dict[str, str | None] = {}
        self.last_close_reason_by_symbol: Dict[str, str | None] = {}
        self.last_close_reason_by_strategy: Dict[str, str | None] = {}
        self.last_loss_at_by_symbol: Dict[str, str] = {}
        self.last_loss_at_by_symbol_strategy: Dict[tuple[str, str], str] = {}
        self.consecutive_losses_by_symbol: Dict[str, int] = {}
        self.consecutive_losses_by_symbol_strategy: Dict[tuple[str, str], int] = {}
        self.latest_risk_diag_by_symbol_strategy: Dict[tuple[str, str], dict] = {}
        self.recovery_reconciliation_completed = True
        self.safety_guard = SafetyGuard(settings)
        self.big_setup_detector = BigSetupDetector()

    @property
    def enabled(self) -> bool:
        return self.settings.paper_trading and not self.settings.demo_trading and not self.settings.allow_live_trading

    def reset_on_startup(self) -> None:
        self.open_trades.clear()
        self.paper_open_duplicate_count = 0
        self.open_duplicates_by_symbol = {}
        self.last_result_by_symbol.clear()
        self.last_result_by_strategy.clear()
        self.last_close_reason_by_symbol.clear()
        self.last_close_reason_by_strategy.clear()
        self.last_loss_at_by_symbol.clear()
        self.last_loss_at_by_symbol_strategy.clear()
        self.consecutive_losses_by_symbol.clear()
        self.consecutive_losses_by_symbol_strategy.clear()
        self.recovery_reconciliation_completed = False
        if self.enabled:
            log.info("[PAPER] Reset in-memory paper open positions on startup")

    def recover_open_trades(self, rows: list[dict]) -> dict:
        loaded = 0
        invalid = 0
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            trade = self._recovered_open_trade(row)
            if not trade:
                invalid += 1
                continue
            symbol = str(trade.get("symbol") or "")
            by_symbol.setdefault(symbol, []).append(trade)
            loaded += 1
            log.info(
                "[PAPER_RECOVERY] loaded open paper trade symbol=%s id=%s dir=%s entry=%s sl=%s tp=%s",
                symbol,
                trade.get("paper_trade_id"),
                trade.get("direction"),
                trade.get("entry"),
                trade.get("sl"),
                trade.get("tp"),
            )

        self.open_trades = by_symbol
        self.open_duplicates_by_symbol = {symbol: len(trades) for symbol, trades in by_symbol.items() if len(trades) > 1}
        self.paper_open_duplicate_count = sum(count - 1 for count in self.open_duplicates_by_symbol.values())
        for symbol, count in self.open_duplicates_by_symbol.items():
            log.warning("[PAPER_RECOVERY] duplicate open rows detected symbol=%s count=%s", symbol, count)
        log.info(
            "[PAPER_RECOVERY] completed loaded=%s duplicates=%s invalid=%s",
            loaded,
            self.paper_open_duplicate_count,
            invalid,
        )
        return {
            "loaded": loaded,
            "duplicates": self.paper_open_duplicate_count,
            "invalid": invalid,
            "open_duplicates_by_symbol": dict(self.open_duplicates_by_symbol),
        }

    def mark_recovery_reconciliation_completed(self) -> None:
        self.recovery_reconciliation_completed = True

    def reconcile_recovered_trades(
        self,
        symbol: str,
        broker_symbol: str,
        tick: dict | None = None,
        symbol_specs: dict | None = None,
        equity: float | None = None,
    ) -> list[dict]:
        if not self.enabled or not self.has_open_trade(symbol):
            return []
        out = []
        for trade in list(self.open_trades.get(symbol) or []):
            out.extend(
                self._process_single_closure(
                    symbol,
                    broker_symbol,
                    None,
                    tick or {},
                    symbol_specs or {},
                    equity,
                    trade,
                    recovery=True,
                )
            )
        return out

    def process_closures(
        self,
        symbol: str,
        broker_symbol: str,
        candles: pd.DataFrame,
        tick: dict | None = None,
        symbol_specs: dict | None = None,
        equity: float | None = None,
    ) -> list[dict]:
        if not self.enabled or not self.has_open_trade(symbol):
            return []

        candle = None if candles.empty else self._latest_completed_candle(candles)
        out = []
        for trade in list(self.open_trades.get(symbol) or []):
            out.extend(self._process_single_closure(symbol, broker_symbol, candle, tick or {}, symbol_specs or {}, equity, trade))
        return out

    def _process_single_closure(
        self,
        symbol: str,
        broker_symbol: str,
        candle: pd.Series | None,
        tick: dict,
        symbol_specs: dict,
        equity: float | None,
        trade: dict,
        recovery: bool = False,
    ) -> list[dict]:
        direction = trade["direction"]
        price, fallback = self._exit_price(direction, candle, tick)
        exit_price = None
        close_reason = None
        result = None

        if price is None:
            self._log_close_check(symbol, broker_symbol, trade, tick, None, False, "NO_PRICE", fallback)
            return []

        recovery_context: dict = {}
        if recovery:
            close_reason, result, exit_price = self._recovery_breach(trade, price)
            recovery_context = self._recovery_context(close_reason, exit_price, broker_symbol)
            log.info(
                "[PAPER_RECOVERY_RECONCILE] checking symbol=%s id=%s dir=%s bid=%s ask=%s entry=%s sl=%s tp=%s",
                symbol,
                trade.get("paper_trade_id"),
                direction,
                tick.get("bid"),
                tick.get("ask"),
                trade.get("entry"),
                trade.get("sl"),
                trade.get("tp"),
            )
        elif direction == "BUY":
            if price >= trade["tp"]:
                exit_price = price
                close_reason = "TP"
                result = "WIN"
            elif price <= trade["sl"]:
                exit_price = price
                close_reason = "SL"
                result = "LOSS"
        elif direction == "SELL":
            if price <= trade["tp"]:
                exit_price = price
                close_reason = "TP"
                result = "WIN"
            elif price >= trade["sl"]:
                exit_price = price
                close_reason = "SL"
                result = "LOSS"

        if not recovery and exit_price is None and self._is_time_exit(trade):
            exit_price = price
            close_reason = "TIME_EXIT"
            result = "WIN" if self._pnl(trade, exit_price, symbol_specs) >= 0 else "LOSS"

        self._log_close_check(symbol, broker_symbol, trade, tick, price, exit_price is not None, close_reason, fallback)

        if exit_price is None:
            if recovery:
                log.info("[PAPER_RECOVERY_RECONCILE] no_close_required symbol=%s id=%s", symbol, trade.get("paper_trade_id"))
            return []
        if recovery:
            log.info(
                "[PAPER_RECOVERY_RECONCILE] close_required symbol=%s id=%s reason=%s exit_price=%s",
                symbol,
                trade.get("paper_trade_id"),
                close_reason,
                exit_price,
            )

        pnl = self._pnl(trade, exit_price, symbol_specs)
        self._log_pnl_diag(symbol, trade, exit_price, pnl, symbol_specs)
        risk_audit = self._risk_audit(symbol, trade, exit_price, pnl, symbol_specs, equity)
        risk_diag = self._risk_diagnostics(symbol, broker_symbol, trade, exit_price, pnl, symbol_specs, equity)
        self.latest_risk_diag_by_symbol_strategy[(symbol, str(trade.get("strategy") or "UNKNOWN"))] = dict(risk_diag)
        now = datetime.now(timezone.utc).isoformat()
        close_source = {
            **trade,
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "exit_price": exit_price,
            "close_reason": close_reason,
            "exit_reason": close_reason,
            "result": result,
            "pnl": pnl,
            **recovery_context,
            **risk_diag,
        }
        close_journal = self._journal_fields(close_source, "PAPER_CLOSE", close_reason, date_time=now)
        self._remember_closed_trade(symbol, str(trade.get("strategy") or "UNKNOWN"), result, close_reason, now)
        log.info(
            "[JOURNAL] close symbol=%s result=%s pnl=%s lesson=%s",
            symbol,
            result,
            pnl,
            close_journal.get("lesson_tag"),
        )
        log.info(
            "[PAPER] Trade closed symbol=%s broker_symbol=%s dir=%s result=%s pnl=%s exit_price=%s reason=%s",
            symbol,
            broker_symbol,
            direction,
            result,
            pnl,
            exit_price,
            close_reason,
        )
        return [
            {
                "table": "execution_events",
                "paper_action": "CLOSE_EVENT",
                "symbol": symbol,
                "data": {
                    "event_type": "PAPER_CLOSE",
                    "mode": "PAPER",
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "dir": direction,
                    "timeframe": "M5",
                    "paper_trade_id": trade["paper_trade_id"],
                    "setup_id": trade.get("setup_id"),
                    "magic_number": self.settings.hermes_magic_number,
                    "status": "CLOSED",
                    "entry": trade["entry"],
                    "exit_price": exit_price,
                    "sl": trade["sl"],
                    "tp": trade["tp"],
                    "lot_size": trade["lot_size"],
                    "pnl": pnl,
                    "result": result,
                    "reason": close_reason,
                    **recovery_context,
                    "strategy": trade.get("strategy"),
                    "confidence": trade.get("confidence"),
                    "final_risk": trade.get("final_risk"),
                    "reward_risk": trade.get("reward_risk"),
                    "spread": trade.get("spread"),
                    "market_state": trade.get("market_state"),
                    "markov_state": trade.get("market_state"),
                    "realized_risk_percent": risk_audit.get("realized_risk_percent"),
                    "risk_audit": risk_audit.get("risk_audit"),
                    **risk_diag,
                    **close_journal,
                    "raw_payload": self._mtfa_payload({**close_source, **close_journal, **recovery_context, **risk_diag}),
                    "created_at": now,
                },
            },
            {
                "table": "trades",
                "paper_action": "CLOSE_TRADE_ROW",
                "symbol": symbol,
                "data": self._trade_close_payload(trade, broker_symbol, exit_price, close_reason, result, pnl, now, risk_audit, recovery_context, risk_diag),
            },
        ]

    def process_decision(
        self,
        decision: dict,
        spread: float,
        opened_candle_time: str | None = None,
        max_spread: float | None = None,
        setup_id: str | None = None,
        broker_symbol: str | None = None,
        symbol_specs: dict | None = None,
        equity: float | None = None,
    ) -> list[dict]:
        if not self.enabled:
            return []
        if decision.get("decision") not in {"ENTER_ANALYSIS_ONLY", "ENTER_PAPER"}:
            return []
        if not self.recovery_reconciliation_completed:
            reason = "RECOVERY_RECONCILIATION_PENDING"
            symbol = str(decision.get("symbol") or "")
            paper_symbol_max_lot = self.settings.paper_max_lot_for_symbol(symbol)
            self._log_skip(symbol, reason, decision, paper_symbol_max_lot)
            return [self._skip_event(symbol, reason, decision, paper_symbol_max_lot, setup_id, spread, {})]

        symbol = str(decision.get("symbol") or "")
        decision = {**decision, **self._reentry_context(symbol, str(decision.get("strategy") or "UNKNOWN"))}
        open_risk_diag = self._risk_diagnostics(symbol, broker_symbol, decision, None, None, symbol_specs or {}, equity)
        decision = {**decision, **open_risk_diag}
        safety = self.safety_guard.evaluate(
            decision,
            self.latest_risk_diag_by_symbol_strategy.get((symbol, str(decision.get("strategy") or "UNKNOWN"))),
        )
        decision = {**decision, **safety}
        big_setup = self.big_setup_detector.evaluate(decision)
        decision = {**decision, **big_setup}
        paper_symbol_max_lot = self.settings.paper_max_lot_for_symbol(symbol)
        checklist = build_execution_checklist(
            decision,
            spread,
            max_spread,
            paper_symbol_max_lot,
            max_open_trades_ok=not self.has_open_trade(symbol),
        )
        setup_journal = self._journal_fields(decision, "SETUP", None, checklist)
        self._log_journal_setup(symbol, decision, setup_journal, checklist)
        self._log_reentry_context(symbol, decision)
        if safety.get("safety_guard_status") == "BLOCK":
            reason = str(safety.get("safety_guard_reason") or "SAFETY_GUARD_BLOCK")
            self._log_skip(symbol, reason, decision, paper_symbol_max_lot)
            skip_journal = self._journal_fields(decision, "PAPER_SKIP", reason, checklist)
            return [self._skip_event(symbol, reason, decision, paper_symbol_max_lot, setup_id, spread, skip_journal)]
        if self.has_open_trade(symbol):
            reason = "MAX_ONE_OPEN_PAPER_TRADE_PER_SYMBOL"
            self._log_skip(symbol, reason, decision, paper_symbol_max_lot)
            skip_checklist = dict(checklist)
            skip_checklist["max_open_trades_ok"] = False
            skip_journal = self._journal_fields(decision, "PAPER_SKIP", reason, skip_checklist)
            return [self._skip_event(symbol, reason, decision, paper_symbol_max_lot, setup_id, spread, skip_journal)]

        skip_reason = self._skip_reason(decision, spread, max_spread, paper_symbol_max_lot)
        if skip_reason:
            self._log_skip(symbol, skip_reason, decision, paper_symbol_max_lot)
            skip_journal = self._journal_fields(decision, "PAPER_SKIP", skip_reason, checklist)
            return [self._skip_event(symbol, skip_reason, decision, paper_symbol_max_lot, setup_id, spread, skip_journal)]

        now = datetime.now(timezone.utc).isoformat()
        open_journal = self._journal_fields(decision, "PAPER_OPEN", None, checklist, now)
        trade = {
            "paper_trade_id": str(uuid4()),
            "symbol": symbol,
            "timeframe": decision.get("timeframe", "M5"),
            "magic_number": self.settings.hermes_magic_number,
            "status": "PAPER_OPEN",
            "mode": "PAPER",
            "dir": decision.get("signal"),
            "direction": decision.get("signal"),
            "entry": _to_float(decision["entry"]),
            "sl": _to_float(decision["sl"]),
            "tp": _to_float(decision["tp"]),
            "lot_size": _to_float(decision["lot_size"]),
            "strategy": decision.get("strategy"),
            "confidence": decision.get("confidence"),
            "risk_status": decision.get("risk_status"),
            "final_risk": decision.get("final_risk"),
            "reward_risk": decision.get("reward_risk"),
            "market_state": decision.get("market_state"),
            "markov_probability": decision.get("markov_probability"),
            "h1_bias": decision.get("h1_bias"),
            "m15_liquidity": decision.get("m15_liquidity"),
            "m15_liquidity_type": decision.get("m15_liquidity_type"),
            "m5_cisd": decision.get("m5_cisd"),
            "mtfa_status": decision.get("mtfa_status"),
            "mtfa_reason": decision.get("mtfa_reason"),
            "mtfa_score": decision.get("mtfa_score"),
            "mtfa_mode": decision.get("mtfa_mode"),
            "mtf_structure_strategy": decision.get("mtf_structure_strategy"),
            "mtf_structure_mode": decision.get("mtf_structure_mode"),
            "mtf_structure_status": decision.get("mtf_structure_status"),
            "mtf_structure_direction": decision.get("mtf_structure_direction"),
            "h4_bias": decision.get("h4_bias"),
            "h4_zone": decision.get("h4_zone"),
            "m15_confirmation": decision.get("m15_confirmation"),
            "m15_structure_shift": decision.get("m15_structure_shift"),
            "m1_entry_confirmation": decision.get("m1_entry_confirmation"),
            "entry_price_suggestion": decision.get("entry_price_suggestion"),
            "sl_suggestion": decision.get("sl_suggestion"),
            "tp1_suggestion": decision.get("tp1_suggestion"),
            "tp2_suggestion": decision.get("tp2_suggestion"),
            "risk_reward_suggestion": decision.get("risk_reward_suggestion"),
            "mtf_structure_score": decision.get("mtf_structure_score"),
            "mtf_structure_reason": decision.get("mtf_structure_reason"),
            "h4_recent_support": decision.get("h4_recent_support"),
            "h4_recent_resistance": decision.get("h4_recent_resistance"),
            "h4_supply_zone": decision.get("h4_supply_zone"),
            "h4_demand_zone": decision.get("h4_demand_zone"),
            "h4_last_swing_high": decision.get("h4_last_swing_high"),
            "h4_last_swing_low": decision.get("h4_last_swing_low"),
            **journal_payload(decision),
            "setup_id": setup_id,
            "spread": spread,
            "learning_params": decision.get("learning_params"),
            "simulated": True,
            "opened_candle_time": opened_candle_time,
            "opened_at": now,
            "created_at": now,
            **open_journal,
        }
        log.info("[PAPER] Trade opened symbol=%s lot_size=%s paper_max_lot=%s", symbol, trade["lot_size"], paper_symbol_max_lot)
        return [
            {
                "table": "execution_events",
                "data": {
                    "event_type": "PAPER_OPEN",
                    "mode": "PAPER",
                    "symbol": symbol,
                    "timeframe": "M5",
                    "paper_trade_id": trade["paper_trade_id"],
                    "setup_id": setup_id,
                    "magic_number": self.settings.hermes_magic_number,
                    "status": "OPENED",
                    "direction": trade["direction"],
                    "entry": trade["entry"],
                    "sl": trade["sl"],
                    "tp": trade["tp"],
                    "lot_size": trade["lot_size"],
                    **journal_payload(trade),
                    "raw_payload": self._mtfa_payload(trade),
                    "created_at": now,
                },
            },
            {"table": "trades", "paper_action": "OPEN_TRADE", "trade": trade, "data": self._trade_open_payload(trade)},
        ]

    def confirm_open_trade(self, trade: dict) -> None:
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            return
        self.open_trades.setdefault(symbol, []).append(trade)
        self._refresh_duplicate_summary()
        log.info("[PAPER] Trade persisted symbol=%s id=%s lot_size=%s", symbol, trade.get("paper_trade_id"), trade.get("lot_size"))

    def confirm_close_trade(self, symbol: str, paper_trade_id: str | None = None) -> None:
        if symbol not in self.open_trades:
            return
        if paper_trade_id:
            remaining = [trade for trade in self.open_trades[symbol] if str(trade.get("paper_trade_id") or "") != paper_trade_id]
        else:
            remaining = []
        if remaining:
            self.open_trades[symbol] = remaining
        else:
            self.open_trades.pop(symbol, None)
        self._refresh_duplicate_summary()

    def clear_stale_open_trade(self, symbol: str) -> None:
        if symbol in self.open_trades:
            self.open_trades.pop(symbol, None)
            self._refresh_duplicate_summary()
            log.info("[PAPER] Cleared stale open paper trade symbol=%s", symbol)

    def has_open_trade(self, symbol: str) -> bool:
        return bool(self.open_trades.get(symbol))

    def open_duplicate_summary(self) -> dict:
        return {
            "paper_open_duplicate_count": self.paper_open_duplicate_count,
            "open_duplicates_by_symbol": dict(self.open_duplicates_by_symbol),
        }

    def _refresh_duplicate_summary(self) -> None:
        self.open_duplicates_by_symbol = {symbol: len(trades) for symbol, trades in self.open_trades.items() if len(trades) > 1}
        self.paper_open_duplicate_count = sum(count - 1 for count in self.open_duplicates_by_symbol.values())

    def _recovery_breach(self, trade: dict, price: float) -> tuple[str | None, str | None, float | None]:
        direction = trade.get("direction")
        sl = _to_float(trade.get("sl"))
        tp = _to_float(trade.get("tp"))
        if sl is None or tp is None:
            return None, None, None
        if direction == "BUY":
            sl_hit = price <= sl
            tp_hit = price >= tp
        elif direction == "SELL":
            sl_hit = price >= sl
            tp_hit = price <= tp
        else:
            return None, None, None
        if sl_hit and tp_hit:
            return "RECOVERY_AMBIGUOUS_BREACH", "LOSS", price
        if sl_hit:
            return "RECOVERY_SL_BREACH", "LOSS", price
        if tp_hit:
            return "RECOVERY_TP_BREACH", "WIN", price
        return None, None, None

    def _recovery_context(self, reason: str | None, exit_price: float | None, broker_symbol: str) -> dict:
        if not reason:
            return {}
        return {
            "recovery_close": True,
            "missed_exit": True,
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "recovery_exit_price": exit_price,
            "recovery_reason": reason,
            "broker_symbol": broker_symbol,
        }

    def _recovered_open_trade(self, row: dict) -> dict | None:
        if not isinstance(row, dict):
            return None
        raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
        magic = row.get("magic_number", row.get("magic"))
        if _to_float(magic) != float(self.settings.hermes_magic_number):
            return None
        mode = str(row.get("mode") or raw_payload.get("mode") or "").upper()
        if mode and mode != "PAPER":
            return None
        result = row.get("result")
        if result not in {None, "", "-"}:
            return None
        if row.get("closed_at") not in {None, "", "-"}:
            return None
        status = str(row.get("status") or raw_payload.get("status") or "").upper()
        if status and status not in {"OPEN", "OPENED", "PAPER_OPEN"}:
            return None

        paper_trade_id = str(row.get("paper_trade_id") or raw_payload.get("paper_trade_id") or row.get("id") or "")
        symbol = str(row.get("symbol") or raw_payload.get("symbol") or "")
        direction = str(row.get("direction") or row.get("dir") or row.get("signal") or raw_payload.get("direction") or raw_payload.get("dir") or "").upper()
        entry = _to_float(_first_non_empty(row.get("entry"), raw_payload.get("entry")))
        sl = _to_float(_first_non_empty(row.get("sl"), raw_payload.get("sl")))
        tp = _to_float(_first_non_empty(row.get("tp"), raw_payload.get("tp")))
        lot_size = _to_float(_first_non_empty(row.get("lot_size"), row.get("lot"), raw_payload.get("lot_size"), raw_payload.get("lot")))
        if not paper_trade_id or not symbol or direction not in {"BUY", "SELL"}:
            return None
        if entry is None or sl is None or tp is None or lot_size is None or lot_size <= 0:
            return None

        confidence = row.get("confidence", raw_payload.get("confidence_decimal"))
        confidence_float = _to_float(confidence)
        if confidence_float is not None and confidence_float > 1:
            confidence_float = confidence_float / 100.0
        opened_at = row.get("opened_at") or raw_payload.get("opened_at") or row.get("created_at") or raw_payload.get("created_at")
        return {
            **raw_payload,
            **row,
            "paper_trade_id": paper_trade_id,
            "symbol": symbol,
            "mode": "PAPER",
            "status": "PAPER_OPEN",
            "magic_number": self.settings.hermes_magic_number,
            "dir": direction,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "lot_size": lot_size,
            "confidence": confidence_float,
            "strategy": row.get("strategy") or raw_payload.get("strategy"),
            "opened_at": opened_at,
            "created_at": row.get("created_at") or raw_payload.get("created_at") or opened_at,
            "learning_params": raw_payload.get("learning_params") if isinstance(raw_payload.get("learning_params"), dict) else {},
        }

    def _trade_open_payload(self, trade: dict) -> dict:
        direction = trade.get("direction")
        lot_size = trade.get("lot_size")
        confidence_decimal = _to_float(trade.get("confidence"))
        top_level_journal = journal_payload(trade)
        top_level_journal.pop("confidence", None)
        return {
            "id": trade.get("paper_trade_id"),
            "dir": direction,
            "signal": direction,
            "symbol": trade.get("symbol"),
            "entry": trade.get("entry"),
            "sl": trade.get("sl"),
            "tp": trade.get("tp"),
            "lot_size": lot_size,
            "lot": lot_size,
            "magic_number": self.settings.hermes_magic_number,
            "magic": self.settings.hermes_magic_number,
            "strategy": trade.get("strategy"),
            "confidence": _confidence_percent(confidence_decimal),
            "opened_at": trade.get("opened_at"),
            "created_at": trade.get("created_at"),
            **top_level_journal,
            "raw_payload": {
                "mode": "PAPER",
                "status": "OPEN",
                "timeframe": trade.get("timeframe"),
                "paper_trade_id": trade.get("paper_trade_id"),
                "setup_id": trade.get("setup_id"),
                "risk_status": trade.get("risk_status"),
                "final_risk": trade.get("final_risk"),
                "reward_risk": trade.get("reward_risk"),
                "market_state": trade.get("market_state"),
                "markov_probability": trade.get("markov_probability"),
                "spread": trade.get("spread"),
                "learning_params": trade.get("learning_params"),
                "confidence_decimal": confidence_decimal,
                "h1_bias": trade.get("h1_bias"),
                "m15_liquidity": trade.get("m15_liquidity"),
                "m15_liquidity_type": trade.get("m15_liquidity_type"),
                "m5_cisd": trade.get("m5_cisd"),
                "mtfa_status": trade.get("mtfa_status"),
                "mtfa_reason": trade.get("mtfa_reason"),
                "mtfa_score": trade.get("mtfa_score"),
                "mtfa_mode": trade.get("mtfa_mode"),
                "mtf_structure_strategy": trade.get("mtf_structure_strategy"),
                "mtf_structure_mode": trade.get("mtf_structure_mode"),
                "mtf_structure_status": trade.get("mtf_structure_status"),
                "mtf_structure_direction": trade.get("mtf_structure_direction"),
                "h4_bias": trade.get("h4_bias"),
                "h4_zone": trade.get("h4_zone"),
                "m15_confirmation": trade.get("m15_confirmation"),
                "m15_structure_shift": trade.get("m15_structure_shift"),
                "m1_entry_confirmation": trade.get("m1_entry_confirmation"),
                "entry_price_suggestion": trade.get("entry_price_suggestion"),
                "sl_suggestion": trade.get("sl_suggestion"),
                "tp1_suggestion": trade.get("tp1_suggestion"),
                "tp2_suggestion": trade.get("tp2_suggestion"),
                "risk_reward_suggestion": trade.get("risk_reward_suggestion"),
                "mtf_structure_score": trade.get("mtf_structure_score"),
                "mtf_structure_reason": trade.get("mtf_structure_reason"),
                "h4_recent_support": trade.get("h4_recent_support"),
                "h4_recent_resistance": trade.get("h4_recent_resistance"),
                "h4_supply_zone": trade.get("h4_supply_zone"),
                "h4_demand_zone": trade.get("h4_demand_zone"),
                "h4_last_swing_high": trade.get("h4_last_swing_high"),
                "h4_last_swing_low": trade.get("h4_last_swing_low"),
                "simulated": True,
                "opened_candle_time": trade.get("opened_candle_time"),
                "direction": direction,
                **journal_payload(trade),
            },
        }

    def _trade_close_payload(
        self,
        trade: dict,
        broker_symbol: str,
        exit_price: float,
        reason: str | None,
        result: str | None,
        pnl: float,
        closed_at: str,
        risk_audit: dict | None = None,
        recovery_context: dict | None = None,
        risk_diag: dict | None = None,
    ) -> dict:
        direction = trade.get("direction")
        lot_size = trade.get("lot_size")
        confidence_decimal = _to_float(trade.get("confidence"))
        risk_audit = risk_audit or {}
        recovery_context = recovery_context or {}
        risk_diag = risk_diag or {}
        close_source = {
            **trade,
            "broker_symbol": broker_symbol,
            "exit_price": exit_price,
            "close_reason": reason,
            "exit_reason": reason,
            "result": result,
            "pnl": pnl,
            **recovery_context,
            **risk_diag,
        }
        close_journal = self._journal_fields(close_source, "PAPER_CLOSE", reason, date_time=closed_at)
        top_level_journal = dict(close_journal)
        top_level_journal.pop("confidence", None)
        return {
            "id": trade.get("paper_trade_id"),
            "dir": direction,
            "signal": direction,
            "symbol": trade.get("symbol"),
            "broker_symbol": broker_symbol,
            "entry": trade.get("entry"),
            "sl": trade.get("sl"),
            "tp": trade.get("tp"),
            "lot_size": lot_size,
            "lot": lot_size,
            "magic_number": self.settings.hermes_magic_number,
            "magic": self.settings.hermes_magic_number,
            "strategy": trade.get("strategy"),
            "confidence": _confidence_percent(confidence_decimal),
            "opened_at": trade.get("opened_at"),
            "closed_at": closed_at,
            "pnl": pnl,
            "result": result,
            "reason": reason,
            **recovery_context,
            **risk_diag,
            **top_level_journal,
            "raw_payload": {
                "mode": "PAPER",
                "status": "CLOSED",
                "timeframe": trade.get("timeframe"),
                "paper_trade_id": trade.get("paper_trade_id"),
                "broker_symbol": broker_symbol,
                "setup_id": trade.get("setup_id"),
                "risk_status": trade.get("risk_status"),
                "final_risk": trade.get("final_risk"),
                "reward_risk": trade.get("reward_risk"),
                "market_state": trade.get("market_state"),
                "markov_probability": trade.get("markov_probability"),
                "spread": trade.get("spread"),
                "learning_params": trade.get("learning_params"),
                "confidence_decimal": confidence_decimal,
                "h1_bias": trade.get("h1_bias"),
                "m15_liquidity": trade.get("m15_liquidity"),
                "m15_liquidity_type": trade.get("m15_liquidity_type"),
                "m5_cisd": trade.get("m5_cisd"),
                "mtfa_status": trade.get("mtfa_status"),
                "mtfa_reason": trade.get("mtfa_reason"),
                "mtfa_score": trade.get("mtfa_score"),
                "mtfa_mode": trade.get("mtfa_mode"),
                "mtf_structure_strategy": trade.get("mtf_structure_strategy"),
                "mtf_structure_mode": trade.get("mtf_structure_mode"),
                "mtf_structure_status": trade.get("mtf_structure_status"),
                "mtf_structure_direction": trade.get("mtf_structure_direction"),
                "h4_bias": trade.get("h4_bias"),
                "h4_zone": trade.get("h4_zone"),
                "m15_confirmation": trade.get("m15_confirmation"),
                "m15_structure_shift": trade.get("m15_structure_shift"),
                "m1_entry_confirmation": trade.get("m1_entry_confirmation"),
                "entry_price_suggestion": trade.get("entry_price_suggestion"),
                "sl_suggestion": trade.get("sl_suggestion"),
                "tp1_suggestion": trade.get("tp1_suggestion"),
                "tp2_suggestion": trade.get("tp2_suggestion"),
                "risk_reward_suggestion": trade.get("risk_reward_suggestion"),
                "mtf_structure_score": trade.get("mtf_structure_score"),
                "mtf_structure_reason": trade.get("mtf_structure_reason"),
                "h4_recent_support": trade.get("h4_recent_support"),
                "h4_recent_resistance": trade.get("h4_recent_resistance"),
                "h4_supply_zone": trade.get("h4_supply_zone"),
                "h4_demand_zone": trade.get("h4_demand_zone"),
                "h4_last_swing_high": trade.get("h4_last_swing_high"),
                "h4_last_swing_low": trade.get("h4_last_swing_low"),
                "risk_audit": risk_audit.get("risk_audit"),
                "realized_risk_percent": risk_audit.get("realized_risk_percent"),
                "equity": risk_audit.get("equity"),
                "max_risk": self.settings.max_risk_per_trade,
                **risk_diag,
                "simulated": True,
                "opened_candle_time": trade.get("opened_candle_time"),
                "direction": direction,
                "exit_price": exit_price,
                "close_reason": reason,
                **recovery_context,
                **close_journal,
            },
        }

    def _skip_reason(
        self,
        decision: dict,
        spread: float,
        max_spread: float | None = None,
        paper_symbol_max_lot: float | None = None,
    ) -> str | None:
        strategy = str(decision.get("strategy") or "").upper()
        if strategy == "SECOND_ENTRY" and not self.settings.second_entry_enabled:
            return "SECOND_ENTRY_DISABLED_LEGACY_OBSERVER"
        if strategy == "SCALPING_AGENT" and not self.settings.scalping_agent_enabled:
            return "SCALPING_AGENT_DISABLED_LEGACY_OBSERVER"
        new_strategy_reason = self._new_strategy_skip_reason(decision)
        if new_strategy_reason:
            return new_strategy_reason
        if decision.get("signal") not in {"BUY", "SELL"}:
            return "NO_DIRECTION"
        if decision.get("sl") is None:
            return "MISSING_SL"
        if decision.get("tp") is None:
            return "MISSING_TP"
        lot_size = decision.get("lot_size")
        lot = _to_float(lot_size)
        if lot is None or lot <= 0:
            return self._zero_lot_reason(decision)
        if decision.get("risk_status") != "APPROVED":
            return "RISK_NOT_APPROVED"
        if (
            decision.get("signal") in {"BUY", "SELL"}
            and self.settings.mtfa_enabled
            and self.settings.mtfa_mode == "ENFORCE"
            and decision.get("mtfa_status") != "PASS"
        ):
            return "MTFA_FILTER_FAIL"
        effective_paper_max_lot = paper_symbol_max_lot if paper_symbol_max_lot is not None else self.settings.paper_max_lot
        if lot > effective_paper_max_lot:
            return "LOT_ABOVE_PAPER_MAX"
        final_risk = _to_float(decision.get("final_risk")) or 0.0
        if final_risk > self.settings.max_risk_per_trade:
            return "MAX_RISK_EXCEEDED"
        effective_max_spread = max_spread if max_spread is not None else self.settings.max_spread
        if spread > effective_max_spread:
            return "MAX_SPREAD"
        confidence = _to_float(decision.get("confidence")) or 0.0
        if confidence < 0.65:
            return "LOW_CONFIDENCE"
        if _to_float(decision.get("entry")) is None:
            return "MISSING_ENTRY"
        if _to_float(decision.get("sl")) is None or _to_float(decision.get("tp")) is None:
            return "INVALID_SL_OR_TP"
        return None

    def _new_strategy_skip_reason(self, decision: dict) -> str | None:
        strategy = str(decision.get("strategy") or "").upper()
        if strategy not in {"CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"}:
            return None
        if self.settings.new_strategies_paper_only and (self.settings.demo_trading or self.settings.allow_live_trading):
            return "NEW_STRATEGIES_PAPER_ONLY"
        if self.settings.new_strategies_require_safety_pass and decision.get("safety_guard_status") != "PASS":
            return "NEW_STRATEGY_SAFETY_NOT_PASS"
        if self.settings.new_strategies_require_risk_ok and decision.get("risk_diag_status") != "OK":
            return "NEW_STRATEGY_RISK_DIAG_NOT_OK"
        rr = _to_float(decision.get("risk_reward") or decision.get("reward_risk")) or 0.0
        if rr < self.settings.new_strategies_require_rr_min:
            return "NEW_STRATEGY_RR_TOO_LOW"
        score_field = {
            "CRT_TBS_REVERSAL": "crt_tbs_score",
            "AMD_FVG_IFVG_REVERSAL": "amd_fvg_score",
            "FIB_OTE_RETEST": "fib_ote_score",
        }[strategy]
        score = _to_float(decision.get(score_field)) or 0.0
        if score < self.settings.new_strategies_min_score:
            return "NEW_STRATEGY_SCORE_TOO_LOW"
        return None

    def _zero_lot_reason(self, decision: dict) -> str:
        if decision.get("lot_blocked_reason"):
            return str(decision["lot_blocked_reason"])
        blocked_reason = str(decision.get("blocked_reason") or "")
        for reason in [
            "MISSING_EQUITY",
            "MISSING_SL",
            "ZERO_SL_DISTANCE",
            "MISSING_SYMBOL_INFO",
            "INVALID_TICK_VALUE",
            "RAW_LOT_ZERO",
            "MIN_LOT_EXCEEDS_RISK",
            "MAX_RISK_EXCEEDED",
            "INVALID_PROBABILITY",
            "NO_POSITIVE_EDGE",
        ]:
            if reason in blocked_reason:
                return reason
        return "RAW_LOT_ZERO"

    def _skip_event(
        self,
        symbol: str,
        reason: str,
        decision: dict,
        paper_symbol_max_lot: float,
        setup_id: str | None,
        spread: float,
        journal_fields: dict | None = None,
    ) -> dict:
        journal_fields = journal_fields or {}
        return {
            "table": "execution_events",
            "data": {
                "event_type": "PAPER_SKIP",
                "mode": "PAPER",
                "symbol": symbol,
                "timeframe": decision.get("timeframe", "M5"),
                "setup_id": setup_id,
                "magic_number": self.settings.hermes_magic_number,
                "status": "SKIPPED",
                "reason": reason,
                "signal": decision.get("signal"),
                "confidence": decision.get("confidence"),
                "risk_status": decision.get("risk_status"),
                "raw_lot": decision.get("raw_lot"),
                "approved_lot": decision.get("approved_lot"),
                "lot_size": decision.get("lot_size"),
                "paper_max_lot": self.settings.paper_max_lot,
                "paper_symbol_max_lot": paper_symbol_max_lot,
                "final_risk": decision.get("final_risk"),
                "spread": spread,
                "market_state": decision.get("market_state"),
                "markov_state": decision.get("market_state"),
                "h1_bias": decision.get("h1_bias"),
                "m15_liquidity": decision.get("m15_liquidity"),
                "m15_liquidity_type": decision.get("m15_liquidity_type"),
                "m5_cisd": decision.get("m5_cisd"),
                "mtfa_status": decision.get("mtfa_status"),
                "mtfa_reason": decision.get("mtfa_reason"),
                "mtfa_score": decision.get("mtfa_score"),
                "mtfa_mode": decision.get("mtfa_mode"),
                "mtf_structure_strategy": decision.get("mtf_structure_strategy"),
                "mtf_structure_mode": decision.get("mtf_structure_mode"),
                "mtf_structure_status": decision.get("mtf_structure_status"),
                "mtf_structure_direction": decision.get("mtf_structure_direction"),
                "h4_bias": decision.get("h4_bias"),
                "h4_zone": decision.get("h4_zone"),
                "m15_confirmation": decision.get("m15_confirmation"),
                "m15_structure_shift": decision.get("m15_structure_shift"),
                "m1_entry_confirmation": decision.get("m1_entry_confirmation"),
                "entry_price_suggestion": decision.get("entry_price_suggestion"),
                "sl_suggestion": decision.get("sl_suggestion"),
                "tp1_suggestion": decision.get("tp1_suggestion"),
                "tp2_suggestion": decision.get("tp2_suggestion"),
                "risk_reward_suggestion": decision.get("risk_reward_suggestion"),
                "mtf_structure_score": decision.get("mtf_structure_score"),
                "mtf_structure_reason": decision.get("mtf_structure_reason"),
                "h4_recent_support": decision.get("h4_recent_support"),
                "h4_recent_resistance": decision.get("h4_recent_resistance"),
                "h4_supply_zone": decision.get("h4_supply_zone"),
                "h4_demand_zone": decision.get("h4_demand_zone"),
                "h4_last_swing_high": decision.get("h4_last_swing_high"),
                "h4_last_swing_low": decision.get("h4_last_swing_low"),
                "max_risk": self.settings.max_risk_per_trade,
                **journal_fields,
                "raw_payload": self._mtfa_payload({**decision, **journal_fields}),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _log_skip(self, symbol: str, reason: str, decision: dict, paper_symbol_max_lot: float) -> None:
        log.info(
            "[PAPER] Trade skipped symbol=%s reason=%s lot_size=%s paper_max_lot=%s paper_symbol_max_lot=%s final_risk=%s max_risk=%s raw_lot=%s approved_lot=%s",
            symbol,
            reason,
            decision.get("lot_size"),
            self.settings.paper_max_lot,
            paper_symbol_max_lot,
            decision.get("final_risk"),
            self.settings.max_risk_per_trade,
            decision.get("raw_lot"),
            decision.get("approved_lot"),
        )

    def _mtfa_payload(self, payload: dict) -> dict:
        return {
            "h1_bias": payload.get("h1_bias"),
            "m15_liquidity": payload.get("m15_liquidity"),
            "m15_liquidity_type": payload.get("m15_liquidity_type"),
            "m5_cisd": payload.get("m5_cisd"),
            "mtfa_status": payload.get("mtfa_status"),
            "mtfa_reason": payload.get("mtfa_reason"),
            "mtfa_score": payload.get("mtfa_score"),
            "mtfa_mode": payload.get("mtfa_mode"),
            "mtf_structure_strategy": payload.get("mtf_structure_strategy"),
            "mtf_structure_mode": payload.get("mtf_structure_mode"),
            "mtf_structure_status": payload.get("mtf_structure_status"),
            "mtf_structure_direction": payload.get("mtf_structure_direction"),
            "h4_bias": payload.get("h4_bias"),
            "h4_zone": payload.get("h4_zone"),
            "m15_confirmation": payload.get("m15_confirmation"),
            "m15_structure_shift": payload.get("m15_structure_shift"),
            "m1_entry_confirmation": payload.get("m1_entry_confirmation"),
            "entry_price_suggestion": payload.get("entry_price_suggestion"),
            "sl_suggestion": payload.get("sl_suggestion"),
            "tp1_suggestion": payload.get("tp1_suggestion"),
            "tp2_suggestion": payload.get("tp2_suggestion"),
            "risk_reward_suggestion": payload.get("risk_reward_suggestion"),
            "mtf_structure_score": payload.get("mtf_structure_score"),
            "mtf_structure_reason": payload.get("mtf_structure_reason"),
            "h4_recent_support": payload.get("h4_recent_support"),
            "h4_recent_resistance": payload.get("h4_recent_resistance"),
            "h4_supply_zone": payload.get("h4_supply_zone"),
            "h4_demand_zone": payload.get("h4_demand_zone"),
            "h4_last_swing_high": payload.get("h4_last_swing_high"),
            "h4_last_swing_low": payload.get("h4_last_swing_low"),
            "recovery_close": payload.get("recovery_close"),
            "missed_exit": payload.get("missed_exit"),
            "recovered_at": payload.get("recovered_at"),
            "recovery_exit_price": payload.get("recovery_exit_price"),
            "recovery_reason": payload.get("recovery_reason"),
            "broker_symbol": payload.get("broker_symbol"),
            **{field: payload.get(field) for field in RISK_DIAG_FIELDS},
            **journal_payload(payload),
        }

    def _latest_completed_candle(self, candles: pd.DataFrame) -> pd.Series | None:
        if len(candles) >= 2:
            return candles.iloc[-2]
        if len(candles) == 1:
            return candles.iloc[-1]
        return None

    def _exit_price(self, direction: str, candle: pd.Series | None, tick: dict) -> tuple[float | None, bool]:
        if direction == "BUY":
            bid = _to_float(tick.get("bid"))
            if bid is not None:
                return bid, False
            return _candle_close(candle), True
        if direction == "SELL":
            ask = _to_float(tick.get("ask"))
            if ask is not None:
                return ask, False
            return _candle_close(candle), True
        return _candle_close(candle), True

    def _log_close_check(
        self,
        symbol: str,
        broker_symbol: str,
        trade: dict,
        tick: dict,
        price_used: float | None,
        should_close: bool,
        reason: str | None,
        fallback: bool,
    ) -> None:
        log.info(
            "[PAPER_CLOSE_CHECK] symbol=%s broker_symbol=%s dir=%s bid=%s ask=%s price_used=%s entry=%s sl=%s tp=%s should_close=%s reason=%s fallback=%s",
            symbol,
            broker_symbol,
            trade.get("direction"),
            tick.get("bid"),
            tick.get("ask"),
            price_used,
            trade.get("entry"),
            trade.get("sl"),
            trade.get("tp"),
            should_close,
            reason,
            fallback,
        )

    def _is_time_exit(self, trade: dict) -> bool:
        opened_at = _parse_iso(str(trade.get("opened_at") or ""))
        if opened_at is None:
            return False
        learning_params = trade.get("learning_params") if isinstance(trade.get("learning_params"), dict) else {}
        max_hold = _to_float(learning_params.get("max_hold_minutes")) or self.settings.paper_max_hold_minutes
        age_minutes = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
        return age_minutes >= max_hold

    def _pnl(self, trade: dict, exit_price: float, symbol_specs: dict) -> float:
        price_diff = self._price_diff(trade, exit_price)
        lot_size = _to_float(trade.get("lot_size")) or 0.0
        tick_value = _to_float(symbol_specs.get("tick_value"))
        tick_size = _to_float(symbol_specs.get("tick_size"))
        if tick_value is None or tick_size is None or tick_size <= 0:
            return round(price_diff * lot_size, 6)
        return round((price_diff / tick_size) * tick_value * lot_size, 6)

    def _price_diff(self, trade: dict, exit_price: float) -> float:
        entry = _to_float(trade.get("entry")) or 0.0
        if trade["direction"] == "BUY":
            return exit_price - entry
        return entry - exit_price

    def _log_pnl_diag(self, symbol: str, trade: dict, exit_price: float, pnl: float, symbol_specs: dict) -> None:
        log.info(
            "[PAPER_PNL_DIAG] symbol=%s dir=%s entry=%s exit_price=%s lot_size=%s tick_value=%s tick_size=%s contract_size=%s price_diff=%s pnl=%s",
            symbol,
            trade.get("direction"),
            trade.get("entry"),
            exit_price,
            trade.get("lot_size"),
            symbol_specs.get("tick_value"),
            symbol_specs.get("tick_size"),
            symbol_specs.get("contract_size"),
            self._price_diff(trade, exit_price),
            pnl,
        )

    def _risk_audit(self, symbol: str, trade: dict, exit_price: float, pnl: float, symbol_specs: dict, equity: float | None) -> dict:
        realized_risk_percent = None
        if equity is not None and equity > 0:
            realized_risk_percent = round((abs(pnl) / equity) * 100.0, 8)
        audit_status = "OK"
        if realized_risk_percent is not None and realized_risk_percent > self.settings.max_risk_per_trade + 0.05:
            audit_status = "EXCEEDED"

        log.info(
            "[RISK_AUDIT] symbol=%s pnl=%s equity=%s realized_risk_percent=%s max_risk=%s lot_size=%s entry=%s sl=%s exit_price=%s tick_value=%s tick_size=%s",
            symbol,
            pnl,
            equity,
            realized_risk_percent,
            self.settings.max_risk_per_trade,
            trade.get("lot_size"),
            trade.get("entry"),
            trade.get("sl"),
            exit_price,
            symbol_specs.get("tick_value"),
            symbol_specs.get("tick_size"),
        )
        if audit_status == "EXCEEDED":
            log.warning(
                "[RISK_AUDIT] exceeded symbol=%s realized_risk_percent=%s max_risk=%s",
                symbol,
                realized_risk_percent,
                self.settings.max_risk_per_trade,
            )
        return {
            "risk_audit": audit_status,
            "realized_risk_percent": realized_risk_percent,
            "equity": equity,
        }

    def _risk_diagnostics(
        self,
        symbol: str,
        broker_symbol: str | None,
        trade: dict,
        exit_price: float | None,
        pnl: float | None,
        symbol_specs: dict | None,
        equity: float | None,
    ) -> dict:
        symbol_specs = symbol_specs or {}
        entry = _to_float(trade.get("entry"))
        sl = _to_float(trade.get("sl"))
        lot_size = _to_float(trade.get("lot_size"))
        point = _to_float(symbol_specs.get("point"))
        tick_value = _to_float(symbol_specs.get("tick_value"))
        tick_size = _to_float(symbol_specs.get("tick_size"))
        contract_size = _to_float(symbol_specs.get("contract_size"))
        digits = symbol_specs.get("digits")
        sl_distance_price = abs(entry - sl) if entry is not None and sl is not None else None
        sl_distance_points = sl_distance_price / point if sl_distance_price is not None and point not in {None, 0} else None
        expected_risk_money = None
        if entry is not None and sl is not None:
            diagnostic_trade = dict(trade)
            diagnostic_trade["entry"] = entry
            diagnostic_trade["sl"] = sl
            diagnostic_trade["lot_size"] = lot_size
            diagnostic_trade["direction"] = diagnostic_trade.get("direction") or diagnostic_trade.get("signal") or diagnostic_trade.get("dir")
            expected_risk_money = abs(self._pnl(diagnostic_trade, sl, symbol_specs))
        expected_risk_percent = None
        realized_risk_percent = None
        if equity is not None and equity > 0:
            if expected_risk_money is not None:
                expected_risk_percent = round((expected_risk_money / equity) * 100.0, 8)
            if pnl is not None:
                realized_risk_percent = round((abs(pnl) / equity) * 100.0, 8)
        mismatch_percent = None
        status = "UNKNOWN"
        if expected_risk_percent is not None:
            status = "OK"
            compare_value = realized_risk_percent if realized_risk_percent is not None else _to_float(trade.get("final_risk"))
            if compare_value is not None:
                mismatch_percent = round(compare_value - expected_risk_percent, 8)
                if abs(mismatch_percent) > 0.05:
                    status = "MISMATCH"
        out = {
            "risk_diag_account_equity": equity,
            "risk_diag_entry": entry,
            "risk_diag_sl": sl,
            "risk_diag_exit_price": exit_price,
            "risk_diag_lot_size": lot_size,
            "risk_diag_symbol": symbol,
            "risk_diag_broker_symbol": broker_symbol,
            "risk_diag_point": point,
            "risk_diag_digits": digits,
            "risk_diag_tick_value": tick_value,
            "risk_diag_tick_size": tick_size,
            "risk_diag_contract_size": contract_size,
            "risk_diag_sl_distance_price": round(sl_distance_price, 10) if sl_distance_price is not None else None,
            "risk_diag_sl_distance_points": round(sl_distance_points, 4) if sl_distance_points is not None else None,
            "risk_diag_expected_risk_money": round(expected_risk_money, 6) if expected_risk_money is not None else None,
            "risk_diag_expected_risk_percent": expected_risk_percent,
            "risk_diag_realized_pnl": pnl,
            "risk_diag_realized_risk_percent": realized_risk_percent,
            "risk_diag_mismatch_percent": mismatch_percent,
            "risk_diag_status": status,
        }
        log.info(
            "[RISK_DIAG] symbol=%s lot=%s entry=%s sl=%s expected_risk_pct=%s realized_risk_pct=%s status=%s",
            symbol,
            lot_size,
            entry,
            sl,
            expected_risk_percent,
            realized_risk_percent,
            status,
        )
        return out

    def _journal_fields(
        self,
        payload: dict,
        sample_type: str,
        reason: str | None,
        checklist: dict | None = None,
        date_time: str | None = None,
    ) -> dict:
        if not self.settings.journal_layer_enabled:
            return {}
        enriched = dict(payload)
        if checklist:
            enriched["execution_checklist"] = checklist
        fields = build_journal_fields(enriched, sample_type, reason, date_time)
        if not self.settings.confluence_tagging_enabled:
            fields["confluence_score"] = 0
            fields["confluence_factors"] = []
        return fields

    def _log_journal_setup(self, symbol: str, decision: dict, journal_fields: dict, checklist: dict) -> None:
        if not self.settings.journal_layer_enabled:
            return
        log.info(
            "[JOURNAL] setup symbol=%s strategy=%s confluence_score=%s market_structure=%s phase=%s checklist=%s",
            symbol,
            decision.get("strategy"),
            journal_fields.get("confluence_score"),
            journal_fields.get("market_structure"),
            journal_fields.get("market_phase"),
            checklist,
        )

    def _log_reentry_context(self, symbol: str, decision: dict) -> None:
        if not self.settings.journal_layer_enabled:
            return
        log.info(
            "[JOURNAL] reentry_context symbol=%s strategy=%s consecutive_losses=%s reentry_after_sl=%s",
            symbol,
            decision.get("strategy"),
            decision.get("consecutive_losses_symbol_strategy"),
            decision.get("reentry_after_sl"),
        )

    def _reentry_context(self, symbol: str, strategy: str) -> dict:
        symbol_strategy = (symbol, strategy)
        last_loss_at = self.last_loss_at_by_symbol_strategy.get(symbol_strategy) or self.last_loss_at_by_symbol.get(symbol)
        previous_symbol_reason = self.last_close_reason_by_symbol.get(symbol)
        previous_strategy_reason = self.last_close_reason_by_strategy.get(strategy)
        return {
            "previous_trade_result_for_symbol": self.last_result_by_symbol.get(symbol),
            "previous_trade_result_for_strategy": self.last_result_by_strategy.get(strategy),
            "previous_trade_result_for_symbol_strategy": self.last_result_by_symbol.get(symbol)
            if self.last_result_by_strategy.get(strategy) == self.last_result_by_symbol.get(symbol)
            else None,
            "consecutive_losses_symbol": self.consecutive_losses_by_symbol.get(symbol, 0),
            "consecutive_losses_symbol_strategy": self.consecutive_losses_by_symbol_strategy.get(symbol_strategy, 0),
            "minutes_since_last_loss": self._minutes_since(last_loss_at),
            "reentry_after_sl": previous_symbol_reason == "SL" or previous_strategy_reason == "SL",
        }

    def _remember_closed_trade(
        self,
        symbol: str,
        strategy: str,
        result: str | None,
        close_reason: str | None,
        closed_at: str,
    ) -> None:
        symbol_strategy = (symbol, strategy)
        normalized_result = str(result or "").upper() or None
        normalized_reason = str(close_reason or "").upper() or None
        self.last_result_by_symbol[symbol] = normalized_result
        self.last_result_by_strategy[strategy] = normalized_result
        self.last_close_reason_by_symbol[symbol] = normalized_reason
        self.last_close_reason_by_strategy[strategy] = normalized_reason
        if normalized_result == "LOSS":
            self.consecutive_losses_by_symbol[symbol] = self.consecutive_losses_by_symbol.get(symbol, 0) + 1
            self.consecutive_losses_by_symbol_strategy[symbol_strategy] = (
                self.consecutive_losses_by_symbol_strategy.get(symbol_strategy, 0) + 1
            )
            self.last_loss_at_by_symbol[symbol] = closed_at
            self.last_loss_at_by_symbol_strategy[symbol_strategy] = closed_at
        elif normalized_result == "WIN":
            self.consecutive_losses_by_symbol[symbol] = 0
            self.consecutive_losses_by_symbol_strategy[symbol_strategy] = 0

    def _minutes_since(self, value: str | None) -> float | None:
        if not value:
            return None
        parsed = _parse_iso(value)
        if parsed is None:
            return None
        return round((datetime.now(timezone.utc) - parsed).total_seconds() / 60.0, 4)


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _first_non_empty(*values: object) -> object:
    for value in values:
        if value is not None and value != "" and value != "-":
            return value
    return None


def _candle_close(candle: pd.Series | None) -> float | None:
    if candle is None:
        return None
    return _to_float(candle.get("close"))


def _confidence_percent(value: float | None) -> float | None:
    if value is None:
        return None
    if 0 <= value <= 1:
        return round(value * 100.0, 4)
    return value


def _parse_iso(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None
