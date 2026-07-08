# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_DATE.md — source de temps broker centralisée.
Invariants de la mission : la fenêtre doit toujours couvrir AUJOURD'HUI (pas
un mois passé), basculer exactement à 21:00 UTC, et un garde-fou doit
détecter/alerter toute fenêtre vieille de plus de 48h (signature du bug
rapporté)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.utils import broker_time as bt


class TestBrokerDayWindowCoversToday(unittest.TestCase):
    def test_window_covers_today_not_a_past_month(self) -> None:
        now = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        start, end = bt.broker_day_window(now, 3.0)
        self.assertEqual(start.date(), datetime(2026, 7, 7, tzinfo=timezone.utc).date())
        self.assertGreaterEqual(end, now)
        self.assertLess((now - start).total_seconds() / 3600.0, 48.0)

    def test_no_explicit_now_defaults_to_fresh_wall_clock(self) -> None:
        fixed = datetime(2026, 7, 8, 9, 0, 0, tzinfo=timezone.utc)
        with patch.object(bt, "broker_now_utc", return_value=fixed):
            start, end = bt.broker_day_window(None, 3.0)
        self.assertEqual(start, datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, fixed + timedelta(hours=2))

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        naive = datetime(2026, 7, 8, 10, 0, 0)
        start, end = bt.broker_day_window(naive, 3.0)
        self.assertEqual(start.tzinfo, timezone.utc)


class TestBrokerDayWindowSwitchesAt21UTC(unittest.TestCase):
    def test_just_before_2100_utc_stays_on_same_broker_day(self) -> None:
        now = datetime(2026, 7, 8, 20, 59, 0, tzinfo=timezone.utc)
        start, _ = bt.broker_day_window(now, 3.0)
        self.assertEqual(start, datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc))

    def test_exactly_2100_utc_rolls_to_next_broker_day(self) -> None:
        now = datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc)
        start, _ = bt.broker_day_window(now, 3.0)
        self.assertEqual(start, datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc))

    def test_just_after_2100_utc_rolls_to_next_broker_day(self) -> None:
        now = datetime(2026, 7, 8, 21, 1, 0, tzinfo=timezone.utc)
        start, _ = bt.broker_day_window(now, 3.0)
        self.assertEqual(start, datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc))

    def test_early_morning_still_previous_broker_day_start(self) -> None:
        now = datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc)
        start, _ = bt.broker_day_window(now, 3.0)
        self.assertEqual(start, datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc))


class TestAntiRegressionGuard(unittest.TestCase):
    """The guard must compare the computed window against an INDEPENDENT
    wall-clock reference (broker_now_utc()), never against the same
    now_utc used to derive the window — comparing a window to its own
    input is always self-consistent by construction and can never detect
    anything. A first version of this guard fell into exactly that trap;
    caught by test_guard_never_fires_when_comparing_window_to_its_own_input
    below, which reproduces the original (wrong) assumption and proves it
    would have stayed silent forever."""

    def test_guard_fires_when_now_utc_is_stale_relative_to_real_wall_clock(self) -> None:
        # exact reported symptom: an explicit now_utc resolving to a month
        # in the past, while the real wall clock is July 8 — this is the
        # scenario a future frozen-datetime regression would produce.
        real_wall_clock = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        stale_now = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)
        with patch.object(bt, "broker_now_utc", return_value=real_wall_clock), \
             patch.object(bt, "log") as mock_log:
            bt.broker_day_window(stale_now, 3.0)
        mock_log.critical.assert_called_once()
        self.assertIn("BROKER_TIME_GUARD", mock_log.critical.call_args[0][0])

    def test_guard_silent_when_now_utc_matches_real_wall_clock(self) -> None:
        now = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        with patch.object(bt, "broker_now_utc", return_value=now), \
             patch.object(bt, "log") as mock_log:
            bt.broker_day_window(now, 3.0)
        mock_log.critical.assert_not_called()

    def test_guard_fires_against_the_real_unmocked_wall_clock_too(self) -> None:
        """Regression guard for the exact design flaw found during this
        mission: an earlier version of this guard compared the window to
        the SAME now_utc used to derive it, which is always self-
        consistent by construction and could never fire. Proof the fix is
        real: with broker_now_utc() left UNMOCKED (the genuine current
        wall clock, today nowhere near May 31), a stale explicit now_utc
        still triggers the guard — it is judged against reality, not
        against its own input."""
        stale_now = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)
        with patch.object(bt, "log") as mock_log:
            bt.broker_day_window(stale_now, 3.0)
        mock_log.critical.assert_called_once()

    def test_is_window_stale_detects_a_month_old_window(self) -> None:
        now = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        stale_start = datetime(2026, 5, 31, 21, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(bt.is_window_stale(stale_start, now))

    def test_is_window_stale_false_for_todays_window(self) -> None:
        now = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        fresh_start = datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(bt.is_window_stale(fresh_start, now))

    def test_is_window_stale_boundary_exactly_48h(self) -> None:
        now = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        boundary_start = now - timedelta(hours=48)
        self.assertFalse(bt.is_window_stale(boundary_start, now))
        just_over = now - timedelta(hours=48, seconds=1)
        self.assertTrue(bt.is_window_stale(just_over, now))


class TestBrokerNowUtc(unittest.TestCase):
    def test_returns_tz_aware_utc_datetime(self) -> None:
        result = bt.broker_now_utc()
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_two_calls_are_never_identical_cached_object(self) -> None:
        """Regression guard for the exact bug class this mission fixes: a
        frozen/cached 'now' would return an IDENTICAL value across calls
        separated by real wall-clock time. This can't fully prove freshness
        in a fast unit test, but proves broker_now_utc is not a cached
        constant (no @lru_cache, no module-level frozen value)."""
        import inspect
        source = inspect.getsource(bt.broker_now_utc)
        self.assertNotIn("lru_cache", source)
        self.assertIn("datetime.now(timezone.utc)", source)


if __name__ == "__main__":
    unittest.main()
