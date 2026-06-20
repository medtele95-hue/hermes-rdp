"""Tests: US100Cash# tick and dashboard payload emission in run_simo_index_cycle().

Guards the fix for: WAIT path never fetched tick, leaving price=None in _per_symbol_state,
causing the dashboard to show "NO DATA" for US100Cash# even when MT5 has live quotes.

Covers:
  1. WAIT path populates price, spread, session, time_gate from tick/time_engine
  2. WAIT: latest_decision=WAIT, latest_reason=SIMO_NO_ATM_SETUP (from strategy result)
  3. Tick missing: price=None, latest_reason=NO_RECENT_TICK (honest, not generic)
  4. NOT_DEMO_ELIGIBLE path: price populated from already-fetched tick
  5. Dashboard _symbols_block reflects the populated state (available=True, price set)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.dashboard_snapshot import _symbols_block, dashboard_snapshot


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


def _make_simo_backend(tick_return: dict | None = None) -> object:
    """Minimal HermesBackend stub for run_simo_index_cycle tick-emission tests.

    MT5 is connected, SIMO is enabled, broker_symbol=US100Cash#.
    Tick is whatever the caller passes in (can be None for missing-tick tests).
    """
    import pandas as pd
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
    b._per_symbol_state = {}
    b.latest_order_flow_snapshots = {}
    b.ingest_client = MagicMock()
    b.ingest_client.send_bulk.return_value = [{"ok": True}]
    b._latest_completed_spread = lambda *a, **kw: 5.0

    m5 = pd.DataFrame({
        "open": [30100.0], "high": [30200.0], "low": [30050.0],
        "close": [30112.0], "volume": [1000],
    })
    b.reader.get_all_timeframes.return_value = {
        "M5": m5, "H1": m5, "H4": m5, "D1": m5,
    }
    b.reader.symbol_tick.return_value = tick_return
    b.reader.symbol_trade_specs.return_value = {}
    b.reader.account_snapshot.return_value = {"balance": 10000, "equity": 10000}
    b.reader.hermes_open_positions_count.return_value = 0

    b.time_engine.evaluate.return_value = {
        "time_gate_status": "PASS",
        "time_gate_reason": "TIME_GATE_PASS",
        "session_name": "LONDON",
        "is_weekend": False,
        "market_open": True,
    }

    hunter_result = MagicMock()
    hunter_result.decision = {"decision": "WAIT"}
    hunter_result.best_candidate = {
        "demo_eligible": False,
        "best_strategy": "SIMO_ATM_BREAKOUT",
    }
    b.setup_hunter.evaluate.return_value = hunter_result
    return b


_LIVE_TICK = {"bid": 30112.0, "ask": 30113.0, "last": 0.0, "time": 1718000000}
_LAST_TICK = {"bid": 30112.0, "ask": 30113.0, "last": 30112.8, "time": 1718000000}
_WAIT_RESULT = {"signal": "WAIT", "reason": "SIMO_NO_ATM_SETUP"}
_STATE_KEY = "US100CASH#"
_NO_TICK = object()  # sentinel: caller explicitly wants tick=None (not "use default")


# ── 1. WAIT path: price/spread/session/time_gate populated ────────────────────

class TestUS100WaitPathTickPopulation(unittest.TestCase):
    """WAIT path must fetch tick and populate _per_symbol_state with market data."""

    def _run_wait(self, tick=None):
        tick = tick if tick is not None else _LIVE_TICK
        b = _make_simo_backend(tick_return=tick)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        return b._per_symbol_state.get(_STATE_KEY, {})

    def test_us100_wait_price_mid_from_bid_ask(self) -> None:
        """price must be mid bid/ask when last=0 and tick has bid/ask."""
        entry = self._run_wait(_LIVE_TICK)
        self.assertIsNotNone(entry.get("price"),
            "price must not be None after WAIT; stale code never fetched tick on WAIT path")
        self.assertAlmostEqual(entry["price"], 30112.5, places=2,
            msg=f"Expected mid 30112.5, got {entry['price']}")

    def test_us100_wait_price_uses_last_when_available(self) -> None:
        """price must prefer tick.last when > 0."""
        entry = self._run_wait(_LAST_TICK)
        self.assertAlmostEqual(entry.get("price"), 30112.8, places=2,
            msg=f"Expected last=30112.8, got {entry.get('price')}")

    def test_us100_wait_spread_populated(self) -> None:
        """spread must be ask - bid = 1.0."""
        entry = self._run_wait(_LIVE_TICK)
        self.assertIsNotNone(entry.get("spread"), "spread must not be None")
        self.assertAlmostEqual(entry["spread"], 1.0, places=4)

    def test_us100_wait_spread_status_ok(self) -> None:
        """spread_status must be OK when tick has valid bid/ask."""
        entry = self._run_wait(_LIVE_TICK)
        self.assertEqual(entry.get("spread_status"), "OK")

    def test_us100_wait_session_from_time_engine(self) -> None:
        """session must be populated from time_engine.evaluate result."""
        entry = self._run_wait()
        self.assertEqual(entry.get("session"), "LONDON",
            f"Expected LONDON from mock, got {entry.get('session')!r}")

    def test_us100_wait_time_gate_from_time_engine(self) -> None:
        """time_gate must be populated from time_engine.evaluate result."""
        entry = self._run_wait()
        self.assertEqual(entry.get("time_gate"), "PASS",
            f"Expected PASS from mock, got {entry.get('time_gate')!r}")

    def test_us100_wait_broker_symbol_set(self) -> None:
        """broker_symbol must be the actual broker symbol string."""
        entry = self._run_wait()
        self.assertEqual(entry.get("broker_symbol"), "US100Cash#")


# ── 2. WAIT path: decision fields ────────────────────────────────────────────

class TestUS100WaitDecisionFields(unittest.TestCase):
    """WAIT path must emit correct decision/reason fields."""

    def _run_wait(self):
        b = _make_simo_backend(tick_return=_LIVE_TICK)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        return b._per_symbol_state.get(_STATE_KEY, {})

    def test_us100_wait_latest_decision_is_wait(self) -> None:
        entry = self._run_wait()
        self.assertEqual(entry.get("latest_decision"), "WAIT")

    def test_us100_wait_route_status_is_wait(self) -> None:
        entry = self._run_wait()
        self.assertEqual(entry.get("route_status"), "WAIT")

    def test_us100_wait_latest_reason_is_simo_no_atm_setup(self) -> None:
        """latest_reason must be the simo_result reason when tick exists."""
        entry = self._run_wait()
        self.assertEqual(entry.get("latest_reason"), "SIMO_NO_ATM_SETUP",
            f"Expected SIMO_NO_ATM_SETUP, got {entry.get('latest_reason')!r}")

    def test_us100_wait_returns_analyzed_count(self) -> None:
        """Counts must still be analyzed=1 skipped=0 after fix."""
        b = _make_simo_backend(tick_return=_LIVE_TICK)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            _, counts = b.run_simo_index_cycle()
        self.assertEqual(counts["analyzed"], 1)
        self.assertEqual(counts["skipped"], 0)


# ── 3. Tick missing: honest reason, not generic ───────────────────────────────

class TestUS100TickMissingReason(unittest.TestCase):
    """When tick is missing/zero, price=None and latest_reason=NO_RECENT_TICK."""

    def _run_wait(self, tick):
        b = _make_simo_backend(tick_return=tick)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        return b._per_symbol_state.get(_STATE_KEY, {})

    def test_us100_tick_none_price_is_none(self) -> None:
        entry = self._run_wait(None)
        self.assertIsNone(entry.get("price"),
            "price must be None when tick is not available")

    def test_us100_tick_none_reason_is_no_recent_tick(self) -> None:
        entry = self._run_wait(None)
        self.assertEqual(entry.get("latest_reason"), "NO_RECENT_TICK",
            f"Expected NO_RECENT_TICK, got {entry.get('latest_reason')!r}")

    def test_us100_tick_empty_dict_price_is_none(self) -> None:
        """Empty dict (all zeros) is treated as missing tick."""
        entry = self._run_wait({})
        self.assertIsNone(entry.get("price"))

    def test_us100_tick_zero_bid_price_is_none(self) -> None:
        """bid=0 ask=0 last=0 must not produce a fake 0.0 price."""
        entry = self._run_wait({"bid": 0.0, "ask": 0.0, "last": 0.0})
        self.assertIsNone(entry.get("price"),
            "Zero bid/ask must yield price=None, not 0.0")

    def test_us100_tick_missing_spread_status_is_no_tick(self) -> None:
        entry = self._run_wait(None)
        self.assertEqual(entry.get("spread_status"), "NO_TICK")

    def test_us100_tick_missing_reason_not_generic_unavailable(self) -> None:
        """NO_RECENT_TICK is specific; UNAVAILABLE_OR_NO_RATES is the stale init value."""
        entry = self._run_wait(None)
        self.assertNotEqual(entry.get("latest_reason"), "UNAVAILABLE_OR_NO_RATES",
            "Stale init value must be overwritten; got UNAVAILABLE_OR_NO_RATES unchanged")


# ── 4. NOT_DEMO_ELIGIBLE path ─────────────────────────────────────────────────

class TestUS100NotDemoEligibleTickEmission(unittest.TestCase):
    """NOT_DEMO_ELIGIBLE path must also populate _per_symbol_state from tick."""

    def _run_not_eligible(self, tick):
        b = _make_simo_backend(tick_return=tick)
        hunter_result = MagicMock()
        hunter_result.decision = {"decision": "WAIT"}
        hunter_result.best_candidate = {
            "demo_eligible": False,
            "best_strategy": "SIMO_ATM_BREAKOUT",
        }
        b.setup_hunter.evaluate.return_value = hunter_result
        with patch("app.strategies.simo_atm_breakout.evaluate",
                   return_value={"signal": "BUY", "reason": "ATM_SIGNAL"}):
            b.run_simo_index_cycle()
        return b._per_symbol_state.get(_STATE_KEY, {})

    def test_us100_not_eligible_price_populated(self) -> None:
        entry = self._run_not_eligible(_LIVE_TICK)
        self.assertIsNotNone(entry.get("price"),
            "price must not be None on NOT_DEMO_ELIGIBLE path when tick exists")
        self.assertAlmostEqual(entry["price"], 30112.5, places=2)

    def test_us100_not_eligible_spread_populated(self) -> None:
        entry = self._run_not_eligible(_LIVE_TICK)
        self.assertIsNotNone(entry.get("spread"))
        self.assertAlmostEqual(entry["spread"], 1.0, places=4)

    def test_us100_not_eligible_reason(self) -> None:
        """latest_reason must be NOT_DEMO_ELIGIBLE when tick exists."""
        entry = self._run_not_eligible(_LIVE_TICK)
        self.assertEqual(entry.get("latest_reason"), "NOT_DEMO_ELIGIBLE")

    def test_us100_not_eligible_tick_missing_honest_reason(self) -> None:
        entry = self._run_not_eligible(None)
        self.assertIsNone(entry.get("price"))
        self.assertEqual(entry.get("latest_reason"), "NO_RECENT_TICK")


# ── 5. Dashboard _symbols_block reflects state ───────────────────────────────

class TestUS100DashboardSymbolsBlock(unittest.TestCase):
    """dashboard_snapshot._symbols_block must show real data for US100Cash# after WAIT cycle."""

    def _symbols(self, tick=None):
        tick = tick if tick is not None else _LIVE_TICK
        b = _make_simo_backend(tick_return=tick)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        return _symbols_block(b._per_symbol_state, b.settings)

    def test_us100_dashboard_price_not_none_when_tick_exists(self) -> None:
        """symbols_block['US100Cash#']['price'] must not be None when tick has bid/ask."""
        us = self._symbols()["US100Cash#"]
        self.assertIsNotNone(us.get("price"),
            "Dashboard price must not be None when MT5 tick exists; "
            "stale code kept price=None from initialization")

    def test_us100_dashboard_available_true(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertTrue(us.get("available"), "available must be True")

    def test_us100_dashboard_enabled_true(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertTrue(us.get("enabled"))

    def test_us100_dashboard_in_main_cycle_true(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertTrue(us.get("in_main_cycle"))

    def test_us100_dashboard_latest_decision_wait(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertEqual(us.get("latest_decision"), "WAIT")

    def test_us100_dashboard_latest_reason_simo_no_atm_setup(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertEqual(us.get("latest_reason"), "SIMO_NO_ATM_SETUP",
            f"Expected SIMO_NO_ATM_SETUP, got {us.get('latest_reason')!r}")

    def test_us100_dashboard_route_status_wait(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertEqual(us.get("route_status"), "WAIT")

    def test_us100_dashboard_session_from_time_engine(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertEqual(us.get("session"), "LONDON")

    def test_us100_dashboard_time_gate_from_time_engine(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertEqual(us.get("time_gate"), "PASS")

    def test_us100_dashboard_tick_missing_price_none(self) -> None:
        """When tick is None, dashboard price must be None (honest)."""
        b = _make_simo_backend(tick_return=None)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        us = _symbols_block(b._per_symbol_state, b.settings).get("US100Cash#", {})
        self.assertIsNone(us.get("price"), "Honest: no tick → no price")

    def test_us100_dashboard_tick_missing_honest_reason(self) -> None:
        """When tick is None, latest_reason must be NO_RECENT_TICK."""
        b = _make_simo_backend(tick_return=None)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=_WAIT_RESULT):
            b.run_simo_index_cycle()
        us = _symbols_block(b._per_symbol_state, b.settings).get("US100Cash#", {})
        self.assertEqual(us.get("latest_reason"), "NO_RECENT_TICK")

    def test_us100_dashboard_spread_populated(self) -> None:
        us = self._symbols()["US100Cash#"]
        self.assertIsNotNone(us.get("spread"))


# ── 6. Full dashboard_snapshot payload regression ─────────────────────────────

class TestUS100DashboardStatusPayload(unittest.TestCase):
    """Regression: full dashboard_snapshot() must carry US100Cash# tick data
    through to the 'symbols' block after the WAIT path completes.

    Root cause guarded: _per_symbol_state['US100CASH#'] was absent before the
    first run_simo_index_cycle() call, causing available=false / price=null in
    all heartbeats that fired before SIMO ran (startup, background thread, and
    the first heartbeat in run_cycle() which fires before SIMO).
    """

    def _build_payload(self, tick=_NO_TICK, wait_result=None):
        # tick=_NO_TICK (default) → use _LIVE_TICK; tick=None → pass None to backend
        resolved_tick = _LIVE_TICK if tick is _NO_TICK else tick
        wait_result = wait_result or _WAIT_RESULT
        b = _make_simo_backend(tick_return=resolved_tick)
        with patch("app.strategies.simo_atm_breakout.evaluate", return_value=wait_result):
            b.run_simo_index_cycle()
        return dashboard_snapshot(b.settings, per_symbol_state=b._per_symbol_state)

    def _us100(self, tick=_NO_TICK, wait_result=None):
        payload = self._build_payload(tick=tick, wait_result=wait_result)
        return payload.get("symbols", {}).get("US100Cash#", {})

    def test_full_payload_us100_available_true(self) -> None:
        """After WAIT path, dashboard_snapshot symbols['US100Cash#']['available'] must be True."""
        us = self._us100()
        self.assertTrue(us.get("available"),
            "available must be True after run_simo_index_cycle WAIT path; "
            "root cause: key absent from _per_symbol_state before SIMO runs")

    def test_full_payload_us100_price_not_none(self) -> None:
        """price must survive from _per_symbol_state through dashboard_snapshot."""
        us = self._us100()
        self.assertIsNotNone(us.get("price"),
            f"price must not be None after WAIT path with live tick, got {us}")

    def test_full_payload_us100_spread_not_none(self) -> None:
        us = self._us100()
        self.assertIsNotNone(us.get("spread"))

    def test_full_payload_us100_session_not_none(self) -> None:
        us = self._us100()
        self.assertIsNotNone(us.get("session"),
            f"session must be set from time_engine after WAIT path, got {us}")

    def test_full_payload_us100_time_gate_not_none(self) -> None:
        us = self._us100()
        self.assertIsNotNone(us.get("time_gate"),
            f"time_gate must be set from time_engine after WAIT path, got {us}")

    def test_full_payload_us100_last_update_utc_not_none(self) -> None:
        """last_update_utc must not be null — it was null in the bug report."""
        us = self._us100()
        self.assertIsNotNone(us.get("last_update_utc"),
            "last_update_utc was null in production bug report; must be set after WAIT path")

    def test_full_payload_us100_latest_decision_wait(self) -> None:
        us = self._us100()
        self.assertEqual(us.get("latest_decision"), "WAIT")

    def test_full_payload_us100_latest_reason_simo_no_atm_setup(self) -> None:
        us = self._us100()
        self.assertEqual(us.get("latest_reason"), "SIMO_NO_ATM_SETUP")

    def test_full_payload_us100_route_status_wait(self) -> None:
        us = self._us100()
        self.assertEqual(us.get("route_status"), "WAIT")

    def test_full_payload_us100_enabled_true(self) -> None:
        us = self._us100()
        self.assertTrue(us.get("enabled"))

    def test_full_payload_us100_in_main_cycle_true(self) -> None:
        us = self._us100()
        self.assertTrue(us.get("in_main_cycle"))

    def test_full_payload_tick_missing_available_true(self) -> None:
        """Even with no tick, available must be True (key exists; price is honestly null)."""
        us = self._us100(tick=None)  # None → no tick from broker
        self.assertTrue(us.get("available"),
            "available must be True even with missing tick — key must exist in per_symbol_state")

    def test_full_payload_tick_missing_honest_reason(self) -> None:
        us = self._us100(tick=None)  # None → no tick from broker
        self.assertEqual(us.get("latest_reason"), "NO_RECENT_TICK")

    def test_full_payload_symbols_key_preserves_config_case(self) -> None:
        """symbols dict key must be 'US100Cash#' (config case), not 'US100CASH#'."""
        payload = self._build_payload()
        syms = payload.get("symbols", {})
        self.assertIn("US100Cash#", syms,
            f"Expected key 'US100Cash#' in payload symbols; found keys: {list(syms.keys())}")


if __name__ == "__main__":
    unittest.main()
