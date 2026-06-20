"""Tests for BTC Market Narrator (Pièce 1).

Covers:
- Blocking verdicts: TRAPPING, EXHAUSTED, DANGEROUS
- Positive verdicts: MARKET_WANTS_UP, UNDECIDED, COMPRESSING
- Narrative upgrade B → A (verdict_upgrades_b_to_a)
- Exit profile selection (HOLD_IF_STRONG, NORMAL_SCALP, FAST_POSITIVE)
- Optional inputs (no CVD, no order flow) — no crash, no penalty
- Silent risks
- Safety invariants: zero MT5 import, zero order_send
"""
from __future__ import annotations

import inspect
import unittest

from app.mt5.btc_market_narrator import BtcMarketNarrator, BtcNarratorInput, BtcNarrative


# ---------------------------------------------------------------------------
# Test fixture builder
# ---------------------------------------------------------------------------

def _inp(**overrides) -> BtcNarratorInput:
    """All signals strongly aligned for BUY — override to test deviations."""
    defaults: dict = {
        "smc_direction": "BULLISH",
        "smc_score": 0.85,
        "smc_order_block_bull": True,
        "smc_order_block_bear": False,
        "smc_fvg_bull": True,
        "smc_fvg_bear": False,
        "smc_liquidity_above": False,
        "smc_liquidity_below": False,
        "smc_inducement": False,
        "mtfa_bias": "BULL",
        "mtfa_score": 80.0,
        "mtfa_h1_direction": "BULLISH",
        "mtfa_m15_liquidity_ok": True,
        "mtfa_cisd_m5": True,
        "confluence_score": 88.0,
        "confluence_grade": "A",
        "order_flow_bonus": 5.0,
        "danger_signal_count": 0,
        "price_in_premium": False,
        "price_in_discount": True,
        "atr_m5": 120.0,
        "impulse_score": 0.40,
        "range_compression": 0.55,
        "m1_last_body_pct": 0.45,
        "m1_upper_wick_pct": 0.25,
        "m1_lower_wick_pct": 0.30,
        "m5_ema_stack": "BULL",
        "m5_last_close_vs_open": 60.0,
        "cvd_slope_m5": 0.35,
        "delta_last": 600.0,
        "dominant_side": "BUYERS",
        "session_name": "LONDON",
        "session_quality": 0.80,
        "direction": "BUY",
        "server_time_utc": 1718700000,
    }
    defaults.update(overrides)
    return BtcNarratorInput(**defaults)


# ---------------------------------------------------------------------------
# Blocking verdicts
# ---------------------------------------------------------------------------

class TestBlockingVerdicts(unittest.TestCase):

    def setUp(self):
        self.narrator = BtcMarketNarrator()

    def test_trapping_verdict_blocks_entry(self):
        """smc_inducement=True + smc_liquidity_above=True + BUY → MARKET_IS_TRAPPING, blocks_entry=True."""
        result = self.narrator.narrate(_inp(smc_inducement=True, smc_liquidity_above=True))
        self.assertEqual(result.verdict, "MARKET_IS_TRAPPING")
        self.assertTrue(result.verdict_blocks_entry)
        self.assertEqual(result.suggested_exit_profile, "FAST_POSITIVE")

    def test_trapping_sell_side(self):
        """smc_inducement=True + smc_liquidity_below=True + SELL → MARKET_IS_TRAPPING."""
        result = self.narrator.narrate(_inp(
            smc_inducement=True, smc_liquidity_below=True,
            direction="SELL", smc_direction="BEARISH",
            mtfa_bias="BEAR", m5_ema_stack="BEAR",
            price_in_discount=False, price_in_premium=True,
            cvd_slope_m5=-0.3,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_TRAPPING")
        self.assertTrue(result.verdict_blocks_entry)

    def test_inducement_without_liquidity_does_not_trap(self):
        """Inducement alone (no matching liquidity) does not trigger TRAPPING."""
        result = self.narrator.narrate(_inp(
            smc_inducement=True,
            smc_liquidity_above=False,
            smc_liquidity_below=False,
        ))
        self.assertNotEqual(result.verdict, "MARKET_IS_TRAPPING")

    def test_exhausted_verdict_blocks_entry(self):
        """impulse_score=0.85 + m1_last_body_pct=0.75 + range_compression<0.2 → EXHAUSTED."""
        result = self.narrator.narrate(_inp(
            impulse_score=0.85,
            m1_last_body_pct=0.75,
            range_compression=0.10,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_EXHAUSTED")
        self.assertTrue(result.verdict_blocks_entry)
        self.assertEqual(result.suggested_exit_profile, "FAST_POSITIVE")

    def test_exhausted_requires_all_three_conditions(self):
        """EXHAUSTED requires impulse>=0.80, body>=0.70, AND range_compression<0.2."""
        # Only 2 of 3 conditions — should NOT be EXHAUSTED
        result = self.narrator.narrate(_inp(
            impulse_score=0.85,
            m1_last_body_pct=0.75,
            range_compression=0.50,   # >= 0.2, so not late
        ))
        self.assertNotEqual(result.verdict, "MARKET_IS_EXHAUSTED")

    def test_dangerous_verdict_from_exit_signals(self):
        """danger_signal_count=2 → MARKET_IS_DANGEROUS, blocks_entry=True."""
        result = self.narrator.narrate(_inp(danger_signal_count=2))
        self.assertEqual(result.verdict, "MARKET_IS_DANGEROUS")
        self.assertTrue(result.verdict_blocks_entry)

    def test_dangerous_verdict_from_smc_contradiction(self):
        """SMC BEARISH (score 0.85) + direction BUY → MARKET_IS_DANGEROUS."""
        result = self.narrator.narrate(_inp(
            smc_direction="BEARISH",
            smc_score=0.85,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_DANGEROUS")
        self.assertTrue(result.verdict_blocks_entry)

    def test_dangerous_verdict_from_mtfa_contradiction(self):
        """mtfa_h1_direction BEARISH (score 75) + direction BUY → MARKET_IS_DANGEROUS."""
        result = self.narrator.narrate(_inp(
            mtfa_h1_direction="BEARISH",
            mtfa_score=75.0,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_DANGEROUS")
        self.assertTrue(result.verdict_blocks_entry)

    def test_narrative_downgrades_a_when_dangerous(self):
        """DANGEROUS verdict sets blocks_entry=True — grade downgrade applied in intelligence engine."""
        result = self.narrator.narrate(_inp(danger_signal_count=2))
        self.assertEqual(result.verdict, "MARKET_IS_DANGEROUS")
        self.assertTrue(result.verdict_blocks_entry)
        self.assertFalse(result.verdict_upgrades_b_to_a)

    def test_trapping_priority_over_dangerous(self):
        """TRAPPING takes priority over DANGEROUS when both conditions met."""
        result = self.narrator.narrate(_inp(
            smc_inducement=True,
            smc_liquidity_above=True,
            danger_signal_count=3,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_TRAPPING")


# ---------------------------------------------------------------------------
# Positive verdicts
# ---------------------------------------------------------------------------

class TestPositiveVerdicts(unittest.TestCase):

    def setUp(self):
        self.narrator = BtcMarketNarrator()

    def test_strong_bull_narrative(self):
        """All BUY signals aligned → MARKET_WANTS_UP, coherence>=0.78."""
        result = self.narrator.narrate(_inp())
        self.assertEqual(result.verdict, "MARKET_WANTS_UP")
        self.assertGreaterEqual(result.coherence, 0.78)
        self.assertFalse(result.verdict_blocks_entry)
        self.assertIn("SMC_ALIGNED", result.strong_confirmations)
        self.assertIn("MTFA_ALIGNED", result.strong_confirmations)

    def test_strong_bear_narrative(self):
        """All SELL signals aligned → MARKET_WANTS_DOWN."""
        result = self.narrator.narrate(_inp(
            direction="SELL",
            smc_direction="BEARISH",
            mtfa_bias="BEAR",
            mtfa_h1_direction="BEARISH",
            m5_ema_stack="BEAR",
            price_in_premium=True,
            price_in_discount=False,
            cvd_slope_m5=-0.35,
            smc_fvg_bull=False,
            smc_fvg_bear=True,
            smc_order_block_bull=False,
            smc_order_block_bear=True,
        ))
        self.assertEqual(result.verdict, "MARKET_WANTS_DOWN")
        self.assertGreaterEqual(result.coherence, 0.78)
        self.assertFalse(result.verdict_blocks_entry)

    def test_undecided_narrative(self):
        """Mixed signals (SMC+MTFA neutral) → MARKET_IS_UNDECIDED."""
        result = self.narrator.narrate(_inp(
            smc_direction="NEUTRAL",
            mtfa_bias="NEUTRAL",
        ))
        self.assertEqual(result.verdict, "MARKET_IS_UNDECIDED")
        self.assertFalse(result.verdict_blocks_entry)
        self.assertFalse(result.verdict_upgrades_b_to_a)

    def test_compression_narrative(self):
        """Low coherence + high range_compression → MARKET_IS_COMPRESSING."""
        result = self.narrator.narrate(_inp(
            smc_direction="NEUTRAL",
            mtfa_bias="NEUTRAL",
            confluence_grade="C",
            order_flow_bonus=-2.0,
            m5_ema_stack="MIXED",
            price_in_discount=False,
            session_quality=0.3,
            mtfa_cisd_m5=False,
            smc_fvg_bull=False,
            smc_order_block_bull=False,
            range_compression=0.75,
            cvd_slope_m5=None,
        ))
        self.assertEqual(result.verdict, "MARKET_IS_COMPRESSING")
        self.assertFalse(result.verdict_blocks_entry)
        self.assertAlmostEqual(result.confidence, 0.4, places=5)

    def test_coherence_is_between_0_and_1(self):
        """Coherence is always in [0, 1]."""
        for grade in ("A+", "A", "B", "C", "D"):
            result = self.narrator.narrate(_inp(confluence_grade=grade))
            self.assertGreaterEqual(result.coherence, 0.0)
            self.assertLessEqual(result.coherence, 1.0)

    def test_wants_up_requires_smc_and_mtfa_aligned(self):
        """coherence>=0.78 alone is not enough — SMC+MTFA must also be aligned."""
        # High coherence but SMC neutral → can't be WANTS_UP
        result = self.narrator.narrate(_inp(smc_direction="NEUTRAL"))
        self.assertNotEqual(result.verdict, "MARKET_WANTS_UP")


# ---------------------------------------------------------------------------
# Upgrade and exit profile
# ---------------------------------------------------------------------------

class TestUpgradesAndProfiles(unittest.TestCase):

    def setUp(self):
        self.narrator = BtcMarketNarrator()

    def test_narrative_upgrades_strong_b_to_a(self):
        """coherence>=0.85 + WANTS_UP + confluence_grade=B → verdict_upgrades_b_to_a=True."""
        # With grade=B, CONFLUENCE_HIGH check fails (−0.15), other checks pass
        # Score = 1.26 − 0.15 = 1.11 → coherence ≈ 0.881
        result = self.narrator.narrate(_inp(confluence_grade="B"))
        self.assertEqual(result.verdict, "MARKET_WANTS_UP")
        self.assertGreaterEqual(result.coherence, 0.85)
        self.assertTrue(result.verdict_upgrades_b_to_a)

    def test_no_upgrade_when_coherence_below_085(self):
        """Grade B upgrade requires coherence >= 0.85."""
        # Fail SMC + MTFA → coherence drops significantly
        result = self.narrator.narrate(_inp(
            confluence_grade="B",
            smc_direction="NEUTRAL",
            mtfa_bias="NEUTRAL",
        ))
        self.assertFalse(result.verdict_upgrades_b_to_a)

    def test_hold_if_strong_only_for_aplus_high_coherence(self):
        """WANTS_UP + coherence>=0.85 + order_flow>0 + cvd>0 → HOLD_IF_STRONG."""
        result = self.narrator.narrate(_inp(
            confluence_grade="A+",
            order_flow_bonus=5.0,
            cvd_slope_m5=0.4,
        ))
        self.assertEqual(result.verdict, "MARKET_WANTS_UP")
        self.assertGreaterEqual(result.coherence, 0.85)
        self.assertEqual(result.suggested_exit_profile, "HOLD_IF_STRONG")

    def test_normal_scalp_when_no_cvd(self):
        """WANTS_UP + coherence>=0.85 + no CVD → NORMAL_SCALP (not HOLD_IF_STRONG)."""
        result = self.narrator.narrate(_inp(
            confluence_grade="A+",
            order_flow_bonus=5.0,
            cvd_slope_m5=None,
        ))
        self.assertEqual(result.verdict, "MARKET_WANTS_UP")
        # cvd_slope_m5 is None so (cvd_slope_m5 or 0) = 0, not > 0 → NORMAL_SCALP
        self.assertEqual(result.suggested_exit_profile, "NORMAL_SCALP")

    def test_fast_positive_for_trapping(self):
        """verdict=TRAPPING → suggested_exit_profile=FAST_POSITIVE."""
        result = self.narrator.narrate(_inp(smc_inducement=True, smc_liquidity_above=True))
        self.assertEqual(result.verdict, "MARKET_IS_TRAPPING")
        self.assertEqual(result.suggested_exit_profile, "FAST_POSITIVE")

    def test_fast_positive_for_exhausted(self):
        """verdict=EXHAUSTED → suggested_exit_profile=FAST_POSITIVE."""
        result = self.narrator.narrate(_inp(
            impulse_score=0.85, m1_last_body_pct=0.75, range_compression=0.10,
        ))
        self.assertEqual(result.suggested_exit_profile, "FAST_POSITIVE")

    def test_fast_positive_for_dangerous(self):
        """verdict=DANGEROUS → suggested_exit_profile=FAST_POSITIVE."""
        result = self.narrator.narrate(_inp(danger_signal_count=2))
        self.assertEqual(result.suggested_exit_profile, "FAST_POSITIVE")

    def test_normal_scalp_for_grade_a_undecided(self):
        """Grade A but UNDECIDED (SMC neutral) → NORMAL_SCALP (confluence A is in exit fallback)."""
        result = self.narrator.narrate(_inp(smc_direction="NEUTRAL", mtfa_bias="NEUTRAL"))
        # Coherence > 0.55 → UNDECIDED. confluence_grade=A → NORMAL_SCALP
        self.assertEqual(result.suggested_exit_profile, "NORMAL_SCALP")


# ---------------------------------------------------------------------------
# Optional inputs
# ---------------------------------------------------------------------------

class TestOptionalInputs(unittest.TestCase):

    def setUp(self):
        self.narrator = BtcMarketNarrator()

    def test_narrator_works_without_cvd(self):
        """cvd_slope_m5=None → no crash, CVD check skipped, no penalty."""
        result_with = self.narrator.narrate(_inp(cvd_slope_m5=0.35))
        result_without = self.narrator.narrate(_inp(cvd_slope_m5=None))
        # Both should succeed
        self.assertIn(result_with.verdict, ["MARKET_WANTS_UP", "MARKET_IS_UNDECIDED"])
        self.assertIn(result_without.verdict, ["MARKET_WANTS_UP", "MARKET_IS_UNDECIDED"])
        # Coherence without CVD can be equally high (no weight deducted)
        self.assertGreater(result_without.coherence, 0.0)

    def test_narrator_works_without_order_flow(self):
        """dominant_side=None, delta_last=None → narrative built normally."""
        result = self.narrator.narrate(_inp(dominant_side=None, delta_last=None))
        self.assertIn(result.verdict, ["MARKET_WANTS_UP", "MARKET_IS_UNDECIDED"])
        self.assertIsInstance(result.coherence, float)

    def test_narrator_works_without_all_optional_fields(self):
        """All optional fields None → no crash, valid narrative."""
        result = self.narrator.narrate(_inp(
            cvd_slope_m5=None,
            delta_last=None,
            dominant_side=None,
        ))
        self.assertIn(result.verdict, list(["MARKET_WANTS_UP", "MARKET_WANTS_DOWN",
                                            "MARKET_IS_UNDECIDED", "MARKET_IS_COMPRESSING",
                                            "MARKET_IS_DANGEROUS", "MARKET_IS_TRAPPING",
                                            "MARKET_IS_EXHAUSTED"]))
        self.assertIsInstance(result.strong_confirmations, list)
        self.assertIsInstance(result.weak_points, list)
        self.assertIsInstance(result.silent_risks, list)

    def test_no_cvd_penalty_on_coherence(self):
        """Without CVD the coherence denominator excludes the CVD weight — no penalty."""
        # With cvd_slope=None, coherence = score_without_cvd / max_without_cvd
        # This should be >= coherence with a failing CVD
        result_none_cvd   = self.narrator.narrate(_inp(cvd_slope_m5=None))
        result_bad_cvd    = self.narrator.narrate(_inp(cvd_slope_m5=-0.5))  # against BUY
        self.assertGreaterEqual(result_none_cvd.coherence, result_bad_cvd.coherence)


# ---------------------------------------------------------------------------
# Silent risks
# ---------------------------------------------------------------------------

class TestSilentRisks(unittest.TestCase):

    def setUp(self):
        self.narrator = BtcMarketNarrator()

    def test_liquidity_above_risk_on_buy(self):
        """smc_liquidity_above=True + BUY → LIQUIDITY_ABOVE_MAY_TRAP in risks (no inducement)."""
        result = self.narrator.narrate(_inp(
            smc_liquidity_above=True,
            smc_inducement=False,   # no trap verdict, just risk
        ))
        self.assertIn("LIQUIDITY_ABOVE_MAY_TRAP", result.silent_risks)

    def test_impulse_risk_when_high(self):
        """impulse_score >= 0.6 → RECENT_IMPULSE_ENTRY_RISK in risks."""
        result = self.narrator.narrate(_inp(impulse_score=0.65, range_compression=0.5))
        self.assertIn("RECENT_IMPULSE_ENTRY_RISK", result.silent_risks)

    def test_low_liquidity_asia_session(self):
        """session_name=ASIA → LOW_LIQUIDITY_SESSION in risks."""
        result = self.narrator.narrate(_inp(session_name="ASIA"))
        self.assertIn("LOW_LIQUIDITY_SESSION", result.silent_risks)

    def test_weak_delta_conviction(self):
        """delta_last with abs() < 0.05 → WEAK_DELTA_NO_CONVICTION."""
        result = self.narrator.narrate(_inp(delta_last=0.02))
        self.assertIn("WEAK_DELTA_NO_CONVICTION", result.silent_risks)

    def test_no_delta_risk_when_none(self):
        """delta_last=None → no WEAK_DELTA_NO_CONVICTION (not available ≠ weak)."""
        result = self.narrator.narrate(_inp(delta_last=None))
        self.assertNotIn("WEAK_DELTA_NO_CONVICTION", result.silent_risks)

    def test_extended_market_risk(self):
        """range_compression<0.2 + impulse_score>=0.5 → EXTENDED_MARKET_LATE_ENTRY."""
        result = self.narrator.narrate(_inp(
            range_compression=0.15,
            impulse_score=0.55,
            m1_last_body_pct=0.4,   # keep below 0.70 to avoid EXHAUSTED
        ))
        self.assertIn("EXTENDED_MARKET_LATE_ENTRY", result.silent_risks)


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariants(unittest.TestCase):

    def test_no_mt5_import(self):
        """btc_market_narrator must not import MetaTrader5."""
        import app.mt5.btc_market_narrator as mod
        src = inspect.getsource(mod)
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("import mt5", src)

    def test_no_order_send(self):
        """btc_market_narrator must not call or import order_send."""
        import app.mt5.btc_market_narrator as mod
        src = inspect.getsource(mod)
        # Docstring may mention "order_send" as a constraint note — check for actual usage
        self.assertNotIn("mt5.order_send", src)
        self.assertNotIn("order_send(", src)

    def test_schema_version(self):
        """BtcNarrative schema_version is btc_narrator.v1."""
        result = BtcMarketNarrator().narrate(_inp())
        self.assertEqual(result.schema_version, "btc_narrator.v1")

    def test_verdict_always_valid(self):
        """All produced verdicts must be in the known set."""
        valid = {
            "MARKET_WANTS_UP", "MARKET_WANTS_DOWN", "MARKET_IS_UNDECIDED",
            "MARKET_IS_DANGEROUS", "MARKET_IS_TRAPPING", "MARKET_IS_EXHAUSTED",
            "MARKET_IS_COMPRESSING",
        }
        for scenario in [
            _inp(),
            _inp(danger_signal_count=2),
            _inp(smc_inducement=True, smc_liquidity_above=True),
            _inp(impulse_score=0.85, m1_last_body_pct=0.75, range_compression=0.1),
            _inp(smc_direction="NEUTRAL", mtfa_bias="NEUTRAL"),
            _inp(range_compression=0.75, smc_direction="NEUTRAL", mtfa_bias="NEUTRAL",
                 confluence_grade="D", order_flow_bonus=-5.0, m5_ema_stack="MIXED",
                 price_in_discount=False, session_quality=0.2, mtfa_cisd_m5=False,
                 smc_fvg_bull=False, smc_order_block_bull=False, cvd_slope_m5=None),
        ]:
            result = BtcMarketNarrator().narrate(scenario)
            self.assertIn(result.verdict, valid, f"Unknown verdict: {result.verdict}")

    def test_exit_profile_always_valid(self):
        """suggested_exit_profile is always one of the three valid values."""
        valid = {"FAST_POSITIVE", "NORMAL_SCALP", "HOLD_IF_STRONG"}
        for scenario in [
            _inp(),
            _inp(danger_signal_count=2),
            _inp(smc_direction="NEUTRAL", mtfa_bias="NEUTRAL"),
        ]:
            result = BtcMarketNarrator().narrate(scenario)
            self.assertIn(result.suggested_exit_profile, valid)

    def test_blocking_verdict_never_upgrades_b(self):
        """Blocking verdicts always set verdict_upgrades_b_to_a=False."""
        for scenario in [
            _inp(smc_inducement=True, smc_liquidity_above=True),
            _inp(impulse_score=0.85, m1_last_body_pct=0.75, range_compression=0.1),
            _inp(danger_signal_count=2),
        ]:
            result = BtcMarketNarrator().narrate(scenario)
            self.assertTrue(result.verdict_blocks_entry)
            self.assertFalse(result.verdict_upgrades_b_to_a)

    def test_returns_btc_narrative_type(self):
        """narrate() always returns a BtcNarrative instance."""
        result = BtcMarketNarrator().narrate(_inp())
        self.assertIsInstance(result, BtcNarrative)

    def test_coherence_and_confidence_are_finite_floats(self):
        """coherence and confidence are always finite floats."""
        import math
        result = BtcMarketNarrator().narrate(_inp())
        self.assertTrue(math.isfinite(result.coherence))
        self.assertTrue(math.isfinite(result.confidence))
        self.assertGreaterEqual(result.coherence, 0.0)
        self.assertLessEqual(result.coherence, 1.0)
        self.assertGreaterEqual(result.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
