# -*- coding: utf-8 -*-
"""Tests du HERMES WATCHDOG — chaque violation simulée déclenche son alerte,
et le gardien ne peut PAS envoyer d'ordre hors du mode urgence (preuve)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watchdog import hermes_watchdog as wd


def _pos(**over):
    payload = {"ticket": 1, "symbol": "GOLD#", "magic": 909002, "type": 0,
               "volume": 0.01, "sl": 4100.0, "tp": 4200.0, "profit": 0.0, "comment": "HERMES"}
    payload.update(over)
    return SimpleNamespace(**payload)


def _deal(**over):
    payload = {"ticket": 1, "symbol": "GOLD#", "magic": 909002, "entry": 1,
               "profit": 1.0, "commission": 0.0, "swap": 0.0, "time": 1700000000}
    payload.update(over)
    return SimpleNamespace(**payload)


def _mgr(tmpdir: Path, cfg: dict | None = None) -> wd.AlertManager:
    return wd.AlertManager(cfg or {}, state_file=tmpdir / "state.json")


def _mt5_stub(positions=None, deals=None, equity=9000.0):
    stub = MagicMock()
    stub.positions_get.return_value = positions or []
    stub.history_deals_get.return_value = deals or []
    stub.account_info.return_value = SimpleNamespace(equity=equity, balance=equity)
    return stub


NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)  # mercredi, heures de marché


class Check1SymbolTests(unittest.TestCase):
    def test_forbidden_position_is_critical(self) -> None:
        alerts = wd.check_1_symbols([_pos(symbol="EURUSD")], [])
        self.assertTrue(any(lvl == wd.CRITICAL and "EURUSD" in msg for lvl, _, msg in alerts))

    def test_allowlisted_symbols_are_clean(self) -> None:
        self.assertEqual(wd.check_1_symbols([_pos(symbol="GOLD#"), _pos(symbol="BTCUSD#")], []), [])

    def test_forbidden_deal_of_the_day_is_critical(self) -> None:
        alerts = wd.check_1_symbols([], [_deal(symbol="US100Cash#")])
        self.assertTrue(any("US100Cash#" in msg for _, _, msg in alerts))


class Check2MaxOpenTests(unittest.TestCase):
    def test_two_gold_positions_is_critical(self) -> None:
        alerts = wd.check_2_max_open([_pos(ticket=1), _pos(ticket=2)])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], wd.CRITICAL)

    def test_one_per_symbol_is_clean(self) -> None:
        self.assertEqual(wd.check_2_max_open([_pos(ticket=1), _pos(ticket=2, symbol="BTCUSD#")]), [])


class Check3IdentityTests(unittest.TestCase):
    def test_oversized_lot_is_critical(self) -> None:
        alerts = wd.check_3_identity([_pos(volume=0.5)])
        self.assertTrue(any("lot" in msg for _, _, msg in alerts))

    def test_foreign_magic_with_hermes_comment_is_critical(self) -> None:
        alerts = wd.check_3_identity([_pos(magic=42, comment="HERMES_X")])
        self.assertTrue(any("magic" in msg for _, _, msg in alerts))


class Check4NakedTests(unittest.TestCase):
    def test_missing_sl_is_critical(self) -> None:
        alerts = wd.check_4_naked([_pos(sl=0.0)])
        self.assertEqual(alerts[0][0], wd.CRITICAL)

    def test_missing_tp_is_critical(self) -> None:
        alerts = wd.check_4_naked([_pos(tp=0.0)])
        self.assertEqual(alerts[0][0], wd.CRITICAL)


class Check5FloatingTests(unittest.TestCase):
    def test_floating_loss_above_3pct_is_high(self) -> None:
        acc = SimpleNamespace(equity=1000.0)
        alerts = wd.check_5_floating([_pos(profit=-40.0)], acc)
        self.assertEqual(alerts[0][0], wd.HIGH)

    def test_small_floating_loss_is_clean(self) -> None:
        acc = SimpleNamespace(equity=1000.0)
        self.assertEqual(wd.check_5_floating([_pos(profit=-5.0)], acc), [])


class Check6HeartbeatTests(unittest.TestCase):
    def test_stale_files_alert_during_market_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "dataset.jsonl"
            stale.write_text("x", encoding="utf-8")
            import os
            old = (NOW - timedelta(minutes=30)).timestamp()
            os.utime(stale, (old, old))
            with (patch.object(wd, "DATASET_FILE", stale),
                  patch.object(wd, "EVENTS_FILE", Path(tmp) / "absent1"),
                  patch.object(wd, "BOT_LOG_FILE", Path(tmp) / "absent2")):
                alerts = wd.check_6_bot_heartbeat(NOW)
        self.assertTrue(any(key == "BOT_STALLED" for _, key, _ in alerts))

    def test_weekend_needs_no_heartbeat(self) -> None:
        saturday = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(wd.check_6_bot_heartbeat(saturday), [])


class Check7KillswitchTests(unittest.TestCase):
    def test_pierced_killswitch_is_critical(self) -> None:
        deals = [_deal(profit=-1.0) for _ in range(7)] + [_deal(entry=0, time=1800000000)]
        alerts = wd.check_7_killswitch(deals, NOW)
        self.assertTrue(any(key == "KILLSWITCH_PIERCED" and lvl == wd.CRITICAL for lvl, key, _ in alerts))

    def test_quota_ok_is_clean(self) -> None:
        deals = [_deal(profit=-1.0) for _ in range(3)]
        self.assertEqual(wd.check_7_killswitch(deals, NOW), [])


class Check10DashboardHeartbeatTests(unittest.TestCase):
    """mission/DASHBOARD.md — le dashboard est optionnel : absence de
    fichier = pas d'erreur, heartbeat périmé = INFO seulement (jamais
    HAUTE/CRITIQUE, l'absence du dashboard n'affecte jamais le trading)."""

    def test_no_file_is_clean_dashboard_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(wd, "DASHBOARD_HEARTBEAT_FILE", Path(tmp) / "absent.txt"):
                self.assertEqual(wd.check_10_dashboard_heartbeat(NOW), [])

    def test_fresh_heartbeat_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "dashboard_heartbeat.txt"
            hb.write_text((NOW - timedelta(minutes=1)).isoformat(), encoding="utf-8")
            with patch.object(wd, "DASHBOARD_HEARTBEAT_FILE", hb):
                self.assertEqual(wd.check_10_dashboard_heartbeat(NOW), [])

    def test_stale_heartbeat_is_info_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "dashboard_heartbeat.txt"
            hb.write_text((NOW - timedelta(minutes=20)).isoformat(), encoding="utf-8")
            with patch.object(wd, "DASHBOARD_HEARTBEAT_FILE", hb):
                alerts = wd.check_10_dashboard_heartbeat(NOW)
        self.assertTrue(any(key == "DASHBOARD_STALLED" and lvl == wd.INFO for lvl, key, _ in alerts))
        self.assertFalse(any(lvl in (wd.HIGH, wd.CRITICAL) for lvl, _, _ in alerts))

    def test_corrupt_heartbeat_file_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "dashboard_heartbeat.txt"
            hb.write_text("not-a-timestamp", encoding="utf-8")
            with patch.object(wd, "DASHBOARD_HEARTBEAT_FILE", hb):
                self.assertEqual(wd.check_10_dashboard_heartbeat(NOW), [])


class NoOrderOutsideEmergencyTests(unittest.TestCase):
    """LA preuve : hors mode urgence, le watchdog n'appelle JAMAIS order_send,
    même face à une position interdite. En mode urgence, il ne ferme QUE la
    position sur symbole interdit."""

    def test_no_order_send_without_emergency_flag(self) -> None:
        stub = _mt5_stub(positions=[_pos(symbol="EURUSD"), _pos(volume=0.9, sl=0.0)])
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _mgr(Path(tmp))
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(wd, "HEARTBEAT_FILE", Path(tmp) / "hb.txt"):
                alerts = wd.run_cycle(stub, mgr, {"WATCHDOG_EMERGENCY_CLOSE": "false"}, NOW)
        stub.order_send.assert_not_called()
        self.assertTrue(alerts)  # les violations ont bien alerté

    def test_emergency_true_closes_only_forbidden_symbol(self) -> None:
        forbidden = _pos(ticket=77, symbol="EURUSD", profit=-3.0)
        legit = _pos(ticket=88, symbol="GOLD#", sl=0.0)  # nue mais allowlistée: alerte, pas de close
        stub = _mt5_stub(positions=[forbidden, legit])
        stub.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.2)
        stub.order_send.return_value = SimpleNamespace(retcode=10009)
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _mgr(Path(tmp))
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(wd, "HEARTBEAT_FILE", Path(tmp) / "hb.txt"):
                wd.run_cycle(stub, mgr, {"WATCHDOG_EMERGENCY_CLOSE": "true"}, NOW)
        stub.order_send.assert_called_once()
        request = stub.order_send.call_args.args[0]
        self.assertEqual(request["position"], 77)
        self.assertEqual(request["comment"], "WATCHDOG_FORCE_CLOSE")


class TelegramFailSafeTests(unittest.TestCase):
    def test_alert_survives_telegram_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alerts_log = Path(tmp) / "alerts.log"
            mgr = wd.AlertManager({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"},
                                  state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", alerts_log), \
                 patch("urllib.request.urlopen", side_effect=OSError("network down")):
                mgr.alert(wd.CRITICAL, "TEST_KEY", "telegram est mort mais le fichier vit")
            content = alerts_log.read_text(encoding="utf-8")
        self.assertIn("TEST_KEY", content)

    def test_antispam_30_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = wd.AlertManager({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"},
                                  state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(mgr, "send_telegram", return_value=True) as send:
                mgr.alert(wd.CRITICAL, "SAME_KEY", "premier envoi")
                mgr.alert(wd.CRITICAL, "SAME_KEY", "renvoi immédiat -> supprimé")
            self.assertEqual(send.call_count, 1)


class AutoMedicTriggerTests(unittest.TestCase):
    """GRAND_PLAN_2 mission4 (2026-07-08, SIMO validé GO) — le watchdog
    déclenche l'auto-médecin sur toute alerte CRITIQUE/HAUTE, avec la même
    fenêtre anti-spam que Telegram (30 min) pour ne pas relancer une
    session claude -p en boucle."""

    def test_critical_alert_triggers_auto_medic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = wd.AlertManager({}, state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(mgr, "trigger_auto_medic") as trigger:
                mgr.alert(wd.CRITICAL, "TEST_KEY", "probleme grave")
            trigger.assert_called_once_with("CRITIQUE:TEST_KEY")

    def test_high_alert_triggers_auto_medic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = wd.AlertManager({}, state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(mgr, "trigger_auto_medic") as trigger:
                mgr.alert(wd.HIGH, "TEST_KEY2", "probleme")
            trigger.assert_called_once_with("HAUTE:TEST_KEY2")

    def test_repeated_alert_within_antispam_window_does_not_retrigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = wd.AlertManager({}, state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(mgr, "trigger_auto_medic") as trigger:
                mgr.alert(wd.CRITICAL, "SAME_KEY", "premier")
                mgr.alert(wd.CRITICAL, "SAME_KEY", "renvoi immediat")
            self.assertEqual(trigger.call_count, 1)

    def test_alert_survives_schtasks_unavailable(self) -> None:
        """A broken/missing scheduled task must never crash the watchdog —
        same fail-safe philosophy as every other watchdog side-effect."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = wd.AlertManager({}, state_file=Path(tmp) / "state.json")
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch("subprocess.run", side_effect=OSError("schtasks not found")):
                mgr.alert(wd.CRITICAL, "TEST_KEY3", "probleme")  # must not raise
            content = (Path(tmp) / "alerts.log").read_text(encoding="utf-8")
        self.assertIn("TEST_KEY3", content)


class KillswitchHistoryQueryBoundsTests(unittest.TestCase):
    """auto_medic (2026-07-08): run_cycle() passed broker_day_window()'s
    TRUE-UTC bounds to mt5.history_deals_get() after only stripping tzinfo
    (`start.replace(tzinfo=None)`), the exact bug mission/FIX_KILLSWITCH_PNL.md
    already fixed in daily_killswitch.py and app/dashboard_api/data.py but
    never migrated here — MT5 compares raw clock fields against the
    broker's own wall clock (UTC+3), so the naive bounds queried 3h too
    early and pulled in the prior broker day's deals, mislabeling them as
    "today" and inflating the loss count (false KILLSWITCH_PIERCED)."""

    def test_history_deals_get_uses_broker_shifted_bounds(self) -> None:
        stub = _mt5_stub()
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _mgr(Path(tmp))
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(wd, "HEARTBEAT_FILE", Path(tmp) / "hb.txt"):
                wd.run_cycle(stub, mgr, {}, NOW)
        start, end = wd.broker_day_window(NOW)
        expected = wd.to_mt5_query_bounds(start, end, wd.BROKER_UTC_OFFSET_HOURS)
        stub.history_deals_get.assert_called_once_with(*expected)
        called_start, called_end = stub.history_deals_get.call_args.args
        self.assertIsNone(called_start.tzinfo)
        self.assertIsNone(called_end.tzinfo)
        # la borne naive-mais-decalee doit differer de la borne naive-nue
        # (sinon le test ne detecte rien) -- preuve que le decalage +3h est applique
        self.assertNotEqual(called_start, start.replace(tzinfo=None))


class CheckErrorNeverKillsGuardianTests(unittest.TestCase):
    def test_broken_check_is_contained(self) -> None:
        stub = _mt5_stub()
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _mgr(Path(tmp))
            with patch.object(wd, "ALERTS_LOG", Path(tmp) / "alerts.log"), \
                 patch.object(wd, "HEARTBEAT_FILE", Path(tmp) / "hb.txt"), \
                 patch.object(wd, "check_2_max_open", side_effect=RuntimeError("boom")):
                alerts = wd.run_cycle(stub, mgr, {}, NOW)
        self.assertTrue(any(key == "CHECK_ERROR" for _, key, _ in alerts))


if __name__ == "__main__":
    unittest.main()
