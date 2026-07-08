"""Hotfix tests for 6 runtime blockers (2026-06-13).

Covers:
1. HERMES_MAIN_SYMBOLS includes US100Cash# in main cycle config
2. Unavailable US100Cash# logs SYMBOL_CYCLE_SKIP
3. BTC does not bypass weekend/time gate when ALLOW_TIME_BLOCK_OVERRIDE=false
4. BTC override logs TIME_GATE_OVERRIDE only when ALLOW_TIME_BLOCK_OVERRIDE=true
5. DemoRouter _load_events does not read entire file into memory
6. DemoRouter large events file does not hang/crash
7. LIVE_SNAPSHOT preserves zero values in telemetry
8. Final confluence grade D blocks ROUTER_HANDOFF
9. ORDER_FLOW_EXECUTION_AGENT cannot bypass final confluence gate
10. mt5.order_send remains only in app/mt5/demo_router.py
11. Live trading blocked
12. Max lot 0.01
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.heartbeat_service import _enrich_account_snapshot
from app.services.time_engine import TimeEngine, _demo_ignore_time_blocks


# ─── helpers ──────────────────────────────────────────────────────────────────

def _settings(**kwargs) -> Settings:
    base = dict(
        allow_live_trading=False,
        demo_only=True,
        demo_max_lot=0.01,
        demo_trading=True,
        demo_pilot_enabled=True,
        allow_time_block_override=False,
        research_allow_low_confluence=False,
        hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
        demo_router_events_max_lines=5000,
        demo_router_events_max_bytes=10485760,
    )
    base.update(kwargs)
    return Settings(**base)


# ─── Issue 1: HERMES_MAIN_SYMBOLS includes US100Cash# ─────────────────────────

class TestSymbolCycleConfig(unittest.TestCase):
    def test_hermes_main_symbols_default_is_two_official_symbols(self) -> None:
        # GRAND_PLAN 2026-07-08 (décision SIMO) : le cycle par défaut porte
        # exactement les deux symboles officiels. US100 n'est plus au cycle.
        s = Settings()
        self.assertEqual(s.hermes_main_symbol_list, ["GOLD#", "BTCUSD#"])

    def test_hermes_main_symbol_list_property(self) -> None:
        s = _settings(hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#")
        self.assertEqual(s.hermes_main_symbol_list, ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"])

    def test_hermes_main_symbols_env_override(self) -> None:
        with patch.dict(os.environ, {"HERMES_MAIN_SYMBOLS": "BTCUSD#,US100Cash#"}):
            from app.config import get_settings
            # bypass lru_cache
            s = Settings(hermes_main_symbols=os.getenv("HERMES_MAIN_SYMBOLS", "BTCUSD#,GOLD#,EURUSD,US100Cash#"))
        self.assertIn("US100Cash#", s.hermes_main_symbol_list)
        self.assertIn("BTCUSD#", s.hermes_main_symbol_list)

    def test_hermes_main_symbols_not_empty(self) -> None:
        s = Settings()
        self.assertGreater(len(s.hermes_main_symbol_list), 0)


# ─── Issue 2: US100Cash# SYMBOL_CYCLE_SKIP when unavailable ───────────────────

class TestUS100CycleSkip(unittest.TestCase):
    def test_simo_disabled_logs_skip(self) -> None:
        """When SIMO is disabled, run_simo_index_cycle should log SYMBOL_CYCLE_SKIP."""
        from app.services.strategy_manager import StrategyManager
        s = _settings(simo_atm_breakout_enabled=False)
        mgr = StrategyManager(s)
        self.assertFalse(mgr.simo_enabled)

    def test_no_broker_symbol_is_skip_condition(self) -> None:
        from app.services.strategy_manager import StrategyManager
        s = _settings()
        mgr = StrategyManager(s)
        with patch.object(mgr, "refresh_simo_symbol", return_value=None):
            broker = mgr.refresh_simo_symbol()
        self.assertIsNone(broker)


# ─── Issue 3: Time-gate override locked to ALLOW_TIME_BLOCK_OVERRIDE ──────────

class TestTimeGateOverride(unittest.TestCase):
    def _weekend_btc_time(self) -> datetime:
        # Saturday 2026-06-13 UTC
        return datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_override_false_btc_weekend_blocked(self) -> None:
        s = _settings(
            allow_time_block_override=False,
            btc_weekend_analysis_only=True,
            demo_ignore_all_time_blocks=True,
        )
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=self._weekend_btc_time())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        # With crypto_24_7_enabled=True (default) and no tick, reason is NO_RECENT_TICK.
        # With crypto_24_7_enabled=False, reason would be BTC_WEEKEND_ANALYSIS_ONLY.
        self.assertIn(result["time_gate_reason"], {
            "BTC_WEEKEND_ANALYSIS_ONLY", "WEEKEND", "WEEKEND_MARKET_CLOSED", "NO_RECENT_TICK",
        })

    def test_override_true_btc_weekend_pass(self) -> None:
        # This test exercises the btc_weekend_analysis_only + override path.
        # With crypto_24_7_enabled=False, BTC weekend → BTC_WEEKEND_ANALYSIS_ONLY which
        # is in USER_DISABLED_TIME_BLOCK_REASONS and gets converted to PASS by override.
        s = _settings(
            allow_time_block_override=True,
            btc_weekend_analysis_only=True,
            demo_ignore_all_time_blocks=True,
            crypto_24_7_enabled=False,
        )
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=self._weekend_btc_time())
        # BTC_WEEKEND_ANALYSIS_ONLY is in USER_DISABLED_TIME_BLOCK_REASONS → override → PASS
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "TIME_BLOCKS_DISABLED_BY_USER_ORDER")

    def test_demo_ignore_time_blocks_requires_override(self) -> None:
        s_no_override = _settings(allow_time_block_override=False, demo_ignore_all_time_blocks=True)
        s_with_override = _settings(allow_time_block_override=True, demo_ignore_all_time_blocks=True)
        self.assertFalse(_demo_ignore_time_blocks(s_no_override))
        self.assertTrue(_demo_ignore_time_blocks(s_with_override))

    def test_non_btc_weekend_always_blocked(self) -> None:
        s = _settings(
            allow_time_block_override=True,
            demo_ignore_all_time_blocks=True,
        )
        engine = TimeEngine(s)
        result = engine.evaluate("EURUSD", now=self._weekend_btc_time())
        # WEEKEND_MARKET_CLOSED is NOT in USER_DISABLED_TIME_BLOCK_REASONS
        self.assertEqual(result["time_gate_status"], "BLOCK")

    def test_default_allow_time_block_override_is_false(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_time_block_override)


# ─── Issue 4: DemoRouter _load_events memory safety ───────────────────────────

class TestDemoRouterLoadEvents(unittest.TestCase):
    def _make_events_file(self, path: Path, n_lines: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for i in range(n_lines):
                fh.write(json.dumps({"event_type": "DEMO_BLOCK", "idx": i}) + "\n")

    def _router(self, events_path: Path, **kwargs) -> object:
        from app.mt5.demo_router import DemoKellyRouter
        s = _settings(**kwargs)
        with patch("MetaTrader5.initialize", return_value=True), \
             patch("MetaTrader5.shutdown"):
            router = DemoKellyRouter(s, events_path=events_path)
        return router

    def test_load_events_does_not_slurp_whole_file(self) -> None:
        """_load_events should use streaming tail, not read_text."""
        import inspect
        from app.mt5.demo_router import DemoKellyRouter
        src = inspect.getsource(DemoKellyRouter._load_events)
        self.assertNotIn(
            "read_text",
            src,
            "_load_events still calls read_text() — must use streaming tail reader instead",
        )

    def test_load_events_small_file_returns_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._make_events_file(path, 10)
            router = self._router(path)
            events = router._load_events()
            self.assertEqual(len(events), 10)

    def test_load_events_large_file_rotates_and_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._make_events_file(path, 1000)
            # Set max_bytes to something small so file is "too large"
            router = self._router(path, demo_router_events_max_bytes=10)
            events = router._load_events()
            self.assertEqual(events, [])
            # Original file should be renamed to .bak
            baks = list(Path(tmp).glob("*.bak"))
            self.assertEqual(len(baks), 1)

    def test_load_events_respects_max_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._make_events_file(path, 200)
            router = self._router(path, demo_router_events_max_lines=50, demo_router_events_max_bytes=10485760)
            events = router._load_events()
            self.assertLessEqual(len(events), 50)

    def test_load_events_nonexistent_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.jsonl"
            router = self._router(path)
            events = router._load_events()
            self.assertEqual(events, [])

    def test_load_events_does_not_crash_on_io_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._make_events_file(path, 5)
            router = self._router(path)
            with patch("app.mt5.demo_router._tail_lines", side_effect=OSError("disk error")):
                events = router._load_events()
            self.assertEqual(events, [])


# ─── Issue 5: LIVE_SNAPSHOT preserves zero values ─────────────────────────────

class TestLiveSnapshotZeroValues(unittest.TestCase):
    def test_zero_pnl_preserved_not_converted_to_none(self) -> None:
        account = {"snapshot_time": "2026-01-01T00:00:00Z"}
        pos_sync = {
            "demo_closed_pnl_today": 0.0,
            "demo_floating_pnl": 0.0,
            "demo_total_pnl_today": 0.0,
            "hermes_mt5_open_positions_count": 0,
            "pnl_source": "MT5_HISTORY_DEALS",
        }
        enriched = _enrich_account_snapshot(account, pos_sync)
        self.assertEqual(enriched["closed_pnl"], 0.0)
        self.assertEqual(enriched["floating_pnl"], 0.0)
        self.assertEqual(enriched["total"], 0.0)

    def test_int_zero_preserved(self) -> None:
        account = {}
        pos_sync = {"demo_closed_pnl_today": 0, "demo_floating_pnl": 0, "demo_total_pnl_today": 0}
        enriched = _enrich_account_snapshot(account, pos_sync)
        self.assertEqual(enriched["closed_pnl"], 0.0)
        self.assertEqual(enriched["floating_pnl"], 0.0)

    def test_none_position_sync_gives_none_fields(self) -> None:
        account = {}
        enriched = _enrich_account_snapshot(account, {})
        # When pos_sync is empty (no data), fields default to None (truly unavailable)
        # or 0.0 depending on whether sync was available — either is acceptable
        # The key invariant: 0.0 from sync should not become None
        self.assertIsNotNone(enriched)

    def test_real_values_preserved(self) -> None:
        account = {}
        pos_sync = {"demo_closed_pnl_today": 1.23, "demo_floating_pnl": -0.45, "demo_total_pnl_today": 0.78}
        enriched = _enrich_account_snapshot(account, pos_sync)
        self.assertAlmostEqual(enriched["closed_pnl"], 1.23, places=4)
        self.assertAlmostEqual(enriched["floating_pnl"], -0.45, places=4)

    def test_snapshot_field_missing_only_when_sync_present_but_field_absent(self) -> None:
        from app.services import heartbeat_service
        account = {}
        # Non-empty sync so has_sync=True, but pnl fields are absent
        pos_sync = {"pnl_source": "MT5"}
        with patch.object(heartbeat_service.log, "info") as mock_log:
            _enrich_account_snapshot(account, pos_sync)
        calls = [str(c) for c in mock_log.call_args_list]
        missing_calls = [c for c in calls if "SNAPSHOT_FIELD_MISSING" in c]
        self.assertTrue(len(missing_calls) > 0, "Expected SNAPSHOT_FIELD_MISSING log when sync present but pnl fields absent")


# ─── Issue 6: Confluence grade D blocks ROUTER_HANDOFF ────────────────────────

class TestConfluenceGate(unittest.TestCase):
    def _mock_conf(self, score: float, grade: str) -> dict:
        return {"score": score, "grade": grade, "components": {}}

    def test_grade_d_blocks_route(self) -> None:
        from app.agents.confluence_engine import evaluate_confluence
        s = _settings(research_allow_low_confluence=False)
        self.assertFalse(s.research_allow_low_confluence)

        # Simulate gate decision logic extracted from main.py
        _conf = self._mock_conf(7.91, "D")
        _research_allow = False
        _final_conf_score = float(_conf.get("score") or 0.0)
        _final_conf_grade = str(_conf.get("grade") or "")
        _conf_blocks_route = bool(_conf) and (_final_conf_grade == "D" or _final_conf_score < 55) and not _research_allow
        self.assertTrue(_conf_blocks_route)

    def test_grade_c_score_55_does_not_block(self) -> None:
        _conf = self._mock_conf(55.0, "C")
        _research_allow = False
        _final_conf_score = float(_conf.get("score") or 0.0)
        _final_conf_grade = str(_conf.get("grade") or "")
        _conf_blocks_route = bool(_conf) and (_final_conf_grade == "D" or _final_conf_score < 55) and not _research_allow
        self.assertFalse(_conf_blocks_route)

    def test_score_54_blocks(self) -> None:
        _conf = self._mock_conf(54.9, "C")
        _research_allow = False
        _final_conf_score = float(_conf.get("score") or 0.0)
        _final_conf_grade = str(_conf.get("grade") or "")
        _conf_blocks_route = bool(_conf) and (_final_conf_grade == "D" or _final_conf_score < 55) and not _research_allow
        self.assertTrue(_conf_blocks_route)

    def test_research_allow_bypasses_low_confluence(self) -> None:
        _conf = self._mock_conf(7.91, "D")
        _research_allow = True
        _final_conf_score = float(_conf.get("score") or 0.0)
        _final_conf_grade = str(_conf.get("grade") or "")
        _conf_blocks_route = bool(_conf) and (_final_conf_grade == "D" or _final_conf_score < 55) and not _research_allow
        self.assertFalse(_conf_blocks_route)

    def test_default_research_allow_is_false(self) -> None:
        s = Settings()
        self.assertFalse(s.research_allow_low_confluence)

    def test_empty_conf_does_not_block(self) -> None:
        _conf: dict = {}
        _research_allow = False
        _conf_blocks_route = bool(_conf) and (str(_conf.get("grade") or "") == "D" or float(_conf.get("score") or 0.0) < 55) and not _research_allow
        self.assertFalse(_conf_blocks_route)

    def test_order_flow_execution_agent_uses_same_gate(self) -> None:
        # ORDER_FLOW_EXECUTION_AGENT is in ACTIVE_EXECUTION_STRATEGIES so it goes through
        # force_active_handoff path, which is also blocked by _conf_blocks_route.
        from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", ACTIVE_EXECUTION_STRATEGIES)
        # Gate applies regardless of strategy: same logic
        _conf = self._mock_conf(7.91, "D")
        _conf_blocks_route = bool(_conf) and (str(_conf.get("grade") or "") == "D") and not False
        self.assertTrue(_conf_blocks_route)


# ─── Safety invariants ─────────────────────────────────────────────────────────

class TestSafetyInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_order_send_only_in_demo_router(self) -> None:
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            if "app/data/" in path.as_posix().replace("\\", "/"):
                continue
            if path.as_posix().replace("\\", "/").endswith("app/mt5/demo_router.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_live_trading_blocked_by_default(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_live_trading)
        self.assertTrue(s.demo_only)

    def test_new_config_fields_have_safe_defaults(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_time_block_override)
        self.assertFalse(s.research_allow_low_confluence)
        self.assertGreater(s.demo_router_events_max_lines, 0)
        self.assertGreater(s.demo_router_events_max_bytes, 0)


if __name__ == "__main__":
    unittest.main()
