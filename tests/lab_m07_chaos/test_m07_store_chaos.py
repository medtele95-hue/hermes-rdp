"""M07-P1 — Campaign 1/7: STORE (M02-P1C ``LifecycleIdentityStore``) chaos.

Proves: every OSError injected at each stage of ``_persist`` (tmp open, tmp
write, fsync, os.replace) across every mutating operation (create, bind,
mark_closed, purge) raises the module's own typed ``StoreWriteError``, never
touches the on-disk file, never leaves an orphan ``.tmp``, never commits the
in-memory state, and leaves the store fully reusable afterward. Also: a
1-byte corruption sweep over a 20-record store always fails closed
(``StoreCorruptedError``, never a silent wrong load), a concurrent-writer
race is detected before any disk write is attempted, and repeated
consecutive faults never degrade the store.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chaos_tools import (
    ByteCorruptor,
    FaultyOS,
    Ledger,
    assert_file_unchanged,
    sha256_file,
)

from app.services.event_identity import MonotonicUUID7Generator
from app.services.lifecycle_identity_store import (
    ConcurrentWriterError,
    LifecycleIdentityStore,
    StoreCorruptedError,
    StoreWriteError,
    try_build_broker_ref,
)

ACCT = "acct-v1-" + "07" * 16
MAGIC = 909707
T1 = "2026-07-22T03:00:00+00:00"
T2 = "2026-07-22T03:15:00+00:00"

LEDGER = Ledger()

_TICKET_SEQ = [10_000]


def make_corr() -> str:
    return str(MonotonicUUID7Generator().new())


def build_ref(**overrides):
    _TICKET_SEQ[0] += 1
    base = dict(
        account_scope_id=ACCT, ticket=_TICKET_SEQ[0], broker_symbol="GOLD#",
        opened_at=T1, magic=MAGIC, position_identifier=_TICKET_SEQ[0] + 500_000,
        direction="BUY",
    )
    base.update(overrides)
    ref, reason = try_build_broker_ref(**base)
    assert reason is None, reason
    return ref


class _Ctx:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lifecycle_store.json"
        self.store = LifecycleIdentityStore(path=self.path)


# --------------------------------------------------------------------------- #
# Fault matrix: {create, bind, mark_closed, purge} x {open, write, fsync, replace}
# --------------------------------------------------------------------------- #
_OPS = ("create", "bind", "mark_closed", "purge")
_FAULT_POINTS = ("open", "write", "fsync", "replace")
_FAULT_KWARGS = {
    "open": dict(fail_open_at={1}),
    "write": dict(fail_write_at={1}),
    "fsync": dict(fail_fsync_at={1}),
    "replace": dict(fail_replace_at={1}),
}


class StoreWriteFaultMatrixTests(unittest.TestCase):
    """One test per (op, fault-point) combination -- generated below the
    class body so every combination is an individually visible/discoverable
    pytest test id."""

    def _prepare(self, op: str):
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        lid = rec["lifecycle_id"]
        if op == "purge":
            ctx.store.mark_closed(lid, closed_at_utc=T2)
        baseline_hash = sha256_file(ctx.path)
        baseline_meta = ctx.store.snapshot_metadata()
        return ctx, lid, baseline_hash, baseline_meta

    def _do(self, ctx: _Ctx, lid: str, op: str):
        if op == "create":
            return ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        if op == "bind":
            return ctx.store.bind_broker_position(lid, build_ref())
        if op == "mark_closed":
            return ctx.store.mark_closed(lid, closed_at_utc=T2)
        if op == "purge":
            return ctx.store.purge_closed(max_to_purge=1)
        raise AssertionError("unknown op %r" % op)

    def _run(self, op: str, point: str) -> None:
        ctx, lid, baseline_hash, baseline_meta = self._prepare(op)
        faulty = FaultyOS(
            open_target="app.services.lifecycle_identity_store",
            **_FAULT_KWARGS[point],
        )
        with faulty:
            with self.assertRaises(StoreWriteError) as cm:
                self._do(ctx, lid, op)
        self.assertTrue(LEDGER.record(cm.exception))
        self.assertEqual(cm.exception.reason_code, "STORE_WRITE_FAILED")

        tmp_path = ctx.path.with_name(ctx.path.name + ".tmp")
        self.assertFalse(tmp_path.exists(), "orphan .tmp left after failed %s/%s" % (op, point))
        assert_file_unchanged(self, ctx.path, baseline_hash, "disk changed after failed %s/%s" % (op, point))
        meta_after_fault = ctx.store.snapshot_metadata()
        self.assertEqual(meta_after_fault["generation"], baseline_meta["generation"])
        self.assertEqual(meta_after_fault["lifecycles_total"], baseline_meta["lifecycles_total"])

        # store fully reusable: the SAME operation, retried outside the
        # fault window, succeeds cleanly.
        result = self._do(ctx, lid, op)
        self.assertIsNotNone(result)
        ctx.tmp.cleanup()


def _make_matrix_test(op: str, point: str):
    def _test(self: StoreWriteFaultMatrixTests) -> None:
        self._run(op, point)

    _test.__name__ = "test_%s_fails_closed_on_%s_fault" % (op, point)
    return _test


for _op in _OPS:
    for _point in _FAULT_POINTS:
        _t = _make_matrix_test(_op, _point)
        setattr(StoreWriteFaultMatrixTests, _t.__name__, _t)
del _op, _point, _t


# --------------------------------------------------------------------------- #
# Byte-corruption sweep (20-record store)
# --------------------------------------------------------------------------- #
class StoreCorruptionSweepTests(unittest.TestCase):
    def test_single_byte_corruption_sweep_always_fails_closed(self) -> None:
        ctx = _Ctx()
        ids = []
        for i in range(20):
            rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
            ids.append(rec["lifecycle_id"])
        for i, lid in enumerate(ids):
            if i % 2 == 0:
                ctx.store.bind_broker_position(lid, build_ref())
            if i % 3 == 0:
                ctx.store.mark_closed(lid, closed_at_utc=T2)

        baseline = ctx.path.read_bytes()
        step = max(1, len(baseline) // 24)
        positions = ByteCorruptor.sweep_positions(len(baseline), step)
        self.assertGreaterEqual(len(positions), 15, "sweep too coarse to be meaningful")

        for pos in positions:
            corrupted = ByteCorruptor.flip_byte(baseline, pos)
            ctx.path.write_bytes(corrupted)
            with self.subTest(position=pos):
                with self.assertRaises(StoreCorruptedError) as cm:
                    LifecycleIdentityStore(path=ctx.path)
                self.assertTrue(LEDGER.record(cm.exception))
            ctx.path.write_bytes(baseline)  # restore before the next position

        # store still fully healthy after the whole sweep (baseline restored)
        fresh = LifecycleIdentityStore(path=ctx.path)
        self.assertEqual(fresh.snapshot_metadata()["lifecycles_total"], 20)
        ctx.tmp.cleanup()

    def test_reload_on_corrupted_file_preserves_prior_memory_state(self) -> None:
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        lid = rec["lifecycle_id"]
        before = ctx.store.snapshot_metadata()

        baseline = ctx.path.read_bytes()
        corrupted = ByteCorruptor.flip_byte(baseline, len(baseline) // 2)
        ctx.path.write_bytes(corrupted)

        with self.assertRaises(StoreCorruptedError) as cm:
            ctx.store.reload()
        self.assertTrue(LEDGER.record(cm.exception))

        after = ctx.store.snapshot_metadata()
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["lifecycles_total"], before["lifecycles_total"])
        self.assertIsNotNone(ctx.store.get_lifecycle(lid))  # prior state intact
        ctx.tmp.cleanup()


# --------------------------------------------------------------------------- #
# Concurrent writer / repeated faults
# --------------------------------------------------------------------------- #
class StoreConcurrencyAndRecoveryTests(unittest.TestCase):
    def test_concurrent_writer_detected_before_any_disk_write(self) -> None:
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        lid = rec["lifecycle_id"]

        # A foreign, independently-opened store instance bumps the on-disk
        # generation behind our ctx.store's back (simulates a second writer).
        foreign = LifecycleIdentityStore(path=ctx.path)
        foreign.mark_closed(lid, closed_at_utc=T2)
        hash_after_foreign_write = sha256_file(ctx.path)

        with self.assertRaises(ConcurrentWriterError) as cm:
            ctx.store.bind_broker_position(lid, build_ref())
        self.assertTrue(LEDGER.record(cm.exception))
        assert_file_unchanged(self, ctx.path, hash_after_foreign_write)
        ctx.tmp.cleanup()

    def test_repeated_consecutive_faults_then_recovery_not_degraded(self) -> None:
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        lid = rec["lifecycle_id"]
        baseline_hash = sha256_file(ctx.path)

        faulty = FaultyOS(
            open_target="app.services.lifecycle_identity_store",
            fail_fsync_at={1, 2, 3},
        )
        with faulty:
            for _ in range(3):
                with self.assertRaises(StoreWriteError) as cm:
                    ctx.store.bind_broker_position(lid, build_ref())
                self.assertTrue(LEDGER.record(cm.exception))

        assert_file_unchanged(self, ctx.path, baseline_hash)
        outcome = ctx.store.bind_broker_position(lid, build_ref())
        self.assertEqual(outcome, "BOUND")
        ctx.tmp.cleanup()


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions observed in this campaign
# --------------------------------------------------------------------------- #
class ZZStoreCampaignLedgerTests(unittest.TestCase):
    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
