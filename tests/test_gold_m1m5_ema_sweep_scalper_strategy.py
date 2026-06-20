from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.config import Settings
from app.main import HermesBackend
from app.mt5.demo_router import DemoKellyRouter
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
        "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD",
        "hermes_analysis_only_symbols": "",
        "gold_liquidity_mode": "trade",
        "gold_liquidity_strategy_enabled": True,
        "gold_disable_generic_strategies": True,
        "gold_max_open_trades": 7,
        "hermes_adaptive_confluence_enabled": False,
        "report_timezone": "UTC",
        "timezone_local": "UTC",
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
        "max_spread_gold": 30,
    }
    values.update(overrides)
    return Settings(**values)


def ohlcv(close: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 100.0} for c in close]
    )


def m5_frame(kind: str = "bullish") -> pd.DataFrame:
    if kind == "bullish":
        closes = [float(i) for i in range(1, 260)]
    elif kind == "bearish":
        closes = [float(260 - i) for i in range(1, 260)]
    else:
        closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(260)]
    return ohlcv(closes)


def prepared_m1(
    direction: str = "BUY",
    *,
    pullback: bool = True,
    cross: bool = True,
    rsi: float = 60.0,
    atr: float = 1.0,
    atr_lo: float = 0.5,
    atr_hi: float = 2.0,
    structure: bool = True,
    sweep: bool = False,
    beyond: bool = True,
) -> pd.DataFrame:
    n = 20
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [100.0] * n,
            "ema_fast": [100.0] * n,
            "ema_slow": [100.0] * n,
            "rsi": [rsi] * n,
            "atr": [atr] * n,
            "atr_lo": [atr_lo] * n,
            "atr_hi": [atr_hi] * n,
            "swing_high": [104.0] * n,
            "swing_low": [99.0] * n,
        }
    )
    i = n - 1
    j = i - scalper.CROSS_LOOKBACK
    if direction == "BUY":
        df.loc[j, ["ema_fast", "ema_slow"]] = [100.0, 101.0] if cross else [102.0, 101.0]
        df.loc[i, ["ema_fast", "ema_slow"]] = [102.0, 101.0] if sweep else ([105.0, 104.0] if cross else [100.0, 101.0])
        df.loc[i, "close"] = 106.0 if structure else 103.0
        df.loc[i, "high"] = 106.5
        df.loc[i, "low"] = 98.5 if sweep else 105.0
        if not beyond:
            df.loc[i, "close"] = 104.0
        if not pullback:
            for idx in range(i - scalper.SETUP_WINDOW, i + 1):
                df.loc[idx, ["close", "ema_slow"]] = [106.0, 100.0]
        else:
            df.loc[i - 2, ["close", "ema_slow"]] = [100.0, 100.2]
    else:
        df.loc[j, ["ema_fast", "ema_slow"]] = [101.0, 100.0] if cross else [99.0, 100.0]
        df.loc[i, ["ema_fast", "ema_slow"]] = ([99.0, 100.0] if structure else [101.0, 102.0]) if cross else [101.0, 100.0]
        df.loc[i, "close"] = 98.0 if structure else 100.5
        df.loc[i, "low"] = 97.5
        df.loc[i, "high"] = 105.0 if sweep else 99.0
        if not beyond:
            df.loc[i, "close"] = 102.0
        if not pullback:
            df.loc[i - 2, ["close", "ema_slow"]] = [94.0, 100.0]
        else:
            df.loc[i - 2, ["close", "ema_slow"]] = [100.0, 100.2]
    if sweep:
        df.loc[i, "swing_high"] = 104.0
        df.loc[i, "swing_low"] = 99.0
    if not structure and not sweep:
        df.loc[i, "swing_high"] = 110.0
        df.loc[i, "swing_low"] = 90.0
    return df


class GoldM1M5ServiceTests(unittest.TestCase):
    def analyze(self, m1: pd.DataFrame, trend: str = "bullish", direction: int = 1, spread: float = 1, hour: int = 10, relaxed: bool = False) -> dict:
        m5 = m5_frame(trend)
        trend_dir, reason = scalper._trend_context(m5)
        if direction in {-1, 0, 1}:
            trend_dir = direction
        state = {"active": relaxed, "reason": "NO_SETUP_24H" if relaxed else None, "hours_without_setup": 24 if relaxed else None}
        return scalper._analyze("GOLD#", m1, m5, trend_dir, spread, hour, reason, settings(), state)

    def test_non_gold_returns_immediately_without_indicators(self) -> None:
        for symbol in ("BTCUSD", "EURUSD"):
            with self.subTest(symbol=symbol), patch.object(scalper, "_prepare_entry", side_effect=AssertionError("no indicators")):
                result = scalper.evaluate(symbol, {"M1": object(), "M5": object()}, {}, settings())
            self.assertEqual(result["signal"], "WAIT")
            self.assertEqual(result["reason"], "SYMBOL_NOT_GOLD")

    def test_candle_validation(self) -> None:
        s = settings()
        m1 = ohlcv([100.0] * 260)
        m5 = m5_frame("bullish")
        self.assertEqual(scalper.evaluate_gold_m1m5_scalper("GOLD#", None, m5, {}, s)["block_reason"], "NO_M1_CANDLES")
        self.assertEqual(scalper.evaluate_gold_m1m5_scalper("GOLD#", m1, None, {}, s)["block_reason"], "NO_M5_CANDLES")
        self.assertEqual(scalper.evaluate_gold_m1m5_scalper("GOLD#", ohlcv([100.0] * 20), m5, {}, s)["block_reason"], "NOT_ENOUGH_CANDLES")
        self.assertEqual(scalper.evaluate_gold_m1m5_scalper("GOLD#", pd.DataFrame({"bad": [1]}), m5, {}, s)["block_reason"], "NO_M1_CANDLES")

    def test_m5_trend_detection(self) -> None:
        self.assertEqual(scalper._m5_snapshot(m5_frame("bullish"), 1)["trend_m5"], "BULLISH")
        self.assertEqual(scalper._m5_snapshot(m5_frame("bearish"), -1)["trend_m5"], "BEARISH")
        result = self.analyze(prepared_m1(), "mixed", direction=0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["block_reason"], "NO_M5_TREND")

    def test_pullback_logic(self) -> None:
        self.assertTrue(scalper._setup_detection(prepared_m1(pullback=True), 1, 19))
        result = self.analyze(prepared_m1(pullback=False))
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["block_reason"], "NO_PULLBACK")

    def test_ema_cross_logic(self) -> None:
        self.assertTrue(scalper._cross(prepared_m1("BUY", cross=True), 19, 1))
        self.assertTrue(scalper._cross(prepared_m1("SELL", cross=True), 19, -1))
        result = self.analyze(prepared_m1("BUY", cross=False))
        self.assertEqual(result["block_reason"], "NO_EMA_CROSS")

    def test_rsi_filters_and_scoring(self) -> None:
        self.assertEqual(self.analyze(prepared_m1("BUY", rsi=71))["block_reason"], "RSI_OVERBOUGHT")
        self.assertEqual(self.analyze(prepared_m1("SELL", rsi=29), "bearish", -1)["block_reason"], "RSI_OVERSOLD")
        self.assertEqual(self.analyze(prepared_m1("BUY", rsi=60))["score_components"]["rsi_zone"], 10)
        self.assertEqual(self.analyze(prepared_m1("SELL", rsi=40), "bearish", -1)["score_components"]["rsi_zone"], 10)

    def test_atr_band_filters_and_score(self) -> None:
        self.assertEqual(self.analyze(prepared_m1(atr=0.4, atr_lo=0.5))["block_reason"], "ATR_TOO_LOW")
        self.assertEqual(self.analyze(prepared_m1(atr=2.5, atr_hi=2.0))["block_reason"], "ATR_TOO_HIGH")
        self.assertEqual(self.analyze(prepared_m1())["score_components"]["atr_band"], 10)

    def test_relaxed_atr_low_threshold_can_pass_without_changing_strict(self) -> None:
        strict = self.analyze(prepared_m1(atr=0.45, atr_lo=0.5))
        relaxed = self.analyze(prepared_m1(atr=0.45, atr_lo=0.5), relaxed=True)
        self.assertEqual(strict["block_reason"], "ATR_TOO_LOW")
        self.assertEqual(relaxed["decision"], "BUY")
        raw = relaxed["gold_m1_m5_ema_sweep_scalper"]
        self.assertTrue(raw["relaxed_mode_active"])
        self.assertEqual(raw["relaxed_threshold"], 65)

    def test_fractal_swing_no_lookahead(self) -> None:
        df = ohlcv([100.0] * 12)
        df.loc[6, "high"] = 120.0
        prepared = scalper._prepare_entry(df)
        self.assertNotEqual(float(prepared.loc[6, "swing_high"]), 120.0)
        self.assertNotEqual(float(prepared.loc[7, "swing_high"]), 120.0)
        self.assertEqual(float(prepared.loc[8, "swing_high"]), 120.0)
        df.loc[11, "high"] = 130.0
        prepared = scalper._prepare_entry(df)
        self.assertNotEqual(float(prepared.loc[11, "swing_high"]), 130.0)

    def test_buy_structure_confirmation_and_sl_tp(self) -> None:
        result = self.analyze(prepared_m1("BUY", structure=True, sweep=False))
        self.assertEqual(result["decision"], "BUY")
        raw = result["gold_m1_m5_ema_sweep_scalper"]
        self.assertGreaterEqual(raw["score"], 75)
        self.assertTrue(raw["structure_break"])
        entry, sl, atr = raw["entry"], raw["sl"], raw["atr"]
        self.assertAlmostEqual(sl, raw["swing_low"] - atr * 0.3)
        self.assertAlmostEqual(raw["tp1"], entry + abs(entry - sl) * 1.0)
        self.assertAlmostEqual(raw["tp2"], entry + abs(entry - sl) * 1.5)
        self.assertAlmostEqual(raw["rr_to_tp1"], 1.0)

    def test_buy_liquidity_sweep_confirmation(self) -> None:
        result = self.analyze(prepared_m1("BUY", structure=False, sweep=True))
        self.assertEqual(result["decision"], "BUY")
        self.assertTrue(result["gold_m1_m5_ema_sweep_scalper"]["liquidity_sweep"])

    def test_sell_structure_confirmation_and_sl_tp(self) -> None:
        result = self.analyze(prepared_m1("SELL", structure=True, sweep=False, rsi=40), "bearish", -1)
        self.assertEqual(result["decision"], "SELL")
        raw = result["gold_m1_m5_ema_sweep_scalper"]
        entry, sl, atr = raw["entry"], raw["sl"], raw["atr"]
        self.assertAlmostEqual(sl, raw["swing_high"] + atr * 0.3)
        self.assertAlmostEqual(raw["tp1"], entry - abs(entry - sl) * 1.0)
        self.assertAlmostEqual(raw["tp2"], entry - abs(entry - sl) * 1.5)

    def test_sell_liquidity_sweep_confirmation(self) -> None:
        result = self.analyze(prepared_m1("SELL", structure=False, sweep=True, rsi=40), "bearish", -1)
        self.assertEqual(result["decision"], "SELL")
        self.assertTrue(result["gold_m1_m5_ema_sweep_scalper"]["liquidity_sweep"])

    def test_scoring_thresholds_and_components(self) -> None:
        trade = self.analyze(prepared_m1("BUY", rsi=60), hour=10)
        self.assertEqual(trade["decision"], "BUY")
        self.assertGreaterEqual(trade["score"], 75)
        partial = self.analyze(prepared_m1("BUY", rsi=69), hour=1)
        self.assertEqual(partial["decision"], "WAIT")
        self.assertEqual(partial["block_reason"], "PARTIAL_SETUP")
        low = self.analyze(prepared_m1("BUY", cross=False, structure=False, sweep=False, rsi=50), hour=1)
        self.assertEqual(low["decision"], "WAIT")
        self.assertLess(low["score"], 60)
        self.assertEqual(
            trade["score_components"],
            {"trend": 20, "ema_cross": 15, "rsi_zone": 10, "atr_band": 10, "structure_break": 15, "liquidity_sweep": 0, "spread": 5, "session": 5},
        )

    def test_partial_setup_score_60_to_74_waits(self) -> None:
        result = self.analyze(prepared_m1("BUY", rsi=69), hour=1)
        self.assertEqual(result["decision"], "WAIT")

    def test_spread_filter(self) -> None:
        high = self.analyze(prepared_m1(), spread=31)
        self.assertEqual(high["block_reason"], "SPREAD_HIGH")
        ok = self.analyze(prepared_m1(), spread=30)
        self.assertEqual(ok["score_components"]["spread"], 5)

    def test_payload_shape(self) -> None:
        signal = scalper._signal_from_result("GOLD#", self.analyze(prepared_m1()))
        raw = signal["gold_m1_m5_ema_sweep_scalper"]
        expected = {
            "enabled", "mode", "strategy", "decision", "trend_m5", "ema20_m5", "ema50_m5", "ema200_m5",
            "ema9_m1", "ema21_m1", "rsi", "atr", "atr_lo", "atr_hi", "pullback_ok", "ema_cross",
            "close_beyond_ema9", "structure_break", "liquidity_sweep", "swing_high", "swing_low",
            "score", "min_score_trade", "entry", "sl", "tp1", "tp2", "rr_to_tp1", "final_lot",
            "block_reason", "warnings",
        }
        self.assertTrue(expected.issubset(raw.keys()))
        self.assertEqual(raw["strategy"], scalper.STRATEGY)


class GoldM1M5RouterPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events_path = Path(self.tmp.name) / "events.jsonl"
        self.now = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def router(self) -> DemoKellyRouter:
        return DemoKellyRouter(settings(), self.events_path)

    def account(self) -> dict:
        return {"login": 1, "trade_mode": 0, "trade_allowed": True, "trade_expert": True, "balance": 10000, "equity": 10000}

    def specs(self) -> dict:
        return {"tick_value": 1.0, "tick_size": 0.1, "volume_step": 0.01}

    def scalper_decision(self, symbol: str = "GOLD#") -> dict:
        raw = scalper._decision("BUY", 80, 2300.0, 2299.0, 2301.0, 2301.5, symbol=symbol, trend_m5="BULLISH", rr=1.5)
        return scalper._signal_from_result(symbol, raw)

    def evaluate(self, decision: dict, broker_symbol: str):
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            return self.router().evaluate(decision, {"approved_lot": 0.01}, self.account(), broker_symbol, {}, {"bid": 2300.0, "ask": 2300.1}, self.specs(), 1, 30, True, now=self.now)

    def test_gold_symbols_route_for_scalper(self) -> None:
        for symbol, broker in (("GOLD#", "GOLD#"), ("GOLD", "GOLD#"), ("XAUUSD", "XAUUSD")):
            with self.subTest(symbol=symbol):
                result = self.evaluate(self.scalper_decision(symbol), broker)
            self.assertEqual(result.decision, "PASS")

    def test_scalper_not_routeable_for_btc_or_eur(self) -> None:
        self.assertEqual(self.evaluate(self.scalper_decision("BTCUSD#"), "BTCUSD#").reason, "GOLD_M1_M5_SYMBOL_NOT_GOLD")
        self.assertEqual(self.evaluate(self.scalper_decision("EURUSD"), "EURUSD").reason, "GOLD_M1_M5_SYMBOL_NOT_GOLD")

    def test_generic_gold_disabled_and_gold_liquidity_still_routeable(self) -> None:
        generic = {
            **self.scalper_decision("GOLD#"),
            "strategy": "BREAKOUT_RETEST",
            "setup_type": "BREAKOUT_RETEST",
            "gold_m1m5_scalper": None,
        }
        self.assertEqual(self.evaluate(generic, "GOLD#").reason, "GOLD_GENERIC_STRATEGY_DISABLED")
        liquidity = {
            **self.scalper_decision("GOLD#"),
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "gold_liquidity_hunter": {"decision": "BUY", "reversal_signal": "ABS", "sweep_side": "SSL", "premium_discount": "DISCOUNT", "zone_stars": 4, "liquidity_score": 85, "rr_plan": {"rr": 2.0}},
            "gold_liquidity_signal": "ABS",
            "gold_liquidity_score": 85,
            "reward_risk": 2.0,
        }
        self.assertEqual(self.evaluate(liquidity, "GOLD#").decision, "PASS")

    def test_wait_and_block_never_reach_order_send(self) -> None:
        wait = self.scalper_decision("GOLD#")
        wait.update({"signal": "WAIT", "direction": "WAIT"})
        blocked = self.scalper_decision("GOLD#")
        blocked["gold_m1m5_scalper"] = {**blocked["gold_m1m5_scalper"], "decision": "BLOCK", "block_reason": "UNIT_BLOCK"}
        execution_api = "app.mt5.demo_router.mt5." + "order_" + "send"
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(execution_api) as send:
            wait_items = self.router().process_decision(wait, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 30, True)
            block_items = self.router().process_decision(blocked, {"approved_lot": 0.01}, self.account(), "GOLD#", {}, {}, self.specs(), 1, 30, True)
        send.assert_not_called()
        self.assertEqual(wait_items, [])
        self.assertEqual(block_items[0]["data"]["reason"], "UNIT_BLOCK")

    def test_buy_sell_are_candidates_execution_stays_in_demo_router(self) -> None:
        backend = HermesBackend.__new__(HermesBackend)
        self.assertTrue(backend._should_route_to_demo({"strategy": scalper.STRATEGY, "signal": "BUY"}))
        paths = []
        root = Path(__file__).resolve().parents[1]
        for path in (root / "app").rglob("*.py"):
            if "order_send" in path.read_text(encoding="utf-8", errors="ignore"):
                paths.append(path.relative_to(root).as_posix())
        self.assertEqual(paths, ["app/mt5/demo_router.py"])


if __name__ == "__main__":
    unittest.main()
