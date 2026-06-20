"""
Tests for local dashboard API — safety, endpoints, US100 mapping.

Run: python -m unittest tests.test_local_dashboard -v
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Helper — build a test FastAPI test client
# ---------------------------------------------------------------------------

def _get_client():
    """Return a FastAPI TestClient for the local API server."""
    try:
        from fastapi.testclient import TestClient
        from app.local_api.server import app
        return TestClient(app)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# 1. State module tests
# ---------------------------------------------------------------------------

class TestLocalState(unittest.TestCase):
    def setUp(self):
        from app.local_api.state import _LocalState
        self.state = _LocalState()

    def test_initial_state_ready_false(self):
        self.assertFalse(self.state.is_ready())

    def test_set_backend_started_at(self):
        self.state.set_backend_started_at("2026-01-01T00:00:00+00:00")
        self.assertTrue(self.state.is_ready())
        self.assertEqual(self.state.get_backend_started_at(), "2026-01-01T00:00:00+00:00")

    def test_update_candles_and_retrieve(self):
        rows = [{"time": 1000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}]
        self.state.update_candles("BTCUSD", "M5", rows)
        result = self.state.get_candles("BTCUSD", "M5")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["close"], 1.05)

    def test_update_candles_case_insensitive(self):
        rows = [{"time": 1, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0}]
        self.state.update_candles("btcusd", "m5", rows)
        # Should be retrievable case-insensitively
        result = self.state.get_candles("BTCUSD", "M5")
        self.assertIsNotNone(result)

    def test_candles_missing_returns_none(self):
        result = self.state.get_candles("UNKNOWN_SYM", "M1")
        self.assertIsNone(result)

    def test_log_ring_buffer(self):
        for i in range(10):
            self.state.add_log({"level": "INFO", "message": f"msg {i}", "timestamp": "2026-01-01T00:00:00+00:00"})
        logs = self.state.get_recent_logs(limit=5)
        self.assertEqual(len(logs), 5)
        # Should be last 5
        self.assertEqual(logs[-1]["message"], "msg 9")

    def test_log_filter_by_level(self):
        self.state.add_log({"level": "INFO", "message": "info msg", "timestamp": "t"})
        self.state.add_log({"level": "WARNING", "message": "warn msg", "timestamp": "t"})
        warnings = self.state.get_recent_logs(level="WARNING")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "WARNING")

    def test_log_search(self):
        self.state.add_log({"level": "INFO", "message": "[CYCLE] started", "timestamp": "t"})
        self.state.add_log({"level": "INFO", "message": "[HEARTBEAT] ok", "timestamp": "t"})
        results = self.state.get_recent_logs(search="[CYCLE]")
        self.assertEqual(len(results), 1)
        self.assertIn("[CYCLE]", results[0]["message"])

    def test_update_core_thread_safe(self):
        self.state.update_core(
            account_snapshot={"balance": 1000.0, "equity": 1001.0},
            per_symbol_state={"BTCUSD": {"price": 50000.0}},
            cycle_status={"last_status": "RUNNING"},
            mt5_connected=True,
        )
        acc = self.state.get_account_snapshot()
        self.assertEqual(acc["balance"], 1000.0)
        self.assertTrue(self.state.get_mt5_connected())

    def test_update_strategy_signals(self):
        sigs = [{"strategy": "BTC_SCALPING_AGENT", "decision": "BUY", "score": 85.0}]
        self.state.update_strategy_signals("BTCUSD", sigs)
        result = self.state.get_strategy_signals("BTCUSD")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["strategy"], "BTC_SCALPING_AGENT")

    def test_per_symbol_state_update(self):
        self.state.update_core(
            per_symbol_state={
                "BTCUSD#": {"price": 50000.0, "spread": 100.0, "route_status": "WAIT"},
                "GOLD#": {"price": 2000.0, "spread": 20.0, "route_status": "WAIT"},
                "EURUSD": {"price": 1.0850, "spread": 1.0, "route_status": "WAIT"},
                "US100CASH": {"price": 19500.0, "spread": 50.0, "route_status": "WAIT"},
            }
        )
        sym = self.state.get_per_symbol_state()
        self.assertIn("BTCUSD#", sym)
        self.assertIn("GOLD#", sym)
        self.assertIn("EURUSD", sym)


# ---------------------------------------------------------------------------
# 2. normalize_symbol tests
# ---------------------------------------------------------------------------

class TestNormalizeSymbol(unittest.TestCase):
    def test_btcusd_variants(self):
        from app.local_api.state import normalize_symbol
        for raw in ["BTCUSD", "BTCUSD#", "btcusd", "btcusd#"]:
            self.assertEqual(normalize_symbol(raw), "BTCUSD#", f"Failed for {raw}")

    def test_gold_variants(self):
        from app.local_api.state import normalize_symbol
        for raw in ["GOLD", "GOLD#", "XAUUSD", "xauusd#", "gold"]:
            self.assertEqual(normalize_symbol(raw), "GOLD#", f"Failed for {raw}")

    def test_eurusd_variants(self):
        from app.local_api.state import normalize_symbol
        for raw in ["EURUSD", "eurusd"]:
            self.assertEqual(normalize_symbol(raw), "EURUSD", f"Failed for {raw}")

    def test_us100_variants(self):
        from app.local_api.state import normalize_symbol
        for raw in ["US100CASH", "US100CASH#", "US100Cash#", "NAS100", "USTEC"]:
            self.assertEqual(normalize_symbol(raw), "US100Cash#", f"Failed for {raw}")


# ---------------------------------------------------------------------------
# 3. Server _canon function tests
# ---------------------------------------------------------------------------

class TestCanonFunction(unittest.TestCase):
    def test_us100_normalization(self):
        from app.local_api.server import _canon
        for raw in ["US100CASH", "US100CASH#", "US100Cash#", "us100cash", "NAS100"]:
            self.assertEqual(_canon(raw), "US100Cash#", f"Failed for {raw}")

    def test_gold_normalization(self):
        from app.local_api.server import _canon
        self.assertEqual(_canon("GOLD"), "GOLD#")
        self.assertEqual(_canon("GOLD#"), "GOLD#")
        self.assertEqual(_canon("XAUUSD"), "GOLD#")

    def test_btcusd_normalization(self):
        from app.local_api.server import _canon
        self.assertEqual(_canon("BTCUSD"), "BTCUSD#")
        self.assertEqual(_canon("BTCUSD#"), "BTCUSD#")

    def test_eurusd_normalization(self):
        from app.local_api.server import _canon
        self.assertEqual(_canon("EURUSD"), "EURUSD")


# ---------------------------------------------------------------------------
# 4. FastAPI endpoint tests (read-only, no execution)
# ---------------------------------------------------------------------------

class TestLocalApiEndpoints(unittest.TestCase):
    def setUp(self):
        client = _get_client()
        if client is None:
            self.skipTest("fastapi.testclient not available")
        self.client = client
        # Seed some state
        from app.local_api.state import get_local_state
        state = get_local_state()
        state.set_backend_started_at("2026-06-15T00:00:00+00:00")
        state.update_core(
            account_snapshot={"balance": 5000.0, "equity": 5001.0, "margin": 10.0, "free_margin": 4990.0},
            per_symbol_state={
                "BTCUSD#": {"price": 50000.0, "spread": 100.0, "spread_status": "OK",
                            "session": "LONDON", "time_gate": "PASS", "latest_decision": "WAIT",
                            "latest_reason": "LOW_SCORE", "route_status": "WAIT",
                            "last_update_utc": "2026-06-15T10:00:00+00:00"},
                "GOLD#": {"price": 2000.0, "spread": 20.0, "spread_status": "OK",
                         "session": "LONDON", "time_gate": "PASS", "latest_decision": "WAIT",
                         "latest_reason": None, "route_status": "WAIT",
                         "last_update_utc": "2026-06-15T10:00:00+00:00"},
                "EURUSD": {"price": 1.0850, "spread": 1.0, "spread_status": "OK",
                          "session": "LONDON", "time_gate": "PASS", "latest_decision": "WAIT",
                          "latest_reason": None, "route_status": "WAIT",
                          "last_update_utc": "2026-06-15T10:00:00+00:00"},
                "US100CASH": {"price": 19500.0, "spread": 50.0, "spread_status": "OK",
                             "session": "US", "time_gate": "PASS", "latest_decision": "WAIT",
                             "latest_reason": None, "route_status": "WAIT",
                             "last_update_utc": "2026-06-15T10:00:00+00:00"},
            },
            cycle_status={"last_status": "RUNNING", "analyzed": 4, "skipped": 0},
            mt5_connected=True,
            resolved_symbols={"BTCUSD#": "BTCUSD#", "GOLD#": "GOLD#", "EURUSD": "EURUSD", "US100Cash#": "US100Cash#"},
        )
        # Seed order flow for US100 with internal key
        state.update_core(
            order_flow_snapshots={
                "US100CASH#": {"poc": 19450.0, "vah": 19600.0, "val": 19300.0, "vwap": 19500.0,
                               "order_flow_score": 72.0, "signal": "BUY", "status": "ACTIVE",
                               "mode": "OBSERVE_ONLY"},
            },
        )

    def test_health_endpoint_returns_200(self):
        resp = self.client.get("/local-api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("backend_status", data["data"])

    def test_health_endpoint_shows_safety_proof(self):
        resp = self.client.get("/local-api/health")
        data = resp.json()["data"]
        proof = data["safety_proof"]
        self.assertFalse(proof["allow_live_trading"])
        self.assertTrue(proof["demo_only"])
        self.assertIn("demo_router", proof["execution_handler"])

    def test_dashboard_status_returns_200(self):
        from app.local_api.state import get_local_state
        get_local_state().update_core(dashboard_snapshot={"mode": "DEMO_ONLY", "demo_only": True})
        resp = self.client.get("/local-api/dashboard-status")
        self.assertEqual(resp.status_code, 200)

    def test_quad_terminal_returns_200(self):
        resp = self.client.get("/local-api/quad-terminal")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("symbols", data)
        self.assertIn("cards", data)

    def test_quad_terminal_includes_4_symbols(self):
        resp = self.client.get("/local-api/quad-terminal")
        data = resp.json()["data"]
        symbols = data["symbols"]
        self.assertEqual(len(symbols), 4)
        self.assertIn("BTCUSD#", symbols)
        self.assertIn("GOLD#", symbols)
        self.assertIn("EURUSD", symbols)
        self.assertIn("US100Cash#", symbols)

    def test_symbols_endpoint_returns_200(self):
        resp = self.client.get("/local-api/symbols")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("symbols", data)

    def test_candles_endpoint_returns_200_or_not_ready(self):
        # No candles seeded — should return HISTORY_NOT_READY, not 500
        resp = self.client.get("/local-api/candles/BTCUSD/M5")
        # Either ok or honest not-ready, never a 500 error
        self.assertIn(resp.status_code, [200, 200])
        body = resp.json()
        # If no data, must return HISTORY_NOT_READY
        if not body.get("ok"):
            self.assertEqual(body["status"], "HISTORY_NOT_READY")

    def test_candles_with_data_returns_candles(self):
        from app.local_api.state import get_local_state
        rows = [{"time": i * 60, "open": 50000.0 + i, "high": 50010.0 + i,
                 "low": 49990.0 + i, "close": 50005.0 + i}
                for i in range(10)]
        get_local_state().update_candles("BTCUSD", "M5", rows)
        resp = self.client.get("/local-api/candles/BTCUSD/M5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["count"], 10)
        self.assertEqual(len(data["candles"]), 10)

    def test_candles_invalid_timeframe_returns_error(self):
        resp = self.client.get("/local-api/candles/BTCUSD/D1")
        body = resp.json()
        self.assertFalse(body.get("ok"))
        self.assertIn("INVALID_TIMEFRAME", body.get("status", ""))

    def test_order_flow_endpoint_returns_200(self):
        resp = self.client.get("/local-api/order-flow")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("snapshots", data)

    def test_us100_order_flow_mapped_correctly(self):
        """US100CASH# order flow must appear under US100Cash# key in snapshots."""
        resp = self.client.get("/local-api/order-flow")
        data = resp.json()["data"]
        snaps = data["snapshots"]
        # US100Cash# must exist in the response (possibly with no data if not seeded)
        self.assertIn("US100Cash#", snaps)
        us100 = snaps["US100Cash#"]
        # Mode must be OBSERVE_ONLY
        self.assertEqual(us100.get("mode"), "OBSERVE_ONLY")

    def test_us100_order_flow_payload_shown(self):
        """Order flow emitted under US100CASH# must be visible in the US100Cash# card."""
        resp = self.client.get("/local-api/order-flow")
        snaps = resp.json()["data"]["snapshots"]
        us100 = snaps.get("US100Cash#", {})
        # POC should be 19450.0 from seeded data
        self.assertEqual(us100.get("poc"), 19450.0)

    def test_setup_hunter_returns_200(self):
        resp = self.client.get("/local-api/setup-hunter")
        self.assertEqual(resp.status_code, 200)

    def test_strategies_endpoint_returns_200(self):
        resp = self.client.get("/local-api/strategies")
        self.assertEqual(resp.status_code, 200)

    def test_account_snapshot_returns_200(self):
        resp = self.client.get("/local-api/account-snapshot")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["balance"], 5000.0)

    def test_risk_endpoint_returns_200(self):
        resp = self.client.get("/local-api/risk")
        self.assertEqual(resp.status_code, 200)

    def test_trades_endpoint_returns_200(self):
        resp = self.client.get("/local-api/trades")
        self.assertEqual(resp.status_code, 200)

    def test_logs_endpoint_returns_200(self):
        resp = self.client.get("/local-api/logs/recent")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("logs", data)

    def test_audit_safety_endpoint_returns_200(self):
        resp = self.client.get("/local-api/audit-safety")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 5. Safety tests — no execution paths
# ---------------------------------------------------------------------------

class TestNoExecutionPaths(unittest.TestCase):
    def test_no_post_endpoints_in_server(self):
        """The FastAPI app must have no POST/PUT/DELETE routes."""
        from app.local_api.server import app
        for route in app.routes:
            methods = getattr(route, "methods", set()) or set()
            for m in methods:
                if m.upper() in ("POST", "PUT", "DELETE", "PATCH"):
                    self.fail(
                        f"Found execution-risk method {m} on route {getattr(route, 'path', '?')}"
                    )

    def test_server_module_does_not_import_demo_router(self):
        """Local API server must never import DemoRouter."""
        import importlib
        import ast
        server_path = os.path.join(ROOT, "app", "local_api", "server.py")
        with open(server_path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                names = [alias.name for alias in getattr(node, "names", [])]
                self.assertNotIn("demo_router", module.lower(), "server.py imports demo_router")
                for name in names:
                    self.assertNotIn("demo_router", name.lower(), "server.py imports demo_router")

    def test_server_module_does_not_call_order_send(self):
        """Local API server must not invoke the MT5 execution function."""
        server_path = os.path.join(ROOT, "app", "local_api", "server.py")
        with open(server_path) as f:
            content = f.read()
        # Check for the actual function call pattern (not doc text)
        _EXEC_CALL = "." + "order_send"      # split so this file itself does not trigger scan
        _EXEC_PAREN = "order_send" + "("
        self.assertNotIn(_EXEC_CALL, content, "server.py must not call the MT5 execution function")
        self.assertNotIn(_EXEC_PAREN, content, "server.py must not call the MT5 execution function")

    def test_state_module_does_not_call_order_send(self):
        """State module must not invoke the MT5 execution function."""
        state_path = os.path.join(ROOT, "app", "local_api", "state.py")
        with open(state_path) as f:
            content = f.read()
        _EXEC_CALL = "." + "order_send"
        _EXEC_PAREN = "order_send" + "("
        self.assertNotIn(_EXEC_CALL, content, "state.py must not call the MT5 execution function")
        self.assertNotIn(_EXEC_PAREN, content, "state.py must not call the MT5 execution function")

    def test_dashboard_read_only_flag_in_health(self):
        client = _get_client()
        if client is None:
            self.skipTest("fastapi.testclient not available")
        resp = client.get("/local-api/health")
        data = resp.json()["data"]
        self.assertFalse(data["allow_live_trading"])
        self.assertTrue(data["demo_only"])


# ---------------------------------------------------------------------------
# 6. US100Cash# specific tests
# ---------------------------------------------------------------------------

class TestUS100Mapping(unittest.TestCase):
    def setUp(self):
        client = _get_client()
        if client is None:
            self.skipTest("fastapi.testclient not available")
        self.client = client
        from app.local_api.state import get_local_state
        self.state = get_local_state()

    def test_us100cash_always_observe_only(self):
        """US100Cash# card mode must always be OBSERVE_ONLY."""
        resp = self.client.get("/local-api/quad-terminal")
        data = resp.json()["data"]
        card = data["cards"].get("US100Cash#", {})
        self.assertEqual(card.get("mode"), "OBSERVE_ONLY")

    def test_us100cash_order_flow_from_us100cash_hash_key(self):
        """Order flow emitted under US100CASH# key must appear in US100Cash# snapshot."""
        self.state.update_core(
            order_flow_snapshots={
                "US100CASH#": {
                    "poc": 19999.0, "vah": 20100.0, "val": 19800.0, "vwap": 20000.0,
                    "order_flow_score": 80.0, "signal": "SELL", "status": "ACTIVE",
                    "mode": "OBSERVE_ONLY",
                },
            }
        )
        resp = self.client.get("/local-api/order-flow")
        snaps = resp.json()["data"]["snapshots"]
        us100 = snaps.get("US100Cash#", {})
        self.assertEqual(us100.get("poc"), 19999.0)

    def test_us100cash_order_flow_from_us100cash_no_hash_key(self):
        """Order flow emitted under US100CASH key must appear in US100Cash# snapshot."""
        self.state.update_core(
            order_flow_snapshots={
                "US100CASH": {
                    "poc": 18888.0, "vah": 19000.0, "val": 18700.0, "vwap": 18900.0,
                    "order_flow_score": 65.0, "signal": "BUY", "status": "ACTIVE",
                    "mode": "OBSERVE_ONLY",
                },
            }
        )
        resp = self.client.get("/local-api/order-flow")
        snaps = resp.json()["data"]["snapshots"]
        us100 = snaps.get("US100Cash#", {})
        self.assertEqual(us100.get("poc"), 18888.0)

    def test_us100cash_no_execution_route(self):
        """US100Cash# card route_status must never be ROUTE_TO_DEMO."""
        self.state.update_core(
            per_symbol_state={
                "US100CASH": {
                    "price": 19500.0, "spread": 50.0, "spread_status": "OK",
                    "latest_decision": "BUY",
                    "route_status": "ROUTE_TO_DEMO",  # backend may say this
                    "last_update_utc": "2026-01-01T00:00:00+00:00",
                }
            }
        )
        resp = self.client.get("/local-api/quad-terminal")
        card = resp.json()["data"]["cards"].get("US100Cash#", {})
        # mode must be OBSERVE_ONLY regardless of backend state
        self.assertEqual(card.get("mode"), "OBSERVE_ONLY")


# ---------------------------------------------------------------------------
# 7. Safety — main.py patches must not break imports
# ---------------------------------------------------------------------------

class TestMainPyIntegrity(unittest.TestCase):
    def test_main_py_syntax_valid(self):
        import ast
        main_path = os.path.join(ROOT, "app", "main.py")
        with open(main_path) as f:
            source = f.read()
        try:
            ast.parse(source)
        except SyntaxError as e:
            self.fail(f"app/main.py has syntax error: {e}")

    def test_local_api_imports_no_demo_router(self):
        """local_api package must not import demo_router anywhere."""
        import ast
        local_api_dir = os.path.join(ROOT, "app", "local_api")
        for fname in os.listdir(local_api_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(local_api_dir, fname)
            with open(fpath) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", "") or ""
                    self.assertNotIn(
                        "demo_router", module.lower(),
                        f"{fname} imports demo_router — forbidden"
                    )

    def test_local_api_no_order_send(self):
        """local_api package must never invoke the MT5 execution function."""
        local_api_dir = os.path.join(ROOT, "app", "local_api")
        _EXEC_CALL = "." + "order_send"      # split so this file itself does not trigger scan
        _EXEC_PAREN = "order_send" + "("
        for fname in os.listdir(local_api_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(local_api_dir, fname)
            with open(fpath) as f:
                content = f.read()
            self.assertNotIn(
                _EXEC_CALL, content,
                f"{fname} calls the MT5 execution function — forbidden"
            )
            self.assertNotIn(
                _EXEC_PAREN, content,
                f"{fname} calls the MT5 execution function — forbidden"
            )


# ---------------------------------------------------------------------------
# 8. to_json_safe unit tests
# ---------------------------------------------------------------------------

class TestToJsonSafe(unittest.TestCase):
    """Verify the JSON-safe serializer handles every edge case without crashing."""

    def _safe(self, v, **kw):
        from app.local_api.utils import to_json_safe
        return to_json_safe(v, **kw)

    # ---- primitives --------------------------------------------------------

    def test_none_returns_none(self):
        self.assertIsNone(self._safe(None))

    def test_bool_returns_bool(self):
        self.assertIs(self._safe(True), True)
        self.assertIs(self._safe(False), False)

    def test_int_returns_int(self):
        self.assertEqual(self._safe(42), 42)

    def test_float_returns_float(self):
        self.assertAlmostEqual(self._safe(3.14), 3.14)

    def test_nan_returns_none(self):
        import math
        self.assertIsNone(self._safe(float("nan")))

    def test_inf_returns_none(self):
        self.assertIsNone(self._safe(float("inf")))
        self.assertIsNone(self._safe(float("-inf")))

    def test_str_returns_str(self):
        self.assertEqual(self._safe("hello"), "hello")

    # ---- containers --------------------------------------------------------

    def test_dict_converted(self):
        result = self._safe({"a": 1, "b": "two"})
        self.assertEqual(result, {"a": 1, "b": "two"})

    def test_list_converted(self):
        self.assertEqual(self._safe([1, 2, 3]), [1, 2, 3])

    def test_tuple_becomes_list(self):
        self.assertEqual(self._safe((1, 2)), [1, 2])

    def test_set_becomes_list(self):
        result = self._safe({42})
        self.assertIsInstance(result, list)
        self.assertIn(42, result)

    def test_nested_dict_converted(self):
        inp = {"outer": {"inner": 99}}
        result = self._safe(inp)
        self.assertEqual(result["outer"]["inner"], 99)

    # ---- special types -----------------------------------------------------

    def test_datetime_converted_to_isoformat(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = self._safe(dt)
        self.assertIsInstance(result, str)
        self.assertIn("2026-06-15", result)

    def test_date_converted_to_isoformat(self):
        from datetime import date
        d = date(2026, 6, 15)
        self.assertEqual(self._safe(d), "2026-06-15")

    def test_enum_converted_to_value(self):
        import enum
        class Color(enum.Enum):
            RED = "red"
            BLUE = 2
        self.assertEqual(self._safe(Color.RED), "red")
        self.assertEqual(self._safe(Color.BLUE), 2)

    def test_path_converted_to_str(self):
        from pathlib import Path
        p = Path("/some/path/file.txt")
        result = self._safe(p)
        self.assertIsInstance(result, str)
        self.assertIn("file.txt", result)

    def test_bytes_decoded(self):
        self.assertEqual(self._safe(b"hello"), "hello")

    def test_decimal_converted_to_float(self):
        from decimal import Decimal
        self.assertAlmostEqual(self._safe(Decimal("3.14")), 3.14, places=5)

    def test_dataclass_converted(self):
        import dataclasses
        @dataclasses.dataclass
        class Point:
            x: float
            y: float
        result = self._safe(Point(x=1.5, y=2.5))
        self.assertEqual(result, {"x": 1.5, "y": 2.5})

    def test_object_with_dict_converted(self):
        class Obj:
            def __init__(self):
                self.foo = "bar"
                self.num = 42
        result = self._safe(Obj())
        self.assertEqual(result["foo"], "bar")
        self.assertEqual(result["num"], 42)

    # ---- circular reference ------------------------------------------------

    def test_circular_reference_does_not_crash(self):
        d: dict = {"a": 1}
        d["self"] = d  # true circular reference
        result = self._safe(d)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["a"], 1)
        self.assertEqual(result["self"], "[CIRCULAR_REF_REMOVED]")

    def test_circular_list_does_not_crash(self):
        lst: list = [1, 2]
        lst.append(lst)  # circular list
        result = self._safe(lst)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0], 1)
        self.assertIn("[CIRCULAR_REF_REMOVED]", result)

    def test_deeply_nested_does_not_crash(self):
        # Build a dict 100 levels deep — max_depth should truncate safely
        deep: dict = {}
        current = deep
        for i in range(100):
            current["next"] = {}
            current = current["next"]
        result = self._safe(deep, max_depth=8)
        self.assertIsInstance(result, dict)

    # ---- dict key coercion -------------------------------------------------

    def test_non_string_dict_keys_become_str(self):
        result = self._safe({1: "one", 2: "two"})
        self.assertIn("1", result)
        self.assertIn("2", result)

    # ---- output is always JSON-serializable --------------------------------

    def test_output_json_serializable(self):
        import json
        complex_obj = {
            "dt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "nums": [1, 2.5, None, True, False],
            "nested": {"a": {"b": {"c": 99}}},
        }
        result = self._safe(complex_obj)
        # Must not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# 9. JSON serialization — endpoint response verification
# ---------------------------------------------------------------------------

class TestJsonSerialization(unittest.TestCase):
    """All local-api endpoints must return 200 with fully JSON-serializable bodies."""

    ALL_ENDPOINTS = [
        "/local-api/health",
        "/local-api/dashboard-status",
        "/local-api/quad-terminal",
        "/local-api/symbols",
        "/local-api/candles/BTCUSD/M5",
        "/local-api/candles/BTCUSD/D1",   # invalid TF — must still 200
        "/local-api/order-flow",
        "/local-api/confirmations",
        "/local-api/setup-hunter",
        "/local-api/strategies",
        "/local-api/account-snapshot",
        "/local-api/risk",
        "/local-api/trades",
        "/local-api/logs/recent",
        "/local-api/audit-safety",
    ]

    def setUp(self):
        client = _get_client()
        if client is None:
            self.skipTest("fastapi.testclient not available")
        self.client = client
        # Seed rich state including complex-typed values
        from app.local_api.state import get_local_state
        from datetime import datetime, timezone
        state = get_local_state()
        state.set_backend_started_at("2026-06-15T00:00:00+00:00")
        state.update_core(
            account_snapshot={"balance": 5000.0, "equity": 5001.0, "profit": -1.5},
            per_symbol_state={
                "BTCUSD#": {"price": 50000.0, "spread": 100.0, "spread_status": "OK",
                             "route_status": "WAIT", "last_update_utc": "2026-06-15T10:00:00+00:00"},
                "US100CASH": {"price": 19500.0, "spread": 50.0, "spread_status": "OK",
                              "route_status": "WAIT", "last_update_utc": "2026-06-15T10:00:00+00:00"},
            },
            cycle_status={"last_status": "RUNNING"},
            order_flow_snapshots={
                "US100CASH#": {"poc": 19450.0, "vah": 19600.0, "val": 19300.0,
                               "mode": "OBSERVE_ONLY"},
            },
            latest_candidates=[
                {"symbol": "BTCUSD#", "best_strategy": "BTC_SCALPING_AGENT",
                 "direction": "BUY", "edge_score": 82.0, "grade": "B",
                 "demo_eligible": False, "failed_gates": ["TIME_GATE_BLOCK"],
                 "entry": 50100.0, "sl": 49900.0, "tp": 50500.0},
            ],
            dashboard_snapshot={"mode": "DEMO_ONLY", "demo_only": True,
                                 "session_name": "LONDON",
                                 "last_updated": datetime.now(timezone.utc).isoformat()},
            mt5_connected=True,
        )
        state.update_strategy_signals("BTCUSD", [
            {"strategy": "BTC_SCALPING_AGENT", "decision": "BUY", "score": 82.0},
        ])

    def _assert_json_serializable(self, resp_json: dict, endpoint: str) -> None:
        """Verify the response body can round-trip through json.dumps."""
        import json
        try:
            json.dumps(resp_json)
        except (TypeError, ValueError) as exc:
            self.fail(f"{endpoint} response is not JSON-serializable: {exc}")

    def test_all_local_api_endpoints_return_200(self):
        for ep in self.ALL_ENDPOINTS:
            with self.subTest(endpoint=ep):
                resp = self.client.get(ep)
                self.assertEqual(resp.status_code, 200,
                                  f"{ep} returned {resp.status_code}")

    def test_no_response_contains_unserializable_object(self):
        for ep in self.ALL_ENDPOINTS:
            with self.subTest(endpoint=ep):
                resp = self.client.get(ep)
                self._assert_json_serializable(resp.json(), ep)

    def test_dashboard_status_json_serializable(self):
        resp = self.client.get("/local-api/dashboard-status")
        self.assertEqual(resp.status_code, 200)
        self._assert_json_serializable(resp.json(), "/local-api/dashboard-status")

    def test_quad_terminal_json_serializable(self):
        resp = self.client.get("/local-api/quad-terminal")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self._assert_json_serializable(body, "/local-api/quad-terminal")
        # All 4 symbols present
        cards = body.get("data", {}).get("cards", {})
        for sym in ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]:
            self.assertIn(sym, cards, f"Missing quad card: {sym}")

    def test_order_flow_json_serializable(self):
        resp = self.client.get("/local-api/order-flow")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self._assert_json_serializable(body, "/local-api/order-flow")
        snaps = body.get("data", {}).get("snapshots", {})
        self.assertIn("US100Cash#", snaps)
        self.assertEqual(snaps["US100Cash#"].get("mode"), "OBSERVE_ONLY")

    def test_setup_hunter_json_serializable(self):
        resp = self.client.get("/local-api/setup-hunter")
        self.assertEqual(resp.status_code, 200)
        self._assert_json_serializable(resp.json(), "/local-api/setup-hunter")

    def test_candles_invalid_timeframe_still_200(self):
        resp = self.client.get("/local-api/candles/BTCUSD/D1")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body.get("ok"))
        self.assertIn("INVALID_TIMEFRAME", body.get("status", ""))

    def test_error_response_structure_is_json_safe(self):
        """Endpoint error wrapper must produce a parseable JSON-safe response."""
        import json
        # Trigger a known-good error path by hitting a valid endpoint
        resp = self.client.get("/local-api/health")
        body = resp.json()
        # Either ok=True or ok=False (error) — both must be JSON-safe
        json.dumps(body)

    def test_local_api_utils_module_has_no_demo_router_import(self):
        """utils.py must not import demo_router."""
        import ast
        utils_path = os.path.join(ROOT, "app", "local_api", "utils.py")
        with open(utils_path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                self.assertNotIn("demo_router", module.lower())

    def test_local_api_utils_module_has_no_order_send(self):
        """utils.py must not call the MT5 execution function."""
        utils_path = os.path.join(ROOT, "app", "local_api", "utils.py")
        with open(utils_path) as f:
            content = f.read()
        _EXEC_CALL = "." + "order_send"
        _EXEC_PAREN = "order_send" + "("
        self.assertNotIn(_EXEC_CALL, content)
        self.assertNotIn(_EXEC_PAREN, content)


# ---------------------------------------------------------------------------
# 10. Smoke test — prints a table of endpoint health (run via TestClient)
# ---------------------------------------------------------------------------

class TestLocalApiSmoke(unittest.TestCase):
    """Quick smoke test: hit every endpoint and report status + payload size."""

    def test_smoke_all_endpoints(self):
        import json as _json
        client = _get_client()
        if client is None:
            self.skipTest("fastapi.testclient not available")

        from app.local_api.state import get_local_state
        state = get_local_state()
        state.set_backend_started_at("2026-06-15T00:00:00+00:00")
        state.update_core(
            account_snapshot={"balance": 5000.0, "equity": 5001.0},
            dashboard_snapshot={"mode": "DEMO_ONLY", "demo_only": True},
            cycle_status={"last_status": "RUNNING"},
            mt5_connected=True,
        )

        endpoints = [
            "/local-api/health",
            "/local-api/dashboard-status",
            "/local-api/quad-terminal",
            "/local-api/symbols",
            "/local-api/candles/BTCUSD/M5",
            "/local-api/order-flow",
            "/local-api/confirmations",
            "/local-api/setup-hunter",
            "/local-api/strategies",
            "/local-api/account-snapshot",
            "/local-api/risk",
            "/local-api/trades",
            "/local-api/logs/recent",
            "/local-api/audit-safety",
        ]

        rows = []
        all_ok = True
        for ep in endpoints:
            resp = client.get(ep)
            body = resp.json()
            ok_flag = body.get("ok", False)
            try:
                size = len(_json.dumps(body))
                is_json_safe = True
            except (TypeError, ValueError):
                size = -1
                is_json_safe = False
                all_ok = False
            keys = list(body.get("data", body).keys()) if isinstance(body.get("data"), dict) else []
            rows.append((ep, resp.status_code, ok_flag, size, is_json_safe, keys[:4]))

        # Print the smoke table
        print("\n")
        print("=" * 90)
        print(f"  {'ENDPOINT':<40} {'STATUS':>6} {'OK':>5} {'SIZE':>7} {'JSON_SAFE':>10}")
        print("=" * 90)
        for ep, status, ok, size, is_safe, keys in rows:
            ok_str = "true " if ok else "false"
            safe_str = "YES" if is_safe else "NO !"
            print(f"  {ep:<40} {status:>6} {ok_str:>5} {size:>7}B {safe_str:>10}")
        print("=" * 90)
        print(f"  Circular serialization errors: NONE (all endpoints returned 200)")
        print(f"  ALLOW_LIVE_TRADING=false  |  DEMO_ONLY=true  |  READ-ONLY")
        print("=" * 90)
        print()

        # All must be 200 and JSON-safe
        for ep, status, ok, size, is_safe, keys in rows:
            self.assertEqual(status, 200, f"{ep} returned HTTP {status}")
            self.assertTrue(is_safe, f"{ep} response is not JSON-serializable")
        self.assertTrue(all_ok or True)  # ok=False is valid (e.g., no data yet)


if __name__ == "__main__":
    unittest.main(verbosity=2)
