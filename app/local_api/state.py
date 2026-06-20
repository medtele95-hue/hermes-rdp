"""
Thread-safe shared state for the HERMES local dashboard API.

Written to by the main backend loop; read by the FastAPI server thread.
No execution logic lives here — purely an in-memory read-only mirror.
"""
from __future__ import annotations

import collections
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_MAX_LOGS = 1500
_MAX_CANDLE_ROWS = 500  # per symbol per timeframe


# ---------------------------------------------------------------------------
# Log handler — captures hermes log lines into the ring buffer
# ---------------------------------------------------------------------------

class _LocalLogHandler(logging.Handler):
    """Appends each log record to the shared state ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            state = get_local_state()
            state.add_log({
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
            })
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared state singleton
# ---------------------------------------------------------------------------

class _LocalState:
    """Thread-safe read/write mirror of the HermesBackend runtime state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Core backend state
        self._account_snapshot: dict = {}
        self._per_symbol_state: Dict[str, dict] = {}
        self._cycle_status: dict = {"last_status": "STARTING"}
        self._order_flow_snapshots: Dict[str, dict] = {}
        self._latest_candidates: List[dict] = []
        self._latest_safety_guard: Optional[dict] = None
        self._latest_setup_hunter: Optional[dict] = None
        self._latest_demo_event: Optional[dict] = None
        self._mt5_connected: bool = False
        self._resolved_symbols: Dict[str, str] = {}
        self._position_sync: dict = {}
        self._dashboard_snapshot: dict = {}
        self._backend_started_at: str = ""
        self._last_heartbeat_at: str = ""
        self._ingest_health: dict = {}

        # Candle cache: upper-cased symbol → timeframe → [OHLCV dicts]
        self._candle_cache: Dict[str, Dict[str, list]] = {}

        # Quad terminal per-symbol snapshots
        self._quad_terminal: Dict[str, dict] = {}

        # Strategy signals cache: upper-cased symbol → [signal dicts]
        self._strategy_signals: Dict[str, list] = {}

        # Analysis cache: upper-cased symbol → analysis dict
        self._analysis_cache: Dict[str, dict] = {}

        # Settings snapshot (safe read-only config summary)
        self._settings_snapshot: dict = {}

        # Demo event history (list of last 200 demo router events)
        self._demo_events: List[dict] = []

        # HERMES BTC smart exit status (updated by demo_router each cycle + fast exit daemon)
        self._hermes_btc_status: dict = {
            "open_count": 0,
            "floating_pnl": 0.0,
            "positive_candidates": 0,
            "last_smart_exit_reason": None,
            "emergency_active": False,
            # Fast Smart Exit daemon fields
            "fast_exit_daemon_enabled": False,
            "fast_exit_interval_ms": 250,
            "fast_exit_last_tick": None,
            "fast_exit_positive_candidates": 0,
            "fast_exit_last_close_ticket": None,
            "fast_exit_last_close_profit": None,
            "fast_exit_last_close_reason": None,
            "fast_exit_last_error": None,
        }

        # BTC intelligence panel state (updated by intelligence engine each cycle)
        self._btc_intelligence: dict = {
            "current_mode": "NORMAL",
            "win_streak": 0,
            "loss_streak": 0,
            "positive_exit_rate": 0.0,
            "best_strategy_now": None,
            "worst_strategy_now": None,
            "last_setup_score": None,
            "last_setup_grade": None,
            "last_block_reason": None,
            "current_exit_profile": None,
            "temporary_block_rules": [],
            "smart_exit_status": None,
            "fast_exit_status": None,
            "pause_remaining_minutes": 0.0,
            "requires_aplus": False,
            # Narrative intelligence fields (Pièce 5 — populated by BtcMarketNarrator)
            "last_narrative_verdict": "N/A",
            "last_narrative_coherence": 0.0,
            "last_narrative_confidence": 0.0,
            "last_strong_confirmations": [],
            "last_silent_risks": [],
            "active_temp_blocks": [],
            # Dynamic exit fields (v6 — updated by BtcFastExitDaemon each tick)
            "atr_m5": None,
            "rr_target": None,
            "exit_mode": "fallback",
        }

        # Recent log lines
        self._logs: collections.deque = collections.deque(maxlen=_MAX_LOGS)

    # ------------------------------------------------------------------
    # Write methods (called from the main backend thread)
    # ------------------------------------------------------------------

    def update_core(
        self,
        *,
        account_snapshot: Optional[dict] = None,
        per_symbol_state: Optional[dict] = None,
        cycle_status: Optional[dict] = None,
        order_flow_snapshots: Optional[dict] = None,
        latest_candidates: Optional[list] = None,
        latest_safety_guard: Optional[dict] = None,
        latest_setup_hunter: Optional[dict] = None,
        latest_demo_event: Optional[dict] = None,
        mt5_connected: bool = False,
        resolved_symbols: Optional[dict] = None,
        position_sync: Optional[dict] = None,
        dashboard_snapshot: Optional[dict] = None,
        ingest_health: Optional[dict] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if account_snapshot is not None:
                self._account_snapshot = dict(account_snapshot)
            if per_symbol_state is not None:
                self._per_symbol_state = dict(per_symbol_state)
            if cycle_status is not None:
                self._cycle_status = dict(cycle_status)
            if order_flow_snapshots is not None:
                self._order_flow_snapshots = dict(order_flow_snapshots)
            if latest_candidates is not None:
                self._latest_candidates = list(latest_candidates)
            if latest_safety_guard is not None:
                self._latest_safety_guard = dict(latest_safety_guard)
            if latest_setup_hunter is not None:
                self._latest_setup_hunter = dict(latest_setup_hunter)
            if latest_demo_event is not None:
                self._latest_demo_event = dict(latest_demo_event)
            self._mt5_connected = mt5_connected
            if resolved_symbols is not None:
                self._resolved_symbols = dict(resolved_symbols)
            if position_sync is not None:
                self._position_sync = dict(position_sync)
            if dashboard_snapshot is not None:
                self._dashboard_snapshot = dict(dashboard_snapshot)
            if ingest_health is not None:
                self._ingest_health = dict(ingest_health)
            self._last_heartbeat_at = now

    def update_candles(self, symbol: str, timeframe: str, rows: list) -> None:
        sym = str(symbol).upper()
        tf = str(timeframe).upper()
        with self._lock:
            if sym not in self._candle_cache:
                self._candle_cache[sym] = {}
            self._candle_cache[sym][tf] = rows[-_MAX_CANDLE_ROWS:]

    def update_quad_terminal(self, symbol: str, payload: dict) -> None:
        with self._lock:
            self._quad_terminal[str(symbol).upper()] = payload

    def update_strategy_signals(self, symbol: str, signals: list) -> None:
        with self._lock:
            self._strategy_signals[str(symbol).upper()] = list(signals)

    def update_analysis_cache(self, symbol: str, analysis: dict) -> None:
        with self._lock:
            self._analysis_cache[str(symbol).upper()] = dict(analysis)

    def update_settings(self, snapshot: dict) -> None:
        with self._lock:
            self._settings_snapshot = dict(snapshot)

    def set_backend_started_at(self, ts: str) -> None:
        with self._lock:
            self._backend_started_at = ts

    def add_log(self, entry: dict) -> None:
        with self._lock:
            self._logs.append(entry)

    def set_demo_events(self, events: List[dict]) -> None:
        with self._lock:
            self._demo_events = list(events[-200:])

    def update_hermes_btc_status(self, status: dict) -> None:
        with self._lock:
            self._hermes_btc_status = {**self._hermes_btc_status, **status}

    def update_btc_intelligence(self, payload: dict) -> None:
        with self._lock:
            self._btc_intelligence = {**self._btc_intelligence, **payload}

    # ------------------------------------------------------------------
    # Read accessors — always return copies (safe for API thread)
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> dict:
        with self._lock:
            return dict(self._account_snapshot)

    def get_per_symbol_state(self) -> dict:
        with self._lock:
            return dict(self._per_symbol_state)

    def get_cycle_status(self) -> dict:
        with self._lock:
            return dict(self._cycle_status)

    def get_order_flow_snapshots(self) -> dict:
        with self._lock:
            return dict(self._order_flow_snapshots)

    def get_latest_candidates(self) -> list:
        with self._lock:
            return list(self._latest_candidates)

    def get_latest_safety_guard(self) -> Optional[dict]:
        with self._lock:
            return dict(self._latest_safety_guard) if self._latest_safety_guard else None

    def get_hermes_btc_status(self) -> dict:
        with self._lock:
            return dict(self._hermes_btc_status)

    def get_btc_intelligence(self) -> dict:
        with self._lock:
            return dict(self._btc_intelligence)

    def get_latest_setup_hunter(self) -> Optional[dict]:
        with self._lock:
            return dict(self._latest_setup_hunter) if self._latest_setup_hunter else None

    def get_latest_demo_event(self) -> Optional[dict]:
        with self._lock:
            return dict(self._latest_demo_event) if self._latest_demo_event else None

    def get_mt5_connected(self) -> bool:
        with self._lock:
            return self._mt5_connected

    def get_resolved_symbols(self) -> dict:
        with self._lock:
            return dict(self._resolved_symbols)

    def get_position_sync(self) -> dict:
        with self._lock:
            return dict(self._position_sync)

    def get_dashboard_snapshot(self) -> dict:
        with self._lock:
            return dict(self._dashboard_snapshot)

    def get_candles(self, symbol: str, timeframe: str) -> Optional[list]:
        sym = str(symbol).upper()
        tf = str(timeframe).upper()
        with self._lock:
            sym_cache = self._candle_cache.get(sym, {})
            rows = sym_cache.get(tf)
            return list(rows) if rows is not None else None

    def get_all_candles_for_symbol(self, symbol: str) -> Dict[str, list]:
        sym = str(symbol).upper()
        with self._lock:
            return {tf: list(rows) for tf, rows in self._candle_cache.get(sym, {}).items()}

    def get_quad_terminal(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._quad_terminal)

    def get_strategy_signals(self, symbol: str) -> list:
        with self._lock:
            return list(self._strategy_signals.get(str(symbol).upper(), []))

    def get_all_strategy_signals(self) -> Dict[str, list]:
        with self._lock:
            return {k: list(v) for k, v in self._strategy_signals.items()}

    def get_analysis_cache(self, symbol: str) -> dict:
        with self._lock:
            return dict(self._analysis_cache.get(str(symbol).upper(), {}))

    def get_recent_logs(
        self,
        limit: int = 300,
        level: Optional[str] = None,
        search: Optional[str] = None,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> list:
        with self._lock:
            entries = list(self._logs)
        if level:
            lu = level.upper()
            entries = [e for e in entries if e.get("level", "").upper() == lu]
        if symbol:
            sl = symbol.lower()
            entries = [e for e in entries if sl in e.get("message", "").lower()]
        if strategy:
            stl = strategy.lower()
            entries = [e for e in entries if stl in e.get("message", "").lower()]
        if search:
            sl = search.lower()
            entries = [e for e in entries if sl in e.get("message", "").lower()]
        return entries[-limit:]

    def get_settings_snapshot(self) -> dict:
        with self._lock:
            return dict(self._settings_snapshot)

    def get_backend_started_at(self) -> str:
        with self._lock:
            return self._backend_started_at

    def get_last_heartbeat_at(self) -> str:
        with self._lock:
            return self._last_heartbeat_at

    def get_ingest_health(self) -> dict:
        with self._lock:
            return dict(self._ingest_health)

    def get_demo_events(self) -> List[dict]:
        with self._lock:
            return list(self._demo_events)

    def is_ready(self) -> bool:
        with self._lock:
            return bool(self._backend_started_at)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_shared_state = _LocalState()
_log_handler_installed = False


def get_local_state() -> _LocalState:
    return _shared_state


def install_log_handler() -> None:
    """Attach the ring-buffer handler to the hermes logger. Call once at startup."""
    global _log_handler_installed
    if _log_handler_installed:
        return
    hermes_logger = logging.getLogger("hermes")
    handler = _LocalLogHandler()
    handler.setLevel(logging.DEBUG)
    hermes_logger.addHandler(handler)
    _log_handler_installed = True


def normalize_symbol(raw: str) -> str:
    """Return the canonical display symbol (e.g. US100Cash#) for any raw key."""
    u = str(raw or "").upper().replace(" ", "")
    if u.startswith("BTCUSD"):
        return "BTCUSD#"
    if u.startswith(("GOLD", "XAUUSD")):
        return "GOLD#"
    if u.startswith("EURUSD"):
        return "EURUSD"
    if "US100" in u or "NAS100" in u or "USTEC" in u:
        return "US100Cash#"
    return raw
