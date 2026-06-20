"""Phase 10 — Noise/reliability: throttle correctness for high-frequency log events.

Verifies:
- should_emit() core semantics: True on first call, False within window, True after expiry.
- SAFETY_GUARD PASS is logged at most once per 300 s window — never every cycle.
- REMOTE_DEMO_READ_UNAVAILABLE warning is throttled — not emitted on every fallback call.
- LOVABLE_INGEST_CB_SKIP is throttled — not emitted for every row skipped during cooldown.
- position_sync_skipped_MAIN_CYCLE_ACTIVE respects should_emit — not spammed every heartbeat.
- Safety invariants unchanged.
- order_send remains only in demo_router.py.
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.utils.throttle as _throttle_mod
from app.agents.safety_guard import _log_result
from app.config import Settings
from app.services.ingest_client import IngestClient


# ── helpers ───────────────────────────────────────────────────────────────────

def _reset_throttle(*keys: str) -> None:
    """Remove specific throttle keys so the next call is always fresh."""
    for k in keys:
        _throttle_mod._state.pop(k, None)


def _exhaust_throttle(key: str) -> None:
    """Mark a key as just emitted so subsequent calls within the window are suppressed."""
    _throttle_mod._state[key] = datetime.now(timezone.utc)


def _expire_throttle(key: str, interval: int = 300) -> None:
    """Back-date the last emit so the next call treats it as expired."""
    _throttle_mod._state[key] = datetime.now(timezone.utc) - timedelta(seconds=interval + 1)


def _ingest_settings() -> Settings:
    return Settings(
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


def _pass_result() -> dict:
    return {"safety_guard_status": "PASS", "safety_guard_reason": "SAFETY_GUARD_PASS"}


def _block_result() -> dict:
    return {"safety_guard_status": "BLOCK", "safety_guard_reason": "WEEKEND_MARKET_CLOSED"}


# ── core throttle tests ────────────────────────────────────────────────────────

class TestShouldEmitCore(unittest.TestCase):
    """Unit tests for the should_emit() primitive itself."""

    def setUp(self) -> None:
        _reset_throttle("_test_key_a", "_test_key_b", "_test_key_c")

    def test_first_call_returns_true(self) -> None:
        from app.utils.throttle import should_emit
        self.assertTrue(should_emit("_test_key_a"))

    def test_second_call_within_window_returns_false(self) -> None:
        from app.utils.throttle import should_emit
        should_emit("_test_key_a")
        self.assertFalse(should_emit("_test_key_a"))

    def test_third_call_within_window_also_false(self) -> None:
        from app.utils.throttle import should_emit
        should_emit("_test_key_a")
        self.assertFalse(should_emit("_test_key_a"))
        self.assertFalse(should_emit("_test_key_a"))

    def test_expired_entry_returns_true(self) -> None:
        from app.utils.throttle import should_emit
        _expire_throttle("_test_key_a", interval=300)
        self.assertTrue(should_emit("_test_key_a"))

    def test_different_keys_are_independent(self) -> None:
        from app.utils.throttle import should_emit
        self.assertTrue(should_emit("_test_key_a"))
        self.assertTrue(should_emit("_test_key_b"))  # fresh key, not affected

    def test_custom_short_interval_respected(self) -> None:
        from app.utils.throttle import should_emit
        should_emit("_test_key_c", interval_seconds=1)
        # Just emitted — should be suppressed within 1s
        self.assertFalse(should_emit("_test_key_c", interval_seconds=1))

    def test_expired_custom_interval_returns_true(self) -> None:
        from app.utils.throttle import should_emit
        _expire_throttle("_test_key_c", interval=1)
        self.assertTrue(should_emit("_test_key_c", interval_seconds=1))

    def test_default_interval_is_300s(self) -> None:
        self.assertEqual(_throttle_mod.THROTTLE_SECONDS, 300)

    def test_state_is_dict(self) -> None:
        self.assertIsInstance(_throttle_mod._state, dict)


# ── SAFETY_GUARD PASS throttle ────────────────────────────────────────────────

class TestSafetyGuardPassThrottle(unittest.TestCase):
    """SAFETY_GUARD PASS log is emitted once per 300s, not every cycle."""

    def setUp(self) -> None:
        _reset_throttle("SAFETY_GUARD_PASS")

    def test_pass_logged_on_first_call(self) -> None:
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_pass_result())
        mock_log.info.assert_called_once()

    def test_pass_not_logged_on_second_call_within_window(self) -> None:
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_pass_result())  # first → logs
            _log_result(_pass_result())  # second → throttled
        self.assertEqual(mock_log.info.call_count, 1, "PASS should only log once within window")

    def test_pass_suppressed_for_many_consecutive_calls(self) -> None:
        with patch("app.agents.safety_guard.log") as mock_log:
            for _ in range(20):
                _log_result(_pass_result())
        self.assertEqual(mock_log.info.call_count, 1, "20 consecutive PASS calls → only 1 log")

    def test_block_always_logged_regardless_of_throttle(self) -> None:
        _exhaust_throttle("SAFETY_GUARD_PASS")
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_block_result())
            _log_result(_block_result())
        self.assertEqual(mock_log.info.call_count, 2, "BLOCK is never throttled")

    def test_pass_logged_again_after_expiry(self) -> None:
        _expire_throttle("SAFETY_GUARD_PASS")
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_pass_result())
        mock_log.info.assert_called_once()

    def test_pass_after_block_does_not_log_if_throttled(self) -> None:
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_pass_result())   # PASS → logs, throttle armed
            _log_result(_block_result())  # BLOCK → always logs
            _log_result(_pass_result())   # PASS → throttled
        self.assertEqual(mock_log.info.call_count, 2, "PASS+BLOCK+PASS = 2 logs (PASS throttled)")

    def test_pass_result_key_is_safety_guard_pass(self) -> None:
        # Verify the throttle key name used by safety_guard
        _exhaust_throttle("SAFETY_GUARD_PASS")
        with patch("app.agents.safety_guard.log") as mock_log:
            _log_result(_pass_result())
        mock_log.info.assert_not_called()


# ── REMOTE_DEMO_READ_UNAVAILABLE throttle ─────────────────────────────────────

class TestRemoteDemoReadUnavailableThrottle(unittest.TestCase):
    """get_open_demo_trades() logs only once per 300s regardless of call frequency."""

    def setUp(self) -> None:
        _reset_throttle("REMOTE_DEMO_READ_UNAVAILABLE")

    def _client(self) -> IngestClient:
        return IngestClient(_ingest_settings())

    def test_first_call_emits_warning(self) -> None:
        client = self._client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.get_open_demo_trades(12345)
        mock_log.warning.assert_called_once()

    def test_second_call_within_window_no_warning(self) -> None:
        client = self._client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.get_open_demo_trades(12345)  # first → warns
            client.get_open_demo_trades(12345)  # second → throttled
        self.assertEqual(mock_log.warning.call_count, 1, "Second call within window must not warn")

    def test_many_calls_emit_exactly_one_warning(self) -> None:
        client = self._client()
        with patch("app.services.ingest_client.log") as mock_log:
            for _ in range(50):
                client.get_open_demo_trades(12345)
        self.assertEqual(mock_log.warning.call_count, 1, "50 calls → exactly 1 warning")

    def test_always_returns_ok_false(self) -> None:
        client = self._client()
        r1 = client.get_open_demo_trades(12345)
        r2 = client.get_open_demo_trades(12345)
        self.assertFalse(r1.get("ok"))
        self.assertFalse(r2.get("ok"))

    def test_always_returns_remote_demo_read_unavailable_error(self) -> None:
        client = self._client()
        result = client.get_open_demo_trades(12345)
        self.assertEqual(result.get("error"), "REMOTE_DEMO_READ_UNAVAILABLE")

    def test_warning_logged_again_after_throttle_expiry(self) -> None:
        _expire_throttle("REMOTE_DEMO_READ_UNAVAILABLE")
        client = self._client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.get_open_demo_trades(12345)
        mock_log.warning.assert_called_once()

    def test_does_not_extend_circuit_breaker(self) -> None:
        client = self._client()
        self.assertFalse(client._fail_soft_skip_active())
        client.get_open_demo_trades(12345)
        client.get_open_demo_trades(12345)
        self.assertFalse(client._fail_soft_skip_active(), "get_open_demo_trades must not activate circuit breaker")


# ── LOVABLE_INGEST_CB_SKIP throttle ───────────────────────────────────────────

class TestLovableIngestCBSkipThrottle(unittest.TestCase):
    """Circuit breaker skip log is throttled — not spammed for every skipped row."""

    def setUp(self) -> None:
        _reset_throttle("LOVABLE_INGEST_CB_SKIP")

    def _active_client(self) -> IngestClient:
        """Return a client with an active (unexpired) circuit breaker."""
        client = IngestClient(_ingest_settings())
        client._fail_soft_until = time.monotonic() + 300.0
        client._fail_soft_logged = True
        return client

    def test_first_skip_logs_warning(self) -> None:
        client = self._active_client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.send_row("test_table", {"k": "v"})
        mock_log.warning.assert_called_once()
        self.assertIn("CIRCUIT_BREAKER_ACTIVE", mock_log.warning.call_args[0][0])

    def test_second_skip_within_window_not_logged(self) -> None:
        client = self._active_client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.send_row("test_table", {"k": "v"})   # first → warns
            client.send_row("test_table", {"k": "v2"})  # second → throttled
        self.assertEqual(mock_log.warning.call_count, 1, "CB_SKIP warns only once per window")

    def test_many_skipped_rows_emit_exactly_one_warning(self) -> None:
        client = self._active_client()
        with patch("app.services.ingest_client.log") as mock_log:
            for i in range(100):
                client.send_row("test_table", {"k": i})
        self.assertEqual(mock_log.warning.call_count, 1, "100 skipped rows → exactly 1 CB_SKIP warning")

    def test_skip_log_emitted_again_after_throttle_expiry(self) -> None:
        _expire_throttle("LOVABLE_INGEST_CB_SKIP")
        client = self._active_client()
        with patch("app.services.ingest_client.log") as mock_log:
            client.send_row("test_table", {"k": "v"})
        mock_log.warning.assert_called_once()

    def test_skipped_rows_still_return_fail_dict(self) -> None:
        client = self._active_client()
        result = client.send_row("test_table", {"k": "v"})
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("ok"))
        self.assertIn("LOVABLE_INGEST_FAIL_SOFT_SKIP", result.get("error", ""))

    def test_cb_skip_key_distinct_from_degraded_key(self) -> None:
        # LOVABLE_INGEST_CB_SKIP and LOVABLE_INGEST_HEALTH_DEGRADED are separate keys
        _exhaust_throttle("LOVABLE_INGEST_CB_SKIP")
        # The DEGRADED warning (logged via _mark_fail_soft) uses _fail_soft_logged bool, not throttle
        client = IngestClient(_ingest_settings())
        with patch("requests.post", side_effect=Exception("timeout")):
            with patch("app.services.ingest_client.log") as mock_log:
                client.send_row("test_table", {"k": "v"})
        # DEGRADED log uses _fail_soft_logged flag — independent of CB_SKIP throttle key
        degraded = [c for c in mock_log.warning.call_args_list if "DEGRADED" in str(c)]
        self.assertEqual(len(degraded), 1)


# ── position_sync_skipped throttle ────────────────────────────────────────────

class TestPositionSyncSkippedThrottle(unittest.TestCase):
    """position_sync_skipped_MAIN_CYCLE_ACTIVE log is guarded by should_emit."""

    _KEY = "position_sync_skipped_MAIN_CYCLE_ACTIVE"

    def setUp(self) -> None:
        _reset_throttle(self._KEY)

    def test_first_emit_returns_true(self) -> None:
        from app.utils.throttle import should_emit
        self.assertTrue(should_emit(self._KEY))

    def test_second_emit_within_window_returns_false(self) -> None:
        from app.utils.throttle import should_emit
        should_emit(self._KEY)
        self.assertFalse(should_emit(self._KEY))

    def test_repeated_skips_suppressed_within_window(self) -> None:
        from app.utils.throttle import should_emit
        first = should_emit(self._KEY)
        subsequent = [should_emit(self._KEY) for _ in range(10)]
        self.assertTrue(first)
        self.assertTrue(all(not x for x in subsequent), "All subsequent within window must be False")

    def test_emits_after_expiry(self) -> None:
        from app.utils.throttle import should_emit
        _expire_throttle(self._KEY)
        self.assertTrue(should_emit(self._KEY))

    def test_key_name_used_in_main_matches(self) -> None:
        """Confirm main.py uses the exact key we're testing."""
        main_path = ROOT / "app" / "main.py"
        text = main_path.read_text(encoding="utf-8", errors="ignore")
        self.assertIn(f'should_emit("{self._KEY}")', text, "main.py must use this exact throttle key")


# ── throttle isolation ─────────────────────────────────────────────────────────

class TestThrottleIsolation(unittest.TestCase):
    """Throttle _state dict doesn't bleed between unrelated keys."""

    def setUp(self) -> None:
        _reset_throttle("_iso_a", "_iso_b")

    def test_exhausting_one_key_does_not_affect_another(self) -> None:
        from app.utils.throttle import should_emit
        _exhaust_throttle("_iso_a")
        self.assertTrue(should_emit("_iso_b"), "_iso_b should not be affected by _iso_a")

    def test_expiring_one_key_does_not_affect_another(self) -> None:
        from app.utils.throttle import should_emit
        _exhaust_throttle("_iso_a")
        _exhaust_throttle("_iso_b")
        _expire_throttle("_iso_a")
        self.assertTrue(should_emit("_iso_a"))
        self.assertFalse(should_emit("_iso_b"))

    def test_state_persists_across_calls(self) -> None:
        from app.utils.throttle import should_emit
        should_emit("_iso_a")
        # Calling again immediately → still suppressed
        self.assertFalse(should_emit("_iso_a"))
        self.assertFalse(should_emit("_iso_a"))


# ── safety invariants ──────────────────────────────────────────────────────────

class TestSafetyInvariantsUnchanged(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_throttle_module_has_no_order_send(self) -> None:
        text = (ROOT / "app" / "utils" / "throttle.py").read_text(encoding="utf-8")
        self.assertNotIn("order_send", text)

    def test_order_send_only_in_demo_router(self) -> None:
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text and not path.name == "demo_router.py":
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"order_send outside demo_router: {offenders}")


if __name__ == "__main__":
    unittest.main()
