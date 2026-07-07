"""BLOC 7 — protected calendar tests (weekend flat + news shield).

All rules in explicit UTC. Proves:
a) no entry after Friday 18:00 UTC; everything closed Friday 20:30 UTC.
b) post-weekend blackout Sunday -> Monday 03:00 UTC.
c) ForexFactory weekly feed with local cache, FAIL-SAFE on dead feed,
   HIGH USD +-10min entry blackout, non-armed positions pre-closed before
   majors while armed positions keep their lock, calendar->broker timezone.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.mt5.demo_router import DemoKellyRouter
from app.services.protected_calendar import (
    NewsCalendar,
    to_broker_time,
    weekend_entry_block,
    weekend_flat_close_due,
)


def _utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestWeekendRules(unittest.TestCase):
    # 2026-07-03 is a Friday
    def test_no_entry_after_friday_18_utc(self) -> None:
        self.assertIsNone(weekend_entry_block(_utc(2026, 7, 3, 17, 59)))
        self.assertEqual(weekend_entry_block(_utc(2026, 7, 3, 18, 0)), "WEEKEND_FLAT_NO_ENTRY")
        self.assertEqual(weekend_entry_block(_utc(2026, 7, 4, 12, 0)), "WEEKEND_FLAT_NO_ENTRY")

    def test_post_weekend_blackout_until_monday_03_utc(self) -> None:
        self.assertEqual(weekend_entry_block(_utc(2026, 7, 5, 12, 0)), "POST_WEEKEND_BLACKOUT")
        self.assertEqual(weekend_entry_block(_utc(2026, 7, 6, 2, 59)), "POST_WEEKEND_BLACKOUT")
        self.assertIsNone(weekend_entry_block(_utc(2026, 7, 6, 3, 0)))
        self.assertIsNone(weekend_entry_block(_utc(2026, 7, 8, 12, 0)))

    def test_flat_close_due_friday_2030_utc(self) -> None:
        self.assertFalse(weekend_flat_close_due(_utc(2026, 7, 3, 20, 29)))
        self.assertTrue(weekend_flat_close_due(_utc(2026, 7, 3, 20, 30)))
        self.assertTrue(weekend_flat_close_due(_utc(2026, 7, 4, 6, 0)))
        self.assertFalse(weekend_flat_close_due(_utc(2026, 7, 6, 10, 0)))

    def test_calendar_to_broker_timezone(self) -> None:
        # feed stamp 08:30-04:00 == 12:30 UTC == 15:30 broker (UTC+3)
        feed_dt = datetime.fromisoformat("2026-07-07T08:30:00-04:00")
        broker = to_broker_time(feed_dt, 3.0)
        self.assertEqual(broker.hour, 15)
        self.assertEqual(broker.minute, 30)


def _feed_item(date: str, title: str = "CPI y/y", country: str = "USD", impact: str = "High") -> dict:
    return {"title": title, "country": country, "impact": impact, "date": date}


class TestNewsCalendar(unittest.TestCase):
    def _calendar(self, items: list[dict], tmp_name: str, fail: bool = False) -> NewsCalendar:
        cache = Path("tests") / "__tmp_bloc7_cache" / f"{tmp_name}.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            cache.unlink()

        def fetch(url):
            if fail:
                raise RuntimeError("feed down")
            return items

        return NewsCalendar(settings=None, cache_path=cache, fetch_fn=fetch)

    def test_high_usd_blackout_window(self) -> None:
        calendar = self._calendar([_feed_item("2026-07-07T08:30:00-04:00")], "blackout")
        calendar.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        # event at 12:30 UTC -> blackout inside [12:20, 12:40]
        self.assertIsNotNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 25), window_minutes=10))
        self.assertIsNotNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 39), window_minutes=10))
        self.assertIsNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 41), window_minutes=10))
        self.assertIsNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 9), window_minutes=10))

    def test_low_impact_or_non_usd_ignored(self) -> None:
        calendar = self._calendar(
            [
                _feed_item("2026-07-07T08:30:00-04:00", impact="Medium"),
                _feed_item("2026-07-07T08:30:00-04:00", country="EUR"),
            ],
            "ignored",
        )
        calendar.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        self.assertIsNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 30), window_minutes=10))

    def test_fail_safe_dead_feed_trading_continues(self) -> None:
        calendar = self._calendar([], "dead", fail=True)
        changed = calendar.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        self.assertFalse(changed)
        self.assertIsNone(calendar.news_blackout(_utc(2026, 7, 7, 12, 30), window_minutes=10))

    def test_failed_refresh_retries_hourly_not_every_call(self) -> None:
        calls = {"n": 0}

        def fetch(url):
            calls["n"] += 1
            raise RuntimeError("down")

        cache = Path("tests") / "__tmp_bloc7_cache" / "throttle.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            cache.unlink()
        calendar = NewsCalendar(settings=None, cache_path=cache, fetch_fn=fetch)
        base = _utc(2026, 7, 7, 9, 0)
        calendar.refresh(base)
        calendar.refresh(base + timedelta(minutes=5))
        calendar.refresh(base + timedelta(minutes=59))
        self.assertEqual(calls["n"], 1)
        calendar.refresh(base + timedelta(minutes=61))
        self.assertEqual(calls["n"], 2)

    def test_cache_survives_new_instance(self) -> None:
        cache = Path("tests") / "__tmp_bloc7_cache" / "persist.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            cache.unlink()
        first = NewsCalendar(settings=None, cache_path=cache, fetch_fn=lambda url: [_feed_item("2026-07-07T08:30:00-04:00")])
        first.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        second = NewsCalendar(settings=None, cache_path=cache, fetch_fn=lambda url: (_ for _ in ()).throw(RuntimeError))
        self.assertEqual(len(second.high_usd_events()), 1)
        # fetched_at restored from cache -> no refetch within 24h
        self.assertFalse(second.refresh(_utc(2026, 7, 7, 10, 0)))

    def test_major_preclose_window(self) -> None:
        calendar = self._calendar(
            [_feed_item("2026-07-07T08:30:00-04:00", title="Non-Farm Employment Change")],
            "major",
        )
        calendar.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        # event 12:30 UTC: pre-close window is [12:20, 12:30]
        self.assertIsNotNone(calendar.major_preclose_event(_utc(2026, 7, 7, 12, 22), window_minutes=10))
        self.assertIsNone(calendar.major_preclose_event(_utc(2026, 7, 7, 12, 35), window_minutes=10))
        self.assertIsNone(calendar.major_preclose_event(_utc(2026, 7, 7, 12, 5), window_minutes=10))

    def test_non_major_high_usd_not_precosed(self) -> None:
        calendar = self._calendar([_feed_item("2026-07-07T08:30:00-04:00", title="Retail Sales m/m")], "nonmajor")
        calendar.refresh(_utc(2026, 7, 7, 9, 0), force=True)
        self.assertIsNone(calendar.major_preclose_event(_utc(2026, 7, 7, 12, 25), window_minutes=10))


class TestRouterCalendarIntegration(unittest.TestCase):
    def _router(self, name: str) -> DemoKellyRouter:
        from test_paper_learning_safety import demo_settings

        events = Path("tests") / "__tmp_bloc7_events" / f"{name}.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        if events.exists():
            events.unlink()
        return DemoKellyRouter(demo_settings(), events_path=events)

    def _decision(self) -> dict:
        return {
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

    def _account(self) -> dict:
        return {
            "login": 345297734,
            "trade_mode": 0,
            "trade_allowed": True,
            "trade_expert": True,
            "balance": 10000.0,
            "equity": 10000.0,
        }

    def _evaluate(self, router: DemoKellyRouter, now: datetime):
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=1.1, ask=1.10001)),
            patch("app.mt5.demo_router.mt5.order_check", return_value=SimpleNamespace(retcode=10009, comment="Done")),
            patch("app.services.daily_killswitch._mt5_history_deals", return_value=[]),
        ):
            return router.evaluate(
                self._decision(), {"approved_lot": 0.01}, self._account(), "EURUSD", {},
                {"bid": 1.1, "ask": 1.10001},
                {"tick_value": 1.0, "tick_size": 0.0001, "volume_step": 0.01},
                1, 30, True, "setup-cal", now,
            )

    def test_friday_evening_entry_blocked(self) -> None:
        # 2026-06-05 is a Friday inside the demo pilot window
        result = self._evaluate(self._router("weekend"), _utc(2026, 6, 5, 19, 0))
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "WEEKEND_FLAT_NO_ENTRY")

    def test_sunday_entry_blocked_post_weekend(self) -> None:
        result = self._evaluate(self._router("sunday"), _utc(2026, 6, 7, 12, 0))
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "POST_WEEKEND_BLACKOUT")

    def test_news_blackout_blocks_entry(self) -> None:
        router = self._router("news")
        now = _utc(2026, 6, 1, 10, 0)  # Monday
        router.news_calendar._events = [
            {"title": "CPI y/y", "country": "USD", "impact": "High", "time_utc": now + timedelta(minutes=5)}
        ]
        result = self._evaluate(router, now)
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "NEWS_BLACKOUT")

    def test_weekend_flat_closes_all_positions(self) -> None:
        router = self._router("flatclose")
        pos = SimpleNamespace(
            ticket=555, symbol="GOLD#", profit=1.0, magic=909002, comment="HERMES",
            type=0, volume=0.01, price_open=3300.0, sl=3290.0, tp=0.0, time=0,
        )
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[pos]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=3300.0, ask=3300.2)),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01)),
            patch("app.mt5.demo_router.mt5.order_send", return_value=SimpleNamespace(retcode=10009, order=1, deal=2)) as send,
        ):
            items = router.process_quick_exits(account=self._account(), mt5_connected=True, now=_utc(2026, 6, 5, 20, 35))
        send.assert_called_once()
        self.assertTrue(any(item["data"].get("event_type") == "WEEKEND_FLAT" for item in items))

    def test_news_preclose_spares_armed_positions(self) -> None:
        router = self._router("preclose")
        now = _utc(2026, 6, 1, 10, 0)  # Monday
        router.news_calendar._events = [
            {"title": "FOMC Statement", "country": "USD", "impact": "High", "time_utc": now + timedelta(minutes=8)}
        ]
        armed = SimpleNamespace(
            ticket=701, symbol="GOLD#", profit=3.0, magic=909002, comment="HERMES",
            type=0, volume=0.01, price_open=3300.0, sl=3290.0, tp=0.0, time=0,
        )
        naked = SimpleNamespace(
            ticket=702, symbol="GOLD#", profit=0.5, magic=909002, comment="HERMES",
            type=0, volume=0.01, price_open=3300.0, sl=3290.0, tp=0.0, time=0,
        )
        router._exit_v2_state[701] = {"peak_usd": 3.0, "be_armed": True}
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[armed, naked]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=3300.0, ask=3300.2)),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01)),
            patch("app.mt5.demo_router.mt5.order_send", return_value=SimpleNamespace(retcode=10009, order=1, deal=2)) as send,
        ):
            items = router.process_quick_exits(account=self._account(), mt5_connected=True, now=now)
        send.assert_called_once()  # only the non-armed position was closed
        closed_request = send.call_args.args[0]
        self.assertEqual(closed_request.get("position"), 702)
        self.assertTrue(any(item["data"].get("event_type") == "NEWS_PRECLOSE" for item in items))


if __name__ == "__main__":
    unittest.main()
