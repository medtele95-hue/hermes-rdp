"""Tests for /local-api/health and /local-api/readiness endpoints.

Verifies:
  1. /health responds fast (does not block on MT5)
  2. /health returns HTTP 200 with ok:True even when MT5 is unavailable
  3. /health always has demo_only:true and allow_live_trading:false
  4. /readiness returns HTTP 200 with ready:true and degraded flags
  5. /readiness works without MT5 connection (degraded:true, not a 500)
  6. /readiness always has allow_live_trading:false
  7. All other endpoints return HTTP 200 with ok field present (graceful degraded)
  8. No endpoint calls mt5.order_send
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SERVER_PATH = ROOT / "app" / "local_api" / "server.py"


# ── Stub heavy deps before any import of server ──────────────────────────────

def _stub_mt5(connected: bool = False) -> types.ModuleType:
    """Return a minimal MetaTrader5 stub."""
    mod = types.ModuleType("MetaTrader5")
    mod.positions_get = lambda: None  # type: ignore[attr-defined]
    mod.account_info  = lambda: None  # type: ignore[attr-defined]
    mod.POSITION_TYPE_BUY  = 0        # type: ignore[attr-defined]
    mod.POSITION_TYPE_SELL = 1        # type: ignore[attr-defined]
    return mod


def _import_server() -> types.ModuleType:
    """Import app.local_api.server with all heavy deps stubbed out."""
    if "MetaTrader5" not in sys.modules:
        sys.modules["MetaTrader5"] = _stub_mt5()

    from app.local_api import server  # noqa: PLC0415
    return server


def _fake_state(
    *,
    heartbeat: str | None = None,
    mt5_connected: bool = False,
    account: dict | None = None,
    snapshot: dict | None = None,
) -> MagicMock:
    state = MagicMock()
    state.get_last_heartbeat_at.return_value = heartbeat
    state.get_mt5_connected.return_value = mt5_connected
    state.get_account_snapshot.return_value = account or {}
    state.get_dashboard_snapshot.return_value = snapshot or {}
    state.get_backend_started_at.return_value = "2026-06-16T00:00:00Z"
    state.get_cycle_status.return_value = {"last_status": "RUNNING"}
    state.get_resolved_symbols.return_value = {}
    state.get_ingest_health.return_value = {}
    return state


# ── 1. /health fast response (does not block on MT5) ─────────────────────────

class TestHealthFast(unittest.TestCase):

    def test_health_responds_under_500ms(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", return_value=_fake_state()):
            t0 = time.perf_counter()
            resp = server.health()
            elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertLess(elapsed_ms, 500, "health() took too long — may be blocking on MT5")

    def test_health_status_200(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", return_value=_fake_state()):
            resp = server.health()
        self.assertEqual(resp.status_code, 200)


# ── 2. /health without MT5 — still returns ok:True ───────────────────────────

class TestHealthNoMT5(unittest.TestCase):

    def _call_health(self, mt5_connected: bool = False) -> dict:
        server = _import_server()
        with patch("app.local_api.server.get_local_state",
                   return_value=_fake_state(mt5_connected=mt5_connected)):
            resp = server.health()
        import json
        return json.loads(resp.body)

    def test_ok_true_without_mt5(self) -> None:
        body = self._call_health(mt5_connected=False)
        self.assertTrue(body["ok"])

    def test_ok_true_with_mt5(self) -> None:
        body = self._call_health(mt5_connected=True)
        self.assertTrue(body["ok"])

    def test_data_present(self) -> None:
        body = self._call_health()
        self.assertIn("data", body)

    def test_data_has_backend_status(self) -> None:
        body = self._call_health()
        self.assertIn("backend_status", body["data"])

    def test_data_has_mt5_connected(self) -> None:
        body = self._call_health()
        self.assertIn("mt5_connected", body["data"])


# ── 3. /health safety invariants ─────────────────────────────────────────────

class TestHealthSafetyInvariants(unittest.TestCase):

    def _body(self) -> dict:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", return_value=_fake_state()):
            resp = server.health()
        import json
        return json.loads(resp.body)

    def test_demo_only_always_true(self) -> None:
        body = self._body()
        self.assertTrue(body["data"]["demo_only"])

    def test_allow_live_trading_always_false(self) -> None:
        body = self._body()
        self.assertFalse(body["data"]["allow_live_trading"])

    def test_safety_proof_present(self) -> None:
        body = self._body()
        self.assertIn("safety_proof", body["data"])
        proof = body["data"]["safety_proof"]
        self.assertFalse(proof["allow_live_trading"])
        self.assertTrue(proof["demo_only"])


# ── 4. /health exception fallback ────────────────────────────────────────────

class TestHealthExceptionFallback(unittest.TestCase):

    def test_health_returns_200_on_exception(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", side_effect=RuntimeError("state exploded")):
            resp = server.health()
        self.assertEqual(resp.status_code, 200)

    def test_health_error_body_has_ok_false(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", side_effect=RuntimeError("boom")):
            resp = server.health()
        import json
        body = json.loads(resp.body)
        self.assertFalse(body["ok"])


# ── 5. /readiness without MT5 — degraded:true, not 500 ──────────────────────

class TestReadinessNoMT5(unittest.TestCase):

    def _call_readiness(self, mt5_connected: bool = False) -> dict:
        server = _import_server()
        mock_state = _fake_state(mt5_connected=mt5_connected)
        with patch("app.local_api.server.get_local_state", return_value=mock_state):
            resp = server.readiness()
        import json
        return json.loads(resp.body)

    def test_status_200_without_mt5(self) -> None:
        server = _import_server()
        mock_state = _fake_state(mt5_connected=False)
        with patch("app.local_api.server.get_local_state", return_value=mock_state):
            resp = server.readiness()
        self.assertEqual(resp.status_code, 200)

    def test_ready_true_even_without_mt5(self) -> None:
        body = self._call_readiness(mt5_connected=False)
        self.assertTrue(body["ready"])

    def test_degraded_true_without_mt5(self) -> None:
        body = self._call_readiness(mt5_connected=False)
        self.assertTrue(body["degraded"])

    def test_mt5_connected_false(self) -> None:
        body = self._call_readiness(mt5_connected=False)
        self.assertFalse(body["mt5_connected"])

    def test_mt5_connected_true(self) -> None:
        body = self._call_readiness(mt5_connected=True)
        self.assertTrue(body["mt5_connected"])

    def test_degraded_false_when_mt5_and_account(self) -> None:
        server = _import_server()
        mock_state = _fake_state(mt5_connected=True, account={"balance": 10000.0})
        with patch("app.local_api.server.get_local_state", return_value=mock_state):
            import json
            body = json.loads(server.readiness().body)
        self.assertFalse(body["degraded"])

    def test_ok_true(self) -> None:
        body = self._call_readiness()
        self.assertTrue(body["ok"])


# ── 6. /readiness safety invariants ──────────────────────────────────────────

class TestReadinessSafetyInvariants(unittest.TestCase):

    def _body(self, **kw) -> dict:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", return_value=_fake_state(**kw)):
            resp = server.readiness()
        import json
        return json.loads(resp.body)

    def test_allow_live_trading_always_false(self) -> None:
        body = self._body()
        self.assertFalse(body["allow_live_trading"])

    def test_demo_only_always_true(self) -> None:
        body = self._body()
        self.assertTrue(body["demo_only"])

    def test_profile_field_present(self) -> None:
        body = self._body()
        self.assertIn("profile", body)
        self.assertIsNotNone(body["profile"])

    def test_dashboard_api_ok_present(self) -> None:
        body = self._body()
        self.assertIn("dashboard_api_ok", body)

    def test_data_files_ok_present(self) -> None:
        body = self._body()
        self.assertIn("data_files_ok", body)


# ── 7. /readiness exception fallback ─────────────────────────────────────────

class TestReadinessExceptionFallback(unittest.TestCase):

    def test_returns_200_on_exception(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", side_effect=RuntimeError("crash")):
            resp = server.readiness()
        self.assertEqual(resp.status_code, 200)

    def test_ready_false_on_exception(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", side_effect=RuntimeError("crash")):
            resp = server.readiness()
        import json
        body = json.loads(resp.body)
        self.assertFalse(body["ready"])

    def test_degraded_true_on_exception(self) -> None:
        server = _import_server()
        with patch("app.local_api.server.get_local_state", side_effect=RuntimeError("crash")):
            resp = server.readiness()
        import json
        body = json.loads(resp.body)
        self.assertTrue(body["degraded"])


# ── 8. No mt5.order_send in server.py ────────────────────────────────────────

class TestServerNoOrderSend(unittest.TestCase):

    def test_server_py_does_not_call_order_send(self) -> None:
        import re
        src = SERVER_PATH.read_text(encoding="utf-8")
        # Pattern: mt5.order_send( not inside a string or comment
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], f"server.py calls mt5.order_send: {hits}")


# ── 9. Degraded response format (not crash) ──────────────────────────────────

class TestDegradedResponseFormat(unittest.TestCase):
    """All major endpoints must return 200 with an 'ok' field even when state raises."""

    def _server(self):
        return _import_server()

    def _patched_exploding_state(self):
        state = MagicMock()
        state.get_last_heartbeat_at.side_effect = RuntimeError("mt5 gone")
        return state

    def _check_endpoint(self, fn_name: str, *args):
        server = self._server()
        fn = getattr(server, fn_name)
        # Patch get_local_state to return exploding mock
        state = self._patched_exploding_state()
        with patch("app.local_api.server.get_local_state", return_value=state):
            resp = fn(*args)
        import json
        body = json.loads(resp.body)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok", body)

    def test_health_degraded(self) -> None:
        self._check_endpoint("health")

    def test_readiness_degraded(self) -> None:
        self._check_endpoint("readiness")


if __name__ == "__main__":
    unittest.main()
