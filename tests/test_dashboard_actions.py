# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — action handlers. The account-switch tests are the
highest-stakes coverage in this whole mission: an unconfigured REAL profile
must ALWAYS refuse, a wrong/missing typed confirmation must ALWAYS refuse,
and open positions on the account being left must ALWAYS refuse."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.dashboard_api import actions
from app.mt5 import account_profiles as ap


class TestBotStop(unittest.TestCase):
    def test_writes_stopped_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            flag = Path(tmp) / "logs" / "stopped_by_user.flag"
            with patch.object(actions, "STOPPED_BY_USER_FLAG", flag), \
                 patch.object(actions, "_find_bot_pids", return_value=[]):
                result = actions.action_bot_stop()
            self.assertTrue(result["ok"])
            self.assertTrue(flag.exists())

    def test_documents_sl_tp_behaviour(self) -> None:
        with TemporaryDirectory() as tmp:
            flag = Path(tmp) / "stopped_by_user.flag"
            with patch.object(actions, "STOPPED_BY_USER_FLAG", flag), \
                 patch.object(actions, "_find_bot_pids", return_value=[]):
                result = actions.action_bot_stop()
        self.assertIn("SL/TP", result["note"])


class TestBotStart(unittest.TestCase):
    def test_clears_stopped_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            flag = Path(tmp) / "stopped_by_user.flag"
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("stopped", encoding="utf-8")
            with patch.object(actions, "STOPPED_BY_USER_FLAG", flag):
                actions.action_bot_start()
        self.assertFalse(flag.exists())

    def test_no_error_when_flag_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            flag = Path(tmp) / "missing.flag"
            with patch.object(actions, "STOPPED_BY_USER_FLAG", flag):
                result = actions.action_bot_start()
        self.assertTrue(result["ok"])


class TestSymbolsToggle(unittest.TestCase):
    def test_refuses_both_off_while_bot_running(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            with patch.object(actions, "ACTIVE_SYMBOLS_FILE", path), \
                 patch.object(actions, "is_bot_running", return_value=True):
                result = actions.action_symbols_toggle(False, False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "AT_LEAST_ONE_SYMBOL_OR_BOT_PAUSED_REQUIRED")
        self.assertFalse(path.exists())

    def test_allows_both_off_when_bot_stopped(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            with patch.object(actions, "ACTIVE_SYMBOLS_FILE", path), \
                 patch.object(actions, "is_bot_running", return_value=False):
                result = actions.action_symbols_toggle(False, False)
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_gold_only(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            with patch.object(actions, "ACTIVE_SYMBOLS_FILE", path), \
                 patch.object(actions, "is_bot_running", return_value=True):
                result = actions.action_symbols_toggle(True, False)
        self.assertEqual(result["active_symbols"], ["GOLD#"])


class TestAccountSwitch(unittest.TestCase):
    def _empty_profiles(self, tmp: str) -> Path:
        return Path(tmp) / "account_profiles.env"

    def test_rejects_invalid_target(self) -> None:
        result = actions.action_account_switch("MARS")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "INVALID_TARGET")

    def test_real_without_confirmation_text_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = self._empty_profiles(tmp)
            profiles_file.write_text("REAL_PROFILE_LOGIN=999\nREAL_PROFILE_PASSWORD=p\nREAL_PROFILE_SERVER=s\n", encoding="utf-8")
            with patch.object(ap, "PROFILES_FILE", profiles_file):
                result = actions.action_account_switch("REAL", confirm_real_text=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "REAL_CONFIRMATION_TEXT_MISMATCH")

    def test_real_with_wrong_confirmation_text_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = self._empty_profiles(tmp)
            profiles_file.write_text("REAL_PROFILE_LOGIN=999\nREAL_PROFILE_PASSWORD=p\nREAL_PROFILE_SERVER=s\n", encoding="utf-8")
            with patch.object(ap, "PROFILES_FILE", profiles_file):
                result = actions.action_account_switch("REAL", confirm_real_text="real")  # lowercase, wrong
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "REAL_CONFIRMATION_TEXT_MISMATCH")

    def test_real_unconfigured_always_refuses_even_with_correct_confirmation(self) -> None:
        """THE most important safety property in the whole mission: an
        empty REAL profile in account_profiles.env can never be switched
        to, no matter what the caller sends."""
        with TemporaryDirectory() as tmp:
            profiles_file = self._empty_profiles(tmp)  # never written -> empty
            with patch.object(ap, "PROFILES_FILE", profiles_file):
                result = actions.action_account_switch("REAL", confirm_real_text="REAL")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "PROFILE_NOT_CONFIGURED")

    def test_refuses_when_open_positions_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = self._empty_profiles(tmp)
            profiles_file.write_text("DEMO_PROFILE_LOGIN=345297734\nDEMO_PROFILE_SERVER=s\n", encoding="utf-8")
            with patch.object(ap, "PROFILES_FILE", profiles_file), \
                 patch.object(actions, "_current_account_has_open_positions", return_value=True):
                result = actions.action_account_switch("DEMO")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "OPEN_POSITIONS_ON_CURRENT_ACCOUNT")

    def test_mt5_unreadable_fails_safe_refuses_switch(self) -> None:
        """If we can't verify open positions, refuse rather than risk
        abandoning them."""
        with patch("MetaTrader5.positions_get", side_effect=Exception("no connection"), create=True):
            self.assertTrue(actions._current_account_has_open_positions())

    def test_successful_demo_switch_sets_active_profile_and_restarts(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = self._empty_profiles(tmp)
            profiles_file.write_text("DEMO_PROFILE_LOGIN=345297734\nDEMO_PROFILE_SERVER=s\n", encoding="utf-8")
            active_file = Path(tmp) / "active_profile.txt"
            with patch.object(ap, "PROFILES_FILE", profiles_file), \
                 patch.object(ap, "ACTIVE_PROFILE_FILE", active_file), \
                 patch.object(actions, "_current_account_has_open_positions", return_value=False), \
                 patch.object(actions, "_find_bot_pids", return_value=[]):
                result = actions.action_account_switch("DEMO")
        self.assertTrue(result["ok"])
        self.assertEqual(ap.get_active_profile_name(), "DEMO")

    def test_never_imports_app_config_settings(self) -> None:
        """Structural guarantee: actions.py has no code path that could ever
        read or write app.config.Settings (allow_live_trading,
        real_declared_login/server) — it doesn't import that module at all.
        (The docstring legitimately names those fields in prose to explain
        the sovereign gate it can't reach; that's not a code reference.)"""
        import inspect
        source = inspect.getsource(actions)
        self.assertNotIn("import app.config", source)
        self.assertNotIn("from app.config", source)
        self.assertNotIn("from app import config", source)


class TestFindBotPids(unittest.TestCase):
    def test_excludes_watchdog_and_dashboard_processes(self) -> None:
        fake_csv = (
            "Node,CommandLine,ProcessId\n"
            ",python watchdog\\hermes_watchdog.py,111\n"
            ",python -m app.main,222\n"
            ",python -m app.dashboard_api.server,333\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = fake_csv
        with patch("subprocess.run", return_value=mock_result):
            pids = actions._find_bot_pids()
        self.assertEqual(pids, [222])


if __name__ == "__main__":
    unittest.main()
