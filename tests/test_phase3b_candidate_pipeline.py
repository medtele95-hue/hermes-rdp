"""Phase 3B: Candidate pipeline cleanup + GOLD/EUR/OrderFlow candidate visibility tests.

Covers:
- WAIT signal is never passed to validate_candidate
- WAIT signal logs [CANDIDATE_WAIT]
- BUY/SELL with missing required field logs [CANDIDATE_REJECT]
- GOLD WAIT logs exact [GOLD_CANDIDATE] reason
- EUR WAIT logs exact [EUR_CANDIDATE] reason
- ORDER_FLOW_EXECUTION_AGENT logs [ORDER_FLOW_EXEC_AGENT] WAIT for every evaluated symbol
- Safety invariants: order_send only in demo_router, live trading blocked, max lot 0.01
"""
from __future__ import annotations

import glob
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.agents.setup_hunter import SetupHunter, _candidate_wait_reason, _GOLD_CANDIDATE_STRATEGIES
from app.strategies.candidate import validate_candidate
from app.strategies.order_flow_execution_agent import evaluate as of_evaluate, _wait as of_wait


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _base_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.demo_only = True
    s.allow_live_trading = False
    s.demo_max_lot = 0.01
    s.demo_ignore_all_time_blocks = True
    s.safety_guard_enabled = False
    s.gold_liquidity_strategy_enabled = True
    s.gold_liquidity_trade_enabled = True
    s.gold_order_flow_execution_enabled = False
    s.order_flow_execution_enabled = False
    s.order_flow_min_score = 75
    s.order_flow_min_rr = 1.5
    s.order_flow_cooldown_minutes = 15
    s.order_flow_allowed_symbols = "BTCUSD,GOLD,EURUSD"
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


def _time_gate(open: bool = True) -> dict:
    return {
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "symbol_market_open": open,
        "market_open": open,
    }


def _wait_signal(strategy: str, symbol: str = "GOLD", reason: str = "TEST_WAIT") -> dict:
    """Build a minimal strategy signal that returns WAIT."""
    return {
        "strategy": strategy,
        "setup_type": strategy,
        "symbol": symbol,
        "signal": "WAIT",
        "direction": "WAIT",
        "confidence": 0.0,
        "entry": None,
        "sl": None,
        "tp": None,
        "risk_reward": None,
        "reward_risk": None,
        "safety_guard_status": "PASS",
        "smc_confluence_status": "FAIL",
        "smc_confluence_score": 0,
        "mtfa_status": "FAIL",
        "mtfa_score": 0,
        "symbol_market_open": True,
        "market_open": True,
        f"{_strategy_reason_key(strategy)}": reason,
    }


def _buy_signal(strategy: str, symbol: str = "BTCUSD") -> dict:
    """Build a minimal valid BUY signal."""
    return {
        "strategy": strategy,
        "setup_type": strategy,
        "symbol": symbol,
        "signal": "BUY",
        "direction": "BUY",
        "confidence": 80.0,
        "entry": 100.0,
        "sl": 98.0,
        "tp": 104.0,
        "risk_reward": 2.0,
        "reward_risk": 2.0,
        "safety_guard_status": "PASS",
        "smc_confluence_status": "FAIL",
        "smc_confluence_score": 0,
        "mtfa_status": "FAIL",
        "mtfa_score": 0,
        "symbol_market_open": True,
        "market_open": True,
    }


def _strategy_reason_key(strategy: str) -> str:
    return {
        "GOLD_LIQUIDITY_HUNTER_PRO": "gold_liquidity_reason",
        "GOLD_M1_M5_EMA_SWEEP_SCALPER": "gold_m1m5_scalper_reason",
        "GOLD_ORDER_FLOW_CVD_VWAP": "gold_order_flow_reason",
        "ORDER_FLOW_EXECUTION_AGENT": "order_flow_execution_agent_reason",
        "EUR_EMA_RSI_ATR_CROSSOVER": "eur_ema_rsi_atr_reason",
    }.get(strategy, "reason")


def _run_hunter(symbol: str, broker_symbol: str, signals: list[dict], settings=None) -> object:
    s = settings or _base_settings()
    hunter = SetupHunter(s)
    analysis = {"ai_decision": {}, "strategy_signals": signals}
    return hunter.evaluate(symbol, broker_symbol, analysis, _time_gate(), 10, 50)


# ---------------------------------------------------------------------------
# 1. WAIT is not passed to validate_candidate
# ---------------------------------------------------------------------------

class TestWaitNotValidated(unittest.TestCase):
    """WAIT signals must never reach validate_candidate."""

    def test_gold_liquidity_wait_not_validated(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with patch("app.agents.setup_hunter.validate_candidate") as mock_vc:
            _run_hunter("GOLD", "GOLD#", [signal])
        mock_vc.assert_not_called()

    def test_gold_m1m5_wait_not_validated(self):
        signal = _wait_signal("GOLD_M1_M5_EMA_SWEEP_SCALPER", symbol="GOLD", reason="ATR_TOO_HIGH")
        with patch("app.agents.setup_hunter.validate_candidate") as mock_vc:
            _run_hunter("GOLD", "GOLD#", [signal])
        mock_vc.assert_not_called()

    def test_eur_wait_not_validated(self):
        signal = _wait_signal("EUR_EMA_RSI_ATR_CROSSOVER", symbol="EURUSD", reason="NO_EMA_CROSS")
        with patch("app.agents.setup_hunter.validate_candidate") as mock_vc:
            _run_hunter("EURUSD", "EURUSD", [signal])
        mock_vc.assert_not_called()

    def test_order_flow_wait_not_validated(self):
        signal = _wait_signal("ORDER_FLOW_EXECUTION_AGENT", symbol="GOLD", reason="ORDER_FLOW_EXECUTION_DISABLED")
        with patch("app.agents.setup_hunter.validate_candidate") as mock_vc:
            _run_hunter("GOLD", "GOLD#", [signal])
        mock_vc.assert_not_called()


# ---------------------------------------------------------------------------
# 2. WAIT logs [CANDIDATE_WAIT] not [CANDIDATE_REJECT direction=WAIT]
# ---------------------------------------------------------------------------

class TestWaitLogging(unittest.TestCase):

    def test_gold_wait_logs_candidate_wait(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[CANDIDATE_WAIT]", combined)
        self.assertIn("GOLD_LIQUIDITY_HUNTER_PRO", combined)

    def test_gold_wait_never_logs_candidate_reject_direction_wait(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with self.assertLogs("hermes", level="WARNING") as cm:
            # inject a warning-level log so assertLogs doesn't fail on empty
            import logging
            logging.getLogger("hermes").warning("_test_sentinel_")
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        # Must NOT have CANDIDATE_REJECT with direction=WAIT
        self.assertNotIn("CANDIDATE_REJECT", combined.replace("_test_sentinel_", ""))

    def test_eur_wait_logs_candidate_wait(self):
        signal = _wait_signal("EUR_EMA_RSI_ATR_CROSSOVER", symbol="EURUSD", reason="NO_EMA_CROSS")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("EURUSD", "EURUSD", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[CANDIDATE_WAIT]", combined)
        self.assertIn("EUR_EMA_RSI_ATR_CROSSOVER", combined)

    def test_setup_hunter_reject_logged_for_wait(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[SETUP_HUNTER_REJECT]", combined)


# ---------------------------------------------------------------------------
# 3. BUY/SELL missing field logs [CANDIDATE_REJECT]
# ---------------------------------------------------------------------------

class TestBuySellValidation(unittest.TestCase):

    def test_buy_missing_entry_logs_candidate_reject(self):
        signal = _buy_signal("ORDER_FLOW_EXECUTION_AGENT", symbol="GOLD")
        signal["entry"] = None  # force missing field
        with self.assertLogs("hermes", level="WARNING") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[CANDIDATE_REJECT]", combined)

    def test_buy_missing_sl_logs_candidate_reject(self):
        signal = _buy_signal("ORDER_FLOW_EXECUTION_AGENT", symbol="GOLD")
        signal["sl"] = None
        with self.assertLogs("hermes", level="WARNING") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[CANDIDATE_REJECT]", combined)


# ---------------------------------------------------------------------------
# 4. [GOLD_CANDIDATE] logs
# ---------------------------------------------------------------------------

class TestGoldCandidateLogs(unittest.TestCase):

    def test_gold_liquidity_wait_logs_gold_candidate_with_reason(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[GOLD_CANDIDATE]", combined)
        self.assertIn("GOLD_LIQUIDITY_HUNTER_PRO", combined)
        self.assertIn("GOLD_LIQUIDITY_WAIT", combined)

    def test_gold_m1m5_wait_logs_gold_candidate_atr_too_high(self):
        signal = _wait_signal("GOLD_M1_M5_EMA_SWEEP_SCALPER", symbol="GOLD", reason="ATR_TOO_HIGH")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[GOLD_CANDIDATE]", combined)
        self.assertIn("ATR_TOO_HIGH", combined)

    def test_gold_candidate_has_decision_wait(self):
        signal = _wait_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD", reason="GOLD_LIQUIDITY_WAIT")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("GOLD", "GOLD#", [signal])
        gold_lines = [line for line in cm.output if "[GOLD_CANDIDATE]" in line]
        self.assertTrue(len(gold_lines) >= 1)
        self.assertTrue(any("decision=WAIT" in line for line in gold_lines))

    def test_gold_candidate_strategies_set(self):
        self.assertIn("GOLD_LIQUIDITY_HUNTER_PRO", _GOLD_CANDIDATE_STRATEGIES)
        self.assertIn("GOLD_M1_M5_EMA_SWEEP_SCALPER", _GOLD_CANDIDATE_STRATEGIES)
        self.assertIn("GOLD_ORDER_FLOW_CVD_VWAP", _GOLD_CANDIDATE_STRATEGIES)
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", _GOLD_CANDIDATE_STRATEGIES)


# ---------------------------------------------------------------------------
# 5. [EUR_CANDIDATE] logs
# ---------------------------------------------------------------------------

class TestEurCandidateLogs(unittest.TestCase):

    def test_eur_wait_logs_eur_candidate_with_reason(self):
        signal = _wait_signal("EUR_EMA_RSI_ATR_CROSSOVER", symbol="EURUSD", reason="NO_EMA_CROSS")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("EURUSD", "EURUSD", [signal])
        combined = "\n".join(cm.output)
        self.assertIn("[EUR_CANDIDATE]", combined)
        self.assertIn("EUR_EMA_RSI_ATR_CROSSOVER", combined)
        self.assertIn("NO_EMA_CROSS", combined)

    def test_eur_candidate_has_decision_wait(self):
        signal = _wait_signal("EUR_EMA_RSI_ATR_CROSSOVER", symbol="EURUSD", reason="NO_EMA_CROSS")
        with self.assertLogs("hermes", level="INFO") as cm:
            _run_hunter("EURUSD", "EURUSD", [signal])
        eur_lines = [line for line in cm.output if "[EUR_CANDIDATE]" in line]
        self.assertTrue(len(eur_lines) >= 1)
        self.assertTrue(any("decision=WAIT" in line for line in eur_lines))


# ---------------------------------------------------------------------------
# 6. ORDER_FLOW_EXECUTION_AGENT logs [ORDER_FLOW_EXEC_AGENT] for every WAIT
# ---------------------------------------------------------------------------

class TestOrderFlowExecAgentLogs(unittest.TestCase):

    def _snap(self, **kwargs):
        defaults = {
            "price": 2300.0, "vwap": 2295.0, "poc": 2290.0,
            "vah": 2310.0, "val": 2280.0, "cvd_slope": None,
            "delta_proxy": None, "divergence": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        defaults.update(kwargs)
        return defaults

    def _settings(self, enabled=False):
        s = MagicMock()
        s.order_flow_execution_enabled = enabled
        s.order_flow_min_score = 75
        s.order_flow_min_rr = 1.5
        s.order_flow_cooldown_minutes = 15
        s.order_flow_allowed_symbols = "BTCUSD,GOLD,EURUSD"
        s.allow_live_trading = False
        s.demo_only = True
        s.demo_max_lot = 0.01
        return s

    def test_disabled_logs_wait(self):
        s = self._settings(enabled=False)
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={"order_flow_snapshot": self._snap()}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("decision=WAIT", combined)
        self.assertIn("ORDER_FLOW_EXECUTION_DISABLED", combined)

    def test_missing_snapshot_logs_wait(self):
        s = self._settings(enabled=True)
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("decision=WAIT", combined)

    def test_stale_snapshot_logs_wait(self):
        s = self._settings(enabled=True)
        snap = self._snap(created_at="2020-01-01T00:00:00+00:00")
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={"order_flow_snapshot": snap}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("ORDER_FLOW_DATA_STALE", combined)

    def test_no_valid_setup_logs_wait(self):
        s = self._settings(enabled=True)
        # price in middle of range with no cvd/delta → no setup
        snap = self._snap(price=2295.0, cvd_slope=None, delta_proxy=None)
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={"order_flow_snapshot": snap}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("decision=WAIT", combined)

    def test_score_below_threshold_logs_wait(self):
        s = self._settings(enabled=True)
        s.order_flow_min_score = 100  # unreachable threshold
        snap = self._snap(price=2281.0, val=2280.0, delta_proxy=100.0)
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={"order_flow_snapshot": snap}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("decision=WAIT", combined)

    def test_wait_helper_logs(self):
        with self.assertLogs("hermes", level="INFO") as cm:
            of_wait("GOLD", "ORDER_FLOW_COOLDOWN_ACTIVE")
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("ORDER_FLOW_COOLDOWN_ACTIVE", combined)

    def test_symbol_not_allowed_logs_wait(self):
        s = self._settings(enabled=True)
        s.order_flow_allowed_symbols = "BTCUSD"
        snap = self._snap()
        with self.assertLogs("hermes", level="INFO") as cm:
            of_evaluate("GOLD", None, context={"order_flow_snapshot": snap}, settings=s)
        combined = "\n".join(cm.output)
        self.assertIn("[ORDER_FLOW_EXEC_AGENT]", combined)
        self.assertIn("ORDER_FLOW_SYMBOL_NOT_ALLOWED", combined)


# ---------------------------------------------------------------------------
# 7. _candidate_wait_reason helper
# ---------------------------------------------------------------------------

class TestCandidateWaitReason(unittest.TestCase):

    def test_gold_liquidity_reason_extracted(self):
        candidate = {"best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO", "gold_liquidity_reason": "GOLD_LIQUIDITY_WAIT"}
        self.assertEqual(_candidate_wait_reason(candidate), "GOLD_LIQUIDITY_WAIT")

    def test_gold_m1m5_reason_extracted(self):
        candidate = {"best_strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER", "gold_m1m5_scalper_reason": "ATR_TOO_HIGH"}
        self.assertEqual(_candidate_wait_reason(candidate), "ATR_TOO_HIGH")

    def test_eur_reason_extracted(self):
        candidate = {"best_strategy": "EUR_EMA_RSI_ATR_CROSSOVER", "eur_ema_rsi_atr_reason": "NO_EMA_CROSS"}
        self.assertEqual(_candidate_wait_reason(candidate), "NO_EMA_CROSS")

    def test_order_flow_reason_extracted(self):
        candidate = {"best_strategy": "ORDER_FLOW_EXECUTION_AGENT", "order_flow_execution_agent_reason": "ORDER_FLOW_EXECUTION_DISABLED"}
        self.assertEqual(_candidate_wait_reason(candidate), "ORDER_FLOW_EXECUTION_DISABLED")

    def test_fallback_to_failed_gates(self):
        candidate = {"best_strategy": "SIMO_ATM_BREAKOUT", "failed_gates": ["RR_TOO_LOW"]}
        self.assertEqual(_candidate_wait_reason(candidate), "RR_TOO_LOW")

    def test_fallback_when_empty(self):
        candidate = {"best_strategy": "UNKNOWN_STRATEGY", "failed_gates": []}
        self.assertEqual(_candidate_wait_reason(candidate), "WAIT")


# ---------------------------------------------------------------------------
# 8. Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariantsPhase3B(unittest.TestCase):

    def test_order_send_only_in_demo_router(self):
        pattern = re.compile(r"mt5\.order_send\s*\(")
        violations = []
        for path in glob.glob("C:/hermes-mt5-agent/app/**/*.py", recursive=True):
            if "demo_router.py" in path or "app/data/" in path or "app\\data\\" in path:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern.search(line):
                            violations.append(f"{path}:{i}: {line.rstrip()}")
            except (OSError, UnicodeDecodeError):
                pass
        self.assertEqual(violations, [], f"order_send outside demo_router: {violations}")

    def test_live_trading_remains_false(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_remains_true(self):
        from app.config import Settings
        s = Settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_remains_001(self):
        from app.config import Settings
        s = Settings()
        self.assertAlmostEqual(s.demo_max_lot, 0.01)

    def test_order_flow_execution_agent_default_disabled(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(getattr(s, "order_flow_execution_enabled", False))


# ---------------------------------------------------------------------------
# 9. SetupHunter policy: strategy not allowed for symbol skipped before validation
# ---------------------------------------------------------------------------

class TestSetupHunterPolicy(unittest.TestCase):

    def test_btc_strategy_skipped_for_gold_symbol(self):
        """BTC_SCALPING_AGENT on a GOLD symbol should be policy-blocked, not validated."""
        signal = _buy_signal("BTC_SCALPING_AGENT", symbol="GOLD")
        # Should not raise, should not validate the candidate as GOLD-eligible
        result = _run_hunter("GOLD", "GOLD#", [signal])
        best = result.best_candidate
        # best should not be BTC_SCALPING for GOLD
        self.assertNotEqual(best.get("best_strategy"), "BTC_SCALPING_AGENT")

    def test_gold_liquidity_only_for_gold(self):
        """GOLD strategies only processed when symbol is GOLD."""
        signal = {
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 80.0,
            "entry": 100.0,
            "sl": 98.0,
            "tp": 104.0,
            "risk_reward": 2.0,
            "safety_guard_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
        }
        result = _run_hunter("EURUSD", "EURUSD", [signal])
        best = result.best_candidate
        # GOLD_LIQUIDITY_HUNTER_PRO should not be the best for EURUSD
        self.assertNotEqual(best.get("best_strategy"), "GOLD_LIQUIDITY_HUNTER_PRO")


if __name__ == "__main__":
    unittest.main()
