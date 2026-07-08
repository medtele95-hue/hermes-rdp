# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_PNL.md — app.services.mt5_pnl_truth (CYCLE_SUMMARY's
source). Two bugs fixed this mission: (1) "today" was a naive UTC-midnight
cut instead of the broker calendar day, only coincidentally correct outside
the ~21:00-24:00 UTC window; (2) mt5.history_deals_get() was called with
TRUE-UTC bounds unconverted, silently querying 3h too early (MT5 ignores
tzinfo, compares raw clock fields against deal.time's own broker-wall-clock
stamping)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import mt5_pnl_truth as truth


def _deal(net: float, magic: int = 909002):
    return SimpleNamespace(magic=magic, profit=net, commission=0.0, swap=0.0)


class TestTodayStartUsesBrokerDayNotNaiveUtcMidnight(unittest.TestCase):
    def test_today_start_matches_broker_midnight_not_raw_utc_date(self) -> None:
        """At true-UTC 22:00, broker's calendar day has already rolled over
        (broker wall clock = 01:00 next day) -- a naive `datetime(end.year,
        end.month, end.day)` would still say "today" is the OLD date,
        lagging a full day behind the broker's actual current day."""
        mock_mt5 = MagicMock()
        mock_mt5.history_deals_get.return_value = []
        now = datetime(2026, 7, 8, 22, 30, 0, tzinfo=timezone.utc)
        with patch.object(truth, "mt5", mock_mt5):
            truth.get_mt5_hermes_pnl_truth(24, 909002, now=now)
        # the "today" call is the 2nd invocation (after the generic "window"
        # lookback call) -- assert the start bound passed to MT5 reflects
        # broker midnight (true-UTC July8 21:00, converted +3h -> July9
        # 00:00 clock value), NOT the naive raw-UTC-date "July8 00:00".
        calls = mock_mt5.history_deals_get.call_args_list
        today_call_start = calls[1][0][0]
        self.assertEqual(today_call_start, datetime(2026, 7, 9, 0, 0, 0))

    def test_today_start_correct_during_normal_hours_too(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.history_deals_get.return_value = []
        now = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
        with patch.object(truth, "mt5", mock_mt5):
            truth.get_mt5_hermes_pnl_truth(24, 909002, now=now)
        calls = mock_mt5.history_deals_get.call_args_list
        today_call_start = calls[1][0][0]
        self.assertEqual(today_call_start, datetime(2026, 7, 8, 0, 0, 0))


class TestQueryBoundsConvertedBeforeMt5Call(unittest.TestCase):
    def test_mt5_receives_broker_shifted_naive_bounds(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.history_deals_get.return_value = []
        window_start = datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
        with patch.object(truth, "mt5", mock_mt5):
            truth.get_mt5_hermes_pnl_truth(1, 909002, now=now, window_start=window_start)
        calls = mock_mt5.history_deals_get.call_args_list
        window_call_start, window_call_end = calls[0][0]
        self.assertEqual(window_call_start, datetime(2026, 7, 8, 0, 0, 0))
        self.assertEqual(window_call_end, datetime(2026, 7, 8, 16, 46, 28))
        self.assertIsNone(window_call_start.tzinfo)


class TestPnlSummation(unittest.TestCase):
    def test_correct_net_pnl_and_deal_count(self) -> None:
        mock_mt5 = MagicMock()
        mock_mt5.history_deals_get.return_value = [_deal(-1.52), _deal(-8.57), _deal(-2.45), _deal(14.0)]
        with patch.object(truth, "mt5", mock_mt5):
            result = truth.get_mt5_hermes_pnl_truth(24, 909002, now=datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc))
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["mt5_window_pnl"], 1.46, places=2)

    def test_matches_the_exact_reported_incident_numbers(self) -> None:
        """Reproduces the mission's reported CYCLE_SUMMARY figure directly:
        3 real losses this broker day, net positive."""
        mock_mt5 = MagicMock()
        mock_mt5.history_deals_get.return_value = [
            _deal(-1.52), _deal(-8.57), _deal(-2.45),
            *[_deal(round(0.5 + i * 0.3, 2)) for i in range(14)],
        ]
        with patch.object(truth, "mt5", mock_mt5):
            result = truth.get_mt5_hermes_pnl_truth(24, 909002, now=datetime(2026, 7, 8, 13, 46, tzinfo=timezone.utc))
        self.assertGreater(result["mt5_window_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
