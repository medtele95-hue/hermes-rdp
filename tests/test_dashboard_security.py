# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — PIN auth, lockout, rate limiting."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.dashboard_api import security as sec


class TestEnsurePinConfigured(unittest.TestCase):
    def test_generates_pin_when_none_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard" / ".env"
            pin = sec.ensure_pin_configured(path)
        self.assertIsNotNone(pin)
        self.assertEqual(len(pin), 6)
        self.assertTrue(pin.isdigit())

    def test_writes_hash_not_plaintext(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard" / ".env"
            pin = sec.ensure_pin_configured(path)
            content = path.read_text(encoding="utf-8")
        self.assertNotIn(pin, content)
        self.assertIn("DASHBOARD_PIN_HASH=", content)
        self.assertIn("DASHBOARD_PIN_SALT=", content)

    def test_returns_none_when_already_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard" / ".env"
            sec.ensure_pin_configured(path)
            second_call = sec.ensure_pin_configured(path)
        self.assertIsNone(second_call)

    def test_generated_pin_actually_verifies(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard" / ".env"
            pin = sec.ensure_pin_configured(path)
            self.assertTrue(sec.verify_pin(pin, path))
            self.assertFalse(sec.verify_pin("000000" if pin != "000000" else "111111", path))


class TestVerifyPin(unittest.TestCase):
    def test_missing_file_never_verifies(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.env"
            self.assertFalse(sec.verify_pin("123456", path))

    def test_wrong_pin_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            correct = sec.ensure_pin_configured(path)
            wrong = "999999" if correct != "999999" else "888888"
            self.assertFalse(sec.verify_pin(wrong, path))


class TestLockout(unittest.TestCase):
    def setUp(self) -> None:
        sec.reset_state_for_tests()

    def tearDown(self) -> None:
        sec.reset_state_for_tests()

    def test_correct_pin_resets_failure_count(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            pin = sec.ensure_pin_configured(path)
            with patch.object(sec, "DASHBOARD_ENV_FILE", path):
                ok, reason = sec.check_and_record_attempt(pin)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_five_wrong_attempts_triggers_lockout(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            sec.ensure_pin_configured(path)
            with patch.object(sec, "DASHBOARD_ENV_FILE", path):
                results = [sec.check_and_record_attempt("000000") for _ in range(5)]
        self.assertFalse(results[-1][0])
        self.assertEqual(results[-1][1], "LOCKOUT_TRIGGERED")
        locked, remaining = sec.is_locked_out()
        self.assertTrue(locked)
        self.assertGreater(remaining, 0)

    def test_locked_out_blocks_even_correct_pin(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            pin = sec.ensure_pin_configured(path)
            with patch.object(sec, "DASHBOARD_ENV_FILE", path):
                for _ in range(5):
                    sec.check_and_record_attempt("000000")
                ok, reason = sec.check_and_record_attempt(pin)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("LOCKED_OUT_"))

    def test_failure_count_increments_before_lockout(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            sec.ensure_pin_configured(path)
            with patch.object(sec, "DASHBOARD_ENV_FILE", path):
                _, reason1 = sec.check_and_record_attempt("000000")
                _, reason2 = sec.check_and_record_attempt("000000")
        self.assertEqual(reason1, "WRONG_PIN_1_OF_5")
        self.assertEqual(reason2, "WRONG_PIN_2_OF_5")


class TestRateLimit(unittest.TestCase):
    def setUp(self) -> None:
        sec.reset_state_for_tests()

    def tearDown(self) -> None:
        sec.reset_state_for_tests()

    def test_first_call_allowed(self) -> None:
        allowed, retry_after = sec.check_rate_limit()
        self.assertTrue(allowed)
        self.assertEqual(retry_after, 0.0)

    def test_immediate_second_call_blocked(self) -> None:
        sec.check_rate_limit()
        allowed, retry_after = sec.check_rate_limit()
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)
        self.assertLessEqual(retry_after, 5.0)


if __name__ == "__main__":
    unittest.main()
