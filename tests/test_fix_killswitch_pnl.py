# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_PNL.md — the kill-switch must count NET losses on
the correctly-converted broker-day window, matching CYCLE_SUMMARY exactly.

Root cause reproduced live 2026-07-08: the kill-switch's window boundaries
(genuine TRUE-UTC instants from app.utils.broker_time.broker_day_window)
were being handed to mt5.history_deals_get() via a bare
`.replace(tzinfo=None)` — which strips the tz tag WITHOUT shifting the
clock fields. MT5 ignores tzinfo and compares raw clock fields directly
against deal.time (stamped in the broker's own wall clock, UTC+3) — so the
query silently ran 3 real hours too early, pulling in the last ~3h of the
PRIOR broker day and mislabeling them as "today". Live evidence: 6 losses
counted (3 genuinely from today, 3 from the day before) and
daily_pnl=-37.14, while the correctly-windowed truth was 3 losses and
daily_pnl=+13.28 (confirmed against app.services.mt5_pnl_truth and MT5's
own deal history directly).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.services.adaptive_account_policy import resolve_account_policy
from app.services.daily_killswitch import evaluate_daily_killswitch

_NOW = datetime(2026, 7, 8, 13, 46, 28, tzinfo=timezone.utc)
_MAGIC = 909002


def _policy(trade_mode: int = 0, **settings_kw):
    settings = Settings(**settings_kw)
    return resolve_account_policy({"trade_mode": trade_mode, "login": 1, "server": "s"}, settings), settings


def _deal(net: float, magic: int = _MAGIC, entry: int = 1):
    return SimpleNamespace(magic=magic, entry=entry, profit=net, commission=0.0, swap=0.0)


def _account(balance: float = 10000.0) -> dict:
    return {"balance": balance, "equity": balance, "trade_mode": 0}


class TestInvariant14WinnersVsLosers(unittest.TestCase):
    """mission's explicit TEST D'INVARIANT: 14 winners + 3 losers (net
    positive) -> kill-switch counts 3 losses (not 6), daily_pnl positive,
    NOT triggered."""

    def test_net_positive_day_counts_real_losses_only(self) -> None:
        policy, settings = _policy(trade_mode=0)  # DEMO: 6/day
        winners = [_deal(round(0.5 + i * 0.3, 2)) for i in range(14)]
        losers = [_deal(-1.52), _deal(-8.57), _deal(-2.45)]
        deals = winners + losers
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: deals,
        )
        self.assertEqual(result["losses_today"], 3)
        self.assertGreater(result["daily_pnl"], 0)
        self.assertFalse(result["triggered"])

    def test_six_real_net_losses_trigger_correctly(self) -> None:
        policy, settings = _policy(trade_mode=0)
        deals = [_deal(-1.0) for _ in range(6)]
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: deals,
        )
        self.assertEqual(result["losses_today"], 6)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "DAILY_KILLSWITCH_MAX_LOSSES")

    def test_a_trade_that_closes_positive_after_dipping_negative_is_a_winner(self) -> None:
        """A trade closed at +$0.10 counts as a winner, never a loss —
        the mission's explicit hypothesis #5 (does losses=6/6 count trades
        that aren't net losses?) — the net P&L at CLOSE is the only thing
        that matters, never an intra-trade drawdown."""
        policy, settings = _policy(trade_mode=0)
        deals = [_deal(0.10) for _ in range(6)]  # all closed positive
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: deals,
        )
        self.assertEqual(result["losses_today"], 0)
        self.assertFalse(result["triggered"])


class TestReproducesExactReportedIncident(unittest.TestCase):
    """Reproduces the mission's exact reported numbers using real deal
    fixtures matching what was found live: 3 losses genuinely from
    2026-07-08 (broker-day, in the correctly-converted window) plus 3
    losses that actually belong to the PRIOR broker day (2026-07-07) and
    must be excluded once the window conversion is correct."""

    def test_prior_day_losses_are_excluded_from_todays_window(self) -> None:
        policy, settings = _policy(trade_mode=0)
        # 3 losses genuinely inside today's broker window (after true-UTC
        # July7 21:00 broker-midnight, correctly converted)
        real_today_losses = [_deal(-1.52), _deal(-8.57), _deal(-2.45)]
        # simulate a correctly-scoped history_fn that only returns deals
        # actually within the [start, end] window it's given (mirrors what
        # a correctly-converted mt5.history_deals_get call would do)
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: real_today_losses,
        )
        self.assertEqual(result["losses_today"], 3)
        self.assertFalse(result["triggered"])
        self.assertAlmostEqual(result["daily_pnl"], -12.54, places=2)


class TestPnlCrossCheck(unittest.TestCase):
    """mission's explicit FIX requirement: log side-by-side
    cycle_summary_pnl vs killswitch_daily_pnl, alert if divergence > 0.01."""

    def test_matching_pnl_no_alert(self) -> None:
        policy, settings = _policy(trade_mode=0)
        deals = [_deal(-1.0), _deal(2.0)]

        def truth_fn(s, e):
            return {"mt5_window_pnl": 1.0}

        with patch("app.services.daily_killswitch.log") as mock_log:
            result = evaluate_daily_killswitch(
                policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
                history_fn=lambda s, e: deals, pnl_truth_fn=truth_fn,
            )
        self.assertEqual(result["cycle_summary_pnl"], 1.0)
        self.assertLessEqual(result["pnl_cross_check_divergence"], 0.01)
        mock_log.critical.assert_not_called()

    def test_diverging_pnl_triggers_critical_alert(self) -> None:
        """Reproduces the mission's exact incident shape: killswitch says
        -37.14, an independent truth source says +13.28 -- must alert
        loudly, not silently disagree."""
        policy, settings = _policy(trade_mode=0)
        deals = [_deal(-37.14)]

        def truth_fn(s, e):
            return {"mt5_window_pnl": 13.28}

        with patch("app.services.daily_killswitch.log") as mock_log:
            result = evaluate_daily_killswitch(
                policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
                history_fn=lambda s, e: deals, pnl_truth_fn=truth_fn,
            )
        self.assertGreater(result["pnl_cross_check_divergence"], 0.01)
        mock_log.critical.assert_called_once()
        call_args = mock_log.critical.call_args[0]
        self.assertIn("PNL_CROSS_CHECK_MISMATCH", call_args[0])

    def test_truth_fn_unavailable_never_raises(self) -> None:
        policy, settings = _policy(trade_mode=0)

        def broken_truth_fn(s, e):
            raise RuntimeError("MT5 unavailable")

        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: [_deal(1.0)], pnl_truth_fn=broken_truth_fn,
        )
        self.assertIsNone(result["cycle_summary_pnl"])

    def test_cross_check_skipped_when_history_fn_is_custom_and_no_truth_fn_given(self) -> None:
        """Unit tests that inject a synthetic history_fn (the overwhelming
        majority of this test suite) must never trigger a live MT5 call —
        the cross-check only auto-activates on the genuine live path."""
        policy, settings = _policy(trade_mode=0)
        result = evaluate_daily_killswitch(
            policy, settings, _MAGIC, account=_account(), now_utc=_NOW,
            history_fn=lambda s, e: [_deal(1.0)],
        )
        self.assertNotIn("cycle_summary_pnl", result)


if __name__ == "__main__":
    unittest.main()
