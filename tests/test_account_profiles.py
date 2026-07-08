# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — account_profiles.env resolution. The critical
safety property: an unconfigured REAL profile must ALWAYS refuse to be
switched to, and resolving connection kwargs must be a no-op ({}) until
SIMO explicitly fills in a profile by hand."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.mt5 import account_profiles as ap


class TestLoadProfiles(unittest.TestCase):
    def test_missing_file_returns_unconfigured_profiles(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles = ap.load_profiles(Path(tmp) / "does_not_exist.env")
        self.assertFalse(profiles["DEMO"].configured)
        self.assertFalse(profiles["REAL"].configured)

    def test_demo_configured_real_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "account_profiles.env"
            path.write_text(
                "DEMO_PROFILE_LOGIN=345297734\n"
                "DEMO_PROFILE_PASSWORD=\n"
                "DEMO_PROFILE_SERVER=XMGlobal-MT5 10\n"
                "REAL_PROFILE_LOGIN=\n",
                encoding="utf-8",
            )
            profiles = ap.load_profiles(path)
        self.assertTrue(profiles["DEMO"].configured)
        self.assertEqual(profiles["DEMO"].login, 345297734)
        self.assertFalse(profiles["REAL"].configured)

    def test_real_configured_when_login_present(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "account_profiles.env"
            path.write_text(
                "REAL_PROFILE_LOGIN=999888\n"
                "REAL_PROFILE_PASSWORD=hunter2\n"
                "REAL_PROFILE_SERVER=Broker-Live\n",
                encoding="utf-8",
            )
            profiles = ap.load_profiles(path)
        self.assertTrue(profiles["REAL"].configured)
        self.assertEqual(profiles["REAL"].login, 999888)

    def test_non_digit_login_is_unconfigured(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "account_profiles.env"
            path.write_text("REAL_PROFILE_LOGIN=not-a-number\n", encoding="utf-8")
            profiles = ap.load_profiles(path)
        self.assertFalse(profiles["REAL"].configured)


class TestActiveProfile(unittest.TestCase):
    def test_defaults_to_demo_when_no_state_file(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(ap, "ACTIVE_PROFILE_FILE", Path(tmp) / "active_profile.txt"):
                self.assertEqual(ap.get_active_profile_name(), "DEMO")

    def test_set_then_get_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(ap, "ACTIVE_PROFILE_FILE", Path(tmp) / "sub" / "active_profile.txt"):
                ap.set_active_profile_name("REAL")
                self.assertEqual(ap.get_active_profile_name(), "REAL")

    def test_set_rejects_invalid_name(self) -> None:
        with self.assertRaises(ValueError):
            ap.set_active_profile_name("NOT_A_PROFILE")

    def test_corrupt_state_file_falls_back_to_demo(self) -> None:
        with TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "active_profile.txt"
            state_file.write_text("GARBAGE_VALUE", encoding="utf-8")
            with patch.object(ap, "ACTIVE_PROFILE_FILE", state_file):
                self.assertEqual(ap.get_active_profile_name(), "DEMO")


class TestResolveActiveConnectionKwargs(unittest.TestCase):
    def test_unconfigured_active_profile_returns_empty_dict(self) -> None:
        """The single most important safety property in this module: with no
        account_profiles.env (or an unconfigured active profile), connecting
        must be IDENTICAL to today's zero-arg behaviour."""
        with TemporaryDirectory() as tmp:
            with patch.object(ap, "ACTIVE_PROFILE_FILE", Path(tmp) / "active_profile.txt"), \
                 patch.object(ap, "PROFILES_FILE", Path(tmp) / "account_profiles.env"):
                self.assertEqual(ap.resolve_active_connection_kwargs(), {})

    def test_configured_demo_returns_login_kwargs(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = Path(tmp) / "account_profiles.env"
            profiles_file.write_text(
                "DEMO_PROFILE_LOGIN=345297734\n"
                "DEMO_PROFILE_PASSWORD=secret\n"
                "DEMO_PROFILE_SERVER=XMGlobal-MT5 10\n",
                encoding="utf-8",
            )
            with patch.object(ap, "ACTIVE_PROFILE_FILE", Path(tmp) / "active_profile.txt"), \
                 patch.object(ap, "PROFILES_FILE", profiles_file):
                kwargs = ap.resolve_active_connection_kwargs()
        self.assertEqual(kwargs["login"], 345297734)
        self.assertEqual(kwargs["password"], "secret")
        self.assertEqual(kwargs["server"], "XMGlobal-MT5 10")
        self.assertNotIn("path", kwargs)

    def test_real_active_but_unconfigured_returns_empty_dict(self) -> None:
        """Even if active_profile.txt says REAL, an empty REAL profile in
        account_profiles.env must still resolve to {} — never a partial or
        garbage credential set."""
        with TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "active_profile.txt"
            active_file.parent.mkdir(parents=True, exist_ok=True)
            active_file.write_text("REAL", encoding="utf-8")
            with patch.object(ap, "ACTIVE_PROFILE_FILE", active_file), \
                 patch.object(ap, "PROFILES_FILE", Path(tmp) / "account_profiles.env"):
                self.assertEqual(ap.resolve_active_connection_kwargs(), {})

    def test_terminal_path_included_when_present(self) -> None:
        with TemporaryDirectory() as tmp:
            profiles_file = Path(tmp) / "account_profiles.env"
            profiles_file.write_text(
                "REAL_PROFILE_LOGIN=999888\n"
                "REAL_PROFILE_PASSWORD=hunter2\n"
                "REAL_PROFILE_SERVER=Broker-Live\n"
                "REAL_PROFILE_TERMINAL_PATH=C:\\MT5Real\\terminal64.exe\n",
                encoding="utf-8",
            )
            active_file = Path(tmp) / "active_profile.txt"
            active_file.write_text("REAL", encoding="utf-8")
            with patch.object(ap, "ACTIVE_PROFILE_FILE", active_file), \
                 patch.object(ap, "PROFILES_FILE", profiles_file):
                kwargs = ap.resolve_active_connection_kwargs()
        self.assertEqual(kwargs["path"], "C:\\MT5Real\\terminal64.exe")


if __name__ == "__main__":
    unittest.main()
