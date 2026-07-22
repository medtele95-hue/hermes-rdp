"""M02-P1C — tests laboratoire : lifecycle mapping durable + dettes P1A.

Aucun .env, aucun bail, aucun MT5, aucun reseau, aucun fichier production :
tout le disque passe par tempfile.TemporaryDirectory.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from app.services.event_identity import (
    EventIdentityContext,
    MonotonicUUID7Generator,
)
from app.services.event_identity_runtime import IdentityShadowEnricher
from app.services.lifecycle_identity_store import (
    ConcurrentWriterError,
    ConflictError,
    LifecycleIdentityStore,
    LifecycleValidationError,
    StoreCapacityError,
    StoreCorruptedError,
    StoreWriteError,
    UnknownLifecycleError,
    broker_ref_key,
    try_build_broker_ref,
)
from app.services.runtime_provenance import RuntimeProvenance

ACCT = "acct-v1-" + "ab" * 16
T1 = "2026-07-21T10:00:00+00:00"
T2 = "2026-07-21T14:30:00+00:00"
NOW = "2026-07-21T15:00:00+00:00"


def make_corr():
    return str(MonotonicUUID7Generator().new())


def ref_kwargs(**overrides):
    base = dict(account_scope_id=ACCT, ticket=12345, broker_symbol="GOLD#",
                opened_at=T1, magic=909002, position_identifier=8001,
                direction="BUY")
    base.update(overrides)
    return base


def build_ref(**overrides):
    ref, reason = try_build_broker_ref(**ref_kwargs(**overrides))
    assert reason is None, reason
    return ref


class _Ctx:
    """Petit contexte : store + chemin temporaire par test."""

    def __init__(self, **kwargs):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lifecycle_store.json"
        self.store = LifecycleIdentityStore(path=self.path, **kwargs)


class BrokerRefContractTests(unittest.TestCase):
    def test_strict_types_no_silent_conversion(self):
        cases = [
            (dict(ticket=True), "TICKET_INVALID"),
            (dict(ticket="12345"), "TICKET_INVALID"),
            (dict(ticket=-1), "TICKET_INVALID"),
            (dict(magic=True), "MAGIC_INVALID"),
            (dict(magic="909002"), "MAGIC_INVALID"),
            (dict(position_identifier="8001"), "POSITION_IDENTIFIER_INVALID"),
            (dict(account_scope_id="login:111000999"), "ACCOUNT_SCOPE_INVALID"),
            (dict(account_scope_id=None), "ACCOUNT_SCOPE_INVALID"),
            (dict(broker_symbol="  "), "SYMBOL_MISSING"),
            (dict(opened_at="2026-07-21T10:00:00"), "OPENED_AT_INVALID"),  # naif
            (dict(opened_at=None), "OPENED_AT_INVALID"),
            (dict(opened_at=True), "OPENED_AT_INVALID"),
            (dict(direction="LONG"), "DIRECTION_INVALID"),
        ]
        for overrides, expected in cases:
            ref, reason = try_build_broker_ref(**ref_kwargs(**overrides))
            self.assertIsNone(ref, overrides)
            self.assertEqual(reason, expected)

    def test_ticket_alone_is_never_a_key(self):
        a = build_ref()
        b = build_ref(position_identifier=9107, opened_at=T2)
        self.assertEqual(a["ticket"], b["ticket"])
        self.assertNotEqual(broker_ref_key(a), broker_ref_key(b))

    def test_opened_at_msc_accepted(self):
        ref = build_ref(opened_at=1789000000000)
        self.assertEqual(ref["opened_at"], "msc:1789000000000")


class StoreCoreTests(unittest.TestCase):
    def test_lifecycle_uuid7_unique_never_from_ticket(self):
        ctx = _Ctx()
        ids = set()
        for _ in range(20):
            rec = ctx.store.create_lifecycle(
                correlation_id=make_corr(), created_at_utc=NOW)
            parsed = uuid.UUID(rec["lifecycle_id"])
            self.assertEqual(parsed.version, 7)
            ids.add(rec["lifecycle_id"])
        self.assertEqual(len(ids), 20)

    def test_bind_idempotent_no_generation_bump(self):
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        ref = build_ref()
        self.assertEqual(ctx.store.bind_broker_position(rec["lifecycle_id"], ref), "BOUND")
        gen = ctx.store.snapshot_metadata()["generation"]
        self.assertEqual(ctx.store.bind_broker_position(rec["lifecycle_id"], ref), "ALREADY_BOUND")
        self.assertEqual(ctx.store.snapshot_metadata()["generation"], gen)

    def test_conflicts_refused_no_overwrite(self):
        ctx = _Ctx()
        a = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        b = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        ref = build_ref()
        ctx.store.bind_broker_position(a["lifecycle_id"], ref)
        with self.assertRaises(ConflictError) as cm:
            ctx.store.bind_broker_position(b["lifecycle_id"], ref)
        self.assertEqual(cm.exception.reason_code, "REFERENCE_ALREADY_BOUND")
        other = build_ref(ticket=99999)
        with self.assertRaises(ConflictError) as cm:
            ctx.store.bind_broker_position(a["lifecycle_id"], other)
        self.assertEqual(cm.exception.reason_code, "LIFECYCLE_ALREADY_BOUND")
        resolved, _ = ctx.store.resolve_by_broker_position(ref)
        self.assertEqual(resolved["lifecycle_id"], a["lifecycle_id"])  # intact

    def test_unknown_lifecycle_and_invalid_inputs(self):
        ctx = _Ctx()
        with self.assertRaises(UnknownLifecycleError):
            ctx.store.bind_broker_position("0" * 32, build_ref())
        with self.assertRaises(LifecycleValidationError):
            ctx.store.create_lifecycle(correlation_id=str(uuid.uuid4()),  # v4 rejete
                                       created_at_utc=NOW)
        with self.assertRaises(LifecycleValidationError):
            ctx.store.create_lifecycle(correlation_id=make_corr(),
                                       created_at_utc="2026-07-21T15:00:00")  # naif
        with self.assertRaises(LifecycleValidationError):
            ctx.store.create_lifecycle(correlation_id=make_corr(),
                                       created_at_utc=NOW, cycle_id=True)

    def test_resolve_incomplete_ref_unresolved(self):
        ctx = _Ctx()
        record = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        ctx.store.bind_broker_position(record["lifecycle_id"], build_ref())
        bad = dict(build_ref())
        bad["opened_at"] = "n/a"
        resolved, reason = ctx.store.resolve_by_broker_position(bad)
        self.assertIsNone(resolved)
        self.assertEqual(reason, "BROKER_REF_INVALID")
        unknown = build_ref(ticket=777)
        resolved, reason = ctx.store.resolve_by_broker_position(unknown)
        self.assertIsNone(resolved)
        self.assertEqual(reason, "BROKER_REF_UNKNOWN")


class TicketRecyclingTests(unittest.TestCase):
    def test_recycled_ticket_yields_distinct_lifecycles(self):
        ctx = _Ctx()
        la = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        ref_a = build_ref(position_identifier=8001, opened_at=T1)
        ctx.store.bind_broker_position(la["lifecycle_id"], ref_a)
        ctx.store.mark_closed(la["lifecycle_id"], closed_at_utc=T2)

        lb = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T2)
        ref_b = build_ref(position_identifier=9107, opened_at=T2)
        ctx.store.bind_broker_position(lb["lifecycle_id"], ref_b)

        self.assertNotEqual(la["lifecycle_id"], lb["lifecycle_id"])
        got_a, _ = ctx.store.resolve_by_broker_position(ref_a)
        got_b, _ = ctx.store.resolve_by_broker_position(ref_b)
        self.assertEqual(got_a["lifecycle_id"], la["lifecycle_id"])
        self.assertEqual(got_b["lifecycle_id"], lb["lifecycle_id"])
        self.assertEqual(got_a["state"], "CLOSED")
        self.assertEqual(got_b["state"], "OPEN")
        # aucun etat de A attribue a B
        self.assertIsNone(got_b["closed_at_utc"])

    def test_recycled_ticket_without_position_identifier(self):
        ctx = _Ctx()
        la = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T1)
        lb = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=T2)
        ref_a = build_ref(position_identifier=None, opened_at=T1)
        ref_b = build_ref(position_identifier=None, opened_at=T2)
        ctx.store.bind_broker_position(la["lifecycle_id"], ref_a)
        ctx.store.bind_broker_position(lb["lifecycle_id"], ref_b)
        got_a, _ = ctx.store.resolve_by_broker_position(ref_a)
        got_b, _ = ctx.store.resolve_by_broker_position(ref_b)
        self.assertNotEqual(got_a["lifecycle_id"], got_b["lifecycle_id"])
        # discriminant manquant -> UNRESOLVED, aucune supposition
        ref, reason = try_build_broker_ref(**{**ref_kwargs(), "opened_at": None,
                                              "position_identifier": None})
        self.assertIsNone(ref)
        self.assertEqual(reason, "OPENED_AT_INVALID")


class RestartTests(unittest.TestCase):
    def test_full_restart_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            corr = make_corr()
            prov_a = RuntimeProvenance()
            store_a = LifecycleIdentityStore(path=path)
            rec = store_a.create_lifecycle(
                correlation_id=corr, created_at_utc=T1,
                boot_id=prov_a.boot_id, cycle_id=7,
                setup_id="3f0c1a2e-1111-2222-3333-444455556666")
            ref = build_ref()
            store_a.bind_broker_position(rec["lifecycle_id"], ref)
            del store_a  # instance A completement fermee

            prov_b = RuntimeProvenance()  # nouvelle provenance process (OK)
            store_b = LifecycleIdentityStore(path=path)
            got, _ = store_b.resolve_by_broker_position(ref)
            self.assertEqual(got["lifecycle_id"], rec["lifecycle_id"])  # jamais re-minte
            self.assertEqual(got["correlation_id"], corr)
            self.assertEqual(got["broker_key"], broker_ref_key(ref))
            self.assertEqual(got["state"], "OPEN")
            self.assertEqual(got["boot_id"], prov_a.boot_id)  # provenance de CREATION
            self.assertNotEqual(prov_b.boot_id, prov_a.boot_id)

            self.assertEqual(store_b.mark_closed(rec["lifecycle_id"], closed_at_utc=T2), "CLOSED")
            store_c = LifecycleIdentityStore(path=path)
            got_c = store_c.get_lifecycle(rec["lifecycle_id"])
            self.assertEqual(got_c["state"], "CLOSED")
            self.assertEqual(got_c["closed_at_utc"], T2)
            # idempotence : re-fermer ne change rien, closed_at preserve
            self.assertEqual(store_c.mark_closed(rec["lifecycle_id"], closed_at_utc=NOW),
                             "ALREADY_CLOSED")
            self.assertEqual(store_c.get_lifecycle(rec["lifecycle_id"])["closed_at_utc"], T2)


class CrashCorruptionTests(unittest.TestCase):
    def _seeded(self):
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        ctx.store.bind_broker_position(rec["lifecycle_id"], build_ref())
        return ctx, rec

    def test_write_failure_preserves_old_file_and_memory_uncommitted(self):
        ctx, _rec = self._seeded()
        before = ctx.path.read_bytes()
        gen = ctx.store.snapshot_metadata()["generation"]
        with mock.patch("app.services.lifecycle_identity_store.os.replace",
                        side_effect=OSError("disk full")):
            with self.assertRaises(StoreWriteError):
                ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        self.assertEqual(ctx.path.read_bytes(), before)  # ancien fichier preserve
        self.assertEqual(ctx.store.snapshot_metadata()["generation"], gen)
        self.assertFalse((ctx.path.parent / (ctx.path.name + ".tmp")).exists())
        # le store reste utilisable apres l'echec
        ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)

    def test_leftover_tmp_ignored(self):
        ctx, rec = self._seeded()
        (ctx.path.parent / (ctx.path.name + ".tmp")).write_text("{trunc", encoding="utf-8")
        again = LifecycleIdentityStore(path=ctx.path)
        self.assertIsNotNone(again.get_lifecycle(rec["lifecycle_id"]))

    def test_corruptions_fail_closed_no_silent_reset(self):
        ctx, _rec = self._seeded()
        original = ctx.path.read_text(encoding="utf-8")
        state = json.loads(original)

        def corrupt_variants():
            yield original[: len(original) // 2], "STORE_INVALID_JSON"        # tronque
            yield "pas du json", "STORE_INVALID_JSON"
            s = dict(state); s["champ_inconnu"] = 1
            yield json.dumps(s), "STORE_SCHEMA_INVALID"
            s = {k: v for k, v in state.items() if k != "generation"}
            yield json.dumps(s), "STORE_SCHEMA_INVALID"
            s = dict(state); s["generation"] = "3"
            yield json.dumps(s), "STORE_GENERATION_INVALID"
            s = dict(state); s["content_digest"] = "0" * 64
            yield json.dumps(s), "STORE_DIGEST_MISMATCH"
            s = json.loads(original); next(iter(s["lifecycles"].values()))["state"] = "LIMBO"
            yield json.dumps(s), "STORE_DIGEST_MISMATCH"  # digest proteste d'abord

        for text, expected_reason in corrupt_variants():
            ctx.path.write_text(text, encoding="utf-8")
            with self.assertRaises(StoreCorruptedError) as cm:
                LifecycleIdentityStore(path=ctx.path)
            self.assertEqual(cm.exception.reason_code, expected_reason)
            # aucun reset silencieux : le fichier corrompu est laisse tel quel
            self.assertEqual(ctx.path.read_text(encoding="utf-8"), text)

    def test_record_level_corruption_with_valid_digest(self):
        ctx, _rec = self._seeded()
        state = json.loads(ctx.path.read_text(encoding="utf-8"))
        rec = next(iter(state["lifecycles"].values()))
        rec["state"] = "LIMBO"
        from app.services.lifecycle_identity_store import _content_digest

        state["content_digest"] = _content_digest(
            state["generation"], state["lifecycles"], state["broker_index"])
        ctx.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(StoreCorruptedError) as cm:
            LifecycleIdentityStore(path=ctx.path)
        self.assertEqual(cm.exception.reason_code, "RECORD_STATE_INVALID")

    def test_reload_on_corruption_keeps_memory(self):
        ctx, rec = self._seeded()
        ctx.path.write_text("corrompu", encoding="utf-8")
        with self.assertRaises(StoreCorruptedError):
            ctx.store.reload()
        self.assertIsNotNone(ctx.store.get_lifecycle(rec["lifecycle_id"]))  # memoire intacte

    def test_concurrent_writer_detected_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            a = LifecycleIdentityStore(path=path)
            a.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
            b = LifecycleIdentityStore(path=path)  # meme generation chargee
            rec_a2 = a.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
            with self.assertRaises(ConcurrentWriterError):
                b.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
            # l'etat concurrent de A n'est pas ecrase
            fresh = LifecycleIdentityStore(path=path)
            self.assertIsNotNone(fresh.get_lifecycle(rec_a2["lifecycle_id"]))
            self.assertEqual(fresh.snapshot_metadata()["lifecycles_total"], 2)

    def test_symlink_and_allowed_dir_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(StoreCorruptedError) as cm:
                    LifecycleIdentityStore(path=path)
            self.assertEqual(cm.exception.reason_code, "STORE_PATH_SYMLINK")
            other = Path(tmp) / "ailleurs"
            other.mkdir()
            with self.assertRaises(StoreCorruptedError) as cm:
                LifecycleIdentityStore(path=path, allowed_dir=other)
            self.assertEqual(cm.exception.reason_code, "STORE_PATH_OUTSIDE_ALLOWED_DIR")

    def test_max_bytes_rejected(self):
        ctx, _rec = self._seeded()
        with self.assertRaises(StoreCorruptedError) as cm:
            LifecycleIdentityStore(path=ctx.path, max_bytes=10)
        self.assertEqual(cm.exception.reason_code, "STORE_TOO_LARGE")

    def test_thread_safety_creates(self):
        ctx = _Ctx()
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            for _ in range(10):
                try:
                    rec = ctx.store.create_lifecycle(
                        correlation_id=make_corr(), created_at_utc=NOW)
                    with lock:
                        results.append(rec["lifecycle_id"])
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 40)
        ctx.store.reload()
        self.assertEqual(ctx.store.snapshot_metadata()["lifecycles_total"], 40)


class RetentionTests(unittest.TestCase):
    def test_capacity_open_only_fail_closed(self):
        ctx = _Ctx(max_lifecycles=3)
        recs = [ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
                for _ in range(3)]
        with self.assertRaises(StoreCapacityError):
            ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        # aucun OPEN purge automatiquement, aucun overwrite du plus ancien
        for rec in recs:
            self.assertIsNotNone(ctx.store.get_lifecycle(rec["lifecycle_id"]))
        self.assertEqual(ctx.store.purge_closed(max_to_purge=10), 0)  # OPEN jamais purge

    def test_explicit_closed_purge_frees_capacity(self):
        ctx = _Ctx(max_lifecycles=3)
        recs = [ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
                for _ in range(3)]
        ctx.store.mark_closed(recs[0]["lifecycle_id"], closed_at_utc=NOW)
        self.assertEqual(ctx.store.purge_closed(max_to_purge=1), 1)
        ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        self.assertIsNone(ctx.store.get_lifecycle(recs[0]["lifecycle_id"]))
        self.assertIsNotNone(ctx.store.get_lifecycle(recs[1]["lifecycle_id"]))


class PurityAndSafetyTests(unittest.TestCase):
    def test_store_module_purity(self):
        import app.services.lifecycle_identity_store as mod

        import inspect

        src = inspect.getsource(mod)
        for forbidden in ("MetaTrader5", "os.environ", "getenv", "socket",
                          "subprocess", "datetime.now", "time.time"):
            self.assertNotIn(forbidden, src)
        imported = {name for name, value in vars(mod).items()
                    if isinstance(value, types.ModuleType)}
        self.assertEqual(imported, {"hashlib", "json", "os", "re", "threading", "uuid"})

    def test_no_sensitive_data_on_disk(self):
        ctx = _Ctx()
        rec = ctx.store.create_lifecycle(correlation_id=make_corr(), created_at_utc=NOW)
        ctx.store.bind_broker_position(rec["lifecycle_id"], build_ref())
        text = ctx.path.read_text(encoding="utf-8")
        for lit in ("111000999", "canary-user", "login", "password"):
            self.assertNotIn(lit, text)


def lab_context(instance="bot-lab-m02p1c"):
    return EventIdentityContext(
        server_id="srv-" + "0" * 16,
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )


def deep_diff_count(a, b):
    if type(a) is not type(b):
        return 1
    if isinstance(a, dict):
        n = len(a.keys() ^ b.keys())
        return n + sum(deep_diff_count(a[k], b[k]) for k in a.keys() & b.keys())
    if isinstance(a, list):
        if len(a) != len(b):
            return 1
        return sum(deep_diff_count(x, y) for x, y in zip(a, b))
    return 0 if a == b else 1


BASE_EVENT = {"event_type": "DEMO_SKIP", "setup_id": "3f0c1a2e-1111-2222-3333-444455556666",
              "broker_symbol": "GOLD#", "strategy": "GOLD_RANGE_BREAKOUT",
              "direction": "SELL"}


class SetupIdDebtTests(unittest.TestCase):
    def setUp(self):
        self.enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)

    def test_non_str_setup_id_is_invalid_never_converted(self):
        for bad in (123, 12.5, True, ["x"], {"a": 1}):
            event = dict(BASE_EVENT, setup_id=bad)
            snapshot = copy.deepcopy(event)
            out = self.enricher.enrich(event)  # aucune exception
            shadow = out["identity_shadow"]
            self.assertEqual(shadow["correlation_quality"], "UNRESOLVED")
            self.assertEqual(shadow["unresolved_reason_code"], "SETUP_ID_INVALID")
            self.assertIsNone(shadow["correlation_id"])
            stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
            self.assertEqual(deep_diff_count(snapshot, stripped), 0)

    def test_missing_and_valid_setup_id_unchanged_contract(self):
        out = self.enricher.enrich(dict(BASE_EVENT, setup_id=None))
        self.assertEqual(out["identity_shadow"]["unresolved_reason_code"], "SETUP_ID_MISSING")
        out = self.enricher.enrich(dict(BASE_EVENT, setup_id="   "))
        self.assertEqual(out["identity_shadow"]["unresolved_reason_code"], "SETUP_ID_MISSING")
        out = self.enricher.enrich(dict(BASE_EVENT))
        self.assertEqual(out["identity_shadow"]["correlation_quality"], "PARTIAL")


class DuplicateShadowDebtTests(unittest.TestCase):
    def test_preexisting_identity_shadow_preserved_never_overwritten(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        forged = {"forged": True, "event_id": "attacker-controlled"}
        event = dict(BASE_EVENT, identity_shadow=forged)
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)
        self.assertIs(out, event)
        self.assertIs(out["identity_shadow"], forged)  # preserve, pas remplace
        self.assertEqual(deep_diff_count(snapshot, out), 0)
        diag = enricher.duplicate_shadow_diagnostics()
        self.assertEqual(diag["reason_code"], "DUPLICATE_IDENTITY_SHADOW")
        self.assertEqual(diag["count"], 1)
        # pas de champ top-level legacy invente
        self.assertEqual(set(out) - set(snapshot), set())

    def test_double_enrich_second_pass_is_noop(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        event = dict(BASE_EVENT)
        first = enricher.enrich(event)
        shadow_first = first["identity_shadow"]
        second = enricher.enrich(first)
        self.assertIs(second["identity_shadow"], shadow_first)  # inchange
        self.assertEqual(enricher.duplicate_shadow_count, 1)


class ResolverIntegrationTests(unittest.TestCase):
    def _make_resolver(self, store):
        """Resolver labo : construit la reference stricte depuis l'evenement ;
        discriminants incomplets -> UNRESOLVED (jamais ticket seul)."""

        def resolver(event):
            ref, reason = try_build_broker_ref(
                account_scope_id=event.get("account_scope_id"),
                ticket=event.get("ticket"),
                broker_symbol=event.get("broker_symbol") or event.get("symbol"),
                opened_at=event.get("opened_at"),
                magic=event.get("magic"),
                position_identifier=event.get("position_identifier"),
                direction=event.get("direction"),
            )
            if ref is None:
                return {"reason": reason}
            record, why = store.resolve_by_broker_position(ref)
            if record is None:
                return {"reason": why}
            return {"lifecycle_id": record["lifecycle_id"],
                    "correlation_id": record["correlation_id"]}

        return resolver

    def setUp(self):
        self.ctx = _Ctx()
        self.corr = make_corr()
        self.rec = self.ctx.store.create_lifecycle(
            correlation_id=self.corr, created_at_utc=T1)
        self.ref = build_ref()
        self.ctx.store.bind_broker_position(self.rec["lifecycle_id"], self.ref)
        self.enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True,
            provenance=RuntimeProvenance(),
            lifecycle_resolver=self._make_resolver(self.ctx.store))

    def _close_event(self, **overrides):
        event = {"event_type": "EXIT_V2_CLOSE", "ticket": 12345,
                 "account_scope_id": ACCT, "broker_symbol": "GOLD#",
                 "opened_at": T1, "magic": 909002,
                 "position_identifier": 8001, "direction": "BUY",
                 "profit_usd": 4.2}
        event.update(overrides)
        return event

    def test_close_event_resolved_by_full_broker_reference(self):
        event = self._close_event()
        snapshot = copy.deepcopy(event)
        out = self.enricher.enrich(event)
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["lifecycle_id"], self.rec["lifecycle_id"])
        self.assertEqual(shadow["lifecycle_resolution"], "RESOLVED")
        self.assertEqual(shadow["correlation_id"], self.corr)  # restaure
        self.assertEqual(shadow["correlation_quality"], "PARTIAL")
        self.assertIsNone(shadow["unresolved_reason_code"])
        self.assertIn("boot_id", shadow)  # provenance compatible
        stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
        self.assertEqual(deep_diff_count(snapshot, stripped), 0)

    def test_insufficient_reference_honest_unresolved(self):
        out = self.enricher.enrich(self._close_event(opened_at=None))
        shadow = out["identity_shadow"]
        self.assertIsNone(shadow["lifecycle_id"])
        self.assertEqual(shadow["lifecycle_resolution"], "UNRESOLVED")
        self.assertEqual(shadow["lifecycle_unresolved_reason"], "OPENED_AT_INVALID")
        # jamais de matching par ticket seul malgre un ticket valide present
        self.assertEqual(shadow["correlation_quality"], "UNRESOLVED")

    def test_unknown_reference_unresolved(self):
        out = self.enricher.enrich(self._close_event(position_identifier=4242))
        shadow = out["identity_shadow"]
        self.assertIsNone(shadow["lifecycle_id"])
        self.assertEqual(shadow["lifecycle_unresolved_reason"], "BROKER_REF_UNKNOWN")

    def test_resolver_error_confined(self):
        def boom(_event):
            raise RuntimeError("secret-interne")

        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, lifecycle_resolver=boom)
        event = dict(BASE_EVENT)
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)  # jamais d'exception vers le caller
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["lifecycle_resolution"], "UNRESOLVED")
        self.assertEqual(shadow["lifecycle_unresolved_reason"], "RESOLVER_ERROR:RuntimeError")
        self.assertNotIn("secret-interne", json.dumps(shadow))
        self.assertIsNotNone(shadow["event_id"])  # enrichissement normal preserve
        stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
        self.assertEqual(deep_diff_count(snapshot, stripped), 0)

    def test_without_resolver_byte_compatible(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        out = enricher.enrich(dict(BASE_EVENT))
        shadow = out["identity_shadow"]
        for key in ("lifecycle_id", "lifecycle_resolution", "lifecycle_unresolved_reason"):
            self.assertNotIn(key, shadow)


if __name__ == "__main__":
    unittest.main()
