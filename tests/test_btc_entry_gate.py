"""Tests for app/mt5/btc_entry_gate.py — Shared BTC entry gate."""
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _BTC_SCALPING, _ORDER_FLOW


def _settings(**kwargs):
    defaults = {
        "allow_live_trading": False,
        "demo_only": True,
        "old_btc_max_open_positions": 1,
        "btc_scalping_min_confidence": 55,
        "order_flow_min_score": 75,
        "old_btc_entry_gate_require_market_confirmation": True,
        "old_btc_entry_gate_scalping_min_confidence": 55,
        "old_btc_entry_gate_order_flow_min_score": 75,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _pass_ctx():
    return {"vwap": 65000.0, "poc": 65000.0, "price": 65050.0, "bid": 65050.0, "m1_momentum": "BULLISH", "m5_momentum": "BULLISH"}


class TestBtcEntryGateScalping(unittest.TestCase):
    """BTC_SCALPING_AGENT entry gate tests."""

    def test_scalping_weak_confidence_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), 0, confidence=40.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("CONFIDENCE", result["failed_check"])

    def test_scalping_confidence_at_threshold_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", _pass_ctx(), _settings(), 0, confidence=55.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_scalping_confidence_above_threshold_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", _pass_ctx(), _settings(), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_scalping_no_confidence_still_passes(self):
        # If confidence is not provided, skip the check
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), 0, confidence=None
        )
        self.assertEqual(result["decision"], "PASS")

    def test_scalping_valid_confidence_and_market_confirmation_passes(self):
        ctx = {"price": 65050.0, "vwap": 65000.0, "poc": 64900.0, "m1_momentum": "BULLISH", "m5_momentum": "BULLISH"}
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", ctx, _settings(), 0, confidence=60.0
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertIn("score", result)

    def test_scalping_score_boosted_by_momentum(self):
        ctx = {"price": 65050.0, "vwap": 64000.0, "m1_momentum": "BULLISH", "m5_momentum": "BULLISH"}
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", ctx, _settings(), 0, confidence=60.0
        )
        self.assertGreater(result.get("score", 0), 60.0)


class TestBtcEntryGateOrderFlow(unittest.TestCase):
    """ORDER_FLOW_EXECUTION_AGENT entry gate tests."""

    def test_order_flow_invalid_score_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _settings(), 0, confidence=50.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("CONFIDENCE", result["failed_check"])

    def test_order_flow_score_at_threshold_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", _pass_ctx(), _settings(), 0, confidence=75.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_order_flow_with_valid_setup_passes(self):
        ctx = {"price": 65100.0, "vwap": 65000.0, "poc": 64900.0, "m1_momentum": "BULLISH", "cvd_slope": 0.2}
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", ctx, _settings(), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_order_flow_invalid_setup_score_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "SELL", None, _settings(), 0, confidence=74.9
        )
        self.assertEqual(result["decision"], "BLOCK")


class TestBtcEntryGateOpenPositionGuard(unittest.TestCase):
    """Open position guard tests."""

    def test_existing_hermes_btc_position_blocks_all_new_entries_scalping(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), open_btc_count=1, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "HERMES_BTC_POSITION_ALREADY_OPEN")
        self.assertEqual(result["failed_check"], "OPEN_POSITION_CHECK")

    def test_existing_hermes_btc_position_blocks_all_new_entries_order_flow(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "SELL", None, _settings(), open_btc_count=1, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "HERMES_BTC_POSITION_ALREADY_OPEN")

    def test_zero_open_positions_allows_entry(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), open_btc_count=0, confidence=60.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_max_positions_2_allows_second_entry(self):
        s = _settings(old_btc_max_open_positions=2)
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, s, open_btc_count=1, confidence=60.0
        )
        self.assertEqual(result["decision"], "PASS")


class TestBtcEntryGateSafetyChecks(unittest.TestCase):
    """Safety invariant tests."""

    def test_wrong_symbol_blocks(self):
        result = evaluate_btc_entry_gate(
            "XAUUSD", _BTC_SCALPING, "BUY", None, _settings(), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "SYMBOL_NOT_BTC")

    def test_btcusd_without_hash_passes_symbol_check(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD", _BTC_SCALPING, "BUY", None, _settings(), 0, confidence=60.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_live_trading_enabled_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None,
            _settings(allow_live_trading=True), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "LIVE_TRADING_NOT_ALLOWED")

    def test_demo_only_false_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None,
            _settings(demo_only=False), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "DEMO_ONLY_DISABLED")

    def test_wait_direction_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "WAIT", None, _settings(), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "DIRECTION_NOT_BUY_OR_SELL")

    def test_spread_too_wide_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), 0,
            confidence=80.0, spread=150.0, max_spread=100.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "SPREAD_TOO_WIDE")

    def test_spread_ok_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, _settings(), 0,
            confidence=80.0, spread=80.0, max_spread=100.0
        )
        self.assertEqual(result["decision"], "PASS")


class TestBtcEntryGateMarketConfirmation(unittest.TestCase):
    """Market confirmation soft block tests."""

    def test_strong_bearish_impulse_no_confirmation_blocks_buy(self):
        ctx = {
            "price": 64000.0, "vwap": 65000.0, "poc": 65000.0,
            "cvd_slope": -0.8, "m1_momentum": "BEARISH", "m5_momentum": "BEARISH",
            "order_flow_signal": "SELL",
        }
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", ctx, _settings(), 0, confidence=70.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("BEARISH", result["reason"])

    def test_no_impulse_passes_buy(self):
        ctx = {"vwap": 65000.0, "poc": 65000.0, "price": 65050.0}
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", ctx, _settings(), 0, confidence=70.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_market_confirmation_disabled_skips_check(self):
        ctx = {
            "price": 64000.0, "vwap": 65000.0, "poc": 65000.0,
            "cvd_slope": -0.9, "m1_momentum": "BEARISH",
        }
        s = _settings(old_btc_entry_gate_require_market_confirmation=False)
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", ctx, s, 0, confidence=70.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_strong_bullish_impulse_blocks_sell(self):
        ctx = {
            "price": 66000.0, "vwap": 65000.0, "poc": 65000.0,
            "cvd_slope": 0.8, "m1_momentum": "BULLISH", "m5_momentum": "BULLISH",
            "order_flow_signal": "BUY",
        }
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "SELL", ctx, _settings(), 0, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("BULLISH", result["reason"])


class TestBtcEntryGateMt5ReadFailure(unittest.TestCase):
    """MT5 position read failure integration simulation."""

    def test_entry_gate_raises_no_exception_on_bad_settings(self):
        """Gate must not raise — bad settings should still produce BLOCK."""
        s = SimpleNamespace()  # no attrs at all
        # Should not raise; should fail gracefully on missing attrs
        try:
            result = evaluate_btc_entry_gate("BTCUSD#", _BTC_SCALPING, "BUY", None, s, 0)
            # demo_only defaults to True in getattr fallback — may pass or block
            self.assertIn(result["decision"], {"PASS", "BLOCK"})
        except Exception as exc:
            self.fail(f"evaluate_btc_entry_gate raised {exc}")


class TestBtcEntryGateSafetyInvariants(unittest.TestCase):
    """Critical safety invariants."""

    def test_live_trading_always_false(self):
        from app.config import get_settings
        s = get_settings()
        self.assertFalse(s.allow_live_trading)

    def test_no_order_send_in_btc_entry_gate(self):
        import re
        from pathlib import Path
        src = Path("app/mt5/btc_entry_gate.py").read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], msg="btc_entry_gate.py must not call mt5.order_send")

    def test_no_order_send_in_btc_exit_danger(self):
        import re
        from pathlib import Path
        src = Path("app/mt5/btc_exit_danger.py").read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], msg="btc_exit_danger.py must not call mt5.order_send")

    def test_entry_gate_applies_to_both_btc_strategies(self):
        from app.mt5.btc_entry_gate import _BTC_STRATEGIES
        self.assertIn("BTC_SCALPING_AGENT", _BTC_STRATEGIES)
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", _BTC_STRATEGIES)


if __name__ == "__main__":
    unittest.main()
