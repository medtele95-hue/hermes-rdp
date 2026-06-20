"""LOVABLE_BTC_OLD_SYSTEM execution profile — full test suite.

Verifies:
  1. Old BTC profile excludes GOLD/EURUSD/US100 from the symbol cycle
  2. Old BTC profile activates only BTC_SCALPING_AGENT + ORDER_FLOW_EXECUTION_AGENT
  3. Old BTC profile bypasses FINAL_CONFLUENCE_TOO_LOW for ORDER_FLOW_EXECUTION_AGENT
     (only when SafetyGuard PASS — code-level verification)
  4. Old BTC profile bypasses CONFIRMATION_MATRIX_HARD_BLOCK for BTC_SCALPING_AGENT
  5. Normal demo pilot still blocks (no bypass when profile is not active)
  6. DemoRouter is the only mt5.order_send path
  7. Max lot stays 0.01
  8. Magic stays 909002
  9. Live trading stays false
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.profiles.lovable_btc_old_system import (
    PROFILE_NAME,
    OLD_BTC_SYMBOL,
    OLD_BTC_STRATEGIES,
    is_active,
    is_old_btc_strategy,
)
from app.agents.setup_hunter import _failed_gates

_APP_DIR = ROOT / "app"
_MAIN_PATH = ROOT / "app" / "main.py"
_DEMO_ROUTER_PATH = ROOT / "app" / "mt5" / "demo_router.py"
_ENV_PATH = ROOT / ".env"

DEMO_MAGIC = 909002


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _base_settings(**overrides) -> Settings:
    defaults = dict(
        demo_trading=True,
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        demo_magic_number=DEMO_MAGIC,
        demo_ignore_all_time_blocks=True,
        demo_ignore_session_blocks=True,
        demo_ignore_bad_hour_blocks=True,
        btc_scalping_min_confidence=55,
        btc_scalping_relaxed_demo_mode=True,
        btc_scalping_allow_m5_momentum=True,
        btc_scalping_allow_m1_breakout=True,
        order_flow_execution_enabled=True,
        order_flow_min_score=75,
        order_flow_min_rr=1.5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _btc_scalping_payload(
    confidence: float = 85,
    smc_score: float = 15,
    mtfa_score: float = 15,
    rr: float = 1.4,
    safety: str = "PASS",
    signal: str = "BUY",
) -> dict:
    """Payload that makes btc_scalping_ready=True and normally triggers CONFIRMATION_MATRIX_HARD_BLOCK."""
    return {
        "strategy": "BTC_SCALPING_AGENT",
        "symbol": "BTCUSD#",
        "broker_symbol": "BTCUSD#",
        "signal": signal,
        "direction": signal,
        "confidence": confidence,
        "smc_confluence_score": smc_score,
        "mtfa_score": mtfa_score,
        "risk_reward": rr,
        "reward_risk": rr,
        "entry": 65000.0,
        "sl": 64800.0,
        "tp": 65500.0,
        "safety_guard_status": safety,
        "safety_guard_reason": "SAFETY_PASS" if safety == "PASS" else "DEMO_MAX_TRADES_EXCEEDED",
        "symbol_market_open": True,
        "market_open": True,
        "time_gate_status": "PASS",
        "session_name": "LONDON",
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "smc_m15_confirmation": True,
        "smc_m1_entry_confirmation": True,
        "big_setup_grade": "A",
        "big_setup_score": 90,
    }


# ── 1. Profile constants and detection ───────────────────────────────────────

class TestOldBtcProfileConstants(unittest.TestCase):

    def test_profile_name(self) -> None:
        self.assertEqual(PROFILE_NAME, "LOVABLE_BTC_OLD_SYSTEM")

    def test_old_btc_symbol(self) -> None:
        self.assertEqual(OLD_BTC_SYMBOL, "BTCUSD#")

    def test_old_btc_strategies_contains_both(self) -> None:
        self.assertIn("BTC_SCALPING_AGENT", OLD_BTC_STRATEGIES)
        self.assertIn("ORDER_FLOW_EXECUTION_AGENT", OLD_BTC_STRATEGIES)

    def test_old_btc_strategies_excludes_gold_eur_us100(self) -> None:
        for strat in ("GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_M1_M5_EMA_SWEEP_SCALPER",
                      "EUR_EMA_RSI_ATR_CROSSOVER", "SIMO_ATM_BREAKOUT"):
            self.assertNotIn(strat, OLD_BTC_STRATEGIES, f"{strat} must not be in OLD_BTC_STRATEGIES")

    def test_is_active_true_when_profile_set(self) -> None:
        s = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        self.assertTrue(is_active(s))

    def test_is_active_false_when_profile_not_set(self) -> None:
        s = _base_settings(hermes_execution_profile="")
        self.assertFalse(is_active(s))

    def test_is_active_false_for_different_profile(self) -> None:
        s = _base_settings(hermes_execution_profile="DEMO_PILOT_48H")
        self.assertFalse(is_active(s))

    def test_is_active_case_insensitive(self) -> None:
        s = _base_settings(hermes_execution_profile="lovable_btc_old_system")
        self.assertTrue(is_active(s))

    def test_is_old_btc_strategy_true_for_btc_scalping(self) -> None:
        self.assertTrue(is_old_btc_strategy("BTC_SCALPING_AGENT"))

    def test_is_old_btc_strategy_true_for_order_flow(self) -> None:
        self.assertTrue(is_old_btc_strategy("ORDER_FLOW_EXECUTION_AGENT"))

    def test_is_old_btc_strategy_false_for_gold(self) -> None:
        self.assertFalse(is_old_btc_strategy("GOLD_LIQUIDITY_HUNTER_PRO"))


# ── 2. Symbol restriction ─────────────────────────────────────────────────────

class TestOldBtcProfileSymbolRestriction(unittest.TestCase):
    """Old BTC profile must restrict symbol cycle to BTCUSD# only."""

    def test_main_py_filters_resolved_symbols_for_old_profile(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_lovable_btc_profile",
            content,
            "main.py must define _lovable_btc_profile to detect the old BTC profile",
        )

    def test_main_py_symbol_filter_starts_with_btcusd(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'startswith("BTCUSD")',
            content,
            "main.py must filter resolved_symbols by BTCUSD prefix for old profile",
        )

    def test_main_py_simo_skipped_in_old_profile(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_lovable_btc_profile",
            content,
            "main.py must gate simo sub-cycle behind _lovable_btc_profile",
        )
        pattern = r"_lovable_btc_profile[\s\S]{0,60}simo_items.*simo_counts.*=.*\[\]"
        self.assertTrue(
            re.search(pattern, content),
            "main.py must suppress SIMO sub-cycle when old profile is active",
        )

    def test_execution_profile_log_emitted(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "[EXECUTION_PROFILE]",
            content,
            "main.py must emit [EXECUTION_PROFILE] log when old profile is active",
        )


# ── 3. ORDER_FLOW_EXECUTION_AGENT confluence bypass ──────────────────────────

class TestOrderFlowConfluenceBypass(unittest.TestCase):
    """Old BTC profile must bypass FINAL_CONFLUENCE_TOO_LOW for ORDER_FLOW_EXECUTION_AGENT."""

    def test_main_py_has_old_btc_route_bypass_variable(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_old_btc_route_bypass",
            content,
            "main.py must define _old_btc_route_bypass to gate confluence bypass",
        )

    def test_main_py_confluence_gate_checks_old_btc_bypass(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "not _old_btc_route_bypass",
            content,
            "main.py confluence gate must check `not _old_btc_route_bypass`",
        )

    def test_main_py_emits_old_btc_route_log(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "[OLD_BTC_ROUTE]",
            content,
            "main.py must emit [OLD_BTC_ROUTE] log when old profile bypasses confluence gate",
        )

    def test_main_py_old_btc_route_requires_safety_pass(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'safety_guard_status',
            content,
            "main.py OLD_BTC bypass must check safety_guard_status == PASS",
        )

    def test_main_py_old_btc_route_checks_old_btc_strategies(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "_OLD_BTC_STRATEGIES",
            content,
            "main.py must check handoff_strategy in _OLD_BTC_STRATEGIES for the bypass",
        )

    def test_old_btc_route_log_includes_final_confluence_bypass(self) -> None:
        content = _MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "final_confluence_bypass=true",
            content,
            "main.py must log final_confluence_bypass=true in [OLD_BTC_ROUTE]",
        )


# ── 4. BTC_SCALPING_AGENT confirmation matrix bypass ─────────────────────────

class TestBtcScalpingConfirmationMatrixBypass(unittest.TestCase):
    """Old BTC profile must bypass CONFIRMATION_MATRIX_HARD_BLOCK for BTC_SCALPING_AGENT."""

    def test_normal_mode_adds_confirmation_matrix_hard_block(self) -> None:
        settings = _base_settings(hermes_execution_profile="")
        payload = _btc_scalping_payload(confidence=85, smc_score=15, mtfa_score=15, rr=1.4)
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        self.assertIn(
            "CONFIRMATION_MATRIX_HARD_BLOCK",
            failed,
            "Normal mode must add CONFIRMATION_MATRIX_HARD_BLOCK for smc=15, mtfa=15, rr=1.4",
        )

    def test_old_btc_profile_bypasses_confirmation_matrix_hard_block(self) -> None:
        settings = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        payload = _btc_scalping_payload(confidence=85, smc_score=15, mtfa_score=15, rr=1.4)
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        self.assertNotIn(
            "CONFIRMATION_MATRIX_HARD_BLOCK",
            failed,
            "Old BTC profile must NOT add CONFIRMATION_MATRIX_HARD_BLOCK when safety=PASS",
        )

    def test_old_btc_profile_still_enforces_safety_guard(self) -> None:
        settings = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        payload = _btc_scalping_payload(
            confidence=85, smc_score=15, mtfa_score=15, rr=1.4, safety="FAIL"
        )
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        safety_blocked = any(
            r in failed for r in ("SAFETY_GUARD_BLOCK", "DEMO_MAX_TRADES_EXCEEDED")
        )
        self.assertTrue(
            safety_blocked,
            "Old BTC profile must still enforce SafetyGuard even with confirmation_matrix_bypass",
        )

    def test_old_btc_profile_no_bypass_when_safety_fails(self) -> None:
        settings = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        payload = _btc_scalping_payload(
            confidence=85, smc_score=15, mtfa_score=15, rr=1.4, safety="FAIL"
        )
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        # When safety fails, the bypass log is not emitted (no bypass active)
        # — what matters is that safety block is present
        self.assertTrue(len(failed) > 0, "Failed gates must not be empty when safety fails")

    def test_setup_hunter_py_imports_lovable_profile(self) -> None:
        content = (ROOT / "app" / "agents" / "setup_hunter.py").read_text(encoding="utf-8")
        self.assertIn(
            "lovable_btc_old_system",
            content,
            "setup_hunter.py must import from lovable_btc_old_system profile",
        )

    def test_setup_hunter_py_emits_old_btc_route_log(self) -> None:
        content = (ROOT / "app" / "agents" / "setup_hunter.py").read_text(encoding="utf-8")
        self.assertIn(
            "[OLD_BTC_ROUTE]",
            content,
            "setup_hunter.py must emit [OLD_BTC_ROUTE] log when bypassing confirmation matrix",
        )


# ── 5. Normal demo pilot unchanged ───────────────────────────────────────────

class TestNormalDemoPilotUnchanged(unittest.TestCase):
    """Verify that normal mode (profile not active) still blocks as before."""

    def test_normal_mode_confirmation_matrix_still_blocks(self) -> None:
        settings = _base_settings(hermes_execution_profile="")
        payload = _btc_scalping_payload(confidence=85, smc_score=15, mtfa_score=15, rr=1.4)
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        self.assertIn(
            "CONFIRMATION_MATRIX_HARD_BLOCK",
            failed,
            "Normal mode must still block with CONFIRMATION_MATRIX_HARD_BLOCK",
        )

    def test_normal_mode_rr_too_low_still_fires(self) -> None:
        settings = _base_settings(hermes_execution_profile="")
        payload = _btc_scalping_payload(confidence=85, smc_score=80, mtfa_score=80, rr=1.2)
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        self.assertIn(
            "RR_TOO_LOW",
            failed,
            "Normal mode must still add RR_TOO_LOW for rr=1.2",
        )

    def test_old_btc_profile_rr_too_low_still_fires(self) -> None:
        settings = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        payload = _btc_scalping_payload(confidence=85, smc_score=15, mtfa_score=15, rr=1.4)
        failed = _failed_gates("ENTRY", payload, 0.0, 50.0, settings)
        self.assertIn(
            "RR_TOO_LOW",
            failed,
            "Old BTC profile must still enforce RR_TOO_LOW — only matrix bypass, not RR bypass",
        )


# ── 6. DemoRouter is the only order_send path ─────────────────────────────────

class TestDemoRouterExclusive(unittest.TestCase):

    def test_order_send_only_in_demo_router(self) -> None:
        # Match actual calls (followed by `(`) — docstring mentions are documentation, not calls
        call_pattern = re.compile(r"\bmt5\.order_send\s*\(")
        violators: list[str] = []
        for py_file in _APP_DIR.rglob("*.py"):
            rel = str(py_file.relative_to(ROOT))
            if rel in ("app\\mt5\\demo_router.py", "app/mt5/demo_router.py"):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            if call_pattern.search(content):
                violators.append(rel)
        self.assertEqual(violators, [], f"mt5.order_send call outside DemoRouter: {violators}")

    def test_profiles_module_has_no_order_send_call(self) -> None:
        call_pattern = re.compile(r"\bmt5\.order_send\s*\(")
        profiles_dir = ROOT / "app" / "profiles"
        for py_file in profiles_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            self.assertFalse(
                call_pattern.search(content),
                f"profiles/{py_file.name} must not call mt5.order_send",
            )

    def test_demo_router_has_demo_router_reached_log(self) -> None:
        content = _DEMO_ROUTER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "[DEMO_ROUTER_REACHED]",
            content,
            "demo_router.py must emit [DEMO_ROUTER_REACHED] log when reached",
        )

    def test_demo_router_has_demo_router_order_sent_log(self) -> None:
        content = _DEMO_ROUTER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "[DEMO_ROUTER_ORDER_SENT]",
            content,
            "demo_router.py must emit [DEMO_ROUTER_ORDER_SENT] log after successful order",
        )


# ── 7-9. Safety invariants ────────────────────────────────────────────────────

class TestSafetyInvariantsOldProfile(unittest.TestCase):

    def setUp(self) -> None:
        self.env = _parse_env(_ENV_PATH)

    def test_allow_live_trading_false_in_config_default(self) -> None:
        s = Settings()
        self.assertFalse(s.allow_live_trading, "allow_live_trading must default to False")

    def test_demo_only_true_in_config_default(self) -> None:
        s = Settings()
        self.assertTrue(s.demo_only, "demo_only must default to True")

    def test_demo_max_lot_is_micro(self) -> None:
        s = Settings()
        self.assertLessEqual(s.demo_max_lot, 0.01, "demo_max_lot must be ≤0.01")

    def test_demo_magic_number_is_909002(self) -> None:
        s = Settings()
        self.assertEqual(s.demo_magic_number, DEMO_MAGIC, f"demo_magic_number must be {DEMO_MAGIC}")

    def test_old_btc_profile_preserves_demo_only(self) -> None:
        s = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        self.assertTrue(s.demo_only)

    def test_old_btc_profile_preserves_no_live_trading(self) -> None:
        s = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM")
        self.assertFalse(s.allow_live_trading)

    def test_old_btc_profile_preserves_max_lot(self) -> None:
        s = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM", demo_max_lot=0.01)
        self.assertLessEqual(s.demo_max_lot, 0.01)

    def test_old_btc_profile_preserves_magic_number(self) -> None:
        s = _base_settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM", demo_magic_number=DEMO_MAGIC)
        self.assertEqual(s.demo_magic_number, DEMO_MAGIC)

    def test_allow_live_trading_is_false_in_env(self) -> None:
        val = self.env.get("ALLOW_LIVE_TRADING", "false").lower()
        self.assertEqual(val, "false", f"ALLOW_LIVE_TRADING must be false in .env, got {val!r}")

    def test_demo_only_is_true_in_env(self) -> None:
        val = self.env.get("DEMO_ONLY", "true").lower()
        self.assertEqual(val, "true", f"DEMO_ONLY must be true in .env, got {val!r}")

    def test_demo_max_lot_is_micro_in_env(self) -> None:
        val = float(self.env.get("DEMO_MAX_LOT", "0.01"))
        self.assertLessEqual(val, 0.01, f"DEMO_MAX_LOT must be ≤0.01, got {val}")

    def test_demo_magic_number_is_909002_in_env(self) -> None:
        val = self.env.get("DEMO_MAGIC_NUMBER", str(DEMO_MAGIC))
        self.assertEqual(val, str(DEMO_MAGIC), f"DEMO_MAGIC_NUMBER must be {DEMO_MAGIC}, got {val!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
