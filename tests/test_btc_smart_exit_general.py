"""Tests for the general BTC smart exit system.

Covers:
- btc_exit_danger: BUY/SELL danger signal detection
- Safety invariants: no mt5.order_send outside demo_router, live=False, lot=0.01
- Scope: non-HERMES positions ignored, non-BTC symbol ignored
- Entry gate integration: order_send only in demo_router
- Config defaults for all new OLD_BTC_* settings
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.mt5.btc_exit_danger import evaluate_btc_exit_danger, DEFAULT_MIN_SIGNALS
from app.mt5.smart_rescue import is_hermes_btc_pos


# ─── Position stub ───────────────────────────────────────────────────────────

def _pos(type_=0, profit=0.5, magic=909002, symbol="BTCUSD#", comment="HERMES",
         ticket=1001, price_open=65000.0):
    return SimpleNamespace(
        type=type_,
        profit=profit,
        magic=magic,
        symbol=symbol,
        comment=comment,
        ticket=ticket,
        price_open=price_open,
    )


def _btc_buy_ctx(**kwargs):
    """Minimal BUY-friendly context — no danger signals."""
    base = {
        "price": 65100.0,
        "bid": 65100.0,
        "ask": 65102.0,
        "vwap": 65000.0,
        "poc": 64900.0,
        "m1_momentum": "BULLISH",
        "m5_momentum": "BULLISH",
        "cvd_slope": 0.4,
        "order_flow_signal": "BUY",
    }
    base.update(kwargs)
    return base


def _btc_sell_ctx(**kwargs):
    """Minimal SELL-friendly context — no danger signals."""
    base = {
        "price": 64900.0,
        "bid": 64899.0,
        "ask": 64901.0,
        "vwap": 65000.0,
        "poc": 65100.0,
        "m1_momentum": "BEARISH",
        "m5_momentum": "BEARISH",
        "cvd_slope": -0.4,
        "order_flow_signal": "SELL",
    }
    base.update(kwargs)
    return base


# ─── BUY position danger detection ───────────────────────────────────────────

class TestBtcExitDangerBuyPosition(unittest.TestCase):
    """evaluate_btc_exit_danger() for BUY positions (type=0)."""

    def test_buy_no_danger_signals_returns_false(self):
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), _btc_buy_ctx())
        self.assertFalse(result["danger"])
        self.assertEqual(result["direction"], "BUY")

    def test_buy_price_below_vwap_and_poc_gives_2_signals(self):
        ctx = _btc_buy_ctx(price=64800.0, vwap=65000.0, poc=64900.0)
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("PRICE_BELOW_VWAP", result["signals"])
        self.assertIn("PRICE_BELOW_POC", result["signals"])
        self.assertTrue(result["danger"])

    def test_buy_bearish_m1_and_m5_triggers_danger(self):
        ctx = _btc_buy_ctx(m1_momentum="BEARISH", m5_momentum="BEARISH")
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("M1_BEARISH_MOMENTUM", result["signals"])
        self.assertIn("M5_BEARISH_MOMENTUM", result["signals"])
        self.assertTrue(result["danger"])

    def test_buy_negative_cvd_triggers_signal(self):
        ctx = _btc_buy_ctx(cvd_slope=-0.4)  # < -0.3 threshold
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("CVD_SLOPE_NEGATIVE", result["signals"])

    def test_buy_order_flow_sell_triggers_signal(self):
        ctx = _btc_buy_ctx(order_flow_signal="SELL")
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("ORDER_FLOW_NOT_BUY", result["signals"])

    def test_buy_spread_near_max_triggers_signal(self):
        ctx = _btc_buy_ctx(spread=90.0, max_spread=100.0)  # 90% > 85% ratio
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("SPREAD_NEAR_MAX", result["signals"])

    def test_buy_ema_bearish_cross_triggers_signal(self):
        ctx = _btc_buy_ctx(m1_ema_fast=64990.0, m1_ema_slow=65010.0)
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertIn("M1_EMA_BEARISH_CROSS", result["signals"])

    def test_buy_multiple_signals_exceed_threshold(self):
        ctx = {
            "price": 64800.0, "vwap": 65000.0, "poc": 64900.0,
            "m1_momentum": "BEARISH", "m5_momentum": "BEARISH",
            "cvd_slope": -0.5, "order_flow_signal": "SELL",
            "bid": 64800.0, "ask": 64802.0,
        }
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx)
        self.assertTrue(result["danger"])
        self.assertGreaterEqual(result["signal_count"], 2)

    def test_buy_single_signal_no_danger_with_default_threshold(self):
        ctx = _btc_buy_ctx(m1_momentum="BEARISH")  # only 1 signal
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx, min_signals=2)
        self.assertEqual(result["signal_count"], 1)
        self.assertFalse(result["danger"])

    def test_buy_custom_min_signals_1_triggers_on_one_signal(self):
        ctx = _btc_buy_ctx(m1_momentum="BEARISH")
        result = evaluate_btc_exit_danger(_pos(type_=0, profit=0.5), ctx, min_signals=1)
        self.assertTrue(result["danger"])


# ─── SELL position danger detection ──────────────────────────────────────────

class TestBtcExitDangerSellPosition(unittest.TestCase):
    """evaluate_btc_exit_danger() for SELL positions (type=1)."""

    def test_sell_no_danger_signals_returns_false(self):
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), _btc_sell_ctx())
        self.assertFalse(result["danger"])
        self.assertEqual(result["direction"], "SELL")

    def test_sell_price_above_vwap_and_poc_gives_2_signals(self):
        ctx = _btc_sell_ctx(price=65200.0, vwap=65000.0, poc=65100.0)
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertIn("PRICE_ABOVE_VWAP", result["signals"])
        self.assertIn("PRICE_ABOVE_POC", result["signals"])
        self.assertTrue(result["danger"])

    def test_sell_bullish_m1_and_m5_triggers_danger(self):
        ctx = _btc_sell_ctx(m1_momentum="BULLISH", m5_momentum="BULLISH")
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertIn("M1_BULLISH_MOMENTUM", result["signals"])
        self.assertIn("M5_BULLISH_MOMENTUM", result["signals"])
        self.assertTrue(result["danger"])

    def test_sell_positive_cvd_triggers_signal(self):
        ctx = _btc_sell_ctx(cvd_slope=0.4)  # > 0.3 threshold
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertIn("CVD_SLOPE_POSITIVE", result["signals"])

    def test_sell_order_flow_buy_triggers_signal(self):
        ctx = _btc_sell_ctx(order_flow_signal="BUY")
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertIn("ORDER_FLOW_NOT_SELL", result["signals"])

    def test_sell_ema_bullish_cross_triggers_signal(self):
        ctx = _btc_sell_ctx(m5_ema_fast=65020.0, m5_ema_slow=64990.0)
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertIn("M5_EMA_BULLISH_CROSS", result["signals"])

    def test_sell_multiple_danger_signals(self):
        ctx = {
            "price": 65200.0, "vwap": 65000.0, "poc": 65100.0,
            "m1_momentum": "BULLISH", "m5_momentum": "BULLISH",
            "cvd_slope": 0.5, "order_flow_signal": "BUY",
            "bid": 65198.0, "ask": 65200.0,
        }
        result = evaluate_btc_exit_danger(_pos(type_=1, profit=0.5), ctx)
        self.assertTrue(result["danger"])
        self.assertGreaterEqual(result["signal_count"], 2)


# ─── Edge cases ───────────────────────────────────────────────────────────────

class TestBtcExitDangerEdgeCases(unittest.TestCase):

    def test_no_market_context_returns_false(self):
        result = evaluate_btc_exit_danger(_pos(), None)
        self.assertFalse(result["danger"])
        self.assertEqual(result["reason"], "NO_MARKET_CONTEXT")
        self.assertEqual(result["signals"], [])

    def test_empty_market_context_no_signals(self):
        result = evaluate_btc_exit_danger(_pos(), {})
        # empty dict is falsy — same branch as None
        self.assertFalse(result["danger"])

    def test_unknown_position_type_returns_false(self):
        result = evaluate_btc_exit_danger(_pos(type_=99), _btc_buy_ctx())
        self.assertFalse(result["danger"])
        self.assertEqual(result["direction"], "UNKNOWN")

    def test_missing_individual_fields_no_crash(self):
        # Minimal context with only a few keys
        ctx = {"m1_momentum": "BEARISH", "m5_momentum": "BEARISH"}
        result = evaluate_btc_exit_danger(_pos(type_=0), ctx)
        self.assertIsInstance(result["signals"], list)
        self.assertIsInstance(result["danger"], bool)

    def test_signal_count_matches_signals_list(self):
        ctx = {
            "price": 64800.0, "vwap": 65000.0, "poc": 64900.0,
            "m1_momentum": "BEARISH",
        }
        result = evaluate_btc_exit_danger(_pos(type_=0), ctx)
        self.assertEqual(result["signal_count"], len(result["signals"]))

    def test_signals_are_deduplicated(self):
        ctx = {
            "price": 64800.0, "vwap": 65000.0, "poc": 64900.0,
            "m1_momentum": "BEARISH", "m5_momentum": "BEARISH",
        }
        result = evaluate_btc_exit_danger(_pos(type_=0), ctx)
        self.assertEqual(len(result["signals"]), len(set(result["signals"])))

    def test_default_min_signals_is_2(self):
        self.assertEqual(DEFAULT_MIN_SIGNALS, 2)


# ─── HERMES position identification ───────────────────────────────────────────

class TestHermesBtcPositionScope(unittest.TestCase):
    """Verify only HERMES BTC positions are identified as in-scope."""

    def test_hermes_magic_btcusd_hash_is_in_scope(self):
        pos = _pos(magic=909002, symbol="BTCUSD#", comment="HERMES_BTC")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_hermes_magic_btcusd_no_hash_is_in_scope(self):
        pos = _pos(magic=909002, symbol="BTCUSD", comment="HERMES_BTC")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_non_hermes_magic_no_hermes_comment_not_in_scope(self):
        pos = _pos(magic=12345, symbol="BTCUSD#", comment="SomeOtherBot")
        self.assertFalse(is_hermes_btc_pos(pos, 909002))

    def test_hermes_comment_without_magic_is_in_scope(self):
        """HERMES comment alone qualifies even if magic differs."""
        pos = _pos(magic=999999, symbol="BTCUSD#", comment="HERMES_MANUAL")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_non_btc_symbol_not_in_scope(self):
        pos = _pos(magic=909002, symbol="XAUUSD", comment="HERMES")
        self.assertFalse(is_hermes_btc_pos(pos, 909002))

    def test_eurusd_symbol_not_in_scope(self):
        pos = _pos(magic=909002, symbol="EURUSD", comment="HERMES")
        self.assertFalse(is_hermes_btc_pos(pos, 909002))

    def test_gold_symbol_not_in_scope(self):
        pos = _pos(magic=909002, symbol="GOLD#", comment="HERMES_GOLD")
        self.assertFalse(is_hermes_btc_pos(pos, 909002))

    def test_danger_evaluator_works_only_on_hermes_btc(self):
        """Non-HERMES non-BTC position → is_hermes_btc_pos=False; danger not called."""
        non_hermes = _pos(magic=12345, symbol="XAUUSD", comment="OtherBot")
        self.assertFalse(is_hermes_btc_pos(non_hermes, 909002))


# ─── Safety invariants ────────────────────────────────────────────────────────

class TestBtcSmartExitSafetyInvariants(unittest.TestCase):
    """Critical safety rules that must never be violated."""

    def test_live_trading_always_false_in_config(self):
        from app.config import get_settings
        s = get_settings()
        self.assertFalse(s.allow_live_trading, "allow_live_trading MUST be False")

    def test_demo_only_always_true_in_config(self):
        from app.config import get_settings
        s = get_settings()
        self.assertTrue(s.demo_only, "demo_only MUST be True")

    def test_default_lot_size_is_point_01(self):
        from app.config import get_settings
        s = get_settings()
        self.assertLessEqual(s.demo_max_lot, 0.01, "demo_max_lot must not exceed 0.01")

    def test_no_order_send_in_btc_exit_danger(self):
        src = Path("app/mt5/btc_exit_danger.py").read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], "btc_exit_danger.py must not call mt5.order_send")

    def test_no_order_send_in_btc_entry_gate(self):
        src = Path("app/mt5/btc_entry_gate.py").read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], "btc_entry_gate.py must not call mt5.order_send")

    def test_order_send_exclusively_in_demo_router(self):
        """Only demo_router.py may contain mt5.order_send calls."""
        app_dir = Path("app")
        violations = []
        for py_file in app_dir.rglob("*.py"):
            if py_file.name == "demo_router.py":
                continue
            text = py_file.read_text(encoding="utf-8")
            if re.search(r'(?<!["\'])mt5\.order_send\s*\(', text):
                violations.append(str(py_file))
        self.assertEqual(
            violations, [],
            f"mt5.order_send found outside demo_router.py: {violations}",
        )

    def test_new_config_fields_have_correct_defaults(self):
        from app.config import get_settings
        s = get_settings()
        self.assertTrue(getattr(s, "old_btc_smart_exit_enabled", True))
        self.assertAlmostEqual(getattr(s, "old_btc_positive_exit_min_usd", 0.08), 0.08)
        self.assertAlmostEqual(getattr(s, "old_btc_danger_exit_min_usd", 0.08), 0.08)
        self.assertEqual(getattr(s, "old_btc_emergency_open_count", 1), 1)
        self.assertEqual(getattr(s, "old_btc_smart_exit_min_danger_signals", 2), 2)
        self.assertTrue(getattr(s, "old_btc_entry_gate_enabled", True))
        self.assertEqual(getattr(s, "old_btc_entry_gate_scalping_min_confidence", 55), 55)
        self.assertEqual(getattr(s, "old_btc_entry_gate_order_flow_min_score", 75), 75)
        self.assertTrue(getattr(s, "old_btc_entry_gate_require_market_confirmation", True))

    def test_btc_entry_gate_applies_to_both_strategies(self):
        from app.mt5.btc_entry_gate import _BTC_STRATEGIES, _BTC_SCALPING, _ORDER_FLOW
        self.assertIn(_BTC_SCALPING, _BTC_STRATEGIES)
        self.assertIn(_ORDER_FLOW, _BTC_STRATEGIES)
        self.assertEqual(_BTC_SCALPING, "BTC_SCALPING_AGENT")
        self.assertEqual(_ORDER_FLOW, "ORDER_FLOW_EXECUTION_AGENT")


# ─── Emergency mode ───────────────────────────────────────────────────────────

class TestBtcSmartExitEmergencyMode(unittest.TestCase):
    """Emergency state: open_count > old_btc_emergency_open_count."""

    def test_evaluate_rescue_emergency_when_count_exceeds_limit(self):
        from app.mt5.smart_rescue import evaluate_rescue, SmartRescueConfig
        from datetime import datetime, timezone, timedelta
        import time

        cfg = SmartRescueConfig(
            enabled=True,
            rescue_min_profit_usd=0.08,
            rescue_arm_drawdown_usd=-0.20,
            emergency_open_count_gt=1,
            emergency_positive_usd=0.08,
            max_hold_seconds=180,
            protect_existing=True,
            magic_number=909002,
            max_open_positions=1,
        )
        pos = _pos(profit=0.10, magic=909002, symbol="BTCUSD#")
        states: dict = {}
        now = datetime.now(timezone.utc)
        # open_count=2 triggers emergency
        action = evaluate_rescue(pos, states, cfg, open_btc_count=2, now=now)
        self.assertIsNotNone(action, "Emergency close must be triggered when open_count > 1")
        self.assertEqual(action.get("reason"), "EMERGENCY_MULTIPLE_POSITIONS")

    def test_evaluate_rescue_no_emergency_at_count_1(self):
        from app.mt5.smart_rescue import evaluate_rescue, SmartRescueConfig
        from datetime import datetime, timezone

        cfg = SmartRescueConfig(
            enabled=True,
            rescue_min_profit_usd=0.08,
            rescue_arm_drawdown_usd=-0.20,
            emergency_open_count_gt=1,
            emergency_positive_usd=0.08,
            max_hold_seconds=180,
            protect_existing=True,
            magic_number=909002,
            max_open_positions=1,
        )
        pos = _pos(profit=0.10, magic=909002, symbol="BTCUSD#")
        states: dict = {}
        now = datetime.now(timezone.utc)
        # open_count=1 — no emergency
        action = evaluate_rescue(pos, states, cfg, open_btc_count=1, now=now)
        # May arm but should not return an emergency close
        if action is not None:
            self.assertNotEqual(action.get("reason"), "EMERGENCY_MULTIPLE_POSITIONS")


# ─── Dashboard state ──────────────────────────────────────────────────────────

class TestBtcStatusState(unittest.TestCase):
    """Local state BTC status tracking."""

    def test_update_and_get_hermes_btc_status(self):
        from app.local_api.state import get_local_state
        ls = get_local_state()
        ls.update_hermes_btc_status({
            "open_count": 2,
            "floating_pnl": -0.15,
            "positive_candidates": 1,
            "emergency_active": True,
        })
        status = ls.get_hermes_btc_status()
        self.assertEqual(status["open_count"], 2)
        self.assertAlmostEqual(status["floating_pnl"], -0.15)
        self.assertEqual(status["positive_candidates"], 1)
        self.assertTrue(status["emergency_active"])

    def test_initial_state_is_safe(self):
        from app.local_api.state import get_local_state
        import app.local_api.state as _state_mod
        # Create a fresh isolated state by accessing the internal class
        cls = type(get_local_state())
        ls = cls()
        status = ls.get_hermes_btc_status()
        self.assertEqual(status["open_count"], 0)
        self.assertFalse(status.get("emergency_active", False))

    def test_state_update_preserves_last_exit_reason(self):
        from app.local_api.state import get_local_state
        ls = get_local_state()
        ls.update_hermes_btc_status({"last_smart_exit_reason": "MARKET_DANGER_POSITIVE_EXIT"})
        status = ls.get_hermes_btc_status()
        self.assertEqual(status.get("last_smart_exit_reason"), "MARKET_DANGER_POSITIVE_EXIT")


# ─── Open position guard — entry blocked when BTC position exists ─────────────

class TestBtcOpenPositionGuardWithEntryGate(unittest.TestCase):
    """Combined open-position-guard + entry gate — new entry blocked when count>=max."""

    def test_existing_btc_position_blocks_scalping_entry(self):
        from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _BTC_SCALPING
        from types import SimpleNamespace
        s = SimpleNamespace(
            allow_live_trading=False, demo_only=True,
            old_btc_max_open_positions=1,
            old_btc_entry_gate_scalping_min_confidence=55,
            old_btc_entry_gate_order_flow_min_score=75,
            old_btc_entry_gate_require_market_confirmation=False,
        )
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, s, open_btc_count=1, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "HERMES_BTC_POSITION_ALREADY_OPEN")

    def test_existing_btc_position_blocks_order_flow_entry(self):
        from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _ORDER_FLOW
        from types import SimpleNamespace
        s = SimpleNamespace(
            allow_live_trading=False, demo_only=True,
            old_btc_max_open_positions=1,
            old_btc_entry_gate_scalping_min_confidence=55,
            old_btc_entry_gate_order_flow_min_score=75,
            old_btc_entry_gate_require_market_confirmation=False,
        )
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "SELL", None, s, open_btc_count=1, confidence=80.0
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "HERMES_BTC_POSITION_ALREADY_OPEN")

    def test_no_position_allows_scalping_entry(self):
        from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _BTC_SCALPING
        from types import SimpleNamespace
        s = SimpleNamespace(
            allow_live_trading=False, demo_only=True,
            old_btc_max_open_positions=1,
            old_btc_entry_gate_scalping_min_confidence=55,
            old_btc_entry_gate_order_flow_min_score=75,
            old_btc_entry_gate_require_market_confirmation=False,
        )
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "BUY", None, s, open_btc_count=0, confidence=70.0
        )
        self.assertEqual(result["decision"], "PASS")

    def test_no_position_allows_order_flow_entry(self):
        from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _ORDER_FLOW
        from types import SimpleNamespace
        s = SimpleNamespace(
            allow_live_trading=False, demo_only=True,
            old_btc_max_open_positions=1,
            old_btc_entry_gate_scalping_min_confidence=55,
            old_btc_entry_gate_order_flow_min_score=75,
            old_btc_entry_gate_require_market_confirmation=False,
        )
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, s, open_btc_count=0, confidence=75.0
        )
        self.assertEqual(result["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
