"""M07-P1/M07-P2 — Campaign 2/7: JOURNAL (M03 ``event_journal``) chaos.

Proves the append-only journal's fail-closed guarantees under injected OS
faults, a stolen/foreign lock, and a 1-byte corruption sweep across a
multi-segment file. 100% additive: no ``app/`` file is touched from here.
All disk I/O goes through ``tempfile.TemporaryDirectory``. All values are
100% fictional.

M07-P1 originally found and documented 3 KNOWN_GAPS in the write-side
self-poisoning of ``JournalWriter`` (see the M07-P1 writer report / TESTS_
ORACLE review for the full narrative). M07-P2 CLOSED all 3 gaps in
``app/services/event_journal.py`` (terminal ``_poisoned`` state + typed
``JournalPoisonedError``, checked at the head of every write operation
BEFORE any I/O -- see the module docstring's "ETAT POISONED" section). The
tests below now encode the FIXED, corrected behaviour as strict assertions
rather than documenting a gap:

- INIT (very first segment creation): an ``open``-fault leaves nothing on
  disk (clean, unchanged). A ``write``-fault leaves a stray, EMPTY segment
  file on disk; the FIXED writer now cleans that orphan file up as part of
  poisoning the (discarded, never-returned) failed instance, so the very
  next open just works directly -- no human intervention needed any more. A
  ``flush``/``fsync``-fault still self-heals exactly as before (the header
  was already durably written or gets flushed for real on cleanup) -- the
  segment ends up complete and the next open just works, records=0 so far.
- STEADY-STATE ``append()``: a ``write``-fault is fully clean (nothing ever
  reaches the buffer). A ``flush``- or ``fsync``-fault is NOT disk-clean
  (bytes may already be durable -- fsync-fault ALSO breaks the "disk never
  changes on a failed op" property in the strict sense, by construction of
  where the fault is injected) but the writer instance is now immediately
  and terminally poisoned: the very NEXT ``append()`` on the SAME instance
  raises ``JournalPoisonedError`` BEFORE touching the disk at all -- never
  again a silent "looks-fine" success. ``close()`` (always safe, even
  poisoned) may durably flush the one already-serialized faulted line still
  sitting in the OS/Python buffer; if it does, that line is a STRUCTURALLY
  COMPLETE, valid record (correct seq, correct hash chain) so a reopen
  accepts it as a normal record -- exactly the documented "accept a valid
  but never-acknowledged final line" resume semantics, and there is never a
  second write from the same instance to collide with it.
- ROTATION: the same fix, generalized -- reusing a writer after a
  rotation-time fault is now blocked synchronously by the poisoned guard,
  so a malformed/duplicated segment can no longer be produced at all (it is
  not merely "caught later at read time" any more).

Takeaway for any future caller/mission: NEVER call ``append()`` again on a
``JournalWriter`` instance after it raised -- and now the writer itself
enforces this instead of merely documenting it. Always ``close()`` and
reopen -- that path is proven here to recover with zero data loss every
time.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chaos_tools import (
    ByteCorruptor,
    FaultyOS,
    Ledger,
    LockStealer,
    assert_file_unchanged,
    faulty_existing_handle,
    sha256_file,
)

from app.services.event_journal import (
    JournalCorruptedError,
    JournalPoisonedError,
    JournalReader,
    JournalWriter,
    OwnershipError,
)

LEDGER = Ledger()
# M07-P1 found 3 write-side self-poisoning gaps here; M07-P2 CLOSED all 3 in
# app/services/event_journal.py (terminal _poisoned state, checked before
# any I/O). KNOWN_GAPS is kept as a SEPARATE counter (rather than deleted)
# so the historical "3 documented, all typed" shape of the campaign stays
# visible -- but every scenario that used to record into it now instead
# proves the FIXED behaviour (JournalPoisonedError raised synchronously,
# zero corruption ever reaches disk from a reused instance) and records
# into LEDGER like every other correctly fail-closed scenario in this
# module. See ZZJournalCampaignLedgerTests below: KNOWN_GAPS is now
# expected to stay EMPTY (0 typed, 0 untyped) -- itself a regression guard
# against the gaps ever being silently reintroduced.
KNOWN_GAPS = Ledger()


def _tmp_journal():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name) / "journal.jsonl"


# --------------------------------------------------------------------------- #
# Initial segment creation: {open, write, flush, fsync} faults
# --------------------------------------------------------------------------- #
class InitFaultMatrixTests(unittest.TestCase):
    def test_open_fault_leaves_nothing_clean_retry(self) -> None:
        tmp, path = _tmp_journal()
        faulty = FaultyOS(open_target="app.services.event_journal", fail_open_at={1})
        with faulty:
            with self.assertRaises(OSError) as cm:
                JournalWriter(path=path)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertFalse(path.exists())
        self.assertFalse(LockStealer.lock_path_for(path).exists())

        w = JournalWriter(path=path)
        env = w.append({"ok": True})
        self.assertEqual(env["seq"], 1)
        w.close()
        tmp.cleanup()

    def test_write_fault_self_cleans_stray_empty_segment_reopen_needs_no_operator(self) -> None:
        """FIXED (was: test_write_fault_leaves_stray_empty_segment_blocking_
        reopen): the failed (discarded, never returned) JournalWriter
        instance now poisons itself AND best-effort cleans up the orphan
        EMPTY segment file it created before raising -- so the very next
        open needs zero manual/operator intervention any more."""
        tmp, path = _tmp_journal()
        faulty = FaultyOS(open_target="app.services.event_journal", fail_write_at={1})
        with faulty:
            with self.assertRaises(OSError) as cm:
                JournalWriter(path=path)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertFalse(LockStealer.lock_path_for(path).exists())

        # the orphan empty segment is gone -- self-cleaned, not left stray:
        self.assertFalse(path.exists())

        # next open just works directly, no unlink(), no JournalCorruptedError:
        w = JournalWriter(path=path)
        self.assertFalse(w.resume_report()["resumed"])
        env = w.append({"ok": True})
        self.assertEqual(env["seq"], 1)
        w.close()
        tmp.cleanup()

    def _run_self_healing_point(self, point: str, kwargs: dict) -> None:
        tmp, path = _tmp_journal()
        faulty = FaultyOS(open_target="app.services.event_journal", **kwargs)
        with faulty:
            with self.assertRaises(OSError) as cm:
                JournalWriter(path=path)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertFalse(LockStealer.lock_path_for(path).exists())

        # KNOWN GAP (self-healing side): the header was already durably
        # written (fsync-fault) or gets flushed for real when the leaked
        # real handle is garbage-collected (flush-fault) -- the segment
        # ends up complete, so reopening just works with zero records yet.
        w = JournalWriter(path=path)
        report = w.resume_report()
        self.assertTrue(report["resumed"], "expected the %s-fault header to have self-healed" % point)
        self.assertEqual(w.seq, 0)
        env = w.append({"ok": True})
        self.assertEqual(env["seq"], 1)
        w.close()

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 1)
        tmp.cleanup()

    def test_flush_fault_self_heals_via_gc_finalized_handle(self) -> None:
        self._run_self_healing_point("flush", dict(fail_flush_at={1}))

    def test_fsync_fault_self_heals_header_already_durable(self) -> None:
        self._run_self_healing_point("fsync", dict(fail_fsync_at={1}))


# --------------------------------------------------------------------------- #
# Steady-state append (no rotation)
# --------------------------------------------------------------------------- #
class AppendFaultMatrixTests(unittest.TestCase):
    def test_write_fault_is_fully_clean_reused_instance_now_poisoned(self) -> None:
        """FIXED (was: test_write_fault_is_fully_clean_and_recoverable): a
        write-fault is still fully clean on disk (nothing ever reaches the
        buffer) -- but the writer now applies the SAME uniform poisoning
        rule to every I/O exception, including this cleanest one. Reusing
        the SAME instance is blocked synchronously; recovery is always via
        close()+reopen, exactly like the flush/fsync cases below."""
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path)
        for i in range(3):
            w.append({"n": i})
        baseline_hash = sha256_file(path)
        baseline_seq = w.seq

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(w, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    w.append({"n": "faulted"})
        self.assertTrue(LEDGER.record(cm.exception))

        assert_file_unchanged(self, path, baseline_hash, "disk changed after a failed write-fault append")
        self.assertEqual(w.seq, baseline_seq)
        self.assertTrue(w.poisoned)
        self.assertEqual(w.poison_reason, "APPEND_IO_FAILED:OSError")

        # reusing the SAME instance is now refused synchronously, BEFORE
        # any I/O -- disk stays byte-identical to right after the fault:
        with self.assertRaises(JournalPoisonedError) as cm2:
            w.append({"n": "must-not-write"})
        self.assertTrue(LEDGER.record(cm2.exception))
        assert_file_unchanged(self, path, baseline_hash, "poisoned instance must never write")
        self.assertEqual(w.seq, baseline_seq)
        w.close()

        reopened = JournalWriter(path=path)
        self.assertEqual(reopened.seq, baseline_seq)  # zero data loss
        env = reopened.append({"n": "recovered"})
        self.assertEqual(env["seq"], baseline_seq + 1)
        reopened.close()

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], baseline_seq + 1)
        payloads = reader.replay()
        self.assertEqual(payloads[-1], {"n": "recovered"})
        tmp.cleanup()

    def _run_fixed_point(self, point: str, kwargs: dict, *, disk_changes_on_fault: bool) -> None:
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path)
        for i in range(3):
            w.append({"n": i})
        baseline_hash = sha256_file(path)
        baseline_seq = w.seq

        faulty = FaultyOS(**kwargs)
        with faulty:
            with faulty_existing_handle(w, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    w.append({"n": "faulted"})
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertEqual(w.seq, baseline_seq, "internal seq must never advance on a raised append")
        self.assertTrue(w.poisoned)
        self.assertEqual(w.poison_reason, "APPEND_IO_FAILED:OSError")

        hash_after_first_fault = sha256_file(path)
        changed = hash_after_first_fault != baseline_hash
        self.assertEqual(
            changed, disk_changes_on_fault,
            "%s-fault disk-mutation-on-failure expectation mismatch" % point,
        )

        # FIXED: the caller is now told immediately and synchronously -- the
        # very next append() on the SAME (un-reopened) writer instance
        # raises JournalPoisonedError BEFORE any further I/O, instead of
        # silently returning success.
        with self.assertRaises(JournalPoisonedError) as cm2:
            w.append({"n": "looks-fine"})
        self.assertTrue(LEDGER.record(cm2.exception))
        self.assertEqual(w.seq, baseline_seq)
        assert_file_unchanged(
            self, path, hash_after_first_fault,
            "the poisoned second append() must never touch disk (%s-fault)" % point,
        )
        w.close()

        # close()+reopen (the one documented recovery path) always yields a
        # journal the reader accepts as intact -- never a corrupted chain,
        # and never a duplicated/colliding seq, since the poisoned instance
        # could never issue a second write to collide with the first.
        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"], "%s-fault: journal must never end up corrupted" % point)
        payloads = reader.replay()  # must not raise
        # the one already-serialized faulted line MAY have become durable
        # (immediately for fsync -- write()+flush() already completed for
        # real; or later, via w.close() flushing the pending Python buffer,
        # for flush -- see module docstring "ETAT POISONED"/semantique E/S)
        # -- either way it is a structurally valid, non-colliding record.
        self.assertIn(v["records"], (baseline_seq, baseline_seq + 1))
        if v["records"] == baseline_seq + 1:
            self.assertEqual(payloads[-1], {"n": "faulted"})

        reopened = JournalWriter(path=path)
        self.assertEqual(reopened.seq, v["records"])
        env = reopened.append({"n": "after-recovery"})
        self.assertEqual(env["seq"], v["records"] + 1)
        reopened.close()
        final = JournalReader(path).verify()
        self.assertTrue(final["intact"])
        tmp.cleanup()

    def test_flush_fault_next_append_now_blocked_by_poisoning(self) -> None:
        self._run_fixed_point("flush", dict(fail_flush_at={1}), disk_changes_on_fault=False)

    def test_fsync_fault_next_append_now_blocked_by_poisoning(self) -> None:
        # fsync-only fault: write()+flush() already completed for real
        # BEFORE the injected fault fires -- this is the one scenario in
        # the whole M07 campaign where "disk unchanged after a failed
        # operation" does NOT hold, by construction of where the fault is
        # injected (see module docstring). Fixed behaviour still applies:
        # the poisoned instance can never issue a colliding second write.
        self._run_fixed_point("fsync", dict(fail_fsync_at={1}), disk_changes_on_fault=True)


# --------------------------------------------------------------------------- #
# Rotation-in-panne (same root cause, generalized to a segment rollover)
# --------------------------------------------------------------------------- #
class RotationFaultTests(unittest.TestCase):
    @staticmethod
    def _seed_near_rotation(path) -> JournalWriter:
        w = JournalWriter(path=path, max_segment_bytes=200)
        for i in range(5):
            w.append({"n": i, "pad": "x" * 20})
        return w

    def test_rotation_fault_then_close_reopen_recovers_zero_data_loss(self) -> None:
        tmp, path = _tmp_journal()
        w = self._seed_near_rotation(path)
        seq_before = w.seq

        faulty = FaultyOS(open_target="app.services.event_journal", fail_fsync_at={1})
        with faulty:
            with self.assertRaises(OSError) as cm:
                w.append({"n": "boom"})
        self.assertTrue(LEDGER.record(cm.exception))

        # SAFE discipline: never call append() again on a writer that just
        # raised -- close it and reopen instead.
        w.close()

        reopened = JournalWriter(path=path)
        self.assertEqual(reopened.seq, seq_before)  # zero data loss
        env = reopened.append({"n": "after-recovery"})
        self.assertEqual(env["seq"], seq_before + 1)
        reopened.close()

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], seq_before + 1)
        payloads = reader.replay()
        self.assertEqual(payloads[-1], {"n": "after-recovery"})
        tmp.cleanup()

    def test_reusing_writer_after_rotation_fault_now_blocked_before_corruption(self) -> None:
        """FIXED (was: test_known_gap_reusing_writer_after_rotation_fault_
        is_caught_at_read_time): deliberately exercises the UNSAFE pattern
        (reusing a writer instance after it raised). The fixed writer no
        longer lets this corrupt the on-disk journal at all -- the reused
        call is refused synchronously by the poisoned guard, so the
        malformed/duplicated-segment scenario this test used to produce can
        no longer be produced in the first place."""
        tmp, path = _tmp_journal()
        w = self._seed_near_rotation(path)
        seq_before = w.seq

        faulty = FaultyOS(open_target="app.services.event_journal", fail_fsync_at={1})
        with faulty:
            with self.assertRaises(OSError) as cm:
                w.append({"n": "boom-1"})
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertTrue(w.poisoned)
        self.assertEqual(w.poison_reason, "SEGMENT_CREATE_FAILED:OSError")

        # UNSAFE pattern still attempted deliberately: the caller ignores
        # the failure and tries to reuse the same instance -- now refused
        # immediately instead of silently corrupting the journal.
        with self.assertRaises(JournalPoisonedError) as cm2:
            w.append({"n": "boom-2"})
        self.assertTrue(LEDGER.record(cm2.exception))
        w.close()

        reopened = JournalWriter(path=path)
        self.assertEqual(reopened.seq, seq_before)  # zero data loss
        env = reopened.append({"n": "after-recovery"})
        self.assertEqual(env["seq"], seq_before + 1)
        reopened.close()

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"], "reused-after-rotation-fault must never corrupt the journal any more")
        self.assertEqual(v["records"], seq_before + 1)
        payloads = reader.replay()
        self.assertEqual(payloads[-1], {"n": "after-recovery"})
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Lock chaos: foreign lock + abandoned ("crashed") writer
# --------------------------------------------------------------------------- #
class LockChaosTests(unittest.TestCase):
    def test_foreign_lock_blocks_new_writer_and_is_preserved(self) -> None:
        tmp, path = _tmp_journal()
        lock_path = LockStealer.plant_foreign_lock(path)
        with self.assertRaises(OwnershipError) as cm:
            JournalWriter(path=path)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertTrue(lock_path.exists())
        self.assertEqual(
            json.loads(lock_path.read_text(encoding="utf-8"))["writer_token"],
            "m07-chaos-foreign-token",
        )
        lock_path.unlink()  # operator cleanup
        w = JournalWriter(path=path)
        w.append({"ok": True})
        w.close()
        tmp.cleanup()

    def test_abandoned_writer_simulates_crash_second_writer_refused_no_corruption(self) -> None:
        tmp, path = _tmp_journal()
        w1 = JournalWriter(path=path)
        w1.append({"n": 1})
        w1.append({"n": 2})
        hash_at_crash = sha256_file(path)
        # SIMULATED CRASH: w1 is abandoned (never .close()'d) -- its lock
        # file stays behind, exactly like a killed process would leave it.
        del w1

        with self.assertRaises(OwnershipError) as cm:
            JournalWriter(path=path)
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, path, hash_at_crash)

        lock_path = LockStealer.lock_path_for(path)
        self.assertTrue(lock_path.exists())
        lock_path.unlink()  # documented operator recovery step after a real crash

        w2 = JournalWriter(path=path)
        self.assertEqual(w2.seq, 2)  # zero data loss
        env = w2.append({"n": 3})
        self.assertEqual(env["seq"], 3)
        w2.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Byte-corruption sweep, multi-segment
# --------------------------------------------------------------------------- #
class CorruptionSweepTests(unittest.TestCase):
    def test_multi_segment_corruption_sweep_always_fails_closed(self) -> None:
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path, max_segment_bytes=300)
        for i in range(20):
            w.append({"n": i, "pad": "x" * 8})
        w.close()

        segments = [path]
        i = 1
        while True:
            p = Path(str(path) + "." + str(i))
            if not p.exists():
                break
            segments.append(p)
            i += 1
        self.assertGreater(len(segments), 1)

        target = segments[-1]
        baseline = target.read_bytes()
        step = max(1, len(baseline) // 12)
        positions = ByteCorruptor.sweep_positions(len(baseline), step)
        self.assertGreaterEqual(len(positions), 6)

        for pos in positions:
            corrupted = ByteCorruptor.flip_byte(baseline, pos)
            target.write_bytes(corrupted)
            with self.subTest(position=pos):
                reader = JournalReader(path)
                v = reader.verify()
                if v["intact"] and v["tail_status"] == "TORN_TAIL_QUARANTINED":
                    # Corrupting the file's FINAL trailing newline is
                    # legitimately indistinguishable from "the writer
                    # crashed mid-write of the very last line" -- the
                    # DESIGNED, intentional graceful degradation: the last
                    # record is quarantined, everything before it intact.
                    self.assertEqual(v["records"], 19)
                elif v["intact"]:
                    self.assertEqual(v["records"], 20)
                else:
                    with self.assertRaises(JournalCorruptedError) as cm:
                        reader.replay()
                    self.assertTrue(LEDGER.record(cm.exception))
            target.write_bytes(baseline)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 20)
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Transversal: zero UNDOCUMENTED untyped exceptions observed
# --------------------------------------------------------------------------- #
class ZZJournalCampaignLedgerTests(unittest.TestCase):
    def test_zz_known_gap_ledger_now_empty_gaps_fixed(self) -> None:
        """M07-P2 CLOSED all 3 gaps M07-P1 found: no scenario in this file
        records into KNOWN_GAPS any more (every one now proves the FIXED
        behaviour and records into LEDGER instead, like every other
        correctly fail-closed scenario). This assertion doubles as a
        regression guard: if a future change reopens one of these gaps, the
        corresponding test above starts failing before this one could ever
        see a KNOWN_GAPS entry again."""
        self.assertEqual(len(KNOWN_GAPS.untyped), 0, KNOWN_GAPS.describe_untyped())
        self.assertEqual(len(KNOWN_GAPS.typed), 0)

    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
