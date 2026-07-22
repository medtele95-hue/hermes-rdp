"""M08-P1 -- tests laboratoire : app/services/traceability_audit.py
(TraceabilityAuditor / audit_production_events). 100% additif, aucun fichier
de production touche : tout le disque passe par tempfile.TemporaryDirectory.
Valeurs 100% fictives. Aucun MT5, aucun order_send/order_check (meme
simule), aucun reseau.
"""
from __future__ import annotations

import json
import py_compile
import tempfile
import types
import unittest
import uuid
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalWriter
from app.services.lifecycle_identity_store import LifecycleIdentityStore, try_build_broker_ref
from app.services.lifecycle_pnl import DealRecord, PnlLedger
from app.services.order_intent import OrderIntentBook
from app.services.traceability_audit import (
    AuditInputError,
    LiveInstanceRejectedError,
    STATUS_BROKEN,
    STATUS_NOT_APPLICABLE,
    STATUS_VERIFIED,
    TraceabilityAuditor,
    VERDICT_BROKEN,
    VERDICT_FULL_TRACE,
    VERDICT_PARTIAL_TRACE,
    audit_production_events,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "app" / "services" / "traceability_audit.py"
_TEST_MODULE_PATH = Path(__file__).resolve()

ACCT = "acct-v1-" + "ab" * 16
T0 = "2026-07-22T03:00:00+00:00"
T1 = "2026-07-22T03:15:00+00:00"
_MAGIC = 909321

CANARY_TICKET = 900000222
CANARY_SYMBOL = "CANARY-M08-FIXTURE#"
CANARY_LIFECYCLE_ID = "canary-lifecycle-m08-fixture"


def _fixed_uuid_generator() -> MonotonicUUID7Generator:
    return MonotonicUUID7Generator(clock_ms=lambda: 1784350900000, random_bits=lambda n: 0)


def _uuid7_str(gen: MonotonicUUID7Generator) -> str:
    return str(gen.new())


def _intent_request(**overrides) -> dict:
    base = {"symbol": "GOLD#", "direction": "BUY", "volume": 0.10, "magic": _MAGIC}
    base.update(overrides)
    return base


def _deal(deal_id, position_id, ticket, kind, at_utc, price="2000.00", profit="0", volume="0.10"):
    return DealRecord.from_mapping({
        "deal_id": deal_id, "position_id": position_id, "ticket": ticket, "kind": kind,
        "volume": volume, "price": price, "profit": profit, "commission": "0", "swap": "0",
        "fee": "0", "at_utc": at_utc, "symbol": "GOLD#",
    })


class _World:
    """Builds a small world with the REAL M02/M03/M04/M05 modules inside a
    caller-supplied temp directory, closing every writer at the end -- no
    live instance ever survives into the auditor under test."""

    def __init__(self, root: Path):
        self.root = root
        self.store_path = root / "store.json"
        self.intents_path = root / "intents.jsonl"
        self.pnl_path = root / "pnl.jsonl"
        self.gen = _fixed_uuid_generator()

    def build_healthy(self, *, close_lifecycle=True, record_pnl=True, fill_intent=True,
                       intent_provenance=True):
        store = LifecycleIdentityStore(path=self.store_path)
        correlation_id = _uuid7_str(self.gen)
        boot_id = str(uuid.uuid4())
        rec = store.create_lifecycle(
            correlation_id=correlation_id, created_at_utc=T0, boot_id=boot_id, cycle_id=1, setup_id="S1")
        lid = rec["lifecycle_id"]
        ref, reason = try_build_broker_ref(
            account_scope_id=ACCT, ticket=555001, broker_symbol="GOLD#", opened_at=T0,
            magic=_MAGIC, position_identifier=777001, direction="BUY")
        assert ref is not None, reason
        store.bind_broker_position(lid, ref)
        if close_lifecycle:
            store.mark_closed(lid, closed_at_utc=T1)

        book = OrderIntentBook(self.intents_path, uuid_generator=self.gen)
        provenance = {"boot_id": boot_id, "cycle_id": 1} if intent_provenance else None
        intent = book.create_intent(
            _intent_request(), created_at_utc=T0, provenance=provenance,
            links={"lifecycle_id": lid, "correlation_id": correlation_id, "setup_id": "S1"})
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=T0)
        if fill_intent:
            book.transition(iid, "FILLED", at_utc=T0)
        book.close()

        if record_pnl:
            ledger = PnlLedger(self.pnl_path)
            deals = [
                _deal(1, 777001, 555001, "ENTRY", T0),
                _deal(2, 777001, 555001, "EXIT", T1, price="2015.00", profit="15.00"),
            ]
            ledger.compute_and_record(store.get_lifecycle(lid), deals)
            ledger.close()
        else:
            # touch an empty ledger journal so JournalReader.verify() sees an
            # intact-but-empty journal rather than an absent path.
            JournalWriter(self.pnl_path).close()

        return {"lifecycle_id": lid, "intent_id": iid, "correlation_id": correlation_id, "boot_id": boot_id}


def _tmpdir():
    return tempfile.TemporaryDirectory()


class CompileTests(unittest.TestCase):
    def test_py_compile_module(self):
        py_compile.compile(str(_MODULE_PATH), doraise=True)

    def test_py_compile_test_module(self):
        py_compile.compile(str(_TEST_MODULE_PATH), doraise=True)


class PurityTests(unittest.TestCase):
    def test_module_purity(self):
        import inspect
        import re

        import app.services.traceability_audit as mod

        src = inspect.getsource(mod)
        code_only = re.sub(r'"""[\s\S]*?"""', "", src)
        for forbidden in (
            "MetaTrader5", "os.environ", "getenv", "socket", "subprocess",
            "datetime.now", "time.time", "import time",
            "order_send", "order_check", "import os",
        ):
            self.assertNotIn(forbidden, code_only)
        imported_modules = {
            name for name, value in vars(mod).items() if isinstance(value, types.ModuleType)
        }
        self.assertEqual(imported_modules, {"hashlib", "json", "uuid"})


class HealthyWorldTests(unittest.TestCase):
    def test_full_trace_end_to_end(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy()
            auditor = TraceabilityAuditor(
                store_path=world.store_path,
                journal_paths={"intents": world.intents_path, "pnl": world.pnl_path},
            )
            report = auditor.audit()
        self.assertEqual(report["verdict"], VERDICT_FULL_TRACE)
        self.assertEqual(report["broken_links"], [])
        self.assertTrue(report["coverage"]["journal_integrity"])
        self.assertTrue(report["coverage"]["store_readable"])
        self.assertEqual(report["coverage"]["intents_linked_pct"], 100.0)
        self.assertEqual(report["coverage"]["lifecycles_with_pnl_pct"], 100.0)
        self.assertEqual(report["coverage"]["chains_complete_pct"], 100.0)
        for chain in report["chains"]:
            self.assertTrue(chain["complete"], chain)

    def test_digest_stable_across_two_runs(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy()
            kwargs = dict(
                store_path=world.store_path,
                journal_paths={"intents": world.intents_path, "pnl": world.pnl_path},
            )
            r1 = TraceabilityAuditor(**kwargs).audit()
            r2 = TraceabilityAuditor(**kwargs).audit()
        self.assertEqual(r1["report_digest"], r2["report_digest"])
        self.assertEqual(r1, r2)


class BrokenLinkIsolationTests(unittest.TestCase):
    def test_intent_without_provenance_is_warn_partial_trace(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy(intent_provenance=False)
            report = TraceabilityAuditor(
                store_path=world.store_path,
                journal_paths={"intents": world.intents_path, "pnl": world.pnl_path},
            ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("INTENT_PROVENANCE_MISSING", reasons)
        self.assertEqual(report["verdict"], VERDICT_PARTIAL_TRACE)

    def test_closed_lifecycle_without_pnl_is_warn_partial_trace(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy(record_pnl=False)
            report = TraceabilityAuditor(
                store_path=world.store_path,
                journal_paths={"intents": world.intents_path, "pnl": world.pnl_path},
            ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("CLOSED_WITHOUT_PNL", reasons)
        self.assertEqual(report["verdict"], VERDICT_PARTIAL_TRACE)

    def test_pnl_orphan_lifecycle_is_critical_broken(self):
        store_state = {"lifecycles": {}}
        ledger_state = {"lifecycles": {"orphan-lid": [
            {"op": "pnl_computed", "lifecycle_id": "orphan-lid", "net_profit": "10.00",
             "computed_over_deals": 2, "deals_digest": "0" * 16},
        ]}}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state={"intents": []}, ledger_state=ledger_state,
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("PNL_ORPHAN_LIFECYCLE", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)
        chain_ids = {c["chain_id"] for c in report["chains"]}
        self.assertIn("PNL:orphan-lid", chain_ids)

    def test_corrupted_journal_forces_broken_verdict(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy()
            # Simulate a torn/corrupted journal: append raw garbage bytes.
            with open(world.intents_path, "ab") as fh:
                fh.write(b"not-json-and-not-a-valid-envelope\n")
            report = TraceabilityAuditor(
                store_path=world.store_path,
                journal_paths={"intents": world.intents_path, "pnl": world.pnl_path},
            ).audit()
        self.assertFalse(report["coverage"]["journal_integrity"])
        self.assertEqual(report["verdict"], VERDICT_BROKEN)
        journal_chain = next(c for c in report["chains"] if c["chain_id"] == "JOURNAL:intents")
        self.assertEqual(journal_chain["links"][0]["status"], STATUS_BROKEN)
        self.assertEqual(journal_chain["links"][0]["reason"], "JOURNAL_CORRUPTED")

    def test_duplicate_id_cross_module_is_critical_broken(self):
        shared_id = "shared-id-0001"
        store_state = {"lifecycles": {shared_id: {
            "lifecycle_id": shared_id, "correlation_id": None, "boot_id": None, "cycle_id": None,
            "setup_id": None, "state": "OPEN", "created_at_utc": T0, "closed_at_utc": None,
            "broker_ref": None, "broker_key": None,
        }}}
        intent_book_state = {"intents": [{
            "intent_id": shared_id, "idempotency_key": "ORDER_INTENT:" + "0" * 32, "state": "CREATED",
            "request": {"symbol": "GOLD#", "direction": "BUY", "volume": 0.1, "magic": 1,
                        "sl": None, "tp": None, "price": None},
            "provenance": None, "links": None, "created_at_utc": T0,
            "history": [{"state": "CREATED", "at_utc": T0}],
        }]}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state=intent_book_state, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("DUPLICATE_ID_CROSS_MODULE", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)

    def test_invalid_correlation_id_is_warn(self):
        lid = "lid-bad-corr"
        store_state = {"lifecycles": {lid: {
            "lifecycle_id": lid, "correlation_id": "not-a-uuid7", "boot_id": None, "cycle_id": None,
            "setup_id": None, "state": "OPEN", "created_at_utc": T0, "closed_at_utc": None,
            "broker_ref": None, "broker_key": None,
        }}}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state={"intents": []}, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("CORRELATION_ID_INVALID", reasons)
        self.assertEqual(report["verdict"], VERDICT_PARTIAL_TRACE)

    def test_inconsistent_broker_ref_is_critical(self):
        lid = "lid-bad-ref"
        store_state = {"lifecycles": {lid: {
            "lifecycle_id": lid, "correlation_id": None, "boot_id": None, "cycle_id": None,
            "setup_id": None, "state": "OPEN", "created_at_utc": T0, "closed_at_utc": None,
            "broker_ref": {"account_scope_id": ACCT, "ticket": 1, "position_identifier": None,
                            "broker_symbol": "GOLD#", "opened_at": "iso:" + T0, "magic": 1, "direction": "BUY"},
            "broker_key": "mismatched-key-should-never-equal-real-derivation",
        }}}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state={"intents": []}, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("BROKER_REF_INCONSISTENT", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)

    def test_inconsistent_creation_provenance_is_warn(self):
        lid = "lid-half-prov"
        store_state = {"lifecycles": {lid: {
            "lifecycle_id": lid, "correlation_id": None, "boot_id": str(uuid.uuid4()), "cycle_id": None,
            "setup_id": None, "state": "OPEN", "created_at_utc": T0, "closed_at_utc": None,
            "broker_ref": None, "broker_key": None,
        }}}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state={"intents": []}, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("PROVENANCE_INCONSISTENT", reasons)
        self.assertEqual(report["verdict"], VERDICT_PARTIAL_TRACE)

    def test_timestamps_incoherent_is_critical(self):
        lid = "lid-bad-ts"
        store_state = {"lifecycles": {lid: {
            "lifecycle_id": lid, "correlation_id": None, "boot_id": None, "cycle_id": None,
            "setup_id": None, "state": "CLOSED", "created_at_utc": T1, "closed_at_utc": T0,
            "broker_ref": None, "broker_key": None,
        }}}
        ledger_state = {"lifecycles": {lid: [
            {"op": "pnl_computed", "lifecycle_id": lid, "net_profit": "1.00",
             "computed_over_deals": 1, "deals_digest": "0" * 16},
        ]}}
        report = TraceabilityAuditor(
            store_state=store_state, intent_book_state={"intents": []}, ledger_state=ledger_state,
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("TIMESTAMPS_INCOHERENT", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)

    def test_intent_filled_without_lifecycle_is_critical(self):
        intent_book_state = {"intents": [{
            "intent_id": "iid-filled-orphan", "idempotency_key": "ORDER_INTENT:" + "1" * 32,
            "state": "FILLED",
            "request": {"symbol": "GOLD#", "direction": "BUY", "volume": 0.1, "magic": 1,
                        "sl": None, "tp": None, "price": None},
            "provenance": None, "links": None, "created_at_utc": T0,
            "history": [{"state": "CREATED", "at_utc": T0}, {"state": "FILLED", "at_utc": T0}],
        }]}
        report = TraceabilityAuditor(
            store_state={"lifecycles": {}}, intent_book_state=intent_book_state, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("INTENT_FILLED_WITHOUT_LIFECYCLE", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)

    def test_intent_explicit_lifecycle_unresolved_is_critical(self):
        intent_book_state = {"intents": [{
            "intent_id": "iid-explicit-orphan", "idempotency_key": "ORDER_INTENT:" + "2" * 32,
            "state": "CREATED",
            "request": {"symbol": "GOLD#", "direction": "BUY", "volume": 0.1, "magic": 1,
                        "sl": None, "tp": None, "price": None},
            "provenance": None, "links": {"lifecycle_id": "nowhere-lid", "correlation_id": None, "setup_id": None},
            "created_at_utc": T0, "history": [{"state": "CREATED", "at_utc": T0}],
        }]}
        report = TraceabilityAuditor(
            store_state={"lifecycles": {}}, intent_book_state=intent_book_state, ledger_state={"lifecycles": {}},
        ).audit()
        reasons = {b["reason"] for b in report["broken_links"]}
        self.assertIn("INTENT_LIFECYCLE_UNRESOLVED", reasons)
        self.assertEqual(report["verdict"], VERDICT_BROKEN)


class EmptyWorldTests(unittest.TestCase):
    def test_empty_world_is_full_trace_vacuously(self):
        report = TraceabilityAuditor().audit()
        self.assertEqual(report["verdict"], VERDICT_FULL_TRACE)
        self.assertEqual(report["broken_links"], [])
        self.assertEqual(report["coverage"]["intents_linked_pct"], 100.0)
        self.assertEqual(report["coverage"]["lifecycles_with_pnl_pct"], 100.0)
        self.assertTrue(report["coverage"]["journal_integrity"])
        self.assertTrue(report["coverage"]["store_readable"])
        self.assertEqual(report["coverage"]["chains_total"], 1)  # TRANSVERSAL_IDS only
        self.assertEqual(report["chains"][0]["chain_id"], "TRANSVERSAL_IDS")
        self.assertTrue(report["chains"][0]["complete"])


class LiveInstanceRejectionTests(unittest.TestCase):
    def test_rejects_live_store_instance(self):
        with _tmpdir() as tmp:
            store = LifecycleIdentityStore(path=Path(tmp) / "store.json")
            try:
                with self.assertRaises(LiveInstanceRejectedError):
                    TraceabilityAuditor(store_state=store)
            finally:
                pass  # LifecycleIdentityStore holds no lock/handle to release

    def test_rejects_live_order_intent_book(self):
        with _tmpdir() as tmp:
            book = OrderIntentBook(Path(tmp) / "intents.jsonl", uuid_generator=_fixed_uuid_generator())
            try:
                with self.assertRaises(LiveInstanceRejectedError):
                    TraceabilityAuditor(intent_book_state=book)
            finally:
                book.close()

    def test_rejects_live_pnl_ledger(self):
        with _tmpdir() as tmp:
            ledger = PnlLedger(Path(tmp) / "pnl.jsonl")
            try:
                with self.assertRaises(LiveInstanceRejectedError):
                    TraceabilityAuditor(ledger_state=ledger)
            finally:
                ledger.close()

    def test_rejects_live_journal_writer_as_path(self):
        with _tmpdir() as tmp:
            writer = JournalWriter(Path(tmp) / "j.jsonl")
            try:
                with self.assertRaises(LiveInstanceRejectedError):
                    TraceabilityAuditor(journal_paths={"intents": writer})
            finally:
                writer.close()

    def test_rejects_malformed_store_state_shape(self):
        with self.assertRaises(AuditInputError):
            TraceabilityAuditor(store_state={"wrong_key": {}})

    def test_rejects_store_path_and_store_state_together(self):
        with self.assertRaises(AuditInputError):
            TraceabilityAuditor(store_path="somewhere.json", store_state={"lifecycles": {}})


class DeterminismOrderInsensitivityTests(unittest.TestCase):
    def test_dict_insertion_order_does_not_affect_digest(self):
        lid_a, lid_b = "lid-a", "lid-b"

        def make(order):
            recs = {}
            for lid in order:
                recs[lid] = {
                    "lifecycle_id": lid, "correlation_id": None, "boot_id": None, "cycle_id": None,
                    "setup_id": None, "state": "OPEN", "created_at_utc": T0, "closed_at_utc": None,
                    "broker_ref": None, "broker_key": None,
                }
            return TraceabilityAuditor(
                store_state={"lifecycles": recs}, intent_book_state={"intents": []},
                ledger_state={"lifecycles": {}},
            ).audit()

        r1 = make([lid_a, lid_b])
        r2 = make([lid_b, lid_a])
        self.assertEqual(r1["report_digest"], r2["report_digest"])
        self.assertEqual(r1["chains"], r2["chains"])


class ProductionEventsAuditTests(unittest.TestCase):
    def _write(self, tmp, lines):
        path = Path(tmp) / "events.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        return path

    def test_synthetic_fixture_exact_stats(self):
        lines = [
            json.dumps({"event_type": "SYSTEM_HEARTBEAT", "boot_id": "b1", "cycle_id": 1}),
            json.dumps({"event_type": "SYSTEM_HEARTBEAT", "boot_id": None, "cycle_id": None}),
            json.dumps({"event_type": "DEMO_SKIP", "setup_id": "S1", "ticket": None}),
            json.dumps({"event_type": "POSITION_SYNC", "ticket": "123456"}),
            json.dumps({"event_type": "SETUP_HUNTER", "identity_shadow": {"identity_shadow_version": 1}}),
            "not-json-garbage-line",
            "",
        ]
        with _tmpdir() as tmp:
            path = self._write(tmp, lines)
            report = audit_production_events(path)
        self.assertEqual(report["lines_total"], 7)
        self.assertEqual(report["lines_parsed"], 5)
        self.assertEqual(report["lines_corrupted"], 2)
        self.assertEqual(report["by_event_type"], {
            "SYSTEM_HEARTBEAT": 2, "DEMO_SKIP": 1, "POSITION_SYNC": 1, "SETUP_HUNTER": 1,
        })
        self.assertEqual(report["identity_shadow"]["present_count"], 1)
        self.assertAlmostEqual(report["identity_shadow"]["present_pct"], 20.0)
        self.assertEqual(report["system_heartbeat"]["total"], 2)
        self.assertEqual(report["system_heartbeat"]["with_boot_and_cycle"], 1)
        self.assertAlmostEqual(report["system_heartbeat"]["with_boot_and_cycle_pct"], 50.0)
        demo_skip_fields = report["identity_fields_by_event_type"]["DEMO_SKIP"]
        self.assertEqual(demo_skip_fields["sample_count"], 1)
        self.assertAlmostEqual(demo_skip_fields["setup_id_present_pct"], 100.0)
        self.assertAlmostEqual(demo_skip_fields["ticket_present_pct"], 100.0)  # key present, value None
        position_sync_fields = report["identity_fields_by_event_type"]["POSITION_SYNC"]
        self.assertAlmostEqual(position_sync_fields["ticket_present_pct"], 100.0)
        self.assertAlmostEqual(position_sync_fields["setup_id_present_pct"], 0.0)

    def test_max_lines_caps_reading(self):
        lines = [json.dumps({"event_type": "SETUP_HUNTER"}) for _ in range(10)]
        with _tmpdir() as tmp:
            path = self._write(tmp, lines)
            report = audit_production_events(path, max_lines=3)
        self.assertEqual(report["lines_total"], 3)
        self.assertEqual(report["lines_parsed"], 3)

    def test_missing_file_returns_empty_snapshot_never_raises(self):
        report = audit_production_events(Path(tempfile.gettempdir()) / "does-not-exist-m08.jsonl")
        self.assertEqual(report["lines_total"], 0)
        self.assertEqual(report["identity_shadow"]["present_count"], 0)

    def test_digest_present_and_deterministic(self):
        lines = [json.dumps({"event_type": "SETUP_HUNTER"})]
        with _tmpdir() as tmp:
            path = self._write(tmp, lines)
            r1 = audit_production_events(path)
            r2 = audit_production_events(path)
        self.assertEqual(r1["digest"], r2["digest"])


class CanaryAbsenceTests(unittest.TestCase):
    def test_canaries_absent_from_module_source(self):
        import inspect

        import app.services.traceability_audit as mod

        src = inspect.getsource(mod)
        self.assertNotIn(str(CANARY_TICKET), src)
        self.assertNotIn(CANARY_SYMBOL, src)
        self.assertNotIn(CANARY_LIFECYCLE_ID, src)

    def test_canaries_absent_from_generated_world_files(self):
        with _tmpdir() as tmp:
            world = _World(Path(tmp))
            world.build_healthy()
            raw_all = "".join(
                p.read_text(encoding="utf-8") for p in Path(tmp).glob("*") if p.is_file()
            )
        self.assertNotIn(str(CANARY_TICKET), raw_all)
        self.assertNotIn(CANARY_SYMBOL, raw_all)
        self.assertNotIn(CANARY_LIFECYCLE_ID, raw_all)


if __name__ == "__main__":
    unittest.main()
