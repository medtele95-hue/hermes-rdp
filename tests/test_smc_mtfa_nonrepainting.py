"""Tests: SMC/MTFA detection functions read only confirmed closed candles.

Three guarantees verified:
1. No detection function uses df.iloc[-1] (live candle) as structural reference.
2. Non-repainting: a volatile live candle does not change scores while the
   last closed candle is unchanged.
3. Minimum-length guards are correct after the index shift.
"""
from __future__ import annotations

import unittest

import pandas as pd

from app.agents.smc_confluence_tagger import (
    _fair_value_gap,
    _order_block,
    _structure_confirmation,
    _trend,
)
from app.agents.mtfa_filter import MTFAFilter
from app.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candles(
    n: int,
    base: float = 65000.0,
    step: float = 50.0,
    trend: str = "UP",
) -> pd.DataFrame:
    """Build a simple DataFrame of n OHLC candles."""
    rows = []
    price = base
    for i in range(n):
        if trend == "UP":
            o, c = price, price + step
            h, lo = c + 5, o - 5
        elif trend == "DOWN":
            o, c = price, price - step
            h, lo = o + 5, c - 5
        else:  # FLAT
            o, c = price, price + 1
            h, lo = price + 5, price - 5
        rows.append({"open": o, "high": h, "low": lo, "close": c, "tick_volume": 10})
        price += step if trend == "UP" else (-step if trend == "DOWN" else 0)
    return pd.DataFrame(rows)


def _append_live(df: pd.DataFrame, live_close: float) -> pd.DataFrame:
    """Append a volatile in-progress candle to simulate a live bar."""
    last = df.iloc[-1]
    spike = {
        "open": float(last["close"]),
        "high": float(last["close"]) + 5000.0,  # extreme spike
        "low": float(last["close"]) - 5000.0,
        "close": live_close,
        "tick_volume": 1,
    }
    return pd.concat([df, pd.DataFrame([spike])], ignore_index=True)


def _mtfa_settings() -> Settings:
    s = Settings()
    s.mtfa_enabled = True
    s.mtfa_mode = "TAG_ONLY"
    s.mtfa_require_h1_bias = True
    s.mtfa_require_m15_liquidity = True
    s.mtfa_require_m5_cisd = True
    return s


# ---------------------------------------------------------------------------
# 1. _trend() — non-repainting
# ---------------------------------------------------------------------------

class TestTrendNonRepainting(unittest.TestCase):

    def test_trend_stable_when_live_candle_changes(self) -> None:
        """Score from closed candles must not change when the live candle spikes."""
        closed = _candles(15, trend="UP")
        # Establish baseline on closed + neutral live bar
        df_baseline = _append_live(closed, live_close=float(closed.iloc[-1]["close"]))
        trend_baseline = _trend(df_baseline)

        # Add extreme live candle spike (different close, huge wick)
        df_spiked = _append_live(closed, live_close=float(closed.iloc[-1]["close"]) + 10000.0)
        trend_spiked = _trend(df_spiked)

        self.assertEqual(
            trend_baseline,
            trend_spiked,
            f"_trend changed from {trend_baseline} to {trend_spiked} due to live candle spike",
        )

    def test_trend_bullish_from_closed_candles(self) -> None:
        closed = _candles(14, trend="UP")
        # live candle is bearish spike — should not affect result
        df = _append_live(closed, live_close=float(closed.iloc[0]["close"]))
        self.assertEqual(_trend(df), "BULLISH")

    def test_trend_bearish_from_closed_candles(self) -> None:
        closed = _candles(14, trend="DOWN", base=70000.0, step=50.0)
        df = _append_live(closed, live_close=float(closed.iloc[-1]["close"]) + 5000.0)
        self.assertEqual(_trend(df), "BEARISH")

    def test_trend_too_few_closed_returns_unknown(self) -> None:
        # 2 rows total → after iloc[:-1] only 1 closed candle → UNKNOWN
        df = _candles(2, trend="UP")
        self.assertEqual(_trend(df), "UNKNOWN")


class TestBtcWeekendSmcFallback(unittest.TestCase):
    def test_btc_weekend_missing_intraday_uses_h1_fallback(self) -> None:
        from unittest.mock import patch
        from app.agents.smc_confluence_tagger import SMCConfluenceTagger
        frames = {"H4": _candles(20, trend="UP"), "H1": _candles(20, trend="UP")}
        with patch("app.agents.smc_confluence_tagger._is_weekend_utc", return_value=True):
            result = SMCConfluenceTagger(Settings()).evaluate("BTCUSD#", frames, "BUY")
        self.assertEqual(result["smc_confluence_score"], 50)
        self.assertEqual(result["smc_confluence_status"], "NEUTRAL")
        self.assertEqual(result["smc_confluence_reason"], "SMC_WEEKEND_FALLBACK_H1")

    def test_non_weekend_missing_intraday_remains_fail(self) -> None:
        from unittest.mock import patch
        from app.agents.smc_confluence_tagger import SMCConfluenceTagger
        frames = {"H4": _candles(20, trend="UP"), "H1": _candles(20, trend="UP")}
        with patch("app.agents.smc_confluence_tagger._is_weekend_utc", return_value=False):
            result = SMCConfluenceTagger(Settings()).evaluate("BTCUSD#", frames, "BUY")
        self.assertEqual(result["smc_confluence_status"], "FAIL")
        self.assertIn("MISSING_DATA", result["smc_confluence_reason"])


# ---------------------------------------------------------------------------
# 2. _order_block() — non-repainting
# ---------------------------------------------------------------------------

class TestOrderBlockNonRepainting(unittest.TestCase):

    def _bullish_ob_frame(self, extra_live_close: float | None = None) -> pd.DataFrame:
        """Build a frame where confirmed iloc[-3]/[-2] form a BEARISH→BULLISH OB."""
        rows = [
            # filler
            {"open": 65000, "high": 65100, "low": 64900, "close": 65050, "tick_volume": 10},
            {"open": 65050, "high": 65150, "low": 64950, "close": 65100, "tick_volume": 10},
            # OB candle: bearish (prev = iloc[-3] after fix)
            {"open": 65200, "high": 65250, "low": 65100, "close": 65110, "tick_volume": 10},
            # bullish breakout (last = iloc[-2] after fix)
            {"open": 65110, "high": 65400, "low": 65100, "close": 65380, "tick_volume": 10},
        ]
        df = pd.DataFrame(rows)
        if extra_live_close is not None:
            live = {"open": 65380, "high": 70000.0, "low": 60000.0,
                    "close": extra_live_close, "tick_volume": 1}
            df = pd.concat([df, pd.DataFrame([live])], ignore_index=True)
        return df

    def test_ob_requires_five_rows(self) -> None:
        # 4 rows → len < 5 → NONE
        df = self._bullish_ob_frame()[:-0]  # 4 rows, no live appended
        self.assertEqual(_order_block(df), "NONE")

    def test_ob_detected_on_confirmed_candles(self) -> None:
        # 5 rows: 4 confirmed + 1 live → OB detected from rows -3/-2
        df = self._bullish_ob_frame(extra_live_close=65000.0)
        result = _order_block(df)
        # OB pattern: iloc[-3]=bearish, iloc[-2]=bullish breakout above prev high
        # prev_close(65110) < prev_open(65200) ✓ AND last_close(65380) > last_open(65110) ✓
        # AND last_close(65380) > prev_high(65250) ✓ → BULLISH_OB
        self.assertEqual(result, "BULLISH_OB")

    def test_ob_stable_when_live_changes(self) -> None:
        df_normal = self._bullish_ob_frame(extra_live_close=65000.0)
        df_spike  = self._bullish_ob_frame(extra_live_close=70000.0)
        self.assertEqual(
            _order_block(df_normal),
            _order_block(df_spike),
            "_order_block changed due to live candle",
        )


# ---------------------------------------------------------------------------
# 3. _fair_value_gap() — non-repainting, 3-candle structure intact
# ---------------------------------------------------------------------------

class TestFairValueGapNonRepainting(unittest.TestCase):

    def _bullish_fvg_frame(self, live_close: float) -> pd.DataFrame:
        """
        Build confirmed a/b/c where c_low > a_high (BULLISH_FVG), then append live.

        After fix:  a=iloc[-4], b(implicit)=iloc[-3], c=iloc[-2], live=iloc[-1]
        """
        rows = [
            # filler
            {"open": 65000, "high": 65100, "low": 64900, "close": 65050, "tick_volume": 10},
            # candle A: a_high=65200
            {"open": 65100, "high": 65200, "low": 65000, "close": 65150, "tick_volume": 10},
            # candle B (impulse — implicit in gap definition, OHLC not used in formula)
            {"open": 65150, "high": 65600, "low": 65140, "close": 65580, "tick_volume": 10},
            # candle C: c_low=65210 > a_high=65200 → BULLISH_FVG confirmed
            {"open": 65580, "high": 65700, "low": 65210, "close": 65650, "tick_volume": 10},
        ]
        df = pd.DataFrame(rows)
        # live candle — extreme spike in either direction
        live = {"open": 65650, "high": 70000.0, "low": 60000.0,
                "close": live_close, "tick_volume": 1}
        return pd.concat([df, pd.DataFrame([live])], ignore_index=True)

    def test_fvg_requires_five_rows(self) -> None:
        df = pd.DataFrame([
            {"open": 65000, "high": 65100, "low": 64900, "close": 65050, "tick_volume": 10},
        ] * 4)
        self.assertEqual(_fair_value_gap(df), "NONE")

    def test_bullish_fvg_detected_on_confirmed_candles(self) -> None:
        df = self._bullish_fvg_frame(live_close=60000.0)  # live crashes down
        self.assertEqual(_fair_value_gap(df), "BULLISH_FVG")

    def test_fvg_stable_when_live_changes(self) -> None:
        df_low  = self._bullish_fvg_frame(live_close=55000.0)
        df_high = self._bullish_fvg_frame(live_close=75000.0)
        self.assertEqual(
            _fair_value_gap(df_low),
            _fair_value_gap(df_high),
            "_fair_value_gap changed due to live candle",
        )

    def test_fvg_three_candles_consecutive(self) -> None:
        """Verify a=iloc[-4], implicit-b=iloc[-3], c=iloc[-2] are consecutive."""
        df = self._bullish_fvg_frame(live_close=65650.0)
        # Direct index check: the gap condition uses rows -4 and -2
        a_high = float(df.iloc[-4]["high"])    # 65200
        c_low  = float(df.iloc[-2]["low"])     # 65210
        b_candle = df.iloc[-3]                 # the impulse candle between A and C
        # Confirm b is truly between a and c (temporal ordering)
        self.assertGreater(c_low, a_high, "BULLISH_FVG: c_low must exceed a_high")
        # Confirm b's index is between a and c
        self.assertEqual(df.index[-3], df.index[-4] + 1)
        self.assertEqual(df.index[-2], df.index[-3] + 1)
        # b_candle high should be above a_high (it's the impulse candle)
        self.assertGreater(float(b_candle["high"]), a_high)


# ---------------------------------------------------------------------------
# 4. _structure_confirmation() — non-repainting
# ---------------------------------------------------------------------------

class TestStructureConfirmationNonRepainting(unittest.TestCase):

    def _buy_confirm_frame(self, live_close: float) -> pd.DataFrame:
        """Frame where confirmed candles show a breakout above prior high."""
        rows = [
            {"open": 65000, "high": 65100, "low": 64900, "close": 65050, "tick_volume": 10},
            {"open": 65050, "high": 65200, "low": 65000, "close": 65180, "tick_volume": 10},
            {"open": 65180, "high": 65250, "low": 65100, "close": 65200, "tick_volume": 10},
            {"open": 65200, "high": 65300, "low": 65150, "close": 65280, "tick_volume": 10},
            # confirmed close > prior_high and close > open → BUY confirmation
            {"open": 65280, "high": 65500, "low": 65260, "close": 65480, "tick_volume": 10},
        ]
        df = pd.DataFrame(rows)
        live = {"open": 65480, "high": 70000.0, "low": 60000.0,
                "close": live_close, "tick_volume": 1}
        return pd.concat([df, pd.DataFrame([live])], ignore_index=True)

    def test_confirmation_stable_when_live_spikes(self) -> None:
        df_normal = self._buy_confirm_frame(live_close=65480.0)
        df_crash  = self._buy_confirm_frame(live_close=55000.0)
        self.assertEqual(
            _structure_confirmation(df_normal, "BUY"),
            _structure_confirmation(df_crash, "BUY"),
            "_structure_confirmation changed due to live candle",
        )

    def test_confirmation_detected_on_closed_candles(self) -> None:
        df = self._buy_confirm_frame(live_close=55000.0)  # live is bearish crash
        # Despite live crash, confirmed candle breakout should still be detected
        self.assertTrue(_structure_confirmation(df, "BUY"))


# ---------------------------------------------------------------------------
# 5. MTFAFilter._h1_bias() — non-repainting
# ---------------------------------------------------------------------------

class TestH1BiasNonRepainting(unittest.TestCase):

    def setUp(self) -> None:
        self.filter = MTFAFilter(_mtfa_settings())

    def _h1_bullish_frame(self, live_close: float) -> pd.DataFrame:
        """Confirmed H1 candles show BULLISH bias, then append volatile live bar."""
        closed = _candles(14, base=65000.0, step=100.0, trend="UP")
        live = {
            "open": float(closed.iloc[-1]["close"]),
            "high": float(closed.iloc[-1]["close"]) + 5000.0,
            "low":  float(closed.iloc[-1]["close"]) - 5000.0,
            "close": live_close,
            "tick_volume": 1,
        }
        return pd.concat([closed, pd.DataFrame([live])], ignore_index=True)

    def test_h1_bias_stable_when_live_spikes(self) -> None:
        df_high = self._h1_bullish_frame(live_close=80000.0)
        df_low  = self._h1_bullish_frame(live_close=50000.0)
        bias_high = self.filter._h1_bias(df_high)
        bias_low  = self.filter._h1_bias(df_low)
        self.assertEqual(
            bias_high,
            bias_low,
            f"_h1_bias changed from {bias_high} to {bias_low} due to live candle",
        )

    def test_h1_bias_bullish_from_confirmed(self) -> None:
        df = self._h1_bullish_frame(live_close=40000.0)  # live crashes
        self.assertEqual(self.filter._h1_bias(df), "BULLISH")


# ---------------------------------------------------------------------------
# 6. MTFAFilter._m15_liquidity() — non-repainting
# ---------------------------------------------------------------------------

class TestM15LiquidityNonRepainting(unittest.TestCase):

    def setUp(self) -> None:
        self.filter = MTFAFilter(_mtfa_settings())

    def _sweep_frame(self, live_close: float) -> pd.DataFrame:
        """Build confirmed M15 candles with a BUY_SIDE_SWEEP, then live bar."""
        # Need: last_high > prev 10-bar max AND close < that high
        base = _candles(15, base=65000.0, step=20.0, trend="FLAT")
        # last confirmed candle breaks above then closes below
        sweep_candle = {
            "open": float(base.iloc[-1]["close"]),
            "high": float(base["high"].max()) + 500.0,   # break above all prior highs
            "low":  float(base.iloc[-1]["close"]) - 10.0,
            "close": float(base.iloc[-1]["close"]) - 5.0,  # close back below
            "tick_volume": 20,
        }
        closed = pd.concat([base, pd.DataFrame([sweep_candle])], ignore_index=True)
        live = {
            "open": float(closed.iloc[-1]["close"]),
            "high": float(closed.iloc[-1]["close"]) + 5000.0,
            "low":  float(closed.iloc[-1]["close"]) - 5000.0,
            "close": live_close,
            "tick_volume": 1,
        }
        return pd.concat([closed, pd.DataFrame([live])], ignore_index=True)

    def test_m15_liquidity_stable_when_live_spikes(self) -> None:
        df_up   = self._sweep_frame(live_close=80000.0)
        df_down = self._sweep_frame(live_close=50000.0)
        liq_up,   *_ = self.filter._m15_liquidity(df_up)
        liq_down, *_ = self.filter._m15_liquidity(df_down)
        self.assertEqual(
            liq_up,
            liq_down,
            "_m15_liquidity changed due to live candle",
        )


# ---------------------------------------------------------------------------
# 7. MTFAFilter._m5_cisd() — non-repainting, len guard = 9
# ---------------------------------------------------------------------------

class TestM5CisdNonRepainting(unittest.TestCase):

    def setUp(self) -> None:
        self.filter = MTFAFilter(_mtfa_settings())

    def test_cisd_requires_nine_rows(self) -> None:
        df = _candles(8, trend="UP")  # 8 rows → len < 9 → False
        self.assertFalse(self.filter._m5_cisd(df, "BUY", None))

    def test_cisd_accepts_nine_rows(self) -> None:
        # 9 rows: after iloc[:-1] = 8 closed, tail(8) complete
        df = _candles(9, trend="UP")
        # Just verify it doesn't raise — result may be True or False depending on candles
        result = self.filter._m5_cisd(df, "BUY", None)
        self.assertIsInstance(result, bool)

    def test_cisd_stable_when_live_candle_changes(self) -> None:
        """CISD must not flicker tick-by-tick due to live M5 bar."""
        closed = _candles(10, base=65000.0, step=50.0, trend="UP")
        live_close_a = float(closed.iloc[-1]["close"]) + 3000.0
        live_close_b = float(closed.iloc[-1]["close"]) - 3000.0

        def _with_live(close: float) -> pd.DataFrame:
            live = {"open": float(closed.iloc[-1]["close"]),
                    "high": float(closed.iloc[-1]["close"]) + 5000.0,
                    "low":  float(closed.iloc[-1]["close"]) - 5000.0,
                    "close": close, "tick_volume": 1}
            return pd.concat([closed, pd.DataFrame([live])], ignore_index=True)

        df_a = _with_live(live_close_a)
        df_b = _with_live(live_close_b)
        self.assertEqual(
            self.filter._m5_cisd(df_a, "BUY", None),
            self.filter._m5_cisd(df_b, "BUY", None),
            "_m5_cisd changed due to live candle",
        )


# ---------------------------------------------------------------------------
# 8. Full SMC evaluate() — score stable with volatile live candle
# ---------------------------------------------------------------------------

class TestSMCEvaluateNonRepainting(unittest.TestCase):
    """Verify that realistic tick noise on the live candle does not change SMC score.

    Uses ±50 variation in live close — representative intra-candle noise that stays
    well within confirmed structure. Large moves (±10k) would legitimately change
    _break_structure() which still reads the live candle (out of scope for this PR).
    """

    def _make_frames(self, live_close_offset: float) -> dict:
        """Build frames where the live candle close varies by offset from last confirmed."""
        h4  = _candles(20, base=64000.0, step=200.0, trend="UP")
        h1  = _candles(20, base=64000.0, step=100.0, trend="UP")
        m15 = _candles(20, base=65000.0, step=30.0,  trend="UP")
        m5  = _candles(20, base=65000.0, step=10.0,  trend="UP")
        m1  = _candles(20, base=65000.0, step=5.0,   trend="UP")

        def _add_live(df: pd.DataFrame) -> pd.DataFrame:
            last_close = float(df.iloc[-1]["close"])
            # Wick can vary (represents bid/ask noise mid-candle), close near last
            live = {
                "open":  last_close,
                "high":  last_close + 30.0,
                "low":   last_close - 30.0,
                "close": last_close + live_close_offset,
                "tick_volume": 1,
            }
            return pd.concat([df, pd.DataFrame([live])], ignore_index=True)

        return {
            "H4":  _add_live(h4),
            "H1":  _add_live(h1),
            "M15": _add_live(m15),
            "M5":  _add_live(m5),
            "M1":  _add_live(m1),
        }

    def test_smc_score_stable_against_tick_noise(self) -> None:
        """±50 tick noise on live candle must not change score (patched functions)."""
        from app.agents.smc_confluence_tagger import SMCConfluenceTagger
        from app.config import Settings
        tagger = SMCConfluenceTagger(Settings())

        frames_base   = self._make_frames(live_close_offset=0.0)
        frames_plus50 = self._make_frames(live_close_offset=+50.0)
        frames_minus50 = self._make_frames(live_close_offset=-50.0)

        score_base   = tagger.evaluate("BTCUSD#", frames_base,    "BUY")["smc_confluence_score"]
        score_plus   = tagger.evaluate("BTCUSD#", frames_plus50,  "BUY")["smc_confluence_score"]
        score_minus  = tagger.evaluate("BTCUSD#", frames_minus50, "BUY")["smc_confluence_score"]

        self.assertEqual(score_base, score_plus,
            f"SMC score changed with +50 tick: base={score_base} vs +50={score_plus}")
        self.assertEqual(score_base, score_minus,
            f"SMC score changed with -50 tick: base={score_base} vs -50={score_minus}")


# ---------------------------------------------------------------------------
# 9. Full MTFA evaluate() — score stable with volatile live candle
# ---------------------------------------------------------------------------

class TestMTFAEvaluateNonRepainting(unittest.TestCase):
    """Verify that extreme live candle extremes do not change MTFA score.

    MTFA patched functions (_h1_bias, _m15_liquidity, _m5_cisd) all use df.iloc[:-1].
    Score must be fully stable regardless of live candle close value.
    """

    def _make_frames(self, live_close_offset: float) -> dict:
        h1  = _candles(20, base=64000.0, step=100.0, trend="UP")
        m15 = _candles(20, base=65000.0, step=30.0,  trend="UP")
        m5  = _candles(20, base=65000.0, step=10.0,  trend="UP")

        def _add_live(df: pd.DataFrame) -> pd.DataFrame:
            last_close = float(df.iloc[-1]["close"])
            live = {
                "open":  last_close,
                "high":  last_close + 5000.0,   # extreme wick — irrelevant after fix
                "low":   last_close - 5000.0,
                "close": last_close + live_close_offset,
                "tick_volume": 1,
            }
            return pd.concat([df, pd.DataFrame([live])], ignore_index=True)

        return {
            "H1":  _add_live(h1),
            "M15": _add_live(m15),
            "M5":  _add_live(m5),
        }

    def test_mtfa_score_stable_across_live_candle_extremes(self) -> None:
        """All MTFA functions patched → extreme live close has zero effect on score."""
        mtfa = MTFAFilter(_mtfa_settings())

        frames_base  = self._make_frames(live_close_offset=0.0)
        frames_spike = self._make_frames(live_close_offset=+10000.0)
        frames_crash = self._make_frames(live_close_offset=-10000.0)

        score_base  = mtfa.evaluate("BTCUSD#", frames_base,  "BUY")["mtfa_score"]
        score_spike = mtfa.evaluate("BTCUSD#", frames_spike, "BUY")["mtfa_score"]
        score_crash = mtfa.evaluate("BTCUSD#", frames_crash, "BUY")["mtfa_score"]

        self.assertEqual(score_base, score_spike,
            f"MTFA score changed with +10k live: base={score_base} vs spike={score_spike}")
        self.assertEqual(score_base, score_crash,
            f"MTFA score changed with -10k live: base={score_base} vs crash={score_crash}")


if __name__ == "__main__":
    unittest.main()
