"""Integration tests: btc_setup_intelligence + BtcMarketNarrator (Pièce 2).

Covers:
- narrator_input=None → fully backward-compatible, same results as before
- TRAPPING/EXHAUSTED/DANGEROUS → BLOCK regardless of 12-factor score
- Grade B upgrade to A when narrative coherence very high
- Grade A/A+ downgrade when narrative UNDECIDED + low coherence
- Silent risks propagated to reasons with RISK: prefix
- Exit profile override (conservative only)
- Narrative fields present in all return dicts (default "N/A"/0.0/[] when no narrator)
- Non-BTC symbols with narrator_input=None work fine
- Safety: no MT5 import added, no order_send
"""
from __future__ import annotations

import inspect
import unittest

from app.mt5.btc_market_narrator import BtcNarratorInput
from app.mt5.btc_setup_intelligence import evaluate_btc_setup_intelligence


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _perf(**overrides) -> dict:
    base = {
        "adaptive_mode": "NORMAL",
        "is_paused": False,
        "win_streak": 0,
        "loss_streak": 0,
        "requires_aplus": False,
    }
    base.update(overrides)
    return base


def _strong_buy_ctx() -> dict:
    """Market context that produces A+ grade from the 12-factor engine."""
    return {
        "price": 65200.0, "bid": 65200.0,
        "vwap": 65000.0,           # above vwap → good for BUY
        "poc": 64900.0,            # above poc → good for BUY
        "cvd_slope": 0.5,          # strong positive CVD
        "delta_proxy": 800.0,      # strong positive delta
        "order_flow_signal": "BUY",
        "m1_momentum": "BULLISH",
        "m5_momentum": "BULLISH",
        "spread": 30.0,
        "max_spread": 200.0,       # frac 0.15 → OK
        "volatility_status": "NORMAL",
        "session": "LONDON_NY",    # best session
    }


def _a_grade_ctx() -> dict:
    """Context that produces grade A (score ~78) from the 12-factor engine."""
    return {
        "price": 65200.0, "bid": 65200.0,
        "vwap": 65000.0,
        "poc": 64900.0,
        "cvd_slope": 0.15,         # mild CVD
        "delta_proxy": 200.0,      # moderate delta
        "order_flow_signal": "BUY",
        "m1_momentum": "NEUTRAL",  # reduced
        "m5_momentum": "NEUTRAL",  # reduced
        "spread": 30.0,
        "max_spread": 200.0,
        "volatility_status": "NORMAL",
        "session": "LONDON",
    }


def _b_grade_ctx() -> dict:
    """Context that produces grade B (score ~73) from the 12-factor engine.

    Price below VWAP for BUY (against), mildly opposing CVD.
    """
    return {
        "price": 65200.0, "bid": 65200.0,
        "vwap": 65300.0,           # price BELOW vwap → against BUY (0 pts)
        "poc": 64900.0,            # above poc → good for BUY
        "cvd_slope": -0.08,        # slightly negative, >= -0.1 → 0.25 pts
        "delta_proxy": 200.0,
        "order_flow_signal": "BUY",
        "m1_momentum": "BULLISH",
        "m5_momentum": "NEUTRAL",
        "spread": 30.0,
        "max_spread": 200.0,
        "volatility_status": "NORMAL",
        "session": "LONDON",
    }


def _good_cand() -> dict:
    return {"smc_score": 70.0, "mtfa_score": 60.0, "rr": 2.0}


def _narrator_inp_strong_buy(**overrides) -> BtcNarratorInput:
    """Narrator input: all signals strongly aligned for BUY."""
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
        "confluence_grade": "B",
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


def _narrator_inp_trapping() -> BtcNarratorInput:
    """Narrator input that triggers MARKET_IS_TRAPPING verdict."""
    return _narrator_inp_strong_buy(
        smc_inducement=True,
        smc_liquidity_above=True,
    )


def _narrator_inp_dangerous() -> BtcNarratorInput:
    """Narrator input that triggers MARKET_IS_DANGEROUS via exit signals."""
    return _narrator_inp_strong_buy(danger_signal_count=2)


def _narrator_inp_undecided_low_coherence() -> BtcNarratorInput:
    """Narrator input that produces UNDECIDED with coherence << 0.55."""
    return _narrator_inp_strong_buy(
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
        range_compression=0.40,
        cvd_slope_m5=None,
    )


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility(unittest.TestCase):

    def test_narrator_input_backward_compatible(self):
        """evaluate_btc_setup_intelligence without narrator_input → same structure as before."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            # narrator_input intentionally omitted
        )
        self.assertIn("decision", result)
        self.assertIn("setup_quality_score", result)
        self.assertIn("grade", result)
        self.assertIn("reasons", result)
        self.assertIn("entry_mode", result)
        self.assertIn("confidence_multiplier", result)
        self.assertIn("exit_profile", result)

    def test_narrator_none_gives_default_narrative_fields(self):
        """narrator_input=None → narrative fields default to N/A/0.0/[]."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=None,
        )
        self.assertEqual(result["narrative_verdict"], "N/A")
        self.assertEqual(result["narrative_coherence"], 0.0)
        self.assertEqual(result["narrative_confidence"], 0.0)
        self.assertEqual(result["strong_confirmations"], [])
        self.assertEqual(result["silent_risks"], [])

    def test_narrator_not_called_for_non_btc_symbols(self):
        """Non-BTC symbol with narrator_input=None → narrative fields default."""
        result = evaluate_btc_setup_intelligence(
            "GOLD#", "GOLD_LIQUIDITY_HUNTER_PRO", "BUY",
            {"smc_score": 80.0, "rr": 2.0},
            {"price": 2000.0, "vwap": 1995.0, "session": "LONDON"},
            {},
            _perf(),
            narrator_input=None,
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["narrative_verdict"], "N/A")
        self.assertEqual(result["narrative_coherence"], 0.0)

    def test_existing_decision_logic_unchanged_without_narrator(self):
        """Without narrator, decision logic is identical to original behavior."""
        # A+ grade in NORMAL mode should PASS
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
        )
        self.assertEqual(result["decision"], "PASS")
        self.assertIn(result["grade"], ("A+", "A"))

    def test_invalid_direction_still_returns_narrative_fields(self):
        """_result() early return also contains narrative fields (defaults)."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "WAIT",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("narrative_verdict", result)
        self.assertEqual(result["narrative_verdict"], "N/A")


# ---------------------------------------------------------------------------
# Blocking verdicts override high score
# ---------------------------------------------------------------------------

class TestNarratorBlockOverridesScore(unittest.TestCase):

    def test_narrator_block_overrides_high_score_trapping(self):
        """score=A+ but narrator=TRAPPING → decision=BLOCK."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_trapping(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("NARRATIVE_BLOCK:MARKET_IS_TRAPPING", result["reasons"])

    def test_narrator_block_overrides_high_score_dangerous(self):
        """score=A+ but narrator=DANGEROUS (danger_signal_count=2) → BLOCK."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_dangerous(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("NARRATIVE_BLOCK:MARKET_IS_DANGEROUS", result["reasons"])

    def test_blocking_result_includes_narrative_fields(self):
        """The early BLOCK return from narrator includes narrative fields."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_trapping(),
        )
        self.assertEqual(result["narrative_verdict"], "MARKET_IS_TRAPPING")
        self.assertGreater(result["narrative_coherence"], 0.0)
        self.assertIsInstance(result["strong_confirmations"], list)
        self.assertIsInstance(result["silent_risks"], list)

    def test_blocking_result_entry_mode_defensive(self):
        """Narrator block returns entry_mode=DEFENSIVE."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_trapping(),
        )
        self.assertEqual(result["entry_mode"], "DEFENSIVE")
        self.assertEqual(result["exit_profile"], "FAST_POSITIVE")

    def test_blocking_result_no_lot_field(self):
        """Narrator BLOCK result must not contain lot fields."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_trapping(),
        )
        self.assertNotIn("lot_size", result)
        self.assertNotIn("lot", result)


# ---------------------------------------------------------------------------
# Grade upgrade B → A
# ---------------------------------------------------------------------------

class TestNarratorGradeUpgrade(unittest.TestCase):

    def test_narrator_upgrade_applied_to_strong_b(self):
        """Strong B (score>=70) + high narrative coherence → grade becomes A."""
        # First verify without narrator: should be grade B
        without = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _b_grade_ctx(), {}, _perf(),
        )
        self.assertEqual(without["grade"], "B",
                         f"Expected B grade without narrator, got {without['grade']} (score={without['setup_quality_score']})")

        # With strong narrator (coherence >= 0.85, upgrades_b_to_a=True)
        with_narrator = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _b_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(),
        )
        self.assertEqual(with_narrator["grade"], "A",
                         f"Expected A after upgrade, got {with_narrator['grade']} "
                         f"(coherence={with_narrator['narrative_coherence']:.3f})")
        self.assertTrue(
            any("NARRATIVE_UPGRADE_B_TO_A" in r for r in with_narrator["reasons"]),
            f"Missing upgrade reason in: {with_narrator['reasons']}",
        )

    def test_upgrade_only_when_score_above_threshold(self):
        """Grade B upgrade does NOT fire when score < STRONG_B_THRESHOLD (70.0)."""
        # Use context where score falls below 70 (no upgrade eligible)
        low_b_ctx = {
            **_b_grade_ctx(),
            "order_flow_signal": "WAIT",   # neutral → 0.5 * 15 = 7.5 (instead of 15)
            "m1_momentum": "NEUTRAL",
            "session": "ASIA",             # 0.6 * 8 = 4.8
        }
        without = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), low_b_ctx, {}, _perf(),
        )
        # Verify it's still grade B (not C/D) and score < 70
        if without["grade"] != "B" or without["setup_quality_score"] >= 70.0:
            self.skipTest(
                f"Context produced grade={without['grade']} score={without['setup_quality_score']}, "
                "not eligible for this test"
            )
        with_narrator = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), low_b_ctx, {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(),
        )
        # Score below threshold → no upgrade
        self.assertEqual(with_narrator["grade"], "B")
        self.assertFalse(
            any("NARRATIVE_UPGRADE_B_TO_A" in r for r in with_narrator["reasons"])
        )

    def test_upgrade_requires_high_coherence(self):
        """Upgrade B→A requires narrator coherence >= 0.85 (low coherence → no upgrade)."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _b_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_undecided_low_coherence(),
        )
        # Low coherence → no upgrade (grade stays B or gets downgraded)
        self.assertNotIn("A+", [result["grade"]])
        self.assertFalse(
            any("NARRATIVE_UPGRADE_B_TO_A" in r for r in result["reasons"])
        )

    def test_b_upgrade_passes_in_normal_mode(self):
        """After B→A upgrade in normal mode, decision should be PASS."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _b_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(),
        )
        if result["grade"] == "A":
            self.assertEqual(result["decision"], "PASS")


# ---------------------------------------------------------------------------
# Grade downgrade A → B
# ---------------------------------------------------------------------------

class TestNarratorGradeDowngrade(unittest.TestCase):

    def test_a_grade_downgraded_to_b_when_undecided_low_coherence(self):
        """grade=A + narrative=UNDECIDED + coherence<0.55 → grade becomes B."""
        without = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _a_grade_ctx(), {}, _perf(),
        )
        # Verify baseline is A grade
        if without["grade"] not in ("A", "A+"):
            self.skipTest(f"Context produced {without['grade']}, need A or A+")

        with_narrator = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _a_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_undecided_low_coherence(),
        )
        self.assertLess(
            with_narrator["narrative_coherence"], 0.55,
            f"Expected coherence < 0.55, got {with_narrator['narrative_coherence']}"
        )
        self.assertEqual(with_narrator["narrative_verdict"], "MARKET_IS_UNDECIDED")
        # A → B downgrade
        if without["grade"] == "A":
            self.assertEqual(with_narrator["grade"], "B",
                             f"Expected B after downgrade, got {with_narrator['grade']}")
            self.assertTrue(
                any("NARRATIVE_DOWNGRADE" in r for r in with_narrator["reasons"])
            )

    def test_downgrade_does_not_apply_when_coherence_high(self):
        """No downgrade when narrative coherence >= 0.55."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _a_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(
                # Give it some but not all signals — coherence ~0.70 (>= 0.55)
                smc_direction="NEUTRAL",
                mtfa_bias="NEUTRAL",
            ),
        )
        # Coherence might be >= 0.55 (many other checks pass)
        if result["narrative_coherence"] >= 0.55:
            self.assertFalse(
                any("NARRATIVE_DOWNGRADE" in r for r in result["reasons"])
            )


# ---------------------------------------------------------------------------
# Silent risks propagation
# ---------------------------------------------------------------------------

class TestNarratorSilentRisks(unittest.TestCase):

    def test_narrator_silent_risks_in_reasons(self):
        """Silent risks from narrator are added to reasons with RISK: prefix."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(
                session_name="ASIA",    # → LOW_LIQUIDITY_SESSION risk
                delta_last=0.01,        # → WEAK_DELTA_NO_CONVICTION risk
            ),
        )
        risk_reasons = [r for r in result["reasons"] if r.startswith("RISK:")]
        self.assertTrue(len(risk_reasons) >= 1, f"Expected RISK: reasons, got: {result['reasons']}")
        self.assertTrue(any("LOW_LIQUIDITY_SESSION" in r for r in risk_reasons),
                        f"Missing RISK:LOW_LIQUIDITY_SESSION in {risk_reasons}")

    def test_no_silent_risks_when_no_narrator(self):
        """No RISK: prefixed reasons when narrator_input is None."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
        )
        risk_reasons = [r for r in result["reasons"] if r.startswith("RISK:")]
        self.assertEqual(risk_reasons, [])

    def test_silent_risks_also_in_return_dict(self):
        """silent_risks in return dict contains the raw list (without RISK: prefix)."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(session_name="ASIA"),
        )
        self.assertIsInstance(result["silent_risks"], list)
        # Raw list has no prefix
        self.assertTrue(
            any("LOW_LIQUIDITY_SESSION" in r for r in result["silent_risks"])
        )


# ---------------------------------------------------------------------------
# Exit profile override
# ---------------------------------------------------------------------------

class TestNarratorExitProfileOverride(unittest.TestCase):

    def test_narrator_forces_fast_positive_for_trapping(self):
        """Blocking verdict → exit_profile=FAST_POSITIVE in block return."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_trapping(),
        )
        self.assertEqual(result["exit_profile"], "FAST_POSITIVE")

    def test_narrator_conservative_exit_cannot_force_hold_on_a_grade(self):
        """HOLD_IF_STRONG from narrator only applies if grade=A+; A grade stays NORMAL_SCALP."""
        # Get a setup with grade A (not A+) and strong narrator suggesting HOLD_IF_STRONG
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _a_grade_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(
                confluence_grade="A+",    # strong narrator
                order_flow_bonus=5.0,
                cvd_slope_m5=0.4,
            ),
        )
        if result["grade"] == "A":
            # A grade: HOLD_IF_STRONG is only allowed for A+ by the override rule
            self.assertIn(result["exit_profile"], ("NORMAL_SCALP", "FAST_POSITIVE"))


# ---------------------------------------------------------------------------
# Narrative fields in return dict
# ---------------------------------------------------------------------------

class TestNarrativeFieldsInReturnDict(unittest.TestCase):

    def test_narrative_fields_always_present(self):
        """All narrative fields are present in every return, with or without narrator."""
        required = {
            "narrative_verdict",
            "narrative_coherence",
            "narrative_confidence",
            "strong_confirmations",
            "silent_risks",
        }
        for narrator in [None, _narrator_inp_strong_buy(), _narrator_inp_trapping()]:
            result = evaluate_btc_setup_intelligence(
                "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
                _good_cand(), _strong_buy_ctx(), {}, _perf(),
                narrator_input=narrator,
            )
            for key in required:
                self.assertIn(key, result, f"Key '{key}' missing with narrator={type(narrator).__name__}")

    def test_narrative_fields_types(self):
        """Narrative fields have correct types when narrator is provided."""
        result = evaluate_btc_setup_intelligence(
            "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
            _good_cand(), _strong_buy_ctx(), {}, _perf(),
            narrator_input=_narrator_inp_strong_buy(),
        )
        self.assertIsInstance(result["narrative_verdict"], str)
        self.assertIsInstance(result["narrative_coherence"], float)
        self.assertIsInstance(result["narrative_confidence"], float)
        self.assertIsInstance(result["strong_confirmations"], list)
        self.assertIsInstance(result["silent_risks"], list)

    def test_narrative_coherence_in_valid_range(self):
        """narrative_coherence is always in [0, 1]."""
        for narrator in [None, _narrator_inp_strong_buy(), _narrator_inp_undecided_low_coherence()]:
            result = evaluate_btc_setup_intelligence(
                "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
                _good_cand(), _b_grade_ctx(), {}, _perf(),
                narrator_input=narrator,
            )
            self.assertGreaterEqual(result["narrative_coherence"], 0.0)
            self.assertLessEqual(result["narrative_coherence"], 1.0)


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariants(unittest.TestCase):

    def test_no_mt5_import_in_intelligence(self):
        """btc_setup_intelligence must not import MetaTrader5."""
        import app.mt5.btc_setup_intelligence as mod
        src = inspect.getsource(mod)
        self.assertNotIn("import MetaTrader5", src)

    def test_no_order_send_call_in_intelligence(self):
        """btc_setup_intelligence must not call order_send."""
        import app.mt5.btc_setup_intelligence as mod
        src = inspect.getsource(mod)
        self.assertNotIn("mt5.order_send", src)
        self.assertNotIn("order_send(", src)

    def test_no_lot_field_in_any_result(self):
        """Intelligence engine result never contains lot or lot_size key."""
        for narrator in [None, _narrator_inp_strong_buy(), _narrator_inp_trapping()]:
            result = evaluate_btc_setup_intelligence(
                "BTCUSD#", "BTC_SCALPING_AGENT", "BUY",
                _good_cand(), _strong_buy_ctx(), {}, _perf(),
                narrator_input=narrator,
            )
            self.assertNotIn("lot_size", result)
            self.assertNotIn("lot", result)

    def test_demo_live_trading_remains_false(self):
        """Neither btc_setup_intelligence nor btc_market_narrator touch live trading."""
        import app.mt5.btc_setup_intelligence as intel_mod
        import app.mt5.btc_market_narrator as narrator_mod
        for mod in [intel_mod, narrator_mod]:
            src = inspect.getsource(mod)
            self.assertNotIn("allow_live_trading = True", src)
            self.assertNotIn("allow_live_trading=True", src)


if __name__ == "__main__":
    unittest.main()
