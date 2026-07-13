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

    # ── P0-3 (2026-07-13) — surveillance de la connexion en cours de session ──
    # Avant ce correctif, `self.connected` n'etait ecrit QU'UNE FOIS, ici meme,
    # a la ligne 59, au boot. Rien ne le remettait jamais a False. Consequence :
    # tous les gardes qui le testent (app/main.py:707, :1785 -> MT5_NOT_CONNECTED)
    # etaient du CODE MORT. Si le terminal tombait en cours de session, le bot
    # continuait a appeler MT5 — qui renvoyait None partout — en silence, en se
    # croyant connecte.
    #
    # Preuve que le cas se produit : WATCHDOG_ALERTS.log, 11 alertes
    # `MT5_INIT_FAIL :: (-6, 'Terminal: Authorization failed')` le 2026-07-11.
    #
    # health_check() est appele a CHAQUE cycle (app/main.py) : il sonde le
    # terminal et remet `connected` a jour. Les gardes redeviennent vivants.

    def health_check(self) -> bool:
        """Sonde MT5 et met `self.connected` a jour. Trois conditions, toutes
        necessaires : le terminal repond, il se declare connecte au broker, et
        le compte est lisible. Un terminal qui repond mais a perdu le broker
        (`terminal_info().connected == False`) N'EST PAS connecte — c'est
        precisement l'etat ou positions_get() renvoie None."""
        try:
            terminal = mt5.terminal_info()
            if terminal is None:
                self.connected = False
                return False
            if not bool(getattr(terminal, "connected", True)):
                self.connected = False
                return False
            if mt5.account_info() is None:
                self.connected = False
                return False
        except Exception as exc:
            log.warning("[MT5_HEALTH_CHECK] probe_raised error=%s", str(exc)[:160])
            self.connected = False
            return False
        self.connected = True
        return True

    def ensure_connected(self) -> bool:
        """health_check(), puis UNE tentative de reconnexion s'il echoue.
        Retourne l'etat reel de la connexion apres tentative.

        Fail-closed par construction : si la reconnexion echoue, `connected`
        reste False et les gardes appelants coupent le trading. Mieux vaut un
        cycle perdu qu'un cycle joue a l'aveugle sur un MT5 muet."""
        was_connected = self.connected
        if self.health_check():
            return True

        log.critical(
            "[MT5_CONNECTION_LOST] le terminal ne repond plus (last_error=%s) — "
            "trading suspendu, tentative de reconnexion",
            self._last_error_text(),
        )
        if self.connect():
            log.warning("[MT5_RECONNECTED] connexion MT5 retablie (etat precedent connected=%s)", was_connected)
            return True
        log.critical(
            "[MT5_RECONNECT_FAILED] reconnexion MT5 impossible (last_error=%s) — "
            "le bot reste en mode degrade, aucun ordre ne sera envoye",
            self._last_error_text(),
        )
        self.connected = False
        return False

    @staticmethod
    def _last_error_text() -> str:
        try:
            return str(mt5.last_error())
        except Exception:
            return "UNKNOWN"

    def terminal_info(self) -> Optional[Any]:
        return mt5.terminal_info()

    def account_info(self) -> Optional[Any]:
        return mt5.account_info()

    def shutdown(self) -> None:
        mt5.shutdown()
        self.connected = False
