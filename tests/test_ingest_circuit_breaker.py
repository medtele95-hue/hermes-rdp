"""Tests for IngestClient circuit breaker and fail-soft behavior.

Verifies:
- Timeout activates circuit breaker for configured cooldown seconds.
- During cooldown, send_row/send_bulk return immediately without HTTP calls.
- After cooldown expires, HTTP call is retried (and can fail again).
- Recovery is logged after first successful write following degradation.
- get_open_demo_trades never marks fail_soft (it is not an HTTP failure path).
- Safety invariants are unchanged.
- mt5.order_send remains only in demo_router.py.
"""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services.ingest_client import IngestClient


def _settings(cb_seconds: float = 300.0, timeout: float = 2.0) -> Settings:
    return Settings(
        hermes_ingest_url="https://example.invalid/ingest",
        hermes_ingest_secret="test-secret",
        lovable_ingest_timeout_seconds=timeout,
        lovable_ingest_fail_soft=True,
        lovable_ingest_circuit_breaker_enabled=True,
        lovable_ingest_circuit_breaker_seconds=cb_seconds,
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
    )


class TestCircuitBreakerActivation(unittest.TestCase):
    def test_http_error_activates_circuit_breaker(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", side_effect=Exception("connection refused")):
            result = client.send_row("test_table", {"key": "value"})
        self.assertFalse(result.get("ok"))
        self.assertTrue(client._fail_soft_skip_active(), "Circuit breaker should be active after failure")

    def test_circuit_breaker_duration_is_300s(self) -> None:
        client = IngestClient(_settings(cb_seconds=300.0))
        with patch("requests.post", side_effect=Exception("timeout")):
            client.send_row("test_table", {"k": "v"})
        remaining = client._fail_soft_until - time.monotonic()
        self.assertGreater(remaining, 250.0, "Breaker should be set for ~300s")
        self.assertLessEqual(remaining, 310.0)

    def test_circuit_breaker_duration_uses_setting(self) -> None:
        client = IngestClient(_settings(cb_seconds=120.0))
        with patch("requests.post", side_effect=Exception("timeout")):
            client.send_row("test_table", {"k": "v"})
        remaining = client._fail_soft_until - time.monotonic()
        self.assertGreater(remaining, 100.0)
        self.assertLessEqual(remaining, 130.0)

    def test_minimum_circuit_breaker_is_30s(self) -> None:
        client = IngestClient(_settings(cb_seconds=5.0))  # below minimum
        with patch("requests.post", side_effect=Exception("timeout")):
            client.send_row("test_table", {"k": "v"})
        remaining = client._fail_soft_until - time.monotonic()
        self.assertGreaterEqual(remaining, 29.0, "Minimum breaker should be 30s")

    def test_degraded_log_emitted_once(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", side_effect=Exception("timeout")):
            with self.assertLogs("hermes", level="WARNING") as cm:
                client.send_row("test_table", {"k": "v"})
                client.send_row("test_table", {"k": "v2"})  # skipped, no HTTP
        degraded_logs = [m for m in cm.output if "DEGRADED" in m]
        self.assertEqual(len(degraded_logs), 1, "DEGRADED should be logged exactly once per degradation")

    def test_degraded_log_contains_health_key(self) -> None:
        client = IngestClient(_settings())
        with patch("requests.post", side_effect=Exception("timeout")):
            with self.assertLogs("hermes", level="WARNING") as cm:
                client.send_row("test_table", {"k": "v"})
        self.assertTrue(
            any("LOVABLE_INGEST_HEALTH" in m and "DEGRADED" in m for m in cm.output),
            f"Expected [LOVABLE_INGEST_HEALTH] status=DEGRADED in logs: {cm.output}",
        )


class TestCircuitBreakerSkipsHTTP(unittest.TestCase):
    def test_send_row_skips_http_during_cooldown(self) -> None:
        client = IngestClient(_settings())
        # Manually activate circuit breaker
        client._fail_soft_until = time.monotonic() + 300.0
        client._fail_soft_logged = True
        post_mock = MagicMock()
        with patch("requests.post", post_mock):
            result = client.send_row("any_table", {"x": 1})
        post_mock.assert_not_called()
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "LOVABLE_INGEST_FAIL_SOFT_SKIP")

    def test_update_row_skips_http_during_cooldown(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        post_mock = MagicMock()
        with patch("requests.post", post_mock):
            result = client.update_row("bot_status", {"bot_name": "HERMES"}, {"status": "RUNNING"})
        post_mock.assert_not_called()
        self.assertFalse(result.get("ok"))

    def test_send_bulk_skips_http_during_cooldown(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        rows = [{"a": 1}, {"b": 2}, {"c": 3}]
        post_mock = MagicMock()
        with patch("requests.post", post_mock):
            result = client.send_bulk("market_candles", rows)
        post_mock.assert_not_called()
        self.assertEqual(result.get("failed"), 3)

    def test_circuit_breaker_expires_allows_retry(self) -> None:
        client = IngestClient(_settings())
        # Set breaker to already-expired
        client._fail_soft_until = time.monotonic() - 1.0
        post_mock = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'
        post_mock.return_value = mock_response
        with patch("requests.post", post_mock):
            result = client.send_row("test_table", {"k": "v"})
        post_mock.assert_called_once()
        self.assertTrue(result.get("ok"))


class TestCircuitBreakerRecovery(unittest.TestCase):
    def test_recovery_logged_after_successful_write(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() - 1.0  # expired
        client._fail_soft_logged = True  # was degraded
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'
        with patch("requests.post", return_value=mock_response):
            with self.assertLogs("hermes", level="INFO") as cm:
                result = client.send_row("test_table", {"k": "v"})
        self.assertTrue(result.get("ok"))
        self.assertFalse(client._fail_soft_logged, "Flag should be cleared after recovery")
        self.assertTrue(
            any("RECOVERED" in m for m in cm.output),
            f"Expected RECOVERED log: {cm.output}",
        )

    def test_recovery_clears_degraded_flag(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_logged = True
        client._check_recovery()
        self.assertFalse(client._fail_soft_logged)

    def test_recovery_not_logged_when_not_degraded(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_logged = False
        # _check_recovery with no prior degradation — should not log anything
        client._check_recovery()
        self.assertFalse(client._fail_soft_logged)


class TestGetOpenDemoTradesNeverMarksFailSoft(unittest.TestCase):
    """get_open_demo_trades is not an HTTP call — must never extend the circuit breaker."""

    def test_get_open_demo_trades_does_not_activate_breaker(self) -> None:
        client = IngestClient(_settings())
        self.assertFalse(client._fail_soft_skip_active())
        client.get_open_demo_trades(12345)
        self.assertFalse(
            client._fail_soft_skip_active(),
            "get_open_demo_trades must NOT activate the circuit breaker",
        )

    def test_get_open_demo_trades_does_not_extend_breaker(self) -> None:
        client = IngestClient(_settings())
        # Simulate an active circuit breaker set 10 seconds ago
        client._fail_soft_until = time.monotonic() + 50.0  # expires in 50s
        before = client._fail_soft_until
        client.get_open_demo_trades(12345)
        after = client._fail_soft_until
        self.assertAlmostEqual(before, after, delta=0.1, msg="get_open_demo_trades must not extend the circuit breaker")

    def test_get_open_demo_trades_returns_fast(self) -> None:
        client = IngestClient(_settings())
        start = time.monotonic()
        result = client.get_open_demo_trades(12345)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, "get_open_demo_trades should return immediately")
        self.assertFalse(result.get("ok"))


class TestCycleRunsWhenDegraded(unittest.TestCase):
    """Verify send_row/send_bulk return fast error dict (not exception) during degradation."""

    def test_send_row_returns_dict_not_exception(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.send_row("ai_decisions", {"strategy": "TEST"})
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("ok"))

    def test_send_bulk_returns_dict_not_exception(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.send_bulk("strategy_signals", [{"s": 1}, {"s": 2}])
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("ok"))

    def test_log_event_returns_dict_not_exception(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.log_event("INFO", "test message", {})
        self.assertIsInstance(result, dict)

    def test_update_row_returns_dict_not_exception(self) -> None:
        client = IngestClient(_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        result = client.update_row("bot_status", {"bot_name": "HERMES"}, {"status": "RUNNING"})
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("ok"))


class TestSafetyInvariantsUnchanged(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_true(self) -> None:
        s = Settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_001(self) -> None:
        s = Settings()
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)

    def test_circuit_breaker_defaults(self) -> None:
        s = Settings()
        self.assertTrue(s.lovable_ingest_circuit_breaker_enabled)
        self.assertAlmostEqual(s.lovable_ingest_circuit_breaker_seconds, 300.0, places=1)

    def test_order_send_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        demo_router = (root / "app" / "mt5" / "demo_router.py").as_posix()
        offenders = []
        for path in (root / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text and not path.as_posix().endswith("app/mt5/demo_router.py"):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")


if __name__ == "__main__":
    unittest.main()
