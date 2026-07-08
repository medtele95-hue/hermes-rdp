# -*- coding: utf-8 -*-
"""COEUR_V2 chantier 1 — FINAL_CONFLUENCE devient une somme pondérée normalisée
(geo/smc/mtfa/of chacun sur 0-100) au lieu d'une somme brute où geo_score (0-100)
dominait structurellement smc/mtfa (±15) et of (-5/+20). Voir MATH_CORE_AUDIT.md
§3 et COEUR_V2_REPORT.md pour la preuve du déséquilibre corrigé ici."""
from __future__ import annotations

import unittest

import pandas as pd

from app.agents.confluence_engine import _DEFAULT_WEIGHTS, ConfluenceEngine, _of_norm


def _flat_frames(n: int = 25, price: float = 100.0) -> dict:
    df = pd.DataFrame({
        "open": [price] * n, "high": [price + 0.5] * n,
        "low": [price - 0.5] * n, "close": [price] * n,
    })
    return {"M5": df}


class TestDefaultWeights(unittest.TestCase):
    def test_default_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(_DEFAULT_WEIGHTS.values()), 1.0, places=6)

    def test_of_weight_is_dominant(self) -> None:
        """Order-flow (the strategy actually active in production) keeps the
        strongest weight — geo becomes one voice among four, not the whole sum."""
        self.assertEqual(max(_DEFAULT_WEIGHTS, key=_DEFAULT_WEIGHTS.get), "of")


class TestOfNormHelper(unittest.TestCase):
    def test_uses_order_flow_reader_score(self) -> None:
        self.assertEqual(_of_norm({"order_flow_reader": {"score": 72.0}}), 72.0)

    def test_clamped_to_0_100(self) -> None:
        self.assertEqual(_of_norm({"order_flow_reader": {"score": 150.0}}), 100.0)
        self.assertEqual(_of_norm({"order_flow_reader": {"score": -30.0}}), 0.0)

    def test_missing_data_defaults_neutral_50(self) -> None:
        self.assertEqual(_of_norm({}), 50.0)
        self.assertEqual(_of_norm({"order_flow_reader": "INVALID"}), 50.0)


class TestGeoNoLongerDominatesAlone(unittest.TestCase):
    """The exact scenario MATH_CORE_AUDIT.md §3 proved as broken: a candidate with
    strong geometry but ZERO SMC/MTFA/OF confirmation used to score up to grade A
    on geometry alone. Under the v2 weighted sum it cannot, by construction, exceed
    w_geo*100 + w_of*50 (neutral OF default) when smc/mtfa are genuinely absent."""

    def test_geo_only_candidate_capped_by_weight(self) -> None:
        engine = ConfluenceEngine()
        # Trending data pushes geo_score up; no smc/mtfa/of context at all.
        n = 30
        closes = [100.0 + i * 0.8 for i in range(n)]
        df = pd.DataFrame({
            "open": [c - 0.2 for c in closes], "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes], "close": closes,
        })
        result = engine.evaluate("GOLD", "NON_OF_NATIVE_STRATEGY", {"M5": df}, {})
        # Even if geo_score maxes at 100, score <= 0.25*100 + 0.25*0 + 0.20*0 + 0.30*50 = 40
        self.assertLessEqual(result["score"], 40.5)

    def test_confirmed_candidate_with_flat_geometry_still_scores_meaningfully(self) -> None:
        """The mirror case: strong SMC+MTFA+OF confirmation with flat/no-pattern
        price action must NOT be structurally capped near-zero the way the old
        raw-sum formula effectively required geo to carry the score."""
        engine = ConfluenceEngine()
        ctx = {
            "smc_confluence_score": 90.0,
            "mtfa_score": 90.0,
            "order_flow_reader": {"score": 90.0, "signal": "BUY"},
        }
        result = engine.evaluate("GOLD", "NON_OF_NATIVE_STRATEGY", _flat_frames(), ctx)
        # 0.25*geo(~low) + 0.25*90 + 0.20*90 + 0.30*90 >= ~63 even with geo=0
        self.assertGreaterEqual(result["score"], 60.0)


class TestWeightsConfigurable(unittest.TestCase):
    def test_custom_weights_change_score(self) -> None:
        engine = ConfluenceEngine()
        ctx = {"order_flow_reader": {"score": 100.0, "signal": "BUY"}}
        default_result = engine.evaluate("GOLD", "NON_OF_NATIVE", _flat_frames(), ctx)
        of_heavy = engine.evaluate(
            "GOLD", "NON_OF_NATIVE", _flat_frames(), ctx,
            weights={"geo": 0.0, "smc": 0.0, "mtfa": 0.0, "of": 1.0},
        )
        self.assertAlmostEqual(of_heavy["score"], 100.0, places=1)
        self.assertNotEqual(default_result["score"], of_heavy["score"])

    def test_weights_present_in_result(self) -> None:
        engine = ConfluenceEngine()
        result = engine.evaluate("GOLD", "X", _flat_frames(), {})
        self.assertEqual(result["weights"], _DEFAULT_WEIGHTS)

    def test_zero_weights_fall_back_to_defaults(self) -> None:
        engine = ConfluenceEngine()
        result = engine.evaluate(
            "GOLD", "X", _flat_frames(), {},
            weights={"geo": 0.0, "smc": 0.0, "mtfa": 0.0, "of": 0.0},
        )
        self.assertEqual(result["weights"], _DEFAULT_WEIGHTS)


class TestLegacyScorePreservedForComparison(unittest.TestCase):
    def test_legacy_score_and_grade_present(self) -> None:
        engine = ConfluenceEngine()
        result = engine.evaluate("GOLD", "SIMO_ATM_BREAKOUT", _flat_frames(), {})
        self.assertIn("legacy_score", result)
        self.assertIn("legacy_grade", result)
        self.assertGreaterEqual(result["legacy_score"], 0.0)
        self.assertLessEqual(result["legacy_score"], 100.0)

    def test_components_v2_present(self) -> None:
        engine = ConfluenceEngine()
        ctx = {"smc_confluence_score": 70.0, "mtfa_score": 60.0, "order_flow_reader": {"score": 80.0}}
        result = engine.evaluate("GOLD", "SIMO_ATM_BREAKOUT", _flat_frames(), ctx)
        v2 = result["components_v2"]
        self.assertEqual(v2["smc_norm"], 70.0)
        self.assertEqual(v2["mtfa_norm"], 60.0)
        self.assertEqual(v2["of_norm"], 80.0)


class TestOfNativeNeutralFloorV2(unittest.TestCase):
    """Normalized-domain translation of the legacy `max(0.0, contrib)` clamp:
    OF-native strategies must never have smc_norm/mtfa_norm dragged below neutral
    (50) by weak SMC/MTFA — but a GOOD SMC/MTFA reading can still help."""

    def test_weak_smc_mtfa_floored_at_neutral_for_of_native(self) -> None:
        engine = ConfluenceEngine()
        ctx = {"smc_confluence_score": 5.0, "mtfa_score": 5.0, "order_flow_reader": {"score": 90.0, "grade": "A"}}
        result = engine.evaluate("GOLD", "ORDER_FLOW_EXECUTION_AGENT", _flat_frames(), ctx)
        self.assertEqual(result["components_v2"]["smc_norm"], 50.0)
        self.assertEqual(result["components_v2"]["mtfa_norm"], 50.0)

    def test_strong_smc_mtfa_not_capped_at_neutral_for_of_native(self) -> None:
        engine = ConfluenceEngine()
        ctx = {"smc_confluence_score": 95.0, "mtfa_score": 95.0, "order_flow_reader": {"score": 90.0, "grade": "A"}}
        result = engine.evaluate("GOLD", "ORDER_FLOW_EXECUTION_AGENT", _flat_frames(), ctx)
        self.assertEqual(result["components_v2"]["smc_norm"], 95.0)
        self.assertEqual(result["components_v2"]["mtfa_norm"], 95.0)


class TestBtcRangeModeV2Floor(unittest.TestCase):
    def test_btc_range_smc_floored_at_40_not_dragged_to_zero(self) -> None:
        engine = ConfluenceEngine()
        ctx = {
            "smc_confluence": {"smc_h4_direction": "RANGE", "smc_h1_trend": "RANGE", "smc_confluence_score": 5},
            "mtfa": {"mtfa_score": 80},
        }
        result = engine.evaluate("BTCUSD#", "NON_OF_NATIVE", _flat_frames(), ctx)
        self.assertEqual(result["components_v2"]["smc_norm"], 40.0)

    def test_non_btc_not_floored(self) -> None:
        engine = ConfluenceEngine()
        ctx = {
            "smc_confluence": {"smc_h4_direction": "RANGE", "smc_h1_trend": "RANGE", "smc_confluence_score": 5},
            "mtfa": {"mtfa_score": 80},
        }
        result = engine.evaluate("GOLD", "NON_OF_NATIVE", _flat_frames(), ctx)
        self.assertEqual(result["components_v2"]["smc_norm"], 5.0)


if __name__ == "__main__":
    unittest.main()
