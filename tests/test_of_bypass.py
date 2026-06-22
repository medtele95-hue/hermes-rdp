from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.agents.setup_hunter import SetupHunter


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.demo_only = True
    settings.allow_live_trading = False
    settings.demo_max_lot = 0.01
    settings.safety_guard_enabled = False
    settings.order_flow_execution_enabled = True
    settings.order_flow_min_score = 75
    settings.order_flow_min_rr = 1.5
    settings.order_flow_allowed_symbols = "BTCUSD,BTCUSD#"
    settings.hermes_strategy_pack_enabled = False
    settings.multi_tf_momentum_enabled = False
    settings.hermes_adaptive_confluence_enabled = False
    settings.hermes_entry_gates_enabled = False
    settings.hermes_setup_tier_enabled = False
    settings.demo_ignore_all_time_blocks = True
    settings.gold_order_flow_execution_enabled = False
    settings.gold_liquidity_strategy_enabled = False
    settings.btc_scalping_min_confidence = 75
    settings.btc_disable_quant_statistical_pullback = False
    settings.hermes_quant_min_score = 75
    settings.hermes_quant_min_rr = 2.0
    settings.hermes_quant_pro_min_score = 75
    settings.hermes_quant_pro_min_rr = 2.0
    settings.gold_min_liquidity_score = 75
    settings.gold_min_rr = 2.0
    settings.gold_m1m5_min_score_strict = 75
    settings.gold_m1m5_min_score_relaxed = 65
    settings.report_timezone = "UTC"
    settings.geometric_mode = "SHADOW"
    return settings


def _signal(*, smc: float = 75, mtfa: float = 65) -> dict:
    return {
        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
        "symbol": "BTCUSD",
        "signal": "BUY",
        "order_flow_execution_agent_score": 95,
        "confidence": 95,
        "risk_reward": 1.5,
        "entry": 100,
        "sl": 99,
        "tp": 101.5,
        "safety_guard_status": "PASS",
        "smc_confluence_score": smc,
        "mtfa_score": mtfa,
        "final_confluence_grade": "A",
        "final_confluence_score": 40,
        "symbol_market_open": True,
    }


def _evaluate(signal: dict):
    hunter = SetupHunter(_settings())
    with patch("app.agents.setup_hunter._confirmation_matrix", wraps=__import__("app.agents.confirmation_matrix", fromlist=["evaluate"]).evaluate):
        return hunter.evaluate(
            "BTCUSD", "BTCUSD#", {"ai_decision": {}, "strategy_signals": [signal]},
            {"time_gate_status": "PASS", "market_open": True, "symbol_market_open": True}, 1, 100,
        ).best_candidate


def test_of_grade_a_bypasses_legacy_confluence_check():
    candidate = _evaluate(_signal())
    assert "ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B" not in candidate["failed_gates"]
    assert "ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65" not in candidate["failed_gates"]


def test_of_high_score_overrides_confirmation_hard_block():
    # §4.6: of_score=95 >= 90 → STRONG_FAIL becomes soft penalty, hard_block removed
    candidate = _evaluate(_signal(smc=10, mtfa=10))
    assert "CONFIRMATION_MATRIX_HARD_BLOCK" not in candidate["failed_gates"]


def test_of_low_score_keeps_confirmation_hard_block():
    # of_score < 90 → STRONG_FAIL still hard-blocks
    signal = _signal(smc=10, mtfa=10)
    signal["order_flow_execution_agent_score"] = 80
    signal["confidence"] = 80
    candidate = _evaluate(signal)
    assert "CONFIRMATION_MATRIX_HARD_BLOCK" in candidate["failed_gates"]
    assert candidate["demo_eligible"] is False


def test_of_score_below_90_keeps_legacy_confluence_check():
    signal = _signal()
    signal["order_flow_execution_agent_score"] = 89
    signal["confidence"] = 89
    candidate = _evaluate(signal)
    assert "ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65" in candidate["failed_gates"]


def test_momentum_opposition_is_an_additional_filter():
    signal = _signal()
    momentum = {
        "momentum_score": 0,
        "confluence_active": True,
        "confluence_direction": "BEAR",
    }
    hunter = SetupHunter(_settings())
    candidate = hunter.evaluate(
        "BTCUSD", "BTCUSD#",
        {"ai_decision": {}, "strategy_signals": [signal], "momentum_result": momentum},
        {"time_gate_status": "PASS", "market_open": True, "symbol_market_open": True}, 1, 100,
    ).best_candidate
    assert "MOMENTUM_DIVERGENCE" in candidate["failed_gates"]
    assert candidate["momentum_alignment_adjustment"] == -10.0
    assert candidate["demo_eligible"] is False
    assert candidate["final_trade_gate_v2"]["component_scores"]["momentum"] == 0


def test_momentum_alignment_records_bonus_without_forcing_execution():
    signal = _signal()
    signal["signal"] = "WAIT"
    momentum = {
        "momentum_score": 100,
        "confluence_active": True,
        "confluence_direction": "BULL",
    }
    hunter = SetupHunter(_settings())
    candidate = hunter.evaluate(
        "BTCUSD", "BTCUSD#",
        {"ai_decision": {}, "strategy_signals": [signal], "momentum_result": momentum},
        {"time_gate_status": "PASS", "market_open": True, "symbol_market_open": True}, 1, 100,
    ).best_candidate
    assert candidate["demo_eligible"] is False
    assert "final_trade_gate_v2" in candidate


def test_single_btc_exit_arbiter_enabled_by_default():
    from app.config import Settings
    settings = Settings()
    assert settings.btc_exit_arbiter_enabled is True
