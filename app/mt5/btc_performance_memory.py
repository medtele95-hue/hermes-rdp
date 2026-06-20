"""BTC Performance Memory — adaptive confidence and defensive mode for HERMES BTC.

Tracks recent closed HERMES BTC trades, manages win/loss streaks, entry pauses,
self-diagnosis of repeating loss patterns, and temporary block rules.

No MT5 calls. No order execution. Pure in-memory performance state.
Singleton: import get_btc_performance_memory() to get the shared instance.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from app.logger import log

_BTC_SCALPING = "BTC_SCALPING_AGENT"
_ORDER_FLOW = "ORDER_FLOW_EXECUTION_AGENT"

_MAX_HISTORY = 50
_RECENT_N = 5
_PATTERN_THRESHOLD = 2          # occurrences before temp block triggers
_WIN_STREAK_CONFIDENT = 3
_WIN_STREAK_VERY_CONFIDENT = 4
_LOSS_STREAK_DEFENSIVE = 3
_LOSS_STREAK_FULL_DEFENSIVE = 4
_PAUSE_MINUTES_DEFENSIVE = 30
_PAUSE_MINUTES_FULL_DEFENSIVE = 60

# How long (minutes) each loss pattern is blocked after detection
_PATTERN_BLOCK_MINUTES: dict[str, int] = {
    "late_entry": 45,
    "against_vwap": 60,
    "against_poc": 45,
    "weak_cvd": 30,
    "high_spread": 30,
    "smc_mtfa_hard_fail": 60,
    "bad_session": 120,
    "before_reversal": 45,
}


class BtcPerformanceMemory:
    """Thread-safe performance tracker for HERMES BTC demo trades."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._trade_history: list[dict] = []
        self._temp_block_rules: dict[str, float] = {}   # pattern → expire_ts (unix)
        self._pause_until: float = 0.0
        self._pause_reason: str = ""
        self._require_aplus_after_pause: bool = False

    # ─── Public write API ────────────────────────────────────────────────────

    def record_trade(self, trade: dict) -> None:
        """Record a closed HERMES BTC trade and update all derived state.

        Caller must include: profit (float), strategy (str), plus optional
        loss-pattern flags (bool): late_entry, against_vwap, against_poc,
        weak_cvd, high_spread, smc_mtfa_hard_fail, bad_session, before_reversal.
        """
        with self._lock:
            self._trade_history.append(dict(trade))
            if len(self._trade_history) > _MAX_HISTORY:
                self._trade_history = self._trade_history[-_MAX_HISTORY:]
            self._update_modes()
            if float(trade.get("profit", 0) or 0) < 0:
                self._diagnose_loss(trade)

    # ─── Public query API ────────────────────────────────────────────────────

    def get_performance_summary(self) -> dict:
        """Return full performance state for the intelligence engine."""
        with self._lock:
            return self._compute_summary()

    def is_entry_paused(self) -> bool:
        with self._lock:
            return time.time() < self._pause_until

    def get_pause_remaining_minutes(self) -> float:
        with self._lock:
            return round(max(0.0, (self._pause_until - time.time()) / 60.0), 1)

    def get_adaptive_mode(self) -> str:
        """Return PAUSED | DEFENSIVE | CONFIDENT | NORMAL."""
        with self._lock:
            return self._get_mode()

    def is_temp_blocked(self, pattern: str) -> bool:
        with self._lock:
            expire = self._temp_block_rules.get(pattern, 0.0)
            if time.time() < expire:
                return True
            self._temp_block_rules.pop(pattern, None)
            return False

    def get_temp_block_rules(self) -> dict:
        """Return currently active temp block rules {pattern: expire_ts}."""
        with self._lock:
            now = time.time()
            active = {p: ts for p, ts in self._temp_block_rules.items() if now < ts}
            self._temp_block_rules = dict(active)
            return dict(active)

    def get_win_streak(self) -> int:
        with self._lock:
            return self._win_streak()

    def get_loss_streak(self) -> int:
        with self._lock:
            return self._loss_streak()

    def requires_aplus(self) -> bool:
        """True if A+ grade is required (set after LOSS_STREAK_4 pause clears)."""
        with self._lock:
            if time.time() < self._pause_until:
                return False   # still paused — no entries at all
            return self._require_aplus_after_pause

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _get_mode(self) -> str:
        if time.time() < self._pause_until:
            return "PAUSED"
        loss_s = self._loss_streak()
        if loss_s >= _LOSS_STREAK_DEFENSIVE:
            return "DEFENSIVE"
        if self._win_streak() >= _WIN_STREAK_CONFIDENT:
            return "CONFIDENT"
        return "NORMAL"

    def _win_streak(self) -> int:
        count = 0
        for t in reversed(self._trade_history):
            if float(t.get("profit", 0) or 0) > 0:
                count += 1
            else:
                break
        return count

    def _loss_streak(self) -> int:
        count = 0
        for t in reversed(self._trade_history):
            if float(t.get("profit", 0) or 0) < 0:
                count += 1
            else:
                break
        return count

    def _update_modes(self) -> None:
        loss_s = self._loss_streak()
        win_s = self._win_streak()

        if loss_s >= _LOSS_STREAK_FULL_DEFENSIVE:
            pause_m = _PAUSE_MINUTES_FULL_DEFENSIVE
            reason = "LOSS_STREAK_4"
            self._pause_until = time.time() + pause_m * 60
            self._pause_reason = reason
            self._require_aplus_after_pause = True
            log.info(
                "[BTC_DEFENSIVE_MODE] decision=PAUSE reason=%s duration_minutes=%s",
                reason, pause_m,
            )
        elif loss_s >= _LOSS_STREAK_DEFENSIVE:
            pause_m = _PAUSE_MINUTES_DEFENSIVE
            reason = "LOSS_STREAK_3"
            self._pause_until = time.time() + pause_m * 60
            self._pause_reason = reason
            # Don't override require_aplus if already set by LOSS_STREAK_4
            if not self._require_aplus_after_pause:
                self._require_aplus_after_pause = False
            log.info(
                "[BTC_DEFENSIVE_MODE] decision=PAUSE reason=%s duration_minutes=%s",
                reason, pause_m,
            )
        elif win_s >= _WIN_STREAK_VERY_CONFIDENT:
            log.info(
                "[BTC_ADAPTIVE_CONFIDENCE] mode=CONFIDENT reason=WIN_STREAK_%s "
                "lot_unchanged=True risk_unchanged=True",
                win_s,
            )
        elif win_s >= _WIN_STREAK_CONFIDENT:
            log.info(
                "[BTC_ADAPTIVE_CONFIDENCE] mode=CONFIDENT reason=WIN_STREAK_%s",
                win_s,
            )

        self._log_performance_summary()

    def _compute_summary(self) -> dict:
        trades = self._trade_history
        if not trades:
            return self._empty_summary()

        recent = trades[-_RECENT_N:]
        recent_profits = [float(t.get("profit", 0) or 0) for t in recent]
        all_profits = [float(t.get("profit", 0) or 0) for t in trades]

        wins = [p for p in all_profits if p > 0]
        losses = [p for p in all_profits if p < 0]
        total = len(all_profits)
        win_rate = len(wins) / total if total > 0 else 0.0

        # Per-strategy win rates
        strat_all: dict[str, list[float]] = {_BTC_SCALPING: [], _ORDER_FLOW: []}
        strat_wins: dict[str, int] = {_BTC_SCALPING: 0, _ORDER_FLOW: 0}
        for t in trades:
            s = str(t.get("strategy", "") or "").upper().strip()
            p = float(t.get("profit", 0) or 0)
            if s in strat_all:
                strat_all[s].append(p)
                if p > 0:
                    strat_wins[s] += 1

        def _wr(s: str) -> float:
            a = strat_all.get(s, [])
            return strat_wins.get(s, 0) / len(a) if a else 0.0

        scalping_wr = _wr(_BTC_SCALPING)
        order_flow_wr = _wr(_ORDER_FLOW)

        # Best / worst strategy — only compare if both have data; else use totals
        has_scalping = bool(strat_all.get(_BTC_SCALPING))
        has_order_flow = bool(strat_all.get(_ORDER_FLOW))
        if has_scalping and has_order_flow:
            best = _BTC_SCALPING if scalping_wr >= order_flow_wr else _ORDER_FLOW
            worst = _ORDER_FLOW if best == _BTC_SCALPING else _BTC_SCALPING
        elif has_scalping:
            best, worst = _BTC_SCALPING, None
        elif has_order_flow:
            best, worst = _ORDER_FLOW, None
        else:
            best, worst = None, None

        win_streak = self._win_streak()
        loss_streak = self._loss_streak()
        mode = self._get_mode()

        # Active temp blocks (clean expired ones)
        now = time.time()
        active_blocks = {p: ts for p, ts in self._temp_block_rules.items() if now < ts}
        self._temp_block_rules = dict(active_blocks)

        return {
            "last_5_results": recent_profits,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "positive_exit_rate": round(win_rate, 3),
            "average_profit": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "average_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "strategy_win_rate": {
                _BTC_SCALPING: round(scalping_wr, 3),
                _ORDER_FLOW: round(order_flow_wr, 3),
            },
            "best_strategy_now": best,
            "worst_strategy_now": worst,
            "adaptive_mode": mode,
            "is_paused": time.time() < self._pause_until,
            "pause_remaining_minutes": self.get_pause_remaining_minutes(),
            "pause_reason": self._pause_reason if time.time() < self._pause_until else "",
            "requires_aplus": self.requires_aplus(),
            "temp_block_rules": list(active_blocks.keys()),
            "total_trades": total,
        }

    def _empty_summary(self) -> dict:
        return {
            "last_5_results": [],
            "win_streak": 0,
            "loss_streak": 0,
            "positive_exit_rate": 0.0,
            "average_profit": 0.0,
            "average_loss": 0.0,
            "strategy_win_rate": {_BTC_SCALPING: 0.0, _ORDER_FLOW: 0.0},
            "best_strategy_now": None,
            "worst_strategy_now": None,
            "adaptive_mode": "NORMAL",
            "is_paused": False,
            "pause_remaining_minutes": 0.0,
            "pause_reason": "",
            "requires_aplus": False,
            "temp_block_rules": [],
            "total_trades": 0,
        }

    def _log_performance_summary(self) -> None:
        s = self._compute_summary()
        log.info(
            "[BTC_PERFORMANCE_MEMORY] win_streak=%s loss_streak=%s "
            "positive_exit_rate=%.3f mode=%s total_trades=%s",
            s["win_streak"], s["loss_streak"],
            s["positive_exit_rate"], s["adaptive_mode"], s["total_trades"],
        )

    def _diagnose_loss(self, trade: dict) -> None:
        """Detect repeated failure patterns in recent losses and add temp blocks."""
        # Pattern flags that should be stored in trade records
        patterns = list(_PATTERN_BLOCK_MINUTES.keys())

        recent_losses = [
            t for t in self._trade_history[-20:]
            if float(t.get("profit", 0) or 0) < 0
        ]

        for pattern in patterns:
            if not bool(trade.get(pattern)):
                continue
            # Count occurrences in recent losses (including this trade)
            count = sum(1 for t in recent_losses if bool(t.get(pattern)))
            if count >= _PATTERN_THRESHOLD:
                duration_m = _PATTERN_BLOCK_MINUTES[pattern]
                expire_ts = time.time() + duration_m * 60
                self._temp_block_rules[pattern] = expire_ts
                log.info(
                    "[BTC_SELF_DIAGNOSIS] pattern=%s action=TEMP_BLOCK "
                    "duration_minutes=%s occurrences=%s",
                    pattern, duration_m, count,
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[BtcPerformanceMemory] = None
_instance_lock = threading.Lock()


def get_btc_performance_memory() -> BtcPerformanceMemory:
    """Return the shared BtcPerformanceMemory singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = BtcPerformanceMemory()
    return _instance
