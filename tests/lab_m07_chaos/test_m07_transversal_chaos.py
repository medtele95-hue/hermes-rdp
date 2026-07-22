"""M07-P1 — Campaign 7/7: TRANSVERSAL properties across the whole M02-M06 stack.

Two cross-cutting properties, checked directly against a small,
self-contained representative operation from EVERY module in this
campaign (self-sufficient even if this file is run in isolation, e.g.
``pytest tests/lab_m07_chaos/test_m07_transversal_chaos.py``):

1. "State-on-disk (or in-memory input) is NEVER changed by a FAILED
   operation" -- checked via a SHA-256 hash before/after (files) or a deep
   equality snapshot before/after (in-memory dict inputs to the pure
   ``Reconciler``).
2. "Global count of caught, UNTYPED exceptions is zero" -- every fault
   injected across this whole file is asserted, individually, to be an
   instance of the curated ``TYPED_EXCEPTION_BASES`` allow-list (a raw
   ``OSError`` counts as an honest, typed surfacing of a genuine OS fault;
   see ``chaos_tools`` module docstring).

Plus a determinism sentinel: the SAME fault plan, replayed in a fresh
temp directory, always produces the SAME structural outcome (reason codes,
counts) -- there is no hidden randomness anywhere in this campaign's
injectors.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from chaos_tools import (
    ByteCorruptor,
    FaultyOS,
    Ledger,
    assert_file_unchanged,
    faulty_existing_handle,
    sha256_file,
)

from app.services.event_identity import MonotonicUUID7Generator
from app.services.lifecycle_identity_store import LifecycleIdentityStore, StoreWriteError
from app.services.event_journal import JournalWriter
from app.services.order_intent import OrderIntentBook
from app.services.lifecycle_pnl import DealRecord, PnlLedger
from app.services.reconciliation_lab import BrokerSnapshot, Reconciler, ReconciliationInputError

T0 = "2026-07-22T03:00:00+00:00"
T1 = "2026-07-22T03:05:00+00:00"
T2 = "2026-07-22T03:20:00+00:00"
ACCT = "acct-v1-" + "07" * 16

LEDGER = Ledger()


def _snapshot():
    return BrokerSnapshot.from_mapping({"account_scope_id": ACCT, "positions": [], "recent_deals": []})


# --------------------------------------------------------------------------- #
# Property 1: state never changes on a FAILED operation, one per module
# --------------------------------------------------------------------------- #
class TransversalStateUnchangedPropertyTests(unittest.TestCase):
    def test_store_disk_unchanged_on_failed_mark_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "store.json"
        store = LifecycleIdentityStore(path=path)
        rec = store.create_lifecycle(correlation_id=None, created_at_utc=T0)
        baseline = sha256_file(path)

        faulty = FaultyOS(open_target="app.services.lifecycle_identity_store", fail_fsync_at={1})
        with faulty:
            with self.assertRaises(StoreWriteError) as cm:
                store.mark_closed(rec["lifecycle_id"], closed_at_utc=T1)
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, path, baseline)
        tmp.cleanup()

    def test_journal_disk_unchanged_on_failed_append(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "journal.jsonl"
        w = JournalWriter(path=path)
        w.append({"n": 0})
        baseline = sha256_file(path)

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(w, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    w.append({"n": "faulted"})
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, path, baseline)
        w.close()
        tmp.cleanup()

    def test_order_intent_journal_unchanged_on_failed_create(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "order_intent.jsonl"
        book = OrderIntentBook(path, uuid_generator=MonotonicUUID7Generator())
        baseline = sha256_file(path)  # header-only, but still a real baseline

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(book._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    book.create_intent(
                        {"symbol": "GOLD#", "direction": "BUY", "volume": 0.1, "magic": 1},
                        created_at_utc=T0,
                    )
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, path, baseline)
        book.close()
        tmp.cleanup()

    def test_pnl_ledger_unchanged_on_failed_record(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(path)
        baseline = sha256_file(path)
        lc = {"lifecycle_id": "lc-transversal", "broker_ref": None, "broker_key": None}
        deals = [DealRecord.from_mapping({
            "deal_id": 1, "position_id": 1, "ticket": 1, "kind": "ENTRY", "volume": "0.01",
            "price": "2000.00", "profit": "0", "commission": "0", "swap": "0", "fee": "0",
            "at_utc": T0, "symbol": "GOLD#",
        })]

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(ledger._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    ledger.compute_and_record(lc, deals)
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, path, baseline)
        ledger.close()
        tmp.cleanup()

    def test_reconciliation_inputs_never_mutated_on_malformed_shape(self) -> None:
        store_state = {"lifecycles": {}, "journal_rebuilt": None}
        intents_state = {"intents": []}
        ledger_state_bad = {"not_lifecycles": {}}  # malformed on purpose
        snap = _snapshot()
        before_store = copy.deepcopy(store_state)
        before_intents = copy.deepcopy(intents_state)
        before_ledger = copy.deepcopy(ledger_state_bad)

        with self.assertRaises(ReconciliationInputError) as cm:
            Reconciler(store_state, intents_state, ledger_state_bad, snap, as_of_utc=T2)
        self.assertTrue(LEDGER.record(cm.exception))

        self.assertEqual(store_state, before_store)
        self.assertEqual(intents_state, before_intents)
        self.assertEqual(ledger_state_bad, before_ledger)


# --------------------------------------------------------------------------- #
# Determinism sentinel
# --------------------------------------------------------------------------- #
class DeterminismSentinelTests(unittest.TestCase):
    def test_same_fault_plan_replayed_produces_same_structural_outcome(self) -> None:
        outcomes = []
        for _ in range(2):
            tmp = tempfile.TemporaryDirectory()
            path = Path(tmp.name) / "store.json"
            store = LifecycleIdentityStore(path=path)
            rec = store.create_lifecycle(correlation_id=None, created_at_utc=T0)

            faulty = FaultyOS(open_target="app.services.lifecycle_identity_store", fail_fsync_at={1})
            with faulty:
                with self.assertRaises(StoreWriteError) as cm:
                    store.mark_closed(rec["lifecycle_id"], closed_at_utc=T1)
            self.assertTrue(LEDGER.record(cm.exception))
            outcomes.append((
                cm.exception.reason_code,
                store.snapshot_metadata()["generation"],
                store.snapshot_metadata()["lifecycles_total"],
                store.get_lifecycle(rec["lifecycle_id"])["state"],
            ))
            tmp.cleanup()
        self.assertEqual(outcomes[0], outcomes[1])

    def test_byte_corruptor_sweep_positions_pure_and_deterministic(self) -> None:
        a = ByteCorruptor.sweep_positions(1000, 37)
        b = ByteCorruptor.sweep_positions(1000, 37)
        self.assertEqual(a, b)
        self.assertEqual(a, list(range(0, 1000, 37)))


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions observed
# --------------------------------------------------------------------------- #
class ZZTransversalCampaignLedgerTests(unittest.TestCase):
    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
