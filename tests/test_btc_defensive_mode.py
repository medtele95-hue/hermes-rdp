"""Tests for BTC defensive mode and smart exit continuation.

Covers:
- 3 losses → 30-min pause, entries blocked
- 4 losses → 60-min pause, A+ required after
- During pause: new entries blocked
- During pause: smart exit logic continues (is_entry_paused doesn't block exit)
- Pause expiry restores normal mode
- Safety invariants: lot unchanged, live trading disabled
"""
from __future__ import annotations

import time
import unittest

from app.mt5.btc_performance_memory import (
    BtcPerformanceMemory,
    _PAUSE_MINUTES_DEFENSIVE,
    _PAUSE_MINUTES_FULL_DEFENSIVE,
)
from app.mt5.btc_setup_intelligence import evaluate_btc_setup_intelligence


def _loss() -> dict:
    return {"profit": -1.5, "strategy": "BTC_SCALPING_AGENT"}


def _win() -> dict:
    return {"profit": 2.0, "strategy": "BTC_SCALPING_AGENT"}


def _strong_ctx() -> dict:
    return {
        "price": 65200.0, "bid": 65200.0,
        "vwap": 65000.0, "poc": 64900.0,
        "cvd_slope": 0.6, "delta_proxy": 800.0,
        "order_flow_signal": "BUY",
        "m1_momentum": "BULLISH", "m5_momentum": "BULLISH",
        "spread": 30.0, "max_spread": 100.0,
        "volatility_status": "NORMAL", "session": "LONDON_NY",
    }


def _good_cand() -> dict:
    return {"smc_score": 80.0, "mtfa_score": 75.0, "rr": 2.0}


class TestDefensiveModePauseEntries(unittest.TestCase):

    def setUp(self):
        self.mem = BtcPerformanceMemory()

    def test_3_losses_pause_blocks_new_entry(self):
        for _ in range(3):
            self.mem.record_trade(_loss())
        self.assertTrue(self.mem.is_entry_paused())
        perf = self.mem.get_performance_summary()
        self.assertTrue(perf["is_paused"])

    def test_3_losses_intelligence_decision_is_block(self):
        for _ in range(3):
            self.mem.record_trade(_loss())
        perf = self.mem.get_performance_summary()
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_ctx(), {}, perf,
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["entry_mode"], "PAUSED")

    def test_4_losses_pause_longer(self):
        for _ in range(4):
            self.mem.record_trade(_loss())
        remaining = self.mem.get_pause_remaining_minutes()
        self.assertAlmostEqual(remaining, _PAUSE_MINUTES_FULL_DEFENSIVE, delta=0.1)

    def test_3_losses_pause_30_minutes(self):
        for _ in range(3):
            self.mem.record_trade(_loss())
        remaining = self.mem.get_pause_remaining_minutes()
        self.assertAlmostEqual(remaining, _PAUSE_MINUTES_DEFENSIVE, delta=0.1)

    def test_4_losses_requires_aplus_after_pause(self):
        for _ in range(4):
            self.mem.record_trade(_loss())
        # While paused: no entries at all, requires_aplus is False
        self.assertFalse(self.mem.requires_aplus())
        # After pause expires:
        self.mem._pause_until = time.time() - 1
        self.assertTrue(self.mem.requires_aplus())

    def test_during_pause_smart_exit_not_blocked(self):
        """is_entry_paused() only blocks entry, not exit management."""
        for _ in range(3):
            self.mem.record_trade(_loss())
        self.assertTrue(self.mem.is_entry_paused())
        # Exit evaluation is independent — it doesn't check is_entry_paused
        # This test verifies the function doesn't raise and returns useful data
        from app.mt5.btc_exit_danger import evaluate_btc_exit_danger
        from types import SimpleNamespace
        pos = SimpleNamespace(type=0, profit=0.5, price_open=65000.0)
        ctx = _strong_ctx()
        exit_result = evaluate_btc_exit_danger(pos, ctx)
        self.assertIn("danger", exit_result)
        # Smart exit still runs regardless of entry pause
        self.assertIsInstance(exit_result["danger"], bool)

    def test_pause_expires_mode_returns_normal_or_defensive(self):
        for _ in range(3):
            self.mem.record_trade(_loss())
        self.mem._pause_until = time.time() - 1
        # After pause, 3-loss streak still → DEFENSIVE (not PAUSED)
        mode = self.mem.get_adaptive_mode()
        self.assertIn(mode, ("DEFENSIVE", "NORMAL"))

    def test_2_losses_does_not_pause(self):
        for _ in range(2):
            self.mem.record_trade(_loss())
        self.assertFalse(self.mem.is_entry_paused())


class TestSmartExitContinuesDuringPause(unittest.TestCase):
    """Verify smart exit and fast exit functions are independent of entry pause."""

    def test_btc_exit_danger_not_affected_by_pause(self):
        from app.mt5.btc_exit_danger import evaluate_btc_exit_danger
        from types import SimpleNamespace
        mem = BtcPerformanceMemory()
        for _ in range(4):
            mem.record_trade(_loss())
        # Entries are paused
        self.assertTrue(mem.is_entry_paused())
        # But exit danger evaluation still works
        pos = SimpleNamespace(type=1, profit=0.8, price_open=65500.0)
        ctx = {
            "price": 65400.0, "bid": 65400.0, "vwap": 65500.0,
            "cvd_slope": 0.5, "m1_momentum": "BULLISH",
        }
        result = evaluate_btc_exit_danger(pos, ctx)
        self.assertIn("danger", result)
        # Doesn't throw; returns a result
        self.assertIsInstance(result["signal_count"], int)

    def test_performance_memory_does_not_call_order_send(self):
        """BtcPerformanceMemory must not call mt5.order_send."""
        import inspect
        import app.mt5.btc_performance_memory as mod
        src = inspect.getsource(mod)
        self.assertNotIn("order_send", src)

    def test_intelligence_engine_does_not_call_order_send(self):
        import inspect
        import app.mt5.btc_setup_intelligence as mod
        src = inspect.getsource(mod)
        self.assertNotIn("order_send", src)


class TestSafetyInvariants(unittest.TestCase):

    def test_no_lot_change_in_performance_memory(self):
        """BtcPerformanceMemory never modifies lot size — it only tracks P&L."""
        import inspect
        import app.mt5.btc_performance_memory as mod
        src = inspect.getsource(mod)
        # Never sets lot_size or volume — those belong to demo_router only
        self.assertNotIn("lot_size", src)
        self.assertNotIn("volume", src.lower())

    def test_no_live_trading_in_performance_memory(self):
        import inspect
        import app.mt5.btc_performance_memory as mod
        src = inspect.getsource(mod)
        self.assertNotIn("allow_live_trading", src)

    def test_intelligence_engine_no_demo_router_import(self):
        """Intelligence engine must not import from demo_router."""
        import inspect
        import app.mt5.btc_setup_intelligence as mod
        src = inspect.getsource(mod)
        # Mentioning demo_router in a docstring is OK; importing it is not
        self.assertNotIn("from app.mt5.demo_router", src)
        self.assertNotIn("import demo_router", src)

    def test_intelligence_engine_no_mt5_import(self):
        import inspect
        import app.mt5.btc_setup_intelligence as mod
        src = inspect.getsource(mod)
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("import mt5", src)

    def test_performance_memory_no_mt5_import(self):
        import inspect
        import app.mt5.btc_performance_memory as mod
        src = inspect.getsource(mod)
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("import mt5", src)

    def test_live_trading_remains_false(self):
        """Safety invariant: ALLOW_LIVE_TRADING must remain False."""
        import re
        from pathlib import Path
        demo_router = Path("app/mt5/demo_router.py").read_text(encoding="utf-8")
        # Should not have any unconditional allow_live_trading = True
        self.assertNotIn("allow_live_trading = True", demo_router)
        self.assertNotIn("allow_live_trading=True", demo_router)

    def test_lot_unchanged_in_intelligence(self):
        """Intelligence engine result never contains lot field."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_ctx(), {},
            {"adaptive_mode": "CONFIDENT", "is_paused": False, "win_streak": 4,
             "loss_streak": 0, "requires_aplus": False},
        )
        # Even with win_streak=4, no lot_size in result
        self.assertNotIn("lot_size", result)
        self.assertNotIn("lot", result)


if __name__ == "__main__":
    unittest.main()
