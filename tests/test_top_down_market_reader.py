from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
import pandas as pd

from app.services.top_down_market_reader import (
    TopDownMarketReader,
    detect_fvg,
    premium_discount_zone,
    validate_rr,
)


def frame_from_closes(closes: list[float], start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for idx, close in enumerate(closes):
        open_ = closes[idx - 1] if idx else close
        high = max(open_, close) + 0.3
        low = min(open_, close) - 0.3
        rows.append(
            {
                "candle_time": start + timedelta(minutes=idx),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "spread": 1,
                "tick_volume": 100,
            }
        )
    return pd.DataFrame(rows)


def trend_frame(direction: str = "UP", count: int = 130) -> pd.DataFrame:
    closes = []
    value = 100.0
    for idx in range(count):
        wave = (idx % 10) * 0.08
        if direction == "UP":
            value += 0.18
            closes.append(value + wave)
        elif direction == "DOWN":
            value -= 0.18
            closes.append(value - wave)
        else:
            closes.append(100.0 + ((idx % 12) - 6) * 0.08)
    return frame_from_closes(closes)


def bullish_execution_frame(count: int = 130) -> pd.DataFrame:
    df = trend_frame("UP", count)
    idx = len(df) - 3
    df.loc[idx - 2, ["high", "low", "open", "close"]] = [118.0, 116.0, 117.0, 117.5]
    df.loc[idx - 1, ["high", "low", "open", "close"]] = [117.8, 116.8, 117.2, 117.4]
    df.loc[idx, ["high", "low", "open", "close"]] = [121.8, 119.4, 119.7, 121.4]
    df.loc[idx + 1, ["high", "low", "open", "close"]] = [120.8, 119.1, 120.4, 120.0]
    df.loc[idx + 2, ["high", "low", "open", "close"]] = [123.8, 120.2, 120.6, 123.4]
    return df


def bearish_execution_frame(count: int = 130) -> pd.DataFrame:
    df = trend_frame("DOWN", count)
    idx = len(df) - 3
    df.loc[idx - 2, ["high", "low", "open", "close"]] = [84.0, 82.0, 83.0, 82.5]
    df.loc[idx - 1, ["high", "low", "open", "close"]] = [83.2, 82.2, 82.8, 82.6]
    df.loc[idx, ["high", "low", "open", "close"]] = [80.6, 78.2, 80.3, 78.6]
    df.loc[idx + 1, ["high", "low", "open", "close"]] = [81.0, 79.2, 79.7, 80.1]
    df.loc[idx + 2, ["high", "low", "open", "close"]] = [79.8, 76.2, 79.4, 76.6]
    return df


def aligned_frames(direction: str = "BUY") -> dict[str, pd.DataFrame]:
    if direction == "BUY":
        execution = bullish_execution_frame()
        return {"D1": trend_frame("UP"), "H4": trend_frame("UP"), "H1": trend_frame("UP"), "M15": execution, "M5": execution.copy(), "M1": execution.copy()}
    execution = bearish_execution_frame()
    return {"D1": trend_frame("DOWN"), "H4": trend_frame("DOWN"), "H1": trend_frame("DOWN"), "M15": execution, "M5": execution.copy(), "M1": execution.copy()}


class TopDownMarketReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = TopDownMarketReader()
        self.now = datetime(2026, 6, 1, 2, 10, tzinfo=timezone.utc)

    def test_h4_h1_range_no_m1_trigger_returns_wait_or_fail(self) -> None:
        flat = frame_from_closes([100.0] * 130)
        frames = {tf: flat.copy() for tf in ("D1", "H4", "H1", "M15", "M5", "M1")}
        result = self.reader.evaluate("BTCUSD", frames, "BUY", 100.0, 99.0, 102.0, 1, 30, self.now)
        self.assertIn(result["top_down_status"], {"WAIT", "FAIL"})
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M1_ENTRY", result["missing_confirmations"])

    def test_h4_h1_aligned_m15_m1_rr_valid_returns_pass(self) -> None:
        result = self.reader.evaluate("EURUSD", aligned_frames("BUY"), "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertEqual(result["top_down_status"], "PASS")
        self.assertEqual(result["decision"], "ALLOW_DEMO")
        self.assertGreaterEqual(result["entry_readiness_score"], 75)
        self.assertTrue(result["m15_confirmation"])
        self.assertTrue(result["m1_trigger"])
        self.assertEqual(result["d1_macro_bias"], "BULLISH")
        self.assertEqual(result["h4_main_bias"], "BULLISH")
        self.assertEqual(result["m15_confirmation_status"], "PASS")
        self.assertEqual(result["m1_trigger_status"], "PASS")

    def test_missing_bos_and_ob_fvg_lowers_score(self) -> None:
        frames = {"D1": trend_frame("UP"), "H4": trend_frame("UP"), "H1": trend_frame("UP"), "M15": trend_frame("UP"), "M5": trend_frame("UP"), "M1": trend_frame("UP")}
        result = self.reader.evaluate("EURUSD", frames, "BUY", 122.0, 121.0, 124.0, 1, 30, self.now)
        self.assertIn("OB_FVG", result["missing_confirmations"])
        self.assertLess(result["score_breakdown"]["ob_fvg_ifvg"], 10)

    def test_invalid_buy_sl_tp_detected(self) -> None:
        result = validate_rr("BUY", 100.0, 101.0, 103.0)
        self.assertFalse(result["valid"])
        self.assertEqual(result["score"], -20)

    def test_invalid_sell_sl_tp_detected(self) -> None:
        result = validate_rr("SELL", 100.0, 99.0, 97.0)
        self.assertFalse(result["valid"])
        self.assertEqual(result["score"], -20)

    def test_rr_formula_buy_correct(self) -> None:
        result = validate_rr("BUY", 100.0, 99.0, 102.0)
        self.assertTrue(result["valid"])
        self.assertEqual(result["rr"], 2.0)

    def test_rr_formula_sell_correct(self) -> None:
        result = validate_rr("SELL", 100.0, 101.0, 98.0)
        self.assertTrue(result["valid"])
        self.assertEqual(result["rr"], 2.0)

    def test_spread_fail_returns_avoid(self) -> None:
        result = self.reader.evaluate("EURUSD", aligned_frames("BUY"), "BUY", 123.4, 122.4, 125.6, 40, 30, self.now)
        self.assertEqual(result["decision"], "AVOID")
        self.assertIn("SPREAD_OK", result["missing_confirmations"])

    def test_fvg_bullish_formula_correct(self) -> None:
        df = pd.DataFrame(
            [
                {"open": 1.0, "high": 10.0, "low": 9.0, "close": 9.5},
                {"open": 11.0, "high": 11.5, "low": 10.5, "close": 11.0},
                {"open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5},
            ]
        )
        zones = detect_fvg(df)
        self.assertEqual(zones[0]["type"], "BULLISH_FVG")
        self.assertEqual(zones[0]["low"], 10.0)
        self.assertEqual(zones[0]["high"], 11.0)

    def test_fvg_bearish_formula_correct(self) -> None:
        df = pd.DataFrame(
            [
                {"open": 12.0, "high": 13.0, "low": 11.0, "close": 11.5},
                {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0},
                {"open": 8.0, "high": 9.0, "low": 7.0, "close": 7.5},
            ]
        )
        zones = detect_fvg(df)
        self.assertEqual(zones[0]["type"], "BEARISH_FVG")
        self.assertEqual(zones[0]["low"], 9.0)
        self.assertEqual(zones[0]["high"], 11.0)

    def test_ote_premium_discount_range_calculation_correct(self) -> None:
        self.assertEqual(premium_discount_zone(0.20), "DISCOUNT")
        self.assertEqual(premium_discount_zone(0.50), "MID")
        self.assertEqual(premium_discount_zone(0.80), "PREMIUM")

    def test_bos_choch_detection_works(self) -> None:
        result = self.reader.evaluate("EURUSD", aligned_frames("BUY"), "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertTrue(result["bos_choch"])
        self.assertEqual(result["score_breakdown"]["bos_choch"], 12)

    def test_m1_trigger_stale_over_five_candles_returns_wait(self) -> None:
        frames = aligned_frames("BUY")
        m1 = frames["M1"].copy()
        for idx in range(len(m1) - 5, len(m1)):
            m1.loc[idx, ["open", "high", "low", "close"]] = [120.0, 120.1, 119.9, 120.0]
        frames["M1"] = m1
        result = self.reader.evaluate("EURUSD", frames, "BUY", 120.0, 119.0, 122.0, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M1_ENTRY", result["missing_confirmations"])

    def test_no_lookahead_uses_only_candles_before_decision_time(self) -> None:
        frames = aligned_frames("BUY")
        future_time = self.now + timedelta(hours=1)
        for timeframe, df in list(frames.items()):
            future = df.iloc[-1:].copy()
            future["candle_time"] = future_time
            future[["open", "high", "low", "close"]] = [200.0, 210.0, 199.0, 209.0]
            frames[timeframe] = pd.concat([df, future], ignore_index=True)
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertLess(result["entry_readiness_score"], 100)

    def test_m1_trigger_alone_cannot_allow_demo(self) -> None:
        flat = frame_from_closes([100.0] * 130)
        frames = {tf: flat.copy() for tf in ("D1", "H4", "H1", "M15", "M5")}
        frames["M1"] = bullish_execution_frame()
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M15_CONFIRMATION", result["missing_confirmations"])
        self.assertIn("H4_DIRECTION_CLEAR", result["missing_confirmations"])

    def test_m5_signal_alone_cannot_allow_demo(self) -> None:
        flat = frame_from_closes([100.0] * 130)
        frames = {tf: flat.copy() for tf in ("D1", "H4", "H1", "M15", "M1")}
        frames["M5"] = bullish_execution_frame()
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M15_CONFIRMATION", result["missing_confirmations"])
        self.assertIn("M1_ENTRY", result["missing_confirmations"])

    def test_h4_h1_conflict_returns_wait_or_fail(self) -> None:
        frames = aligned_frames("BUY")
        frames["H1"] = trend_frame("DOWN")
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertIn(result["top_down_status"], {"WAIT", "FAIL"})
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertEqual(result["timeframe_alignment_status"], "CONFLICT")
        self.assertIn("H1_CONFLICT", result["missing_confirmations"])

    def test_h4_range_requires_sweep_bos_m15_and_m1(self) -> None:
        frames = aligned_frames("BUY")
        frames["H4"] = trend_frame("RANGE")
        frames["M15"] = trend_frame("UP")
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("H4_DIRECTION_CLEAR", result["missing_confirmations"])
        self.assertIn("LIQUIDITY_SWEEP", result["missing_confirmations"])

    def test_d1_h4_alignment_boosts_score(self) -> None:
        aligned = aligned_frames("BUY")
        conflicted = aligned_frames("BUY")
        conflicted["D1"] = trend_frame("DOWN")
        aligned_result = self.reader.evaluate("EURUSD", aligned, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        conflicted_result = self.reader.evaluate("EURUSD", conflicted, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertGreater(aligned_result["entry_readiness_score"], conflicted_result["entry_readiness_score"])
        self.assertGreater(aligned_result["score_breakdown"]["d1_macro_bias"], conflicted_result["score_breakdown"]["d1_macro_bias"])

    def test_m15_missing_blocks_allow_demo(self) -> None:
        frames = aligned_frames("BUY")
        frames["M15"] = frame_from_closes([100.0] * 130)
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M15_CONFIRMATION", result["missing_confirmations"])

    def test_m1_missing_blocks_allow_demo(self) -> None:
        frames = aligned_frames("BUY")
        frames["M1"] = frame_from_closes([100.0] * 130)
        result = self.reader.evaluate("EURUSD", frames, "BUY", 123.4, 122.4, 125.6, 1, 30, self.now)
        self.assertNotEqual(result["decision"], "ALLOW_DEMO")
        self.assertIn("M1_ENTRY", result["missing_confirmations"])

if __name__ == "__main__":
    unittest.main()
