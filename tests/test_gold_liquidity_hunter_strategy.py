from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter
from app.services import gold_liquidity_hunter_strategy as gold
from app.services import gold_m1m5_ema_sweep_scalper_strategy as scalper


def settings(**overrides) -> Settings:
    values = {
        "paper_trading": False,
        "demo_trading": True,
        "allow_live_trading": False,
        "demo_only": True,
        "demo_pilot_enabled": True,
        "demo_pilot_hours": 240,
        "demo_pilot_started_at": "2026-06-01T00:00:00+00:00",
        "demo_magic_number": 909002,
        "demo_max_lot": 0.01,
        "demo_max_risk_per_trade_pct": 999,
        "demo_max_daily_loss_pct": 999,
        "demo_max_open_trades_total": 21,
        "demo_max_open_trades_per_symbol": 7,
        "demo_max_open_trades_per_symbol_strategy": 7,
        "demo_max_trades_per_day_total": 999,
        "demo_max_trades_per_symbol_per_day": 999,
        "demo_ignore_all_time_blocks": False,
        "demo_ignore_session_blocks": False,
        "demo_ignore_bad_hour_blocks": False,
        "demo_ignore_duration_blocks": False,
        "demo_ignore_setup_wait_hours": False,
        "max_money_tp_enabled": False,
        "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD",
        "hermes_analysis_only_symbols": "EURUSD",
        "gold_liquidity_mode": "trade",
        "gold_liquidity_strategy_enabled": True,
        "gold_disable_generic_strategies": True,
        "gold_pivot_length": 15,
        "gold_atr_zone_thickness": 0.5,
        "gold_zone_capacity": 5.0,
        "gold_max_zones_per_side": 10,
        "gold_min_zone_stars": 3,
        "gold_min_liquidity_score": 75,
        "gold_min_rr": 2.0,
        "gold_max_open_trades": 1,
        "gold_allowed_signals": "ABS,REJ",
        "gold_observer_signals": "EXH,DIV",
        "hermes_adaptive_confluence_enabled": False,
        "report_timezone": "UTC",
        "timezone_local": "UTC",
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
    }
    values.update(overrides)
    return Settings(**values)


def candles(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": v} for o, h, l, c, v in rows])


class GoldLiquidityMathTests(unittest.TestCase):
    def test_normalize_handles_dataframe(self) -> None:
        df = candles([(1, 2, 0.5, 1.5, 10)])
        out = gold._normalize_candles(df)
        self.assertEqual(list(out.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(float(out.loc[0, "close"]), 1.5)

    def test_normalize_handles_duplicate_columns(self) -> None:
        df = pd.DataFrame([[1, 9, 2, 0.5, 1.5, 10]], columns=["o", "open", "h", "l", "c", "v"])
        out = gold._normalize_candles(df)
        self.assertEqual(float(out.loc[0, "open"]), 1.0)
        self.assertEqual(float(out.loc[0, "volume"]), 10.0)

    def test_normalize_handles_list_dict_and_dict_with_candles(self) -> None:
        row = {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "tick_volume": 10}
        self.assertEqual(len(gold._normalize_candles([row])), 1)
        nested = gold._normalize_candles({"candles": [row]})
        self.assertEqual(float(nested.loc[0, "high"]), 2.0)

    def test_invalid_candles_return_wait_not_exception(self) -> None:
        result = gold.evaluate_gold_liquidity("GOLD#", pd.DataFrame({"bad": [1]}), settings())
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["block_reason"], "GOLD_LIQUIDITY_NO_VALID_CANDLES")

    def test_non_gold_returns_before_normalization(self) -> None:
        with patch("app.services.gold_liquidity_hunter_strategy._normalize_candles", side_effect=AssertionError("should not normalize")):
            result = gold.evaluate("BTCUSD#", {"M5": object()}, {}, settings())
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "GOLD_LIQUIDITY_SYMBOL_NOT_GOLD")

    def test_internal_error_returns_wait_payload(self) -> None:
        with patch("app.services.gold_liquidity_hunter_strategy._closed_frame", side_effect=TypeError("bad candles")):
            result = gold.evaluate("GOLD#", {"M5": object()}, {}, settings())
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "GOLD_LIQUIDITY_INTERNAL_ERROR")
        self.assertEqual(result["gold_liquidity_hunter"]["block_reason"], "GOLD_LIQUIDITY_INTERNAL_ERROR")

    def test_no_lookahead_pivot_confirmation(self) -> None:
        df = candles([(10, 11, 9, 10, 100)] * 40)
        df.loc[20, "high"] = 20
        self.assertFalse(gold.is_confirmed_pivot_high(df.iloc[:35], 20, 15))
        self.assertTrue(gold.is_confirmed_pivot_high(df, 20, 15))

    def test_bsl_and_ssl_zone_creation(self) -> None:
        s = settings()
        df = candles([(10, 12, 8, 11, 100)])
        bsl = gold.create_bsl_zone(df, 0, 2.0, 100.0, s)
        ssl = gold.create_ssl_zone(df, 0, 2.0, 100.0, s)
        self.assertEqual(bsl.side, "BSL")
        self.assertEqual(bsl.top, 12)
        self.assertEqual(ssl.side, "SSL")
        self.assertEqual(ssl.bottom, 8)

    def test_overlap_filter_and_health_decay(self) -> None:
        z1 = gold.GoldZone("BSL", 12, 10, 12, 1, 2, 500, 100, 2)
        z2 = gold.GoldZone("BSL", 11, 9, 11, 2, 2, 500, 100, 2)
        self.assertTrue(gold.zones_overlap(z2, z1))
        self.assertEqual(gold.health_pct(250, 500), 50)

    def test_strength_stars_formula(self) -> None:
        self.assertGreaterEqual(gold.strength_stars(300, 100, 6, 2), 4)

    def test_abs_executable_sell_from_bsl_premium_sweep(self) -> None:
        s = settings()
        zone = gold.GoldZone("BSL", 100, 98, 100, 1, 2, 500, 100, 2, deltas=[0, 0, 0, -80])
        zone.volume_traded = 300
        zone.test_count = 1
        row = pd.Series({"open": 100.5, "high": 101, "low": 98.5, "close": 99, "volume": 100})
        self.assertEqual(gold.eval_reversal(zone, row), "ABS")
        result = gold.process_zone_bar(zone, row, 90, s)
        self.assertEqual(result["decision"], "SELL")
        self.assertEqual(result["premium_discount"], "PREMIUM")

    def test_rej_executable_buy_from_ssl_discount_sweep(self) -> None:
        s = settings()
        zone = gold.GoldZone("SSL", 102, 100, 100, 1, 2, 500, 100, 2, deltas=[15, 0, 0, 0])
        zone.volume_traded = 300
        zone.test_count = 1
        row = pd.Series({"open": 100, "high": 102, "low": 99, "close": 101.5, "volume": 100})
        result = gold.process_zone_bar(zone, row, 110, s)
        self.assertIn(result["signal"], {"ABS", "REJ"})
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["premium_discount"], "DISCOUNT")

    def test_exh_and_div_observer_only(self) -> None:
        s = settings()
        self.assertEqual(gold.gold_liquidity_block_reason("BSL", "SELL", "EXH", "PREMIUM", 5, 90, 2.0, s), "GOLD_SIGNAL_EXH_OBSERVER_ONLY")
        self.assertEqual(gold.gold_liquidity_block_reason("SSL", "BUY", "DIV", "DISCOUNT", 5, 90, 2.0, s), "GOLD_SIGNAL_DIV_OBSERVER_ONLY")

    def test_rr_plan_formula(self) -> None:
        zone = gold.GoldZone("BSL", 100, 98, 100, 1, 2, 500, 100, 2)
        plan = gold.rr_plan_for_zone(zone, "SELL", 99, 2)
        self.assertEqual(plan["sl"], 100.6)
        self.assertEqual(plan["tp"], 95.8)
        self.assertEqual(plan["rr"], 2)


class GoldM1M5ScalperMathTests(unittest.TestCase):
    def test_non_gold_returns_wait_payload(self) -> None:
        result = scalper.evaluate("EURUSD", {"M1": pd.DataFrame(), "M5": pd.DataFrame()}, {}, settings())
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "SYMBOL_NOT_GOLD")

    def test_signal_payload_uses_backend_strategy_contract(self) -> None:
        result = scalper._signal_from_result(
            "GOLD#",
            {
                "decision": "BUY",
                "confidence": 80,
                "entry": 2300.0,
                "sl": 2299.0,
                "tp": 2301.5,
                "rr": 1.5,
                "block_reason": None,
            },
        )
        self.assertEqual(result["strategy"], "GOLD_M1_M5_EMA_SWEEP_SCALPER")
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["reward_risk"], 1.5)
        self.assertEqual(result["gold_m1m5_scalper"]["decision"], "BUY")


class GoldLiquidityRouterTests(unittest.TestCase):
    def account(self) -> dict:
        return {"login": 1, "trade_mode": 0, "trade_allowed": True, "trade_expert": True, "balance": 10000, "equity": 10000}

    def specs(self) -> dict:
        return {"tick_value": 1.0, "tick_size": 0.1, "volume_step": 0.01}

    def decision(self, **overrides) -> dict:
        hunter = {
            "enabled": True,
            "mode": "ENTRY_STRATEGY",
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "source": gold.SOURCE,
            "decision": "SELL",
            "sweep_side": "BSL",
            "reversal_signal": "ABS",
            "zone_stars": 4,
            "zone_health_pct": 80,
            "premium_discount": "PREMIUM",
            "sweep_count": 1,
            "test_count": 1,
            "liquidity_score": 85,
            "directional_confirmation": "SELL",
            "rr_plan": {"entry": 2300, "sl": 2301, "tp": 2298, "rr": 2.0},
            "block_reason": None,
            "warnings": [],
        }
        payload = {
            "symbol": "GOLD#",
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "signal": "SELL",
            "entry": 2300.0,
            "sl": 2301.0,
            "tp": 2298.0,
            "reward_risk": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "gold_liquidity_hunter": hunter,
            "gold_liquidity_signal": "ABS",
            "gold_liquidity_score": 85,
        }
        payload.update(overrides)
        return payload

    def scalper_decision(self, **overrides) -> dict:
        scalper_payload = {
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "source": scalper.SOURCE,
            "decision": "BUY",
            "confidence": 80,
            "entry": 2300.0,
            "sl": 2299.0,
            "tp": 2301.5,
            "rr": 1.5,
            "reasons": ["test"],
            "block_reason": None,
        }
        payload = {
            "symbol": "GOLD#",
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "signal": "BUY",
            "entry": 2300.0,
            "sl": 2299.0,
            "tp": 2301.5,
            "reward_risk": 1.5,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "m15_confirmation_status": "PASS",
            "m1_trigger_status": "PASS",
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "big_setup_grade": "B",
            "gold_m1m5_scalper": scalper_payload,
            "gold_m1m5_scalper_decision": "BUY",
            "gold_m1m5_scalper_score": 80,
        }
        payload.update(overrides)
        return payload

    def evaluate(self, router: DemoKellyRouter, decision: dict, spread: float = 1, max_spread: float = 50):
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            return router.evaluate(decision, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), spread, max_spread, True, now=datetime(2026, 6, 1, 10, tzinfo=timezone.utc))

    def test_gold_liquidity_mode_false_blocks_analysis_only(self) -> None:
        router = DemoKellyRouter(settings(gold_liquidity_mode="false", gold_liquidity_strategy_enabled=False))
        result = self.evaluate(router, self.decision())
        self.assertEqual(result.reason, "GOLD_ANALYSIS_ONLY")

    def test_gold_generic_strategy_blocks(self) -> None:
        router = DemoKellyRouter(settings())
        result = self.evaluate(router, self.decision(strategy="BREAKOUT_RETEST"))
        self.assertEqual(result.reason, "GOLD_GENERIC_STRATEGY_DISABLED")

    def test_order_flow_execution_agent_not_blocked_by_gold_liquidity_gate(self) -> None:
        """ORDER_FLOW_EXECUTION_AGENT on GOLD must not receive GOLD_LIQUIDITY_WAIT.

        The gold liquidity gate reads gold_liquidity_hunter payload that
        ORDER_FLOW_EXECUTION_AGENT never populates.  The two strategies are
        independent and must not be coupled.
        """
        router = DemoKellyRouter(settings())
        of_payload = {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "decision": "BUY",
            "signal": "BUY",
            "score": 100.0,
            "grade": "A",
            "confidence": 100.0,
        }
        decision = self.decision(
            strategy="ORDER_FLOW_EXECUTION_AGENT",
            signal="BUY",
            entry=2300.0,
            sl=2299.0,
            tp=2302.0,
            reward_risk=2.0,
            order_flow_execution_agent=of_payload,
            order_flow_execution_agent_signal="BUY",
            order_flow_execution_agent_score=100,
            gold_liquidity_hunter=None,
        )
        result = self.evaluate(router, decision)
        self.assertNotEqual(result.reason, "GOLD_LIQUIDITY_WAIT")

    def test_gold_liquidity_valid_can_pass_gate(self) -> None:
        router = DemoKellyRouter(settings())
        result = self.evaluate(router, self.decision())
        self.assertEqual(result.decision, "PASS")

    def test_gold_m1m5_scalper_valid_can_pass_gate(self) -> None:
        router = DemoKellyRouter(settings())
        result = self.evaluate(router, self.scalper_decision())
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["gate_statuses"]["gold_m1m5_scalper"]["decision"], "BUY")

    def test_gold_m1m5_scalper_wait_never_reaches_execution_api(self) -> None:
        router = DemoKellyRouter(settings())
        wait_payload = {**self.scalper_decision()["gold_m1m5_scalper"], "decision": "WAIT", "block_reason": "AWAITING_FULL_CONFIRMATION"}
        decision = self.scalper_decision(signal="WAIT", gold_m1m5_scalper=wait_payload)
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(execution_api) as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True)
        send.assert_not_called()
        self.assertEqual(items, [])

    def test_gold_liquidity_relaxed_zone_stars_keeps_abs_rej_only(self) -> None:
        relaxed = {"active": True, "reason": "NO_SETUP_24H", "hours_without_setup": 24}
        self.assertIsNone(gold.gold_liquidity_block_reason("SSL", "BUY", "ABS", "DISCOUNT", 2, 80, 2.0, settings(), relaxed))
        self.assertEqual(
            gold.gold_liquidity_block_reason("SSL", "BUY", "EXH", "DISCOUNT", 2, 80, 2.0, settings(), relaxed),
            "GOLD_SIGNAL_EXH_OBSERVER_ONLY",
        )

    def test_gold_bad_liquidity_hour_blocks(self) -> None:
        router = DemoKellyRouter(settings())
        decision = self.decision()
        with patch("app.mt5.demo_router.TimeEngine.evaluate", return_value={"time_gate_status": "PASS", "time_gate_reason": "BAD_LIQUIDITY_HOUR", "symbol_market_open": True, "is_bad_hour": True}), patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(decision, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True, now=datetime(2026, 6, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(result.reason, "BAD_LIQUIDITY_HOUR")

    def test_gold_spread_rr_lot_and_open_trade_blocks(self) -> None:
        router = DemoKellyRouter(settings())
        self.assertEqual(self.evaluate(router, self.decision(), spread=100, max_spread=50).reason, "MAX_SPREAD")
        self.assertEqual(self.evaluate(router, self.decision(reward_risk=1.5, gold_liquidity_hunter={**self.decision()["gold_liquidity_hunter"], "rr_plan": {"rr": 1.5}})).reason, "GOLD_RR_BELOW_2")
        self.assertEqual(self.evaluate(router, self.decision(), spread=1, max_spread=50).decision, "PASS")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(symbol="GOLD#", magic=909002)]):
            result = router.evaluate(self.decision(), {"approved_lot": 0.02}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True, now=datetime(2026, 6, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(result.reason, "MAX_OPEN_TRADES_PER_SYMBOL")

    def test_gold_block_never_reaches_execution_api(self) -> None:
        router = DemoKellyRouter(settings(gold_liquidity_mode="false", gold_liquidity_strategy_enabled=False))
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(execution_api) as send:
            items = router.process_decision(self.decision(), {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True)
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "GOLD_ANALYSIS_ONLY")

    def test_gold_internal_error_signal_never_reaches_execution_api(self) -> None:
        router = DemoKellyRouter(settings())
        decision = gold.evaluate("GOLD#", {"M5": object()}, {}, settings())
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(execution_api) as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True)
        send.assert_not_called()
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
