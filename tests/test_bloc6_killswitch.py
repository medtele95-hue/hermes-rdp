"""BLOC 6 — armored daily kill-switch tests.

Proves:
a) BROKER-day window [broker midnight, now+2h]: resets at 21:00 UTC and a
   deal stamped "in the future" (UTC+3 broker stamps) is never missed.
b) Nothing in memory: the count is re-read from the deals history at every
   evaluation and therefore survives restarts (restart x2 test).
c) Quotas per policy: DEMO 6 losses/day + 3% DD (first one stops);
   REAL_DECLARED 3/day; REAL_UNKNOWN 1/day.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter
from app.services.adaptive_account_policy import resolve_account_policy
from app.services.daily_killswitch import broker_day_window, evaluate_daily_killswitch

_NOW = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)
_MAGIC = 909002


def _policy(trade_mode: int = 0, **settings_kw):
    settings = Settings(**settings_kw)
    return resolve_account_policy({"trade_mode": trade_mode, "login": 1, "server": "s"}, settings), settings


def _deal(net: float, magic: int = _MAGIC, entry: int = 1):
    return SimpleNamespace(magic=magic, entry=entry, profit=net, commission=0.0, swap=0.0)


def _account(balance: float = 10000.0) -> dict:
    return {"balance": balance, "equity": balance, "trade_mode": 0}


class TestBrokerDayWindow(unittest.TestCase):
    def test_window_anchored_on_broker_midnight(self) -> None:
        start, end = broker_day_window(_NOW, 3.0)
        self.assertEqual(start, datetime(2026, 7, 6, 21, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, _NOW + timedelta(hours=2))

    def test_window_resets_at_21_utc(self) -> None:
        before = datetime(2026, 7, 6, 20, 59, 0, tzinfo=timezone.utc)
        after = datetime(2026, 7, 6, 21, 1, 0, tzinfo=timezone.utc)
        start_before, _ = broker_day_window(before, 3.0)
        start_after, _ = broker_day_window(after, 3.0)
        self.assertEqual(start_before, datetime(2026, 7, 5, 21, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(start_after, datetime(2026, 7, 6, 21, 0, 0, tzinfo=timezone.utc))

    def test_blind_spot_replay_future_broker_stamp_is_covered(self) -> None:
        # A deal whose broker stamp (UTC+3) lands 90 minutes "in the future"
        # relative to UTC must still be inside the requested window.
        policy, settings = _policy(trade_mode=2)  # REAL_UNKNOWN: 1 loss/day
        deal_stamp = _NOW + timedelta(minutes=90)

        def history(start, end):
            self.assertLessEqual(start, _NOW)
            # the +2h tail must cover the future-stamped deal
            self.assertGreaterEqual(end, deal_stamp)
            return [_deal(-5.0)]

        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=history
        )
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "DAILY_KILLSWITCH_MAX_LOSSES")


class TestStatelessCounter(unittest.TestCase):
    def test_restart_x2_count_survives(self) -> None:
        policy, settings = _policy(trade_mode=0)  # DEMO: 6/day
        deals = [_deal(-1.0) for _ in range(6)]
        history = lambda start, end: deals  # noqa: E731
        # "restart" = fresh evaluation with zero shared state, twice
        first = evaluate_daily_killswitch(policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=history)
        second = evaluate_daily_killswitch(policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=history)
        for result in (first, second):
            self.assertTrue(result["triggered"])
            self.assertEqual(result["losses_today"], 6)

    def test_magic_and_entry_filters(self) -> None:
        policy, settings = _policy(trade_mode=0)
        deals = [
            _deal(-1.0, magic=111111),   # foreign magic — ignored
            _deal(-1.0, entry=0),        # DEAL_ENTRY_IN — ignored
            _deal(-1.0),                 # counted
        ]
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=lambda s, e: deals
        )
        self.assertEqual(result["losses_today"], 1)
        self.assertFalse(result["triggered"])

    def test_history_unreadable_fails_closed(self) -> None:
        policy, settings = _policy(trade_mode=0)

        def broken(start, end):
            raise RuntimeError("terminal gone")

        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=broken
        )
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "DAILY_KILLSWITCH_HISTORY_UNREADABLE")


class TestPolicyQuotas(unittest.TestCase):
    def _run(self, trade_mode: int, losses: int, balance: float = 10000.0, loss_size: float = -1.0):
        policy, settings = _policy(trade_mode=trade_mode)
        deals = [_deal(loss_size) for _ in range(losses)]
        return evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(balance), now_utc=_NOW, history_fn=lambda s, e: deals
        )

    def test_demo_6_losses_per_day(self) -> None:
        self.assertFalse(self._run(0, 5)["triggered"])
        result = self._run(0, 6)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "DAILY_KILLSWITCH_MAX_LOSSES")

    def test_demo_3_percent_drawdown_first_one_stops(self) -> None:
        # a single -301 USD deal on a 10k account = 3.01% DD -> stops even
        # though the loss count (1) is far below the DEMO quota (6)
        result = self._run(0, 1, loss_size=-301.0)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "DAILY_KILLSWITCH_DRAWDOWN")

    def test_real_declared_3_per_day(self) -> None:
        policy, settings = _policy(
            trade_mode=2,
            real_declared_login="1",
            real_declared_server="s",
        )
        self.assertEqual(policy.level, "REAL_DECLARED")
        deals2 = [_deal(-1.0)] * 2
        deals3 = [_deal(-1.0)] * 3
        ok = evaluate_daily_killswitch(policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=lambda s, e: deals2)
        stop = evaluate_daily_killswitch(policy, settings, _MAGIC, account=_account(), now_utc=_NOW, history_fn=lambda s, e: deals3)
        self.assertFalse(ok["triggered"])
        self.assertTrue(stop["triggered"])

    def test_real_unknown_1_per_day(self) -> None:
        result = self._run(2, 1)
        self.assertTrue(result["triggered"])

    def test_account_policy_tagged(self) -> None:
        self.assertEqual(self._run(0, 0)["account_policy"], "DEMO")
        self.assertEqual(self._run(2, 0)["account_policy"], "REAL_UNKNOWN")


class TestRouterIntegration(unittest.TestCase):
    def test_router_blocks_order_when_killswitch_triggered(self) -> None:
        from test_paper_learning_safety import demo_settings

        events = Path("tests") / "__tmp_bloc6_events" / "ks.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        if events.exists():
            events.unlink()
        router = DemoKellyRouter(demo_settings(), events_path=events)
        decision = {
            "symbol": "EURUSD",
            "strategy": "SIMO_ATM_BREAKOUT",
            "signal": "BUY",
            "entry": 1.1000,
            "sl": 1.0990,
            "tp": 1.1020,
            "reward_risk": 2.0,
            "risk_status": "APPROVED",
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "big_setup_grade": "B",
            "edge_score": 100,
            "setup_score": 100,
            "setup_hunter_score": 100,
            "final_confluence_score": 75,
            "final_confluence_grade": "B",
        }
        account = {
            "login": 345297734,
            "trade_mode": 0,
            "trade_allowed": True,
            "trade_expert": True,
            "balance": 10000.0,
            "equity": 10000.0,
        }
        losing_deals = [_deal(-1.0) for _ in range(6)]
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=1.1, ask=1.10001)),
            patch("app.mt5.demo_router.mt5.order_check", return_value=SimpleNamespace(retcode=10009, comment="Done")),
            patch("app.services.daily_killswitch._mt5_history_deals", return_value=losing_deals),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            result = router.evaluate(
                decision, {"approved_lot": 0.01}, account, "EURUSD", {},
                {"bid": 1.1, "ask": 1.10001},
                {"tick_value": 1.0, "tick_size": 0.0001, "volume_step": 0.01},
                1, 30, True, "setup-ks", datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            )
        send.assert_not_called()
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "DAILY_KILLSWITCH_MAX_LOSSES")
        self.assertTrue(result.event["daily_killswitch"]["triggered"])
        self.assertEqual(result.event["daily_killswitch"]["losses_today"], 6)


if __name__ == "__main__":
    unittest.main()
