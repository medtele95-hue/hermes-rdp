from __future__ import annotations

import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from app.agents.setup_hunter import SetupHunter
from app.config import Settings
from app.main import _routeable_setup_hunter_decision
from app.services.strategy_manager import StrategyManager
from app.strategies import simo_atm_breakout
from app.strategies.registry import ACTIVE_EXECUTION_STRATEGIES, OBSERVATION_STRATEGIES, allowed_for_symbol, strategy_role
from app.utils.throttle import should_emit, _state as _throttle_state


def settings() -> Settings:
    return Settings(
        demo_trading=True,
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        demo_ignore_all_time_blocks=True,
        demo_ignore_session_blocks=True,
        demo_ignore_bad_hour_blocks=True,
        new_strategies_min_score=75,
        strategy_manager_enabled=True,
        simo_atm_breakout_enabled=True,
        simo_atm_breakout_mode="ACTIVE_EXECUTION",
        simo_atm_breakout_symbols="US100,NAS100,USTEC,US100Cash#,NASDAQ",
    )


def simo_signal(symbol: str = "BTCUSD#") -> dict:
    return {
        "strategy": "SIMO_ATM_BREAKOUT",
        "symbol": symbol,
        "signal": "BUY",
        "direction": "BUY",
        "confidence": 80,
        "simo_atm_score": 80,
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


class SimoAtmBreakoutIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("simo_atm_breakout", None)

    def test_registry_marks_simo_active_execution_and_generics_observation(self) -> None:
        self.assertIn("SIMO_ATM_BREAKOUT", ACTIVE_EXECUTION_STRATEGIES)
        self.assertEqual(strategy_role("SIMO_ATM_BREAKOUT"), "ENTRY")
        self.assertIn("QUANT_PRO_REGIME_SWITCHING", OBSERVATION_STRATEGIES)
        self.assertEqual(strategy_role("QUANT_PRO_REGIME_SWITCHING"), "OBSERVER")
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "BTCUSD#"))
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "GOLD#"))
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "EURUSD"))

    def test_adapter_waits_when_uploaded_source_is_missing(self) -> None:
        result = simo_atm_breakout.evaluate("BTCUSD#", {}, {}, settings())
        self.assertEqual(result["strategy"], "SIMO_ATM_BREAKOUT")
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["reason"], "SIMO_INSUFFICIENT_DATA")
        self.assertFalse(result["simo_atm_breakout"]["source_loaded"])

    def test_adapter_normalizes_valid_uploaded_source_output(self) -> None:
        module = types.ModuleType("simo_atm_breakout")

        def evaluate(**_: object) -> dict:
            return {"signal": "SELL", "entry": 100.0, "sl": 102.0, "tp": 96.0, "confidence": 0.8}

        module.evaluate = evaluate
        sys.modules["simo_atm_breakout"] = module

        result = simo_atm_breakout.evaluate("BTCUSD#", {}, {}, settings())
        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["confidence"], 80)
        self.assertEqual(result["risk_reward"], 2.0)
        self.assertEqual(result["status"], "ORDER_READY")

    def test_setup_hunter_selects_valid_simo_as_demo_eligible_entry(self) -> None:
        hunter = SetupHunter(settings())
        result = hunter.evaluate(
            "BTCUSD",
            "BTCUSD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [simo_signal()]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True, "market_open": True},
            spread=1.0,
            max_spread=10.0,
        )
        best = result.best_candidate
        self.assertEqual(best["best_strategy"], "SIMO_ATM_BREAKOUT")
        self.assertEqual(best["strategy_role"], "ENTRY")
        self.assertTrue(best["demo_eligible"])
        self.assertEqual(result.decision["strategy"], "SIMO_ATM_BREAKOUT")

    def test_setup_hunter_does_not_select_generic_gold_fallback(self) -> None:
        hunter = SetupHunter(settings())
        generic = {**simo_signal("GOLD#"), "strategy": "TREND_CONTINUATION_BREAKDOWN", "trend_continuation_score": 100}
        wait = {**simo_signal("GOLD#"), "signal": "WAIT", "direction": "WAIT", "confidence": 0, "simo_atm_score": 0}
        result = hunter.evaluate(
            "GOLD",
            "GOLD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [generic, wait]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True, "market_open": True},
            spread=1.0,
            max_spread=10.0,
        )
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertEqual(result.best_candidate["empty_reason"], "NO_ALLOWED_EXECUTION_CANDIDATE")

    def test_main_handoff_marks_simo_route_to_demo(self) -> None:
        candidate = {
            "best_strategy": "SIMO_ATM_BREAKOUT",
            "demo_eligible": True,
            "direction": "BUY",
            "symbol": "BTCUSD",
            "broker_symbol": "BTCUSD#",
            "entry": 100.0,
            "sl": 98.0,
            "tp": 104.0,
            "rr": 2.0,
            "edge_score": 80,
            "setup_score": 80,
            "grade": "B",
        }
        routed = _routeable_setup_hunter_decision({"strategy": "NONE", "signal": "WAIT"}, candidate)
        self.assertEqual(routed["strategy"], "SIMO_ATM_BREAKOUT")
        self.assertEqual(routed["decision"], "ROUTE_TO_DEMO")
        self.assertTrue(routed["route_to_demo"])

    def test_order_send_remains_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in (root / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text and path.as_posix().replace("/", "\\").endswith("app\\mt5\\demo_router.py") is False:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_simo_config_settings_present(self) -> None:
        s = settings()
        self.assertTrue(s.strategy_manager_enabled)
        self.assertTrue(s.simo_atm_breakout_enabled)
        self.assertEqual(s.simo_atm_breakout_mode, "ACTIVE_EXECUTION")
        self.assertIn("US100", s.simo_atm_symbol_list)
        self.assertIn("NAS100", s.simo_atm_symbol_list)
        self.assertIn("USTEC", s.simo_atm_symbol_list)

    def test_strategy_manager_simo_enabled_property(self) -> None:
        s = settings()
        mgr = StrategyManager(s)
        self.assertTrue(mgr.simo_enabled)

    def test_strategy_manager_simo_disabled_when_flag_off(self) -> None:
        s = Settings(strategy_manager_enabled=False)
        mgr = StrategyManager(s)
        self.assertFalse(mgr.simo_enabled)

    def test_strategy_manager_symbol_candidates_list(self) -> None:
        s = settings()
        mgr = StrategyManager(s)
        candidates = mgr._symbol_candidates
        self.assertIn("US100", candidates)
        self.assertIn("NAS100", candidates)

    def test_strategy_manager_discover_returns_none_when_mt5_unavailable(self) -> None:
        s = settings()
        mgr = StrategyManager(s)
        with unittest.mock.patch.dict(sys.modules, {"MetaTrader5": None}):
            result = mgr.discover_simo_symbol()
        self.assertIsNone(result)

    def test_simo_invalid_signal_does_not_route(self) -> None:
        hunter = SetupHunter(settings())
        wait_signal = {**simo_signal(), "signal": "WAIT", "direction": "WAIT", "confidence": 0, "simo_atm_score": 0}
        result = hunter.evaluate(
            "BTCUSD",
            "BTCUSD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [wait_signal]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True, "market_open": True},
            spread=1.0,
            max_spread=10.0,
        )
        self.assertFalse(result.best_candidate.get("demo_eligible"))

    def test_eur_generic_strategies_filtered_before_demo_router(self) -> None:
        hunter = SetupHunter(settings())
        generic = {
            **simo_signal("EURUSD"),
            "strategy": "QUANT_PRO_REGIME_SWITCHING",
            "signal": "BUY",
            "direction": "BUY",
            "confidence": 90,
        }
        wait = {**simo_signal("EURUSD"), "signal": "WAIT", "direction": "WAIT", "confidence": 0, "simo_atm_score": 0}
        result = hunter.evaluate(
            "EURUSD",
            "EURUSD",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [generic, wait]},
            {"time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True, "market_open": True},
            spread=1.0,
            max_spread=10.0,
        )
        self.assertEqual(result.best_candidate.get("best_strategy"), "NONE")
        self.assertEqual(result.best_candidate.get("empty_reason"), "NO_ALLOWED_EXECUTION_CANDIDATE")

    def test_warning_throttle_emits_once_per_interval(self) -> None:
        _throttle_state.pop("TEST_THROTTLE_KEY", None)
        self.assertTrue(should_emit("TEST_THROTTLE_KEY", interval_seconds=300))
        self.assertFalse(should_emit("TEST_THROTTLE_KEY", interval_seconds=300))
        self.assertFalse(should_emit("TEST_THROTTLE_KEY", interval_seconds=300))

    def test_warning_throttle_independent_keys(self) -> None:
        _throttle_state.pop("KEY_A", None)
        _throttle_state.pop("KEY_B", None)
        self.assertTrue(should_emit("KEY_A"))
        self.assertTrue(should_emit("KEY_B"))
        self.assertFalse(should_emit("KEY_A"))
        self.assertFalse(should_emit("KEY_B"))


if __name__ == "__main__":
    unittest.main()
