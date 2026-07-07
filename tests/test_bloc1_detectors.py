"""BLOC 1 — broken detectors resurrection tests.

Proves the three fixes:
a) MTF_STRUCTURE._direction no longer requires the mutually-exclusive
   bias-at-highs + price-at-lows combination (5943/5943 WAIT measured).
b) SMC_TAGGER accepts body-close OR wick breakout and 2-of-3 timeframe
   confirmations (simultaneous 3-TF body breakout was unreachable, 0/5943).
c) Zone tolerances are ATR-relative; SFP sweeps require an ATR fraction.
"""
from __future__ import annotations

import unittest

import pandas as pd

from app.agents.mtf_structure_detector import (
    MTFStructureDetector,
    _atr_value,
    _last_closed_rejects,
)
from app.agents.smc_confluence_tagger import (
    _status_reason,
    _structure_confirmation,
    _supply_demand_zone,
    _zone_tolerance,
)
from app.config import Settings
from app.strategies.order_flow_execution_agent import _check_sfp


def _frame(rows: list[tuple[float, float, float, float]], volume: float | None = None) -> pd.DataFrame:
    data = {
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
    }
    if volume is not None:
        data["tick_volume"] = [volume] * len(rows)
    return pd.DataFrame(data)


def _h4_uptrend(n: int = 20, red_last_closed: bool = False) -> pd.DataFrame:
    rows = []
    for i in range(n):
        open_ = 100.0 + 2.0 * i
        close = open_ + 2.0
        rows.append((open_, close + 0.5, open_ - 0.5, close))
    if red_last_closed:
        # keep highs/lows ascending but close the candle red (rejection body)
        open_, high, low, _ = rows[-2]
        rows[-2] = (open_ + 1.9, high, low, open_ + 0.1)
    return _frame(rows)


def _h4_downtrend(n: int = 20) -> pd.DataFrame:
    rows = []
    for i in range(n):
        open_ = 200.0 - 2.0 * i
        close = open_ - 2.0
        rows.append((open_, open_ + 0.5, close - 0.5, close))
    return _frame(rows)


def _m15_buy_confirm() -> pd.DataFrame:
    rows = [(100.0, 101.0, 99.0, 100.5)] * 7
    rows.append((100.5, 100.9, 99.9, 100.4))   # r7
    rows.append((100.4, 100.8, 99.8, 100.1))   # r8 correction (close < r7 close)
    rows.append((100.2, 101.8, 100.0, 101.6))  # r9 body breakout above 101
    return _frame(rows)


def _m1_buy_confirm() -> pd.DataFrame:
    rows = [(100.0, 100.5, 99.5, 100.0)] * 5
    rows.append((100.0, 100.4, 99.8, 100.3))   # r5
    rows.append((100.3, 100.4, 99.9, 100.1))   # r6 pullback (close < r5 close)
    rows.append((100.2, 101.0, 100.0, 100.9))  # r7 breakout above 100.5
    return _frame(rows)


def _mirror(df: pd.DataFrame, pivot: float = 200.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": pivot - df["open"],
            "high": pivot - df["low"],
            "low": pivot - df["high"],
            "close": pivot - df["close"],
        }
    )


class TestMtfDirectionResurrected(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = MTFStructureDetector(Settings(mtf_structure_enabled=True))

    def test_textbook_bullish_breakout_emits_buy(self) -> None:
        frames = {"H4": _h4_uptrend(), "M15": _m15_buy_confirm(), "M1": _m1_buy_confirm()}
        result = self.detector.evaluate("GOLD#", frames, "BUY")
        self.assertEqual(result["h4_bias"], "BULLISH")
        self.assertEqual(result["mtf_structure_direction"], "BUY")
        self.assertEqual(result["mtf_structure_status"], "PASS")
        self.assertGreaterEqual(result["mtf_structure_score"], 80)
        self.assertEqual(result["mtf_structure_reason"], "MTF_STRUCTURE_ALIGNED")

    def test_textbook_bearish_breakdown_emits_sell(self) -> None:
        frames = {
            "H4": _h4_downtrend(),
            "M15": _mirror(_m15_buy_confirm()),
            "M1": _mirror(_m1_buy_confirm()),
        }
        result = self.detector.evaluate("GOLD#", frames, "SELL")
        self.assertEqual(result["h4_bias"], "BEARISH")
        self.assertEqual(result["mtf_structure_direction"], "SELL")
        self.assertEqual(result["mtf_structure_status"], "PASS")
        self.assertGreaterEqual(result["mtf_structure_score"], 80)

    def test_score_uses_full_scale_not_capped_at_50(self) -> None:
        frames = {"H4": _h4_uptrend(), "M15": _m15_buy_confirm(), "M1": _m1_buy_confirm()}
        result = self.detector.evaluate("GOLD#", frames, "BUY")
        self.assertGreater(result["mtf_structure_score"], 50)

    def test_genuine_contradiction_still_waits(self) -> None:
        h4 = _h4_uptrend(red_last_closed=True)
        # price parked exactly at the H4 resistance while last closed H4 rejected
        resistance = float(h4["high"].iloc[:-1].tail(10).max())
        m15 = _m15_buy_confirm()
        m15.loc[m15.index[-1], "close"] = resistance
        frames = {"H4": h4, "M15": m15, "M1": _m1_buy_confirm()}
        result = self.detector.evaluate("GOLD#", frames, "BUY")
        self.assertEqual(result["mtf_structure_direction"], "WAIT")
        self.assertIn("H4_ZONE_CONTRADICTION", result["mtf_structure_reason"])

    def test_mtf_dir_log_emitted(self) -> None:
        frames = {"H4": _h4_uptrend(), "M15": _m15_buy_confirm(), "M1": _m1_buy_confirm()}
        with self.assertLogs("hermes", level="INFO") as captured:
            self.detector.evaluate("GOLD#", frames, "BUY")
        self.assertTrue(any("[MTF_DIR]" in line for line in captured.output))

    def test_last_closed_rejects_uses_closed_candle_only(self) -> None:
        df = _h4_uptrend(red_last_closed=True)
        self.assertTrue(_last_closed_rejects(df, "BUY"))
        self.assertFalse(_last_closed_rejects(_h4_uptrend(), "BUY"))


class TestSmcTwoOfThreeAndWick(unittest.TestCase):
    def _wick_breakout_frame(self) -> pd.DataFrame:
        rows = [(100.0, 101.0, 99.0, 100.5)] * 5
        # closed candle: wick above 101 prior high, body closes back inside
        rows.append((100.4, 101.6, 100.0, 100.8))
        rows.append((100.8, 100.9, 100.2, 100.5))  # live candle (excluded)
        return _frame(rows)

    def test_wick_breakout_confirms(self) -> None:
        self.assertTrue(_structure_confirmation(self._wick_breakout_frame(), "BUY"))

    def test_body_breakout_still_confirms(self) -> None:
        rows = [(100.0, 101.0, 99.0, 100.5)] * 5
        rows.append((100.5, 101.9, 100.2, 101.7))  # body close beyond prior high
        rows.append((101.7, 101.8, 101.0, 101.2))  # live candle
        self.assertTrue(_structure_confirmation(_frame(rows), "BUY"))

    def test_no_breakout_does_not_confirm(self) -> None:
        rows = [(100.0, 101.0, 99.0, 100.5)] * 6
        rows.append((100.5, 100.9, 100.0, 100.3))  # live candle
        self.assertFalse(_structure_confirmation(_frame(rows), "BUY"))

    def test_two_of_three_pass_at_60(self) -> None:
        status, reason = _status_reason("BUY", 60, [], True, True, False)
        self.assertEqual(status, "PASS")
        self.assertEqual(reason, "SMC_CONFLUENCE_ALIGNED")
        status, reason = _status_reason("BUY", 60, [], True, False, True)
        self.assertEqual(status, "PASS")

    def test_one_of_three_fails(self) -> None:
        status, reason = _status_reason("BUY", 75, [], True, False, False)
        self.assertEqual(status, "FAIL")
        self.assertIn("NO_M5_CONFIRMATION", reason)

    def test_pass_threshold_60_unchanged(self) -> None:
        status, reason = _status_reason("BUY", 59, [], True, True, True)
        self.assertEqual(status, "FAIL")
        self.assertIn("LOW_SMC_CONFLUENCE_SCORE", reason)


class TestAtrRelativeTolerances(unittest.TestCase):
    def _high_vol_frame(self) -> pd.DataFrame:
        # gold-like: price ~3300, true range ~40 -> ATR ~40, old pct tol ~8
        rows = []
        for _ in range(19):
            rows.append((3280.0, 3300.0, 3260.0, 3280.0))
        rows.append((3280.0, 3290.0, 3270.0, 3275.0))  # close 15 above the 3260 support
        return _frame(rows)

    def test_zone_tolerance_scales_with_atr(self) -> None:
        df = self._high_vol_frame()
        close = float(df["close"].iloc[-1])
        tol = _zone_tolerance(df, close)
        self.assertGreater(tol, abs(close) * 0.0025)  # wider than the old fixed pct

    def test_zone_detected_at_atr_distance_missed_by_fixed_pct(self) -> None:
        df = self._high_vol_frame()
        # distance to support = 15 > old ~$8 tolerance, < 0.5*ATR (~20)
        self.assertEqual(_supply_demand_zone(df, "BULLISH"), "DEMAND")

    def test_atr_value_none_on_short_frames(self) -> None:
        self.assertIsNone(_atr_value(_frame([(1, 2, 0.5, 1.5)] * 3)))


class TestSfpAtrFraction(unittest.TestCase):
    def _sfp_frame(self, last_high: float, last_close: float, last_vol: float) -> pd.DataFrame:
        rows = [(102.0, 105.0, 100.0, 103.0)] * 15
        rows.append((103.0, last_high, 101.0, last_close))  # last CLOSED candle
        rows.append((103.0, 104.0, 102.0, 103.0))           # live candle (excluded)
        df = _frame(rows, volume=100.0)
        df.loc[df.index[-2], "tick_volume"] = last_vol
        return df

    def test_micro_wick_below_atr_fraction_is_not_a_sweep(self) -> None:
        # ATR ~5 -> margin 0.5; a 0.2 poke above 105 must NOT count as a sweep
        result = _check_sfp(self._sfp_frame(105.2, 103.0, 300.0), "SELL")
        self.assertIsNone(result)

    def test_real_sweep_with_reversal_and_volume_confirms(self) -> None:
        result = _check_sfp(self._sfp_frame(108.0, 104.0, 300.0), "SELL")
        self.assertTrue(result)

    def test_sweep_without_reversal_returns_false(self) -> None:
        result = _check_sfp(self._sfp_frame(108.0, 107.5, 300.0), "SELL")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
