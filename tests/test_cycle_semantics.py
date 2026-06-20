"""Tests for HERMES cycle semantics patch.

Covers:
  1. cycle_start emit_bot_log uses settings.hermes_main_symbol_list (exact case, includes US100Cash#)
  2. SYMBOL_CYCLE_IN log primary symbol is the configured name (BTCUSD#), not resolved key (BTCUSD)
  3. US100Cash# WAIT evaluation increments analyzed (not lost)
  4. US100Cash# unavailable increments skipped
  5. analyzed + skipped == len(hermes_main_symbol_list) after every cycle
  6. BTC 24/7 PASS unchanged
  7. Safety invariants unchanged
  8. order_send only in demo_router
  9. SYMBOL_CYCLE_IN log format: symbol=BTCUSD# raw_symbol=BTCUSD broker_symbol=BTCUSD# (log capture)
  10. run_simo_index_cycle analyzed/skipped counts propagate to _cycle_status (injection test)
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.time_engine import TimeEngine
from app.main import _cfg_sym_for_resolved


# ── helpers ───────────────────────────────────────────────────────────────────

def _settings(**kwargs) -> Settings:
    base = dict(
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        allow_time_block_override=False,
        research_allow_low_confluence=False,
        hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
        crypto_24_7_enabled=True,
        btc_weekend_analysis_only=True,
        symbols="BTCUSD,XAUUSD,EURUSD",
        simo_atm_breakout_symbols="US100,NAS100,USTEC,US100Cash#,NASDAQ",
        simo_atm_breakout_enabled=True,
    )
    base.update(kwargs)
    return Settings(**base)


def _weekend() -> datetime:
    return datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)  # Saturday


def _weekday() -> datetime:
    return datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # Monday


def _tick(bid: float) -> dict:
    return {"bid": bid, "ask": bid + 1.0, "time": 0}


# ── 1. cycle_start uses hermes_main_symbol_list ───────────────────────────────

class TestCycleStartSymbols(unittest.TestCase):
    """emit_bot_log for cycle_start must carry hermes_main_symbol_list, not resolved_symbols keys."""

    def _make_backend_mocked(self, settings: Settings):
        """Build a HermesBackend with all heavy dependencies stubbed out."""
        from app.main import HermesBackend
        backend = HermesBackend.__new__(HermesBackend)
        backend.settings = settings
        backend.ingest_client = MagicMock()
        backend.heartbeat = MagicMock()
        backend.reader = MagicMock()
        backend.mt5 = MagicMock()
        backend.mt5.connected = False
        backend.paper_trader = MagicMock()
        backend.demo_router = MagicMock()
        backend.demo_router.process_quick_exits.return_value = []
        backend.setup_hunter = MagicMock()
        backend.time_engine = MagicMock()
        backend.strategy_manager = MagicMock()
        backend.strategy_manager.simo_enabled = False
        backend.agent = MagicMock()
        backend.learning_optimizer = MagicMock()
        backend.resolved_symbols = {"BTCUSD": "BTCUSD", "XAUUSD": "GOLD#", "EURUSD": "EURUSD"}
        backend._per_symbol_state = {}
        backend._cycle_status = {"last_cycle_end_utc": None}
        backend._cycle_active = False
        backend._live_snapshot_lock = MagicMock()
        backend._live_snapshot_lock.acquire.return_value = True
        backend._latest_candidates_all = []
        backend._latest_safety_guard = {}
        backend.latest_agent_state = None
        backend.latest_demo_event = None
        backend.latest_setup_hunter = None
        backend.latest_position_sync = {}
        backend.latest_order_flow_snapshots = []
        backend.sent_candle_keys = set()
        return backend

    def test_cycle_start_emit_uses_hermes_main_symbol_list(self) -> None:
        """cycle_start bot-log payload must contain exact hermes_main_symbol_list, not resolved_symbols keys."""
        s = _settings()
        backend = self._make_backend_mocked(s)

        # Make reader return enough to short-circuit quickly (empty M5 → skip)
        import pandas as pd
        empty_df = pd.DataFrame()
        backend.reader.latest_candle_rows.return_value = []
        backend.reader.get_all_timeframes.return_value = {"M5": empty_df, "H1": empty_df, "H4": empty_df, "D1": empty_df}
        backend.reader.symbol_tick.return_value = None
        backend.reader.symbol_trade_specs.return_value = {}
        backend.reader.account_snapshot.return_value = {"balance": 1000, "equity": 1000}
        backend.reader.hermes_open_positions_count.return_value = 0
        backend.time_engine.evaluate.return_value = {
            "time_gate_status": "BLOCK", "time_gate_reason": "TEST", "session_name": "OFF",
            "is_weekend": True, "market_open": False,
        }
        backend.paper_trader.process_closures.return_value = []
        backend.paper_trader.reconcile_recovered_trades = MagicMock(return_value=[])
        backend.ingest_client.send_bulk.return_value = {"sent": 0, "failed": 0}
        backend.ingest_client.log_event.return_value = {}
        backend.ingest_client.set_time_snapshot.return_value = None
        backend.ingest_client.send_row.return_value = {"ok": True}
        backend.ingest_client.emit_bot_log.return_value = {}
        backend.demo_router.process_quick_exits.return_value = []

        backend.run_cycle()

        # Find the cycle_start emit_bot_log call
        calls = backend.ingest_client.emit_bot_log.call_args_list
        cycle_start_calls = [c for c in calls if c.args[0] == "CYCLE" and "cycle_start" in str(c.args[1])]
        self.assertTrue(cycle_start_calls, "No cycle_start emit_bot_log call found")
        call = cycle_start_calls[0]
        # Third arg is the payload dict
        payload = call.args[2] if len(call.args) > 2 else call.kwargs.get("data", {})
        syms = payload.get("symbols", [])
        self.assertIn("BTCUSD#", syms, f"BTCUSD# missing from cycle_start symbols: {syms}")
        self.assertIn("US100Cash#", syms, f"US100Cash# missing from cycle_start symbols: {syms}")
        self.assertIn("GOLD#", syms, f"GOLD# missing: {syms}")
        self.assertIn("EURUSD", syms, f"EURUSD missing: {syms}")
        # Resolved broker keys must NOT appear as display symbols
        self.assertNotIn("BTCUSD", syms, f"Raw BTCUSD key leaked into cycle_start symbols: {syms}")
        self.assertNotIn("XAUUSD", syms, f"Raw XAUUSD key leaked into cycle_start symbols: {syms}")
        self.assertEqual(len(syms), 4, f"Expected 4 symbols, got: {syms}")

    def test_cycle_start_message_uses_hermes_main_symbol_list(self) -> None:
        """cycle_start message string must reference configured symbols, not raw resolved keys."""
        s = _settings()
        backend = self._make_backend_mocked(s)

        import pandas as pd
        empty_df = pd.DataFrame()
        backend.reader.latest_candle_rows.return_value = []
        backend.reader.get_all_timeframes.return_value = {"M5": empty_df, "H1": empty_df, "H4": empty_df, "D1": empty_df}
        backend.reader.symbol_tick.return_value = None
        backend.reader.symbol_trade_specs.return_value = {}
        backend.reader.account_snapshot.return_value = {"balance": 1000, "equity": 1000}
        backend.reader.hermes_open_positions_count.return_value = 0
        backend.time_engine.evaluate.return_value = {
            "time_gate_status": "BLOCK", "time_gate_reason": "TEST", "session_name": "OFF",
            "is_weekend": True, "market_open": False,
        }
        backend.paper_trader.process_closures.return_value = []
        backend.ingest_client.send_bulk.return_value = {"sent": 0, "failed": 0}
        backend.ingest_client.log_event.return_value = {}
        backend.ingest_client.set_time_snapshot.return_value = None
        backend.ingest_client.send_row.return_value = {"ok": True}
        backend.ingest_client.emit_bot_log.return_value = {}
        backend.demo_router.process_quick_exits.return_value = []

        backend.run_cycle()

        calls = backend.ingest_client.emit_bot_log.call_args_list
        cycle_start_calls = [c for c in calls if c.args[0] == "CYCLE" and "cycle_start" in str(c.args[1])]
        self.assertTrue(cycle_start_calls)
        msg = str(cycle_start_calls[0].args[1])
        self.assertIn("BTCUSD#", msg, f"BTCUSD# not in message: {msg!r}")
        self.assertIn("US100Cash#", msg, f"US100Cash# not in message: {msg!r}")


# ── 2. _cfg_sym_for_resolved maps correctly ────────────────────────────────────

class TestCfgSymForResolved(unittest.TestCase):
    """_cfg_sym_for_resolved maps resolved_symbols keys → hermes_main_symbol_list display name."""

    MAIN = ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]

    def test_btcusd_maps_to_btcusd_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("BTCUSD", self.MAIN), "BTCUSD#")

    def test_btcusd_hash_maps_to_btcusd_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("BTCUSD#", self.MAIN), "BTCUSD#")

    def test_xauusd_maps_to_gold_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("XAUUSD", self.MAIN), "GOLD#")

    def test_gold_maps_to_gold_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("GOLD", self.MAIN), "GOLD#")

    def test_eurusd_maps_to_eurusd(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("EURUSD", self.MAIN), "EURUSD")

    def test_us100cash_maps_to_us100cash_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("US100CASH", self.MAIN), "US100Cash#")

    def test_nas100_maps_to_us100cash_hash(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("NAS100", self.MAIN), "US100Cash#")

    def test_unknown_symbol_falls_through(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("UNKNOWN", self.MAIN), "UNKNOWN")

    def test_case_insensitive_btcusd(self) -> None:
        self.assertEqual(_cfg_sym_for_resolved("btcusd", self.MAIN), "BTCUSD#")


# ── 3 & 4 & 5. SIMO cycle counts ──────────────────────────────────────────────

class TestSimoCycleCounts(unittest.TestCase):
    """run_simo_index_cycle returns (items, counts) and counts correctly."""

    def _make_simo_backend(self, simo_enabled: bool = True, mt5_connected: bool = True,
                            broker_symbol: str | None = "US100Cash#",
                            simo_signal: str = "WAIT") -> object:
        s = _settings(simo_atm_breakout_enabled=simo_enabled)
        from app.main import HermesBackend
        backend = HermesBackend.__new__(HermesBackend)
        backend.settings = s
        backend.strategy_manager = MagicMock()
        backend.strategy_manager.simo_enabled = simo_enabled
        backend.mt5 = MagicMock()
        backend.mt5.connected = mt5_connected
        backend.reader = MagicMock()
        backend.time_engine = MagicMock()
        backend.setup_hunter = MagicMock()
        backend.agent = MagicMock()
        backend.demo_router = MagicMock()
        backend.demo_router.process_decision.return_value = []
        backend._per_symbol_state = {}
        backend.latest_order_flow_snapshots = {}
        backend.ingest_client = MagicMock()
        backend.ingest_client.send_bulk.return_value = [{"ok": True}]

        import pandas as pd
        m5 = pd.DataFrame({"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05], "volume": [100]})
        backend.reader.get_all_timeframes.return_value = {"M5": m5, "H1": m5, "H4": m5, "D1": m5}
        backend.reader.symbol_tick.return_value = _tick(50000.0)
        backend.reader.symbol_trade_specs.return_value = {}
        backend.reader.account_snapshot.return_value = {"balance": 1000, "equity": 1000}
        backend.reader.hermes_open_positions_count.return_value = 0

        backend.strategy_manager.refresh_simo_symbol.return_value = broker_symbol
        backend.strategy_manager.log_active_status.return_value = None

        backend.time_engine.evaluate.return_value = {
            "time_gate_status": "PASS", "time_gate_reason": "TIME_GATE_PASS",
            "session_name": "LONDON", "is_weekend": False, "market_open": True,
        }
        # Setup hunter returns not-demo-eligible by default (so we don't reach order_send)
        hunter_result = MagicMock()
        hunter_result.decision = {"decision": "WAIT"}
        hunter_result.best_candidate = {"demo_eligible": False, "best_strategy": "SIMO_ATM_BREAKOUT"}
        backend.setup_hunter.evaluate.return_value = hunter_result

        # simo_atm_breakout.evaluate patched per test
        backend._simo_signal = simo_signal
        return backend

    def _latest_completed_spread(self, *a, **kw):
        return 5.0

    def test_simo_disabled_returns_skipped_count(self) -> None:
        backend = self._make_simo_backend(simo_enabled=False)
        backend._latest_completed_spread = self._latest_completed_spread
        items, counts = backend.run_simo_index_cycle()
        self.assertEqual(items, [])
        self.assertEqual(counts, {"analyzed": 0, "skipped": 1})

    def test_mt5_disconnected_returns_skipped_count(self) -> None:
        backend = self._make_simo_backend(simo_enabled=True, mt5_connected=False)
        backend._latest_completed_spread = self._latest_completed_spread
        items, counts = backend.run_simo_index_cycle()
        self.assertEqual(items, [])
        self.assertEqual(counts, {"analyzed": 0, "skipped": 1})

    def test_no_broker_symbol_returns_skipped_count(self) -> None:
        backend = self._make_simo_backend(broker_symbol=None)
        backend._latest_completed_spread = self._latest_completed_spread
        items, counts = backend.run_simo_index_cycle()
        self.assertEqual(items, [])
        self.assertEqual(counts, {"analyzed": 0, "skipped": 1})

    def test_no_m5_candles_returns_skipped_count(self) -> None:
        import pandas as pd
        backend = self._make_simo_backend()
        backend._latest_completed_spread = self._latest_completed_spread
        backend.reader.get_all_timeframes.return_value = {"M5": pd.DataFrame(), "H1": pd.DataFrame()}
        items, counts = backend.run_simo_index_cycle()
        self.assertEqual(items, [])
        self.assertEqual(counts, {"analyzed": 0, "skipped": 1})

    def test_simo_wait_signal_returns_analyzed_count(self) -> None:
        backend = self._make_simo_backend(simo_signal="WAIT")
        backend._latest_completed_spread = self._latest_completed_spread
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value={"signal": "WAIT", "reason": "SIMO_NO_ATM_SETUP"}):
            items, counts = backend.run_simo_index_cycle()
        self.assertEqual(items, [])
        self.assertEqual(counts["analyzed"], 1)
        self.assertEqual(counts["skipped"], 0)

    def test_simo_not_demo_eligible_returns_analyzed_count(self) -> None:
        backend = self._make_simo_backend(simo_signal="BUY")
        backend._latest_completed_spread = self._latest_completed_spread
        # hunter returns not demo_eligible
        hunter_result = MagicMock()
        hunter_result.decision = {"decision": "WAIT"}
        hunter_result.best_candidate = {"demo_eligible": False, "best_strategy": "SIMO_ATM_BREAKOUT"}
        backend.setup_hunter.evaluate.return_value = hunter_result
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value={"signal": "BUY", "reason": "ATM_SIGNAL"}):
            items, counts = backend.run_simo_index_cycle()
        self.assertEqual(counts["analyzed"], 1)
        self.assertEqual(counts["skipped"], 0)

    def test_analyzed_plus_skipped_equals_one(self) -> None:
        """Every simo path must produce exactly analyzed+skipped=1."""
        cases = [
            {"simo_enabled": False},
            {"mt5_connected": False},
            {"broker_symbol": None},
        ]
        for case in cases:
            with self.subTest(case=case):
                backend = self._make_simo_backend(**case)
                backend._latest_completed_spread = self._latest_completed_spread
                _, counts = backend.run_simo_index_cycle()
                total = counts["analyzed"] + counts["skipped"]
                self.assertEqual(total, 1, f"analyzed+skipped={total} for case {case}, expected 1")


# ── 5. Full cycle analyzed+skipped invariant ──────────────────────────────────

class TestCycleCountInvariant(unittest.TestCase):
    """After run_cycle: analyzed + skipped == len(hermes_main_symbol_list)."""

    def test_analyzed_plus_skipped_equals_main_symbol_count(self) -> None:
        """Verify simo counts propagate into _cycle_status so total == 4."""
        s = _settings()
        from app.main import HermesBackend
        backend = HermesBackend.__new__(HermesBackend)
        backend.settings = s
        backend.ingest_client = MagicMock()
        backend.ingest_client.emit_bot_log.return_value = {}
        backend.ingest_client.send_bulk.return_value = {"sent": 0, "failed": 0}
        backend.ingest_client.log_event.return_value = {}
        backend.ingest_client.set_time_snapshot.return_value = None
        backend.ingest_client.send_row.return_value = {"ok": True}
        backend.heartbeat = MagicMock()
        backend.reader = MagicMock()
        backend.mt5 = MagicMock()
        backend.mt5.connected = False
        backend.paper_trader = MagicMock()
        backend.paper_trader.process_closures.return_value = []
        backend.demo_router = MagicMock()
        backend.demo_router.process_quick_exits.return_value = []
        backend.setup_hunter = MagicMock()
        backend.time_engine = MagicMock()
        backend.time_engine.evaluate.return_value = {
            "time_gate_status": "BLOCK", "time_gate_reason": "TEST",
            "session_name": "OFF", "is_weekend": True, "market_open": False,
        }
        backend.strategy_manager = MagicMock()
        backend.strategy_manager.simo_enabled = False  # SIMO disabled → skipped
        backend.agent = MagicMock()
        backend.learning_optimizer = MagicMock()
        backend.resolved_symbols = {"BTCUSD": "BTCUSD", "XAUUSD": "GOLD#", "EURUSD": "EURUSD"}
        backend._per_symbol_state = {}
        backend._cycle_status = {"last_cycle_end_utc": None}
        backend._cycle_active = False
        backend._live_snapshot_lock = MagicMock()
        backend._live_snapshot_lock.acquire.return_value = True
        backend._latest_candidates_all = []
        backend._latest_safety_guard = {}
        backend.latest_agent_state = None
        backend.latest_demo_event = None
        backend.latest_setup_hunter = None
        backend.latest_position_sync = {}
        backend.latest_order_flow_snapshots = []
        backend.sent_candle_keys = set()

        import pandas as pd
        empty_df = pd.DataFrame()
        backend.reader.latest_candle_rows.return_value = []
        backend.reader.get_all_timeframes.return_value = {"M5": empty_df, "H1": empty_df}
        backend.reader.symbol_tick.return_value = None
        backend.reader.symbol_trade_specs.return_value = {}
        backend.reader.account_snapshot.return_value = {"balance": 1000, "equity": 1000}
        backend.reader.hermes_open_positions_count.return_value = 0

        backend.run_cycle()

        cs = backend._cycle_status
        total = cs["analyzed"] + cs["skipped"]
        # 3 resolved symbols (BTCUSD, XAUUSD, EURUSD) all skipped (no M5), SIMO disabled = skipped
        # Total must be 4 == len(hermes_main_symbol_list)
        expected = len(s.hermes_main_symbol_list)
        self.assertEqual(total, expected,
            f"analyzed={cs['analyzed']} skipped={cs['skipped']} total={total} expected={expected}")


# ── 6. BTC 24/7 unchanged ─────────────────────────────────────────────────────

class TestBTC247Unchanged(unittest.TestCase):
    """BTC 24/7 weekend PASS still works after cycle semantics patch."""

    def test_btc_weekend_tick_bid_positive_passes(self) -> None:
        s = _settings(crypto_24_7_enabled=True, btc_weekend_analysis_only=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=_weekend(), tick=_tick(45000.0))
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "BTC_24_7_ALLOWED")

    def test_btc_weekend_no_tick_blocked(self) -> None:
        s = _settings(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=_weekend(), tick=None)
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "NO_RECENT_TICK")

    def test_btc_weekend_broker_closed_blocked(self) -> None:
        s = _settings(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=_weekend(), tick=_tick(0.0))
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "BROKER_SESSION_CLOSED")

    def test_gold_still_blocked_on_weekend(self) -> None:
        s = _settings()
        engine = TimeEngine(s)
        result = engine.evaluate("GOLD#", now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")

    def test_eurusd_still_blocked_on_weekend(self) -> None:
        s = _settings()
        engine = TimeEngine(s)
        result = engine.evaluate("EURUSD", now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")


# ── 7 & 8. Safety invariants ──────────────────────────────────────────────────

class TestCycleSemanticsSafetyInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        s = _settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_true(self) -> None:
        s = _settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_unchanged(self) -> None:
        s = _settings()
        self.assertAlmostEqual(s.demo_max_lot, 0.01)

    def test_allow_time_block_override_false(self) -> None:
        s = _settings()
        self.assertFalse(s.allow_time_block_override)

    def test_order_send_only_in_demo_router(self) -> None:
        """mt5.order_send must not appear in any production app/ file except demo_router.py."""
        app_dir = ROOT / "app"
        violations: list[str] = []
        for path in app_dir.rglob("*.py"):
            if path.as_posix().endswith("app/mt5/demo_router.py") or \
               path.as_posix().replace("\\", "/").endswith("app/mt5/demo_router.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                violations.append(str(path))
        self.assertEqual(violations, [], f"order_send found outside demo_router: {violations}")

    def test_cfg_sym_for_resolved_is_display_only_no_broker_changes(self) -> None:
        """_cfg_sym_for_resolved is pure (no side effects) and doesn't touch broker logic."""
        main_syms = ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]
        # Pure function: same input → same output, no state
        r1 = _cfg_sym_for_resolved("BTCUSD", main_syms)
        r2 = _cfg_sym_for_resolved("BTCUSD", main_syms)
        self.assertEqual(r1, r2)
        self.assertEqual(r1, "BTCUSD#")


# ── 9. SYMBOL_CYCLE_IN log format (log capture) ──────────────────────────────

def _make_minimal_backend(settings: Settings, resolved_symbols: dict) -> object:
    """Minimal HermesBackend stub for log-capture and count-propagation tests."""
    from app.main import HermesBackend
    import pandas as pd

    b = HermesBackend.__new__(HermesBackend)
    b.settings = settings
    b.ingest_client = MagicMock()
    b.ingest_client.emit_bot_log.return_value = {}
    b.ingest_client.send_bulk.return_value = {"sent": 0, "failed": 0}
    b.ingest_client.log_event.return_value = {}
    b.ingest_client.set_time_snapshot.return_value = None
    b.ingest_client.send_row.return_value = {"ok": True}
    b.heartbeat = MagicMock()
    b.reader = MagicMock()
    b.reader.account_snapshot.return_value = {"balance": 1000, "equity": 1000}
    b.reader.latest_candle_rows.return_value = []
    empty_df = pd.DataFrame()
    b.reader.get_all_timeframes.return_value = {
        "M5": empty_df, "H1": empty_df, "H4": empty_df, "D1": empty_df,
    }
    b.reader.symbol_tick.return_value = None
    b.reader.symbol_trade_specs.return_value = {}
    b.reader.hermes_open_positions_count.return_value = 0
    b.mt5 = MagicMock()
    b.mt5.connected = False
    b.paper_trader = MagicMock()
    b.paper_trader.process_closures.return_value = []
    b.demo_router = MagicMock()
    b.demo_router.process_quick_exits.return_value = []
    b.setup_hunter = MagicMock()
    b.time_engine = MagicMock()
    b.time_engine.evaluate.return_value = {
        "time_gate_status": "BLOCK", "time_gate_reason": "TEST",
        "session_name": "OFF", "is_weekend": True, "market_open": False,
    }
    b.strategy_manager = MagicMock()
    b.strategy_manager.simo_enabled = False  # SIMO off by default; override per-test
    b.agent = MagicMock()
    b.learning_optimizer = MagicMock()
    b.resolved_symbols = resolved_symbols
    b._per_symbol_state = {}
    b._cycle_status = {"last_cycle_end_utc": None}
    b._cycle_active = False
    b._live_snapshot_lock = MagicMock()
    b._live_snapshot_lock.acquire.return_value = True
    b._latest_candidates_all = []
    b._latest_safety_guard = {}
    b.latest_agent_state = None
    b.latest_demo_event = None
    b.latest_setup_hunter = None
    b.latest_position_sync = {}
    b.latest_order_flow_snapshots = []
    b.sent_candle_keys = set()
    return b


class TestSymbolCycleInLogFormat(unittest.TestCase):
    """[SYMBOL_CYCLE_IN] must log symbol= as the display name (BTCUSD#) not the raw key (BTCUSD).

    Regression guard: if _display_sym is replaced with requested_symbol the symbol= field reverts
    to the raw resolved key (e.g. 'BTCUSD') and these tests will fail.
    """

    def test_symbol_cycle_in_logs_canonical_display_name_for_btcusd(self) -> None:
        """[SYMBOL_CYCLE_IN] must emit symbol=BTCUSD# raw_symbol=BTCUSD broker_symbol=BTCUSD#."""
        import logging
        import re
        s = _settings()
        # resolved key is BTCUSD (raw); broker is BTCUSD# (with hash suffix)
        b = _make_minimal_backend(s, {"BTCUSD": "BTCUSD#", "XAUUSD": "GOLD#", "EURUSD": "EURUSD"})
        hermes_logger = logging.getLogger("hermes")

        with self.assertLogs(hermes_logger, level="INFO") as cm:
            b.run_cycle()

        # Find lines that contain SYMBOL_CYCLE_IN and reference BTCUSD
        cycle_in_lines = [
            line for line in cm.output
            if "SYMBOL_CYCLE_IN" in line and "BTCUSD" in line and "raw_symbol" in line
        ]
        self.assertTrue(cycle_in_lines, f"No [SYMBOL_CYCLE_IN] log for BTCUSD: {cm.output}")
        line = cycle_in_lines[0]

        # Primary symbol= must be the configured canonical name, not the raw resolved key
        m = re.search(r"\[SYMBOL_CYCLE_IN\]\s+symbol=(\S+)", line)
        self.assertIsNotNone(m, f"Could not parse symbol= from: {line!r}")
        self.assertEqual(m.group(1), "BTCUSD#",
            f"Expected display name BTCUSD# as primary symbol, got {m.group(1)!r} — "
            f"stale code uses raw resolved key 'BTCUSD' instead of configured 'BTCUSD#'")

        # raw_symbol must be the resolver key (BTCUSD without hash)
        self.assertIn("raw_symbol=BTCUSD", line,
            f"Expected raw_symbol=BTCUSD in log: {line!r}")

        # broker_symbol must be the MT5 broker symbol
        self.assertIn("broker_symbol=BTCUSD#", line,
            f"Expected broker_symbol=BTCUSD# in log: {line!r}")

    def test_symbol_cycle_in_primary_symbol_contains_hash(self) -> None:
        """Primary symbol= must include the # suffix when config has BTCUSD#."""
        import logging
        import re
        s = _settings()
        b = _make_minimal_backend(s, {"BTCUSD": "BTCUSD#"})
        hermes_logger = logging.getLogger("hermes")

        with self.assertLogs(hermes_logger, level="INFO") as cm:
            b.run_cycle()

        cycle_in_lines = [l for l in cm.output if "SYMBOL_CYCLE_IN" in l and "BTCUSD" in l]
        self.assertTrue(cycle_in_lines, "No [SYMBOL_CYCLE_IN] line found")
        line = cycle_in_lines[0]

        m = re.search(r"\[SYMBOL_CYCLE_IN\]\s+symbol=(\S+)", line)
        self.assertIsNotNone(m, f"symbol= not found in: {line!r}")
        primary = m.group(1)
        self.assertIn("#", primary,
            f"Primary symbol should be BTCUSD# (with #) but got {primary!r} — "
            f"stale code emits raw key without broker-hash suffix")

    def test_symbol_cycle_in_gold_maps_to_gold_hash(self) -> None:
        """XAUUSD resolved key must appear as symbol=GOLD# (config display name)."""
        import logging
        import re
        s = _settings()
        b = _make_minimal_backend(s, {"XAUUSD": "GOLD#"})
        hermes_logger = logging.getLogger("hermes")

        with self.assertLogs(hermes_logger, level="INFO") as cm:
            b.run_cycle()

        cycle_in_lines = [l for l in cm.output if "SYMBOL_CYCLE_IN" in l and "GOLD" in l]
        self.assertTrue(cycle_in_lines, f"No [SYMBOL_CYCLE_IN] for GOLD: {cm.output}")
        line = cycle_in_lines[0]
        m = re.search(r"\[SYMBOL_CYCLE_IN\]\s+symbol=(\S+)", line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "GOLD#",
            f"Expected GOLD# as display name, got {m.group(1)!r}")


# ── 10. SIMO analyzed/skipped count propagation (injection test) ──────────────

class TestSimoAnalyzedCountPropagation(unittest.TestCase):
    """run_simo_index_cycle analyzed/skipped counts must be added to _cycle_status.

    Regression guard: if lines `analyzed += simo_counts.get('analyzed', 0)` and
    `skipped += simo_counts.get('skipped', 0)` are removed, US100Cash# analysis
    results disappear from the cycle totals and these tests will fail.
    """

    def _backend(self, resolved_symbols: dict | None = None) -> object:
        s = _settings()
        return _make_minimal_backend(s, resolved_symbols or {})

    def test_simo_analyzed_count_propagates_to_cycle_status(self) -> None:
        """SIMO returning analyzed=1 must appear in _cycle_status['analyzed']."""
        b = self._backend()
        with patch.object(b, "run_simo_index_cycle", return_value=([], {"analyzed": 1, "skipped": 0})):
            b.run_cycle()
        self.assertEqual(b._cycle_status["analyzed"], 1,
            "SIMO analyzed=1 must increment _cycle_status['analyzed']; "
            "stale code loses US100Cash# count entirely")
        self.assertEqual(b._cycle_status["skipped"], 0)

    def test_simo_skipped_count_propagates_to_cycle_status(self) -> None:
        """SIMO returning skipped=1 must appear in _cycle_status['skipped']."""
        b = self._backend()
        with patch.object(b, "run_simo_index_cycle", return_value=([], {"analyzed": 0, "skipped": 1})):
            b.run_cycle()
        self.assertEqual(b._cycle_status["analyzed"], 0)
        self.assertEqual(b._cycle_status["skipped"], 1,
            "SIMO skipped=1 must increment _cycle_status['skipped']")

    def test_simo_analyzed_plus_main_skips_equals_total(self) -> None:
        """3 main symbols skipped + SIMO analyzed=1 must total to 4 (== len(hermes_main_symbol_list))."""
        s = _settings()
        b = _make_minimal_backend(s, {"BTCUSD": "BTCUSD#", "XAUUSD": "GOLD#", "EURUSD": "EURUSD"})
        b.paper_trader.process_closures.return_value = []

        # SIMO returns US100Cash# analyzed (e.g. WAIT signal processed)
        with patch.object(b, "run_simo_index_cycle", return_value=([], {"analyzed": 1, "skipped": 0})):
            b.run_cycle()

        cs = b._cycle_status
        # All 3 main symbols have empty M5 → each contributes skipped=1
        self.assertEqual(cs["skipped"], 3,
            f"Expected 3 skipped from main loop, got {cs['skipped']}")
        # SIMO contributes analyzed=1
        self.assertEqual(cs["analyzed"], 1,
            f"Expected analyzed=1 from SIMO, got {cs['analyzed']}; "
            f"stale code did not propagate run_simo_index_cycle counts")
        # Total must equal len(hermes_main_symbol_list) = 4
        total = cs["analyzed"] + cs["skipped"]
        expected = len(s.hermes_main_symbol_list)
        self.assertEqual(total, expected,
            f"analyzed={cs['analyzed']} + skipped={cs['skipped']} = {total}, expected {expected}")

    def test_cycle_completed_status_reflects_simo_analyzed(self) -> None:
        """After run_cycle, last_status=COMPLETED and analyzed includes SIMO contribution."""
        b = self._backend()
        with patch.object(b, "run_simo_index_cycle", return_value=([], {"analyzed": 1, "skipped": 0})):
            b.run_cycle()
        self.assertEqual(b._cycle_status["last_status"], "COMPLETED")
        self.assertGreaterEqual(b._cycle_status["analyzed"], 1,
            "SIMO-analyzed symbols must be reflected in completed cycle status")


if __name__ == "__main__":
    unittest.main()
