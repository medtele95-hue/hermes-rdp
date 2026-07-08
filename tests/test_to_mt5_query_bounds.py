# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_PNL.md — to_mt5_query_bounds(). Root cause of the
P&L contradiction: MT5 stamps deal.time in the broker's own wall clock
(UTC+3 for XM) and ignores tzinfo on query bounds, comparing raw clock
fields directly. A bare .replace(tzinfo=None) on a TRUE-UTC boundary
queries 3 real hours too early. Verified live 2026-07-08: tick.time read
16:47:30 while genuine system UTC was 13:47:29 (+3h, exact)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.utils.broker_time import to_mt5_query_bounds


class TestToMt5QueryBounds(unittest.TestCase):
    def test_shifts_forward_by_offset_and_strips_tzinfo(self) -> None:
        start = datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
        q_start, q_end = to_mt5_query_bounds(start, end, 3.0)
        self.assertEqual(q_start, datetime(2026, 7, 8, 0, 0, 0))
        self.assertEqual(q_end, datetime(2026, 7, 8, 16, 46, 28))
        self.assertIsNone(q_start.tzinfo)
        self.assertIsNone(q_end.tzinfo)

    def test_naive_input_treated_as_utc(self) -> None:
        start = datetime(2026, 7, 7, 21, 0, 0)
        end = datetime(2026, 7, 8, 13, 0, 0)
        q_start, q_end = to_mt5_query_bounds(start, end, 3.0)
        self.assertEqual(q_start, datetime(2026, 7, 8, 0, 0, 0))
        self.assertEqual(q_end, datetime(2026, 7, 8, 16, 0, 0))

    def test_zero_offset_is_a_pure_tzinfo_strip(self) -> None:
        start = datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 8, 13, 0, 0, tzinfo=timezone.utc)
        q_start, q_end = to_mt5_query_bounds(start, end, 0.0)
        self.assertEqual(q_start, datetime(2026, 7, 7, 21, 0, 0))
        self.assertEqual(q_end, datetime(2026, 7, 8, 13, 0, 0))

    def test_reproduces_the_exact_reported_symptom(self) -> None:
        """The old buggy behaviour (start_utc.replace(tzinfo=None), no
        shift) queried from clock-value "July7 21:00" — 3h too early,
        pulling in the tail of the PRIOR broker day. The fix must query
        from "July8 00:00" instead — the true broker midnight in the
        clock-value terms MT5 itself uses for deal.time."""
        broker_midnight_true_utc = datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc)
        now_true_utc = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
        q_start, _ = to_mt5_query_bounds(broker_midnight_true_utc, now_true_utc, 3.0)
        old_buggy_start = broker_midnight_true_utc.replace(tzinfo=None)
        self.assertNotEqual(q_start, old_buggy_start)
        self.assertEqual(q_start, datetime(2026, 7, 8, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
