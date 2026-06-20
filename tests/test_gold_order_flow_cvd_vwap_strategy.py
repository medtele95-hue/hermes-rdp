from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from app.agents.setup_hunter import SetupHunter
from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter, _strategy_report_breakdown
from app.services.dashboard_snapshot import dashboard_snapshot
from app.strategies import gold_order_flow_cvd_vwap as order_flow


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
        "demo_ignore_all_time_blocks": True,
        "demo_ignore_session_blocks": True,
        "demo_ignore_bad_hour_blocks": True,
        "demo_ignore_duration_blocks": True,
        "demo_ignore_setup_wait_hours": True,
        "max_money_tp_enabled": False,
        "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,XAUUSD#,GOLDCash#",
        "hermes_analysis_only_symbols": "EURUSD",
        "gold_liquidity_mode": "trade",
        "gold_liquidity_strategy_enabled": True,
        "gold_disable_generic_strategies": True,
        "gold_order_flow_execution_enabled": True,
        "gold_order_flow_min_confidence": 70,
        "gold_order_flow_require_divergence": True,
        "strict_gold_order_flow_topdown": False,
        "gold_min_liquidity_score": 75,
        "gold_min_rr": 2.0,
        "gold_max_open_trades": 1,
        "hermes_adaptive_confluence_enabled": False,
        "report_timezone": "UTC",
        "timezone_local": "UTC",
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
    }
    values.update(overrides)
    return Settings(**values)


def gold_candles(kind: str = "bull", near_level: bool = True, bars: int = 80) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2026-06-01T08:00:00Z")
    for i in range(bars):
        if kind == "bull":
            base = 2300.0 - min(i, 36) * 0.08
            if i >= bars - 24:
                base = 2299.0 - (i - (bars - 24)) * 0.04
            if i >= bars - 12:
                base = 2298.5 + (i - (bars - 12)) * 0.06
            close = base + (0.08 if i == bars - 1 else 0.0)
            low = base - (0.35 if i == bars - 5 else 0.18)
            high = base + 0.2
        else:
            base = 2300.0 + min(i, 36) * 0.08
            if i >= bars - 24:
                base = 2301.0 + (i - (bars - 24)) * 0.04
            if i >= bars - 12:
                base = 2301.5 - (i - (bars - 12)) * 0.06
            close = base - (0.08 if i == bars - 1 else 0.0)
            low = base - 0.2
            high = base + (0.35 if i == bars - 5 else 0.18)
        if not near_level and i == bars - 1:
            close = close + (18.0 if kind == "bull" else -18.0)
            high = max(high, close + 0.2)
            low = min(low, close - 0.2)
        rows.append(
            {
                "time": int((start + pd.Timedelta(minutes=5 * i)).timestamp()),
                "open": base,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": 100 + (20 if i > bars - 24 else 0),
            }
        )
    df = pd.DataFrame(rows)
    if kind == "bull":
        first = df.index[-24:-12]
        second = df.index[-12:]
        df.loc[first, "cvd_fixture"] = -100
        df.loc[second, "cvd_fixture"] = 20
    return df


def force_order_flow(df: pd.DataFrame, kind: str = "bull", near: bool = True) -> pd.DataFrame:
    out = order_flow.compute_vwap(order_flow.compute_delta_cvd(order_flow._normalize_candles(df), None))
    if kind == "bull":
        out.loc[out.index[-24:-12], "cvd"] = -100
        out.loc[out.index[-12:], "cvd"] = list(range(10, 22))
        out.loc[out.index[-1], "delta"] = 10
    else:
        out.loc[out.index[-24:-12], "cvd"] = 100
        out.loc[out.index[-12:], "cvd"] = list(range(-10, -22, -1))
        out.loc[out.index[-1], "delta"] = -10
    if near:
        price = float(out["close"].iloc[-1])
        out["vwap"] = price
    return out


class GoldOrderFlowStrategyTests(unittest.TestCase):
    def test_runs_only_on_gold_symbols(self) -> None:
        for symbol in ["XAUUSD", "XAUUSD#", "GOLD", "GOLD#", "GOLDCash#"]:
            with self.subTest(symbol=symbol):
                result = order_flow.evaluate(symbol, {"M5": gold_candles()}, {}, settings())
                self.assertEqual(result["strategy"], "GOLD_ORDER_FLOW_CVD_VWAP")
        for symbol in ["BTCUSD#", "EURUSD", "US100", "JP225", "ETHUSD"]:
            with self.subTest(symbol=symbol):
                result = order_flow.evaluate(symbol, {"M5": object()}, {}, settings())
                self.assertEqual(result["signal"], "WAIT")
                self.assertEqual(result["strategy"], "ORDER_FLOW_READER")
                self.assertEqual(result["status"], "OBSERVE_ONLY")

    def test_no_signal_without_divergence(self) -> None:
        df = order_flow._normalize_candles(gold_candles())
        df["open"] = 2300.0
        df["high"] = 2300.2
        df["low"] = 2299.8
        df["close"] = 2300.0
        df = order_flow.compute_vwap(df)
        df["delta"] = 0
        df["cvd"] = 0
        result = order_flow.generate_signal("GOLD#", df, {"poc": df["close"].iloc[-1], "vah": df["close"].iloc[-1], "val": df["close"].iloc[-1], "vwap": df["close"].iloc[-1]}, order_flow.OrderFlowConfig())
        self.assertIsNone(result)

    def test_no_signal_when_not_near_key_level(self) -> None:
        df = force_order_flow(gold_candles(), near=False)
        price = float(df["close"].iloc[-1])
        levels = {"poc": price + 20, "vah": price + 21, "val": price - 20, "vwap": price + 22}
        self.assertIsNone(order_flow.generate_signal("GOLD#", df, levels, order_flow.OrderFlowConfig()))

    def test_buy_signal_uses_volume_profile_vwap_cvd_and_swing_rr(self) -> None:
        df = force_order_flow(gold_candles("bull"))
        price = float(df["close"].iloc[-1])
        levels = {"poc": price, "vah": price + 0.1, "val": price - 0.1, "vwap": price - 0.05}
        result = order_flow.generate_signal("GOLD#", df, levels, order_flow.OrderFlowConfig())
        self.assertEqual(result["decision"], "BUY")
        self.assertGreaterEqual(result["confidence"], 70)
        self.assertIn("divergence_bull", result["confirmations"])
        self.assertEqual(result["entry"], round(price, 5))
        self.assertLess(result["sl"], result["entry"])
        self.assertGreater(result["tp"], result["entry"])
        self.assertEqual(result["rr"], 2.0)

    def test_sell_signal_uses_swing_sl_and_rr_tp(self) -> None:
        df = force_order_flow(gold_candles("bear"), "bear")
        price = float(df["close"].iloc[-1])
        levels = {"poc": price, "vah": price + 0.1, "val": price - 0.1, "vwap": price + 0.05}
        result = order_flow.generate_signal("GOLD#", df, levels, order_flow.OrderFlowConfig())
        self.assertEqual(result["decision"], "SELL")
        self.assertGreater(result["sl"], result["entry"])
        self.assertLess(result["tp"], result["entry"])


class GoldOrderFlowRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.events_path = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def router(self, **overrides) -> DemoKellyRouter:
        return DemoKellyRouter(settings(**overrides), events_path=self.events_path)

    def account(self) -> dict:
        return {"login": 1, "trade_mode": 0, "trade_allowed": True, "trade_expert": True, "balance": 10000, "equity": 10000}

    def specs(self) -> dict:
        return {"tick_value": 1.0, "tick_size": 0.1, "volume_step": 0.01}

    def decision(self, confidence: int = 75, symbol: str = "GOLD#", signal: str = "BUY", **overrides) -> dict:
        payload = {
            "enabled": True,
            "mode": "ENTRY_STRATEGY",
            "strategy": "GOLD_ORDER_FLOW_CVD_VWAP",
            "source": order_flow.SOURCE,
            "decision": signal,
            "confidence": confidence,
            "entry": 2300.0,
            "sl": 2299.0,
            "tp": 2302.0,
            "rr": 2.0,
            "poc": 2300.0,
            "vah": 2300.5,
            "val": 2299.5,
            "vwap": 2300.1,
            "cvd_proxy": 100.0,
            "cvd_slope": 15.0,
            "delta_proxy": 10.0,
            "latest_delta": 10.0,
            "divergence": "bull" if signal == "BUY" else "bear",
            "mt5_order_flow_warning": "MT5_TICK_DELTA_PROXY_NOT_REAL_ORDER_BOOK",
            "block_reason": None,
        }
        out = {
            "symbol": symbol,
            "strategy": "GOLD_ORDER_FLOW_CVD_VWAP",
            "signal": signal,
            "entry": 2300.0,
            "sl": 2299.0,
            "tp": 2302.0,
            "reward_risk": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "m15_confirmation_status": "PASS",
            "m1_trigger_status": "PASS",
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "big_setup_grade": "B",
            "gold_order_flow_cvd_vwap": payload,
            "gold_order_flow_score": confidence,
            "gold_order_flow_signal": signal,
            "raw_payload": {
                "strategy_id": "GOLD_ORDER_FLOW_CVD_VWAP",
                "symbol": symbol,
                "timeframe": "M5",
                "status": "ORDER_READY" if signal in {"BUY", "SELL"} else "WAIT",
                "side": signal if signal in {"BUY", "SELL"} else None,
                "confidence": confidence,
                "entry": 2300.0,
                "sl": 2299.0,
                "tp": 2302.0,
                "poc": 2300.0,
                "vah": 2300.5,
                "val": 2299.5,
                "vwap": 2300.1,
                "cvd_proxy": 100.0,
                "cvd_slope": 15.0,
                "delta_proxy": 10.0,
                "latest_delta": 10.0,
                "divergence": "bull" if signal == "BUY" else "bear",
                "block_reason": None,
                "router_decision": None,
                "demo_gate_reason": None,
                "mt5_order_flow_warning": "MT5_TICK_DELTA_PROXY_NOT_REAL_ORDER_BOOK",
            },
        }
        out.update(overrides)
        return out

    def evaluate(self, router: DemoKellyRouter, decision: dict, broker_symbol: str = "GOLD#"):
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            return router.evaluate(decision, {"approved_lot": 0.01}, self.account(), broker_symbol, {}, {}, self.specs(), 1, 50, True, now=datetime(2026, 6, 1, 10, tzinfo=timezone.utc))

    def test_confidence_below_70_blocks_route(self) -> None:
        router = self.router()
        result = self.evaluate(router, self.decision(confidence=69))
        self.assertEqual(result.reason, "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70")

    def test_full_order_flow_payload_emitted_even_when_blocked(self) -> None:
        router = self.router()
        items = router.process_decision(
            self.decision(confidence=69),
            {"approved_lot": 0.01},
            self.account(),
            "GOLD#",
            {},
            {},
            self.specs(),
            1,
            50,
            True,
        )
        raw = items[0]["data"]["raw_payload"]
        for key in [
            "strategy_id",
            "symbol",
            "timeframe",
            "status",
            "side",
            "confidence",
            "entry",
            "sl",
            "tp",
            "poc",
            "vah",
            "val",
            "vwap",
            "cvd_slope",
            "latest_delta",
            "divergence",
            "block_reason",
            "router_decision",
            "demo_gate_reason",
            "mt5_order_flow_warning",
        ]:
            self.assertIn(key, raw)
        self.assertEqual(raw["strategy_id"], "GOLD_ORDER_FLOW_CVD_VWAP")
        self.assertEqual(raw["router_decision"], "BLOCK")
        self.assertEqual(raw["demo_gate_reason"], "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70")
        self.assertEqual(raw["poc"], 2300.0)
        self.assertEqual(raw["divergence"], "bull")

    def test_divergence_none_blocks_route(self) -> None:
        router = self.router()
        decision = self.decision(confidence=90)
        decision["gold_order_flow_cvd_vwap"]["divergence"] = None
        decision["raw_payload"]["divergence"] = None
        result = self.evaluate(router, decision)
        self.assertEqual(result.reason, "GOLD_ORDER_FLOW_NO_DIVERGENCE")

    def test_can_route_demo_when_confidence_and_safety_pass(self) -> None:
        router = self.router()
        result = self.evaluate(router, self.decision(confidence=70))
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["gate_statuses"]["gold_order_flow_cvd_vwap"]["decision"], "BUY")

    def test_micro_discovery_low_confluence_is_not_final_blocker_for_order_flow(self) -> None:
        router = self.router(hermes_demo_micro_discovery_mode=True)
        decision = self.decision(
            confidence=90,
            final_confluence_score=10,
            micro_discovery_reason="MICRO_DISCOVERY_CONFLUENCE_TOO_LOW",
            smc_confluence_status="FAIL",
            mtfa_status="FAIL",
        )
        result = self.evaluate(router, decision)
        self.assertEqual(result.decision, "PASS")
        self.assertNotEqual(result.reason, "MICRO_DISCOVERY_CONFLUENCE_TOO_LOW")

    def test_setup_hunter_marks_order_flow_order_ready(self) -> None:
        hunter = SetupHunter(settings())
        signal = self.decision(confidence=76)
        result = hunter.evaluate("GOLD#", "GOLD#", {"ai_decision": {}, "strategy_signals": [signal]}, {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True}, 1, 50)
        self.assertEqual(result.best_candidate["best_strategy"], "GOLD_ORDER_FLOW_CVD_VWAP")
        self.assertTrue(result.best_candidate["demo_eligible"])

    def test_order_flow_entry_disabled_prevents_gold_order_flow_trade(self) -> None:
        hunter = SetupHunter(settings(gold_order_flow_execution_enabled=False))
        result = hunter.evaluate(
            "GOLD#",
            "GOLD#",
            {"ai_decision": {}, "strategy_signals": [self.decision(confidence=90)]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True},
            1,
            50,
        )
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertFalse(result.best_candidate["demo_eligible"])
        router = self.router(gold_order_flow_execution_enabled=False)
        self.assertEqual(self.evaluate(router, self.decision(confidence=90)).reason, "GOLD_ORDER_FLOW_EXECUTION_DISABLED")

    def test_missing_order_flow_fields_cannot_create_router_handoff(self) -> None:
        hunter = SetupHunter(settings())
        signal = self.decision(confidence=90)
        signal["gold_order_flow_cvd_vwap"]["vwap"] = None
        signal["raw_payload"]["vwap"] = None
        result = hunter.evaluate(
            "GOLD#",
            "GOLD#",
            {"ai_decision": {}, "strategy_signals": [signal]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True},
            1,
            50,
        )
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertTrue(any("ORDER_FLOW_REQUIRED_FIELDS_MISSING" in item.get("failed_gates", []) for item in result.candidates))

    def test_order_flow_reader_does_not_block_btc_strategy(self) -> None:
        hunter = SetupHunter(settings(hermes_trade_symbols="BTCUSD#,BTCUSD"))
        scalp = {
            "symbol": "BTCUSD#",
            "broker_symbol": "BTCUSD#",
            "strategy": "BTC_SCALPING_AGENT",
            "signal": "BUY",
            "confidence": 76,
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "risk_reward": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 70,
            "mtfa_score": 60,
        }
        reader = {"strategy": "ORDER_FLOW_READER", "signal": "WAIT", "status": "OBSERVE_ONLY", "warnings": ["ORDER_FLOW_MISSING"]}
        result = hunter.evaluate(
            "BTCUSD",
            "BTCUSD#",
            {"ai_decision": {}, "strategy_signals": [reader, scalp]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True},
            1,
            50,
        )
        self.assertEqual(result.best_candidate["best_strategy"], "BTC_SCALPING_AGENT")
        self.assertTrue(result.best_candidate["demo_eligible"])

    def test_dashboard_gold_order_flow_card_never_receives_btc_snapshot(self) -> None:
        now = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
        snapshots = {
            "BTCUSD": {"symbol": "BTCUSD", "broker_symbol": "BTCUSD#", "price": 65000, "vwap": 64900, "poc": 64800, "vah": 65100, "val": 64600, "cvd_proxy": 1, "delta_proxy": 2, "created_at": now.isoformat(), "status": "OBSERVE_ONLY"},
            "GOLD": {"symbol": "GOLD", "broker_symbol": "GOLD#", "price": 2300, "vwap": 2299, "poc": 2301, "vah": 2302, "val": 2298, "cvd_proxy": 3, "delta_proxy": 4, "created_at": now.isoformat(), "status": "OBSERVE_ONLY"},
        }
        payload = dashboard_snapshot(settings(), {}, {}, {}, True, now=now, order_flow_snapshots=snapshots)
        self.assertEqual(payload["order_flow"]["tabs"]["GOLD"]["broker_symbol"], "GOLD#")
        self.assertEqual(payload["order_flow"]["tabs"]["GOLD"]["price"], 2300)
        self.assertEqual(payload["order_flow"]["tabs"]["BTCUSD"]["price"], 65000)

    def test_detected_order_flow_symbols_are_observe_only(self) -> None:
        now = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
        payload = dashboard_snapshot(
            settings(),
            {},
            {},
            {},
            True,
            now=now,
            order_flow_snapshots={"US100": {"symbol": "US100", "broker_symbol": "US100", "price": 1, "created_at": now.isoformat(), "status": "OBSERVE_ONLY"}},
        )
        self.assertEqual(payload["order_flow"]["detected_symbols"]["US100"]["mode"], "OBSERVE_ONLY")

    def test_setup_hunter_does_not_fallback_to_generic_gold_when_order_flow_waits(self) -> None:
        hunter = SetupHunter(settings())
        order_flow_wait = self.decision(confidence=0, signal="WAIT")
        order_flow_wait["gold_order_flow_cvd_vwap"]["block_reason"] = "GOLD_ORDER_FLOW_NO_DIVERGENCE"
        generic_buy = {
            "symbol": "GOLD#",
            "strategy": "TREND_CONTINUATION_BREAKDOWN",
            "signal": "BUY",
            "entry": 2300.0,
            "sl": 2299.0,
            "tp": 2302.0,
            "risk_reward": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
        }
        result = hunter.evaluate(
            "GOLD#",
            "GOLD#",
            {"ai_decision": generic_buy, "strategy_signals": [order_flow_wait, generic_buy]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True},
            1,
            50,
        )
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertEqual(result.best_candidate["empty_reason"], "NO_ALLOWED_EXECUTION_CANDIDATE")
        self.assertFalse(any(item["best_strategy"] == "TREND_CONTINUATION_BREAKDOWN" for item in result.candidates))

    def test_gold_order_flow_cannot_trade_btc_or_eur(self) -> None:
        router = self.router(hermes_analysis_only_symbols="")
        for symbol in ["BTCUSD#", "EURUSD"]:
            with self.subTest(symbol=symbol):
                result = self.evaluate(router, self.decision(symbol=symbol), broker_symbol=symbol)
                self.assertIn(result.reason, {"GOLD_ORDER_FLOW_SYMBOL_NOT_GOLD", "EUR_GENERIC_STRATEGY_DISABLED", "SYMBOL_NOT_ALLOWED"})

    def test_block_never_reaches_order_send(self) -> None:
        router = self.router()
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(execution_api) as send:
            items = router.process_decision(self.decision(confidence=69), {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 50, True)
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70")

    def test_invalid_stops_precheck_blocks_before_order_send(self) -> None:
        router = self.router()
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        info = type(
            "Info",
            (),
            {"digits": 2, "point": 0.01, "trade_stops_level": 50, "trade_freeze_level": 0},
        )()
        tick = type("Tick", (), {"bid": 2300.0, "ask": 2300.1})()
        decision = self.decision(confidence=90, sl=2299.8, tp=2300.2)
        decision["gold_order_flow_cvd_vwap"]["sl"] = 2299.8
        decision["gold_order_flow_cvd_vwap"]["tp"] = 2300.2
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_select", return_value=True),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=info),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.mt5.demo_router.mt5.order_check", return_value=None),
            patch(execution_api) as send,
        ):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.1},
                {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01, "point": 0.01},
                1,
                50,
                True,
                now=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
            )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "INVALID_STOPS_PRECHECK")

    def test_order_check_retcode_zero_done_is_pass(self) -> None:
        router = self.router()
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        info = type(
            "Info",
            (),
            {"digits": 2, "point": 0.01, "trade_stops_level": 1, "trade_freeze_level": 0},
        )()
        tick = type("Tick", (), {"bid": 2300.0, "ask": 2300.1})()
        check_result = type("CheckResult", (), {"retcode": 0, "comment": "Done", "order": 0, "deal": 0})()
        send_result = type("SendResult", (), {"retcode": 10009, "comment": "Done", "order": 123456, "deal": 0})()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_select", return_value=True),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=info),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.mt5.demo_router.mt5.order_check", return_value=check_result),
            patch(execution_api, return_value=send_result) as send,
        ):
            items = router.process_decision(
                self.decision(confidence=90),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.1},
                {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01, "point": 0.01},
                1,
                50,
                True,
                now=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
            )
        send.assert_called_once()
        self.assertEqual(items[0]["data"]["order_check"]["decision"], "PASS")
        self.assertEqual(items[0]["data"]["order_retcode"], 10009)

    def test_order_send_still_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matches = []
        allowed = {str(root / "app" / "mt5" / "demo_router.py")}
        for path in (root / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text and str(path) not in allowed:
                matches.append(str(path))
        self.assertEqual(matches, [])

    def test_report_breakdown_includes_order_flow_strategy_metrics(self) -> None:
        setup = {
            "event_type": "SETUP_HUNTER",
            "strategy": "GOLD_ORDER_FLOW_CVD_VWAP",
            "direction": "BUY",
            "demo_eligible": True,
            "execution_candidate": True,
        }
        open_event = {"event_type": "DEMO_ORDER", "strategy": "GOLD_ORDER_FLOW_CVD_VWAP", "order_success": True, "ticket": 123}
        close_win = {"event_type": "DEMO_CLOSE", "strategy": "GOLD_ORDER_FLOW_CVD_VWAP", "pnl": 2.0, "result": "WIN"}
        close_loss = {"event_type": "DEMO_CLOSE", "strategy": "GOLD_ORDER_FLOW_CVD_VWAP", "pnl": -1.0, "result": "LOSS"}
        block = {"event_type": "DEMO_SKIP", "strategy": "GOLD_ORDER_FLOW_CVD_VWAP", "reason": "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70"}
        breakdown = _strategy_report_breakdown([setup, open_event, close_win, close_loss, block], [setup], [open_event], [close_win, close_loss])
        row = breakdown["GOLD_ORDER_FLOW_CVD_VWAP"]
        self.assertEqual(row["trades"], 1)
        self.assertEqual(row["pnl"], 1.0)
        self.assertEqual(row["winrate"], 0.5)
        self.assertEqual(row["profit_factor"], 2.0)
        self.assertEqual(row["blocked_reasons"]["GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70"], 1)
        self.assertEqual(row["signal_count"], 1)
        self.assertEqual(row["order_ready_count"], 1)
        self.assertEqual(row["confirmed_order_count"], 1)


if __name__ == "__main__":
    unittest.main()
