# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md (2026-07-08) — account_profiles.env resolution.

Holds the DEMO and REAL connection profiles the dashboard's account-switch
action can select between. Profiles are pre-declared by SIMO by hand in
account_profiles.env (gitignored, local-only, never committed) — this
module NEVER writes profile credentials, only reads them and records which
one is "active" (a plain symbol pointer, not a credential).

If a profile has no explicit login, connecting falls back to whatever MT5
terminal is already logged in — today's exact behaviour. This is the safe
default for DEMO (works unconfigured) and the deliberate safety floor for
REAL (an empty REAL profile can never be switched to — see `configured`).

This module NEVER touches allow_live_trading or real_declared_login/server
in app.config.Settings — those remain manual, human-edited gates, completely
unreachable from the dashboard. Even a successful profile switch to the
REAL-labelled profile does not grant trading permission by itself:
app.services.adaptive_account_policy.trading_authorized() re-verifies the
live MT5 account's trade_mode/login/server and the allow_live_trading master
switch on every single order, independent of anything in this module — the
dashboard cannot reach or influence that check.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROFILES_FILE = Path(__file__).resolve().parents[2] / "account_profiles.env"
ACTIVE_PROFILE_FILE = Path(__file__).resolve().parents[2] / "logs" / "active_profile.txt"

VALID_PROFILES = ("DEMO", "REAL")


@dataclass(frozen=True)
class MT5Profile:
    name: str
    login: int | None
    password: str
    server: str
    terminal_path: str

    @property
    def configured(self) -> bool:
        """A profile is switchable-to only if it has an explicit login.
        DEMO can still be used unconfigured (falls back to the already
        logged-in terminal) but this flag is what the REAL-switch action
        checks before allowing anything — an empty REAL profile always
        refuses, by construction."""
        return self.login is not None


def _load_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def load_profiles(path: Path | None = None) -> dict[str, MT5Profile]:
    # `path` defaults to None (not the module-level PROFILES_FILE constant
    # directly) so that patching app.mt5.account_profiles.PROFILES_FILE
    # (tests, or any future caller) is respected — a mutable-default-bound-
    # at-def-time would silently ignore reassignment of the module global.
    if path is None:
        path = PROFILES_FILE
    raw = _load_env_file(path)
    profiles: dict[str, MT5Profile] = {}
    for name in VALID_PROFILES:
        login_raw = raw.get(f"{name}_PROFILE_LOGIN", "").strip()
        login = int(login_raw) if login_raw.isdigit() else None
        profiles[name] = MT5Profile(
            name=name,
            login=login,
            password=raw.get(f"{name}_PROFILE_PASSWORD", ""),
            server=raw.get(f"{name}_PROFILE_SERVER", ""),
            terminal_path=raw.get(f"{name}_PROFILE_TERMINAL_PATH", ""),
        )
    return profiles


def get_active_profile_name() -> str:
    try:
        if ACTIVE_PROFILE_FILE.exists():
            name = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip().upper()
            if name in VALID_PROFILES:
                return name
    except OSError:
        pass
    return "DEMO"


def set_active_profile_name(name: str) -> None:
    if name not in VALID_PROFILES:
        raise ValueError(f"invalid profile name: {name}")
    ACTIVE_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROFILE_FILE.write_text(name, encoding="utf-8")


def resolve_active_connection_kwargs() -> dict:
    """Kwargs for MT5Connection.connect() matching the active profile.
    Returns {} (attach to whatever terminal is already logged in) when the
    active profile isn't configured with an explicit login — always true
    for DEMO by default, so this is a no-op for today's running bot until
    SIMO explicitly fills in account_profiles.env."""
    name = get_active_profile_name()
    profile = load_profiles().get(name)
    if profile is None or not profile.configured:
        return {}
    kwargs: dict = {"login": profile.login, "password": profile.password, "server": profile.server}
    if profile.terminal_path:
        kwargs["path"] = profile.terminal_path
    return kwargs
