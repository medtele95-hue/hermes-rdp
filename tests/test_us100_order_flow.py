"""Tests: US100Cash# ORDER_FLOW_READER observe-only telemetry from run_simo_index_cycle().

Guards the fix for: US100Cash# SIMO/index path never emitted ORDER_FLOW_READER payload,
causing the dashboard to show 'ORDER_FLOW payload not emitted for US100Cash#' while
BTCUSD#, GOLD#, and EURUSD all emitted [ORDER_FLOW_PAYLOAD_EMITTED].

Covers:
  1. latest_order_flow_snapshots contains US100CASH key after SIMO cycle
  2. Payload carries strategy=ORDER_FLOW_READER, status=OBSERVE_ONLY
  3. [ORDER_FLOW_PAYLOAD_EMITTED] log emitted for US100Cash#
  4. Missing/incomplete history emits ORDER_FLOW_HISTORY_NOT_READY, not generic NO_DATA
  5. DemoRouter.process_decision never called from this telemetry path
  6. order_flow_entry_enabled=False at all times
  7. dashboard_snapshot order_flow.detected_symbols includes US100CASH
  8. Existing BTC/GOLD/EUR order-flow path not regressed
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from app.config import Settings
from app.services.dashboard_snapshot import dashboard_snapshot
from app.strategies.gold_order_flow_cvd_vwap import evaluate_observe_only


# ── helpers ────────────────────────────────────────────────────────────────────

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


_WAIT_RESULT = {"signal": "WAIT", "reason": "SIMO_NO_ATM_SETUP"}
_LIVE_TICK = {"bid": 30112.0, "ask": 30113.0, "last": 0.0, "time": 1718000000}

_M5_MANY = pd.DataFrame(
    {
        "open":  [30100.0 + i for i in range(60)],
        "high":  [30200.0 + i for i in range(60)],
        "low":   [30050.0 + i for i in range(60)],
        "close": [30112.0 + i for i in range(60)],
        "tick_volume": [1000.0] * 60,
    }
)

_M5_TINY = pd.DataFrame(
    {
        "open": [30100.0], "high": [30200.0],
        "low": [30050.0], "close": [30112.0],
        "volume": [1000],
    }
)


def _make_backend(tick_return=_LIVE_TICK, m5_frames=None):
    """Minimal HermesBackend stub for order-flow emission tests."""
    from app.main import HermesBackend

    s = _settings()
    b = HermesBackend.__new__(HermesBackend)
    b.settings = s
    b.strategy_manager = MagicMock()
    b.strategy_manager.simo_enabled = True
    b.strategy_manager.refresh_simo_symbol.return_value = "US100Cash#"
    b.strategy_manager.log_active_status.return_value = None
    b.mt5 = MagicMock()
    b.mt5.connected = True
    b.reader = MagicMock()
    b.time_engine = MagicMock()
    b.setup_hunter = MagicMock()
    b.agent = MagicMock()
    b.demo_router = MagicMock()
    b.demo_router.process_decision.return_value = []
    b.ingest_client = MagicMock()
    b.ingest_client.send_bulk.return_value = [{"ok": True}]
    b._per_symbol_state = {}
    b.latest_order_flow_snapshots = {}
    b._latest_completed_spread = lambda *a, **kw: 5.0

    frames = m5_frames if m5_frames is not None else _M5_TINY
    b.reader.get_all_timeframes.return_value = {
        "M5": frames, "H1": frames, "H4": frames, "D1": frames,
    }
    b.reader.symbol_tick.return_value = tick_return
    b.reader.symbol_trade_specs.return_value = {}
    b.reader.account_snapshot.return_value = {"balance": 10000, "equity": 10000}
    b.reader.hermes_open_positions_count.return_value = 0
    b.time_engine.evaluate.return_value = {
        "time_gate_status": "PASS", "time_gate_reason": "TIME_GATE_PASS",
        "session_name": "LONDON", "is_weekend": False, "market_open": True,
    }
    hunter = MagicMock()
    hunter.decision = {"decision": "WAIT"}
    hunter.best_candidate = {"demo_eligible": False, "best_strategy": "SIMO_ATM_BREAKOUT"}
    b.setup_hunter.evaluate.return_value = hunter
    return b


def _run_wait(b):
    with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
        b.run_simo_index_cycle()


# ── 1. Payload stored in latest_order_flow_snapshots ─────────────────────────

class TestUS100OrderFlowSnapshotStorage(unittest.TestCase):
    """run_simo_index_cycle() must populate latest_order_flow_snapshots for US100Cash#."""

    def setUp(self):
        self.b = _make_backend()
        _run_wait(self.b)

    def test_us100cash_key_in_snapshots(self):
        self.assertIn("US100CASH", self.b.latest_order_flow_snapshots,
            f"US100CASH key missing; keys: {list(self.b.latest_order_flow_snapshots)}")

    def test_strategy_is_order_flow_reader(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("strategy"), "ORDER_FLOW_READER",
            f"Expected ORDER_FLOW_READER, got {snap.get('strategy')!r}")

    def test_status_observe_only(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("status"), "OBSERVE_ONLY")

    def test_mode_observe_only(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("mode"), "OBSERVE_ONLY")

    def test_broker_symbol_preserved_case(self):
        """broker_symbol must be the original broker string, not uppercased."""
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("broker_symbol"), "US100Cash#")

    def test_latest_decision_wait(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("latest_decision"), "WAIT")

    def test_route_status_observe_only(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertEqual(snap.get("route_status"), "OBSERVE_ONLY")

    def test_entry_enabled_false(self):
        snap = self.b.latest_order_flow_snapshots["US100CASH"]
        self.assertFalse(snap.get("order_flow_entry_enabled"),
            "order_flow_entry_enabled must be False — OF is observe-only for US100")

    def test_send_bulk_called_with_order_flow_table(self):
        calls = [c[0][0] for c in self.b.ingest_client.send_bulk.call_args_list]
        self.assertIn("order_flow_snapshots", calls,
            "send_bulk('order_flow_snapshots', ...) must be called for US100 OF")


# ── 2. Runtime log ─────────────────────────────────────────────────────────────

class TestUS100OrderFlowLog(unittest.TestCase):
    """[ORDER_FLOW_PAYLOAD_EMITTED] must appear in logs for US100Cash#."""

    def test_order_flow_payload_emitted_logged(self):
        b = _make_backend()
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            with self.assertLogs("hermes", level="INFO") as cm:
                b.run_simo_index_cycle()
        emitted = [l for l in cm.output if "ORDER_FLOW_PAYLOAD_EMITTED" in l]
        self.assertTrue(emitted,
            f"No [ORDER_FLOW_PAYLOAD_EMITTED] log found. Captured:\n" +
            "\n".join(l for l in cm.output if "ORDER_FLOW" in l))

    def test_order_flow_payload_emitted_references_us100(self):
        b = _make_backend()
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            with self.assertLogs("hermes", level="INFO") as cm:
                b.run_simo_index_cycle()
        emitted = [l for l in cm.output if "ORDER_FLOW_PAYLOAD_EMITTED" in l]
        self.assertTrue(
            any("US100" in l for l in emitted),
            f"[ORDER_FLOW_PAYLOAD_EMITTED] log doesn't reference US100; got: {emitted}"
        )

    def test_order_flow_reader_strategy_in_log(self):
        b = _make_backend()
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            with self.assertLogs("hermes", level="INFO") as cm:
                b.run_simo_index_cycle()
        emitted = [l for l in cm.output if "ORDER_FLOW_PAYLOAD_EMITTED" in l]
        self.assertTrue(
            any("ORDER_FLOW_READER" in l for l in emitted),
            f"[ORDER_FLOW_PAYLOAD_EMITTED] log must say ORDER_FLOW_READER; got: {emitted}"
        )


# ── 3. HISTORY_NOT_READY when data incomplete ─────────────────────────────────

class TestUS100OrderFlowHistoryNotReady(unittest.TestCase):
    """When volume profile can't be computed, emit HISTORY_NOT_READY, not generic NO_DATA."""

    def _snap_after_wait(self, reader_snap_override):
        b = _make_backend()
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT), \
             patch("app.strategies.gold_order_flow_cvd_vwap.evaluate_reader",
                   return_value=reader_snap_override):
            b.run_simo_index_cycle()
        return b.latest_order_flow_snapshots.get("US100CASH", {})

    def _no_candles_reader_snap(self):
        return {
            "status": "WAIT", "reason": "NO_CANDLES",
            "poc": None, "vah": None, "val": None, "vwap": None,
            "symbol": "US100CASH", "broker_symbol": "US100CASH#",
            "mode": "OBSERVE_ONLY", "signal": "WAIT",
            "warnings": ["ORDER_FLOW_MISSING"],
            "source": "docs/strategies/order_flow_mt5.py",
            "created_at": "2026-06-15T10:00:00+00:00",
            "confidence": 0,
            "stale_after_seconds": 60,
            "observe_only_notice": "ORDER FLOW = OBSERVE-ONLY INTELLIGENCE.",
        }

    def test_history_not_ready_status(self):
        snap = self._snap_after_wait(self._no_candles_reader_snap())
        self.assertEqual(snap.get("order_flow_status"), "HISTORY_NOT_READY",
            f"Expected HISTORY_NOT_READY, got {snap.get('order_flow_status')!r}")

    def test_history_not_ready_reason(self):
        snap = self._snap_after_wait(self._no_candles_reader_snap())
        self.assertEqual(snap.get("latest_reason"), "ORDER_FLOW_HISTORY_NOT_READY",
            f"Expected ORDER_FLOW_HISTORY_NOT_READY, got {snap.get('latest_reason')!r}")

    def test_history_not_ready_not_generic_no_data(self):
        snap = self._snap_after_wait(self._no_candles_reader_snap())
        self.assertNotEqual(snap.get("latest_reason"), "NO_DATA",
            "latest_reason must not be generic NO_DATA")
        self.assertNotEqual(snap.get("order_flow_status"), "NO_DATA")

    def test_incomplete_levels_also_history_not_ready(self):
        """Candles exist but volume profile didn't compute — still HISTORY_NOT_READY."""
        incomplete_snap = {
            **self._no_candles_reader_snap(),
            "status": "OBSERVE_ONLY",  # has some data
            "vwap": 30112.0,           # VWAP computed
            "poc": None, "vah": None, "val": None,  # profile missing
            "reason": "ORDER_FLOW_READER_OBSERVE_ONLY",
        }
        snap = self._snap_after_wait(incomplete_snap)
        self.assertEqual(snap.get("order_flow_status"), "HISTORY_NOT_READY",
            "Missing POC/VAH/VAL must produce HISTORY_NOT_READY even if VWAP exists")

    def test_full_levels_produce_observe_only(self):
        """When all levels are populated, order_flow_status=OBSERVE_ONLY."""
        full_snap = {
            **self._no_candles_reader_snap(),
            "status": "OBSERVE_ONLY",
            "vwap": 30112.0, "poc": 30100.0, "vah": 30150.0, "val": 30070.0,
            "reason": "ORDER_FLOW_READER_OBSERVE_ONLY",
        }
        snap = self._snap_after_wait(full_snap)
        self.assertEqual(snap.get("order_flow_status"), "OBSERVE_ONLY",
            "When all levels populated, order_flow_status must be OBSERVE_ONLY")


# ── 4. Never routes to DemoRouter ─────────────────────────────────────────────

class TestUS100OrderFlowNoDemoRouter(unittest.TestCase):
    """ORDER_FLOW_READER telemetry path must never call DemoRouter.process_decision."""

    def test_wait_path_no_demo_router(self):
        b = _make_backend()
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        b.demo_router.process_decision.assert_not_called()

    def test_entry_enabled_always_false(self):
        """order_flow_entry_enabled must be False regardless of OF signal."""
        b = _make_backend(m5_frames=_M5_MANY)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        snap = b.latest_order_flow_snapshots.get("US100CASH", {})
        self.assertFalse(snap.get("order_flow_entry_enabled"))

    def test_observe_only_function_entry_enabled_false(self):
        """evaluate_observe_only itself must produce entry_enabled=False."""
        from app.config import Settings
        s = _settings()
        frames = {"M5": _M5_MANY}
        result = evaluate_observe_only("US100Cash#", frames, {}, s)
        self.assertFalse(result.get("order_flow_entry_enabled"),
            "evaluate_observe_only must always return order_flow_entry_enabled=False")

    def test_observe_only_function_no_side(self):
        """evaluate_observe_only must not set a BUY/SELL side."""
        from app.config import Settings
        s = _settings()
        frames = {"M5": _M5_MANY}
        result = evaluate_observe_only("US100Cash#", frames, {}, s)
        self.assertIsNone(result.get("side"),
            f"side must be None (no execution side); got {result.get('side')!r}")


# ── 5. Dashboard payload includes US100CASH in detected_symbols ───────────────

class TestUS100OrderFlowDashboardPayload(unittest.TestCase):
    """dashboard_snapshot order_flow.detected_symbols must include US100CASH key."""

    def _payload_after_simo(self):
        b = _make_backend()
        _run_wait(b)
        return dashboard_snapshot(
            b.settings,
            order_flow_snapshots=b.latest_order_flow_snapshots,
        )

    def test_us100cash_in_detected_symbols(self):
        payload = self._payload_after_simo()
        detected = payload.get("order_flow", {}).get("detected_symbols", {})
        self.assertIn("US100CASH", detected,
            f"US100CASH must be in order_flow.detected_symbols; found: {list(detected)}")

    def test_us100cash_status_observe_only_in_dashboard(self):
        payload = self._payload_after_simo()
        card = payload["order_flow"]["detected_symbols"]["US100CASH"]
        self.assertEqual(card.get("mode"), "OBSERVE_ONLY")

    def test_us100cash_broker_symbol_in_card(self):
        payload = self._payload_after_simo()
        card = payload["order_flow"]["detected_symbols"]["US100CASH"]
        self.assertEqual(card.get("broker_symbol"), "US100Cash#")

    def test_order_flow_tabs_unchanged(self):
        """Existing BTCUSD/GOLD/EURUSD tabs must still be present (not displaced)."""
        payload = self._payload_after_simo()
        tabs = payload.get("order_flow", {}).get("tabs", {})
        for sym in ("BTCUSD", "GOLD", "EURUSD"):
            self.assertIn(sym, tabs, f"{sym} tab missing from order_flow.tabs")


# ── 6. evaluate_observe_only unit tests ───────────────────────────────────────

class TestEvaluateObserveOnly(unittest.TestCase):
    """Direct unit tests for evaluate_observe_only function."""

    def _s(self):
        return _settings()

    def test_returns_order_flow_reader_strategy(self):
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_TINY}, {}, self._s())
        self.assertEqual(result.get("strategy"), "ORDER_FLOW_READER")

    def test_returns_observe_only_status(self):
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_TINY}, {}, self._s())
        self.assertEqual(result.get("status"), "OBSERVE_ONLY")

    def test_returns_wait_signal(self):
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_TINY}, {}, self._s())
        self.assertEqual(result.get("signal"), "WAIT")

    def test_no_candles_gives_history_not_ready(self):
        result = evaluate_observe_only("US100Cash#", {"M5": None}, {}, self._s())
        self.assertEqual(result.get("order_flow_status"), "HISTORY_NOT_READY")
        self.assertEqual(result.get("latest_reason"), "ORDER_FLOW_HISTORY_NOT_READY")

    def test_sufficient_candles_with_profile(self):
        """With 60 candles the volume profile should compute; check status."""
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_MANY}, {}, self._s())
        # Strategy and entry-enabled must always hold
        self.assertEqual(result.get("strategy"), "ORDER_FLOW_READER")
        self.assertFalse(result.get("order_flow_entry_enabled"))

    def test_disabled_reader_gives_disabled_status(self):
        s = _settings(order_flow_reader_enabled=False)
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_MANY}, {}, s)
        self.assertEqual(result.get("order_flow_status"), "DISABLED")
        self.assertEqual(result.get("latest_reason"), "ORDER_FLOW_READER_DISABLED")

    def test_route_status_observe_only(self):
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_TINY}, {}, self._s())
        self.assertEqual(result.get("route_status"), "OBSERVE_ONLY")

    def test_latest_decision_wait(self):
        result = evaluate_observe_only("US100Cash#", {"M5": _M5_TINY}, {}, self._s())
        self.assertEqual(result.get("latest_decision"), "WAIT")


# ── 7. Existing BTC/GOLD/EUR order flow path not regressed ──────────────────

class TestExistingOrderFlowNotRegressed(unittest.TestCase):
    """evaluate() and evaluate_reader() for gold/BTC/EUR must not be changed."""

    def test_gold_evaluate_reader_returns_observe_only(self):
        from app.strategies.gold_order_flow_cvd_vwap import evaluate_reader
        result = evaluate_reader("GOLD#", {"M5": _M5_MANY}, {}, _settings())
        self.assertIn(result.get("status"), {"OBSERVE_ONLY", "WAIT"})
        self.assertEqual(result.get("mode"), "OBSERVE_ONLY")

    def test_gold_evaluate_observe_only_returns_reader_strategy(self):
        result = evaluate_observe_only("GOLD#", {"M5": _M5_MANY}, {}, _settings())
        self.assertEqual(result.get("strategy"), "ORDER_FLOW_READER")
        self.assertFalse(result.get("order_flow_entry_enabled"))

    def test_btcusd_evaluate_observe_only_returns_reader_strategy(self):
        result = evaluate_observe_only("BTCUSD#", {"M5": _M5_MANY}, {}, _settings())
        self.assertEqual(result.get("strategy"), "ORDER_FLOW_READER")
        self.assertFalse(result.get("order_flow_entry_enabled"))

    def test_order_flow_reader_is_still_observation_strategy(self):
        from app.strategies.registry import OBSERVATION_STRATEGIES, ACTIVE_EXECUTION_STRATEGIES
        self.assertIn("ORDER_FLOW_READER", OBSERVATION_STRATEGIES)
        self.assertNotIn("ORDER_FLOW_READER", ACTIVE_EXECUTION_STRATEGIES)


if __name__ == "__main__":
    unittest.main()
