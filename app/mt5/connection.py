from __future__ import annotations

import time
from typing import Any, Optional

import MetaTrader5 as mt5

from app.logger import log


class MT5Connection:
    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> bool:
        for attempt in range(1, 4):
            if mt5.initialize():
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
