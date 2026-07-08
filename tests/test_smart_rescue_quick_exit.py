"""Tests for Smart Rescue Quick Exit (LOVABLE_BTC_OLD_SYSTEM).

Covers:
  A. evaluate_rescue() pure-logic rules
  B. is_hermes_btc_pos() and count_hermes_btc_open() helpers
  C. SmartRescueConfig defaults
  D. DemoKellyRouter.process_quick_exits() integration (rescue branch)
  E. Open position guard (both inside DemoRouter.evaluate() and standalone)
  F. Safety invariants (live account blocked, mt5.order_send only in demo_router.py)
"""
from __future__ import annotations

import re
import sys
import time
import types
import unittest
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_ROUTER_PATH = ROOT / "app" / "mt5" / "demo_router.py"
SMART_RESCUE_PATH = ROOT / "app" / "mt5" / "smart_rescue.py"

# ── Fake position namedtuple ─────────────────────────────────────────────────

_Pos = namedtuple(
    "_Pos",
    ["ticket", "symbol", "type", "volume", "profit", "magic", "comment", "time"],
)

def _btc_pos(
    ticket: int = 100001,
    profit: float = 0.0,
    magic: int = 909002,
    comment: str = "HERMES_RESCUE_TEST",
    pos_type: int = 0,       # 0=BUY
    pos_time: int = 0,
) -> _Pos:
    return _Pos(
        ticket=ticket, symbol="BTCUSD#", type=pos_type,
        volume=0.01, profit=profit, magic=magic,
        comment=comment, time=pos_time,
    )

def _other_pos(symbol: str = "EURUSD", ticket: int = 200001, profit: float = 0.5,
               magic: int = 909002, comment: str = "HERMES_DEMO_KELLY") -> _Pos:
    return _Pos(ticket=ticket, symbol=symbol, type=0, volume=0.01,
                profit=profit, magic=magic, comment=comment, time=0)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

# ── Imports ──────────────────────────────────────────────────────────────────

from app.mt5.smart_rescue import (
    SmartRescueConfig,
    evaluate_rescue,
    is_hermes_btc_pos,
    count_hermes_btc_open,
)


# ══════════════════════════════════════════════════════════════════════════════
# A. SmartRescueConfig defaults
# ══════════════════════════════════════════════════════════════════════════════

class TestSmartRescueConfigDefaults(unittest.TestCase):

    def test_enabled_default(self) -> None:
        self.assertTrue(SmartRescueConfig().enabled)

    def test_rescue_min_profit_default(self) -> None:
        self.assertAlmostEqual(SmartRescueConfig().rescue_min_profit_usd, 0.08)

    def test_rescue_arm_drawdown_default(self) -> None:
        self.assertAlmostEqual(SmartRescueConfig().rescue_arm_drawdown_usd, -0.20)

    def test_emergency_open_count_gt_default(self) -> None:
        self.assertEqual(SmartRescueConfig().emergency_open_count_gt, 1)

    def test_emergency_positive_usd_default(self) -> None:
        self.assertAlmostEqual(SmartRescueConfig().emergency_positive_usd, 0.08)

    def test_max_hold_seconds_default(self) -> None:
        self.assertEqual(SmartRescueConfig().max_hold_seconds, 180)

    def test_magic_number_default(self) -> None:
        self.assertEqual(SmartRescueConfig().magic_number, 909002)

    def test_max_open_positions_default(self) -> None:
        self.assertEqual(SmartRescueConfig().max_open_positions, 1)


# ══════════════════════════════════════════════════════════════════════════════
# B. is_hermes_btc_pos & count_hermes_btc_open
# ══════════════════════════════════════════════════════════════════════════════

class TestIsHermesBtcPos(unittest.TestCase):

    def test_btcusd_hash_magic_match(self) -> None:
        self.assertTrue(is_hermes_btc_pos(_btc_pos(magic=909002), 909002))

    def test_btcusd_no_hash_magic_match(self) -> None:
        pos = _Pos(ticket=1, symbol="BTCUSD", type=0, volume=0.01, profit=0,
                   magic=909002, comment="HERMES_TEST", time=0)
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_btcusd_hermes_in_comment(self) -> None:
        pos = _btc_pos(magic=999999, comment="HERMES_OLD")
        self.assertTrue(is_hermes_btc_pos(pos, 909002))

    def test_non_btc_symbol_excluded(self) -> None:
        self.assertFalse(is_hermes_btc_pos(_other_pos(symbol="EURUSD"), 909002))

    def test_gold_excluded(self) -> None:
        self.assertFalse(is_hermes_btc_pos(_other_pos(symbol="GOLD#"), 909002))

    def test_btc_wrong_magic_no_hermes_comment_excluded(self) -> None:
        pos = _btc_pos(magic=12345, comment="OTHER_BOT")
        self.assertFalse(is_hermes_btc_pos(pos, 909002))

    def test_count_zero_when_no_positions(self) -> None:
        self.assertEqual(count_hermes_btc_open([], 909002), 0)

    def test_count_one_hermes_btc(self) -> None:
        self.assertEqual(count_hermes_btc_open([_btc_pos()], 909002), 1)

    def test_count_ignores_other_symbols(self) -> None:
        positions = [_btc_pos(), _other_pos(symbol="EURUSD")]
        self.assertEqual(count_hermes_btc_open(positions, 909002), 1)

    def test_count_multiple_btc(self) -> None:
        positions = [_btc_pos(ticket=1), _btc_pos(ticket=2), _btc_pos(ticket=3)]
        self.assertEqual(count_hermes_btc_open(positions, 909002), 3)


# ══════════════════════════════════════════════════════════════════════════════
# C. evaluate_rescue() rule logic
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateRescueRule1(unittest.TestCase):
    """Rule 1: NEGATIVE_THEN_SMALL_POSITIVE (armed + recovered)."""

    def _cfg(self) -> SmartRescueConfig:
        return SmartRescueConfig()

    def test_armed_at_minus030_closes_at_plus008(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        # Tick 1: profit = -0.30 → arms rescue
        pos1 = _btc_pos(profit=-0.30)
        result1 = evaluate_rescue(pos1, states, cfg, 1, now)
        self.assertIsNotNone(result1)
        self.assertEqual(result1["action"], "ARMED")
        # Tick 2: profit = +0.08 → rescue close
        pos2 = _btc_pos(profit=0.08)
        result2 = evaluate_rescue(pos2, states, cfg, 1, now)
        self.assertIsNotNone(result2)
        self.assertEqual(result2["action"], "RESCUE_CLOSE")
        self.assertEqual(result2["reason"], "NEGATIVE_THEN_SMALL_POSITIVE")

    def test_armed_at_minus020_closes_at_plus008(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        # Exactly at the arm threshold
        evaluate_rescue(_btc_pos(profit=-0.20), states, cfg, 1, now)
        result = evaluate_rescue(_btc_pos(profit=0.08), states, cfg, 1, now)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "RESCUE_CLOSE")
        self.assertEqual(result["reason"], "NEGATIVE_THEN_SMALL_POSITIVE")

    def test_plus007_does_not_close(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        evaluate_rescue(_btc_pos(profit=-0.25), states, cfg, 1, now)
        result = evaluate_rescue(_btc_pos(profit=0.07), states, cfg, 1, now)
        # profit 0.07 < rescue_min_profit_usd 0.08
        self.assertIsNone(result)

    def test_never_armed_positive_does_not_trigger_rule1(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        # Position never goes negative
        result = evaluate_rescue(_btc_pos(profit=0.10), states, cfg, 1, now)
        self.assertIsNone(result)

    def test_armed_flag_persists_across_ticks(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        evaluate_rescue(_btc_pos(profit=-0.30), states, cfg, 1, now)  # ARMED
        evaluate_rescue(_btc_pos(profit=-0.05), states, cfg, 1, now)  # no action yet
        result = evaluate_rescue(_btc_pos(profit=0.10), states, cfg, 1, now)  # close
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "RESCUE_CLOSE")

    def test_min_seen_profit_tracked(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = _now()
        evaluate_rescue(_btc_pos(profit=-0.10), states, cfg, 1, now)
        evaluate_rescue(_btc_pos(profit=-0.40), states, cfg, 1, now)
        evaluate_rescue(_btc_pos(profit=-0.15), states, cfg, 1, now)
        result = evaluate_rescue(_btc_pos(profit=0.10), states, cfg, 1, now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["min_seen_profit"], -0.40, places=5)


class TestEvaluateRescueRule2(unittest.TestCase):
    """Rule 2: EMERGENCY_MULTIPLE_POSITIONS."""

    def _cfg(self) -> SmartRescueConfig:
        return SmartRescueConfig()

    def test_emergency_mode_open_count_165_profit_plus008_closes(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        result = evaluate_rescue(_btc_pos(profit=0.08), states, cfg, open_btc_count=165)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "RESCUE_CLOSE")
        self.assertEqual(result["reason"], "EMERGENCY_MULTIPLE_POSITIONS")
        self.assertEqual(result["open_count"], 165)

    def test_emergency_mode_open_count_165_profit_negative_does_not_close(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        result = evaluate_rescue(_btc_pos(profit=-0.10), states, cfg, open_btc_count=165)
        # -0.10 < 0.08 emergency threshold, and not armed yet, so None
        self.assertIsNone(result)

    def test_open_count_1_does_not_trigger_emergency(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        result = evaluate_rescue(_btc_pos(profit=0.50), states, cfg, open_btc_count=1)
        # open_btc_count must be > 1 for emergency
        self.assertIsNone(result)

    def test_old_existing_position_unknown_drawdown_closes_at_plus008_when_multiple_open(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        # Position arrives with no history (first sighting, profit slightly positive)
        # open_btc_count > 1 triggers emergency
        result = evaluate_rescue(_btc_pos(profit=0.09), states, cfg, open_btc_count=2)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "EMERGENCY_MULTIPLE_POSITIONS")


class TestEvaluateRescueRule3(unittest.TestCase):
    """Rule 3: AGE_EXCEEDED."""

    def _cfg(self) -> SmartRescueConfig:
        return SmartRescueConfig(max_hold_seconds=180)

    def test_old_position_plus008_closes(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = datetime.now(timezone.utc)
        # Position opened 200 seconds ago
        old_time = int(now.timestamp()) - 200
        pos = _btc_pos(profit=0.08, pos_time=old_time)
        result = evaluate_rescue(pos, states, cfg, open_btc_count=1, now=now)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "AGE_EXCEEDED")

    def test_young_position_plus008_does_not_close_via_age(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = datetime.now(timezone.utc)
        young_time = int(now.timestamp()) - 30   # only 30 seconds old
        pos = _btc_pos(profit=0.08, pos_time=young_time)
        result = evaluate_rescue(pos, states, cfg, open_btc_count=1, now=now)
        self.assertIsNone(result)

    def test_old_position_negative_does_not_trigger_age_rule(self) -> None:
        states: dict = {}
        cfg = self._cfg()
        now = datetime.now(timezone.utc)
        old_time = int(now.timestamp()) - 300
        pos = _btc_pos(profit=-0.05, pos_time=old_time)
        result = evaluate_rescue(pos, states, cfg, open_btc_count=1, now=now)
        # -0.05 does not arm rescue and profit < 0.08 min, age rule won't fire
        self.assertIsNone(result)


# ══════════════════════════════════════════════════════════════════════════════
# D. DemoKellyRouter.process_quick_exits() rescue integration
# ══════════════════════════════════════════════════════════════════════════════

def _make_mt5_stub() -> MagicMock:
    """Return a MagicMock that looks like the MetaTrader5 module."""
    stub = MagicMock()
    stub.positions_get.return_value = []
    stub.symbol_info_tick.return_value = None
    stub.symbol_info.return_value = None
    stub.order_send.return_value = None
    stub.POSITION_TYPE_BUY  = 0
    stub.POSITION_TYPE_SELL = 1
    stub.ORDER_TYPE_BUY     = 0
    stub.ORDER_TYPE_SELL    = 1
    stub.TRADE_ACTION_DEAL  = 1
    stub.ORDER_FILLING_IOC  = 1
    return stub


def _make_settings(**overrides):
    from app.config import Settings
    defaults = dict(
        demo_trading=True,
        demo_only=True,
        allow_live_trading=False,
        demo_pilot_enabled=True,
        quick_exit_enabled=True,
        quick_exit_demo_only=True,
        demo_magic_number=909002,
        old_btc_smart_quick_exit_enabled=True,
        old_btc_rescue_min_profit_usd=0.08,
        old_btc_rescue_arm_drawdown_usd=-0.20,
        old_btc_emergency_any_positive_exit_when_open_count_gt=1,
        old_btc_emergency_any_positive_exit_usd=0.08,
        old_btc_rescue_max_hold_seconds=180,
        old_btc_protect_existing_positions=True,
        old_btc_max_open_positions=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_router(settings=None):
    # Ensure MetaTrader5 stub exists before importing demo_router
    if "MetaTrader5" not in sys.modules:
        sys.modules["MetaTrader5"] = _make_mt5_stub()
    from app.mt5.demo_router import DemoKellyRouter
    s = settings or _make_settings()
    events_path = Path(".") / "demo_pilot_events.jsonl"
    return DemoKellyRouter(s, events_path=events_path)


# Ensure demo_router is importable with a stub
if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = _make_mt5_stub()
import app.mt5.demo_router as _dr_module  # noqa: E402


class TestRouterRescueIntegration(unittest.TestCase):
    """Integration tests: patch app.mt5.demo_router.mt5 directly (avoids rebinding issues)."""

    def _demo_account(self) -> dict:
        return {"account_type": "DEMO", "login": 12345, "trade_allowed": True,
                "trade_expert": True, "trade_mode": 0}

    def _run_quick_exits(self, router, positions, settings_overrides=None):
        stub = _make_mt5_stub()
        stub.positions_get.return_value = positions
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_record_event"):
                    return router.process_quick_exits(
                        account=self._demo_account(), mt5_connected=True
                    ), stub

    def test_rescue_never_arms_on_exit_v2_symbol(self) -> None:
        # GRAND_PLAN 2026-07-08 : Exit V2 est l'autorité de sortie UNIQUE des
        # symboles officiels (GOLD#, BTCUSD#). Le Smart Rescue ne doit plus
        # jamais armer un état sur une position BTC — elle appartient à Exit V2.
        router = _make_router()
        pos = _btc_pos(profit=-0.30)
        self._run_quick_exits(router, [pos])
        self.assertEqual(router._rescue_states, {})

    def test_rescue_close_never_called_on_exit_v2_symbol(self) -> None:
        # Même pré-armé (état résiduel d'avant le pivot), le rescue ne ferme
        # plus une position BTC : Exit V2 est l'autorité unique.
        router = _make_router()
        router._rescue_states[100001] = {
            "ticket": 100001, "symbol": "BTCUSD#", "magic": 909002, "comment": "HERMES",
            "opened_at": 0, "min_seen_profit": -0.30, "max_seen_profit": -0.30,
            "was_negative": True, "rescue_armed": True, "last_seen_profit": -0.30,
        }
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_btc_pos(profit=0.10)]
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_rescue_close") as mock_close:
                    with patch.object(router, "_record_event"):
                        router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        mock_close.assert_not_called()

    def test_non_hermes_btc_position_skipped(self) -> None:
        router = _make_router()
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_other_pos(symbol="EURUSD", magic=909002, profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_rescue_close") as mock_close:
                    with patch.object(router, "_record_event"):
                        router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        mock_close.assert_not_called()

    def test_other_symbol_skipped(self) -> None:
        router = _make_router()
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_other_pos(symbol="GOLD#", magic=909002, profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_rescue_close") as mock_close:
                    with patch.object(router, "_record_event"):
                        router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        mock_close.assert_not_called()

    def test_live_account_blocks_rescue(self) -> None:
        router = _make_router(settings=_make_settings(allow_live_trading=True))
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_btc_pos(profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            result = router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        self.assertEqual(result, [])

    def test_demo_only_false_blocks_rescue(self) -> None:
        router = _make_router(settings=_make_settings(demo_only=False))
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_btc_pos(profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            result = router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        self.assertEqual(result, [])

    def test_non_demo_account_type_blocks_rescue(self) -> None:
        router = _make_router()
        live_acct = {"account_type": "LIVE", "login": 99, "trade_allowed": True,
                     "trade_expert": True, "trade_mode": 0}
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_btc_pos(profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=live_acct):
                with patch.object(router, "_rescue_close") as mock_close:
                    router.process_quick_exits(account=live_acct, mt5_connected=True)
        mock_close.assert_not_called()

    def test_mt5_not_connected_blocks_rescue(self) -> None:
        router = _make_router()
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [_btc_pos(profit=0.50)]
        with patch.object(_dr_module, "mt5", stub):
            result = router.process_quick_exits(account=self._demo_account(), mt5_connected=False)
        self.assertEqual(result, [])

    def test_rescue_states_pruned_when_position_closes(self) -> None:
        router = _make_router()
        router._rescue_states[999] = {"ticket": 999, "rescue_armed": True}
        stub = _make_mt5_stub()
        stub.positions_get.return_value = []  # position 999 gone
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_record_event"):
                    router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        self.assertNotIn(999, router._rescue_states)

    def test_emergency_multiple_open_no_longer_closes_exit_v2_symbols(self) -> None:
        # GRAND_PLAN 2026-07-08 : même à 2 positions BTC ouvertes, le rescue
        # d'urgence ne ferme plus — Exit V2 est l'autorité unique des symboles
        # officiels (et MAX_OPEN=1 par symbole empêche ce scénario en amont).
        router = _make_router()
        pos1 = _btc_pos(ticket=300001, profit=0.09)
        pos2 = _btc_pos(ticket=300002, profit=0.09)
        stub = _make_mt5_stub()
        stub.positions_get.return_value = [pos1, pos2]
        with patch.object(_dr_module, "mt5", stub):
            with patch.object(router, "account_diagnostics", return_value=self._demo_account()):
                with patch.object(router, "_rescue_close") as mock_close:
                    with patch.object(router, "_record_event"):
                        router.process_quick_exits(account=self._demo_account(), mt5_connected=True)
        mock_close.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# E. Open position guard
# ══════════════════════════════════════════════════════════════════════════════

class TestOpenPositionGuardEvaluate(unittest.TestCase):
    """Guard inside DemoKellyRouter.evaluate() blocks new entries when position open."""

    def _make_decision(self) -> dict:
        return {
            "strategy": "BTC_SCALPING_AGENT",
            "symbol": "BTCUSD#",
            "signal": "BUY",
            "direction": "BUY",
            "entry": 65000.0,
            "sl": 64000.0,
            "tp": 67000.0,
            "old_btc_mode": "DEMO_ADAPTIVE_FALLBACK",
        }

    def _evaluate(self, positions: list, settings=None):
        router = _make_router(settings=settings or _make_settings(old_btc_max_open_positions=1))
        stub = _make_mt5_stub()
        stub.positions_get.return_value = positions
        with patch.object(_dr_module, "mt5", stub):
            return router.evaluate(
                decision=self._make_decision(),
                kelly_risk=None,
                account={"account_type": "DEMO", "login": 1},
                broker_symbol="BTCUSD#",
                frames=None,
                tick=None,
                symbol_specs=None,
                spread=0.0,
                max_spread=100.0,
                mt5_connected=True,
            )

    def test_new_entry_blocked_when_one_position_already_open(self) -> None:
        existing_pos = _btc_pos(ticket=77001, profit=0.05)
        result = self._evaluate([existing_pos])
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "HERMES_BTC_POSITION_ALREADY_OPEN")

    def test_new_entry_not_blocked_by_guard_when_no_open_positions(self) -> None:
        result = self._evaluate([])
        # Guard does not fire — reason should NOT be HERMES_BTC_POSITION_ALREADY_OPEN
        self.assertNotEqual(result.reason, "HERMES_BTC_POSITION_ALREADY_OPEN")


class TestNewEntriesBlockedByCount(unittest.TestCase):
    """count_hermes_btc_open with open positions blocks new entries."""

    def test_count_hermes_btc_open_returns_correct_count(self) -> None:
        positions = [_btc_pos(ticket=1), _btc_pos(ticket=2), _other_pos(symbol="EURUSD")]
        self.assertEqual(count_hermes_btc_open(positions, 909002), 2)

    def test_count_zero_when_only_other_symbols(self) -> None:
        positions = [_other_pos(symbol="GOLD#"), _other_pos(symbol="EURUSD")]
        self.assertEqual(count_hermes_btc_open(positions, 909002), 0)


# ══════════════════════════════════════════════════════════════════════════════
# F. Safety invariants
# ══════════════════════════════════════════════════════════════════════════════

class TestOrderSendOnlyInDemoRouter(unittest.TestCase):
    """mt5.order_send must NOT appear in smart_rescue.py (only in demo_router.py)."""

    def test_smart_rescue_py_has_no_order_send(self) -> None:
        src = SMART_RESCUE_PATH.read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertEqual(hits, [], f"smart_rescue.py calls mt5.order_send: {hits}")

    def test_demo_router_is_the_only_order_send_location(self) -> None:
        # Verify demo_router.py still has order_send calls (it's allowed there)
        src = DEMO_ROUTER_PATH.read_text(encoding="utf-8")
        hits = re.findall(r'(?<!["\'])mt5\.order_send\s*\(', src)
        self.assertGreater(len(hits), 0, "demo_router.py should have mt5.order_send calls")

    def test_config_rescue_fields_present(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertTrue(hasattr(s, "old_btc_smart_quick_exit_enabled"))
        self.assertTrue(hasattr(s, "old_btc_rescue_min_profit_usd"))
        self.assertTrue(hasattr(s, "old_btc_rescue_arm_drawdown_usd"))
        self.assertTrue(hasattr(s, "old_btc_emergency_any_positive_exit_when_open_count_gt"))
        self.assertTrue(hasattr(s, "old_btc_emergency_any_positive_exit_usd"))
        self.assertTrue(hasattr(s, "old_btc_rescue_max_hold_seconds"))
        self.assertTrue(hasattr(s, "old_btc_protect_existing_positions"))
        self.assertTrue(hasattr(s, "old_btc_max_open_positions"))

    def test_config_rescue_defaults(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertTrue(s.old_btc_smart_quick_exit_enabled)
        self.assertAlmostEqual(s.old_btc_rescue_min_profit_usd, 0.08)
        self.assertAlmostEqual(s.old_btc_rescue_arm_drawdown_usd, -0.20)
        self.assertEqual(s.old_btc_emergency_any_positive_exit_when_open_count_gt, 1)
        self.assertAlmostEqual(s.old_btc_emergency_any_positive_exit_usd, 0.08)
        self.assertEqual(s.old_btc_rescue_max_hold_seconds, 180)
        self.assertTrue(s.old_btc_protect_existing_positions)
        self.assertEqual(s.old_btc_max_open_positions, 1)

    def test_allow_live_trading_always_false_in_settings(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.allow_live_trading)


if __name__ == "__main__":
    unittest.main()
