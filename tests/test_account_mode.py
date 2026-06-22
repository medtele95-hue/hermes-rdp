"""Tests for app/mt5/account_mode.py — centralized MT5 account mode detection.

Covers:
- trade_mode=0 (DEMO) correctly detected as DEMO — the root bug fix
- trade_mode=1 (CONTEST) correctly blocked
- trade_mode=2 (LIVE/REAL) correctly blocked
- None account_info returns False (MT5 disconnected)
- Missing trade_mode attribute returns False
- trade_mode=None returns False
- String "0" cast to int → DEMO (True)
- Non-castable trade_mode returns False
- BtcFastExitDaemon._is_demo_account() uses is_mt5_demo_account correctly
- Old buggy pattern (int(x or -1) with x=0) would have returned -1 ≠ 0 → documenting the fix
- BUY position type=0 is not confused with DEMO mode detection
- [MT5_ACCOUNT_MODE_DIAG] log token present in source
- [OLD_BTC_FAST_EXIT_ACCOUNT_CHECK] log token present in daemon source
- No order_send calls added in account_mode module
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.mt5.account_mode import is_mt5_demo_account
from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(trade_mode=None, **kwargs) -> SimpleNamespace:
    ns = SimpleNamespace(**kwargs)
    if trade_mode is not None:
        ns.trade_mode = trade_mode
    return ns


def _settings(**kwargs) -> SimpleNamespace:
    defaults = {
        "old_btc_fast_exit_daemon_enabled": True,
        "old_btc_fast_exit_interval_ms": 250,
        "old_btc_fast_exit_min_profit_usd": 0.03,
        "old_btc_fast_exit_hard_min_profit_usd": 0.01,
        "old_btc_fast_exit_close_at_any_positive": True,
        "demo_magic_number": 909002,
        "allow_live_trading": False,
        "demo_only": True,
        "btc_exit_arbiter_enabled": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Core is_mt5_demo_account tests
# ---------------------------------------------------------------------------

class TestIsMt5DemoAccount(unittest.TestCase):

    def test_trade_mode_0_is_demo(self):
        """THE ROOT BUG FIX: trade_mode=0 must return True, not False."""
        acc = _account(trade_mode=0)
        self.assertTrue(is_mt5_demo_account(acc))

    def test_trade_mode_1_is_not_demo(self):
        """CONTEST (trade_mode=1) must not be treated as DEMO."""
        acc = _account(trade_mode=1)
        self.assertFalse(is_mt5_demo_account(acc))

    def test_trade_mode_2_is_not_demo(self):
        """LIVE/REAL (trade_mode=2) must not be treated as DEMO."""
        acc = _account(trade_mode=2)
        self.assertFalse(is_mt5_demo_account(acc))

    def test_none_account_info_returns_false(self):
        """MT5 disconnected → account_info() returns None → must block."""
        self.assertFalse(is_mt5_demo_account(None))

    def test_missing_trade_mode_attribute_returns_false(self):
        """Account object with no trade_mode field → must block."""
        acc = SimpleNamespace(login=12345, balance=1000.0)
        self.assertFalse(is_mt5_demo_account(acc))

    def test_trade_mode_none_returns_false(self):
        """Explicitly trade_mode=None (attribute exists but is None) → must block."""
        acc = SimpleNamespace(trade_mode=None)
        self.assertFalse(is_mt5_demo_account(acc))

    def test_trade_mode_string_zero_is_demo(self):
        """String "0" cast to int 0 → DEMO (True)."""
        acc = SimpleNamespace(trade_mode="0")
        self.assertTrue(is_mt5_demo_account(acc))

    def test_trade_mode_string_two_is_not_demo(self):
        """String "2" cast to int 2 → LIVE (False)."""
        acc = SimpleNamespace(trade_mode="2")
        self.assertFalse(is_mt5_demo_account(acc))

    def test_non_castable_trade_mode_returns_false(self):
        """Non-numeric string → TypeError in int() → must return False."""
        acc = SimpleNamespace(trade_mode="DEMO")
        self.assertFalse(is_mt5_demo_account(acc))

    def test_float_zero_is_demo(self):
        """0.0 cast to int → 0 → DEMO."""
        acc = SimpleNamespace(trade_mode=0.0)
        self.assertTrue(is_mt5_demo_account(acc))

    def test_float_two_is_not_demo(self):
        """2.0 cast to int → 2 → LIVE."""
        acc = SimpleNamespace(trade_mode=2.0)
        self.assertFalse(is_mt5_demo_account(acc))


# ---------------------------------------------------------------------------
# Documenting the old bug pattern
# ---------------------------------------------------------------------------

class TestOldBugDocumentation(unittest.TestCase):

    def test_old_or_pattern_would_fail_for_trade_mode_0(self):
        """Documents the root cause: `0 or -1` = -1, which != 0 (DEMO value).

        The old code: int(getattr(info, "trade_mode", -1) or -1) == 1
        With trade_mode=0:  0 or -1  →  -1  ≠  1  →  False  (WRONG: skips DEMO)
        The new code: is_mt5_demo_account checks int(trade_mode) == 0 directly.
        """
        trade_mode = 0
        old_result = int(trade_mode or -1)  # the old buggy pattern
        self.assertEqual(old_result, -1, "Python: 0 or -1 = -1 (falsy trap)")
        self.assertNotEqual(old_result, 0, "Old pattern never equals DEMO value 0")

        acc = SimpleNamespace(trade_mode=0)
        self.assertTrue(is_mt5_demo_account(acc), "New function correctly returns True for DEMO=0")

    def test_old_wrong_constant_would_fail(self):
        """Documents the second bug: _MT5_TRADE_MODE_DEMO = 1 was wrong.

        DEMO is mode 0, CONTEST is mode 1.  Using 1 as the DEMO constant
        meant CONTEST accounts could pass and DEMO accounts would be blocked.
        """
        _OLD_CONSTANT = 1
        acc_demo = SimpleNamespace(trade_mode=0)
        acc_contest = SimpleNamespace(trade_mode=1)

        # Old wrong constant logic
        old_demo_check = int(getattr(acc_demo, "trade_mode", -1)) == _OLD_CONSTANT
        old_contest_check = int(getattr(acc_contest, "trade_mode", -1)) == _OLD_CONSTANT

        self.assertFalse(old_demo_check, "Old code: DEMO account (mode=0) would fail check")
        self.assertTrue(old_contest_check, "Old code: CONTEST account (mode=1) would pass as 'DEMO'")

        # New correct function
        self.assertTrue(is_mt5_demo_account(acc_demo))
        self.assertFalse(is_mt5_demo_account(acc_contest))


# ---------------------------------------------------------------------------
# BtcFastExitDaemon._is_demo_account integration
# ---------------------------------------------------------------------------

class TestDaemonIsDemoAccount(unittest.TestCase):

    def _make_daemon(self) -> BtcFastExitDaemon:
        return BtcFastExitDaemon(_settings(), lambda pos, reason: {"status": "ORDER_CONFIRMED"})

    def test_is_demo_account_true_for_trade_mode_0(self):
        """Daemon correctly identifies DEMO (trade_mode=0) as demo."""
        d = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai:
            mock_ai.return_value = SimpleNamespace(trade_mode=0)
            self.assertTrue(d._is_demo_account())

    def test_is_demo_account_false_for_trade_mode_2(self):
        """Daemon rejects LIVE account (trade_mode=2)."""
        d = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai:
            mock_ai.return_value = SimpleNamespace(trade_mode=2)
            self.assertFalse(d._is_demo_account())

    def test_is_demo_account_false_for_trade_mode_1(self):
        """Daemon rejects CONTEST account (trade_mode=1)."""
        d = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai:
            mock_ai.return_value = SimpleNamespace(trade_mode=1)
            self.assertFalse(d._is_demo_account())

    def test_is_demo_account_false_when_account_info_none(self):
        """Daemon rejects None account_info (MT5 disconnected)."""
        d = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai:
            mock_ai.return_value = None
            self.assertFalse(d._is_demo_account())

    def test_tick_closes_on_demo_trade_mode_0(self):
        """End-to-end: daemon tick with trade_mode=0 allows close."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}

        d = self._make_daemon()
        d._close_fn = capture

        pos = SimpleNamespace(
            ticket=5001, profit=0.05, magic=909002, symbol="BTCUSD#",
            comment="HERMES_BTC", type=0, volume=0.01, price_open=65000.0,
        )
        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            mock_ai.return_value = SimpleNamespace(trade_mode=0)  # DEMO = 0
            mock_pos.return_value = [pos]
            d._tick()

        self.assertEqual(len(closed), 1, "Should close on DEMO (trade_mode=0)")

    def test_tick_blocks_on_live_trade_mode_2(self):
        """End-to-end: daemon tick with trade_mode=2 (LIVE) must not close."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}

        d = self._make_daemon()
        d._close_fn = capture

        pos = SimpleNamespace(
            ticket=5002, profit=0.10, magic=909002, symbol="BTCUSD#",
            comment="HERMES_BTC", type=0, volume=0.01, price_open=65000.0,
        )
        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            mock_ai.return_value = SimpleNamespace(trade_mode=2)  # LIVE = 2
            mock_pos.return_value = [pos]
            d._tick()

        self.assertEqual(len(closed), 0, "Must not close on LIVE account")


# ---------------------------------------------------------------------------
# BUY position type=0 — not confused with DEMO trade_mode=0
# ---------------------------------------------------------------------------

class TestBuyPositionTypeNotConfusedWithDemoMode(unittest.TestCase):

    def test_buy_position_type_0_is_not_demo_check(self):
        """A BUY position has type=0 (POSITION_TYPE_BUY).
        is_mt5_demo_account should NOT be called on position objects.
        Positions have no trade_mode — correctly returns False when called by mistake.
        """
        buy_pos = SimpleNamespace(
            ticket=9001, type=0, symbol="BTCUSD#", magic=909002,
            profit=0.05, volume=0.01,
        )
        # Position object has no trade_mode → is_mt5_demo_account returns False
        # (not that we'd ever call it on a position — this just shows no false positive)
        self.assertFalse(is_mt5_demo_account(buy_pos))

    def test_buy_position_type_0_correctly_identified_as_buy(self):
        """Verifies POSITION_TYPE_BUY (0) comparison doesn't suffer the same truthiness trap."""
        import MetaTrader5 as mt5
        position_type_buy = getattr(mt5, "POSITION_TYPE_BUY", 0)
        pos_type = 0  # BUY position type
        is_buy = pos_type == position_type_buy
        self.assertTrue(is_buy, "pos_type=0 must be identified as BUY (POSITION_TYPE_BUY=0)")


# ---------------------------------------------------------------------------
# Source-level invariants
# ---------------------------------------------------------------------------

class TestAccountModeSourceInvariants(unittest.TestCase):

    def _src(self, module_path: str) -> str:
        import importlib
        import inspect
        mod = importlib.import_module(module_path)
        return inspect.getsource(mod)

    def test_mt5_account_mode_diag_token_in_account_mode(self):
        src = self._src("app.mt5.account_mode")
        self.assertIn("[MT5_ACCOUNT_MODE_DIAG]", src)

    def test_no_order_send_in_account_mode(self):
        src = self._src("app.mt5.account_mode")
        self.assertNotIn("mt5.order_send", src)
        self.assertNotIn("order_send(", src)

    def test_no_or_minus_one_code_pattern_in_account_mode(self):
        """The `or -1)` truthiness trap must not appear as executable code."""
        import ast, textwrap
        import importlib, inspect
        mod = importlib.import_module("app.mt5.account_mode")
        tree = ast.parse(inspect.getsource(mod))
        # Walk all BoolOp nodes — there must be no `x or -1` pattern
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                for val in node.values:
                    if isinstance(val, ast.UnaryOp) and isinstance(val.op, ast.USub):
                        if isinstance(val.operand, ast.Constant) and val.operand.value == 1:
                            self.fail("Found `or -1` BoolOp in account_mode.py at line %s" % node.col_offset)

    def test_old_btc_fast_exit_account_check_token_in_daemon(self):
        src = self._src("app.mt5.btc_fast_exit_daemon")
        self.assertIn("[OLD_BTC_FAST_EXIT_ACCOUNT_CHECK]", src)

    def test_no_or_minus_one_pattern_in_daemon(self):
        """The `or -1` truthiness trap must not exist in btc_fast_exit_daemon.py."""
        src = self._src("app.mt5.btc_fast_exit_daemon")
        self.assertNotIn("or -1", src)

    def test_no_wrong_demo_constant_in_daemon(self):
        """_MT5_TRADE_MODE_DEMO = 1 was the wrong constant and must be gone."""
        src = self._src("app.mt5.btc_fast_exit_daemon")
        self.assertNotIn("_MT5_TRADE_MODE_DEMO", src)

    def test_is_mt5_demo_account_used_in_daemon(self):
        src = self._src("app.mt5.btc_fast_exit_daemon")
        self.assertIn("is_mt5_demo_account", src)

    def test_is_mt5_demo_account_used_in_demo_router(self):
        src = self._src("app.mt5.demo_router")
        self.assertIn("is_mt5_demo_account", src)
        self.assertIn("[OLD_BTC_FAST_EXIT_ACCOUNT_CHECK]", src)
