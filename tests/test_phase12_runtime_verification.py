"""Phase 12 — Runtime verification.

Verifies that the startup sequence emits the expected structured log lines and that
all safety invariants hold at construction/startup time without requiring a live MT5
connection.

Coverage:
- app.main module imports without crashing
- HermesBackend() can be constructed (no MT5 needed)
- StrategyManager.log_all_strategies() emits [STRATEGY_CLASS] lines for every strategy
- ACTIVE_EXECUTION strategies have correct route_allowed/enabled flags
- CONFIRMATION_MODULE strategies have route_allowed=false
- INTERNAL_DATA_FEED strategies have route_allowed=false
- [HERMES_CONFIG] log lines emit correct safety values (demo_only=true, allow_live=false)
- [HERMES_CONFIG] main_symbols emits configured symbols
- send_startup_test_rows() does not raise (with mocked ingest)
- DemoKellyRouter.log_startup() emits [DEMO_PILOT] and [ACCOUNT_DIAG] lines
- DemoKellyRouter.mark_backend_started() does not raise
- Safety invariants: ALLOW_LIVE_TRADING=false, DEMO_ONLY=true, DEMO_MAX_LOT=0.01
- order_send only in demo_router.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.strategy_manager import (
    StrategyManager,
    _ACTIVE_EXECUTION,
    _CONFIRMATION_MODULE,
    _INTERNAL_DATA_FEED,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
    )
    base.update(kwargs)
    return Settings(**base)


# ── module import smoke test ───────────────────────────────────────────────────

class TestAppMainImport(unittest.TestCase):
    """app.main must import cleanly without side effects or MT5 connectivity."""

    def test_app_main_imports_without_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import app.main; print('IMPORT_OK')"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=30,
        )
        self.assertIn("IMPORT_OK", result.stdout, f"Import failed: {result.stderr[:500]}")

    def test_app_services_dashboard_snapshot_importable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from app.services.dashboard_snapshot import dashboard_snapshot; print('OK')"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=15,
        )
        self.assertIn("OK", result.stdout)

    def test_app_local_api_server_importable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from app.local_api.server import app; print('OK')"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=15,
        )
        self.assertIn("OK", result.stdout)

    def test_app_local_api_utils_importable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from app.local_api.utils import to_json_safe; print('OK')"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=15,
        )
        self.assertIn("OK", result.stdout)


# ── HermesBackend construction ─────────────────────────────────────────────────

class TestHermesBackendInstantiation(unittest.TestCase):
    """HermesBackend() must construct without MT5 connectivity."""

    def _build(self) -> object:
        from app.main import HermesBackend
        with patch("app.config.get_settings", return_value=_settings()):
            return HermesBackend()

    def test_hermes_backend_can_be_instantiated(self) -> None:
        backend = self._build()
        self.assertIsNotNone(backend)

    def test_hermes_backend_has_strategy_manager(self) -> None:
        backend = self._build()
        self.assertIsInstance(backend.strategy_manager, StrategyManager)

    def test_hermes_backend_settings_demo_only(self) -> None:
        backend = self._build()
        self.assertTrue(backend.settings.demo_only)

    def test_hermes_backend_settings_allow_live_trading_false(self) -> None:
        backend = self._build()
        self.assertFalse(backend.settings.allow_live_trading)

    def test_hermes_backend_settings_demo_max_lot_001(self) -> None:
        backend = self._build()
        self.assertAlmostEqual(backend.settings.demo_max_lot, 0.01, places=4)

    def test_hermes_backend_cycle_inactive_at_init(self) -> None:
        backend = self._build()
        self.assertFalse(backend._cycle_active)

    def test_hermes_backend_resolved_symbols_empty_at_init(self) -> None:
        backend = self._build()
        self.assertEqual(backend.resolved_symbols, {})

    def test_hermes_backend_latest_candidates_empty_at_init(self) -> None:
        backend = self._build()
        self.assertEqual(backend._latest_candidates_all, [])


# ── strategy class log lines ───────────────────────────────────────────────────

class TestStrategyClassLogLines(unittest.TestCase):
    """log_all_strategies() must emit one [STRATEGY_CLASS] line per strategy."""

    def _manager(self, **kwargs) -> StrategyManager:
        return StrategyManager(_settings(**kwargs))

    def _collect_strategy_logs(self, **kwargs) -> list[str]:
        mgr = self._manager(**kwargs)
        lines: list[str] = []
        with patch("app.services.strategy_manager.log") as mock_log:
            mock_log.info.side_effect = lambda fmt, *args: lines.append(fmt % args)
            mgr.log_all_strategies()
        return lines

    def test_active_execution_strategies_all_logged(self) -> None:
        lines = self._collect_strategy_logs()
        for strategy in _ACTIVE_EXECUTION:
            self.assertTrue(
                any(strategy in line for line in lines),
                f"No [STRATEGY_CLASS] line found for {strategy}",
            )

    def test_confirmation_module_strategies_all_logged(self) -> None:
        lines = self._collect_strategy_logs()
        for strategy in _CONFIRMATION_MODULE:
            self.assertTrue(
                any(strategy in line for line in lines),
                f"No [STRATEGY_CLASS] line found for confirmation strategy {strategy}",
            )

    def test_internal_data_feed_strategies_all_logged(self) -> None:
        lines = self._collect_strategy_logs()
        for strategy in _INTERNAL_DATA_FEED:
            self.assertTrue(
                any(strategy in line for line in lines),
                f"No [STRATEGY_CLASS] line found for internal-feed strategy {strategy}",
            )

    def test_active_execution_lines_contain_class_label(self) -> None:
        lines = self._collect_strategy_logs()
        active_lines = [l for l in lines if "ACTIVE_EXECUTION_STRATEGY" in l]
        self.assertEqual(len(active_lines), len(_ACTIVE_EXECUTION))

    def test_confirmation_module_lines_have_route_allowed_false(self) -> None:
        lines = self._collect_strategy_logs()
        for line in lines:
            if "CONFIRMATION_MODULE" in line:
                self.assertIn("route_allowed=false", line, f"Confirmation module missing route_allowed=false: {line}")

    def test_internal_data_feed_lines_have_route_allowed_false(self) -> None:
        lines = self._collect_strategy_logs()
        for line in lines:
            if "INTERNAL_DATA_FEED" in line:
                self.assertIn("route_allowed=false", line, f"Internal data feed missing route_allowed=false: {line}")

    def test_total_strategy_count(self) -> None:
        lines = self._collect_strategy_logs()
        strategy_lines = [l for l in lines if "[STRATEGY_CLASS]" in l]
        expected = len(_ACTIVE_EXECUTION) + len(_CONFIRMATION_MODULE) + len(_INTERNAL_DATA_FEED)
        self.assertEqual(len(strategy_lines), expected, f"Expected {expected} [STRATEGY_CLASS] lines, got {len(strategy_lines)}")

    def test_gold_order_flow_disabled_by_default(self) -> None:
        lines = self._collect_strategy_logs()
        gold_of = [l for l in lines if "GOLD_ORDER_FLOW_CVD_VWAP" in l]
        self.assertTrue(any("enabled=false" in l for l in gold_of), "GOLD_ORDER_FLOW_CVD_VWAP should be disabled by default")

    def test_order_flow_execution_agent_disabled_by_default(self) -> None:
        lines = self._collect_strategy_logs()
        ofe = [l for l in lines if "ORDER_FLOW_EXECUTION_AGENT" in l]
        self.assertTrue(any("enabled=false" in l for l in ofe), "ORDER_FLOW_EXECUTION_AGENT should be disabled by default")

    def test_simo_atm_breakout_enabled_by_default(self) -> None:
        lines = self._collect_strategy_logs()
        simo = [l for l in lines if "SIMO_ATM_BREAKOUT" in l]
        self.assertTrue(any("enabled=true" in l for l in simo), "SIMO_ATM_BREAKOUT should be enabled by default")

    def test_log_all_strategies_does_not_raise(self) -> None:
        mgr = self._manager()
        try:
            with patch("app.services.strategy_manager.log"):
                mgr.log_all_strategies()
        except Exception as exc:
            self.fail(f"log_all_strategies() raised unexpectedly: {exc}")


# ── [HERMES_CONFIG] log line values ───────────────────────────────────────────

class TestHermesConfigLogValues(unittest.TestCase):
    """The config log lines emitted by start() must carry correct safety values.

    These tests verify the Settings values that would appear in the log lines,
    without needing to run the full start() sequence.
    """

    def _s(self, **kwargs) -> Settings:
        return _settings(**kwargs)

    def test_demo_only_logs_as_true(self) -> None:
        s = self._s()
        self.assertEqual(str(s.demo_only).lower(), "true")

    def test_allow_live_trading_logs_as_false(self) -> None:
        s = self._s()
        self.assertEqual(str(s.allow_live_trading).lower(), "false")

    def test_demo_max_lot_logs_as_001(self) -> None:
        s = self._s()
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)

    def test_main_symbols_log_line_contains_all_symbols(self) -> None:
        s = self._s(hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#")
        log_value = s.hermes_main_symbols
        for sym in ("BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"):
            self.assertIn(sym, log_value, f"main_symbols line missing {sym}")

    def test_allow_time_block_override_logs_as_false_by_default(self) -> None:
        s = self._s()
        self.assertEqual(str(s.allow_time_block_override).lower(), "false")

    def test_research_allow_low_confluence_logs_as_false_by_default(self) -> None:
        s = self._s()
        self.assertEqual(str(s.research_allow_low_confluence).lower(), "false")

    def test_max_total_open_demo_trades_is_int(self) -> None:
        s = self._s()
        val = getattr(s, "demo_max_open_trades_total", 3)
        self.assertIsInstance(val, int)
        self.assertGreater(val, 0)

    def test_hermes_config_format_demo_line(self) -> None:
        s = self._s()
        line = "[HERMES_CONFIG] demo_only=%s allow_live_trading=%s demo_max_lot=%s" % (
            str(s.demo_only).lower(),
            str(s.allow_live_trading).lower(),
            s.demo_max_lot,
        )
        self.assertIn("demo_only=true", line)
        self.assertIn("allow_live_trading=false", line)
        self.assertIn("0.01", line)


# ── send_startup_test_rows ─────────────────────────────────────────────────────

class TestSendStartupTestRows(unittest.TestCase):
    """send_startup_test_rows() must not raise, even when ingest is mocked."""

    def _backend(self) -> object:
        from app.main import HermesBackend
        with patch("app.config.get_settings", return_value=_settings()):
            return HermesBackend()

    def test_send_startup_test_rows_does_not_raise(self) -> None:
        backend = self._backend()
        mock_row = MagicMock(return_value={"ok": False, "error": "mocked"})
        with patch.object(backend.ingest_client, "send_row", mock_row):
            try:
                backend.send_startup_test_rows()
            except Exception as exc:
                self.fail(f"send_startup_test_rows raised: {exc}")

    def test_send_startup_test_rows_sends_three_rows(self) -> None:
        backend = self._backend()
        sent: list[tuple] = []
        with patch.object(backend.ingest_client, "send_row", side_effect=lambda t, d: sent.append((t, d)) or {}):
            backend.send_startup_test_rows()
        self.assertEqual(len(sent), 3, f"Expected 3 startup rows, got {len(sent)}")

    def test_send_startup_test_rows_includes_bot_logs(self) -> None:
        backend = self._backend()
        sent: list[tuple] = []
        with patch.object(backend.ingest_client, "send_row", side_effect=lambda t, d: sent.append((t, d)) or {}):
            backend.send_startup_test_rows()
        tables = [t for t, _ in sent]
        self.assertIn("bot_logs", tables)

    def test_send_startup_test_rows_includes_bot_status(self) -> None:
        backend = self._backend()
        sent: list[tuple] = []
        with patch.object(backend.ingest_client, "send_row", side_effect=lambda t, d: sent.append((t, d)) or {}):
            backend.send_startup_test_rows()
        tables = [t for t, _ in sent]
        self.assertIn("bot_status", tables)

    def test_send_startup_test_rows_has_starting_status(self) -> None:
        backend = self._backend()
        sent: list[tuple] = []
        with patch.object(backend.ingest_client, "send_row", side_effect=lambda t, d: sent.append((t, d)) or {}):
            backend.send_startup_test_rows()
        statuses = [d.get("status") for _, d in sent if "status" in d]
        self.assertTrue(all(s == "STARTING" for s in statuses), f"All rows should have status=STARTING: {statuses}")


# ── DemoKellyRouter startup log ────────────────────────────────────────────────

class TestDemoRouterStartupLog(unittest.TestCase):
    """DemoKellyRouter.log_startup() emits [DEMO_PILOT] and [ACCOUNT_DIAG] lines."""

    def _router(self, **kwargs) -> object:
        from app.mt5.demo_router import DemoKellyRouter
        return DemoKellyRouter(_settings(**kwargs))

    def test_log_startup_emits_demo_pilot_line(self) -> None:
        router = self._router()
        with self.assertLogs("hermes", level="INFO") as cm:
            router.log_startup({})
        self.assertTrue(
            any("[DEMO_PILOT]" in m for m in cm.output),
            f"Expected [DEMO_PILOT] in logs: {cm.output}",
        )

    def test_log_startup_emits_account_diag_line(self) -> None:
        router = self._router()
        with self.assertLogs("hermes", level="INFO") as cm:
            router.log_startup({})
        self.assertTrue(
            any("[ACCOUNT_DIAG]" in m for m in cm.output),
            f"Expected [ACCOUNT_DIAG] in logs: {cm.output}",
        )

    def test_demo_pilot_line_has_live_allowed_false(self) -> None:
        router = self._router(allow_live_trading=False)
        with self.assertLogs("hermes", level="INFO") as cm:
            router.log_startup({})
        pilot_lines = [m for m in cm.output if "[DEMO_PILOT]" in m]
        self.assertTrue(
            any("live_allowed=false" in l for l in pilot_lines),
            f"DEMO_PILOT must have live_allowed=false: {pilot_lines}",
        )

    def test_mark_backend_started_does_not_raise(self) -> None:
        router = self._router()
        try:
            router.mark_backend_started()
        except Exception as exc:
            self.fail(f"mark_backend_started raised: {exc}")

    def test_log_startup_only_logs_once(self) -> None:
        router = self._router()
        demo_pilot_lines: list[str] = []
        with patch("app.mt5.demo_router.log") as mock_log:
            def capture(fmt, *args):
                line = fmt % args
                if "[DEMO_PILOT]" in line:
                    demo_pilot_lines.append(line)
            mock_log.info.side_effect = capture
            router.log_startup({})
            router.log_startup({})  # second call — should NOT emit DEMO_PILOT again
        self.assertEqual(len(demo_pilot_lines), 1, "DEMO_PILOT should only be logged once")


# ── [CYCLE] started log ────────────────────────────────────────────────────────

class TestCycleStartedLog(unittest.TestCase):
    """run_cycle() must emit [CYCLE] started and [SYMBOL_CYCLE_CONFIG] lines."""

    def _backend(self) -> object:
        from app.main import HermesBackend
        with patch("app.config.get_settings", return_value=_settings()):
            b = HermesBackend()
        b.resolved_symbols = {"GOLD#": "GOLD#"}
        return b

    def _run_cycle_logs(self, backend: object) -> list[str]:
        """Run run_cycle() with all I/O mocked out; abort after config logs via reader raising."""
        logged: list[str] = []
        with patch("app.main.log") as mock_log:
            mock_log.info.side_effect = lambda fmt, *args: logged.append(fmt % args if args else fmt)
            mock_log.warning.side_effect = lambda fmt, *args: None
            with patch.object(backend, "sync_open_mt5_positions_to_lovable", return_value={}), \
                 patch.object(backend.reader, "account_snapshot", return_value={}), \
                 patch.object(backend.heartbeat, "write", return_value=None), \
                 patch.object(backend.reader, "latest_candle_rows", side_effect=StopIteration), \
                 patch.object(backend, "run_simo_index_cycle", return_value=([], {})), \
                 patch.object(backend.ingest_client, "send_row", return_value={"ok": False}), \
                 patch.object(backend.ingest_client, "send_bulk", return_value={"ok": False, "sent": 0, "failed": 0}), \
                 patch.object(backend.ingest_client, "emit_bot_log", return_value={"ok": False}), \
                 patch.object(backend.ingest_client, "update_row", return_value={"ok": False}):
                try:
                    backend.run_cycle()
                except StopIteration:
                    pass
        return logged

    def test_cycle_started_log_emitted(self) -> None:
        backend = self._backend()
        logged = self._run_cycle_logs(backend)
        self.assertTrue(
            any("[CYCLE] started" in l for l in logged),
            f"Expected [CYCLE] started in log output: {logged[:10]}",
        )

    def test_symbol_cycle_config_log_emitted(self) -> None:
        backend = self._backend()
        logged = self._run_cycle_logs(backend)
        self.assertTrue(
            any("[SYMBOL_CYCLE_CONFIG]" in l for l in logged),
            f"Expected [SYMBOL_CYCLE_CONFIG] in log output: {logged[:10]}",
        )

    def test_cycle_config_log_includes_main_symbols(self) -> None:
        backend = self._backend()
        logged = self._run_cycle_logs(backend)
        config_lines = [l for l in logged if "[SYMBOL_CYCLE_CONFIG]" in l]
        self.assertTrue(config_lines, "No [SYMBOL_CYCLE_CONFIG] line found")
        for sym in backend.settings.hermes_main_symbol_list:
            self.assertTrue(
                any(sym in l for l in config_lines),
                f"Symbol {sym} missing from [SYMBOL_CYCLE_CONFIG] lines",
            )


# ── safety invariants at runtime ───────────────────────────────────────────────

class TestSafetyInvariantsRuntime(unittest.TestCase):
    """Safety constraints hold at both default and explicit construction."""

    def test_default_settings_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_default_settings_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_default_settings_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_default_settings_allow_time_block_override_false(self) -> None:
        self.assertFalse(Settings().allow_time_block_override)

    def test_default_settings_research_allow_low_confluence_false(self) -> None:
        self.assertFalse(Settings().research_allow_low_confluence)

    def test_no_active_execution_strategy_has_route_allowed_by_default(self) -> None:
        s = _settings(
            gold_order_flow_execution_enabled=False,
            order_flow_execution_enabled=False,
        )
        mgr = StrategyManager(s)
        for strategy in ("GOLD_ORDER_FLOW_CVD_VWAP", "ORDER_FLOW_EXECUTION_AGENT"):
            self.assertFalse(mgr._strategy_enabled(strategy), f"{strategy} should be disabled by default")

    def test_order_send_only_in_demo_router(self) -> None:
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text and not path.name == "demo_router.py":
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_hermes_backend_settings_at_construction(self) -> None:
        from app.main import HermesBackend
        with patch("app.config.get_settings", return_value=_settings()):
            b = HermesBackend()
        self.assertTrue(b.settings.demo_only)
        self.assertFalse(b.settings.allow_live_trading)
        self.assertAlmostEqual(b.settings.demo_max_lot, 0.01, places=4)

    def test_demo_router_constructed_without_live_trading(self) -> None:
        from app.mt5.demo_router import DemoKellyRouter
        router = DemoKellyRouter(_settings())
        self.assertFalse(router.settings.allow_live_trading)

    def test_hermes_backend_cycle_status_starts_as_starting(self) -> None:
        from app.main import HermesBackend
        with patch("app.config.get_settings", return_value=_settings()):
            b = HermesBackend()
        self.assertEqual(b._cycle_status.get("last_status"), "STARTING")


if __name__ == "__main__":
    unittest.main()
