from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import pandas as pd

from app.agents.strategies import quant_pro_regime_switching as quant_pro
from app.config import Settings
from app.services.quant_pro_regime_switching import QuantProRegimeSwitching, ewma_volatility, reward_risk


def settings(**overrides) -> Settings:
    defaults = {
        "hermes_quant_pro_enabled": True,
        "hermes_quant_pro_role": "ENTRY_STRATEGY",
        "hermes_quant_pro_reg_period": 50,
        "hermes_quant_pro_tcrit": 2.0,
        "hermes_quant_pro_kalman_period": 100,
        "hermes_quant_pro_ou_period": 100,
        "hermes_quant_pro_hurst_period": 128,
        "hermes_quant_pro_ewma_vol_period": 50,
        "hermes_quant_pro_min_score": 75,
        "hermes_quant_pro_min_rr": 2.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 0.01 for value in closes],
            "low": [value - 0.01 for value in closes],
            "close": closes,
            "spread": [1] * len(closes),
        }
    )


def trend_buy_closes() -> list[float]:
    return [100 + 0.05 * i + 0.02 * math.sin(i) for i in range(140)] + [90.0]


def trend_sell_closes() -> list[float]:
    return [110 - 0.05 * i + 0.02 * math.sin(i) for i in range(140)] + [120.0]


def mean_reverting_closes(shock: float) -> list[float]:
    values = []
    price = 100.0
    for i in range(140):
        price = 100 + 0.95 * (price - 100) + 0.1 * math.sin(i * 1.7)
        values.append(price)
    return values[:-1] + [100 + shock] + [999.0]


class QuantProRegimeSwitchingTests(unittest.TestCase):
    def evaluate(self, closes: list[float], **overrides) -> dict:
        return QuantProRegimeSwitching(settings(**overrides)).evaluate("EURUSD", frame(closes))

    def test_trend_buy_creates_buy_candidate(self) -> None:
        result = self.evaluate(trend_buy_closes())
        self.assertEqual(result["regime"], "TREND")
        self.assertEqual(result["direction"], "BUY")
        self.assertGreater(result["ols_tstat"], 2.0)
        self.assertGreater(result["kalman_velocity"], 0)
        self.assertGreaterEqual(result["hurst"], 0.5)
        self.assertGreaterEqual(result["score"], 75)

    def test_trend_sell_creates_sell_candidate(self) -> None:
        result = self.evaluate(trend_sell_closes())
        self.assertEqual(result["regime"], "TREND")
        self.assertEqual(result["direction"], "SELL")
        self.assertLess(result["ols_tstat"], -2.0)
        self.assertLess(result["kalman_velocity"], 0)
        self.assertGreaterEqual(result["hurst"], 0.5)

    def test_hurst_0_90_passes_trend_filter(self) -> None:
        with patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=0.90):
            result = self.evaluate(trend_buy_closes())
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["quant_pro_hurst_filter"]["trend_strength"], "STRONG_TREND")
        self.assertTrue(result["quant_pro_hurst_filter"]["passed"])

    def test_hurst_0_95_passes_trend_filter(self) -> None:
        with patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=0.95):
            result = self.evaluate(trend_buy_closes())
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["quant_pro_hurst_filter"]["hurst"], 0.95)
        self.assertTrue(result["quant_pro_hurst_filter"]["passed"])

    def test_hurst_0_89_blocks_trend_filter(self) -> None:
        with patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=0.89):
            result = self.evaluate(trend_buy_closes())
        self.assertEqual(result["direction"], "WAIT")
        self.assertEqual(result["reason"], "QUANT_PRO_HURST_TREND_TOO_WEAK")
        self.assertEqual(result["quant_pro_hurst_filter"]["trend_strength"], "WEAK_TREND")
        self.assertFalse(result["quant_pro_hurst_filter"]["passed"])

    def test_missing_or_nan_hurst_blocks_trend_filter(self) -> None:
        for value in (None, math.nan):
            with self.subTest(value=value), patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=value):
                result = self.evaluate(trend_buy_closes())
            self.assertEqual(result["direction"], "WAIT")
            self.assertEqual(result["reason"], "QUANT_PRO_HURST_MISSING")
            self.assertEqual(result["quant_pro_hurst_filter"]["trend_strength"], "UNKNOWN")

    def test_hurst_does_not_choose_direction_by_itself(self) -> None:
        with patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=0.95):
            result = self.evaluate(trend_sell_closes())
        self.assertEqual(result["direction"], "SELL")
        self.assertLess(result["ols_slope"], 0)
        self.assertLess(result["kalman_velocity"], 0)

    def test_hurst_filter_applies_only_to_trend_quant_pro(self) -> None:
        with patch("app.services.quant_pro_regime_switching.hurst_rs", return_value=0.10):
            result = self.evaluate(mean_reverting_closes(-3.0), hermes_quant_pro_tcrit=0.2, hermes_quant_pro_hl_min=0.1)
        self.assertEqual(result["regime"], "MEAN_REVERSION")
        self.assertEqual(result["direction"], "BUY")
        self.assertTrue(result["quant_pro_hurst_filter"]["passed"])

    def test_mean_reversion_buy_creates_buy_candidate(self) -> None:
        result = self.evaluate(mean_reverting_closes(-3.0), hermes_quant_pro_tcrit=0.2, hermes_quant_pro_hl_min=0.1)
        self.assertEqual(result["regime"], "MEAN_REVERSION")
        self.assertEqual(result["direction"], "BUY")
        self.assertLess(result["kalman_z"], -1.0)
        self.assertLess(result["ou_tstat"], -0.2)

    def test_mean_reversion_sell_creates_sell_candidate(self) -> None:
        result = self.evaluate(mean_reverting_closes(3.0), hermes_quant_pro_tcrit=0.2, hermes_quant_pro_hl_min=0.1)
        self.assertEqual(result["regime"], "MEAN_REVERSION")
        self.assertEqual(result["direction"], "SELL")
        self.assertGreater(result["kalman_z"], 1.0)

    def test_flat_regime_creates_no_candidate(self) -> None:
        values = [100 + 0.01 * math.sin(i) for i in range(140)] + [100.0]
        result = self.evaluate(values)
        self.assertEqual(result["direction"], "WAIT")
        self.assertIn(result["regime"], {"FLAT", "MEAN_REVERSION"})

    def test_invalid_ewma_vol_creates_no_candidate(self) -> None:
        result = self.evaluate([100.0] * 140)
        self.assertEqual(result["direction"], "WAIT")
        self.assertEqual(result["reason"], "QUANT_PRO_INVALID_EWMA_VOL")

    def test_buy_sl_tp_rr_valid(self) -> None:
        result = self.evaluate(trend_buy_closes())
        self.assertLess(result["sl"], result["entry"])
        self.assertGreater(result["tp"], result["entry"])
        self.assertGreaterEqual(result["rr"], 2.0)

    def test_sell_sl_tp_rr_valid(self) -> None:
        result = self.evaluate(trend_sell_closes())
        self.assertGreater(result["sl"], result["entry"])
        self.assertLess(result["tp"], result["entry"])
        self.assertGreaterEqual(result["rr"], 2.0)

    def test_no_lookahead_ignores_forming_candle(self) -> None:
        closed = trend_buy_closes()[:-1]
        baseline = self.evaluate(closed + [closed[-1]])
        with_forming_spike = self.evaluate(closed + [50.0])
        self.assertEqual(baseline["direction"], with_forming_spike["direction"])
        self.assertEqual(baseline["entry"], with_forming_spike["entry"])
        self.assertTrue(with_forming_spike["no_lookahead"])

    def test_strategy_wrapper_maps_candidate_fields(self) -> None:
        result = quant_pro.evaluate("EURUSD", {"M5": frame(trend_buy_closes())}, {}, settings())
        self.assertEqual(result["strategy"], "QUANT_PRO_REGIME_SWITCHING")
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["quant_pro_signal"], "BUY")
        self.assertEqual(result["m15_confirmation_status"], "PASS")
        self.assertGreaterEqual(result["quant_pro_score"], 75)
        self.assertEqual(result["quant_pro_hurst_filter_status"], "PASS")
        self.assertEqual(result["quant_pro_trend_strength"], "STRONG_TREND")

    def test_reward_risk_helpers_validate_buy_and_sell(self) -> None:
        self.assertEqual(reward_risk("BUY", 100, 99, 102), 2.0)
        self.assertEqual(reward_risk("SELL", 100, 101, 98), 2.0)
        self.assertIsNone(reward_risk("BUY", 100, 101, 102))
        self.assertGreater(ewma_volatility([100, 101, 100.5], 0.94), 0)


if __name__ == "__main__":
    unittest.main()
