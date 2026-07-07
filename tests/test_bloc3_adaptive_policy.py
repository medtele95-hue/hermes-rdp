"""BLOC 3 — ADAPTIVE_ACCOUNT_POLICY tests.

Three levels via MT5 trade_mode, applied at BOOT and PER-ORDER; fail-closed
to REAL_UNKNOWN; ACCOUNT_PROFILE built from live specs with zero hardcode.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter
from app.services.adaptive_account_policy import (
    LEVEL_DEMO,
    LEVEL_REAL_DECLARED,
    LEVEL_REAL_UNKNOWN,
    build_account_profile,
    resolve_account_policy,
    trading_authorized,
)


def _demo_account(**overrides) -> dict:
    account = {
        "login": 345297734,
        "server": "XMGlobal-MT5 10",
        "trade_mode": 0,
        "balance": 10000.0,
        "equity": 10000.0,
        "currency": "USD",
        "trade_allowed": True,
        "trade_expert": True,
    }
    account.update(overrides)
    return account


class TestPolicyResolution(unittest.TestCase):
    def test_demo_account_full_policy_no_pin(self) -> None:
        policy = resolve_account_policy(_demo_account(), Settings())
        self.assertEqual(policy.level, LEVEL_DEMO)
        self.assertTrue(policy.exploration_executable)
        self.assertIsNone(policy.risk_cap_percent)
        self.assertFalse(policy.enforce_volume_min)
        self.assertEqual(policy.max_losses_per_day, 6)

    def test_real_declared_requires_login_and_server(self) -> None:
        settings = Settings(real_declared_login="111222", real_declared_server="Broker-Live")
        account = _demo_account(trade_mode=2, login=111222, server="Broker-Live")
        policy = resolve_account_policy(account, settings)
        self.assertEqual(policy.level, LEVEL_REAL_DECLARED)
        self.assertFalse(policy.exploration_executable)
        self.assertEqual(policy.max_losses_per_day, 3)

    def test_real_login_mismatch_is_unknown(self) -> None:
        settings = Settings(real_declared_login="111222", real_declared_server="Broker-Live")
        account = _demo_account(trade_mode=2, login=999999, server="Broker-Live")
        policy = resolve_account_policy(account, settings)
        self.assertEqual(policy.level, LEVEL_REAL_UNKNOWN)

    def test_real_server_mismatch_is_unknown(self) -> None:
        settings = Settings(real_declared_login="111222", real_declared_server="Broker-Live")
        account = _demo_account(trade_mode=2, login=111222, server="Other-Server")
        self.assertEqual(resolve_account_policy(account, settings).level, LEVEL_REAL_UNKNOWN)

    def test_real_unknown_is_ultra_cautious(self) -> None:
        policy = resolve_account_policy(_demo_account(trade_mode=2), Settings())
        self.assertEqual(policy.level, LEVEL_REAL_UNKNOWN)
        self.assertEqual(policy.risk_cap_percent, 2.0)
        self.assertTrue(policy.enforce_volume_min)
        self.assertEqual(policy.max_losses_per_day, 1)
        self.assertEqual(policy.min_confluence, 80.0)

    def test_unreadable_trade_mode_fails_closed(self) -> None:
        self.assertEqual(resolve_account_policy(None, Settings()).level, LEVEL_REAL_UNKNOWN)
        self.assertEqual(
            resolve_account_policy(_demo_account(trade_mode=None), Settings()).level,
            LEVEL_REAL_UNKNOWN,
        )
        self.assertEqual(
            resolve_account_policy(_demo_account(trade_mode="garbage"), Settings()).level,
            LEVEL_REAL_UNKNOWN,
        )

    def test_contest_fails_closed_unless_allowed(self) -> None:
        self.assertEqual(
            resolve_account_policy(_demo_account(trade_mode=1), Settings()).level,
            LEVEL_REAL_UNKNOWN,
        )
        self.assertEqual(
            resolve_account_policy(_demo_account(trade_mode=1), Settings(demo_allow_contest=True)).level,
            LEVEL_DEMO,
        )


class TestOrderAuthorization(unittest.TestCase):
    def test_demo_authorized(self) -> None:
        ok, reason, policy = trading_authorized(_demo_account(), Settings())
        self.assertTrue(ok)
        self.assertEqual(reason, "ORDER_AUTH_DEMO_OK")
        self.assertEqual(policy.level, LEVEL_DEMO)

    def test_unreadable_account_blocked(self) -> None:
        ok, reason, policy = trading_authorized(None, Settings())
        self.assertFalse(ok)
        self.assertEqual(reason, "ORDER_AUTH_ACCOUNT_UNREADABLE")
        self.assertEqual(policy.level, LEVEL_REAL_UNKNOWN)

    def test_real_blocked_without_master_live_switch(self) -> None:
        ok, reason, policy = trading_authorized(_demo_account(trade_mode=2), Settings())
        self.assertFalse(ok)
        self.assertEqual(reason, "ORDER_AUTH_LIVE_DISABLED_FAIL_CLOSED")

    def test_real_declared_authorized_with_live_switch(self) -> None:
        settings = Settings(
            allow_live_trading=True,
            real_declared_login="111222",
            real_declared_server="Broker-Live",
        )
        account = _demo_account(trade_mode=2, login=111222, server="Broker-Live")
        ok, reason, policy = trading_authorized(account, settings)
        self.assertTrue(ok)
        self.assertEqual(policy.level, LEVEL_REAL_DECLARED)

    def test_real_unknown_authorized_ultra_cautious_with_live_switch(self) -> None:
        ok, reason, policy = trading_authorized(_demo_account(trade_mode=2), Settings(allow_live_trading=True))
        self.assertTrue(ok)
        self.assertEqual(reason, "ORDER_AUTH_REAL_UNKNOWN_ULTRA_CAUTIOUS")
        self.assertEqual(policy.level, LEVEL_REAL_UNKNOWN)


class TestAccountProfile(unittest.TestCase):
    def _symbol_info(self, name: str):
        if name == "DEAD":
            return None
        return SimpleNamespace(
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_tick_value=1.0,
            trade_tick_size=0.01,
            trade_stops_level=30,
            spread=25,
            point=0.01,
            trade_mode=4,
        )

    def test_profile_reads_live_specs_and_funds_sl(self) -> None:
        policy = resolve_account_policy(_demo_account(trade_mode=2), Settings())  # cap 2%
        profile = build_account_profile(
            _demo_account(trade_mode=2),
            {"GOLD#": "GOLD#", "DEAD": "DEAD"},
            policy,
            symbol_info_fn=self._symbol_info,
        )
        gold = profile["symbols"]["GOLD#"]
        self.assertTrue(gold["tradable"])
        self.assertEqual(gold["volume_min"], 0.01)
        self.assertAlmostEqual(gold["max_fundable_sl_usd"], 200.0)  # 2% of 10k equity
        # value/unit = 1.0/0.01*0.01 = 1 USD per price unit -> distance 200
        self.assertAlmostEqual(gold["max_fundable_sl_distance"], 200.0)
        self.assertFalse(profile["symbols"]["DEAD"]["tradable"])
        self.assertEqual(profile["policy_level"], LEVEL_REAL_UNKNOWN)

    def test_demo_profile_uses_full_funding(self) -> None:
        policy = resolve_account_policy(_demo_account(), Settings())
        profile = build_account_profile(
            _demo_account(), ["GOLD#"], policy, symbol_info_fn=self._symbol_info
        )
        self.assertAlmostEqual(profile["symbols"]["GOLD#"]["max_fundable_sl_usd"], 10000.0)


class TestRouterWiring(unittest.TestCase):
    def _router(self, tmp_name: str) -> DemoKellyRouter:
        settings = Settings(demo_trading=True, demo_only=True, demo_pilot_enabled=True)
        events = Path("tests") / "__tmp_bloc3_events" / f"{tmp_name}.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        if events.exists():
            events.unlink()
        return DemoKellyRouter(settings, events_path=events)

    def _decision(self) -> dict:
        return {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "signal": "BUY",
            "symbol": "GOLD#",
            "entry": 3300.0,
            "sl": 3290.0,
            "tp": 3320.0,
        }

    def test_live_account_order_blocked_per_order(self) -> None:
        router = self._router("live_block")
        items = router.process_decision(
            self._decision(),
            {"approved_lot": 0.01},
            _demo_account(trade_mode=2),
            "GOLD#",
            {},
            {"bid": 3300.0, "ask": 3300.2},
            {},
            1,
            30,
            True,
            "setup-auth",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["data"]["reason"], "ORDER_AUTH_LIVE_DISABLED_FAIL_CLOSED")
        self.assertEqual(items[0]["data"]["account_policy"], LEVEL_REAL_UNKNOWN)

    def test_unreadable_account_order_blocked(self) -> None:
        router = self._router("unreadable_block")
        items = router.process_decision(
            self._decision(),
            {"approved_lot": 0.01},
            None,
            "GOLD#",
            {},
            {"bid": 3300.0, "ask": 3300.2},
            {},
            1,
            30,
            True,
            "setup-auth-2",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["data"]["reason"], "ORDER_AUTH_ACCOUNT_UNREADABLE")

    def test_demo_login_pin_removed_for_demo_accounts(self) -> None:
        settings = Settings(demo_trading=True, demo_allowed_login="999999")
        router = DemoKellyRouter(settings, events_path=Path("tests") / "__tmp_bloc3_events" / "pin.jsonl")
        diag = router.account_diagnostics(_demo_account(login=345297734))
        self.assertIsNone(diag["block_reason"])

    def test_login_pin_still_applies_to_non_demo(self) -> None:
        settings = Settings(demo_trading=True, demo_allowed_login="999999")
        router = DemoKellyRouter(settings, events_path=Path("tests") / "__tmp_bloc3_events" / "pin2.jsonl")
        diag = router.account_diagnostics(_demo_account(trade_mode=2, login=345297734))
        self.assertIn(diag["block_reason"], {"ACCOUNT_TRADE_MODE_REAL"})


if __name__ == "__main__":
    unittest.main()
