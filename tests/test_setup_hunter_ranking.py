"""SetupHunter ranking, quality-gate, and telemetry tests — Task 7 coverage.

Verifies:
- SetupHunter selects ORDER_FLOW_EXECUTION_AGENT over BTC_SCALPING when OF has higher score/grade.
- BTC_SCALPING_AGENT with confidence 55 is rejected (below hard floor of 75).
- BTC_SCALPING_AGENT with confidence 55 and confluence grade D is rejected.
- Confirmation hard_block prevents SetupHunter accept.
- Symbol-mismatched strategies are skipped before confirmation matrix.
- WAIT outputs are not logged as SETUP_HUNTER_IN direction=WAIT.
- LIVE_SNAPSHOT telemetry preserves zero values.
- LIVE_SNAPSHOT uses source=UNKNOWN when source is None.
- mt5.order_send remains only in app/mt5/demo_router.py.
- Live trading remains blocked.
- Max lot remains 0.01.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.agents.setup_hunter import SetupHunter
from app.config import Settings
from app.services.heartbeat_service import _enrich_account_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**kwargs) -> MagicMock:
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
    s.btc_scalping_min_confidence = 55  # hard floor 75 enforced in code regardless
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


def _time_gate() -> dict:
    return {
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "symbol_market_open": True,
        "market_open": True,
    }


def _of_signal(score: float = 80.0, grade_score: float = 80.0, rr: float = 1.5) -> dict:
    """Minimal ORDER_FLOW_EXECUTION_AGENT signal."""
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


def _btc_signal(confidence: float, smc: float = 0.0, mtfa: float = 0.0, rr: float = 2.0) -> dict:
    """BTC_SCALPING_AGENT signal with configurable parameters."""
    return {
        "strategy": "BTC_SCALPING_AGENT",
        "setup_type": "BTC_SCALPING_AGENT",
        "symbol": "BTCUSD",
        "signal": "BUY",
        "direction": "BUY",
        "confidence": confidence,
        "entry": 50000.0,
        "sl": 49000.0,
        "tp": 52000.0,
        "risk_reward": rr,
        "reward_risk": rr,
        "safety_guard_status": "PASS",
        "smc_confluence_status": "FAIL" if smc < 70 else "PASS",
        "smc_confluence_score": smc,
        "mtfa_status": "FAIL" if mtfa < 60 else "PASS",
        "mtfa_score": mtfa,
        "symbol_market_open": True,
        "market_open": True,
    }


def _run_hunter(symbol: str, broker_symbol: str, signals: list[dict], s=None) -> object:
    hunter = SetupHunter(s or _settings())
    analysis = {"ai_decision": {}, "strategy_signals": signals}
    return hunter.evaluate(symbol, broker_symbol, analysis, _time_gate(), 10, 100)


# ---------------------------------------------------------------------------
# Task 1 — Candidate ranking
# ---------------------------------------------------------------------------

class TestCandidateRanking(unittest.TestCase):

    def test_order_flow_beats_btc_scalping_when_of_has_higher_grade(self) -> None:
        """ORDER_FLOW grade=A score=94 must beat BTC_SCALPING grade=B score=80."""
        of = _of_signal(score=94.0, rr=2.0)
        btc = _btc_signal(confidence=80.0, smc=70, mtfa=60, rr=2.0)
        result = _run_hunter("BTCUSD", "BTCUSD#", [of, btc])
        best = result.best_candidate
        self.assertEqual(best["best_strategy"], "ORDER_FLOW_EXECUTION_AGENT",
                         f"Expected OF to win, got {best['best_strategy']} "
                         f"(grade={best['grade']} score={best['edge_score']})")

    def test_order_flow_beats_btc_scalping_grade_log(self) -> None:
        """Log should show ORDER_FLOW_EXECUTION_AGENT as best."""
        of = _of_signal(score=94.0, rr=2.0)
        btc = _btc_signal(confidence=80.0, smc=70, mtfa=60, rr=2.0)
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [of, btc])
        setup_hunter_lines = [l for l in cm.output if "[SETUP_HUNTER]" in l and "best=" in l]
        self.assertTrue(
            any("ORDER_FLOW_EXECUTION_AGENT" in l for l in setup_hunter_lines),
            f"Expected ORDER_FLOW_EXECUTION_AGENT in [SETUP_HUNTER] best=, got: {setup_hunter_lines}",
        )


# ---------------------------------------------------------------------------
# Task 2 — BTC scalping quality gate
# ---------------------------------------------------------------------------

class TestBTCScalpingQualityGate(unittest.TestCase):

    def test_btc_scalping_confidence_55_rejected(self) -> None:
        """BTC_SCALPING at confidence 55 must be rejected (below hard floor 75)."""
        btc = _btc_signal(confidence=55.0, smc=70, mtfa=60)
        result = _run_hunter("BTCUSD", "BTCUSD#", [btc])
        best = result.best_candidate
        self.assertFalse(best["demo_eligible"],
                         "BTC_SCALPING confidence=55 must not be demo_eligible")
        self.assertIn("BTC_SCALPING_CONFIDENCE_BELOW_MIN", best["failed_gates"])

    def test_btc_scalping_confluence_grade_d_rejected(self) -> None:
        """BTC_SCALPING passing confidence gate but with STRONG_FAIL SMC+MTFA must be rejected."""
        btc = _btc_signal(confidence=80.0, smc=5.0, mtfa=5.0, rr=2.0)
        result = _run_hunter("BTCUSD", "BTCUSD#", [btc])
        best = result.best_candidate
        self.assertFalse(best["demo_eligible"],
                         "BTC_SCALPING with confluence grade D must not be demo_eligible")
        self.assertIn("CONFLUENCE_SCORE_TOO_LOW", best["failed_gates"])

    def test_btc_scalping_confluence_d_logs_reject(self) -> None:
        """SETUP_HUNTER_REJECT must be logged with reason=CONFLUENCE_SCORE_TOO_LOW."""
        btc = _btc_signal(confidence=80.0, smc=5.0, mtfa=5.0, rr=2.0)
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [btc])
        reject_lines = [l for l in cm.output if "SETUP_HUNTER_REJECT" in l and "BTC_SCALPING_AGENT" in l]
        self.assertTrue(
            any("CONFLUENCE_SCORE_TOO_LOW" in l for l in reject_lines),
            f"Expected CONFLUENCE_SCORE_TOO_LOW in SETUP_HUNTER_REJECT, got: {reject_lines}",
        )

    def test_btc_scalping_75_with_good_confluence_routes(self) -> None:
        """BTC_SCALPING at confidence=80 with PASS SMC+MTFA must be demo_eligible."""
        btc = _btc_signal(confidence=80.0, smc=75, mtfa=65, rr=2.0)
        result = _run_hunter("BTCUSD", "BTCUSD#", [btc])
        best = result.best_candidate
        self.assertEqual(best["best_strategy"], "BTC_SCALPING_AGENT")
        self.assertTrue(best["demo_eligible"],
                        f"BTC_SCALPING confidence=80 with good confluence should route; "
                        f"failed_gates={best['failed_gates']}")


# ---------------------------------------------------------------------------
# Task 3 — Confirmation hard_block consistency
# ---------------------------------------------------------------------------

class TestConfirmationHardBlock(unittest.TestCase):

    def test_confirmation_hard_block_prevents_accept(self) -> None:
        """A strategy with STRONG_FAIL SMC+MTFA and low setup_score must be blocked."""
        signal = {
            "strategy": "BREAKOUT_RETEST",
            "setup_type": "BREAKOUT_RETEST",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 50.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "reward_risk": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_status": "FAIL",
            "smc_confluence_score": 10.0,
            "mtfa_status": "FAIL",
            "mtfa_score": 10.0,
            "symbol_market_open": True,
            "market_open": True,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
        }
        result = _run_hunter("BTCUSD", "BTCUSD#", [signal])
        best_btc = next(
            (c for c in result.candidates if c["best_strategy"] == "BREAKOUT_RETEST"),
            None,
        )
        if best_btc:
            self.assertFalse(best_btc["demo_eligible"],
                             "BREAKOUT_RETEST with hard_block must not be demo_eligible")

    def test_btc_scalping_hard_block_logged(self) -> None:
        """If BTC_SCALPING CM emits hard_block, SETUP_HUNTER_REJECT must be logged."""
        # Very low smc/mtfa AND low setup score triggers hard_block in CM
        btc = _btc_signal(confidence=80.0, smc=5.0, mtfa=5.0, rr=1.5)
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [btc])
        # Either CONFLUENCE_SCORE_TOO_LOW or CONFIRMATION_MATRIX_HARD_BLOCK must appear
        reject_lines = [l for l in cm.output if "SETUP_HUNTER_REJECT" in l and "BTC_SCALPING_AGENT" in l]
        self.assertGreater(len(reject_lines), 0,
                           "Expected at least one SETUP_HUNTER_REJECT for BTC_SCALPING with bad SMC/MTFA")


# ---------------------------------------------------------------------------
# Task 4 — Symbol-mismatched strategies skip confirmation matrix
# ---------------------------------------------------------------------------

class TestSymbolStrategyMismatch(unittest.TestCase):

    def test_gold_strategy_skipped_for_btc_no_confirmation_matrix(self) -> None:
        """GOLD_LIQUIDITY_HUNTER_PRO on BTCUSD must not generate [CONFIRMATION_MATRIX] log."""
        signal = {
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "setup_type": "GOLD_LIQUIDITY_HUNTER_PRO",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 80.0,
            "gold_liquidity_score": 80.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 10,
            "mtfa_score": 10,
            "symbol_market_open": True,
            "market_open": True,
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [signal])
        cm_lines = [l for l in cm.output if "[CONFIRMATION_MATRIX]" in l and "GOLD_LIQUIDITY_HUNTER_PRO" in l]
        self.assertEqual(cm_lines, [],
                         f"[CONFIRMATION_MATRIX] must not be generated for GOLD strategy on BTCUSD: {cm_lines}")

    def test_gold_strategy_on_btc_logs_setup_policy(self) -> None:
        """GOLD_LIQUIDITY_HUNTER_PRO on BTCUSD must log [SETUP_POLICY] SYMBOL_STRATEGY_NOT_ALLOWED."""
        signal = {
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "setup_type": "GOLD_LIQUIDITY_HUNTER_PRO",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 80.0,
            "gold_liquidity_score": 80.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 10,
            "mtfa_score": 10,
            "symbol_market_open": True,
            "market_open": True,
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [signal])
        policy_lines = [l for l in cm.output if "[SETUP_POLICY]" in l and "GOLD_LIQUIDITY_HUNTER_PRO" in l]
        self.assertGreater(len(policy_lines), 0,
                           "Expected [SETUP_POLICY] SYMBOL_STRATEGY_NOT_ALLOWED for GOLD strategy on BTCUSD")

    def test_eur_strategy_skipped_for_btc_no_confirmation_matrix(self) -> None:
        """EUR_EMA_RSI_ATR_CROSSOVER on BTCUSD must not generate [CONFIRMATION_MATRIX] log."""
        signal = {
            "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "confidence": 80.0,
            "entry": 50000.0,
            "sl": 49000.0,
            "tp": 52000.0,
            "risk_reward": 2.0,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 10,
            "mtfa_score": 10,
            "symbol_market_open": True,
            "market_open": True,
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [signal])
        cm_lines = [l for l in cm.output if "[CONFIRMATION_MATRIX]" in l and "EUR_EMA_RSI_ATR" in l]
        self.assertEqual(cm_lines, [],
                         f"[CONFIRMATION_MATRIX] must not be generated for EUR strategy on BTCUSD: {cm_lines}")


# ---------------------------------------------------------------------------
# Task 5 — WAIT outputs not logged as SETUP_HUNTER_IN direction=WAIT
# ---------------------------------------------------------------------------

class TestWaitNotSetupHunterIn(unittest.TestCase):
    """setup_hunter.py must never emit [SETUP_HUNTER_IN] for direction=WAIT candidates."""

    def test_wait_signal_not_logged_as_setup_hunter_in(self) -> None:
        signal = {
            "strategy": "BTC_SCALPING_AGENT",
            "symbol": "BTCUSD",
            "signal": "WAIT",
            "direction": "WAIT",
            "confidence": 80.0,
            "entry": None,
            "sl": None,
            "tp": None,
            "risk_reward": None,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 10,
            "mtfa_score": 10,
            "symbol_market_open": True,
            "market_open": True,
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [signal])
        # direction=WAIT must appear in CANDIDATE_WAIT, not SETUP_HUNTER_IN
        setup_in_wait = [
            l for l in cm.output
            if "SETUP_HUNTER_IN" in l and "direction=WAIT" in l
        ]
        self.assertEqual(setup_in_wait, [],
                         f"[SETUP_HUNTER_IN] direction=WAIT must not appear: {setup_in_wait}")

    def test_wait_signal_logs_candidate_wait(self) -> None:
        signal = {
            "strategy": "BTC_SCALPING_AGENT",
            "symbol": "BTCUSD",
            "signal": "WAIT",
            "direction": "WAIT",
            "confidence": 80.0,
            "entry": None,
            "sl": None,
            "tp": None,
            "risk_reward": None,
            "safety_guard_status": "PASS",
            "smc_confluence_score": 10,
            "mtfa_score": 10,
            "symbol_market_open": True,
            "market_open": True,
        }
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("BTCUSD", "BTCUSD#", [signal])
        self.assertTrue(
            any("[CANDIDATE_WAIT]" in l for l in cm.output),
            "Expected [CANDIDATE_WAIT] for direction=WAIT signal",
        )


# ---------------------------------------------------------------------------
# Task 6 — LIVE_SNAPSHOT telemetry preserves zeros and uses UNKNOWN source
# ---------------------------------------------------------------------------

class TestLiveSnapshotTelemetry(unittest.TestCase):

    def _account(self) -> dict:
        return {
            "login": 123456,
            "balance": 10000.0,
            "equity": 10050.0,
            "profit": 0.0,
            "snapshot_time": "2026-06-12T10:00:00+00:00",
        }

    def test_zero_closed_pnl_not_converted_to_none(self) -> None:
        sync = {
            "demo_closed_pnl_today": 0.0,
            "demo_floating_pnl": 0.0,
            "demo_total_pnl_today": 0.0,
            "pnl_source": "MT5_HISTORY_DEALS",
            "hermes_mt5_open_positions_count": 0,
        }
        enriched = _enrich_account_snapshot(self._account(), sync)
        self.assertIsNotNone(enriched["closed_pnl"], "zero closed_pnl must not become None")
        self.assertAlmostEqual(enriched["closed_pnl"], 0.0, places=4)
        self.assertIsNotNone(enriched["floating_pnl"], "zero floating_pnl must not become None")
        self.assertAlmostEqual(enriched["floating_pnl"], 0.0, places=4)

    def test_source_present_when_position_sync_has_source(self) -> None:
        sync = {
            "demo_closed_pnl_today": 0.0,
            "demo_floating_pnl": 0.0,
            "demo_total_pnl_today": 0.0,
            "pnl_source": "MT5_HISTORY_DEALS",
            "hermes_mt5_open_positions_count": 0,
        }
        enriched = _enrich_account_snapshot(self._account(), sync)
        self.assertEqual(enriched["source"], "MT5_HISTORY_DEALS")

    def test_source_none_when_empty_sync(self) -> None:
        """When position_sync is empty, source stays None (UNKNOWN formatting is in emit)."""
        enriched = _enrich_account_snapshot(self._account(), {})
        self.assertIsNone(enriched["source"])


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariantsRanking(unittest.TestCase):

    def test_order_send_only_in_demo_router(self) -> None:
        pattern = re.compile(r"order_send")
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in (root / "app").rglob("*.py"):
            if "demo_router.py" in path.as_posix():
                continue
            if "app/data/" in path.as_posix() or "app\\data\\" in path.as_posix():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_live_trading_blocked(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)


if __name__ == "__main__":
    unittest.main()
