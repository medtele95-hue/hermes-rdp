# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — FastAPI wiring. Focus: read routes never require a
PIN, action routes always do, rate-limit/lockout responses use the right
HTTP status codes, and the static page is served at /."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.dashboard_api import actions, audit, security
from app.dashboard_api.server import app


class DashboardServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        security.reset_state_for_tests()
        self._tmp = TemporaryDirectory()
        self._dashboard_env = Path(self._tmp.name) / "dashboard.env"
        self._audit_log = Path(self._tmp.name) / "audit.log"
        self._pin = security.ensure_pin_configured(self._dashboard_env)
        self._patches = [
            patch.object(security, "DASHBOARD_ENV_FILE", self._dashboard_env),
            patch.object(audit, "AUDIT_LOG_FILE", self._audit_log),
            patch.object(audit, "DASHBOARD_ENV_FILE", self._dashboard_env),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        security.reset_state_for_tests()
        self._tmp.cleanup()


class TestReadRoutesNoPin(DashboardServerTestCase):
    def test_status_no_pin_required(self) -> None:
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("equity", r.json())

    def test_today_no_pin_required(self) -> None:
        r = self.client.get("/api/today")
        self.assertEqual(r.status_code, 200)

    def test_system_no_pin_required(self) -> None:
        r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 200)

    def test_senses_no_pin_required(self) -> None:
        r = self.client.get("/api/senses")
        self.assertEqual(r.status_code, 200)

    def test_journal_no_pin_required(self) -> None:
        r = self.client.get("/api/journal")
        self.assertEqual(r.status_code, 200)

    def test_index_page_served(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"HERMES", r.content)


class TestActionRoutesRequirePin(DashboardServerTestCase):
    def test_bot_stop_wrong_pin_refused(self) -> None:
        with patch.object(actions, "_find_bot_pids", return_value=[]):
            r = self.client.post("/api/action/bot/stop", json={"pin": "000000"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_bot_stop_correct_pin_succeeds(self) -> None:
        with patch.object(actions, "_find_bot_pids", return_value=[]), \
             patch.object(actions, "STOPPED_BY_USER_FLAG", Path(self._tmp.name) / "flag"):
            r = self.client.post("/api/action/bot/stop", json={"pin": self._pin})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_missing_pin_field_treated_as_wrong(self) -> None:
        r = self.client.post("/api/action/bot/start", json={})
        self.assertEqual(r.status_code, 401)

    def test_rate_limit_blocks_immediate_second_action(self) -> None:
        with patch.object(actions, "_find_bot_pids", return_value=[]), \
             patch.object(actions, "STOPPED_BY_USER_FLAG", Path(self._tmp.name) / "flag"):
            r1 = self.client.post("/api/action/bot/stop", json={"pin": self._pin})
            r2 = self.client.post("/api/action/bot/start", json={"pin": self._pin})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 429)

    def test_five_wrong_pins_locks_out_even_correct_pin(self) -> None:
        # rate-limit (1 action/5s) is tested independently in
        # test_dashboard_security.py and would itself block 5 rapid HTTP
        # calls before the lockout counter ever reached 5 — bypass it here
        # to isolate the lockout integration specifically.
        with patch.object(security, "check_rate_limit", return_value=(True, 0.0)):
            for _ in range(5):
                self.client.post("/api/action/bot/stop", json={"pin": "000000"})
            r = self.client.post("/api/action/bot/stop", json={"pin": self._pin})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.json()["reason"].startswith("LOCKED_OUT_"))

    def test_account_switch_real_without_confirmation_refused_but_200(self) -> None:
        """PIN correct but the domain-level refusal (missing REAL
        confirmation text) is a 200 with ok=False, not an HTTP error — the
        PIN gate and the action's own business rules are separate layers."""
        r = self.client.post("/api/action/account/switch", json={"pin": self._pin, "target": "REAL"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(r.json()["reason"], "REAL_CONFIRMATION_TEXT_MISMATCH")

    def test_every_action_writes_audit_entry(self) -> None:
        with patch.object(actions, "_find_bot_pids", return_value=[]), \
             patch.object(actions, "STOPPED_BY_USER_FLAG", Path(self._tmp.name) / "flag"):
            self.client.post("/api/action/bot/stop", json={"pin": self._pin})
        self.assertTrue(self._audit_log.exists())
        content = self._audit_log.read_text(encoding="utf-8")
        self.assertIn("bot_stop", content)

    def test_refused_action_also_writes_audit_entry(self) -> None:
        self.client.post("/api/action/bot/stop", json={"pin": "000000"})
        self.assertTrue(self._audit_log.exists())
        content = self._audit_log.read_text(encoding="utf-8")
        self.assertIn("REFUSED", content)


if __name__ == "__main__":
    unittest.main()
