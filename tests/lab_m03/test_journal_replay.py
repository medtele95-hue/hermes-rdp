"""M03-P1 — tests laboratoire : rebuild_lifecycle_state (pont journal ->
etat lifecycle) + bench append vs LifecycleIdentityStore.create_lifecycle.
100% additif, aucun fichier de production touche, valeurs 100% fictives.
"""
from __future__ import annotations

import statistics
import tempfile
import time
import unittest
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import (
    JournalReader,
    JournalWriter,
    rebuild_lifecycle_state,
)
from app.services.lifecycle_identity_store import (
    LifecycleIdentityStore,
    broker_ref_key,
    try_build_broker_ref,
)

ACCT = "acct-v1-" + "cd" * 16
T1 = "2026-07-22T10:00:00+00:00"
T2 = "2026-07-22T14:30:00+00:00"
NOW = "2026-07-22T15:00:00+00:00"


def gen_uuid7() -> str:
    return str(MonotonicUUID7Generator().new())


def ref_kwargs(**overrides):
    base = dict(account_scope_id=ACCT, ticket=555000, broker_symbol="GOLD#",
                opened_at=T1, magic=909333, position_identifier=7001, direction="BUY")
    base.update(overrides)
    return base


def build_ref(**overrides):
    ref, reason = try_build_broker_ref(**ref_kwargs(**overrides))
    assert reason is None, reason
    return ref


class ReconstructionEquivalenceTests(unittest.TestCase):
    def test_create_bind_close_recycled_ticket_matches_real_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.json"
            journal_path = Path(tmp) / "journal.jsonl"
            store = LifecycleIdentityStore(path=store_path)
            writer = JournalWriter(path=journal_path)

            def do_create(corr, created_at):
                rec = store.create_lifecycle(correlation_id=corr, created_at_utc=created_at)
                writer.append({
                    "op": "create", "lifecycle_id": rec["lifecycle_id"],
                    "correlation_id": rec["correlation_id"],
                    "created_at_utc": rec["created_at_utc"],
                    "boot_id": rec["boot_id"], "cycle_id": rec["cycle_id"],
                    "setup_id": rec["setup_id"],
                })
                return rec

            def do_bind(lifecycle_id, ref):
                store.bind_broker_position(lifecycle_id, ref)
                writer.append({"op": "bind", "lifecycle_id": lifecycle_id, "ref": ref})

            def do_close(lifecycle_id, closed_at):
                store.mark_closed(lifecycle_id, closed_at_utc=closed_at)
                writer.append({"op": "mark_closed", "lifecycle_id": lifecycle_id,
                                "closed_at_utc": closed_at})

            # cycle 1 : create -> bind -> close, puis rejeu ALREADY_BOUND / ALREADY_CLOSED
            a = do_create(gen_uuid7(), T1)
            ref_a = build_ref(position_identifier=7001, opened_at=T1)
            do_bind(a["lifecycle_id"], ref_a)
            do_close(a["lifecycle_id"], T2)
            do_bind(a["lifecycle_id"], ref_a)          # ALREADY_BOUND (idempotent)
            do_close(a["lifecycle_id"], NOW)            # ALREADY_CLOSED (idempotent)

            # ticket recycle -> lifecycle B distinct (discrimine par
            # position_identifier + opened_at, jamais par le ticket seul)
            b = do_create(gen_uuid7(), T2)
            ref_b = build_ref(position_identifier=9333, opened_at=T2)
            do_bind(b["lifecycle_id"], ref_b)

            writer.close()

            reader = JournalReader(journal_path)
            rebuilt = rebuild_lifecycle_state(reader)
            self.assertEqual(rebuilt["conflicts"], [])

            for lid in (a["lifecycle_id"], b["lifecycle_id"]):
                expected = store.get_lifecycle(lid)
                got = rebuilt["lifecycles"][lid]
                self.assertEqual(got, expected)

            expected_index = {}
            for lid in (a["lifecycle_id"], b["lifecycle_id"]):
                rec = store.get_lifecycle(lid)
                if rec["broker_key"] is not None:
                    expected_index[rec["broker_key"]] = lid
            self.assertEqual(rebuilt["broker_index"], expected_index)
            self.assertEqual(set(rebuilt["lifecycles"].keys()),
                              {a["lifecycle_id"], b["lifecycle_id"]})

    def test_conflicts_reported_and_ignored_never_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.jsonl"
            writer = JournalWriter(path=journal_path)
            lid1 = gen_uuid7()
            lid2 = gen_uuid7()
            ref = build_ref()

            def create_payload(lid):
                return {"op": "create", "lifecycle_id": lid, "correlation_id": None,
                         "created_at_utc": T1, "boot_id": None, "cycle_id": None,
                         "setup_id": None}

            writer.append(create_payload(lid1))
            writer.append(create_payload(lid1))                       # doublon -> conflit
            writer.append({"op": "bind", "lifecycle_id": lid1, "ref": ref})
            writer.append({"op": "bind", "lifecycle_id": lid2, "ref": ref})  # lid2 inconnu
            writer.append(create_payload(lid2))
            writer.append({"op": "bind", "lifecycle_id": lid2, "ref": ref})  # ref deja liee a lid1
            writer.append({"op": "teleport", "lifecycle_id": lid1})          # op inconnue
            writer.append({"op": "bind", "lifecycle_id": lid1, "ref": "pas-un-dict"})
            # NB : un payload racine non-dict est deja refuse par
            # JournalWriter.append() (ValidationError) -- il ne peut donc
            # jamais atterrir dans un journal reel ; rebuild_lifecycle_state
            # reste neanmoins defensif sur ce cas (isinstance(payload, dict)).
            writer.close()

            reader = JournalReader(journal_path)
            rebuilt = rebuild_lifecycle_state(reader)
            reasons = [c["reason"] for c in rebuilt["conflicts"]]

            self.assertIn("LIFECYCLE_ALREADY_EXISTS", reasons)
            self.assertIn("LIFECYCLE_UNKNOWN", reasons)
            self.assertIn("REFERENCE_ALREADY_BOUND", reasons)
            self.assertIn("UNKNOWN_OP", reasons)
            self.assertIn("BROKER_REF_INVALID", reasons)
            self.assertGreater(rebuilt["ignored"], 0)

            # aucun ecrasement : lid1 reste correctement lie a la ref d'origine
            self.assertIn(lid1, rebuilt["lifecycles"])
            self.assertEqual(rebuilt["lifecycles"][lid1]["broker_key"], broker_ref_key(ref))
            self.assertEqual(rebuilt["broker_index"][broker_ref_key(ref)], lid1)


class BenchTests(unittest.TestCase):
    def test_append_median_vs_store_create_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "bench_journal_nofsync.jsonl"
            store_path = Path(tmp) / "bench_store.json"
            writer = JournalWriter(path=journal_path, fsync_per_append=False)
            store = LifecycleIdentityStore(path=store_path)

            n = 200
            append_times = []
            for i in range(n):
                t0 = time.perf_counter()
                writer.append({"n": i, "kind": "BENCH"})
                append_times.append(time.perf_counter() - t0)
            writer.close()

            create_times = []
            for _ in range(n):
                t0 = time.perf_counter()
                store.create_lifecycle(correlation_id=None, created_at_utc=T1)
                create_times.append(time.perf_counter() - t0)

            med_append_ms = statistics.median(append_times) * 1000.0
            med_create_ms = statistics.median(create_times) * 1000.0
            print(
                "\n[M03-P1 BENCH] append(fsync=False) median=%.4fms | "
                "store.create_lifecycle median=%.4fms (O(n) full-file rewrite, n=%d records)"
                % (med_append_ms, med_create_ms, n)
            )
            # garde-fou large (labo) : l'append pur (pas de reecriture totale
            # du fichier) reste tres en dessous du cout O(n) du store.
            self.assertLess(med_append_ms, 5.0)

    def test_append_median_with_fsync_reported_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "bench_journal_fsync.jsonl"
            writer = JournalWriter(path=journal_path, fsync_per_append=True)
            n = 100
            times = []
            for i in range(n):
                t0 = time.perf_counter()
                writer.append({"n": i})
                times.append(time.perf_counter() - t0)
            writer.close()
            med_ms = statistics.median(times) * 1000.0
            print("\n[M03-P1 BENCH] append(fsync=True) median=%.4fms (n=%d, cout disque reel)"
                  % (med_ms, n))
            # aucun seuil strict : fsync engage un cout materiel variable,
            # ce test rapporte seulement le chiffre.


if __name__ == "__main__":
    unittest.main()
