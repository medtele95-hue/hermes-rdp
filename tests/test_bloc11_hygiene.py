"""BLOC 11 — hygiene + resilience tests.

a) [POSITION_CLOSED] anti-loop: PERSISTENT idempotence set (survives
   restarts), provisional close corrected by the real broker deal, REAL
   pnl on the event.
b) POSITION_SYNC reads MT5 directly; Lovable calls purged (flag off).
e) single-instance lock: same account+magic refuses a second start; a
   stale lock (dead PID) is reclaimed.
(c bridge verdict and d backup are operational deliverables — see
docs/BACKUP_SIMO.md and RESURRECTION_REPORT.md.)
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services.mt5_position_sync import _close_missing_lovable_trades, _closed_seen_path
from app.utils.single_instance import (
    SingleInstanceError,
    acquire_single_instance_lock,
    lock_path,
    release_single_instance_lock,
)

_NOW = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(demo_magic_number=909002, position_sync_lovable_enabled=False, **overrides)


def _events_file(name: str, ticket: str = "777") -> Path:
    path = Path("tests") / "__tmp_bloc11" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    seen = _closed_seen_path(path)
    if seen.exists():
        seen.unlink()
    open_event = {
        "event_type": "POSITION_SYNC",
        "status": "OPEN",
        "result": "OPEN",
        "ticket": ticket,
        "magic_number": 909002,
        "symbol": "GOLD#",
        "payload": {"symbol": "GOLD#", "profit": 1.0},
    }
    path.write_text(json.dumps(open_event) + "\n", encoding="utf-8")
    return path


def _deal(net: float):
    return SimpleNamespace(
        ticket=555001, order=555000, position_id=777, magic=909002,
        profit=net, commission=-0.1, swap=0.0, time=1751882400, type=1, entry=1,
        symbol="GOLD#", volume=0.01, price=3300.0,
    )


class TestPositionClosedAntiLoop(unittest.TestCase):
    def _run(self, events_path: Path, deals):
        ingest = MagicMock()
        with patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=deals):
            closed, events, already = _close_missing_lovable_trades(
                _settings(), ingest, set(), _NOW, events_path
            )
        return closed, events, already, ingest

    def test_close_emitted_once_with_real_pnl(self) -> None:
        events_path = _events_file("once")
        closed, events, already, _ = self._run(events_path, [_deal(-4.9)])
        self.assertEqual(closed, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pnl_source"], "MT5_HISTORY_DEALS")
        self.assertAlmostEqual(events[0]["pnl"], -5.0)  # profit + commission + swap
        # second evaluation: the persistent set blocks any re-emission
        closed2, events2, already2, _ = self._run(events_path, [_deal(-4.9)])
        self.assertEqual(closed2, 0)
        self.assertEqual(events2, [])
        self.assertIn("777", already2)

    def test_idempotence_survives_restart(self) -> None:
        events_path = _events_file("restart")
        self._run(events_path, [_deal(-4.9)])
        # "restart": nothing shared in memory — only the persistent file
        seen = json.loads(_closed_seen_path(events_path).read_text(encoding="utf-8"))
        self.assertEqual(seen.get("777"), "final")
        closed, events, already, _ = self._run(events_path, [_deal(-4.9)])
        self.assertEqual((closed, events), (0, []))

    def test_provisional_close_corrected_by_real_deal(self) -> None:
        events_path = _events_file("provisional")
        # deal not yet visible in history -> provisional close
        closed, events, _, _ = self._run(events_path, [])
        self.assertEqual(closed, 1)
        self.assertEqual(events[0]["pnl_source"], "PROVISIONAL_PENDING_DEAL")
        self.assertIsNone(events[0]["pnl"])
        # deal still absent -> silent wait, no event spam
        closed2, events2, _, _ = self._run(events_path, [])
        self.assertEqual((closed2, events2), (0, []))
        # deal appears -> ONE corrected event with the REAL pnl
        closed3, events3, _, _ = self._run(events_path, [_deal(2.4)])
        self.assertEqual(closed3, 1)
        self.assertEqual(events3[0]["result"], "CLOSED_CORRECTED")
        self.assertAlmostEqual(events3[0]["pnl"], 2.3)
        # and never again
        closed4, events4, _, _ = self._run(events_path, [_deal(2.4)])
        self.assertEqual((closed4, events4), (0, []))


class TestLovablePurged(unittest.TestCase):
    def test_no_lovable_calls_when_flag_off(self) -> None:
        events_path = _events_file("nolovable")
        ingest = MagicMock()
        with patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=[_deal(-1.0)]):
            _close_missing_lovable_trades(_settings(), ingest, set(), _NOW, events_path)
        ingest.get_open_demo_trades.assert_not_called()
        ingest.update_row.assert_not_called()
        ingest.send_row.assert_not_called()

    def test_flag_default_off(self) -> None:
        self.assertFalse(Settings().position_sync_lovable_enabled)


class TestSingleInstanceLock(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_dir = Path("tests") / "__tmp_bloc11" / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.lock_dir.glob("*.lock"):
            stale.unlink()

    def test_acquire_and_release(self) -> None:
        path = acquire_single_instance_lock(345297734, 909002, self.lock_dir)
        self.assertTrue(path.exists())
        release_single_instance_lock(345297734, 909002, self.lock_dir)
        self.assertFalse(path.exists())

    def test_second_instance_refused_while_owner_alive(self) -> None:
        path = lock_path(345297734, 909002, self.lock_dir)
        path.write_text(json.dumps({"pid": 424242}), encoding="utf-8")
        with patch("app.utils.single_instance._pid_alive", return_value=True):
            with self.assertRaises(SingleInstanceError):
                acquire_single_instance_lock(345297734, 909002, self.lock_dir)

    def test_stale_lock_reclaimed(self) -> None:
        path = lock_path(345297734, 909002, self.lock_dir)
        path.write_text(json.dumps({"pid": 424242}), encoding="utf-8")
        with patch("app.utils.single_instance._pid_alive", return_value=False):
            acquired = acquire_single_instance_lock(345297734, 909002, self.lock_dir)
        self.assertTrue(acquired.exists())
        release_single_instance_lock(345297734, 909002, self.lock_dir)

    def test_different_magic_not_blocked(self) -> None:
        acquire_single_instance_lock(345297734, 909002, self.lock_dir)
        # same account, different magic: allowed
        other = acquire_single_instance_lock(345297734, 909001, self.lock_dir)
        self.assertTrue(other.exists())
        release_single_instance_lock(345297734, 909002, self.lock_dir)
        release_single_instance_lock(345297734, 909001, self.lock_dir)


if __name__ == "__main__":
    unittest.main()
