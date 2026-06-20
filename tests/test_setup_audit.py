"""
Tests for app/tools/setup_audit.py — 48-hour setup pipeline audit tool.

Coverage:
- determine_final_status()
- extract_stages()
- extract_block_reason()
- extract_setup_reason()
- generate_summary()
- load_events() (with temp JSONL)
- export_json(), export_csv(), export_markdown()
- run_audit() integration
- Part F: top-down missing fields, status reclassification, filename tags
- Safety invariant: NO execution, NO order_send, NO live trading
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tools.setup_audit import (
    determine_final_status,
    extract_block_reason,
    extract_setup_reason,
    extract_stages,
    export_csv,
    export_json,
    export_markdown,
    generate_summary,
    load_events,
    parse_event,
    run_audit,
    DEFAULT_EVENTS_PATH,
    ALL_STAGES,
    FINAL_STATUSES,
    _TOP_DOWN_MISSING_REASONS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(hours_ago: float = 1.0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat()


def _sh_reject(**kw) -> dict:
    """SETUP_HUNTER event that should be rejected (execution_candidate=False)."""
    base: dict = {
        "event_type": "SETUP_HUNTER",
        "created_at": _ts(1.0),
        "broker_symbol": "GOLD#",
        "strategy": "NONE",
        "execution_candidate": False,
        "demo_eligible": False,
        "execution_policy": "NONE",
        "time_gate_status": "PASS",
        "session": "LONDON",
        "missing_confirmations": [],
        "smc_status": "FAIL",
        "mtfa_status": "FAIL",
        "top_down_status": "FAIL",
    }
    base.update(kw)
    return base


def _sh_accept(**kw) -> dict:
    """SETUP_HUNTER event accepted by SetupHunter but blocked at router."""
    base: dict = {
        "event_type": "SETUP_HUNTER",
        "created_at": _ts(1.0),
        "broker_symbol": "GOLD#",
        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
        "execution_candidate": True,
        "demo_eligible": True,
        "execution_policy": "EXECUTABLE",
        "time_gate_status": "PASS",
        "session": "OVERLAP",
        "missing_confirmations": ["TOP_DOWN_DATA_MISSING"],
        "smc_status": "FAIL",
        "mtfa_status": "FAIL",
        "top_down_status": "FAIL",
    }
    base.update(kw)
    return base


def _sh_accept_clean(**kw) -> dict:
    """SETUP_HUNTER accepted with no missing confirmations (should be ACCEPTED_BUT_NOT_SENT)."""
    base: dict = {
        "event_type": "SETUP_HUNTER",
        "created_at": _ts(1.0),
        "broker_symbol": "EURUSD",
        "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
        "execution_candidate": True,
        "demo_eligible": True,
        "execution_policy": "EXECUTABLE",
        "time_gate_status": "PASS",
        "session": "LONDON",
        "missing_confirmations": [],
        "smc_status": "PASS",
        "mtfa_status": "PASS",
        "top_down_status": "PASS",
    }
    base.update(kw)
    return base


def _demo_skip(**kw) -> dict:
    base: dict = {
        "event_type": "DEMO_SKIP",
        "created_at": _ts(0.5),
        "broker_symbol": "GOLD#",
        "strategy": "GOLD_LIQUIDITY_HUNTER_PRO",
        "direction": "SELL",
        "allowed_symbol_check": "BLOCK",
    }
    base.update(kw)
    return base


def _position_sync(**kw) -> dict:
    base: dict = {
        "event_type": "POSITION_SYNC",
        "created_at": _ts(0.25),
        "broker_symbol": "GOLD#",
        "result": "CLOSED",
        "close_reason": "MT5_POSITION_MISSING_CLOSED",
        "source": "MT5_POSITIONS",
    }
    base.update(kw)
    return base


def _demo_order(**kw) -> dict:
    base: dict = {
        "event_type": "DEMO_ORDER",
        "created_at": _ts(0.1),
        "broker_symbol": "EURUSD",
        "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
        "direction": "BUY",
    }
    base.update(kw)
    return base


def _demo_router_sent(**kw) -> dict:
    base: dict = {
        "event_type": "DEMO_ROUTER_ORDER_SENT",
        "created_at": _ts(0.1),
        "broker_symbol": "EURUSD",
        "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
        "direction": "BUY",
    }
    base.update(kw)
    return base


def _write_jsonl(events: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


# ── TestDetermineStatus ───────────────────────────────────────────────────────

class TestDetermineStatusPositionSync(unittest.TestCase):
    def test_closed_position_sync_returns_mt5_synced_closed(self) -> None:
        self.assertEqual(determine_final_status(_position_sync(result="CLOSED")), "MT5_SYNCED_CLOSED")

    def test_open_position_sync_returns_mt5_synced_closed(self) -> None:
        self.assertEqual(determine_final_status(_position_sync(result="OPEN")), "MT5_SYNCED_CLOSED")

    def test_position_sync_never_returns_executed_by_hermes(self) -> None:
        status = determine_final_status(_position_sync())
        self.assertNotEqual(status, "EXECUTED_BY_HERMES")


class TestDetermineStatusDemoSkip(unittest.TestCase):
    def test_allowed_symbol_check_block_returns_accepted_but_router_blocked(self) -> None:
        self.assertEqual(
            determine_final_status(_demo_skip(allowed_symbol_check="BLOCK")),
            "ACCEPTED_BUT_ROUTER_BLOCKED",
        )

    def test_allowed_symbol_check_pass_returns_sent(self) -> None:
        self.assertEqual(
            determine_final_status(_demo_skip(allowed_symbol_check="PASS")),
            "SENT_TO_DEMO_ROUTER",
        )


class TestDetermineStatusDemoOrder(unittest.TestCase):
    def test_demo_order_event_returns_executed_by_hermes(self) -> None:
        self.assertEqual(determine_final_status(_demo_order()), "EXECUTED_BY_HERMES")

    def test_demo_router_order_sent_returns_executed_by_hermes(self) -> None:
        self.assertEqual(determine_final_status(_demo_router_sent()), "EXECUTED_BY_HERMES")


class TestDetermineStatusSetupHunterReject(unittest.TestCase):
    def test_no_execution_candidate_returns_no_valid_setup(self) -> None:
        ev = _sh_reject(strategy="NONE", execution_policy="NONE")
        self.assertEqual(determine_final_status(ev), "NO_VALID_SETUP")

    def test_has_strategy_but_not_candidate_returns_rejected(self) -> None:
        ev = _sh_reject(strategy="ORDER_FLOW_EXECUTION_AGENT", execution_policy="NONE",
                        execution_candidate=False)
        self.assertEqual(determine_final_status(ev), "SETUP_HUNTER_REJECTED")

    def test_time_gate_block_returns_safety_blocked(self) -> None:
        ev = _sh_accept(time_gate_status="BLOCK")
        self.assertEqual(determine_final_status(ev), "SAFETY_BLOCKED")


class TestDetermineStatusAcceptedButBlocked(unittest.TestCase):
    def test_missing_confirmations_returns_accepted_but_router_blocked(self) -> None:
        ev = _sh_accept(missing_confirmations=["TOP_DOWN_DATA_MISSING"])
        self.assertEqual(determine_final_status(ev), "ACCEPTED_BUT_ROUTER_BLOCKED")

    def test_rates_missing_returns_accepted_but_router_blocked(self) -> None:
        ev = _sh_accept(missing_confirmations=["H4_RATES_MISSING", "M15_RATES_MISSING"])
        self.assertEqual(determine_final_status(ev), "ACCEPTED_BUT_ROUTER_BLOCKED")

    def test_all_confirmations_fail_returns_accepted_but_router_blocked(self) -> None:
        ev = _sh_accept(missing_confirmations=[], smc_status="FAIL",
                        mtfa_status="FAIL", top_down_status="FAIL")
        self.assertEqual(determine_final_status(ev), "ACCEPTED_BUT_ROUTER_BLOCKED")

    def test_analysis_only_policy_returns_observe_only(self) -> None:
        ev = _sh_accept(execution_policy="ANALYSIS_ONLY", missing_confirmations=[])
        self.assertEqual(determine_final_status(ev), "OBSERVE_ONLY")

    def test_clean_accepted_returns_accepted_but_not_sent(self) -> None:
        ev = _sh_accept_clean()
        self.assertEqual(determine_final_status(ev), "ACCEPTED_BUT_NOT_SENT")


# ── TestExtractStages ─────────────────────────────────────────────────────────

class TestExtractStages(unittest.TestCase):
    def test_position_sync_stages(self) -> None:
        stages = extract_stages(_position_sync())
        self.assertIn("MT5_POSITION_OPENED", stages)
        self.assertIn("POSITION_SYNC", stages)

    def test_demo_order_stages_include_router_pass(self) -> None:
        stages = extract_stages(_demo_order())
        self.assertIn("ROUTER_PASS", stages)
        self.assertIn("DEMO_ROUTER_ORDER_SENT", stages)
        self.assertIn("SETUP_HUNTER_ACCEPT", stages)

    def test_demo_router_sent_stages_include_router_pass(self) -> None:
        stages = extract_stages(_demo_router_sent())
        self.assertIn("ROUTER_PASS", stages)
        self.assertIn("DEMO_ROUTER_ORDER_SENT", stages)

    def test_sh_reject_stages(self) -> None:
        stages = extract_stages(_sh_reject())
        self.assertIn("SETUP_HUNTER_REJECT", stages)
        self.assertNotIn("SETUP_HUNTER_ACCEPT", stages)

    def test_sh_accept_router_blocked_stages(self) -> None:
        stages = extract_stages(_sh_accept())
        self.assertIn("SETUP_HUNTER_ACCEPT", stages)
        self.assertIn("SAFETY_GUARD_PASS", stages)
        self.assertIn("ROUTER_BLOCK", stages)
        self.assertNotIn("ROUTER_PASS", stages)

    def test_demo_skip_blocked_stages(self) -> None:
        stages = extract_stages(_demo_skip(allowed_symbol_check="BLOCK"))
        self.assertIn("ROUTER_BLOCK", stages)
        self.assertNotIn("DEMO_ROUTER_ORDER_SENT", stages)

    def test_time_gate_block_stages(self) -> None:
        stages = extract_stages(_sh_accept(time_gate_status="BLOCK"))
        self.assertIn("SAFETY_GUARD_BLOCK", stages)
        self.assertNotIn("SAFETY_GUARD_PASS", stages)


# ── TestExtractBlockReason ────────────────────────────────────────────────────

class TestExtractBlockReason(unittest.TestCase):
    def test_no_valid_setup_returns_no_strategy_signal(self) -> None:
        reason = extract_block_reason(_sh_reject(strategy="NONE"))
        self.assertEqual(reason, "NO_STRATEGY_SIGNAL")

    def test_execution_candidate_false_returns_appropriate_reason(self) -> None:
        ev = _sh_reject(strategy="ORDER_FLOW_EXECUTION_AGENT", execution_candidate=False)
        reason = extract_block_reason(ev)
        self.assertEqual(reason, "EXECUTION_CANDIDATE_FALSE")

    def test_missing_confirmations_returns_first_item(self) -> None:
        ev = _sh_accept(missing_confirmations=["TOP_DOWN_DATA_MISSING", "SMC_FAIL"])
        reason = extract_block_reason(ev)
        self.assertEqual(reason, "TOP_DOWN_DATA_MISSING")

    def test_smc_fail_only_returns_smc_reason(self) -> None:
        ev = _sh_accept(missing_confirmations=[], smc_status="FAIL",
                        mtfa_status="FAIL", top_down_status="FAIL")
        reason = extract_block_reason(ev)
        self.assertIsNotNone(reason)
        self.assertIn("CONFLUENCE", str(reason).upper())

    def test_demo_skip_block_returns_symbol_not_allowed(self) -> None:
        reason = extract_block_reason(_demo_skip(allowed_symbol_check="BLOCK"))
        self.assertEqual(reason, "SYMBOL_NOT_ALLOWED")

    def test_position_sync_returns_close_reason(self) -> None:
        reason = extract_block_reason(_position_sync(close_reason="MT5_POSITION_MISSING_CLOSED"))
        self.assertEqual(reason, "MT5_POSITION_MISSING_CLOSED")

    def test_time_gate_block_returns_time_gate(self) -> None:
        reason = extract_block_reason(_sh_accept(time_gate_status="BLOCK"))
        self.assertEqual(reason, "TIME_GATE_BLOCK")


# ── TestExtractSetupReason ────────────────────────────────────────────────────

class TestExtractSetupReason(unittest.TestCase):
    def test_order_flow_reason_extracted(self) -> None:
        ev = _sh_accept(order_flow_execution_agent_reason="DELTA_DIVERGENCE_REVERSAL")
        self.assertEqual(extract_setup_reason(ev), "DELTA_DIVERGENCE_REVERSAL")

    def test_no_reason_returns_none(self) -> None:
        ev = _sh_reject()
        result = extract_setup_reason(ev)
        self.assertIsNone(result)


# ── TestParseEvent ────────────────────────────────────────────────────────────

class TestParseEvent(unittest.TestCase):
    def test_parse_sh_accept_record_has_all_fields(self) -> None:
        rec = parse_event(_sh_accept())
        self.assertIn("final_status", rec)
        self.assertIn("pipeline_stages", rec)
        self.assertIn("rejection_reason", rec)
        self.assertIn("sh_decision", rec)
        self.assertIn("safety_decision", rec)
        self.assertIn("router_decision", rec)
        self.assertIn("mt5_status", rec)
        self.assertIn("top_down_missing", rec)
        self.assertIn("top_down_missing_fields", rec)
        self.assertEqual(rec["sh_decision"], "ACCEPT")

    def test_parse_sh_reject_decision_is_reject(self) -> None:
        rec = parse_event(_sh_reject())
        self.assertEqual(rec["sh_decision"], "REJECT")

    def test_parse_position_sync_mt5_status_is_synced_closed(self) -> None:
        rec = parse_event(_position_sync())
        self.assertEqual(rec["mt5_status"], "SYNCED_CLOSED")
        self.assertEqual(rec["final_status"], "MT5_SYNCED_CLOSED")

    def test_parse_demo_order_mt5_status_is_opened(self) -> None:
        rec = parse_event(_demo_order())
        self.assertEqual(rec["mt5_status"], "OPENED")
        self.assertEqual(rec["final_status"], "EXECUTED_BY_HERMES")

    def test_parse_demo_router_sent_is_executed_by_hermes(self) -> None:
        rec = parse_event(_demo_router_sent())
        self.assertEqual(rec["final_status"], "EXECUTED_BY_HERMES")
        self.assertEqual(rec["mt5_status"], "OPENED")

    def test_parse_keeps_symbol(self) -> None:
        rec = parse_event(_sh_accept(broker_symbol="EURUSD"))
        self.assertEqual(rec["symbol"], "EURUSD")

    def test_parse_top_down_missing_true_for_generic_missing(self) -> None:
        ev = _sh_accept(missing_confirmations=["TOP_DOWN_DATA_MISSING"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])
        self.assertIn("TOP_DOWN_DATA_MISSING", rec["top_down_missing_fields"])

    def test_parse_top_down_missing_true_for_rates_missing(self) -> None:
        ev = _sh_accept(missing_confirmations=["H4_RATES_MISSING", "M15_RATES_MISSING"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])
        self.assertIn("H4_RATES_MISSING", rec["top_down_missing_fields"])
        self.assertIn("M15_RATES_MISSING", rec["top_down_missing_fields"])

    def test_parse_top_down_missing_true_for_insufficient_suffix(self) -> None:
        ev = _sh_accept(missing_confirmations=["D1_MISSING_OR_INSUFFICIENT"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])

    def test_parse_top_down_missing_false_when_no_missing(self) -> None:
        ev = _sh_accept_clean()
        rec = parse_event(ev)
        self.assertFalse(rec["top_down_missing"])
        self.assertEqual(rec["top_down_missing_fields"], [])

    def test_parse_top_down_missing_reads_nested_reader_dict(self) -> None:
        ev = _sh_accept(
            missing_confirmations=[],
            top_down_reader={"missing_confirmations": ["H1_RATES_MISSING"]},
        )
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])
        self.assertIn("H1_RATES_MISSING", rec["top_down_missing_fields"])


# ── TestLoadEvents ────────────────────────────────────────────────────────────

class TestLoadEvents(unittest.TestCase):
    def test_load_events_filters_old_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            old = _sh_reject()
            old["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
            recent = _sh_accept()
            recent["created_at"] = _ts(1.0)
            _write_jsonl([old, recent], p)
            events = load_events(p, hours=48)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["execution_candidate"], True)

    def test_load_events_includes_events_within_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            events_in = [_sh_reject(), _sh_accept(), _demo_skip()]
            _write_jsonl(events_in, p)
            result = load_events(p, hours=48)
        self.assertEqual(len(result), 3)

    def test_load_events_handles_missing_file(self) -> None:
        result = load_events(Path("/nonexistent/path.jsonl"), hours=48)
        self.assertEqual(result, [])

    def test_load_events_skips_invalid_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            with p.open("w") as fh:
                fh.write(json.dumps(_sh_accept()) + "\n")
                fh.write("NOT VALID JSON\n")
                fh.write(json.dumps(_sh_reject()) + "\n")
            result = load_events(p, hours=48)
        self.assertEqual(len(result), 2)

    def test_load_events_handles_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text("")
            result = load_events(p, hours=48)
        self.assertEqual(result, [])


# ── TestGenerateSummary ───────────────────────────────────────────────────────

class TestGenerateSummary(unittest.TestCase):
    def _records(self) -> list[dict]:
        events = [
            _sh_reject(),                                     # NO_VALID_SETUP
            _sh_reject(strategy="ORDER_FLOW_EXECUTION_AGENT"), # SETUP_HUNTER_REJECTED
            _sh_accept(),                                     # ACCEPTED_BUT_ROUTER_BLOCKED
            _sh_accept(),                                     # ACCEPTED_BUT_ROUTER_BLOCKED
            _demo_skip(),                                     # ACCEPTED_BUT_ROUTER_BLOCKED
            _position_sync(),                                 # MT5_SYNCED_CLOSED
            _demo_order(),                                    # EXECUTED_BY_HERMES
        ]
        return [parse_event(e) for e in events]

    def test_summary_total_count_matches_input(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertEqual(s["total_setups_detected"], 7)

    def test_summary_mt5_synced_closed_count(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertEqual(s["mt5_synced_closed"], 1)

    def test_summary_executed_by_hermes_count(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertEqual(s["executed_by_hermes"], 1)

    def test_summary_accepted_but_router_blocked_count(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertGreater(s["accepted_but_router_blocked"], 0)

    def test_summary_setup_hunter_accepted_count(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertGreater(s["setup_hunter_accepted"], 0)

    def test_summary_accepted_not_executed_excludes_executed_and_synced(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        for r in s["accepted_not_executed"]:
            self.assertNotIn(r["final_status"], ("EXECUTED_BY_HERMES", "MT5_SYNCED_CLOSED", "SENT_TO_DEMO_ROUTER"))

    def test_summary_top_down_missing_count(self) -> None:
        records = [parse_event(_sh_accept()) for _ in range(5)]
        s = generate_summary(records)
        self.assertEqual(s["top_down_missing_count"], 5)

    def test_summary_top_missing_fields_counts_per_field(self) -> None:
        records = [
            parse_event(_sh_accept(missing_confirmations=["H4_RATES_MISSING", "M15_RATES_MISSING"])),
            parse_event(_sh_accept(missing_confirmations=["H4_RATES_MISSING"])),
        ]
        s = generate_summary(records)
        self.assertEqual(s["top_missing_fields"].get("H4_RATES_MISSING"), 2)
        self.assertEqual(s["top_missing_fields"].get("M15_RATES_MISSING"), 1)

    def test_summary_top_down_missing_by_symbol(self) -> None:
        records = [parse_event(_sh_accept(broker_symbol="GOLD#")) for _ in range(3)]
        s = generate_summary(records)
        self.assertEqual(s["top_down_missing_by_symbol"].get("GOLD#"), 3)

    def test_summary_top_missing_fields_empty_when_no_missing(self) -> None:
        records = [parse_event(_sh_accept_clean())]
        s = generate_summary(records)
        self.assertEqual(s["top_missing_fields"], {})
        self.assertEqual(s["top_down_missing_count"], 0)

    def test_summary_by_status_keys_are_strings(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        for k in s["by_final_status"]:
            self.assertIsInstance(k, str)

    def test_summary_hours_field(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=72)
        self.assertEqual(s["hours"], 72)

    def test_summary_top_block_reasons_is_dict(self) -> None:
        records = self._records()
        s = generate_summary(records, hours=48)
        self.assertIsInstance(s["top_block_reasons"], dict)

    def test_summary_by_symbol_counts_correctly(self) -> None:
        records = [parse_event(_sh_accept(broker_symbol="GOLD#")) for _ in range(5)]
        s = generate_summary(records)
        self.assertEqual(s["by_symbol"].get("GOLD#"), 5)

    def test_position_sync_not_in_accepted_not_executed(self) -> None:
        records = [parse_event(_position_sync())]
        s = generate_summary(records)
        self.assertEqual(len(s["accepted_not_executed"]), 0)
        self.assertEqual(s["mt5_synced_closed"], 1)

    def test_demo_router_sent_counts_as_executed_by_hermes(self) -> None:
        records = [parse_event(_demo_router_sent())]
        s = generate_summary(records)
        self.assertEqual(s["executed_by_hermes"], 1)
        self.assertEqual(len(s["accepted_not_executed"]), 0)


# ── TestExportFunctions ───────────────────────────────────────────────────────

class TestExportJson(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary(self) -> dict:
        records = [parse_event(_sh_accept()), parse_event(_position_sync())]
        return generate_summary(records, hours=48)

    def test_json_file_created(self) -> None:
        export_json(self._summary(), self._out, tag="test")
        self.assertTrue((self._out / "setup_audit_test.json").exists())

    def test_json_is_valid(self) -> None:
        s = self._summary()
        path = export_json(s, self._out, tag="test")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("total_setups_detected", data)
        self.assertIsInstance(data["total_setups_detected"], int)

    def test_json_contains_new_summary_metrics(self) -> None:
        path = export_json(self._summary(), self._out, tag="test")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "setup_hunter_accepted", "executed_by_hermes", "mt5_synced_closed",
            "accepted_but_router_blocked", "accepted_but_not_sent",
            "top_down_missing_count", "top_missing_fields", "by_final_status",
        ):
            self.assertIn(key, data)


class TestExportCsv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary(self) -> dict:
        records = [parse_event(_sh_accept()), parse_event(_sh_reject())]
        return generate_summary(records, hours=48)

    def test_csv_file_created(self) -> None:
        export_csv(self._summary(), self._out, tag="test")
        self.assertTrue((self._out / "setup_audit_test.csv").exists())

    def test_csv_has_header_row(self) -> None:
        s = self._summary()
        path = export_csv(s, self._out, tag="test")
        with path.open(encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
        self.assertIn("symbol", header)
        self.assertIn("final_status", header)
        self.assertIn("top_down_missing", header)
        self.assertIn("top_down_missing_fields", header)

    def test_csv_has_data_rows(self) -> None:
        s = self._summary()
        path = export_csv(s, self._out, tag="test")
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)

    def test_csv_final_status_column_populated(self) -> None:
        s = self._summary()
        path = export_csv(s, self._out, tag="test")
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            self.assertIn(row["final_status"], FINAL_STATUSES)


class TestExportMarkdown(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary(self) -> dict:
        records = [parse_event(_sh_accept()), parse_event(_position_sync())]
        return generate_summary(records, hours=48)

    def test_markdown_file_created(self) -> None:
        export_markdown(self._summary(), self._out, tag="test")
        self.assertTrue((self._out / "setup_audit_summary_test.md").exists())

    def test_markdown_contains_heading(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# HERMES 48H Setup Audit Report", text)

    def test_markdown_contains_read_only_notice(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Read-only", text)
        self.assertIn("ALLOW_LIVE_TRADING=false", text)

    def test_markdown_contains_block_reasons_section(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Block Reasons", text)

    def test_markdown_contains_top_down_missing_section(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Top-Down Missing", text)

    def test_markdown_contains_executed_by_hermes_metric(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Executed by Hermes", text)

    def test_markdown_contains_mt5_synced_closed_metric(self) -> None:
        s = self._summary()
        path = export_markdown(s, self._out, tag="test")
        text = path.read_text(encoding="utf-8")
        self.assertIn("MT5 Synced Closed", text)


# ── TestRunAuditIntegration ───────────────────────────────────────────────────

class TestRunAuditIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._out = Path(self._tmp.name) / "reports"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _events_file(self, events: list[dict]) -> Path:
        p = Path(self._tmp.name) / "events.jsonl"
        _write_jsonl(events, p)
        return p

    def test_run_audit_returns_summary_dict(self) -> None:
        p = self._events_file([_sh_accept(), _sh_reject(), _position_sync()])
        s = run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        self.assertIsInstance(s, dict)
        self.assertIn("total_setups_detected", s)

    def test_run_audit_creates_all_three_files(self) -> None:
        p = self._events_file([_sh_accept()])
        run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        self.assertTrue((self._out / "setup_audit_last_48h.json").exists())
        self.assertTrue((self._out / "setup_audit_last_48h.csv").exists())
        self.assertTrue((self._out / "setup_audit_summary_last_48h.md").exists())

    def test_run_audit_hours_48_uses_last_48h_tag(self) -> None:
        p = self._events_file([_sh_accept()])
        run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        json_path = self._out / "setup_audit_last_48h.json"
        csv_path = self._out / "setup_audit_last_48h.csv"
        md_path = self._out / "setup_audit_summary_last_48h.md"
        self.assertTrue(json_path.exists(), f"Expected {json_path}")
        self.assertTrue(csv_path.exists(), f"Expected {csv_path}")
        self.assertTrue(md_path.exists(), f"Expected {md_path}")

    def test_run_audit_hours_24_uses_last_24h_tag(self) -> None:
        p = self._events_file([_sh_accept()])
        run_audit(hours=24, events_path=p, out_dir=self._out, quiet=True)
        self.assertTrue((self._out / "setup_audit_last_24h.json").exists())
        self.assertTrue((self._out / "setup_audit_last_24h.csv").exists())
        self.assertTrue((self._out / "setup_audit_summary_last_24h.md").exists())

    def test_run_audit_with_real_events_file(self) -> None:
        if not DEFAULT_EVENTS_PATH.exists():
            self.skipTest("demo_pilot_events.jsonl not present")
        s = run_audit(hours=9999, events_path=DEFAULT_EVENTS_PATH,
                      out_dir=self._out, quiet=True)
        self.assertGreater(s["total_setups_detected"], 0)

    def test_run_audit_real_events_no_valid_setup_present(self) -> None:
        if not DEFAULT_EVENTS_PATH.exists():
            self.skipTest("demo_pilot_events.jsonl not present")
        s = run_audit(hours=9999, events_path=DEFAULT_EVENTS_PATH,
                      out_dir=self._out, quiet=True)
        by_status = s["by_final_status"]
        self.assertIn("NO_VALID_SETUP", by_status)
        self.assertGreater(by_status["NO_VALID_SETUP"], 0)

    def test_run_audit_real_events_accepted_but_router_blocked_present(self) -> None:
        if not DEFAULT_EVENTS_PATH.exists():
            self.skipTest("demo_pilot_events.jsonl not present")
        s = run_audit(hours=9999, events_path=DEFAULT_EVENTS_PATH,
                      out_dir=self._out, quiet=True)
        self.assertGreater(s["accepted_but_router_blocked"], 0)

    def test_run_audit_real_events_position_sync_classified_as_mt5_synced(self) -> None:
        if not DEFAULT_EVENTS_PATH.exists():
            self.skipTest("demo_pilot_events.jsonl not present")
        s = run_audit(hours=9999, events_path=DEFAULT_EVENTS_PATH,
                      out_dir=self._out, quiet=True)
        self.assertGreaterEqual(s["mt5_synced_closed"], 0)
        # POSITION_SYNC without DemoRouter send must NOT be EXECUTED_BY_HERMES
        self.assertEqual(s["executed_by_hermes"], 0,
                         "Real event log has no DEMO_ROUTER_ORDER_SENT — executed_by_hermes must be 0")

    def test_run_audit_real_events_top_down_missing_count(self) -> None:
        if not DEFAULT_EVENTS_PATH.exists():
            self.skipTest("demo_pilot_events.jsonl not present")
        s = run_audit(hours=9999, events_path=DEFAULT_EVENTS_PATH,
                      out_dir=self._out, quiet=True)
        self.assertGreater(s["top_down_missing_count"], 0)

    def test_run_audit_returns_zero_for_empty_file(self) -> None:
        p = Path(self._tmp.name) / "empty.jsonl"
        p.write_text("")
        s = run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        self.assertEqual(s["total_setups_detected"], 0)


# ── TestTopDownMissingClassification (Part F) ─────────────────────────────────

class TestTopDownMissingClassification(unittest.TestCase):
    """Part F: TOP_DOWN_DATA_MISSING produces specific missing fields."""

    def test_generic_top_down_missing_detected(self) -> None:
        ev = _sh_accept(missing_confirmations=["TOP_DOWN_DATA_MISSING"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])
        self.assertIn("TOP_DOWN_DATA_MISSING", rec["top_down_missing_fields"])

    def test_all_six_rates_missing_detected(self) -> None:
        fields = ["D1_RATES_MISSING", "H4_RATES_MISSING", "H1_RATES_MISSING",
                  "M15_RATES_MISSING", "M5_RATES_MISSING", "M1_RATES_MISSING"]
        ev = _sh_accept(missing_confirmations=fields)
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])
        for f in fields:
            self.assertIn(f, rec["top_down_missing_fields"])

    def test_insufficient_suffix_detected(self) -> None:
        ev = _sh_accept(missing_confirmations=["H4_MISSING_OR_INSUFFICIENT"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])

    def test_snapshot_not_ready_detected(self) -> None:
        ev = _sh_accept(missing_confirmations=["TOP_DOWN_SNAPSHOT_NOT_READY"])
        rec = parse_event(ev)
        self.assertTrue(rec["top_down_missing"])

    def test_candidate_with_top_down_context_not_missing(self) -> None:
        ev = _sh_accept_clean()
        rec = parse_event(ev)
        self.assertFalse(rec["top_down_missing"])
        self.assertEqual(rec["top_down_missing_fields"], [])

    def test_candidate_without_top_down_missing_field_list_honest(self) -> None:
        ev = _sh_accept(missing_confirmations=["H4_RATES_MISSING", "M1_RATES_MISSING"])
        rec = parse_event(ev)
        self.assertFalse("TOP_DOWN_DATA_MISSING" in rec["top_down_missing_fields"],
                         "Should report specific missing fields, not generic TOP_DOWN_DATA_MISSING")
        self.assertTrue(rec["top_down_missing"])
        self.assertIn("H4_RATES_MISSING", rec["top_down_missing_fields"])

    def test_top_down_missing_reasons_frozenset_has_all_expected_entries(self) -> None:
        expected = {
            "TOP_DOWN_DATA_MISSING", "TOP_DOWN_SNAPSHOT_NOT_READY",
            "D1_RATES_MISSING", "H4_RATES_MISSING", "H1_RATES_MISSING",
            "M15_RATES_MISSING", "M5_RATES_MISSING", "M1_RATES_MISSING",
        }
        for entry in expected:
            self.assertIn(entry, _TOP_DOWN_MISSING_REASONS)


class TestPositionSyncReclassification(unittest.TestCase):
    """Part F: POSITION_SYNC without DemoRouter send → MT5_SYNCED_CLOSED."""

    def test_position_sync_closed_is_mt5_synced_closed(self) -> None:
        rec = parse_event(_position_sync(result="CLOSED"))
        self.assertEqual(rec["final_status"], "MT5_SYNCED_CLOSED")

    def test_position_sync_open_is_mt5_synced_closed(self) -> None:
        rec = parse_event(_position_sync(result="OPEN"))
        self.assertEqual(rec["final_status"], "MT5_SYNCED_CLOSED")

    def test_position_sync_mt5_status_synced_closed(self) -> None:
        rec = parse_event(_position_sync())
        self.assertEqual(rec["mt5_status"], "SYNCED_CLOSED")

    def test_position_sync_not_executed_by_hermes(self) -> None:
        rec = parse_event(_position_sync())
        self.assertNotEqual(rec["final_status"], "EXECUTED_BY_HERMES")

    def test_demo_order_is_executed_by_hermes_not_synced(self) -> None:
        rec = parse_event(_demo_order())
        self.assertEqual(rec["final_status"], "EXECUTED_BY_HERMES")
        self.assertNotEqual(rec["final_status"], "MT5_SYNCED_CLOSED")

    def test_summary_position_sync_goes_to_mt5_synced_closed_bucket(self) -> None:
        records = [parse_event(_position_sync()) for _ in range(5)]
        s = generate_summary(records)
        self.assertEqual(s["mt5_synced_closed"], 5)
        self.assertEqual(s["executed_by_hermes"], 0)


class TestFilenameTagging(unittest.TestCase):
    """Part F: --hours 48 exports last_48h filenames."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._out = Path(self._tmp.name) / "reports"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _events_file(self) -> Path:
        p = Path(self._tmp.name) / "events.jsonl"
        _write_jsonl([_sh_accept()], p)
        return p

    def test_hours_48_produces_last_48h_filenames(self) -> None:
        p = self._events_file()
        run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        self.assertTrue((self._out / "setup_audit_last_48h.json").exists())
        self.assertTrue((self._out / "setup_audit_last_48h.csv").exists())
        self.assertTrue((self._out / "setup_audit_summary_last_48h.md").exists())

    def test_hours_72_produces_last_72h_filenames(self) -> None:
        p = self._events_file()
        run_audit(hours=72, events_path=p, out_dir=self._out, quiet=True)
        self.assertTrue((self._out / "setup_audit_last_72h.json").exists())
        self.assertTrue((self._out / "setup_audit_last_72h.csv").exists())
        self.assertTrue((self._out / "setup_audit_summary_last_72h.md").exists())

    def test_hours_1_produces_last_1h_filenames(self) -> None:
        p = self._events_file()
        run_audit(hours=1, events_path=p, out_dir=self._out, quiet=True)
        self.assertTrue((self._out / "setup_audit_last_1h.json").exists())

    def test_no_old_naming_convention_in_48h_output(self) -> None:
        p = self._events_file()
        run_audit(hours=48, events_path=p, out_dir=self._out, quiet=True)
        # Old naming would be setup_audit_48h.json without "last_" prefix
        self.assertFalse((self._out / "setup_audit_48h.json").exists())
        self.assertFalse((self._out / "setup_audit_48h.csv").exists())


# ── TestSetupAuditSafetyInvariants ───────────────────────────────────────────

class TestSetupAuditSafetyInvariants(unittest.TestCase):
    """Verify that setup_audit.py never touches execution paths."""

    def test_setup_audit_does_not_import_demo_router(self) -> None:
        import app.tools.setup_audit as sa
        source = Path(sa.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from app.mt5.demo_router", source)
        self.assertNotIn("import demo_router", source)
        self.assertNotIn("DemoKellyRouter", source)

    def test_setup_audit_does_not_call_order_send(self) -> None:
        import app.tools.setup_audit as sa
        source = Path(sa.__file__).read_text(encoding="utf-8")
        self.assertNotIn("order_send(", source)
        self.assertNotIn(".order_send(", source)

    def test_setup_audit_does_not_import_mt5(self) -> None:
        import app.tools.setup_audit as sa
        source = Path(sa.__file__).read_text(encoding="utf-8")
        self.assertNotIn("MetaTrader5", source)
        self.assertNotIn("import mt5", source)
        self.assertNotIn("import MetaTrader5", source)

    def test_setup_audit_has_no_post_put_delete_routes(self) -> None:
        import app.tools.setup_audit as sa
        source = Path(sa.__file__).read_text(encoding="utf-8")
        for method in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(method, source)

    def test_server_endpoint_is_get_only(self) -> None:
        import app.local_api.server as srv
        source = Path(srv.__file__).read_text(encoding="utf-8")
        idx = source.find("/local-api/setup-audit")
        self.assertGreater(idx, 0, "setup-audit endpoint not found in server.py")
        snippet = source[max(0, idx - 200):idx + 50]
        self.assertIn("@app.get", snippet)

    def test_allow_live_trading_false_not_changed(self) -> None:
        from app.config import get_settings
        cfg = get_settings()
        self.assertFalse(cfg.allow_live_trading, "allow_live_trading must remain False")

    def test_demo_only_true_not_changed(self) -> None:
        from app.config import get_settings
        cfg = get_settings()
        self.assertTrue(cfg.demo_only, "demo_only must remain True")


# ── TestConstantsAndMetadata ──────────────────────────────────────────────────

class TestConstantsAndMetadata(unittest.TestCase):
    def test_all_stages_is_non_empty_tuple(self) -> None:
        self.assertIsInstance(ALL_STAGES, tuple)
        self.assertGreater(len(ALL_STAGES), 0)

    def test_final_statuses_contains_new_values(self) -> None:
        self.assertIn("NO_VALID_SETUP", FINAL_STATUSES)
        self.assertIn("EXECUTED_BY_HERMES", FINAL_STATUSES)
        self.assertIn("MT5_SYNCED_CLOSED", FINAL_STATUSES)
        self.assertIn("ACCEPTED_BUT_ROUTER_BLOCKED", FINAL_STATUSES)
        self.assertIn("ACCEPTED_BUT_NOT_SENT", FINAL_STATUSES)

    def test_final_statuses_does_not_contain_old_names(self) -> None:
        self.assertNotIn("ROUTER_BLOCKED", FINAL_STATUSES)
        self.assertNotIn("EXECUTED_IN_MT5", FINAL_STATUSES)

    def test_default_events_path_is_path(self) -> None:
        self.assertIsInstance(DEFAULT_EVENTS_PATH, Path)

    def test_top_down_missing_reasons_is_frozenset(self) -> None:
        self.assertIsInstance(_TOP_DOWN_MISSING_REASONS, frozenset)
        self.assertGreater(len(_TOP_DOWN_MISSING_REASONS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
