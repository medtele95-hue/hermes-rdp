"""M07-P1 — Campaign 3/7: ORDER_INTENT (M04 ``OrderIntentBook``) chaos.

Proves that a journal-in-panne during ``create_intent``/``transition`` never
leaves the book half-updated (an intent either exists fully -- journaled AND
indexed -- or not at all; a transition either fully applies -- journaled AND
reflected in-memory -- or the prior state is untouched), that ILLEGAL
transitions are refused BEFORE any I/O is attempted (so a fault never even
fires for them), that a torn-tail crash mid-transition rebuilds to the last
COMPLETE state (never a half-applied one), and that structurally corrupted
journal bytes always fail closed with a typed error during
``rebuild_from_journal`` -- never a silent load of the wrong state.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional. No MT5, no
``order_send``/``order_check`` (even simulated).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chaos_tools import ByteCorruptor, FaultyOS, Ledger, faulty_existing_handle

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalCorruptedError, JournalPoisonedError
from app.services.order_intent import (
    IntentReplayError,
    IntentTransitionError,
    OrderIntentBook,
)

T0 = "2026-07-22T02:00:00+00:00"
T1 = "2026-07-22T02:00:01+00:00"
T2 = "2026-07-22T02:00:02+00:00"

LEDGER = Ledger()


def _tmp_journal():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name) / "order_intent.jsonl"


def _book(path: Path) -> OrderIntentBook:
    return OrderIntentBook(path, uuid_generator=MonotonicUUID7Generator())


def _req(**overrides):
    base = {"symbol": "GOLD#", "direction": "BUY", "volume": 0.10, "magic": 909707}
    base.update(overrides)
    return base


def _tear_last_line(path: Path) -> None:
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    last = lines.pop()
    torn = last[: max(1, len(last) // 2)]
    new_raw = b"\n".join(lines) + (b"\n" if lines else b"") + torn
    path.write_bytes(new_raw)


# --------------------------------------------------------------------------- #
# Journal-in-panne during create_intent / transition
# --------------------------------------------------------------------------- #
class CreateAndTransitionFaultTests(unittest.TestCase):
    def test_create_intent_journal_fault_never_half_registers(self) -> None:
        """FIXED (M07-P2): event_journal.JournalWriter now self-poisons on
        ANY I/O exception (including a fully-clean write-fault) -- retrying
        on the SAME (un-reopened) book instance is refused synchronously by
        JournalPoisonedError, never a silent write. The documented recovery
        is close()+``rebuild_from_journal`` (a NEW instance), exactly as for
        the underlying writer."""
        tmp, path = _tmp_journal()
        book = _book(path)
        request = _req()

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(book._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    book.create_intent(request, created_at_utc=T0)
        self.assertTrue(LEDGER.record(cm.exception))

        self.assertEqual(book.diagnostics()["created"], 0)
        self.assertEqual(book.active_intents(), [])
        self.assertTrue(book._writer.poisoned)

        with self.assertRaises(JournalPoisonedError) as cm2:
            book.create_intent(request, created_at_utc=T0)
        self.assertTrue(LEDGER.record(cm2.exception))
        book.close()

        # documented recovery: close()+reopen a NEW book over the same path.
        reopened = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(reopened.diagnostics()["created"], 0)
        # retry (SAME request -> same idempotency key) is NOT treated as a
        # duplicate of a half-registered intent: the failed attempt left
        # nothing in the dedup index, so this creates a genuinely new one.
        created = reopened.create_intent(request, created_at_utc=T0)
        self.assertFalse(created["deduplicated"])
        self.assertEqual(reopened.diagnostics()["created"], 1)
        reopened.close()
        tmp.cleanup()

    def test_transition_journal_fault_never_half_applies(self) -> None:
        """FIXED (M07-P2): same poisoning rule applies to a mid-life
        transition fault -- retrying on the SAME instance is refused
        synchronously; close()+``rebuild_from_journal`` recovers and the
        retry then fully applies."""
        tmp, path = _tmp_journal()
        book = _book(path)
        created = book.create_intent(_req(), created_at_utc=T0)
        iid = created["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=T1)
        diag_before = book.diagnostics()

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(book._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    book.transition(iid, "FILLED", at_utc=T2)
        self.assertTrue(LEDGER.record(cm.exception))

        record = book.get(iid)
        self.assertEqual(record["state"], "SUBMITTED")  # unchanged
        self.assertEqual(len(record["history"]), 2)  # CREATED, SUBMITTED only
        diag_after_fault = book.diagnostics()
        self.assertEqual(diag_after_fault["transitions"], diag_before["transitions"])
        self.assertEqual(diag_after_fault["by_state"], diag_before["by_state"])
        self.assertTrue(book._writer.poisoned)

        with self.assertRaises(JournalPoisonedError) as cm2:
            book.transition(iid, "FILLED", at_utc=T2)
        self.assertTrue(LEDGER.record(cm2.exception))
        self.assertEqual(book.get(iid)["state"], "SUBMITTED")  # still unchanged
        book.close()

        # documented recovery: close()+reopen, then the retry fully applies.
        reopened = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(reopened.get(iid)["state"], "SUBMITTED")
        applied = reopened.transition(iid, "FILLED", at_utc=T2)
        self.assertEqual(applied["transition_result"], "APPLIED")
        self.assertEqual(reopened.get(iid)["state"], "FILLED")
        reopened.close()
        tmp.cleanup()

    def test_illegal_transition_refused_before_any_io_fault_never_fires(self) -> None:
        tmp, path = _tmp_journal()
        book = _book(path)
        created = book.create_intent(_req(), created_at_utc=T0)
        iid = created["intent_id"]
        # CREATED -> FILLED is illegal (must go through SUBMITTED first).

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(book._writer, "_fh", faulty):
                with self.assertRaises(IntentTransitionError) as cm:
                    book.transition(iid, "FILLED", at_utc=T1)
            # validation happens BEFORE any journal write -- the fault plan
            # (armed for the FIRST write call) never actually fired.
            self.assertEqual(faulty.counts["write"], 0)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertEqual(book.get(iid)["state"], "CREATED")
        book.close()
        tmp.cleanup()

    def test_book_closes_and_reopens_cleanly_after_a_faulted_create(self) -> None:
        tmp, path = _tmp_journal()
        book = _book(path)
        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(book._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    book.create_intent(_req(), created_at_utc=T0)
        self.assertTrue(LEDGER.record(cm.exception))
        book.close()  # never raises, lock released

        reopened = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(reopened.diagnostics()["created"], 0)
        reopened.create_intent(_req(), created_at_utc=T0)
        reopened.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Torn-tail crash mid-transition
# --------------------------------------------------------------------------- #
class TornTailRebuildTests(unittest.TestCase):
    def test_torn_tail_during_terminal_transition_rebuilds_to_last_complete_state(self) -> None:
        tmp, path = _tmp_journal()
        book = _book(path)
        created = book.create_intent(_req(), created_at_utc=T0)
        iid = created["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=T1)
        book.transition(iid, "FILLED", at_utc=T2)  # this line will be torn
        book.close()

        _tear_last_line(path)  # simulates a crash mid-append of the FILLED line

        rebuilt = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        record = rebuilt.get(iid)
        self.assertEqual(record["state"], "SUBMITTED")  # never a half-applied FILLED
        self.assertEqual(rebuilt.diagnostics()["by_state"]["SUBMITTED"], 1)
        self.assertEqual(rebuilt.diagnostics()["by_state"]["FILLED"], 0)
        rebuilt.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Structural / byte-level journal corruption -> always typed, never silent
# --------------------------------------------------------------------------- #
class CorruptionRebuildTests(unittest.TestCase):
    def test_mid_journal_corruption_sweep_rebuild_always_typed(self) -> None:
        tmp, path = _tmp_journal()
        book = _book(path)
        ids = []
        for i in range(6):
            created = book.create_intent(_req(magic=909707 + i), created_at_utc=T0)
            ids.append(created["intent_id"])
        for iid in ids[:3]:
            book.transition(iid, "SUBMITTED", at_utc=T1)
        book.close()

        baseline = path.read_bytes()
        step = max(1, len(baseline) // 10)
        positions = ByteCorruptor.sweep_positions(len(baseline), step)
        # never corrupt the very last line's tail (that is the DESIGNED
        # torn-tail path, already covered by TornTailRebuildTests); keep to
        # the first 80% of the file.
        positions = [p for p in positions if p < int(len(baseline) * 0.8)]
        self.assertGreaterEqual(len(positions), 4)

        for pos in positions:
            corrupted = ByteCorruptor.flip_byte(baseline, pos)
            path.write_bytes(corrupted)
            with self.subTest(position=pos):
                with self.assertRaises((JournalCorruptedError, IntentReplayError)) as cm:
                    OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
                self.assertTrue(LEDGER.record(cm.exception))
            path.write_bytes(baseline)

        healthy = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(healthy.diagnostics()["created"], 6)
        healthy.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Diagnostics consistency across a mixed faulted/successful sequence
# --------------------------------------------------------------------------- #
class DiagnosticsConsistencyTests(unittest.TestCase):
    def test_diagnostics_stay_consistent_after_mixed_faults(self) -> None:
        """FIXED (M07-P2): a faulted write now poisons the book's
        underlying writer terminally -- the ORIGINAL retry-on-same-instance
        pattern this test used is no longer valid (see
        ``CreateAndTransitionFaultTests`` above). Restructured to follow the
        one documented recovery discipline (close()+``rebuild_from_journal``
        immediately after each fault, BEFORE any further operation) while
        still proving the same end property: diagnostics stay fully
        consistent across a sequence mixing faulted and successful
        operations, even across multiple close/reopen cycles."""
        tmp, path = _tmp_journal()
        book = _book(path)
        created_ids = []
        for i in range(4):
            request = _req(magic=909800 + i)
            if i == 1:
                faulty = FaultyOS(fail_write_at={1})
                with faulty:
                    with faulty_existing_handle(book._writer, "_fh", faulty):
                        with self.assertRaises(OSError) as cm:
                            book.create_intent(request, created_at_utc=T0)
                self.assertTrue(LEDGER.record(cm.exception))
                self.assertTrue(book._writer.poisoned)
                with self.assertRaises(JournalPoisonedError) as cm_poisoned:
                    book.create_intent(request, created_at_utc=T0)
                self.assertTrue(LEDGER.record(cm_poisoned.exception))
                book.close()
                book = OrderIntentBook.rebuild_from_journal(
                    path, uuid_generator=MonotonicUUID7Generator())
            created = book.create_intent(request, created_at_utc=T0)
            created_ids.append(created["intent_id"])

        for i, iid in enumerate(created_ids):
            book.transition(iid, "SUBMITTED", at_utc=T1)
            if i == 2:
                faulty = FaultyOS(fail_write_at={1})
                with faulty:
                    with faulty_existing_handle(book._writer, "_fh", faulty):
                        with self.assertRaises(OSError) as cm2:
                            book.transition(iid, "FILLED", at_utc=T2)
                self.assertTrue(LEDGER.record(cm2.exception))
                self.assertTrue(book._writer.poisoned)
                with self.assertRaises(JournalPoisonedError) as cm2_poisoned:
                    book.transition(iid, "FILLED", at_utc=T2)
                self.assertTrue(LEDGER.record(cm2_poisoned.exception))
                book.close()
                book = OrderIntentBook.rebuild_from_journal(
                    path, uuid_generator=MonotonicUUID7Generator())
            book.transition(iid, "FILLED", at_utc=T2)

        diag = book.diagnostics()
        self.assertEqual(diag["created"], 4)
        self.assertEqual(sum(diag["by_state"].values()), 4)
        self.assertEqual(diag["by_state"]["FILLED"], 4)
        self.assertEqual(diag["by_state"]["CREATED"], 0)
        self.assertEqual(diag["by_state"]["SUBMITTED"], 0)
        for iid in created_ids:
            self.assertEqual(book.get(iid)["state"], "FILLED")
        book.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions observed
# --------------------------------------------------------------------------- #
class ZZOrderIntentCampaignLedgerTests(unittest.TestCase):
    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
