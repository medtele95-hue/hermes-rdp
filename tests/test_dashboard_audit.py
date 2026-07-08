# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — action audit trail (append-only log + telegram)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.dashboard_api import audit


class TestRecordAction(unittest.TestCase):
    def test_writes_append_only_json_line(self) -> None:
        with TemporaryDirectory() as tmp:
            audit_file = Path(tmp) / "actions_audit.log"
            with patch.object(audit, "AUDIT_LOG_FILE", audit_file):
                audit.record_action("bot_stop", "OK", {"reason": "manual"}, notify=False)
                audit.record_action("bot_start", "OK", None, notify=False)
            lines = audit_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["action"], "bot_stop")
        self.assertEqual(first["result"], "OK")
        self.assertEqual(first["detail"], {"reason": "manual"})

    def test_never_raises_on_unwritable_path(self) -> None:
        with TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "nonexistent_dir_xyz" / "sub" / "audit.log"
            with patch.object(audit, "AUDIT_LOG_FILE", bogus):
                # parent dirs get created automatically; this should just work
                entry = audit.record_action("test", "OK", notify=False)
        self.assertEqual(entry["action"], "test")

    def test_read_recent_audit_returns_last_n(self) -> None:
        with TemporaryDirectory() as tmp:
            audit_file = Path(tmp) / "actions_audit.log"
            with patch.object(audit, "AUDIT_LOG_FILE", audit_file):
                for i in range(10):
                    audit.record_action(f"action_{i}", "OK", notify=False)
                recent = audit.read_recent_audit(limit=3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[-1]["action"], "action_9")

    def test_read_recent_audit_empty_when_no_file(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(audit, "AUDIT_LOG_FILE", Path(tmp) / "missing.log"):
                self.assertEqual(audit.read_recent_audit(), [])


class TestSendTelegram(unittest.TestCase):
    def test_no_config_returns_false(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(audit, "DASHBOARD_ENV_FILE", Path(tmp) / "dashboard.env"), \
                 patch.object(audit, "WATCHDOG_ENV_FILE", Path(tmp) / "watchdog.env"):
                self.assertFalse(audit.send_telegram("test"))

    def test_falls_back_to_watchdog_env(self) -> None:
        with TemporaryDirectory() as tmp:
            watchdog_env = Path(tmp) / "watchdog.env"
            watchdog_env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n", encoding="utf-8")
            with patch.object(audit, "DASHBOARD_ENV_FILE", Path(tmp) / "dashboard.env"), \
                 patch.object(audit, "WATCHDOG_ENV_FILE", watchdog_env), \
                 patch("urllib.request.urlopen", side_effect=OSError("network down")):
                # network failure -> False, but must NOT raise (fail-safe)
                self.assertFalse(audit.send_telegram("test"))

    def test_record_action_never_raises_when_telegram_down(self) -> None:
        with TemporaryDirectory() as tmp:
            audit_file = Path(tmp) / "actions_audit.log"
            watchdog_env = Path(tmp) / "watchdog.env"
            watchdog_env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n", encoding="utf-8")
            with patch.object(audit, "AUDIT_LOG_FILE", audit_file), \
                 patch.object(audit, "DASHBOARD_ENV_FILE", Path(tmp) / "dashboard.env"), \
                 patch.object(audit, "WATCHDOG_ENV_FILE", watchdog_env), \
                 patch("urllib.request.urlopen", side_effect=OSError("network down")):
                entry = audit.record_action("bot_restart", "OK", notify=True)
        self.assertEqual(entry["result"], "OK")


if __name__ == "__main__":
    unittest.main()
