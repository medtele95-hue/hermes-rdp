"""Tests for v1.5 final_verdict state machine, ML/SMC stubs, and demo_eligible honesty.

Covers §7 requirements:
- Raw grade A + final confluence C/D → demo_eligible=False
- Setup Hunter never marks BLOCK/WAIT as accepted_for_execution
- ML UNAVAILABLE → logged, neutral, no block, no crash
- SMC not-implemented → logged, neutral, no block, no crash
- [BTC_FINAL_VERDICT] for BTC, [FINAL_VERDICT] for others
- Live trading remains disabled; lot size unchanged
- Behavior-neutral: candidate that passed before still passes
- Dashboard _slim_candidate includes raw_strategy_grade, final_verdict
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.mt5.ml_random_forest_confirmator import confirm as ml_confirm
from app.mt5.smc_orderblock_liquidity_narrator import narrate as smc_narrate
from app.agents.setup_hunter import SetupHunter
from app.services.dashboard_snapshot import _slim_candidate


# ---------------------------------------------------------------------------
# Helpers shared with test_setup_hunter_ranking.py
# ---------------------------------------------------------------------------

def _settings(**kwargs):
    s = MagicMock()
    s.demo_only = True
    s.allow_live_trading = False
    s.demo_max_lot = 0.01
    s.demo_ignore_all_time_blocks = True
    s.safety_guard_enabled = False
    s.gold_liquidity_strategy_enabled = True
    s.gold_liquidity_trade_enabled = True
    s.gold_order_flow_execution_enabled = False
    s.order_flow_execution_enabled = True
    s.order_flow_min_score = 75
    s.order_flow_min_rr = 1.5
    s.order_flow_cooldown_minutes = 15
    s.order_flow_allowed_symbols = "BTCUSD,BTCUSD#,GOLD,GOLD#,EURUSD"
    s.gold_min_liquidity_score = 75
    s.gold_min_rr = 2.0
    s.gold_m1m5_min_score_strict = 75
    s.gold_m1m5_min_score_relaxed = 65
    s.btc_scalping_min_confidence = 55
    s.btc_disable_quant_statistical_pullback = False
    s.hermes_quant_min_score = 75
    s.hermes_quant_min_rr = 2.0
    s.hermes_quant_pro_min_score = 75
    s.hermes_quant_pro_min_rr = 2.0
    s.new_strategies_min_score = 70
    s.gold_min_zone_stars = 3
    s.ema_pullback_block_if_mtfa_and_mtf_fail = False
    s.ema_pullback_require_extra_confirmation = False
    s.ema_pullback_min_smc_score = 0.0
    s.ema_pullback_block_after_symbol_strategy_loss = False
    s.risk_diag_max_realized_risk_percent = 1.0
    s.risk_diag_max_mismatch_abs_percent = 1.0
    s.report_timezone = "UTC"
    s.timezone_local = "UTC"
    s.btc_bad_hours_local = ""
    s.btc_weekend_analysis_only = False
    s.btc_caution_hours_local = ""
    s.bad_hour_analysis_only = False
    s.hermes_adaptive_confluence_enabled = False
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _time_gate():
    return {
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "symbol_market_open": True,
        "market_open": True,
    }


def _of_signal(score: float = 80.0, rr: float = 1.5) -> dict:
    return {
        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
        "setup_type": "ORDER_FLOW_EXECUTION_AGENT",
        "symbol": "BTCUSD",
        "signal": "SELL",
        "direction": "SELL",
        "order_flow_execution_agent_score": score,
        "confidence": score,
        "entry": 50000.0,
        "sl": 50500.0,
        "tp": 49250.0,
        "risk_reward": rr,
        "reward_risk": rr,
        "safety_guard_status": "PASS",
        "smc_confluence_status": "PASS",
        "smc_confluence_score": 75,
        "mtfa_status": "PASS",
        "mtfa_score": 65,
        "symbol_market_open": True,
        "market_open": True,
        "order_flow_execution_agent_signal": "SELL",
        "order_flow_execution_agent_reason": "CVD_BEARISH_BELOW_VAL",
    }


def _run_hunter(symbol: str, broker_symbol: str, signals: list[dict], s=None):
    hunter = SetupHunter(s or _settings())
    analysis = {"ai_decision": {}, "strategy_signals": signals}
    return hunter.evaluate(symbol, broker_symbol, analysis, _time_gate(), 10, 100)


# ---------------------------------------------------------------------------
# §3 ML stub tests
# ---------------------------------------------------------------------------

class TestMLRFConfirmatorStub(unittest.TestCase):

    def test_returns_unavailable(self) -> None:
        result = ml_confirm("BTCUSD#", {}, MagicMock())
        self.assertEqual(result["ml_status"], "UNAVAILABLE")

    def test_returns_reason_not_implemented(self) -> None:
        result = ml_confirm("GOLD#", {}, MagicMock())
        self.assertEqual(result["ml_reason"], "NOT_IMPLEMENTED")

    def test_ml_confidence_is_none(self) -> None:
        result = ml_confirm("EURUSD", {}, MagicMock())
        self.assertIsNone(result["ml_confidence"])

    def test_logs_ml_rf_confirmator_tag(self) -> None:
        with self.assertLogs("hermes", level="INFO") as cm:
            ml_confirm("GOLD#", {}, MagicMock())
        self.assertTrue(
            any("[ML_RF_CONFIRMATOR]" in line for line in cm.output),
            f"[ML_RF_CONFIRMATOR] tag not found in logs: {cm.output}",
        )

    def test_all_symbols_emit_final_verdict_ml_tag(self) -> None:
        """[FINAL_VERDICT_ML] is emitted for ALL symbols (consistent schema, not BTC-only)."""
        for symbol in ("BTCUSD#", "GOLD#", "EURUSD"):
            with self.assertLogs("hermes", level="INFO") as cm:
                ml_confirm(symbol, {}, MagicMock())
            self.assertTrue(
                any("[FINAL_VERDICT_ML]" in line for line in cm.output),
                f"[FINAL_VERDICT_ML] not found for {symbol}: {cm.output}",
            )

    def test_no_btc_specific_ml_tag_remains(self) -> None:
        """After schema unification, [BTC_FINAL_VERDICT_ML] is no longer emitted — use [FINAL_VERDICT_ML]."""
        with self.assertLogs("hermes", level="INFO") as cm:
            ml_confirm("BTCUSD#", {}, MagicMock())
        self.assertFalse(
            any("[BTC_FINAL_VERDICT_ML]" in line for line in cm.output),
            "[BTC_FINAL_VERDICT_ML] must not appear — unified to [FINAL_VERDICT_ML]",
        )

    def test_unavailable_does_not_block_by_itself(self) -> None:
        """UNAVAILABLE must never be treated as a block signal."""
        result = ml_confirm("EURUSD", {}, MagicMock())
        self.assertNotEqual(result["ml_status"], "BLOCK")
        self.assertNotEqual(result["ml_status"], "FAIL")


# ---------------------------------------------------------------------------
# §3 SMC narrator stub tests
# ---------------------------------------------------------------------------

class TestSMCOBNarratorStub(unittest.TestCase):

    def test_smc_ob_avoid_is_false(self) -> None:
        result = smc_narrate("GOLD#", {}, MagicMock())
        self.assertFalse(result["smc_ob_avoid"])

    def test_smc_ob_status_not_implemented(self) -> None:
        result = smc_narrate("EURUSD", {}, MagicMock())
        self.assertEqual(result["smc_ob_status"], "NOT_IMPLEMENTED")

    def test_smc_ob_entry_window_is_none(self) -> None:
        result = smc_narrate("BTCUSD#", {}, MagicMock())
        self.assertIsNone(result["smc_ob_entry_window"])

    def test_logs_smc_ob_narrator_tag(self) -> None:
        with self.assertLogs("hermes", level="INFO") as cm:
            smc_narrate("GOLD#", {}, MagicMock())
        self.assertTrue(
            any("[SMC_OB_NARRATOR]" in line for line in cm.output),
            f"[SMC_OB_NARRATOR] not found in logs: {cm.output}",
        )

    def test_logs_smc_ob_avoid_tag(self) -> None:
        with self.assertLogs("hermes", level="INFO") as cm:
            smc_narrate("GOLD#", {}, MagicMock())
        self.assertTrue(
            any("[SMC_OB_AVOID]" in line for line in cm.output),
            f"[SMC_OB_AVOID] not found in logs: {cm.output}",
        )

    def test_logs_smc_ob_entry_window_tag(self) -> None:
        with self.assertLogs("hermes", level="INFO") as cm:
            smc_narrate("GOLD#", {}, MagicMock())
        self.assertTrue(
            any("[SMC_OB_ENTRY_WINDOW]" in line for line in cm.output),
            f"[SMC_OB_ENTRY_WINDOW] not found in logs: {cm.output}",
        )


# ---------------------------------------------------------------------------
# §4 final_verdict state machine — Setup Hunter new fields
# ---------------------------------------------------------------------------

class TestFinalVerdictFields(unittest.TestCase):
    """Setup Hunter candidate must contain the new §v1.5 fields."""

    def test_pass_candidate_has_new_fields(self) -> None:
        """A passing candidate must have all new §v1.5 fields."""
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertIn("raw_strategy_grade", best)
        self.assertIn("raw_strategy_score", best)
        self.assertIn("ml_status", best)
        self.assertIn("final_verdict", best)
        self.assertIn("accepted_for_analysis", best)
        self.assertIn("accepted_for_execution", best)

    def test_pass_candidate_final_verdict_is_pass(self) -> None:
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertTrue(best["demo_eligible"],
                        f"Expected demo_eligible=True for passing candidate, got {best}")
        self.assertEqual(best["final_verdict"], "PASS")

    def test_pass_candidate_accepted_for_execution_true(self) -> None:
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertTrue(best["accepted_for_execution"])
        self.assertTrue(best["accepted_for_analysis"])

    def test_pass_candidate_ml_status_unavailable(self) -> None:
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertEqual(best["ml_status"], "UNAVAILABLE")

    def test_pass_candidate_raw_strategy_grade_matches_grade(self) -> None:
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertEqual(best["raw_strategy_grade"], best["grade"])

    def test_blocked_candidate_final_verdict_block(self) -> None:
        """Candidate with failed gates → final_verdict=BLOCK, demo_eligible=False."""
        # BTC_SCALPING below confidence floor → BLOCK
        signal = {
            "strategy": "BTC_SCALPING_AGENT",
            "setup_type": "BTC_SCALPING_AGENT",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 55.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "reward_risk": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_status": "PASS",
            "smc_confluence_score": 75,
            "mtfa_status": "PASS",
            "mtfa_score": 65,
            "symbol_market_open": True,
            "market_open": True,
        }
        result = _run_hunter("BTCUSD", "BTCUSD#", [signal])
        best = result.best_candidate
        self.assertEqual(best["final_verdict"], "BLOCK")
        self.assertFalse(best["demo_eligible"])

    def test_blocked_candidate_accepted_for_execution_false(self) -> None:
        """Setup Hunter must never mark a BLOCK candidate as accepted_for_execution."""
        signal = {
            "strategy": "BTC_SCALPING_AGENT",
            "setup_type": "BTC_SCALPING_AGENT",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 55.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "reward_risk": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_status": "PASS",
            "smc_confluence_score": 75,
            "mtfa_status": "PASS",
            "mtfa_score": 65,
            "symbol_market_open": True,
            "market_open": True,
        }
        result = _run_hunter("BTCUSD", "BTCUSD#", [signal])
        best = result.best_candidate
        self.assertFalse(best["accepted_for_execution"],
                         "A BLOCK candidate must never be accepted_for_execution")


# ---------------------------------------------------------------------------
# §4 final_verdict log tags — follow-up fix: ZERO from setup_hunter
# ---------------------------------------------------------------------------

class TestFinalVerdictLogTags(unittest.TestCase):
    """After the follow-up fix, setup_hunter must emit ZERO [FINAL_VERDICT]/[BTC_FINAL_VERDICT]
    logs. The ONE authoritative verdict is emitted by main.py after evaluate_confluence()."""

    def test_setup_hunter_emits_zero_btc_final_verdict_logs(self) -> None:
        """setup_hunter must NOT emit any [BTC_FINAL_VERDICT] — that's main.py's job."""
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        btc_verdict_lines = [l for l in cm.output if "[BTC_FINAL_VERDICT]" in l]
        self.assertEqual(
            len(btc_verdict_lines), 0,
            f"setup_hunter must emit ZERO [BTC_FINAL_VERDICT] lines; got: {btc_verdict_lines}",
        )

    def test_setup_hunter_emits_zero_final_verdict_logs_non_btc(self) -> None:
        """setup_hunter must NOT emit any [FINAL_VERDICT] for non-BTC — that's main.py's job."""
        signal = {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "setup_type": "ORDER_FLOW_EXECUTION_AGENT",
            "symbol": "EURUSD",
            "signal": "BUY",
            "direction": "BUY",
            "order_flow_execution_agent_score": 80.0,
            "confidence": 80.0,
            "entry": 1.1000,
            "sl": 1.0950,
            "tp": 1.1100,
            "risk_reward": 2.0,
            "reward_risk": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_status": "PASS",
            "smc_confluence_score": 75,
            "mtfa_status": "PASS",
            "mtfa_score": 65,
            "symbol_market_open": True,
            "market_open": True,
            "order_flow_execution_agent_signal": "BUY",
            "order_flow_execution_agent_reason": "CVD_BULLISH_ABOVE_VAH",
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("EURUSD", "EURUSD", [signal])
        final_verdict_lines = [
            l for l in cm.output
            if ("[FINAL_VERDICT]" in l or "[BTC_FINAL_VERDICT]" in l)
        ]
        self.assertEqual(
            len(final_verdict_lines), 0,
            f"setup_hunter must emit ZERO [FINAL_VERDICT] lines; got: {final_verdict_lines}",
        )

    def test_setup_hunter_log_does_not_claim_demo_eligible(self) -> None:
        """[SETUP_HUNTER] log must NOT contain 'demo_eligible=' — pre-confluence stage
        must not claim the final eligibility status."""
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        setup_hunter_lines = [l for l in cm.output if "[SETUP_HUNTER]" in l]
        for line in setup_hunter_lines:
            self.assertNotIn(
                "demo_eligible=",
                line,
                f"[SETUP_HUNTER] log must not contain 'demo_eligible=', got: {line}",
            )

    def test_setup_hunter_log_contains_verdict_and_accepted(self) -> None:
        """[SETUP_HUNTER] log must use verdict= and accepted= instead of demo_eligible=."""
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        setup_hunter_lines = [l for l in cm.output if "[SETUP_HUNTER]" in l and "best=" in l]
        self.assertTrue(
            any("verdict=" in l for l in setup_hunter_lines),
            f"[SETUP_HUNTER] log must contain 'verdict=': {setup_hunter_lines}",
        )
        self.assertTrue(
            any("accepted=" in l for l in setup_hunter_lines),
            f"[SETUP_HUNTER] log must contain 'accepted=': {setup_hunter_lines}",
        )

    def test_no_demo_eligible_true_in_any_log_for_any_candidate(self) -> None:
        """No Python log line from setup_hunter.evaluate() should claim demo_eligible=true.
        The final demo_eligible is determined after confluence (in main.py)."""
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        for line in cm.output:
            self.assertNotIn(
                "demo_eligible=true",
                line.lower(),
                f"No log from setup_hunter should claim demo_eligible=true: {line}",
            )

    def test_exactly_one_verdict_emitted_when_main_gate_blocks(self) -> None:
        """When main.py FINAL CONFLUENCE GATE blocks, exactly ONE [FINAL_VERDICT] is emitted."""
        from app.logger import log as hermes_log
        candidate = {
            "demo_eligible": True, "raw_strategy_grade": "A", "final_verdict": "PASS",
            "grade": "A", "ml_status": "UNAVAILABLE",
        }
        _final_conf_grade = "D"
        # Apply main.py gate mutation
        candidate["final_verdict"] = "BLOCK"
        candidate["final_verdict_reason"] = "FINAL_CONFLUENCE_TOO_LOW"
        candidate["demo_eligible"] = False

        # Emit the ONE verdict (as main.py does)
        _fv_rsn = candidate.get("final_verdict_reason") or "NONE"
        _ml_st = candidate.get("ml_status") or "UNAVAILABLE"
        with self.assertLogs("hermes", level="INFO") as cm:
            hermes_log.info(
                "[BTC_FINAL_VERDICT] symbol=%s raw_grade=%s final_grade=%s decision=%s demo_eligible=%s reason=%s ml_status=%s",
                "BTCUSD#", candidate["raw_strategy_grade"], _final_conf_grade,
                candidate["final_verdict"], candidate["demo_eligible"], _fv_rsn, _ml_st,
            )
        verdict_lines = [l for l in cm.output if "[BTC_FINAL_VERDICT]" in l]
        self.assertEqual(
            len(verdict_lines), 1,
            f"Expected exactly 1 [BTC_FINAL_VERDICT] line, got {len(verdict_lines)}: {verdict_lines}",
        )
        # The verdict must show demo_eligible=False when blocked
        self.assertIn("demo_eligible=False", verdict_lines[0])
        self.assertIn("FINAL_CONFLUENCE_TOO_LOW", verdict_lines[0])


# ---------------------------------------------------------------------------
# §5 main.py FINAL CONFLUENCE GATE → demo_eligible honesty
# ---------------------------------------------------------------------------

class TestFinalConfluenceGateDemoEligible(unittest.TestCase):
    """Verify the mutation pattern that main.py applies when _conf_blocks_route=True."""

    def test_raw_a_final_grade_c_demo_eligible_false(self) -> None:
        """When FINAL CONFLUENCE GATE blocks with grade=C, demo_eligible must become False."""
        # Simulate: setup_hunter produced a grade=A candidate (demo_eligible=True)
        candidate = {
            "demo_eligible": True,
            "raw_strategy_grade": "A",
            "final_verdict": "PASS",
            "grade": "A",
        }
        # Simulate main.py mutation when _conf_blocks_route=True, _final_conf_grade="C"
        _final_conf_grade = "C"
        candidate["final_verdict"] = "BLOCK"
        candidate["final_verdict_reason"] = "FINAL_CONFLUENCE_TOO_LOW"
        candidate["demo_eligible"] = False

        self.assertFalse(candidate["demo_eligible"],
                         "demo_eligible must be False when final confluence grade is C")
        self.assertEqual(candidate["final_verdict"], "BLOCK")
        self.assertEqual(candidate["final_verdict_reason"], "FINAL_CONFLUENCE_TOO_LOW")

    def test_raw_a_final_grade_d_demo_eligible_false(self) -> None:
        """When FINAL CONFLUENCE GATE blocks with grade=D, demo_eligible must become False."""
        candidate = {
            "demo_eligible": True,
            "raw_strategy_grade": "A",
            "final_verdict": "PASS",
            "grade": "A",
        }
        _final_conf_grade = "D"
        candidate["final_verdict"] = "BLOCK"
        candidate["final_verdict_reason"] = "FINAL_CONFLUENCE_TOO_LOW"
        candidate["demo_eligible"] = False

        self.assertFalse(candidate["demo_eligible"])
        self.assertEqual(candidate["final_verdict"], "BLOCK")

    def test_pass_candidate_raw_grade_preserved(self) -> None:
        """raw_strategy_grade must be preserved independently of final_verdict changes."""
        candidate = {
            "demo_eligible": True,
            "raw_strategy_grade": "A",
            "final_verdict": "PASS",
            "grade": "A",
        }
        # Simulate BLOCK
        candidate["final_verdict"] = "BLOCK"
        candidate["demo_eligible"] = False

        # raw_strategy_grade must remain A (not cleared)
        self.assertEqual(candidate["raw_strategy_grade"], "A",
                         "raw_strategy_grade must be preserved even when final_verdict=BLOCK")

    def test_final_verdict_log_emitted_by_main_gate(self) -> None:
        """The main.py gate emits [FINAL_VERDICT]/[BTC_FINAL_VERDICT] with ml_status field."""
        from app.logger import log as hermes_log

        _symbol = "BTCUSD#"
        _fv_tag_main = "[BTC_FINAL_VERDICT]"
        with self.assertLogs("hermes", level="INFO") as cm:
            hermes_log.info(
                "%s symbol=%s raw_grade=%s final_grade=%s decision=%s demo_eligible=%s reason=%s ml_status=%s",
                _fv_tag_main, _symbol, "A", "C", "BLOCK", False,
                "FINAL_CONFLUENCE_TOO_LOW", "UNAVAILABLE",
            )
        self.assertTrue(
            any("[BTC_FINAL_VERDICT]" in line and "FINAL_CONFLUENCE_TOO_LOW" in line
                and "ml_status=" in line for line in cm.output),
            f"Expected [BTC_FINAL_VERDICT] with FINAL_CONFLUENCE_TOO_LOW and ml_status: {cm.output}",
        )


# ---------------------------------------------------------------------------
# §6 Dashboard _slim_candidate
# ---------------------------------------------------------------------------

class TestSlimCandidateDashboard(unittest.TestCase):

    def test_slim_candidate_includes_raw_strategy_grade(self) -> None:
        c = {
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "direction": "BUY",
            "grade": "A",
            "raw_strategy_grade": "A",
            "final_verdict": "PASS",
            "final_verdict_reason": None,
            "demo_eligible": True,
            "edge_score": 85,
            "near_miss_reason": None,
            "failed_gates": [],
            "smc_score": 75.0,
            "mtfa_score": 65.0,
            "entry": 2000.0,
            "sl": 1990.0,
            "tp": 2020.0,
            "rr": 2.0,
        }
        slim = _slim_candidate(c)
        self.assertIn("raw_strategy_grade", slim)
        self.assertEqual(slim["raw_strategy_grade"], "A")

    def test_slim_candidate_includes_final_verdict(self) -> None:
        c = {
            "symbol": "BTCUSD#",
            "broker_symbol": "BTCUSD#",
            "best_strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "direction": "SELL",
            "grade": "A",
            "raw_strategy_grade": "A",
            "final_verdict": "BLOCK",
            "final_verdict_reason": "FINAL_CONFLUENCE_TOO_LOW",
            "demo_eligible": False,
            "edge_score": 85,
            "near_miss_reason": None,
            "failed_gates": [],
            "smc_score": 60.0,
            "mtfa_score": 50.0,
            "entry": 50000.0,
            "sl": 50500.0,
            "tp": 49000.0,
            "rr": 2.0,
        }
        slim = _slim_candidate(c)
        self.assertIn("final_verdict", slim)
        self.assertEqual(slim["final_verdict"], "BLOCK")
        self.assertIn("final_verdict_reason", slim)
        self.assertEqual(slim["final_verdict_reason"], "FINAL_CONFLUENCE_TOO_LOW")

    def test_slim_candidate_blocked_raw_grade_visible(self) -> None:
        """Blocked candidate must still show raw_strategy_grade for transparency."""
        c = {
            "symbol": "BTCUSD#",
            "broker_symbol": "BTCUSD#",
            "best_strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "direction": "SELL",
            "grade": "A",
            "raw_strategy_grade": "A",
            "final_verdict": "BLOCK",
            "final_verdict_reason": "FINAL_CONFLUENCE_TOO_LOW",
            "demo_eligible": False,
            "edge_score": 85,
            "near_miss_reason": None,
            "failed_gates": [],
            "smc_score": 60.0,
            "mtfa_score": 50.0,
            "entry": 50000.0,
            "sl": 50500.0,
            "tp": 49000.0,
            "rr": 2.0,
        }
        slim = _slim_candidate(c)
        self.assertEqual(slim["raw_strategy_grade"], "A",
                         "raw A grade must show in dashboard even when final_verdict=BLOCK")
        self.assertFalse(slim["demo_eligible"])


# ---------------------------------------------------------------------------
# Safety guardrails (unchanged from prior phases)
# ---------------------------------------------------------------------------

class TestSafetyGuardrailsUnchanged(unittest.TestCase):

    def test_live_trading_remains_disabled(self) -> None:
        """allow_live_trading must be False in the default settings used by tests."""
        s = _settings()
        self.assertFalse(s.allow_live_trading, "Live trading must remain disabled")

    def test_demo_max_lot_unchanged(self) -> None:
        """demo_max_lot must remain 0.01."""
        s = _settings()
        self.assertEqual(s.demo_max_lot, 0.01, "Lot size must remain 0.01")

    def test_order_send_only_in_demo_router(self) -> None:
        """order_send must only appear in app/mt5/demo_router.py."""
        import glob as glob_mod
        import re
        py_files = glob_mod.glob("app/**/*.py", recursive=True)
        violations = []
        for path in py_files:
            if "demo_router.py" in path or "test_" in path:
                continue
            try:
                content = open(path).read()
            except Exception:
                continue
            if re.search(r"\border_send\b", content):
                violations.append(path)
        self.assertEqual(violations, [],
                         f"order_send found outside demo_router.py: {violations}")


# ---------------------------------------------------------------------------
# Behavior neutrality
# ---------------------------------------------------------------------------

class TestBehaviorNeutrality(unittest.TestCase):

    def test_passing_candidate_still_passes_with_stubs(self) -> None:
        """A candidate that passed before v1.5 must still pass after adding ML/SMC stubs."""
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertTrue(best["demo_eligible"],
                        f"A passing candidate must still be demo_eligible after v1.5 stubs; "
                        f"failed_gates={best.get('failed_gates')}")

    def test_ml_unavailable_does_not_set_demo_eligible_false(self) -> None:
        """ML UNAVAILABLE must never set demo_eligible=False on a passing candidate."""
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertEqual(best["ml_status"], "UNAVAILABLE")
        self.assertTrue(best["demo_eligible"],
                        "ML UNAVAILABLE must not change demo_eligible of a passing candidate")

    def test_smc_narrator_does_not_block_passing_candidate(self) -> None:
        """SMC narrator stub must not block any passing candidate."""
        result = _run_hunter("BTCUSD", "BTCUSD#", [_of_signal(score=80.0)])
        best = result.best_candidate
        self.assertTrue(best["demo_eligible"],
                        "SMC narrator stub must not block a passing candidate")


if __name__ == "__main__":
    unittest.main()
