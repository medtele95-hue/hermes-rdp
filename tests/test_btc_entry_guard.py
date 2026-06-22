"""URGENT PATCH tests — Order Flow confirmation bypass removal and BTC entry guard.

Tests 1, 2, 3, 4, 5, 6, 11, 12, 13 from the patch specification.
"""
from __future__ import annotations

import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mt5.btc_entry_gate import evaluate_btc_entry_gate, _ORDER_FLOW, _BTC_SCALPING
from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
from app.agents.setup_hunter import SetupHunter, _compute_penalty
from app.agents.confirmation_matrix import evaluate as evaluate_confirmation_matrix


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sh_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.demo_only = True
    s.allow_live_trading = False
    s.demo_max_lot = 0.01
    s.demo_ignore_all_time_blocks = True
    s.safety_guard_enabled = False
    s.order_flow_execution_enabled = True
    s.order_flow_min_score = 75
    s.order_flow_min_rr = 1.5
    s.order_flow_cooldown_minutes = 15
    s.order_flow_allowed_symbols = "BTCUSD,BTCUSD#"
    s.gold_liquidity_strategy_enabled = True
    s.gold_liquidity_trade_enabled = True
    s.gold_order_flow_execution_enabled = False
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


def _gate_settings(**kwargs):
    defaults = {
        "allow_live_trading": False,
        "demo_only": True,
        "old_btc_max_open_positions": 1,
        "btc_scalping_min_confidence": 55,
        "order_flow_min_score": 75,
        "old_btc_entry_gate_require_market_confirmation": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _time_gate() -> dict:
    return {
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "symbol_market_open": True,
        "market_open": True,
    }


def _of_signal(score: float = 80.0, rr: float = 2.0,
               final_confluence_grade: str = "A",
               final_confluence_score: float = 70.0) -> dict:
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
        "smc_confluence_score": 75.0,
        "mtfa_status": "PASS",
        "mtfa_score": 65.0,
        "symbol_market_open": True,
        "market_open": True,
        "final_confluence_grade": final_confluence_grade,
        "final_confluence_score": final_confluence_score,
    }


_CM_PASS = {"hard_block": False, "smc_calibrated_status": "PASS", "mtfa_calibrated_status": "PASS"}
_CM_HARD_BLOCK = {"hard_block": True, "smc_calibrated_status": "STRONG_FAIL", "mtfa_calibrated_status": "STRONG_FAIL"}


def _run_hunter(signals: list[dict], s=None) -> object:
    hunter = SetupHunter(s or _sh_settings())
    analysis = {"ai_decision": {}, "strategy_signals": signals}
    return hunter.evaluate("BTCUSD", "BTCUSD#", analysis, _time_gate(), 10, 100)


# ─── Tests 1 & 6: ORDER_FLOW CM hard_block gates ──────────────────────────────

class TestOrderFlowConfirmationMatrixHardBlock(unittest.TestCase):
    """Tests 1 & 6: CM hard_block=True makes ORDER_FLOW ineligible for execution."""

    def test_order_flow_hard_block_adds_cm_failed_gate(self):
        """Test 1: hard_block → CONFIRMATION_MATRIX_HARD_BLOCK in failed_gates."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_HARD_BLOCK):
            result = _run_hunter([_of_signal(final_confluence_grade="A", final_confluence_score=70.0)])
        best = result.best_candidate
        self.assertIn(
            "CONFIRMATION_MATRIX_HARD_BLOCK", best["failed_gates"],
            f"Expected CM_HARD_BLOCK in failed_gates, got: {best['failed_gates']}",
        )
        self.assertFalse(best["demo_eligible"],
                         "ORDER_FLOW with CM hard_block must not be demo_eligible")

    def test_order_flow_hard_block_not_accepted_for_execution(self):
        """Test 6: candidate with CM hard_block is never accepted for execution."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_HARD_BLOCK):
            result = _run_hunter([_of_signal(final_confluence_grade="A", final_confluence_score=70.0)])
        best = result.best_candidate
        self.assertFalse(bool(best.get("accepted_for_execution")),
                         "CM hard_block must prevent accepted_for_execution")
        self.assertFalse(bool(best.get("demo_eligible")))

    def test_order_flow_no_hard_block_does_not_add_cm_failure(self):
        """CM hard_block=False AND grade A AND score >= 65 → no CM failure added."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_PASS):
            result = _run_hunter([_of_signal(final_confluence_grade="A", final_confluence_score=70.0)])
        best = result.best_candidate
        self.assertNotIn(
            "CONFIRMATION_MATRIX_HARD_BLOCK", best["failed_gates"],
            f"No CM block expected when hard_block=False, got: {best['failed_gates']}",
        )

    def test_order_flow_hard_block_logs_btc_entry_guard(self):
        """[BTC_ENTRY_GUARD] with reason=CONFIRMATION_MATRIX_HARD_BLOCK is logged."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_HARD_BLOCK):
            with self.assertLogs("hermes", level="INFO") as cm:
                _run_hunter([_of_signal(final_confluence_grade="A", final_confluence_score=70.0)])
        guard_lines = [l for l in cm.output
                       if "[BTC_ENTRY_GUARD]" in l and "CONFIRMATION_MATRIX_HARD_BLOCK" in l]
        self.assertTrue(guard_lines,
                        f"Expected [BTC_ENTRY_GUARD] CONFIRMATION_MATRIX_HARD_BLOCK log, "
                        f"got logs: {[l for l in cm.output if 'BTC_ENTRY_GUARD' in l]}")

    def test_order_flow_smc_strong_fail_is_hard_block_even_with_strong_setup(self):
        result = evaluate_confirmation_matrix(
            "BTCUSD", "ORDER_FLOW_EXECUTION_AGENT", 20.0, 65.0, 90.0, 2.0,
        )
        self.assertTrue(result["hard_block"])
        self.assertEqual(result["hard_block_reason"], "SMC_STRONG_FAIL")

    def test_order_flow_mtfa_strong_fail_is_hard_block_even_with_strong_setup(self):
        result = evaluate_confirmation_matrix(
            "BTCUSD", "ORDER_FLOW_EXECUTION_AGENT", 75.0, 20.0, 90.0, 2.0,
        )
        self.assertTrue(result["hard_block"])
        self.assertEqual(result["hard_block_reason"], "MTFA_STRONG_FAIL")

    def test_both_strong_fail_emit_honest_matrix_and_setup_reject(self):
        signal = _of_signal(final_confluence_grade="A", final_confluence_score=70.0)
        signal.update({"smc_confluence_score": 20.0, "mtfa_score": 20.0})
        with self.assertLogs("hermes", level="INFO") as captured:
            result = _run_hunter([signal])
        matrix = [line for line in captured.output if "[CONFIRMATION_MATRIX]" in line]
        rejects = [line for line in captured.output if "[SETUP_HUNTER_REJECT]" in line]
        self.assertTrue(any("hard_block=true" in line and "reason=SMC_STRONG_FAIL,MTFA_STRONG_FAIL" in line for line in matrix))
        self.assertTrue(any("reason=CONFIRMATION_MATRIX_HARD_BLOCK" in line for line in rejects))
        self.assertIn("CONFIRMATION_MATRIX_HARD_BLOCK", result.best_candidate["failed_gates"])


# ─── Test 2: ORDER_FLOW confluence grade/score requirements ───────────────────

class TestOrderFlowConfluenceRequirements(unittest.TestCase):
    """Test 2: ORDER_FLOW with final grade < B or score < 65 is analysis-only."""

    def test_grade_c_blocked(self):
        """final_confluence_grade=C → ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B in failed_gates."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_PASS):
            result = _run_hunter([_of_signal(final_confluence_grade="C", final_confluence_score=70.0)])
        best = result.best_candidate
        self.assertIn("ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B", best["failed_gates"],
                      f"Grade C must fail, got: {best['failed_gates']}")
        self.assertFalse(best["demo_eligible"])

    def test_grade_d_blocked(self):
        """final_confluence_grade=D → blocked."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_PASS):
            result = _run_hunter([_of_signal(final_confluence_grade="D", final_confluence_score=70.0)])
        best = result.best_candidate
        self.assertIn("ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B", best["failed_gates"])

    def test_score_below_65_blocked(self):
        """final_confluence_score=60 < 65 → ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_PASS):
            result = _run_hunter([_of_signal(final_confluence_grade="A", final_confluence_score=60.0)])
        best = result.best_candidate
        self.assertIn("ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65", best["failed_gates"],
                      f"Score 60 < 65 must fail, got: {best['failed_gates']}")

    def test_grade_b_score_65_not_blocked_by_confluence(self):
        """grade=B and score=65 → grade/score gates do not block."""
        with patch("app.agents.setup_hunter._confirmation_matrix", return_value=_CM_PASS):
            result = _run_hunter([_of_signal(final_confluence_grade="B", final_confluence_score=65.0)])
        best = result.best_candidate
        self.assertNotIn("ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B", best["failed_gates"],
                         f"Grade B must pass, got: {best['failed_gates']}")
        self.assertNotIn("ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65", best["failed_gates"])


# ─── Tests 3 & 4: Entry gate daily loss + loss streak ─────────────────────────

class TestBtcEntryGateDailyLossLimit(unittest.TestCase):
    """Test 3: Daily closed P&L ≤ -$5 blocks new BTC entries."""

    def test_daily_loss_at_limit_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, closed_pnl_today=-5.00,
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "DAILY_LOSS_LIMIT")
        self.assertEqual(result["failed_check"], "DAILY_LOSS_CHECK")

    def test_daily_loss_below_limit_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _BTC_SCALPING, "SELL", None, _gate_settings(), 0,
            confidence=60.0, closed_pnl_today=-7.50,
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "DAILY_LOSS_LIMIT")

    def test_daily_loss_just_above_limit_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, closed_pnl_today=-4.99,
        )
        self.assertEqual(result["decision"], "PASS")

    def test_daily_loss_positive_pnl_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, closed_pnl_today=2.50,
        )
        self.assertEqual(result["decision"], "PASS")

    def test_daily_loss_none_skips_check(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, closed_pnl_today=None,
        )
        self.assertEqual(result["decision"], "PASS")


class TestBtcEntryGateLossStreak(unittest.TestCase):
    """Test 4: Two consecutive losses within 90 min block new BTC entries."""

    def _two_recent_losses(self) -> list[dict]:
        now = time.time()
        return [
            {"profit": -0.5, "close_time": now - 200},
            {"profit": -0.3, "close_time": now - 10},
        ]

    def _two_expired_losses(self) -> list[dict]:
        old = time.time() - 5500  # 91+ minutes ago
        return [
            {"profit": -0.5, "close_time": old},
            {"profit": -0.3, "close_time": old},
        ]

    def test_two_recent_losses_blocks(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, recent_btc_results=self._two_recent_losses(),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["reason"], "LOSS_STREAK")
        self.assertEqual(result["failed_check"], "LOSS_STREAK_CHECK")

    def test_one_loss_does_not_trigger_streak(self):
        now = time.time()
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, recent_btc_results=[{"profit": -0.5, "close_time": now - 10}],
        )
        self.assertEqual(result["decision"], "PASS")

    def test_streak_cooldown_expired_passes(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, recent_btc_results=self._two_expired_losses(),
        )
        self.assertEqual(result["decision"], "PASS")

    def test_last_trade_win_no_streak_block(self):
        now = time.time()
        trades = [
            {"profit": -0.5, "close_time": now - 100},
            {"profit": 0.3, "close_time": now - 10},
        ]
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, recent_btc_results=trades,
        )
        self.assertEqual(result["decision"], "PASS")

    def test_none_recent_results_skips_check(self):
        result = evaluate_btc_entry_gate(
            "BTCUSD#", _ORDER_FLOW, "BUY", None, _gate_settings(), 0,
            confidence=80.0, recent_btc_results=None,
        )
        self.assertEqual(result["decision"], "PASS")


# ─── Test 5: Entry guard never blocks exits ───────────────────────────────────

class TestEntryGuardDoesNotBlockExits(unittest.TestCase):
    """Test 5: BtcFastExitDaemon closes positions regardless of entry gate state."""

    def _daemon(self, close_fn=None):
        s = SimpleNamespace(
            old_btc_fast_exit_daemon_enabled=True,
            old_btc_fast_exit_interval_ms=250,
            old_btc_fast_exit_min_profit_usd=0.03,
            old_btc_fast_exit_hard_min_profit_usd=0.01,
            old_btc_fast_exit_close_at_any_positive=True,
            demo_magic_number=909002,
            allow_live_trading=False,
            demo_only=True,
            hermes_execution_profile="",
            btc_exit_arbiter_enabled=False,
        )
        return BtcFastExitDaemon(
            s,
            close_fn or (lambda pos, r: {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}),
        )

    def test_daemon_closes_profitable_position_exit_not_gated(self):
        """Daemon closes profitable positions; evaluate_btc_entry_gate is never consulted."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}

        pos = SimpleNamespace(
            ticket=9001, profit=0.10, magic=909002, symbol="BTCUSD#",
            comment="HERMES_BTC", type=0, volume=0.01, price_open=65000.0,
        )
        daemon = self._daemon(capture)
        daemon._evaluate_position(pos, 0.03, 0.01, True)
        self.assertEqual(len(closed), 1, "Daemon must close profitable position")
        self.assertEqual(closed[0], "ANY_POSITIVE_FAST_EXIT")

    def test_evaluate_btc_entry_gate_not_called_in_exit_daemon(self):
        """exit daemon source must not reference evaluate_btc_entry_gate."""
        src = Path("app/mt5/btc_fast_exit_daemon.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "evaluate_btc_entry_gate", src,
            "Exit daemon must not call the entry gate",
        )


class TestGeometricV2SetupHunterIntegration(unittest.TestCase):
    def test_shadow_mode_records_geometry_without_new_block(self):
        signal = _of_signal(final_confluence_grade="A", final_confluence_score=70.0)
        result = _run_hunter([signal], _sh_settings(geometric_mode="SHADOW"))
        self.assertNotIn("OF_GEO_INSUFFICIENT", result.best_candidate["failed_gates"])
        self.assertEqual(result.best_candidate["geometric_v2"]["mode"], "SHADOW")

    def test_execution_filter_blocks_low_geometry_order_flow(self):
        signal = _of_signal(final_confluence_grade="A", final_confluence_score=70.0)
        result = _run_hunter([signal], _sh_settings(geometric_mode="EXECUTION_FILTER"))
        self.assertIn("OF_GEO_INSUFFICIENT", result.best_candidate["failed_gates"])
        self.assertFalse(result.best_candidate["demo_eligible"])

    def test_aplus_order_flow_is_exempt_from_low_geometry_guard(self):
        signal = _of_signal(final_confluence_grade="A", final_confluence_score=70.0)
        signal["order_flow_grade"] = "A+"
        result = _run_hunter([signal], _sh_settings(geometric_mode="EXECUTION_FILTER"))
        self.assertNotIn("OF_GEO_INSUFFICIENT", result.best_candidate["failed_gates"])

    def test_graded_penalty_positive_boundaries(self):
        self.assertEqual(_compute_penalty(80.0), 10.0)
        self.assertEqual(_compute_penalty(50.0), 0.0)
        self.assertEqual(_compute_penalty(20.0), -5.0)

    def test_graded_penalty_distinguishes_deep_strong_fail(self):
        self.assertEqual(_compute_penalty(31.0), -5.0)
        self.assertLess(_compute_penalty(5.0), _compute_penalty(31.0))
        self.assertEqual(_compute_penalty(0.0), -30.0)


# ─── Safety invariants (tests 11–13) ──────────────────────────────────────────

class TestPatchSafetyInvariants(unittest.TestCase):
    """Tests 11-13: live trading, lot size, order_send isolation unchanged by patch."""

    def test_live_trading_remains_disabled(self):
        """Test 11: allow_live_trading must be False."""
        from app.config import get_settings
        self.assertFalse(get_settings().allow_live_trading)

    def test_demo_max_lot_remains_0_01(self):
        """Test 12: demo_max_lot must be 0.01."""
        from app.config import get_settings
        self.assertAlmostEqual(float(get_settings().demo_max_lot), 0.01,
                               msg="demo_max_lot must remain 0.01")

    def test_order_send_only_in_demo_router(self):
        """Test 13: mt5.order_send must not appear outside app/mt5/demo_router.py."""
        pattern = re.compile(r'(?<!["\'])mt5\.order_send\s*\(')
        allowed = {"app/mt5/demo_router.py"}
        violations = []
        for path in Path("app").rglob("*.py"):
            rel = path.as_posix()
            if rel in allowed:
                continue
            src = path.read_text(encoding="utf-8")
            if pattern.search(src):
                violations.append(rel)
        self.assertEqual(violations, [],
                         f"mt5.order_send found outside demo_router.py: {violations}")


if __name__ == "__main__":
    unittest.main()
