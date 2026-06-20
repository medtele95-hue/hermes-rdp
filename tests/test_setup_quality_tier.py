"""Tests for app/agents/setup_quality_tier.py — §4.0 tier system."""
from __future__ import annotations

import pytest

from app.agents.setup_quality_tier import (
    _b1_liquidity_sweep,
    _b2_fvg_aligned,
    _b3_cvd_divergence,
    _b4_impulse_strength,
    _b5_deep_zone,
    _b6_quality_session,
    _b7_clean_path,
    _b8_volume_confirmed,
    _b9_absorption_confirmed,
    _m1_bias_aligned,
    _m2_structure_and_ob,
    _m3_entry_box,
    _m4_of_all_agree,
    _om1_not_opposing,
    _om2_near_key_level,
    _om4_real_participation,
    evaluate_setup_quality,
    tier_to_gate_failures,
)


# ── Payload factories ─────────────────────────────────────────────────────────

def _buy_payload(
    h4="BULLISH", h1_trend="BULLISH",
    h1_break="BOS_UP", h1_ob="BULLISH_OB",
    score=60.0,
    amd_bpr_ote=True,
    of_data=None,
):
    p = {
        "signal": "BUY",
        "smc_h4_direction": h4,
        "smc_h1_trend": h1_trend,
        "smc_h1_break_structure": h1_break,
        "smc_h1_order_block": h1_ob,
        "smc_confluence_score": score,
        "amd_bpr_ote": amd_bpr_ote,
        "smc_h1_liquidity": "EQUAL_LOWS",
        "smc_h1_fvg": "BULLISH_FVG",
        "impulse_score_m5": 70.0,
        "crt_price_zone": "DISCOUNT",
        "session_name": "LONDON",
        "risk_reward": 2.5,
        "entry": 2000.0,
        "volume_engine": {
            "hvn_levels": [1995.0, 2000.5, 2010.0],
            "break_vol_ratio": 2.0,
            "real_break": True,
            "atr": 5.0,
        },
        "cvd_absorption": {
            "bear_absorption": False,
            "bull_absorption": True,
            "pattern": "engulf",
            "valid": True,
        },
    }
    if of_data is not None:
        p["order_flow_snapshot"] = of_data
    return p


def _full_buy_of() -> dict:
    return {"vwap": 1990.0, "cvd_slope": 80.0, "delta": 200.0}


def _sell_payload(**kwargs):
    p = _buy_payload(**kwargs)
    p.update({
        "signal": "SELL",
        "smc_h4_direction": "BEARISH",
        "smc_h1_trend": "BEARISH",
        "smc_h1_break_structure": "BOS_DOWN",
        "smc_h1_order_block": "BEARISH_OB",
        "smc_h1_liquidity": "EQUAL_HIGHS",
        "smc_h1_fvg": "BEARISH_FVG",
        "crt_price_zone": "PREMIUM",
        "entry": 2010.0,
        "order_flow_snapshot": {"vwap": 2020.0, "cvd_slope": -80.0, "delta": -200.0},
        "cvd_absorption": {
            "bear_absorption": True,
            "bull_absorption": False,
            "pattern": "pin",
            "valid": True,
        },
    })
    return p


# ── M1: H4 + H1 bias aligned ──────────────────────────────────────────────────

class TestM1:
    def test_buy_both_bullish_passes(self):
        assert _m1_bias_aligned("BUY", {"smc_h4_direction": "BULLISH", "smc_h1_trend": "BULLISH"}) is True

    def test_buy_h4_bearish_fails(self):
        assert _m1_bias_aligned("BUY", {"smc_h4_direction": "BEARISH", "smc_h1_trend": "BULLISH"}) is False

    def test_buy_h1_bearish_fails(self):
        assert _m1_bias_aligned("BUY", {"smc_h4_direction": "BULLISH", "smc_h1_trend": "BEARISH"}) is False

    def test_sell_both_bearish_passes(self):
        assert _m1_bias_aligned("SELL", {"smc_h4_direction": "BEARISH", "smc_h1_trend": "BEARISH"}) is True

    def test_missing_h4_passes_fail_safe(self):
        assert _m1_bias_aligned("BUY", {"smc_h1_trend": "BULLISH"}) is True

    def test_range_h4_passes_fail_safe(self):
        assert _m1_bias_aligned("BUY", {"smc_h4_direction": "RANGE", "smc_h1_trend": "BULLISH"}) is True

    def test_unknown_both_passes_fail_safe(self):
        assert _m1_bias_aligned("BUY", {}) is True


# ── M2: structure + OB ────────────────────────────────────────────────────────

class TestM2:
    def test_buy_bos_up_bullish_ob_passes(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "BOS_UP", "smc_h1_order_block": "BULLISH_OB"}) is True

    def test_buy_choch_up_bullish_ob_passes(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "CHOCH_UP", "smc_h1_order_block": "BULLISH_OB"}) is True

    def test_buy_bos_down_fails(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "BOS_DOWN", "smc_h1_order_block": "BULLISH_OB"}) is False

    def test_buy_bos_up_bearish_ob_fails(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "BOS_UP", "smc_h1_order_block": "BEARISH_OB"}) is False

    def test_no_structure_passes_fail_safe(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "NONE"}) is True

    def test_missing_structure_passes_fail_safe(self):
        assert _m2_structure_and_ob("BUY", {}) is True

    def test_bos_up_no_ob_passes(self):
        assert _m2_structure_and_ob("BUY", {"smc_h1_break_structure": "BOS_UP"}) is True


# ── M3: entry box ─────────────────────────────────────────────────────────────

class TestM3:
    def test_m2_ok_and_ote_tag_passes(self):
        p = {"smc_h1_break_structure": "BOS_UP", "smc_h1_order_block": "BULLISH_OB", "amd_bpr_ote": True}
        assert _m3_entry_box("BUY", p) is True

    def test_m2_ok_and_high_score_passes(self):
        p = {"smc_h1_break_structure": "BOS_UP", "smc_h1_order_block": "BULLISH_OB", "smc_confluence_score": 55.0}
        assert _m3_entry_box("BUY", p) is True

    def test_m2_fail_blocks_m3(self):
        p = {"smc_h1_break_structure": "BOS_DOWN", "smc_h1_order_block": "BULLISH_OB"}
        assert _m3_entry_box("BUY", p) is False

    def test_m2_ok_low_score_no_ote_fails(self):
        p = {"smc_h1_break_structure": "BOS_UP", "smc_h1_order_block": "BULLISH_OB", "smc_confluence_score": 30.0}
        assert _m3_entry_box("BUY", p) is False


# ── M4: ALL THREE OF signals ──────────────────────────────────────────────────

class TestM4:
    def test_all_three_agree_buy_passes(self):
        p = {"entry": 2001.0, "order_flow_snapshot": {"vwap": 1990.0, "cvd_slope": 80.0, "delta": 100.0}}
        ok, skip = _m4_of_all_agree("BUY", p)
        assert ok is True and skip is False

    def test_all_three_agree_sell_passes(self):
        p = {"entry": 1989.0, "order_flow_snapshot": {"vwap": 2000.0, "cvd_slope": -80.0, "delta": -100.0}}
        ok, skip = _m4_of_all_agree("SELL", p)
        assert ok is True and skip is False

    def test_cvd_wrong_direction_fails(self):
        p = {"entry": 2001.0, "order_flow_snapshot": {"vwap": 1990.0, "cvd_slope": -80.0, "delta": 100.0}}
        ok, skip = _m4_of_all_agree("BUY", p)
        assert ok is False and skip is False

    def test_no_of_data_skip(self):
        ok, skip = _m4_of_all_agree("BUY", {})
        assert ok is False and skip is True

    def test_two_of_three_fails(self):
        # VWAP + CVD agree, delta against
        p = {"entry": 2001.0, "order_flow_snapshot": {"vwap": 1990.0, "cvd_slope": 80.0, "delta": -10.0}}
        ok, skip = _m4_of_all_agree("BUY", p)
        assert ok is False and skip is False

    def test_exactly_at_cvd_threshold_fails(self):
        # cvd_slope == 50 is NOT > 50
        p = {"entry": 2001.0, "order_flow_snapshot": {"vwap": 1990.0, "cvd_slope": 50.0, "delta": 100.0}}
        ok, skip = _m4_of_all_agree("BUY", p)
        assert ok is False

    def test_cvd_just_above_threshold_passes(self):
        p = {"entry": 2001.0, "order_flow_snapshot": {"vwap": 1990.0, "cvd_slope": 50.1, "delta": 100.0}}
        ok, skip = _m4_of_all_agree("BUY", p)
        assert ok is True


# ── Bonus pillar unit tests ───────────────────────────────────────────────────

class TestBonusPillars:
    def test_b1_buy_equal_lows(self):
        assert _b1_liquidity_sweep("BUY", {"smc_h1_liquidity": "EQUAL_LOWS"}) is True
        assert _b1_liquidity_sweep("BUY", {"smc_h1_liquidity": "SELL_SIDE"}) is True
        assert _b1_liquidity_sweep("BUY", {"smc_h1_liquidity": "BUY_SIDE"}) is False

    def test_b1_sell_equal_highs(self):
        assert _b1_liquidity_sweep("SELL", {"smc_h1_liquidity": "EQUAL_HIGHS"}) is True
        assert _b1_liquidity_sweep("SELL", {"smc_h1_liquidity": "BUY_SIDE"}) is True
        assert _b1_liquidity_sweep("SELL", {"smc_h1_liquidity": "SELL_SIDE"}) is False

    def test_b2_fvg_buy(self):
        assert _b2_fvg_aligned("BUY", {"smc_h1_fvg": "BULLISH_FVG"}) is True
        assert _b2_fvg_aligned("BUY", {"smc_h1_fvg": "BEARISH_FVG"}) is False

    def test_b3_divergence_buy(self):
        assert _b3_cvd_divergence("BUY", {"order_flow_snapshot": {"divergence": "bull"}}) is True
        assert _b3_cvd_divergence("BUY", {"order_flow_snapshot": {"divergence": "bear"}}) is False

    def test_b3_no_data_false(self):
        assert _b3_cvd_divergence("BUY", {}) is False

    def test_b4_impulse_high(self):
        assert _b4_impulse_strength({"impulse_score_m5": 60.0}) is True
        assert _b4_impulse_strength({"impulse_score_m5": 59.9}) is False
        assert _b4_impulse_strength({}) is False

    def test_b5_exact_a10_buy_deep_discount(self):
        """A10: entry < rngLo + range*0.382 → deep discount."""
        rng_lo, rng_hi = 1900.0, 2100.0
        rng = rng_hi - rng_lo          # 200
        threshold = rng * 0.382        # 76.4
        deep_entry = rng_lo + threshold - 1.0   # 1975.4 (inside)
        shallow_entry = rng_lo + threshold + 1.0  # 1977.4 (outside)
        assert _b5_deep_zone("BUY", {"entry": deep_entry, "pd_swing_low": rng_lo, "pd_swing_high": rng_hi}) is True
        assert _b5_deep_zone("BUY", {"entry": shallow_entry, "pd_swing_low": rng_lo, "pd_swing_high": rng_hi}) is False

    def test_b5_exact_a10_sell_deep_premium(self):
        """A10: entry > rngHi - range*0.382 → deep premium."""
        rng_lo, rng_hi = 1900.0, 2100.0
        rng = rng_hi - rng_lo
        threshold = rng * 0.382
        deep_entry = rng_hi - threshold + 1.0   # 2024.6 (inside)
        shallow_entry = rng_hi - threshold - 1.0  # 2022.6 (outside)
        assert _b5_deep_zone("SELL", {"entry": deep_entry, "pd_swing_low": rng_lo, "pd_swing_high": rng_hi}) is True
        assert _b5_deep_zone("SELL", {"entry": shallow_entry, "pd_swing_low": rng_lo, "pd_swing_high": rng_hi}) is False

    def test_b5_fallback_crt_zone_without_swing_range(self):
        """Without pd_swing_high/low, falls back to payload field proxies."""
        assert _b5_deep_zone("BUY", {"crt_price_zone": "DISCOUNT"}) is True
        assert _b5_deep_zone("BUY", {"fib_ote_zone": "OTE", "fib_ote_bias": "BULLISH"}) is True
        assert _b5_deep_zone("BUY", {"crt_price_zone": "PREMIUM"}) is False

    def test_b5_discount_buy(self):
        assert _b5_deep_zone("BUY", {"crt_price_zone": "DISCOUNT"}) is True
        assert _b5_deep_zone("BUY", {"fib_ote_zone": "OTE", "fib_ote_bias": "BULLISH"}) is True
        assert _b5_deep_zone("BUY", {"crt_price_zone": "PREMIUM"}) is False

    def test_b5_premium_sell(self):
        assert _b5_deep_zone("SELL", {"crt_price_zone": "PREMIUM"}) is True
        assert _b5_deep_zone("SELL", {"fib_ote_zone": "OTE", "fib_ote_bias": "BEARISH"}) is True

    def test_b6_london_session(self):
        assert _b6_quality_session({"session_name": "LONDON"}) is True
        assert _b6_quality_session({"session_name": "OVERLAP"}) is True
        assert _b6_quality_session({"session_name": "ASIAN"}) is False
        assert _b6_quality_session({}) is False

    def test_b7_rr_ok(self):
        assert _b7_clean_path({"risk_reward": 2.0}) is True
        assert _b7_clean_path({"risk_reward": 1.9}) is False
        assert _b7_clean_path({}) is False

    def test_b8_real_break_hvn_overlap(self):
        ve = {"hvn_levels": [1998.0, 2001.0], "break_vol_ratio": 2.0, "real_break": True, "atr": 5.0}
        assert _b8_volume_confirmed("BUY", {"entry": 2000.0, "volume_engine": ve}) is True

    def test_b8_no_real_break_fails(self):
        ve = {"hvn_levels": [2000.0], "break_vol_ratio": 0.8, "real_break": False, "atr": 5.0}
        assert _b8_volume_confirmed("BUY", {"entry": 2000.0, "volume_engine": ve}) is False

    def test_b8_no_volume_engine_false(self):
        assert _b8_volume_confirmed("BUY", {}) is False

    def test_b9_bull_absorption_buy(self):
        ctx = {"bear_absorption": False, "bull_absorption": True, "pattern": "engulf", "valid": True}
        assert _b9_absorption_confirmed("BUY", {"cvd_absorption": ctx}) is True

    def test_b9_invalid_absorption_false(self):
        ctx = {"bear_absorption": False, "bull_absorption": True, "pattern": None, "valid": False}
        assert _b9_absorption_confirmed("BUY", {"cvd_absorption": ctx}) is False


# ── evaluate_setup_quality ────────────────────────────────────────────────────

class TestEvaluateSetupQuality:
    def _full_buy_payload(self) -> dict:
        p = _buy_payload(of_data=_full_buy_of())
        p["order_flow_snapshot"] = _full_buy_of()
        return p

    def test_a_plus_with_5_bonuses_and_b1(self):
        p = self._full_buy_payload()
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert result["mandatory_ok"] is True
        # Should be A or A+ depending on total bonus count
        assert result["tier"] in {"A+", "A"}
        assert result["bonus_count"] >= 4

    def test_reject_when_m1_fails(self):
        p = self._full_buy_payload()
        p["smc_h4_direction"] = "BEARISH"  # H4 against BUY
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert result["tier"] == "REJECT"
        assert "M1_HTF_BIAS_NOT_ALIGNED" in result["failed_mandatory"]

    def test_reject_when_m2_fails(self):
        p = self._full_buy_payload()
        p["smc_h1_break_structure"] = "BOS_DOWN"  # structure against BUY
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert result["tier"] == "REJECT"
        assert "M2_STRUCTURE_OB_UNCONFIRMED" in result["failed_mandatory"]

    def test_reject_when_m4_fails(self):
        p = self._full_buy_payload()
        p["order_flow_snapshot"] = {"vwap": 2010.0, "cvd_slope": -80.0, "delta": -100.0}  # all against BUY
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert "M4_OF_SIGNALS_INCOMPLETE" in result["failed_mandatory"]

    def test_reject_when_too_few_bonuses(self):
        p = {
            "signal": "BUY",
            "smc_h4_direction": "BULLISH",
            "smc_h1_trend": "BULLISH",
            "smc_h1_break_structure": "BOS_UP",
            "smc_h1_order_block": "BULLISH_OB",
            "smc_confluence_score": 55.0,
            "amd_bpr_ote": True,
            # All bonuses missing
        }
        result = evaluate_setup_quality("AMD_FVG_IFVG_REVERSAL", "BUY", p)
        assert result["tier"] == "REJECT"
        assert result["bonus_count"] < 3

    def test_b_plus_with_exactly_3_bonuses(self):
        p = {
            "signal": "BUY",
            "smc_h4_direction": "BULLISH",
            "smc_h1_trend": "BULLISH",
            "smc_h1_break_structure": "BOS_UP",
            "smc_h1_order_block": "BULLISH_OB",
            "smc_confluence_score": 55.0,
            "amd_bpr_ote": True,
            "smc_h1_liquidity": "EQUAL_LOWS",  # B1
            "smc_h1_fvg": "BULLISH_FVG",        # B2
            "impulse_score_m5": 65.0,            # B4
        }
        result = evaluate_setup_quality("AMD_FVG_IFVG_REVERSAL", "BUY", p)
        assert result["tier"] == "B+"
        assert result["bonus_count"] == 3

    def test_skip_strategy_returns_a_plus(self):
        result = evaluate_setup_quality("BTC_SCALPING_AGENT", "BUY", {})
        assert result["tier"] == "A+"
        assert result["mandatory_ok"] is True

    def test_invalid_direction_skip(self):
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "WAIT", {})
        assert result["tier"] == "A+"

    def test_sell_tier_evaluation(self):
        p = _sell_payload()
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "SELL", p)
        assert result["mandatory_ok"] is True

    def test_result_keys_present(self):
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", self._full_buy_payload())
        for key in ("tier", "mandatory_ok", "failed_mandatory", "bonus_count", "bonus_passed", "reason"):
            assert key in result

    def test_bonus_passed_has_all_b_pillars(self):
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", self._full_buy_payload())
        for b in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"):
            assert b in result["bonus_passed"]


# ── tier_to_gate_failures ─────────────────────────────────────────────────────

class TestTierToGateFailures:
    def test_a_plus_no_failure(self):
        assert tier_to_gate_failures({"tier": "A+"}) == []

    def test_a_no_failure(self):
        assert tier_to_gate_failures({"tier": "A"}) == []

    def test_b_plus_no_failure(self):
        assert tier_to_gate_failures({"tier": "B+"}) == []

    def test_reject_returns_failure(self):
        failures = tier_to_gate_failures({"tier": "REJECT"})
        assert failures == ["SETUP_TIER_REJECT"]

    def test_missing_tier_defaults_to_reject(self):
        failures = tier_to_gate_failures({})
        assert failures == ["SETUP_TIER_REJECT"]


# ── Integration: setup_hunter wiring ─────────────────────────────────────────

class TestSetupHunterTierIntegration:
    """Tests that the tier check wires correctly through setup_hunter."""

    def test_flag_false_no_tier_block(self):
        from unittest.mock import MagicMock, patch
        from app.agents.setup_hunter import SetupHunter

        settings = MagicMock()
        settings.hermes_entry_gates_enabled = False
        settings.hermes_setup_tier_enabled = False

        hunter = SetupHunter(settings)
        payload = _buy_payload()
        payload.update({
            "signal": "BUY",
            "spread": 0.5,
            "big_setup_grade": "A",
            "setup_score": 90,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
        })
        from app.agents import setup_hunter
        failed = setup_hunter._failed_gates("ENTRY", payload, 0.5, 5.0, settings)
        assert "SETUP_TIER_REJECT" not in failed

    def test_flag_true_reject_blocks(self):
        from unittest.mock import MagicMock
        from app.agents import setup_hunter

        settings = MagicMock()
        settings.hermes_entry_gates_enabled = False
        settings.hermes_setup_tier_enabled = True  # actual True, not Mock

        # Payload designed to fail M1 (H4 against BUY)
        payload = _buy_payload()
        payload.update({
            "signal": "BUY",
            "spread": 0.5,
            "big_setup_grade": "A",
            "setup_score": 90,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
            "smc_h4_direction": "BEARISH",  # M1 fail
        })
        failed = setup_hunter._failed_gates("ENTRY", payload, 0.5, 5.0, settings)
        assert "SETUP_TIER_REJECT" in failed

    def test_flag_mock_does_not_trigger(self):
        """MagicMock attribute is not True → tier must not activate."""
        from unittest.mock import MagicMock
        from app.agents import setup_hunter

        settings = MagicMock()  # all attributes are Mock objects, not True
        payload = _buy_payload()
        payload.update({
            "signal": "BUY",
            "spread": 0.5,
            "big_setup_grade": "A",
            "setup_score": 90,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
            "smc_h4_direction": "BEARISH",  # would trigger reject if tier ran
        })
        failed = setup_hunter._failed_gates("ENTRY", payload, 0.5, 5.0, settings)
        assert "SETUP_TIER_REJECT" not in failed


# ── v1.4: ORDER_FLOW_NATIVE mandatory pillar helpers ──────────────────────────

class TestOM1NotOpposing:
    def test_buy_range_h4_passes(self):
        assert _om1_not_opposing("BUY", {"smc_h4_direction": "RANGE", "smc_h1_trend": "BULLISH"})

    def test_buy_unknown_passes(self):
        assert _om1_not_opposing("BUY", {})

    def test_buy_bearish_h4_fails(self):
        assert not _om1_not_opposing("BUY", {"smc_h4_direction": "BEARISH", "smc_h1_trend": "RANGE"})

    def test_buy_bearish_h1_fails(self):
        assert not _om1_not_opposing("BUY", {"smc_h4_direction": "RANGE", "smc_h1_trend": "BEARISH"})

    def test_buy_both_bullish_passes(self):
        assert _om1_not_opposing("BUY", {"smc_h4_direction": "BULLISH", "smc_h1_trend": "BULLISH"})

    def test_sell_bullish_h4_fails(self):
        assert not _om1_not_opposing("SELL", {"smc_h4_direction": "BULLISH", "smc_h1_trend": "RANGE"})

    def test_sell_bearish_h1_passes(self):
        assert _om1_not_opposing("SELL", {"smc_h4_direction": "RANGE", "smc_h1_trend": "BEARISH"})


class TestOM2NearKeyLevel:
    def _ve(self, atr=5.0, hvn=None):
        return {"atr": atr, "hvn_levels": hvn or []}

    def test_no_entry_passes_failsafe(self):
        assert _om2_near_key_level("BUY", {})

    def test_no_atr_passes_failsafe(self):
        assert _om2_near_key_level("BUY", {"entry": 2000.0, "volume_engine": {"atr": None}})

    def test_near_vwap_passes(self):
        payload = {
            "entry": 2000.0,
            "volume_engine": self._ve(atr=5.0),
            "order_flow_snapshot": {"vwap": 2001.0, "cvd_slope": 60.0, "delta": 100.0},
        }
        # 2001 - 2000 = 1.0 <= 0.25 * 5.0 = 1.25
        assert _om2_near_key_level("BUY", payload)

    def test_far_from_all_levels_fails(self):
        payload = {
            "entry": 2000.0,
            "volume_engine": self._ve(atr=5.0, hvn=[1970.0, 2030.0]),
            "order_flow_snapshot": {"vwap": 1990.0},
        }
        # 2000 - 1990 = 10 > 1.25; HVNs 30 away
        assert not _om2_near_key_level("BUY", payload)

    def test_near_hvn_passes(self):
        payload = {
            "entry": 2000.0,
            "volume_engine": self._ve(atr=5.0, hvn=[1999.8]),
        }
        # 0.2 <= 1.25
        assert _om2_near_key_level("BUY", payload)

    def test_b1_sweep_counts_as_key_level(self):
        payload = {
            "entry": 2000.0,
            "volume_engine": self._ve(atr=5.0),
            "smc_h1_liquidity": "EQUAL_LOWS",  # triggers B1 for BUY
        }
        assert _om2_near_key_level("BUY", payload)

    def test_near_val_passes(self):
        payload = {
            "entry": 2000.0,
            "volume_engine": self._ve(atr=5.0),
            "order_flow_snapshot": {"vwap": 1950.0, "val": 2000.5},
        }
        assert _om2_near_key_level("BUY", payload)


class TestOM4RealParticipation:
    def test_real_break_passes(self):
        payload = {"volume_engine": {"real_break": True, "hvn_levels": [], "atr": 5.0}}
        assert _om4_real_participation("BUY", payload)

    def test_no_real_break_no_absorption_fails(self):
        payload = {"volume_engine": {"real_break": False}}
        assert not _om4_real_participation("BUY", payload)

    def test_absorption_passes(self):
        payload = {
            "volume_engine": {"real_break": False},
            "cvd_absorption": {"bull_absorption": True, "bear_absorption": False,
                               "pattern": "engulf", "valid": True},
        }
        assert _om4_real_participation("BUY", payload)

    def test_invalid_absorption_fails(self):
        payload = {
            "volume_engine": {"real_break": False},
            "cvd_absorption": {"bull_absorption": True, "bear_absorption": False,
                               "pattern": None, "valid": False},
        }
        assert not _om4_real_participation("BUY", payload)


# ── v1.4: evaluate_setup_quality for ORDER_FLOW_NATIVE path ──────────────────

def _of_buy_payload():
    """Rich OF payload that satisfies OM1-OM4 + most bonuses."""
    return {
        "signal": "BUY",
        "entry": 2000.0,
        "smc_h4_direction": "BULLISH",
        "smc_h1_trend": "RANGE",       # OM1: not opposing
        "smc_h1_liquidity": "EQUAL_LOWS",  # B1 (sweep) + OM2 (swept level)
        "smc_h1_fvg": "BULLISH_FVG",
        "impulse_score_m5": 65.0,
        "crt_price_zone": "DISCOUNT",
        "session_name": "LONDON",
        "risk_reward": 2.5,
        "order_flow_snapshot": {
            "vwap": 1999.0,    # OM2: 1.0 <= 0.25*5=1.25; OM3: price>vwap
            "cvd_slope": 80.0,
            "delta": 150.0,
        },
        "volume_engine": {
            "hvn_levels": [1998.0, 2001.0],
            "break_vol_ratio": 2.0,
            "real_break": True,      # OM4
            "atr": 5.0,
        },
        "cvd_absorption": {
            "bull_absorption": True, "bear_absorption": False,
            "pattern": "engulf", "valid": True,
        },
        "pd_swing_high": 2020.0,
        "pd_swing_low": 1980.0,
    }


class TestEvaluateSetupQualityOFNative:
    def test_of_native_all_om_pass_gets_tier(self):
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", _of_buy_payload())
        assert result["mandatory_ok"] is True, f"Mandatory failed: {result['failed_mandatory']}"
        assert result["tier"] != "REJECT", f"Unexpected REJECT: {result['reason']}"

    def test_of_native_om1_fail_when_bearish_h4(self):
        p = _of_buy_payload()
        p["smc_h4_direction"] = "BEARISH"
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", p)
        assert "OM1_HTF_AGAINST_DIRECTION" in result["failed_mandatory"]
        assert result["tier"] == "REJECT"

    def test_of_native_om3_fail_when_of_disagrees(self):
        p = _of_buy_payload()
        p["order_flow_snapshot"] = {"vwap": 2010.0, "cvd_slope": -80.0, "delta": -100.0}
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", p)
        assert "OM3_OF_SIGNALS_INCOMPLETE" in result["failed_mandatory"]

    def test_of_native_om4_fail_when_no_participation(self):
        p = _of_buy_payload()
        p["volume_engine"] = {"real_break": False, "hvn_levels": [], "atr": 5.0}
        p["cvd_absorption"] = {"bull_absorption": False, "bear_absorption": False,
                               "pattern": None, "valid": False}
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", p)
        assert "OM4_NO_REAL_PARTICIPATION" in result["failed_mandatory"]

    def test_of_native_m2_m3_not_in_mandatory_set(self):
        """ORDER_FLOW_NATIVE must NOT check M2/M3 (no OB/OTE requirements)."""
        p = _of_buy_payload()
        p["smc_h1_break_structure"] = "BOS_DOWN"  # would fail M2 for SMC_NATIVE
        p["smc_h1_order_block"] = "BEARISH_OB"
        p["amd_bpr_ote"] = False
        p["smc_confluence_score"] = 20.0
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", p)
        # M2/M3 must NOT appear in failed_mandatory
        assert "M2_STRUCTURE_OB_UNCONFIRMED" not in result["failed_mandatory"]
        assert "M3_ENTRY_BOX_EMPTY" not in result["failed_mandatory"]

    def test_of_native_a_plus_anchor_b9_not_only_b1(self):
        """A+ can be anchored by B9 (absorption) as well as B1 (sweep)."""
        p = _of_buy_payload()
        p["smc_h1_liquidity"] = "NONE"  # no B1 sweep
        # Keep B9 absorption valid
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", p)
        if result["bonus_count"] >= 5 and result["bonus_passed"].get("B9"):
            assert result["tier"] == "A+"

    def test_gold_liquidity_hunter_uses_of_pillars(self):
        result = evaluate_setup_quality("GOLD_LIQUIDITY_HUNTER_PRO", "BUY", _of_buy_payload())
        assert result["mandatory_ok"] is True

    def test_gold_cvd_vwap_uses_of_pillars(self):
        result = evaluate_setup_quality("GOLD_ORDER_FLOW_CVD_VWAP", "BUY", _of_buy_payload())
        assert result["mandatory_ok"] is True


class TestEvaluateSetupQualitySMCNative:
    def test_smc_native_uses_m1_m4(self):
        """SMC_NATIVE must check M1-M4 (same as before v1.4)."""
        p = _buy_payload()  # full SMC payload
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert result["mandatory_ok"] is True

    def test_smc_native_fails_m1(self):
        p = _buy_payload(h4="BEARISH")
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        assert "M1_HTF_BIAS_NOT_ALIGNED" in result["failed_mandatory"]

    def test_smc_native_fails_m2(self):
        p = _buy_payload(h1_break="BOS_DOWN")
        result = evaluate_setup_quality("FIB_CONFLUENCE_EXECUTION_AGENT", "BUY", p)
        assert "M2_STRUCTURE_OB_UNCONFIRMED" in result["failed_mandatory"]

    def test_smc_native_no_om1_om4_labels(self):
        """SMC_NATIVE must not produce OM-prefixed failure labels."""
        p = _buy_payload(h4="BEARISH")
        result = evaluate_setup_quality("SIMO_ATM_BREAKOUT", "BUY", p)
        for label in result["failed_mandatory"]:
            assert not label.startswith("OM"), f"SMC_NATIVE must not emit OM labels: {label}"


class TestEvaluateSetupQualityDefault:
    def test_default_only_checks_m1_m4(self):
        """Non-SMC_NATIVE, non-OF strategy: only M1 + M4 are checked."""
        p = _buy_payload()
        # Break M2 signal (should not affect DEFAULT)
        p["smc_h1_break_structure"] = "BOS_DOWN"
        result = evaluate_setup_quality("TREND_CONTINUATION_BREAKDOWN", "BUY", p)
        assert "M2_STRUCTURE_OB_UNCONFIRMED" not in result["failed_mandatory"]

    def test_default_fails_m1(self):
        p = _buy_payload(h4="BEARISH")
        result = evaluate_setup_quality("TREND_CONTINUATION_BREAKDOWN", "BUY", p)
        assert "M1_HTF_BIAS_NOT_ALIGNED" in result["failed_mandatory"]

    def test_default_m3_not_checked(self):
        p = _buy_payload()
        p["amd_bpr_ote"] = False
        p["smc_confluence_score"] = 10.0
        result = evaluate_setup_quality("TREND_CONTINUATION_BREAKDOWN", "BUY", p)
        assert "M3_ENTRY_BOX_EMPTY" not in result["failed_mandatory"]


# ── v1.4 Part 3 acceptance tests ─────────────────────────────────────────────

class TestAcceptanceV14:
    """Acceptance: GOLD VWAP_RECLAIM_REJECTION passes tier; EUR mid-range rejects."""

    def test_gold_vwap_reclaim_rejection_passes_tier(self):
        """GOLD ORDER_FLOW_EXECUTION_AGENT with strong OF at VWAP level → passes mandatory."""
        payload = {
            "signal": "SELL",
            "entry": 2350.0,
            "smc_h4_direction": "RANGE",   # OM1: not opposing (RANGE = pass)
            "smc_h1_trend": "RANGE",
            "smc_h1_liquidity": "EQUAL_HIGHS",  # B1 + OM2 (swept sell-side = key level)
            "smc_h1_fvg": "BEARISH_FVG",
            "impulse_score_m5": 70.0,
            "session_name": "LONDON",
            "risk_reward": 2.2,
            "order_flow_snapshot": {
                "vwap": 2350.5,   # OM2: within 0.25*5=1.25; OM3: price<vwap for SELL
                "cvd_slope": -90.0,
                "delta": -300.0,
            },
            "volume_engine": {
                "hvn_levels": [2349.0, 2352.0],
                "break_vol_ratio": 1.8,
                "real_break": True,  # OM4
                "atr": 5.0,
            },
            "cvd_absorption": {
                "bear_absorption": True, "bull_absorption": False,
                "pattern": "pin", "valid": True,
            },
            "pd_swing_high": 2380.0,
            "pd_swing_low": 2310.0,
            "crt_price_zone": "PREMIUM",
        }
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "SELL", payload)
        assert result["mandatory_ok"] is True, (
            f"GOLD VWAP_RECLAIM_REJECTION must pass mandatory pillars. "
            f"Failed: {result['failed_mandatory']}"
        )
        assert result["tier"] != "REJECT", f"Must not REJECT: {result['reason']}"
        gate_fails = tier_to_gate_failures(result)
        assert gate_fails == [], f"Must reach order layer, got gate failures: {gate_fails}"

    def test_eur_mid_range_h1_range_rejects(self):
        """EUR mid-range setup: H1=RANGE, no key level, no OF agreement → OM rejects."""
        payload = {
            "signal": "BUY",
            "entry": 1.0850,
            "smc_h4_direction": "RANGE",
            "smc_h1_trend": "RANGE",
            "smc_h1_liquidity": "NONE",  # no swept level
            "order_flow_snapshot": {
                "vwap": 1.0900,   # price far from VWAP: |1.0850 - 1.0900| = 50 pips
                "cvd_slope": -20.0,  # negative for a BUY signal → OM3 fail
                "delta": -50.0,
            },
            "volume_engine": {
                "hvn_levels": [1.0700, 1.1000],  # far from entry
                "real_break": False,   # OM4 fail
                "atr": 0.0010,         # 10 pip ATR; tol = 0.25 * 0.001 = 0.00025 = 2.5 pips
            },
            "cvd_absorption": {"valid": False},
            "session_name": "ASIAN",
            "risk_reward": 1.2,
        }
        result = evaluate_setup_quality("ORDER_FLOW_EXECUTION_AGENT", "BUY", payload)
        # Must fail OM2 (not near key level: 50 pips > 2.5 pip tol) AND/OR OM3/OM4
        assert result["tier"] == "REJECT", (
            f"EUR mid-range must REJECT. Tier={result['tier']}, "
            f"Failed={result['failed_mandatory']}, Bonus={result['bonus_count']}"
        )
