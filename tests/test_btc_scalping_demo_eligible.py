"""Tests for Intelligence Upgrade v5 — BTC_SCALPING_AGENT demo_eligible fix.

Covers:
- LOVABLE_BTC active + safety=PASS + confidence<75 → BTC_SCALPING_AGENT demo_eligible=True
  (BTC_SCALPING_CONFIDENCE_BELOW_MIN gate skipped by _btc_lovable_bypass)
- LOVABLE_BTC active + safety=BLOCK + confidence<75 → demo_eligible=False
  (bypass requires safety=PASS; gate still applied)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.agents.setup_hunter import SetupHunter


def _lovable_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.demo_only = True
    s.allow_live_trading = False
    s.demo_max_lot = 0.01
    s.demo_ignore_all_time_blocks = True
    s.safety_guard_enabled = False
    s.btc_scalping_min_confidence = 55
    s.btc_disable_quant_statistical_pullback = False
    s.hermes_execution_profile = "LOVABLE_BTC_OLD_SYSTEM"
    s.old_btc_max_open_positions = 1
    s.gold_liquidity_strategy_enabled = False
    s.gold_liquidity_trade_enabled = False
    s.gold_order_flow_execution_enabled = False
    s.order_flow_execution_enabled = False
    s.order_flow_min_score = 75
    s.order_flow_min_rr = 1.5
    s.order_flow_cooldown_minutes = 15
    s.order_flow_allowed_symbols = "BTCUSD,BTCUSD#"
    s.gold_min_liquidity_score = 75
    s.gold_min_rr = 2.0
    s.gold_m1m5_min_score_strict = 75
    s.gold_m1m5_min_score_relaxed = 65
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


def _btc_scalping_signal(confidence: float = 55.0, safety: str = "PASS") -> dict:
    """BTC_SCALPING_AGENT signal with low confidence (below 75 hard floor)."""
    return {
        "strategy": "BTC_SCALPING_AGENT",
        "setup_type": "BTC_SCALPING_AGENT",
        "symbol": "BTCUSD",
        "broker_symbol": "BTCUSD#",
        "signal": "BUY",
        "direction": "BUY",
        "confidence": confidence,
        "entry": 65000.0,
        "sl": 64000.0,
        "tp": 67000.0,
        "risk_reward": 2.0,
        "reward_risk": 2.0,
        "safety_guard_status": safety,
        "safety_guard_reason": "ALL_CLEAR" if safety == "PASS" else "OPEN_POSITIONS_EXCEEDED",
        "smc_confluence_status": "FAIL",
        "smc_confluence_score": 15,
        "mtfa_status": "FAIL",
        "mtfa_score": 15,
        "symbol_market_open": True,
        "market_open": True,
    }


class TestBtcScalpingDemoEligible(unittest.TestCase):

    def _run(self, signals: list, settings=None) -> dict:
        hunter = SetupHunter(settings or _lovable_settings())
        analysis = {"ai_decision": {}, "strategy_signals": signals}
        result = hunter.evaluate("BTCUSD", "BTCUSD#", analysis, _time_gate(), 10, 100)
        return result.best_candidate or {}

    def test_lovable_bypass_makes_btc_scalping_demo_eligible(self) -> None:
        """LOVABLE_BTC + safety=PASS + confidence=55 → demo_eligible=True.

        Before the fix, BTC_SCALPING_CONFIDENCE_BELOW_MIN was always added when
        confidence < 75, blocking demo_eligible. With the bypass, confidence gate
        is skipped when LOVABLE_BTC is active and safety=PASS.
        """
        sig = _btc_scalping_signal(confidence=55.0, safety="PASS")
        best = self._run([sig])
        self.assertEqual(best.get("best_strategy"), "BTC_SCALPING_AGENT",
                         f"Expected BTC_SCALPING_AGENT to be best, got {best.get('best_strategy')}")
        self.assertTrue(
            best.get("demo_eligible"),
            f"Expected demo_eligible=True with lovable bypass + safety=PASS, "
            f"got demo_eligible={best.get('demo_eligible')}, "
            f"failed_gates={best.get('failed_gates')}",
        )
        self.assertNotIn(
            "BTC_SCALPING_CONFIDENCE_BELOW_MIN",
            best.get("failed_gates") or [],
            "BTC_SCALPING_CONFIDENCE_BELOW_MIN must be absent when bypass is active",
        )

    def test_lovable_bypass_requires_safety_pass(self) -> None:
        """LOVABLE_BTC active but safety=BLOCK → bypass does NOT apply → demo_eligible=False."""
        sig = _btc_scalping_signal(confidence=55.0, safety="BLOCK")
        best = self._run([sig])
        self.assertFalse(
            best.get("demo_eligible"),
            f"demo_eligible must be False when safety=BLOCK (bypass requires PASS), "
            f"got demo_eligible={best.get('demo_eligible')}",
        )
        self.assertIn(
            "BTC_SCALPING_CONFIDENCE_BELOW_MIN",
            best.get("failed_gates") or [],
            "BTC_SCALPING_CONFIDENCE_BELOW_MIN must appear when bypass is inactive",
        )


if __name__ == "__main__":
    unittest.main()
