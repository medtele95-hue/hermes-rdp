"""Tests for the BTC Fast Smart Exit daemon.

Tests the daemon logic (no MT5 calls — close_fn is mocked).
"""
from __future__ import annotations

import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def _pos(ticket=1001, profit=0.05, magic=909002, symbol="BTCUSD#",
         comment="HERMES_BTC", type_=0):
    return SimpleNamespace(
        ticket=ticket,
        profit=profit,
        magic=magic,
        symbol=symbol,
        comment=comment,
        type=type_,
        volume=0.01,
        price_open=65000.0,
    )


def _close_ok(pos, reason):
    return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}


def _close_fail(pos, reason):
    return {"status": "ORDER_FAILED", "order_result": {"retcode": 10004},
            "order_failure_reason": "REQUOTE"}


# ─── Core close logic ─────────────────────────────────────────────────────────

class TestFastExitCloseLogic(unittest.TestCase):
    """Tests that exercise _evaluate_position directly without starting a thread."""

    def _make_daemon(self, close_fn=None, **kw) -> BtcFastExitDaemon:
        return BtcFastExitDaemon(_settings(**kw), close_fn or _close_ok)

    def test_closes_at_min_profit(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture, old_btc_fast_exit_min_profit_usd=0.03)
        d._evaluate_position(_pos(profit=0.03), 0.03, 0.01, True)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][1], "ANY_POSITIVE_FAST_EXIT")

    def test_does_not_close_at_zero(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture)
        d._evaluate_position(_pos(profit=0.00), 0.03, 0.01, True)
        self.assertEqual(len(closed), 0)

    def test_does_not_close_below_min(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture)
        d._evaluate_position(_pos(profit=0.02), 0.03, 0.01, True)
        self.assertEqual(len(closed), 0)

    def test_closes_at_hard_min_after_negative(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture)
        # Mark was_negative
        d._was_negative[1001] = True
        d._evaluate_position(_pos(profit=0.01), 0.03, 0.01, True)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][1], "NEGATIVE_THEN_TINY_POSITIVE")

    def test_does_not_close_at_hard_min_without_negative_history(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture)
        # No negative history, profit only at hard_min threshold
        d._evaluate_position(_pos(profit=0.01), 0.03, 0.01, True)
        # profit=0.01 < min_profit=0.03 AND no was_neg → no close
        self.assertEqual(len(closed), 0)

    def test_negative_sets_was_negative_flag(self):
        d = self._make_daemon()
        d._evaluate_position(_pos(profit=-0.05), 0.03, 0.01, True)
        self.assertTrue(d._was_negative.get(1001, False))

    def test_does_not_close_at_zero_even_with_negative_history(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = self._make_daemon(capture)
        d._was_negative[1001] = True
        # profit=0.00 < hard_min=0.01
        d._evaluate_position(_pos(profit=0.00), 0.03, 0.01, True)
        self.assertEqual(len(closed), 0)

    def test_close_success_updates_last_close_state(self):
        d = self._make_daemon(_close_ok)
        d._evaluate_position(_pos(ticket=2001, profit=0.05), 0.03, 0.01, True)
        with d._lock:
            self.assertEqual(d._last_close_ticket, 2001)
            self.assertAlmostEqual(d._last_close_profit, 0.05)
            self.assertEqual(d._last_close_reason, "ANY_POSITIVE_FAST_EXIT")

    def test_close_failure_clears_in_progress(self):
        d = self._make_daemon(_close_fail)
        d._evaluate_position(_pos(ticket=3001, profit=0.05), 0.03, 0.01, True)
        with d._lock:
            self.assertFalse(d._closing_in_progress.get(3001, False))


# ─── Duplicate close prevention ───────────────────────────────────────────────

class TestFastExitDuplicatePrevention(unittest.TestCase):

    def test_duplicate_close_attempt_skipped(self):
        call_count = [0]

        def counting_close(pos, reason):
            call_count[0] += 1
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), counting_close)
        # Mark as already in progress
        d._closing_in_progress[1001] = True
        d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        self.assertEqual(call_count[0], 0, "Close must be skipped when in-progress=True")

    def test_first_close_not_skipped(self):
        call_count = [0]

        def counting_close(pos, reason):
            call_count[0] += 1
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), counting_close)
        d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        self.assertEqual(call_count[0], 1)

    def test_in_progress_set_before_close_called(self):
        order = []

        def recording_close(pos, reason):
            with d._lock:
                order.append(("close_called", d._closing_in_progress.get(1001)))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), recording_close)
        d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        # in_progress must be True at the time close was called
        self.assertEqual(order[0], ("close_called", True))

    def test_closed_ticket_pruned_from_state(self):
        d = BtcFastExitDaemon(_settings(), _close_ok)
        d._min_seen_profit[9999] = -0.10
        d._was_negative[9999] = True
        d._closing_in_progress[9999] = True

        # Simulate: 9999 is no longer in live_tickets
        with d._lock:
            live_tickets = {1001}
            for t in list(d._min_seen_profit):
                if t not in live_tickets:
                    d._min_seen_profit.pop(t, None)
                    d._was_negative.pop(t, None)
                    d._closing_in_progress.pop(t, None)

        self.assertNotIn(9999, d._min_seen_profit)
        self.assertNotIn(9999, d._closing_in_progress)


# ─── Scope: non-HERMES and non-BTC ────────────────────────────────────────────

class TestFastExitScope(unittest.TestCase):

    def test_non_hermes_btc_not_closed_by_evaluate_position(self):
        """Non-HERMES position never reaches _evaluate_position (filtered by is_hermes_btc_pos).
        Test the filter directly."""
        from app.mt5.smart_rescue import is_hermes_btc_pos
        non_hermes = _pos(magic=99999, comment="OtherBot", symbol="BTCUSD#")
        self.assertFalse(is_hermes_btc_pos(non_hermes, 909002))

    def test_gold_symbol_not_in_scope(self):
        from app.mt5.smart_rescue import is_hermes_btc_pos
        gold = _pos(magic=909002, symbol="GOLD#", comment="HERMES_GOLD")
        self.assertFalse(is_hermes_btc_pos(gold, 909002))

    def test_eurusd_symbol_not_in_scope(self):
        from app.mt5.smart_rescue import is_hermes_btc_pos
        eur = _pos(magic=909002, symbol="EURUSD", comment="HERMES")
        self.assertFalse(is_hermes_btc_pos(eur, 909002))

    def test_btcusd_hash_hermes_magic_in_scope(self):
        from app.mt5.smart_rescue import is_hermes_btc_pos
        pos = _pos(magic=909002, symbol="BTCUSD#", comment="HERMES_BTC")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_btcusd_no_hash_hermes_magic_in_scope(self):
        from app.mt5.smart_rescue import is_hermes_btc_pos
        pos = _pos(magic=909002, symbol="BTCUSD", comment="HERMES_BTC")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_hermes_comment_only_qualifies(self):
        from app.mt5.smart_rescue import is_hermes_btc_pos
        pos = _pos(magic=999, symbol="BTCUSD#", comment="HERMES_SPECIAL")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))


# ─── DEMO account safety ──────────────────────────────────────────────────────

class TestFastExitDemoSafety(unittest.TestCase):

    def test_tick_aborts_on_non_demo_account(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), capture)

        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            # MT5_ACCOUNT_TRADE_MODE_REAL = 2
            account = SimpleNamespace(trade_mode=2)
            mock_ai.return_value = account
            mock_pos.return_value = [_pos(profit=0.10)]
            d._tick()

        self.assertEqual(len(closed), 0, "Must not close on live account")

    def test_tick_closes_on_demo_account(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), capture)

        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            account = SimpleNamespace(trade_mode=0)  # MT5_ACCOUNT_TRADE_MODE_DEMO = 0
            mock_ai.return_value = account
            mock_pos.return_value = [_pos(profit=0.05)]
            d._tick()

        self.assertEqual(len(closed), 1)

    def test_tick_aborts_when_account_info_returns_none(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), capture)

        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            mock_ai.return_value = None
            mock_pos.return_value = [_pos(profit=0.10)]
            d._tick()

        self.assertEqual(len(closed), 0)


# ─── get_status ───────────────────────────────────────────────────────────────

class TestFastExitStatus(unittest.TestCase):

    def test_get_status_returns_expected_keys(self):
        d = BtcFastExitDaemon(_settings(), _close_ok)
        status = d.get_status()
        self.assertIn("fast_exit_daemon_enabled", status)
        self.assertIn("fast_exit_interval_ms", status)
        self.assertIn("fast_exit_last_tick", status)
        self.assertIn("fast_exit_positive_candidates", status)
        self.assertIn("fast_exit_last_close_ticket", status)
        self.assertIn("fast_exit_last_close_profit", status)
        self.assertIn("fast_exit_last_close_reason", status)
        self.assertIn("fast_exit_last_error", status)

    def test_get_status_daemon_enabled_matches_settings(self):
        d = BtcFastExitDaemon(_settings(old_btc_fast_exit_daemon_enabled=True), _close_ok)
        self.assertTrue(d.get_status()["fast_exit_daemon_enabled"])

        d2 = BtcFastExitDaemon(_settings(old_btc_fast_exit_daemon_enabled=False), _close_ok)
        self.assertFalse(d2.get_status()["fast_exit_daemon_enabled"])

    def test_get_status_interval_matches_settings(self):
        d = BtcFastExitDaemon(_settings(old_btc_fast_exit_interval_ms=500), _close_ok)
        self.assertEqual(d.get_status()["fast_exit_interval_ms"], 500)


# ─── Daemon disabled ──────────────────────────────────────────────────────────

class TestFastExitDisabled(unittest.TestCase):

    def test_tick_noop_when_disabled(self):
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(
            _settings(old_btc_fast_exit_daemon_enabled=False), capture
        )
        with patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos:
            mock_pos.return_value = [_pos(profit=0.10)]
            d._tick()

        self.assertEqual(len(closed), 0)
        mock_pos.assert_not_called()


# ─── min_seen_profit tracking ────────────────────────────────────────────────

class TestFastExitMinSeenProfit(unittest.TestCase):

    def test_min_seen_profit_tracks_minimum(self):
        d = BtcFastExitDaemon(_settings(), _close_ok)
        d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        d._evaluate_position(_pos(profit=-0.05), 0.03, 0.01, True)
        self.assertAlmostEqual(d._min_seen_profit.get(1001, 0), -0.05)

    def test_min_seen_profit_does_not_increase(self):
        d = BtcFastExitDaemon(_settings(), _close_ok)
        d._min_seen_profit[1001] = -0.20
        d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        self.assertAlmostEqual(d._min_seen_profit.get(1001, 0), -0.20)


# ─── Position managed regardless of entry blocks ──────────────────────────────

class TestFastExitIndependentOfCycle(unittest.TestCase):
    """Fast exit runs even when strategy is WAIT or entries are blocked."""

    def test_daemon_manages_position_when_entry_gate_would_block(self):
        """Entry gate blocks new entries; daemon still closes existing position."""
        from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _BTC_SCALPING
        # Entry gate blocks because open_count=1
        gate_result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None,
            _settings(), open_btc_count=1, confidence=80.0,
        )
        self.assertEqual(gate_result["decision"], "BLOCK")

        # Daemon can still close the existing position
        closed = []

        def capture(pos, reason):
            closed.append((pos.ticket, reason))
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), capture)
        d._evaluate_position(_pos(profit=0.05), 0.03, 0.01, True)
        self.assertEqual(len(closed), 1, "Daemon must close even when entry gate blocks")

    def test_daemon_manages_position_when_no_new_entries_allowed(self):
        """Daemon closes existing positions; does not care about new entry permission."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        d = BtcFastExitDaemon(_settings(), capture)
        d._evaluate_position(_pos(profit=0.04), 0.03, 0.01, True)
        self.assertIn("ANY_POSITIVE_FAST_EXIT", closed)


# ─── Safety invariants ────────────────────────────────────────────────────────

class TestFastExitSafetyInvariants(unittest.TestCase):

    def test_no_order_send_in_fast_exit_daemon(self):
        import re
        from pathlib import Path
        src = Path("app/mt5/btc_fast_exit_daemon.py").read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], "btc_fast_exit_daemon.py must NOT call mt5.order_send")

    def test_live_trading_always_false(self):
        from app.config import get_settings
        self.assertFalse(get_settings().allow_live_trading)

    def test_new_config_fields_exist_with_correct_defaults(self):
        from app.config import get_settings
        s = get_settings()
        self.assertTrue(getattr(s, "old_btc_fast_exit_daemon_enabled", None))
        self.assertEqual(getattr(s, "old_btc_fast_exit_interval_ms", None), 250)
        self.assertAlmostEqual(getattr(s, "old_btc_fast_exit_min_profit_usd", None), 0.03)
        self.assertAlmostEqual(getattr(s, "old_btc_fast_exit_hard_min_profit_usd", None), 0.01)
        self.assertTrue(getattr(s, "old_btc_fast_exit_close_at_any_positive", None))

    def test_fast_exit_close_position_in_demo_router(self):
        """DemoKellyRouter has fast_exit_close_position method."""
        from app.mt5.demo_router import DemoKellyRouter
        self.assertTrue(hasattr(DemoKellyRouter, "fast_exit_close_position"))

    def test_fast_exit_close_position_skips_non_hermes(self):
        """fast_exit_close_position must reject non-HERMES positions.

        DEMO check runs first — must pass with a DEMO account so that
        the NOT_HERMES_BTC SKIP reason is actually reached.
        """
        from app.mt5.demo_router import DemoKellyRouter
        from app.config import get_settings

        router = DemoKellyRouter(get_settings())
        non_hermes = _pos(magic=99999, comment="OtherBot")
        with patch("app.mt5.demo_router.mt5.account_info") as mock_ai:
            mock_ai.return_value = SimpleNamespace(trade_mode=0)  # DEMO passes
            result = router.fast_exit_close_position(non_hermes, "TEST")
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "NOT_HERMES_BTC")

    def test_start_fast_exit_daemon_method_exists(self):
        """DemoKellyRouter has start_fast_exit_daemon method."""
        from app.mt5.demo_router import DemoKellyRouter
        self.assertTrue(hasattr(DemoKellyRouter, "start_fast_exit_daemon"))


if __name__ == "__main__":
    unittest.main()
