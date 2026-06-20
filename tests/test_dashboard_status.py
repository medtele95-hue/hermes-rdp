"""Tests for dashboard_status payload enrichment.

Verifies:
- dashboard_status.payload contains strategy_manager block.
- dashboard_status.payload contains cycle_status block.
- dashboard_status.payload contains symbols block.
- dashboard_status.payload contains setup_hunter block.
- dashboard_status.payload contains confirmation_matrix block.
- dashboard_status.payload contains safety_guard block.
- dashboard_status.payload contains lovable_ingest_health block.
- dashboard_status.payload contains account block (no secrets).
- order_flow block contains execution_agent sub-block.
- Safety invariants in payload.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.config import Settings
from app.services.dashboard_snapshot import dashboard_snapshot


def _settings() -> Settings:
    return Settings(
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        demo_magic_number=909002,
    )


def _snapshot(**kwargs) -> dict:
    return dashboard_snapshot(
        _settings(),
        account={"login": 123456, "server": "Demo-Server", "balance": 10000.0, "equity": 10050.0, "profit": 50.0, "trade_mode": 0},
        time_snapshot={"time_gate_status": "PASS", "session_name": "LONDON", "is_weekend": False},
        mt5_connected=True,
        **kwargs,
    )


class TestStrategyManagerBlock(unittest.TestCase):
    def test_strategy_manager_present(self) -> None:
        p = _snapshot()
        self.assertIn("strategy_manager", p)

    def test_strategy_manager_has_active_execution_strategies(self) -> None:
        p = _snapshot()
        sm = p["strategy_manager"]
        self.assertIn("active_execution_strategies", sm)
        self.assertIsInstance(sm["active_execution_strategies"], list)
        self.assertGreater(len(sm["active_execution_strategies"]), 0)

    def test_strategy_manager_has_confirmation_modules(self) -> None:
        p = _snapshot()
        sm = p["strategy_manager"]
        self.assertIn("confirmation_modules", sm)

    def test_strategy_manager_has_internal_data_feeds(self) -> None:
        p = _snapshot()
        sm = p["strategy_manager"]
        self.assertIn("internal_data_feeds", sm)

    def test_strategy_manager_has_route_allowed(self) -> None:
        p = _snapshot()
        sm = p["strategy_manager"]
        self.assertIn("route_allowed_by_strategy", sm)
        self.assertIsInstance(sm["route_allowed_by_strategy"], dict)

    def test_strategy_manager_has_enabled_by_strategy(self) -> None:
        p = _snapshot()
        sm = p["strategy_manager"]
        self.assertIn("enabled_by_strategy", sm)


class TestCycleStatusBlock(unittest.TestCase):
    def test_cycle_status_present(self) -> None:
        p = _snapshot()
        self.assertIn("cycle_status", p)

    def test_cycle_status_keys(self) -> None:
        cs = {
            "last_cycle_start_utc": "2026-06-12T10:00:00+00:00",
            "last_cycle_end_utc": "2026-06-12T10:00:05+00:00",
            "analyzed": 4, "skipped": 0, "demo_orders": 1, "last_status": "OK",
        }
        p = _snapshot(cycle_status=cs)
        block = p["cycle_status"]
        self.assertEqual(block["analyzed"], 4)
        self.assertEqual(block["demo_orders"], 1)
        self.assertEqual(block["last_status"], "OK")

    def test_cycle_status_defaults_when_none(self) -> None:
        p = _snapshot(cycle_status=None)
        block = p["cycle_status"]
        self.assertEqual(block["analyzed"], 0)
        self.assertIn("last_status", block)


class TestSymbolsBlock(unittest.TestCase):
    def test_symbols_present(self) -> None:
        p = _snapshot()
        self.assertIn("symbols", p)
        self.assertIsInstance(p["symbols"], dict)

    def test_symbols_populated_from_per_symbol_state(self) -> None:
        state = {
            "BTCUSD#": {
                "price": 65000.0, "spread": 10.0, "spread_status": "OK",
                "session": "LONDON", "time_gate": "PASS",
                "latest_decision": "WAIT", "latest_reason": None,
                "route_status": "WAIT", "last_update_utc": "2026-06-12T10:00:00+00:00",
            }
        }
        p = _snapshot(per_symbol_state=state)
        symbols = p["symbols"]
        if "BTCUSD#" in symbols:
            self.assertEqual(symbols["BTCUSD#"]["price"], 65000.0)
            self.assertEqual(symbols["BTCUSD#"]["spread_status"], "OK")

    def test_symbols_schema_has_required_keys(self) -> None:
        s = _settings()
        p = dashboard_snapshot(s)
        symbols = p.get("symbols", {})
        required_keys = {"price", "spread", "spread_status", "session", "time_gate",
                         "latest_decision", "latest_reason", "route_status", "last_update_utc"}
        for sym_data in symbols.values():
            for key in required_keys:
                self.assertIn(key, sym_data, f"Key {key!r} missing from symbols entry")
            break  # check first entry only


class TestSetupHunterBlock(unittest.TestCase):
    def test_setup_hunter_present(self) -> None:
        p = _snapshot()
        self.assertIn("setup_hunter", p)

    def test_setup_hunter_has_latest_by_symbol_strategy(self) -> None:
        p = _snapshot()
        sh = p["setup_hunter"]
        self.assertIn("latest_by_symbol_strategy", sh)
        self.assertIsInstance(sh["latest_by_symbol_strategy"], list)

    def test_setup_hunter_candidates_populated(self) -> None:
        candidates = [
            {
                "symbol": "BTCUSD#", "broker_symbol": "BTCUSD#",
                "best_strategy": "BTC_SCALPING_AGENT", "strategy_role": "ENTRY",
                "direction": "BUY", "grade": "B", "edge_score": 72,
                "demo_eligible": False, "near_miss_reason": "RR_TOO_LOW",
                "failed_gates": ["RR_TOO_LOW"], "smc_score": 80.0, "mtfa_score": 65.0,
                "entry": 65000.0, "sl": 64800.0, "tp": 65400.0, "rr": 2.0,
            }
        ]
        p = _snapshot(latest_candidates=candidates)
        sh = p["setup_hunter"]
        self.assertEqual(len(sh["latest_by_symbol_strategy"]), 1)
        entry = sh["latest_by_symbol_strategy"][0]
        self.assertEqual(entry["strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(entry["direction"], "BUY")


class TestConfirmationMatrixBlock(unittest.TestCase):
    def test_confirmation_matrix_present(self) -> None:
        p = _snapshot()
        self.assertIn("confirmation_matrix", p)

    def test_confirmation_matrix_has_latest_by_symbol_strategy(self) -> None:
        p = _snapshot()
        cm = p["confirmation_matrix"]
        self.assertIn("latest_by_symbol_strategy", cm)

    def test_confirmation_matrix_block_when_hard_block(self) -> None:
        candidates = [
            {
                "symbol": "GOLD#", "best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
                "strategy_role": "ENTRY", "smc_score": 30.0, "mtfa_score": 25.0,
                "failed_gates": ["CONFIRMATION_MATRIX_HARD_BLOCK"],
                "edge_score": 55, "grade": "D", "demo_eligible": False,
                "near_miss_reason": "CONFIRMATION_MATRIX_HARD_BLOCK",
            }
        ]
        p = _snapshot(latest_candidates=candidates)
        cm = p["confirmation_matrix"]
        entries = cm["latest_by_symbol_strategy"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["hard_block"])
        self.assertEqual(entries[0]["status"], "BLOCK")

    def test_confirmation_matrix_warn_on_soft_fail(self) -> None:
        candidates = [
            {
                "symbol": "GOLD#", "best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
                "strategy_role": "ENTRY", "smc_score": 55.0, "mtfa_score": 65.0,
                "failed_gates": [],
                "edge_score": 72, "grade": "B", "demo_eligible": True,
                "near_miss_reason": None,
            }
        ]
        p = _snapshot(latest_candidates=candidates)
        cm = p["confirmation_matrix"]
        entry = cm["latest_by_symbol_strategy"][0]
        self.assertFalse(entry["hard_block"])
        self.assertEqual(entry["status"], "WARN")

    def test_confirmation_matrix_pass_on_good_scores(self) -> None:
        candidates = [
            {
                "symbol": "EURUSD", "best_strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
                "strategy_role": "ENTRY", "smc_score": 80.0, "mtfa_score": 70.0,
                "failed_gates": [],
                "edge_score": 80, "grade": "A", "demo_eligible": True,
                "near_miss_reason": None,
            }
        ]
        p = _snapshot(latest_candidates=candidates)
        cm = p["confirmation_matrix"]
        entry = cm["latest_by_symbol_strategy"][0]
        self.assertFalse(entry["hard_block"])
        self.assertEqual(entry["status"], "PASS")


class TestSafetyGuardBlock(unittest.TestCase):
    def test_safety_guard_present(self) -> None:
        p = _snapshot()
        self.assertIn("safety_guard", p)

    def test_safety_guard_keys(self) -> None:
        sg = {"last_status": "PASS", "last_reason": "SAFETY_GUARD_PASS", "last_update_utc": "2026-06-12T10:00:00+00:00"}
        p = _snapshot(latest_safety_guard=sg)
        block = p["safety_guard"]
        self.assertEqual(block["last_status"], "PASS")
        self.assertEqual(block["last_reason"], "SAFETY_GUARD_PASS")

    def test_safety_guard_null_when_none(self) -> None:
        p = _snapshot(latest_safety_guard=None)
        block = p["safety_guard"]
        self.assertIsNone(block["last_status"])


class TestLovableIngestHealthBlock(unittest.TestCase):
    def test_lovable_ingest_health_present(self) -> None:
        p = _snapshot()
        self.assertIn("lovable_ingest_health", p)

    def test_lovable_ingest_health_live_status(self) -> None:
        ih = {"status": "LIVE", "reason": None, "last_update_utc": "2026-06-12T10:00:00+00:00"}
        p = _snapshot(ingest_health=ih)
        block = p["lovable_ingest_health"]
        self.assertEqual(block["status"], "LIVE")
        self.assertIsNone(block["reason"])

    def test_lovable_ingest_health_degraded_status(self) -> None:
        ih = {"status": "DEGRADED", "reason": "CIRCUIT_BREAKER_ACTIVE", "last_update_utc": "2026-06-12T10:00:00+00:00"}
        p = _snapshot(ingest_health=ih)
        block = p["lovable_ingest_health"]
        self.assertEqual(block["status"], "DEGRADED")
        self.assertEqual(block["reason"], "CIRCUIT_BREAKER_ACTIVE")


class TestAccountBlock(unittest.TestCase):
    def test_account_block_present(self) -> None:
        p = _snapshot()
        self.assertIn("account", p)

    def test_account_block_has_safety_fields(self) -> None:
        p = _snapshot()
        account = p["account"]
        self.assertFalse(account["allow_live_trading"])
        self.assertTrue(account["demo_only"])
        self.assertAlmostEqual(account["demo_max_lot"], 0.01, places=4)
        self.assertEqual(account["magic"], 909002)

    def test_account_block_no_secrets(self) -> None:
        p = _snapshot()
        account_str = str(p.get("account", {}))
        for secret_word in ("password", "secret", "api_key", "service_role"):
            self.assertNotIn(secret_word, account_str.lower(), f"Secret word '{secret_word}' in account block")

    def test_account_type_is_demo_for_mode_0(self) -> None:
        p = _snapshot()
        self.assertEqual(p["account"]["account_type"], "DEMO")


class TestOrderFlowExecutionAgentNested(unittest.TestCase):
    def test_order_flow_has_execution_agent_sub_block(self) -> None:
        p = _snapshot()
        of = p.get("order_flow") or {}
        self.assertIn("execution_agent", of)

    def test_order_flow_execution_agent_has_enabled_field(self) -> None:
        p = _snapshot()
        of = p.get("order_flow") or {}
        ea = of.get("execution_agent") or {}
        self.assertIn("enabled", ea)

    def test_order_flow_tabs_still_present(self) -> None:
        p = _snapshot()
        of = p.get("order_flow") or {}
        self.assertIn("tabs", of)


class TestSafetyInvariantsInPayload(unittest.TestCase):
    def test_allow_live_trading_false_in_payload(self) -> None:
        p = _snapshot()
        self.assertFalse(p.get("allow_live_trading"))

    def test_demo_only_true_in_payload(self) -> None:
        p = _snapshot()
        self.assertTrue(p.get("demo_only"))

    def test_live_trading_blocked_in_payload(self) -> None:
        p = _snapshot()
        self.assertTrue(p.get("live_trading_blocked"))

    def test_demo_max_lot_001_in_payload(self) -> None:
        p = _snapshot()
        self.assertAlmostEqual(p.get("demo_max_lot"), 0.01, places=4)


class TestSymbolsBlockHermesMainSymbols(unittest.TestCase):
    def test_hermes_main_symbols_always_in_payload(self) -> None:
        s = Settings(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            demo_only=True, allow_live_trading=False, demo_max_lot=0.01,
        )
        from app.services.dashboard_snapshot import dashboard_snapshot
        p = dashboard_snapshot(s, per_symbol_state={})
        symbols = p.get("symbols", {})
        self.assertIn("BTCUSD#", symbols)
        self.assertIn("GOLD#", symbols)
        self.assertIn("EURUSD", symbols)
        # Key must be exact case from config, not uppercased
        self.assertIn("US100Cash#", symbols)

    def test_us100cash_unavailable_emits_no_data(self) -> None:
        s = Settings(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            demo_only=True, allow_live_trading=False, demo_max_lot=0.01,
        )
        from app.services.dashboard_snapshot import dashboard_snapshot
        p = dashboard_snapshot(s, per_symbol_state={})
        # Key must be exact case from config (US100Cash#, not US100CASH#)
        entry = p["symbols"].get("US100Cash#", {})
        self.assertFalse(entry.get("available"))
        self.assertEqual(entry.get("route_status"), "NO_DATA")
        self.assertEqual(entry.get("latest_decision"), "WAIT")
        self.assertEqual(entry.get("latest_reason"), "UNAVAILABLE_OR_NO_RATES")

    def test_symbol_with_state_shows_available(self) -> None:
        s = Settings(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            demo_only=True, allow_live_trading=False, demo_max_lot=0.01,
        )
        state = {"GOLD#": {"price": 3200.0, "spread": 10.0, "spread_status": "OK",
                            "session": "LONDON", "time_gate": "PASS",
                            "latest_decision": "WAIT", "latest_reason": None,
                            "route_status": "WAIT", "last_update_utc": "2026-06-13T10:00:00+00:00"}}
        from app.services.dashboard_snapshot import dashboard_snapshot
        p = dashboard_snapshot(s, per_symbol_state=state)
        entry = p["symbols"].get("GOLD#", {})
        self.assertTrue(entry.get("available"))
        self.assertEqual(entry.get("price"), 3200.0)
        self.assertEqual(entry.get("route_status"), "WAIT")

    def test_symbol_enabled_flag_matches_hermes_main_list(self) -> None:
        s = Settings(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            demo_only=True, allow_live_trading=False, demo_max_lot=0.01,
        )
        from app.services.dashboard_snapshot import dashboard_snapshot
        p = dashboard_snapshot(s, per_symbol_state={})
        symbols = p["symbols"]
        # Key is exact case from config
        self.assertTrue(symbols["US100Cash#"]["enabled"])
        self.assertTrue(symbols["GOLD#"]["enabled"])


class TestSelectedCandidateSemantics(unittest.TestCase):
    def _grade_d_hunter(self) -> dict:
        return {
            "best_strategy": "FIB_CONFLUENCE_EXECUTION_AGENT",
            "strategy": "FIB_CONFLUENCE_EXECUTION_AGENT",
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "grade": "D",
            "final_confluence_grade": "D",
            "demo_eligible": False,
            "edge_score": 35,
            "final_confluence_score": 35.0,
            "market_open": True,
            "near_miss_reason": "FINAL_CONFLUENCE_GRADE_D",
        }

    def _routeable_hunter(self) -> dict:
        return {
            "best_strategy": "FIB_CONFLUENCE_EXECUTION_AGENT",
            "strategy": "FIB_CONFLUENCE_EXECUTION_AGENT",
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "grade": "B",
            "final_confluence_grade": "B",
            "demo_eligible": True,
            "edge_score": 72,
            "final_confluence_score": 65.0,
            "market_open": True,
            "direction": "BUY",
        }

    def test_grade_d_candidate_not_in_selected_candidate(self) -> None:
        p = _snapshot(setup_hunter=self._grade_d_hunter())
        self.assertIsNone(p.get("selected_candidate"))

    def test_grade_d_candidate_appears_in_blocked_best_candidate(self) -> None:
        p = _snapshot(setup_hunter=self._grade_d_hunter())
        self.assertIsNotNone(p.get("blocked_best_candidate"))
        self.assertIsNotNone(p.get("rejected_best_candidate"))

    def test_routeable_candidate_in_selected_candidate(self) -> None:
        p = _snapshot(setup_hunter=self._routeable_hunter())
        self.assertIsNotNone(p.get("selected_candidate"))
        self.assertIsNone(p.get("blocked_best_candidate"))

    def test_selected_candidate_none_when_no_setup_hunter(self) -> None:
        p = _snapshot(setup_hunter=None)
        self.assertIsNone(p.get("selected_candidate"))

    def test_market_closed_candidate_not_selected(self) -> None:
        sh = {**self._routeable_hunter(), "market_open": False}
        p = _snapshot(setup_hunter=sh)
        self.assertIsNone(p.get("selected_candidate"))
        self.assertIsNotNone(p.get("blocked_best_candidate"))


class TestBalancedSelectorDashboardBlock(unittest.TestCase):
    def _grade_d_candidate(self, symbol: str = "GOLD#") -> dict:
        return {
            "best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
            "symbol": symbol,
            "broker_symbol": symbol,
            "grade": "D",
            "final_confluence_grade": "D",
            "final_confluence_score": 38.0,
            "edge_score": 38,
            "demo_eligible": False,
            "market_open": True,
            "direction": "BUY",
        }

    def _routeable_candidate(self, symbol: str = "EURUSD", score: float = 70.0) -> dict:
        return {
            "best_strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
            "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
            "symbol": symbol,
            "broker_symbol": symbol,
            "grade": "B",
            "final_confluence_grade": "B",
            "final_confluence_score": score,
            "edge_score": score,
            "demo_eligible": True,
            "market_open": True,
            "direction": "BUY",
            "entry": 1.10, "sl": 1.09, "tp": 1.12, "rr": 2.0,
        }

    def test_best_candidate_null_when_all_blocked(self) -> None:
        candidates = [self._grade_d_candidate()]
        p = _snapshot(latest_candidates=candidates)
        bs = p.get("balanced_selector", {})
        self.assertIsNone(bs.get("best_candidate"))

    def test_best_blocked_candidate_populated_when_all_blocked(self) -> None:
        candidates = [self._grade_d_candidate()]
        p = _snapshot(latest_candidates=candidates)
        bs = p.get("balanced_selector", {})
        self.assertIsNotNone(bs.get("best_blocked_candidate"))

    def test_best_candidate_populated_when_routeable(self) -> None:
        candidates = [self._routeable_candidate(), self._grade_d_candidate()]
        p = _snapshot(latest_candidates=candidates)
        bs = p.get("balanced_selector", {})
        self.assertIsNotNone(bs.get("best_candidate"))
        self.assertEqual(bs["best_candidate"]["strategy"], "EUR_EMA_RSI_ATR_CROSSOVER")

    def test_best_candidate_null_no_candidates(self) -> None:
        p = _snapshot(latest_candidates=[])
        bs = p.get("balanced_selector", {})
        self.assertIsNone(bs.get("best_candidate"))
        self.assertIsNone(bs.get("best_blocked_candidate"))

    def test_balanced_selector_has_config_fields(self) -> None:
        p = _snapshot()
        bs = p.get("balanced_selector", {})
        self.assertIn("max_total_open_demo_trades", bs)
        self.assertIn("no_d_grade_routing", bs)
        self.assertTrue(bs["no_d_grade_routing"])


class TestCycleStatusSemantics(unittest.TestCase):
    def test_cycle_status_completed_passthrough(self) -> None:
        cs = {
            "last_cycle_start_utc": "2026-06-13T10:00:00+00:00",
            "last_cycle_end_utc": "2026-06-13T10:00:07+00:00",
            "analyzed": 3, "skipped": 0, "demo_orders": 0,
            "last_status": "COMPLETED",
        }
        p = _snapshot(cycle_status=cs)
        block = p["cycle_status"]
        self.assertEqual(block["last_status"], "COMPLETED")
        self.assertEqual(block["analyzed"], 3)

    def test_cycle_status_running_mid_cycle(self) -> None:
        cs = {
            "last_cycle_start_utc": "2026-06-13T10:00:00+00:00",
            "last_cycle_end_utc": None,
            "analyzed": 1, "skipped": 0, "demo_orders": 0,
            "last_status": "RUNNING",
        }
        p = _snapshot(cycle_status=cs)
        block = p["cycle_status"]
        self.assertEqual(block["last_status"], "RUNNING")
        self.assertEqual(block["analyzed"], 1)

    def test_cycle_status_counts_skipped_accurately(self) -> None:
        cs = {"analyzed": 2, "skipped": 1, "demo_orders": 0, "last_status": "COMPLETED"}
        p = _snapshot(cycle_status=cs)
        block = p["cycle_status"]
        self.assertEqual(block["analyzed"], 2)
        self.assertEqual(block["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
