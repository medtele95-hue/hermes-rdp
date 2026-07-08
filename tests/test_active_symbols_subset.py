# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — dashboard-controlled symbol subset, applied AFTER
the hard SYMBOL_ALLOWLIST invariant. Can only narrow it, never widen it."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.mt5 import demo_router


class TestActiveSymbolsSubset(unittest.TestCase):
    def test_defaults_to_full_allowlist_when_file_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", Path(tmp) / "missing.json"):
                self.assertEqual(demo_router._active_symbols_subset(), frozenset(demo_router.SYMBOL_ALLOWLIST))

    def test_narrows_to_gold_only(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text(json.dumps(["GOLD#"]), encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                self.assertEqual(demo_router._active_symbols_subset(), frozenset({"GOLD#"}))

    def test_cannot_widen_beyond_allowlist(self) -> None:
        """A malicious or corrupt file listing symbols outside
        SYMBOL_ALLOWLIST must never be able to add trading exposure —
        those entries are silently dropped, never honored."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text(json.dumps(["GOLD#", "EURUSD#", "US100Cash#"]), encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                result = demo_router._active_symbols_subset()
        self.assertEqual(result, frozenset({"GOLD#"}))
        self.assertNotIn("EURUSD#", result)
        self.assertNotIn("US100Cash#", result)

    def test_empty_list_fails_open_to_full_allowlist(self) -> None:
        """An empty active-symbols file must never silently halt ALL trading
        with no visible cause — fails open, not closed."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                self.assertEqual(demo_router._active_symbols_subset(), frozenset(demo_router.SYMBOL_ALLOWLIST))

    def test_corrupt_file_fails_open_to_full_allowlist(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text("{not valid json", encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                self.assertEqual(demo_router._active_symbols_subset(), frozenset(demo_router.SYMBOL_ALLOWLIST))


class TestExecutionInvariantsBlockWithSubset(unittest.TestCase):
    def test_hard_allowlist_check_runs_before_subset_check(self) -> None:
        """A symbol outside SYMBOL_ALLOWLIST must still be SYMBOL_BLOCKED
        (the hard invariant reason), never DASHBOARD_SYMBOL_DISABLED —
        the dashboard subset must never be able to mask/replace the
        original hard-invariant block reason."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text(json.dumps(["GOLD#", "BTCUSD#"]), encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                reason = demo_router._execution_invariants_block(
                    {"symbol": "EURUSD#", "sl": 1.0, "tp": 1.0, "volume": 0.01, "magic": 909002}, "TEST",
                )
        self.assertEqual(reason, "SYMBOL_BLOCKED")

    def test_disabled_via_dashboard_when_subset_excludes_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "active_symbols.json"
            path.write_text(json.dumps(["BTCUSD#"]), encoding="utf-8")
            with patch.object(demo_router, "ACTIVE_SYMBOLS_FILE", path):
                reason = demo_router._execution_invariants_block(
                    {"symbol": "GOLD#", "sl": 1.0, "tp": 1.0, "volume": 0.01, "magic": 909002}, "TEST",
                )
        self.assertEqual(reason, "SYMBOL_DISABLED_VIA_DASHBOARD")


if __name__ == "__main__":
    unittest.main()
