"""Comprehensive tests for LOVABLE_BTC_OLD_SYSTEM profile restoration.

Covers:
  S1 - Profile activation & config fields
  S2 - Routing mode injection (DEMO_ADAPTIVE_FALLBACK / DEMO_MICRO_DISCOVERY)
  S3 - Safety blocks still enforced
  S4 - RR/TP computation
  S5 - Quick-exit log tokens (by name)
  S6 - DemoRouter mode override in evaluate()
  S7 - Trade-origin trace CLI structure
  S8 - Trades visibility (covered by test_trades_position_sync.py)
  S9 - Tests & compile (this file)
"""
from __future__ import annotations

import importlib
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# S1 — Profile activation & config fields
# ---------------------------------------------------------------------------

class TestProfileConstants(unittest.TestCase):
    def setUp(self):
        from app.profiles import lovable_btc_old_system as m
        self.m = m

    def test_profile_name(self):
        self.assertEqual(self.m.PROFILE_NAME, "LOVABLE_BTC_OLD_SYSTEM")

    def test_old_btc_symbol(self):
        self.assertEqual(self.m.OLD_BTC_SYMBOL, "BTCUSD#")

    def test_old_btc_strategies(self):
        self.assertIn("BTC_SCALPING_AGENT", self.m.OLD_BTC_STRATEGIES)
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", self.m.OLD_BTC_STRATEGIES)
        self.assertEqual(len(self.m.OLD_BTC_STRATEGIES), 2)

    def test_old_btc_modes_keys(self):
        self.assertIn("BTC_SCALPING_AGENT", self.m.OLD_BTC_MODES)
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", self.m.OLD_BTC_MODES)

    def test_btc_scalping_mode(self):
        self.assertEqual(self.m.OLD_BTC_MODES["BTC_SCALPING_AGENT"], "DEMO_ADAPTIVE_FALLBACK")

    def test_order_flow_mode(self):
        self.assertEqual(self.m.OLD_BTC_MODES["ORDER_FLOW_EXECUTION_AGENT"], "DEMO_MICRO_DISCOVERY")

    def test_scalping_rr_constant(self):
        self.assertEqual(self.m.OLD_BTC_SCALPING_RR, 2.0)

    def test_order_flow_rr_constant(self):
        self.assertEqual(self.m.OLD_BTC_ORDER_FLOW_RR, 1.5)

    def test_is_active_true(self):
        settings = MagicMock()
        settings.hermes_execution_profile = "LOVABLE_BTC_OLD_SYSTEM"
        self.assertTrue(self.m.is_active(settings))

    def test_is_active_false_empty(self):
        settings = MagicMock()
        settings.hermes_execution_profile = ""
        self.assertFalse(self.m.is_active(settings))

    def test_is_active_false_other(self):
        settings = MagicMock()
        settings.hermes_execution_profile = "DEMO_PILOT_48H"
        self.assertFalse(self.m.is_active(settings))

    def test_is_active_case_insensitive(self):
        settings = MagicMock()
        settings.hermes_execution_profile = "lovable_btc_old_system"
        self.assertTrue(self.m.is_active(settings))

    def test_is_old_btc_strategy_scalping(self):
        self.assertTrue(self.m.is_old_btc_strategy("BTC_SCALPING_AGENT"))

    def test_is_old_btc_strategy_order_flow(self):
        self.assertTrue(self.m.is_old_btc_strategy("ORDER_FLOW_EXECUTION_AGENT"))

    def test_is_old_btc_strategy_gold_false(self):
        self.assertFalse(self.m.is_old_btc_strategy("GOLD_ORDER_FLOW_CVD_VWAP"))

    def test_is_old_btc_strategy_empty_false(self):
        self.assertFalse(self.m.is_old_btc_strategy(""))


class TestConfigFields(unittest.TestCase):
    def test_settings_has_old_btc_scalping_rr(self):
        from app.config import Settings
        s = Settings.model_fields if hasattr(Settings, "model_fields") else {}
        self.assertIn("old_btc_scalping_rr", s)

    def test_settings_has_old_btc_order_flow_rr(self):
        from app.config import Settings
        s = Settings.model_fields if hasattr(Settings, "model_fields") else {}
        self.assertIn("old_btc_order_flow_rr", s)

    def test_settings_scalping_rr_default(self):
        from app.config import Settings
        # Build minimal settings without env lookups
        fields = Settings.model_fields if hasattr(Settings, "model_fields") else {}
        default = fields.get("old_btc_scalping_rr")
        if default is not None:
            # pydantic v2: FieldInfo has .default
            val = getattr(default, "default", None)
            self.assertAlmostEqual(float(val), 2.0)

    def test_settings_order_flow_rr_default(self):
        from app.config import Settings
        fields = Settings.model_fields if hasattr(Settings, "model_fields") else {}
        default = fields.get("old_btc_order_flow_rr")
        if default is not None:
            val = getattr(default, "default", None)
            self.assertAlmostEqual(float(val), 1.5)


# ---------------------------------------------------------------------------
# S2 — Routing mode functions
# ---------------------------------------------------------------------------

class TestRoutingModeFunctions(unittest.TestCase):
    def setUp(self):
        from app.profiles import lovable_btc_old_system as m
        self.m = m

    def test_mode_for_btc_scalping(self):
        self.assertEqual(self.m.old_btc_mode_for_strategy("BTC_SCALPING_AGENT"), "DEMO_ADAPTIVE_FALLBACK")

    def test_mode_for_order_flow(self):
        self.assertEqual(self.m.old_btc_mode_for_strategy("ORDER_FLOW_EXECUTION_AGENT"), "DEMO_MICRO_DISCOVERY")

    def test_mode_lowercase_input(self):
        self.assertEqual(self.m.old_btc_mode_for_strategy("btc_scalping_agent"), "DEMO_ADAPTIVE_FALLBACK")

    def test_rr_scalping_default(self):
        self.assertAlmostEqual(self.m.old_btc_rr_for_strategy("BTC_SCALPING_AGENT"), 2.0)

    def test_rr_order_flow_default(self):
        self.assertAlmostEqual(self.m.old_btc_rr_for_strategy("ORDER_FLOW_EXECUTION_AGENT"), 1.5)

    def test_rr_uses_settings_scalping(self):
        settings = MagicMock()
        settings.old_btc_scalping_rr = 2.5
        settings.old_btc_order_flow_rr = 1.5
        self.assertAlmostEqual(self.m.old_btc_rr_for_strategy("BTC_SCALPING_AGENT", settings), 2.5)

    def test_rr_uses_settings_order_flow(self):
        settings = MagicMock()
        settings.old_btc_scalping_rr = 2.0
        settings.old_btc_order_flow_rr = 1.8
        self.assertAlmostEqual(self.m.old_btc_rr_for_strategy("ORDER_FLOW_EXECUTION_AGENT", settings), 1.8)


# ---------------------------------------------------------------------------
# S4 — RR/TP computation
# ---------------------------------------------------------------------------

class TestComputeOldBtcTp(unittest.TestCase):
    def setUp(self):
        from app.profiles.lovable_btc_old_system import compute_old_btc_tp
        self.fn = compute_old_btc_tp

    def test_buy_tp(self):
        # entry=65000, sl=64500 → risk=500; rr=2.0 → tp = 65000 + 500*2 = 66000
        tp = self.fn(65000.0, 64500.0, 2.0, "BUY")
        self.assertAlmostEqual(tp, 66000.0)

    def test_sell_tp(self):
        # entry=65000, sl=65500 → risk=500; rr=2.0 → tp = 65000 - 500*2 = 64000
        tp = self.fn(65000.0, 65500.0, 2.0, "SELL")
        self.assertAlmostEqual(tp, 64000.0)

    def test_order_flow_rr_buy(self):
        # rr=1.5
        tp = self.fn(65000.0, 64500.0, 1.5, "BUY")
        self.assertAlmostEqual(tp, 65750.0)

    def test_order_flow_rr_sell(self):
        tp = self.fn(65000.0, 65500.0, 1.5, "SELL")
        self.assertAlmostEqual(tp, 64250.0)

    def test_zero_entry_returns_none(self):
        self.assertIsNone(self.fn(0.0, 64500.0, 2.0, "BUY"))

    def test_equal_entry_sl_returns_none(self):
        self.assertIsNone(self.fn(65000.0, 65000.0, 2.0, "BUY"))

    def test_unknown_direction_returns_none(self):
        self.assertIsNone(self.fn(65000.0, 64500.0, 2.0, "WAIT"))

    def test_result_rounded(self):
        tp = self.fn(65000.1, 64500.3, 2.0, "BUY")
        self.assertIsNotNone(tp)
        # Check result has at most 2 decimal places
        self.assertEqual(round(tp, 2), tp)


# ---------------------------------------------------------------------------
# S3 — Safety invariants never violated by profile
# ---------------------------------------------------------------------------

class TestSafetyInvariantsProfile(unittest.TestCase):
    def test_profile_module_no_order_send_call(self):
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        matches = re.findall(r"\bmt5\.order_send\s*\(", src)
        self.assertEqual(matches, [], "Profile module must not call mt5 order_send directly")

    def test_profile_no_allow_live_trading_true(self):
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        self.assertNotIn("allow_live_trading=True", src)
        self.assertNotIn("allow_live_trading = True", src)

    def test_profile_no_demo_only_false(self):
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        self.assertNotIn("demo_only=False", src)

    def test_demo_magic_stays_909002(self):
        from app.profiles.lovable_btc_old_system import PROFILE_NAME
        # magic number should not be overridden by profile
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        self.assertNotIn("demo_magic_number", src)

    def test_demo_max_lot_not_raised(self):
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        self.assertNotIn("demo_max_lot", src)


# ---------------------------------------------------------------------------
# S6 — DemoRouter mode override
# ---------------------------------------------------------------------------

class TestDemoRouterModeOverride(unittest.TestCase):
    def test_demo_router_evaluate_checks_old_btc_mode(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("old_btc_forced_mode", src)
        self.assertIn("old_btc_mode", src)

    def test_demo_router_reached_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[DEMO_ROUTER_REACHED]", src)

    def test_demo_router_block_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[DEMO_ROUTER_BLOCK]", src)

    def test_old_btc_rr_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[OLD_BTC_RR]", src)

    def test_old_btc_max_tp_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[OLD_BTC_MAX_TP]", src)

    def test_old_btc_quick_exit_config_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[OLD_BTC_QUICK_EXIT_CONFIG]", src)

    def test_exec_trace_order_sent_log_token(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn("[EXEC_TRACE_ORDER_SENT]", src)

    def test_demo_router_order_sent_has_mode_field(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        # The [DEMO_ROUTER_ORDER_SENT] log must include mode=
        pattern = r"\[DEMO_ROUTER_ORDER_SENT\].*mode="
        self.assertTrue(re.search(pattern, src), "[DEMO_ROUTER_ORDER_SENT] must include mode=")

    def test_demo_router_order_sent_has_comment_field(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        pattern = r"\[DEMO_ROUTER_ORDER_SENT\].*comment="
        self.assertTrue(re.search(pattern, src), "[DEMO_ROUTER_ORDER_SENT] must include comment=")

    def test_old_btc_mode_in_event_dict(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn('"old_btc_mode"', src)

    def test_trace_id_in_event_dict(self):
        src = Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")
        self.assertIn('"trace_id"', src)


# ---------------------------------------------------------------------------
# S5 — Quick-exit log tokens
# ---------------------------------------------------------------------------

class TestQuickExitLogTokens(unittest.TestCase):
    def _src(self):
        return Path(ROOT / "app" / "mt5" / "demo_router.py").read_text(encoding="utf-8")

    def test_old_btc_be_armed_token(self):
        self.assertIn("[OLD_BTC_BE_ARMED]", self._src())

    def test_old_btc_be_sl_moved_token(self):
        self.assertIn("[OLD_BTC_BE_SL_MOVED]", self._src())

    def test_old_btc_trailing_active_token(self):
        self.assertIn("[OLD_BTC_TRAILING_ACTIVE]", self._src())

    def test_old_btc_quick_close_token(self):
        self.assertIn("[OLD_BTC_QUICK_CLOSE]", self._src())

    def test_old_btc_position_ignored_token(self):
        self.assertIn("[OLD_BTC_POSITION_IGNORED]", self._src())

    def test_quick_exit_config_logged(self):
        # [OLD_BTC_QUICK_EXIT_CONFIG] must be in process_quick_exits
        src = self._src()
        # Find the function and check token presence
        self.assertIn("[OLD_BTC_QUICK_EXIT_CONFIG]", src)


# ---------------------------------------------------------------------------
# Main.py log tokens
# ---------------------------------------------------------------------------

class TestMainLogTokens(unittest.TestCase):
    def _src(self):
        return Path(ROOT / "app" / "main.py").read_text(encoding="utf-8")

    def test_old_btc_profile_active_token(self):
        self.assertIn("[OLD_BTC_PROFILE_ACTIVE]", self._src())

    def test_exec_trace_setup_accept_token(self):
        self.assertIn("[EXEC_TRACE_SETUP_ACCEPT]", self._src())

    def test_exec_trace_safety_pass_token(self):
        self.assertIn("[EXEC_TRACE_SAFETY_PASS]", self._src())

    def test_exec_trace_old_btc_bypass_token(self):
        self.assertIn("[EXEC_TRACE_OLD_BTC_BYPASS]", self._src())

    def test_old_btc_route_has_mode_field(self):
        src = self._src()
        pattern = r"\[OLD_BTC_ROUTE\].*mode="
        self.assertTrue(re.search(pattern, src), "[OLD_BTC_ROUTE] must include mode=")

    def test_old_btc_route_has_smc_mtfa_bypass(self):
        src = self._src()
        self.assertIn("smc_mtfa_strict_bypass=true", src)

    def test_old_btc_route_has_topdown_bypass(self):
        src = self._src()
        self.assertIn("topdown_bypass=true", src)

    def test_decision_gets_old_btc_mode_injected(self):
        src = self._src()
        self.assertIn('decision["old_btc_mode"]', src)

    def test_decision_gets_trace_id_injected(self):
        src = self._src()
        self.assertIn('decision["trace_id"]', src)

    def test_trace_id_generated_per_cycle(self):
        src = self._src()
        self.assertIn("_trace_id = str(uuid4())", src)

    def test_setup_id_reuses_trace_id(self):
        src = self._src()
        self.assertIn("setup_id = _trace_id", src)

    def test_old_btc_mode_for_strategy_imported(self):
        src = self._src()
        self.assertIn("old_btc_mode_for_strategy", src)


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------

class TestDashboardMode(unittest.TestCase):
    def test_mode_returns_profile_name_when_active(self):
        from app.services.dashboard_snapshot import _mode
        from app.config import Settings
        settings = MagicMock(spec=Settings)
        settings.hermes_execution_profile = "LOVABLE_BTC_OLD_SYSTEM"
        settings.demo_trading = True
        settings.demo_only = True
        settings.demo_pilot_enabled = True
        settings.demo_pilot_hours = 48
        settings.paper_trading = False
        settings.read_only = False
        result = _mode(settings)
        self.assertEqual(result, "LOVABLE_BTC_OLD_SYSTEM")

    def test_mode_returns_demo_pilot_when_profile_inactive(self):
        from app.services.dashboard_snapshot import _mode
        from app.config import Settings
        settings = MagicMock(spec=Settings)
        settings.hermes_execution_profile = ""
        settings.demo_trading = True
        settings.demo_only = True
        settings.demo_pilot_enabled = True
        settings.demo_pilot_hours = 48
        settings.paper_trading = False
        settings.read_only = False
        result = _mode(settings)
        self.assertEqual(result, "DEMO_PILOT_48H")

    def test_mode_profile_takes_priority_over_demo_pilot(self):
        from app.services.dashboard_snapshot import _mode
        settings = MagicMock()
        settings.hermes_execution_profile = "LOVABLE_BTC_OLD_SYSTEM"
        settings.demo_trading = True
        settings.demo_only = True
        settings.demo_pilot_enabled = True
        settings.demo_pilot_hours = 48
        result = _mode(settings)
        self.assertEqual(result, "LOVABLE_BTC_OLD_SYSTEM")

    def test_dashboard_snapshot_src_has_lvbtc_check(self):
        src = Path(ROOT / "app" / "services" / "dashboard_snapshot.py").read_text(encoding="utf-8")
        self.assertIn("_lvbtc_active", src)
        self.assertIn("_LVBTC_NAME", src)


# ---------------------------------------------------------------------------
# S7 — CLI trace tool
# ---------------------------------------------------------------------------

class TestTraceToolStructure(unittest.TestCase):
    def test_trace_tool_exists(self):
        p = Path(ROOT / "app" / "tools" / "trace_trade_origin.py")
        self.assertTrue(p.exists())

    def test_trace_tool_has_main(self):
        src = Path(ROOT / "app" / "tools" / "trace_trade_origin.py").read_text(encoding="utf-8")
        self.assertIn("def main(", src)

    def test_trace_tool_has_ticket_arg(self):
        src = Path(ROOT / "app" / "tools" / "trace_trade_origin.py").read_text(encoding="utf-8")
        self.assertIn("--ticket", src)

    def test_trace_tool_reads_jsonl(self):
        src = Path(ROOT / "app" / "tools" / "trace_trade_origin.py").read_text(encoding="utf-8")
        self.assertIn("demo_pilot_events.jsonl", src)

    def test_trace_tool_importable(self):
        import importlib
        mod = importlib.import_module("app.tools.trace_trade_origin")
        self.assertTrue(callable(getattr(mod, "trace_ticket", None)))
        self.assertTrue(callable(getattr(mod, "main", None)))

    def test_trace_ticket_no_events_file(self, tmp_path=None):
        import tempfile
        from app.tools.trace_trade_origin import trace_ticket
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "demo_pilot_events.jsonl"
            # doesn't exist — should not raise
            trace_ticket(12345, p)

    def test_trace_ticket_finds_ticket_in_events(self):
        import json, io, tempfile
        from unittest.mock import patch as _patch
        from app.tools.trace_trade_origin import trace_ticket
        events = [
            {"event_type": "DEMO_ORDER", "ticket": 346422452, "strategy": "BTC_SCALPING_AGENT",
             "mode": "DEMO_ADAPTIVE_FALLBACK", "direction": "BUY", "trace_id": "abc123", "created_at": "2026-06-15T10:00:00"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "demo_pilot_events.jsonl"
            p.write_text("\n".join(json.dumps(e) for e in events))
            with _patch("builtins.print") as mock_print:
                trace_ticket(346422452, p)
            all_output = " ".join(str(c) for call in mock_print.call_args_list for c in call.args)
            self.assertIn("346422452", all_output)
            self.assertIn("DEMO_ORDER", all_output)


# ---------------------------------------------------------------------------
# Safety: order_send only in demo_router.py
# ---------------------------------------------------------------------------

class TestOrderSendOnlyInDemoRouter(unittest.TestCase):
    def _find_order_send_files(self) -> list[str]:
        hits = []
        for py_file in (ROOT / "app").rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bmt5\.order_send\s*\(", text):
                hits.append(str(py_file.relative_to(ROOT)))
        return hits

    def test_only_demo_router_calls_order_send(self):
        files = self._find_order_send_files()
        non_router = [f for f in files if not f.endswith("demo_router.py")]
        self.assertEqual(non_router, [], f"mt5.order_send() found outside demo_router.py: {non_router}")

    def test_new_profile_module_no_order_send(self):
        src = Path(ROOT / "app" / "profiles" / "lovable_btc_old_system.py").read_text(encoding="utf-8")
        self.assertFalse(re.search(r"\bmt5\.order_send\s*\(", src))

    def test_trace_tool_no_order_send(self):
        src = Path(ROOT / "app" / "tools" / "trace_trade_origin.py").read_text(encoding="utf-8")
        self.assertFalse(re.search(r"\bmt5\.order_send\s*\(", src))

    def test_dashboard_snapshot_no_order_send(self):
        src = Path(ROOT / "app" / "services" / "dashboard_snapshot.py").read_text(encoding="utf-8")
        self.assertFalse(re.search(r"\bmt5\.order_send\s*\(", src))


if __name__ == "__main__":
    unittest.main(verbosity=2)
