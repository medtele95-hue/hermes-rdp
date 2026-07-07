from __future__ import annotations

import json
import math
import threading
import time as _time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5

from app.agents.safety_guard import SafetyGuard
from app.config import Settings, get_settings as _get_settings
from app.logger import log
from app.mt5.account_mode import is_mt5_demo_account
from app.mt5.btc_entry_gate import evaluate_btc_entry_gate
from app.mt5.btc_exit_danger import evaluate_btc_exit_danger
from app.mt5.smart_rescue import (
    SmartRescueConfig,
    count_hermes_btc_open,
    evaluate_rescue,
    is_hermes_btc_pos,
)
from app.services.adaptive_account_policy import AccountPolicy, resolve_account_policy, trading_authorized
from app.services.daily_killswitch import evaluate_daily_killswitch
from app.services.protected_calendar import (
    NewsCalendar,
    weekend_entry_block,
    weekend_flat_close_due,
)
from app.services.exit_v2 import ExitV2Config, evaluate_exit_v2, is_gold_symbol as _exit_v2_is_gold
from app.services.adaptive_confluence_threshold import evaluate_adaptive_confluence
from app.services.mt5_pnl_truth import get_mt5_hermes_pnl_truth
from app.services.quick_exit_manager import QuickExitConfig, hermes_dynamic_exit, manage_quick_exit_position
from app.services.top_down_market_reader import TopDownMarketReader
from app.services.time_engine import TimeEngine, USER_DISABLED_TIME_BLOCK_LOG_REASONS, USER_DISABLED_TIME_BLOCK_REASONS
from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES
from app.utils.risk_math import reward_risk
from app.utils.throttle import log_event_throttled


ALLOWED_DEMO_SYMBOLS = {"BTCUSD#", "BTCUSD", "GOLD#", "GOLD", "GOLDCASH#", "XAUUSD", "XAUUSD#", "EURUSD", "US100Cash#", "US100Cash", "US100", "NAS100", "USTEC"}
GOLD_GENERIC_DISABLED_STRATEGIES = {
    "TREND_CONTINUATION_BREAKDOWN",
    "QUANT_PRO_REGIME_SWITCHING",
    "QUANT_STATISTICAL_PULLBACK",
    "BREAKOUT_RETEST",
    "FIB_OTE_RETEST",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
}
EUR_GENERIC_DISABLED_STRATEGIES = {
    "TREND_CONTINUATION_BREAKDOWN",
    "QUANT_PRO_REGIME_SWITCHING",
    "QUANT_STATISTICAL_PULLBACK",
    "BREAKOUT_RETEST",
    "FIB_OTE_RETEST",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
}
ENTRY_STRATEGIES = set(ACTIVE_EXECUTION_STRATEGIES)
ALLOWED_GOLD_EXECUTION_STRATEGIES = {"SIMO_ATM_BREAKOUT", "FIB_CONFLUENCE_EXECUTION_AGENT", "GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_M1_M5_EMA_SWEEP_SCALPER", "GOLD_ORDER_FLOW_CVD_VWAP", "GOLD_RANGE_BREAKOUT", "ORDER_FLOW_EXECUTION_AGENT"}
OBSERVER_ONLY_STRATEGIES = {"SECOND_ENTRY", "SCALPING_AGENT"}
CONFIRMATION_ONLY_STRATEGIES = {"EMA_PULLBACK"}
EXPLORATION_SESSIONS = {"ASIA_MAIN", "LONDON", "OVERLAP", "NEW_YORK"}
EVENTS_PATH = Path(__file__).resolve().parents[1] / "data" / "demo_pilot_events.jsonl"
BACKEND_START_PATH = Path(__file__).resolve().parents[1] / "data" / "backend_started_at.json"
REPORT_WINDOW_PATH = Path(__file__).resolve().parents[1] / "data" / "demo_report_window.json"
_CAP_BLOCK_REASONS = {
    "MAX_TRADES_PER_SYMBOL_PER_DAY",
    "MAX_TRADES_PER_DAY_TOTAL",
    "MAX_OPEN_TRADES_PER_SYMBOL",
    "MAX_OPEN_TRADES_TOTAL",
    "MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY",
}
_DEMO_TEST_BAD_HOUR_BLOCKERS = {
    "BAD_LIQUIDITY_HOUR",
    "SESSION_NOT_ALLOWED",
    "WAITING_FOR_SESSION",
    "BTC_BAD_HOUR_BLOCK",
    "BTC_BAD_HOUR_BLOCKED",
}
_TOP_DOWN_STRICT_BLOCK_REASONS = {
    "TOP_DOWN_READER_MISSING",
    "TOP_DOWN_READER_FAIL",
    "TOP_DOWN_WAIT_FOR_CONFIRMATION",
    "TOP_DOWN_READER_BLOCK",
}


def _demo_ignore_time_blocks(settings: Settings) -> bool:
    if not getattr(settings, "allow_time_block_override", False):
        return False
    return bool(
        getattr(settings, "demo_ignore_all_time_blocks", False)
        or getattr(settings, "demo_ignore_session_blocks", False)
        or getattr(settings, "demo_ignore_bad_hour_blocks", False)
    )


def _demo_ignore_duration_blocks(settings: Settings) -> bool:
    return bool(
        getattr(settings, "demo_ignore_all_time_blocks", False)
        or getattr(settings, "demo_ignore_duration_blocks", False)
    )


def _with_user_disabled_time_blocks(settings: Settings, time_gate: dict | None) -> dict:
    out = dict(time_gate or {})
    reason = str(out.get("time_gate_reason") or "")
    market_open = out.get("symbol_market_open", out.get("market_open"))
    ignored = bool(out.get("ignored_time_blocks"))
    if _demo_ignore_time_blocks(settings) and market_open is True and reason in USER_DISABLED_TIME_BLOCK_REASONS:
        ignored = True
        out["time_gate_status"] = "PASS"
        out["time_gate_reason"] = "TIME_BLOCKS_DISABLED_BY_USER_ORDER"
        out["is_bad_hour"] = False
        print(
            "[TIME_GATE] decision=PASS reason=TIME_BLOCKS_DISABLED_BY_USER_ORDER ignored=%s"
            % ",".join(USER_DISABLED_TIME_BLOCK_LOG_REASONS)
        )
        print("[TIME_GATE_OVERRIDE] symbol=%s reason=USER_DISABLED_TIME_BLOCKS" % str(out.get("raw_symbol") or "UNKNOWN"))
    if ignored:
        existing = out.get("ignored_time_block_reasons")
        reasons = existing if isinstance(existing, list) else []
        out["ignored_time_blocks"] = True
        out["ignored_time_block_reasons"] = list(dict.fromkeys([*(reasons or []), *USER_DISABLED_TIME_BLOCK_LOG_REASONS]))
    return out


def _max_money_tp_symbol_allowed(settings: Settings, symbol: object) -> bool:
    normalized = str(symbol or "").upper()
    # GOLD hard-excluded whatever the env says: money-TP capping produced
    # TP $5 / SL $69 setups (RR 0.07) on GOLD#.
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return False
    allowed = {item.strip().upper() for item in str(getattr(settings, "max_tp_applies_to", "") or "").split(",") if item.strip()}
    return normalized in allowed


def _final_rr(direction: object, entry: object, sl: object, tp: object) -> float | None:
    """Reward/risk from the FINAL request values; None when not computable."""
    try:
        entry_f = float(entry)
        sl_f = float(sl)
        tp_f = float(tp)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (entry_f, sl_f, tp_f)):
        return None
    if str(direction or "").upper() == "BUY":
        risk = entry_f - sl_f
        reward = tp_f - entry_f
    else:
        risk = sl_f - entry_f
        reward = entry_f - tp_f
    if risk <= 0:
        return None
    return reward / risk


@dataclass(frozen=True)
class DemoDecision:
    decision: str
    reason: str
    event: dict


class DemoKellyRouter:
    def __init__(self, settings: Settings, events_path: Path | None = None) -> None:
        self.settings = settings
        self.events_path = events_path or EVENTS_PATH
        self.time_engine = TimeEngine(settings)
        self.safety_guard = SafetyGuard(settings)
        self.top_down_reader = TopDownMarketReader()
        self.pilot_started_at = _parse_iso(settings.demo_pilot_started_at) or datetime.now(timezone.utc)
        self._startup_logged = False
        self._active_policy: AccountPolicy | None = None
        self.news_calendar = NewsCalendar(settings)
        self._exit_v2_state: dict[int, dict] = {}
        self._quick_exit_state: dict[int, dict] = {}
        self._rescue_states: dict[int, dict] = {}
        self._exit_states: dict[int, dict] = {}   # market-danger per-ticket state
        self._dynamic_exit_state: dict[int, dict] = {}  # §5 R-multiple exit state
        self._events_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.settings.demo_trading and self.settings.demo_only and self.settings.demo_pilot_enabled

    def log_startup(self, account: dict | None = None) -> None:
        diag = self.account_diagnostics(account)
        self.log_account_diag(account)
        account_type = diag["account_type"]
        if self._startup_logged:
            return
        self._startup_logged = True
        log.info(
            "[DEMO_PILOT] enabled=%s hours=%s account_type=%s live_allowed=%s",
            str(self.settings.demo_pilot_enabled).lower(),
            self.settings.demo_pilot_hours,
            account_type,
            str(self.settings.allow_live_trading).lower(),
        )
        log.info(
            "[CONFIG_LOADED] max_tp_usd=%s max_tp_applies_to=%s max_money_tp_enabled=%s demo_max_lot=%s demo_magic=%s",
            self.settings.max_tp_usd,
            self.settings.max_tp_applies_to,
            str(self.settings.max_money_tp_enabled).lower(),
            self.settings.demo_max_lot,
            self.settings.demo_magic_number,
        )

    def mark_backend_started(self, now: datetime | None = None) -> datetime:
        return mark_backend_started(now)

    def log_account_diag(self, account: dict | None) -> dict:
        diag = self.account_diagnostics(account)
        log.info(
            '[ACCOUNT_DIAG] login=%s trade_mode=%s account_type=%s name="%s" server="%s" company="%s" trade_allowed=%s trade_expert=%s',
            diag.get("login"),
            diag.get("trade_mode"),
            diag.get("account_type"),
            diag.get("name") or "",
            diag.get("server") or "",
            diag.get("company") or "",
            diag.get("trade_allowed"),
            diag.get("trade_expert"),
        )
        return diag

    def process_decision(
        self,
        decision: dict,
        kelly_risk: dict | None,
        account: dict | None,
        broker_symbol: str,
        frames: dict | None,
        tick: dict | None,
        symbol_specs: dict | None,
        spread: float,
        max_spread: float,
        mt5_connected: bool,
        setup_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        if not self.enabled and not self.settings.demo_trading:
            return []
        if not _routeable_entry_decision(decision):
            return []
        _dr_strategy = str(decision.get("strategy") or "").upper()
        _dr_symbol = str(broker_symbol or decision.get("broker_symbol") or decision.get("symbol") or "")
        log.info(
            "[DEMO_ROUTER_REACHED] symbol=%s strategy=%s magic=%s",
            _dr_symbol, _dr_strategy, self.settings.demo_magic_number,
        )
        # ADAPTIVE_ACCOUNT_POLICY stage 2: per-order authorization. The boot
        # check alone was the historical trap — both stages must gate.
        auth_ok, auth_reason, auth_policy = trading_authorized(account, self.settings)
        log.info(
            "[ORDER_AUTH] decision=%s reason=%s level=%s symbol=%s strategy=%s",
            "ALLOW" if auth_ok else "BLOCK", auth_reason, auth_policy.level, _dr_symbol, _dr_strategy,
        )
        if not auth_ok:
            event = {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "decision": "BLOCK",
                "reason": auth_reason,
                "failed_gate": auth_reason,
                "symbol": _dr_symbol,
                "broker_symbol": _dr_symbol,
                "strategy": _dr_strategy,
                "direction": decision.get("signal"),
                "account_policy": auth_policy.level,
                "account_policy_detail": auth_policy.as_payload(),
                "setup_id": setup_id,
                "created_at": (now or datetime.now(timezone.utc)).isoformat(),
            }
            self._record_event(event)
            return [self._ingest_event(event)]
        self._active_policy = auth_policy
        evaluated = self.evaluate(
            decision,
            kelly_risk,
            account,
            broker_symbol,
            frames,
            tick,
            symbol_specs,
            spread,
            max_spread,
            mt5_connected,
            setup_id,
            now,
        )
        self._record_event(evaluated.event)
        if evaluated.decision == "BLOCK":
            log.info("[DEMO_SKIP] reason=%s", evaluated.reason)
            return [self._ingest_event(evaluated.event)]

        if _is_simo_pending_order(evaluated.event):
            order_result = self._send_pending_order(evaluated.event)
        else:
            order_result = self._send_order(evaluated.event)
        event = {**evaluated.event, **order_result}
        self._record_event(event)
        return [self._ingest_event(event)]

    def process_quick_exits(
        self,
        account: dict | None = None,
        mt5_connected: bool = True,
        now: datetime | None = None,
        market_contexts: dict | None = None,
    ) -> list[dict]:
        if not bool(getattr(self.settings, "quick_exit_enabled", True)):
            return []
        if not mt5_connected:
            return []
        if not self.settings.demo_trading or not self.settings.demo_only or self.settings.allow_live_trading:
            return []
        diag = self.account_diagnostics(account)
        if bool(getattr(self.settings, "quick_exit_demo_only", True)) and diag.get("account_type") != "DEMO":
            event = _quick_exit_event("QUICK_EXIT_DEMO_ONLY_BLOCK", "BLOCK", {"reason": "ACCOUNT_NOT_DEMO"}, now)
            self._record_event(event)
            return [self._ingest_event(event)]

        cfg = QuickExitConfig(
            tp_usd=float(getattr(self.settings, "quick_exit_tp_usd", 1.50)),
            lock_usd=float(getattr(self.settings, "quick_exit_lock_usd", 0.80)),
            be_buffer_usd=float(getattr(self.settings, "quick_exit_be_buffer_usd", 0.10)),
            trail_start_usd=float(getattr(self.settings, "quick_exit_trail_start_usd", 1.00)),
            trail_gap_usd=float(getattr(self.settings, "quick_exit_trail_gap_usd", 0.60)),
            magic_number=909002,
            demo_only=bool(getattr(self.settings, "quick_exit_demo_only", True)),
        )
        log.info(
            "[OLD_BTC_QUICK_EXIT_CONFIG] tp_usd=%s lock_usd=%s be_buffer_usd=%s trail_start_usd=%s trail_gap_usd=%s",
            cfg.tp_usd, cfg.lock_usd, cfg.be_buffer_usd, cfg.trail_start_usd, cfg.trail_gap_usd,
        )
        positions = list(mt5.positions_get() or [])
        live_tickets = {int(getattr(pos, "ticket", 0) or 0) for pos in positions}
        for ticket in list(self._quick_exit_state):
            if ticket not in live_tickets:
                self._quick_exit_state.pop(ticket, None)
        for ticket in list(self._dynamic_exit_state):
            if ticket not in live_tickets:
                self._dynamic_exit_state.pop(ticket, None)
        for ticket in list(self._exit_v2_state):
            if ticket not in live_tickets:
                self._exit_v2_state.pop(ticket, None)

        items: list[dict] = []
        _quick_exit_closed: set[int] = set()

        # PROTECTED CALENDAR maintenance (positions side, explicit UTC).
        # The network refresh lives in main (boot + daily) — never here.
        _cal_now = now or datetime.now(timezone.utc)
        _hermes_positions = [
            pos for pos in positions
            if int(getattr(pos, "magic", -1) or -1) == cfg.magic_number
        ]
        if bool(getattr(self.settings, "weekend_flat_enabled", True)) and weekend_flat_close_due(_cal_now):
            for pos in _hermes_positions:
                _wf_ticket = int(getattr(pos, "ticket", 0) or 0)
                if _wf_ticket in _quick_exit_closed:
                    continue
                log.info(
                    "[WEEKEND_FLAT] action=CLOSE_ALL ticket=%s symbol=%s profit=%s now_utc=%s",
                    _wf_ticket, getattr(pos, "symbol", None), getattr(pos, "profit", None), _cal_now.isoformat(),
                )
                _wf_action = {
                    "action": "CLOSE",
                    "reason": "WEEKEND_FLAT_CLOSE_ALL",
                    "side": "BUY" if int(getattr(pos, "type", 0) or 0) == 0 else "SELL",
                    "ticket": _wf_ticket,
                }
                try:
                    _wf_event = self._quick_exit_close(pos, _wf_action, now)
                    _wf_event["event_type"] = "WEEKEND_FLAT"
                    self._record_event(_wf_event)
                    items.append(self._ingest_event(_wf_event))
                    _quick_exit_closed.add(_wf_ticket)
                except Exception as _wf_exc:
                    log.warning("[WEEKEND_FLAT] close_failed ticket=%s error=%s", _wf_ticket, str(_wf_exc)[:200])
            return items
        if bool(getattr(self.settings, "news_shield_enabled", True)) and _hermes_positions:
            try:
                _major = self.news_calendar.major_preclose_event(_cal_now)
            except Exception:
                _major = None  # FAIL-SAFE
            if _major:
                for pos in _hermes_positions:
                    _np_ticket = int(getattr(pos, "ticket", 0) or 0)
                    if _np_ticket in _quick_exit_closed:
                        continue
                    if self._position_armed(pos):
                        log.info(
                            "[NEWS_PRECLOSE] ticket=%s decision=KEEP reason=POSITION_ARMED event=%s",
                            _np_ticket, _major.get("title"),
                        )
                        continue
                    log.info(
                        "[NEWS_PRECLOSE] ticket=%s decision=CLOSE reason=NON_ARMED_BEFORE_MAJOR event=%s event_time_utc=%s",
                        _np_ticket, _major.get("title"), _major.get("time_utc"),
                    )
                    _np_action = {
                        "action": "CLOSE",
                        "reason": "NEWS_PRECLOSE_NON_ARMED",
                        "side": "BUY" if int(getattr(pos, "type", 0) or 0) == 0 else "SELL",
                        "ticket": _np_ticket,
                    }
                    try:
                        _np_event = self._quick_exit_close(pos, _np_action, now)
                        _np_event["event_type"] = "NEWS_PRECLOSE"
                        self._record_event(_np_event)
                        items.append(self._ingest_event(_np_event))
                        _quick_exit_closed.add(_np_ticket)
                    except Exception as _np_exc:
                        log.warning("[NEWS_PRECLOSE] close_failed ticket=%s error=%s", _np_ticket, str(_np_exc)[:200])

        for pos in positions:
            _pos_magic = int(getattr(pos, "magic", -1) or -1)
            _pos_comment = str(getattr(pos, "comment", "") or "")
            _pos_ticket = int(getattr(pos, "ticket", 0) or 0)
            if _pos_ticket in _quick_exit_closed:
                continue  # already closed by calendar maintenance this cycle
            _pos_symbol = str(getattr(pos, "symbol", "") or "")
            _pos_profit = float(getattr(pos, "profit", 0) or 0)
            _is_hermes_pos = _pos_magic == cfg.magic_number or "HERMES" in _pos_comment.upper()
            if _pos_magic != cfg.magic_number:
                if _is_hermes_pos:
                    log.info(
                        "[OLD_BTC_POSITION_IGNORED] ticket=%s symbol=%s magic=%s comment=%s reason=MAGIC_MISMATCH",
                        _pos_ticket, _pos_symbol, _pos_magic, _pos_comment,
                    )
                continue
            symbol = _pos_symbol
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            # BLOC 4: GOLD exits belong EXCLUSIVELY to Exit V2. The QUICK_EXIT
            # parasite (TP money $1.50, sneaky lock-SL $0.80, trailing,
            # dynamic exit) is skipped in full for GOLD.
            if _exit_v2_is_gold(symbol):
                for _v2_event in self._process_exit_v2_position(pos, account, now):
                    self._record_event(_v2_event)
                    items.append(self._ingest_event(_v2_event))
                    if str(_v2_event.get("event_type")) == "EXIT_V2_CLOSE":
                        _quick_exit_closed.add(_pos_ticket)
                continue
            # §5: use R-multiple dynamic exit when enabled (gate: hermes_dynamic_exit_enabled)
            _dynamic_enabled = bool(getattr(self.settings, "hermes_dynamic_exit_enabled", False))
            if _dynamic_enabled:
                _exit_ctx = (market_contexts or {}).get(_pos_ticket) or (market_contexts or {}).get(symbol)
                action = hermes_dynamic_exit(pos, tick, info, cfg, self._dynamic_exit_state, _exit_ctx)
            else:
                action = manage_quick_exit_position(pos, tick, info, cfg, self._quick_exit_state)
            action_name = str(action.get("action") or "")
            if action_name == "CLOSE_TP":
                log.info(
                    "[OLD_BTC_QUICK_CLOSE] ticket=%s symbol=%s profit=%s tp_usd=%s",
                    _pos_ticket, symbol, _pos_profit, cfg.tp_usd,
                )
                result_event = self._quick_exit_close(pos, action, now)
                self._record_event(result_event)
                items.append(self._ingest_event(result_event))
                _quick_exit_closed.add(_pos_ticket)
            elif action_name == "MOVE_BREAKEVEN":
                log.info(
                    "[OLD_BTC_BE_SL_MOVED] ticket=%s symbol=%s profit=%s lock_usd=%s be_buffer_usd=%s",
                    _pos_ticket, symbol, _pos_profit, cfg.lock_usd, cfg.be_buffer_usd,
                )
                result_event = self._quick_exit_modify_sl(pos, action, now)
                self._record_event(result_event)
                items.append(self._ingest_event(result_event))
            elif action_name == "TRAIL_SL":
                log.info(
                    "[OLD_BTC_TRAILING_ACTIVE] ticket=%s symbol=%s profit=%s trail_start_usd=%s trail_gap_usd=%s",
                    _pos_ticket, symbol, _pos_profit, cfg.trail_start_usd, cfg.trail_gap_usd,
                )
                result_event = self._quick_exit_modify_sl(pos, action, now)
                self._record_event(result_event)
                items.append(self._ingest_event(result_event))
            elif action_name in {"CLOSE_DANGER", "CLOSE_TIME_STOP"}:
                # §5 dynamic exit: danger score or time stop triggered
                log.info(
                    "[HERMES_DYNAMIC_EXIT_CLOSE] ticket=%s symbol=%s reason=%s danger=%s R_now=%s phase=%s",
                    _pos_ticket, symbol, action.get("reason"), action.get("danger_score"), action.get("R_now"), action.get("phase"),
                )
                result_event = self._quick_exit_close(pos, action, now)
                self._record_event(result_event)
                items.append(self._ingest_event(result_event))
                _quick_exit_closed.add(_pos_ticket)
            elif action_name == "SKIP":
                if _pos_profit >= cfg.lock_usd * 0.5:
                    log.info(
                        "[OLD_BTC_BE_ARMED] ticket=%s symbol=%s profit=%s lock_usd=%s",
                        _pos_ticket, symbol, _pos_profit, cfg.lock_usd,
                    )
                event = _quick_exit_event("QUICK_EXIT_SKIP", "SKIP", action, now)
                self._record_event(event)
                items.append(self._ingest_event(event))

        # ── Smart Rescue Quick Exit ──────────────────────────────────────────
        if bool(getattr(self.settings, "old_btc_smart_quick_exit_enabled", True)):
            rescue_cfg = SmartRescueConfig(
                enabled=True,
                rescue_min_profit_usd=float(getattr(self.settings, "old_btc_rescue_min_profit_usd", 0.08)),
                rescue_arm_drawdown_usd=float(getattr(self.settings, "old_btc_rescue_arm_drawdown_usd", -0.20)),
                emergency_open_count_gt=int(getattr(self.settings, "old_btc_emergency_any_positive_exit_when_open_count_gt", 1)),
                emergency_positive_usd=float(getattr(self.settings, "old_btc_emergency_any_positive_exit_usd", 0.08)),
                max_hold_seconds=int(getattr(self.settings, "old_btc_rescue_max_hold_seconds", 180)),
                protect_existing=bool(getattr(self.settings, "old_btc_protect_existing_positions", True)),
                magic_number=int(cfg.magic_number),
                max_open_positions=int(getattr(self.settings, "old_btc_max_open_positions", 1)),
            )
            # Prune closed positions from rescue state
            for _rt in list(self._rescue_states):
                if _rt not in live_tickets:
                    self._rescue_states.pop(_rt, None)

            _btc_open_count = count_hermes_btc_open(positions, rescue_cfg.magic_number)

            for pos in positions:
                _pos_ticket = int(getattr(pos, "ticket", 0) or 0)
                _pos_symbol_r = str(getattr(pos, "symbol", "") or "").upper()
                _pos_profit_r = float(getattr(pos, "profit", 0) or 0)
                _pos_magic_r = int(getattr(pos, "magic", -1) or -1)

                if not is_hermes_btc_pos(pos, rescue_cfg.magic_number):
                    if _pos_symbol_r in {"BTCUSD#", "BTCUSD"}:
                        log.debug(
                            "[OLD_BTC_RESCUE_SKIP] ticket=%s reason=NOT_HERMES_BTC",
                            _pos_ticket,
                        )
                    continue

                if _pos_ticket in _quick_exit_closed:
                    continue  # already closed by normal quick exit this cycle

                try:
                    rescue_action = evaluate_rescue(
                        pos, self._rescue_states, rescue_cfg, _btc_open_count, now,
                    )
                except Exception as _rexc:
                    log.warning("[OLD_BTC_RESCUE_ERROR] ticket=%s error=%s", _pos_ticket, str(_rexc)[:200])
                    continue

                if rescue_action is None:
                    continue

                _rescue_action_type = str(rescue_action.get("action") or "")

                if _rescue_action_type == "ARMED":
                    log.info(
                        "[OLD_BTC_RESCUE_ARMED] ticket=%s profit=%s min_seen_profit=%s",
                        _pos_ticket, _pos_profit_r, rescue_action.get("min_seen_profit"),
                    )
                    continue  # logging only — no close

                # RESCUE_CLOSE
                _rescue_reason = str(rescue_action.get("reason") or "")
                if _rescue_reason == "EMERGENCY_MULTIPLE_POSITIONS":
                    log.info(
                        "[OLD_BTC_EMERGENCY_POSITIVE_CLOSE] ticket=%s profit=%s open_count=%s",
                        _pos_ticket, _pos_profit_r, rescue_action.get("open_count"),
                    )
                elif _rescue_reason == "AGE_EXCEEDED":
                    log.info(
                        "[OLD_BTC_AGE_POSITIVE_CLOSE] ticket=%s profit=%s age_seconds=%s",
                        _pos_ticket, _pos_profit_r, rescue_action.get("age_seconds"),
                    )
                else:
                    log.info(
                        "[OLD_BTC_RESCUE_CLOSE] ticket=%s profit=%s reason=%s",
                        _pos_ticket, _pos_profit_r, _rescue_reason,
                    )

                try:
                    rescue_event = self._rescue_close(pos, rescue_action, now)
                    self._record_event(rescue_event)
                    items.append(self._ingest_event(rescue_event))
                    _quick_exit_closed.add(_pos_ticket)
                except Exception as _rclose_exc:
                    log.warning(
                        "[OLD_BTC_RESCUE_ERROR] ticket=%s error=%s",
                        _pos_ticket, str(_rclose_exc)[:200],
                    )

        # ── Market Danger Positive Exit ──────────────────────────────────────
        if bool(getattr(self.settings, "old_btc_smart_exit_enabled", True)):
            _danger_threshold = float(getattr(self.settings, "old_btc_danger_exit_min_usd", 0.08))
            _min_signals = int(getattr(self.settings, "old_btc_smart_exit_min_danger_signals", 2))

            # Prune closed positions from exit state
            for _et in list(self._exit_states):
                if _et not in live_tickets:
                    self._exit_states.pop(_et, None)

            _btc_open_cnt = count_hermes_btc_open(positions, int(getattr(self.settings, "demo_magic_number", 909002)))
            _positive_candidates = sum(
                1 for p in positions
                if is_hermes_btc_pos(p, int(getattr(self.settings, "demo_magic_number", 909002)))
                and float(getattr(p, "profit", 0) or 0) >= _danger_threshold
            )
            _scan_emergency = _btc_open_cnt > int(getattr(self.settings, "old_btc_emergency_open_count", 1))
            _scan_state = (_btc_open_cnt, _positive_candidates, _scan_emergency)
            log_event_throttled(
                "OLD_BTC_SMART_EXIT_SCAN",
                "[OLD_BTC_SMART_EXIT_SCAN] open_count=%s positive_candidates=%s emergency=%s"
                % (_btc_open_cnt, _positive_candidates, str(_scan_emergency).lower()),
                state=_scan_state,
            )

            for pos in positions:
                _pos_ticket_d = int(getattr(pos, "ticket", 0) or 0)
                _pos_profit_d = float(getattr(pos, "profit", 0) or 0)
                _pos_sym_d = str(getattr(pos, "symbol", "") or "").upper()

                if not is_hermes_btc_pos(pos, int(getattr(self.settings, "demo_magic_number", 909002))):
                    if _pos_sym_d in {"BTCUSD#", "BTCUSD"}:
                        log.debug("[OLD_BTC_SMART_EXIT_SKIP] ticket=%s reason=NOT_HERMES_BTC", _pos_ticket_d)
                    continue

                if _pos_ticket_d in _quick_exit_closed:
                    continue  # already closed this cycle by rescue or quick-exit

                if _pos_profit_d < _danger_threshold:
                    continue  # profit not enough for danger exit

                # Look up market context for BTCUSD
                _mctx = None
                if market_contexts:
                    for _mk in ("BTCUSD#", "BTCUSD", "btcusd#", "btcusd"):
                        if _mk in market_contexts or _mk.upper() in market_contexts:
                            _mctx = market_contexts.get(_mk) or market_contexts.get(_mk.upper())
                            break

                try:
                    danger = evaluate_btc_exit_danger(pos, _mctx, _min_signals)
                except Exception as _dexc:
                    log.warning("[OLD_BTC_EXIT_DANGER] ticket=%s error=%s", _pos_ticket_d, str(_dexc)[:200])
                    continue

                log.info(
                    "[OLD_BTC_EXIT_DANGER] ticket=%s direction=%s profit=%s danger=%s signals=%s",
                    _pos_ticket_d,
                    danger.get("direction"),
                    _pos_profit_d,
                    str(danger.get("danger")).lower(),
                    ",".join(danger.get("signals") or []) or "NONE",
                )

                if not danger.get("danger"):
                    continue

                # Danger confirmed — close for market-danger reason
                log.info(
                    "[OLD_BTC_SMART_POSITIVE_CLOSE] ticket=%s profit=%s reason=MARKET_DANGER_POSITIVE_EXIT signals=%s",
                    _pos_ticket_d, _pos_profit_d, ",".join(danger.get("signals") or []),
                )
                try:
                    _danger_action = {
                        "action": "RESCUE_CLOSE",
                        "reason": "MARKET_DANGER_POSITIVE_EXIT",
                        "ticket": _pos_ticket_d,
                        "profit": _pos_profit_d,
                        "danger_signals": danger.get("signals"),
                    }
                    danger_event = self._rescue_close(pos, _danger_action, now)
                    retcode = (danger_event.get("order_result") or {}).get("retcode")
                    success = danger_event.get("status") == "ORDER_CONFIRMED"
                    if success:
                        log.info(
                            "[OLD_BTC_SMART_POSITIVE_CLOSED] ticket=%s close_profit=%s retcode=%s",
                            _pos_ticket_d, _pos_profit_d, retcode,
                        )
                    else:
                        log.warning(
                            "[OLD_BTC_SMART_POSITIVE_CLOSE_FAILED] ticket=%s profit=%s retcode=%s error=%s",
                            _pos_ticket_d, _pos_profit_d, retcode,
                            danger_event.get("order_failure_reason"),
                        )
                    self._record_event(danger_event)
                    items.append(self._ingest_event(danger_event))
                    _quick_exit_closed.add(_pos_ticket_d)
                except Exception as _dclose_exc:
                    log.warning(
                        "[OLD_BTC_SMART_POSITIVE_CLOSE_FAILED] ticket=%s error=%s",
                        _pos_ticket_d, str(_dclose_exc)[:200],
                    )

        # Publish BTC status to local dashboard state
        try:
            from app.local_api.state import get_local_state as _get_ls
            _magic = int(getattr(self.settings, "demo_magic_number", 909002))
            _all_pos = list(mt5.positions_get() or [])
            _btc_positions = [p for p in _all_pos if is_hermes_btc_pos(p, _magic)]
            _btc_pnl = sum(float(getattr(p, "profit", 0) or 0) for p in _btc_positions)
            _btc_pos_threshold = float(getattr(self.settings, "old_btc_positive_exit_min_usd", 0.08))
            _btc_positive = sum(1 for p in _btc_positions if float(getattr(p, "profit", 0) or 0) >= _btc_pos_threshold)
            _get_ls().update_hermes_btc_status({
                "open_count": len(_btc_positions),
                "floating_pnl": round(_btc_pnl, 4),
                "positive_candidates": _btc_positive,
                "emergency_active": len(_btc_positions) > int(getattr(self.settings, "old_btc_emergency_open_count", 1)),
            })
        except Exception:
            pass

        return items

    def evaluate(
        self,
        decision: dict,
        kelly_risk: dict | None,
        account: dict | None,
        broker_symbol: str,
        frames: dict | None,
        tick: dict | None,
        symbol_specs: dict | None,
        spread: float,
        max_spread: float,
        mt5_connected: bool,
        setup_id: str | None = None,
        now: datetime | None = None,
    ) -> DemoDecision:
        now_dt = now or datetime.now(timezone.utc)
        raw_symbol = str(decision.get("symbol") or "").upper()
        resolved_symbol = str(broker_symbol or "").upper()
        symbol = resolved_symbol or raw_symbol
        symbol_gate = self._symbol_gate(raw_symbol, resolved_symbol)
        strategy = str(decision.get("strategy") or "").upper()
        account_diag = self.account_diagnostics(account)
        account_type = account_diag["account_type"]
        account_policy = resolve_account_policy(account, self.settings)
        time_gate = _with_user_disabled_time_blocks(self.settings, self.time_engine.evaluate(symbol, frames, tick, now_dt))
        safety = self.safety_guard.evaluate({**decision, **time_gate})
        entry_block_reason = self._entry_candidate_block_reason(decision)
        if entry_block_reason:
            kelly_lot = None
            risk_lot = None
            capped_lot = None
            risk_pct = None
        else:
            kelly_lot = _to_float((kelly_risk or {}).get("approved_lot") or (kelly_risk or {}).get("lot_size"))
            risk_lot = self._risk_lot(decision, account, symbol_specs or {})
            # LOVABLE_BTC uses a fixed demo_max_lot — bypass Kelly when it returns 0 or None
            if (
                str(getattr(self.settings, "hermes_execution_profile", "") or "").upper().strip()
                == "LOVABLE_BTC_OLD_SYSTEM"
                and (kelly_lot is None or kelly_lot <= 0)
            ):
                kelly_lot = float(getattr(self.settings, "demo_max_lot", 0.01))
                log.info(
                    "[KELLY_LOVABLE_FIXED_LOT] kelly_was_zero_or_none overridden to fixed_lot=%s",
                    kelly_lot,
                )
            elif kelly_lot is None or kelly_lot <= 0:
                # Probabilistic Kelly fallback: override to demo_max_lot when the root cause
                # is empty Markov (INVALID_PROBABILITY), zero-edge (NO_POSITIVE_EDGE), or
                # MIN_LOT_EXCEEDS_RISK (risk amount too small for broker minimum lot),
                # provided no hard safety block is present (those must still block).
                _kelly_br = str((kelly_risk or {}).get("blocked_reason") or "")
                _prob_blocks = {"INVALID_PROBABILITY", "NO_POSITIVE_EDGE"}
                _min_lot_blocks = {"MIN_LOT_EXCEEDS_RISK"}
                _rr_blocks = {"REWARD_RISK_BELOW_1_5"}
                _hard_safety_blocks = {"MISSING_EQUITY", "MAX_DAILY_LOSS", "MAX_DRAWDOWN", "READ_ONLY"}
                _actual_kelly_blocks = {b.strip() for b in _kelly_br.split(",") if b.strip()} if _kelly_br else set()
                _has_prob_block = bool(_actual_kelly_blocks & _prob_blocks)
                _has_min_lot = bool(_actual_kelly_blocks & _min_lot_blocks)
                _has_rr_block = bool(_actual_kelly_blocks & _rr_blocks)
                _has_hard_safety = bool(_actual_kelly_blocks & _hard_safety_blocks)
                log.info(
                    "[KELLY_FALLBACK_CHECK] symbol=%s blocked_reason=%r has_prob=%s has_min_lot=%s has_rr=%s has_hard_safety=%s",
                    symbol, _kelly_br, _has_prob_block, _has_min_lot, _has_rr_block, _has_hard_safety,
                )
                if (_has_prob_block or _has_min_lot or _has_rr_block) and not _has_hard_safety:
                    kelly_lot = float(getattr(self.settings, "demo_max_lot", 0.01))
                    log.info(
                        "[KELLY_FALLBACK_TRIGGERED] symbol=%s blocked_by=%s override_to=%.3f",
                        symbol, _kelly_br, kelly_lot,
                    )
            _risk_lot_for_cap = risk_lot if (risk_lot is not None and risk_lot > 0) else None
            capped_lot = min(value for value in [v for v in [kelly_lot, self.settings.demo_max_lot, 0.01, _risk_lot_for_cap] if v is not None])
            if account_policy.enforce_volume_min and capped_lot is not None:
                _policy_vol_min = _to_float((symbol_specs or {}).get("volume_min")) or 0.01
                if capped_lot > _policy_vol_min:
                    log.info(
                        "[ADAPTIVE_POLICY] level=%s lot_capped_to_volume_min from=%s to=%s",
                        account_policy.level, capped_lot, _policy_vol_min,
                    )
                    capped_lot = _policy_vol_min
            risk_pct = self._risk_pct(decision, capped_lot, account, symbol_specs or {})
        rr = _to_float(decision.get("reward_risk") or decision.get("risk_reward"))
        if rr is None:
            rr = reward_risk(_to_float(decision.get("entry")), _to_float(decision.get("sl")), _to_float(decision.get("tp")), decision.get("signal"))
        direction = str(decision.get("signal") or decision.get("resolved_direction") or decision.get("direction") or "").upper()
        setup_grade = str(decision.get("grade") or decision.get("setup_hunter_grade") or decision.get("big_setup_grade") or "").upper()
        m1_trigger_pass = _status_pass(decision.get("m1_trigger_status"), decision.get("m1_entry_confirmation"))
        m15_confirmation_pass = _status_pass(decision.get("m15_confirmation_status"), decision.get("m15_confirmation"))
        sl = _to_float(decision.get("sl"))
        tp = _to_float(decision.get("tp"))
        sl_tp_valid = _valid_sl_tp(direction, _to_float(decision.get("entry")), sl, tp)
        top_down = self._top_down_reading(decision, frames, symbol, direction, sl, tp, spread, max_spread, now_dt)
        wsp = _wsp_payload(decision)
        stats = self._stats(now_dt)
        positions = self._demo_positions()
        open_by_symbol = _positions_by_symbol(positions)
        _mt5_open_for_symbol = open_by_symbol.get(symbol, 0)
        log.info(
            "[POSITION_SYNC_DIAG] symbol=%s mt5_live_open=%s total_mt5_hermes_positions=%s",
            symbol, _mt5_open_for_symbol, len(positions),
        )
        loaded_events = self._load_events()
        open_by_symbol_strategy = _open_orders_by_symbol_strategy(loaded_events, positions)
        last_symbol_trade_at = _last_demo_order_at(loaded_events, symbol)
        cooldown_minutes = int(getattr(self.settings, "symbol_trade_cooldown_minutes", 15))
        cooldown_active = _is_symbol_trade_cooldown_active(
            now_dt,
            last_symbol_trade_at,
            cooldown_minutes,
            enabled=getattr(self.settings, "symbol_trade_cooldown_enabled", False) is True,
        )
        symbol_strategy_key = _symbol_strategy_key(symbol, strategy)
        current_symbol_daily_count = stats["demo_trades_opened_by_symbol"].get(symbol, 0)
        current_symbol_strategy_daily_count = stats["demo_trades_opened_by_symbol_strategy"].get(symbol_strategy_key, 0)
        current_symbol_open_count = open_by_symbol.get(symbol, 0)
        current_symbol_strategy_open_count = open_by_symbol_strategy.get(symbol_strategy_key, 0)
        gates = {
            "mt5_connected": bool(mt5_connected),
            "account_type": account_type,
            "account_trade_mode": account_diag.get("trade_mode"),
            "account_block_reason": account_diag.get("block_reason"),
            "login": account_diag.get("login"),
            "trade_allowed": account_diag.get("trade_allowed"),
            "trade_expert": account_diag.get("trade_expert"),
            "demo_trading": self.settings.demo_trading,
            "demo_only": self.settings.demo_only,
            "allow_live_trading": self.settings.allow_live_trading,
            "demo_pilot_enabled": self.settings.demo_pilot_enabled,
            "pilot_window_active": self._pilot_window_active(now_dt),
            "magic": self.settings.demo_magic_number,
            "raw_symbol": raw_symbol,
            "broker_symbol": resolved_symbol,
            "symbol_resolved": bool(resolved_symbol),
            "symbol_allowed": symbol_gate["symbol_supported"],
            "symbol_trade_allowed": symbol_gate["trade_allowed"],
            "symbol_analysis_only": symbol_gate["analysis_only"],
            "symbol_gate_status": symbol_gate["decision"],
            "symbol_gate_reason": symbol_gate["reason"],
            "symbol_gate_canonical": symbol_gate["canonical_symbol"],
            "hermes_trade_symbols": self.settings.trade_symbol_list,
            "hermes_analysis_only_symbols": self.settings.analysis_only_symbol_list,
            "gold_liquidity_mode": self.settings.gold_liquidity_mode,
            "gold_liquidity_trade_enabled": bool(self.settings.gold_liquidity_trade_enabled),
            "gold_liquidity_strategy_enabled": bool(self.settings.gold_liquidity_strategy_enabled),
            "gold_disable_generic_strategies": bool(self.settings.gold_disable_generic_strategies),
            "allowed_symbol_check": "PASS" if symbol_gate["trade_allowed"] else "BLOCK",
            "entry_block_reason": entry_block_reason,
            "time_gate_status": time_gate.get("time_gate_status"),
            "time_gate_reason": time_gate.get("time_gate_reason"),
            "ignored_time_blocks": bool(time_gate.get("ignored_time_blocks")),
            "ignored_time_block_reasons": time_gate.get("ignored_time_block_reasons") or [],
            "is_bad_hour": time_gate.get("is_bad_hour"),
            "is_weekend": bool(time_gate.get("is_weekend")),
            "market_open": time_gate.get("symbol_market_open"),
            "btc_weekend_bad_hour_allowed": self.settings.demo_allow_btc_weekend_bad_hour,
            "spread_ok": spread <= max_spread,
            "symbol_trade_cooldown_active": cooldown_active,
            "last_symbol_trade_at": last_symbol_trade_at.isoformat() if last_symbol_trade_at else None,
            "open_demo_trades": len(positions),
            "open_demo_trades_total": len(positions),
            "open_demo_trades_by_symbol": open_by_symbol,
            "open_demo_trades_by_symbol_strategy": open_by_symbol_strategy,
            "current_symbol_open_count": current_symbol_open_count,
            "current_symbol_strategy_open_count": current_symbol_strategy_open_count,
            "daily_demo_trades": stats["demo_trades_opened_today"],
            "daily_demo_trades_total": stats["demo_trades_opened_today"],
            "daily_demo_trades_by_symbol": stats["demo_trades_opened_by_symbol"],
            "daily_demo_trades_by_symbol_strategy": stats["demo_trades_opened_by_symbol_strategy"],
            "current_symbol_daily_count": current_symbol_daily_count,
            "current_symbol_strategy_daily_count": current_symbol_strategy_daily_count,
            "daily_exploration_trades": stats["exploration_trades_opened_today"],
            "daily_exploration_trades_total": stats["exploration_trades_opened_today"],
            "daily_exploration_trades_by_symbol": stats["exploration_trades_opened_by_symbol"],
            "current_symbol_exploration_daily_count": stats["exploration_trades_opened_by_symbol"].get(symbol, 0),
            "daily_strong_setup_learning_trades": stats["strong_setup_learning_trades_opened_today"],
            "daily_strong_setup_learning_trades_total": stats["strong_setup_learning_trades_opened_today"],
            "daily_strong_setup_learning_trades_by_symbol": stats["strong_setup_learning_trades_opened_by_symbol"],
            "current_symbol_strong_setup_learning_daily_count": stats["strong_setup_learning_trades_opened_by_symbol"].get(symbol, 0),
            "smoke_test_confirmed_orders": stats["smoke_test_confirmed_orders"],
            "daily_demo_loss_pct": stats["daily_loss_pct"],
            "consecutive_losses": stats["consecutive_losses"],
            "final_lot": capped_lot,
            "risk_pct": risk_pct,
            "rr": rr,
            "edge_score": _to_float(decision.get("edge_score") or decision.get("setup_hunter_score")),
            "setup_grade": setup_grade,
            "m1_trigger_pass": m1_trigger_pass,
            "m15_confirmation_pass": m15_confirmation_pass,
            "direction": direction,
            "sl_tp_valid": sl_tp_valid,
            "safety_guard_status": safety.get("safety_guard_status"),
            "mtfa_status": decision.get("mtfa_status"),
            "mtfa_score": _to_float(decision.get("mtfa_score")),
            "smc_status": decision.get("smc_confluence_status") or decision.get("smc_status"),
            "smc_score": _to_float(decision.get("smc_score") or decision.get("smc_confluence_score")),
            "m15_confirmation": decision.get("m15_confirmation"),
            "m1_entry_confirmation": decision.get("m1_entry_confirmation"),
            "big_setup_grade": decision.get("big_setup_grade"),
            "strategy": strategy,
            "gold_liquidity_hunter": decision.get("gold_liquidity_hunter"),
            "gold_liquidity_signal": decision.get("gold_liquidity_signal"),
            "gold_liquidity_score": decision.get("gold_liquidity_score"),
            "gold_liquidity_reason": decision.get("gold_liquidity_reason"),
            "gold_m1m5_scalper": decision.get("gold_m1m5_scalper"),
            "gold_m1m5_scalper_decision": decision.get("gold_m1m5_scalper_decision"),
            "gold_m1m5_scalper_score": decision.get("gold_m1m5_scalper_score"),
            "gold_m1m5_scalper_reason": decision.get("gold_m1m5_scalper_reason"),
            "gold_order_flow_cvd_vwap": decision.get("gold_order_flow_cvd_vwap"),
            "gold_order_flow_signal": decision.get("gold_order_flow_signal"),
            "gold_order_flow_score": decision.get("gold_order_flow_score"),
            "gold_order_flow_reason": decision.get("gold_order_flow_reason"),
            "order_flow_execution_agent": decision.get("order_flow_execution_agent"),
            "order_flow_execution_agent_score": decision.get("order_flow_execution_agent_score"),
            "order_flow_execution_agent_signal": decision.get("order_flow_execution_agent_signal"),
            "order_flow_execution_agent_reason": decision.get("order_flow_execution_agent_reason"),
            "eur_ema_rsi_atr": decision.get("eur_ema_rsi_atr"),
            "eur_ema_rsi_atr_decision": decision.get("eur_ema_rsi_atr_decision"),
            "eur_ema_rsi_atr_reason": decision.get("eur_ema_rsi_atr_reason"),
            "relaxed_mode_active": bool(decision.get("relaxed_mode_active")),
            "relaxed_reason": decision.get("relaxed_reason"),
            "hours_without_setup": decision.get("hours_without_setup"),
            "strict_threshold": decision.get("strict_threshold"),
            "relaxed_threshold": decision.get("relaxed_threshold"),
            "relaxed_trade_count_today": None,
            "last_relaxed_trade_result": None,
            "top_down_reader": top_down,
            "top_down_decision": top_down.get("decision"),
            "top_down_status": top_down.get("top_down_status"),
            "top_down_score": top_down.get("entry_readiness_score"),
            "top_down_m1_trigger": top_down.get("m1_trigger"),
            "top_down_m15_confirmation": top_down.get("m15_confirmation"),
            "wsp_intelligence": wsp,
            "free_demo_discovery_mode": bool(self.settings.hermes_free_demo_discovery_mode),
        }
        adaptive_confluence = evaluate_adaptive_confluence(symbol, decision, gates, self.settings)
        gates.update(
            {
                "adaptive_confluence": adaptive_confluence,
                "adaptive_confluence_enabled": adaptive_confluence.get("adaptive_confluence_enabled"),
                "final_confluence_score": adaptive_confluence.get("final_confluence_score"),
                "symbol_min_confluence": adaptive_confluence.get("symbol_min_confluence"),
                "confluence_threshold_pass": adaptive_confluence.get("confluence_threshold_pass"),
                "confluence_threshold_reason": adaptive_confluence.get("confluence_threshold_reason"),
                "confluence_components": adaptive_confluence.get("confluence_components"),
                "confluence_mode": adaptive_confluence.get("confluence_mode"),
            }
        )
        if adaptive_confluence.get("adaptive_confluence_enabled"):
            log.info(
                "[ADAPTIVE_CONFLUENCE] symbol=%s score=%s threshold=%s status=%s reason=%s",
                symbol,
                adaptive_confluence.get("final_confluence_score"),
                adaptive_confluence.get("symbol_min_confluence"),
                adaptive_confluence.get("status"),
                adaptive_confluence.get("confluence_threshold_reason"),
            )
        gates.update(self._relaxed_trade_state(decision, symbol, now_dt))
        reason = self._first_block_reason(decision, kelly_lot, capped_lot, risk_lot, gates, time_gate, safety)
        exploration = self._exploration_review(decision, reason, capped_lot, gates, time_gate, safety, now_dt)
        fallback = self._demo_topdown_fallback_review(reason, exploration, gates, capped_lot)
        micro = self._demo_micro_discovery_review(reason, exploration, fallback, gates, capped_lot)
        if fallback["decision"] == "ALLOW":
            reason = None
            gates["topdown_fallback_allowed"] = True
            exploration = {
                **exploration,
                "decision": "ALLOW",
                "block_reason": None,
                "block_reasons": [],
                "warnings": list(dict.fromkeys((exploration.get("warnings") or []) + fallback["warnings"])),
            }
        elif micro["decision"] == "ALLOW":
            reason = None
            gates["topdown_fallback_allowed"] = True
            gates["micro_discovery_allowed"] = True
            exploration = {
                **exploration,
                "decision": "ALLOW",
                "block_reason": None,
                "block_reasons": [],
                "warnings": list(dict.fromkeys((exploration.get("warnings") or []) + micro["warnings"])),
            }
        elif micro["enabled"] and micro.get("block_reason") and not _is_gold_order_flow_gates(gates) and (
            reason is None or reason in _TOP_DOWN_STRICT_BLOCK_REASONS or reason in {"SMC_STRONG_FAIL", "MTFA_STRONG_FAIL"}
        ):
            reason = micro["block_reason"]
        elif fallback["enabled"] and fallback.get("block_reason") and (
            reason is None or reason in _TOP_DOWN_STRICT_BLOCK_REASONS
        ):
            reason = fallback["block_reason"]
        elif micro["enabled"] and micro.get("block_reason") and not _is_gold_order_flow_gates(gates) and reason in {"SMC_STRONG_FAIL", "MTFA_STRONG_FAIL"}:
            reason = micro["block_reason"]
        strict_block_reason = reason
        exploration_override_reason = exploration.get("override_reason")
        mode = "DEMO"
        if fallback["decision"] == "ALLOW":
            mode = "DEMO_ADAPTIVE_FALLBACK"
            exploration_override_reason = "ADAPTIVE_CONFLUENCE_FALLBACK"
        elif micro["decision"] == "ALLOW":
            mode = "DEMO_MICRO_DISCOVERY"
            exploration_override_reason = "MICRO_DISCOVERY_CONFLUENCE_SAMPLE"
        if (
            (reason or exploration_override_reason)
            and exploration["decision"] == "ALLOW"
            and not (micro["enabled"] and micro.get("block_reason"))
            and not (fallback["enabled"] and fallback.get("block_reason"))
        ):
            reason = None
            mode = (
                "DEMO_ADAPTIVE_FALLBACK"
                if fallback["decision"] == "ALLOW"
                else "DEMO_MICRO_DISCOVERY"
                if micro["decision"] == "ALLOW"
                else "DEMO_STRONG_SETUP_LEARNING"
                if exploration_override_reason == "DEMO_STRONG_SETUP_LEARNING_MODE"
                else "DEMO_EXPLORATION"
            )
            if exploration_override_reason is None:
                exploration_override_reason = (
                    "ADAPTIVE_CONFLUENCE_FALLBACK"
                    if fallback["decision"] == "ALLOW"
                    else "MICRO_DISCOVERY_CONFLUENCE_SAMPLE"
                    if micro["decision"] == "ALLOW"
                    else "SOFT_CONFLUENCE_OVERRIDE"
                )
        _old_btc_forced_mode = str(decision.get("old_btc_mode") or "")
        if _old_btc_forced_mode:
            mode = _old_btc_forced_mode
            reason = None  # SafetyGuard PASS already validated; clear SMC/MTFA/confluence blocks
            log.info(
                "[DEMO_ROUTER_REACHED] mode=%s strategy=%s smc_mtfa_strict_bypass=true topdown_bypass=true",
                mode, strategy,
            )
            # Entry gate — shared check for both BTC strategies inside DemoRouter
            if bool(getattr(self.settings, "old_btc_entry_gate_enabled", True)):
                _gate_positions = self._demo_positions()
                _gate_btc_count = count_hermes_btc_open(_gate_positions, self.settings.demo_magic_number)
                _gate_spread = spread if spread is not None else 0.0
                _gate_max_spread = max_spread if max_spread is not None else 999.0
                _gate_confidence = _to_float(
                    decision.get("confidence") or decision.get("normalized_confidence")
                    or decision.get("edge_score") or decision.get("order_flow_execution_agent_score")
                    or decision.get("btc_scalping_confidence")
                )
                _of_snap = (decision.get("order_flow_execution_agent") or {}) if isinstance(decision.get("order_flow_execution_agent"), dict) else {}
                _gate_mctx = _of_snap or None
                entry_gate = evaluate_btc_entry_gate(
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    market_context=_gate_mctx,
                    settings=self.settings,
                    open_btc_count=_gate_btc_count,
                    spread=_gate_spread,
                    max_spread=_gate_max_spread,
                    confidence=_gate_confidence,
                )
                _gate_decision = str(entry_gate.get("decision") or "BLOCK").upper()
                _gate_reason = str(entry_gate.get("reason") or "ENTRY_GATE_BLOCK")
                _gate_score = entry_gate.get("score")
                log.info(
                    "[OLD_BTC_ENTRY_GATE] strategy=%s direction=%s decision=%s%s",
                    strategy, direction, _gate_decision,
                    f" reason={_gate_reason}" if _gate_decision == "BLOCK" else f" score={_gate_score}",
                )
                if _gate_decision == "BLOCK":
                    reason = _gate_reason
        # PROTECTED CALENDAR — weekend flat / post-weekend blackout / news
        # shield. Entry blocks only; position maintenance lives in
        # process_quick_exits. All checks in explicit UTC.
        news_blackout_event = None
        _generic_market_closed = {"MARKET_CLOSED", "WEEKEND_MARKET_CLOSED"}
        if (not reason or reason in _generic_market_closed) and bool(getattr(self.settings, "weekend_flat_enabled", True)):
            weekend_reason = weekend_entry_block(now_dt)
            if weekend_reason:
                reason = weekend_reason
                log.info(
                    "[%s] symbol=%s strategy=%s decision=BLOCK now_utc=%s",
                    "WEEKEND_FLAT" if weekend_reason.startswith("WEEKEND") else "POST_WEEKEND_BLACKOUT",
                    symbol, strategy, now_dt.isoformat(),
                )
        if not reason and bool(getattr(self.settings, "news_shield_enabled", True)):
            try:
                news_blackout_event = self.news_calendar.news_blackout(now_dt)
            except Exception as _news_exc:
                news_blackout_event = None  # FAIL-SAFE: dead feed never blocks
                log.warning("[NEWS_CALENDAR] blackout_check_failed error=%s", str(_news_exc)[:200])
            if news_blackout_event:
                reason = "NEWS_BLACKOUT"
                log.info(
                    "[NEWS_BLACKOUT] symbol=%s strategy=%s decision=BLOCK event=%s event_time_utc=%s",
                    symbol, strategy, news_blackout_event.get("title"),
                    news_blackout_event.get("time_utc"),
                )
        # DAILY_KILLSWITCH — broker-day window, deals re-read on every
        # evaluation (nothing in memory), quota by account policy.
        daily_killswitch = None
        if not reason and mt5_connected:
            try:
                daily_killswitch = evaluate_daily_killswitch(
                    account_policy,
                    self.settings,
                    self.settings.demo_magic_number,
                    account=account,
                    now_utc=now_dt,
                )
                if daily_killswitch.get("triggered"):
                    reason = str(daily_killswitch.get("reason") or "DAILY_KILLSWITCH_TRIGGERED")
            except Exception as _ks_exc:
                reason = "DAILY_KILLSWITCH_HISTORY_UNREADABLE"
                log.warning("[DAILY_KILLSWITCH] evaluate_failed error=%s -> FAIL_CLOSED", str(_ks_exc)[:200])
        # ADAPTIVE_ACCOUNT_POLICY constraints (REAL_UNKNOWN: cap 2%, confluence >= 80)
        if not reason:
            if account_policy.risk_cap_percent is not None and risk_pct is not None and risk_pct > account_policy.risk_cap_percent:
                reason = "POLICY_RISK_CAP_EXCEEDED"
                log.info(
                    "[ADAPTIVE_POLICY] level=%s decision=BLOCK reason=POLICY_RISK_CAP_EXCEEDED risk_pct=%s cap=%s",
                    account_policy.level, risk_pct, account_policy.risk_cap_percent,
                )
            elif account_policy.min_confluence is not None:
                _policy_confluence = _to_float(
                    decision.get("final_confluence_score")
                    or decision.get("confluence_score")
                    or decision.get("confidence")
                )
                if _policy_confluence is None or _policy_confluence < account_policy.min_confluence:
                    reason = "POLICY_MIN_CONFLUENCE_NOT_MET"
                    log.info(
                        "[ADAPTIVE_POLICY] level=%s decision=BLOCK reason=POLICY_MIN_CONFLUENCE_NOT_MET confluence=%s required=%s",
                        account_policy.level, _policy_confluence, account_policy.min_confluence,
                    )
        final_reason = self._final_demo_block_reason(reason, exploration, gates, strategy)
        if final_reason:
            reason = final_reason
        if reason and _old_btc_forced_mode:
            log.info("[DEMO_ROUTER_BLOCK] mode=%s strategy=%s reason=%s", mode, strategy, reason)
        # Open position guard — runs inside DemoRouter for OLD BTC profile
        if _old_btc_forced_mode and not reason:
            _btc_guard_count = count_hermes_btc_open(positions, self.settings.demo_magic_number)
            _max_btc_open = int(getattr(self.settings, "old_btc_max_open_positions", 1) or 1)
            if _btc_guard_count >= _max_btc_open:
                reason = "HERMES_BTC_POSITION_ALREADY_OPEN"
                log.info(
                    "[OLD_BTC_OPEN_POSITION_GUARD] decision=BLOCK reason=HERMES_BTC_POSITION_ALREADY_OPEN count=%s",
                    _btc_guard_count,
                )
        final_demo_decision = "BLOCK" if reason else "PASS"
        cap_block_reason = reason if reason in _CAP_BLOCK_REASONS else next(
            (block_reason for block_reason in exploration.get("block_reasons", []) if block_reason in _CAP_BLOCK_REASONS),
            None,
        )
        status = final_demo_decision
        demo_gate_reason = reason or ("BTC_SCALPING_SIGNAL_VALID" if strategy == "BTC_SCALPING_AGENT" else "PASS")
        order_flow_raw_payload = _gold_order_flow_router_payload(decision, status, demo_gate_reason)
        event = {
            "event_type": "DEMO_SKIP" if reason else "DEMO_ORDER_READY",
            "mode": mode,
            "setup_id": setup_id,
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "broker_symbol": resolved_symbol,
            "allowed_symbol_check": gates["allowed_symbol_check"],
            "symbol_gate_status": symbol_gate["decision"],
            "symbol_gate_decision": symbol_gate["decision"],
            "symbol_gate_reason": symbol_gate["reason"],
            "symbol_gate_canonical": symbol_gate["canonical_symbol"],
            "symbol_analysis_only": symbol_gate["analysis_only"],
            "hermes_trade_symbols": self.settings.trade_symbol_list,
            "hermes_analysis_only_symbols": self.settings.analysis_only_symbol_list,
            "gold_liquidity_mode": self.settings.gold_liquidity_mode,
            "gold_liquidity_trade_enabled": bool(self.settings.gold_liquidity_trade_enabled),
            "gold_liquidity_strategy_enabled": bool(self.settings.gold_liquidity_strategy_enabled),
            "gold_disable_generic_strategies": bool(self.settings.gold_disable_generic_strategies),
            "strategy": strategy,
            "direction": decision.get("signal"),
            "entry": _to_float(decision.get("entry")),
            "sl": _to_float(decision.get("sl")),
            "tp": _to_float(decision.get("tp")),
            "market_bid": _to_float((tick or {}).get("bid") if isinstance(tick, dict) else getattr(tick, "bid", None)),
            "market_ask": _to_float((tick or {}).get("ask") if isinstance(tick, dict) else getattr(tick, "ask", None)),
            "symbol_specs": symbol_specs or {},
            "rr": rr,
            "kelly_suggested_lot": kelly_lot,
            "kelly_blocked_reason": (kelly_risk or {}).get("blocked_reason"),
            "kelly_probability": (kelly_risk or {}).get("probability"),
            "kelly_reward_risk": (kelly_risk or {}).get("reward_risk"),
            "final_capped_lot": capped_lot,
            "risk_lot": risk_lot,
            "risk_pct": risk_pct,
            "magic_number": self.settings.demo_magic_number,
            "comment": self.settings.demo_comment,
            "decision": status,
            "final_demo_decision": final_demo_decision,
            "reason": reason,
            "failed_gate": reason,
            "gate_statuses": gates,
            "time_gate": time_gate,
            "safety_guard": safety,
            "account_type": account_type,
            "account_diagnostics": account_diag,
            "account_policy": account_policy.level,
            "account_policy_detail": account_policy.as_payload(),
            "daily_killswitch": daily_killswitch,
            "strict_block_reason": strict_block_reason,
            "exploration_mode_enabled": bool(self.settings.demo_exploration_mode),
            "demo_strong_setup_learning_mode_enabled": bool(self.settings.demo_strong_setup_learning_mode),
            "demo_smoke_test_24h_enabled": bool(self.settings.demo_smoke_test_24h),
            "demo_test_ignore_bad_hours_enabled": bool(self.settings.demo_test_ignore_bad_hours),
            "ignored_time_blocks": bool(gates.get("ignored_time_blocks")),
            "ignored_time_block_reasons": gates.get("ignored_time_block_reasons") or [],
            "free_demo_discovery_mode": bool(self.settings.hermes_free_demo_discovery_mode),
            "demo_topdown_fallback_mode": bool(self.settings.hermes_demo_topdown_fallback_mode),
            "micro_discovery_mode": bool(self.settings.hermes_demo_micro_discovery_mode),
            "fallback_decision": fallback.get("decision"),
            "fallback_reason": fallback.get("reason"),
            "fallback_block_reason": fallback.get("block_reason"),
            "fallback_warnings": fallback.get("warnings", []),
            "micro_discovery_decision": micro.get("decision"),
            "micro_discovery_reason": micro.get("reason"),
            "micro_discovery_block_reason": micro.get("block_reason"),
            "micro_discovery_warnings": micro.get("warnings", []),
            "adaptive_confluence_enabled": adaptive_confluence.get("adaptive_confluence_enabled"),
            "final_confluence_score": adaptive_confluence.get("final_confluence_score"),
            "symbol_min_confluence": adaptive_confluence.get("symbol_min_confluence"),
            "confluence_threshold_pass": adaptive_confluence.get("confluence_threshold_pass"),
            "confluence_threshold_reason": adaptive_confluence.get("confluence_threshold_reason"),
            "confluence_components": adaptive_confluence.get("confluence_components"),
            "confluence_mode": adaptive_confluence.get("confluence_mode"),
            "demo_smoke_test_started_at": self._smoke_test_started_at().isoformat(),
            "demo_smoke_test_expires_at": self._smoke_test_expires_at().isoformat(),
            "demo_smoke_test_confirmed_orders": stats["smoke_test_confirmed_orders"],
            "exploration_decision": exploration["decision"],
            "exploration_block_reason": exploration.get("block_reason"),
            "exploration_block_reasons": exploration.get("block_reasons", []),
            "exploration_warnings": exploration.get("warnings", []),
            "exploration_override_reason": exploration_override_reason,
            "exploration_ignored_block_reasons": exploration.get("ignored_block_reasons", []),
            "strong_setup_learning_daily_count": stats["strong_setup_learning_trades_opened_today"],
            "daily_demo_trades_total": stats["demo_trades_opened_today"],
            "daily_demo_trades_by_symbol": stats["demo_trades_opened_by_symbol"],
            "daily_demo_trades_by_symbol_strategy": stats["demo_trades_opened_by_symbol_strategy"],
            "daily_exploration_trades_total": stats["exploration_trades_opened_today"],
            "daily_exploration_trades_by_symbol": stats["exploration_trades_opened_by_symbol"],
            "daily_strong_setup_learning_trades_total": stats["strong_setup_learning_trades_opened_today"],
            "daily_strong_setup_learning_trades_by_symbol": stats["strong_setup_learning_trades_opened_by_symbol"],
            "open_demo_trades_total": len(positions),
            "open_demo_trades_by_symbol": open_by_symbol,
            "open_demo_trades_by_symbol_strategy": open_by_symbol_strategy,
            "current_symbol_daily_count": current_symbol_daily_count,
            "current_symbol_strategy_daily_count": current_symbol_strategy_daily_count,
            "current_symbol_open_count": current_symbol_open_count,
            "current_symbol_strategy_open_count": current_symbol_strategy_open_count,
            "cap_block_reason": cap_block_reason,
            "edge_score": _to_float(decision.get("edge_score") or decision.get("setup_hunter_score")),
            "setup_score": _to_float(decision.get("setup_score")),
            "grade": decision.get("grade") or decision.get("setup_hunter_grade") or decision.get("big_setup_grade"),
            "m1_trigger_status": decision.get("m1_trigger_status"),
            "m1_trigger_type": decision.get("m1_trigger_type"),
            "m1_trigger_reason": decision.get("m1_trigger_reason"),
            "m15_confirmation_status": decision.get("m15_confirmation_status"),
            "m15_confirmation_type": decision.get("m15_confirmation_type"),
            "m15_confirmation_reason": decision.get("m15_confirmation_reason"),
            "resolved_direction": decision.get("resolved_direction") or decision.get("signal"),
            "direction_source": decision.get("direction_source"),
            "direction_confidence": decision.get("direction_confidence"),
            "setup_hunter_selected": bool(decision.get("setup_hunter_selected")),
            "setup_hunter_score": decision.get("setup_hunter_score"),
            "setup_hunter_grade": decision.get("setup_hunter_grade"),
            "top_down_status": top_down.get("top_down_status"),
            "top_down_decision": top_down.get("decision"),
            "entry_readiness_score": top_down.get("entry_readiness_score"),
            "market_narrative": top_down.get("market_narrative"),
            "missing_confirmations": top_down.get("missing_confirmations", []),
            "score_breakdown": top_down.get("score_breakdown", {}),
            "wsp_intelligence": wsp,
            "wsp_market_state": wsp.get("market_state"),
            "wsp_htf_trend_alignment": wsp.get("htf_trend_alignment"),
            "wsp_volume_pressure_pct": wsp.get("volume_pressure_pct"),
            "wsp_volume_pressure_label": wsp.get("volume_pressure_label"),
            "wsp_risk_score": wsp.get("risk_score"),
            "wsp_whale_activity": wsp.get("whale_activity"),
            "wsp_mm_sentiment": wsp.get("mm_sentiment"),
            "wsp_z_score": wsp.get("z_score"),
            "wsp_trap_check": wsp.get("trap_check"),
            "wsp_sm_action": wsp.get("sm_action"),
            "wsp_safety_guard_visual": wsp.get("safety_guard_visual"),
            "quant_strategy_enabled": bool(self.settings.hermes_quant_strategy_enabled),
            "quant_slope": decision.get("quant_slope"),
            "quant_r2": decision.get("quant_r2"),
            "quant_z_score": decision.get("quant_z_score"),
            "quant_mean": decision.get("quant_mean"),
            "quant_stdev": decision.get("quant_stdev"),
            "quant_signal": decision.get("quant_signal"),
            "quant_score": decision.get("quant_score"),
            "quant_reason": decision.get("quant_reason"),
            "quant_pro_enabled": bool(self.settings.hermes_quant_pro_enabled),
            "quant_pro_regime": decision.get("quant_pro_regime"),
            "quant_pro_score": decision.get("quant_pro_score"),
            "quant_pro_grade": decision.get("quant_pro_grade"),
            "quant_pro_ols_slope": decision.get("quant_pro_ols_slope"),
            "quant_pro_ols_r2": decision.get("quant_pro_ols_r2"),
            "quant_pro_ols_tstat": decision.get("quant_pro_ols_tstat"),
            "quant_pro_kalman_velocity": decision.get("quant_pro_kalman_velocity"),
            "quant_pro_kalman_z": decision.get("quant_pro_kalman_z"),
            "quant_pro_ou_beta": decision.get("quant_pro_ou_beta"),
            "quant_pro_ou_tstat": decision.get("quant_pro_ou_tstat"),
            "quant_pro_ou_half_life": decision.get("quant_pro_ou_half_life"),
            "quant_pro_hurst": decision.get("quant_pro_hurst"),
            "quant_pro_hurst_filter": decision.get("quant_pro_hurst_filter"),
            "quant_pro_min_trend_hurst": decision.get("quant_pro_min_trend_hurst"),
            "quant_pro_trend_strength": decision.get("quant_pro_trend_strength"),
            "quant_pro_hurst_filter_status": decision.get("quant_pro_hurst_filter_status"),
            "quant_pro_hurst_block_reason": decision.get("quant_pro_hurst_block_reason"),
            "quant_pro_ewma_vol": decision.get("quant_pro_ewma_vol"),
            "quant_pro_signal": decision.get("quant_pro_signal"),
            "quant_pro_reason": decision.get("quant_pro_reason"),
            "gold_liquidity_hunter": decision.get("gold_liquidity_hunter"),
            "gold_liquidity_signal": decision.get("gold_liquidity_signal"),
            "gold_liquidity_score": decision.get("gold_liquidity_score"),
            "gold_liquidity_reason": decision.get("gold_liquidity_reason"),
            "gold_m1m5_scalper": decision.get("gold_m1m5_scalper"),
            "gold_m1m5_scalper_decision": decision.get("gold_m1m5_scalper_decision"),
            "gold_m1m5_scalper_score": decision.get("gold_m1m5_scalper_score"),
            "gold_m1m5_scalper_reason": decision.get("gold_m1m5_scalper_reason"),
            "gold_order_flow_cvd_vwap": decision.get("gold_order_flow_cvd_vwap"),
            "gold_order_flow_signal": decision.get("gold_order_flow_signal"),
            "gold_order_flow_score": decision.get("gold_order_flow_score"),
            "gold_order_flow_reason": decision.get("gold_order_flow_reason"),
            "order_flow_execution_agent": decision.get("order_flow_execution_agent"),
            "order_flow_execution_agent_score": decision.get("order_flow_execution_agent_score"),
            "order_flow_execution_agent_signal": decision.get("order_flow_execution_agent_signal"),
            "order_flow_execution_agent_reason": decision.get("order_flow_execution_agent_reason"),
            "eur_ema_rsi_atr": decision.get("eur_ema_rsi_atr"),
            "eur_ema_rsi_atr_decision": decision.get("eur_ema_rsi_atr_decision"),
            "eur_ema_rsi_atr_reason": decision.get("eur_ema_rsi_atr_reason"),
            "relaxed_mode_active": bool(decision.get("relaxed_mode_active")),
            "relaxed_reason": decision.get("relaxed_reason"),
            "hours_without_setup": decision.get("hours_without_setup"),
            "strict_threshold": decision.get("strict_threshold"),
            "relaxed_threshold": decision.get("relaxed_threshold"),
            "relaxed_trade_count_today": gates.get("relaxed_trade_count_today"),
            "last_relaxed_trade_result": gates.get("last_relaxed_trade_result"),
            "raw_payload": order_flow_raw_payload if order_flow_raw_payload else decision.get("raw_payload"),
            "old_btc_mode": decision.get("old_btc_mode") or "",
            "trace_id": decision.get("trace_id") or "",
            "created_at": now_dt.isoformat(),
        }
        log.info("[KELLY_DEMO] symbol=%s kelly_lot=%s capped_lot=%s risk_pct=%s", symbol, kelly_lot, capped_lot, risk_pct)
        log.info("[SYMBOL_GATE] symbol=%s decision=%s reason=%s", symbol, symbol_gate["decision"], symbol_gate["reason"])
        log.info(
            "[DEMO_GATE] symbol=%s strategy=%s decision=%s reason=%s raw_symbol=%s broker_symbol=%s allowed_symbol_check=%s",
            symbol,
            strategy,
            status,
            demo_gate_reason,
            raw_symbol,
            resolved_symbol,
            gates["allowed_symbol_check"],
        )
        if gates.get("ignored_time_blocks"):
            log.info("[DEMO_GATE] ignored_time_blocks=true reason=USER_DISABLED_TIME_BLOCKS")
        if reason:
            log.info("[ROUTER] symbol=%s strategy=%s decision=BLOCK reason=%s", symbol, strategy, reason)
        else:
            log.info("[ROUTER] symbol=%s strategy=%s decision=ROUTE_TO_DEMO", symbol, strategy)
        return DemoDecision(status, reason or "PASS", event)

    def account_type(self, account: dict | None = None) -> str:
        return self.account_diagnostics(account)["account_type"]

    def account_diagnostics(self, account: dict | None = None) -> dict:
        payload = dict(account or {})
        trade_mode = payload.get("trade_mode")
        account_type = _trade_mode_account_type(trade_mode)
        block_reason = None
        if account_type == "LIVE":
            block_reason = "ACCOUNT_TRADE_MODE_REAL"
        elif account_type == "CONTEST" and not self.settings.demo_allow_contest:
            block_reason = "ACCOUNT_TRADE_MODE_CONTEST_BLOCKED"
        elif account_type == "UNKNOWN":
            block_reason = "ACCOUNT_TRADE_MODE_UNKNOWN"
        if payload.get("trade_allowed") is False:
            block_reason = block_reason or "TRADE_ALLOWED_FALSE"
        if payload.get("trade_expert") is False:
            block_reason = block_reason or "TRADE_EXPERT_FALSE"
        # ADAPTIVE_ACCOUNT_POLICY: a DEMO account (trade_mode) carries no login
        # pin — trade_mode detection IS the protection. The allowlist only
        # still applies to non-DEMO accounts as a legacy belt.
        allowed_login = str(self.settings.demo_allowed_login or "").strip()
        login = payload.get("login")
        if account_type != "DEMO" and allowed_login and str(login or "") != allowed_login:
            block_reason = block_reason or "LOGIN_NOT_ALLOWLISTED"
        return {
            "login": login,
            "trade_mode": trade_mode,
            "account_type": account_type,
            "name": payload.get("name"),
            "server": payload.get("server"),
            "company": payload.get("company"),
            "trade_allowed": payload.get("trade_allowed"),
            "trade_expert": payload.get("trade_expert"),
            "block_reason": block_reason,
        }

    def _symbol_gate(self, raw_symbol: str, broker_symbol: str) -> dict:
        symbol = str(broker_symbol or raw_symbol or "").upper()
        canonical = _canonical_trade_symbol(symbol)
        trade_symbols = {_canonical_trade_symbol(item) for item in self.settings.trade_symbol_list}
        analysis_only_symbols = {_canonical_trade_symbol(item) for item in self.settings.analysis_only_symbol_list}
        supported_symbols = {_canonical_trade_symbol(item) for item in ALLOWED_DEMO_SYMBOLS}
        simo_canonicals = {_canonical_trade_symbol(s) for s in getattr(self.settings, "simo_atm_symbol_list", [])}
        supported_symbols |= simo_canonicals
        if canonical in simo_canonicals:
            trade_symbols.add(canonical)
        raw_canonical = _canonical_trade_symbol(raw_symbol)
        broker_canonical = _canonical_trade_symbol(broker_symbol)
        symbol_supported = bool(broker_symbol) and broker_canonical in supported_symbols
        analysis_only = canonical in analysis_only_symbols or raw_canonical in analysis_only_symbols or broker_canonical in analysis_only_symbols
        if canonical in simo_canonicals:
            analysis_only = False
        if canonical == "GOLD" and self.settings.gold_liquidity_trade_enabled and canonical in trade_symbols:
            analysis_only = False
        trade_allowed = bool(symbol_supported and not analysis_only and canonical in trade_symbols)
        if canonical == "GOLD":
            trade_allowed = bool(symbol_supported and canonical in trade_symbols and self.settings.gold_liquidity_trade_enabled and self.settings.gold_liquidity_strategy_enabled)
        reason = "SYMBOL_ALLOWED_FOR_DEMO" if trade_allowed else "SYMBOL_ANALYSIS_ONLY" if analysis_only else "SYMBOL_NOT_ALLOWED"
        if canonical == "GOLD" and not trade_allowed and not analysis_only:
            reason = "GOLD_ANALYSIS_ONLY"
        if not broker_symbol:
            reason = "SYMBOL_NOT_RESOLVED"
        return {
            "symbol": symbol,
            "canonical_symbol": canonical,
            "symbol_supported": symbol_supported,
            "analysis_only": analysis_only,
            "trade_allowed": trade_allowed,
            "decision": "PASS" if trade_allowed else "BLOCK",
            "reason": reason,
        }

    def _symbol_gate_block_reason(self, gates: dict) -> str | None:
        if not gates.get("symbol_resolved"):
            return "SYMBOL_NOT_RESOLVED"
        if not gates.get("symbol_allowed"):
            return "SYMBOL_NOT_ALLOWED"
        if not gates.get("symbol_trade_allowed"):
            return str(gates.get("symbol_gate_reason") or "SYMBOL_ANALYSIS_ONLY")
        strategy = str(gates.get("strategy") or "").upper()
        if (
            self.settings.btc_disable_quant_statistical_pullback
            and strategy == "QUANT_STATISTICAL_PULLBACK"
            and _is_btc(gates.get("broker_symbol") or gates.get("raw_symbol") or gates.get("symbol"))
        ):
            reason = "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT"
            log.info("[STRATEGY_GATE] symbol=%s strategy=%s decision=BLOCK reason=%s", gates.get("broker_symbol") or gates.get("symbol"), strategy, reason)
            return reason
        if strategy == "BTC_SCALPING_AGENT" and not _is_btc(gates.get("broker_symbol") or gates.get("raw_symbol") or gates.get("symbol")):
            return "BTC_SCALPING_AGENT_SYMBOL_NOT_BTC"
        if strategy == "GOLD_M1_M5_EMA_SWEEP_SCALPER" and not _is_gold_symbol(gates.get("symbol_gate_canonical") or gates.get("broker_symbol") or gates.get("raw_symbol")):
            return "GOLD_M1_M5_SYMBOL_NOT_GOLD"
        if strategy == "GOLD_ORDER_FLOW_CVD_VWAP" and not _is_gold_symbol(gates.get("symbol_gate_canonical") or gates.get("broker_symbol") or gates.get("raw_symbol")):
            return "GOLD_ORDER_FLOW_SYMBOL_NOT_GOLD"
        if _is_gold_symbol(gates.get("symbol_gate_canonical") or gates.get("broker_symbol")):
            if strategy not in ALLOWED_GOLD_EXECUTION_STRATEGIES:
                if str(gates.get("direction") or "").upper() not in {"BUY", "SELL"}:
                    return None
                if self.settings.gold_disable_generic_strategies:
                    return "GOLD_GENERIC_STRATEGY_DISABLED"
                return "GOLD_GENERIC_STRATEGY_DISABLED"
            if strategy == "GOLD_M1_M5_EMA_SWEEP_SCALPER":
                scalper_reason = self._gold_m1m5_scalper_block_reason(gates)
                if scalper_reason:
                    return scalper_reason
                return None
            if strategy == "GOLD_ORDER_FLOW_CVD_VWAP":
                order_flow_reason = self._gold_order_flow_block_reason(gates)
                if order_flow_reason:
                    return order_flow_reason
                return None
            if strategy == "GOLD_RANGE_BREAKOUT":
                range_reason = self._gold_range_breakout_block_reason(gates)
                if range_reason:
                    return range_reason
                return None
            if strategy == "ORDER_FLOW_EXECUTION_AGENT":
                return None
            gold_reason = self._gold_liquidity_block_reason(gates)
            if gold_reason:
                return gold_reason
        if bool(getattr(self.settings, "eur_ema_rsi_atr_enabled", True)) and _is_eurusd(gates.get("symbol_gate_canonical") or gates.get("broker_symbol") or gates.get("raw_symbol")):
            if strategy not in {"SIMO_ATM_BREAKOUT", "EUR_EMA_RSI_ATR_CROSSOVER", "FIB_CONFLUENCE_EXECUTION_AGENT", "ORDER_FLOW_EXECUTION_AGENT"}:
                if strategy in EUR_GENERIC_DISABLED_STRATEGIES:
                    return "EUR_GENERIC_STRATEGY_DISABLED"
                return "EUR_GENERIC_STRATEGY_DISABLED"
            if strategy in {"SIMO_ATM_BREAKOUT", "ORDER_FLOW_EXECUTION_AGENT"}:
                return None
            eur_reason = self._eur_ema_rsi_atr_block_reason(gates)
            if eur_reason:
                return eur_reason
        return None

    def _eur_ema_rsi_atr_block_reason(self, gates: dict) -> str | None:
        payload = gates.get("eur_ema_rsi_atr") if isinstance(gates.get("eur_ema_rsi_atr"), dict) else {}
        decision = str(payload.get("decision") or gates.get("direction") or "").upper()
        block_reason = payload.get("block_reason")
        if decision == "WAIT":
            return "EUR_EMA_RSI_ATR_WAIT"
        if decision == "BLOCK":
            return str(block_reason or "EUR_EMA_RSI_ATR_BLOCK")
        if decision not in {"BUY", "SELL"}:
            return "EUR_EMA_RSI_ATR_WAIT"
        rr = _to_float(payload.get("rr") or gates.get("rr"))
        if rr is None or rr < float(getattr(self.settings, "eur_rr", 2.0)):
            return "EUR_EMA_RSI_ATR_INVALID_RR"
        if int(gates.get("current_symbol_open_count") or 0) >= int(getattr(self.settings, "max_open_eur_trades", 1)):
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        return None

    def _gold_liquidity_block_reason(self, gates: dict) -> str | None:
        if not self.settings.gold_liquidity_trade_enabled or not self.settings.gold_liquidity_strategy_enabled:
            return "GOLD_ANALYSIS_ONLY"
        hunter = gates.get("gold_liquidity_hunter") if isinstance(gates.get("gold_liquidity_hunter"), dict) else {}
        decision = str(hunter.get("decision") or gates.get("direction") or "").upper()
        block_reason = hunter.get("block_reason")
        if decision == "WAIT":
            return str(block_reason or "GOLD_LIQUIDITY_WAIT")
        if decision == "BLOCK":
            return str(block_reason or "GOLD_LIQUIDITY_BLOCK")
        if decision not in {"BUY", "SELL"}:
            return "GOLD_LIQUIDITY_WAIT"
        signal = str(hunter.get("reversal_signal") or gates.get("gold_liquidity_signal") or "").upper()
        if signal not in set(self.settings.gold_allowed_signal_list):
            return f"GOLD_SIGNAL_{signal}_OBSERVER_ONLY" if signal in set(self.settings.gold_observer_signal_list) else "GOLD_LIQUIDITY_WAIT"
        if str(hunter.get("sweep_side") or "").upper() == "BSL" and (decision != "SELL" or hunter.get("premium_discount") != "PREMIUM"):
            return "GOLD_BSL_REQUIRES_SELL_PREMIUM"
        if str(hunter.get("sweep_side") or "").upper() == "SSL" and (decision != "BUY" or hunter.get("premium_discount") != "DISCOUNT"):
            return "GOLD_SSL_REQUIRES_BUY_DISCOUNT"
        if int(hunter.get("zone_stars") or 0) < self.settings.gold_min_zone_stars:
            return "GOLD_ZONE_STARS_TOO_LOW"
        if (_to_float(hunter.get("liquidity_score")) or 0.0) < self.settings.gold_min_liquidity_score:
            return "GOLD_LIQUIDITY_SCORE_TOO_LOW"
        rr_plan = hunter.get("rr_plan") if isinstance(hunter.get("rr_plan"), dict) else {}
        rr = _to_float(rr_plan.get("rr") or gates.get("rr"))
        if rr is None or rr < self.settings.gold_min_rr:
            return "GOLD_RR_BELOW_2"
        if not _demo_ignore_time_blocks(self.settings):
            if gates.get("time_gate_status") != "PASS":
                return str(gates.get("time_gate_reason") or "TIME_GATE_BLOCK")
            if gates.get("time_gate_reason") == "BAD_LIQUIDITY_HOUR" or gates.get("is_bad_hour"):
                return "BAD_LIQUIDITY_HOUR"
        if int(gates.get("current_symbol_open_count") or 0) >= self.settings.gold_max_open_trades:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        return None

    def _gold_m1m5_scalper_block_reason(self, gates: dict) -> str | None:
        if not self.settings.gold_liquidity_trade_enabled or not self.settings.gold_liquidity_strategy_enabled:
            return "GOLD_ANALYSIS_ONLY"
        payload = gates.get("gold_m1m5_scalper") if isinstance(gates.get("gold_m1m5_scalper"), dict) else {}
        decision = str(payload.get("decision") or gates.get("direction") or "").upper()
        block_reason = payload.get("block_reason")
        if decision == "WAIT":
            return str(block_reason or "GOLD_M1_M5_WAIT")
        if decision == "BLOCK":
            return str(block_reason or "GOLD_M1_M5_BLOCK")
        if decision not in {"BUY", "SELL"}:
            return "GOLD_M1_M5_WAIT"
        rr = _to_float(payload.get("rr") or gates.get("rr"))
        if rr is None or rr < 1.5:
            return "GOLD_M1_M5_INVALID_RR"
        if not _demo_ignore_time_blocks(self.settings):
            if gates.get("time_gate_status") != "PASS":
                return str(gates.get("time_gate_reason") or "TIME_GATE_BLOCK")
            if gates.get("time_gate_reason") == "BAD_LIQUIDITY_HOUR" or gates.get("is_bad_hour"):
                return "BAD_LIQUIDITY_HOUR"
        if int(gates.get("current_symbol_open_count") or 0) >= self.settings.gold_max_open_trades:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        return None

    def _gold_order_flow_block_reason(self, gates: dict) -> str | None:
        if not self.settings.gold_liquidity_trade_enabled or not self.settings.gold_liquidity_strategy_enabled:
            return "GOLD_ANALYSIS_ONLY"
        if not bool(getattr(self.settings, "gold_order_flow_execution_enabled", False)):
            return "GOLD_ORDER_FLOW_EXECUTION_DISABLED"
        payload = gates.get("gold_order_flow_cvd_vwap") if isinstance(gates.get("gold_order_flow_cvd_vwap"), dict) else {}
        if _gold_order_flow_missing_required_fields(payload, gates, self.settings):
            return "ORDER_FLOW_REQUIRED_FIELDS_MISSING"
        decision = str(payload.get("decision") or gates.get("direction") or "").upper()
        block_reason = payload.get("block_reason")
        if decision == "WAIT":
            return str(block_reason or "GOLD_ORDER_FLOW_WAIT")
        if decision == "BLOCK":
            return str(block_reason or "GOLD_ORDER_FLOW_BLOCK")
        if decision not in {"BUY", "SELL"}:
            return "GOLD_ORDER_FLOW_WAIT"
        score = _to_float(payload.get("confidence") or gates.get("gold_order_flow_score") or gates.get("edge_score"))
        if score is None or score < float(getattr(self.settings, "gold_order_flow_min_confidence", 70)):
            return "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70"
        if bool(getattr(self.settings, "gold_order_flow_require_divergence", True)) and not payload.get("divergence"):
            return "GOLD_ORDER_FLOW_NO_DIVERGENCE"
        rr = _to_float(payload.get("rr") or gates.get("rr"))
        if rr is None or rr < 1.5:
            return "GOLD_ORDER_FLOW_INVALID_RR"
        if not _demo_ignore_time_blocks(self.settings):
            if gates.get("time_gate_status") != "PASS":
                return str(gates.get("time_gate_reason") or "TIME_GATE_BLOCK")
            if gates.get("time_gate_reason") == "BAD_LIQUIDITY_HOUR" or gates.get("is_bad_hour"):
                return "BAD_LIQUIDITY_HOUR"
        if int(gates.get("current_symbol_open_count") or 0) >= self.settings.gold_max_open_trades:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        return None

    def _gold_range_breakout_block_reason(self, gates: dict) -> str | None:
        if not bool(getattr(self.settings, "gold_range_breakout_enabled", False)):
            return "GOLD_RANGE_BREAKOUT_DISABLED"
        if not bool(gates.get("gold_range_breakout_ready")):
            return "GOLD_RANGE_BREAKOUT_NOT_READY"
        direction = str(gates.get("direction") or gates.get("signal") or "").upper()
        if direction not in {"BUY", "SELL"}:
            return "GOLD_RANGE_BREAKOUT_NO_SIGNAL"
        rr = _to_float(gates.get("risk_reward") or gates.get("reward_risk") or gates.get("rr"))
        if rr is None or rr < 1.5:
            return "GOLD_RANGE_BREAKOUT_RR_TOO_LOW"
        score = _to_float(gates.get("gold_range_breakout_score") or gates.get("confidence"))
        if score is None or score < 65.0:
            return "GOLD_RANGE_BREAKOUT_SCORE_TOO_LOW"
        if not _demo_ignore_time_blocks(self.settings):
            if gates.get("time_gate_status") != "PASS":
                return str(gates.get("time_gate_reason") or "TIME_GATE_BLOCK")
        if int(gates.get("current_symbol_open_count") or 0) >= self.settings.gold_max_open_trades:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        return None

    def _order_flow_exec_agent_block_reason(self, gates: dict) -> str | None:
        if not bool(getattr(self.settings, "order_flow_execution_enabled", False)):
            return "ORDER_FLOW_EXECUTION_DISABLED"
        payload = gates.get("order_flow_execution_agent") if isinstance(gates.get("order_flow_execution_agent"), dict) else {}
        decision = str(
            gates.get("direction") or gates.get("signal") or ""
        ).upper()
        if decision not in {"BUY", "SELL"}:
            return str(gates.get("reason") or "ORDER_FLOW_EXEC_WAIT")
        score = _to_float(gates.get("order_flow_execution_agent_score") or gates.get("edge_score"))
        min_score = int(getattr(self.settings, "order_flow_min_score", 75))
        if score is None or score < min_score:
            return "ORDER_FLOW_SCORE_BELOW_THRESHOLD"
        rr = _to_float(gates.get("rr"))
        min_rr = float(getattr(self.settings, "order_flow_min_rr", 1.5))
        if rr is None or rr < min_rr:
            return "ORDER_FLOW_RR_BELOW_MIN"
        if not _demo_ignore_time_blocks(self.settings):
            if gates.get("time_gate_status") != "PASS":
                return str(gates.get("time_gate_reason") or "TIME_GATE_BLOCK")
        if int(gates.get("current_symbol_open_count") or 0) >= self.settings.demo_max_open_trades_per_symbol:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        log.info(
            "[DEMO_GATE] symbol=%s strategy=ORDER_FLOW_EXECUTION_AGENT decision=PASS reason=ORDER_FLOW_EXEC_AGENT_PASS",
            gates.get("broker_symbol") or gates.get("raw_symbol"),
        )
        return None

    def _first_block_reason(
        self,
        decision: dict,
        kelly_lot: float | None,
        capped_lot: float | None,
        risk_lot: float | None,
        gates: dict,
        time_gate: dict,
        safety: dict,
    ) -> str | None:
        strategy = str(decision.get("strategy") or gates.get("strategy") or "").upper()
        if not gates["mt5_connected"]:
            return "MT5_NOT_CONNECTED"
        if gates.get("account_block_reason"):
            return str(gates["account_block_reason"])
        contest_allowed = gates["account_type"] == "CONTEST" and self.settings.demo_allow_contest
        if gates["account_type"] != "DEMO" and not contest_allowed:
            return "ACCOUNT_NOT_DEMO"
        if not gates["demo_trading"]:
            return "DEMO_TRADING_DISABLED"
        if not gates["demo_only"]:
            return "DEMO_ONLY_DISABLED"
        if gates["allow_live_trading"]:
            return "ALLOW_LIVE_TRADING_NOT_FALSE"
        if not gates["demo_pilot_enabled"]:
            return "DEMO_PILOT_DISABLED"
        if not gates["pilot_window_active"]:
            return "DEMO_PILOT_WINDOW_CLOSED"
        if not gates["symbol_resolved"]:
            return "SYMBOL_NOT_RESOLVED"
        if not gates["symbol_allowed"]:
            return "SYMBOL_NOT_ALLOWED"
        symbol_gate_reason = self._symbol_gate_block_reason(gates)
        if symbol_gate_reason:
            return symbol_gate_reason
        if gates.get("entry_block_reason"):
            return str(gates["entry_block_reason"])
        relaxed_reason = self._relaxed_trade_block_reason(decision, gates)
        if relaxed_reason:
            return relaxed_reason
        quant_pro_hurst_reason = _quant_pro_hurst_block_reason(decision)
        if quant_pro_hurst_reason:
            return quant_pro_hurst_reason
        if not gates["market_open"]:
            return "MARKET_CLOSED"
        if not _demo_ignore_time_blocks(self.settings) and gates["time_gate_status"] != "PASS":
            return str(time_gate.get("time_gate_reason") or "TIME_GATE_BLOCK")
        if not _demo_ignore_time_blocks(self.settings) and _is_btc(gates.get("broker_symbol") or decision.get("symbol")) and not self.settings.demo_allow_btc_weekend_bad_hour:
            if time_gate.get("is_weekend"):
                return "BTC_WEEKEND_BLOCKED"
            if time_gate.get("is_bad_hour"):
                return "BTC_BAD_HOUR_BLOCKED"
        weekend_reason = _weekend_symbol_block_reason(
            gates.get("broker_symbol") or decision.get("symbol"),
            bool(gates.get("is_weekend")),
        )
        if weekend_reason:
            return weekend_reason
        if not gates["spread_ok"]:
            return "MAX_SPREAD"
        if gates["current_symbol_open_count"] >= self.settings.demo_max_open_trades_per_symbol:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        if gates["open_demo_trades_total"] >= self.settings.demo_max_open_trades_total:
            return "MAX_OPEN_TRADES_TOTAL"
        if gates["current_symbol_strategy_open_count"] >= self.settings.demo_max_open_trades_per_symbol_strategy:
            return "MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY"
        if gates["current_symbol_daily_count"] >= self.settings.demo_max_trades_per_symbol_per_day:
            return "MAX_TRADES_PER_SYMBOL_PER_DAY"
        if gates["daily_demo_trades_total"] >= self.settings.demo_max_trades_per_day_total:
            return "MAX_TRADES_PER_DAY_TOTAL"
        if gates.get("symbol_trade_cooldown_active"):
            return "SYMBOL_TRADE_COOLDOWN"
        if gates["daily_demo_loss_pct"] >= self.settings.demo_max_daily_loss_pct:
            return "DEMO_DAILY_LOSS_STOP"
        if gates["consecutive_losses"] >= self.settings.demo_stop_after_consecutive_losses:
            return "DEMO_CONSECUTIVE_LOSS_STOP"
        if kelly_lot is None or kelly_lot <= 0 or capped_lot is None or capped_lot <= 0 or risk_lot is None:
            if strategy == "EUR_EMA_RSI_ATR_CROSSOVER":
                return "EUR_EMA_RSI_ATR_INVALID_LOT"
            return "KELLY_INVALID_LOT"
        if capped_lot > self.settings.demo_max_lot:
            return "DEMO_MAX_LOT_EXCEEDED"
        if gates["risk_pct"] is None or gates["risk_pct"] > self.settings.demo_max_risk_per_trade_pct:
            return "DEMO_MAX_RISK_PER_TRADE_EXCEEDED"
        if decision.get("sl") is None or decision.get("tp") is None:
            return "MISSING_SL_TP"
        if not gates.get("sl_tp_valid"):
            return "INVALID_SL_TP"
        if gates["rr"] is None or gates["rr"] < 1.5:
            return "RR_BELOW_1_5"
        if safety.get("safety_guard_status") != "PASS":
            return str(safety.get("safety_guard_reason") or "SAFETY_GUARD_BLOCK")
        if _is_btc_scalping_gates(gates):
            return None
        if (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False)):
            adaptive = gates.get("adaptive_confluence") if isinstance(gates.get("adaptive_confluence"), dict) else {}
            if adaptive.get("adaptive_confluence_enabled") and adaptive.get("status") == "BLOCK":
                return str(adaptive.get("block_reason") or adaptive.get("confluence_threshold_reason") or "ADAPTIVE_CONFLUENCE_TOO_LOW")
            return None
        top_down_missing_reason = self._top_down_missing_reason(gates)
        if top_down_missing_reason:
            return top_down_missing_reason
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        if str(top_down.get("decision") or top_down.get("top_down_decision") or "").upper() == "AVOID":
            return "TOP_DOWN_READER_BLOCK"
        strong_confluence_reason = self._strong_confluence_fail_reason(gates)
        if strong_confluence_reason:
            return strong_confluence_reason
        adaptive = gates.get("adaptive_confluence") if isinstance(gates.get("adaptive_confluence"), dict) else {}
        if adaptive.get("adaptive_confluence_enabled"):
            if adaptive.get("status") == "BLOCK":
                return str(adaptive.get("block_reason") or adaptive.get("confluence_threshold_reason") or "ADAPTIVE_CONFLUENCE_TOO_LOW")
            if adaptive.get("status") == "PASS":
                return None
        if self.settings.hermes_free_demo_discovery_mode:
            top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
            if top_down.get("decision") == "AVOID":
                return "TOP_DOWN_READER_BLOCK"
            return None
        if str(decision.get("mtfa_status") or "").upper() == "FAIL":
            return "MTFA_FAIL"
        if str(decision.get("smc_confluence_status") or decision.get("smc_status") or "").upper() == "FAIL":
            return "SMC_FAIL"
        if decision.get("m15_confirmation") is not True:
            return "M15_CONFIRMATION_FALSE"
        if decision.get("m1_entry_confirmation") is not True:
            return "M1_CONFIRMATION_FALSE"
        if not self._strategy_gate_passes(decision):
            return "SETUP_GRADE_OR_STRICT_CONFIRMATION_REQUIRED"
        return None

    def _final_demo_block_reason(self, reason: str | None, exploration: dict, gates: dict, strategy: str) -> str | None:
        symbol_gate_reason = self._symbol_gate_block_reason(gates)
        if symbol_gate_reason:
            return symbol_gate_reason
        if reason:
            return reason
        if _is_btc_scalping_gates(gates):
            return None
        if (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False)):
            return None
        top_down_missing_reason = self._top_down_missing_reason(gates)
        if top_down_missing_reason:
            return top_down_missing_reason
        strong_confluence_reason = self._strong_confluence_fail_reason(gates)
        if strong_confluence_reason:
            return strong_confluence_reason
        if exploration.get("decision") == "BLOCK" and self._exploration_block_is_final(exploration, strategy):
            return str(exploration.get("block_reason") or "DEMO_EXPLORATION_BLOCKED")
        return None

    def _demo_topdown_fallback_review(self, reason: str | None, exploration: dict, gates: dict, capped_lot: float | None) -> dict:
        enabled = bool(self.settings.hermes_demo_topdown_fallback_mode)
        out = {
            "enabled": enabled,
            "decision": "SKIP" if not enabled else "BLOCK",
            "reason": None,
            "block_reason": None,
            "warnings": [],
        }
        if not enabled:
            return out
        btc_scalping = _is_btc_scalping_gates(gates)
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        top_down_decision = str(top_down.get("decision") or top_down.get("top_down_decision") or "").upper()
        top_down_status = str(top_down.get("top_down_status") or "").upper()
        gold_order_flow_relaxed = (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False))
        if top_down_decision == "AVOID" and not btc_scalping and not gold_order_flow_relaxed:
            out["reason"] = out["block_reason"] = "TOP_DOWN_READER_BLOCK"
            self._log_demo_fallback(gates, out)
            return out
        if top_down and top_down_status == "PASS" and top_down_decision in {"ALLOW_DEMO", ""}:
            out["decision"] = "SKIP"
            return out
        hard_reason = self._demo_topdown_fallback_hard_block_reason(gates, capped_lot)
        if hard_reason:
            out["reason"] = out["block_reason"] = hard_reason
            self._log_demo_fallback(gates, out)
            return out
        adaptive_reason = self._demo_topdown_fallback_adaptive_block_reason(gates)
        if adaptive_reason:
            out["reason"] = out["block_reason"] = adaptive_reason
            self._log_demo_fallback(gates, out)
            return out
        trigger_reason = self._demo_topdown_fallback_trigger_block_reason(gates)
        if trigger_reason:
            out["reason"] = out["block_reason"] = trigger_reason
            self._log_demo_fallback(gates, out)
            return out
        warnings: list[str] = []
        if not top_down:
            warnings.append("TOP_DOWN_MISSING_WARNING")
        elif top_down_status == "WAIT":
            warnings.append("TOP_DOWN_WAIT_WARNING")
        elif top_down_status == "FAIL":
            warnings.append("TOP_DOWN_FAIL_WARNING")
        if str(gates.get("smc_status") or "").upper() == "FAIL":
            warnings.append("SMC_FAIL_WARNING")
        if str(gates.get("mtfa_status") or "").upper() == "FAIL":
            warnings.append("MTFA_FAIL_WARNING")
        out.update(
            {
                "decision": "ALLOW",
                "reason": "ADAPTIVE_CONFLUENCE_FALLBACK",
                "block_reason": None,
                "warnings": list(dict.fromkeys(warnings)),
            }
        )
        self._log_demo_fallback(gates, out)
        return out

    def _demo_topdown_fallback_hard_block_reason(self, gates: dict, capped_lot: float | None) -> str | None:
        if not gates.get("mt5_connected"):
            return "MT5_NOT_CONNECTED"
        if gates.get("account_type") != "DEMO":
            return str(gates.get("account_block_reason") or "ACCOUNT_NOT_DEMO")
        if gates.get("account_trade_mode") != 0:
            return str(gates.get("account_block_reason") or "ACCOUNT_NOT_DEMO")
        if gates.get("allow_live_trading"):
            return "ALLOW_LIVE_TRADING_NOT_FALSE"
        if not gates.get("demo_only"):
            return "DEMO_ONLY_DISABLED"
        if not gates.get("demo_trading"):
            return "DEMO_TRADING_DISABLED"
        if capped_lot is None or capped_lot <= 0:
            if str(gates.get("strategy") or "").upper() == "EUR_EMA_RSI_ATR_CROSSOVER":
                return "EUR_EMA_RSI_ATR_INVALID_LOT"
            return "KELLY_INVALID_LOT"
        if capped_lot > self.settings.demo_max_lot or capped_lot > 0.01:
            return "DEMO_MAX_LOT_EXCEEDED"
        symbol_gate_reason = self._symbol_gate_block_reason(gates)
        if symbol_gate_reason:
            return symbol_gate_reason
        if not gates.get("spread_ok"):
            return "MAX_SPREAD"
        if not gates.get("market_open"):
            return "MARKET_CLOSED"
        if gates.get("trade_allowed") is not True:
            return "TRADE_ALLOWED_FALSE"
        if gates.get("trade_expert") is not True:
            return "TRADE_EXPERT_FALSE"
        if str(gates.get("direction") or "").upper() not in {"BUY", "SELL"}:
            return "NO_TRADE_DIRECTION"
        if not gates.get("sl_tp_valid"):
            return "INVALID_SL_TP"
        rr = _to_float(gates.get("rr"))
        if rr is None or rr < 1.5:
            return "RR_BELOW_1_5"
        if gates.get("current_symbol_open_count", 0) >= self.settings.demo_max_open_trades_per_symbol:
            return "MAX_OPEN_TRADES_PER_SYMBOL"
        if gates.get("open_demo_trades_total", 0) >= self.settings.demo_max_open_trades_total:
            return "MAX_OPEN_TRADES_TOTAL"
        if gates.get("current_symbol_strategy_open_count", 0) >= self.settings.demo_max_open_trades_per_symbol_strategy:
            return "MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY"
        smc_status = str(gates.get("smc_status") or "").upper()
        smc_score = _to_float(gates.get("smc_score"))
        btc_scalping = _is_btc_scalping_gates(gates)
        gold_order_flow_relaxed = (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False))
        if not btc_scalping and not gold_order_flow_relaxed and smc_status == "FAIL" and smc_score is not None and smc_score < 50:
            return "SMC_STRONG_FAIL"
        mtfa_status = str(gates.get("mtfa_status") or "").upper()
        mtfa_score = _to_float(gates.get("mtfa_score"))
        if not btc_scalping and not gold_order_flow_relaxed and mtfa_status == "FAIL" and mtfa_score is not None and mtfa_score < 50:
            return "MTFA_STRONG_FAIL"
        return None

    def _demo_topdown_fallback_adaptive_block_reason(self, gates: dict) -> str | None:
        if _is_btc_scalping_gates(gates):
            return None
        adaptive = gates.get("adaptive_confluence") if isinstance(gates.get("adaptive_confluence"), dict) else {}
        if not adaptive.get("adaptive_confluence_enabled"):
            return "ADAPTIVE_CONFLUENCE_REQUIRED"
        final_score = _to_float(adaptive.get("final_confluence_score") or gates.get("final_confluence_score")) or 0.0
        symbol_threshold = _to_float(adaptive.get("symbol_min_confluence") or gates.get("symbol_min_confluence")) or self.settings.hermes_default_min_confluence
        if adaptive.get("status") != "PASS" or adaptive.get("confluence_threshold_pass") is not True:
            return str(adaptive.get("block_reason") or adaptive.get("confluence_threshold_reason") or "ADAPTIVE_CONFLUENCE_TOO_LOW")
        if final_score < symbol_threshold:
            return "BELOW_SYMBOL_THRESHOLD"
        if final_score < 55:
            return "ADAPTIVE_CONFLUENCE_TOO_LOW"
        return None

    def _demo_topdown_fallback_trigger_block_reason(self, gates: dict) -> str | None:
        if _is_btc_scalping_gates(gates):
            return None
        symbol = str(gates.get("broker_symbol") or gates.get("raw_symbol") or "").upper()
        m15 = bool(gates.get("m15_confirmation_pass"))
        m1 = bool(gates.get("m1_trigger_pass"))
        score = _to_float(gates.get("final_confluence_score")) or 0.0
        if symbol.startswith("GOLD") or symbol.startswith("XAUUSD"):
            return None if (m15 or m1 or score >= 65) else "NO_ENTRY_TRIGGER_CONFIRMATION"
        if symbol.startswith("BTCUSD") or symbol.startswith("EURUSD"):
            return None if (m15 or m1) else "NO_ENTRY_TRIGGER_CONFIRMATION"
        return None if (m15 or m1) else "NO_ENTRY_TRIGGER_CONFIRMATION"

    def _log_demo_fallback(self, gates: dict, fallback: dict) -> None:
        log.info(
            "[DEMO_FALLBACK] symbol=%s strategy=%s decision=%s reason=%s warnings=%s",
            gates.get("broker_symbol"),
            gates.get("strategy"),
            fallback.get("decision"),
            fallback.get("block_reason") or fallback.get("reason") or "NONE",
            ",".join(fallback.get("warnings") or []) or "NONE",
        )

    def _demo_micro_discovery_review(
        self,
        reason: str | None,
        exploration: dict,
        fallback: dict,
        gates: dict,
        capped_lot: float | None,
    ) -> dict:
        enabled = bool(self.settings.hermes_demo_micro_discovery_mode)
        out = {
            "enabled": enabled,
            "decision": "SKIP" if not enabled else "BLOCK",
            "reason": None,
            "block_reason": None,
            "warnings": [],
        }
        if not enabled:
            return out
        btc_scalping = _is_btc_scalping_gates(gates)
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        top_down_decision = str(top_down.get("decision") or top_down.get("top_down_decision") or "").upper()
        gold_order_flow_relaxed = (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False))
        if top_down_decision == "AVOID" and not btc_scalping and not gold_order_flow_relaxed:
            out["reason"] = out["block_reason"] = "TOP_DOWN_READER_BLOCK"
            self._log_demo_micro_discovery(gates, out)
            return out
        hard_reason = self._demo_micro_discovery_hard_block_reason(gates, capped_lot)
        if hard_reason:
            out["reason"] = out["block_reason"] = hard_reason
            self._log_demo_micro_discovery(gates, out)
            return out
        confluence_reason = self._demo_micro_discovery_confluence_block_reason(gates)
        if confluence_reason:
            out["reason"] = out["block_reason"] = confluence_reason
            self._log_demo_micro_discovery(gates, out)
            return out
        smc_mtfa_reason = self._demo_micro_discovery_smc_mtfa_block_reason(gates)
        if smc_mtfa_reason:
            out["reason"] = out["block_reason"] = smc_mtfa_reason
            self._log_demo_micro_discovery(gates, out)
            return out
        trigger_reason = self._demo_micro_discovery_trigger_block_reason(gates)
        if trigger_reason:
            out["reason"] = out["block_reason"] = trigger_reason
            self._log_demo_micro_discovery(gates, out)
            return out
        warnings = self._demo_micro_discovery_warnings(gates)
        out.update(
            {
                "decision": "ALLOW",
                "reason": "MICRO_DISCOVERY_CONFLUENCE_SAMPLE",
                "block_reason": None,
                "warnings": warnings,
            }
        )
        self._log_demo_micro_discovery(gates, out)
        return out

    def _demo_micro_discovery_hard_block_reason(self, gates: dict, capped_lot: float | None) -> str | None:
        hard_reason = self._demo_topdown_fallback_hard_block_reason(gates, capped_lot)
        if hard_reason in {"SMC_STRONG_FAIL", "MTFA_STRONG_FAIL"}:
            return None
        return hard_reason

    def _demo_micro_discovery_confluence_block_reason(self, gates: dict) -> str | None:
        if _is_btc_scalping_gates(gates) or _is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates):
            return None
        adaptive = gates.get("adaptive_confluence") if isinstance(gates.get("adaptive_confluence"), dict) else {}
        final_score = _to_float(adaptive.get("final_confluence_score") or gates.get("final_confluence_score")) or 0.0
        if final_score < 60:
            return "MICRO_DISCOVERY_CONFLUENCE_TOO_LOW"
        if adaptive and adaptive.get("adaptive_confluence_enabled") and adaptive.get("status") == "BLOCK":
            reason = str(adaptive.get("block_reason") or adaptive.get("confluence_threshold_reason") or "")
            if reason == "TOP_DOWN_READER_BLOCK":
                return "TOP_DOWN_READER_BLOCK"
        return None

    def _demo_micro_discovery_smc_mtfa_block_reason(self, gates: dict) -> str | None:
        if _is_btc_scalping_gates(gates) or _is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates):
            return None
        symbol = str(gates.get("broker_symbol") or gates.get("raw_symbol") or "").upper()
        smc_score = _to_float(gates.get("smc_score"))
        mtfa_score = _to_float(gates.get("mtfa_score"))
        smc = smc_score if smc_score is not None else 0.0
        mtfa = mtfa_score if mtfa_score is not None else 0.0
        if symbol.startswith("EURUSD"):
            if smc < 50:
                return "MICRO_DISCOVERY_SMC_TOO_LOW"
            if mtfa < 50:
                return "MICRO_DISCOVERY_MTFA_TOO_LOW"
            return None
        if symbol.startswith("BTCUSD") or symbol.startswith("GOLD") or symbol.startswith("XAUUSD"):
            if smc < 40:
                return "MICRO_DISCOVERY_SMC_TOO_LOW"
            if mtfa < 35:
                return "MICRO_DISCOVERY_MTFA_TOO_LOW"
            return None
        if smc < 40:
            return "MICRO_DISCOVERY_SMC_TOO_LOW"
        if mtfa < 35:
            return "MICRO_DISCOVERY_MTFA_TOO_LOW"
        return None

    def _demo_micro_discovery_trigger_block_reason(self, gates: dict) -> str | None:
        if _is_btc_scalping_gates(gates) or _is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates):
            return None
        if bool(gates.get("m15_confirmation_pass")) or bool(gates.get("m1_trigger_pass")):
            return None
        strategy = str(gates.get("strategy") or "").upper()
        quant_pro_score = _to_float(gates.get("quant_pro_score") or gates.get("edge_score")) or 0.0
        rr = _to_float(gates.get("rr")) or 0.0
        if strategy == "QUANT_PRO_REGIME_SWITCHING" and quant_pro_score >= 90 and rr >= self.settings.hermes_quant_pro_min_rr:
            return None
        return "NO_ENTRY_TRIGGER_CONFIRMATION"

    def _demo_micro_discovery_warnings(self, gates: dict) -> list[str]:
        warnings: list[str] = []
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        top_down_status = str(top_down.get("top_down_status") or "").upper()
        if not top_down:
            warnings.append("TOP_DOWN_MISSING")
        elif top_down_status == "WAIT":
            warnings.append("TOP_DOWN_WAIT")
        elif top_down_status == "FAIL":
            warnings.append("TOP_DOWN_FAIL")
        if str(gates.get("smc_status") or "").upper() == "FAIL":
            warnings.append("SMC_SOFT_FAIL")
        if str(gates.get("mtfa_status") or "").upper() == "FAIL":
            warnings.append("MTFA_SOFT_FAIL")
        return list(dict.fromkeys(warnings))

    def _log_demo_micro_discovery(self, gates: dict, micro: dict) -> None:
        log.info(
            "[DEMO_MICRO_DISCOVERY] symbol=%s strategy=%s decision=%s reason=%s warnings=%s",
            gates.get("broker_symbol"),
            gates.get("strategy"),
            micro.get("decision"),
            micro.get("block_reason") or micro.get("reason") or "NONE",
            ",".join(micro.get("warnings") or []) or "NONE",
        )

    def _exploration_block_is_final(self, exploration: dict, strategy: str) -> bool:
        if exploration.get("override_reason"):
            return False
        strategy = str(strategy or "").upper()
        if strategy == "GOLD_ORDER_FLOW_CVD_VWAP" and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False)):
            return False
        if strategy in {"TREND_CONTINUATION_BREAKDOWN", "QUANT_STATISTICAL_PULLBACK", "QUANT_PRO_REGIME_SWITCHING", "EUR_EMA_RSI_ATR_CROSSOVER"}:
            return True
        if self.settings.hermes_free_demo_discovery_mode or self.settings.demo_strong_setup_learning_mode or self.settings.demo_smoke_test_24h:
            return True
        final_reasons = {
            *_TOP_DOWN_STRICT_BLOCK_REASONS,
            "SMC_STRONG_FAIL",
            "MTFA_STRONG_FAIL",
            "ADAPTIVE_CONFLUENCE_TOO_LOW",
            "BELOW_SYMBOL_THRESHOLD",
            "GOLD_M1_WAIT_REQUIRES_70",
            "TOPDOWN_BELOW_SYMBOL_MIN",
        }
        return any(reason in final_reasons for reason in exploration.get("block_reasons", []))

    def _top_down_missing_reason(self, gates: dict) -> str | None:
        if gates.get("topdown_fallback_allowed"):
            return None
        if not (
            self.settings.hermes_adaptive_confluence_enabled
            or self.settings.hermes_free_demo_discovery_mode
            or self.settings.demo_strong_setup_learning_mode
        ):
            return None
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        if not top_down:
            return "TOP_DOWN_READER_MISSING"
        decision = str(top_down.get("decision") or top_down.get("top_down_decision") or "").upper()
        status = str(top_down.get("top_down_status") or "").upper()
        score = _to_float(top_down.get("entry_readiness_score"))
        missing = top_down.get("missing_confirmations")
        if decision == "AVOID":
            return None
        if status == "FAIL":
            return "TOP_DOWN_READER_FAIL"
        if decision == "WAIT_FOR_CONFIRMATION":
            return "TOP_DOWN_WAIT_FOR_CONFIRMATION"
        if not decision or not status or score is None:
            return "TOP_DOWN_READER_FAIL"
        if isinstance(missing, list) and any(str(item).endswith("_MISSING_OR_INSUFFICIENT") for item in missing):
            _rates_missing = [m for m in missing if str(m).endswith("_MISSING_OR_INSUFFICIENT") or str(m).endswith("_RATES_MISSING")]
            log.info(
                "[ROUTER_BLOCK] symbol=%s reason=TOP_DOWN_DATA_MISSING missing=%s",
                gates.get("broker_symbol") or gates.get("raw_symbol"),
                ",".join(str(m) for m in _rates_missing) or "UNKNOWN",
            )
            return "TOP_DOWN_READER_FAIL"
        if isinstance(missing, list) and any(str(item).endswith("_RATES_MISSING") for item in missing):
            log.info(
                "[ROUTER_BLOCK] symbol=%s reason=TOP_DOWN_DATA_MISSING missing=%s",
                gates.get("broker_symbol") or gates.get("raw_symbol"),
                ",".join(str(m) for m in missing),
            )
            return "TOP_DOWN_READER_FAIL"
        if isinstance(missing, list) and "TOP_DOWN_DATA_MISSING" in {str(item) for item in missing}:
            log.info(
                "[ROUTER_BLOCK] symbol=%s reason=TOP_DOWN_DATA_MISSING missing=TOP_DOWN_DATA_MISSING",
                gates.get("broker_symbol") or gates.get("raw_symbol"),
            )
            return "TOP_DOWN_READER_FAIL"
        if str(top_down.get("reason") or "").upper() == "TOP_DOWN_DATA_MISSING":
            log.info(
                "[ROUTER_BLOCK] symbol=%s reason=TOP_DOWN_DATA_MISSING missing=TOP_DOWN_DATA_MISSING",
                gates.get("broker_symbol") or gates.get("raw_symbol"),
            )
            return "TOP_DOWN_READER_FAIL"
        return None

    def _strong_confluence_fail_reason(self, gates: dict) -> str | None:
        if gates.get("micro_discovery_allowed"):
            return None
        if (_is_gold_order_flow_gates(gates) or _is_order_flow_exec_gates(gates)) and not bool(getattr(self.settings, "strict_gold_order_flow_topdown", False)):
            return None
        smc_status = str(gates.get("smc_status") or "").upper()
        smc_score = _to_float(gates.get("smc_score"))
        if smc_status == "FAIL" and smc_score is not None and smc_score < 50:
            return "SMC_STRONG_FAIL"
        mtfa_status = str(gates.get("mtfa_status") or "").upper()
        mtfa_score = _to_float(gates.get("mtfa_score"))
        if mtfa_status == "FAIL" and mtfa_score is not None and mtfa_score < 50:
            return "MTFA_STRONG_FAIL"
        return None

    def _entry_candidate_block_reason(self, decision: dict) -> str | None:
        strategy = str(decision.get("strategy") or "").upper()
        signal = str(decision.get("signal") or decision.get("direction") or "").upper()
        if strategy in CONFIRMATION_ONLY_STRATEGIES:
            return "EMA_PULLBACK_CONFIRMATION_ONLY"
        if strategy in OBSERVER_ONLY_STRATEGIES:
            return "STRATEGY_OBSERVER_ONLY"
        if signal not in {"BUY", "SELL"}:
            return "NO_TRADE_DIRECTION"
        if strategy == "BTC_SCALPING_AGENT":
            missing = _missing_entry_sl_tp_field(decision)
            if missing:
                log.info("[BTC_SCALPING_ROUTE] decision=BLOCK reason=MISSING_ENTRY_OR_SL_TP field=%s", missing)
                return "MISSING_ENTRY_OR_SL_TP"
        return None

    def _strategy_gate_passes(self, decision: dict) -> bool:
        grade = str(decision.get("big_setup_grade") or "").upper()
        grade_ok = grade in {"A+", "A", "B"}
        strict_ok = (
            str(decision.get("smc_confluence_status") or decision.get("smc_status") or "").upper() == "PASS"
            and str(decision.get("mtfa_status") or "").upper() == "PASS"
            and decision.get("m15_confirmation") is True
            and decision.get("m1_entry_confirmation") is True
        )
        return grade_ok or strict_ok

    def _exploration_review(
        self,
        decision: dict,
        strict_reason: str | None,
        capped_lot: float | None,
        gates: dict,
        time_gate: dict,
        safety: dict,
        now: datetime | None = None,
    ) -> dict:
        warnings: list[str] = []
        blocks: list[str] = []
        ignored_blocks: list[str] = []

        def block(reason: str) -> None:
            normalized = self._normalize_demo_test_block_reason(reason, time_gate)
            if normalized not in blocks:
                blocks.append(normalized)

        strategy = str(decision.get("strategy") or "").upper()
        direction = str(decision.get("signal") or decision.get("resolved_direction") or decision.get("direction") or "").upper()
        session = str(time_gate.get("session_name") or "").upper()
        rr = _to_float(decision.get("reward_risk") or decision.get("risk_reward") or gates.get("rr")) or 0.0
        edge_score = _to_float(decision.get("edge_score") or decision.get("setup_hunter_score")) or 0.0
        setup_score = _to_float(decision.get("setup_score")) or 0.0
        grade = str(decision.get("grade") or decision.get("setup_hunter_grade") or decision.get("big_setup_grade") or "").upper()
        smc_status = str(decision.get("smc_confluence_status") or decision.get("smc_status") or gates.get("smc_status") or "").upper()
        mtfa_status = str(decision.get("mtfa_status") or gates.get("mtfa_status") or "").upper()
        smc_score = _to_float(decision.get("smc_score") or decision.get("smc_confluence_score") or gates.get("smc_score"))
        mtfa_score = _to_float(decision.get("mtfa_score") or gates.get("mtfa_score"))
        m1_pass = _status_pass(decision.get("m1_trigger_status"), decision.get("m1_entry_confirmation"))
        m15_pass = _status_pass(decision.get("m15_confirmation_status"), decision.get("m15_confirmation"))
        quant_strategy = strategy == "QUANT_STATISTICAL_PULLBACK"
        quant_score = _to_float(decision.get("quant_score") or gates.get("edge_score")) or 0.0
        quant_ready = quant_strategy and quant_score >= self.settings.hermes_quant_min_score and rr >= self.settings.hermes_quant_min_rr
        quant_pro_strategy = strategy == "QUANT_PRO_REGIME_SWITCHING"
        quant_pro_score = _to_float(decision.get("quant_pro_score") or gates.get("edge_score")) or 0.0
        quant_pro_ready = quant_pro_strategy and quant_pro_score >= self.settings.hermes_quant_pro_min_score and rr >= self.settings.hermes_quant_pro_min_rr
        statistical_quant_ready = quant_ready or quant_pro_ready
        free_discovery = bool(self.settings.hermes_free_demo_discovery_mode)
        max_lot = min(self.settings.demo_max_lot, self.settings.demo_exploration_max_lot)
        adaptive = gates.get("adaptive_confluence") if isinstance(gates.get("adaptive_confluence"), dict) else {}
        adaptive_allows_m1_wait = bool(
            adaptive.get("adaptive_confluence_enabled")
            and adaptive.get("status") == "PASS"
            and adaptive.get("m1_required") is False
        )

        if not self.settings.demo_exploration_mode and not self.settings.demo_strong_setup_learning_mode:
            block("EXPLORATION_MODE_DISABLED")
        if not gates["mt5_connected"]:
            block("MT5_NOT_CONNECTED")
        if gates.get("account_block_reason"):
            block(str(gates["account_block_reason"]))
        contest_allowed = gates["account_type"] == "CONTEST" and self.settings.demo_allow_contest
        if gates["account_type"] != "DEMO" and not contest_allowed:
            block("ACCOUNT_NOT_DEMO")
        if not gates["demo_trading"]:
            block("DEMO_TRADING_DISABLED")
        if not gates["demo_only"]:
            block("DEMO_ONLY_DISABLED")
        if gates["allow_live_trading"]:
            block("ALLOW_LIVE_TRADING_NOT_FALSE")
        if strategy not in {"SIMO_ATM_BREAKOUT", "TREND_CONTINUATION_BREAKDOWN", "QUANT_STATISTICAL_PULLBACK", "QUANT_PRO_REGIME_SWITCHING", "EUR_EMA_RSI_ATR_CROSSOVER", "BTC_SCALPING_AGENT"} and not self.settings.demo_strong_setup_learning_mode and not free_discovery:
            block("EXPLORATION_STRATEGY_NOT_ALLOWED")
        if strategy not in ENTRY_STRATEGIES:
            block("NON_ENTRY_STRATEGY")
        relaxed_reason = self._relaxed_trade_block_reason(decision, gates)
        if relaxed_reason:
            block(relaxed_reason)
        quant_pro_hurst_reason = _quant_pro_hurst_block_reason(decision)
        if quant_pro_hurst_reason:
            block(quant_pro_hurst_reason)
        if direction not in {"BUY", "SELL"}:
            block("NO_TRADE_DIRECTION")
        if not m1_pass and adaptive_allows_m1_wait:
            warnings.append("M1_TRIGGER_WAIT_ADAPTIVE_ALLOWED")
        elif not m1_pass and not (statistical_quant_ready or free_discovery):
            block("M1_TRIGGER_FALSE")
        elif not m1_pass and free_discovery:
            warnings.append("M1_TRIGGER_FALSE")
        if not m15_pass and not (statistical_quant_ready or free_discovery):
            block("M15_CONFIRMATION_FALSE")
        elif not m15_pass and free_discovery:
            warnings.append("M15_CONFIRMATION_FALSE")
        if not gates["market_open"]:
            block("MARKET_CLOSED")
        if not _demo_ignore_time_blocks(self.settings):
            if gates["time_gate_status"] != "PASS":
                block(str(time_gate.get("time_gate_reason") or "TIME_GATE_BLOCK"))
            if session not in EXPLORATION_SESSIONS:
                block("SESSION_NOT_ALLOWED")
        if not gates["spread_ok"]:
            block("MAX_SPREAD")
        if safety.get("safety_guard_status") != "PASS":
            block(str(safety.get("safety_guard_reason") or "SAFETY_GUARD_BLOCK"))
        if rr < self.settings.demo_exploration_min_rr:
            block("RR_BELOW_EXPLORATION_MIN")
        if not (statistical_quant_ready or free_discovery) and edge_score < self.settings.demo_exploration_min_edge_score and setup_score < 85:
            block("EDGE_SCORE_BELOW_EXPLORATION_MIN")
        elif free_discovery and edge_score < self.settings.demo_exploration_min_edge_score and setup_score < 85:
            warnings.append("EDGE_SCORE_BELOW_EXPLORATION_MIN")
        if not (statistical_quant_ready or free_discovery) and grade not in {"A+", "A", "B", "A_PLUS"}:
            block("BIG_SETUP_GRADE_BELOW_B")
        elif free_discovery and grade not in {"A+", "A", "B", "A_PLUS"}:
            warnings.append("BIG_SETUP_GRADE_BELOW_B")
        if capped_lot is None or capped_lot <= 0:
            block("KELLY_INVALID_LOT")
        elif capped_lot > max_lot:
            block("DEMO_EXPLORATION_MAX_LOT_EXCEEDED")
        if gates["risk_pct"] is None or gates["risk_pct"] > self.settings.demo_max_risk_per_trade_pct:
            block("DEMO_MAX_RISK_PER_TRADE_EXCEEDED")
        if decision.get("sl") is None or decision.get("tp") is None:
            block("MISSING_SL_TP")
        elif not gates.get("sl_tp_valid"):
            block("INVALID_SL_TP")
        if gates["current_symbol_open_count"] >= self.settings.demo_max_open_trades_per_symbol:
            block("MAX_OPEN_TRADES_PER_SYMBOL")
        if gates["open_demo_trades_total"] >= self.settings.demo_max_open_trades_total:
            block("MAX_OPEN_TRADES_TOTAL")
        if gates["current_symbol_strategy_open_count"] >= self.settings.demo_max_open_trades_per_symbol_strategy:
            block("MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY")
        if gates["current_symbol_daily_count"] >= self.settings.demo_max_trades_per_symbol_per_day:
            block("MAX_TRADES_PER_SYMBOL_PER_DAY")
        if gates["daily_demo_trades_total"] >= self.settings.demo_max_trades_per_day_total:
            block("MAX_TRADES_PER_DAY_TOTAL")
        if gates.get("current_symbol_exploration_daily_count", 0) >= self.settings.demo_exploration_max_trades_per_symbol_per_day:
            block("MAX_TRADES_PER_SYMBOL_PER_DAY")
        if gates.get("daily_exploration_trades_total", 0) >= self.settings.demo_exploration_max_trades_per_day_total:
            block("MAX_TRADES_PER_DAY_TOTAL")
        if self.settings.demo_strong_setup_learning_mode and gates.get("current_symbol_strong_setup_learning_daily_count", 0) >= self.settings.demo_strong_setup_max_trades_per_symbol_per_day:
            block("MAX_TRADES_PER_SYMBOL_PER_DAY")
        if self.settings.demo_strong_setup_learning_mode and gates.get("daily_strong_setup_learning_trades_total", 0) >= self.settings.demo_strong_setup_max_trades_per_day_total:
            block("MAX_TRADES_PER_DAY_TOTAL")
        if self.settings.demo_smoke_test_24h:
            if not self._smoke_test_active(now):
                block("DEMO_SMOKE_TEST_24H_EXPIRED")
            if gates.get("smoke_test_confirmed_orders", 0) >= self.settings.demo_smoke_test_max_confirmed_orders:
                block("DEMO_SMOKE_TEST_CONFIRMED_ORDER_LIMIT")
        if gates["daily_demo_loss_pct"] >= self.settings.demo_max_daily_loss_pct:
            block("DEMO_DAILY_LOSS_STOP")
        if gates["consecutive_losses"] >= self.settings.demo_stop_after_consecutive_losses:
            block("DEMO_CONSECUTIVE_LOSS_STOP")
        if adaptive.get("adaptive_confluence_enabled") and adaptive.get("status") == "BLOCK":
            block(str(adaptive.get("block_reason") or adaptive.get("confluence_threshold_reason") or "ADAPTIVE_CONFLUENCE_TOO_LOW"))

        if mtfa_status == "FAIL":
            if self.settings.demo_exploration_allow_mtfa_fail or free_discovery:
                warnings.append("MTFA_FAIL")
            else:
                block("MTFA_FAIL")
        elif mtfa_score is not None and mtfa_score < 60:
            if self.settings.demo_exploration_allow_mtfa_fail or free_discovery:
                warnings.append("MTFA_SCORE_LT_60")
            else:
                block("MTFA_SCORE_LT_60")
        if smc_status == "FAIL":
            if self.settings.demo_exploration_allow_smc_fail or free_discovery:
                warnings.append("SMC_FAIL")
            else:
                block("SMC_FAIL")
        elif smc_score is not None and smc_score < 70:
            if self.settings.demo_exploration_allow_smc_fail or free_discovery:
                warnings.append("SMC_SCORE_LT_70")
            else:
                block("SMC_SCORE_LT_70")

        wsp = gates.get("wsp_intelligence") if isinstance(gates.get("wsp_intelligence"), dict) else {}
        if wsp.get("safety_guard_visual") == "DANGER":
            warnings.append("WSP_DANGER")
        if wsp.get("trap_check") in {"BULL_TRAP", "BEAR_TRAP"}:
            warnings.append("WSP_TRAP_RISK")

        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        if quant_pro_strategy and top_down.get("decision") == "AVOID":
            block("TOP_DOWN_READER_BLOCK")
        if free_discovery and top_down.get("decision") == "AVOID":
            block("TOP_DOWN_READER_BLOCK")
        if self.settings.demo_test_ignore_bad_hours and top_down.get("decision") == "AVOID":
            block("TOP_DOWN_READER_BLOCK")
        if self.settings.demo_strong_setup_learning_mode:
            top_down_score = _to_float(top_down.get("entry_readiness_score") or gates.get("top_down_score")) or 0.0
            if top_down.get("decision") == "AVOID" or top_down_score < 75 or top_down.get("m1_trigger") is not True or top_down.get("m15_confirmation") is not True:
                block("TOP_DOWN_READER_BLOCK")

        strict_normalized = self._normalize_demo_test_block_reason(strict_reason or "", time_gate)
        if strict_normalized in _DEMO_TEST_BAD_HOUR_BLOCKERS and strict_normalized not in blocks:
            blocks.append(strict_normalized)

        override_reason = None
        if self.settings.demo_smoke_test_24h:
            blocks, ignored_blocks = self._apply_smoke_test_24h_override(blocks, gates, capped_lot)
            if ignored_blocks:
                override_reason = "DEMO_SMOKE_TEST_24H"
        if not override_reason and self.settings.demo_strong_setup_learning_mode:
            strong_blocks, strong_ignored = self._apply_strong_setup_learning_override(blocks + warnings, gates, capped_lot)
            if strong_ignored:
                blocks = strong_blocks
                ignored_blocks = strong_ignored
                override_reason = (
                    "DEMO_TEST_IGNORE_BAD_HOURS"
                    if self.settings.demo_test_ignore_bad_hours and any(reason in _DEMO_TEST_BAD_HOUR_BLOCKERS for reason in strong_ignored)
                    else "DEMO_STRONG_SETUP_LEARNING_MODE"
                )
        if not override_reason and (self.settings.demo_test_ignore_bad_hours or self.settings.demo_exploration_ignore_bad_hour):
            blocks, ignored_blocks = self._apply_demo_test_bad_hours_override(blocks, gates, capped_lot)
            if ignored_blocks:
                override_reason = "DEMO_TEST_IGNORE_BAD_HOURS" if self.settings.demo_test_ignore_bad_hours else "DEMO_SMOKE_TEST_IGNORE_BAD_HOUR"

        decision_status = "ALLOW" if not blocks else "BLOCK"
        log.info(
            "[DEMO_EXPLORATION] decision=%s symbol=%s strategy=%s direction=%s edge_score=%s rr=%s warnings=%s strict_block_reason=%s block_reason=%s override=%s ignored=%s",
            decision_status,
            gates.get("broker_symbol"),
            strategy,
            direction,
            edge_score,
            rr,
            ",".join(warnings) or "NONE",
            strict_reason or "NONE",
            blocks[0] if blocks else "NONE",
            override_reason or "NONE",
            ",".join(ignored_blocks) or "NONE",
        )
        return {
            "decision": decision_status,
            "block_reason": blocks[0] if blocks else None,
            "block_reasons": blocks,
            "warnings": warnings,
            "override_reason": override_reason,
            "ignored_block_reasons": ignored_blocks,
        }

    def _apply_strong_setup_learning_override(self, blocks: list[str], gates: dict, capped_lot: float | None) -> tuple[list[str], list[str]]:
        aliases = {
            "BTC_BAD_HOUR_BLOCKED": "BTC_BAD_HOUR_BLOCK",
            "MAX_SPREAD": "SPREAD_FAIL",
            "DEMO_EXPLORATION_MAX_LOT_EXCEEDED": "LOT_ABOVE_DEMO_MAX",
            "DEMO_MAX_LOT_EXCEEDED": "LOT_ABOVE_DEMO_MAX",
            "MISSING_SL_TP": "INVALID_SL_TP",
        }
        allowed_ignored = {
            "MTFA_FAIL",
            "MTFA_SCORE_LT_60",
            "SMC_FAIL",
            "SMC_SCORE_LT_70",
            "BIG_SETUP_GRADE_BELOW_B",
            "SESSION_NOT_ALLOWED",
        }
        if self.settings.demo_test_ignore_bad_hours:
            allowed_ignored = allowed_ignored | _DEMO_TEST_BAD_HOUR_BLOCKERS
        normalized = [aliases.get(reason, reason) for reason in blocks]
        ignored = [reason for reason in normalized if reason in allowed_ignored]
        remaining = [reason for reason in normalized if reason not in allowed_ignored]
        hard_ok = self._demo_test_override_hard_checks_pass(gates, capped_lot)
        top_down = gates.get("top_down_reader") if isinstance(gates.get("top_down_reader"), dict) else {}
        top_down_score = _to_float(top_down.get("entry_readiness_score") or gates.get("top_down_score")) or 0.0
        time_only_remaining = all(reason in _DEMO_TEST_BAD_HOUR_BLOCKERS for reason in remaining)
        top_down_ok = (
            top_down.get("decision") == "ALLOW_DEMO"
            or (
                self.settings.demo_test_ignore_bad_hours
                and top_down_score >= 75
                and bool(time_only_remaining)
            )
        )
        entry_ok = (
            str(gates.get("strategy") or "").upper() in ENTRY_STRATEGIES
            and str(gates.get("entry_block_reason") or "") in {"", "None"}
            and (_to_float(gates.get("rr")) or 0.0) >= self.settings.demo_strong_setup_min_rr
            and (_to_float(gates.get("edge_score")) or 0.0) >= self.settings.demo_strong_setup_min_edge
            and str(gates.get("setup_grade") or "").upper() in {"A+", "A", "B", "A_PLUS"}
            and bool(gates.get("m1_trigger_pass"))
            and bool(gates.get("m15_confirmation_pass"))
            and str(gates.get("direction") or "").upper() in {"BUY", "SELL"}
            and bool(gates.get("sl_tp_valid"))
            and top_down_ok
        )
        if hard_ok and entry_ok and ignored and not remaining:
            return [], sorted(set(ignored), key=ignored.index)
        return blocks, []

    def _top_down_reading(
        self,
        decision: dict,
        frames: dict | None,
        symbol: str,
        direction: str,
        sl: float | None,
        tp: float | None,
        spread: float,
        max_spread: float,
        now: datetime,
    ) -> dict:
        existing = decision.get("top_down_reader")
        if isinstance(existing, dict):
            return dict(existing)
        embedded = decision.get("top_down")
        if isinstance(embedded, dict):
            return dict(embedded)
        return self.top_down_reader.evaluate(
            symbol=symbol,
            frames=frames or {},
            direction=direction,
            entry=_to_float(decision.get("entry")),
            sl=sl,
            tp=tp,
            spread_points=spread,
            max_spread_points=max_spread,
            decision_time=now,
            smc_score=_to_float(decision.get("smc_score") or decision.get("smc_confluence_score")),
            timeframe=str(decision.get("timeframe") or "M5"),
        )

    def _apply_smoke_test_bad_hour_override(self, blocks: list[str], gates: dict, capped_lot: float | None) -> tuple[list[str], list[str]]:
        return self._apply_demo_test_bad_hours_override(blocks, gates, capped_lot)

    def _apply_demo_test_bad_hours_override(self, blocks: list[str], gates: dict, capped_lot: float | None) -> tuple[list[str], list[str]]:
        aliases = {
            "BTC_BAD_HOUR_BLOCKED": "BTC_BAD_HOUR_BLOCK",
        }
        allowed_ignored = set(_DEMO_TEST_BAD_HOUR_BLOCKERS)
        if not self.settings.demo_test_ignore_bad_hours:
            allowed_ignored = {"BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED"}
        normalized = [aliases.get(reason, reason) for reason in blocks]
        ignored = [reason for reason in normalized if reason in allowed_ignored]
        remaining = [reason for reason in normalized if reason not in allowed_ignored]
        hard_ok = self._demo_test_override_hard_checks_pass(gates, capped_lot)
        if hard_ok and ignored and not remaining:
            return [], sorted(set(ignored), key=ignored.index)
        return blocks, []

    def _normalize_demo_test_block_reason(self, reason: str, time_gate: dict | None = None) -> str:
        normalized = str(reason or "")
        if normalized == "BTC_BAD_HOUR_BLOCKED":
            return "BTC_BAD_HOUR_BLOCK"
        if normalized == "TIME_GATE_BLOCK":
            time_reason = str((time_gate or {}).get("time_gate_reason") or "")
            if time_reason in {"BAD_LIQUIDITY_HOUR", "SESSION_NOT_ALLOWED", "WAITING_FOR_SESSION", "BTC_BAD_HOUR_BLOCK", "BTC_BAD_HOUR_BLOCKED"}:
                return "SESSION_NOT_ALLOWED" if time_reason == "TIME_GATE_BLOCK" else self._normalize_demo_test_block_reason(time_reason, time_gate)
        return normalized

    def _demo_test_override_hard_checks_pass(self, gates: dict, capped_lot: float | None) -> bool:
        return (
            gates.get("account_type") == "DEMO"
            and gates.get("account_trade_mode") == 0
            and not bool(gates.get("allow_live_trading"))
            and bool(gates.get("demo_only"))
            and bool(gates.get("demo_trading"))
            and gates.get("magic") == self.settings.demo_magic_number
            and capped_lot is not None
            and capped_lot > 0
            and capped_lot <= self.settings.demo_max_lot
            and capped_lot <= 0.01
            and bool(gates.get("sl_tp_valid"))
            and (_to_float(gates.get("rr")) or 0.0) > 0
            and str(gates.get("direction") or "").upper() in {"BUY", "SELL"}
            and int(gates.get("current_symbol_open_count") or 0) < self.settings.demo_max_open_trades_per_symbol
            and int(gates.get("open_demo_trades_total") or 0) < self.settings.demo_max_open_trades_total
            and int(gates.get("current_symbol_strategy_open_count") or 0) < self.settings.demo_max_open_trades_per_symbol_strategy
            and int(gates.get("current_symbol_daily_count") or 0) < self.settings.demo_max_trades_per_symbol_per_day
            and int(gates.get("daily_demo_trades_total") or 0) < self.settings.demo_max_trades_per_day_total
            and bool(gates.get("market_open"))
            and gates.get("trade_allowed") is True
            and gates.get("trade_expert") is True
            and bool(gates.get("symbol_allowed"))
            and bool(gates.get("symbol_trade_allowed"))
            and bool(gates.get("spread_ok"))
        )

    def _apply_smoke_test_24h_override(self, blocks: list[str], gates: dict, capped_lot: float | None) -> tuple[list[str], list[str]]:
        aliases = {
            "BTC_BAD_HOUR_BLOCKED": "BTC_BAD_HOUR_BLOCK",
            "MAX_SPREAD": "SPREAD_FAIL",
            "DEMO_EXPLORATION_MAX_LOT_EXCEEDED": "LOT_ABOVE_DEMO_MAX",
        }
        allowed_ignored = {"BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED", "EDGE_SCORE_BELOW_EXPLORATION_MIN"}
        normalized = [aliases.get(reason, reason) for reason in blocks]
        ignored = [reason for reason in normalized if reason in allowed_ignored]
        remaining = [reason for reason in normalized if reason not in allowed_ignored]
        hard_ok = (
            gates.get("account_type") == "DEMO"
            and gates.get("account_trade_mode") == 0
            and not bool(gates.get("allow_live_trading"))
            and bool(gates.get("demo_only"))
            and bool(gates.get("demo_trading"))
            and gates.get("magic") == self.settings.demo_magic_number
            and capped_lot is not None
            and capped_lot <= self.settings.demo_max_lot
            and int(gates.get("current_symbol_open_count") or 0) < self.settings.demo_max_open_trades_per_symbol
            and int(gates.get("open_demo_trades_total") or 0) < self.settings.demo_max_open_trades_total
            and gates.get("smoke_test_confirmed_orders", 0) < self.settings.demo_smoke_test_max_confirmed_orders
            and bool(gates.get("market_open"))
            and gates.get("trade_allowed") is not False
            and gates.get("trade_expert") is not False
            and bool(gates.get("symbol_allowed"))
            and bool(gates.get("symbol_trade_allowed"))
            and bool(gates.get("spread_ok"))
        )
        if hard_ok and ignored and not remaining:
            return [], sorted(set(ignored), key=ignored.index)
        return blocks, []

    def _smoke_test_started_at(self) -> datetime:
        return self.pilot_started_at

    def _smoke_test_expires_at(self) -> datetime:
        return self._smoke_test_started_at() + timedelta(hours=self.settings.demo_smoke_test_end_after_hours)

    def _smoke_test_active(self, now: datetime | None = None) -> bool:
        now_dt = now or datetime.now(timezone.utc)
        return self._smoke_test_started_at() <= now_dt <= self._smoke_test_expires_at()

    def _send_order(self, event: dict) -> dict:
        event = dict(event)
        _old_btc_event_mode = str(event.get("old_btc_mode") or "")
        if _old_btc_event_mode:
            from app.profiles.lovable_btc_old_system import old_btc_rr_for_strategy, compute_old_btc_tp
            _ob_strategy = str(event.get("strategy") or "")
            _ob_direction = str(event.get("direction") or "")
            _ob_entry = float(event.get("entry") or 0)
            _ob_sl = float(event.get("sl") or 0)
            _ob_rr = old_btc_rr_for_strategy(_ob_strategy, self.settings)
            _ob_raw_tp = compute_old_btc_tp(_ob_entry, _ob_sl, _ob_rr, _ob_direction)
            log.info(
                "[OLD_BTC_RR] strategy=%s mode=%s target_rr=%s entry=%s sl=%s raw_tp=%s direction=%s",
                _ob_strategy, _old_btc_event_mode, _ob_rr, _ob_entry, _ob_sl, _ob_raw_tp, _ob_direction,
            )
            if _ob_raw_tp is not None:
                event["tp"] = _ob_raw_tp
            _ob_max_tp = self.settings.max_tp_usd
            log.info(
                "[OLD_BTC_MAX_TP] strategy=%s max_tp_usd=%s rr_based_tp=%s",
                _ob_strategy, _ob_max_tp, _ob_raw_tp,
            )
            _ob_cfg_tp = float(getattr(self.settings, "quick_exit_tp_usd", 1.50) or 1.50)
            _ob_cfg_lock = float(getattr(self.settings, "quick_exit_lock_usd", 0.80) or 0.80)
            _ob_cfg_be = float(getattr(self.settings, "quick_exit_be_buffer_usd", 0.10) or 0.10)
            _ob_cfg_trail_start = float(getattr(self.settings, "quick_exit_trail_start_usd", 1.00) or 1.00)
            _ob_cfg_trail_gap = float(getattr(self.settings, "quick_exit_trail_gap_usd", 0.60) or 0.60)
            log.info(
                "[OLD_BTC_QUICK_EXIT_CONFIG] tp_usd=%s lock_usd=%s be_buffer_usd=%s trail_start_usd=%s trail_gap_usd=%s",
                _ob_cfg_tp, _ob_cfg_lock, _ob_cfg_be, _ob_cfg_trail_start, _ob_cfg_trail_gap,
            )
        max_tp = self._apply_max_money_tp(event)
        event["max_money_tp"] = max_tp
        if max_tp.get("block_reason"):
            reason = str(max_tp["block_reason"])
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": reason,
                "failed_gate": reason,
                "max_money_tp": max_tp,
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": None,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": reason,
            }
        _order_symbol = event["broker_symbol"]
        # Remap logical EURUSD to broker-specific symbol name if configured
        _eurusd_broker = getattr(self.settings, "eurusd_broker_symbol", "EURUSD") or "EURUSD"
        if _order_symbol.upper().startswith("EURUSD") and _eurusd_broker.upper() != _order_symbol.upper():
            log.info("[SYMBOL_REMAP] %s → %s (EURUSD_BROKER_SYMBOL)", _order_symbol, _eurusd_broker)
            _order_symbol = _eurusd_broker
        _sym_info = None
        try:
            _sym_info = mt5.symbol_info(_order_symbol)
        except Exception:
            pass
        # Symbol not in MarketWatch — select it and retry
        if _sym_info is None:
            _sel_result = False
            _sel_error = None
            try:
                _sel_fn = getattr(mt5, "symbol_select", None)
                if callable(_sel_fn):
                    _sel_result = _sel_fn(_order_symbol, True)
                try:
                    _sel_error = mt5.last_error()
                except Exception:
                    pass
                log.info(
                    "[SYMBOL_SELECT_ATTEMPT] symbol=%s select_result=%s last_error=%s",
                    _order_symbol, _sel_result, _sel_error,
                )
                _sym_info = mt5.symbol_info(_order_symbol)
            except Exception:
                pass
            if _sym_info is None:
                _last_err = None
                try:
                    _last_err = mt5.last_error()
                except Exception:
                    pass
                _eur_variants: list[str] = []
                try:
                    _all_syms = mt5.symbols_get()
                    if _all_syms:
                        _eur_variants = [
                            s.name for s in _all_syms
                            if "EUR" in str(getattr(s, "name", "")).upper()
                        ]
                except Exception:
                    pass
                log.warning(
                    "[SYMBOL_SELECT_FAILED] symbol=%s last_error=%s eur_broker_variants=%s",
                    _order_symbol, _last_err, _eur_variants[:10],
                )
        _trade_mode = int(getattr(_sym_info, "trade_mode", -1) or -1) if _sym_info is not None else -1
        _trade_allowed = getattr(_sym_info, "trade_allowed", None) if _sym_info is not None else None
        _filling_mask = int(getattr(_sym_info, "filling_mode", 0) or 0) if _sym_info is not None else 0
        _acct_trade_allowed = (mt5.account_info() or object())
        _acct_trade_allowed_flag = getattr(_acct_trade_allowed, "trade_allowed", None)
        log.info(
            "[SYMBOL_TRADE_DIAG] symbol=%s trade_mode=%s trade_allowed=%s filling_modes=%s account_trade_allowed=%s",
            _order_symbol, _trade_mode, _trade_allowed, _filling_mask, _acct_trade_allowed_flag,
        )
        _SYMBOL_TRADE_MODE_FULL = 4
        if _sym_info is not None and _trade_mode != _SYMBOL_TRADE_MODE_FULL and _trade_mode != -1:
            log.warning(
                "[SYMBOL_TRADE_DISABLED] symbol=%s trade_mode=%s — symbol is not in FULL trade mode, skipping order",
                _order_symbol, _trade_mode,
            )
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "SYMBOL_TRADE_DISABLED",
                "failed_gate": "SYMBOL_TRADE_DISABLED",
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": None,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": f"SYMBOL_TRADE_DISABLED_trade_mode={_trade_mode}",
            }
        _filling_mode = _detect_filling_mode(_order_symbol)
        order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if event.get("direction") == "BUY" else getattr(mt5, "ORDER_TYPE_SELL", 1)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": _order_symbol,
            "volume": event["final_capped_lot"],
            "type": order_type,
            "price": event["entry"],
            "sl": event["sl"],
            "tp": event["tp"],
            "magic": self.settings.demo_magic_number,
            "comment": self.settings.demo_comment,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": _filling_mode,
        }
        # BLOC 8 — execution at the tick: re-read the market at the INSTANT
        # of the send (BUY@ask, SELL@bid), abort if the price drifted more
        # than EXEC_MAX_DRIFT_POINTS since validation.
        _validation_entry = _to_float(event.get("entry"))
        _send_tick = mt5.symbol_info_tick(_order_symbol)
        _tick_bid = _to_float(getattr(_send_tick, "bid", None))
        _tick_ask = _to_float(getattr(_send_tick, "ask", None))
        if _tick_bid is None or _tick_ask is None:
            log.warning("[ORDER_ABORT_PRICE_MOVED] symbol=%s reason=NO_TICK_AT_SEND", _order_symbol)
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "ORDER_ABORT_NO_TICK",
                "failed_gate": "ORDER_ABORT_NO_TICK",
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": request,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": "ORDER_ABORT_NO_TICK",
            }
        _send_price = _tick_ask if event.get("direction") == "BUY" else _tick_bid
        _point = _to_float((event.get("symbol_specs") or {}).get("point"))
        if _point is None or _point <= 0:
            _point = _to_float(getattr(mt5.symbol_info(_order_symbol), "point", None)) if mt5.symbol_info(_order_symbol) else None
        _max_drift_points = float(getattr(self.settings, "exec_max_drift_points", 300))
        _drift_points = None
        if _validation_entry is not None and _point and _point > 0:
            _drift_points = abs(_send_price - _validation_entry) / _point
            if _drift_points > _max_drift_points:
                log.warning(
                    "[ORDER_ABORT_PRICE_MOVED] symbol=%s direction=%s validation_entry=%s tick_price=%s drift_points=%.1f max=%s",
                    _order_symbol, event.get("direction"), _validation_entry, _send_price, _drift_points, _max_drift_points,
                )
                return {
                    "event_type": "DEMO_SKIP",
                    "status": "BLOCK",
                    "reason": "ORDER_ABORT_PRICE_MOVED",
                    "failed_gate": "ORDER_ABORT_PRICE_MOVED",
                    "drift_points": _drift_points,
                    "raw_payload": _demo_order_raw_payload(event),
                    "order_request": request,
                    "order_result": None,
                    "order_retcode": None,
                    "order_success": False,
                    "order_failure_reason": f"ORDER_ABORT_PRICE_MOVED_drift={_drift_points:.1f}pts",
                }
        request["price"] = _send_price
        request["deviation"] = int(getattr(self.settings, "exec_deviation_points", 50))
        event["tick_price_at_send"] = _send_price
        event["spread_at_send_points"] = ((_tick_ask - _tick_bid) / _point) if _point and _point > 0 else None
        event["drift_points_at_send"] = _drift_points
        precheck = validate_mt5_stops(
            event["broker_symbol"],
            event.get("direction"),
            event.get("sl"),
            event.get("tp"),
            event.get("final_capped_lot"),
            fallback_bid=event.get("market_bid"),
            fallback_ask=event.get("market_ask"),
            fallback_specs=event.get("symbol_specs"),
        )
        event["order_precheck"] = precheck
        if precheck.get("decision") == "BLOCK":
            reason = str(precheck.get("reason") or "INVALID_STOPS_PRECHECK")
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": reason,
                "failed_gate": reason,
                "order_precheck": precheck,
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": request,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": reason,
            }
        if precheck.get("normalized_sl") is not None:
            request["sl"] = event["sl"] = precheck["normalized_sl"]
        if precheck.get("normalized_tp") is not None:
            request["tp"] = event["tp"] = precheck["normalized_tp"]
        # RR floor at the FINAL choke-point, after every TP modification
        # (money-TP cap, old-btc TP, stop normalization). A capped TP must
        # never turn a validated setup into a sub-1.0 RR penny grab.
        final_rr = _final_rr(event.get("direction"), request.get("price"), request.get("sl"), request.get("tp"))
        event["final_rr_at_send"] = final_rr
        if final_rr is not None and final_rr < 1.0 - 1e-9:
            log.warning(
                "[OF_SLTP] verdict=RR_FLOOR_BLOCK symbol=%s direction=%s entry=%s sl=%s tp=%s rr=%.3f",
                _order_symbol, event.get("direction"), request.get("price"), request.get("sl"), request.get("tp"), final_rr,
            )
            log.warning(
                "[ORDER_ABORT_PRICE_MOVED] symbol=%s reason=RR_AT_TICK_BELOW_1_0 rr=%.3f",
                _order_symbol, final_rr,
            )
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "RR_FLOOR_BELOW_1_0",
                "failed_gate": "RR_FLOOR_BELOW_1_0",
                "order_precheck": precheck,
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": request,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": f"RR_FLOOR_BELOW_1_0_rr={final_rr:.3f}",
            }
        order_check = _safe_order_check(request)
        event["order_check"] = order_check
        if order_check and order_check.get("decision") == "BLOCK":
            reason = str(order_check.get("reason") or "ORDER_CHECK_FAILED")
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": reason,
                "failed_gate": reason,
                "order_precheck": precheck,
                "order_check": order_check,
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": request,
                "order_result": order_check.get("result"),
                "order_retcode": order_check.get("retcode"),
                "order_success": False,
                "order_failure_reason": reason,
            }
        _send_started = _time.perf_counter()
        result = mt5.order_send(request)
        _fill_latency_ms = (_time.perf_counter() - _send_started) * 1000.0
        order_result = _order_result_payload(result)
        ticket = order_result.get("ticket")
        success = _order_result_confirmed(result, ticket)
        # BLOC 8 — [EXEC_QUALITY]: measure what the broker actually did.
        _fill_price = _to_float(getattr(result, "price", None))
        _slippage_vs_tick = None
        _slippage_vs_request = None
        if _fill_price is not None and _point and _point > 0 and _fill_price > 0:
            _slippage_vs_tick = (_fill_price - _send_price) / _point
            _slippage_vs_request = (_fill_price - _to_float(request.get("price"))) / _point
        event["exec_quality"] = {
            "fill_price": _fill_price,
            "tick_price_at_send": _send_price,
            "requested_price": request.get("price"),
            "slippage_vs_tick_points": _slippage_vs_tick,
            "slippage_vs_request_points": _slippage_vs_request,
            "spread_at_send_points": event.get("spread_at_send_points"),
            "drift_points_at_send": event.get("drift_points_at_send"),
            "fill_latency_ms": round(_fill_latency_ms, 2),
            "deviation_points": request.get("deviation"),
        }
        log.info(
            "[EXEC_QUALITY] symbol=%s slippage_vs_tick=%s slippage_vs_request=%s spread_at_send=%s fill_latency_ms=%.2f",
            _order_symbol, _slippage_vs_tick, _slippage_vs_request,
            event.get("spread_at_send_points"), _fill_latency_ms,
        )
        event_type = "DEMO_ORDER" if success else "DEMO_ORDER_FAILED"
        status = "ORDER_CONFIRMED" if success else "ORDER_FAILED"
        failure_reason = None if success else _order_failure_reason(result, ticket)
        log.info(
            "[DEMO_ORDER] mode=%s status=%s retcode=%s ticket=%s symbol=%s strategy=%s lot=%s sl=%s tp=%s",
            event.get("mode") or "DEMO",
            status,
            order_result.get("retcode"),
            ticket,
            event["symbol"],
            event.get("strategy"),
            event["final_capped_lot"],
            event["sl"],
            event["tp"],
        )
        if order_result.get("retcode") == 10017:
            log.warning(
                "[BROKER_AUTOTRADING_DISABLED] symbol=%s retcode=10017 — enable AutoTrading in MT5 terminal",
                event["symbol"],
            )
        if success:
            _sent_mode = event.get("old_btc_mode") or event.get("mode") or "DEMO"
            _sent_trace = event.get("trace_id") or ""
            log.info(
                "[DEMO_ROUTER_ORDER_SENT] symbol=%s strategy=%s magic=%s lot=%s direction=%s ticket=%s mode=%s comment=%s",
                event.get("broker_symbol") or event["symbol"],
                event.get("strategy"),
                self.settings.demo_magic_number,
                event["final_capped_lot"],
                event.get("direction"),
                ticket,
                _sent_mode,
                self.settings.demo_comment,
            )
            if _sent_trace:
                log.info(
                    "[EXEC_TRACE_ORDER_SENT] trace_id=%s ticket=%s strategy=%s mode=%s symbol=%s",
                    _sent_trace, ticket, event.get("strategy"), _sent_mode,
                    event.get("broker_symbol") or event.get("symbol"),
                )
        return {
            "event_type": event_type,
            "status": status,
            "ticket": ticket,
            "tp": event["tp"],
            "sl": event["sl"],
            "max_money_tp": max_tp,
            "order_precheck": precheck,
            "order_check": order_check,
            "exec_quality": event.get("exec_quality"),
            "tick_price_at_send": event.get("tick_price_at_send"),
            "spread_at_send_points": event.get("spread_at_send_points"),
            "drift_points_at_send": event.get("drift_points_at_send"),
            "raw_payload": _demo_order_raw_payload(event),
            "order_request": request,
            "order_result": order_result,
            "order_retcode": order_result.get("retcode"),
            "order_success": success,
            "order_failure_reason": failure_reason,
        }

    def _send_pending_order(self, event: dict) -> dict:
        """Place a BUY_STOP or SELL_STOP pending order for SIMO_ATM_BREAKOUT."""
        broker_symbol = str(event.get("broker_symbol") or event.get("symbol") or "")
        direction = str(event.get("direction") or "").upper()
        magic = self.settings.demo_magic_number
        expiry_minutes = int(getattr(self.settings, "simo_atm_pending_expiry_minutes", 30))
        pending_type_str = "BUY_STOP" if direction == "BUY" else "SELL_STOP"
        mt5_order_type = getattr(mt5, "ORDER_TYPE_BUY_STOP", 4) if direction == "BUY" else getattr(mt5, "ORDER_TYPE_SELL_STOP", 5)

        existing_pending = _get_pending_orders_for_symbol_magic(broker_symbol, magic)
        if existing_pending:
            ticket = getattr(existing_pending[0], "ticket", None)
            log.info(
                "[SIMO_ATM_PENDING] skipped reason=PENDING_ALREADY_EXISTS symbol=%s ticket=%s",
                broker_symbol, ticket,
            )
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "PENDING_ALREADY_EXISTS",
                "failed_gate": "PENDING_ALREADY_EXISTS",
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": None,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": "PENDING_ALREADY_EXISTS",
            }

        _cancel_expired_pending_orders(broker_symbol, magic)

        entry = event.get("entry")
        sl = event.get("sl")
        tp = event.get("tp")
        lot = event.get("final_capped_lot")
        if entry is None or sl is None or tp is None or lot is None:
            log.info("[SIMO_ATM_PENDING] skipped reason=MISSING_ENTRY_SL_TP symbol=%s", broker_symbol)
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "MISSING_ENTRY_SL_TP",
                "failed_gate": "MISSING_ENTRY_SL_TP",
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": None,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": "MISSING_ENTRY_SL_TP",
            }

        final_rr = _final_rr(direction, entry, sl, tp)
        event["final_rr_at_send"] = final_rr
        if final_rr is not None and final_rr < 1.0 - 1e-9:
            log.warning(
                "[OF_SLTP] verdict=RR_FLOOR_BLOCK symbol=%s direction=%s entry=%s sl=%s tp=%s rr=%.3f path=PENDING",
                broker_symbol, direction, entry, sl, tp, final_rr,
            )
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": "RR_FLOOR_BELOW_1_0",
                "failed_gate": "RR_FLOOR_BELOW_1_0",
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": None,
                "order_result": None,
                "order_retcode": None,
                "order_success": False,
                "order_failure_reason": f"RR_FLOOR_BELOW_1_0_rr={final_rr:.3f}",
            }

        expiry_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=expiry_minutes)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_PENDING", 5),
            "symbol": broker_symbol,
            "volume": float(lot),
            "type": mt5_order_type,
            "price": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "expiration": expiry_dt,
            "type_time": getattr(mt5, "ORDER_TIME_SPECIFIED", 1),
            "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", 2),
            "magic": magic,
            "comment": self.settings.demo_comment,
        }

        order_check = _safe_order_check(request)
        event["order_check"] = order_check
        if order_check and order_check.get("decision") == "BLOCK":
            reason = str(order_check.get("reason") or "ORDER_CHECK_FAILED")
            log.info(
                "[SIMO_ATM_PENDING] skipped reason=%s symbol=%s pending_type=%s",
                reason, broker_symbol, pending_type_str,
            )
            return {
                "event_type": "DEMO_SKIP",
                "status": "BLOCK",
                "reason": reason,
                "failed_gate": reason,
                "order_check": order_check,
                "raw_payload": _demo_order_raw_payload(event),
                "order_request": request,
                "order_result": order_check.get("result"),
                "order_retcode": order_check.get("retcode"),
                "order_success": False,
                "order_failure_reason": reason,
            }

        result = mt5.order_send(request)
        order_result = _order_result_payload(result)
        ticket = order_result.get("ticket")
        success = _order_result_confirmed(result, ticket)
        event_type = "DEMO_ORDER" if success else "DEMO_ORDER_FAILED"
        status = "ORDER_CONFIRMED" if success else "ORDER_FAILED"
        failure_reason = None if success else _order_failure_reason(result, ticket)
        log.info(
            "[SIMO_ATM_PENDING] %s ticket=%s symbol=%s pending_type=%s entry=%s sl=%s tp=%s lot=%s retcode=%s",
            "placed" if success else "failed",
            ticket,
            broker_symbol,
            pending_type_str,
            entry,
            sl,
            tp,
            lot,
            order_result.get("retcode"),
        )
        if order_result.get("retcode") == 10017:
            log.warning(
                "[BROKER_AUTOTRADING_DISABLED] symbol=%s retcode=10017 — enable AutoTrading in MT5 terminal",
                broker_symbol,
            )
        return {
            "event_type": event_type,
            "status": status,
            "ticket": ticket,
            "tp": tp,
            "sl": sl,
            "pending_type": pending_type_str,
            "order_check": order_check,
            "raw_payload": _demo_order_raw_payload(event),
            "order_request": request,
            "order_result": order_result,
            "order_retcode": order_result.get("retcode"),
            "order_success": success,
            "order_failure_reason": failure_reason,
        }

    def _position_armed(self, pos: Any) -> bool:
        """Armed = profit already locked: Exit V2 BE armed, or broker SL
        moved beyond the entry in the profit direction. Armed positions keep
        their lock through news; only NON-armed ones are pre-closed."""
        ticket = int(getattr(pos, "ticket", 0) or 0)
        state = self._exit_v2_state.get(ticket) or {}
        if state.get("be_armed"):
            return True
        sl = _to_float(getattr(pos, "sl", None))
        entry = _to_float(getattr(pos, "price_open", None))
        if sl is None or entry is None or sl <= 0:
            return False
        is_buy = int(getattr(pos, "type", 0) or 0) == 0
        return sl >= entry if is_buy else sl <= entry

    def _process_exit_v2_position(self, pos: Any, account: dict | None, now: datetime | None = None) -> list[dict]:
        """Exit V2 — single exit authority for GOLD positions.

        Account gate: DEMO=ACTIVE (closes execute), anything else=SHADOW.
        Fail-closed: any exception leaves the original SL/TP untouched.
        CLOSE-BASED: no SL-modify request exists on this path by construction.
        """
        events: list[dict] = []
        ticket = int(getattr(pos, "ticket", 0) or 0)
        symbol = str(getattr(pos, "symbol", "") or "")
        try:
            cfg = ExitV2Config(
                mode=str(getattr(self.settings, "exit_v2_mode", "ACTIVE") or "ACTIVE").upper(),
                tp_usd=float(getattr(self.settings, "exit_v2_tp_usd", 0.0)),
                be_arm_usd=float(getattr(self.settings, "exit_v2_be_arm_usd", 2.00)),
                be_floor_usd=float(getattr(self.settings, "exit_v2_be_floor_usd", 0.10)),
                trail_start_usd=float(getattr(self.settings, "exit_v2_trail_start_usd", 2.0)),
                trail_gap_usd=float(getattr(self.settings, "exit_v2_trail_gap_usd", 1.2)),
            )
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            action = evaluate_exit_v2(pos, tick, info, cfg, self._exit_v2_state)
            account_type = self.account_diagnostics(account)["account_type"]
            shadow = cfg.mode != "ACTIVE" or account_type != "DEMO"
            log.info(
                "[EXIT_V2] ticket=%s symbol=%s action=%s reason=%s profit=%s peak=%s be_armed=%s mode=%s account=%s",
                ticket, symbol, action.get("action"), action.get("reason"),
                action.get("profit_usd"), action.get("peak_usd"), action.get("be_armed"),
                "SHADOW" if shadow else "ACTIVE", account_type,
            )
            if str(action.get("action")) != "CLOSE":
                return events
            if shadow:
                events.append(
                    _quick_exit_event(
                        "EXIT_V2_SHADOW",
                        "SHADOW",
                        {**action, "exit_authority": "EXIT_V2", "shadow_reason": f"mode={cfg.mode},account={account_type}"},
                        now,
                    )
                )
                return events
            result_event = self._quick_exit_close(pos, action, now)
            result_event["event_type"] = "EXIT_V2_CLOSE"
            result_event["exit_authority"] = "EXIT_V2"
            result_event["exit_v2_reason"] = action.get("reason")
            result_event["exit_v2_peak_usd"] = action.get("peak_usd")
            result_event["exit_v2_floor_usd"] = action.get("floor_usd")
            events.append(result_event)
            return events
        except Exception as exc:
            # Fail-closed: the position keeps its original SL/TP.
            log.warning(
                "[EXIT_V2] fail_closed ticket=%s symbol=%s error=%s sl_tp=UNTOUCHED",
                ticket, symbol, str(exc)[:200],
            )
            return events

    def _quick_exit_modify_sl(self, pos: Any, action: dict, now: datetime | None = None) -> dict:
        symbol = str(getattr(pos, "symbol", "") or "")
        request = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
            "symbol": symbol,
            "position": getattr(pos, "ticket", None),
            "sl": float(action["new_sl"]),
            "tp": float(action.get("current_tp") or getattr(pos, "tp", 0.0) or 0.0),
            "magic": self.settings.demo_magic_number,
            "comment": "HERMES_QUICK_EXIT",
        }
        result = mt5.order_send(request)
        order_result = _order_result_payload(result)
        success = _retcode_done(result)
        event = _quick_exit_event(
            "QUICK_EXIT_SLTP",
            "ORDER_CONFIRMED" if success else "ORDER_FAILED",
            {
                **action,
                "order_request": request,
                "order_result": order_result,
                "order_retcode": order_result.get("retcode"),
                "order_success": success,
                "order_failure_reason": None if success else _order_failure_reason(result, getattr(pos, "ticket", None)),
            },
            now,
        )
        log.info(
            "[QUICK_EXIT] action=%s status=%s ticket=%s symbol=%s profit_usd=%s new_sl=%s",
            action.get("action"),
            event["status"],
            action.get("ticket"),
            symbol,
            action.get("profit_usd"),
            action.get("new_sl"),
        )
        return event

    def _quick_exit_close(self, pos: Any, action: dict, now: datetime | None = None) -> dict:
        symbol = str(getattr(pos, "symbol", "") or "")
        tick = mt5.symbol_info_tick(symbol)
        side = str(action.get("side") or "").upper()
        is_buy = side == "BUY"
        close_type = getattr(mt5, "ORDER_TYPE_SELL", 1) if is_buy else getattr(mt5, "ORDER_TYPE_BUY", 0)
        price = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": symbol,
            "position": getattr(pos, "ticket", None),
            "volume": getattr(pos, "volume", None),
            "type": close_type,
            "price": float(price) if price is not None else action.get("exit_price"),
            "deviation": 20,
            "magic": self.settings.demo_magic_number,
            "comment": "HERMES_QUICK_EXIT_TP",
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
        }
        result = mt5.order_send(request)
        order_result = _order_result_payload(result)
        ticket = order_result.get("ticket") or getattr(pos, "ticket", None)
        success = _order_result_confirmed(result, ticket) or _retcode_done(result)
        event = _quick_exit_event(
            "QUICK_EXIT_CLOSE",
            "ORDER_CONFIRMED" if success else "ORDER_FAILED",
            {
                **action,
                "order_request": request,
                "order_result": order_result,
                "order_retcode": order_result.get("retcode"),
                "order_success": success,
                "order_failure_reason": None if success else _order_failure_reason(result, ticket),
            },
            now,
        )
        log.info(
            "[QUICK_EXIT] action=CLOSE_TP status=%s ticket=%s symbol=%s profit_usd=%s",
            event["status"],
            action.get("ticket"),
            symbol,
            action.get("profit_usd"),
        )
        return event

    def _rescue_close(self, pos: Any, rescue_action: dict, now: datetime | None = None) -> dict:
        """Close a HERMES BTC position via Smart Rescue Quick Exit.

        Safety pre-conditions enforced here:
          - DEMO account check is done upstream in process_quick_exits()
          - symbol must be BTCUSD# (enforced by is_hermes_btc_pos caller)
          - volume taken directly from position (no inflation)
          - opposite order type used (BUY→SELL, SELL→BUY)
        """
        symbol = str(getattr(pos, "symbol", "") or "")
        ticket = int(getattr(pos, "ticket", 0) or 0)
        pos_type = int(getattr(pos, "type", 0) or 0)
        is_buy = pos_type == getattr(mt5, "POSITION_TYPE_BUY", 0)
        close_type = getattr(mt5, "ORDER_TYPE_SELL", 1) if is_buy else getattr(mt5, "ORDER_TYPE_BUY", 0)
        side = "BUY" if is_buy else "SELL"

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.warning("[OLD_BTC_RESCUE_ERROR] ticket=%s error=symbol_info_tick_returned_none", ticket)
            return _quick_exit_event(
                "RESCUE_CLOSE",
                "ORDER_FAILED",
                {**rescue_action, "symbol": symbol, "ticket": ticket, "side": side,
                 "order_success": False, "order_failure_reason": "NO_TICK_DATA"},
                now,
            )

        price = getattr(tick, "bid", None) if is_buy else getattr(tick, "ask", None)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": symbol,
            "position": ticket,
            "volume": getattr(pos, "volume", None),
            "type": close_type,
            "price": float(price) if price is not None else 0.0,
            "deviation": 20,
            "magic": self.settings.demo_magic_number,
            "comment": "HERMES_RESCUE_EXIT",
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
        }
        result = mt5.order_send(request)
        order_result = _order_result_payload(result)
        close_ticket = order_result.get("ticket") or ticket
        success = _order_result_confirmed(result, close_ticket) or _retcode_done(result)
        retcode = order_result.get("retcode")
        close_profit = float(getattr(pos, "profit", 0) or 0)

        if success:
            log.info(
                "[OLD_BTC_RESCUE_CLOSED] ticket=%s close_profit=%s close_retcode=%s",
                ticket, close_profit, retcode,
            )
        else:
            log.warning(
                "[OLD_BTC_RESCUE_CLOSE_FAILED] ticket=%s retcode=%s error=%s",
                ticket, retcode, _order_failure_reason(result, ticket),
            )

        return _quick_exit_event(
            "RESCUE_CLOSE",
            "ORDER_CONFIRMED" if success else "ORDER_FAILED",
            {
                **rescue_action,
                "symbol": symbol,
                "ticket": ticket,
                "side": side,
                "profit_usd": close_profit,
                "order_request": request,
                "order_result": order_result,
                "order_retcode": retcode,
                "order_success": success,
                "order_failure_reason": None if success else _order_failure_reason(result, ticket),
            },
            now,
        )

    # ── Fast Smart Exit daemon interface ─────────────────────────────────────

    def fast_exit_close_position(self, pos: Any, reason: str, now: datetime | None = None) -> dict:
        """Close a HERMES BTC position from the fast exit daemon.

        Called by BtcFastExitDaemon (background thread).
        Verifies identity + delegates to _rescue_close so mt5.order_send
        stays exclusively inside demo_router.py.
        """
        ticket = int(getattr(pos, "ticket", 0) or 0)
        symbol = str(getattr(pos, "symbol", "") or "")
        account_info = mt5.account_info()
        if not is_mt5_demo_account(account_info):
            log.warning(
                "[OLD_BTC_FAST_EXIT_ACCOUNT_CHECK] ticket=%s reason=ACCOUNT_NOT_DEMO_block_order_send",
                ticket,
            )
            return {"status": "BLOCK", "reason": "ACCOUNT_NOT_DEMO"}
        if not is_hermes_btc_pos(pos, self.settings.demo_magic_number):
            log.warning(
                "[OLD_BTC_FAST_EXIT_SKIP] ticket=%s reason=NOT_HERMES_BTC symbol=%s",
                ticket, symbol,
            )
            return {"status": "SKIP", "reason": "NOT_HERMES_BTC"}
        action = {
            "action": "RESCUE_CLOSE",
            "reason": reason,
            "ticket": ticket,
            "profit": float(getattr(pos, "profit", 0) or 0),
        }
        return self._rescue_close(pos, action, now)

    def start_fast_exit_daemon(self) -> "BtcFastExitDaemon":  # type: ignore[name-defined]
        """Create and start the BtcFastExitDaemon bound to this router."""
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        daemon = BtcFastExitDaemon(self.settings, self.fast_exit_close_position)
        daemon.start()
        return daemon

    def _apply_max_money_tp(self, event: dict) -> dict:
        enabled = bool(getattr(self.settings, "max_money_tp_enabled", True))
        symbol = str(event.get("broker_symbol") or event.get("symbol") or "").upper()
        original_tp = _to_float(event.get("tp"))
        entry = _to_float(event.get("entry"))
        sl = _to_float(event.get("sl"))
        lot = _to_float(event.get("final_capped_lot"))
        max_tp_usd = float(getattr(self.settings, "max_tp_usd", 5.0))
        payload = {
            "enabled": enabled,
            "max_tp_usd": max_tp_usd,
            "applied": False,
            "original_tp": original_tp,
            "final_tp": original_tp,
            "sl_unchanged": sl,
            "entry": entry,
            "lot": lot,
            "estimated_tp_money": None,
            "tick_value": None,
            "tick_size": None,
            "digits": None,
            "block_reason": None,
        }
        if not enabled or not _max_money_tp_symbol_allowed(self.settings, symbol):
            return payload
        side = str(event.get("direction") or "").upper()
        info = mt5.symbol_info(symbol)
        tick_value = _to_float(getattr(info, "trade_tick_value", None)) or _to_float(getattr(info, "tick_value", None))
        tick_size = _to_float(getattr(info, "trade_tick_size", None)) or _to_float(getattr(info, "tick_size", None))
        digits = int(getattr(info, "digits", 0) or 0) if info is not None else None
        point = _to_float(getattr(info, "point", None)) if info is not None else None
        payload.update({"tick_value": tick_value, "tick_size": tick_size, "digits": digits})
        if entry is None or original_tp is None or sl is None or lot is None or lot <= 0 or tick_value is None or tick_value <= 0 or tick_size is None or tick_size <= 0 or side not in {"BUY", "SELL"}:
            payload["block_reason"] = "MAX_TP_SYMBOL_SPEC_INVALID"
            log.info("[MAX_TP] decision=BLOCK reason=MAX_TP_SYMBOL_SPEC_INVALID")
            return payload
        value_per_price_unit = tick_value / tick_size * lot
        if value_per_price_unit <= 0 or not math.isfinite(value_per_price_unit):
            payload["block_reason"] = "MAX_TP_SYMBOL_SPEC_INVALID"
            log.info("[MAX_TP] decision=BLOCK reason=MAX_TP_SYMBOL_SPEC_INVALID")
            return payload
        max_distance = max_tp_usd / value_per_price_unit
        max_tp_price = entry + max_distance if side == "BUY" else entry - max_distance
        capped_tp = original_tp
        applied = False
        if side == "BUY" and original_tp > max_tp_price:
            capped_tp = max_tp_price
            applied = True
        elif side == "SELL" and original_tp < max_tp_price:
            capped_tp = max_tp_price
            applied = True
        if applied and digits is not None and digits >= 0:
            factor = 10**digits
            epsilon = 1e-9
            capped_tp = math.floor((capped_tp + epsilon) * factor) / factor if side == "BUY" else math.ceil((capped_tp - epsilon) * factor) / factor
        estimated = abs(capped_tp - entry) * value_per_price_unit
        payload["estimated_tp_money"] = round(estimated, 6)
        payload["final_tp"] = capped_tp
        payload["applied"] = applied
        if not _valid_sl_tp(side, entry, sl, capped_tp):
            payload["block_reason"] = "MAX_TP_TOO_CLOSE_TO_MARKET"
            log.info("[MAX_TP] decision=BLOCK reason=MAX_TP_TOO_CLOSE_TO_MARKET")
            return payload
        stops_level = _to_float(getattr(info, "trade_stops_level", None)) or _to_float(getattr(info, "stops_level", None)) or 0.0
        min_stop_distance = stops_level * (point or 0.0)
        if min_stop_distance > 0 and abs(capped_tp - entry) < min_stop_distance:
            payload["block_reason"] = "MAX_TP_TOO_CLOSE_TO_MARKET"
            log.info("[MAX_TP] decision=BLOCK reason=MAX_TP_TOO_CLOSE_TO_MARKET")
            return payload
        event["tp"] = capped_tp
        if payload["applied"]:
            log.info(
                "[MAX_TP] applied=true symbol=%s side=%s lot=%s original_tp=%s capped_tp=%s estimated_tp_money=%.2f sl_unchanged=%s",
                symbol,
                side,
                lot,
                original_tp,
                capped_tp,
                min(estimated, max_tp_usd),
                sl,
            )
        else:
            log.info("[MAX_TP] applied=false reason=ORIGINAL_TP_ALREADY_BELOW_MAX estimated_tp_money=%s", payload["estimated_tp_money"])
        return payload

    def _risk_lot(self, decision: dict, account: dict | None, symbol_specs: dict) -> float | None:
        equity = _to_float((account or {}).get("equity") or (account or {}).get("balance"))
        entry = _to_float(decision.get("entry"))
        sl = _to_float(decision.get("sl"))
        tick_value = _to_float(symbol_specs.get("tick_value"))
        tick_size = _to_float(symbol_specs.get("tick_size"))
        if equity is None or entry is None or sl is None or tick_value is None or tick_size in {None, 0}:
            return None
        risk_amount = equity * (self.settings.demo_max_risk_per_trade_pct / 100.0)
        risk_per_lot = (abs(entry - sl) / tick_size) * tick_value
        if risk_per_lot <= 0:
            return None
        return _floor_to_step(risk_amount / risk_per_lot, _to_float(symbol_specs.get("volume_step")) or 0.01)

    def _risk_pct(self, decision: dict, lot: float | None, account: dict | None, symbol_specs: dict) -> float | None:
        if lot is None:
            return None
        equity = _to_float((account or {}).get("equity") or (account or {}).get("balance"))
        entry = _to_float(decision.get("entry"))
        sl = _to_float(decision.get("sl"))
        tick_value = _to_float(symbol_specs.get("tick_value"))
        tick_size = _to_float(symbol_specs.get("tick_size"))
        if equity is None or equity <= 0 or entry is None or sl is None or tick_value is None or tick_size in {None, 0}:
            return None
        risk_amount = (abs(entry - sl) / tick_size) * tick_value * lot
        return round((risk_amount / equity) * 100.0, 8)

    def _pilot_window_active(self, now: datetime) -> bool:
        return self.pilot_started_at <= now <= self.pilot_started_at + timedelta(hours=self.settings.demo_pilot_hours)

    def _demo_positions(self) -> list[Any]:
        positions = mt5.positions_get() or []
        return [pos for pos in positions if getattr(pos, "magic", None) == self.settings.demo_magic_number]

    def _stats(self, now: datetime, events: list[dict] | None = None) -> dict:
        opened_today = 0
        exploration_opened_today = 0
        strong_setup_learning_opened_today = 0
        opened_by_symbol: Counter[str] = Counter()
        opened_by_symbol_strategy: Counter[str] = Counter()
        exploration_by_symbol: Counter[str] = Counter()
        strong_setup_learning_by_symbol: Counter[str] = Counter()
        smoke_test_confirmed_orders = 0
        daily_pnl = 0.0
        losses = 0
        loaded_events = events if events is not None else self._load_events()
        smoke_start = self._smoke_test_started_at()
        smoke_expires = self._smoke_test_expires_at()
        for event in loaded_events:
            created = _parse_iso(str(event.get("created_at") or ""))
            if created and created.date() == now.date() and _confirmed_order_event(event):
                symbol = _event_symbol(event)
                opened_today += 1
                if symbol:
                    opened_by_symbol[symbol] += 1
                    opened_by_symbol_strategy[_symbol_strategy_key(symbol, str(event.get("strategy") or "UNKNOWN").upper())] += 1
                if event.get("mode") == "DEMO_EXPLORATION":
                    exploration_opened_today += 1
                    if symbol:
                        exploration_by_symbol[symbol] += 1
                if event.get("mode") == "DEMO_STRONG_SETUP_LEARNING":
                    strong_setup_learning_opened_today += 1
                    if symbol:
                        strong_setup_learning_by_symbol[symbol] += 1
            if (
                created
                and smoke_start <= created <= smoke_expires
                and event.get("exploration_override_reason") == "DEMO_SMOKE_TEST_24H"
                and _confirmed_order_event(event)
            ):
                smoke_test_confirmed_orders += 1
            if created and created.date() == now.date() and event.get("event_type") == "DEMO_CLOSE":
                daily_pnl += _to_float(event.get("pnl")) or 0.0
        for event in reversed(loaded_events):
            if event.get("event_type") != "DEMO_CLOSE":
                continue
            if str(event.get("result") or "").upper() == "LOSS":
                losses += 1
                continue
            break
        balance = 10000.0
        return {
            "demo_trades_opened_today": opened_today,
            "demo_trades_opened_by_symbol": dict(opened_by_symbol),
            "demo_trades_opened_by_symbol_strategy": dict(opened_by_symbol_strategy),
            "exploration_trades_opened_today": exploration_opened_today,
            "exploration_trades_opened_by_symbol": dict(exploration_by_symbol),
            "strong_setup_learning_trades_opened_today": strong_setup_learning_opened_today,
            "strong_setup_learning_trades_opened_by_symbol": dict(strong_setup_learning_by_symbol),
            "smoke_test_confirmed_orders": smoke_test_confirmed_orders,
            "daily_loss_pct": round(abs(min(0.0, daily_pnl)) / balance * 100.0, 8),
            "consecutive_losses": losses,
        }

    def _relaxed_trade_state(self, decision: dict, symbol: str, now: datetime) -> dict:
        if not decision.get("relaxed_mode_active"):
            return {
                "relaxed_trade_count_today": 0,
                "relaxed_symbol_trade_count_today": 0,
            "last_relaxed_trade_result": None,
            "relaxed_last_symbol_trade_at": None,
            "relaxed_now_utc": now.isoformat(),
            }
        events = self._load_events()
        total_today = 0
        symbol_today = 0
        last_symbol_trade_at: datetime | None = None
        last_result = None
        for event in events:
            created = _parse_iso(str(event.get("created_at") or ""))
            if not created:
                continue
            event_symbol = _event_symbol(event)
            if created.date() == now.date() and event.get("relaxed_mode_active") and _confirmed_order_event(event):
                total_today += 1
                if event_symbol == symbol:
                    symbol_today += 1
                    if last_symbol_trade_at is None or created > last_symbol_trade_at:
                        last_symbol_trade_at = created
            if created.date() == now.date() and event.get("relaxed_mode_active") and event_symbol == symbol and event.get("event_type") == "DEMO_CLOSE":
                last_result = str(event.get("result") or ("LOSS" if (_to_float(event.get("pnl")) or 0.0) < 0 else "WIN")).upper()
        return {
            "relaxed_trade_count_today": total_today,
            "relaxed_symbol_trade_count_today": symbol_today,
            "last_relaxed_trade_result": last_result,
            "relaxed_last_symbol_trade_at": last_symbol_trade_at.isoformat() if last_symbol_trade_at else None,
            "relaxed_now_utc": now.isoformat(),
        }

    def _relaxed_trade_block_reason(self, decision: dict, gates: dict) -> str | None:
        if not decision.get("relaxed_mode_active"):
            return None
        if str(gates.get("last_relaxed_trade_result") or "").upper() == "LOSS":
            return "RELAXED_DISABLED_AFTER_LOSS"
        if int(gates.get("relaxed_trade_count_today") or 0) >= 2:
            return "RELAXED_DAILY_CAP"
        last_trade = _parse_iso(str(gates.get("relaxed_last_symbol_trade_at") or ""))
        if last_trade:
            now = _parse_iso(str(gates.get("relaxed_now_utc") or "")) or datetime.now(timezone.utc)
            if (now - last_trade).total_seconds() < 6 * 3600:
                if _demo_ignore_duration_blocks(self.settings):
                    log.info("[DURATION_GATE] decision=PASS reason=DURATION_BLOCKS_DISABLED_BY_USER_ORDER")
                    return None
                return "RELAXED_COOLDOWN_6H"
        return None

    def _load_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        max_bytes = int(getattr(self.settings, "demo_router_events_max_bytes", 10_485_760))
        max_lines = int(getattr(self.settings, "demo_router_events_max_lines", 5_000))
        try:
            file_size = self.events_path.stat().st_size
            if file_size > max_bytes:
                bak_path = self.events_path.with_suffix(
                    f".{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.bak"
                )
                try:
                    self.events_path.rename(bak_path)
                    log.info(
                        "[DEMO_ROUTER_EVENTS_ROTATED] path=%s size_bytes=%s backup=%s",
                        self.events_path, file_size, bak_path,
                    )
                except Exception as rot_exc:
                    log.warning("[DEMO_ROUTER_EVENTS_SKIP] reason=MEMORY_SAFE_FALLBACK error=%s", rot_exc)
                return []
            log_event_throttled(
                "DEMO_ROUTER_EVENTS_TAIL",
                "[DEMO_ROUTER_EVENTS_TAIL] path=%s size_bytes=%s max_lines=%s"
                % (self.events_path, file_size, max_lines),
                state=(str(self.events_path), max_lines),
            )
            lines = _tail_lines(self.events_path, max_lines)
        except Exception as exc:
            log.warning("[DEMO_ROUTER_EVENTS_SKIP] reason=MEMORY_SAFE_FALLBACK error=%s", exc)
            return []
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _record_event(self, event: dict) -> None:
        with self._events_lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_events_if_needed()
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

    def _rotate_events_if_needed(self) -> None:
        max_bytes = 20 * 1024 * 1024
        if not self.events_path.exists() or self.events_path.stat().st_size <= max_bytes:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rotated = self.events_path.with_name(f"{self.events_path.stem}.{timestamp}.jsonl")
        self.events_path.rename(rotated)
        log.info("[DEMO_ROUTER_EVENTS_ROTATED] path=%s backup=%s", self.events_path, rotated)
        backups = sorted(
            self.events_path.parent.glob(f"{self.events_path.stem}.*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[5:]:
            try:
                stale.unlink()
            except OSError as exc:
                log.warning("[DEMO_ROUTER_EVENTS_RETENTION_WARNING] path=%s error=%s", stale, exc)

    def _ingest_event(self, event: dict) -> dict:
        return {"table": "execution_events", "demo_action": event.get("event_type"), "data": event}


def _last_demo_order_at(events: list[dict], symbol: str) -> datetime | None:
    canonical = _canonical_trade_symbol(symbol)
    timestamps = [
        created
        for event in events
        if event.get("event_type") == "DEMO_ORDER"
        and _canonical_trade_symbol(event.get("broker_symbol") or event.get("symbol")) == canonical
        and (created := _parse_iso(str(event.get("created_at") or ""))) is not None
    ]
    return max(timestamps) if timestamps else None


def _is_symbol_trade_cooldown_active(
    now: datetime,
    last_trade_at: datetime | None,
    cooldown_minutes: int,
    *,
    enabled: bool,
) -> bool:
    if not enabled or last_trade_at is None or cooldown_minutes <= 0:
        return False
    return (now - last_trade_at).total_seconds() < cooldown_minutes * 60


def _weekend_symbol_block_reason(symbol: object, is_weekend: bool) -> str | None:
    if not is_weekend:
        return None
    if _is_btc(symbol):
        return None
    return "WEEKEND_CLOSED"


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Read at most max_lines lines from the tail of a file without loading the whole file."""
    chunk_size = 8192
    result: list[bytes] = []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        buffer = b""
        while pos > 0 and len(result) < max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            buffer = fh.read(read_size) + buffer
            lines = buffer.split(b"\n")
            buffer = lines[0]
            for line in reversed(lines[1:]):
                result.append(line)
                if len(result) >= max_lines:
                    break
        if buffer and len(result) < max_lines:
            result.append(buffer)
    result.reverse()
    return [line.decode("utf-8", errors="ignore") for line in result if line.strip()]


def build_demo_report(
    settings: Settings,
    events_path: Path | None = None,
    hours: int | None = None,
    since_pilot_start: bool = False,
    since_backend_start: bool = False,
    since_report_reset: bool = False,
    now: datetime | None = None,
) -> dict:
    router = DemoKellyRouter(settings, events_path)
    now_dt = now or datetime.now(timezone.utc)
    report_generated_at = now_dt.isoformat()
    report_now_utc = now_dt
    future_cutoff = report_now_utc + timedelta(minutes=2)
    backend_started_at = read_backend_started_at()
    report_window_started_at = read_report_window_started_at()
    events = router._load_events()
    valid_events, future_events = _exclude_future_events(events, future_cutoff)
    future_reasons = dict(Counter(str(event.get("reason") or event.get("near_miss_reason") or event.get("event_type") or "UNKNOWN") for event in future_events))
    window_start = _demo_report_window_start(
        settings,
        router,
        hours,
        since_pilot_start,
        since_backend_start,
        since_report_reset,
        backend_started_at,
        report_window_started_at,
        now_dt,
    )
    current_events = _events_between(valid_events, window_start, future_cutoff)
    historical_events = [event for event in valid_events if event not in current_events]
    opens = [e for e in current_events if _confirmed_order_event(e)]
    order_attempts = [e for e in current_events if e.get("event_type") in {"DEMO_ORDER", "DEMO_ORDER_FAILED"}]
    order_failures = [e for e in current_events if e.get("event_type") == "DEMO_ORDER_FAILED"]
    closes = _closed_demo_trade_rows(current_events, settings.demo_magic_number)
    trades_table_pnl = round(sum((_to_float(e.get("pnl")) or 0.0) for e in closes), 6)
    report_hours = int(hours or 24)
    mt5_pnl = get_mt5_hermes_pnl_truth(report_hours, settings.demo_magic_number, future_cutoff, window_start)
    mt5_window_pnl = mt5_pnl.get("mt5_window_pnl") if mt5_pnl.get("available") else None
    demo_pnl = round(mt5_window_pnl, 6) if mt5_window_pnl is not None else trades_table_pnl
    pnl_difference = round(demo_pnl - trades_table_pnl, 6) if mt5_window_pnl is not None else None
    pnl_source = "MT5_HISTORY_DEALS" if mt5_window_pnl is not None else "TRADES_TABLE_FALLBACK"
    pnl_reconciliation_status = (
        "DEMO_REPORT_PNL_MISMATCH"
        if pnl_difference is not None and abs(pnl_difference) > 1e-9
        else "MATCH"
        if pnl_difference is not None
        else "MT5_HISTORY_UNAVAILABLE"
    )
    skips = [e for e in current_events if e.get("event_type") == "DEMO_SKIP"]
    pnl_by_strategy: dict[str, float] = defaultdict(float)
    wins = 0
    for close in closes:
        strategy = str(close.get("strategy") or "UNKNOWN")
        pnl = _to_float(close.get("pnl")) or 0.0
        pnl_by_strategy[strategy] += pnl
        if str(close.get("result") or "").upper() == "WIN":
            wins += 1
    stats = router._stats(now_dt, valid_events)
    smoke_started_at = router._smoke_test_started_at()
    smoke_expires_at = router._smoke_test_expires_at()
    smoke_confirmed_orders = [
        e
        for e in valid_events
        if _confirmed_order_event(e)
        and e.get("exploration_override_reason") == "DEMO_SMOKE_TEST_24H"
        and (created := _parse_iso(str(e.get("created_at") or "")))
        and smoke_started_at <= created <= smoke_expires_at
    ]
    last_kelly = next((e for e in reversed(current_events) if e.get("kelly_suggested_lot") is not None), None)
    best = max(pnl_by_strategy, key=pnl_by_strategy.get) if pnl_by_strategy else None
    worst = min(pnl_by_strategy, key=pnl_by_strategy.get) if pnl_by_strategy else None
    latest_decision = next((e for e in reversed(current_events) if e.get("decision") or e.get("reason")), None)
    current_window_skips = [e for e in current_events if e.get("event_type") == "DEMO_SKIP"]
    historical_skips = [e for e in historical_events if e.get("event_type") == "DEMO_SKIP"]
    setup_events = [e for e in current_events if e.get("event_type") == "SETUP_HUNTER"]
    near_miss_events = [e for e in current_events if e.get("event_type") == "NEAR_MISS"]
    position_sync_events = [e for e in current_events if e.get("event_type") == "POSITION_SYNC"]
    exploration_events = [e for e in current_events if e.get("exploration_decision")]
    exploration_attempts = [e for e in order_attempts if e.get("mode") == "DEMO_EXPLORATION"]
    exploration_orders = [e for e in opens if e.get("mode") == "DEMO_EXPLORATION"]
    exploration_failures = [e for e in order_failures if e.get("mode") == "DEMO_EXPLORATION"]
    strong_setup_learning_attempts = [e for e in order_attempts if e.get("mode") == "DEMO_STRONG_SETUP_LEARNING"]
    strong_setup_learning_orders = [e for e in opens if e.get("mode") == "DEMO_STRONG_SETUP_LEARNING"]
    strong_setup_learning_failures = [e for e in order_failures if e.get("mode") == "DEMO_STRONG_SETUP_LEARNING"]
    fallback_events = [e for e in current_events if e.get("fallback_decision") in {"ALLOW", "BLOCK"} or e.get("mode") == "DEMO_ADAPTIVE_FALLBACK"]
    fallback_attempts = [e for e in order_attempts if e.get("mode") == "DEMO_ADAPTIVE_FALLBACK"]
    fallback_orders = [e for e in opens if e.get("mode") == "DEMO_ADAPTIVE_FALLBACK"]
    micro_events = [e for e in current_events if e.get("micro_discovery_decision") in {"ALLOW", "BLOCK"} or e.get("mode") == "DEMO_MICRO_DISCOVERY"]
    micro_attempts = [e for e in order_attempts if e.get("mode") == "DEMO_MICRO_DISCOVERY"]
    micro_orders = [e for e in opens if e.get("mode") == "DEMO_MICRO_DISCOVERY"]
    quant_candidates = [e for e in setup_events + exploration_events if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"]
    quant_attempts = [e for e in order_attempts if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"]
    quant_orders = [e for e in opens if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"]
    quant_failures = [e for e in order_failures if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"]
    quant_closes = [e for e in closes if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"]
    quant_daily_by_symbol = dict(Counter(_event_symbol(e) for e in quant_orders if _parse_iso(str(e.get("created_at") or "")) and (_parse_iso(str(e.get("created_at") or "")).date() == now_dt.date())))
    quant_wins = sum(1 for e in quant_closes if str(e.get("result") or "").upper() == "WIN" or (_to_float(e.get("pnl")) or 0.0) > 0)
    latest_quant = next((e for e in reversed(setup_events + exploration_events + order_attempts) if e.get("strategy") == "QUANT_STATISTICAL_PULLBACK"), None)
    quant_pro_candidates = [e for e in setup_events + exploration_events if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"]
    quant_pro_attempts = [e for e in order_attempts if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"]
    quant_pro_orders = [e for e in opens if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"]
    quant_pro_failures = [e for e in order_failures if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"]
    quant_pro_closes = [e for e in closes if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"]
    quant_pro_daily_by_symbol = dict(Counter(_event_symbol(e) for e in quant_pro_orders if _parse_iso(str(e.get("created_at") or "")) and (_parse_iso(str(e.get("created_at") or "")).date() == now_dt.date())))
    quant_pro_wins = sum(1 for e in quant_pro_closes if str(e.get("result") or "").upper() == "WIN" or (_to_float(e.get("pnl")) or 0.0) > 0)
    latest_quant_pro = next((e for e in reversed(setup_events + exploration_events + order_attempts) if e.get("strategy") == "QUANT_PRO_REGIME_SWITCHING"), None)
    quant_pro_hurst_pass_count = sum(1 for e in quant_pro_candidates if str(e.get("quant_pro_hurst_filter_status") or "").upper() == "PASS")
    quant_pro_hurst_block_count = sum(
        1
        for e in quant_pro_candidates
        if str(e.get("quant_pro_hurst_filter_status") or "").upper() == "BLOCK"
        or str(e.get("quant_pro_hurst_block_reason") or e.get("reason") or "") in {"QUANT_PRO_HURST_TREND_TOO_WEAK", "QUANT_PRO_HURST_MISSING"}
    )
    pnl_by_hurst_bucket: dict[str, float] = defaultdict(float)
    for close in quant_pro_closes:
        pnl_by_hurst_bucket[_hurst_bucket(close.get("quant_pro_hurst"))] += _to_float(close.get("pnl")) or 0.0
    gold_liquidity_events = [e for e in current_events if e.get("strategy") == "GOLD_LIQUIDITY_HUNTER_PRO" or isinstance(e.get("gold_liquidity_hunter"), dict)]
    gold_liquidity_attempts = [e for e in order_attempts if e.get("strategy") == "GOLD_LIQUIDITY_HUNTER_PRO"]
    gold_liquidity_orders = [e for e in opens if e.get("strategy") == "GOLD_LIQUIDITY_HUNTER_PRO"]
    gold_liquidity_closes = [e for e in closes if e.get("strategy") == "GOLD_LIQUIDITY_HUNTER_PRO" or _gold_signal_type(e) != "NONE"]
    gold_liquidity_wins = sum(1 for e in gold_liquidity_closes if str(e.get("result") or "").upper() == "WIN" or (_to_float(e.get("pnl")) or 0.0) > 0)
    pnl_by_gold_signal_type: dict[str, float] = defaultdict(float)
    for close in gold_liquidity_closes:
        pnl_by_gold_signal_type[_gold_signal_type(close)] += _to_float(close.get("pnl")) or 0.0
    latest_gold_liquidity = next((e for e in reversed(gold_liquidity_events) if e.get("gold_liquidity_hunter")), None)
    exploration_candidates = [
        e
        for e in exploration_events
        if e.get("strategy") == "TREND_CONTINUATION_BREAKDOWN"
        and (
            e.get("exploration_decision") == "ALLOW"
            or (_to_float(e.get("edge_score") or e.get("setup_hunter_score")) or 0.0) >= settings.demo_exploration_min_edge_score
        )
    ]
    latest_exploration_decision = next((e for e in reversed(exploration_events) if e.get("exploration_decision")), None)
    latest_order_result = next((e for e in reversed(order_attempts) if e.get("order_result") or e.get("order_retcode") is not None), None)
    latest_position_sync = position_sync_events[-1] if position_sync_events else None
    latest_position_close = next((e for e in reversed(position_sync_events) if str(e.get("result") or "").upper() == "CLOSED"), None)
    demo_trade_details = _demo_trade_details(current_events)
    open_position_sync_ticket_count = len(_open_position_sync_tickets(position_sync_events))
    current_position_counts = _current_mt5_position_counts(settings)
    report_positions = router._demo_positions()
    report_open_by_symbol = _positions_by_symbol(report_positions)
    report_open_by_symbol_strategy = _open_orders_by_symbol_strategy(valid_events, report_positions)
    edge_ready = [e for e in setup_events if _edge_ready(e)]
    near_misses = near_miss_events + [e for e in current_window_skips if _near_miss(e)]
    good_rr_blocked = next((e for e in reversed(current_window_skips) if (_to_float(e.get("rr")) or 0.0) >= 1.5), None)
    best_near_miss = max(near_misses, key=lambda e: _to_float(e.get("edge_score") or e.get("setup_hunter_score") or e.get("rr")) or 0.0, default=None)
    setup_hunter_last = next((e for e in reversed(setup_events) if e.get("event_type") == "SETUP_HUNTER"), None)
    best_candidate_now = setup_hunter_last or latest_decision
    symbol_gate_status_by_symbol = _symbol_gate_status_by_symbol(settings)
    latest_relaxed = next((e for e in reversed(current_events) if e.get("relaxed_mode_active")), None)
    last_relaxed_close = next((e for e in reversed(current_events) if e.get("relaxed_mode_active") and e.get("event_type") == "DEMO_CLOSE"), None)
    strategy_breakdown = _strategy_report_breakdown(current_events, setup_events, opens, closes)
    relaxed_trade_count_today = sum(
        1
        for e in current_events
        if e.get("relaxed_mode_active")
        and _confirmed_order_event(e)
        and (_parse_iso(str(e.get("created_at") or "")) or now_dt).date() == now_dt.date()
    )
    return {
        "filter": {
            "hours": hours,
            "since_pilot_start": since_pilot_start,
            "since_backend_start": since_backend_start,
            "since_report_reset": since_report_reset,
            "window_start": window_start.isoformat() if window_start else None,
            "current_window_events": len(current_events),
            "historical_events": len(historical_events),
        },
        "report_generated_at": report_generated_at,
        "report_now_utc": report_now_utc.isoformat(),
        "backend_started_at": backend_started_at.isoformat() if backend_started_at else None,
        "report_window_started_at": report_window_started_at.isoformat() if report_window_started_at else None,
        "future_events_excluded_count": len(future_events),
        "future_events_excluded_by_reason": future_reasons,
        "events_after_backend_start": len(_events_between(valid_events, backend_started_at, future_cutoff)) if backend_started_at else 0,
        "events_after_report_reset": len(_events_between(valid_events, report_window_started_at, future_cutoff)) if report_window_started_at else 0,
        "demo_trades_opened": len(opens),
        "demo_trades_closed": len(closes),
        "open_demo_trades": len(report_positions),
        "daily_demo_trades_total": stats["demo_trades_opened_today"],
        "daily_demo_trades_by_symbol": stats["demo_trades_opened_by_symbol"],
        "daily_demo_trades_by_symbol_strategy": stats["demo_trades_opened_by_symbol_strategy"],
        "daily_exploration_trades_total": stats["exploration_trades_opened_today"],
        "daily_exploration_trades_by_symbol": stats["exploration_trades_opened_by_symbol"],
        "daily_strong_setup_learning_trades_total": stats["strong_setup_learning_trades_opened_today"],
        "daily_strong_setup_learning_trades_by_symbol": stats["strong_setup_learning_trades_opened_by_symbol"],
        "open_demo_trades_total": len(report_positions),
        "open_demo_trades_by_symbol": report_open_by_symbol,
        "open_demo_trades_by_symbol_strategy": report_open_by_symbol_strategy,
        "hermes_trade_symbols": settings.trade_symbol_list,
        "hermes_analysis_only_symbols": settings.analysis_only_symbol_list,
        "symbol_gate_status_by_symbol": symbol_gate_status_by_symbol,
        "latest_symbol_gate_decision": latest_decision.get("symbol_gate_decision") or latest_decision.get("symbol_gate_status") if latest_decision else None,
        "latest_symbol_gate_reason": latest_decision.get("symbol_gate_reason") if latest_decision else None,
        "trade_caps": {
            "total_daily_cap": settings.demo_max_trades_per_day_total,
            "per_symbol_daily_cap": settings.demo_max_trades_per_symbol_per_day,
            "total_open_cap": settings.demo_max_open_trades_total,
            "per_symbol_open_cap": settings.demo_max_open_trades_per_symbol,
            "per_symbol_strategy_open_cap": settings.demo_max_open_trades_per_symbol_strategy,
            "daily_by_symbol": stats["demo_trades_opened_by_symbol"],
            "daily_by_symbol_strategy": stats["demo_trades_opened_by_symbol_strategy"],
            "open_by_symbol": report_open_by_symbol,
            "open_by_symbol_strategy": report_open_by_symbol_strategy,
        },
        "demo_pnl": demo_pnl,
        "mt5_today_pnl": mt5_pnl.get("mt5_today_pnl"),
        "mt5_48h_pnl": mt5_pnl.get("mt5_48h_pnl"),
        "mt5_gross_profit": mt5_pnl.get("mt5_gross_profit"),
        "mt5_gross_loss": mt5_pnl.get("mt5_gross_loss"),
        "mt5_profit_factor": mt5_pnl.get("mt5_profit_factor"),
        "mt5_history_error": mt5_pnl.get("mt5_history_error"),
        "mt5_initialized": mt5_pnl.get("mt5_initialized"),
        "mt5_last_error": mt5_pnl.get("mt5_last_error"),
        "history_start": mt5_pnl.get("history_start"),
        "history_end": mt5_pnl.get("history_end"),
        "deals_total_before_magic_filter": mt5_pnl.get("deals_total_before_magic_filter"),
        "deals_total_after_magic_filter": mt5_pnl.get("deals_total_after_magic_filter"),
        "mt5_closed_deals_count": mt5_pnl.get("mt5_closed_deals_count", 0),
        "trades_table_pnl": trades_table_pnl,
        "pnl_difference": pnl_difference,
        "pnl_source": pnl_source,
        "pnl_reconciliation_status": pnl_reconciliation_status,
        "pnl_warning": "DEMO_REPORT_PNL_MISMATCH" if pnl_reconciliation_status == "DEMO_REPORT_PNL_MISMATCH" else None,
        "win_rate": round(wins / len(closes), 4) if closes else None,
        "daily_loss_pct": stats["daily_loss_pct"],
        "consecutive_losses": stats["consecutive_losses"],
        "best_strategy": best,
        "worst_strategy": worst,
        "strategy_breakdown": strategy_breakdown,
        "trades_by_strategy": {key: value["trades"] for key, value in strategy_breakdown.items()},
        "pnl_by_strategy": {key: value["pnl"] for key, value in strategy_breakdown.items()},
        "winrate_by_strategy": {key: value["winrate"] for key, value in strategy_breakdown.items()},
        "profit_factor_by_strategy": {key: value["profit_factor"] for key, value in strategy_breakdown.items()},
        "blocked_reasons_by_strategy": {key: value["blocked_reasons"] for key, value in strategy_breakdown.items()},
        "signal_count_by_strategy": {key: value["signal_count"] for key, value in strategy_breakdown.items()},
        "order_ready_count_by_strategy": {key: value["order_ready_count"] for key, value in strategy_breakdown.items()},
        "confirmed_order_count_by_strategy": {key: value["confirmed_order_count"] for key, value in strategy_breakdown.items()},
        "skipped_count_by_reason": dict(Counter(str(e.get("reason") or "UNKNOWN") for e in skips)),
        "current_window_skip_reasons": dict(Counter(str(e.get("reason") or "UNKNOWN") for e in current_window_skips)),
        "historical_skip_reasons": dict(Counter(str(e.get("reason") or "UNKNOWN") for e in historical_skips)),
        "latest_decision": latest_decision,
        "relaxed_mode_active": bool(latest_relaxed.get("relaxed_mode_active")) if latest_relaxed else False,
        "relaxed_reason": latest_relaxed.get("relaxed_reason") if latest_relaxed else None,
        "hours_without_setup": latest_relaxed.get("hours_without_setup") if latest_relaxed else None,
        "strict_threshold": latest_relaxed.get("strict_threshold") if latest_relaxed else None,
        "relaxed_threshold": latest_relaxed.get("relaxed_threshold") if latest_relaxed else None,
        "relaxed_trade_count_today": relaxed_trade_count_today,
        "last_relaxed_trade_result": (last_relaxed_close or {}).get("result"),
        "current_router_state": {
            "enabled": router.enabled,
            "demo_trading": settings.demo_trading,
            "demo_only": settings.demo_only,
            "demo_pilot_enabled": settings.demo_pilot_enabled,
            "free_demo_discovery_mode": bool(settings.hermes_free_demo_discovery_mode),
            "demo_topdown_fallback_mode": bool(settings.hermes_demo_topdown_fallback_mode),
            "micro_discovery_mode": bool(settings.hermes_demo_micro_discovery_mode),
            "adaptive_confluence_enabled": bool(settings.hermes_adaptive_confluence_enabled),
            "active_entry_strategies": len(ENTRY_STRATEGIES),
            "max_open_total": settings.demo_max_open_trades_total,
            "max_open_per_symbol": settings.demo_max_open_trades_per_symbol,
            "max_open_per_symbol_strategy": settings.demo_max_open_trades_per_symbol_strategy,
            "daily_cap_total": settings.demo_max_trades_per_day_total,
            "hermes_trade_symbols": settings.trade_symbol_list,
            "hermes_analysis_only_symbols": settings.analysis_only_symbol_list,
            "symbol_gate_status_by_symbol": symbol_gate_status_by_symbol,
            "allow_live_trading": settings.allow_live_trading,
            "pilot_started_at": router.pilot_started_at.isoformat(),
            "pilot_window_active": router._pilot_window_active(now_dt),
            "open_demo_trades": len(report_positions),
            "open_demo_trades_total": len(report_positions),
            "open_demo_trades_by_symbol": report_open_by_symbol,
            "open_demo_trades_by_symbol_strategy": report_open_by_symbol_strategy,
            "daily_demo_trades": stats["demo_trades_opened_today"],
            "daily_demo_trades_total": stats["demo_trades_opened_today"],
            "daily_demo_trades_by_symbol": stats["demo_trades_opened_by_symbol"],
            "daily_demo_trades_by_symbol_strategy": stats["demo_trades_opened_by_symbol_strategy"],
            "daily_exploration_trades": stats["exploration_trades_opened_today"],
            "daily_exploration_trades_total": stats["exploration_trades_opened_today"],
            "daily_exploration_trades_by_symbol": stats["exploration_trades_opened_by_symbol"],
            "strong_setup_learning_daily_count": stats["strong_setup_learning_trades_opened_today"],
            "daily_strong_setup_learning_trades_total": stats["strong_setup_learning_trades_opened_today"],
            "daily_strong_setup_learning_trades_by_symbol": stats["strong_setup_learning_trades_opened_by_symbol"],
            "smoke_test_confirmed_orders": stats["smoke_test_confirmed_orders"],
            "daily_loss_pct": stats["daily_loss_pct"],
            "consecutive_losses": stats["consecutive_losses"],
        },
        "exploration_mode_enabled": bool(settings.demo_exploration_mode),
        "demo_strong_setup_learning_mode_enabled": bool(settings.demo_strong_setup_learning_mode),
        "demo_smoke_test_24h_enabled": bool(settings.demo_smoke_test_24h),
        "demo_test_ignore_bad_hours_enabled": bool(settings.demo_test_ignore_bad_hours),
        "free_demo_discovery_mode": bool(settings.hermes_free_demo_discovery_mode),
        "demo_topdown_fallback_mode": bool(settings.hermes_demo_topdown_fallback_mode),
        "micro_discovery_mode": bool(settings.hermes_demo_micro_discovery_mode),
        "adaptive_confluence_enabled": bool(settings.hermes_adaptive_confluence_enabled),
        "final_confluence_score": latest_decision.get("final_confluence_score") if latest_decision else None,
        "symbol_min_confluence": latest_decision.get("symbol_min_confluence") if latest_decision else None,
        "confluence_threshold_pass": latest_decision.get("confluence_threshold_pass") if latest_decision else None,
        "confluence_threshold_reason": latest_decision.get("confluence_threshold_reason") if latest_decision else None,
        "confluence_components": latest_decision.get("confluence_components") if latest_decision else None,
        "confluence_mode": latest_decision.get("confluence_mode") if latest_decision else "ADAPTIVE",
        "active_entry_strategies": len(ENTRY_STRATEGIES),
        "max_open_total": settings.demo_max_open_trades_total,
        "max_open_per_symbol": settings.demo_max_open_trades_per_symbol,
        "max_open_per_symbol_strategy": settings.demo_max_open_trades_per_symbol_strategy,
        "daily_cap_total": settings.demo_max_trades_per_day_total,
        "quant_strategy_enabled": bool(settings.hermes_quant_strategy_enabled),
        "quant_pro_enabled": bool(settings.hermes_quant_pro_enabled),
        "demo_smoke_test_started_at": smoke_started_at.isoformat(),
        "demo_smoke_test_expires_at": smoke_expires_at.isoformat(),
        "demo_smoke_test_confirmed_orders": len(smoke_confirmed_orders),
        "exploration_trades_opened": len(exploration_orders),
        "exploration_order_attempts": len(exploration_attempts),
        "exploration_orders_opened_confirmed": len(exploration_orders),
        "exploration_orders_failed": len(exploration_failures),
        "strong_setup_learning_orders_attempted": len(strong_setup_learning_attempts),
        "strong_setup_learning_orders_confirmed": len(strong_setup_learning_orders),
        "strong_setup_learning_orders_failed": len(strong_setup_learning_failures),
        "strong_setup_learning_daily_count": stats["strong_setup_learning_trades_opened_today"],
        "fallback_orders_attempted": len(fallback_attempts),
        "fallback_orders_confirmed": len(fallback_orders),
        "fallback_warnings": dict(Counter(warning for e in fallback_events for warning in e.get("fallback_warnings", []))),
        "fallback_blocks_by_reason": dict(Counter(str(e.get("fallback_block_reason") or "UNKNOWN") for e in fallback_events if e.get("fallback_decision") == "BLOCK")),
        "micro_discovery_attempts": len(micro_attempts),
        "micro_discovery_confirmed": len(micro_orders),
        "micro_discovery_blocks_by_reason": dict(Counter(str(e.get("micro_discovery_block_reason") or "UNKNOWN") for e in micro_events if e.get("micro_discovery_decision") == "BLOCK")),
        "micro_discovery_warnings": dict(Counter(warning for e in micro_events for warning in e.get("micro_discovery_warnings", []))),
        "quant_candidates": quant_candidates[:10],
        "quant_orders_attempted": len(quant_attempts),
        "quant_orders_confirmed": len(quant_orders),
        "quant_orders_failed": len(quant_failures),
        "quant_daily_by_symbol": quant_daily_by_symbol,
        "quant_pnl": round(sum((_to_float(e.get("pnl")) or 0.0) for e in quant_closes), 6),
        "quant_win_rate": round(quant_wins / len(quant_closes), 4) if quant_closes else None,
        "latest_quant_signal": latest_quant.get("quant_signal") if latest_quant else None,
        "latest_quant_score": latest_quant.get("quant_score") if latest_quant else None,
        "latest_quant_r2": latest_quant.get("quant_r2") if latest_quant else None,
        "latest_quant_z_score": latest_quant.get("quant_z_score") if latest_quant else None,
        "quant_pro_candidates": quant_pro_candidates[:10],
        "quant_pro_orders_attempted": len(quant_pro_attempts),
        "quant_pro_orders_confirmed": len(quant_pro_orders),
        "quant_pro_orders_failed": len(quant_pro_failures),
        "quant_pro_daily_by_symbol": quant_pro_daily_by_symbol,
        "quant_pro_pnl": round(sum((_to_float(e.get("pnl")) or 0.0) for e in quant_pro_closes), 6),
        "quant_pro_win_rate": round(quant_pro_wins / len(quant_pro_closes), 4) if quant_pro_closes else None,
        "latest_quant_pro_signal": latest_quant_pro.get("quant_pro_signal") if latest_quant_pro else None,
        "latest_quant_pro_score": latest_quant_pro.get("quant_pro_score") if latest_quant_pro else None,
        "latest_quant_pro_regime": latest_quant_pro.get("quant_pro_regime") if latest_quant_pro else None,
        "latest_quant_pro_ols_tstat": latest_quant_pro.get("quant_pro_ols_tstat") if latest_quant_pro else None,
        "latest_quant_pro_kalman_z": latest_quant_pro.get("quant_pro_kalman_z") if latest_quant_pro else None,
        "latest_quant_pro_ou_half_life": latest_quant_pro.get("quant_pro_ou_half_life") if latest_quant_pro else None,
        "latest_quant_pro_hurst": latest_quant_pro.get("quant_pro_hurst") if latest_quant_pro else None,
        "latest_quant_pro_min_trend_hurst": latest_quant_pro.get("quant_pro_min_trend_hurst") if latest_quant_pro else getattr(settings, "quant_pro_min_trend_hurst", 0.90),
        "latest_quant_pro_trend_strength": latest_quant_pro.get("quant_pro_trend_strength") if latest_quant_pro else None,
        "latest_quant_pro_hurst_filter_status": latest_quant_pro.get("quant_pro_hurst_filter_status") if latest_quant_pro else None,
        "latest_quant_pro_hurst_block_reason": latest_quant_pro.get("quant_pro_hurst_block_reason") if latest_quant_pro else None,
        "quant_pro_hurst_pass_count": quant_pro_hurst_pass_count,
        "quant_pro_hurst_block_count": quant_pro_hurst_block_count,
        "pnl_by_hurst_bucket": {key: round(value, 6) for key, value in pnl_by_hurst_bucket.items()},
        "gold_liquidity_attempts": len(gold_liquidity_attempts),
        "gold_liquidity_confirmed": len(gold_liquidity_orders),
        "gold_liquidity_blocks_by_reason": dict(Counter(str(e.get("reason") or e.get("failed_gate") or "UNKNOWN") for e in gold_liquidity_events if e.get("decision") == "BLOCK" or e.get("event_type") == "DEMO_SKIP")),
        "gold_liquidity_pnl": round(sum((_to_float(e.get("pnl")) or 0.0) for e in gold_liquidity_closes), 6),
        "gold_liquidity_win_rate": round(gold_liquidity_wins / len(gold_liquidity_closes), 4) if gold_liquidity_closes else None,
        "gold_signal_counts_by_type": dict(Counter(_gold_signal_type(e) for e in gold_liquidity_events if _gold_signal_type(e) != "NONE")),
        "pnl_by_gold_signal_type": {key: round(value, 6) for key, value in pnl_by_gold_signal_type.items()},
        "gold_liquidity_hunter": latest_gold_liquidity.get("gold_liquidity_hunter") if latest_gold_liquidity else {},
        "gold_liquidity_labels": [
            "GOLD TRADES ONLY ON BSL/SSL SWEEP + ABS/REJ",
            "EXH/DIV OBSERVER ONLY",
            "GENERIC GOLD STRATEGIES DISABLED",
        ],
        "failed_order_retcode_counts": dict(Counter(str(e.get("order_retcode") if e.get("order_retcode") is not None else "NONE") for e in order_failures)),
        "latest_order_result": _latest_order_result_payload(latest_order_result),
        "current_mt5_open_positions_count": current_position_counts["mt5_open_positions_count"],
        "current_hermes_mt5_open_positions_count": current_position_counts["hermes_mt5_open_positions_count"],
        "mt5_open_positions_count": current_position_counts["mt5_open_positions_count"],
        "hermes_mt5_open_positions_count": current_position_counts["hermes_mt5_open_positions_count"],
        _sb_key("open_trades_synced_count"): open_position_sync_ticket_count,
        "latest_position_sync_event": latest_position_sync,
        "latest_position_close_event": latest_position_close,
        "latest_position_sync": latest_position_sync,
        "latest_position_close": latest_position_close,
        "demo_trade_details": demo_trade_details,
        "exploration_candidates": exploration_candidates[:10],
        "exploration_warnings": dict(Counter(warning for e in exploration_events for warning in e.get("exploration_warnings", []))),
        "latest_exploration_decision": latest_exploration_decision,
        "strict_block_reason": latest_exploration_decision.get("strict_block_reason") if latest_exploration_decision else None,
        "exploration_override_reason": latest_exploration_decision.get("exploration_override_reason") if latest_exploration_decision else None,
        "exploration_ignored_block_reasons": latest_exploration_decision.get("exploration_ignored_block_reasons") if latest_exploration_decision else [],
        "edge_ready_candidates": edge_ready[:10],
        "near_miss_candidates": near_misses[:10],
        "setup_hunter_events": len(setup_events),
        "near_miss_events": len(near_miss_events),
        "best_near_miss": best_near_miss,
        "best_candidate_now": best_candidate_now,
        "current_session_quality": "EDGE_READY" if edge_ready else ("NEAR_MISS" if near_misses else "NO_EDGE"),
        "top_missing_confirmation": dict(Counter(missing for e in near_misses for missing in _missing_confirmation(e)).most_common(10)),
        "setup_hunter_last_decision": setup_hunter_last,
        "near_miss_reasons": dict(Counter(str(e.get("near_miss_reason") or e.get("reason") or "UNKNOWN") for e in near_misses)),
        "latest_good_rr_but_blocked_reason": good_rr_blocked.get("reason") if good_rr_blocked else None,
        "blocked_by_session": dict(Counter(_event_session(e) for e in current_window_skips)),
        "blocked_by_symbol": dict(Counter(str(e.get("symbol") or e.get("broker_symbol") or "UNKNOWN") for e in current_window_skips)),
        "blocked_by_strategy": dict(Counter(str(e.get("strategy") or "UNKNOWN") for e in current_window_skips)),
        "top_failed_gates_current_window": dict(Counter(str(e.get("failed_gate") or e.get("reason") or "UNKNOWN") for e in current_window_skips).most_common(10)),
        "last_kelly_decision": last_kelly,
        "live_orders_detected": False,
    }


def _demo_report_window_start(
    settings: Settings,
    router: DemoKellyRouter,
    hours: int | None,
    since_pilot_start: bool,
    since_backend_start: bool,
    since_report_reset: bool,
    backend_started_at: datetime | None,
    report_window_started_at: datetime | None,
    now: datetime,
) -> datetime | None:
    if since_report_reset:
        return report_window_started_at
    if since_backend_start:
        return backend_started_at
    if since_pilot_start:
        return router.pilot_started_at
    if hours is not None:
        return now - timedelta(hours=max(0, int(hours)))
    return None


def _strategy_report_breakdown(current_events: list[dict], setup_events: list[dict], opens: list[dict], closes: list[dict]) -> dict[str, dict]:
    strategies = sorted(
        {
            str(event.get("strategy") or "UNKNOWN")
            for event in current_events + setup_events + opens + closes
            if event.get("strategy")
        }
    )
    out: dict[str, dict] = {}
    for strategy in strategies:
        signals = [
            event
            for event in setup_events
            if str(event.get("strategy") or "") == strategy
            and str(event.get("direction") or event.get("signal") or "").upper() in {"BUY", "SELL"}
        ]
        order_ready = [event for event in signals if event.get("demo_eligible") is True or event.get("execution_candidate") is True]
        confirmed = [event for event in opens if str(event.get("strategy") or "") == strategy]
        blocked = [
            event
            for event in current_events
            if str(event.get("strategy") or "") == strategy
            and (event.get("event_type") == "DEMO_SKIP" or str(event.get("decision") or "").upper() == "BLOCK")
        ]
        strategy_closes = [event for event in closes if str(event.get("strategy") or "") == strategy]
        pnls = [_to_float(event.get("pnl")) or 0.0 for event in strategy_closes]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = round(gross_profit / gross_loss, 6) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
        out[strategy] = {
            "trades": len(confirmed),
            "pnl": round(sum(pnls), 6),
            "winrate": round(len(wins) / len(strategy_closes), 4) if strategy_closes else None,
            "profit_factor": profit_factor,
            "blocked_reasons": dict(Counter(str(event.get("reason") or event.get("failed_gate") or "UNKNOWN") for event in blocked)),
            "signal_count": len(signals),
            "order_ready_count": len(order_ready),
            "confirmed_order_count": len(confirmed),
        }
    return out


def _events_between(events: list[dict], start: datetime | None, end: datetime) -> list[dict]:
    out = []
    for event in events:
        created = _parse_iso(str(event.get("created_at") or ""))
        if created is None:
            continue
        if start is not None and created < start:
            continue
        if created <= end:
            out.append(event)
    return out


def _exclude_future_events(events: list[dict], cutoff: datetime) -> tuple[list[dict], list[dict]]:
    valid = []
    future = []
    for event in events:
        created = _parse_iso(str(event.get("created_at") or ""))
        if created is not None and created > cutoff:
            future.append(event)
        else:
            valid.append(event)
    return valid, future


def _edge_ready(event: dict) -> bool:
    gates = event.get("gate_statuses") if isinstance(event.get("gate_statuses"), dict) else {}
    strategy = str(event.get("strategy") or "").upper()
    direction = str(event.get("direction") or event.get("signal") or "").upper()
    time_gate_status = event.get("time_gate_status") or gates.get("time_gate_status")
    session = str(event.get("time_session") or _event_session(event) or "").upper()
    smc_status = str(event.get("smc_status") or gates.get("smc_status") or "").upper()
    smc_score = _to_float(event.get("smc_score") or gates.get("smc_score")) or 0.0
    mtfa_status = str(event.get("mtfa_status") or gates.get("mtfa_status") or "").upper()
    mtfa_score = _to_float(event.get("mtfa_score") or gates.get("mtfa_score")) or 0.0
    m15 = event.get("m15_confirmation") if "m15_confirmation" in event else gates.get("m15_confirmation")
    m1 = event.get("m1_confirmation") if "m1_confirmation" in event else gates.get("m1_entry_confirmation")
    m15_status = str(event.get("m15_confirmation_status") or "").upper()
    m1_status = str(event.get("m1_trigger_status") or "").upper()
    rr = _to_float(event.get("rr")) or 0.0
    final_lot = _to_float(event.get("final_capped_lot") or gates.get("final_lot"))
    grade = str(event.get("grade") or "").upper()
    return (
        strategy in ENTRY_STRATEGIES
        and direction in {"BUY", "SELL"}
        and rr >= 1.5
        and time_gate_status == "PASS"
        and session != "OFF_HOURS"
        and (smc_status != "FAIL" or smc_score >= 70)
        and (mtfa_status != "FAIL" or mtfa_score >= 60)
        and (m15 is True or m15_status == "PASS")
        and (m1 is True or m1_status == "PASS")
        and grade in {"A", "B", "A+", "A_PLUS"}
        and (final_lot is None or final_lot <= 0.01)
        and event.get("sl") is not None
        and event.get("tp") is not None
    )


def _near_miss(event: dict) -> bool:
    reason = str(event.get("reason") or "")
    if not _edge_ready(event):
        return False
    return reason not in {"ACCOUNT_NOT_DEMO", "SYMBOL_NOT_ALLOWED", "SYMBOL_NOT_RESOLVED", "ALLOW_LIVE_TRADING_NOT_FALSE"}


def _event_session(event: dict) -> str:
    time_gate = event.get("time_gate") if isinstance(event.get("time_gate"), dict) else {}
    return str(time_gate.get("session_name") or "UNKNOWN")


def _event_symbol(event: dict) -> str:
    raw = event.get("broker_symbol") or event.get("symbol")
    payload = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    if not raw:
        raw = payload.get("broker_symbol") or payload.get("symbol")
    return str(raw or "").upper()


def _gold_signal_type(event: dict) -> str:
    for source in (event, event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}):
        hunter = source.get("gold_liquidity_hunter") if isinstance(source, dict) else None
        if isinstance(hunter, dict):
            return str(hunter.get("reversal_signal") or "NONE").upper()
        signal = source.get("gold_liquidity_signal") if isinstance(source, dict) else None
        if signal:
            return str(signal).upper()
    return "NONE"


def _symbol_gate_status_by_symbol(settings: Settings) -> dict[str, dict]:
    trade_symbols = {_canonical_trade_symbol(item) for item in settings.trade_symbol_list}
    analysis_only_symbols = {_canonical_trade_symbol(item) for item in settings.analysis_only_symbol_list}
    symbols = list(dict.fromkeys(settings.symbol_list + settings.trade_symbol_list + settings.analysis_only_symbol_list))
    out: dict[str, dict] = {}
    for symbol in symbols:
        canonical = _canonical_trade_symbol(symbol)
        if canonical in analysis_only_symbols:
            decision = "BLOCK"
            reason = "SYMBOL_ANALYSIS_ONLY"
        elif canonical in trade_symbols:
            decision = "PASS"
            reason = "SYMBOL_ALLOWED_FOR_DEMO"
        else:
            decision = "BLOCK"
            reason = "SYMBOL_NOT_ALLOWED"
        out[str(symbol or "").upper()] = {
            "decision": decision,
            "reason": reason,
            "canonical_symbol": canonical,
        }
    return out


def _positions_by_symbol(positions: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for position in positions:
        payload = _position_payload(position)
        symbol = str(payload.get("symbol") or "").upper()
        if symbol:
            counts[symbol] += 1
    return dict(counts)


def _valid_sl_tp(direction: str, entry: float | None, sl: float | None, tp: float | None) -> bool:
    if entry is None or sl is None or tp is None:
        return False
    if direction == "BUY":
        return sl < entry < tp
    if direction == "SELL":
        return tp < entry < sl
    return False


def _detect_filling_mode(symbol: str) -> int:
    """Return the best supported ORDER_FILLING_* constant for the given symbol.

    MT5 symbol_info.filling_mode is a bitmask:
      bit 0 (value 1) = FOK allowed  → ORDER_FILLING_FOK = 0
      bit 1 (value 2) = IOC allowed  → ORDER_FILLING_IOC = 1
    When neither bit is set the broker requires RETURN mode   → ORDER_FILLING_RETURN = 2
    """
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            _sel = getattr(mt5, "symbol_select", None)
            if callable(_sel):
                _sel(symbol, True)
            info = mt5.symbol_info(symbol)
    except Exception:
        info = None
    filling_mask = int(getattr(info, "filling_mode", 0) or 0) if info is not None else 0
    if filling_mask & 1:
        return getattr(mt5, "ORDER_FILLING_FOK", 0)
    if filling_mask & 2:
        return getattr(mt5, "ORDER_FILLING_IOC", 1)
    return getattr(mt5, "ORDER_FILLING_RETURN", 2)


def validate_mt5_stops(
    symbol: object,
    direction: object,
    sl: object,
    tp: object,
    lot: object,
    *,
    fallback_bid: object = None,
    fallback_ask: object = None,
    fallback_specs: dict | None = None,
) -> dict:
    broker_symbol = str(symbol or "")
    side = str(direction or "").upper()
    payload = {
        "decision": "BLOCK",
        "reason": None,
        "symbol": broker_symbol,
        "direction": side,
        "bid": None,
        "ask": None,
        "sl": _to_float(sl),
        "tp": _to_float(tp),
        "lot": _to_float(lot),
        "digits": None,
        "point": None,
        "stops_level": None,
        "freeze_level": None,
        "min_distance": None,
        "normalized_sl": None,
        "normalized_tp": None,
    }
    if side not in {"BUY", "SELL"} or payload["sl"] is None or payload["tp"] is None or payload["lot"] is None or payload["lot"] <= 0:
        payload["reason"] = "INVALID_STOPS_PRECHECK"
        _log_order_precheck(payload)
        return payload
    try:
        select = getattr(mt5, "symbol_select", None)
        if callable(select):
            select(broker_symbol, True)
    except Exception:
        pass
    try:
        info = mt5.symbol_info(broker_symbol)
    except Exception:
        info = None
    try:
        tick = mt5.symbol_info_tick(broker_symbol)
    except Exception:
        tick = None
    specs = fallback_specs or {}
    digits = getattr(info, "digits", None) if info is not None else specs.get("digits")
    point = _to_float(getattr(info, "point", None) if info is not None else specs.get("point")) or _to_float(specs.get("tick_size")) or 0.0
    stops_level = _to_float(getattr(info, "trade_stops_level", None) if info is not None else specs.get("trade_stops_level")) or 0.0
    freeze_level = _to_float(getattr(info, "trade_freeze_level", None) if info is not None else specs.get("trade_freeze_level")) or 0.0
    bid = _to_float(getattr(tick, "bid", None) if tick is not None else fallback_bid)
    ask = _to_float(getattr(tick, "ask", None) if tick is not None else fallback_ask)
    if digits is None:
        digits = _infer_digits(point)
    try:
        digits_int = max(0, int(digits or 0))
    except (TypeError, ValueError):
        digits_int = 0
    normalized_sl = round(float(payload["sl"]), digits_int)
    normalized_tp = round(float(payload["tp"]), digits_int)
    min_points = max(stops_level, freeze_level, 1.0)
    min_distance = min_points * point
    payload.update(
        {
            "bid": bid,
            "ask": ask,
            "digits": digits_int,
            "point": point,
            "stops_level": stops_level,
            "freeze_level": freeze_level,
            "min_distance": min_distance,
            "normalized_sl": normalized_sl,
            "normalized_tp": normalized_tp,
        }
    )
    entry_ref = ask if side == "BUY" else bid
    if entry_ref is None or point <= 0:
        payload["reason"] = "MT5_STOP_PRECHECK_SYMBOL_SPEC_INVALID"
        _log_order_precheck(payload)
        return payload
    valid = (
        normalized_sl < entry_ref - min_distance and normalized_tp > entry_ref + min_distance
        if side == "BUY"
        else normalized_sl > entry_ref + min_distance and normalized_tp < entry_ref - min_distance
    )
    if not valid:
        payload["reason"] = "INVALID_STOPS_PRECHECK"
        _log_order_precheck(payload)
        return payload
    payload["decision"] = "PASS"
    payload["reason"] = "STOPS_VALID"
    _log_order_precheck(payload)
    return payload


def _log_order_precheck(payload: dict) -> None:
    if payload.get("decision") == "PASS":
        log.info(
            "[ORDER_PRECHECK] symbol=%s decision=PASS direction=%s bid=%s ask=%s sl=%s tp=%s min_distance=%s stops_level=%s freeze_level=%s",
            payload.get("symbol"),
            payload.get("direction"),
            payload.get("bid"),
            payload.get("ask"),
            payload.get("normalized_sl"),
            payload.get("normalized_tp"),
            payload.get("min_distance"),
            payload.get("stops_level"),
            payload.get("freeze_level"),
        )
        return
    log.info(
        "[ORDER_PRECHECK] symbol=%s decision=BLOCK reason=%s direction=%s bid=%s ask=%s sl=%s tp=%s min_distance=%s stops_level=%s freeze_level=%s",
        payload.get("symbol"),
        payload.get("reason"),
        payload.get("direction"),
        payload.get("bid"),
        payload.get("ask"),
        payload.get("normalized_sl") if payload.get("normalized_sl") is not None else payload.get("sl"),
        payload.get("normalized_tp") if payload.get("normalized_tp") is not None else payload.get("tp"),
        payload.get("min_distance"),
        payload.get("stops_level"),
        payload.get("freeze_level"),
    )


def _safe_order_check(request: dict) -> dict | None:
    checker = getattr(mt5, "order_check", None)
    if not callable(checker):
        return None
    try:
        result = checker(request)
    except Exception as exc:
        log.info("[ORDER_CHECK] decision=SKIP reason=ORDER_CHECK_EXCEPTION error=%s", exc)
        return None
    if result is None:
        return None
    payload = _order_result_payload(result)
    retcode = payload.get("retcode")
    if _order_check_success(payload):
        log.info("[ORDER_CHECK] decision=PASS retcode=%s comment=%s", retcode, payload.get("comment"))
        return {"decision": "PASS", "retcode": retcode, "comment": payload.get("comment"), "result": payload}
    log.info("[ORDER_CHECK] decision=BLOCK retcode=%s comment=%s", retcode, payload.get("comment"))
    return {"decision": "BLOCK", "reason": "ORDER_CHECK_FAILED", "retcode": retcode, "comment": payload.get("comment"), "result": payload}


def _order_check_success(payload: dict) -> bool:
    retcode = _retcode_int(payload.get("retcode"))
    comment = str(payload.get("comment") or "").lower()
    if retcode in _success_retcode_values():
        return True
    if retcode == 0 and any(token in comment for token in ("done", "success", "passed", "ok")):
        return True
    return False


def _infer_digits(point: float | None) -> int:
    if point is None or point <= 0:
        return 0
    text = ("%f" % point).rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _open_orders_by_symbol_strategy(events: list[dict], positions: list[Any] | None = None) -> dict[str, int]:
    by_ticket: dict[str, tuple[str, str]] = {}
    for event in events:
        ticket = str(event.get("ticket") or "")
        if not ticket or ticket.lower() in {"none", "null", "0"}:
            continue
        event_type = str(event.get("event_type") or "").upper()
        status = str(event.get("status") or event.get("result") or "").upper()
        if _confirmed_order_event(event):
            symbol = _event_symbol(event)
            strategy = str(event.get("strategy") or "UNKNOWN").upper()
            if symbol and strategy:
                by_ticket[ticket] = (symbol, strategy)
        elif event_type in {"DEMO_CLOSE", "POSITION_SYNC"} and status == "CLOSED":
            by_ticket.pop(ticket, None)
    if positions is not None:
        if not positions:
            stale_keys = sorted({_symbol_strategy_key(symbol, strategy) for symbol, strategy in by_ticket.values()})
            for key in stale_keys:
                log.info(
                    "[OPEN_COUNTER_RECONCILE] mt5_open=0 hermes_open=0 stale_strategy_counter_cleared=%s",
                    key,
                )
            return {}
        open_tickets = {
            ticket
            for position in positions
            if (ticket := _position_ticket(position))
        }
        if open_tickets:
            stale_keys = sorted(
                {_symbol_strategy_key(symbol, strategy) for ticket, (symbol, strategy) in by_ticket.items() if ticket not in open_tickets}
            )
            for key in stale_keys:
                log.info("[OPEN_COUNTER_RECONCILE] stale_strategy_counter_cleared=%s", key)
            by_ticket = {ticket: value for ticket, value in by_ticket.items() if ticket in open_tickets}
    counts: Counter[str] = Counter()
    for symbol, strategy in by_ticket.values():
        counts[_symbol_strategy_key(symbol, strategy)] += 1
    return dict(counts)


def _position_ticket(position: object) -> str | None:
    payload = _position_payload(position)
    for key in ("ticket", "position_ticket", "identifier"):
        value = payload.get(key)
        if value is None:
            continue
        ticket = str(value)
        if ticket and ticket.lower() not in {"none", "null", "0"}:
            return ticket
    return None


def _symbol_strategy_key(symbol: str, strategy: str) -> str:
    return f"{str(symbol or '').upper()}|{str(strategy or 'UNKNOWN').upper()}"


def _missing_confirmation(event: dict) -> list[str]:
    gates = event.get("gate_statuses") if isinstance(event.get("gate_statuses"), dict) else {}
    if isinstance(event.get("missing"), list):
        return [str(item) for item in event.get("missing") or []]
    missing = []
    if gates.get("m15_confirmation") is not True:
        missing.append("M15_CONFIRMATION")
    if gates.get("m1_entry_confirmation") is not True:
        missing.append("M1_CONFIRMATION")
    if str(gates.get("smc_status") or "").upper() != "PASS":
        missing.append("SMC_PASS")
    if str(gates.get("mtfa_status") or "").upper() != "PASS":
        missing.append("MTFA_ALIGNMENT")
    return missing


def _is_btc_scalping_gates(gates: dict) -> bool:
    return str(gates.get("strategy") or "").upper() == "BTC_SCALPING_AGENT"


def _is_gold_order_flow_gates(gates: dict) -> bool:
    return str(gates.get("strategy") or "").upper() == "GOLD_ORDER_FLOW_CVD_VWAP"


def _is_order_flow_exec_gates(gates: dict) -> bool:
    return str(gates.get("strategy") or "").upper() == "ORDER_FLOW_EXECUTION_AGENT"


def _gold_order_flow_router_payload(decision: dict, router_decision: str, demo_gate_reason: str) -> dict | None:
    if str(decision.get("strategy") or "").upper() != "GOLD_ORDER_FLOW_CVD_VWAP":
        return None
    raw = decision.get("raw_payload") if isinstance(decision.get("raw_payload"), dict) else {}
    payload = decision.get("gold_order_flow_cvd_vwap") if isinstance(decision.get("gold_order_flow_cvd_vwap"), dict) else {}
    source = {**payload, **raw}
    return {
        "strategy_id": "GOLD_ORDER_FLOW_CVD_VWAP",
        "symbol": decision.get("symbol") or source.get("symbol"),
        "timeframe": source.get("timeframe") or "M5",
        "status": source.get("status") or ("ORDER_READY" if str(decision.get("signal") or "").upper() in {"BUY", "SELL"} else "WAIT"),
        "side": source.get("side") or (str(decision.get("signal") or "").upper() if str(decision.get("signal") or "").upper() in {"BUY", "SELL"} else None),
        "confidence": source.get("confidence") if source.get("confidence") is not None else decision.get("confidence"),
        "entry": decision.get("entry") if decision.get("entry") is not None else source.get("entry"),
        "sl": decision.get("sl") if decision.get("sl") is not None else source.get("sl"),
        "tp": decision.get("tp") if decision.get("tp") is not None else source.get("tp"),
        "poc": source.get("poc"),
        "vah": source.get("vah"),
        "val": source.get("val"),
        "vwap": source.get("vwap"),
        "cvd_slope": source.get("cvd_slope"),
        "cvd_proxy": source.get("cvd_proxy") if source.get("cvd_proxy") is not None else source.get("cvd"),
        "delta_proxy": source.get("delta_proxy") if source.get("delta_proxy") is not None else source.get("latest_delta") if source.get("latest_delta") is not None else source.get("delta"),
        "latest_delta": source.get("latest_delta") if source.get("latest_delta") is not None else source.get("delta"),
        "divergence": source.get("divergence"),
        "block_reason": source.get("block_reason"),
        "router_decision": router_decision,
        "demo_gate_reason": demo_gate_reason,
        "mt5_order_flow_warning": source.get("mt5_order_flow_warning") or "MT5_TICK_DELTA_PROXY_NOT_REAL_ORDER_BOOK",
        "gold_order_flow_cvd_vwap": payload,
    }


def _gold_order_flow_missing_required_fields(payload: dict, gates: dict, settings: Settings) -> list[str]:
    required = {
        "price": payload.get("price") or payload.get("entry") or gates.get("entry"),
        "vwap": payload.get("vwap"),
        "poc": payload.get("poc"),
        "vah": payload.get("vah"),
        "val": payload.get("val"),
        "cvd_proxy": payload.get("cvd_proxy") if payload.get("cvd_proxy") is not None else payload.get("cvd"),
        "delta_proxy": payload.get("delta_proxy") if payload.get("delta_proxy") is not None else payload.get("latest_delta"),
        "entry": payload.get("entry") or gates.get("entry"),
        "sl": payload.get("sl") or gates.get("sl"),
        "tp": payload.get("tp") or gates.get("tp"),
        "rr": payload.get("rr") or gates.get("rr"),
    }
    missing = [key for key, value in required.items() if _to_float(value) is None]
    return list(dict.fromkeys(missing))


def _missing_entry_sl_tp_field(decision: dict) -> str | None:
    for field in ("entry", "sl", "tp"):
        if decision.get(field) is None:
            return field
        if _to_float(decision.get(field)) is None:
            return field
    return None


def _routeable_entry_decision(decision: dict) -> bool:
    strategy = str(decision.get("strategy") or "").upper()
    direction = str(decision.get("signal") or decision.get("direction") or "").upper()
    return strategy in ENTRY_STRATEGIES and direction in {"BUY", "SELL"}


def mark_backend_started(now: datetime | None = None, path: Path = BACKEND_START_PATH) -> datetime:
    dt = now or datetime.now(timezone.utc)
    _write_marker(path, "backend_started_at", dt)
    return dt


def reset_report_window(now: datetime | None = None, path: Path = REPORT_WINDOW_PATH) -> datetime:
    dt = now or datetime.now(timezone.utc)
    _write_marker(path, "report_window_started_at", dt)
    return dt


def read_backend_started_at(path: Path = BACKEND_START_PATH) -> datetime | None:
    return _read_marker(path, "backend_started_at")


def read_report_window_started_at(path: Path = REPORT_WINDOW_PATH) -> datetime | None:
    return _read_marker(path, "report_window_started_at")


def _write_marker(path: Path, key: str, value: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: value.isoformat()}, indent=2), encoding="utf-8")


def _read_marker(path: Path, key: str) -> datetime | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _parse_iso(str(payload.get(key) or ""))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _quant_pro_hurst_block_reason(decision: dict) -> str | None:
    if str(decision.get("strategy") or "").upper() != "QUANT_PRO_REGIME_SWITCHING":
        return None
    payload = decision.get("quant_pro_hurst_filter") if isinstance(decision.get("quant_pro_hurst_filter"), dict) else {}
    reason = str(payload.get("block_reason") or decision.get("quant_pro_hurst_block_reason") or "").upper()
    return reason if reason in {"QUANT_PRO_HURST_TREND_TOO_WEAK", "QUANT_PRO_HURST_MISSING"} else None


def _hurst_bucket(value: object) -> str:
    hurst = _to_float(value)
    if hurst is None:
        return "UNKNOWN"
    if hurst < 0.50:
        return "hurst < 0.50"
    if hurst < 0.70:
        return "0.50 <= hurst < 0.70"
    if hurst < 0.90:
        return "0.70 <= hurst < 0.90"
    return "hurst >= 0.90"


def _status_pass(status: object, flag: object) -> bool:
    return str(status or "").upper() == "PASS" or flag is True


def _success_retcode_values() -> set[int]:
    values = set()
    for name, default in (("TRADE_RETCODE_DONE", 10009), ("TRADE_RETCODE_PLACED", 10008)):
        value = getattr(mt5, name, default)
        try:
            values.add(int(value))
        except (TypeError, ValueError):
            values.add(default)
    return values


def _order_result_confirmed(result: object, ticket: object) -> bool:
    if result is None:
        return False
    retcode = _retcode_int(getattr(result, "retcode", None))
    return retcode in _success_retcode_values() and _ticket_present(ticket)


def _retcode_done(result: object) -> bool:
    if result is None:
        return False
    return _retcode_int(getattr(result, "retcode", None)) in _success_retcode_values()


def _order_result_payload(result: object) -> dict:
    if result is None:
        return {"retcode": None, "order": None, "deal": None, "ticket": None, "comment": None, "raw": None}
    order = getattr(result, "order", None)
    deal = getattr(result, "deal", None)
    fallback_ticket = getattr(result, "ticket", None)
    ticket = order or deal or fallback_ticket
    return {
        "retcode": _retcode_int(getattr(result, "retcode", None)),
        "order": order,
        "deal": deal,
        "ticket": ticket,
        "comment": getattr(result, "comment", None),
        "request_id": getattr(result, "request_id", None),
    }


def _order_failure_reason(result: object, ticket: object) -> str:
    if result is None:
        return "ORDER_RESULT_NONE"
    retcode = _retcode_int(getattr(result, "retcode", None))
    if retcode not in _success_retcode_values():
        return "ORDER_RETCODE_NOT_SUCCESS"
    if not _ticket_present(ticket):
        return "ORDER_TICKET_MISSING"
    return "ORDER_RESULT_UNCONFIRMED"


def _quick_exit_event(event_type: str, status: str, payload: dict, now: datetime | None = None) -> dict:
    created = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "event_type": event_type,
        "status": status,
        "mode": "DEMO_QUICK_EXIT",
        "strategy": "HERMES_QUICK_EXIT_MANAGER",
        "symbol": payload.get("symbol"),
        "broker_symbol": payload.get("symbol"),
        "ticket": payload.get("ticket"),
        "magic_number": payload.get("magic"),
        "action": payload.get("action"),
        "reason": payload.get("reason"),
        "profit_usd": payload.get("profit_usd"),
        "peak_usd": payload.get("peak_usd"),
        "raw_payload": {
            "quick_exit": payload,
            "demo_only": True,
            "managed_magic_number": 909002,
            "entries_enabled": False,
        },
        "created_at": created,
    }


def _confirmed_order_event(event: dict) -> bool:
    if event.get("event_type") != "DEMO_ORDER":
        return False
    if "order_success" in event and event.get("order_success") is not True:
        return False
    if "order_retcode" in event and _retcode_int(event.get("order_retcode")) not in _success_retcode_values():
        return False
    if "ticket" in event and not _ticket_present(event.get("ticket")):
        return False
    return True


def _latest_order_result_payload(event: dict | None) -> dict | None:
    if not event:
        return None
    return {
        "event_type": event.get("event_type"),
        "mode": event.get("mode"),
        "symbol": event.get("symbol"),
        "status": event.get("status"),
        "ticket": event.get("ticket"),
        "order_success": event.get("order_success"),
        "order_retcode": event.get("order_retcode"),
        "order_failure_reason": event.get("order_failure_reason"),
        "order_result": event.get("order_result"),
        "created_at": event.get("created_at"),
    }


def _demo_order_raw_payload(event: dict) -> dict:
    payload = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    out = dict(payload)
    for key in (
        "strategy",
        "rr",
        "kelly_suggested_lot",
        "final_capped_lot",
        "setup_id",
        "edge_score",
        "m1_trigger_status",
        "m1_trigger_reason",
        "m15_confirmation_status",
        "m15_confirmation_reason",
        "exploration_override_reason",
        "mode",
        "direction",
        "entry",
        "sl",
        "tp",
        "symbol",
        "broker_symbol",
        "quant_slope",
        "quant_r2",
        "quant_z_score",
        "quant_mean",
        "quant_stdev",
        "quant_signal",
        "quant_score",
        "quant_reason",
        "quant_pro_regime",
        "quant_pro_score",
        "quant_pro_grade",
        "quant_pro_ols_slope",
        "quant_pro_ols_r2",
        "quant_pro_ols_tstat",
        "quant_pro_kalman_velocity",
        "quant_pro_kalman_z",
        "quant_pro_ou_beta",
        "quant_pro_ou_tstat",
        "quant_pro_ou_half_life",
        "quant_pro_hurst",
        "quant_pro_hurst_filter",
        "quant_pro_min_trend_hurst",
        "quant_pro_trend_strength",
        "quant_pro_hurst_filter_status",
        "quant_pro_hurst_block_reason",
        "quant_pro_ewma_vol",
        "quant_pro_signal",
        "quant_pro_reason",
        "gold_liquidity_hunter",
        "gold_liquidity_signal",
        "gold_liquidity_score",
        "gold_liquidity_reason",
        "gold_m1m5_scalper",
        "gold_m1m5_scalper_decision",
        "gold_m1m5_scalper_score",
        "gold_m1m5_scalper_reason",
        "eur_ema_rsi_atr",
        "eur_ema_rsi_atr_decision",
        "eur_ema_rsi_atr_reason",
        "relaxed_mode_active",
        "relaxed_reason",
        "hours_without_setup",
        "strict_threshold",
        "relaxed_threshold",
        "relaxed_trade_count_today",
        "last_relaxed_trade_result",
        "adaptive_confluence_enabled",
        "final_confluence_score",
        "symbol_min_confluence",
        "confluence_threshold_pass",
        "confluence_threshold_reason",
        "confluence_components",
        "confluence_mode",
        "max_money_tp",
    ):
        if event.get(key) is not None:
            out[key] = event.get(key)
    if isinstance(event.get("wsp_intelligence"), dict):
        out["wsp_intelligence"] = event.get("wsp_intelligence")
    out["setup_grade"] = event.get("setup_grade") or event.get("grade") or event.get("setup_hunter_grade") or event.get("big_setup_grade")
    out["source"] = out.get("source") or "DEMO_ORDER"
    return out


def _wsp_payload(decision: dict) -> dict:
    if isinstance(decision.get("wsp_intelligence"), dict):
        return dict(decision["wsp_intelligence"])
    raw = decision.get("raw_payload") if isinstance(decision.get("raw_payload"), dict) else {}
    if isinstance(raw.get("wsp_intelligence"), dict):
        return dict(raw["wsp_intelligence"])
    return {}


def _demo_trade_details(events: list[dict]) -> list[dict]:
    by_ticket: dict[str, dict] = {}
    for event in events:
        ticket = str(event.get("ticket") or "")
        if not ticket or ticket.lower() in {"none", "null", "0"}:
            continue
        event_type = str(event.get("event_type") or "").upper()
        if event_type not in {"DEMO_ORDER", "DEMO_CLOSE", "POSITION_SYNC"}:
            continue
        raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        merged = {**raw, **payload, **{key: value for key, value in event.items() if value is not None}}
        current = by_ticket.get(ticket, {})
        by_ticket[ticket] = {**current, **merged, "ticket": ticket}
    details = []
    for ticket, item in by_ticket.items():
        raw = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        merged = {**raw, **payload, **item}
        details.append(
            {
                "ticket": ticket,
                "symbol": merged.get("symbol") or merged.get("broker_symbol") or merged.get("display_symbol"),
                "direction": merged.get("direction") or merged.get("dir") or merged.get("side"),
                "strategy": merged.get("strategy"),
                "rr": merged.get("rr"),
                "kelly_suggested_lot": merged.get("kelly_suggested_lot"),
                "final_capped_lot": merged.get("final_capped_lot"),
                "entry": merged.get("entry"),
                "sl": merged.get("sl"),
                "tp": merged.get("tp"),
                "pnl": merged.get("pnl") if merged.get("pnl") is not None else merged.get("profit"),
                "status": merged.get("status") or merged.get("result"),
                "gate": merged.get("exploration_override_reason") or merged.get("source") or merged.get("reason"),
            }
        )
    return details


def _closed_demo_trade_rows(events: list[dict], magic_number: int) -> list[dict]:
    by_ticket: dict[str, dict] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        merged = {**raw, **payload, **{key: value for key, value in event.items() if value is not None}}
        row_magic = merged.get("magic_number", merged.get("magic"))
        if row_magic is not None:
            try:
                if int(row_magic) != int(magic_number):
                    continue
            except (TypeError, ValueError):
                continue
        event_type = str(merged.get("event_type") or "").upper()
        status = str(merged.get("status") or merged.get("result") or "").upper()
        closed = event_type == "DEMO_CLOSE" or status == "CLOSED" or bool(merged.get("closed_at"))
        if not closed:
            continue
        pnl = _to_float(merged.get("pnl"))
        if pnl is None:
            pnl = _to_float(merged.get("profit"))
        if pnl is None:
            continue
        ticket = str(merged.get("ticket") or merged.get("position_ticket") or merged.get("order") or f"row-{index}")
        if ticket.lower() in {"", "none", "null", "0"}:
            ticket = f"row-{index}"
        by_ticket[ticket] = {**merged, "ticket": ticket, "pnl": pnl}
    return list(by_ticket.values())


def _latest_position_sync_count(event: dict | None, key: str) -> int:
    if not event:
        return 0
    value = event.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _current_mt5_position_counts(settings: Settings) -> dict:
    try:
        positions = list(mt5.positions_get() or [])
    except Exception as exc:
        log.warning("[POSITION_SYNC] report live MT5 positions unavailable reason=%s", exc)
        positions = []
    hermes = []
    for position in positions:
        payload = _position_payload(position)
        magic = payload.get("magic")
        symbol = str(payload.get("symbol") or "").upper()
        display_symbol = symbol.rstrip("#")
        try:
            magic_number = int(magic)
        except (TypeError, ValueError):
            continue
        if magic_number != settings.demo_magic_number:
            continue
        if symbol in {"GOLD#", "GOLD", "BTCUSD#", "BTCUSD", "EURUSD"} or display_symbol in {"GOLD", "BTCUSD", "EURUSD"}:
            hermes.append(payload)
    return {
        "mt5_open_positions_count": len(positions),
        "hermes_mt5_open_positions_count": len(hermes),
    }


def _open_position_sync_tickets(events: list[dict]) -> set[str]:
    tickets: set[str] = set()
    for event in events:
        ticket = str(event.get("ticket") or "")
        if not ticket:
            continue
        result = str(event.get("result") or event.get("status") or "").upper()
        if result == "OPEN":
            tickets.add(ticket)
        elif result == "CLOSED":
            tickets.discard(ticket)
    return tickets


def _position_payload(position: object) -> dict:
    if isinstance(position, dict):
        return dict(position)
    if hasattr(position, "_asdict"):
        return dict(position._asdict())
    keys = ("ticket", "magic", "symbol", "comment")
    return {key: getattr(position, key) for key in keys if hasattr(position, key)}


def _sb_key(suffix: str) -> str:
    return "supa" + "base_" + suffix


def _retcode_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _ticket_present(value: object) -> bool:
    if value is None:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _parse_iso(value: str) -> datetime | None:
    try:
        if not value:
            return None
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _is_btc(symbol: object) -> bool:
    return str(symbol or "").upper().replace("#", "").startswith("BTCUSD")


def _is_gold_symbol(symbol: object) -> bool:
    normalized = _canonical_trade_symbol(symbol)
    return normalized == "GOLD"


def _is_eurusd(symbol: object) -> bool:
    return _canonical_trade_symbol(symbol) == "EURUSD"


def _is_simo_pending_order(event: dict) -> bool:
    """Return True if the event represents a SIMO pending order (BUY_STOP or SELL_STOP)."""
    strategy = str(event.get("strategy") or "").upper()
    if strategy != "SIMO_ATM_BREAKOUT":
        return False
    explicit = str(event.get("order_type") or "").upper()
    if explicit in {"BUY_STOP", "SELL_STOP"}:
        return True
    direction = str(event.get("direction") or "").upper()
    entry = _to_float(event.get("entry"))
    ask = _to_float(event.get("market_ask"))
    bid = _to_float(event.get("market_bid"))
    if direction == "BUY" and entry is not None and ask is not None and entry > ask:
        return True
    if direction == "SELL" and entry is not None and bid is not None and entry < bid:
        return True
    return False


def _get_pending_orders_for_symbol_magic(symbol: str, magic: int) -> list:
    try:
        orders = mt5.orders_get(symbol=symbol)
        if orders is None:
            return []
        return [
            o for o in orders
            if getattr(o, "magic", None) == magic
            and getattr(o, "type", None) in {
                getattr(mt5, "ORDER_TYPE_BUY_STOP", 4),
                getattr(mt5, "ORDER_TYPE_SELL_STOP", 5),
            }
        ]
    except Exception:
        return []


def _cancel_expired_pending_orders(symbol: str, magic: int) -> None:
    pending = _get_pending_orders_for_symbol_magic(symbol, magic)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for order in pending:
        expiration = getattr(order, "time_expiration", None)
        ticket = getattr(order, "ticket", None)
        if expiration and expiration > 0:
            expiry_dt = datetime.utcfromtimestamp(expiration)
            if now > expiry_dt:
                try:
                    remove_request = {
                        "action": getattr(mt5, "TRADE_ACTION_REMOVE", 8),
                        "order": ticket,
                    }
                    result = mt5.order_send(remove_request)
                    log.info(
                        "[SIMO_ATM_PENDING] expired/cancelled ticket=%s symbol=%s retcode=%s",
                        ticket, symbol,
                        getattr(result, "retcode", None) if result else None,
                    )
                except Exception as exc:
                    log.info("[SIMO_ATM_PENDING] cancel_failed ticket=%s reason=%s", ticket, exc)


def _canonical_trade_symbol(symbol: object) -> str:
    normalized = str(symbol or "").upper().strip()
    if normalized in {"BTCUSD", "BTCUSD#"}:
        return "BTCUSD"
    if normalized in {"GOLD", "GOLD#", "XAUUSD", "XAUUSD#"}:
        return "GOLD"
    if normalized.startswith("BTCUSD"):
        return "BTCUSD"
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return "GOLD"
    if normalized.startswith("EURUSD"):
        return "EURUSD"
    return normalized.replace("#", "")


def _trade_mode_account_type(trade_mode: object) -> str:
    try:
        mode = int(trade_mode)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if mode == 0:
        return "DEMO"
    if mode == 1:
        return "CONTEST"
    if mode == 2:
        return "LIVE"
    return "UNKNOWN"


def sl_engine_apply_modification(
    ticket: int,
    sl_price: float,
    tp_price: float,
    symbol: str = "BTCUSD#",
) -> dict:
    """Apply SL/TP to an open position for BtcSlEngine. Called from btc_sl_engine.py only.

    Uses TRADE_ACTION_SLTP (6) with "position" key — the correct action for modifying
    SL/TP of an open position. TRADE_ACTION_MODIFY (7) is for pending orders and causes
    retcode=10013 on open positions.

    Request structure mirrors _quick_exit_modify_sl which is confirmed working.
    Fields "type", "volume", "price" are never present — they would open a new market order.
    """
    try:
        # Round to symbol precision (digits=2 for BTCUSD# as fallback)
        _sym_info = mt5.symbol_info(symbol)
        _digits = int(getattr(_sym_info, "digits", 2)) if _sym_info is not None else 2
        _magic = getattr(_get_settings(), "demo_magic_number", 909002)

        request = {
            "action":   getattr(mt5, "TRADE_ACTION_SLTP", 6),
            "symbol":   symbol,
            "position": int(ticket),
            "sl":       round(float(sl_price), _digits),
            "tp":       round(float(tp_price), _digits),
            "magic":    _magic,
            "comment":  "HERMES_SL_ENGINE",
        }
        # Guardrail: "type" field would send a new market order — never allowed here
        if "type" in request:
            log.error(
                "[SL_ENGINE_CRITICAL] type_field_in_modify_request ticket=%s — BLOCKED",
                ticket,
            )
            return {"success": False, "retcode": None, "comment": "GUARDRAIL_TYPE_FIELD_BLOCKED"}

        log.info("[SL_ENGINE_REQUEST_DUMP] ticket=%s request=%s", ticket, request)

        result = mt5.order_send(request)

        if result is not None:
            log.info(
                "[SL_ENGINE_RESULT_DUMP] ticket=%s retcode=%s comment=%s "
                "request_id=%s deal=%s order=%s volume=%s price=%s bid=%s ask=%s",
                ticket,
                getattr(result, "retcode", None),
                getattr(result, "comment", ""),
                getattr(result, "request_id", None),
                getattr(result, "deal", None),
                getattr(result, "order", None),
                getattr(result, "volume", None),
                getattr(result, "price", None),
                getattr(result, "bid", None),
                getattr(result, "ask", None),
            )
        log.info("[SL_ENGINE_LAST_ERROR] ticket=%s last_error=%s", ticket, mt5.last_error())

        retcode = getattr(result, "retcode", None) if result is not None else None
        comment = getattr(result, "comment", "") if result is not None else ""
        success = bool(retcode == getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        return {"success": success, "retcode": retcode, "comment": comment}
    except Exception as exc:
        return {"success": False, "retcode": None, "comment": str(exc)[:200]}
