"""M07-P1 — Campaign 5/7: RECONCILIATION (M06 ``reconciliation_lab``) chaos.

Proves that ``Reconciler``/``BrokerSnapshot`` never crash on adverse input
(duplicate broker positions, boundary/adversarial timestamps, malformed
store/intent/ledger shapes, duplicate ledger entries) -- a report is ALWAYS
produced, or a typed ``ReconciliationInputError``/``SnapshotValidationError``
is raised, never a bare ``KeyError``/``AttributeError``. Also: a REAL
fault-injected crash (not just the module's own hand-written "# CRASH HERE"
scenarios) between a lifecycle close and its PnL computation is correctly
surfaced by ``Reconciler.reconcile()`` after a genuine restart, reusing the
SAME real M02-M05 modules ``RestartSimulator`` itself builds on.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chaos_tools import ClockChaos, FaultyOS, Ledger, faulty_existing_handle

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalReader, JournalWriter, rebuild_lifecycle_state
from app.services.lifecycle_identity_store import LifecycleIdentityStore, try_build_broker_ref
from app.services.lifecycle_pnl import DealRecord, PnlLedger
from app.services.order_intent import OrderIntentBook
from app.services.reconciliation_lab import (
    ANOMALY_LEDGER_LIFECYCLE_GAP,
    ANOMALY_ORPHAN_BROKER_POSITION,
    BrokerSnapshot,
    Reconciler,
    ReconciliationInputError,
    RestartSimulator,
    SnapshotValidationError,
    VERDICT_CLEAN,
    VERDICT_WARN,
)

ACCT = "acct-v1-" + "07" * 16
_MAGIC = 909707
T0 = "2026-07-22T03:00:00+00:00"
T1 = "2026-07-22T03:05:00+00:00"
T2 = "2026-07-22T03:20:00+00:00"

LEDGER = Ledger()


def _pos(ticket=500, pid=600, symbol="GOLD#", direction="BUY", volume="0.10", opened_at=T0, magic=1):
    return {
        "ticket": ticket, "position_identifier": pid, "symbol": symbol, "direction": direction,
        "volume": volume, "opened_at": opened_at, "magic": magic,
    }


def _snapshot(positions=None, deals=None, account_scope_id=ACCT):
    return BrokerSnapshot.from_mapping({
        "account_scope_id": account_scope_id,
        "positions": positions or [],
        "recent_deals": deals or [],
    })


def _fixed_uuid_generator():
    return MonotonicUUID7Generator(clock_ms=lambda: 1784350800000, random_bits=lambda n: 0)


def _intent_request(**overrides):
    base = {"symbol": "GOLD#", "direction": "BUY", "volume": 0.10, "magic": _MAGIC}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Duplicate / adverse broker positions
# --------------------------------------------------------------------------- #
class AdverseSnapshotTests(unittest.TestCase):
    def test_duplicate_positions_never_crash_report_always_produced(self) -> None:
        snap = _snapshot(positions=[_pos(ticket=500, pid=600), _pos(ticket=500, pid=600)])
        store = {"lifecycles": {}, "journal_rebuilt": None}
        reconciler = Reconciler(store, {"intents": []}, {"lifecycles": {}}, snap, as_of_utc=T2)
        report = reconciler.reconcile()
        self.assertIn("verdict", report)
        self.assertIn("digest", report)
        self.assertEqual(report["counts"].get(ANOMALY_ORPHAN_BROKER_POSITION), 2)

    def test_boundary_and_adversarial_timestamps_typed_accept_or_reject(self) -> None:
        cases = [
            (ClockChaos.Y2038_EDGE_ISO, True),
            (ClockChaos.Y2038_PLUS_ONE_ISO, True),
            (ClockChaos.FAR_FUTURE_ISO, True),
            (ClockChaos.Y9999_ISO, True),
            (ClockChaos.NAIVE_NO_TZ, False),
            (ClockChaos.NOT_A_STRING, False),
            (ClockChaos.EMPTY, False),
        ]
        for opened_at, should_succeed in cases:
            with self.subTest(opened_at=opened_at):
                if should_succeed:
                    snap = _snapshot(positions=[_pos(opened_at=opened_at)])
                    self.assertEqual(len(snap.positions), 1)
                else:
                    with self.assertRaises(SnapshotValidationError) as cm:
                        _snapshot(positions=[_pos(opened_at=opened_at)])
                    self.assertTrue(LEDGER.record(cm.exception))

    def test_boundary_numeric_fields_min_valid(self) -> None:
        # ticket=1 (minimum positive), magic=0 (minimum allowed) -- both
        # legitimate boundary values, never rejected.
        snap = _snapshot(positions=[_pos(ticket=1, pid=1, magic=0)])
        self.assertEqual(snap.positions[0]["ticket"], 1)
        self.assertEqual(snap.positions[0]["magic"], 0)


# --------------------------------------------------------------------------- #
# Malformed store/intent/ledger shapes
# --------------------------------------------------------------------------- #
class MalformedInputSweepTests(unittest.TestCase):
    def test_malformed_shapes_sweep_always_typed(self) -> None:
        good_snap = _snapshot()
        cases = [
            ({"lifecycles": {}}, {"intents": []}, {"lifecycles": {}}),  # missing journal_rebuilt
            ({"lifecycles": "not-a-dict", "journal_rebuilt": None}, {"intents": []}, {"lifecycles": {}}),
            ({"lifecycles": {}, "journal_rebuilt": {"wrong": "shape"}}, {"intents": []}, {"lifecycles": {}}),
            ({"lifecycles": {}, "journal_rebuilt": None}, {"not_intents": []}, {"lifecycles": {}}),
            ({"lifecycles": {}, "journal_rebuilt": None}, {"intents": "not-a-list"}, {"lifecycles": {}}),
            ({"lifecycles": {}, "journal_rebuilt": None}, {"intents": []}, {"not_lifecycles": {}}),
            ({"lifecycles": {}, "journal_rebuilt": None}, {"intents": []}, {"lifecycles": "not-a-dict"}),
        ]
        for store, intents, ledger_state in cases:
            with self.subTest(store=store, intents=intents, ledger_state=ledger_state):
                with self.assertRaises(ReconciliationInputError) as cm:
                    Reconciler(store, intents, ledger_state, good_snap, as_of_utc=T2)
                self.assertTrue(LEDGER.record(cm.exception))

    def test_invalid_as_of_utc_typed(self) -> None:
        good_snap = _snapshot()
        with self.assertRaises(ReconciliationInputError) as cm:
            Reconciler(
                {"lifecycles": {}, "journal_rebuilt": None}, {"intents": []}, {"lifecycles": {}},
                good_snap, as_of_utc=ClockChaos.NAIVE_NO_TZ,
            )
        self.assertTrue(LEDGER.record(cm.exception))

    def test_duplicate_ledger_entries_for_same_lifecycle_never_crash(self) -> None:
        store = {
            "lifecycles": {
                "lc-y": {
                    "lifecycle_id": "lc-y", "state": "CLOSED", "correlation_id": None,
                    "broker_ref": None, "broker_key": None,
                },
            },
            "journal_rebuilt": None,
        }
        ledger_state = {"lifecycles": {"lc-y": [
            {"lifecycle_id": "lc-y", "net_profit": "1.00", "computed_over_deals": 2, "deals_digest": "aaaa"},
            {"lifecycle_id": "lc-y", "net_profit": "1.00", "computed_over_deals": 2, "deals_digest": "bbbb"},
        ]}}
        reconciler = Reconciler(store, {"intents": []}, ledger_state, _snapshot(), as_of_utc=T2)
        report = reconciler.reconcile()
        self.assertEqual(report["verdict"], VERDICT_CLEAN)


# --------------------------------------------------------------------------- #
# Real fault-injected crash (not a hand-written "# CRASH HERE" scenario)
# --------------------------------------------------------------------------- #
class RealChaosInducedCrashTests(unittest.TestCase):
    def test_chaos_induced_crash_between_close_and_pnl_reconciles_as_warn(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        store_path = root / "store.json"
        journal_path = root / "mirror.jsonl"
        intents_path = root / "intents.jsonl"
        ledger_path = root / "ledger.jsonl"

        store = LifecycleIdentityStore(path=store_path)
        mirror = JournalWriter(path=journal_path)
        book = OrderIntentBook(intents_path, uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(ledger_path)

        rec = RestartSimulator._do_create(store, mirror, created_at_utc=T0)
        lid = rec["lifecycle_id"]
        ref, reason = try_build_broker_ref(
            account_scope_id=ACCT, ticket=555001, broker_symbol="GOLD#",
            opened_at=T0, magic=_MAGIC, position_identifier=666001, direction="BUY",
        )
        assert reason is None, reason
        RestartSimulator._do_bind(store, mirror, lid, ref)

        deals = [
            DealRecord.from_mapping({
                "deal_id": 1, "position_id": 666001, "ticket": 555001, "kind": "ENTRY",
                "volume": "0.10", "price": "2000.00", "profit": "0", "commission": "0",
                "swap": "0", "fee": "0", "at_utc": T0, "symbol": "GOLD#",
            }),
        ]
        # REAL, injected crash: a disk fault while computing/recording PnL,
        # not merely a hand-omitted call. This IS the scenario
        # RestartSimulator's own "crash_between_close_and_pnl" documents,
        # reproduced here via genuine chaos rather than by construction.
        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(ledger._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    ledger.compute_and_record(store.get_lifecycle(lid), deals)
        self.assertTrue(LEDGER.record(cm.exception))

        RestartSimulator._do_close(store, mirror, lid, T1)

        intent = book.create_intent(
            _intent_request(), created_at_utc=T1,
            links={"lifecycle_id": lid, "correlation_id": None, "setup_id": None},
        )
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=T1)
        book.transition(iid, "FILLED", at_utc=T1)

        mirror.close()
        book.close()
        ledger.close()

        # RESTART: fresh instances, disk-only state.
        fresh_store = LifecycleIdentityStore(path=store_path)
        lifecycles = {lid: fresh_store.get_lifecycle(lid)}
        rebuilt = rebuild_lifecycle_state(JournalReader(journal_path))
        journal_rebuilt = {"lifecycles": rebuilt["lifecycles"]}

        fresh_book = OrderIntentBook.rebuild_from_journal(intents_path, uuid_generator=_fixed_uuid_generator())
        try:
            intents = [fresh_book.get(iid)]
        finally:
            fresh_book.close()

        fresh_ledger = PnlLedger(ledger_path)
        try:
            ledger_state = fresh_ledger.snapshot()
        finally:
            fresh_ledger.close()

        snapshot = _snapshot()  # broker shows nothing left open -- already closed
        reconciler = Reconciler(
            {"lifecycles": lifecycles, "journal_rebuilt": journal_rebuilt},
            {"intents": intents}, ledger_state, snapshot, as_of_utc=T2,
        )
        report = reconciler.reconcile()
        self.assertEqual(report["verdict"], VERDICT_WARN)
        self.assertIn(ANOMALY_LEDGER_LIFECYCLE_GAP, report["counts"])
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions observed
# --------------------------------------------------------------------------- #
class ZZReconciliationCampaignLedgerTests(unittest.TestCase):
    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
