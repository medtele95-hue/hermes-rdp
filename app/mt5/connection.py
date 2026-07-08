from __future__ import annotations

import time
from typing import Any, Optional

import MetaTrader5 as mt5

from app.logger import log


class MT5Connection:
    def __init__(self) -> None:
        self.connected = False

    def connect(
        self,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        path: str | None = None,
    ) -> bool:
        # mission/DASHBOARD.md (2026-07-08): optional explicit credentials for
        # the account-switch action. Called with no args (the default), this
        # is byte-for-byte the original zero-arg behaviour — attach to
        # whatever MT5 terminal is already logged in. Only when a caller
        # passes all three of login/password/server does this actually
        # attempt a fresh login to a specific account.
        init_kwargs: dict = {}
        if path:
            init_kwargs["path"] = path
        if login is not None and password and server:
            init_kwargs.update({"login": login, "password": password, "server": server})
        for attempt in range(1, 4):
            if mt5.initialize(**init_kwargs):
                break
            last_error = mt5.last_error()
            log.error("MT5 initialize failed attempt=%s error=%s", attempt, last_error)
            if attempt < 3:
                sleep_seconds = attempt * 5
                log.warning("MT5 initialize retrying after %s seconds", sleep_seconds)
                time.sleep(sleep_seconds)
        else:
            self.connected = False
            log.error("MT5 initialize failed after retries; exiting cleanly")
            return False

        terminal = mt5.terminal_info()
        if terminal is None:
            log.error("MT5 terminal_info failed: %s", mt5.last_error())
            self.connected = False
            return False

        account = mt5.account_info()
        if account is None:
            log.error("MT5 account_info failed: %s", mt5.last_error())
            self.connected = False
            return False

        self.connected = True
        log.info("MT5 connected")
        log.info("Account detected")
        return True

    def terminal_info(self) -> Optional[Any]:
        return mt5.terminal_info()

    def account_info(self) -> Optional[Any]:
        return mt5.account_info()

    def shutdown(self) -> None:
        mt5.shutdown()
        self.connected = False
