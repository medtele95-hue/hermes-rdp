from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from app.config import Settings
from app.services import eur_ema_rsi_atr_strategy as eur


BUY_CLOSES = [
    1.09992363, 1.09997234, 1.09989374, 1.09983020, 1.09978964, 1.09975346, 1.09976472, 1.09976362,
    1.09973375, 1.09966641, 1.09961296, 1.09966448, 1.09959963, 1.09962336, 1.09956175, 1.09962530,
    1.09959680, 1.09962619, 1.09954187, 1.09952392, 1.09949321, 1.09954202, 1.09957433, 1.09952875,
    1.09950514, 1.09947808, 1.09949359, 1.09950057, 1.09947613, 1.09947479, 1.09942765, 1.09944541,
    1.09936876, 1.09928598, 1.09932670, 1.09925758, 1.09918648, 1.09923764, 1.09926937, 1.09925650,
    1.09932200, 1.09936821, 1.09940669, 1.09934880, 1.09938914, 1.09935471, 1.09932802, 1.09931581,
    1.09930012, 1.09928574, 1.09922679, 1.09922546, 1.09921894, 1.09920284, 1.09925690, 1.09922846,
    1.09920994, 1.09918930, 1.09920466, 1.09924857, 1.09928847, 1.09920800, 1.09923436, 1.09922959,
    1.09915057, 1.09911250, 1.09913497, 1.09919521, 1.09917018, 1.09914780, 1.09913606, 1.09919312,
    1.09925172, 1.09930434, 1.09923945, 1.09925480, 1.09921609, 1.09920984, 1.09924036, 1.09926755,
    1.09933581, 1.09926035, 1.09928206, 1.09921956, 1.09919613, 1.09912016, 1.09912476, 1.09909632,
    1.09913705, 1.09905526, 1.09898334, 1.09901296, 1.09898543, 1.09902555, 1.09901441, 1.09907988,
    1.09900943, 1.09906907, 1.09904026, 1.09902390, 1.09902648, 1.09908059, 1.09909373, 1.09915858,
    1.09915796, 1.09919988, 1.09925424, 1.09929042, 1.09920095, 1.09924819, 1.09917754, 1.09918524,
    1.09924521, 1.09915635, 1.09912815, 1.09907232, 1.09902612, 1.09902198, 1.09907091, 1.09904344,
    1.09913966, 1.09930434, 1.09942071, 1.09949014,
]

SELL_CLOSES = [
    1.09995944, 1.09996529, 1.10005359, 1.10003116, 1.10003787, 1.09999030, 1.09996736, 1.10003598,
    1.10001644, 1.09997632, 1.10006483, 1.10009250, 1.10003537, 1.10007720, 1.10010866, 1.10012359,
    1.10009121, 1.10012015, 1.10018878, 1.10025866, 1.10031794, 1.10032282, 1.10029861, 1.10024614,
    1.10028707, 1.10030583, 1.10039411, 1.10038895, 1.10034622, 1.10030889, 1.10030928, 1.10029801,
    1.10023638, 1.10025876, 1.10026205, 1.10020063, 1.10027377, 1.10027961, 1.10034957, 1.10040367,
    1.10048521, 1.10043165, 1.10039892, 1.10034474, 1.10032733, 1.10025815, 1.10020846, 1.10014331,
    1.10022314, 1.10030757, 1.10031312, 1.10038061, 1.10037775, 1.10043116, 1.10044465, 1.10040700,
    1.10040709, 1.10033865, 1.10027127, 1.10032383, 1.10028619, 1.10027410, 1.10027066, 1.10024345,
    1.10025768, 1.10031491, 1.10028093, 1.10022592, 1.10019229, 1.10024966, 1.10030552, 1.10025877,
    1.10025515, 1.10031856, 1.10030136, 1.10037672, 1.10032711, 1.10028157, 1.10021161, 1.10020998,
    1.10017510, 1.10013017, 1.10007608, 1.10009225, 1.10016646, 1.10012353, 1.10005732, 1.10003164,
    1.10009647, 1.10007623, 1.10010426, 1.10012086, 1.10015117, 1.10018191, 1.10014124, 1.10011818,
    1.10019225, 1.10024648, 1.10021155, 1.10014618, 1.10020260, 1.10013390, 1.10015587, 1.10017968,
    1.10019981, 1.10016513, 1.10010224, 1.10013747, 1.10011203, 1.10009395, 1.10014824, 1.10019572,
    1.10022063, 1.10015885, 1.10021110, 1.10017943, 1.10025457, 1.10026757, 1.10022409, 1.10026740,
    1.10018506, 1.10016177, 1.09990113,
]


def settings(**overrides) -> Settings:
    data = {
        "eur_ema_rsi_atr_enabled": True,
        "demo_ignore_all_time_blocks": False,
        "demo_ignore_session_blocks": False,
        "demo_ignore_bad_hour_blocks": False,
        "demo_ignore_duration_blocks": False,
        "demo_ignore_setup_wait_hours": False,
    }
    data.update(overrides)
    return Settings(**data)


def frame(closes: list[float], forming: float | None = None) -> pd.DataFrame:
    values = list(closes)
    if forming is not None:
        values.append(forming)
    rows = []
    for close in values:
        rows.append(
            {
                "open": close,
                "high": close + 0.0002,
                "low": close - 0.0002,
                "close": close,
                "tick_volume": 100,
            }
        )
    return pd.DataFrame(rows)


class EurEmaRsiAtrStrategyTests(unittest.TestCase):
    def test_buy_when_ema20_crosses_above_ema50_and_rsi_below_70(self) -> None:
        result = eur.evaluate("EURUSD", {"M5": frame(BUY_CLOSES, forming=BUY_CLOSES[-1] + 0.001)}, {"ask": 1.2}, settings())
        payload = result["eur_ema_rsi_atr"]
        self.assertEqual(result["signal"], "BUY")
        self.assertTrue(payload["cross_up"])
        self.assertLess(payload["rsi_1"], 70)

    def test_sell_when_ema20_crosses_below_ema50_and_rsi_above_30(self) -> None:
        result = eur.evaluate("EURUSD", {"M5": frame(SELL_CLOSES, forming=SELL_CLOSES[-1] - 0.001)}, {"bid": 1.1}, settings())
        payload = result["eur_ema_rsi_atr"]
        self.assertEqual(result["signal"], "SELL")
        self.assertTrue(payload["cross_down"])
        self.assertGreater(payload["rsi_1"], 30)

    def test_no_lookahead_uses_closed_candles_only(self) -> None:
        flat = [1.1] * 80
        result = eur.evaluate("EURUSD", {"M5": frame(flat, forming=1.2)}, {"ask": 1.2}, settings())
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["eur_ema_rsi_atr"]["block_reason"], "NO_EMA_CROSS")

    def test_near_cross_only_works_in_relaxed_demo_mode(self) -> None:
        data = frame([1.1002] * 80, forming=1.1002)
        fast = pd.Series([1.0999] * 78 + [1.0999, 1.1000])
        slow = pd.Series([1.1001] * 80)
        rsi_values = pd.Series([60.0] * 80)
        atr_values = pd.Series([0.001] * 80)
        with (
            patch("app.services.eur_ema_rsi_atr_strategy.ema", side_effect=[fast, slow]),
            patch("app.services.eur_ema_rsi_atr_strategy.rsi", return_value=rsi_values),
            patch("app.services.eur_ema_rsi_atr_strategy.atr", return_value=atr_values),
        ):
            strict = eur.evaluate("EURUSD", {"M5": data}, {"ask": 1.1002}, settings(eur_rr=1.0))
        with (
            patch("app.services.eur_ema_rsi_atr_strategy.ema", side_effect=[fast, slow]),
            patch("app.services.eur_ema_rsi_atr_strategy.rsi", return_value=rsi_values),
            patch("app.services.eur_ema_rsi_atr_strategy.atr", return_value=atr_values),
        ):
            relaxed = eur.evaluate("EURUSD", {"M5": data}, {"ask": 1.1002, "hours_without_setup": 24}, settings(eur_rr=1.0))
        self.assertEqual(strict["signal"], "WAIT")
        self.assertEqual(relaxed["signal"], "BUY")
        self.assertEqual(relaxed["reason"], "EUR_NEAR_CROSS_RELAXED_DEMO")
        self.assertTrue(relaxed["eur_ema_rsi_atr"]["near_cross"])
        self.assertTrue(relaxed["eur_ema_rsi_atr"]["relaxed_mode_active"])

    def test_sl_tp_formula_matches_atr_mult_and_rr(self) -> None:
        entry = 1.2
        result = eur.evaluate("EURUSD", {"M5": frame(BUY_CLOSES, forming=BUY_CLOSES[-1])}, {"ask": entry}, settings())
        payload = result["eur_ema_rsi_atr"]
        sl_distance = payload["atr_1"] * 1.5
        self.assertAlmostEqual(payload["sl"], entry - sl_distance, places=8)
        self.assertAlmostEqual(payload["tp"], entry + sl_distance * 2.0, places=8)
        self.assertAlmostEqual(payload["rr"], 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
