# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — read-endpoint data builders. Focus: never raises
even with missing files/MT5 unavailable, and shape correctness against
synthetic fixtures."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.dashboard_api import data


class TestEnsureMt5Connected(unittest.TestCase):
    """The actual root cause of a real production symptom (2026-07-08):
    dashboard_api connects once at server startup; if that connection later
    drops for any reason, every subsequent read silently returned None
    forever, with no self-recovery. NOT a "MT5 only allows one connection"
    limitation — watchdog and the bot hold independent live connections at
    the same time this runs; verified live via a 3rd fresh connection
    succeeding while the dashboard's own connection was stale."""

    def test_already_connected_skips_reinitialize(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.terminal_info.return_value = object()
        result = data._ensure_mt5_connected(mock_mt5)
        self.assertTrue(result)
        mock_mt5.initialize.assert_not_called()

    def test_dropped_connection_triggers_reinitialize(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.terminal_info.return_value = None
        mock_mt5.initialize.return_value = True
        result = data._ensure_mt5_connected(mock_mt5)
        self.assertTrue(result)
        mock_mt5.initialize.assert_called_once()

    def test_reinitialize_failure_returns_false_without_raising(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.terminal_info.return_value = None
        mock_mt5.initialize.return_value = False
        result = data._ensure_mt5_connected(mock_mt5)
        self.assertFalse(result)

    def test_terminal_info_exception_falls_through_to_reinitialize(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.terminal_info.side_effect = Exception("IPC error")
        mock_mt5.initialize.return_value = True
        result = data._ensure_mt5_connected(mock_mt5)
        self.assertTrue(result)

    def test_initialize_exception_never_raises(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.terminal_info.return_value = None
        mock_mt5.initialize.side_effect = Exception("no IPC connection")
        result = data._ensure_mt5_connected(mock_mt5)
        self.assertFalse(result)

    def test_build_status_recovers_after_reconnect(self) -> None:
        """End-to-end: a dropped connection (terminal_info=None) must not
        leave build_status() stuck returning nulls forever — after
        _ensure_mt5_connected() re-initializes, the same account_info()
        call in this same invocation succeeds."""
        mock_mt5 = MagicMock()
        mock_mt5.ACCOUNT_TRADE_MODE_DEMO = 0
        mock_mt5.terminal_info.return_value = None  # looks dropped
        mock_mt5.initialize.return_value = True  # reconnect succeeds
        mock_mt5.account_info.return_value = SimpleNamespace(
            equity=9077.72, balance=9077.72, login=345297734, server="XMGlobal-MT5 10", trade_mode=0,
        )
        mock_mt5.positions_get.return_value = []
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            result = data.build_status()
        self.assertEqual(result["equity"], 9077.72)
        self.assertTrue(result["mt5_connected"])
        mock_mt5.initialize.assert_called_once()


class TestBuildStatusNoMt5(unittest.TestCase):
    def test_no_raise_when_mt5_import_fails(self) -> None:
        with patch.dict("sys.modules", {"MetaTrader5": None}):
            result = data.build_status()
        self.assertFalse(result["mt5_connected"])
        self.assertIsNone(result["equity"])
        self.assertEqual(result["positions"], [])


class TestBuildStatusWithMockedMt5(unittest.TestCase):
    def test_account_fields_populated(self) -> None:
        mock_mt5 = MagicMock()
        # MagicMock auto-configures __int__ to return 1 by default for any
        # unconfigured attribute — must set the real MT5 constant explicitly,
        # otherwise int(mock.ACCOUNT_TRADE_MODE_DEMO) silently returns 1
        # instead of hitting is_mt5_demo_account's except-fallback (which IS
        # 0), inverting the demo/real check under test.
        mock_mt5.ACCOUNT_TRADE_MODE_DEMO = 0
        mock_mt5.account_info.return_value = SimpleNamespace(
            equity=1000.0, balance=1000.0, login=345297734, server="XMGlobal-MT5 10", trade_mode=0,
        )
        mock_mt5.positions_get.return_value = []
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            result = data.build_status()
        self.assertEqual(result["equity"], 1000.0)
        self.assertEqual(result["mode"], "DEMO")
        self.assertTrue(result["mt5_connected"])

    def test_only_magic_hard_positions_included(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.account_info.return_value = SimpleNamespace(equity=1.0, balance=1.0, login=1, server="s", trade_mode=0)
        mock_mt5.positions_get.return_value = [
            SimpleNamespace(magic=909002, ticket=1, symbol="GOLD#", type=0, price_open=100, price_current=101, profit=1.0, sl=99, tp=102),
            SimpleNamespace(magic=999999, ticket=2, symbol="GOLD#", type=0, price_open=100, price_current=101, profit=1.0, sl=99, tp=102),
        ]
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            result = data.build_status()
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["positions"][0]["ticket"], 1)

    def test_real_trade_mode_reported(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.ACCOUNT_TRADE_MODE_DEMO = 0
        mock_mt5.account_info.return_value = SimpleNamespace(equity=1.0, balance=1.0, login=1, server="s", trade_mode=2)
        mock_mt5.positions_get.return_value = []
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            result = data.build_status()
        self.assertEqual(result["mode"], "REAL")


class TestBuildTodayNoData(unittest.TestCase):
    def test_no_raise_without_dataset_or_mt5(self) -> None:
        with patch.object(data, "DATASET_FILE", Path("/nonexistent/dataset.jsonl")), \
             patch.dict("sys.modules", {"MetaTrader5": None}):
            result = data.build_today()
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["net_today_usd"], 0.0)


class TestBuildTodayMt5QueryConversion(unittest.TestCase):
    """mission/FIX_KILLSWITCH_PNL.md — build_today() had the same
    tzinfo-stripping bug as app.services.daily_killswitch: TRUE-UTC window
    bounds passed to mt5.history_deals_get() via a bare .replace(tzinfo=
    None), silently querying 3h too early."""

    def test_mt5_receives_broker_shifted_bounds_not_raw_stripped(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.account_info.return_value = SimpleNamespace(equity=1.0, balance=1.0, login=1, server="s", trade_mode=0)
        mock_mt5.positions_get.return_value = []
        mock_mt5.history_deals_get.return_value = []
        fixed_now = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}), \
             patch.object(data, "_now_utc", return_value=fixed_now):
            data.build_today()
        call_start, call_end = mock_mt5.history_deals_get.call_args[0]
        # correct broker-shifted bound: true-UTC broker-midnight July7 21:00
        # -> broker-wall-clock-shaped "July8 00:00" (NOT the old buggy
        # "July7 21:00" from a bare tzinfo strip).
        self.assertEqual(call_start, datetime(2026, 7, 8, 0, 0, 0))
        self.assertIsNone(call_start.tzinfo)


class TestBuildSystemNoData(unittest.TestCase):
    def test_no_raise_with_all_files_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with patch.object(data, "EVENTS_FILE", missing), \
                 patch.object(data, "WATCHDOG_HEARTBEAT_FILE", missing), \
                 patch.object(data, "SUPERVISOR_STATE_FILE", missing), \
                 patch.object(data, "BACKUPS_DIR", missing), \
                 patch.object(data, "WATCHDOG_ALERTS_FILE", missing), \
                 patch.object(data, "DATASET_FILE", missing):
                result = data.build_system()
        self.assertEqual(result["kill_switch"], {})
        self.assertIsNone(result["watchdog_heartbeat_age_seconds"])
        self.assertEqual(result["last_backup"], "AUCUN")
        self.assertEqual(result["dataset_lines"], 0)

    def test_reads_killswitch_from_latest_event(self) -> None:
        with TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "events.jsonl"
            events_file.write_text(
                json.dumps({"daily_killswitch": {"losses_today": 3, "max_losses_per_day": 6, "drawdown_pct": 1.0, "triggered": False}}) + "\n",
                encoding="utf-8",
            )
            missing = Path(tmp) / "missing"
            with patch.object(data, "EVENTS_FILE", events_file), \
                 patch.object(data, "WATCHDOG_HEARTBEAT_FILE", missing), \
                 patch.object(data, "SUPERVISOR_STATE_FILE", missing), \
                 patch.object(data, "BACKUPS_DIR", missing), \
                 patch.object(data, "WATCHDOG_ALERTS_FILE", missing), \
                 patch.object(data, "DATASET_FILE", missing):
                result = data.build_system()
        self.assertEqual(result["kill_switch"]["losses_today"], 3)


class TestBuildSensesNoData(unittest.TestCase):
    def test_no_raise_without_mt5_or_dataset(self) -> None:
        with patch.dict("sys.modules", {"MetaTrader5": None}), \
             patch.object(data, "DATASET_FILE", Path("/nonexistent")), \
             patch.object(data, "NEWS_CACHE_FILE", Path("/nonexistent")):
            result = data.build_senses()
        self.assertIsNone(result["ees_buy"])
        self.assertIsNone(result["next_high_news"])

    def test_next_high_news_filters_by_impact_and_time(self) -> None:
        with TemporaryDirectory() as tmp:
            news_file = Path(tmp) / "news.json"
            news_file.write_text(json.dumps({
                "events": [
                    {"title": "past high", "impact": "High", "time_utc": "2020-01-01T00:00:00+00:00"},
                    {"title": "future low", "impact": "Low", "time_utc": "2099-01-01T00:00:00+00:00"},
                    {"title": "future high", "impact": "High", "time_utc": "2099-01-01T00:00:00+00:00"},
                ]
            }), encoding="utf-8")
            with patch.dict("sys.modules", {"MetaTrader5": None}), \
                 patch.object(data, "DATASET_FILE", Path("/nonexistent")), \
                 patch.object(data, "NEWS_CACHE_FILE", news_file):
                result = data.build_senses()
        self.assertEqual(result["next_high_news"]["title"], "future high")


class TestBuildJournal(unittest.TestCase):
    def _dataset(self, tmp: str) -> Path:
        path = Path(tmp) / "dataset.jsonl"
        rows = [
            {"row_type": "outcome", "ticket": 1, "symbol": "GOLD#", "direction": "BUY", "pnl_reconciled": 5.0,
             "closed_at": "2026-07-08T10:00:00+00:00", "setup_id": "a", "outcome": "TP_HIT"},
            {"row_type": "outcome", "ticket": 2, "symbol": "BTCUSD#", "direction": "SELL", "pnl_reconciled": -3.0,
             "closed_at": "2026-07-08T09:00:00+00:00", "setup_id": "b", "outcome": "SL_HIT"},
            {"row_type": "decision", "setup_id": "a", "strategy": "SIMO_ATM_BREAKOUT", "final_confluence_score": 80.0},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return path

    def test_pagination_and_sort_order(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(data, "DATASET_FILE", self._dataset(tmp)):
                result = data.build_journal(page=1, page_size=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["ticket"], 1)  # most recent closed_at first

    def test_filter_by_result_win(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(data, "DATASET_FILE", self._dataset(tmp)):
                result = data.build_journal(result="win")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["pnl_usd"], 5.0)

    def test_filter_by_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(data, "DATASET_FILE", self._dataset(tmp)):
                result = data.build_journal(symbol="BTCUSD#")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["symbol"], "BTCUSD#")

    def test_crosses_decision_row_for_strategy_and_confluence(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(data, "DATASET_FILE", self._dataset(tmp)):
                result = data.build_journal()
        win_entry = next(e for e in result["entries"] if e["ticket"] == 1)
        self.assertEqual(win_entry["strategy"], "SIMO_ATM_BREAKOUT")
        self.assertEqual(win_entry["confluence_at_entry"], 80.0)


if __name__ == "__main__":
    unittest.main()
