"""Tests for HERMES SL ENGINE v1 — BtcSlEngine geometric SL/TP modification.

Covers:
- dollars_to_price: SELL and BUY price conversion
- calculate_tp_price: RR-based TP from SL distance
- _calculate_sl_price: hard cap, anti-inversion, method selection (SELL→min, BUY→max)
- apply_sl_tp_to_mt5: SL_UNCHANGED skip, SL_WOULD_WIDEN skip
- run(): Conditions 1-4 (RESCUE_MODE, POSITIVE_CANDIDATES, DYNAMIC_TP_SKIP, BREAKEVEN_ZONE)
- run(): full integration with mocked MT5
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.mt5.btc_sl_engine import (
    MAX_SL_DOLLARS,
    BtcSlEngine,
    _method_e_market_profile,
    calculate_tp_price,
    dollars_to_price,
)


def _make_candles(n: int = 20, base: float = 65000.0, swing: float = 10.0) -> list[dict]:
    candles = []
    for i in range(n):
        price = base + (i % 2) * swing
        candles.append({"high": price + swing, "low": price - swing, "close": price})
    return candles


class TestDollarsToPrice(unittest.TestCase):

    def test_dollars_to_price_sell(self) -> None:
        sl = dollars_to_price(65000.0, 3.0, "SELL", lot=0.01)
        self.assertAlmostEqual(sl, 65300.0, places=2)

    def test_dollars_to_price_buy(self) -> None:
        sl = dollars_to_price(65000.0, 3.0, "BUY", lot=0.01)
        self.assertAlmostEqual(sl, 64700.0, places=2)


class TestCalculateTpPrice(unittest.TestCase):

    def test_tp_from_rr(self) -> None:
        # entry=65000, sl=65300 (SELL) → sl_dist=300, tp_dist=300×2.65=795 → tp=64205
        tp = calculate_tp_price(65000.0, 65300.0, "SELL", rr=2.65)
        self.assertAlmostEqual(tp, 64205.0, places=2)


class TestCalculateSlPrice(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcSlEngine()

    def test_hard_cap_sl(self) -> None:
        # ATR=2000 → Method A (SELL) = 65000+3000=68000 > cap=65800 → capped
        sl, method = self.engine._calculate_sl_price(65000.0, "SELL", 2000.0)
        cap = dollars_to_price(65000.0, MAX_SL_DOLLARS, "SELL")
        self.assertAlmostEqual(sl, cap, places=2)  # 65800.0
        self.assertEqual(method, "HARD_CAP")

    def test_anti_inversion_sell(self) -> None:
        # vwap=1.0 → Method C (SELL) = 1+200=201 << entry=65000 (inverted)
        # min(A=65150, C=201) = 201 → anti-inversion → hard cap
        sl, method = self.engine._calculate_sl_price(
            65000.0, "SELL", 100.0, candles_m5=None, vwap=1.0
        )
        cap = dollars_to_price(65000.0, MAX_SL_DOLLARS, "SELL")
        self.assertAlmostEqual(sl, cap, places=2)
        self.assertEqual(method, "ANTI_INVERSION")

    def test_anti_inversion_buy(self) -> None:
        # vwap=130000 → Method C (BUY) = 130000-200=129800 >> entry=65000 (inverted)
        # max(A=64800, C=129800) = 129800 → anti-inversion → hard cap
        sl, method = self.engine._calculate_sl_price(
            65000.0, "BUY", 100.0, candles_m5=None, vwap=130000.0
        )
        cap = dollars_to_price(65000.0, MAX_SL_DOLLARS, "BUY")
        self.assertAlmostEqual(sl, cap, places=2)
        self.assertEqual(method, "ANTI_INVERSION")

    def test_method_a_buy_trails_after_two_atr_profit(self) -> None:
        sl, method = self.engine._calculate_sl_price(
            65000.0, "BUY", 100.0, current_price=65250.0,
        )
        self.assertEqual(method, "A_TRAILING")
        self.assertGreater(sl, 65000.0)
        self.assertEqual(sl, 65150.0)

    def test_method_a_does_not_trail_before_two_atr_profit(self) -> None:
        sl, method = self.engine._calculate_sl_price(
            65000.0, "BUY", 100.0, current_price=65199.0,
        )
        self.assertNotEqual(method, "A_TRAILING")
        self.assertLess(sl, 65000.0)

    def test_method_a_sell_trails_after_two_atr_profit(self) -> None:
        sl, method = self.engine._calculate_sl_price(
            65000.0, "SELL", 100.0, current_price=64750.0,
        )
        self.assertEqual(method, "A_TRAILING")
        self.assertLess(sl, 65000.0)
        self.assertEqual(sl, 64850.0)


class TestMethodSelection(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcSlEngine()

    def test_method_selection_sell(self) -> None:
        # Uniform candles with high=65000 → SELL: B=65000+40=65040, A=65200, D=65065
        # min(A,B,D) = 65040 → method B
        candles = [{"high": 65000.0, "low": 64990.0, "close": 64995.0}] * 55
        sl, method = self.engine._calculate_sl_price(65000.0, "SELL", 133.33, candles)
        expected_b = 65000.0 + 133.33 * 0.3
        self.assertAlmostEqual(sl, expected_b, delta=1.0)
        self.assertEqual(method, "B")

    def test_method_selection_buy(self) -> None:
        # Uniform candles with low=65000 → BUY: B=65000-40=64960, A=64733, D=64935
        # max(A,B,D) = 64960 → method B
        candles = [{"high": 65010.0, "low": 65000.0, "close": 65005.0}] * 55
        sl, method = self.engine._calculate_sl_price(65000.0, "BUY", 133.33, candles)
        expected_b = 65000.0 - 133.33 * 0.3
        self.assertAlmostEqual(sl, expected_b, delta=1.0)
        self.assertEqual(method, "B")


class TestMethodE(unittest.TestCase):

    def setUp(self) -> None:
        self.poc = 65814.32
        self.vah = 66208.99
        self.val = 65527.29
        self.atr = 100.0

    def test_method_e_sell_balance_day(self) -> None:
        # price=65800 between val=65527.29 and vah=66208.99 → Balance Day
        # expected: VAH + atr * 0.3 * 1.5 = 66208.99 + 45 = 66253.99
        sl = _method_e_market_profile(65800.0, "SELL", self.atr, self.poc, self.vah, self.val)
        self.assertIsNotNone(sl)
        self.assertAlmostEqual(sl, self.vah + self.atr * 0.45, places=2)

    def test_method_e_sell_trend_day(self) -> None:
        # price=66500 > vah=66208.99 → Trend Day haussier
        # expected: VAH + atr * 0.2 = 66208.99 + 20 = 66228.99
        sl = _method_e_market_profile(66500.0, "SELL", self.atr, self.poc, self.vah, self.val)
        self.assertIsNotNone(sl)
        self.assertAlmostEqual(sl, self.vah + self.atr * 0.2, places=2)

    def test_method_e_sell_below_val(self) -> None:
        # price=65000 < val=65527.29 → Trend Day baissier
        # expected: VAL + atr * 0.2 = 65527.29 + 20 = 65547.29
        sl = _method_e_market_profile(65000.0, "SELL", self.atr, self.poc, self.vah, self.val)
        self.assertIsNotNone(sl)
        self.assertAlmostEqual(sl, self.val + self.atr * 0.2, places=2)

    def test_method_e_unavailable(self) -> None:
        # Any None in poc/vah/val → method E silently skipped
        sl = _method_e_market_profile(65800.0, "SELL", self.atr, None, None, None)
        self.assertIsNone(sl)
        # Partial None also skipped
        sl2 = _method_e_market_profile(65800.0, "SELL", self.atr, self.poc, None, self.val)
        self.assertIsNone(sl2)

    def test_method_e_wins_selection(self) -> None:
        # entry=65900, vah=66000, val=65000, atr=500 → Balance Day
        # E: 66000 + 500*0.45 = 66225; A: 65900 + 500*1.5 = 66650
        # No candles/vwap → only A and E compete; min(66650, 66225) = 66225 → E wins
        engine = BtcSlEngine()
        sl, method = engine._calculate_sl_price(
            entry_price=65900.0,
            direction="SELL",
            atr=500.0,
            candles_m5=None,
            vwap=None,
            poc=65500.0,
            vah=66000.0,
            val=65000.0,
        )
        self.assertAlmostEqual(sl, 66000.0 + 500.0 * 0.45, places=2)  # 66225.0
        self.assertEqual(method, "E")


class TestApplySlTpToMt5(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcSlEngine()

    def _mock_position(
        self, sl: float = 65300.0, tp: float = 64000.0, pos_type: int = 1
    ) -> MagicMock:
        mock_pos = MagicMock()
        mock_pos.sl = sl
        mock_pos.tp = tp
        mock_pos.type = pos_type
        return mock_pos

    def test_apply_sl_skip_if_unchanged(self) -> None:
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5:
            mock_mt5.positions_get.return_value = [self._mock_position(sl=65300.0)]
            # 65300.001 rounds to 65300.00 — identical to current SL
            result = self.engine.apply_sl_tp_to_mt5(12345, sl_price=65300.001)
            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "SL_UNCHANGED")

    def test_sl_never_widens(self) -> None:
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5:
            # SELL position; current SL=65300; new SL=65400 → would widen → SKIP
            mock_mt5.positions_get.return_value = [
                self._mock_position(sl=65300.0, pos_type=1)  # 1 = SELL
            ]
            result = self.engine.apply_sl_tp_to_mt5(12345, sl_price=65400.0)
            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "SL_WOULD_WIDEN")


class TestRun(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = BtcSlEngine()
        self.candles = _make_candles(20, 65000.0, 10.0)

    def _base_run_kwargs(self, **overrides) -> dict:
        base = {
            "ticket": 12345,
            "entry_price": 65000.0,
            "direction": "SELL",
            "atr": 100.0,
            "current_profit": -0.50,
            "min_seen_profit": -0.20,
            "positive_count": 0,
            "be_buffer_usd": 0.10,
            "exit_mode": "fallback",
            "candles_m5": self.candles,
        }
        base.update(overrides)
        return base

    def test_skip_rescue_mode(self) -> None:
        result = self.engine.run(
            **self._base_run_kwargs(min_seen_profit=-0.60, current_profit=0.05)
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "RESCUE_MODE_ACTIVE")

    def test_skip_positive_count(self) -> None:
        result = self.engine.run(**self._base_run_kwargs(positive_count=1))
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "POSITIVE_CANDIDATES_PRESENT")

    def test_skip_breakeven_zone(self) -> None:
        # |profit|=0.05 < be_buffer=0.10 → BREAKEVEN_ZONE
        result = self.engine.run(**self._base_run_kwargs(current_profit=0.05))
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "BREAKEVEN_ZONE")

    def test_skip_dynamic_exit_tp(self) -> None:
        """exit_mode='dynamic' → SL computed, TP left as None."""
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5, \
             patch("app.mt5.demo_router.sl_engine_apply_modification") as mock_modify:
            mock_pos = MagicMock()
            mock_pos.sl = 66000.0  # current SL far out → new SL will be tighter
            mock_pos.tp = 64000.0
            mock_pos.type = 1  # SELL
            mock_mt5.positions_get.return_value = [mock_pos]
            mock_modify.return_value = {"success": True, "retcode": 10009, "comment": "done"}

            result = self.engine.run(
                **self._base_run_kwargs(exit_mode="dynamic")
            )
            # TP must be None — engine doesn't own TP in dynamic mode
            self.assertIsNone(result.get("tp"), "No TP should be calculated in dynamic mode")
            # SL should be present
            self.assertIsNotNone(result.get("sl"))

    def test_run_full_integration(self) -> None:
        """Mock MT5 end-to-end: run() computes SL, calls sl_engine_apply_modification."""
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5, \
             patch("app.mt5.demo_router.sl_engine_apply_modification") as mock_modify:
            mock_pos = MagicMock()
            mock_pos.sl = 66000.0  # SELL, current SL far → new tighter SL < 66000
            mock_pos.tp = 64000.0
            mock_pos.type = 1  # SELL

            mock_mt5.positions_get.return_value = [mock_pos]
            mock_modify.return_value = {"success": True, "retcode": 10009, "comment": "done"}

            candles = _make_candles(55, 65000.0, 10.0)
            result = self.engine.run(
                ticket=12345,
                entry_price=65000.0,
                direction="SELL",
                atr=133.33,
                current_profit=-0.50,
                min_seen_profit=-0.20,
                positive_count=0,
                be_buffer_usd=0.10,
                exit_mode="fallback",
                candles_m5=candles,
            )

            self.assertIn("applied", result)
            self.assertIn("method", result)
            self.assertIn("sl", result)
            # Conditions met, SL tightened from 66000 → applied=True
            self.assertTrue(result["applied"])
            mock_modify.assert_called_once()
            # TP was calculated (fallback mode)
            self.assertIsNotNone(result.get("tp"))


class TestSlEngineModifyRequestStructure(unittest.TestCase):
    """Verify the MT5 request sent by sl_engine_apply_modification.

    Guards against regression to TRADE_ACTION_MODIFY (7) / "ticket" key, which causes
    retcode=10013 (Invalid request) because MODIFY is for pending orders, not open positions.
    The correct action for open position SL/TP modification is TRADE_ACTION_SLTP (6).
    """

    def _captured_request(self, ticket=12345, sl=65300.0, tp=64000.0, symbol="BTCUSD#"):
        from app.mt5.demo_router import sl_engine_apply_modification
        captured: dict = {}

        def _intercept(req):
            captured["request"] = dict(req)
            mock_result = MagicMock()
            mock_result.retcode = 10009
            mock_result.comment = "done"
            mock_result.request_id = 1
            mock_result.deal = 0
            mock_result.order = 0
            mock_result.volume = 0.0
            mock_result.price = 0.0
            mock_result.bid = 0.0
            mock_result.ask = 0.0
            return mock_result

        with patch("app.mt5.demo_router.mt5") as mock_mt5, \
             patch("app.mt5.demo_router._get_settings") as mock_settings_fn:
            mock_mt5.TRADE_ACTION_SLTP = 6
            mock_mt5.TRADE_RETCODE_DONE = 10009
            mock_mt5.order_send.side_effect = _intercept
            mock_sym_info = MagicMock()
            mock_sym_info.digits = 2
            mock_mt5.symbol_info.return_value = mock_sym_info
            mock_mt5.last_error.return_value = (0, "no error")
            mock_settings_fn.return_value.demo_magic_number = 909002
            sl_engine_apply_modification(ticket, sl, tp, symbol)

        return captured["request"]

    def test_action_is_trade_action_sltp(self) -> None:
        req = self._captured_request()
        self.assertEqual(req["action"], 6,
            "Must use TRADE_ACTION_SLTP (6) for open positions — MODIFY (7) causes retcode=10013")

    def test_position_key_present_not_ticket(self) -> None:
        req = self._captured_request(ticket=99999)
        self.assertIn("position", req, "Must use 'position' key for open position SLTP modification")
        self.assertNotIn("ticket", req, "'ticket' key is for pending orders (TRADE_ACTION_MODIFY)")
        self.assertEqual(req["position"], 99999)

    def test_symbol_present(self) -> None:
        req = self._captured_request(symbol="BTCUSD#")
        self.assertIn("symbol", req, "'symbol' field required by XM Global for TRADE_ACTION_SLTP")
        self.assertEqual(req["symbol"], "BTCUSD#")

    def test_no_type_volume_price_fields(self) -> None:
        req = self._captured_request()
        self.assertNotIn("type", req, "'type' field would open a new market order")
        self.assertNotIn("volume", req, "'volume' field would open a new market order")
        self.assertNotIn("price", req, "'price' field would open a new market order")

    def test_sl_tp_rounded_to_symbol_digits(self) -> None:
        # sl=64149.4865 must be rounded to digits=2 → 64149.49
        req = self._captured_request(sl=64149.4865, tp=63838.804)
        self.assertEqual(req["sl"], 64149.49)
        self.assertEqual(req["tp"], 63838.80)


if __name__ == "__main__":
    unittest.main()
