"""Phase 3A: SMC/MTFA confirmation calibration tests.

Tests cover:
- confirmation_matrix.py: calibrated status thresholds and hard_block logic
- setup_hunter.py: SOFT_FAIL does not block; STRONG_FAIL blocks only weak setups
- confluence_engine.py: PASS/SOFT_FAIL/STRONG_FAIL adjustments
- Safety invariants: order_send location, live trading, max lot

Strategy used for SetupHunter integration tests: EUR_EMA_RSI_ATR_CROSSOVER on
EURUSD — this is an ACTIVE_EXECUTION strategy that is NOT in the
strategy_ready_without_confluence bypass set, so it goes through the
confirmation matrix evaluation path.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

import pandas as pd

from app.agents.confirmation_matrix import (
    evaluate as cm_evaluate,
    mtfa_calibrated_status,
    mtfa_confluence_adjustment,
    smc_calibrated_status,
    smc_confluence_adjustment,
)
from app.agents.confluence_engine import ConfluenceEngine
from app.agents.setup_hunter import SetupHunter
from app.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _demo_settings(**overrides) -> Settings:
    base = {
        "paper_trading": False,
        "demo_trading": True,
        "allow_live_trading": False,
        "demo_only": True,
        "demo_max_lot": 0.01,
        "demo_max_open_trades": 1,
        "demo_max_trades_per_day": 5,
        "demo_max_trades_per_day_total": 15,
        "demo_max_trades_per_symbol_per_day": 5,
        "demo_magic_number": 909002,
        "demo_comment": "HERMES_DEMO_KELLY_24H",
        "safety_guard_enabled": True,
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
        "demo_ignore_all_time_blocks": False,
        "demo_ignore_session_blocks": False,
        "demo_ignore_bad_hour_blocks": False,
        "demo_ignore_duration_blocks": False,
        "demo_ignore_setup_wait_hours": False,
        "max_money_tp_enabled": False,
        "ema_pullback_require_extra_confirmation": False,
        "risk_diag_max_realized_risk_percent": 999.0,
        "risk_diag_max_mismatch_abs_percent": 999.0,
        "eur_ema_rsi_atr_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _time_gate() -> dict:
    return {
        "session_name": "LONDON",
        "time_gate_status": "PASS",
        "time_gate_reason": "TIME_GATE_PASS",
        "symbol_market_open": True,
        "market_open": True,
        "is_bad_hour": False,
    }


def _eur_signal(
    confidence: float = 0.90,
    signal: str = "BUY",
    m15_confirmation: bool = True,
    m1_entry_confirmation: bool = True,
    risk_reward: float = 2.0,
    smc_confluence_score: float = 80.0,
    smc_confluence_status: str = "PASS",
    mtfa_score: float = 80.0,
    mtfa_status: str = "PASS",
    **extra,
) -> dict:
    """EUR_EMA_RSI_ATR_CROSSOVER signal on EURUSD.

    This is an ACTIVE_EXECUTION strategy not in strategy_ready_without_confluence,
    so the confirmation matrix is always evaluated.
    setup_score = round(confidence * 100) via _setup_score("EUR_EMA_RSI_ATR_CROSSOVER").

    BigSetupDetector scoring with these fields:
      htf_alignment=20, poi=15, confirmation=10, risk=10, time=10 → 65 → grade "B"
    This ensures grade_ok=True so BIG_SETUP_GRADE_BELOW_B is not added.
    """
    htf_direction = "BULLISH" if signal == "BUY" else "BEARISH"
    zone_type = "DEMAND" if signal == "BUY" else "SUPPLY"
    return {
        "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
        "signal": signal,
        "confidence": confidence,
        "risk_reward": risk_reward,
        "m15_confirmation": m15_confirmation,
        "m15_confirmation_status": "PASS" if m15_confirmation else "FAIL",
        "m1_entry_confirmation": m1_entry_confirmation,
        "m1_trigger_status": "PASS" if m1_entry_confirmation else "FAIL",
        "smc_confluence_score": smc_confluence_score,
        "smc_confluence_status": smc_confluence_status,
        "mtfa_score": mtfa_score,
        "mtfa_status": mtfa_status,
        "safety_guard_status": "PASS",
        "entry": 1.10,
        "sl": 1.09,
        "tp": 1.12,
        # BigSetupDetector HTF alignment fields to reach grade "B" (score >= 60)
        "smc_h4_direction": htf_direction,
        "smc_h1_trend": htf_direction,
        "smc_h4_key_level_nearby": True,
        "smc_h4_supply_demand_zone": zone_type,
        **extra,
    }


def _eur_analysis(signal: dict) -> dict:
    return {
        "ai_decision": {
            "symbol": "EURUSD",
            "signal": signal.get("signal", "WAIT"),
            "risk_reward": signal.get("risk_reward", 2.0),
            "smc_confluence_status": signal.get("smc_confluence_status", "PASS"),
            "smc_confluence_score": signal.get("smc_confluence_score", 80),
            "mtfa_status": signal.get("mtfa_status", "PASS"),
            "mtfa_score": signal.get("mtfa_score", 80),
            "m15_confirmation": signal.get("m15_confirmation", True),
            "m1_entry_confirmation": signal.get("m1_entry_confirmation", True),
            "safety_guard_status": signal.get("safety_guard_status", "PASS"),
        },
        "strategy_signals": [signal],
    }


def _flat_frames(n: int = 25, price: float = 1.10) -> dict:
    df = pd.DataFrame({
        "open": [price] * n,
        "high": [price + 0.001] * n,
        "low": [price - 0.001] * n,
        "close": [price] * n,
    })
    return {"M5": df}


# ---------------------------------------------------------------------------
# 1. Confirmation matrix — calibrated status thresholds
# ---------------------------------------------------------------------------

class TestConfirmationMatrixThresholds(unittest.TestCase):

    # --- SMC thresholds ---
    def test_confirmation_smc_pass_at_70(self) -> None:
        self.assertEqual(smc_calibrated_status(70), "PASS")

    def test_confirmation_smc_pass_at_100(self) -> None:
        self.assertEqual(smc_calibrated_status(100), "PASS")

    def test_confirmation_smc_soft_fail_at_69(self) -> None:
        self.assertEqual(smc_calibrated_status(69), "SOFT_FAIL")

    def test_confirmation_smc_soft_fail_at_40(self) -> None:
        self.assertEqual(smc_calibrated_status(40), "SOFT_FAIL")

    def test_confirmation_smc_strong_fail_at_39(self) -> None:
        self.assertEqual(smc_calibrated_status(39), "STRONG_FAIL")

    def test_confirmation_smc_strong_fail_at_0(self) -> None:
        self.assertEqual(smc_calibrated_status(0), "STRONG_FAIL")

    # --- MTFA thresholds ---
    def test_confirmation_mtfa_pass_at_60(self) -> None:
        self.assertEqual(mtfa_calibrated_status(60), "PASS")

    def test_confirmation_mtfa_pass_at_100(self) -> None:
        self.assertEqual(mtfa_calibrated_status(100), "PASS")

    def test_confirmation_mtfa_soft_fail_at_59(self) -> None:
        self.assertEqual(mtfa_calibrated_status(59), "SOFT_FAIL")

    def test_confirmation_mtfa_soft_fail_at_35(self) -> None:
        self.assertEqual(mtfa_calibrated_status(35), "SOFT_FAIL")

    def test_confirmation_mtfa_strong_fail_at_34(self) -> None:
        self.assertEqual(mtfa_calibrated_status(34), "STRONG_FAIL")

    def test_confirmation_mtfa_strong_fail_at_0(self) -> None:
        self.assertEqual(mtfa_calibrated_status(0), "STRONG_FAIL")

    # --- Confluence adjustments ---
    def test_confirmation_smc_pass_adjustment_is_plus_10(self) -> None:
        self.assertEqual(smc_confluence_adjustment("PASS"), 10.0)

    def test_confirmation_smc_soft_fail_adjustment_is_minus_5(self) -> None:
        self.assertEqual(smc_confluence_adjustment("SOFT_FAIL"), -5.0)

    def test_confirmation_smc_strong_fail_adjustment_is_minus_15(self) -> None:
        self.assertEqual(smc_confluence_adjustment("STRONG_FAIL"), -15.0)

    def test_confirmation_mtfa_pass_adjustment_is_plus_10(self) -> None:
        self.assertEqual(mtfa_confluence_adjustment("PASS"), 10.0)

    def test_confirmation_mtfa_soft_fail_adjustment_is_minus_5(self) -> None:
        self.assertEqual(mtfa_confluence_adjustment("SOFT_FAIL"), -5.0)

    def test_confirmation_mtfa_strong_fail_adjustment_is_minus_15(self) -> None:
        self.assertEqual(mtfa_confluence_adjustment("STRONG_FAIL"), -15.0)


# ---------------------------------------------------------------------------
# 2. Confirmation matrix — hard_block logic
# ---------------------------------------------------------------------------

class TestConfirmationMatrixHardBlock(unittest.TestCase):

    def _eval(self, smc: float, mtfa: float, setup: float, rr: float | None) -> dict:
        return cm_evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", smc, mtfa, setup, rr)

    # SOFT_FAIL never hard-blocks
    def test_confirmation_smc_soft_fail_does_not_hard_block(self) -> None:
        result = self._eval(smc=55, mtfa=80, setup=60, rr=2.0)
        self.assertFalse(result["hard_block"])
        self.assertEqual(result["smc_calibrated_status"], "SOFT_FAIL")

    def test_confirmation_mtfa_soft_fail_does_not_hard_block(self) -> None:
        result = self._eval(smc=80, mtfa=50, setup=60, rr=2.0)
        self.assertFalse(result["hard_block"])
        self.assertEqual(result["mtfa_calibrated_status"], "SOFT_FAIL")

    def test_confirmation_both_soft_fail_do_not_hard_block(self) -> None:
        result = self._eval(smc=55, mtfa=50, setup=60, rr=2.0)
        self.assertFalse(result["hard_block"])

    # STRONG_FAIL + low confluence (setup < 75) → hard block
    def test_confirmation_strong_fail_blocks_when_low_confluence(self) -> None:
        result = self._eval(smc=20, mtfa=20, setup=60, rr=2.0)
        self.assertTrue(result["hard_block"])

    # STRONG_FAIL + low RR → hard block
    def test_confirmation_strong_fail_blocks_when_low_rr(self) -> None:
        result = self._eval(smc=20, mtfa=20, setup=80, rr=1.2)
        self.assertTrue(result["hard_block"])

    def test_confirmation_strong_fail_blocks_when_rr_none(self) -> None:
        result = self._eval(smc=20, mtfa=20, setup=80, rr=None)
        self.assertTrue(result["hard_block"])

    # STRONG_FAIL + strong setup (>= 75) + RR >= 1.5 → NOT a hard block
    def test_confirmation_strong_fail_does_not_block_strong_setup(self) -> None:
        result = self._eval(smc=20, mtfa=20, setup=80, rr=2.0)
        self.assertFalse(result["hard_block"])

    def test_confirmation_strong_fail_does_not_block_at_boundary_setup_75_rr_1_5(self) -> None:
        result = self._eval(smc=0, mtfa=0, setup=75, rr=1.5)
        self.assertFalse(result["hard_block"])

    # PASS for both → no hard block even with weak setup
    def test_confirmation_both_pass_never_hard_block(self) -> None:
        result = self._eval(smc=80, mtfa=70, setup=30, rr=1.0)
        self.assertFalse(result["hard_block"])

    # Return values
    def test_confirmation_returns_smc_calibrated_status(self) -> None:
        result = cm_evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", 55, 80, 90, 2.0)
        self.assertEqual(result["smc_calibrated_status"], "SOFT_FAIL")

    def test_confirmation_returns_mtfa_calibrated_status(self) -> None:
        result = cm_evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", 80, 50, 90, 2.0)
        self.assertEqual(result["mtfa_calibrated_status"], "SOFT_FAIL")


# ---------------------------------------------------------------------------
# 3. SetupHunter routing with confirmation matrix (EUR_EMA_RSI_ATR_CROSSOVER)
#
# EUR_EMA_RSI_ATR_CROSSOVER on EURUSD is an ACTIVE_EXECUTION strategy that
# is NOT in strategy_ready_without_confluence, so the confirmation matrix is
# evaluated on every candidate.
# ---------------------------------------------------------------------------

class TestSetupHunterConfirmationRouting(unittest.TestCase):

    def setUp(self) -> None:
        self.hunter = SetupHunter(_demo_settings())
        self.tg = _time_gate()

    def _run(self, signal: dict) -> dict:
        return self.hunter.evaluate(
            "EURUSD", "EURUSD", _eur_analysis(signal), self.tg, 1, 50
        ).best_candidate

    # ------------------------------------------------------------------
    # SMC SOFT_FAIL does not hard-block a strong candidate
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_smc_soft_fail_routes_strong_candidate(self) -> None:
        signal = _eur_signal(
            confidence=0.90,          # setup_score=90 >= 75
            smc_confluence_score=55,  # SOFT_FAIL (40-69)
            smc_confluence_status="FAIL",
            mtfa_score=80,
            mtfa_status="PASS",
            risk_reward=2.0,
        )
        best = self._run(signal)
        self.assertTrue(best["demo_eligible"],
                        msg=f"failed_gates={best.get('failed_gates')}")
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))

    # ------------------------------------------------------------------
    # MTFA SOFT_FAIL does not hard-block a strong candidate
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_mtfa_soft_fail_routes_strong_candidate(self) -> None:
        signal = _eur_signal(
            confidence=0.90,
            smc_confluence_score=80,
            smc_confluence_status="PASS",
            mtfa_score=50,            # SOFT_FAIL (35-59)
            mtfa_status="FAIL",
            risk_reward=2.0,
        )
        best = self._run(signal)
        self.assertTrue(best["demo_eligible"],
                        msg=f"failed_gates={best.get('failed_gates')}")
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))

    # ------------------------------------------------------------------
    # Both SOFT_FAIL → routes with strong setup + RR >= 1.5
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_both_soft_fail_routes_valid_candidate(self) -> None:
        signal = _eur_signal(
            confidence=0.90,
            smc_confluence_score=55,  # SOFT_FAIL
            smc_confluence_status="FAIL",
            mtfa_score=50,            # SOFT_FAIL
            mtfa_status="FAIL",
            risk_reward=2.0,
        )
        best = self._run(signal)
        self.assertTrue(best["demo_eligible"],
                        msg=f"failed_gates={best.get('failed_gates')}")
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))

    # ------------------------------------------------------------------
    # Valid candidate (score>=80, RR>=1.5, m15/m1 OK) routes with SOFT_FAIL
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_valid_candidate_routes_with_smc_mtfa_soft_fail(self) -> None:
        signal = _eur_signal(
            confidence=0.90,          # setup_score=90
            m15_confirmation=True,
            m1_entry_confirmation=True,
            risk_reward=2.0,
            smc_confluence_score=55,  # SOFT_FAIL
            smc_confluence_status="FAIL",
            mtfa_score=50,            # SOFT_FAIL
            mtfa_status="FAIL",
        )
        best = self._run(signal)
        self.assertTrue(best["demo_eligible"],
                        msg=f"failed_gates={best.get('failed_gates')}")
        self.assertEqual(best["direction"], "BUY")
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))

    # ------------------------------------------------------------------
    # STRONG_FAIL blocks when setup_score < 75
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_strong_fail_blocks_weak_candidate(self) -> None:
        signal = _eur_signal(
            confidence=0.65,          # setup_score=65 < 75
            smc_confluence_score=20,  # STRONG_FAIL
            smc_confluence_status="FAIL",
            mtfa_score=20,            # STRONG_FAIL
            mtfa_status="FAIL",
            risk_reward=2.0,
        )
        result = self.hunter.evaluate(
            "EURUSD", "EURUSD", _eur_analysis(signal), self.tg, 1, 50
        )
        # The best candidate will be NONE (EUR eur_symbol path) or the blocked candidate
        # Check via candidates list that CONFIRMATION_MATRIX_HARD_BLOCK is present
        candidates = result.candidates
        eur_candidate = next(
            (c for c in candidates if c.get("best_strategy") == "EUR_EMA_RSI_ATR_CROSSOVER"),
            None,
        )
        self.assertIsNotNone(eur_candidate, msg="EUR_EMA_RSI_ATR_CROSSOVER candidate not found")
        self.assertIn("CONFIRMATION_MATRIX_HARD_BLOCK", eur_candidate.get("failed_gates", []))
        self.assertFalse(eur_candidate["demo_eligible"])

    # ------------------------------------------------------------------
    # STRONG_FAIL blocks when RR < 1.5
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_strong_fail_blocks_low_rr(self) -> None:
        signal = _eur_signal(
            confidence=0.90,          # setup_score=90 >= 75
            smc_confluence_score=20,  # STRONG_FAIL
            smc_confluence_status="FAIL",
            mtfa_score=20,            # STRONG_FAIL
            mtfa_status="FAIL",
            risk_reward=1.2,          # RR < 1.5 — triggers both RR_TOO_LOW and matrix
        )
        result = self.hunter.evaluate(
            "EURUSD", "EURUSD", _eur_analysis(signal), self.tg, 1, 50
        )
        candidates = result.candidates
        eur_candidate = next(
            (c for c in candidates if c.get("best_strategy") == "EUR_EMA_RSI_ATR_CROSSOVER"),
            None,
        )
        self.assertIsNotNone(eur_candidate)
        self.assertFalse(eur_candidate["demo_eligible"])
        blocked = set(eur_candidate.get("failed_gates", []))
        self.assertTrue(
            "CONFIRMATION_MATRIX_HARD_BLOCK" in blocked or "RR_TOO_LOW" in blocked,
            msg=f"failed_gates={eur_candidate.get('failed_gates')}",
        )

    # ------------------------------------------------------------------
    # STRONG_FAIL does NOT block when setup_score >= 75 AND RR >= 1.5
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_strong_fail_does_not_block_strong_setup(self) -> None:
        signal = _eur_signal(
            confidence=0.90,          # setup_score=90 >= 75
            smc_confluence_score=20,  # STRONG_FAIL
            smc_confluence_status="FAIL",
            mtfa_score=20,            # STRONG_FAIL
            mtfa_status="FAIL",
            risk_reward=2.0,          # RR >= 1.5
        )
        best = self._run(signal)
        self.assertTrue(best["demo_eligible"],
                        msg=f"failed_gates={best.get('failed_gates')}")
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))

    # ------------------------------------------------------------------
    # BTC_SCALPING_AGENT bypasses confirmation matrix entirely
    # ------------------------------------------------------------------
    def test_setup_hunter_confirmation_btc_scalping_bypasses_matrix(self) -> None:
        signal = {
            "strategy": "BTC_SCALPING_AGENT",
            "signal": "BUY",
            "confidence": 75.0,
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "risk_reward": 2.0,
            "smc_confluence_score": 0,
            "smc_confluence_status": "FAIL",
            "mtfa_score": 0,
            "mtfa_status": "FAIL",
            "m15_confirmation": False,
            "m1_entry_confirmation": False,
            "safety_guard_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
        }
        analysis = {
            "ai_decision": {"signal": "BUY", "smc_confluence_status": "FAIL",
                            "smc_confluence_score": 0, "mtfa_status": "FAIL",
                            "mtfa_score": 0, "safety_guard_status": "PASS"},
            "strategy_signals": [signal],
        }
        best = self.hunter.evaluate(
            "BTCUSD", "BTCUSD#", analysis, self.tg, 1, 30
        ).best_candidate
        self.assertFalse(best["demo_eligible"],
                         msg=f"failed_gates={best.get('failed_gates')}")
        self.assertIn("CONFLUENCE_SCORE_TOO_LOW", best.get("failed_gates", []))
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", best.get("failed_gates", []))


# ---------------------------------------------------------------------------
# 4. Confluence engine — Phase 3A SMC/MTFA scoring
# ---------------------------------------------------------------------------

class TestConfluenceConfirmationScoring(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = ConfluenceEngine()
        self.frames = _flat_frames()

    def _components(self, ctx: dict) -> dict:
        return self.engine.evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", self.frames, ctx)["components"]

    def _score(self, ctx: dict) -> float:
        return self.engine.evaluate("EURUSD", "EUR_EMA_RSI_ATR_CROSSOVER", self.frames, ctx)["score"]

    # --- SMC ---
    def test_confluence_confirmation_smc_pass_applies_plus_10(self) -> None:
        comp = self._components({"smc_confluence_score": 80.0})  # PASS
        self.assertEqual(comp.get("smc"), 10.0)

    def test_confluence_confirmation_smc_soft_fail_applies_minus_5(self) -> None:
        comp = self._components({"smc_confluence_score": 55.0})  # SOFT_FAIL
        self.assertEqual(comp.get("smc"), -5.0)

    def test_confluence_confirmation_smc_strong_fail_applies_minus_15(self) -> None:
        comp = self._components({"smc_confluence_score": 20.0})  # STRONG_FAIL
        self.assertEqual(comp.get("smc"), -15.0)

    # --- MTFA ---
    def test_confluence_confirmation_mtfa_pass_applies_plus_10(self) -> None:
        comp = self._components({"mtfa_score": 70.0})  # PASS
        self.assertEqual(comp.get("mtfa"), 10.0)

    def test_confluence_confirmation_mtfa_soft_fail_applies_minus_5(self) -> None:
        comp = self._components({"mtfa_score": 50.0})  # SOFT_FAIL
        self.assertEqual(comp.get("mtfa"), -5.0)

    def test_confluence_confirmation_mtfa_strong_fail_applies_minus_15(self) -> None:
        comp = self._components({"mtfa_score": 20.0})  # STRONG_FAIL
        self.assertEqual(comp.get("mtfa"), -15.0)

    # --- Score bounds ---
    def test_confluence_confirmation_score_never_below_0(self) -> None:
        score = self._score({"smc_confluence_score": 0.0, "mtfa_score": 0.0})
        self.assertGreaterEqual(score, 0.0)

    def test_confluence_confirmation_score_never_above_100(self) -> None:
        score = self._score({"smc_confluence_score": 100.0, "mtfa_score": 100.0})
        self.assertLessEqual(score, 100.0)

    # --- Direction: PASS > SOFT_FAIL > STRONG_FAIL ---
    def test_confluence_confirmation_smc_pass_beats_soft_fail(self) -> None:
        pass_score = self._score({"smc_confluence_score": 80.0, "mtfa_score": 80.0})
        soft_score = self._score({"smc_confluence_score": 55.0, "mtfa_score": 80.0})
        self.assertGreater(pass_score, soft_score)

    def test_confluence_confirmation_smc_soft_fail_beats_strong_fail(self) -> None:
        soft_score = self._score({"smc_confluence_score": 55.0, "mtfa_score": 80.0})
        strong_score = self._score({"smc_confluence_score": 20.0, "mtfa_score": 80.0})
        self.assertGreater(soft_score, strong_score)

    # --- Calibrated status in context overrides score ---
    def test_confluence_confirmation_calibrated_status_in_context_used(self) -> None:
        # Force PASS even though score=55 would be SOFT_FAIL
        comp = self._components({
            "smc_confluence_score": 55.0,
            "smc_calibrated_status": "PASS",
        })
        self.assertEqual(comp.get("smc"), 10.0)


# ---------------------------------------------------------------------------
# 5. Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariantsConfirmation(unittest.TestCase):

    def test_confirmation_order_send_only_in_demo_router(self) -> None:
        """mt5.order_send must only appear in app/mt5/demo_router.py (production code)."""
        app_dir = _PROJECT_ROOT / "app"
        pattern = re.compile(r"\bmt5\.order_send\b")
        violations: list[str] = []
        for py_file in app_dir.rglob("*.py"):
            if "data" in py_file.parts:
                continue
            text = py_file.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                rel = py_file.relative_to(_PROJECT_ROOT)
                rel_str = str(rel).replace("\\", "/")
                if rel_str != "app/mt5/demo_router.py":
                    violations.append(rel_str)
        self.assertEqual(
            violations, [],
            msg=f"mt5.order_send found outside demo_router.py in production code: {violations}",
        )

    def test_confirmation_live_trading_disabled_in_settings(self) -> None:
        s = _demo_settings()
        self.assertFalse(s.allow_live_trading)

    def test_confirmation_demo_only_flag_set(self) -> None:
        s = _demo_settings()
        self.assertTrue(s.demo_only)

    def test_confirmation_max_lot_is_0_01(self) -> None:
        s = _demo_settings()
        self.assertEqual(s.demo_max_lot, 0.01)

    def test_confirmation_live_trading_unchanged_by_soft_fail_routing(self) -> None:
        s = _demo_settings()
        hunter = SetupHunter(s)
        signal = _eur_signal(
            smc_confluence_score=55,
            smc_confluence_status="FAIL",
            mtfa_score=50,
            mtfa_status="FAIL",
        )
        hunter.evaluate("EURUSD", "EURUSD", _eur_analysis(signal), _time_gate(), 1, 50)
        self.assertFalse(s.allow_live_trading)
        self.assertEqual(s.demo_max_lot, 0.01)


if __name__ == "__main__":
    unittest.main()
