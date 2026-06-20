from __future__ import annotations

import json
import random
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.agents.big_setup_detector import BIG_SETUP_FIELDS
from app.agents.journal_layer import RISK_DIAG_FIELDS, STRATEGY_REPLACEMENT_FIELDS, calculate_performance_metrics, enrich_with_journal
from app.agents.safety_guard import SAFETY_GUARD_FIELDS
from app.agents.smc_confluence_tagger import SMC_FIELDS
from app.config import Settings
from app.logger import log


STRATEGIES = ["EMA_PULLBACK", "BREAKOUT_RETEST", "SECOND_ENTRY", "SCALPING_AGENT"]


class PaperLearningOptimizer:
    def __init__(self, settings: Settings, root: Path | None = None) -> None:
        self.settings = settings
        self.root = root or Path(__file__).resolve().parents[1]
        self.data_dir = self.root / "data"
        self.snapshots_dir = self.data_dir / "learning_snapshots"
        self.params_path = self.data_dir / "learned_paper_params.json"
        self.samples_path = self.data_dir / "paper_learning_samples.jsonl"
        self.closed_trades: list[dict] = []
        self.batch_samples: list[dict] = []
        self.state = self._load()

    @property
    def enabled(self) -> bool:
        return (
            self.settings.learning_mode
            and self.settings.auto_apply_learning
            and self.settings.paper_trading
            and not self.settings.demo_trading
            and not self.settings.allow_live_trading
        )

    def apply_to_signals(self, symbol: str, signals: Iterable[dict]) -> list[dict]:
        if not self.enabled:
            return list(signals)
        priority = _float(self.state.setdefault("symbols", {}).setdefault(symbol, {"priority": 1.0, "strategies": {}}).get("priority"), 1.0)
        if random.random() > _clamp(priority, 0.25, 1.0):
            return [self._priority_skip(signal) for signal in signals]
        adjusted = []
        for signal in signals:
            adjusted.append(self._apply_signal_params(symbol, signal))
        return adjusted

    def _priority_skip(self, signal: dict) -> dict:
        out = dict(signal)
        if out.get("signal") in {"BUY", "SELL"}:
            out["signal"] = "WAIT"
            out["blocked_reason"] = "LEARNING_SYMBOL_PRIORITY"
        return out

    def record_setup(self, sample: dict) -> list[dict]:
        if not self.enabled:
            return []
        try:
            sample = dict(sample)
            sample["sample_type"] = sample.get("sample_type", "SETUP")
            sample["created_at"] = _now()
            if self.settings.journal_layer_enabled:
                sample = enrich_with_journal(sample, str(sample.get("sample_type") or "SETUP"), sample.get("reason"))
            self._write_sample(sample)
            self.batch_samples.append(sample)
            self._update_setup_counts(sample)
            every = max(1, int(self.settings.performance_summary_every_setups or 25))
            if int(self.state.get("total_setups", 0)) % every == 0:
                self._log_performance()
            log.info(
                "[LEARNING] sample written symbol=%s strategy=%s result=%s pnl=%s",
                sample.get("symbol"),
                sample.get("strategy"),
                sample.get("result") or sample.get("reason"),
                sample.get("pnl"),
            )
            if len(self.batch_samples) >= self.settings.learning_batch_size:
                return self._optimize_batch()
        except Exception as exc:
            log.warning("[LEARNING] non-fatal setup learning error: %s", exc)
        return []

    def record_close(self, sample: dict) -> list[dict]:
        if not self.enabled:
            return []
        try:
            sample = dict(sample)
            sample["sample_type"] = "PAPER_CLOSE"
            sample["close_reason"] = sample.get("close_reason") or sample.get("reason")
            raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
            sample["paper_trade_id"] = sample.get("paper_trade_id") or raw_payload.get("paper_trade_id") or sample.get("id")
            sample["setup_id"] = sample.get("setup_id") or raw_payload.get("setup_id")
            sample["final_risk"] = sample.get("final_risk") or raw_payload.get("final_risk")
            sample["reward_risk"] = sample.get("reward_risk") or raw_payload.get("reward_risk")
            sample["spread"] = sample.get("spread") or raw_payload.get("spread")
            sample["market_state"] = sample.get("market_state") or raw_payload.get("market_state")
            sample["markov_state"] = sample.get("markov_state") or raw_payload.get("market_state")
            for field in [
                "recovery_close",
                "missed_exit",
                "recovered_at",
                "recovery_exit_price",
                "recovery_reason",
                "broker_symbol",
            ]:
                sample[field] = sample.get(field) if sample.get(field) is not None else raw_payload.get(field)
            for field in [
                "h1_bias",
                "m15_liquidity",
                "m15_liquidity_type",
                "m5_cisd",
                "mtfa_status",
                "mtfa_reason",
                "mtfa_score",
                "mtfa_mode",
            ]:
                sample[field] = sample.get(field) if sample.get(field) is not None else raw_payload.get(field)
            for field in SMC_FIELDS + RISK_DIAG_FIELDS + SAFETY_GUARD_FIELDS + BIG_SETUP_FIELDS + STRATEGY_REPLACEMENT_FIELDS:
                sample[field] = sample.get(field) if sample.get(field) is not None else raw_payload.get(field)
            sample["direction"] = sample.get("direction") or sample.get("dir") or raw_payload.get("direction")
            sample["created_at"] = _now()
            if self.settings.journal_layer_enabled:
                sample = enrich_with_journal(sample, "PAPER_CLOSE", sample.get("close_reason"))
            if _is_realized_close(sample):
                self.closed_trades.append(sample)
                self.closed_trades = self.closed_trades[-120:]
                self._log_performance()
            self._write_sample(sample)
            self.batch_samples.append(sample)
            log.info(
                "[LEARNING] sample written symbol=%s strategy=%s result=%s pnl=%s",
                sample.get("symbol"),
                sample.get("strategy"),
                sample.get("result"),
                sample.get("pnl"),
            )
            reports = []
            rollback = self._maybe_rollback()
            if rollback:
                reports.append(rollback)
            if len(self.batch_samples) >= self.settings.learning_batch_size:
                reports.extend(self._optimize_batch())
            return reports
        except Exception as exc:
            log.warning("[LEARNING] non-fatal close learning error: %s", exc)
            return []

    def milestone_report_if_due(self) -> dict | None:
        setups = int(self.state.get("total_setups", 0))
        if setups <= 0 or setups % max(1, self.settings.learning_min_setups) != 0:
            return None
        summary = self._summary()
        log.info("[LEARNING] 200 setup milestone summary %s", summary)
        return {
            "table": "nightly_reports",
            "data": {
                "report_date": _now()[:10],
                "status": "LEARNING_MILESTONE",
                "summary": "Autonomous PAPER learning milestone",
                "raw_payload": summary,
                "created_at": _now(),
            },
        }

    def performance_metrics(self) -> dict:
        return calculate_performance_metrics(self.closed_trades)

    def strategy_stats(self, samples: list[dict] | None = None) -> dict:
        samples = samples if samples is not None else self._load_samples()
        stats = {strategy: _empty_strategy_stats() for strategy in STRATEGIES}
        confidence_values: dict[str, list[float]] = {strategy: [] for strategy in STRATEGIES}
        skip_reasons: dict[str, Counter] = {strategy: Counter() for strategy in STRATEGIES}

        for sample in samples:
            strategy = str(sample.get("strategy") or "")
            if strategy not in stats:
                continue
            sample_type = str(sample.get("sample_type") or "").upper()
            result = str(sample.get("result") or "").upper()
            signal = str(sample.get("signal") or sample.get("direction") or "").upper()
            final_decision = str(sample.get("final_decision") or "").upper()
            reason = sample.get("reason_for_skip") or sample.get("reason") or sample.get("blocked_reason")
            item = stats[strategy]

            if sample_type in {"SETUP", "PAPER_OPEN", "PAPER_SKIP"}:
                item["setups_count"] += 1
            if sample_type == "PAPER_OPEN":
                item["paper_opens"] += 1
            elif sample_type == "PAPER_SKIP":
                item["paper_skips"] += 1
                if reason:
                    skip_reasons[strategy][str(reason)] += 1
            elif sample_type == "PAPER_CLOSE" and _is_realized_close(sample):
                item["paper_closes"] += 1
                pnl = _float(sample.get("pnl"), 0.0)
                item["pnl"] = round(item["pnl"] + pnl, 6)
                if result == "WIN":
                    item["wins"] += 1
                elif result == "LOSS":
                    item["losses"] += 1

            if signal == "WAIT" or final_decision == "WAIT_ANALYSIS_ONLY":
                item["wait_count"] += 1
            confidence = _confidence_decimal(sample.get("confidence"))
            if confidence is not None:
                confidence_values[strategy].append(confidence)
            item["latest_signal"] = signal or None
            item["latest_reason"] = reason or sample.get("mtf_structure_reason") or sample.get("mtfa_reason")
            item["latest_status"] = _latest_status(sample)

        for strategy, item in stats.items():
            closed = item["wins"] + item["losses"]
            item["top_skip_reason"] = skip_reasons[strategy].most_common(1)[0][0] if skip_reasons[strategy] else None
            item["win_rate_closed_only"] = round(item["wins"] / closed, 4) if closed else None
            item["avg_confidence"] = (
                round(sum(confidence_values[strategy]) / len(confidence_values[strategy]), 4)
                if confidence_values[strategy]
                else None
            )
            if closed == 0:
                item["pnl"] = 0
                item["latest_status"] = "ANALYSIS_ONLY"
        return stats

    def _apply_signal_params(self, symbol: str, signal: dict) -> dict:
        strategy = str(signal.get("strategy") or "UNKNOWN")
        params = self._strategy(symbol, strategy)
        out = dict(signal)
        out["learning_params"] = dict(params)
        if not params.get("enabled", True):
            out["signal"] = "SKIP"
            out["blocked_reason"] = "LEARNING_STRATEGY_DISABLED"
            return out

        confidence = _float(out.get("confidence"), 0.0) * _float(params.get("weight"), 1.0)
        explore = random.random() < self.settings.learning_explore_rate
        if explore:
            field = random.choice(["min_confidence", "tp_mult", "sl_mult"])
            self._explore(symbol, strategy, params, field)
        else:
            log.info("[LEARNING] exploit symbol=%s strategy=%s", symbol, strategy)

        min_conf = _float(params.get("min_confidence"), 0.60)
        out["confidence"] = round(_clamp(confidence, 0.0, 0.95), 4)
        if out.get("signal") in {"BUY", "SELL"} and out["confidence"] < min_conf:
            out["signal"] = "WAIT"
            out["blocked_reason"] = "LEARNING_MIN_CONFIDENCE"
            return out

        if out.get("signal") in {"BUY", "SELL"}:
            self._apply_tp_sl(out, params)
        return out

    def _apply_tp_sl(self, signal: dict, params: dict) -> None:
        entry = _maybe_float(signal.get("entry"))
        sl = _maybe_float(signal.get("sl"))
        tp = _maybe_float(signal.get("tp"))
        if entry is None or sl is None or tp is None:
            return
        sl_distance = abs(entry - sl) * _float(params.get("sl_mult"), 1.0)
        tp_distance = abs(tp - entry) * _float(params.get("tp_mult"), 1.0)
        if signal.get("signal") == "BUY":
            signal["sl"] = entry - sl_distance
            signal["tp"] = entry + tp_distance
        else:
            signal["sl"] = entry + sl_distance
            signal["tp"] = entry - tp_distance

    def _optimize_batch(self) -> list[dict]:
        batch = self.batch_samples
        self.batch_samples = []
        self._snapshot()
        reports = []
        grouped: dict[tuple[str, str], list[dict]] = {}
        for sample in batch:
            key = (str(sample.get("symbol") or ""), str(sample.get("strategy") or "UNKNOWN"))
            grouped.setdefault(key, []).append(sample)

        for (symbol, strategy), samples in grouped.items():
            if not symbol:
                continue
            params = self._strategy(symbol, strategy)
            closed = [s for s in samples if s.get("sample_type") == "PAPER_CLOSE"]
            realized_closed = [s for s in closed if _is_realized_close(s)]
            skips = [s for s in samples if s.get("sample_type") == "PAPER_SKIP"]
            pnl = sum(_float(s.get("pnl"), 0.0) for s in realized_closed)
            wins = sum(1 for s in realized_closed if s.get("result") == "WIN")
            losses = sum(1 for s in realized_closed if s.get("result") == "LOSS")
            total_closed_for_strategy = int(params.get("wins", 0)) + int(params.get("losses", 0)) + len(realized_closed)
            time_exit_losses = sum(
                1 for s in realized_closed if s.get("close_reason") == "TIME_EXIT" and _float(s.get("pnl"), 0.0) < 0
            )
            time_exit_wins = sum(
                1 for s in realized_closed if s.get("close_reason") == "TIME_EXIT" and _float(s.get("pnl"), 0.0) >= 0
            )

            if not self._has_enough_realized_closes(symbol, strategy, realized_closed):
                log.info(
                    "[LEARNING] skipped weight update because insufficient realized closes symbol=%s strategy=%s closed_trades=%s threshold=%s",
                    symbol,
                    strategy,
                    len(realized_closed),
                    self.settings.min_closed_trades_for_weight_update,
                )
            else:
                for realized_sample in realized_closed:
                    self._update_counts(realized_sample)
                self.apply_weight_update(symbol, strategy, params, realized_closed, pnl)
                if losses > wins:
                    self._apply_batch_change(symbol, strategy, params, "min_confidence", 0.02, "loss_control")
                    self._apply_batch_change(symbol, strategy, params, "sl_mult", -0.05, "sl_hit_rate")
                elif wins > losses:
                    self._apply_batch_change(symbol, strategy, params, "min_confidence", -0.01, "high_win_rate")
                    self._apply_batch_change(symbol, strategy, params, "tp_mult", 0.05, "tp_hit_rate")
                if time_exit_losses > time_exit_wins:
                    self._apply_batch_change(symbol, strategy, params, "max_hold_minutes", -5, "time_exit_losses")
                elif time_exit_wins > time_exit_losses:
                    self._apply_batch_change(symbol, strategy, params, "max_hold_minutes", 5, "time_exit_wins")
                if (
                    params["setups"] >= self.settings.learning_min_setups
                    and total_closed_for_strategy >= self.settings.min_closed_trades_for_strategy_disable
                    and losses >= 8
                    and pnl < 0
                ):
                    old = params["enabled"]
                    params["enabled"] = False
                    self._log_update(symbol, strategy, "enabled", old, False, "too_many_losses")
                self._update_symbol_priority(symbol, pnl)

            reports.append({"symbol": symbol, "strategy": strategy, "closed": len(realized_closed), "skips": len(skips), "pnl": pnl})

        self.state["updated_at"] = _now()
        self._save()
        summary = self._batch_summary(batch)
        log.info(
            "[LEARNING] batch complete setups=%s paper_opens=%s paper_closes=%s realized_pnl=%s realized_win_rate=%s skipped_setups=%s",
            summary["setups"],
            summary["paper_opens"],
            summary["paper_closes"],
            summary["realized_pnl"],
            summary["realized_win_rate"],
            summary["skipped_setups"],
        )
        milestone = self.milestone_report_if_due()
        return [milestone] if milestone else []

    def _apply_batch_change(self, symbol: str, strategy: str, params: dict, field: str, delta: float, reason: str) -> None:
        old = params.get(field)
        if field == "min_confidence":
            params[field] = round(_clamp(_float(old, 0.60) + _clamp(delta, -0.02, 0.02), 0.50, 0.90), 4)
        elif field == "weight":
            log.warning("[LEARNING] blocked direct weight mutation outside apply_weight_update symbol=%s strategy=%s", symbol, strategy)
            return
        elif field in {"tp_mult", "sl_mult"}:
            params[field] = round(_clamp(_float(old, 1.0) * (1.0 + _clamp(delta, -0.05, 0.05)), 0.70, 1.50), 4)
        elif field == "max_hold_minutes":
            params[field] = int(_clamp(_float(old, 30.0) + delta, 5, 45))
        if old != params.get(field):
            self._log_update(symbol, strategy, field, old, params[field], reason)

    def _explore(self, symbol: str, strategy: str, params: dict, field: str) -> None:
        log.info("[LEARNING] explore symbol=%s strategy=%s param=%s", symbol, strategy, field)

    def _calibrate_from_skips(self, symbol: str, strategy: str, params: dict, skips: list[dict]) -> None:
        if not skips:
            return
        low_confidence_skips = sum(1 for skip in skips if skip.get("reason") == "LOW_CONFIDENCE")
        spread_skips = sum(1 for skip in skips if skip.get("reason") == "MAX_SPREAD")
        if spread_skips:
            log.info(
                "[LEARNING] skip spread filter observed symbol=%s strategy=%s skipped_setups=%s",
                symbol,
                strategy,
                spread_skips,
            )

    def apply_weight_update(
        self,
        symbol: str,
        strategy: str,
        params: dict,
        realized_closed: list[dict],
        realized_pnl: float,
    ) -> None:
        realized_closed_trades = len(realized_closed)
        if realized_closed_trades < self.settings.min_closed_trades_for_weight_update:
            log.info(
                "[LEARNING] skipped weight update because insufficient realized closes symbol=%s strategy=%s closed_trades=%s threshold=%s",
                symbol,
                strategy,
                realized_closed_trades,
                self.settings.min_closed_trades_for_weight_update,
            )
            return
        if any(_maybe_float(sample.get("pnl")) is None for sample in realized_closed):
            log.info(
                "[LEARNING] skipped weight update because realized pnl missing symbol=%s strategy=%s closed_trades=%s",
                symbol,
                strategy,
                realized_closed_trades,
            )
            return
        if realized_pnl == 0:
            log.info(
                "[LEARNING] no realized pnl edge for weight update symbol=%s strategy=%s closed_trades=%s realized_pnl=%s",
                symbol,
                strategy,
                realized_closed_trades,
                realized_pnl,
            )
            return

        old = _float(params.get("weight"), 1.0)
        delta = 0.03 if realized_pnl > 0 else -0.03
        reason = "profitable" if realized_pnl > 0 else "losing"
        floor = self._weight_floor(params)
        new = round(_clamp(old + delta, floor, 1.50), 4)
        log.info(
            "[LEARNING_DEBUG] applying weight update symbol=%s strategy=%s closed_trades=%s realized_pnl=%s old=%s new=%s reason=%s",
            symbol,
            strategy,
            realized_closed_trades,
            realized_pnl,
            old,
            new,
            reason,
        )
        params["weight"] = new
        if old != new:
            self._log_update(symbol, strategy, "weight", old, new, reason)

    def _weight_floor(self, params: dict) -> float:
        if int(params.get("setups", 0)) < self.settings.learning_min_setups:
            return self.settings.min_weight_before_200_setups
        return 0.50

    def _has_enough_realized_closes(self, symbol: str, strategy: str, realized_closed: list[dict]) -> bool:
        closed_trades = len(realized_closed)
        if closed_trades < self.settings.min_closed_trades_for_weight_update:
            return False
        if any(_maybe_float(sample.get("pnl")) is None for sample in realized_closed):
            log.info(
                "[LEARNING] skipped weight update because realized pnl missing symbol=%s strategy=%s closed_trades=%s",
                symbol,
                strategy,
                closed_trades,
            )
            return False
        return True

    def _update_symbol_priority(self, symbol: str, pnl: float) -> None:
        sym = self.state.setdefault("symbols", {}).setdefault(symbol, {"priority": 1.0, "strategies": {}})
        old = _float(sym.get("priority"), 1.0)
        delta = 0.05 if pnl > 0 else -0.05
        new = round(_clamp(old + delta, 0.25, 1.0), 4)
        sym["priority"] = new
        if old != new:
            log.info("[LEARNING] params updated symbol=%s strategy=SYMBOL field=priority old=%s new=%s reason=%s", symbol, old, new, "expectancy")

    def _maybe_rollback(self) -> dict | None:
        if len(self.closed_trades) < 100:
            return None
        previous = self.closed_trades[-100:-50]
        recent = self.closed_trades[-50:]
        previous_expectancy = sum(_float(t.get("pnl"), 0.0) for t in previous) / 50
        recent_expectancy = sum(_float(t.get("pnl"), 0.0) for t in recent) / 50
        if recent_expectancy >= previous_expectancy:
            return None
        snapshots = sorted(self.snapshots_dir.glob("learned_paper_params_*.json"))
        if not snapshots:
            return None
        shutil.copyfile(snapshots[-1], self.params_path)
        self.state = self._load()
        log.warning("[LEARNING] rollback applied reason=performance_degraded")
        return {
            "table": "bot_logs",
            "data": {
                "level": "WARNING",
                "source": "LEARNING",
                "message": "[LEARNING] rollback applied reason=performance_degraded",
                "created_at": _now(),
            },
        }

    def _update_counts(self, sample: dict) -> None:
        if not _is_realized_close(sample):
            return
        symbol = str(sample.get("symbol") or "")
        strategy = str(sample.get("strategy") or "UNKNOWN")
        params = self._strategy(symbol, strategy)
        if sample.get("result") == "WIN":
            params["wins"] += 1
        elif sample.get("result") == "LOSS":
            params["losses"] += 1
        params["pnl"] = round(_float(params.get("pnl"), 0.0) + _float(sample.get("pnl"), 0.0), 6)

    def _update_setup_counts(self, sample: dict) -> None:
        symbol = str(sample.get("symbol") or "")
        strategy = str(sample.get("strategy") or "UNKNOWN")
        if not symbol:
            return
        params = self._strategy(symbol, strategy)
        params["setups"] += 1
        self.state["total_setups"] = int(self.state.get("total_setups", 0)) + 1

    def _strategy(self, symbol: str, strategy: str) -> dict:
        symbols = self.state.setdefault("symbols", {})
        sym = symbols.setdefault(symbol, {"priority": 1.0, "strategies": {}})
        strategies = sym.setdefault("strategies", {})
        return strategies.setdefault(strategy, _default_strategy())

    def _load(self) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        if self.params_path.exists():
            try:
                return json.loads(self.params_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                log.warning("[LEARNING] learned params unreadable; starting defaults")
        return {"version": 1, "updated_at": _now(), "total_setups": 0, "symbols": {}}

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.params_path.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")

    def _snapshot(self) -> None:
        if not self.params_path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copyfile(self.params_path, self.snapshots_dir / f"learned_paper_params_{stamp}.json")

    def _write_sample(self, sample: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.samples_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")

    def _load_samples(self) -> list[dict]:
        if not self.samples_path.exists():
            return []
        samples = []
        try:
            for line in self.samples_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    sample = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(sample, dict):
                    samples.append(sample)
        except OSError:
            return []
        return samples

    def _batch_summary(self, batch: list[dict]) -> dict:
        closed = [s for s in batch if _is_realized_close(s)]
        wins = sum(1 for s in closed if s.get("result") == "WIN")
        pnl = sum(_float(s.get("pnl"), 0.0) for s in closed)
        return {
            "setups": len(batch),
            "paper_opens": sum(1 for s in batch if s.get("sample_type") == "PAPER_OPEN"),
            "paper_closes": len(closed),
            "realized_pnl": round(pnl, 6),
            "realized_win_rate": round(wins / len(closed), 4) if closed else 0.0,
            "skipped_setups": sum(1 for s in batch if s.get("sample_type") == "PAPER_SKIP"),
        }

    def _log_performance(self) -> None:
        metrics = self.performance_metrics()
        log.info(
            "[PERFORMANCE] trades=%s win_rate=%s profit_factor=%s expectancy=%s avg_win=%s avg_loss=%s max_drawdown=%s",
            metrics["trades"],
            metrics["win_rate"],
            metrics["profit_factor"],
            metrics["expectancy"],
            metrics["average_win"],
            metrics["average_loss"],
            metrics["max_drawdown"],
        )

    def _summary(self) -> dict:
        symbols = self.state.get("symbols", {})
        total_pnl = 0.0
        total_wins = 0
        total_losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        disabled = []
        learned = {}
        best_symbol = None
        worst_symbol = None
        best_pnl = None
        worst_pnl = None
        for symbol, payload in symbols.items():
            symbol_pnl = 0.0
            learned[symbol] = {}
            for strategy, params in payload.get("strategies", {}).items():
                pnl = _float(params.get("pnl"), 0.0)
                symbol_pnl += pnl
                total_pnl += pnl
                total_wins += int(params.get("wins", 0))
                total_losses += int(params.get("losses", 0))
                if pnl > 0:
                    gross_profit += pnl
                elif pnl < 0:
                    gross_loss += abs(pnl)
                if not params.get("enabled", True):
                    disabled.append({"symbol": symbol, "strategy": strategy})
                learned[symbol][strategy] = {
                    "min_confidence": params.get("min_confidence"),
                    "tp_mult": params.get("tp_mult"),
                    "sl_mult": params.get("sl_mult"),
                    "weight": params.get("weight"),
                }
            if best_pnl is None or symbol_pnl > best_pnl:
                best_symbol, best_pnl = symbol, symbol_pnl
            if worst_pnl is None or symbol_pnl < worst_pnl:
                worst_symbol, worst_pnl = symbol, symbol_pnl
        total_closed = total_wins + total_losses
        return {
            "total_setups": self.state.get("total_setups", 0),
            "paper_opens": total_closed,
            "paper_closes": total_closed,
            "win_rate": round(total_wins / total_closed, 4) if total_closed else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
            "total_pnl": round(total_pnl, 6),
            "best_symbol": best_symbol,
            "worst_symbol": worst_symbol,
            "disabled_strategies": disabled,
            "learned_values": learned,
            "strategy_stats": self.strategy_stats(),
            "positive_expectancy": total_pnl > 0,
        }

    def _log_update(self, symbol: str, strategy: str, field: str, old: object, new: object, reason: str) -> None:
        log.info(
            "[LEARNING] params updated symbol=%s strategy=%s field=%s old=%s new=%s reason=%s",
            symbol,
            strategy,
            field,
            old,
            new,
            reason,
        )


def _default_strategy() -> dict:
    return {
        "enabled": True,
        "min_confidence": 0.60,
        "weight": 1.0,
        "tp_mult": 1.0,
        "sl_mult": 1.0,
        "max_hold_minutes": 30,
        "setups": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(value: object, default: float) -> float:
    out = _maybe_float(value)
    return default if out is None else out


def _maybe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_realized_close(sample: dict) -> bool:
    if sample.get("sample_type") != "PAPER_CLOSE":
        return False
    if _maybe_float(sample.get("pnl")) is None:
        return False
    result = str(sample.get("result") or "").upper()
    close_reason = str(sample.get("close_reason") or "").upper()
    return result in {"WIN", "LOSS"} or close_reason == "TIME_EXIT"


def _empty_strategy_stats() -> dict:
    return {
        "setups_count": 0,
        "paper_opens": 0,
        "paper_closes": 0,
        "paper_skips": 0,
        "wait_count": 0,
        "top_skip_reason": None,
        "wins": 0,
        "losses": 0,
        "pnl": 0,
        "win_rate_closed_only": None,
        "avg_confidence": None,
        "latest_signal": None,
        "latest_status": "ANALYSIS_ONLY",
        "latest_reason": None,
    }


def _confidence_decimal(value: object) -> float | None:
    out = _maybe_float(value)
    if out is None:
        return None
    if out > 1:
        return out / 100.0
    return out


def _latest_status(sample: dict) -> str | None:
    sample_type = str(sample.get("sample_type") or "").upper()
    if sample_type == "PAPER_CLOSE":
        return str(sample.get("result") or "PAPER_CLOSE")
    if sample_type in {"PAPER_OPEN", "PAPER_SKIP", "SETUP"}:
        return sample_type
    return str(sample.get("status") or sample.get("risk_status") or "") or None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
