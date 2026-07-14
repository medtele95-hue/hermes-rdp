from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.agents.journal_layer import enrich_with_journal, journal_payload
from app.agents.journal_layer import calculate_performance_metrics
from app.logger import log, utc_now_iso
from app.profiles.lovable_btc_old_system import (
    is_active as _lovable_btc_is_active,
    PROFILE_NAME as _LOVABLE_BTC_PROFILE_NAME,
    OLD_BTC_SYMBOL as _OLD_BTC_SYMBOL,
    OLD_BTC_STRATEGIES as _OLD_BTC_STRATEGIES,
    old_btc_mode_for_strategy as _old_btc_mode_for_strategy,
)
from app.services.dashboard_snapshot import dashboard_snapshot, dashboard_status_debug_line
from app.services.heartbeat_service import HeartbeatService
from app.services.ingest_client import IngestClient, format_response_body
from app.services.mt5_position_sync import force_close_demo_ticket, sync_open_mt5_positions_to_lovable
from app.services.paper_report_analytics import (
    build_observer_summaries,
    build_time_stats,
    clean_report_samples,
    format_adjusted_report_tables,
    format_time_stats_tables,
    manual_adjusted_report,
)
from app.agents.confluence_engine import evaluate_confluence
from app.services.strategy_manager import StrategyManager
from app.services.balanced_selector import select_best_candidate
from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES, ALLOWED_GOLD_EXECUTION_STRATEGIES, CONFIRMATION_MODULE_STRATEGIES
from app.utils.confidence import normalize_confidence
from app.utils.throttle import should_emit


def _is_demo_eligible_btc_scalping_candidate(candidate: dict | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    return (
        str(candidate.get("best_strategy") or "").upper() == "BTC_SCALPING_AGENT"
        and bool(candidate.get("demo_eligible"))
        and str(candidate.get("direction") or "").upper() in {"BUY", "SELL"}
    )


def _is_demo_eligible_active_candidate(candidate: dict | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    return (
        str(candidate.get("best_strategy") or "").upper() in ACTIVE_EXECUTION_STRATEGIES
        and bool(candidate.get("demo_eligible"))
        and str(candidate.get("direction") or "").upper() in {"BUY", "SELL"}
    )


def _is_gold_symbol(*symbols: object) -> bool:
    return any(str(symbol or "").upper().startswith(("GOLD", "XAUUSD")) for symbol in symbols)


def _is_allowed_gold_execution_candidate(candidate: dict | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    return (
        str(candidate.get("best_strategy") or "").upper() in ALLOWED_GOLD_EXECUTION_STRATEGIES
        and bool(candidate.get("demo_eligible"))
        and str(candidate.get("direction") or "").upper() in {"BUY", "SELL"}
    )


def _routeable_setup_hunter_decision(decision: dict, candidate: dict | None) -> dict:
    if _is_demo_eligible_active_candidate(candidate):
        strategy = str(candidate.get("best_strategy") or "").upper()
        return {
            **decision,
            "symbol": candidate.get("symbol") or decision.get("symbol"),
            "broker_symbol": candidate.get("broker_symbol") or decision.get("broker_symbol"),
            "strategy": strategy,
            "signal": str(candidate.get("direction") or decision.get("signal") or decision.get("direction") or "").upper(),
            "direction": str(candidate.get("direction") or decision.get("direction") or decision.get("signal") or "").upper(),
            "entry": candidate.get("entry") if candidate.get("entry") is not None else decision.get("entry"),
            "sl": candidate.get("sl") if candidate.get("sl") is not None else decision.get("sl"),
            "tp": candidate.get("tp") if candidate.get("tp") is not None else decision.get("tp"),
            "risk_reward": candidate.get("rr") if candidate.get("rr") is not None else decision.get("risk_reward"),
            "reward_risk": candidate.get("rr") if candidate.get("rr") is not None else decision.get("reward_risk"),
            "edge_score": candidate.get("edge_score") if candidate.get("edge_score") is not None else decision.get("edge_score"),
            "setup_score": candidate.get("setup_score") if candidate.get("setup_score") is not None else decision.get("setup_score"),
            "setup_hunter_score": candidate.get("edge_score") if candidate.get("edge_score") is not None else decision.get("setup_hunter_score"),
            "setup_hunter_grade": candidate.get("grade") or decision.get("setup_hunter_grade"),
            "normalized_confidence": normalize_confidence(candidate.get("normalized_confidence") if candidate.get("normalized_confidence") is not None else decision.get("confidence")),
            "btc_scalping_confidence": normalize_confidence(candidate.get("btc_scalping_confidence") if candidate.get("btc_scalping_confidence") is not None else decision.get("confidence")),
            "decision": "ROUTE_TO_DEMO",
            "route_to_demo": True,
        }
    return decision


def _gold_handoff_blocked(candidate: dict | None, symbol: str, broker_symbol: str) -> bool:
    if not _is_gold_symbol(symbol, broker_symbol):
        return False
    return not _is_allowed_gold_execution_candidate(candidate)


def _cfg_sym_for_resolved(requested: str, main_syms: list[str]) -> str:
    """Map a resolved_symbols key to the hermes_main_symbol_list display name (display/log only)."""
    req_norm = requested.upper().rstrip("#")
    for cfg in main_syms:
        cfg_norm = cfg.upper().rstrip("#")
        if req_norm == cfg_norm:
            return cfg
        if req_norm == "XAUUSD" and cfg_norm == "GOLD":
            return cfg
        if req_norm in {"US100CASH", "US100", "NAS100", "USTEC", "NASDAQ"} and "US100" in cfg_norm:
            return cfg
    return requested


def _price_from_tick(tick: dict | None) -> tuple[float | None, float | None]:
    """Return (price, spread) from a tick dict. price=last if >0, else mid bid/ask."""
    t = tick or {}
    bid = float(t.get("bid") or 0)
    ask = float(t.get("ask") or 0)
    last = float(t.get("last") or 0)
    if last > 0:
        spread = round(ask - bid, 5) if bid > 0 and ask > 0 else None
        return last, spread
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 5), round(ask - bid, 5)
    return None, None


def _canonical_order_flow_symbol(symbol: object) -> str:
    text = str(symbol or "").upper().strip()
    if text.startswith("BTCUSD"):
        return "BTCUSD"
    if text.startswith(("GOLD", "XAUUSD")):
        return "GOLD"
    if text.startswith("EURUSD"):
        return "EURUSD"
    return text.replace("#", "")


_QUAD_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4")


def _build_quad_entry(
    broker_symbol: str,
    frames: dict,
    analysis: dict | None,
    symbol_state: dict | None,
    order_flow_snap: dict | None,
) -> dict:
    """Build a read-only Quad Terminal snapshot for one symbol.

    Emits [QUAD_CANDLES_OK], [QUAD_CONFIRMATIONS_BUILT], [QUAD_PAYLOAD_BUILT] logs.
    Never routes to DemoRouter or any execution path.
    """
    candles: dict[str, dict] = {}
    for tf in _QUAD_TIMEFRAMES:
        df = frames.get(tf)
        available = df is not None and not getattr(df, "empty", True) and len(df) > 0
        if available:
            count = len(df)
            cols = list(getattr(df, "columns", []))
            last_close = float(df.iloc[-1]["close"]) if "close" in cols else None
            candles[tf] = {"available": True, "count": count, "last_close": last_close}
            log.info("[QUAD_CANDLES_OK] symbol=%s timeframe=%s count=%s", broker_symbol, tf, count)
        else:
            candles[tf] = {"available": False, "count": 0, "last_close": None}

    of = order_flow_snap or {}
    levels = {
        "poc": of.get("poc"),
        "vah": of.get("vah"),
        "val": of.get("val"),
        "vwap": of.get("vwap"),
    }

    analysis = analysis or {}
    signals = [s for s in (analysis.get("strategy_signals") or []) if isinstance(s, dict)]
    strategies = [
        {
            "strategy": str(s.get("strategy") or s.get("setup_type") or ""),
            "signal": str(s.get("signal") or s.get("direction") or ""),
            "score": s.get("confidence") or s.get("edge_score"),
        }
        for s in signals
    ]
    confirmations = [
        {
            "strategy": str(s.get("strategy") or s.get("setup_type") or ""),
            "signal": str(s.get("signal") or ""),
            "score": s.get("confidence"),
        }
        for s in signals
        if str(s.get("strategy") or s.get("setup_type") or "").upper() in CONFIRMATION_MODULE_STRATEGIES
    ]
    log.info("[QUAD_CONFIRMATIONS_BUILT] symbol=%s count=%s", broker_symbol, len(confirmations))

    ai = analysis.get("ai_decision") or {}
    trade_map = {
        "signal": str(ai.get("signal") or ai.get("direction") or "WAIT"),
        "decision": str(ai.get("decision") or "WAIT"),
        "reason": ai.get("reason") or ai.get("simo_atm_reason"),
        "mode": "OBSERVE_ONLY",
    }

    ss = symbol_state or {}
    log.info("[QUAD_PAYLOAD_BUILT] symbol=%s", broker_symbol)
    return {
        "symbol": _canonical_order_flow_symbol(broker_symbol),
        "broker_symbol": broker_symbol,
        "available": True,
        "mode": "OBSERVE_ONLY",
        "symbol_state": {
            "price": ss.get("price"),
            "spread": ss.get("spread"),
            "spread_status": ss.get("spread_status"),
            "session": ss.get("session"),
            "time_gate": ss.get("time_gate"),
            "latest_decision": ss.get("latest_decision"),
            "latest_reason": ss.get("latest_reason"),
            "route_status": ss.get("route_status"),
            "last_update_utc": ss.get("last_update_utc"),
        },
        "candles": candles,
        "levels": levels,
        "markers": [],
        "zones": [],
        "confirmations": confirmations,
        "strategies": strategies,
        "order_flow": {
            "status": of.get("status"),
            "order_flow_status": of.get("order_flow_status"),
            "latest_decision": of.get("latest_decision"),
            "latest_reason": of.get("latest_reason"),
            "route_status": of.get("route_status"),
            "poc": of.get("poc"),
            "vah": of.get("vah"),
            "val": of.get("val"),
            "vwap": of.get("vwap"),
            "cvd_proxy": of.get("cvd_proxy"),
            "signal": of.get("signal"),
            "mode": "OBSERVE_ONLY",
        },
        "trade_map": trade_map,
        "created_at": utc_now_iso(),
    }


def _active_handoff_missing_field(decision: dict, candidate: dict | None, kelly_risk: dict | None, broker_symbol: str | None) -> str | None:
    if not _is_demo_eligible_active_candidate(candidate):
        return None
    if not broker_symbol and not decision.get("broker_symbol") and not decision.get("symbol"):
        return "symbol"
    for field in ("entry", "sl", "tp"):
        if decision.get(field) is None:
            return field
    lot = (kelly_risk or {}).get("approved_lot") or (kelly_risk or {}).get("lot_size")
    if lot is None:
        return "lot"
    return None


def _btc_scalping_handoff_missing_field(decision: dict, candidate: dict | None, kelly_risk: dict | None, broker_symbol: str | None) -> str | None:
    return _active_handoff_missing_field(decision, candidate, kelly_risk, broker_symbol)


class HermesBackend:
    def __init__(self) -> None:
        from app.agents.hermes_5min_agent import Hermes5MinAgent
        from app.agents.paper_learning_optimizer import PaperLearningOptimizer
        from app.agents.paper_trading_agent import PaperTradingAgent
        from app.agents.setup_hunter import SetupHunter
        from app.mt5.connection import MT5Connection
        from app.mt5.data_reader import MT5DataReader
        from app.mt5.demo_router import DemoKellyRouter
        from app.services.time_engine import TimeEngine

        self.settings = get_settings()
        self.mt5 = MT5Connection()
        self.reader = MT5DataReader()
        self.ingest_client = IngestClient(self.settings)
        self.heartbeat = HeartbeatService(self.settings, self.ingest_client)
        self.learning_optimizer = PaperLearningOptimizer(self.settings)
        self.agent = Hermes5MinAgent(self.settings, self.learning_optimizer)
        self.paper_trader = PaperTradingAgent(self.settings)
        self.demo_router = DemoKellyRouter(self.settings)
        self.setup_hunter = SetupHunter(self.settings)
        self.time_engine = TimeEngine(self.settings)
        self.strategy_manager = StrategyManager(self.settings)
        self.resolved_symbols: dict[str, str] = {}
        self.sent_candle_keys: set[tuple[str, str, str]] = set()
        self.latest_agent_state: dict | None = None
        self.latest_demo_event: dict | None = None
        self.latest_setup_hunter: dict | None = None
        self.latest_setup_hunter_candidates: list[dict] = []
        self.latest_position_sync: dict | None = None
        self.latest_order_flow_snapshots: dict[str, dict] = {}
        self._live_snapshot_interval_seconds = 5.0
        self._live_snapshot_lock = threading.Lock()
        self._live_snapshot_stop = threading.Event()
        self._live_snapshot_thread: threading.Thread | None = None
        self._cycle_active = False
        # Telemetry state — read-only snapshots for dashboard payload
        self._cycle_status: dict = {"last_status": "STARTING"}
        self._per_symbol_state: dict[str, dict] = {}
        # Pre-populate the SIMO index symbol so every heartbeat (startup,
        # background thread, first-in-cycle) sees a non-empty entry and emits
        # available=true.  Without this the key is absent until the first
        # run_simo_index_cycle() call, causing available=false / last_update_utc=null.
        _simo_pre_sym = next(
            (s for s in self.settings.hermes_main_symbol_list
             if any(s.upper().startswith(t.upper().replace("#", ""))
                    for t in self.settings.simo_atm_symbol_list)),
            "US100Cash#",
        )
        self._per_symbol_state[str(_simo_pre_sym or "").upper()] = {
            "broker_symbol": _simo_pre_sym,
            "price": None, "spread": None, "spread_status": None,
            "session": None, "time_gate": None,
            "latest_decision": "WAIT", "latest_reason": "NOT_YET_EVALUATED",
            "route_status": "STARTING", "last_update_utc": utc_now_iso(),
        }
        self._latest_candidates_all: list[dict] = []
        self._latest_safety_guard: dict | None = None
        # Initialise local dashboard state singleton (read-only, never touches execution)
        try:
            from app.local_api.state import get_local_state, install_log_handler
            install_log_handler()
            get_local_state().set_backend_started_at(utc_now_iso())
        except Exception:
            pass

    def _ingest_health_dict(self) -> dict:
        """Read-only snapshot of Lovable ingest circuit breaker state."""
        if self.ingest_client._fail_soft_skip_active():
            status = "DEGRADED"
            reason = "CIRCUIT_BREAKER_ACTIVE"
        elif self.ingest_client._fail_soft_logged:
            status = "DEGRADED"
            reason = "FAIL_SOFT_ACTIVE"
        else:
            status = "LIVE"
            reason = None
        return {"status": status, "reason": reason, "last_update_utc": utc_now_iso()}

    def start(self) -> None:
        from app.mt5.symbol_mapper import SymbolMapper

        self.demo_router.mark_backend_started()
        self.ingest_client.connect()
        self.send_startup_test_rows()
        if self.paper_trader.enabled:
            self.paper_trader.reset_on_startup()
            self.log_paper_trading_enabled()

        # mission/DASHBOARD.md (2026-07-08): connect using the active
        # account_profiles.env profile if SIMO has configured one explicitly;
        # resolves to {} (today's exact zero-arg behaviour) until then.
        from app.mt5.account_profiles import resolve_active_connection_kwargs
        if not self.mt5.connect(**resolve_active_connection_kwargs()):
            raise SystemExit(1)
        startup_account = self.reader.account_snapshot()
        # BLOC 11e — refuse to start when an instance already runs for the
        # same account + magic
        from app.utils.single_instance import SingleInstanceError, acquire_single_instance_lock
        try:
            acquire_single_instance_lock(
                (startup_account or {}).get("login"), self.settings.demo_magic_number
            )
        except SingleInstanceError as _lock_exc:
            log.error("[SINGLE_INSTANCE] %s", _lock_exc)
            raise SystemExit(1)
        self.demo_router.log_startup(startup_account)
        # ADAPTIVE_ACCOUNT_POLICY stage 1 (BOOT): resolve the policy level
        # from the live trade_mode. Stage 2 re-checks per order in the router.
        from app.services.adaptive_account_policy import (
            build_account_profile,
            log_adaptive_policy,
            resolve_account_policy,
        )
        self.account_policy = resolve_account_policy(startup_account, self.settings)
        log_adaptive_policy(self.account_policy, startup_account)
        # NEWS shield: load the weekly calendar at boot (fail-safe on error)
        if bool(getattr(self.settings, "news_shield_enabled", True)):
            try:
                self.demo_router.news_calendar.refresh(force=True)
            except Exception as _news_exc:
                log.warning("[NEWS_CALENDAR] boot_refresh_failed error=%s", _news_exc)
        self.heartbeat.write(
            startup_account, self.resolved_symbols, self.latest_agent_state,
            self.latest_demo_event, self.mt5.connected, self.latest_setup_hunter,
            self.latest_position_sync, self.latest_order_flow_snapshots,
            cycle_status=self._cycle_status,
            per_symbol_state=self._per_symbol_state,
            latest_candidates=self._latest_candidates_all,
            latest_safety_guard=self._latest_safety_guard,
            ingest_health=self._ingest_health_dict(),
        )
        self.start_live_snapshot_heartbeat()
        # Start fast Smart Exit daemon (tick-fast, independent of analysis cycle)
        self._btc_fast_exit_daemon = None
        if bool(getattr(self.settings, "old_btc_fast_exit_daemon_enabled", True)):
            try:
                self._btc_fast_exit_daemon = self.demo_router.start_fast_exit_daemon()
                log.info("[OLD_BTC_FAST_EXIT_DAEMON] started thread=hermes-btc-fast-exit")
            except Exception as _fex:
                log.warning("[OLD_BTC_FAST_EXIT_DAEMON] start_failed reason=%s", _fex)
        # Start the local read-only dashboard API server
        try:
            from app.local_api.server import start_local_api_server
            from app.local_api.state import get_local_state
            start_local_api_server(host="127.0.0.1", port=8000)
            get_local_state().update_settings({
                "demo_only": bool(self.settings.demo_only),
                "allow_live_trading": bool(self.settings.allow_live_trading),
                "demo_max_lot": self.settings.demo_max_lot,
                "demo_max_open_trades": self.settings.demo_max_open_trades,
                "demo_max_trades_per_day": self.settings.demo_max_trades_per_day,
                "demo_max_daily_loss_pct": self.settings.demo_max_daily_loss_pct,
                "demo_max_risk_per_trade_pct": self.settings.demo_max_risk_per_trade_pct,
                "demo_stop_after_consecutive_losses": self.settings.demo_stop_after_consecutive_losses,
                "demo_magic_number": self.settings.demo_magic_number,
                "hermes_main_symbols": self.settings.hermes_main_symbols,
            })
            log.info("[LOCAL_DASHBOARD] API server started at http://127.0.0.1:8000")
            log.info("[LOCAL_DASHBOARD] Frontend at http://127.0.0.1:5173")
        except Exception as _ldex:
            log.warning("[LOCAL_DASHBOARD] server start failed reason=%s", _ldex)

        self.strategy_manager.log_all_strategies()
        log.info(
            "[HERMES_CONFIG] demo_only=%s allow_live_trading=%s demo_max_lot=%s",
            str(self.settings.demo_only).lower(),
            str(self.settings.allow_live_trading).lower(),
            self.settings.demo_max_lot,
        )
        log.info("[HERMES_CONFIG] main_symbols=%s", self.settings.hermes_main_symbols)
        log.info(
            "[HERMES_CONFIG] fib_confluence_enabled=%s",
            str(self.settings.fib_confluence_execution_enabled).lower(),
        )
        log.info(
            "[HERMES_CONFIG] hermes_pack_enabled=%s",
            str(getattr(self.settings, "hermes_strategy_pack_enabled", True)).lower(),
        )
        log.info(
            "[HERMES_CONFIG] max_total_open_demo_trades=%s",
            getattr(self.settings, "demo_max_open_trades_total", 3),
        )
        log.info(
            "[HERMES_CONFIG] allow_time_block_override=%s",
            str(self.settings.allow_time_block_override).lower(),
        )
        log.info(
            "[HERMES_CONFIG] research_allow_low_confluence=%s",
            str(self.settings.research_allow_low_confluence).lower(),
        )
        self.resolved_symbols = SymbolMapper(self.settings.symbol_list).resolve_all()
        if not self.resolved_symbols:
            log.error("No symbols resolved; stopping")
            raise SystemExit(1)
        # ACCOUNT_PROFILE at boot: live specs, max fundable SL, TRADABLE flags
        try:
            self.account_profile = build_account_profile(
                startup_account, self.resolved_symbols, self.account_policy
            )
        except Exception as _apex:
            self.account_profile = None
            log.warning("[ACCOUNT_PROFILE] build_failed reason=%s", _apex)
        if self.paper_trader.enabled:
            recovery = self.recover_paper_open_trades()
            self.reconcile_recovered_paper_trades(recovery)
        self.send_running_agent_update()

        while True:
            started_at = time.time()
            try:
                self.run_cycle()
            except Exception as exc:
                self._cycle_active = False
                log.exception("Hermes analysis cycle failed: %s", exc)
                self.ingest_client.log_event("ERROR", "Hermes analysis cycle failed", {"error": str(exc)})
            elapsed = time.time() - started_at
            time.sleep(max(1, self.settings.poll_seconds - elapsed))

    def start_live_snapshot_heartbeat(self) -> None:
        if self._live_snapshot_thread and self._live_snapshot_thread.is_alive():
            return
        self._live_snapshot_thread = threading.Thread(target=self._live_snapshot_heartbeat_loop, name="hermes-live-snapshot-heartbeat", daemon=True)
        self._live_snapshot_thread.start()

    def _live_snapshot_heartbeat_loop(self) -> None:
        while not self._live_snapshot_stop.wait(self._live_snapshot_interval_seconds):
            try:
                self.write_live_snapshot_heartbeat()
            except Exception as exc:
                log.warning("[LIVE_SNAPSHOT] heartbeat failed reason=%s", exc)

    def write_live_snapshot_heartbeat(self) -> None:
        account = self.reader.account_snapshot() if self.mt5.connected else None
        if self.mt5.connected and self._cycle_active:
            if should_emit("position_sync_skipped_MAIN_CYCLE_ACTIVE"):
                log.warning("[LIVE_SNAPSHOT] position_sync_skipped reason=MAIN_CYCLE_ACTIVE")
        elif self.mt5.connected:
            sync = self.sync_open_mt5_positions_to_lovable(blocking=False)
            if sync.get("skipped") is not True:
                self.latest_position_sync = sync
        # Emit LOVABLE_INGEST_HEALTH token once per heartbeat (throttled by should_emit)
        if should_emit("LOVABLE_INGEST_HEALTH_HEARTBEAT"):
            self.heartbeat.emit_ingest_health(self._ingest_health_dict())
        self.heartbeat.write(
            account,
            self.resolved_symbols,
            self.latest_agent_state,
            self.latest_demo_event,
            self.mt5.connected,
            self.latest_setup_hunter,
            self.latest_position_sync,
            self.latest_order_flow_snapshots,
            cycle_status=self._cycle_status,
            per_symbol_state=self._per_symbol_state,
            latest_candidates=self._latest_candidates_all,
            latest_safety_guard=self._latest_safety_guard,
            ingest_health=self._ingest_health_dict(),
        )
        # Mirror state to local dashboard API (fail-soft, never raises)
        try:
            from app.local_api.state import get_local_state
            from app.services.dashboard_snapshot import dashboard_snapshot as _build_snap
            _snap = _build_snap(
                self.settings, account, self.ingest_client.latest_time_snapshot,
                self.latest_demo_event, self.mt5.connected,
                setup_hunter=self.latest_setup_hunter,
                latest_position_sync=self.latest_position_sync,
                order_flow_snapshots=self.latest_order_flow_snapshots,
                cycle_status=self._cycle_status,
                per_symbol_state=self._per_symbol_state,
                latest_candidates=self._latest_candidates_all,
                latest_safety_guard=self._latest_safety_guard,
                ingest_health=self._ingest_health_dict(),
            )
            get_local_state().update_core(
                account_snapshot=account or {},
                per_symbol_state=self._per_symbol_state,
                cycle_status=self._cycle_status,
                order_flow_snapshots=self.latest_order_flow_snapshots,
                latest_candidates=self._latest_candidates_all,
                latest_safety_guard=self._latest_safety_guard,
                latest_setup_hunter=self.latest_setup_hunter,
                latest_demo_event=self.latest_demo_event,
                mt5_connected=self.mt5.connected,
                resolved_symbols=self.resolved_symbols,
                position_sync=self.latest_position_sync or {},
                dashboard_snapshot=_snap,
                ingest_health=self._ingest_health_dict(),
            )
            # Capture recent demo router events for the trades page
            try:
                _events = list(self.demo_router._load_events() or [])
                get_local_state().set_demo_events(_events)
            except Exception:
                pass
        except Exception:
            pass

    def send_startup_test_rows(self) -> None:
        now = utc_now_iso()
        self.ingest_client.send_row(
            "bot_logs",
            {
                "level": "INFO",
                "source": "HERMES_BACKEND",
                "message": "Hermes ingest startup test",
                "context": {"read_only": self.settings.read_only},
                "created_at": now,
            },
        )
        self.ingest_client.send_row(
            "bot_status",
            {
                "bot_name": "HERMES_5MIN_AGENT",
                "component": "hermes_core",
                "status": "STARTING",
                "mode": "READ_ONLY" if self.settings.read_only else "LIVE_DISABLED",
                "read_only": self.settings.read_only,
                "magic_number": self.settings.hermes_magic_number,
                "updated_at": now,
            },
        )
        self.ingest_client.send_row(
            "hermes_agents",
            {
                "name": "HERMES_5MIN_AGENT",
                "display_name": "MT5 x HERMES 5-MIN AI TRADING AGENT",
                "status": "STARTING",
                "mode": "READ_ONLY" if self.settings.read_only else "LIVE_DISABLED",
                "magic_number": self.settings.hermes_magic_number,
                "updated_at": now,
            },
        )

    def log_paper_trading_enabled(self) -> None:
        message = "PAPER_TRADING ENABLED - SIMULATION ONLY - NO MT5 ORDERS"
        log.info(message)
        self.ingest_client.send_row(
            "bot_logs",
            {
                "level": "INFO",
                "source": "PAPER_TRADING",
                "message": message,
                "created_at": utc_now_iso(),
            },
        )

    def recover_paper_open_trades(self) -> dict:
        try:
            result = self.ingest_client.get_open_paper_trades(self.settings.hermes_magic_number)
            if not result.get("ok"):
                log.warning("[PAPER_RECOVERY] unavailable reason=%s", result.get("error") or result.get("body"))
                return {"loaded": 0, "duplicates": 0, "invalid": 0, "unavailable": True}
            rows = result.get("rows") if isinstance(result.get("rows"), list) else []
            return self.paper_trader.recover_open_trades(rows)
        except Exception as exc:
            log.warning("[PAPER_RECOVERY] unavailable reason=%s", exc)
            return {"loaded": 0, "duplicates": 0, "invalid": 0, "unavailable": True}

    def reconcile_recovered_paper_trades(self, recovery: dict | None = None) -> dict:
        recovery = recovery or {}
        if recovery.get("unavailable"):
            log.warning("[PAPER_RECOVERY_RECONCILE] skipped reason=recovery_unavailable")
            self.paper_trader.mark_recovery_reconciliation_completed()
            return {"checked": 0, "closed": 0, "still_open": 0, "skipped": True}

        checked = 0
        closed = 0
        account = self.reader.account_snapshot()
        equity = self._account_equity(account)
        for requested_symbol, broker_symbol in self.resolved_symbols.items():
            trades = list(self.paper_trader.open_trades.get(requested_symbol) or [])
            if not trades:
                continue
            tick = self.reader.symbol_tick(broker_symbol)
            symbol_specs = self.reader.symbol_trade_specs(broker_symbol)
            checked += len(trades)
            items = self.paper_trader.reconcile_recovered_trades(requested_symbol, broker_symbol, tick, symbol_specs, equity)
            closed += sum(1 for item in items if item.get("paper_action") == "CLOSE_EVENT")
            self.write_ingest_items(items)
        still_open = sum(len(trades) for trades in self.paper_trader.open_trades.values())
        self.paper_trader.mark_recovery_reconciliation_completed()
        log.info("[PAPER_RECOVERY_RECONCILE] completed checked=%s closed=%s still_open=%s", checked, closed, still_open)
        return {"checked": checked, "closed": closed, "still_open": still_open}

    def run_cycle(self) -> None:
        cycle_start_utc = datetime.now(timezone.utc)
        log.info("[CYCLE] started")
        self._cycle_active = True

        # P0-3 (2026-07-13) : sonde la connexion MT5 a CHAQUE cycle et tente une
        # reconnexion si elle est tombee. `self.mt5.connected` n'etait jusqu'ici
        # ecrit qu'une seule fois, au boot (app/mt5/connection.py:59) : tous les
        # gardes qui le testent (ligne 707 ci-dessous, et le garde
        # MT5_NOT_CONNECTED de run_simo_index_cycle) etaient donc du code mort,
        # et un terminal tombe en cours de session laissait le bot tourner a
        # l'aveugle. Cet appel les ranime : desormais `connected` reflete l'etat
        # reel, cycle par cycle.
        self.mt5.ensure_connected()

        analyzed = 0
        skipped = 0
        paper_opened = 0
        paper_closed = 0
        demo_orders = 0
        # Reset per-cycle candidate accumulator
        self._latest_candidates_all = []
        self._cycle_status = {
            "last_cycle_start_utc": cycle_start_utc.isoformat(),
            "last_cycle_end_utc": self._cycle_status.get("last_cycle_end_utc"),
            "analyzed": 0,
            "skipped": 0,
            "demo_orders": 0,
            "last_status": "RUNNING",
        }
        _lovable_btc_profile = _lovable_btc_is_active(self.settings)
        _main_symbols_cfg = self.settings.hermes_main_symbol_list
        if _lovable_btc_profile:
            _main_symbols_cfg = [_OLD_BTC_SYMBOL]
            log.info("[EXECUTION_PROFILE] name=%s", _LOVABLE_BTC_PROFILE_NAME)
            log.info(
                "[OLD_BTC_PROFILE_ACTIVE] profile=%s symbol=%s strategies=%s",
                _LOVABLE_BTC_PROFILE_NAME,
                _OLD_BTC_SYMBOL,
                ",".join(sorted(_OLD_BTC_STRATEGIES)),
            )
        log.info("[SYMBOL_CYCLE_CONFIG] symbols=%s", ",".join(_main_symbols_cfg))
        self.ingest_client.emit_bot_log(
            "CYCLE",
            f"cycle_start symbols={list(_main_symbols_cfg)}",
            {"utc": cycle_start_utc.isoformat(), "symbols": list(_main_symbols_cfg)},
        )
        self.ingest_client.emit_bot_log(
            "SYMBOL_CYCLE_CONFIG",
            f"symbols={','.join(_main_symbols_cfg)}",
            {"symbols": _main_symbols_cfg},
        )
        account = self.reader.account_snapshot()
        if self.mt5.connected:
            # NEWS shield daily refresh (internally throttled, fail-safe)
            if bool(getattr(self.settings, "news_shield_enabled", True)):
                try:
                    self.demo_router.news_calendar.refresh()
                except Exception as _news_exc:
                    log.warning("[NEWS_CALENDAR] cycle_refresh_failed error=%s", _news_exc)
            # SUPER-EYES: pure observation features, zero decisional effect
            try:
                from app.services.market_eyes import collect_market_eyes
                self.demo_router.market_eyes_snapshot = collect_market_eyes(self.settings)
            except Exception as _eyes_exc:
                self.demo_router.market_eyes_snapshot = None
                log.warning("[MARKET_EYES] collect_failed error=%s", _eyes_exc)
            self.latest_position_sync = self.sync_open_mt5_positions_to_lovable()
            # P0-G : la labellisation du dataset ne depend plus de la config des
            # sorties. Elle vivait a l'interieur de process_quick_exits, apres ses
            # retours anticipes : QUICK_EXIT_ENABLED=false aurait supprime tout
            # labelling, pour toujours, sans un seul log.
            self.demo_router.update_outcome_tracker(now=cycle_start_utc)
            self.write_ingest_items(self.demo_router.process_quick_exits(
                account,
                self.mt5.connected,
                market_contexts=self.latest_order_flow_snapshots or {},
            ))
        self.heartbeat.write(
            account, self.resolved_symbols, self.latest_agent_state,
            self.latest_demo_event, self.mt5.connected, self.latest_setup_hunter,
            self.latest_position_sync, self.latest_order_flow_snapshots,
            cycle_status=self._cycle_status,
            per_symbol_state=self._per_symbol_state,
            latest_candidates=self._latest_candidates_all,
            latest_safety_guard=self._latest_safety_guard,
            ingest_health=self._ingest_health_dict(),
        )
        self._cycle_active = False

        _symbols_to_process = (
            {k: v for k, v in self.resolved_symbols.items() if k.upper().startswith("BTCUSD")}
            if _lovable_btc_profile
            else self.resolved_symbols
        )
        for requested_symbol, broker_symbol in _symbols_to_process.items():
            symbol_status = "started"
            _display_sym = _cfg_sym_for_resolved(requested_symbol, list(_main_symbols_cfg))
            log.info("[SYMBOL_CYCLE_IN] symbol=%s raw_symbol=%s broker_symbol=%s", _display_sym, requested_symbol, broker_symbol)
            candle_rows = self.reader.latest_candle_rows(requested_symbol, broker_symbol)
            candle_rows = self._filter_unsent_candles(candle_rows)
            candle_result = self.ingest_client.send_bulk("market_candles", candle_rows)
            if candle_rows and candle_result.get("sent", 0) > 0 and candle_result.get("failed", 0) == 0:
                log.info("Candles pushed")

            frames = self.reader.get_all_timeframes(broker_symbol, count=300)
            # Cache candles for local dashboard (fail-soft)
            try:
                from app.local_api.state import get_local_state as _gls
                _ls = _gls()
                _sym_key = str(requested_symbol).upper().rstrip("#")
                for _tf, _df in frames.items():
                    if _df is not None and not getattr(_df, "empty", True) and len(_df) > 0:
                        _rows = []
                        for _, _row in _df.tail(500).iterrows():
                            _entry: dict = {}
                            for _col in _df.columns:
                                _v = _row[_col]
                                if hasattr(_v, "isoformat"):
                                    _entry[_col] = _v.isoformat()
                                elif hasattr(_v, "item"):
                                    _entry[_col] = _v.item()
                                else:
                                    _entry[_col] = _v
                            _rows.append(_entry)
                        _ls.update_candles(_sym_key, _tf, _rows)
            except Exception:
                pass
            tick = self.reader.symbol_tick(broker_symbol)
            time_snapshot = self.time_engine.evaluate(requested_symbol, frames, tick)
            self.ingest_client.set_time_snapshot(time_snapshot)
            self.ingest_client.emit_bot_log(
                "TIME_GATE",
                f"symbol={requested_symbol} status={time_snapshot.get('time_gate_status')} session={time_snapshot.get('session_name')}",
                {
                    "symbol": requested_symbol,
                    "broker_symbol": broker_symbol,
                    "time_gate_status": time_snapshot.get("time_gate_status"),
                    "time_gate_reason": time_snapshot.get("time_gate_reason"),
                    "session_name": time_snapshot.get("session_name"),
                    "is_weekend": time_snapshot.get("is_weekend"),
                    "market_open": time_snapshot.get("market_open"),
                },
            )
            symbol_specs = self.reader.symbol_trade_specs(broker_symbol)
            equity = self._account_equity(account)
            close_items = self.paper_trader.process_closures(requested_symbol, broker_symbol, frames["M5"], tick, symbol_specs, equity)
            paper_closed += sum(1 for item in close_items if item.get("paper_action") == "CLOSE_EVENT")
            self.write_ingest_items(close_items)

            if frames["M5"].empty:
                self.ingest_client.log_event("WARNING", "No M5 candles available", {"symbol": requested_symbol})
                skipped += 1
                self._cycle_status["skipped"] = skipped
                self._cycle_status["analyzed"] = analyzed
                log.info("[CYCLE] symbol=%s status=skipped_no_m5", requested_symbol)
                log.info("[SYMBOL_CYCLE_SKIP] symbol=%s reason=NO_M5_CANDLES", requested_symbol)
                log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=SKIPPED", requested_symbol)
                continue

            effective_max_spread = self.settings.max_spread_for_symbol(requested_symbol)
            analysis_spread = self._analysis_spread(frames["M5"])
            self.log_spread_diag(requested_symbol, broker_symbol, analysis_spread, symbol_specs, tick, effective_max_spread)

            open_count = self.reader.hermes_open_positions_count(self.settings.hermes_magic_number)
            analysis = self.agent.analyze_symbol(
                requested_symbol,
                frames,
                account,
                open_count,
                symbol_specs,
                effective_max_spread,
                self._relaxed_setup_context(broker_symbol),
            )
            order_flow_rows = self._order_flow_snapshot_rows(analysis, requested_symbol, broker_symbol)
            if order_flow_rows:
                self.ingest_client.send_bulk("order_flow_snapshots", order_flow_rows)
            # Cache strategy signals and analysis for local dashboard (fail-soft)
            try:
                from app.local_api.state import get_local_state as _gls2
                _ls2 = _gls2()
                _sym_key2 = str(requested_symbol).upper().rstrip("#")
                _sigs = analysis.get("strategy_signals") or []
                _ls2.update_strategy_signals(_sym_key2, _sigs)
                _ls2.update_analysis_cache(_sym_key2, {k: v for k, v in analysis.items() if k != "strategy_signals"})
            except Exception:
                pass

            # STRATEGY_CLASS — one row per symbol showing which active-execution strategies fired
            _active_strategies = [
                str(s.get("strategy") or s.get("setup_type") or "").upper()
                for s in (analysis.get("strategy_signals") or [])
                if str(s.get("strategy") or s.get("setup_type") or "").upper() in ACTIVE_EXECUTION_STRATEGIES
            ]
            self.ingest_client.emit_bot_log(
                "STRATEGY_CLASS",
                f"symbol={requested_symbol} active_execution_count={len(_active_strategies)}",
                {
                    "symbol": requested_symbol,
                    "broker_symbol": broker_symbol,
                    "active_execution_strategies": _active_strategies,
                    "total_signals": len(analysis.get("strategy_signals") or []),
                },
            )

            market_state_result = self.ingest_client.send_row("market_states", analysis["market_state"])
            markov_result = self.ingest_client.send_row("markov_predictions", analysis["markov_prediction"])
            if market_state_result.get("ok") and markov_result.get("ok"):
                log.info("Markov prediction written")

            self.ingest_client.send_bulk("strategy_signals", analysis["strategy_signals"])
            kelly_result = self.ingest_client.send_row("kelly_risk", analysis["kelly_risk"])
            if kelly_result.get("ok"):
                log.info("Kelly risk written")

            decision = analysis["ai_decision"]
            hunter = self.setup_hunter.evaluate(
                requested_symbol,
                broker_symbol,
                analysis,
                time_snapshot,
                latest_spread := self._latest_completed_spread(frames["M5"]),
                effective_max_spread,
                symbol_specs=symbol_specs,
                tick=tick,
                recent_candles=self._confirmed_candle_rows(frames["M5"]),
                audit_cycle_id=cycle_start_utc.isoformat(),
            )
            self.latest_setup_hunter = hunter.best_candidate
            self.latest_setup_hunter_candidates = hunter.candidates
            self._latest_candidates_all.extend(hunter.candidates)
            self.persist_setup_hunter_events(hunter.events)

            # ---- Structured telemetry per entry-role candidate ----
            _trace_id = str(uuid4())
            _gold_sym = _is_gold_symbol(requested_symbol, broker_symbol)
            _eur_sym = str(requested_symbol or "").upper().startswith("EURUSD") or str(broker_symbol or "").upper().startswith("EURUSD")
            for _cand in hunter.candidates:
                if not isinstance(_cand, dict):
                    continue
                _role = _cand.get("strategy_role")
                if _role != "ENTRY":
                    continue
                _strat = _cand.get("best_strategy")
                _dir = _cand.get("direction")
                _failed = _cand.get("failed_gates") or []
                _slim = {
                    "symbol": _cand.get("symbol"),
                    "strategy": _strat,
                    "direction": _dir,
                    "score": _cand.get("edge_score"),
                    "grade": _cand.get("grade"),
                    "demo_eligible": _cand.get("demo_eligible"),
                    "rr": _cand.get("rr"),
                    "failed_gates": _failed[:4],
                }
                # Task 5: SETUP_HUNTER_IN only for directional candidates; WAIT gets CANDIDATE_WAIT.
                if _dir not in {"BUY", "SELL"}:
                    _wait_reason = _cand.get("near_miss_reason") or (_failed[0] if _failed else "WAIT")
                    self.ingest_client.emit_bot_log(
                        "CANDIDATE_WAIT",
                        f"symbol={requested_symbol} strategy={_strat} reason={_wait_reason}",
                        {**_slim, "reason": _wait_reason},
                    )
                    self.ingest_client.emit_bot_log(
                        "SETUP_HUNTER_REJECT",
                        f"symbol={requested_symbol} strategy={_strat} reason={_wait_reason}",
                        {**_slim, "reason": _wait_reason},
                    )
                elif _cand.get("demo_eligible"):
                    self.ingest_client.emit_bot_log(
                        "SETUP_HUNTER_IN",
                        f"symbol={requested_symbol} strategy={_strat} direction={_dir}",
                        _slim,
                    )
                    self.ingest_client.emit_bot_log(
                        "SETUP_HUNTER_RAW_ACCEPT",
                        f"symbol={requested_symbol} strategy={_strat} direction={_dir} raw_grade={_cand.get('grade')} accepted_for_execution=false",
                        _slim,
                    )
                    if _lovable_btc_profile and (_strat or "").upper() in _OLD_BTC_STRATEGIES:
                        log.info(
                            "[EXEC_TRACE_SETUP_ACCEPT] trace_id=%s strategy=%s direction=%s symbol=%s",
                            _trace_id, _strat, _dir, requested_symbol,
                        )
                else:
                    self.ingest_client.emit_bot_log(
                        "SETUP_HUNTER_IN",
                        f"symbol={requested_symbol} strategy={_strat} direction={_dir}",
                        _slim,
                    )
                    _rej_reason = _failed[0] if _failed else "UNKNOWN"
                    self.ingest_client.emit_bot_log(
                        "CANDIDATE_REJECT",
                        f"symbol={requested_symbol} strategy={_strat} reason={_rej_reason}",
                        {**_slim, "reason": _rej_reason},
                    )
                    self.ingest_client.emit_bot_log(
                        "SETUP_HUNTER_REJECT",
                        f"symbol={requested_symbol} strategy={_strat} reason={_rej_reason}",
                        {**_slim, "reason": _rej_reason},
                    )
                # GOLD / EUR candidate tokens
                if _gold_sym and str(_strat or "").upper() in {
                    "GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_M1_M5_EMA_SWEEP_SCALPER",
                    "GOLD_ORDER_FLOW_CVD_VWAP", "ORDER_FLOW_EXECUTION_AGENT",
                }:
                    self.ingest_client.emit_bot_log(
                        "GOLD_CANDIDATE",
                        f"strategy={_strat} direction={_dir} score={_cand.get('edge_score')}",
                        _slim,
                    )
                if _eur_sym and str(_strat or "").upper() == "EUR_EMA_RSI_ATR_CROSSOVER":
                    self.ingest_client.emit_bot_log(
                        "EUR_CANDIDATE",
                        f"strategy={_strat} direction={_dir} score={_cand.get('edge_score')}",
                        _slim,
                    )
                # CONFIRMATION_MATRIX token derived from candidate data
                _hard_block = "CONFIRMATION_MATRIX_HARD_BLOCK" in _failed
                _smc = float(_cand.get("smc_score") or 0)
                _mtfa = float(_cand.get("mtfa_score") or 0)
                _smc_soft = 40.0 <= _smc < 70.0
                _mtfa_soft = 35.0 <= _mtfa < 60.0
                _cm_token = (
                    "CONFIRMATION_BLOCK" if _hard_block
                    else ("CONFIRMATION_WARNING" if (_smc_soft or _mtfa_soft) else "CONFIRMATION_MATRIX")
                )
                self.ingest_client.emit_bot_log(
                    _cm_token,
                    f"symbol={requested_symbol} strategy={_strat} smc={_smc:.1f} mtfa={_mtfa:.1f}",
                    {
                        "symbol": requested_symbol,
                        "strategy": _strat,
                        "smc_score": _smc,
                        "mtfa_score": _mtfa,
                        "hard_block": _hard_block,
                        "status": "BLOCK" if _hard_block else ("WARN" if (_smc_soft or _mtfa_soft) else "PASS"),
                    },
                )

            # SAFETY_GUARD — based on best candidate's safety_guard_status
            _sg_status = (hunter.best_candidate or {}).get("safety_guard_status")
            _sg_reason = (hunter.best_candidate or {}).get("safety_guard_reason")
            if _sg_status:
                self.ingest_client.emit_bot_log(
                    "SAFETY_GUARD",
                    f"symbol={requested_symbol} status={_sg_status} reason={_sg_reason}",
                    {
                        "symbol": requested_symbol,
                        "broker_symbol": broker_symbol,
                        "status": _sg_status,
                        "reason": _sg_reason,
                        "rules_triggered": (hunter.best_candidate or {}).get("safety_guard_rules_triggered") or [],
                    },
                )
                if _sg_status == "PASS" and _lovable_btc_profile:
                    log.info(
                        "[EXEC_TRACE_SAFETY_PASS] trace_id=%s symbol=%s strategy=%s",
                        _trace_id,
                        requested_symbol,
                        (hunter.best_candidate or {}).get("best_strategy", ""),
                    )
                self._latest_safety_guard = {
                    "last_status": _sg_status,
                    "last_reason": _sg_reason,
                    "last_update_utc": utc_now_iso(),
                }

            # §v1.5-fix: SETUP_HUNTER emit and route_status deferred to after confluence gate
            # — see "ONE authoritative verdict" block below.
            # Update per-symbol state for dashboard symbols block (route_status corrected post-confluence)
            _best_pre = hunter.best_candidate or {}
            self._per_symbol_state[str(requested_symbol or "").upper()] = {
                "price": (tick or {}).get("bid"),
                "spread": latest_spread,
                "spread_status": "OK" if latest_spread <= effective_max_spread else "MAX_SPREAD",
                "session": time_snapshot.get("session_name"),
                "time_gate": time_snapshot.get("time_gate_status"),
                "latest_decision": None,  # filled after routing below
                "latest_reason": _best_pre.get("near_miss_reason"),
                "route_status": "PENDING",  # updated to final value below
                "last_update_utc": utc_now_iso(),
            }
            # Inject ORDER_FLOW_EXECUTION_AGENT output into analysis for confluence engine (all symbols)
            _of_sig = next(
                (s for s in (analysis.get("strategy_signals") or [])
                 if str((s or {}).get("strategy") or "").upper() == "ORDER_FLOW_EXECUTION_AGENT"),
                {},
            )
            if _of_sig:
                _of_grade = str(_of_sig.get("grade") or "").upper()
                _of_score = float(_of_sig.get("confidence") or _of_sig.get("score") or _of_sig.get("edge_score") or 0)
                _of_signal = str(_of_sig.get("signal") or "").upper()
                analysis["order_flow_reader"] = {"grade": _of_grade, "score": _of_score, "signal": _of_signal}
                if str(requested_symbol or "").upper() in {"BTCUSD#", "BTCUSD"}:
                    _btc_of_bonus = 0.0
                    if _of_grade in {"A", "A+"} and _of_score >= 75:
                        _btc_of_bonus = 8.0 + (_of_score - 75) * 0.4
                    elif _of_grade == "B" and _of_score >= 65:
                        _btc_of_bonus = 4.0 + (_of_score - 65) * 0.2
                    if _btc_of_bonus > 0:
                        analysis["btc_order_flow_bonus"] = _btc_of_bonus
                        log.info(
                            "[CONFLUENCE_BTC_OF_BONUS] symbol=%s of_grade=%s of_score=%s bonus=%.2f",
                            requested_symbol, _of_grade, _of_score, _btc_of_bonus,
                        )

            # Enrich best candidate with confluence score; also used by final confluence gate below
            _conf_strategy = str((hunter.best_candidate or {}).get("best_strategy") or "")
            _of_native_strats = frozenset({
                "ORDER_FLOW_EXECUTION_AGENT", "GOLD_LIQUIDITY_HUNTER_PRO",
                "BTC_SCALPING_AGENT", "GOLD_ORDER_FLOW_CVD_VWAP",
            })
            _conf_strat_aware = (
                getattr(self.settings, "hermes_confluence_strategy_aware", False) is True
                or str(_conf_strategy or "").upper() in _of_native_strats
            )
            _conf: dict = {}
            # P0-C (2026-07-14) : le moteur de confluence est FAIL-CLOSED.
            #
            # Avant, l'exception etait avalee par un `except Exception: pass` et `_conf`
            # restait {}. Or TOUT le FINAL CONFLUENCE GATE est conditionne par
            # `bool(_conf)` (voir _conf_blocks_route plus bas) : une exception dans le
            # moteur DESARMAIT donc integralement le gate de confluence, en silence,
            # sans un seul log. Un setup de grade D / score 0 routait alors sans le
            # moindre controle de confluence. C'etait l'exception avalee la plus
            # dangereuse du depot.
            #
            # Desormais : si le moteur tombe, on BLOQUE. Un cycle perdu vaut mieux
            # qu'un ordre envoye sans controle.
            _conf_engine_failed = False
            if _conf_strategy and _conf_strategy not in ("NONE", ""):
                try:
                    _conf_weights = {
                        "geo": getattr(self.settings, "confluence_weight_geo", 0.25),
                        "smc": getattr(self.settings, "confluence_weight_smc", 0.25),
                        "mtfa": getattr(self.settings, "confluence_weight_mtfa", 0.20),
                        "of": getattr(self.settings, "confluence_weight_of", 0.30),
                    }
                    _conf = evaluate_confluence(
                        requested_symbol, _conf_strategy, frames, analysis,
                        strategy_aware=_conf_strat_aware, weights=_conf_weights,
                    )
                    self.latest_setup_hunter = {**hunter.best_candidate, **_conf}
                except Exception as _conf_exc:
                    _conf_engine_failed = True
                    log.critical(
                        "[CONFLUENCE_ENGINE_FAILED] symbol=%s strategy=%s error=%s — "
                        "FAIL-CLOSED : aucun ordre ne partira sur ce cycle. Le gate de "
                        "confluence ne peut pas etre desarme par une panne.",
                        requested_symbol, _conf_strategy, str(_conf_exc)[:200],
                    )
                    try:
                        from app.services.mt5_position_sync import send_critical_alert
                        send_critical_alert(
                            "HERMES CRITIQUE : le moteur de confluence a leve une exception "
                            f"({requested_symbol}/{_conf_strategy}) : {str(_conf_exc)[:120]}. "
                            "Trading BLOQUE sur ce symbole (fail-closed).",
                            cooldown_key="CONFLUENCE_ENGINE_FAILED",
                        )
                    except Exception:
                        pass
            # §v1.6: Geometric confluence layer (SHADOW by default; ACTIVE via config)
            _geo_result: dict = {}
            _geo_bonus: float = 0.0
            try:
                from app.mt5.geometric_confluence import (
                    analyze_geometric_confluence as _analyze_geo,
                    compute_geometric_bonus as _compute_geo_bonus,
                )
                _geo_mode_cfg = str(
                    getattr(self.settings, "geometric_confluence_mode", "SHADOW") or "SHADOW"
                ).strip().upper()
                _geo_tol = float(getattr(self.settings, "geometric_ratio_tolerance", 0.05) or 0.05)
                _geo_dir = str((hunter.best_candidate or {}).get("direction") or "").upper()
                _geo_result = _analyze_geo(
                    requested_symbol,
                    _geo_dir,
                    frames.get("M1"),
                    frames.get("M5"),
                    frames.get("M15"),
                    frames.get("H1"),
                    setup_context=hunter.best_candidate,
                    mode=_geo_mode_cfg,
                    ratio_tolerance=_geo_tol,
                )
                # SMC hard-block guard: bonus never overrides CONFIRMATION_MATRIX_HARD_BLOCK
                _geo_smc_blocked = "CONFIRMATION_MATRIX_HARD_BLOCK" in (
                    (hunter.best_candidate or {}).get("failed_gates") or []
                )
                _geo_confirm_bonus_cfg = float(
                    getattr(self.settings, "geometric_confirm_bonus", 5.0) or 5.0
                )
                _geo_bonus = _compute_geo_bonus(
                    _geo_result, _geo_smc_blocked, _geo_confirm_bonus_cfg
                )
                # Apply bonus to confluence score BEFORE final gate (ACTIVE mode only; capped at 100)
                if _geo_bonus > 0.0 and bool(_conf):
                    _old_conf_score = float(_conf.get("score") or 0.0)
                    _conf["score"] = min(100.0, _old_conf_score + _geo_bonus)
                    # COEUR_V2: apply the same override to legacy_score so the
                    # old-vs-new dataset comparison stays apples-to-apples (both
                    # formulas receive the same post-hoc overrides, as before).
                    if _conf.get("legacy_score") is not None:
                        _conf["legacy_score"] = min(100.0, float(_conf["legacy_score"]) + _geo_bonus)
                    log.info(
                        "[GEO_BONUS_APPLIED] symbol=%s old_score=%.1f bonus=%.1f"
                        " new_score=%.1f mode=ACTIVE",
                        requested_symbol, _old_conf_score, _geo_bonus, _conf["score"],
                    )
                # Propagate key fields into best_candidate for dashboard
                if isinstance(hunter.best_candidate, dict):
                    hunter.best_candidate.update({
                        "geo_result":                _geo_result,
                        "geometric_score":           _geo_result.get("geometric_score"),
                        "geometric_grade":           _geo_result.get("geometric_grade"),
                        "geometric_decision":        _geo_result.get("decision"),
                        "harmonic_pattern":          _geo_result.get("harmonic_pattern"),
                        "geometric_confluence_mode": _geo_mode_cfg,
                        "geo_would_be_bonus":        _geo_confirm_bonus_cfg
                            if str(_geo_result.get("decision") or "").upper() == "CONFIRM"
                            else (_geo_confirm_bonus_cfg / 2.0
                                  if str(_geo_result.get("decision") or "").upper() == "WEAK_CONFIRM"
                                  else 0.0),
                    })
            except Exception as _geo_exc:
                log.warning(
                    "[GEOMETRIC_CONFLUENCE] symbol=%s error=%s",
                    requested_symbol, str(_geo_exc)[:200],
                )

            decision = _routeable_setup_hunter_decision(hunter.decision, hunter.best_candidate)
            if _gold_handoff_blocked(hunter.best_candidate, requested_symbol, broker_symbol):
                decision = {
                    **decision,
                    "decision": "WAIT_ANALYSIS_ONLY",
                    "signal": "WAIT",
                    "direction": "WAIT",
                    "route_to_demo": False,
                    "reason": "NO_GOLD_EXECUTION_CANDIDATE",
                }
            if hunter.best_candidate.get("demo_eligible"):
                analysis["kelly_risk"] = self.agent.kelly.evaluate(
                    decision,
                    analysis.get("markov_prediction") or {},
                    account,
                    open_count,
                    latest_spread,
                    symbol_specs or {},
                    effective_max_spread,
                )
            self.latest_agent_state = {
                "active_symbol": requested_symbol,
                "timeframe": "M5",
                "latest_signal": decision.get("signal"),
                "confidence": decision.get("confidence"),
                "last_update": utc_now_iso(),
            }
            self.send_running_agent_update(self.latest_agent_state)
            # Pass confluence_engine score to adaptive_confluence (BTC RANGE mode / OF bonus aware)
            if _conf.get("score") is not None:
                decision.setdefault("final_confluence_score", _conf["score"])
            # COEUR_V2 chantier 1: capture both formulas in the dataset for the comparative
            # replay window (see MATH_CORE_AUDIT.md RÉSERVÉ SIMO n°1 / COEUR_V2_REPORT.md).
            if _conf.get("score") is not None:
                decision.setdefault("new_confluence", _conf["score"])
                decision.setdefault("new_confluence_grade", _conf.get("grade"))
            if _conf.get("legacy_score") is not None:
                decision.setdefault("old_confluence", _conf["legacy_score"])
                decision.setdefault("old_confluence_grade", _conf.get("legacy_grade"))

            # Enrich ai_decisions with canonical fields before writing
            _best_cand = hunter.best_candidate or {}
            decision.setdefault("confidence", decision.get("edge_score") or decision.get("setup_hunter_score"))
            decision.setdefault("grade", _best_cand.get("grade") or decision.get("setup_hunter_grade"))
            decision.setdefault("route_status", "ROUTE_TO_DEMO" if _best_cand.get("demo_eligible") else "WAIT")
            decision.setdefault("candidate_valid", bool(_best_cand.get("demo_eligible")))
            decision.setdefault("setup_type", _best_cand.get("best_strategy") or decision.get("strategy"))
            decision.setdefault("utc_time", utc_now_iso())
            decision.setdefault("raw_payload", {
                "best_strategy": _best_cand.get("best_strategy"),
                "direction": _best_cand.get("direction"),
                "grade": _best_cand.get("grade"),
                "score": _best_cand.get("edge_score"),
                "near_miss_reason": _best_cand.get("near_miss_reason"),
                "failed_gates": (_best_cand.get("failed_gates") or [])[:4],
                "entry": _best_cand.get("entry"),
                "sl": _best_cand.get("sl"),
                "tp": _best_cand.get("tp"),
                "rr": _best_cand.get("rr"),
            })
            decision_result = self.ingest_client.send_row("ai_decisions", decision)
            if decision_result.get("ok"):
                log.info("AI decision written")

            opened_candle_time = self._latest_completed_candle_time(frames["M5"])
            setup_id = _trace_id
            paper_items = self.paper_trader.process_decision(
                decision,
                latest_spread,
                opened_candle_time,
                effective_max_spread,
                setup_id,
                broker_symbol,
                symbol_specs,
                equity,
            )
            self.record_learning_setup(requested_symbol, broker_symbol, analysis, decision, paper_items, latest_spread, setup_id)
            paper_opened += sum(1 for item in paper_items if item.get("paper_action") == "OPEN_TRADE")
            self.write_ingest_items(paper_items)
            # ORDER_FLOW_EXEC_AGENT token when OF execution agent is in play
            if str(decision.get("strategy") or "").upper() == "ORDER_FLOW_EXECUTION_AGENT":
                self.ingest_client.emit_bot_log(
                    "ORDER_FLOW_EXEC_AGENT",
                    f"symbol={broker_symbol} direction={decision.get('direction')} score={decision.get('edge_score')}",
                    {
                        "symbol": broker_symbol,
                        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
                        "direction": decision.get("direction"),
                        "score": decision.get("edge_score"),
                        "grade": decision.get("grade"),
                        "demo_eligible": bool((hunter.best_candidate or {}).get("demo_eligible")),
                        "route_allowed": decision.get("route_to_demo"),
                    },
                )

            force_active_handoff = _is_demo_eligible_active_candidate(hunter.best_candidate)
            handoff_strategy = str((hunter.best_candidate or {}).get("best_strategy") or decision.get("strategy") or "").upper()
            handoff_missing_field = _active_handoff_missing_field(decision, hunter.best_candidate, analysis.get("kelly_risk"), broker_symbol)
            gold_handoff_blocked = _gold_handoff_blocked(hunter.best_candidate, requested_symbol, broker_symbol)

            # Open position guard + entry gate — run before DemoRouter for OLD BTC profile.
            # Blocks all new entries if ≥ old_btc_max_open_positions HERMES BTC positions are open.
            if _lovable_btc_profile and force_active_handoff:
                try:
                    import MetaTrader5 as _mt5_opg
                    from app.mt5.smart_rescue import count_hermes_btc_open as _count_btc_opg
                    from app.mt5.btc_entry_gate import evaluate_btc_entry_gate as _eval_gate
                    _opg_positions = list(_mt5_opg.positions_get() or [])
                    _opg_count = _count_btc_opg(_opg_positions, int(getattr(self.settings, "demo_magic_number", 909002)))
                    _opg_max = int(getattr(self.settings, "old_btc_max_open_positions", 1) or 1)
                    if _opg_count >= _opg_max:
                        log.info(
                            "[OLD_BTC_OPEN_POSITION_GUARD] decision=BLOCK "
                            "reason=HERMES_BTC_POSITION_ALREADY_OPEN count=%s",
                            _opg_count,
                        )
                        force_active_handoff = False
                        decision["decision"] = "WAIT_ANALYSIS_ONLY"
                        decision["route_to_demo"] = False
                    elif bool(getattr(self.settings, "old_btc_entry_gate_enabled", True)):
                        _gate_sym = str(broker_symbol or requested_symbol or "").upper()
                        _gate_strat = handoff_strategy
                        _gate_dir = str(decision.get("signal") or decision.get("direction") or "").upper()
                        _gate_conf = float(
                            (hunter.best_candidate or {}).get("normalized_confidence")
                            or (hunter.best_candidate or {}).get("edge_score")
                            or decision.get("confidence") or 0
                        ) or None
                        _of_snap = self.latest_order_flow_snapshots.get(_gate_sym) or self.latest_order_flow_snapshots.get("BTCUSD")
                        _gate_result = _eval_gate(
                            symbol=_gate_sym,
                            strategy=_gate_strat,
                            direction=_gate_dir,
                            market_context=_of_snap,
                            settings=self.settings,
                            open_btc_count=_opg_count,
                            confidence=_gate_conf,
                        )
                        _gate_dec = str(_gate_result.get("decision") or "BLOCK").upper()
                        _gate_rsn = str(_gate_result.get("reason") or "ENTRY_GATE_BLOCK")
                        _gate_scr = _gate_result.get("score")
                        log.info(
                            "[OLD_BTC_ENTRY_GATE] strategy=%s direction=%s decision=%s%s",
                            _gate_strat, _gate_dir, _gate_dec,
                            f" reason={_gate_rsn}" if _gate_dec == "BLOCK" else f" score={_gate_scr}",
                        )
                        if _gate_dec == "BLOCK":
                            force_active_handoff = False
                            decision["decision"] = "WAIT_ANALYSIS_ONLY"
                            decision["route_to_demo"] = False
                except Exception as _opg_exc:
                    log.warning("[OLD_BTC_OPEN_POSITION_GUARD] error=%s decision=BLOCK reason=MT5_POSITION_READ_FAILED", str(_opg_exc)[:200])
                    log.info("[OLD_BTC_OPEN_POSITION_GUARD] decision=BLOCK reason=MT5_POSITION_READ_FAILED")
                    force_active_handoff = False
                    decision["decision"] = "WAIT_ANALYSIS_ONLY"
                    decision["route_to_demo"] = False

            # LOVABLE_BTC_OLD_SYSTEM: bypass both confluence and confirmation-matrix blocks
            # for the two old BTC strategies after SETUP_HUNTER_ACCEPT + SAFETY_GUARD PASS.
            _old_btc_route_bypass = (
                _lovable_btc_profile
                and force_active_handoff
                and handoff_strategy in _OLD_BTC_STRATEGIES
                and str((hunter.best_candidate or {}).get("safety_guard_status") or "").upper() == "PASS"
            )
            if _old_btc_route_bypass:
                _bypass_mode = _old_btc_mode_for_strategy(handoff_strategy)
                log.info(
                    "[OLD_BTC_ROUTE] strategy=%s mode=%s setup_hunter=ACCEPT safety=PASS "
                    "smc_mtfa_strict_bypass=true topdown_bypass=true final_confluence_bypass=true trace_id=%s",
                    handoff_strategy, _bypass_mode, _trace_id,
                )
                log.info(
                    "[EXEC_TRACE_OLD_BTC_BYPASS] trace_id=%s strategy=%s mode=%s",
                    _trace_id, handoff_strategy, _bypass_mode,
                )
                self.ingest_client.emit_bot_log(
                    "OLD_BTC_ROUTE",
                    f"strategy={handoff_strategy} mode={_bypass_mode} setup_hunter=ACCEPT safety=PASS "
                    f"smc_mtfa_strict_bypass=true topdown_bypass=true final_confluence_bypass=true trace_id={_trace_id}",
                    {
                        "strategy": handoff_strategy,
                        "mode": _bypass_mode,
                        "setup_hunter": "ACCEPT",
                        "safety": "PASS",
                        "smc_mtfa_strict_bypass": True,
                        "topdown_bypass": True,
                        "final_confluence_bypass": True,
                        "trace_id": _trace_id,
                    },
                )

            # FINAL CONFLUENCE GATE — applies before any ROUTER_HANDOFF
            _final_conf_score = float(_conf.get("score") or 0.0)
            _final_conf_grade = str(_conf.get("grade") or "")
            _research_allow = bool(getattr(self.settings, "research_allow_low_confluence", False))
            # §4.4 v1.4: per-strategy thresholds when hermes_confluence_strategy_aware is True
            if _conf_strat_aware and bool(_conf):
                _sclass = str(_conf.get("strategy_class") or "DEFAULT").upper()
                if _sclass == "ORDER_FLOW_NATIVE":
                    _conf_threshold = 58.0
                elif _sclass == "SMC_NATIVE":
                    _conf_threshold = 65.0
                else:
                    _conf_threshold = 62.0
            else:
                _conf_threshold = 55.0
            # P0-C : une panne du moteur bloque le routage, sans condition. Aucun mode
            # (research_allow, old_btc bypass) ne peut lever une panne — on ne sait tout
            # simplement pas si le setup est bon.
            if _conf_engine_failed:
                force_active_handoff = False
                decision["decision"] = "WAIT_ANALYSIS_ONLY"
                decision["route_to_demo"] = False
                if isinstance(hunter.best_candidate, dict):
                    hunter.best_candidate["final_verdict"] = "BLOCK"
                    hunter.best_candidate["final_verdict_reason"] = "CONFLUENCE_ENGINE_FAILED"
                    hunter.best_candidate["demo_eligible"] = False

            _conf_blocks_route = _conf_engine_failed or (
                bool(_conf)
                and (_final_conf_grade == "D" or _final_conf_score < _conf_threshold)
                and not _research_allow
                and not _old_btc_route_bypass
            )
            if bool(_conf) and (_final_conf_grade == "D" or _final_conf_score < _conf_threshold) and not _old_btc_route_bypass:
                if _research_allow:
                    log.info("[ROUTER_OVERRIDE] symbol=%s reason=RESEARCH_ALLOW_LOW_CONFLUENCE", broker_symbol)
                    self.ingest_client.emit_bot_log(
                        "ROUTER_OVERRIDE",
                        f"symbol={broker_symbol} reason=RESEARCH_ALLOW_LOW_CONFLUENCE",
                        {"symbol": broker_symbol, "reason": "RESEARCH_ALLOW_LOW_CONFLUENCE"},
                    )
                else:
                    log.info(
                        "[ROUTER_BLOCK] symbol=%s strategy=%s reason=FINAL_CONFLUENCE_TOO_LOW score=%s grade=%s threshold=%s strat_aware=%s",
                        broker_symbol, handoff_strategy, _final_conf_score, _final_conf_grade,
                        _conf_threshold, _conf_strat_aware,
                    )
                    self.ingest_client.emit_bot_log(
                        "ROUTER_BLOCK",
                        f"symbol={broker_symbol} strategy={handoff_strategy} reason=FINAL_CONFLUENCE_TOO_LOW score={_final_conf_score} grade={_final_conf_grade}",
                        {
                            "symbol": broker_symbol,
                            "strategy": handoff_strategy,
                            "reason": "FINAL_CONFLUENCE_TOO_LOW",
                            "score": _final_conf_score,
                            "grade": _final_conf_grade,
                        },
                    )
                    force_active_handoff = False
                    decision["decision"] = "WAIT_ANALYSIS_ONLY"
                    decision["route_to_demo"] = False
                    # §v1.5: update candidate — log emitted once below after this block
                    if isinstance(hunter.best_candidate, dict):
                        hunter.best_candidate["final_verdict"] = "BLOCK"
                        hunter.best_candidate["final_verdict_reason"] = "FINAL_CONFLUENCE_TOO_LOW"
                        hunter.best_candidate["demo_eligible"] = False

            # §v1.5-fix: ONE authoritative [FINAL_VERDICT] per candidate, after confluence gate
            _best_post = hunter.best_candidate or {}
            _de_final = bool(_best_post.get("demo_eligible"))
            _fv_final = str(_best_post.get("final_verdict") or ("PASS" if _de_final else "BLOCK"))
            _fv_rsn = _best_post.get("final_verdict_reason") or ("NONE" if _fv_final == "PASS" else "GATES_FAILED")
            _raw_g_final = _best_post.get("raw_strategy_grade") or _best_post.get("grade")
            _ml_st_final = _best_post.get("ml_status") or "UNAVAILABLE"
            _btc_sym_fvl2 = str(requested_symbol or "").upper().replace("#", "").startswith("BTCUSD")
            _fv_tag_final = "[BTC_FINAL_VERDICT]" if _btc_sym_fvl2 else "[FINAL_VERDICT]"
            log.info(
                "%s symbol=%s raw_grade=%s final_grade=%s decision=%s demo_eligible=%s reason=%s ml_status=%s",
                _fv_tag_final, broker_symbol or requested_symbol, _raw_g_final,
                _final_conf_grade or "NONE", _fv_final, _de_final, _fv_rsn, _ml_st_final,
            )
            if _de_final:
                log.info(
                    "[SETUP_HUNTER_ACCEPT] symbol=%s strategy=%s final_grade=%s decision=PASS accepted_for_execution=true",
                    broker_symbol or requested_symbol,
                    _best_post.get("best_strategy"),
                    _final_conf_grade or _best_post.get("grade"),
                )
            # SETUP_HUNTER telemetry — emitted here so demo_eligible reflects post-confluence state
            self.ingest_client.emit_bot_log(
                "SETUP_HUNTER",
                f"symbol={requested_symbol} best={_best_post.get('best_strategy')} demo_eligible={_de_final} grade={_best_post.get('grade')}",
                {
                    "symbol": requested_symbol,
                    "broker_symbol": broker_symbol,
                    "best_strategy": _best_post.get("best_strategy"),
                    "direction": _best_post.get("direction"),
                    "demo_eligible": _de_final,
                    "grade": _best_post.get("grade"),
                    "raw_strategy_grade": _best_post.get("raw_strategy_grade"),
                    "final_verdict": _fv_final,
                    "score": _best_post.get("edge_score"),
                    "near_miss_reason": _best_post.get("near_miss_reason"),
                    "failed_gates": (_best_post.get("failed_gates") or [])[:4],
                    "entry": _best_post.get("entry"),
                    "sl": _best_post.get("sl"),
                    "tp": _best_post.get("tp"),
                    "rr": _best_post.get("rr"),
                },
            )
            # Update route_status to final post-confluence value
            _sym_key_post = str(requested_symbol or "").upper()
            if _sym_key_post in self._per_symbol_state:
                self._per_symbol_state[_sym_key_post]["route_status"] = (
                    "ROUTE_TO_DEMO" if _de_final else "WAIT"
                )

            # §v1.6: Geometric final verdict input + shadow evaluation log
            if _geo_result:
                _geo_grade_post  = str(_geo_result.get("geometric_grade") or "D")
                _geo_dec_post    = str(_geo_result.get("decision") or "WAIT").upper()
                _geo_mode_post   = str(_geo_result.get("mode") or "SHADOW").upper()
                _geo_wb_bonus    = float(_best_post.get("geo_would_be_bonus") or 0.0)
                log.info(
                    "[GEOMETRIC_FINAL_VERDICT_INPUT] symbol=%s geometric_grade=%s"
                    " final_bonus=%.1f mode=%s",
                    requested_symbol, _geo_grade_post, _geo_bonus, _geo_mode_post,
                )
                log.info(
                    "[GEO_SHADOW] ts=%s symbol=%s direction=%s geometric_grade=%s"
                    " geometric_decision=%s would_be_bonus=%.1f setup_final_decision=%s"
                    " position_id=%s",
                    utc_now_iso(), requested_symbol,
                    _geo_result.get("direction") or _geo_dir,
                    _geo_grade_post, _geo_dec_post,
                    _geo_wb_bonus, _fv_final, "NONE",
                )

            if force_active_handoff:
                _old_btc_mode_inject = _old_btc_mode_for_strategy(handoff_strategy) if (_lovable_btc_profile and handoff_strategy in _OLD_BTC_STRATEGIES) else None
                if _old_btc_mode_inject:
                    decision["old_btc_mode"] = _old_btc_mode_inject
                    decision["trace_id"] = _trace_id
                selector_candidate = {
                    **(hunter.best_candidate or {}),
                    "final_confluence_score": _final_conf_score if _conf else (hunter.best_candidate or {}).get("final_confluence_score"),
                    "final_confluence_grade": _final_conf_grade if _conf else (hunter.best_candidate or {}).get("final_confluence_grade") or (hunter.best_candidate or {}).get("grade"),
                    "spread_ok": latest_spread <= effective_max_spread,
                    "market_open": time_snapshot.get("symbol_market_open", time_snapshot.get("market_open", True)),
                    "time_gate_status": time_snapshot.get("time_gate_status"),
                    "mode": "ACTIVE_EXECUTION",
                    "route_allowed": True,
                    "demo_eligible": True,
                    **({"old_btc_mode": _old_btc_mode_inject} if _old_btc_mode_inject else {}),
                }
                selection = select_best_candidate(
                    [selector_candidate],
                    [],
                    max_total_open=int(getattr(self.settings, "demo_max_open_trades_total", 3) or 3),
                    magic_number=int(getattr(self.settings, "demo_magic_number", getattr(self.settings, "hermes_magic_number", 0)) or 0),
                )
                if selection.best is None:
                    reason = (selection.rejected[0].get("balanced_selector_reason") if selection.rejected else "BALANCED_SELECTOR_REJECT")
                    log.info("[ROUTER_HANDOFF] decision=BLOCK strategy=%s reason=%s", handoff_strategy, reason)
                    self.ingest_client.emit_bot_log(
                        "BALANCED_SELECTOR_REJECT",
                        f"symbol={broker_symbol} strategy={handoff_strategy} reason={reason}",
                        {"symbol": broker_symbol, "strategy": handoff_strategy, "reason": reason},
                    )
                    force_active_handoff = False
                    decision["decision"] = "WAIT_ANALYSIS_ONLY"
                    decision["route_to_demo"] = False

            if gold_handoff_blocked:
                log.info(
                    "[GOLD_ROUTER] decision=WAIT reason=NO_GOLD_EXECUTION_CANDIDATE symbol=%s",
                    broker_symbol,
                )
                demo_items = []
                decision["decision"] = "WAIT_ANALYSIS_ONLY"
                decision["route_to_demo"] = False
            elif force_active_handoff and handoff_missing_field:
                log.info("[ROUTER_HANDOFF] decision=BLOCK strategy=%s reason=MISSING_FIELD field=%s", handoff_strategy, handoff_missing_field)
                self.ingest_client.emit_bot_log(
                    "ROUTER_HANDOFF",
                    f"symbol={broker_symbol} strategy={handoff_strategy} decision=BLOCK reason=MISSING_FIELD field={handoff_missing_field}",
                    {
                        "symbol": broker_symbol, "strategy": handoff_strategy,
                        "decision": "BLOCK", "reason": "MISSING_FIELD", "field": handoff_missing_field,
                    },
                )
                demo_items = []
            elif force_active_handoff:
                _direction_up = str(decision.get("signal") or decision.get("direction") or "").upper()
                log.info(
                    "[ROUTER_HANDOFF] symbol=%s strategy=%s decision=SEND_TO_DEMO_ROUTER direction=%s",
                    broker_symbol, handoff_strategy, _direction_up,
                )
                self.ingest_client.emit_bot_log(
                    "ROUTER_HANDOFF",
                    f"symbol={broker_symbol} strategy={handoff_strategy} decision=SEND_TO_DEMO_ROUTER direction={_direction_up}",
                    {
                        "symbol": broker_symbol, "strategy": handoff_strategy,
                        "decision": "SEND_TO_DEMO_ROUTER", "direction": _direction_up,
                        "entry": decision.get("entry"), "sl": decision.get("sl"), "tp": decision.get("tp"),
                    },
                )
                demo_items = self.demo_router.process_decision(
                    decision,
                    analysis.get("kelly_risk"),
                    account,
                    broker_symbol,
                    frames,
                    tick,
                    symbol_specs,
                    latest_spread,
                    effective_max_spread,
                    self.mt5.connected,
                    setup_id,
                )
            elif self._should_route_to_demo(decision) and not _conf_blocks_route:
                _direction_up = str(decision.get("signal") or decision.get("direction") or "").upper()
                if str(decision.get("strategy") or "").upper() == "GOLD_ORDER_FLOW_CVD_VWAP":
                    log.info(
                        "[ROUTER_HANDOFF] symbol=%s strategy=GOLD_ORDER_FLOW_CVD_VWAP decision=SEND_TO_DEMO_ROUTER direction=%s",
                        broker_symbol, _direction_up,
                    )
                self.ingest_client.emit_bot_log(
                    "ROUTER_HANDOFF",
                    f"symbol={broker_symbol} strategy={decision.get('strategy')} decision=SEND_TO_DEMO_ROUTER direction={_direction_up}",
                    {
                        "symbol": broker_symbol, "strategy": decision.get("strategy"),
                        "decision": "SEND_TO_DEMO_ROUTER", "direction": _direction_up,
                    },
                )
                demo_items = self.demo_router.process_decision(
                    decision,
                    analysis.get("kelly_risk"),
                    account,
                    broker_symbol,
                    frames,
                    tick,
                    symbol_specs,
                    latest_spread,
                    effective_max_spread,
                    self.mt5.connected,
                    setup_id,
                )
            else:
                demo_items = []
            demo_orders += sum(1 for item in demo_items if item.get("demo_action") == "DEMO_ORDER")
            latest_demo_event = next((item.get("data") for item in demo_items if isinstance(item.get("data"), dict)), None)
            if isinstance(latest_demo_event, dict):
                self.latest_demo_event = latest_demo_event
            self.write_ingest_items(demo_items)

            # DEMO_ORDER token when an order was placed
            _demo_order_item = next((item for item in demo_items if item.get("demo_action") == "DEMO_ORDER"), None)
            if _demo_order_item:
                _do_data = _demo_order_item.get("data") or {}
                self.ingest_client.emit_bot_log(
                    "DEMO_ORDER",
                    f"symbol={broker_symbol} ticket={_do_data.get('ticket')} direction={_do_data.get('direction')}",
                    {
                        "symbol": broker_symbol,
                        "strategy": _do_data.get("strategy"),
                        "direction": _do_data.get("direction"),
                        "lot": _do_data.get("final_capped_lot"),
                        "entry": _do_data.get("entry"),
                        "sl": _do_data.get("sl"),
                        "tp": _do_data.get("tp"),
                        "ticket": _do_data.get("ticket"),
                    },
                )

            # Update per-symbol state with final routing decision
            _sym_key = str(requested_symbol or "").upper()
            if _sym_key in self._per_symbol_state:
                self._per_symbol_state[_sym_key]["latest_decision"] = decision.get("decision")
                self._per_symbol_state[_sym_key]["route_status"] = (
                    "DEMO_ORDER" if _demo_order_item
                    else ("ROUTE_TO_DEMO" if decision.get("route_to_demo") else "WAIT")
                )
            analyzed += 1
            self._cycle_status["analyzed"] = analyzed
            self._cycle_status["skipped"] = skipped
            symbol_status = str(decision.get("decision") or "analyzed")
            log.info("[CYCLE] symbol=%s status=%s", requested_symbol, symbol_status)
            log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=%s", requested_symbol, symbol_status)

            log_result = self.ingest_client.send_row(
                "bot_logs",
                {
                    "level": "INFO",
                    "source": "HERMES_BACKEND",
                    "message": "Hermes analysis cycle complete",
                    "context": {"symbol": requested_symbol, "decision": decision.get("decision")},
                    "created_at": utc_now_iso(),
                },
            )
            if log_result.get("ok"):
                log.info("Hermes analysis cycle complete")

        if _lovable_btc_profile:
            simo_items, simo_counts = [], {"analyzed": 0, "skipped": 0}
        else:
            simo_items, simo_counts = self.run_simo_index_cycle()
        demo_orders += sum(1 for item in simo_items if item.get("demo_action") == "DEMO_ORDER")
        analyzed += simo_counts.get("analyzed", 0)
        skipped += simo_counts.get("skipped", 0)
        self._cycle_status["analyzed"] = analyzed
        self._cycle_status["skipped"] = skipped
        self.write_ingest_items(simo_items)

        self.ingest_client.send_row(
            "nightly_reports",
            {
                "report_date": utc_now_iso()[:10],
                "status": "INTRADAY_HEARTBEAT",
                "summary": "Hermes backend running in analysis-only mode",
                "created_at": utc_now_iso(),
            },
        )
        cycle_end_utc = datetime.now(timezone.utc)
        log.info(
            "[CYCLE] completed analyzed=%s skipped=%s paper_opened=%s paper_closed=%s demo_orders=%s",
            analyzed,
            skipped,
            paper_opened,
            paper_closed,
            demo_orders,
        )
        _cycle_candidates = [item for item in self._latest_candidates_all if isinstance(item, dict)]
        _blocked_by_confirmation = sum(
            1 for item in _cycle_candidates
            if "CONFIRMATION_MATRIX_HARD_BLOCK" in {
                *(item.get("failed_confirmations") or []),
                *(item.get("failed_gates") or []),
            }
        )
        _blocked_by_safety = sum(
            1 for item in _cycle_candidates
            if str(item.get("safety_guard_status") or "").upper() == "BLOCK"
        )
        _blocked_by_entry_guard = sum(
            1 for item in _cycle_candidates
            if any(
                gate in {
                    "ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B",
                    "ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65",
                }
                for gate in (item.get("failed_gates") or [])
            )
        )
        _analysis_only = sum(
            1 for item in self._per_symbol_state.values()
            if str((item or {}).get("latest_decision") or "").upper() == "WAIT_ANALYSIS_ONLY"
        )
        _summary_open = int(
            self.latest_position_sync.get("hermes_mt5_open_positions_count")
            or self.latest_position_sync.get("open_demo_trades_count")
            or 0
        )
        _summary_closed_pnl = float(self.latest_position_sync.get("demo_closed_pnl_today") or 0.0)
        _summary_floating_pnl = float(self.latest_position_sync.get("demo_floating_pnl") or 0.0)
        log.info(
            "[CYCLE_SUMMARY] analyzed=%s demo_orders=%s open=%s closed_pnl=%s floating_pnl=%s "
            "blocked_by_confirmation=%s blocked_by_safety=%s blocked_by_entry_guard=%s analysis_only=%s",
            analyzed, demo_orders, _summary_open, _summary_closed_pnl, _summary_floating_pnl,
            _blocked_by_confirmation, _blocked_by_safety, _blocked_by_entry_guard, _analysis_only,
        )
        self._cycle_status = {
            "last_cycle_start_utc": cycle_start_utc.isoformat(),
            "last_cycle_end_utc": cycle_end_utc.isoformat(),
            "analyzed": analyzed,
            "skipped": skipped,
            "demo_orders": demo_orders,
            "last_status": "COMPLETED",
        }
        self.ingest_client.emit_bot_log(
            "CYCLE",
            f"cycle_end analyzed={analyzed} skipped={skipped} demo_orders={demo_orders}",
            {
                "analyzed": analyzed,
                "skipped": skipped,
                "demo_orders": demo_orders,
                "last_cycle_start_utc": cycle_start_utc.isoformat(),
                "last_cycle_end_utc": cycle_end_utc.isoformat(),
            },
        )
        self.heartbeat.write(
            account, self.resolved_symbols, self.latest_agent_state,
            self.latest_demo_event, self.mt5.connected, self.latest_setup_hunter,
            self.latest_position_sync, self.latest_order_flow_snapshots,
            cycle_status=self._cycle_status,
            per_symbol_state=self._per_symbol_state,
            latest_candidates=self._latest_candidates_all,
            latest_safety_guard=self._latest_safety_guard,
            ingest_health=self._ingest_health_dict(),
        )

    def sync_open_mt5_positions_to_lovable(self, blocking: bool = True) -> dict:
        if not blocking and not self._live_snapshot_lock.acquire(blocking=False):
            if should_emit("position_sync_skipped_MAIN_CYCLE_ACTIVE"):
                log.warning("[LIVE_SNAPSHOT] position_sync_skipped reason=MAIN_CYCLE_ACTIVE")
            return {"skipped": True, "reason": "MAIN_CYCLE_ACTIVE"}
        if blocking:
            self._live_snapshot_lock.acquire()
        try:
            return sync_open_mt5_positions_to_lovable(
                self.settings,
                self.ingest_client,
                record_event=self.demo_router._record_event,
            )
        finally:
            self._live_snapshot_lock.release()

    def run_simo_index_cycle(self) -> tuple[list[dict], dict]:
        """Evaluate SIMO_ATM_BREAKOUT on index symbols (US100/NAS100) outside the main loop.

        Returns (items, counts) where counts has "analyzed" and "skipped" deltas so the
        caller can roll them into the cycle totals.  Every configured SIMO symbol always
        contributes exactly 1 to analyzed+skipped.
        """
        _simo_symbols_cfg = self.settings.simo_atm_symbol_list
        _simo_cycle_symbol = next(
            (s for s in self.settings.hermes_main_symbol_list
             if any(s.upper().startswith(t.upper().replace("#", "")) for t in _simo_symbols_cfg)),
            "US100Cash#",
        )
        _simo_key = str(_simo_cycle_symbol or "").upper()
        # Preserve previous cycle's tick data so the background-thread heartbeat
        # never sees a full reset while evaluation is in progress.  Decision/reason
        # fields are cleared to neutral; price/spread/session/time_gate carry forward.
        _prev_simo = self._per_symbol_state.get(_simo_key, {})
        self._per_symbol_state[_simo_key] = {
            "broker_symbol": _prev_simo.get("broker_symbol") or _simo_cycle_symbol,
            "price": _prev_simo.get("price"),
            "spread": _prev_simo.get("spread"),
            "spread_status": _prev_simo.get("spread_status"),
            "session": _prev_simo.get("session"),
            "time_gate": _prev_simo.get("time_gate"),
            "latest_decision": "WAIT",
            "latest_reason": "EVALUATING",
            "route_status": _prev_simo.get("route_status", "STARTING"),
            "last_update_utc": _prev_simo.get("last_update_utc") or utc_now_iso(),
        }
        _skipped: dict = {"analyzed": 0, "skipped": 1}
        _analyzed: dict = {"analyzed": 1, "skipped": 0}
        if not self.strategy_manager.simo_enabled:
            log.info("[SYMBOL_CYCLE_SKIP] symbol=%s reason=SIMO_DISABLED", _simo_cycle_symbol)
            self._per_symbol_state[_simo_key]["latest_reason"] = "SIMO_DISABLED"
            return [], _skipped
        if not self.mt5.connected:
            log.info("[SYMBOL_CYCLE_SKIP] symbol=%s reason=MT5_NOT_CONNECTED", _simo_cycle_symbol)
            self._per_symbol_state[_simo_key]["latest_reason"] = "MT5_NOT_CONNECTED"
            return [], _skipped

        log.info("[SYMBOL_CYCLE_IN] symbol=%s", _simo_cycle_symbol)
        broker_symbol = self.strategy_manager.refresh_simo_symbol()
        if not broker_symbol:
            log.info("[SIMO_ATM] skipped reason=NO_SUPPORTED_INDEX_SYMBOL")
            log.info("[SYMBOL_CYCLE_SKIP] symbol=%s reason=NO_SUPPORTED_INDEX_SYMBOL", _simo_cycle_symbol)
            log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=SKIPPED", _simo_cycle_symbol)
            self._per_symbol_state[_simo_key]["latest_reason"] = "NO_SUPPORTED_INDEX_SYMBOL"
            return [], _skipped

        self.strategy_manager.log_active_status()

        frames = self.reader.get_all_timeframes(broker_symbol, count=300)
        if frames.get("M5") is None or frames["M5"].empty:
            log.info("[SIMO_ATM] skipped reason=NO_M5_CANDLES symbol=%s", broker_symbol)
            log.info("[SYMBOL_CYCLE_SKIP] symbol=%s reason=NO_M5_CANDLES", _simo_cycle_symbol)
            log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=SKIPPED", _simo_cycle_symbol)
            self._per_symbol_state[_simo_key]["latest_reason"] = "NO_M5_CANDLES"
            return [], _skipped

        from app.strategies import simo_atm_breakout

        simo_result = simo_atm_breakout.evaluate(broker_symbol, frames, {}, self.settings)
        signal = str(simo_result.get("signal") or "").upper()
        reason = simo_result.get("reason") or simo_result.get("simo_atm_reason") or "NO_SIGNAL"
        log.info("[SIMO_ATM] symbol=%s signal=%s reason=%s", broker_symbol, signal, reason)

        # Emit ORDER_FLOW_READER telemetry for US100Cash# — identical store path used
        # by BTCUSD#/GOLD#/EURUSD from the main loop.  Observe-only; never routes
        # to DemoRouter or any execution path regardless of the simo signal.
        from app.strategies import gold_order_flow_cvd_vwap as _gof_mod
        _of_payload = _gof_mod.evaluate_observe_only(broker_symbol, frames, {}, self.settings)
        _of_canonical = _canonical_order_flow_symbol(broker_symbol)
        _of_row = {
            **_of_payload,
            "symbol": _of_canonical,
            "broker_symbol": broker_symbol,
            "raw_symbol": _simo_cycle_symbol,
            "mode": "OBSERVE_ONLY",
            "source": _of_payload.get("source") or "docs/strategies/order_flow_mt5.py",
            "raw_payload": _of_payload.get("order_flow_reader") or _of_payload,
        }
        self.latest_order_flow_snapshots[_of_canonical] = _of_row
        self.ingest_client.send_bulk("order_flow_snapshots", [_of_row])

        if signal not in {"BUY", "SELL"}:
            # Ensure symbol is visible in MT5 market watch before tick fetch
            try:
                import MetaTrader5 as _mt5_sel
                _mt5_sel.symbol_select(broker_symbol, True)
            except Exception:
                pass
            _wait_tick = self.reader.symbol_tick(broker_symbol)
            _wait_price, _wait_spread = _price_from_tick(_wait_tick)
            if _wait_price is not None:
                _wt = _wait_tick or {}
                log.info("[US100_TICK_OK] symbol=%s bid=%s ask=%s last=%s",
                         broker_symbol, _wt.get("bid"), _wt.get("ask"), _wt.get("last"))
            else:
                log.info("[US100_TICK_MISSING] symbol=%s reason=NO_RECENT_TICK", broker_symbol)
            _wait_time_snap = self.time_engine.evaluate(broker_symbol, frames, _wait_tick)
            self._per_symbol_state[_simo_key].update({
                "broker_symbol": broker_symbol,
                "price": _wait_price,
                "spread": _wait_spread,
                "spread_status": "OK" if _wait_spread is not None else "NO_TICK",
                "session": _wait_time_snap.get("session_name"),
                "time_gate": _wait_time_snap.get("time_gate_status"),
                "route_status": "WAIT",
                "latest_decision": "WAIT",
                "latest_reason": reason if _wait_price is not None else "NO_RECENT_TICK",
                "last_update_utc": utc_now_iso(),
            })
            return [], _analyzed

        tick = self.reader.symbol_tick(broker_symbol)
        symbol_specs = self.reader.symbol_trade_specs(broker_symbol)
        account = self.reader.account_snapshot()
        effective_max_spread = float(getattr(self.settings, "simo_atm_max_spread_points", 120))
        time_snapshot = self.time_engine.evaluate(broker_symbol, frames, tick)
        simo_audit_cycle_id = utc_now_iso()

        hunter = self.setup_hunter.evaluate(
            broker_symbol,
            broker_symbol,
            {"ai_decision": simo_result, "strategy_signals": [simo_result]},
            time_snapshot,
            self._latest_completed_spread(frames["M5"]),
            effective_max_spread,
            symbol_specs=symbol_specs,
            tick=tick,
            recent_candles=self._confirmed_candle_rows(frames["M5"]),
            audit_cycle_id=simo_audit_cycle_id,
        )
        decision = _routeable_setup_hunter_decision(hunter.decision, hunter.best_candidate)

        if not hunter.best_candidate.get("demo_eligible"):
            log.info("[SIMO_ATM] skipped reason=NOT_DEMO_ELIGIBLE symbol=%s", broker_symbol)
            log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=WAITING", _simo_cycle_symbol)
            _nde_price, _nde_spread = _price_from_tick(tick)
            if _nde_price is not None:
                log.info("[US100_TICK_OK] symbol=%s bid=%s ask=%s last=%s",
                         broker_symbol, (tick or {}).get("bid"), (tick or {}).get("ask"), (tick or {}).get("last"))
            else:
                log.info("[US100_TICK_MISSING] symbol=%s reason=NO_RECENT_TICK", broker_symbol)
            self._per_symbol_state[_simo_key].update({
                "broker_symbol": broker_symbol,
                "price": _nde_price,
                "spread": _nde_spread,
                "spread_status": "OK" if _nde_spread is not None else "NO_TICK",
                "session": time_snapshot.get("session_name"),
                "time_gate": time_snapshot.get("time_gate_status"),
                "route_status": "WAIT",
                "latest_decision": "WAIT",
                "latest_reason": "NOT_DEMO_ELIGIBLE" if _nde_price is not None else "NO_RECENT_TICK",
                "last_update_utc": utc_now_iso(),
            })
            return [], _analyzed

        open_count = self.reader.hermes_open_positions_count(self.settings.hermes_magic_number)
        kelly_risk = self.agent.kelly.evaluate(
            decision,
            {},
            account,
            open_count,
            self._latest_completed_spread(frames["M5"]),
            symbol_specs or {},
            effective_max_spread,
        )
        log.info(
            "[ROUTER_HANDOFF] symbol=%s strategy=SIMO_ATM_BREAKOUT decision=SEND_TO_DEMO_ROUTER direction=%s",
            broker_symbol, signal,
        )
        items = self.demo_router.process_decision(
            decision,
            kelly_risk,
            account,
            broker_symbol,
            frames,
            tick,
            symbol_specs,
            self._latest_completed_spread(frames["M5"]),
            effective_max_spread,
            self.mt5.connected,
            str(uuid4()),
        )
        log.info("[SYMBOL_CYCLE_OUT] symbol=%s status=ROUTED", _simo_cycle_symbol)
        _rt_price, _rt_spread = _price_from_tick(tick)
        if _rt_price is not None:
            log.info("[US100_TICK_OK] symbol=%s bid=%s ask=%s last=%s",
                     broker_symbol, (tick or {}).get("bid"), (tick or {}).get("ask"), (tick or {}).get("last"))
        else:
            log.info("[US100_TICK_MISSING] symbol=%s reason=NO_RECENT_TICK", broker_symbol)
        self._per_symbol_state[_simo_key].update({
            "broker_symbol": broker_symbol,
            "price": _rt_price,
            "spread": _rt_spread,
            "spread_status": "OK" if _rt_spread is not None else "NO_TICK",
            "session": time_snapshot.get("session_name"),
            "time_gate": time_snapshot.get("time_gate_status"),
            "route_status": "ROUTE_TO_DEMO",
            "latest_decision": "ROUTE_TO_DEMO",
            "latest_reason": None,
            "last_update_utc": utc_now_iso(),
        })
        return items, _analyzed

    def send_running_agent_update(self, latest_agent_state: dict | None = None) -> None:
        now = utc_now_iso()
        agent = {
            "name": "HERMES_5MIN_AGENT",
            "status": "RUNNING",
            "mode": "READ_ONLY" if self.settings.read_only else "LIVE_DISABLED",
            "active_symbol": (next(iter(self.resolved_symbols.keys()), None) if self.resolved_symbols else None),
            "timeframe": "M5",
            "latest_signal": None,
            "confidence": None,
            "last_update": now,
        }
        if latest_agent_state:
            agent.update(latest_agent_state)
        self.ingest_client.send_row("hermes_agents", agent)

    def write_ingest_items(self, items: list[dict]) -> None:
        for item in items:
            table = item.get("table")
            data = item.get("data")
            if table and isinstance(data, dict):
                action = item.get("paper_action")
                demo_action = item.get("demo_action")
                if action == "CLOSE_TRADE_ROW" and str(table) == "trades":
                    result = self.update_paper_trade_close(data)
                    if result.get("ok"):
                        self.write_ingest_items(self.learning_optimizer.record_close(data))
                        symbol = str(data.get("symbol") or "")
                        paper_trade_id = str(data.get("id") or "")
                        if symbol:
                            self.paper_trader.confirm_close_trade(symbol, paper_trade_id)
                    continue

                result = self.ingest_client.send_row(str(table), data)

                if demo_action == "DEMO_ORDER":
                    ticket = data.get("ticket")
                    magic = data.get("magic_number") or data.get("magic") or self.settings.demo_magic_number
                    if ticket and str(ticket).lower() not in {"none", "null", "0"}:
                        trade_row = {
                            "ticket": str(ticket),
                            "magic_number": magic,
                            "symbol": data.get("broker_symbol") or data.get("symbol"),
                            "direction": data.get("direction"),
                            "entry": data.get("entry"),
                            "sl": data.get("sl"),
                            "tp": data.get("tp"),
                            "lot": data.get("final_capped_lot"),
                            "strategy": data.get("strategy"),
                            "status": "OPEN",
                            "mode": "DEMO",
                            "created_at": data.get("created_at"),
                        }
                        upsert_result = self.ingest_client.update_row(
                            "trades",
                            {"ticket": str(ticket), "magic_number": magic},
                            trade_row,
                        )
                        if not upsert_result.get("ok"):
                            insert_result = self.ingest_client.send_row("trades", trade_row)
                            if insert_result.get("ok"):
                                log.info(
                                    "[TRADE_UPSERT_FALLBACK] ticket=%s magic=%s action=inserted_after_update_miss",
                                    ticket, magic,
                                )

                if action == "OPEN_TRADE":
                    trade = item.get("trade")
                    symbol = str((trade or {}).get("symbol") or "")
                    if result.get("ok") and isinstance(trade, dict):
                        self.paper_trader.confirm_open_trade(trade)
                    elif symbol:
                        self.paper_trader.clear_stale_open_trade(symbol)

    def persist_setup_hunter_events(self, events: list[dict]) -> None:
        now = utc_now_iso()
        items = []
        for event in events:
            payload = {**event, "created_at": event.get("created_at") or now}
            self.demo_router._record_event(payload)
            items.append({"table": "execution_events", "demo_action": payload.get("event_type"), "data": payload})
        self.write_ingest_items(items)

    def _relaxed_setup_context(self, broker_symbol: str) -> dict:
        symbol = str(broker_symbol or "").upper()
        if symbol.startswith("GOLD") or symbol.startswith("XAUUSD"):
            hours = self._hours_since_allowed_setup({"GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_M1_M5_EMA_SWEEP_SCALPER", "GOLD_ORDER_FLOW_CVD_VWAP"})
            return {"hours_without_setup": hours, "gold_m1m5_hours_without_setup": hours, "gold_liquidity_hours_without_setup": hours}
        if symbol.startswith("EURUSD"):
            hours = self._hours_since_allowed_setup({"EUR_EMA_RSI_ATR_CROSSOVER"})
            return {"hours_without_setup": hours, "eur_hours_without_setup": hours}
        return {}

    def _hours_since_allowed_setup(self, strategies: set[str]) -> float:
        now = datetime.now(timezone.utc)
        latest: datetime | None = None
        for event in self.demo_router._load_events():
            strategy = str(event.get("strategy") or "").upper()
            if strategy not in strategies:
                continue
            direction = str(event.get("direction") or event.get("signal") or event.get("decision") or "").upper()
            eligible = event.get("demo_eligible") is True or event.get("event_type") in {"DEMO_ORDER", "DEMO_ORDER_FAILED"} or direction in {"BUY", "SELL"}
            if not eligible:
                continue
            created = _parse_iso(str(event.get("created_at") or ""))
            if created and (latest is None or created > latest):
                latest = created
        if latest is None:
            return 999.0
        return max(0.0, round((now - latest).total_seconds() / 3600.0, 4))

    def _should_route_to_demo(self, decision: dict) -> bool:
        """P0-B (2026-07-14) — `route_to_demo` etait un champ DECORATIF.

        Cette fonction ne regardait que (strategy, signal). Elle ignorait
        `decision["route_to_demo"]` et `decision["decision"]`. Or TOUTE la chaine de
        garde amont exprime son refus en ecrivant precisement ces deux champs :
        - rejet du balanced_selector      (main.py:1504-1514)
        - OLD_BTC_ENTRY_GATE = BLOCK      (main.py:1309-1312)
        - MT5_POSITION_READ_FAILED        (main.py:1313-1318)
        - FINAL CONFLUENCE GATE           (main.py:1401-1408)
        Ces refus posaient `route_to_demo = False` et `decision = WAIT_ANALYSIS_ONLY`,
        puis la branche de repli du routage rappelait cette fonction — qui, ne lisant
        ni l'un ni l'autre, renvoyait True et envoyait la decision au DemoRouter.
        Un `[ROUTER_HANDOFF] decision=BLOCK` etait loggue... et l'ordre partait.
        Grep `route_to_demo` dans demo_router.py : 0 occurrence — aucun filet en aval.

        Les deux champs sont desormais lus. Un refus amont refuse pour de bon."""
        if decision.get("route_to_demo") is False:
            log.info(
                "[ROUTE_TO_DEMO_REFUSED] strategy=%s reason=ROUTE_TO_DEMO_FALSE — "
                "un garde amont a refuse cette decision",
                decision.get("strategy"),
            )
            return False
        if str(decision.get("decision") or "").upper() == "WAIT_ANALYSIS_ONLY":
            log.info(
                "[ROUTE_TO_DEMO_REFUSED] strategy=%s reason=WAIT_ANALYSIS_ONLY",
                decision.get("strategy"),
            )
            return False
        strategy = str(decision.get("strategy") or "").upper()
        direction = str(decision.get("signal") or decision.get("direction") or "").upper()
        return strategy in ACTIVE_EXECUTION_STRATEGIES and direction in {"BUY", "SELL"}

    def _order_flow_snapshot_rows(self, analysis: dict, requested_symbol: str, broker_symbol: str) -> list[dict]:
        rows: list[dict] = []
        for snapshot in analysis.get("order_flow_snapshots") or []:
            if not isinstance(snapshot, dict):
                continue
            canonical = _canonical_order_flow_symbol(snapshot.get("symbol") or requested_symbol)
            row = {
                **snapshot,
                "symbol": canonical,
                "broker_symbol": broker_symbol or snapshot.get("broker_symbol") or requested_symbol,
                "raw_symbol": requested_symbol,
                "mode": "OBSERVE_ONLY",
                "source": snapshot.get("source") or "docs/strategies/order_flow_mt5.py",
                "raw_payload": snapshot,
            }
            rows.append(row)
            self.latest_order_flow_snapshots[canonical] = row
            log.info(
                "[ORDER_FLOW_SNAPSHOT] symbol=%s broker_symbol=%s status=%s mode=OBSERVE_ONLY",
                canonical,
                row.get("broker_symbol"),
                row.get("status"),
            )
        return rows

    def update_paper_trade_close(self, data: dict) -> dict:
        paper_trade_id = str(data.get("id") or "")
        result = self.ingest_client.update_row(
            "trades",
            {"id": paper_trade_id, "magic_number": self.settings.hermes_magic_number},
            data,
        )
        if result.get("ok"):
            log.info(
                "[PAPER] Trade updated closed symbol=%s id=%s result=%s pnl=%s",
                data.get("symbol"),
                paper_trade_id,
                data.get("result"),
                data.get("pnl"),
            )
            return result

        log.error(
            "[PAPER_CLOSE_UPDATE_FAILED] symbol=%s id=%s body=%s",
            data.get("symbol"),
            paper_trade_id,
            result.get("body") or result.get("error"),
        )
        return result

    def record_learning_setup(
        self,
        symbol: str,
        broker_symbol: str,
        analysis: dict,
        decision: dict,
        paper_items: list[dict],
        spread: float,
        setup_id: str,
    ) -> None:
        try:
            paper_event = next((item.get("data", {}) for item in paper_items if item.get("table") == "execution_events"), {})
            event_type = paper_event.get("event_type")
            if event_type == "PAPER_SKIP":
                sample = {
                    "sample_type": "PAPER_SKIP",
                    "symbol": symbol,
                    "strategy": decision.get("strategy"),
                    "signal": decision.get("signal"),
                    "confidence": decision.get("confidence"),
                    "reason": paper_event.get("reason"),
                    "risk_status": decision.get("risk_status"),
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
                    **journal_payload(decision),
                    "setup_id": setup_id,
                    "raw_payload": paper_event.get("raw_payload"),
                }
            else:
                sample = {
                    "sample_type": "PAPER_OPEN" if event_type == "PAPER_OPEN" else "SETUP",
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "strategy": decision.get("strategy"),
                    "direction": decision.get("signal"),
                    "confidence": decision.get("confidence"),
                    "entry": decision.get("entry"),
                    "sl": decision.get("sl"),
                    "tp": decision.get("tp"),
                    "final_decision": decision.get("decision"),
                    "risk_status": decision.get("risk_status"),
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
                    **journal_payload(decision),
                    "setup_id": setup_id,
                    "paper_trade_id": paper_event.get("paper_trade_id"),
                    "raw_payload": paper_event.get("raw_payload"),
                }
            if self.settings.journal_layer_enabled:
                sample = enrich_with_journal(sample, str(sample.get("sample_type") or "SETUP"), sample.get("reason"))
            self.write_ingest_items(self.learning_optimizer.record_setup(sample))
        except Exception as exc:
            log.warning("[LEARNING] non-fatal setup record error: %s", exc)

    def _latest_completed_spread(self, candles) -> float:
        if candles is None or candles.empty:
            return 0.0
        row = candles.iloc[-2] if len(candles) >= 2 else candles.iloc[-1]
        return float(row.get("spread", 0.0) or 0.0)

    def _confirmed_candle_rows(self, candles) -> list[dict] | None:
        if candles is None or candles.empty:
            return None
        frame = candles.iloc[:-1] if len(candles) >= 2 else candles
        if frame is None or frame.empty:
            return None
        try:
            return frame.to_dict("records")
        except Exception:
            return None

    def _account_equity(self, account: dict | None) -> float | None:
        if not account:
            return None
        try:
            equity = account.get("equity") or account.get("balance")
            return float(equity) if equity is not None else None
        except (TypeError, ValueError):
            return None

    def _analysis_spread(self, candles) -> float:
        if candles is None or candles.empty:
            return 0.0
        row = candles.iloc[-1]
        return float(row.get("spread", 0.0) or 0.0)

    def log_spread_diag(
        self,
        symbol: str,
        broker_symbol: str,
        spread: float,
        symbol_specs: dict,
        tick: dict | None,
        max_spread: float,
    ) -> None:
        symbol_specs = symbol_specs or {}
        tick = tick or {}
        point = float(symbol_specs.get("point") or 0.0)
        bid = tick.get("bid")
        ask = tick.get("ask")
        spread_price = None
        raw_spread_points = float(spread or 0.0)
        if bid is not None and ask is not None:
            try:
                bid_f = float(bid)
                ask_f = float(ask)
                if bid_f > 0 and ask_f > 0 and ask_f >= bid_f:
                    spread_price = abs(ask_f - bid_f)
                    if point > 0:
                        raw_spread_points = spread_price / point
            except (TypeError, ValueError):
                spread_price = None
        if spread_price is None and point > 0:
            spread_price = raw_spread_points * point
        if spread_price is None:
            spread_price = raw_spread_points
        spread_status = "OK" if spread <= max_spread else "MAX_SPREAD"
        log.info(
            "[SPREAD_DIAG] symbol=%s broker_symbol=%s spread=%s spread_price=%s raw_spread_points=%s point=%s digits=%s bid=%s ask=%s max_spread=%s spread_status=%s",
            symbol,
            broker_symbol,
            spread,
            round(float(spread_price), 6),
            round(float(raw_spread_points), 6),
            point if symbol_specs else None,
            symbol_specs.get("digits"),
            bid,
            ask,
            max_spread,
            spread_status,
        )
        self.ingest_client.emit_bot_log(
            "SPREAD_DIAG",
            f"symbol={symbol} spread={spread} spread_price={round(float(spread_price), 6)} max={max_spread} status={spread_status}",
            {
                "symbol": symbol,
                "broker_symbol": broker_symbol,
                "spread": spread,
                "spread_price": round(float(spread_price), 6),
                "raw_spread_points": round(float(raw_spread_points), 6),
                "max_spread": max_spread,
                "spread_status": spread_status,
                "bid": bid,
                "ask": ask,
            },
        )

    def _latest_completed_candle_time(self, candles) -> str | None:
        if candles is None or candles.empty:
            return None
        row = candles.iloc[-2] if len(candles) >= 2 else candles.iloc[-1]
        candle_time = row.get("candle_time")
        return candle_time.isoformat() if hasattr(candle_time, "isoformat") else str(candle_time)

    def _filter_unsent_candles(self, rows: list[dict]) -> list[dict]:
        unsent = []
        for row in rows:
            key = (str(row.get("symbol")), str(row.get("timeframe")), str(row.get("candle_time")))
            if key in self.sent_candle_keys:
                continue
            self.sent_candle_keys.add(key)
            unsent.append(row)
        return unsent


def run_test_ingest() -> None:
    settings = get_settings()
    ingest_client = IngestClient(settings)
    result = ingest_client.send_row(
        "bot_logs",
        {
            "level": "INFO",
            "source": "RDP_TEST",
            "message": "Minimal ingest test",
        },
    )
    print(result.get("body"))


def run_paper_report(hours: int) -> None:
    from app.agents.paper_learning_optimizer import PaperLearningOptimizer
    from app.agents.paper_trading_agent import PaperTradingAgent

    settings = get_settings()
    ingest_client = IngestClient(settings)
    optimizer = PaperLearningOptimizer(settings)
    samples = optimizer._load_samples()
    clean_samples, excluded_summary = clean_report_samples(samples, settings)
    adjusted_report = manual_adjusted_report(samples, settings)
    adjusted_samples = adjusted_report["adjusted_samples"]
    observer_summaries = build_observer_summaries(samples, adjusted_samples)
    raw_performance = calculate_performance_metrics(samples)
    clean_performance = calculate_performance_metrics(clean_samples)
    time_basis = adjusted_samples if adjusted_report.get("excluded_ids") else clean_samples
    time_stats = build_time_stats(time_basis, settings)
    result = ingest_client.get_paper_report(hours)
    duplicate_summary = {"paper_open_duplicate_count": 0, "open_duplicates_by_symbol": {}, **_recovery_close_counts(samples)}
    open_result = ingest_client.get_open_paper_trades(settings.hermes_magic_number)
    if open_result.get("ok"):
        recovery_agent = PaperTradingAgent(settings)
        recovery_agent.recover_open_trades(open_result.get("rows") or [])
        duplicate_summary.update(recovery_agent.open_duplicate_summary())
    strategy_stats = optimizer.strategy_stats(samples)
    _merge_entity_adjusted_stats(strategy_stats, adjusted_report["strategy_adjusted_stats"])
    performance_sections = {
        "raw_performance": raw_performance,
        "adjusted_performance": adjusted_report["adjusted_summary"],
        "clean_performance": clean_performance,
        "excluded_summary": excluded_summary,
        "excluded_outlier_summary": adjusted_report["excluded_outlier_summary"],
        "biggest_losses": adjusted_report["biggest_losses"],
        "symbol_stats": adjusted_report["symbol_adjusted_stats"],
        "report_clean_start_at": settings.report_clean_start_at or None,
        "report_excluded_trade_ids": adjusted_report["excluded_ids"],
        **observer_summaries,
        "observer_summaries": observer_summaries,
    }
    body = _paper_report_body_with_strategy_stats(
        result.get("body"),
        strategy_stats,
        time_stats,
        duplicate_summary,
        performance_sections,
    )
    if body:
        print(body)
        tables = format_time_stats_tables(time_stats)
        if tables:
            print()
            print(format_adjusted_report_tables(adjusted_report))
            print()
            print(tables)
    elif not result.get("ok"):
        print(f"Paper report failed: {result.get('error')}")


def run_demo_report(
    hours: int | None = None,
    since_pilot_start: bool = False,
    since_backend_start: bool = False,
    since_report_reset: bool = False,
) -> None:
    from app.mt5.demo_router import build_demo_report

    settings = get_settings()
    report = build_demo_report(
        settings,
        hours=hours,
        since_pilot_start=since_pilot_start,
        since_backend_start=since_backend_start,
        since_report_reset=since_report_reset,
    )
    print(json.dumps(report, indent=2, sort_keys=False))


def run_reset_demo_report_window() -> None:
    from app.mt5.demo_router import reset_report_window

    started_at = reset_report_window()
    print(f"report_window_started_at={started_at.isoformat()}")


def run_sync_mt5_positions_once() -> None:
    from app.mt5.connection import MT5Connection
    from app.mt5.demo_router import DemoKellyRouter

    settings = get_settings()
    ingest_client = IngestClient(settings)
    demo_router = DemoKellyRouter(settings)
    mt5_conn = MT5Connection()
    connected = mt5_conn.connect()
    if not connected:
        raise SystemExit(1)
    try:
        summary = sync_open_mt5_positions_to_lovable(settings, ingest_client, record_event=demo_router._record_event)
    finally:
        mt5_conn.shutdown()
    print(
        "mt5_open=%s hermes_open=%s synced=%s closed=%s already_closed=%s closed_tickets=%s already_closed_tickets=%s"
        % (
            summary.get("mt5_open_positions_count", 0),
            summary.get("hermes_mt5_open_positions_count", 0),
            summary.get(_sb_key("open_trades_synced_count"), 0),
            summary.get(_sb_key("open_trades_closed_count"), 0),
            summary.get("already_closed_count", 0),
            summary.get("closed_tickets", []),
            summary.get("already_closed_tickets", []),
        )
    )


def run_force_close_demo_ticket(ticket: str) -> None:
    from app.mt5.demo_router import DemoKellyRouter

    settings = get_settings()
    ingest_client = IngestClient(settings)
    demo_router = DemoKellyRouter(settings)
    result = force_close_demo_ticket(settings, ingest_client, ticket, record_event=demo_router._record_event)
    print(
        "force_close_demo_ticket ticket=%s magic_number=%s closed=%s closed_tickets=%s ok=%s"
        % (
            result.get("ticket"),
            result.get("magic_number"),
            result.get("closed", 0),
            result.get("closed_tickets", []),
            str(bool(result.get("ok"))).lower(),
        )
    )


def run_account_diag() -> None:
    from app.mt5.connection import MT5Connection
    from app.mt5.data_reader import MT5DataReader
    from app.mt5.demo_router import DemoKellyRouter

    mt5_conn = MT5Connection()
    if not mt5_conn.connect():
        raise SystemExit(1)
    try:
        account = MT5DataReader().account_snapshot()
        diag = DemoKellyRouter(get_settings()).account_diagnostics(account)
        print(
            '[ACCOUNT_DIAG] login=%s trade_mode=%s account_type=%s name="%s" server="%s" company="%s" trade_allowed=%s trade_expert=%s'
            % (
                diag.get("login"),
                diag.get("trade_mode"),
                diag.get("account_type"),
                diag.get("name") or "",
                diag.get("server") or "",
                diag.get("company") or "",
                diag.get("trade_allowed"),
                diag.get("trade_expert"),
            )
        )
    finally:
        mt5_conn.shutdown()


def run_time_log_diag() -> None:
    settings = get_settings()
    ingest_client = IngestClient(settings)
    row = ingest_client.prepare_row(
        "bot_logs",
        {
            "level": "INFO",
            "source": "HERMES_BACKEND",
            "message": "TIME_LOG_DIAG",
            "context": {"diagnostic": True, "diagnostic_id": str(uuid4())},
            "created_at": utc_now_iso(),
        },
    )
    result = ingest_client.send_row("bot_logs", row)
    sent_payload = row.get("raw_payload", {}) if isinstance(row.get("raw_payload"), dict) else {}
    fetched_payload = _raw_payload_from_ingest_body(result.get("body")) or sent_payload
    print("TIME_LOG_DIAG sent_row=" + json.dumps(row, indent=2, sort_keys=False))
    if result.get("body"):
        print("TIME_LOG_DIAG ingest_body=" + format_response_body(result.get("body")))
    if not result.get("ok"):
        print(f"TIME_LOG_DIAG ingest_result ok=false error={result.get('error')} status_code={result.get('status_code')}")
    print(f"TIME_LOG_DIAG raw_payload.utc_time={fetched_payload.get('utc_time')}")
    print(f"TIME_LOG_DIAG raw_payload.casablanca_time={fetched_payload.get('casablanca_time')}")
    print(f"TIME_LOG_DIAG raw_payload.broker_time_estimate={fetched_payload.get('broker_time_estimate')}")


def run_dashboard_status_diag() -> None:
    from app.mt5.connection import MT5Connection
    from app.mt5.data_reader import MT5DataReader
    from app.mt5.symbol_mapper import SymbolMapper
    from app.services.time_engine import TimeEngine

    settings = get_settings()
    ingest_client = IngestClient(settings)
    heartbeat = HeartbeatService(settings, ingest_client)
    mt5_conn = MT5Connection()
    connected = mt5_conn.connect()
    account = None
    resolved_symbols: dict[str, str] = {}
    try:
        reader = MT5DataReader()
        if connected:
            account = reader.account_snapshot()
            resolved_symbols = SymbolMapper(settings.symbol_list).resolve_all()
            if resolved_symbols:
                raw_symbol, broker_symbol = next(iter(resolved_symbols.items()))
                frames = reader.get_all_timeframes(broker_symbol, count=10)
                tick = reader.symbol_tick(broker_symbol)
                time_snapshot = TimeEngine(settings).evaluate(raw_symbol, frames, tick)
                ingest_client.set_time_snapshot(time_snapshot)
                latest_demo_event = {
                    "symbol": broker_symbol,
                    "raw_symbol": raw_symbol,
                    "broker_symbol": broker_symbol,
                    "allowed_symbol_check": "PASS",
                    "gate_statuses": {"raw_symbol": raw_symbol, "broker_symbol": broker_symbol, "allowed_symbol_check": "PASS"},
                }
            else:
                latest_demo_event = {}
        else:
            latest_demo_event = {}
        payload = dashboard_snapshot(settings, account, ingest_client.latest_time_snapshot, latest_demo_event, connected)
        print(dashboard_status_debug_line(payload))
        print(json.dumps(payload, indent=2, sort_keys=False))
        print(f"mode={payload.get('mode')}")
        print(f"account_type={payload.get('account_type')}")
        print(f"demo_pilot_enabled={str(bool(payload.get('demo_pilot_enabled'))).lower()}")
        print(f"allow_live_trading={str(bool(payload.get('allow_live_trading'))).lower()}")
        print(f"utc_time={payload.get('utc_time')}")
        print(f"casablanca_time={payload.get('casablanca_time')}")
        print(f"broker_time_estimate={payload.get('broker_time_estimate')}")
        print(f"time_gate_status={payload.get('time_gate_status')}")
        result = heartbeat.write_dashboard_status(payload)
        if result.get("body"):
            print("DASHBOARD_STATUS ingest_body=" + format_response_body(result.get("body")))
        if not result.get("ok"):
            print(f"DASHBOARD_STATUS ingest_result ok=false error={result.get('error')} status_code={result.get('status_code')}")
    finally:
        if connected:
            mt5_conn.shutdown()


def run_session_diag() -> None:
    from app.services.time_engine import TimeEngine

    settings = get_settings()
    snapshot = TimeEngine(settings).evaluate("BTCUSD#", now=None)
    print(f"utc_now={snapshot.get('utc_time')}")
    print(f"casablanca_now={snapshot.get('casablanca_time')}")
    print(f"broker_time_estimate={snapshot.get('broker_time_estimate')}")
    print(f"utc_hour={snapshot.get('utc_hour')}")
    print(f"casablanca_hour={snapshot.get('local_hour')}")
    print(f"broker_hour={snapshot.get('broker_hour')}")
    print(f"session_by_utc={snapshot.get('session_name')}")
    print(f"final_session_name={snapshot.get('session_name')}")
    print(f"time_gate_status={snapshot.get('time_gate_status')}")
    print(f"time_gate_reason={snapshot.get('time_gate_reason')}")


def _raw_payload_from_ingest_body(body: str | None) -> dict | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    candidates = parsed if isinstance(parsed, list) else [parsed]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw = candidate.get("raw_payload")
        if isinstance(raw, dict):
            return raw
        data = candidate.get("data")
        if isinstance(data, dict) and isinstance(data.get("raw_payload"), dict):
            return data["raw_payload"]
    return None


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _paper_report_body_with_strategy_stats(
    body: str | None,
    strategy_stats: dict,
    time_stats: dict | None = None,
    duplicate_summary: dict | None = None,
    performance_sections: dict | None = None,
) -> str:
    if body is None:
        return ""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return format_response_body(body)
    if isinstance(parsed, dict):
        parsed["strategy_stats"] = strategy_stats
        if time_stats is not None:
            parsed["time_stats"] = time_stats
        if duplicate_summary is not None:
            parsed.update(duplicate_summary)
        if performance_sections is not None:
            parsed.update(performance_sections)
        return json.dumps(parsed, indent=2, sort_keys=False)
    out = {"report": parsed, "strategy_stats": strategy_stats}
    if time_stats is not None:
        out["time_stats"] = time_stats
    if duplicate_summary is not None:
        out.update(duplicate_summary)
    if performance_sections is not None:
        out.update(performance_sections)
    return json.dumps(out, indent=2, sort_keys=False)


def _recovery_close_counts(samples: list[dict]) -> dict:
    counts = {
        "recovery_closed_count": 0,
        "recovery_sl_breach_count": 0,
        "recovery_tp_breach_count": 0,
        "recovery_ambiguous_count": 0,
    }
    for sample in samples:
        raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
        recovery_close = bool(sample.get("recovery_close") or raw_payload.get("recovery_close"))
        if sample.get("sample_type") != "PAPER_CLOSE" or not recovery_close:
            continue
        counts["recovery_closed_count"] += 1
        reason = str(sample.get("recovery_reason") or raw_payload.get("recovery_reason") or sample.get("reason") or sample.get("close_reason") or "")
        if reason == "RECOVERY_SL_BREACH":
            counts["recovery_sl_breach_count"] += 1
        elif reason == "RECOVERY_TP_BREACH":
            counts["recovery_tp_breach_count"] += 1
        elif reason == "RECOVERY_AMBIGUOUS_BREACH":
            counts["recovery_ambiguous_count"] += 1
    return counts


def _sb_key(suffix: str) -> str:
    return "supa" + "base_" + suffix


def _merge_entity_adjusted_stats(strategy_stats: dict, adjusted_stats: dict) -> None:
    for name, payload in adjusted_stats.items():
        strategy_stats.setdefault(name, {}).update(payload)


def run_strategy_audit(strategy: str, symbol: str, hours: int | None = None) -> None:
    strategy_name = str(strategy or "").upper()
    if strategy_name != "QUANT_STATISTICAL_PULLBACK":
        raise SystemExit(f"Unsupported strategy audit: {strategy}")
    if not symbol:
        raise SystemExit("--symbol is required for strategy audit")
    from app.services.quant_statistical_audit import write_quant_statistical_audit

    path = write_quant_statistical_audit(get_settings(), symbol, hours or 48)
    print(str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-ingest", action="store_true")
    parser.add_argument("--paper-report", type=int, metavar="HOURS")
    parser.add_argument("--demo-report", action="store_true")
    parser.add_argument("--hours", type=int, metavar="N")
    parser.add_argument("--since-pilot-start", action="store_true")
    parser.add_argument("--since-backend-start", action="store_true")
    parser.add_argument("--since-report-reset", action="store_true")
    parser.add_argument("--reset-demo-report-window", action="store_true")
    parser.add_argument("--sync-mt5-positions-once", action="store_true")
    parser.add_argument("--force-close-demo-ticket")
    parser.add_argument("--account-diag", action="store_true")
    parser.add_argument("--time-log-diag", action="store_true")
    parser.add_argument("--dashboard-status-diag", action="store_true")
    parser.add_argument("--session-diag", action="store_true")
    parser.add_argument("--audit-strategy")
    parser.add_argument("--symbol", default="")
    args = parser.parse_args()
    if args.test_ingest:
        run_test_ingest()
        return
    if args.paper_report is not None:
        run_paper_report(args.paper_report)
        return
    if args.demo_report:
        run_demo_report(args.hours, args.since_pilot_start, args.since_backend_start, args.since_report_reset)
        return
    if args.reset_demo_report_window:
        run_reset_demo_report_window()
        return
    if args.sync_mt5_positions_once:
        run_sync_mt5_positions_once()
        return
    if args.force_close_demo_ticket:
        run_force_close_demo_ticket(args.force_close_demo_ticket)
        return
    if args.account_diag:
        run_account_diag()
        return
    if args.time_log_diag:
        run_time_log_diag()
        return
    if args.dashboard_status_diag:
        run_dashboard_status_diag()
        return
    if args.session_diag:
        run_session_diag()
        return
    if args.audit_strategy:
        run_strategy_audit(args.audit_strategy, args.symbol, args.hours)
        return

    backend = HermesBackend()
    backend.start()


if __name__ == "__main__":
    main()
