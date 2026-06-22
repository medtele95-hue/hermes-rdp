"""Fast Smart Exit daemon — tick-fast HERMES BTC position manager.

Runs in a background thread every 250ms (configurable).
Closes HERMES BTC demo positions the moment they turn profitable,
independent of the main analysis cycle.

Constraints:
- No direct MT5 order sending here. Closes are delegated to demo_router.fast_exit_close_position().
- Only acts on DEMO accounts (verified each tick via mt5.account_info()).
- Only manages: symbol in {BTCUSD#, BTCUSD} AND (magic==909002 OR HERMES in comment).
- Does not touch other symbols, other robots, or live accounts.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import MetaTrader5 as mt5

from app.logger import log
from app.mt5.account_mode import is_mt5_demo_account
from app.mt5.smart_rescue import is_hermes_btc_pos
from app.mt5.btc_dynamic_exit import BtcDynamicExit
from app.mt5.btc_sl_engine import BtcSlEngine
from app.mt5.btc_exit_arbiter import select_exit_manager
from app.utils.throttle import log_event_throttled

_dynamic_exit = BtcDynamicExit()
_sl_engine = BtcSlEngine()

# Minimum rescue profit for LOVABLE_BTC fallback mode (no ATR available)
_RESCUE_MIN_PROFIT: float = 0.10


class BtcFastExitDaemon:
    """Background thread that polls HERMES BTC positions every interval_ms milliseconds."""

    def __init__(
        self,
        settings: Any,
        close_fn: Callable[[Any, str], dict],
    ) -> None:
        self.settings = settings
        self._close_fn = close_fn  # demo_router.fast_exit_close_position
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # Per-ticket state — reset when ticket disappears from live positions
        self._min_seen_profit: dict[int, float] = {}
        self._max_seen_profit: dict[int, float] = {}
        self._was_negative: dict[int, bool] = {}
        self._closing_in_progress: dict[int, bool] = {}
        self._sl_engine_last_run: dict[int, float] = {}

        # Log throttle state per ticket (best-effort, no lock needed)
        self._dyn_applied_throttle: dict[int, dict] = {}   # → {"ts", "tp", "trail"}
        self._rescue_throttle: dict[int, dict] = {}        # → {"ts", "threshold", "above"}

        # Dashboard state (published to local state each tick)
        self._last_tick: str | None = None
        self._last_close_ticket: int | None = None
        self._last_close_profit: float | None = None
        self._last_close_reason: str | None = None
        self._last_error: str | None = None
        self._fast_exit_positive_candidates: int = 0

    # ─── Public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        interval_ms = int(getattr(self.settings, "old_btc_fast_exit_interval_ms", 250))
        min_profit = float(getattr(self.settings, "old_btc_fast_exit_min_profit_usd", 0.03))
        hard_min = float(getattr(self.settings, "old_btc_fast_exit_hard_min_profit_usd", 0.01))
        log.info(
            "[OLD_BTC_FAST_EXIT_DAEMON_START] interval_ms=%s min_profit=%s hard_min=%s",
            interval_ms, min_profit, hard_min,
        )
        self._thread = threading.Thread(
            target=self._loop,
            name="hermes-btc-fast-exit",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def get_status(self) -> dict:
        with self._lock:
            return {
                "fast_exit_daemon_enabled": bool(
                    getattr(self.settings, "old_btc_fast_exit_daemon_enabled", True)
                ),
                "fast_exit_interval_ms": int(
                    getattr(self.settings, "old_btc_fast_exit_interval_ms", 250)
                ),
                "fast_exit_last_tick": self._last_tick,
                "fast_exit_positive_candidates": self._fast_exit_positive_candidates,
                "fast_exit_last_close_ticket": self._last_close_ticket,
                "fast_exit_last_close_profit": self._last_close_profit,
                "fast_exit_last_close_reason": self._last_close_reason,
                "fast_exit_last_error": self._last_error,
            }

    # ─── Internal loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        interval_ms = int(getattr(self.settings, "old_btc_fast_exit_interval_ms", 250))
        interval_s = max(0.05, interval_ms / 1000.0)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                err = str(exc)[:200]
                log.warning("[OLD_BTC_FAST_EXIT_ERROR] error=%s", err)
                with self._lock:
                    self._last_error = err
            self._stop_event.wait(interval_s)

    def _is_demo_account(self) -> bool:
        info = mt5.account_info()
        result = is_mt5_demo_account(info)
        log.debug("[OLD_BTC_FAST_EXIT_ACCOUNT_CHECK] is_demo=%s", result)
        return result

    def _tick(self) -> None:
        if not bool(getattr(self.settings, "old_btc_fast_exit_daemon_enabled", True)):
            return

        min_profit = float(getattr(self.settings, "old_btc_fast_exit_min_profit_usd", 0.03))
        hard_min = float(getattr(self.settings, "old_btc_fast_exit_hard_min_profit_usd", 0.01))
        close_any = bool(getattr(self.settings, "old_btc_fast_exit_close_at_any_positive", True))
        magic = int(getattr(self.settings, "demo_magic_number", 909002))

        positions = list(mt5.positions_get() or [])
        live_tickets = {int(getattr(p, "ticket", 0) or 0) for p in positions}

        # Prune closed tickets
        with self._lock:
            for t in list(self._min_seen_profit):
                if t not in live_tickets:
                    self._min_seen_profit.pop(t, None)
                    self._max_seen_profit.pop(t, None)
                    self._was_negative.pop(t, None)
                    self._closing_in_progress.pop(t, None)
                    self._sl_engine_last_run.pop(t, None)
                    self._dyn_applied_throttle.pop(t, None)
                    self._rescue_throttle.pop(t, None)

        hermes_btc = [p for p in positions if is_hermes_btc_pos(p, magic)]
        positive_count = sum(
            1 for p in hermes_btc
            if float(getattr(p, "profit", 0) or 0) >= min_profit
        )

        now_ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_tick = now_ts
            self._fast_exit_positive_candidates = positive_count

        _tick_state = (len(hermes_btc), positive_count)
        log_event_throttled(
            "OLD_BTC_FAST_EXIT_TICK_ZERO_STATE" if _tick_state == (0, 0) else "OLD_BTC_FAST_EXIT_TICK",
            "[OLD_BTC_FAST_EXIT_TICK] open_count=%s positive_count=%s" % _tick_state,
            state=_tick_state,
        )

        self._publish_state()

        if not hermes_btc:
            return

        if not self._is_demo_account():
            log.warning(
                "[OLD_BTC_FAST_EXIT_ERROR] error=ACCOUNT_NOT_DEMO_skipping_tick"
            )
            return

        # Fetch dynamic exit context once per tick (fail-silent; fallback on any error)
        _rates_list: list = []
        _cvd: float | None = None
        _cscore: float = 50.0
        try:
            _raw_rates = mt5.copy_rates_from_pos("BTCUSD#", mt5.TIMEFRAME_M5, 0, 20)
            if _raw_rates is not None:
                _rates_list = [
                    {"high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
                    for r in _raw_rates
                ]
        except Exception:
            pass
        try:
            from app.local_api.state import get_local_state
            _intel = get_local_state().get_btc_intelligence()
            _cscore = float(_intel.get("confluence_score") or 50.0)
            _cvd = _intel.get("cvd_slope")
        except Exception:
            pass

        for pos in hermes_btc:
            self._evaluate_position(pos, min_profit, hard_min, close_any, _rates_list, _cvd, _cscore)

    def _evaluate_position(
        self,
        pos: Any,
        min_profit: float,
        hard_min: float,
        close_any: bool,
        rates_for_dynamic: list | None = None,
        cvd_for_dynamic: float | None = None,
        cscore_for_dynamic: float = 50.0,
    ) -> None:
        ticket = int(getattr(pos, "ticket", 0) or 0)
        profit = float(getattr(pos, "profit", 0) or 0)

        # Update per-ticket tracking
        with self._lock:
            prev_min = self._min_seen_profit.get(ticket, profit)
            new_min = min(prev_min, profit)
            self._min_seen_profit[ticket] = new_min
            prev_max = self._max_seen_profit.get(ticket, 0.0)
            max_profit_seen = max(prev_max, profit)
            self._max_seen_profit[ticket] = max_profit_seen
            if profit < 0:
                self._was_negative[ticket] = True
            was_neg = self._was_negative.get(ticket, False)
            in_progress = self._closing_in_progress.get(ticket, False)

        log.info(
            "[OLD_BTC_FAST_EXIT_ARMED] ticket=%s profit=%s min_seen_profit=%s",
            ticket, round(profit, 4), round(new_min, 4),
        )

        if in_progress:
            log.info(
                "[OLD_BTC_FAST_EXIT_SKIP] ticket=%s reason=CLOSE_ALREADY_IN_PROGRESS",
                ticket,
            )
            return

        # Try dynamic exit computation (fail-closed: on exception, keep fixed values)
        _dir = "BUY" if int(getattr(pos, "type", -1)) == 0 else "SELL"
        _dyn: dict | None = None
        _dyn_mode: str = "fallback"
        try:
            _dyn = _dynamic_exit.compute(
                confluence_score=cscore_for_dynamic,
                rates_m5=rates_for_dynamic or [],
                cvd_slope=cvd_for_dynamic,
                entry_price=float(getattr(pos, "price_open", 0) or 0),
                direction=_dir,
                _log_key=ticket,
            )
            _dyn_mode = _dyn.get("mode", "fallback")
            if _dyn_mode == "dynamic":
                _dyn = _dynamic_exit.adjust_on_tick(
                    current_profit=profit,
                    min_seen_profit=new_min,
                    params=_dyn,
                    cvd_slope=cvd_for_dynamic,
                    direction=_dir,
                )
                _da_now = time.time()
                _da_prev = self._dyn_applied_throttle.get(ticket, {})
                _da_elapsed = _da_now - float(_da_prev.get("ts") or 0)
                _da_tp = _dyn["tp_usd"]
                _da_trail = _dyn["trail_gap_usd"]
                _da_changed = (
                    abs(_da_tp - float(_da_prev.get("tp") or 0)) > 0.001
                    or abs(_da_trail - float(_da_prev.get("trail") or 0)) > 0.001
                )
                if _da_elapsed >= 5.0 or _da_changed:
                    log.info(
                        "[BTC_DYNAMIC_EXIT_ADVISORY] ticket=%s tp_usd=%.3f rr_target=%.2f"
                        " realized_rr=%.4f atr=%.4f trail=%.3f",
                        ticket, _da_tp, _dyn.get("rr_target") or 0,
                        _dyn.get("realized_rr") or 0,
                        _dyn.get("atr_value") or 0, _da_trail,
                    )
                    self._dyn_applied_throttle[ticket] = {"ts": _da_now, "tp": _da_tp, "trail": _da_trail}
            else:
                log.debug("[OLD_BTC_DYNAMIC_EXIT_FALLBACK] ticket=%s reason=ATR_UNAVAILABLE", ticket)
            try:
                from app.local_api.state import get_local_state
                get_local_state().update_btc_intelligence({
                    "atr_m5": _dyn.get("atr_value"),
                    "rr_target": _dyn.get("rr_target"),
                    "exit_mode": _dyn_mode,
                })
            except Exception:
                pass
        except Exception as exc:
            log.warning("[OLD_BTC_DYNAMIC_EXIT_ERROR] ticket=%s error=%s", ticket, str(exc)[:200])
            _dyn = None
            _dyn_mode = "fallback"

        _arbiter_enabled = getattr(self.settings, "btc_exit_arbiter_enabled", True) is True
        if _arbiter_enabled:
            _authority_result = select_exit_manager(
                (_dyn or {}).get("atr_value"),
                str(getattr(pos, "grade", "UNKNOWN") or "UNKNOWN"),
            )
            _exit_authority = str(_authority_result["manager"])
        else:
            _exit_authority = "DYNAMIC" if _dyn_mode == "dynamic" and _dyn is not None else "QUICK"
            _authority_result = {"reason": "LEGACY_TEST_COMPATIBILITY"}
        log.info(
            "[BTC_EXIT_AUTHORITY] ticket=%s manager=%s reason=%s enabled_managers=%s",
            ticket, _exit_authority, _authority_result.get("reason"), _exit_authority,
        )

        # SL Engine — trailing SL/TP modification via MT5 (30 s throttle per ticket)
        if (
            not in_progress
            and _dyn_mode == "dynamic"
            and _dyn is not None
            and (not _arbiter_enabled or _exit_authority in {"SWING", "DYNAMIC"})
        ):
            _atr_for_sl = float(_dyn.get("atr_value") or 0)
            if _atr_for_sl > 0:
                _now = time.time()
                if _now - self._sl_engine_last_run.get(ticket, 0.0) >= 30.0:
                    try:
                        _sl_result = _sl_engine.run(
                            ticket=ticket,
                            entry_price=float(getattr(pos, "price_open", 0) or 0),
                            direction=_dir,
                            atr=_atr_for_sl,
                            current_profit=profit,
                            min_seen_profit=new_min,
                            positive_count=self._fast_exit_positive_candidates,
                            be_buffer_usd=_RESCUE_MIN_PROFIT,
                            exit_mode=_dyn_mode,
                            vwap=None,
                            candles_m5=rates_for_dynamic or [],
                            current_price=float(getattr(pos, "price_current", 0) or 0) or None,
                        )
                        log.info(
                            "[SL_ENGINE_CYCLE] ticket=%s applied=%s method=%s sl=%s reason=%s",
                            ticket,
                            _sl_result.get("applied"),
                            _sl_result.get("method"),
                            _sl_result.get("sl"),
                            _sl_result.get("reason"),
                        )
                        if _sl_result.get("applied"):
                            self._sl_engine_last_run[ticket] = _now
                    except Exception as _sl_exc:
                        log.warning(
                            "[SL_ENGINE_CYCLE_ERROR] ticket=%s error=%s",
                            ticket, str(_sl_exc)[:200],
                        )

        # Determine close reason
        reason: str | None = None
        _is_lovable = (
            str(getattr(self.settings, "hermes_execution_profile", "") or "").upper().strip()
            == "LOVABLE_BTC_OLD_SYSTEM"
        )
        if _arbiter_enabled and _exit_authority == "SWING":
            reason = None
        elif _exit_authority == "DYNAMIC" and _dyn_mode == "dynamic" and _dyn is not None:
            _tp = float(_dyn["tp_usd"])
            _lock = float(_dyn["lock_usd"])
            _trail_start = float(_dyn["trail_start_usd"])
            _trail_gap = float(_dyn["trail_gap_usd"])
            if close_any and profit >= _tp:
                reason = "DYNAMIC_TP_HIT"
            elif close_any and profit >= _trail_start:
                if max_profit_seen - profit >= _trail_gap and profit >= _lock:
                    reason = "DYNAMIC_TRAIL_STOP"
            elif was_neg:
                _rescue_threshold = max(0.05, _lock * 0.3)
                _rth_now = time.time()
                _rth_prev = self._rescue_throttle.get(ticket, {})
                _rth_elapsed = _rth_now - float(_rth_prev.get("ts") or 0)
                _rth_above = profit >= _rescue_threshold
                _rth_prev_above = _rth_prev.get("above", None)
                _rth_crossed = _rth_prev_above is not None and _rth_above != _rth_prev_above
                _rth_thresh_changed = abs(_rescue_threshold - float(_rth_prev.get("threshold") or 0)) > 0.001
                if _rth_elapsed >= 5.0 or _rth_crossed or _rth_thresh_changed:
                    log.info(
                        "[OLD_BTC_RESCUE_THRESHOLD] ticket=%s threshold=%.3f current=%.3f mode=dynamic",
                        ticket, _rescue_threshold, profit,
                    )
                    self._rescue_throttle[ticket] = {"ts": _rth_now, "threshold": _rescue_threshold, "above": _rth_above}
                if profit >= _rescue_threshold:
                    reason = "NEGATIVE_THEN_TINY_POSITIVE"
        else:
            if close_any and profit >= min_profit:
                reason = "ANY_POSITIVE_FAST_EXIT"
            elif was_neg:
                if _is_lovable:
                    _rescue_threshold = _RESCUE_MIN_PROFIT
                    _rth_now_fb = time.time()
                    _rth_prev_fb = self._rescue_throttle.get(ticket, {})
                    _rth_elapsed_fb = _rth_now_fb - float(_rth_prev_fb.get("ts") or 0)
                    _rth_above_fb = profit >= _rescue_threshold
                    _rth_prev_above_fb = _rth_prev_fb.get("above", None)
                    _rth_crossed_fb = _rth_prev_above_fb is not None and _rth_above_fb != _rth_prev_above_fb
                    if _rth_elapsed_fb >= 5.0 or _rth_crossed_fb:
                        log.info(
                            "[OLD_BTC_RESCUE_THRESHOLD] ticket=%s threshold=%.3f current=%.3f mode=fallback_lovable",
                            ticket, _rescue_threshold, profit,
                        )
                        self._rescue_throttle[ticket] = {"ts": _rth_now_fb, "threshold": _rescue_threshold, "above": _rth_above_fb}
                    if profit >= _rescue_threshold and new_min < -0.05:
                        reason = "NEGATIVE_THEN_TINY_POSITIVE"
                elif profit >= hard_min:
                    reason = "NEGATIVE_THEN_TINY_POSITIVE"

        if reason is None:
            return

        log.info(
            "[BTC_EXIT_DECISION] ticket=%s authority=%s action=CLOSE reason=%s",
            ticket, _exit_authority, reason,
        )
        log.info(
            "[OLD_BTC_FAST_EXIT_CLOSE_NOW] ticket=%s profit=%s reason=%s",
            ticket, round(profit, 4), reason,
        )

        with self._lock:
            self._closing_in_progress[ticket] = True

        try:
            result = self._close_fn(pos, reason)
            success = (result or {}).get("status") == "ORDER_CONFIRMED"
            retcode = ((result or {}).get("order_result") or {}).get("retcode")
            if success:
                log.info(
                    "[OLD_BTC_FAST_EXIT_CLOSED] ticket=%s close_profit=%s retcode=%s",
                    ticket, round(profit, 4), retcode,
                )
                with self._lock:
                    self._last_close_ticket = ticket
                    self._last_close_profit = profit
                    self._last_close_reason = reason
                self._publish_state()
            else:
                log.warning(
                    "[OLD_BTC_FAST_EXIT_CLOSE_FAILED] ticket=%s profit=%s retcode=%s error=%s",
                    ticket, round(profit, 4), retcode,
                    (result or {}).get("order_failure_reason"),
                )
                # Allow retry on next tick
                with self._lock:
                    self._closing_in_progress[ticket] = False
        except Exception as exc:
            log.warning(
                "[OLD_BTC_FAST_EXIT_CLOSE_FAILED] ticket=%s profit=%s error=%s",
                ticket, round(profit, 4), str(exc)[:200],
            )
            with self._lock:
                self._closing_in_progress[ticket] = False

    def _publish_state(self) -> None:
        try:
            from app.local_api.state import get_local_state
            get_local_state().update_hermes_btc_status(self.get_status())
        except Exception:
            pass
