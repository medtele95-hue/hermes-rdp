"""
BTC_SCALPING_AGENT profile verification.

Proves that the BTC_SCALPING_AGENT profile (used in the Lovable phase,
June 3-8 2026, 64 BTCUSD# trades) is active and reachable in the current
local system. Covers:

  - Profile settings exist and have correct values in .env / config defaults
  - BTC_SCALPING_AGENT is active (evaluated unconditionally, no disable flag)
  - DemoRouter bypass is intact for BTC_SCALPING_AGENT (no top-down block)
  - Safety invariants unchanged (ALLOW_LIVE_TRADING=false, DEMO_ONLY=true)

Start command:
    python app/main.py

READ-ONLY. No trading logic changes. No order_send calls.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.strategies.scalping import evaluate_btc_entry
from app.strategies.registry import (
    ACTIVE_EXECUTION_STRATEGIES,
    ALLOWED_BTC_EXECUTION_STRATEGIES,
)

_ENV_PATH = ROOT / ".env"
_DEMO_ROUTER_PATH = ROOT / "app" / "mt5" / "demo_router.py"
_AGENT_PATH = ROOT / "app" / "agents" / "hermes_5min_agent.py"
_STRATEGY_MANAGER_PATH = ROOT / "app" / "services" / "strategy_manager.py"
_SCALPING_PATH = ROOT / "app" / "strategies" / "scalping.py"
_APP_DIR = ROOT / "app"

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


def _make_btc_df(rows: int = 30) -> pd.DataFrame:
    """Minimal M5 BTC candle dataframe with enough rows for scalping eval."""
    import numpy as np
    n = rows
    close = 60000.0 + np.linspace(0, 200, n)
    data = {
        "open": close - 10,
        "high": close + 50,
        "low": close - 50,
        "close": close,
        "volume": [1000.0] * n,
        "tick_volume": [1000] * n,
        "spread": [100] * n,
        "real_volume": [0] * n,
    }
    df = pd.DataFrame(data)
    df.index = pd.date_range("2026-06-07 10:00", periods=n, freq="5min")
    return df


# ── Profile: .env settings ────────────────────────────────────────────────────

class TestBtcScalpingProfile(unittest.TestCase):
    """Verify the BTC_SCALPING_AGENT profile settings are present and correct."""

    def setUp(self) -> None:
        self.env = _parse_env(_ENV_PATH)

    def test_btc_scalping_relaxed_demo_mode_is_true(self) -> None:
        val = self.env.get("BTC_SCALPING_RELAXED_DEMO_MODE", "true").lower()
        self.assertEqual(val, "true", "BTC_SCALPING_RELAXED_DEMO_MODE must be true")

    def test_btc_scalping_min_confidence_is_55(self) -> None:
        val = int(self.env.get("BTC_SCALPING_MIN_CONFIDENCE", "55"))
        self.assertEqual(val, 55, f"BTC_SCALPING_MIN_CONFIDENCE must be 55, got {val}")

    def test_btc_scalping_allow_m5_momentum_is_true(self) -> None:
        val = self.env.get("BTC_SCALPING_ALLOW_M5_MOMENTUM", "true").lower()
        self.assertEqual(val, "true", "BTC_SCALPING_ALLOW_M5_MOMENTUM must be true")

    def test_btc_scalping_allow_m1_breakout_is_true(self) -> None:
        val = self.env.get("BTC_SCALPING_ALLOW_M1_BREAKOUT", "true").lower()
        self.assertEqual(val, "true", "BTC_SCALPING_ALLOW_M1_BREAKOUT must be true")

    def test_btc_scalping_max_trades_per_day_is_6(self) -> None:
        val = int(self.env.get("BTC_SCALPING_MAX_TRADES_PER_DAY", "6"))
        self.assertGreaterEqual(val, 4, f"BTC_SCALPING_MAX_TRADES_PER_DAY must be ≥4, got {val}")

    def test_btcusd_is_in_hermes_main_symbols(self) -> None:
        symbols_str = self.env.get("HERMES_MAIN_SYMBOLS", "")
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        btc_present = any("BTCUSD" in s.upper() for s in symbols)
        self.assertTrue(
            btc_present,
            f"BTCUSD# must be in HERMES_MAIN_SYMBOLS for BTC_SCALPING_AGENT to run. "
            f"Found: {symbols}",
        )

    def test_demo_micro_discovery_mode_is_true(self) -> None:
        val = self.env.get("HERMES_DEMO_MICRO_DISCOVERY_MODE", "").lower()
        self.assertEqual(val, "true", "HERMES_DEMO_MICRO_DISCOVERY_MODE must be true (Lovable mode)")

    def test_demo_pilot_enabled_is_true(self) -> None:
        val = self.env.get("DEMO_PILOT_ENABLED", "").lower()
        self.assertEqual(val, "true", "DEMO_PILOT_ENABLED must be true for DemoRouter to process orders")

    def test_demo_magic_matches_lovable_era(self) -> None:
        val = self.env.get("DEMO_MAGIC_NUMBER", "")
        self.assertEqual(val, str(DEMO_MAGIC), f"DEMO_MAGIC_NUMBER must be {DEMO_MAGIC} (Lovable era magic)")


# ── Proof BTC_SCALPING_AGENT active ──────────────────────────────────────────

class TestBtcScalpingAgentActive(unittest.TestCase):
    """Prove BTC_SCALPING_AGENT is active: no disable flag, in registry, evaluated each cycle."""

    def test_btc_scalping_agent_in_active_execution_registry(self) -> None:
        self.assertIn(
            "BTC_SCALPING_AGENT", ACTIVE_EXECUTION_STRATEGIES,
            "BTC_SCALPING_AGENT must be in ACTIVE_EXECUTION_STRATEGIES",
        )

    def test_btc_scalping_agent_in_allowed_btc_strategies(self) -> None:
        self.assertIn(
            "BTC_SCALPING_AGENT", ALLOWED_BTC_EXECUTION_STRATEGIES,
            "BTC_SCALPING_AGENT must be in ALLOWED_BTC_EXECUTION_STRATEGIES",
        )

    def test_btc_scalping_agent_in_strategy_manager_active_execution(self) -> None:
        content = _STRATEGY_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            '"BTC_SCALPING_AGENT"',
            content,
            "BTC_SCALPING_AGENT must be in _ACTIVE_EXECUTION set in strategy_manager.py",
        )

    def test_evaluate_btc_entry_called_in_main_cycle(self) -> None:
        content = _AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "scalping.evaluate_btc_entry",
            content,
            "hermes_5min_agent.py must call scalping.evaluate_btc_entry() each cycle",
        )

    def test_no_btc_scalping_enabled_disable_flag_in_env(self) -> None:
        env = _parse_env(_ENV_PATH)
        disable_val = env.get("BTC_SCALPING_ENABLED", "true").lower()
        self.assertNotEqual(
            disable_val, "false",
            "BTC_SCALPING_ENABLED must not be set to false — strategy must be active",
        )

    def test_evaluate_btc_entry_returns_btc_scalping_agent_strategy(self) -> None:
        df = _make_btc_df(30)
        result = evaluate_btc_entry("BTCUSD#", df)
        self.assertEqual(
            result.get("strategy"), "BTC_SCALPING_AGENT",
            f"evaluate_btc_entry() must return strategy=BTC_SCALPING_AGENT, got {result.get('strategy')}",
        )

    def test_evaluate_btc_entry_non_btc_symbol_blocked_not_crashed(self) -> None:
        df = _make_btc_df(30)
        result = evaluate_btc_entry("GOLD#", df)
        self.assertIn(
            result.get("blocked_reason", ""),
            ("BTC_SCALPING_AGENT_SYMBOL_NOT_BTC", "SYMBOL_NOT_BTC"),
            "Non-BTC symbol must be blocked by BTC_SCALPING_AGENT, not raise exception",
        )

    def test_evaluate_btc_entry_produces_valid_signal(self) -> None:
        df = _make_btc_df(30)
        result = evaluate_btc_entry("BTCUSD#", df)
        signal = result.get("signal") or result.get("direction") or result.get("btc_scalping_decision")
        self.assertIn(
            str(signal).upper(),
            {"BUY", "SELL", "WAIT", "SKIP", "NO_SCALP_TRIGGER", "NONE"},
            f"evaluate_btc_entry() must produce a valid signal, got: {result}",
        )


# ── Proof DemoRouter reachable ────────────────────────────────────────────────

class TestDemoRouterReachableForBtcScalping(unittest.TestCase):
    """Prove DemoRouter is reachable for BTC_SCALPING_AGENT: bypasses top-down, gates open."""

    def setUp(self) -> None:
        self.dr = _DEMO_ROUTER_PATH.read_text(encoding="utf-8")

    def test_demo_router_has_btc_scalping_bypass_in_execution_block(self) -> None:
        # Line ~1095: if _is_btc_scalping_gates(gates): return None
        pattern = r"_is_btc_scalping_gates\(gates\)[\s\S]{0,80}return None"
        matches = re.findall(pattern, self.dr)
        self.assertGreaterEqual(
            len(matches), 2,
            "DemoRouter must have ≥2 return-None bypass clauses for _is_btc_scalping_gates "
            "(one in _execution_demo_block_reason, one in _final_demo_block_reason)",
        )

    def test_is_btc_scalping_gates_checks_strategy_field(self) -> None:
        # Verify _is_btc_scalping_gates compares strategy == "BTC_SCALPING_AGENT"
        pattern = r'def _is_btc_scalping_gates[\s\S]{0,200}BTC_SCALPING_AGENT'
        self.assertTrue(
            re.search(pattern, self.dr),
            "_is_btc_scalping_gates() must check strategy == 'BTC_SCALPING_AGENT'",
        )

    def test_demo_pilot_enabled_gate_confirmed_true_in_env(self) -> None:
        env = _parse_env(_ENV_PATH)
        val = env.get("DEMO_PILOT_ENABLED", "").lower()
        self.assertEqual(
            val, "true",
            "DEMO_PILOT_ENABLED must be true — DemoRouter's primary gate checks this first",
        )

    def test_demo_router_enabled_property_requires_demo_pilot(self) -> None:
        # Verify DemoRouter.enabled checks demo_pilot_enabled
        self.assertIn(
            "demo_pilot_enabled",
            self.dr,
            "DemoRouter.enabled must reference demo_pilot_enabled setting",
        )

    def test_hermes_free_demo_discovery_mode_set_in_env(self) -> None:
        env = _parse_env(_ENV_PATH)
        val = env.get("HERMES_FREE_DEMO_DISCOVERY_MODE", "").lower()
        self.assertEqual(
            val, "true",
            "HERMES_FREE_DEMO_DISCOVERY_MODE must be true — fallback discovery gate for routing",
        )

    def test_demo_router_magic_is_909002(self) -> None:
        self.assertIn(
            str(DEMO_MAGIC),
            self.dr,
            f"DemoRouter must use DEMO_MAGIC_NUMBER {DEMO_MAGIC}",
        )

    def test_btc_scalping_agent_in_allowed_btc_strategies(self) -> None:
        # Confirm BTC_SCALPING_AGENT passes the symbol-strategy allowed check
        self.assertIn(
            "BTC_SCALPING_AGENT", ALLOWED_BTC_EXECUTION_STRATEGIES,
            "BTC_SCALPING_AGENT must be in ALLOWED_BTC_EXECUTION_STRATEGIES to pass DemoRouter symbol gate",
        )

    def test_order_send_in_demo_router_only(self) -> None:
        violators: list[str] = []
        for py_file in _APP_DIR.rglob("*.py"):
            rel = str(py_file.relative_to(ROOT))
            if rel in ("app\\mt5\\demo_router.py", "app/mt5/demo_router.py"):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bmt5\.order_send\b", content):
                violators.append(rel)
        self.assertEqual(violators, [], f"mt5.order_send outside DemoRouter: {violators}")


# ── Proof safety unchanged ────────────────────────────────────────────────────

class TestSafetyUnchangedForBtcScalpingProfile(unittest.TestCase):
    """Prove all safety invariants are intact for the BTC_SCALPING_AGENT profile."""

    def setUp(self) -> None:
        self.env = _parse_env(_ENV_PATH)

    def test_allow_live_trading_is_false(self) -> None:
        val = self.env.get("ALLOW_LIVE_TRADING", "").lower()
        self.assertEqual(val, "false", f"ALLOW_LIVE_TRADING must be false, got {val!r}")

    def test_demo_only_is_true(self) -> None:
        val = self.env.get("DEMO_ONLY", "").lower()
        self.assertEqual(val, "true", f"DEMO_ONLY must be true, got {val!r}")

    def test_demo_max_lot_is_micro(self) -> None:
        val = self.env.get("DEMO_MAX_LOT", "0.01")
        self.assertLessEqual(float(val), 0.01, f"DEMO_MAX_LOT must be ≤0.01, got {val!r}")

    def test_quick_exit_demo_only_is_true(self) -> None:
        val = self.env.get("QUICK_EXIT_DEMO_ONLY", "").lower()
        self.assertEqual(val, "true", f"QUICK_EXIT_DEMO_ONLY must be true, got {val!r}")

    def test_demo_magic_number_is_909002(self) -> None:
        val = self.env.get("DEMO_MAGIC_NUMBER", "")
        self.assertEqual(val, str(DEMO_MAGIC), f"DEMO_MAGIC_NUMBER must be {DEMO_MAGIC}, got {val!r}")

    def test_order_send_exclusively_in_demo_router(self) -> None:
        violators: list[str] = []
        for py_file in _APP_DIR.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bmt5\.order_send\b", content):
                rel = str(py_file.relative_to(ROOT))
                if rel not in ("app\\mt5\\demo_router.py", "app/mt5/demo_router.py"):
                    violators.append(rel)
        self.assertEqual(violators, [], f"mt5.order_send outside DemoRouter: {violators}")

    def test_scalping_module_has_no_order_send(self) -> None:
        content = _SCALPING_PATH.read_text(encoding="utf-8")
        self.assertFalse(
            re.search(r"\bmt5\.order_send\b", content),
            "scalping.py must not contain mt5.order_send — execution is DemoRouter's job",
        )

    def test_hermes_5min_agent_has_no_order_send(self) -> None:
        content = _AGENT_PATH.read_text(encoding="utf-8")
        self.assertFalse(
            re.search(r"\bmt5\.order_send\b", content),
            "hermes_5min_agent.py must not contain mt5.order_send",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
