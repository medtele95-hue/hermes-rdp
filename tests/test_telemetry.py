"""Tests for backend telemetry stage — emit_bot_log and structured tokens.

Verifies:
- emit_bot_log does not crash when Supabase fails.
- emit_bot_log returns a dict (never raises).
- STRATEGY_CLASS token is written to bot_logs.
- SETUP_HUNTER_* tokens are written to bot_logs.
- CONFIRMATION_MATRIX token is written to bot_logs.
- Safety invariants: order_send only in demo_router, live trading blocked, max lot 0.01.
"""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services.ingest_client import IngestClient


def _settings(**kwargs) -> Settings:
    base = dict(
        hermes_ingest_url="https://example.invalid/ingest",
        hermes_ingest_secret="test-secret",
        lovable_ingest_timeout_seconds=2.0,
        lovable_ingest_fail_soft=True,
        lovable_ingest_circuit_breaker_enabled=True,
        lovable_ingest_circuit_breaker_seconds=300.0,
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
    )
    base.update(kwargs)
    return Settings(**base)


def _mock_ok_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = '{"ok": true}'
    return r


class TestEmitBotLogNoRaise(unittest.TestCase):
    def test_emit_does_not_crash_on_http_failure(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", side_effect=Exception("connection refused")):
            result = client.emit_bot_log("STRATEGY_CLASS", "test", {"symbol": "BTCUSD"})
        self.assertIsInstance(result, dict)

    def test_emit_returns_dict_never_raises(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0  # circuit breaker active
        result = client.emit_bot_log("CYCLE", "cycle_start", {"utc": "2026-01-01"})
        self.assertIsInstance(result, dict)

    def test_emit_does_not_crash_when_payload_is_none(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.emit_bot_log("SETUP_HUNTER", "test_message", None)
        self.assertIsInstance(result, dict)

    def test_emit_does_not_crash_when_payload_is_invalid_type(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.emit_bot_log("SAFETY_GUARD", "test", None)  # type: ignore[arg-type]
        self.assertIsInstance(result, dict)

    def test_emit_succeeds_when_supabase_ok(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()):
            result = client.emit_bot_log("CYCLE", "cycle_end", {"analyzed": 4})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("table"), "bot_logs")


class TestEmitBotLogTokenInRow(unittest.TestCase):
    """Verify that the token appears in the row sent to Supabase."""

    def _last_call_payload(self, mock_post: MagicMock) -> dict:
        call_kwargs = mock_post.call_args
        return call_kwargs.kwargs.get("json") or call_kwargs.args[1] if call_kwargs.args else {}

    def test_strategy_class_token_in_source(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("STRATEGY_CLASS", "active=2 symbol=BTCUSD", {"symbol": "BTCUSD"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "STRATEGY_CLASS")
        self.assertIn("STRATEGY_CLASS", row.get("message", ""))

    def test_setup_hunter_in_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("SETUP_HUNTER_IN", "entry candidate GOLD", {"strategy": "GOLD_LIQUIDITY_HUNTER_PRO"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "SETUP_HUNTER_IN")

    def test_setup_hunter_reject_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("SETUP_HUNTER_REJECT", "rejected", {"reason": "RR_TOO_LOW"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "SETUP_HUNTER_REJECT")

    def test_setup_hunter_accept_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("SETUP_HUNTER_ACCEPT", "accepted GOLD grade=A", {"grade": "A"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "SETUP_HUNTER_ACCEPT")

    def test_confirmation_matrix_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("CONFIRMATION_MATRIX", "smc=75 mtfa=65 status=PASS", {"status": "PASS"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "CONFIRMATION_MATRIX")

    def test_confirmation_warning_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("CONFIRMATION_WARNING", "soft fail smc=50", {"smc_score": 50})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "CONFIRMATION_WARNING")

    def test_confirmation_block_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("CONFIRMATION_BLOCK", "hard block", {"hard_block": True})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "CONFIRMATION_BLOCK")

    def test_safety_guard_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("SAFETY_GUARD", "status=PASS", {"status": "PASS"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "SAFETY_GUARD")

    def test_router_handoff_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("ROUTER_HANDOFF", "SEND_TO_DEMO_ROUTER", {"decision": "SEND_TO_DEMO_ROUTER"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "ROUTER_HANDOFF")

    def test_demo_order_token(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("DEMO_ORDER", "ticket=12345", {"ticket": "12345"})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        self.assertEqual(row.get("source"), "DEMO_ORDER")

    def test_payload_is_in_context_field(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("CYCLE", "cycle_end", {"analyzed": 3, "demo_orders": 1})
        payload = self._last_call_payload(m)
        row = payload.get("data") or {}
        context = row.get("context") or {}
        self.assertEqual(context.get("analyzed"), 3)

    def test_table_is_bot_logs(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", return_value=_mock_ok_response()) as m:
            client.emit_bot_log("TIME_GATE", "session=LONDON", {})
        payload = self._last_call_payload(m)
        self.assertEqual(payload.get("table"), "bot_logs")


class TestEmitBotLogCircuitBreaker(unittest.TestCase):
    def test_emit_skips_http_when_circuit_breaker_active(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        with patch("requests.post") as m:
            result = client.emit_bot_log("SPREAD_DIAG", "symbol=GOLD spread=15", {"spread": 15})
        m.assert_not_called()
        self.assertIsInstance(result, dict)

    def test_circuit_breaker_not_extended_by_emit(self) -> None:
        client = IngestClient(_settings())
        before = time.monotonic() + 300.0
        client._fail_soft_until = before
        client.emit_bot_log("LOVABLE_INGEST_HEALTH", "DEGRADED", {"status": "DEGRADED"})
        self.assertAlmostEqual(client._fail_soft_until, before, delta=0.5)


class TestSafetyInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_order_send_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in (root / "app").rglob("*.py"):
            if path.as_posix().endswith("app/mt5/demo_router.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_live_trading_blocked_by_default(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_live_trading)
        self.assertTrue(s.demo_only)

    def test_emit_bot_log_does_not_change_max_lot(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", side_effect=Exception("network error")):
            client.emit_bot_log("DEMO_ORDER", "lot check", {"lot": 0.01})
        self.assertAlmostEqual(client.settings.demo_max_lot, 0.01, places=4)


if __name__ == "__main__":
    unittest.main()
