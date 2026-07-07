"""BLOC 9 — DECISION_DATASET tests.

Proves:
- append-only JSONL with schema_version; every decision (executed or
  refused) becomes one flattened row (~scores, flags, ees, exec quality,
  account_policy, session, regime, momentum_alignment).
- momentum_alignment: 3 votes (5 closed M1, sign cvd_slope, sign delta),
  >=2 -> BULL/BEAR, missing vote counts 0 -> NEUTRAL, then
  ALIGNED/NEUTRAL/AGAINST. Pure shadow.
- Outcome tracker: MFE/MAE + virtual outcome for refusals + pnl
  reconciled from MT5 deals for real tickets.
- Fail-silent: dataset failures never raise into the trading path.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.services.decision_dataset import (
    DecisionDataset,
    atr_percentile,
    build_decision_row,
    kill_zone_active,
    momentum_alignment,
)


def _m1_rows(direction: str) -> list[dict]:
    step = 1.0 if direction == "up" else -1.0
    rows = []
    price = 100.0
    for _ in range(6):
        rows.append({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price + step})
        price += step
    return rows


class TestMomentumAlignment(unittest.TestCase):
    def test_three_bull_votes_aligned_for_buy(self) -> None:
        result = momentum_alignment("BUY", _m1_rows("up"), cvd_slope=1.5, delta=200.0)
        self.assertEqual(result["consensus"], "BULL")
        self.assertEqual(result["alignment"], "ALIGNED")

    def test_bear_consensus_against_buy(self) -> None:
        result = momentum_alignment("BUY", _m1_rows("down"), cvd_slope=-1.0, delta=-50.0)
        self.assertEqual(result["consensus"], "BEAR")
        self.assertEqual(result["alignment"], "AGAINST")

    def test_two_of_three_is_enough(self) -> None:
        result = momentum_alignment("SELL", _m1_rows("down"), cvd_slope=-1.0, delta=None)
        self.assertEqual(result["consensus"], "BEAR")
        self.assertEqual(result["alignment"], "ALIGNED")

    def test_missing_votes_count_zero_neutral(self) -> None:
        result = momentum_alignment("BUY", _m1_rows("up"), cvd_slope=None, delta=None)
        self.assertEqual(result["consensus"], "NEUTRAL")
        self.assertEqual(result["alignment"], "NEUTRAL")
        self.assertEqual(result["votes"]["cvd_slope"], 0)
        self.assertEqual(result["votes"]["delta"], 0)

    def test_no_m1_data_vote_zero(self) -> None:
        result = momentum_alignment("BUY", None, cvd_slope=1.0, delta=-1.0)
        self.assertEqual(result["votes"]["m1_last5"], 0)
        self.assertEqual(result["consensus"], "NEUTRAL")


class TestRegimeFeatures(unittest.TestCase):
    def test_atr_percentile_high_in_expanding_vol(self) -> None:
        rows = []
        for i in range(120):
            spread = 1.0 + i * 0.05  # expanding true range
            rows.append({"open": 100.0, "high": 100.0 + spread, "low": 100.0 - spread, "close": 100.0})
        frame = pd.DataFrame(rows)
        pct = atr_percentile(frame)
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 90.0)

    def test_atr_percentile_none_on_short_frame(self) -> None:
        frame = pd.DataFrame([{"open": 1, "high": 2, "low": 0.5, "close": 1.5}] * 5)
        self.assertIsNone(atr_percentile(frame))

    def test_kill_zone_active(self) -> None:
        from datetime import datetime, timezone

        self.assertTrue(kill_zone_active(datetime(2026, 7, 7, 8, 0, tzinfo=timezone.utc)))
        self.assertTrue(kill_zone_active(datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)))
        self.assertFalse(kill_zone_active(datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)))


def _rich_event(**overrides) -> dict:
    event = {
        "event_type": "DEMO_ORDER",
        "symbol": "GOLD#",
        "broker_symbol": "GOLD#",
        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
        "direction": "BUY",
        "entry": 3300.0,
        "sl": 3295.0,
        "tp": 3312.0,
        "reason": None,
        "setup_id": "setup-9",
        "ticket": 42,
        "order_success": True,
        "account_policy": "DEMO",
        "session_name": "LONDON",
        "ees_score": 22.0,
        "ees_band": "SAIN",
        "ees_buy": 22.0,
        "ees_sell": None,
        "atr": 4.0,
        "spread": 2.0,
        "exec_quality": {"fill_latency_ms": 12.5, "slippage_vs_tick_points": 3.0},
        "gate_statuses": {"rr": 2.4, "symbol_allowed": True},
        "account_policy_detail": {"level": "DEMO", "max_losses_per_day": 6},
        "daily_killswitch": {"triggered": False, "losses_today": 0},
        "order_flow_execution_agent": {"cvd_slope": 1.2, "delta": 150.0, "vwap": 3298.0},
    }
    event.update(overrides)
    return event


class TestDatasetRows(unittest.TestCase):
    def _dataset(self, name: str) -> DecisionDataset:
        path = Path("tests") / "__tmp_bloc9_dataset" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        return DecisionDataset(path)

    def test_row_flattens_features_with_schema_version(self) -> None:
        row = build_decision_row(_rich_event())
        self.assertEqual(row["row_type"], "decision")
        self.assertEqual(row["schema_version"], 2)
        self.assertEqual(row["exec_quality.fill_latency_ms"], 12.5)
        self.assertEqual(row["gate_statuses.rr"], 2.4)
        self.assertEqual(row["daily_killswitch.triggered"], False)
        self.assertEqual(row["ees_band"], "SAIN")
        self.assertAlmostEqual(row["net_rr"], 2.4)
        self.assertAlmostEqual(row["spread_to_atr"], 0.5)
        self.assertIn("momentum_alignment.alignment", row)
        self.assertIn("regime.kill_zone_active", row)

    def test_append_only_jsonl(self) -> None:
        dataset = self._dataset("append")
        dataset.record_decision(_rich_event())
        dataset.record_decision(_rich_event(direction="SELL", sl=3305.0, tp=3288.0))
        lines = dataset.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = json.loads(line)
            self.assertEqual(parsed["schema_version"], 2)

    def test_fail_silent_on_unwritable_path(self) -> None:
        bad_dir = Path("tests") / "__tmp_bloc9_dataset" / "as_dir.jsonl"
        bad_dir.mkdir(parents=True, exist_ok=True)  # a DIRECTORY at the file path
        dataset = DecisionDataset(bad_dir)
        result = dataset.record_decision(_rich_event())  # must not raise
        self.assertIsNone(result)


class TestOutcomeTracker(unittest.TestCase):
    def _dataset(self, name: str) -> DecisionDataset:
        path = Path("tests") / "__tmp_bloc9_dataset" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        return DecisionDataset(path)

    def test_virtual_outcome_for_refused_decision(self) -> None:
        dataset = self._dataset("virtual")
        dataset.record_decision(_rich_event(order_success=False, ticket=None, reason="NEWS_BLACKOUT"))
        self.assertEqual(dataset.tracker.open_count(), 1)
        dataset.tracker.update({"GOLD#": 3305.0})  # MFE update, no close
        outcomes = dataset.tracker.update({"GOLD#": 3312.5})  # TP hit
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertTrue(outcome["virtual"])
        self.assertEqual(outcome["outcome"], "TP_HIT")
        self.assertGreaterEqual(outcome["mfe"], 12.0)
        self.assertIsNotNone(outcome["virtual_r_multiple"])
        rows = [json.loads(line) for line in dataset.path.read_text(encoding="utf-8").strip().splitlines()]
        self.assertTrue(any(r["row_type"] == "outcome" for r in rows))

    def test_mae_tracked_until_sl(self) -> None:
        dataset = self._dataset("mae")
        dataset.record_decision(_rich_event(order_success=False, ticket=None))
        dataset.tracker.update({"GOLD#": 3308.0})
        outcomes = dataset.tracker.update({"GOLD#": 3294.5})  # SL hit
        self.assertEqual(outcomes[0]["outcome"], "SL_HIT")
        self.assertGreaterEqual(outcomes[0]["mfe"], 8.0)
        self.assertLessEqual(outcomes[0]["mae"], -5.0)

    def test_real_ticket_pnl_reconciled_from_deals(self) -> None:
        dataset = self._dataset("real")
        dataset.record_decision(_rich_event())  # order_success=True, ticket 42

        def deals_fn(ticket):
            self.assertEqual(int(ticket), 42)
            return [SimpleNamespace(profit=-5.0, commission=-0.1, swap=0.0)]

        outcomes = dataset.tracker.update({"GOLD#": 3294.0}, deals_fn=deals_fn)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0]["virtual"])
        self.assertEqual(outcomes[0]["pnl_reconciled"], -5.1)
        self.assertEqual(outcomes[0]["pnl_source"], "MT5_HISTORY_DEALS")

    def test_wait_decisions_not_tracked(self) -> None:
        dataset = self._dataset("wait")
        dataset.record_decision(_rich_event(direction="WAIT", entry=None, sl=None, tp=None))
        self.assertEqual(dataset.tracker.open_count(), 0)


if __name__ == "__main__":
    unittest.main()
