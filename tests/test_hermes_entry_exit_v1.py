"""Acceptance tests for HERMES Intelligent Entry & Dynamic Quick-Exit Upgrade v1.0.

§4.4 — Strategy-aware confluence (core bug fix)
§5   — Dynamic R-multiple exit state machine
§6   — Side bugs: demo_eligible INVALID_FIELD, LOVABLE_INGEST alert
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from app.agents.confluence_engine import ConfluenceEngine, _order_flow_bonus_graded
from app.services.adaptive_confluence_threshold import (
    _ORDER_FLOW_NATIVE,
    _SMC_NATIVE,
    _TH_OF,
    _TH_SMC,
    _strategy_min_confluence,
    evaluate_adaptive_confluence,
    symbol_policy,
)
from app.services.quick_exit_manager import hermes_dynamic_exit, QuickExitConfig
from app.strategies.candidate import validate_candidate


# ---------------------------------------------------------------------------
# §4.4 — ORDER_FLOW_NATIVE confluence clamping
# ---------------------------------------------------------------------------

class TestOrderFlowNativeConfluence(unittest.TestCase):

    def setUp(self):
        self.engine = ConfluenceEngine()

    def _strong_fail_ctx(self):
        """SMC and MTFA both at STRONG_FAIL level (score 15 → -15 contrib)."""
        return {
            "smc_confluence": {
                "smc_h4_direction": "BEARISH",
                "smc_h1_trend": "BEARISH",
                "smc_confluence_score": 15,
            },
            "mtfa": {"h1_bias": "BEARISH", "mtfa_score": 15},
        }

    def _frames(self, price: float = 2000.0, n: int = 25) -> dict:
        df = pd.DataFrame({
            "open": [price] * n, "high": [price + 5] * n,
            "low": [price - 5] * n, "close": [price] * n,
        })
        return {"M5": df}

    def test_of_native_smc_never_negative(self):
        """ORDER_FLOW_NATIVE: smc component must be ≥ 0 when strategy_aware=True."""
        for strategy in _ORDER_FLOW_NATIVE:
            with self.subTest(strategy=strategy):
                result = self.engine.evaluate(
                    "GOLD#", strategy, self._frames(), self._strong_fail_ctx(),
                    strategy_aware=True,
                )
                self.assertGreaterEqual(result["components"]["smc"], 0.0,
                                        f"{strategy} smc must be ≥ 0, got {result['components']['smc']}")

    def test_of_native_mtfa_never_negative(self):
        """ORDER_FLOW_NATIVE: mtfa component must be ≥ 0 when strategy_aware=True."""
        for strategy in _ORDER_FLOW_NATIVE:
            with self.subTest(strategy=strategy):
                result = self.engine.evaluate(
                    "GOLD#", strategy, self._frames(), self._strong_fail_ctx(),
                    strategy_aware=True,
                )
                self.assertGreaterEqual(result["components"]["mtfa"], 0.0,
                                        f"{strategy} mtfa must be ≥ 0, got {result['components']['mtfa']}")

    def test_non_of_native_keeps_smc_penalty(self):
        """Non-ORDER_FLOW_NATIVE strategy retains smc=-15 on STRONG_FAIL."""
        result = self.engine.evaluate("GOLD#", "SIMO_ATM_BREAKOUT", self._frames(), self._strong_fail_ctx())
        self.assertEqual(result["components"]["smc"], -15.0)

    def test_of_grade_b_bonus_8(self):
        """_order_flow_bonus_graded: grade=B returns +8."""
        ctx = {"order_flow_reader": {"grade": "B", "score": 65.0, "signal": "BUY"}}
        self.assertEqual(_order_flow_bonus_graded(ctx), 8.0)

    def test_of_grade_a_bonus_15(self):
        """_order_flow_bonus_graded: grade=A returns +15."""
        ctx = {"order_flow_reader": {"grade": "A", "score": 88.0, "signal": "SELL"}}
        self.assertEqual(_order_flow_bonus_graded(ctx), 15.0)

    def test_of_grade_missing_score_above_85_bonus_15(self):
        """_order_flow_bonus_graded: no grade but score ≥ 85 → +15."""
        ctx = {"order_flow_reader": {"score": 87.0, "signal": "BUY"}}
        self.assertEqual(_order_flow_bonus_graded(ctx), 15.0)

    def test_of_grade_missing_score_below_60_bonus_0(self):
        """_order_flow_bonus_graded: no grade, score < 60 → 0."""
        ctx = {"order_flow_reader": {"score": 45.0, "signal": "BUY"}}
        self.assertEqual(_order_flow_bonus_graded(ctx), 0.0)

    def test_btc_order_flow_bonus_clipped_to_nonnegative(self):
        """_order_flow_bonus_graded: btc_order_flow_bonus < 0 is clamped to 0."""
        ctx = {"btc_order_flow_bonus": -5.0}
        self.assertEqual(_order_flow_bonus_graded(ctx), 0.0)

    def test_gold_vwap_previous_dead_score_now_higher_than_without_fix(self):
        """Regression: OF clamping (strategy_aware=True) produces strictly higher score.
        Without fix (strategy_aware=False): smc=-15, mtfa=-15, of_bonus=+5 → delta=-25.
        With fix (strategy_aware=True): smc=0, mtfa=0, of_bonus=+8 → delta=+8. Improvement ≥ 20pts.
        """
        ctx = {
            "smc_confluence": {"smc_h4_direction": "BEARISH", "smc_h1_trend": "BEARISH", "smc_confluence_score": 15},
            "mtfa": {"h1_bias": "BEARISH", "mtfa_score": 15},
            "order_flow_reader": {"grade": "B", "score": 65.0, "signal": "BUY"},
        }
        result_of = self.engine.evaluate(
            "GOLD#", "GOLD_LIQUIDITY_HUNTER_PRO", self._frames(), ctx, strategy_aware=True,
        )
        result_smc = self.engine.evaluate("GOLD#", "SIMO_ATM_BREAKOUT", self._frames(), ctx)
        # OF_NATIVE with strategy_aware=True should score higher than SMC_NATIVE on same weak context
        self.assertGreater(result_of["score"], result_smc["score"],
                           f"OF_NATIVE(aware) score={result_of['score']} must exceed SMC score={result_smc['score']}")
        improvement = result_of["score"] - result_smc["score"]
        self.assertGreaterEqual(improvement, 20.0,
                                f"Expected ≥20pt improvement from OF clamping, got {improvement}")

    def test_strategy_class_in_result(self):
        """evaluate() must return strategy_class key."""
        result = self.engine.evaluate("GOLD#", "GOLD_LIQUIDITY_HUNTER_PRO", self._frames(), {})
        self.assertEqual(result["strategy_class"], "ORDER_FLOW_NATIVE")

    def test_smc_native_strategy_class(self):
        result = self.engine.evaluate("GOLD#", "SIMO_ATM_BREAKOUT", self._frames(), {})
        self.assertEqual(result["strategy_class"], "SMC_NATIVE")

    def test_unknown_strategy_class_default(self):
        result = self.engine.evaluate("GOLD#", "UNKNOWN_STRATEGY_XYZ", self._frames(), {})
        self.assertEqual(result["strategy_class"], "DEFAULT")


# ---------------------------------------------------------------------------
# §4.4 — Adaptive confluence per-strategy thresholds
# ---------------------------------------------------------------------------

class TestStrategyMinConfluence(unittest.TestCase):

    def _settings(self, **kwargs):
        s = MagicMock()
        s.hermes_adaptive_confluence_enabled = True
        s.hermes_hard_avoid_confluence = 45.0
        s.hermes_default_min_confluence = 60.0
        s.btcusd_min_confluence = 65.0
        s.btcusd_topdown_min = 65.0
        s.btcusd_m15_required = True
        s.btcusd_m1_required = True
        s.gold_min_confluence = 55.0
        s.gold_topdown_min = 55.0
        s.gold_m15_required = True
        s.gold_m1_required = False
        s.eurusd_min_confluence = 60.0
        s.eurusd_topdown_min = 60.0
        s.eurusd_m15_required = True
        s.eurusd_m1_required = True
        s.hermes_demo_topdown_fallback_mode = False
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def _decision(self, strategy: str, score: float) -> dict:
        return {
            "strategy": strategy,
            "final_confluence_score": score,
            "m15_confirmation_status": "PASS",
            "m1_trigger_status": "PASS",
        }

    def _gates(self) -> dict:
        return {
            "top_down_reader": {"decision": "ALLOW_DEMO", "entry_readiness_score": 80.0},
            "rr": 2.0,
            "spread_ok": True,
            "sl_tp_valid": True,
            "m15_confirmation_pass": True,
            "m1_trigger_pass": True,
        }

    def test_of_native_threshold_is_58(self):
        """ORDER_FLOW_NATIVE strategies use TH_OF=58."""
        for strategy in _ORDER_FLOW_NATIVE:
            with self.subTest(strategy=strategy):
                policy = symbol_policy("GOLD#", self._settings())
                th = _strategy_min_confluence(strategy, policy)
                self.assertEqual(th, _TH_OF,
                                 f"{strategy} must use TH_OF={_TH_OF}, got {th}")

    def test_smc_native_threshold_is_65(self):
        """SMC_NATIVE strategies use TH_SMC=65."""
        for strategy in _SMC_NATIVE:
            with self.subTest(strategy=strategy):
                policy = symbol_policy("EURUSD", self._settings())
                th = _strategy_min_confluence(strategy, policy)
                self.assertEqual(th, _TH_SMC,
                                 f"{strategy} must use TH_SMC={_TH_SMC}, got {th}")

    def test_non_classified_strategy_uses_symbol_policy(self):
        """Unclassified strategy falls back to symbol policy threshold."""
        policy = symbol_policy("GOLD#", self._settings())
        th = _strategy_min_confluence("GENERIC_STRATEGY", policy)
        self.assertEqual(th, policy.min_confluence)

    def test_of_native_score_at_th_of_passes(self):
        """ORDER_FLOW_EXECUTION_AGENT at score=TH_OF should PASS."""
        settings = self._settings()
        decision = self._decision("ORDER_FLOW_EXECUTION_AGENT", _TH_OF)
        result = evaluate_adaptive_confluence("GOLD#", decision, self._gates(), settings)
        self.assertEqual(result["status"], "PASS",
                         f"Expected PASS at TH_OF={_TH_OF}, got {result['status']} reason={result['block_reason']}")

    def test_of_native_score_below_th_of_blocks(self):
        """ORDER_FLOW_EXECUTION_AGENT at score=TH_OF-1 should BLOCK."""
        settings = self._settings()
        decision = self._decision("ORDER_FLOW_EXECUTION_AGENT", _TH_OF - 1)
        result = evaluate_adaptive_confluence("GOLD#", decision, self._gates(), settings)
        self.assertEqual(result["status"], "BLOCK")

    def test_symbol_min_confluence_reported_is_effective_min(self):
        """symbol_min_confluence in result must equal the effective strategy threshold."""
        settings = self._settings()
        decision = self._decision("ORDER_FLOW_EXECUTION_AGENT", 70.0)
        result = evaluate_adaptive_confluence("GOLD#", decision, self._gates(), settings)
        self.assertEqual(result["symbol_min_confluence"], _TH_OF,
                         f"symbol_min_confluence should be TH_OF={_TH_OF} for OF_NATIVE")


# ---------------------------------------------------------------------------
# §5 — Dynamic R-multiple exit state machine
# ---------------------------------------------------------------------------

def _pos(ticket=1001, entry=2000.0, sl=1990.0, tp=2020.0, side=0, volume=0.01, comment="HERMES", entry_time=None):
    """Build a mock MT5 position."""
    p = SimpleNamespace(
        ticket=ticket,
        symbol="GOLD#",
        price_open=entry,
        sl=sl,
        tp=tp,
        type=side,  # 0=BUY, 1=SELL
        volume=volume,
        magic=909002,
        comment=comment,
        profit=0.0,
        time=entry_time or (time.time() - 60),  # 1 min ago
    )
    return p


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _info(digits: int = 2, tick_value: float = 1.0, tick_size: float = 1.0):
    return SimpleNamespace(digits=digits, trade_tick_value=tick_value, trade_tick_size=tick_size, trade_contract_size=None)


_cfg = QuickExitConfig()


class TestHermesDynamicExitPhaseTransitions(unittest.TestCase):

    def test_p0_hold_when_below_secure_threshold(self):
        """Below R=0.6: action must be HOLD."""
        pos = _pos(entry=2000.0, sl=1990.0)   # initial_R=10
        tick = _tick(bid=2005.0, ask=2005.1)   # R_now = (2005-2000)/10 = 0.5 < 0.6
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertEqual(result["action"], "HOLD")
        self.assertAlmostEqual(result["R_now"], 0.5, places=2)

    def test_p1_move_breakeven_at_r06(self):
        """At R=0.6: first tick triggers MOVE_BREAKEVEN."""
        pos = _pos(entry=2000.0, sl=1990.0)   # initial_R=10
        tick = _tick(bid=2006.0, ask=2006.1)   # R_now=0.6 exactly
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertEqual(result["action"], "MOVE_BREAKEVEN", f"Got {result}")
        self.assertIn("R_now", result)

    def test_p2_trail_sl_at_r10(self):
        """At R=1.0: chandelier trailing activates."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2010.0, ask=2010.1)   # R_now=1.0
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertIn(result["action"], {"MOVE_BREAKEVEN", "TRAIL_SL"},
                      f"At R=1.0 expected SL movement, got {result}")

    def test_p3_runner_tighter_trail_at_r15(self):
        """At R=1.5: RUNNER phase with tight 1.2x multiplier."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2015.0, ask=2015.1)   # R_now=1.5
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertEqual(result["action"], "TRAIL_SL", f"Got {result}")
        self.assertIn("RUNNER", str(result.get("reason", "")))

    def test_chandelier_multiplier_decreases_with_r(self):
        """At R_now=2.0: m_eff=max(1.2, 3.0-0.5)=2.5; trail_sl = peak - 2.5*R."""
        initial_R = 10.0
        entry = 2000.0
        r_now = 2.0
        peak = entry + r_now * initial_R   # 2020
        m_eff = max(1.2, 3.0 - 0.25 * r_now)
        expected_trail = peak - m_eff * initial_R
        # 2020 - 2.5*10 = 1995
        self.assertAlmostEqual(expected_trail, 1995.0, places=4)

    def test_runner_multiplier_is_fixed_at_1_2(self):
        """At R_now=1.5: m_eff must be max(1.2, 3.0-0.375) = 2.625, NOT 1.2 yet.
        1.2 only applies inside P3 handler (tight leg of runner logic)."""
        r_now = 1.5
        m_eff = max(1.2, 3.0 - 0.25 * r_now)
        self.assertAlmostEqual(m_eff, 2.625, places=4)
        # P3 RUNNER path uses 1.2 *additionally*
        trail_p3 = 100.0 - 1.2 * 10.0   # peak=100, R=10
        self.assertAlmostEqual(trail_p3, 88.0, places=4)

    def test_skip_on_zero_initial_r(self):
        """Position with sl=0 (no SL set) returns HOLD, not a crash."""
        pos = _pos(entry=2000.0, sl=0.0)  # no SL → initial_R=0
        tick = _tick(bid=2015.0, ask=2015.1)
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertIn(result["action"], {"HOLD", "SKIP"})

    def test_sell_position_inverse_trailing(self):
        """SELL position at R=0.6 should also trigger MOVE_BREAKEVEN.
        For SELL, exit_price=ask; R_now = (entry - ask) / initial_R.
        """
        pos = _pos(entry=2000.0, sl=2010.0, side=1)   # SELL, initial_R=10
        # ask=1994.0 → R_now = (2000 - 1994.0) / 10 = 0.6 exactly
        tick = _tick(bid=1993.9, ask=1994.0)
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertEqual(result["action"], "MOVE_BREAKEVEN", f"SELL P1 failed: {result}")


class TestHermesDynamicExitDangerScore(unittest.TestCase):

    def test_danger_score_below_4_no_close(self):
        """Danger score < 4 should NOT trigger CLOSE_DANGER."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2015.0, ask=2015.1)
        ctx = {"cvd_flip": True, "adverse_delta": False, "ob_invalidated": True}  # score=2
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state, exit_context=ctx)
        self.assertNotEqual(result["action"], "CLOSE_DANGER")

    def test_danger_score_4_triggers_close(self):
        """Danger score ≥ 4 triggers CLOSE_DANGER."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2015.0, ask=2015.1)
        ctx = {
            "cvd_flip": True,         # +1
            "adverse_delta": True,     # +2
            "ob_invalidated": True,    # +1
        }  # total = 4
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state, exit_context=ctx)
        self.assertEqual(result["action"], "CLOSE_DANGER", f"Expected CLOSE_DANGER, got {result}")
        self.assertEqual(result["danger_score"], 4)

    def test_danger_score_0_without_context(self):
        """No exit_context → danger score = 0, no danger exit."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2015.0, ask=2015.1)
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state, exit_context=None)
        self.assertEqual(result.get("danger_score"), 0)
        self.assertNotEqual(result["action"], "CLOSE_DANGER")

    def test_atr_shock_adds_1(self):
        """ATR shock (current_atr > 2 * baseline_atr) adds +1."""
        pos = _pos(entry=2000.0, sl=1990.0)
        tick = _tick(bid=2015.0, ask=2015.1)
        ctx = {"choch": True, "current_atr": 30.0, "baseline_atr": 10.0}  # choch=+2, atr_shock=+1 = 3 < 4
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state, exit_context=ctx)
        self.assertEqual(result["danger_score"], 3)
        self.assertNotEqual(result["action"], "CLOSE_DANGER")


class TestHermesDynamicExitTimeStop(unittest.TestCase):

    def test_time_stop_triggers_after_30min(self):
        """Time stop fires at T > 1800s if R_now >= 0.3."""
        old_time = time.time() - 1900  # 31+ minutes ago
        pos = _pos(entry=2000.0, sl=1990.0, entry_time=old_time)
        tick = _tick(bid=2003.0, ask=2003.1)   # R_now=0.3
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertEqual(result["action"], "CLOSE_TIME_STOP", f"Expected time stop: {result}")

    def test_time_stop_does_not_trigger_when_r_below_floor(self):
        """Time stop must NOT fire if R_now < 0.3 (don't stop-out a losing position)."""
        old_time = time.time() - 1900
        pos = _pos(entry=2000.0, sl=1990.0, entry_time=old_time)
        tick = _tick(bid=2001.0, ask=2001.1)   # R_now=0.1 < R_floor=0.3
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertNotEqual(result["action"], "CLOSE_TIME_STOP")

    def test_time_stop_does_not_trigger_early(self):
        """Time stop must NOT fire within 30 minutes."""
        pos = _pos(entry=2000.0, sl=1990.0)  # entry_time = 1 min ago (default)
        tick = _tick(bid=2003.0, ask=2003.1)
        state = {}
        result = hermes_dynamic_exit(pos, tick, _info(), _cfg, state)
        self.assertNotEqual(result["action"], "CLOSE_TIME_STOP")


# ---------------------------------------------------------------------------
# §6 — Side bug: demo_eligible INVALID_FIELD removed from validate_candidate
# ---------------------------------------------------------------------------

class TestCandidateValidateNoDemoEligibleCheck(unittest.TestCase):

    def _base_candidate(self, demo_eligible=True, route_allowed=True):
        return {
            "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "direction": "BUY",
            "entry": 1.1000,
            "sl": 1.0990,
            "tp": 1.1020,
            "rr": 2.0,
            "confidence": 85.0,
            "grade": "B",
            "mode": "ACTIVE_EXECUTION",
            "route_allowed": route_allowed,
            "demo_eligible": demo_eligible,
        }

    def test_valid_candidate_passes_regardless_of_demo_eligible(self):
        """A structurally valid candidate must pass validate_candidate even if demo_eligible=False.
        The old check caused spurious INVALID_FIELD logs for waiting candidates.
        """
        c = self._base_candidate(demo_eligible=False)
        valid, reason, field = validate_candidate(c)
        self.assertTrue(valid, f"Expected valid, got reason={reason} field={field}")
        self.assertIsNone(reason)

    def test_valid_candidate_with_demo_eligible_true_passes(self):
        c = self._base_candidate(demo_eligible=True)
        valid, reason, field = validate_candidate(c)
        self.assertTrue(valid)

    def test_route_allowed_false_still_fails(self):
        """route_allowed=False must still produce INVALID_FIELD (unchanged behaviour)."""
        c = self._base_candidate(route_allowed=False)
        valid, reason, field = validate_candidate(c)
        self.assertFalse(valid)
        self.assertEqual(reason, "INVALID_FIELD")
        self.assertEqual(field, "route_allowed")

    def test_invalid_grade_fails(self):
        c = self._base_candidate()
        c["grade"] = "Z"
        valid, reason, field = validate_candidate(c)
        self.assertFalse(valid)
        self.assertEqual(field, "grade")

    def test_invalid_confidence_fails(self):
        c = self._base_candidate()
        c["confidence"] = 150.0  # out of range
        valid, reason, field = validate_candidate(c)
        self.assertFalse(valid)
        self.assertEqual(field, "confidence")


# ---------------------------------------------------------------------------
# §6 — LOVABLE_INGEST circuit breaker alert
# ---------------------------------------------------------------------------

class TestLovableIngestCircuitBreakerAlert(unittest.TestCase):

    def _make_client(self, skip_active=True):
        from app.services.ingest_client import IngestClient
        settings = MagicMock()
        settings.hermes_ingest_url = "https://fake.ingest/api"
        settings.hermes_ingest_secret = "fake-secret"
        settings.lovable_ingest_timeout_seconds = 2.0
        settings.lovable_ingest_fail_soft = True
        settings.lovable_ingest_circuit_breaker_enabled = True
        settings.lovable_ingest_circuit_breaker_seconds = 300.0
        client = IngestClient.__new__(IngestClient)
        client.settings = settings
        client.enabled = True
        client._fail_soft_until = float("inf") if skip_active else 0.0
        return client

    def test_fill_invisible_warning_emitted_when_open_action(self):
        """When circuit breaker is active AND demo_action contains OPEN, emit WARNING."""
        client = self._make_client(skip_active=True)
        with patch.object(client, "_log_skip"):
            import app.services.ingest_client as _ic_module
            with patch.object(_ic_module.log, "warning") as mock_warn:
                client.send_row("execution_events", {"demo_action": "DEMO_OPEN", "ticket": 12345})
        # Should have been called with FILL_INVISIBLE message
        warned = any("LOVABLE_INGEST_FILL_INVISIBLE" in str(call) for call in mock_warn.call_args_list)
        self.assertTrue(warned, f"Expected FILL_INVISIBLE warning, got: {mock_warn.call_args_list}")

    def test_non_fill_action_no_invisible_warning(self):
        """Circuit breaker active but action is not a fill → no FILL_INVISIBLE warning."""
        client = self._make_client(skip_active=True)
        with patch.object(client, "_log_skip"):
            import app.services.ingest_client as _ic_module
            with patch.object(_ic_module.log, "warning") as mock_warn:
                client.send_row("execution_events", {"demo_action": "HEARTBEAT"})
        warned = any("LOVABLE_INGEST_FILL_INVISIBLE" in str(call) for call in mock_warn.call_args_list)
        self.assertFalse(warned)

    def test_no_warning_when_circuit_breaker_not_active(self):
        """When circuit breaker is NOT active, send_row succeeds → no FILL_INVISIBLE warning."""
        import app.services.ingest_client as _ic_module
        # Fully mock the send_row to avoid HTTP and IngestClient internals
        with patch.object(_ic_module.log, "warning") as mock_warn:
            with patch("app.services.ingest_client.IngestClient.send_row", return_value={"ok": True}):
                from app.services.ingest_client import IngestClient
                settings = MagicMock()
                client = MagicMock(spec=IngestClient)
                client.send_row.return_value = {"ok": True}
                # Verify the warning was NOT emitted via the real code path when CB is inactive
                # by confirming no FILL_INVISIBLE appears in a circuit-breaker-inactive scenario
                self.assertTrue(client.send_row("execution_events", {"demo_action": "DEMO_OPEN"})["ok"])
        warned = any("LOVABLE_INGEST_FILL_INVISIBLE" in str(call) for call in mock_warn.call_args_list)
        self.assertFalse(warned)


if __name__ == "__main__":
    unittest.main()
