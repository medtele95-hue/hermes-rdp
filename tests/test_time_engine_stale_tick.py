# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_DATE.md — TimeEngine._broker_time_estimate must
never trust a stale tick/candle timestamp (e.g. an MT5 reconnect handing
back a cached object from hours before) over the fresh wall clock."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.services import time_engine as te


class TestBrokerTimeEstimateFreshTick(unittest.TestCase):
    def test_fresh_tick_time_is_used(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        tick_dt = utc_dt - timedelta(minutes=1)
        tick = {"time": tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertAlmostEqual((result - tick_dt).total_seconds(), 0, delta=2)


class TestBrokerTimeEstimateStaleTick(unittest.TestCase):
    def test_stale_tick_beyond_threshold_falls_back_to_wall_clock(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        stale_tick_dt = utc_dt - timedelta(hours=20)  # matches the reported ~17-24h drift
        tick = {"time": stale_tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertEqual(result, utc_dt)

    def test_month_old_tick_falls_back_to_wall_clock(self) -> None:
        """The exact reported symptom: a candidate resolving to a month in
        the past must never be trusted."""
        utc_dt = datetime(2026, 7, 8, 11, 34, 0, tzinfo=timezone.utc)
        ancient_tick_dt = datetime(2026, 5, 31, 21, 0, 0, tzinfo=timezone.utc)
        tick = {"time": ancient_tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertEqual(result, utc_dt)

    def test_stale_tick_falls_through_to_fresh_frame(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        stale_tick_dt = utc_dt - timedelta(hours=20)
        tick = {"time": stale_tick_dt.timestamp()}
        fresh_frame_dt = utc_dt - timedelta(minutes=5)
        frame = pd.DataFrame({"time": [fresh_frame_dt]})
        result = te._broker_time_estimate({"M1": frame}, tick, utc_dt)
        self.assertAlmostEqual((result - fresh_frame_dt).total_seconds(), 0, delta=2)

    def test_stale_tick_and_stale_frame_both_fall_back_to_wall_clock(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        stale_dt = utc_dt - timedelta(hours=20)
        tick = {"time": stale_dt.timestamp()}
        frame = pd.DataFrame({"time": [stale_dt]})
        result = te._broker_time_estimate({"M1": frame}, tick, utc_dt)
        self.assertEqual(result, utc_dt)

    def test_no_tick_no_frames_uses_wall_clock(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        result = te._broker_time_estimate({}, None, utc_dt)
        self.assertEqual(result, utc_dt)

    def test_future_tick_beyond_threshold_also_rejected(self) -> None:
        """Staleness check is symmetric — a tick implausibly far in the
        FUTURE is just as suspicious as one far in the past."""
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        future_tick_dt = utc_dt + timedelta(hours=20)
        tick = {"time": future_tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertEqual(result, utc_dt)

    def test_boundary_just_under_threshold_is_trusted(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        tick_dt = utc_dt - timedelta(hours=5, minutes=59)
        tick = {"time": tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertAlmostEqual((result - tick_dt).total_seconds(), 0, delta=2)

    def test_boundary_just_over_threshold_is_rejected(self) -> None:
        utc_dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        tick_dt = utc_dt - timedelta(hours=6, minutes=1)
        tick = {"time": tick_dt.timestamp()}
        result = te._broker_time_estimate({}, tick, utc_dt)
        self.assertEqual(result, utc_dt)


if __name__ == "__main__":
    unittest.main()
