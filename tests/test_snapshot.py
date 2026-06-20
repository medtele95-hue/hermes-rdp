"""Tests for account_snapshots PnL enrichment.

Verifies:
- _enrich_account_snapshot populates daily_pnl, total_pnl, floating_pnl from position_sync.
- PnL fields are not NULL when MT5 position_sync provides values.
- Fields remain None when position_sync has no data, with log warning.
- Source field is populated from pnl_source.
- open field reflects hermes open count.
- Existing account fields (balance, equity) are preserved.
- Safety invariants unchanged.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.services.heartbeat_service import _enrich_account_snapshot


def _account_snapshot() -> dict:
    return {
        "login": 123456,
        "balance": 10000.0,
        "equity": 10050.0,
        "profit": 50.0,
        "margin": 500.0,
        "margin_free": 9550.0,
        "snapshot_time": "2026-06-12T10:00:00+00:00",
    }


def _position_sync(
    closed_pnl: float = 12.50,
    floating_pnl: float = 3.75,
    total_pnl: float = 16.25,
    pnl_source: str = "MT5_HISTORY_DEALS",
    open_count: int = 2,
) -> dict:
    return {
        "demo_closed_pnl_today": closed_pnl,
        "demo_floating_pnl": floating_pnl,
        "demo_total_pnl_today": total_pnl,
        "pnl_source": pnl_source,
        "hermes_mt5_open_positions_count": open_count,
        "open_demo_trades_count": open_count,
    }


class TestEnrichAccountSnapshot(unittest.TestCase):
    def test_closed_pnl_populated(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(closed_pnl=12.50))
        self.assertAlmostEqual(enriched["closed_pnl"], 12.50, places=4)

    def test_floating_pnl_populated(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(floating_pnl=3.75))
        self.assertAlmostEqual(enriched["floating_pnl"], 3.75, places=4)

    def test_total_pnl_populated(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(total_pnl=16.25))
        self.assertAlmostEqual(enriched["total"], 16.25, places=4)

    def test_daily_pnl_mirrors_closed_pnl(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(closed_pnl=12.50))
        self.assertAlmostEqual(enriched["daily_pnl"], 12.50, places=4)

    def test_total_pnl_field_mirrors_total(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(total_pnl=16.25))
        self.assertAlmostEqual(enriched["total_pnl"], 16.25, places=4)

    def test_source_populated(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(pnl_source="MT5_HISTORY_DEALS"))
        self.assertEqual(enriched["source"], "MT5_HISTORY_DEALS")

    def test_open_count_populated(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(open_count=2))
        self.assertEqual(enriched["open"], 2)

    def test_original_balance_preserved(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync())
        self.assertAlmostEqual(enriched["balance"], 10000.0, places=2)

    def test_original_equity_preserved(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync())
        self.assertAlmostEqual(enriched["equity"], 10050.0, places=2)

    def test_utc_time_set_from_snapshot_time(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync())
        self.assertIn("utc_time", enriched)
        self.assertIsNotNone(enriched["utc_time"])

    def test_pnl_fields_none_when_empty_sync(self) -> None:
        empty_sync: dict = {}
        with self.assertLogs("hermes", level="INFO") as cm:
            enriched = _enrich_account_snapshot(_account_snapshot(), empty_sync)
        self.assertIsNone(enriched["closed_pnl"])
        self.assertIsNone(enriched["floating_pnl"])
        self.assertIsNone(enriched["total"])
        missing_logs = [m for m in cm.output if "SNAPSHOT_FIELD_MISSING" in m]
        self.assertGreater(len(missing_logs), 0, "Expected SNAPSHOT_FIELD_MISSING log when fields unavailable")

    def test_fallback_source_trades_table(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(pnl_source="TRADES_TABLE_FALLBACK"))
        self.assertEqual(enriched["source"], "TRADES_TABLE_FALLBACK")

    def test_zero_values_are_not_treated_as_missing(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(closed_pnl=0.0, floating_pnl=0.0))
        self.assertIsNotNone(enriched["closed_pnl"])
        self.assertAlmostEqual(enriched["closed_pnl"], 0.0, places=4)

    def test_negative_pnl_values_preserved(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync(closed_pnl=-5.25, floating_pnl=-2.0))
        self.assertAlmostEqual(enriched["closed_pnl"], -5.25, places=4)
        self.assertAlmostEqual(enriched["floating_pnl"], -2.0, places=4)


class TestSnapshotSafetyInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_order_send_only_in_demo_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in (root / "app").rglob("*.py"):
            if path.as_posix().endswith("app/mt5/demo_router.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_enrich_does_not_add_secrets(self) -> None:
        enriched = _enrich_account_snapshot(_account_snapshot(), _position_sync())
        enriched_str = str(enriched)
        for word in ("password", "secret", "api_key", "service_role"):
            self.assertNotIn(word, enriched_str.lower())

    def test_max_lot_not_changed_by_enrichment(self) -> None:
        s = Settings()
        _enrich_account_snapshot(_account_snapshot(), _position_sync())
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)


if __name__ == "__main__":
    unittest.main()
