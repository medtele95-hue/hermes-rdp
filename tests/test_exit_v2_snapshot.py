# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — Exit V2 read-only state snapshot for the dashboard.
Must never affect real exit decisions: it's a passive mirror written after
the fact, wrapped in its own try/except at the call site so a write failure
can never cascade into fail-closed exit skipping."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.mt5 import demo_router
from app.services.exit_v2 import ExitV2Config


class TestWriteExitV2Snapshot(unittest.TestCase):
    def test_writes_expected_shape(self) -> None:
        action = {
            "action": "NONE", "reason": "EXIT_V2_HOLD", "profit_usd": 1.5,
            "peak_usd": 2.0, "be_armed": True, "active_floor": 0.8,
        }
        cfg = ExitV2Config()
        with TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "exit_v2_state.json"
            with patch.object(demo_router, "EXIT_V2_SNAPSHOT_PATH", snapshot_path):
                demo_router._write_exit_v2_snapshot(12345, "GOLD#", action, cfg, shadow=False, account_type="DEMO")
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        entry = data["12345"]
        self.assertEqual(entry["symbol"], "GOLD#")
        self.assertEqual(entry["action"], "NONE")
        self.assertEqual(entry["be_armed"], True)
        self.assertEqual(entry["mode"], "ACTIVE")
        self.assertEqual(entry["account_type"], "DEMO")
        self.assertIn("updated_at", entry)

    def test_merges_with_existing_tickets(self) -> None:
        cfg = ExitV2Config()
        with TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "exit_v2_state.json"
            snapshot_path.write_text(json.dumps({"999": {"symbol": "BTCUSD#"}}), encoding="utf-8")
            with patch.object(demo_router, "EXIT_V2_SNAPSHOT_PATH", snapshot_path):
                demo_router._write_exit_v2_snapshot(111, "GOLD#", {}, cfg, shadow=True, account_type="DEMO")
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertIn("999", data)
        self.assertIn("111", data)

    def test_corrupt_existing_file_does_not_raise(self) -> None:
        cfg = ExitV2Config()
        with TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "exit_v2_state.json"
            snapshot_path.write_text("{not valid json", encoding="utf-8")
            with patch.object(demo_router, "EXIT_V2_SNAPSHOT_PATH", snapshot_path):
                demo_router._write_exit_v2_snapshot(111, "GOLD#", {}, cfg, shadow=True, account_type="DEMO")
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertIn("111", data)

    def test_call_site_never_raises_when_snapshot_write_fails(self) -> None:
        """The caller (_process_exit_v2_position) wraps the snapshot write in
        its OWN try/except separate from the outer fail-closed except — a
        snapshot write failure must never be mistaken for a real exit
        evaluation failure."""
        import inspect
        source = inspect.getsource(demo_router.DemoKellyRouter._process_exit_v2_position)
        idx_snapshot = source.index("_write_exit_v2_snapshot")
        idx_close_check = source.index('!= "CLOSE"')
        # the snapshot call must be wrapped in its own try/except BEFORE the
        # CLOSE/NONE branching continues — verified structurally via the
        # presence of a dedicated try/except immediately around the call.
        surrounding = source[max(0, idx_snapshot - 60):idx_snapshot + 40]
        self.assertIn("try:", surrounding)
        self.assertLess(idx_snapshot, idx_close_check)


if __name__ == "__main__":
    unittest.main()
