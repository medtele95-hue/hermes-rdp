# -*- coding: utf-8 -*-
"""GRAND_PLAN_2 mission3 (2026-07-08, SIMO validé GO) — Exit V2 BTC
thresholds switch from flat $ to a percentage of entry price, calibrated
to give BTC the SAME RELATIVE protection as GOLD's current $2/$4000=0.05%
baseline. GOLD stays on its flat $ thresholds, completely unchanged.

Discovery this mission fixed (from the prior VOLET2 audit, mission
FIX_KILLSWITCH_PNL.md): a flat $2 BE-arm threshold demanded 0.32% of price
movement on BTC (~$63000) vs only 0.05% on GOLD (~$4000) — 6.5x harder to
arm on BTC, verified against real MT5 contract specs (tick_value/tick_size).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.exit_v2 import ExitV2Config, evaluate_exit_v2, is_btc_symbol, is_gold_symbol


def _pos(ticket=1, symbol="GOLD#", type_=0, profit=0.0, volume=0.01, price_open=4000.0):
    return SimpleNamespace(ticket=ticket, symbol=symbol, type=type_, profit=profit, volume=volume, price_open=price_open)


def _gold_symbol_info():
    # GOLD: 1 price unit ($1) move = $1 profit at 0.01 lot (tick_value=1.0,
    # tick_size=0.01 -> ratio 100 * 0.01 lot = 1.0 $/unit). Matches this
    # mission's live-verified specs.
    return SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01)


def _btc_symbol_info():
    # BTC: tick_value=0.01, tick_size=0.01 -> ratio 1.0 * 0.01 lot = 0.01
    # $/unit. Matches this mission's live-verified specs exactly.
    return SimpleNamespace(trade_tick_value=0.01, trade_tick_size=0.01)


class TestSymbolClassifiers(unittest.TestCase):
    def test_is_btc_symbol(self) -> None:
        self.assertTrue(is_btc_symbol("BTCUSD#"))
        self.assertFalse(is_btc_symbol("GOLD#"))

    def test_is_gold_symbol_unaffected(self) -> None:
        self.assertTrue(is_gold_symbol("GOLD#"))
        self.assertFalse(is_gold_symbol("BTCUSD#"))


class TestGoldUnchanged(unittest.TestCase):
    """GOLD must behave EXACTLY as before this mission — flat $ thresholds,
    no percentage logic ever applied."""

    def test_gold_be_arms_at_flat_2_dollars_not_percentage(self) -> None:
        cfg = ExitV2Config()
        pos = _pos(symbol="GOLD#", profit=1.99, price_open=4000.0)
        state = {}
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, _gold_symbol_info(), cfg, state)
        self.assertFalse(state[1]["be_armed"])
        self.assertEqual(action["threshold_scale"], "USD")
        self.assertAlmostEqual(action["be_arm_usd_effective"], 2.00, places=2)

        pos2 = _pos(symbol="GOLD#", profit=2.00, price_open=4000.0)
        with patch("app.services.exit_v2.log"):
            evaluate_exit_v2(pos2, None, _gold_symbol_info(), cfg, state)
        self.assertTrue(state[1]["be_armed"])

    def test_gold_ignores_btc_pct_config_entirely(self) -> None:
        """Even with an absurd BTC pct config, GOLD's own $2 arm is untouched."""
        cfg = ExitV2Config(btc_be_arm_pct=99.0)
        pos = _pos(symbol="GOLD#", profit=2.00, price_open=4000.0)
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, _gold_symbol_info(), cfg, {})
        self.assertEqual(action["threshold_scale"], "USD")
        self.assertAlmostEqual(action["be_arm_usd_effective"], 2.00, places=2)


class TestBtcPercentageThresholds(unittest.TestCase):
    def test_btc_be_arm_matches_gold_relative_equivalence(self) -> None:
        """The core mission invariant: BTC's %-derived $ threshold at its
        own price must reproduce the SAME RELATIVE (%) protection as
        GOLD's flat $2 at $4000 — both should compute to 0.05% of entry
        price."""
        cfg = ExitV2Config()  # btc_be_arm_pct=0.05 default, == GOLD's 2/4000
        btc_price = 63000.0
        pos = _pos(ticket=2, symbol="BTCUSD#", profit=0.0, price_open=btc_price)
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, _btc_symbol_info(), cfg, {})
        expected_usd = btc_price * (0.05 / 100.0) * (0.01 / 0.01 * 0.01)  # tick ratio * volume
        self.assertAlmostEqual(action["be_arm_usd_effective"], expected_usd, places=4)
        # relative check: effective threshold / (price move it represents) == 0.05%
        price_move_for_threshold = action["be_arm_usd_effective"] / (0.01 / 0.01 * 0.01)
        self.assertAlmostEqual(price_move_for_threshold / btc_price * 100, 0.05, places=6)

    def test_btc_arms_at_the_correct_relative_level_not_the_old_flat_2_dollars(self) -> None:
        """Reproduces the mission's exact scenario: at BTC ~$63000, the OLD
        flat $2 threshold demanded 0.0032% move headroom... no — demanded
        a $2 profit which was only 0.05%... wait: this test proves BTC now
        arms at a DIFFERENT (smaller, correctly-scaled) dollar amount than
        the old flat $2, matching 0.05% of ITS OWN price instead."""
        cfg = ExitV2Config()
        btc_price = 63000.0
        state = {}
        # profit just below the new %-based threshold -> must NOT arm
        below = btc_price * (0.05 / 100.0) * 0.01 - 0.001
        pos = _pos(ticket=3, symbol="BTCUSD#", profit=below, price_open=btc_price)
        with patch("app.services.exit_v2.log"):
            evaluate_exit_v2(pos, None, _btc_symbol_info(), cfg, state)
        self.assertFalse(state[3]["be_armed"])

        # profit at/above the new threshold -> must arm
        at_threshold = btc_price * (0.05 / 100.0) * 0.01 + 0.001
        pos2 = _pos(ticket=3, symbol="BTCUSD#", profit=at_threshold, price_open=btc_price)
        with patch("app.services.exit_v2.log"):
            evaluate_exit_v2(pos2, None, _btc_symbol_info(), cfg, state)
        self.assertTrue(state[3]["be_armed"])

    def test_btc_falls_back_to_flat_usd_when_pct_disabled(self) -> None:
        cfg = ExitV2Config(btc_pct_thresholds_enabled=False)
        pos = _pos(ticket=4, symbol="BTCUSD#", profit=2.00, price_open=63000.0)
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, _btc_symbol_info(), cfg, {})
        self.assertEqual(action["threshold_scale"], "USD")
        self.assertAlmostEqual(action["be_arm_usd_effective"], 2.00, places=2)

    def test_btc_falls_back_to_flat_usd_when_symbol_info_missing(self) -> None:
        cfg = ExitV2Config()
        pos = _pos(ticket=5, symbol="BTCUSD#", profit=2.00, price_open=63000.0)
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, None, cfg, {})
        self.assertEqual(action["threshold_scale"], "USD")
        self.assertAlmostEqual(action["be_arm_usd_effective"], 2.00, places=2)

    def test_close_event_carries_effective_threshold(self) -> None:
        cfg = ExitV2Config()
        btc_price = 63000.0
        state = {6: {"peak_usd": 100.0, "be_armed": True}}
        pos = _pos(ticket=6, symbol="BTCUSD#", profit=0.0, price_open=btc_price)
        with patch("app.services.exit_v2.log"):
            action = evaluate_exit_v2(pos, None, _btc_symbol_info(), cfg, state)
        self.assertEqual(action["action"], "CLOSE")
        self.assertEqual(action["threshold_scale"], "PCT")
        self.assertIsNotNone(action["be_arm_usd_effective"])


class TestBtcThresholdLogging(unittest.TestCase):
    """mission's explicit requirement: '[EXIT_V2] doit montrer le seuil
    effectif (% et $ équivalent) par symbole'."""

    def test_be_armed_log_includes_scale_and_pct(self) -> None:
        cfg = ExitV2Config()
        btc_price = 63000.0
        threshold_usd = btc_price * (0.05 / 100.0) * 0.01
        pos = _pos(ticket=7, symbol="BTCUSD#", profit=threshold_usd + 0.01, price_open=btc_price)
        with patch("app.services.exit_v2.log") as mock_log:
            evaluate_exit_v2(pos, None, _btc_symbol_info(), cfg, {})
        mock_log.info.assert_called_once()
        call_args = mock_log.info.call_args[0]
        self.assertIn("scale=%s", call_args[0])
        self.assertIn("be_arm_pct", call_args[0])
        self.assertIn("PCT", call_args)
        self.assertIn(0.05, call_args)


if __name__ == "__main__":
    unittest.main()
