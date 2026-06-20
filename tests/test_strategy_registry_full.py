"""Full strategy registry and routing invariant tests.

Verifies:
- Every strategy is classified as ACTIVE_EXECUTION or OBSERVATION_ONLY
- Observation strategies never reach DemoRouter
- Active strategies can produce demo-eligible candidates
- Gold/EUR generic strategies are blocked before DemoRouter
- mt5.order_send only exists in demo_router.py
- StrategyManager mode/route_allowed helpers work correctly
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

from app.agents.setup_hunter import SetupHunter
from app.config import Settings
from app.services.strategy_manager import StrategyManager, _ACTIVE_EXECUTION, _OBSERVATION_ONLY
from app.strategies.registry import (
    ACTIVE_EXECUTION_STRATEGIES,
    ALLOWED_BTC_EXECUTION_STRATEGIES,
    ALLOWED_GOLD_EXECUTION_STRATEGIES,
    ALLOWED_EUR_EXECUTION_STRATEGIES,
    ALLOWED_US100_EXECUTION_STRATEGIES,
    OBSERVATION_STRATEGIES,
    strategy_mode,
    strategy_role,
    allowed_for_symbol,
    is_active_execution,
    is_observation,
)


def _settings() -> Settings:
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
    )


def _time_gate(open: bool = True) -> dict:
    return {
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "symbol_market_open": open,
        "market_open": open,
    }


def _base_signal(strategy: str, symbol: str = "BTCUSD#", direction: str = "BUY") -> dict:
    return {
        "strategy": strategy,
        "symbol": symbol,
        "signal": direction,
        "direction": direction,
        "confidence": 80,
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


# ---------------------------------------------------------------------------
# Registry classification tests
# ---------------------------------------------------------------------------

class TestRegistryClassification(unittest.TestCase):
    def test_active_execution_strategies_are_nonempty(self) -> None:
        self.assertGreater(len(ACTIVE_EXECUTION_STRATEGIES), 0)

    def test_observation_strategies_are_nonempty(self) -> None:
        self.assertGreater(len(OBSERVATION_STRATEGIES), 0)

    def test_simo_atm_is_active_execution(self) -> None:
        self.assertIn("SIMO_ATM_BREAKOUT", ACTIVE_EXECUTION_STRATEGIES)
        self.assertEqual(strategy_role("SIMO_ATM_BREAKOUT"), "ENTRY")
        self.assertTrue(is_active_execution("SIMO_ATM_BREAKOUT"))

    def test_btc_scalping_is_active_execution(self) -> None:
        self.assertIn("BTC_SCALPING_AGENT", ACTIVE_EXECUTION_STRATEGIES)
        self.assertTrue(is_active_execution("BTC_SCALPING_AGENT"))

    def test_eur_ema_rsi_atr_is_active_execution(self) -> None:
        self.assertIn("EUR_EMA_RSI_ATR_CROSSOVER", ACTIVE_EXECUTION_STRATEGIES)
        self.assertTrue(is_active_execution("EUR_EMA_RSI_ATR_CROSSOVER"))

    def test_fib_confluence_is_active_execution(self) -> None:
        self.assertIn("FIB_CONFLUENCE_EXECUTION_AGENT", ACTIVE_EXECUTION_STRATEGIES)
        self.assertTrue(is_active_execution("FIB_CONFLUENCE_EXECUTION_AGENT"))

    def test_gold_liquidity_hunter_is_active_execution(self) -> None:
        self.assertIn("GOLD_LIQUIDITY_HUNTER_PRO", ACTIVE_EXECUTION_STRATEGIES)
        self.assertTrue(is_active_execution("GOLD_LIQUIDITY_HUNTER_PRO"))

    def test_gold_m1m5_scalper_is_active_execution(self) -> None:
        self.assertIn("GOLD_M1_M5_EMA_SWEEP_SCALPER", ACTIVE_EXECUTION_STRATEGIES)
        self.assertTrue(is_active_execution("GOLD_M1_M5_EMA_SWEEP_SCALPER"))

    def test_order_flow_reader_is_observation(self) -> None:
        self.assertIn("ORDER_FLOW_READER", OBSERVATION_STRATEGIES)
        self.assertTrue(is_observation("ORDER_FLOW_READER"))

    def test_quant_statistical_pullback_is_observation(self) -> None:
        self.assertIn("QUANT_STATISTICAL_PULLBACK", OBSERVATION_STRATEGIES)
        self.assertTrue(is_observation("QUANT_STATISTICAL_PULLBACK"))

    def test_quant_pro_is_observation(self) -> None:
        self.assertIn("QUANT_PRO_REGIME_SWITCHING", OBSERVATION_STRATEGIES)
        self.assertTrue(is_observation("QUANT_PRO_REGIME_SWITCHING"))

    def test_second_entry_is_observation(self) -> None:
        self.assertIn("SECOND_ENTRY", OBSERVATION_STRATEGIES)
        self.assertTrue(is_observation("SECOND_ENTRY"))

    def test_scalping_agent_is_observation(self) -> None:
        self.assertIn("SCALPING_AGENT", OBSERVATION_STRATEGIES)
        self.assertTrue(is_observation("SCALPING_AGENT"))

    def test_active_and_observation_are_disjoint(self) -> None:
        overlap = set(ACTIVE_EXECUTION_STRATEGIES) & set(OBSERVATION_STRATEGIES)
        self.assertEqual(overlap, set(), f"Overlap: {overlap}")

    def test_strategy_mode_returns_correct_values(self) -> None:
        from app.strategies.registry import StrategyMode
        self.assertEqual(strategy_mode("SIMO_ATM_BREAKOUT"), StrategyMode.ACTIVE_EXECUTION)
        self.assertEqual(strategy_mode("ORDER_FLOW_READER"), StrategyMode.OBSERVATION)
        self.assertEqual(strategy_mode("UNKNOWN_STRATEGY"), StrategyMode.OBSERVATION)


# ---------------------------------------------------------------------------
# Symbol-strategy routing rules
# ---------------------------------------------------------------------------

class TestSymbolStrategyRouting(unittest.TestCase):
    def test_simo_allowed_on_all_canonical_symbols(self) -> None:
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "BTCUSD#"))
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "GOLD#"))
        self.assertTrue(allowed_for_symbol("SIMO_ATM_BREAKOUT", "EURUSD"))

    def test_gold_only_allows_gold_strategies(self) -> None:
        self.assertTrue(allowed_for_symbol("GOLD_LIQUIDITY_HUNTER_PRO", "GOLD#"))
        self.assertFalse(allowed_for_symbol("BTC_SCALPING_AGENT", "GOLD#"))
        self.assertFalse(allowed_for_symbol("QUANT_PRO_REGIME_SWITCHING", "GOLD#"))

    def test_eur_only_allows_eur_strategies(self) -> None:
        self.assertTrue(allowed_for_symbol("EUR_EMA_RSI_ATR_CROSSOVER", "EURUSD"))
        self.assertFalse(allowed_for_symbol("BTC_SCALPING_AGENT", "EURUSD"))
        self.assertFalse(allowed_for_symbol("QUANT_PRO_REGIME_SWITCHING", "EURUSD"))

    def test_btc_only_allows_btc_strategies(self) -> None:
        self.assertTrue(allowed_for_symbol("BTC_SCALPING_AGENT", "BTCUSD#"))
        self.assertFalse(allowed_for_symbol("GOLD_LIQUIDITY_HUNTER_PRO", "BTCUSD#"))
        self.assertTrue(allowed_for_symbol("FIB_CONFLUENCE_EXECUTION_AGENT", "BTCUSD#"))

    def test_btc_execution_strategies_match_registry(self) -> None:
        for strat in ALLOWED_BTC_EXECUTION_STRATEGIES:
            self.assertTrue(
                allowed_for_symbol(strat, "BTCUSD#"),
                f"{strat} should be allowed on BTCUSD#",
            )

    def test_gold_execution_strategies_match_registry(self) -> None:
        for strat in ALLOWED_GOLD_EXECUTION_STRATEGIES:
            self.assertTrue(
                allowed_for_symbol(strat, "GOLD#"),
                f"{strat} should be allowed on GOLD",
            )

    def test_eur_execution_strategies_match_registry(self) -> None:
        for strat in ALLOWED_EUR_EXECUTION_STRATEGIES:
            self.assertTrue(
                allowed_for_symbol(strat, "EURUSD"),
                f"{strat} should be allowed on EURUSD",
            )

    def test_us100_execution_strategies_match_registry(self) -> None:
        for strat in ALLOWED_US100_EXECUTION_STRATEGIES:
            self.assertTrue(
                allowed_for_symbol(strat, "US100Cash#"),
                f"{strat} should be allowed on US100Cash#",
            )


# ---------------------------------------------------------------------------
# SetupHunter: observation strategies never become demo-eligible
# ---------------------------------------------------------------------------

class TestObservationStrategyNeverRoutes(unittest.TestCase):
    _observation_strategies = [
        "ORDER_FLOW_READER",
        "QUANT_STATISTICAL_PULLBACK",
        "QUANT_PRO_REGIME_SWITCHING",
        "SECOND_ENTRY",
        "SCALPING_AGENT",
        "BREAKOUT_RETEST",
        "CRT_TBS_REVERSAL",
        "AMD_FVG_IFVG_REVERSAL",
        "FIB_OTE_RETEST",
        "TREND_CONTINUATION_BREAKDOWN",
    ]

    def _hunter(self) -> SetupHunter:
        return SetupHunter(_settings())

    def _run(self, strategy: str) -> dict:
        hunter = self._hunter()
        signal = _base_signal(strategy)
        return hunter.evaluate(
            "BTCUSD",
            "BTCUSD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [signal]},
            _time_gate(),
            spread=1.0,
            max_spread=10.0,
        ).best_candidate

    def test_order_flow_reader_not_demo_eligible(self) -> None:
        best = self._run("ORDER_FLOW_READER")
        self.assertFalse(best.get("demo_eligible"))

    def test_quant_statistical_pullback_not_demo_eligible(self) -> None:
        best = self._run("QUANT_STATISTICAL_PULLBACK")
        self.assertFalse(best.get("demo_eligible"))

    def test_quant_pro_not_demo_eligible(self) -> None:
        best = self._run("QUANT_PRO_REGIME_SWITCHING")
        self.assertFalse(best.get("demo_eligible"))

    def test_second_entry_not_demo_eligible(self) -> None:
        best = self._run("SECOND_ENTRY")
        self.assertFalse(best.get("demo_eligible"))

    def test_scalping_agent_legacy_not_demo_eligible(self) -> None:
        best = self._run("SCALPING_AGENT")
        self.assertFalse(best.get("demo_eligible"))

    def test_crt_tbs_not_demo_eligible_on_btc(self) -> None:
        # CRT_TBS is observation; even with BUY signal it must not be demo-eligible
        best = self._run("CRT_TBS_REVERSAL")
        self.assertFalse(best.get("demo_eligible"))


# ---------------------------------------------------------------------------
# Gold/EUR generic strategies blocked at SetupHunter level
# ---------------------------------------------------------------------------

class TestGoldEurGenericBlocked(unittest.TestCase):
    _gold_generic = [
        "TREND_CONTINUATION_BREAKDOWN",
        "QUANT_PRO_REGIME_SWITCHING",
        "QUANT_STATISTICAL_PULLBACK",
        "BREAKOUT_RETEST",
    ]

    def _hunter(self) -> SetupHunter:
        return SetupHunter(_settings())

    def test_gold_generic_strategies_produce_no_allowed_candidate(self) -> None:
        hunter = self._hunter()
        signals = [
            {**_base_signal(s, symbol="GOLD#"), "simo_atm_score": 0} for s in self._gold_generic
        ]
        result = hunter.evaluate(
            "GOLD", "GOLD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": signals},
            _time_gate(),
            spread=1.0,
            max_spread=10.0,
        )
        self.assertEqual(result.best_candidate.get("best_strategy"), "NONE")
        self.assertFalse(result.best_candidate.get("demo_eligible"))

    def test_eur_generic_strategies_produce_no_allowed_candidate(self) -> None:
        hunter = self._hunter()
        signals = [
            {**_base_signal(s, symbol="EURUSD"), "simo_atm_score": 0}
            for s in ["QUANT_PRO_REGIME_SWITCHING", "TREND_CONTINUATION_BREAKDOWN"]
        ]
        result = hunter.evaluate(
            "EURUSD", "EURUSD",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": signals},
            _time_gate(),
            spread=1.0,
            max_spread=10.0,
        )
        self.assertEqual(result.best_candidate.get("best_strategy"), "NONE")
        self.assertFalse(result.best_candidate.get("demo_eligible"))

    def test_gold_liquidity_hunter_can_be_demo_eligible_on_gold(self) -> None:
        hunter = self._hunter()
        signal = {
            **_base_signal("GOLD_LIQUIDITY_HUNTER_PRO", symbol="GOLD#"),
            "gold_liquidity_score": 80,
            "safety_guard_status": "PASS",
        }
        result = hunter.evaluate(
            "GOLD", "GOLD#",
            {"ai_decision": {"signal": "WAIT"}, "strategy_signals": [signal]},
            _time_gate(),
            spread=1.0,
            max_spread=10.0,
        )
        best = result.best_candidate
        # It should not be blocked for being generic
        self.assertNotEqual(best.get("execution_policy_reason"), "GOLD_GENERIC_STRATEGY_DISABLED")


# ---------------------------------------------------------------------------
# StrategyManager helpers
# ---------------------------------------------------------------------------

class TestStrategyManagerHelpers(unittest.TestCase):
    def test_mode_active_for_simo(self) -> None:
        self.assertEqual(StrategyManager.strategy_mode("SIMO_ATM_BREAKOUT"), "ACTIVE_EXECUTION")

    def test_mode_observation_for_order_flow(self) -> None:
        self.assertEqual(StrategyManager.strategy_mode("ORDER_FLOW_READER"), "OBSERVATION_ONLY")

    def test_route_allowed_true_for_active(self) -> None:
        for strategy in _ACTIVE_EXECUTION:
            self.assertTrue(
                StrategyManager.route_allowed(strategy),
                f"route_allowed should be True for {strategy}",
            )

    def test_route_allowed_false_for_observation(self) -> None:
        for strategy in _OBSERVATION_ONLY:
            self.assertFalse(
                StrategyManager.route_allowed(strategy),
                f"route_allowed should be False for {strategy}",
            )

    def test_active_and_observation_are_disjoint(self) -> None:
        overlap = _ACTIVE_EXECUTION & _OBSERVATION_ONLY
        self.assertEqual(overlap, set(), f"Overlap: {overlap}")

    def test_simo_enabled_with_correct_settings(self) -> None:
        mgr = StrategyManager(_settings())
        self.assertTrue(mgr.simo_enabled)

    def test_simo_disabled_when_flag_off(self) -> None:
        s = Settings(strategy_manager_enabled=False)
        mgr = StrategyManager(s)
        self.assertFalse(mgr.simo_enabled)

    def test_discover_returns_none_without_mt5(self) -> None:
        mgr = StrategyManager(_settings())
        with unittest.mock.patch.dict(sys.modules, {"MetaTrader5": None}):
            result = mgr.discover_simo_symbol()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Safety invariant: order_send only in demo_router
# ---------------------------------------------------------------------------

class TestOrderSendLocation(unittest.TestCase):
    def test_order_send_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        demo_router_path = (root / "app" / "mt5" / "demo_router.py").as_posix()
        offenders = []
        for path in (root / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                posix = path.as_posix()
                if not posix.endswith("app/mt5/demo_router.py"):
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_order_send_not_called_directly_in_tests(self) -> None:
        """Test files may patch order_send but must never call mt5.order_send() directly."""
        import re
        root = Path(__file__).resolve().parents[1]
        this_file = Path(__file__).resolve()
        # Matches actual Python calls: mt5.order_send( — not string literals in patch()
        call_re = re.compile(r'(?<!["\'])mt5\.order_send\s*\(')
        for path in (root / "tests").rglob("*.py"):
            if path.resolve() == this_file:
                continue  # skip self — contains the pattern as a string literal
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                stripped = line.strip()
                # Allow mock/patch usages which reference the module path as a string
                if "patch(" in stripped or stripped.startswith("#"):
                    continue
                if call_re.search(stripped):
                    self.fail(f"Direct mt5.order_send() call found in {path}: {stripped!r}")


# ---------------------------------------------------------------------------
# Safety config invariants
# ---------------------------------------------------------------------------

class TestSafetyConfigInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        s = _settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_true(self) -> None:
        s = _settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_is_001(self) -> None:
        s = _settings()
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)

    def test_default_settings_safe(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_live_trading)
        self.assertTrue(s.demo_only)
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)


if __name__ == "__main__":
    unittest.main()
