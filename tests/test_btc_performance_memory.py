"""Tests for app/mt5/btc_performance_memory.py — BTC Adaptive Confidence and Defensive Mode.

Covers:
- Win streak tracking and CONFIDENT mode
- 3 positive exits enables confident mode
- 4 positive exits keeps lot same / risk same
- 3 losses → 30-min pause
- 4 losses → 60-min pause, A+ required
- Pause blocks new entries, smart exit continues
- Self-diagnosis: repeated patterns create temp block rules
- Temp block expiry
- Summary structure and accuracy
"""
from __future__ import annotations

import time
import unittest

from app.mt5.btc_performance_memory import (
    BtcPerformanceMemory,
    _BTC_SCALPING,
    _ORDER_FLOW,
    _WIN_STREAK_CONFIDENT,
    _WIN_STREAK_VERY_CONFIDENT,
    _LOSS_STREAK_DEFENSIVE,
    _LOSS_STREAK_FULL_DEFENSIVE,
    _PAUSE_MINUTES_DEFENSIVE,
    _PAUSE_MINUTES_FULL_DEFENSIVE,
)


def _win(strategy: str = _BTC_SCALPING, profit: float = 1.5) -> dict:
    return {"profit": profit, "strategy": strategy}


def _loss(strategy: str = _BTC_SCALPING, profit: float = -1.0, **flags) -> dict:
    t = {"profit": profit, "strategy": strategy}
    t.update(flags)
    return t


class TestWinStreakMode(unittest.TestCase):

    def setUp(self):
        self.mem = BtcPerformanceMemory()

    def test_initial_mode_is_normal(self):
        self.assertEqual(self.mem.get_adaptive_mode(), "NORMAL")

    def test_win_streak_0(self):
        self.assertEqual(self.mem.get_win_streak(), 0)

    def test_win_streak_counts_consecutive_wins(self):
        self.mem.record_trade(_win())
        self.mem.record_trade(_win())
        self.assertEqual(self.mem.get_win_streak(), 2)

    def test_win_streak_resets_on_loss(self):
        self.mem.record_trade(_win())
        self.mem.record_trade(_win())
        self.mem.record_trade(_loss())
        self.assertEqual(self.mem.get_win_streak(), 0)

    def test_3_wins_enables_confident_mode(self):
        for _ in range(_WIN_STREAK_CONFIDENT):
            self.mem.record_trade(_win())
        self.assertEqual(self.mem.get_adaptive_mode(), "CONFIDENT")

    def test_4_wins_stays_confident_not_increase_lot(self):
        # 4 wins → still CONFIDENT; no lot or risk increase
        for _ in range(_WIN_STREAK_VERY_CONFIDENT):
            self.mem.record_trade(_win())
        self.assertEqual(self.mem.get_adaptive_mode(), "CONFIDENT")
        # There is no mechanism in BtcPerformanceMemory to change lot — it's analysis only
        # Lot is controlled by settings (DEMO_MAX_LOT=0.01) and never touched here
        summary = self.mem.get_performance_summary()
        self.assertEqual(summary["win_streak"], _WIN_STREAK_VERY_CONFIDENT)

    def test_win_after_loss_streak_resets_loss_streak(self):
        self.mem.record_trade(_loss())
        self.mem.record_trade(_loss())
        self.mem.record_trade(_win())
        self.assertEqual(self.mem.get_loss_streak(), 0)


class TestLossStreakPause(unittest.TestCase):

    def setUp(self):
        self.mem = BtcPerformanceMemory()

    def test_3_losses_pauses_30_minutes(self):
        for _ in range(_LOSS_STREAK_DEFENSIVE):
            self.mem.record_trade(_loss())
        self.assertTrue(self.mem.is_entry_paused())
        remaining = self.mem.get_pause_remaining_minutes()
        # Allow 5s tolerance
        self.assertAlmostEqual(remaining, _PAUSE_MINUTES_DEFENSIVE, delta=0.1)

    def test_4_losses_pauses_60_minutes(self):
        for _ in range(_LOSS_STREAK_FULL_DEFENSIVE):
            self.mem.record_trade(_loss())
        self.assertTrue(self.mem.is_entry_paused())
        remaining = self.mem.get_pause_remaining_minutes()
        self.assertAlmostEqual(remaining, _PAUSE_MINUTES_FULL_DEFENSIVE, delta=0.1)

    def test_3_losses_mode_is_paused(self):
        for _ in range(_LOSS_STREAK_DEFENSIVE):
            self.mem.record_trade(_loss())
        self.assertEqual(self.mem.get_adaptive_mode(), "PAUSED")

    def test_4_losses_requires_aplus_after_pause(self):
        for _ in range(_LOSS_STREAK_FULL_DEFENSIVE):
            self.mem.record_trade(_loss())
        # While paused, requires_aplus is False (entries blocked anyway)
        self.assertFalse(self.mem.requires_aplus())
        # Simulate pause expiry
        self.mem._pause_until = time.time() - 1
        self.assertTrue(self.mem.requires_aplus())

    def test_during_pause_is_entry_paused_true(self):
        for _ in range(_LOSS_STREAK_DEFENSIVE):
            self.mem.record_trade(_loss())
        self.assertTrue(self.mem.is_entry_paused())

    def test_2_losses_does_not_pause(self):
        for _ in range(2):
            self.mem.record_trade(_loss())
        self.assertFalse(self.mem.is_entry_paused())

    def test_pause_mode_returns_paused_string(self):
        for _ in range(_LOSS_STREAK_DEFENSIVE):
            self.mem.record_trade(_loss())
        self.assertEqual(self.mem.get_adaptive_mode(), "PAUSED")

    def test_pause_expires(self):
        for _ in range(_LOSS_STREAK_DEFENSIVE):
            self.mem.record_trade(_loss())
        # Force-expire the pause
        self.mem._pause_until = time.time() - 1
        self.assertFalse(self.mem.is_entry_paused())


class TestSelfDiagnosis(unittest.TestCase):

    def setUp(self):
        self.mem = BtcPerformanceMemory()

    def test_single_late_entry_does_not_block(self):
        self.mem.record_trade(_loss(late_entry=True))
        self.assertFalse(self.mem.is_temp_blocked("late_entry"))

    def test_two_late_entries_create_temp_block(self):
        self.mem.record_trade(_loss(late_entry=True))
        self.mem.record_trade(_loss(late_entry=True))
        self.assertTrue(self.mem.is_temp_blocked("late_entry"))

    def test_two_against_vwap_create_temp_block(self):
        self.mem.record_trade(_loss(against_vwap=True))
        self.mem.record_trade(_loss(against_vwap=True))
        self.assertTrue(self.mem.is_temp_blocked("against_vwap"))

    def test_two_high_spread_create_temp_block(self):
        self.mem.record_trade(_loss(high_spread=True))
        self.mem.record_trade(_loss(high_spread=True))
        self.assertTrue(self.mem.is_temp_blocked("high_spread"))

    def test_two_against_poc_create_temp_block(self):
        self.mem.record_trade(_loss(against_poc=True))
        self.mem.record_trade(_loss(against_poc=True))
        self.assertTrue(self.mem.is_temp_blocked("against_poc"))

    def test_pattern_block_appears_in_summary(self):
        self.mem.record_trade(_loss(late_entry=True))
        self.mem.record_trade(_loss(late_entry=True))
        summary = self.mem.get_performance_summary()
        self.assertIn("late_entry", summary["temp_block_rules"])

    def test_winning_trades_dont_create_blocks(self):
        for _ in range(5):
            self.mem.record_trade(_win())
        self.assertEqual(list(self.mem.get_temp_block_rules().keys()), [])

    def test_temp_block_expires(self):
        self.mem.record_trade(_loss(late_entry=True))
        self.mem.record_trade(_loss(late_entry=True))
        # Force expiry
        self.mem._temp_block_rules["late_entry"] = time.time() - 1
        self.assertFalse(self.mem.is_temp_blocked("late_entry"))


class TestPerformanceSummary(unittest.TestCase):

    def setUp(self):
        self.mem = BtcPerformanceMemory()

    def test_empty_summary_structure(self):
        s = self.mem.get_performance_summary()
        self.assertIn("last_5_results", s)
        self.assertIn("win_streak", s)
        self.assertIn("loss_streak", s)
        self.assertIn("positive_exit_rate", s)
        self.assertIn("average_profit", s)
        self.assertIn("average_loss", s)
        self.assertIn("strategy_win_rate", s)
        self.assertIn("best_strategy_now", s)
        self.assertIn("worst_strategy_now", s)
        self.assertIn("adaptive_mode", s)
        self.assertIn("is_paused", s)
        self.assertIn("temp_block_rules", s)
        self.assertIn("total_trades", s)

    def test_summary_win_rate_accuracy(self):
        self.mem.record_trade(_win())
        self.mem.record_trade(_win())
        self.mem.record_trade(_loss())
        s = self.mem.get_performance_summary()
        self.assertAlmostEqual(s["positive_exit_rate"], 2/3, places=2)

    def test_summary_tracks_strategy_win_rates(self):
        self.mem.record_trade(_win(_BTC_SCALPING))
        self.mem.record_trade(_win(_BTC_SCALPING))
        self.mem.record_trade(_loss(_ORDER_FLOW))
        s = self.mem.get_performance_summary()
        wr = s["strategy_win_rate"]
        self.assertAlmostEqual(wr[_BTC_SCALPING], 1.0)
        self.assertAlmostEqual(wr[_ORDER_FLOW], 0.0)

    def test_summary_best_strategy(self):
        self.mem.record_trade(_win(_BTC_SCALPING))
        self.mem.record_trade(_win(_BTC_SCALPING))
        self.mem.record_trade(_loss(_ORDER_FLOW))
        s = self.mem.get_performance_summary()
        self.assertEqual(s["best_strategy_now"], _BTC_SCALPING)
        self.assertEqual(s["worst_strategy_now"], _ORDER_FLOW)

    def test_summary_last_5_results(self):
        for i in range(7):
            self.mem.record_trade({"profit": float(i), "strategy": _BTC_SCALPING})
        s = self.mem.get_performance_summary()
        self.assertEqual(len(s["last_5_results"]), 5)
        self.assertEqual(s["last_5_results"][-1], 6.0)

    def test_total_trades_count(self):
        for _ in range(10):
            self.mem.record_trade(_win())
        s = self.mem.get_performance_summary()
        self.assertEqual(s["total_trades"], 10)

    def test_average_profit_and_loss(self):
        self.mem.record_trade({"profit": 2.0, "strategy": _BTC_SCALPING})
        self.mem.record_trade({"profit": 4.0, "strategy": _BTC_SCALPING})
        self.mem.record_trade({"profit": -1.0, "strategy": _BTC_SCALPING})
        s = self.mem.get_performance_summary()
        self.assertAlmostEqual(s["average_profit"], 3.0)
        self.assertAlmostEqual(s["average_loss"], -1.0)


class TestSingleton(unittest.TestCase):

    def test_get_btc_performance_memory_singleton(self):
        from app.mt5.btc_performance_memory import get_btc_performance_memory
        a = get_btc_performance_memory()
        b = get_btc_performance_memory()
        self.assertIs(a, b)


if __name__ == "__main__":
    unittest.main()
