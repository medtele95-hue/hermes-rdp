"""P0 (2026-07-13) — resilience MT5. Deux failles fermees ici.

P0-2 — `mt5.positions_get()` renvoie None quand MT5 est muet, [] quand le
compte est reellement plat. L'ancien `list(mt5.positions_get() or [])`
confondait les deux : un MT5 injoignable etait lu comme "aucune position",
et le bot marquait alors CLOSED, une par une, TOUTES ses positions
reellement ouvertes chez le broker — puis cessait de les gerer (plus d'Exit
V2, plus de trailing). Les tests ci-dessous prouvent qu'un MT5 muet ne ferme
plus rien, et qu'un compte vraiment vide continue lui d'etre traite
normalement (sinon le correctif aurait juste casse la synchronisation).

P0-3 — `MT5Connection.connected` n'etait ecrit qu'une fois, au boot. Les
gardes `if not self.mt5.connected` etaient donc du code mort : un terminal
tombe en cours de session laissait le bot tourner a l'aveugle. Les tests
prouvent que `connected` suit desormais l'etat reel, cycle par cycle.

Contexte reel : WATCHDOG_ALERTS.log porte 11 alertes
`MT5_INIT_FAIL :: (-6, 'Terminal: Authorization failed')` le 2026-07-11.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mt5.connection import MT5Connection
from app.services import mt5_position_sync as sync


def _settings() -> SimpleNamespace:
    return SimpleNamespace(demo_magic_number=909002, position_sync_lovable_enabled=False)


def _now() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class TestMt5MuetNeFermeRien(unittest.TestCase):
    """P0-2 — le coeur du correctif."""

    def test_mt5_muet_ne_ferme_AUCUNE_position(self) -> None:
        """LE test. MT5 renvoie None ; aucune position ne doit etre fermee."""
        with patch.object(sync.mt5, "positions_get", return_value=None), \
             patch.object(sync.mt5, "last_error", return_value=(-6, "Terminal: Authorization failed")), \
             patch.object(sync, "_notify_mt5_unreadable") as notify, \
             patch.object(sync, "_close_missing_lovable_trades") as close_fn:
            summary = sync.sync_open_mt5_positions_to_lovable(_settings(), MagicMock(), now=_now())

        # La fonction qui ferme les positions n'a JAMAIS ete appelee.
        close_fn.assert_not_called()
        self.assertEqual(summary["closed_tickets"], [])
        self.assertTrue(summary["mt5_unreadable"])
        self.assertEqual(summary["sync_skipped_reason"], "MT5_POSITIONS_UNREADABLE")
        notify.assert_called_once()

    def test_mt5_muet_ne_declare_pas_zero_position(self) -> None:
        """"Je ne sais pas" n'est pas "il n'y a rien". Remettre 0 ici
        reintroduirait le meme mensonge, deplace dans le dashboard."""
        with patch.object(sync.mt5, "positions_get", return_value=None), \
             patch.object(sync.mt5, "last_error", return_value=(-6, "err")), \
             patch.object(sync, "_notify_mt5_unreadable"):
            summary = sync.sync_open_mt5_positions_to_lovable(_settings(), MagicMock(), now=_now())

        self.assertIsNone(summary["mt5_open_positions_count"])
        self.assertIsNone(summary["hermes_mt5_open_positions_count"])
        self.assertIsNone(summary["open_demo_trades_count"])
        self.assertNotEqual(summary["hermes_mt5_open_positions_count"], 0)

    def test_positions_get_qui_leve_est_aussi_fail_closed(self) -> None:
        with patch.object(sync.mt5, "positions_get", side_effect=RuntimeError("IPC timeout")), \
             patch.object(sync.mt5, "last_error", return_value=(-10004, "No IPC connection")), \
             patch.object(sync, "_notify_mt5_unreadable"), \
             patch.object(sync, "_close_missing_lovable_trades") as close_fn:
            summary = sync.sync_open_mt5_positions_to_lovable(_settings(), MagicMock(), now=_now())

        close_fn.assert_not_called()
        self.assertTrue(summary["mt5_unreadable"])

    def test_log_critical_emis(self) -> None:
        with patch.object(sync.mt5, "positions_get", return_value=None), \
             patch.object(sync.mt5, "last_error", return_value=(-6, "err")), \
             patch.object(sync, "_notify_mt5_unreadable"), \
             patch.object(sync.log, "critical") as crit:
            sync.sync_open_mt5_positions_to_lovable(_settings(), MagicMock(), now=_now())
        crit.assert_called_once()
        self.assertIn("POSITION_SYNC_MT5_UNREADABLE", crit.call_args.args[0])

    def test_compte_REELLEMENT_vide_reste_traite_normalement(self) -> None:
        """Garde-fou anti-surcorrection : [] n'est PAS None. Un compte vraiment
        plat doit continuer a fermer les trades obsoletes, sinon le correctif
        aurait simplement casse la synchronisation."""
        with patch.object(sync.mt5, "positions_get", return_value=[]), \
             patch.object(sync, "_notify_mt5_unreadable") as notify, \
             patch.object(sync, "_confirmed_order_metadata_by_ticket", return_value={}), \
             patch.object(sync, "_closed_pnl_today_from_events", return_value=0.0), \
             patch.object(sync, "get_mt5_hermes_pnl_truth", return_value={"available": False}), \
             patch.object(sync, "_close_missing_lovable_trades", return_value=(0, [], [])) as close_fn:
            summary = sync.sync_open_mt5_positions_to_lovable(_settings(), MagicMock(), now=_now())

        close_fn.assert_called_once()  # le chemin normal vit toujours
        notify.assert_not_called()     # aucune fausse alerte
        self.assertFalse(summary.get("mt5_unreadable", False))
        self.assertEqual(summary["mt5_open_positions_count"], 0)  # 0 REEL, cette fois

    def test_alerte_throttlee_par_cooldown(self) -> None:
        """Le cycle tourne toutes les ~5 s ; une deconnexion dure des minutes.
        Sans cooldown, ce sont des centaines de messages Telegram."""
        sync._last_alert_monotonic = None
        with patch.object(sync, "_send_telegram_alert") as sender, \
             patch.object(sync.threading, "Thread") as thread:
            thread.side_effect = lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args))
            sync._notify_mt5_unreadable("err-1")
            sync._notify_mt5_unreadable("err-2")
            sync._notify_mt5_unreadable("err-3")
        self.assertEqual(sender.call_count, 1)
        sync._last_alert_monotonic = None


class TestConnexionMt5PlusFigeeAuBoot(unittest.TestCase):
    """P0-3 — `connected` doit suivre l'etat reel, pas rester fige au boot."""

    def test_terminal_muet_rend_connected_false(self) -> None:
        conn = MT5Connection()
        conn.connected = True  # etat herite du boot
        with patch.object(sync.mt5, "positions_get"), \
             patch("app.mt5.connection.mt5") as m:
            m.terminal_info.return_value = None
            self.assertFalse(conn.health_check())
        self.assertFalse(conn.connected)

    def test_terminal_repond_mais_broker_perdu_rend_connected_false(self) -> None:
        """Le cas subtil : le terminal repond, mais il a perdu le broker. C'est
        precisement l'etat ou positions_get() renvoie None."""
        conn = MT5Connection()
        conn.connected = True
        with patch("app.mt5.connection.mt5") as m:
            m.terminal_info.return_value = SimpleNamespace(connected=False)
            m.account_info.return_value = SimpleNamespace(login=1)
            self.assertFalse(conn.health_check())
        self.assertFalse(conn.connected)

    def test_compte_illisible_rend_connected_false(self) -> None:
        conn = MT5Connection()
        conn.connected = True
        with patch("app.mt5.connection.mt5") as m:
            m.terminal_info.return_value = SimpleNamespace(connected=True)
            m.account_info.return_value = None
            self.assertFalse(conn.health_check())
        self.assertFalse(conn.connected)

    def test_health_check_nominal(self) -> None:
        conn = MT5Connection()
        with patch("app.mt5.connection.mt5") as m:
            m.terminal_info.return_value = SimpleNamespace(connected=True)
            m.account_info.return_value = SimpleNamespace(login=1)
            self.assertTrue(conn.health_check())
        self.assertTrue(conn.connected)

    def test_ensure_connected_reconnecte(self) -> None:
        conn = MT5Connection()
        with patch("app.mt5.connection.mt5") as m, \
             patch.object(MT5Connection, "connect", return_value=True) as connect:
            m.terminal_info.return_value = None  # sonde en echec
            m.last_error.return_value = (-6, "Terminal: Authorization failed")
            self.assertTrue(conn.ensure_connected())
        connect.assert_called_once()

    def test_ensure_connected_reste_fail_closed_si_reconnexion_impossible(self) -> None:
        """Si la reconnexion echoue, connected DOIT rester False : les gardes
        appelants coupent alors le trading. Un cycle perdu vaut mieux qu'un
        cycle joue a l'aveugle."""
        conn = MT5Connection()
        conn.connected = True
        with patch("app.mt5.connection.mt5") as m, \
             patch.object(MT5Connection, "connect", return_value=False):
            m.terminal_info.return_value = None
            m.last_error.return_value = (-6, "err")
            self.assertFalse(conn.ensure_connected())
        self.assertFalse(conn.connected)

    def test_le_garde_mt5_not_connected_nest_plus_du_code_mort(self) -> None:
        """La regression exacte : avant le correctif, `connected` restait True
        pour toujours apres le boot, donc `if not self.mt5.connected` ne se
        declenchait jamais. On simule ici un boot reussi suivi d'une chute du
        terminal, et on verifie que le garde bascule bien."""
        conn = MT5Connection()
        with patch("app.mt5.connection.mt5") as m:
            m.initialize.return_value = True
            m.terminal_info.return_value = SimpleNamespace(connected=True)
            m.account_info.return_value = SimpleNamespace(login=1)
            self.assertTrue(conn.connect())
            self.assertTrue(conn.connected)  # boot OK

            m.terminal_info.return_value = None  # le terminal tombe
            m.last_error.return_value = (-6, "Terminal: Authorization failed")
            conn.health_check()

        self.assertFalse(conn.connected)  # le garde est vivant


if __name__ == "__main__":
    unittest.main()
