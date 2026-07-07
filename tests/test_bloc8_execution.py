"""BLOC 8 — execution at the tick.

Proves:
- The order is priced on symbol_info_tick AT the send instant
  (BUY@ask, SELL@bid) with deviation=50 points.
- ABORT when the price drifted > 300 points since validation
  ([ORDER_ABORT_PRICE_MOVED], refusal recorded) or when RR at the tick
  falls below 1.0.
- [EXEC_QUALITY] per order: slippage_vs_tick, slippage_vs_request,
  spread_at_send, fill_latency -> event dataset.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

def _make_harness():
    # Imported and built inside the function so pytest never collects the
    # parent TestCase's tests a second time through this module.
    from test_paper_learning_safety import DemoKellyRouterSafetyTests

    cls = type("ExecHarness", (DemoKellyRouterSafetyTests,), {"runTest": lambda self: None})
    return cls("runTest")


def _symbol_info() -> SimpleNamespace:
    return SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        digits=5,
        point=0.00001,
        trade_stops_level=0,
    )


class TestExecutionAtTick(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _make_harness()
        self.h.setUp()

    def tearDown(self) -> None:
        self.h.tearDown()

    def _process(self, tick: SimpleNamespace, result=None, decision=None):
        router = self.h.eur_router()
        decision = decision or self.h.eur_strategy_decision()
        fake_result = result or SimpleNamespace(retcode=10009, order=123456, deal=0, price=tick.ask)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=_symbol_info()),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.services.daily_killswitch._mt5_history_deals", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.h.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.h.specs(),
                1,
                30,
                True,
                "setup-exec",
                self.h.now,
            )
        return items, send

    def test_buy_priced_at_ask_with_deviation_50(self) -> None:
        items, send = self._process(SimpleNamespace(bid=1.09995, ask=1.10005))
        send.assert_called_once()
        request = send.call_args.args[0]
        self.assertEqual(request["price"], 1.10005)  # BUY @ ask at send instant
        self.assertEqual(request["deviation"], 50)

    def test_sell_priced_at_bid(self) -> None:
        sell_payload = {**self.h.eur_strategy_decision()["eur_ema_rsi_atr"], "decision": "SELL"}
        decision = self.h.eur_strategy_decision(
            signal="SELL", entry=1.1000, sl=1.1010, tp=1.0980, eur_ema_rsi_atr=sell_payload
        )
        items, send = self._process(SimpleNamespace(bid=1.09995, ask=1.10005), decision=decision)
        send.assert_called_once()
        request = send.call_args.args[0]
        self.assertEqual(request["price"], 1.09995)  # SELL @ bid

    def test_abort_when_price_drifted_over_300_points(self) -> None:
        # validation entry 1.1000, tick ask 1.10410 -> 410 points drift
        items, send = self._process(SimpleNamespace(bid=1.10400, ask=1.10410))
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "ORDER_ABORT_PRICE_MOVED")
        self.assertGreater(items[0]["data"]["drift_points"], 300)

    def test_abort_when_rr_at_tick_below_1(self) -> None:
        # drift 160 pts (< 300) but the tick eats the reward: rr ~0.15
        items, send = self._process(SimpleNamespace(bid=1.10150, ask=1.10160))
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "RR_FLOOR_BELOW_1_0")

    def test_abort_when_no_tick_available(self) -> None:
        items, send = self._process(SimpleNamespace(bid=None, ask=None))
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "ORDER_ABORT_NO_TICK")

    def test_exec_quality_recorded_on_fill(self) -> None:
        tick = SimpleNamespace(bid=1.09995, ask=1.10005)
        fill = SimpleNamespace(retcode=10009, order=123456, deal=0, price=1.10010)
        items, send = self._process(tick, result=fill)
        send.assert_called_once()
        quality = items[0]["data"]["exec_quality"]
        self.assertAlmostEqual(quality["slippage_vs_tick_points"], 5.0, places=3)
        self.assertAlmostEqual(quality["slippage_vs_request_points"], 5.0, places=3)
        self.assertAlmostEqual(quality["spread_at_send_points"], 10.0, places=3)
        self.assertGreaterEqual(quality["fill_latency_ms"], 0.0)
        self.assertEqual(quality["deviation_points"], 50)


if __name__ == "__main__":
    unittest.main()
