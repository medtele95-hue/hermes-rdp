"""Tests for HERMES Entry Gates §4.1–4.3 (app/agents/hermes_entry_gate.py).

Coverage:
  Gate A — Bias (H4 direction vs. trade direction)
  Gate B — Structure event (BOS/CHoCH vs. trade direction)
  Gate C — OB zone (order block type vs. trade direction)
  §4.3 — Order-flow confirmation (≥2 of 4 for ORDER_FLOW_NATIVE strategies)
  evaluate_entry_gates() routing for all strategy classes
  Integration with setup_hunter._failed_gates() via hermes_entry_gates_enabled flag
"""
from __future__ import annotations

import unittest

from app.agents.hermes_entry_gate import (
    check_gate_a,
    check_gate_b,
    check_gate_c,
    check_of_confirmation,
    evaluate_entry_gates,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _smc_payload(
    h4: str = "BULLISH",
    h1_break: str = "BOS_UP",
    ob: str = "BULLISH_OB",
    smc_score: float = 70.0,
    h1_trend: str = "BULLISH",
    direction: str = "BUY",
) -> dict:
    return {
        "signal": direction,
        "smc_h4_direction": h4,
        "smc_h1_trend": h1_trend,
        "smc_h1_break_structure": h1_break,
        "smc_h1_order_block": ob,
        "smc_confluence_score": smc_score,
    }


def _of_payload(
    entry: float = 1995.0,
    vwap: float = 1990.0,
    cvd_slope: float = 80.0,
    delta: float = 1000.0,
    divergence: str = "bull",
    direction: str = "BUY",
) -> dict:
    return {
        "signal": direction,
        "entry": entry,
        "order_flow": {
            "vwap": vwap,
            "cvd_slope": cvd_slope,
            "delta": delta,
            "divergence": divergence,
        },
    }


# ── Gate A ────────────────────────────────────────────────────────────────────

class TestGateA(unittest.TestCase):

    def test_pass_bullish_h4_buy(self):
        result = check_gate_a("BUY", _smc_payload(h4="BULLISH", smc_score=80))
        self.assertIsNone(result)

    def test_pass_bearish_h4_sell(self):
        result = check_gate_a("SELL", _smc_payload(h4="BEARISH", smc_score=80))
        self.assertIsNone(result)

    def test_block_bearish_h4_buy_high_score(self):
        result = check_gate_a("BUY", _smc_payload(h4="BEARISH", smc_score=70))
        self.assertEqual(result, "ENTRY_GATE_A_BIAS_AGAINST")

    def test_block_bullish_h4_sell_high_score(self):
        result = check_gate_a("SELL", _smc_payload(h4="BULLISH", smc_score=70))
        self.assertEqual(result, "ENTRY_GATE_A_BIAS_AGAINST")

    def test_pass_bearish_h4_buy_low_score_no_conviction(self):
        """Low SMC score means bias is ambiguous — don't block."""
        result = check_gate_a("BUY", _smc_payload(h4="BEARISH", smc_score=45))
        self.assertIsNone(result)

    def test_pass_range_h4_buy(self):
        """RANGE bias means no clear trend — Gate A passes."""
        result = check_gate_a("BUY", _smc_payload(h4="RANGE", smc_score=80))
        self.assertIsNone(result)

    def test_pass_unknown_h4(self):
        result = check_gate_a("BUY", _smc_payload(h4="UNKNOWN", smc_score=80))
        self.assertIsNone(result)

    def test_pass_missing_h4(self):
        result = check_gate_a("BUY", {"smc_h4_direction": None, "smc_confluence_score": 80})
        self.assertIsNone(result)

    def test_boundary_score_51_blocks(self):
        """Score of 51 (> 50) with opposing H4 → block."""
        result = check_gate_a("BUY", _smc_payload(h4="BEARISH", smc_score=51))
        self.assertEqual(result, "ENTRY_GATE_A_BIAS_AGAINST")

    def test_boundary_score_50_passes(self):
        """Score of exactly 50 (not > 50) with opposing H4 → pass."""
        result = check_gate_a("BUY", _smc_payload(h4="BEARISH", smc_score=50))
        self.assertIsNone(result)


# ── Gate B ────────────────────────────────────────────────────────────────────

class TestGateB(unittest.TestCase):

    def test_pass_bos_up_buy(self):
        result = check_gate_b("BUY", _smc_payload(h1_break="BOS_UP"))
        self.assertIsNone(result)

    def test_pass_choch_up_buy(self):
        result = check_gate_b("BUY", _smc_payload(h1_break="CHOCH_UP"))
        self.assertIsNone(result)

    def test_pass_bos_down_sell(self):
        result = check_gate_b("SELL", _smc_payload(h1_break="BOS_DOWN"))
        self.assertIsNone(result)

    def test_pass_choch_down_sell(self):
        result = check_gate_b("SELL", _smc_payload(h1_break="CHOCH_DOWN"))
        self.assertIsNone(result)

    def test_block_bos_down_buy(self):
        result = check_gate_b("BUY", _smc_payload(h1_break="BOS_DOWN"))
        self.assertEqual(result, "ENTRY_GATE_B_STRUCTURE_AGAINST")

    def test_block_choch_down_buy(self):
        result = check_gate_b("BUY", _smc_payload(h1_break="CHOCH_DOWN"))
        self.assertEqual(result, "ENTRY_GATE_B_STRUCTURE_AGAINST")

    def test_block_bos_up_sell(self):
        result = check_gate_b("SELL", _smc_payload(h1_break="BOS_UP"))
        self.assertEqual(result, "ENTRY_GATE_B_STRUCTURE_AGAINST")

    def test_block_choch_up_sell(self):
        result = check_gate_b("SELL", _smc_payload(h1_break="CHOCH_UP"))
        self.assertEqual(result, "ENTRY_GATE_B_STRUCTURE_AGAINST")

    def test_pass_none_break(self):
        """No structure event = no block."""
        result = check_gate_b("BUY", _smc_payload(h1_break="NONE"))
        self.assertIsNone(result)

    def test_pass_missing_break(self):
        result = check_gate_b("BUY", {"smc_h1_break_structure": None})
        self.assertIsNone(result)

    def test_pass_empty_break(self):
        result = check_gate_b("BUY", {})
        self.assertIsNone(result)


# ── Gate C ────────────────────────────────────────────────────────────────────

class TestGateC(unittest.TestCase):

    def test_pass_bullish_ob_buy(self):
        result = check_gate_c("BUY", _smc_payload(ob="BULLISH_OB"))
        self.assertIsNone(result)

    def test_pass_bearish_ob_sell(self):
        result = check_gate_c("SELL", _smc_payload(ob="BEARISH_OB"))
        self.assertIsNone(result)

    def test_block_bearish_ob_buy(self):
        result = check_gate_c("BUY", _smc_payload(ob="BEARISH_OB"))
        self.assertEqual(result, "ENTRY_GATE_C_OB_AGAINST")

    def test_block_bullish_ob_sell(self):
        result = check_gate_c("SELL", _smc_payload(ob="BULLISH_OB"))
        self.assertEqual(result, "ENTRY_GATE_C_OB_AGAINST")

    def test_pass_no_ob(self):
        """NONE = no active OB → pass (fail-safe)."""
        result = check_gate_c("BUY", _smc_payload(ob="NONE"))
        self.assertIsNone(result)

    def test_pass_missing_ob(self):
        result = check_gate_c("BUY", {})
        self.assertIsNone(result)


# ── §4.3 Order-flow confirmation ──────────────────────────────────────────────

class TestOfConfirmation(unittest.TestCase):

    def test_pass_all_four_confirmations_buy(self):
        """All 4 conditions → clearly ≥ 2, passes."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY",
            _of_payload(entry=1995, vwap=1990, cvd_slope=80, delta=500, divergence="bull"),
        )
        self.assertEqual(result, [])

    def test_pass_exactly_two_confirmations_buy(self):
        """Only VWAP + CVD agree → 2 confirmations, passes."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY", {
                "signal": "BUY",
                "entry": 1995.0,
                "order_flow": {
                    "vwap": 1990.0,    # price > vwap → +1
                    "cvd_slope": 80.0,  # +1
                    "delta": -100.0,    # against → +0
                    "divergence": "bear",  # against → +0
                },
            },
        )
        self.assertEqual(result, [])

    def test_block_only_one_confirmation(self):
        """Only VWAP agrees → 1 confirmation → BLOCK."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY", {
                "signal": "BUY",
                "entry": 1995.0,
                "order_flow": {
                    "vwap": 1990.0,    # +1
                    "cvd_slope": -10.0, # against → +0
                    "delta": -50.0,    # against → +0
                    "divergence": "bear",  # against → +0
                },
            },
        )
        self.assertEqual(result, ["ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT"])

    def test_block_zero_confirmations(self):
        """Nothing agrees → block."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY", {
                "signal": "BUY",
                "entry": 1980.0,
                "order_flow": {
                    "vwap": 1990.0,    # price < vwap → +0
                    "cvd_slope": -80.0, # against → +0
                    "delta": -500.0,   # against → +0
                    "divergence": "bear",  # against → +0
                },
            },
        )
        self.assertEqual(result, ["ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT"])

    def test_pass_sell_direction_all_agree(self):
        """SELL with price below VWAP + CVD negative + delta negative + bear divergence."""
        result = check_of_confirmation(
            "GOLD_LIQUIDITY_HUNTER_PRO", "SELL", {
                "signal": "SELL",
                "entry": 1980.0,
                "order_flow": {
                    "vwap": 1990.0,    # price < vwap → +1
                    "cvd_slope": -80.0, # +1
                    "delta": -500.0,   # +1
                    "divergence": "bear",  # +1
                },
            },
        )
        self.assertEqual(result, [])

    def test_pass_no_of_data_fail_safe(self):
        """No OF data → fail-safe pass (don't block on missing data)."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY",
            {"signal": "BUY", "entry": 1995.0},  # no order_flow key
        )
        self.assertEqual(result, [])

    def test_pass_empty_of_data_fail_safe(self):
        """Empty OF dict → fail-safe pass."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY",
            {"signal": "BUY", "entry": 1995.0, "order_flow": {}},
        )
        self.assertEqual(result, [])

    def test_cvd_slope_boundary_exactly_50(self):
        """CVD slope of exactly 50 (not > 50) with buy → does NOT count."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY", {
                "signal": "BUY",
                "entry": 1995.0,
                "order_flow": {
                    "vwap": 1990.0,    # price > vwap → +1
                    "cvd_slope": 50.0,  # exactly 50, not > 50 → +0
                    "delta": -50.0,    # against → +0
                    "divergence": "",
                },
            },
        )
        # Only 1 confirmation → block
        self.assertEqual(result, ["ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT"])

    def test_cvd_slope_boundary_just_over_50(self):
        """CVD slope 50.1 → counts."""
        result = check_of_confirmation(
            "ORDER_FLOW_EXECUTION_AGENT", "BUY", {
                "signal": "BUY",
                "entry": 1995.0,
                "order_flow": {
                    "vwap": 1990.0,
                    "cvd_slope": 50.1,  # +1
                    "delta": 100.0,    # +1
                    "divergence": "",
                },
            },
        )
        self.assertEqual(result, [])

    def test_of_data_via_order_flow_snapshot_key(self):
        """OF data can come from order_flow_snapshot key."""
        result = check_of_confirmation(
            "GOLD_ORDER_FLOW_CVD_VWAP", "BUY", {
                "signal": "BUY",
                "entry": 1995.0,
                "order_flow_snapshot": {
                    "vwap": 1990.0,    # +1
                    "cvd_slope": 80.0,  # +1
                    "delta": 500.0,    # +1
                    "divergence": "bull",
                },
            },
        )
        self.assertEqual(result, [])


# ── evaluate_entry_gates routing ─────────────────────────────────────────────

class TestEvaluateEntryGatesRouting(unittest.TestCase):

    def test_btc_scalping_agent_skipped(self):
        """BTC_SCALPING_AGENT has its own gate system — entry gates must not apply."""
        payload = _smc_payload(h4="BEARISH", smc_score=90, h1_break="BOS_DOWN", ob="BEARISH_OB")
        result = evaluate_entry_gates("BTC_SCALPING_AGENT", "BUY", payload)
        self.assertEqual(result, [])

    def test_wait_direction_skipped(self):
        """WAIT direction → no gate check."""
        result = evaluate_entry_gates("ORDER_FLOW_EXECUTION_AGENT", "WAIT", {})
        self.assertEqual(result, [])

    def test_of_native_uses_of_confirmation(self):
        """ORDER_FLOW_EXECUTION_AGENT routes to §4.3 OF confirmation, not Gates A/B/C."""
        # SMC data is against the direction — but for OF_NATIVE this should NOT trigger Gate A
        payload = {
            "signal": "BUY",
            "entry": 1995.0,
            "smc_h4_direction": "BEARISH",
            "smc_confluence_score": 80.0,  # would block via Gate A for non-OF
            "order_flow": {
                "vwap": 1990.0,    # +1
                "cvd_slope": 80.0,  # +1
                "delta": 500.0,    # +1
                "divergence": "bull",
            },
        }
        result = evaluate_entry_gates("ORDER_FLOW_EXECUTION_AGENT", "BUY", payload)
        # OF_NATIVE skips SMC gates → only OF confirmation matters → should pass (3/4 confirm)
        self.assertEqual(result, [])

    def test_of_native_blocked_by_of_confirmation(self):
        """ORDER_FLOW_EXECUTION_AGENT: insufficient OF confirmation → block."""
        payload = {
            "signal": "BUY",
            "entry": 1980.0,
            "smc_h4_direction": "BULLISH",  # would pass Gate A — irrelevant for OF_NATIVE
            "order_flow": {
                "vwap": 1990.0,    # price < vwap → +0
                "cvd_slope": -80.0, # against → +0
                "delta": -500.0,   # against → +0
                "divergence": "bear",  # against → +0
            },
        }
        result = evaluate_entry_gates("ORDER_FLOW_EXECUTION_AGENT", "BUY", payload)
        self.assertEqual(result, ["ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT"])

    def test_smc_native_all_gates_pass(self):
        """SIMO_ATM_BREAKOUT: all three gates pass."""
        payload = _smc_payload(
            h4="BULLISH", h1_break="BOS_UP", ob="BULLISH_OB", smc_score=80, direction="BUY"
        )
        result = evaluate_entry_gates("SIMO_ATM_BREAKOUT", "BUY", payload)
        self.assertEqual(result, [])

    def test_smc_native_gate_a_blocks(self):
        """SIMO_ATM_BREAKOUT: Gate A blocks when H4 strongly opposes direction."""
        payload = _smc_payload(
            h4="BEARISH", h1_break="BOS_UP", ob="BULLISH_OB", smc_score=70, direction="BUY"
        )
        result = evaluate_entry_gates("SIMO_ATM_BREAKOUT", "BUY", payload)
        self.assertIn("ENTRY_GATE_A_BIAS_AGAINST", result)

    def test_smc_native_gate_b_blocks(self):
        """SIMO_ATM_BREAKOUT: Gate B blocks when last break is against direction."""
        payload = _smc_payload(
            h4="BULLISH", h1_break="BOS_DOWN", ob="BULLISH_OB", smc_score=80, direction="BUY"
        )
        result = evaluate_entry_gates("SIMO_ATM_BREAKOUT", "BUY", payload)
        self.assertIn("ENTRY_GATE_B_STRUCTURE_AGAINST", result)

    def test_smc_native_gate_c_blocks(self):
        """SIMO_ATM_BREAKOUT: Gate C blocks when OB is against direction."""
        payload = _smc_payload(
            h4="BULLISH", h1_break="BOS_UP", ob="BEARISH_OB", smc_score=80, direction="BUY"
        )
        result = evaluate_entry_gates("SIMO_ATM_BREAKOUT", "BUY", payload)
        self.assertIn("ENTRY_GATE_C_OB_AGAINST", result)

    def test_default_strategy_gate_a_b_only(self):
        """Default strategy (EUR_EMA_RSI_ATR): Gates A+B apply, Gate C skipped."""
        # BEARISH_OB with BUY — would trigger Gate C for SMC_NATIVE, but not for default
        payload = _smc_payload(
            h4="BULLISH", h1_break="BOS_UP", ob="BEARISH_OB", smc_score=80, direction="BUY"
        )
        result = evaluate_entry_gates("EUR_EMA_RSI_ATR_CROSSOVER", "BUY", payload)
        # Gate C not applied for default → should pass
        self.assertEqual(result, [])

    def test_default_strategy_gate_a_blocks(self):
        """Default strategy: Gate A still blocks on opposing H4 with conviction."""
        payload = _smc_payload(
            h4="BEARISH", h1_break="BOS_UP", ob="BULLISH_OB", smc_score=70, direction="BUY"
        )
        result = evaluate_entry_gates("EUR_EMA_RSI_ATR_CROSSOVER", "BUY", payload)
        self.assertIn("ENTRY_GATE_A_BIAS_AGAINST", result)

    def test_multiple_failures_returns_all(self):
        """Multiple gate failures are all returned (not short-circuit)."""
        payload = _smc_payload(
            h4="BEARISH", h1_break="BOS_DOWN", ob="BEARISH_OB", smc_score=70, direction="BUY"
        )
        result = evaluate_entry_gates("SIMO_ATM_BREAKOUT", "BUY", payload)
        # Gates A (bearish h4 + score 70) + B (bos_down against buy) + C (bearish_ob against buy)
        self.assertIn("ENTRY_GATE_A_BIAS_AGAINST", result)
        self.assertIn("ENTRY_GATE_B_STRUCTURE_AGAINST", result)
        self.assertIn("ENTRY_GATE_C_OB_AGAINST", result)

    def test_fib_confluence_is_smc_native_gate_c_applies(self):
        """FIB_CONFLUENCE_EXECUTION_AGENT is SMC_NATIVE → Gate C applies."""
        payload = _smc_payload(
            h4="BULLISH", h1_break="BOS_UP", ob="BEARISH_OB", smc_score=80, direction="BUY"
        )
        result = evaluate_entry_gates("FIB_CONFLUENCE_EXECUTION_AGENT", "BUY", payload)
        self.assertIn("ENTRY_GATE_C_OB_AGAINST", result)

    def test_gold_of_native_routes_to_of_check(self):
        """GOLD_LIQUIDITY_HUNTER_PRO is ORDER_FLOW_NATIVE → §4.3 applies, not SMC gates."""
        payload = {
            "signal": "SELL",
            "entry": 1980.0,
            "smc_h4_direction": "BULLISH",  # against SELL — would block via Gate A
            "smc_confluence_score": 80.0,
            "order_flow": {
                "vwap": 1990.0,    # price < vwap → +1 for SELL
                "cvd_slope": -80.0, # +1 for SELL
                "delta": -500.0,   # +1 for SELL
                "divergence": "bear",  # +1 for SELL
            },
        }
        result = evaluate_entry_gates("GOLD_LIQUIDITY_HUNTER_PRO", "SELL", payload)
        # OF_NATIVE: Gate A bypassed, OF confirmation gives 4/4 → pass
        self.assertEqual(result, [])


# ── Integration: setup_hunter._failed_gates wiring ───────────────────────────

class TestSetupHunterIntegration(unittest.TestCase):
    """Verify that hermes_entry_gates_enabled flag controls gate wiring in setup_hunter."""

    def _make_settings(self, gates_enabled: bool) -> object:
        """Return a minimal settings object with hermes_entry_gates_enabled."""
        class _S:
            hermes_entry_gates_enabled = gates_enabled
            # setup_hunter also reads these:
            demo_ignore_time_blocks = False
            hermes_quant_min_score = 80.0
            hermes_quant_min_rr = 1.5
            hermes_quant_pro_min_score = 80.0
            hermes_quant_pro_min_rr = 1.5
            gold_min_liquidity_score = 70.0
            gold_min_rr = 1.5
            gold_m1m5_min_score_strict = 75
            gold_m1m5_min_score_relaxed = 65
            gold_order_flow_execution_enabled = False
            gold_order_flow_min_confidence = 70
            order_flow_execution_enabled = False
            order_flow_min_score = 75
            order_flow_min_rr = 1.5
            btc_scalping_min_confidence = 75
            btc_disable_quant_statistical_pullback = False
            new_strategies_min_score = 70
            hermes_strategy_pack_enabled = False
            simo_atm_breakout_enabled = False
            allow_time_block_override = False
        return _S()

    def test_gates_disabled_no_block(self):
        """When hermes_entry_gates_enabled=False, entry gates are not applied."""
        from app.agents.setup_hunter import _failed_gates
        settings = self._make_settings(gates_enabled=False)
        # Payload that would fail Gate A (bearish H4, score 70, BUY signal)
        payload = {
            "signal": "BUY",
            "strategy": "SIMO_ATM_BREAKOUT",
            "smc_h4_direction": "BEARISH",
            "smc_h1_break_structure": "NONE",
            "smc_h1_order_block": "NONE",
            "smc_confluence_score": 70.0,
            "mtfa_score": 50.0,
            "session_name": "LONDON",
            "time_gate_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
            "risk_reward": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
            "big_setup_grade": "B",
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
        }
        result = _failed_gates("ENTRY", payload, 0.5, 1.0, settings)
        # Entry gates disabled → no gate-specific failure
        self.assertNotIn("ENTRY_GATE_A_BIAS_AGAINST", result)

    def test_gates_enabled_blocks_smc_native_gate_a(self):
        """When hermes_entry_gates_enabled=True, Gate A blocks SMC_NATIVE on opposing H4."""
        from app.agents.setup_hunter import _failed_gates
        settings = self._make_settings(gates_enabled=True)
        payload = {
            "signal": "BUY",
            "strategy": "SIMO_ATM_BREAKOUT",
            "smc_h4_direction": "BEARISH",
            "smc_h1_break_structure": "NONE",
            "smc_h1_order_block": "NONE",
            "smc_confluence_score": 70.0,
            "mtfa_score": 50.0,
            "session_name": "LONDON",
            "time_gate_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
            "risk_reward": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "safety_guard_status": "PASS",
            "big_setup_grade": "B",
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
        }
        result = _failed_gates("ENTRY", payload, 0.5, 1.0, settings)
        self.assertIn("ENTRY_GATE_A_BIAS_AGAINST", result)

    def test_gates_enabled_of_native_passes_with_good_of(self):
        """ORDER_FLOW_EXECUTION_AGENT with good OF data passes even with opposing SMC."""
        from app.agents.setup_hunter import _failed_gates
        settings = self._make_settings(gates_enabled=True)
        payload = {
            "signal": "BUY",
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "smc_h4_direction": "BEARISH",  # opposing — but irrelevant for OF_NATIVE
            "smc_confluence_score": 80.0,
            "smc_h1_break_structure": "BOS_DOWN",  # opposing — irrelevant for OF_NATIVE
            "smc_h1_order_block": "NONE",
            "mtfa_score": 50.0,
            "session_name": "LONDON",
            "time_gate_status": "PASS",
            "symbol_market_open": True,
            "market_open": True,
            "risk_reward": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "m15_confirmation_status": "PASS",
            "m1_trigger_status": "PASS",
            "safety_guard_status": "PASS",
            "big_setup_grade": "B",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "entry": 1.1050,
            "order_flow": {
                "vwap": 1.1040,   # price > vwap → +1
                "cvd_slope": 75.0, # +1
                "delta": 300.0,   # +1
                "divergence": "bull",
            },
            "order_flow_execution_agent_score": 80,
            "order_flow_execution_agent_signal": "BUY",
            "entry": 1.1050,
            "sl": 1.1020,
            "tp": 1.1110,
        }
        result = _failed_gates("ENTRY", payload, 0.5, 1.0, settings)
        # Gate A/B not applied for OF_NATIVE; OF confirmation passes
        self.assertNotIn("ENTRY_GATE_A_BIAS_AGAINST", result)
        self.assertNotIn("ENTRY_GATE_B_STRUCTURE_AGAINST", result)
        self.assertNotIn("ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT", result)


if __name__ == "__main__":
    unittest.main()
