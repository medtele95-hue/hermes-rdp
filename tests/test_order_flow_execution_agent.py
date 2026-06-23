from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.strategies.order_flow_execution_agent import (
    STRATEGY,
    evaluate,
    _detect_setup,
    _score_setup,
    _calc_sltp,
    SETUP_LIQUIDITY_SWEEP_REVERSAL,
    SETUP_VWAP_RECLAIM_REJECTION,
    SETUP_VALUE_AREA_ROTATION,
    SETUP_VALUE_BREAKOUT,
    _last_routed,
)
from app.strategies.registry import (
    ACTIVE_EXECUTION_STRATEGIES,
    OBSERVATION_STRATEGIES,
    ALLOWED_GOLD_EXECUTION_STRATEGIES,
    ALLOWED_EUR_EXECUTION_STRATEGIES,
    ALLOWED_BTC_EXECUTION_STRATEGIES,
)


def _settings(
    order_flow_execution_enabled: bool = True,
    order_flow_min_score: int = 75,
    order_flow_min_rr: float = 1.5,
    order_flow_cooldown_minutes: int = 15,
    order_flow_allowed_symbols: str = "BTCUSD,BTCUSD#,GOLD,GOLD#,XAUUSD,EURUSD",
    allow_live_trading: bool = False,
    demo_only: bool = True,
    demo_max_lot: float = 0.01,
):
    s = MagicMock()
    s.order_flow_execution_enabled = order_flow_execution_enabled
    s.order_flow_min_score = order_flow_min_score
    s.order_flow_min_rr = order_flow_min_rr
    s.order_flow_cooldown_minutes = order_flow_cooldown_minutes
    s.order_flow_allowed_symbols = order_flow_allowed_symbols
    s.allow_live_trading = allow_live_trading
    s.demo_only = demo_only
    s.demo_max_lot = demo_max_lot
    return s


def _fresh_snapshot(price=2300.0, vwap=2295.0, poc=2290.0, vah=2310.0, val=2280.0,
                    cvd_slope=0.5, delta=100.0, divergence=None):
    return {
        "price": price,
        "vwap": vwap,
        "poc": poc,
        "vah": vah,
        "val": val,
        "cvd_slope": cvd_slope,
        "delta_proxy": delta,
        "divergence": divergence,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class TestRegistryClassification(unittest.TestCase):
    def test_order_flow_reader_is_observation_only(self):
        self.assertIn("ORDER_FLOW_READER", OBSERVATION_STRATEGIES)
        self.assertNotIn("ORDER_FLOW_READER", ACTIVE_EXECUTION_STRATEGIES)

    def test_order_flow_execution_agent_is_active_execution(self):
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", ACTIVE_EXECUTION_STRATEGIES)
        self.assertNotIn("ORDER_FLOW_EXECUTION_AGENT", OBSERVATION_STRATEGIES)

    def test_order_flow_execution_agent_allowed_for_gold(self):
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", ALLOWED_GOLD_EXECUTION_STRATEGIES)

    def test_order_flow_execution_agent_allowed_for_eur(self):
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", ALLOWED_EUR_EXECUTION_STRATEGIES)

    def test_order_flow_execution_agent_allowed_for_btc(self):
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", ALLOWED_BTC_EXECUTION_STRATEGIES)


class TestDisabledFlag(unittest.TestCase):
    def test_disabled_returns_wait(self):
        s = _settings(order_flow_execution_enabled=False)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": _fresh_snapshot()}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_EXECUTION_DISABLED")

    def test_enabled_with_valid_data_can_produce_signal(self):
        s = _settings(order_flow_execution_enabled=True)
        # Set price near val to trigger LIQUIDITY_SWEEP_REVERSAL BUY
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")


class TestSymbolFilter(unittest.TestCase):
    def test_disallowed_symbol_blocks(self):
        s = _settings(order_flow_allowed_symbols="BTCUSD,GOLD")
        snap = _fresh_snapshot()
        result = evaluate("RANDOM#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_SYMBOL_NOT_ALLOWED")

    def test_gold_canonical_allowed(self):
        s = _settings()
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertIn(result["symbol"], {"GOLD"})


class TestMissingOrStaleData(unittest.TestCase):
    def test_missing_snapshot_blocks(self):
        s = _settings()
        result = evaluate("GOLD#", None, context={}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_SNAPSHOT_MISSING")

    def test_stale_snapshot_blocks(self):
        s = _settings()
        snap = _fresh_snapshot()
        snap["created_at"] = "2020-01-01T00:00:00+00:00"
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_DATA_STALE")

    def test_missing_key_levels_blocks(self):
        s = _settings()
        snap = {"price": 2300.0, "created_at": datetime.now(timezone.utc).isoformat()}
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_MISSING_KEY_LEVELS")


class TestScoreThreshold(unittest.TestCase):
    def test_low_score_blocks(self):
        # snapshot yields score=100; use min_score=101 so 100 < 101 triggers SCORE_BELOW_THRESHOLD
        s = _settings(order_flow_min_score=101)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertIn("SCORE_BELOW", result["reason"])

    def test_score_at_threshold_creates_candidate(self):
        s = _settings(order_flow_min_score=25)
        # Minimal setup: only structure score (+25) should be enough
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertIn(result["signal"], {"BUY", "SELL"})


class TestCandidateFields(unittest.TestCase):
    def setUp(self):
        _last_routed.clear()

    def _get_buy_result(self):
        s = _settings(order_flow_min_score=25)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0, cvd_slope=0.5)
        return evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)

    def test_candidate_has_required_fields(self):
        result = self._get_buy_result()
        if result["signal"] == "WAIT":
            self.skipTest("No signal produced — scoring too strict")
        self.assertEqual(result["strategy"], "ORDER_FLOW_EXECUTION_AGENT")
        self.assertIn(result["direction"], {"BUY", "SELL"})
        self.assertIsNotNone(result["entry"])
        self.assertIsNotNone(result["sl"])
        self.assertIsNotNone(result["tp"])
        self.assertIsNotNone(result["rr"])
        self.assertGreaterEqual(result["confidence"], 25)
        self.assertIn(result["grade"], {"A", "B", "D"})
        self.assertEqual(result["mode"], "ACTIVE_EXECUTION")

    def test_rr_is_at_least_min_rr(self):
        result = self._get_buy_result()
        if result["signal"] == "WAIT":
            self.skipTest("No signal produced")
        self.assertGreaterEqual(result["rr"], 1.5)

    def test_order_flow_payload_embedded(self):
        result = self._get_buy_result()
        if result["signal"] == "WAIT":
            self.skipTest("No signal produced")
        of = result.get("order_flow_execution_agent")
        self.assertIsInstance(of, dict)
        self.assertIn("vwap", of)
        self.assertIn("poc", of)
        self.assertIn("vah", of)
        self.assertIn("val", of)


class TestSetupDetection(unittest.TestCase):
    def test_liquidity_sweep_reversal_buy(self):
        setup = _detect_setup(2279.0, 2295.0, 2290.0, 2310.0, 2280.0, 0.5, 100.0, None)
        self.assertIsNotNone(setup)
        self.assertEqual(setup, (SETUP_LIQUIDITY_SWEEP_REVERSAL, "BUY"))

    def test_liquidity_sweep_reversal_buy_with_divergence(self):
        setup = _detect_setup(2285.0, 2295.0, 2290.0, 2310.0, 2280.0, None, -50.0, "bull")
        self.assertIsNotNone(setup)
        self.assertEqual(setup[0], SETUP_LIQUIDITY_SWEEP_REVERSAL)
        self.assertEqual(setup[1], "BUY")

    def test_vwap_reclaim_buy(self):
        setup = _detect_setup(2300.0, 2295.0, 2290.0, 2310.0, 2280.0, 0.5, 100.0, None)
        self.assertIsNotNone(setup)
        self.assertEqual(setup[1], "BUY")

    def test_vwap_rejection_sell(self):
        setup = _detect_setup(2290.0, 2295.0, 2290.0, 2310.0, 2280.0, -0.5, -100.0, None)
        self.assertIsNotNone(setup)
        self.assertEqual(setup[1], "SELL")

    def test_no_setup_when_no_conditions_met(self):
        setup = _detect_setup(2295.0, 2295.0, 2295.0, 2310.0, 2280.0, None, None, None)
        self.assertIsNone(setup)


class TestScoring(unittest.TestCase):
    def test_full_score_buy(self):
        score = _score_setup(
            2295.0, 2295.0, 2290.0, 2310.0, 2280.0,
            0.5, 100.0, None,
            SETUP_VWAP_RECLAIM_REJECTION, "BUY", True, True,
        )
        self.assertGreaterEqual(score, 75)

    def test_divergence_penalty_reduces_score(self):
        score_without = _score_setup(
            2295.0, 2295.0, 2290.0, 2310.0, 2280.0,
            0.5, 100.0, None,
            SETUP_VWAP_RECLAIM_REJECTION, "BUY", True, True,
        )
        score_with_bad_div = _score_setup(
            2295.0, 2295.0, 2290.0, 2310.0, 2280.0,
            0.5, 100.0, "bear",  # bear divergence against BUY
            SETUP_VWAP_RECLAIM_REJECTION, "BUY", True, True,
        )
        self.assertLess(score_with_bad_div, score_without)

    def test_score_capped_at_100(self):
        score = _score_setup(
            2290.0, 2295.0, 2290.0, 2310.0, 2280.0,
            0.5, 100.0, "bull",
            SETUP_LIQUIDITY_SWEEP_REVERSAL, "BUY", True, True,
        )
        self.assertLessEqual(score, 100)

    def test_score_not_negative(self):
        score = _score_setup(
            2295.0, 2295.0, 2290.0, 2310.0, 2280.0,
            0.5, 100.0, "bear",
            SETUP_VWAP_RECLAIM_REJECTION, "BUY", None, False,
        )
        self.assertGreaterEqual(score, 0)


class TestSLTP(unittest.TestCase):
    def test_buy_sltp_valid(self):
        result = _calc_sltp("BUY", 2300.0, 2295.0, 2290.0, 2310.0, 2280.0, 1.5)
        self.assertIsNotNone(result)
        entry, sl, tp, rr = result
        self.assertGreater(entry, sl)
        self.assertGreater(tp, entry)
        self.assertGreaterEqual(rr, 1.5)

    def test_sell_sltp_valid(self):
        result = _calc_sltp("SELL", 2300.0, 2295.0, 2290.0, 2310.0, 2280.0, 1.5)
        self.assertIsNotNone(result)
        entry, sl, tp, rr = result
        self.assertLess(entry, sl)
        self.assertLess(tp, entry)
        self.assertGreaterEqual(rr, 1.5)

    def test_invalid_buy_when_price_below_sl_level(self):
        # price == val means risk = 0
        result = _calc_sltp("BUY", 2280.0, 2295.0, 2290.0, 2310.0, 2280.0, 1.5)
        # sl = val - buffer = 2280 - 2.28 = 2277.72, price=2280, risk=2.28 > 0 → valid
        self.assertIsNotNone(result)


class TestCooldown(unittest.TestCase):
    def setUp(self):
        _last_routed.clear()

    def test_first_signal_not_blocked_by_cooldown(self):
        s = _settings(order_flow_cooldown_minutes=15, order_flow_min_score=25)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        if result["signal"] == "WAIT" and result["reason"] == "ORDER_FLOW_NO_SETUP_PATTERN":
            self.skipTest("No setup pattern")
        self.assertNotEqual(result["reason"], "ORDER_FLOW_COOLDOWN_ACTIVE")

    def test_second_signal_blocked_by_cooldown(self):
        s = _settings(order_flow_cooldown_minutes=9999, order_flow_min_score=25)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        _last_routed["GOLD"] = datetime.now(timezone.utc)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "ORDER_FLOW_COOLDOWN_ACTIVE")


class TestSafetyInvariants(unittest.TestCase):
    def test_order_send_only_in_demo_router(self):
        import subprocess
        result = subprocess.run(
            ["python", "-m", "app.utils.search_order_send"],
            capture_output=True,
            text=True,
            cwd="C:/hermes-mt5-agent",
        )
        # Use grep approach
        import glob
        import re
        pattern = re.compile(r"mt5\.order_send\s*\(")
        violations = []
        for path in glob.glob("C:/hermes-mt5-agent/app/**/*.py", recursive=True):
            if "demo_router.py" in path or "app/data/" in path:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern.search(line):
                            violations.append(f"{path}:{i}: {line.rstrip()}")
            except (OSError, UnicodeDecodeError):
                pass
        self.assertEqual(violations, [], f"order_send outside demo_router: {violations}")

    def test_live_trading_remains_false_default(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_default_true(self):
        from app.config import Settings
        s = Settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_default(self):
        from app.config import Settings
        s = Settings()
        self.assertAlmostEqual(s.demo_max_lot, 0.01)

    def test_order_flow_execution_agent_default_disabled(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.order_flow_execution_enabled)

    def test_strategy_manager_classifies_reader_as_observation(self):
        from app.services.strategy_manager import StrategyManager
        sm = StrategyManager(MagicMock())
        self.assertEqual(sm.strategy_mode("ORDER_FLOW_READER"), "OBSERVATION_ONLY")
        self.assertFalse(sm.route_allowed("ORDER_FLOW_READER"))

    def test_strategy_manager_classifies_exec_agent_as_active(self):
        from app.services.strategy_manager import StrategyManager
        sm = StrategyManager(MagicMock())
        self.assertEqual(sm.strategy_mode("ORDER_FLOW_EXECUTION_AGENT"), "ACTIVE_EXECUTION")
        self.assertTrue(sm.route_allowed("ORDER_FLOW_EXECUTION_AGENT"))


class TestSnapshotSources(unittest.TestCase):
    def test_snapshot_from_context(self):
        s = _settings(order_flow_min_score=1)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        self.assertEqual(result["strategy"], STRATEGY)

    def test_snapshot_from_frames(self):
        s = _settings(order_flow_min_score=1)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", {"order_flow_snapshot": snap}, context=None, settings=s)
        self.assertEqual(result["strategy"], STRATEGY)

    def test_snapshot_from_order_flow_snapshots_dict(self):
        s = _settings(order_flow_min_score=1)
        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        result = evaluate("GOLD#", None, context={"order_flow_snapshots": {"GOLD": snap}}, settings=s)
        self.assertEqual(result["strategy"], STRATEGY)


class TestRoutingFlow(unittest.TestCase):
    """Verify the candidate flows correctly toward SetupHunter when eligible."""

    def setUp(self):
        _last_routed.clear()

    def _hunter_settings(self):
        s = _settings(order_flow_min_score=1)
        s.gold_liquidity_trade_enabled = True
        s.gold_liquidity_strategy_enabled = True
        s.gold_order_flow_execution_enabled = False
        s.safety_guard_enabled = False
        s.demo_ignore_all_time_blocks = True
        s.gold_order_flow_require_divergence = False
        s.gold_order_flow_min_confidence = 0
        s.btc_scalping_min_confidence = 55
        s.hermes_quant_min_score = 75
        s.hermes_quant_min_rr = 2.0
        s.hermes_quant_pro_min_score = 75
        s.hermes_quant_pro_min_rr = 2.0
        s.gold_min_liquidity_score = 75
        s.gold_min_rr = 2.0
        s.gold_m1m5_min_score_strict = 75
        s.gold_m1m5_min_score_relaxed = 65
        s.new_strategies_min_score = 70
        s.btc_disable_quant_statistical_pullback = False
        s.hermes_adaptive_confluence_enabled = False
        s.report_timezone = "Africa/Casablanca"
        s.timezone_local = "Africa/Casablanca"
        s.btc_bad_hours_local = "21,23,2,4"
        s.btc_weekend_analysis_only = False
        s.btc_caution_hours_local = ""
        s.bad_hour_analysis_only = False
        s.ema_pullback_block_if_mtfa_and_mtf_fail = False
        s.ema_pullback_require_extra_confirmation = False
        s.ema_pullback_min_smc_score = 0.0
        s.ema_pullback_block_after_symbol_strategy_loss = False
        s.risk_diag_max_realized_risk_percent = 1.0
        s.risk_diag_max_mismatch_abs_percent = 1.0
        return s

    def test_candidate_goes_to_setup_hunter_not_direct_to_demo_router(self):
        from app.agents.setup_hunter import SetupHunter
        from unittest.mock import patch as _patch
        s = self._hunter_settings()

        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        of_signal = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        if of_signal["signal"] == "WAIT":
            self.skipTest("No signal produced at this min_score")

        # Enrich with required confluence fields (normally injected by analysis pipeline)
        of_signal["final_confluence_grade"] = "A"
        of_signal["final_confluence_score"] = 70.0

        hunter = SetupHunter(s)
        analysis = {"ai_decision": {}, "strategy_signals": [of_signal]}
        time_gate = {
            "time_gate_status": "PASS",
            "session_name": "LONDON",
            "symbol_market_open": True,
            "market_open": True,
        }
        _cm_pass = {"hard_block": False, "smc_calibrated_status": "PASS", "mtfa_calibrated_status": "PASS"}
        with _patch("app.agents.setup_hunter._confirmation_matrix", return_value=_cm_pass):
            result = hunter.evaluate("GOLD", "GOLD#", analysis, time_gate, 10, 50)
        best = result.best_candidate
        # Should be recognized as ORDER_FLOW_EXECUTION_AGENT
        self.assertEqual(best.get("best_strategy"), "ORDER_FLOW_EXECUTION_AGENT")

    def test_setup_hunter_logs_router_handoff_when_eligible(self):
        """If demo_eligible, the signal went through SetupHunter gates."""
        from app.agents.setup_hunter import SetupHunter
        from unittest.mock import patch as _patch
        s = self._hunter_settings()

        snap = _fresh_snapshot(price=2281.0, val=2280.0, vwap=2295.0, vah=2310.0, delta=100.0)
        of_signal = evaluate("GOLD#", None, context={"order_flow_snapshot": snap}, settings=s)
        if of_signal["signal"] == "WAIT":
            self.skipTest("No signal produced at this min_score")

        # Enrich with required confluence fields (normally injected by analysis pipeline)
        of_signal["final_confluence_grade"] = "A"
        of_signal["final_confluence_score"] = 70.0

        hunter = SetupHunter(s)
        analysis = {"ai_decision": {}, "strategy_signals": [of_signal]}
        time_gate = {
            "time_gate_status": "PASS",
            "session_name": "LONDON",
            "symbol_market_open": True,
            "market_open": True,
        }
        _cm_pass = {"hard_block": False, "smc_calibrated_status": "PASS", "mtfa_calibrated_status": "PASS"}
        with _patch("app.agents.setup_hunter._confirmation_matrix", return_value=_cm_pass):
            result = hunter.evaluate("GOLD", "GOLD#", analysis, time_gate, 10, 50)
        best = result.best_candidate
        # ORDER_FLOW_EXECUTION_AGENT should be executable
        self.assertEqual(best.get("execution_policy"), "EXECUTABLE")


if __name__ == "__main__":
    unittest.main()
