"""M02-P1B — tests laboratoire : provenance canonique boot_id/cycle_id.

Prouve que SYSTEM_HEARTBEAT et l'enricher Identity SHADOW consomment EXACTEMENT
la meme paire (boot_id, cycle_id) via un provider unique injecte, sans changer
d'un octet les comportements historiques quand aucun provider n'est injecte.

Aucun .env, aucun bail, aucun MT5, aucun reseau, aucun fichier production.
"""
from __future__ import annotations

import copy
import inspect
import json
import threading
import unittest
import uuid

from app.services.event_identity import (
    EventIdentityContext,
    MonotonicUUID7Generator,
)
from app.services.event_identity_runtime import IdentityShadowEnricher
from app.services.runtime_provenance import (
    RuntimeProvenance,
    RuntimeProvenanceError,
)
from app.services.system_heartbeat import SystemHeartbeatEmitter

SENSITIVE_LITERALS = ("111000999", "canary-user")


def lab_context(instance="bot-lab-m02p1b"):
    return EventIdentityContext(
        server_id="srv-" + "0" * 16,
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )


def make_heartbeat(sink, provenance=None, interval=0.001):
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 1.0  # chaque cycle depasse l'intervalle => emission sure
        return clock["t"]

    return SystemHeartbeatEmitter(
        enabled=True, interval_seconds=interval,
        record_event=sink.append, mode="READ_ONLY", now_monotonic=mono,
        provenance=provenance,
    )


TRADE_EVENT = {
    "event_type": "DEMO_SKIP", "setup_id": "3f0c1a2e-1111-2222-3333-444455556666",
    "broker_symbol": "GOLD#", "strategy": "GOLD_RANGE_BREAKOUT",
    "direction": "SELL", "status": "BLOCK",
}


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


class ProviderContractTests(unittest.TestCase):
    def test_boot_id_valid_uuid_generated_once(self):
        p = RuntimeProvenance()
        uuid.UUID(p.boot_id)
        self.assertEqual(p.boot_id, p.boot_id)  # stable
        self.assertEqual(p.cycle_id, 0)

    def test_injected_boot_id_validated_and_normalized(self):
        raw = "BE9B30EE-D640-4C22-9D6B-8F75EABBA373"
        p = RuntimeProvenance(boot_id=raw)
        self.assertEqual(p.boot_id, raw.lower())
        with self.assertRaises(RuntimeProvenanceError):
            RuntimeProvenance(boot_id="pas-un-uuid")

    def test_boot_id_unique_across_simulated_processes(self):
        boots = {RuntimeProvenance().boot_id for _ in range(200)}
        self.assertEqual(len(boots), 200)

    def test_cycle_monotone_never_decrements_no_timestamp(self):
        p = RuntimeProvenance()
        seen = [p.begin_cycle() for _ in range(1000)]
        self.assertEqual(seen, list(range(1, 1001)))
        self.assertFalse(hasattr(p, "_clock"))  # ordinal logique, pas d'horloge

    def test_thread_safety_no_duplicate_no_gap(self):
        p = RuntimeProvenance()
        results = []
        lock = threading.Lock()

        def worker():
            local = [p.begin_cycle() for _ in range(500)]
            with lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), list(range(1, 4001)))
        self.assertEqual(p.cycle_id, 4000)

    def test_snapshot_consistent_pair(self):
        p = RuntimeProvenance()
        p.begin_cycle()
        boot, cyc = p.snapshot()
        self.assertEqual((boot, cyc), (p.boot_id, 1))

    def test_module_is_pure(self):
        import types

        import app.services.runtime_provenance as mod

        src = inspect.getsource(mod)
        for forbidden in ("MetaTrader5", "os.environ", "getenv", "open(",
                          "socket", "subprocess"):
            self.assertNotIn(forbidden, src)
        # Imports reels du module : uuid + threading uniquement (pas d'horloge,
        # pas d'os, pas d'I/O). Verification par namespace, pas par sous-chaine.
        imported = {name for name, value in vars(mod).items()
                    if isinstance(value, types.ModuleType)}
        self.assertEqual(imported, {"uuid", "threading"})


class SharedProvenanceTests(unittest.TestCase):
    """Coeur M02-P1B : heartbeat et enricher partagent la meme paire."""

    def _run_cycles(self, n_cycles, events_per_cycle=3):
        prov = RuntimeProvenance()
        sink = []
        hb = make_heartbeat(sink, provenance=prov)
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, provenance=prov)
        matrix = []
        for _ in range(n_cycles):
            cycle = prov.begin_cycle()
            shadows = []
            for _ in range(events_per_cycle):
                out = enricher.enrich(copy.deepcopy(TRADE_EVENT))
                shadows.append(out["identity_shadow"])
            hb_event = hb.on_cycle_complete(mt5_connected=True)
            matrix.append((cycle, hb_event, shadows))
        return prov, matrix

    def test_same_boot_and_cycle_for_heartbeat_and_shadow(self):
        prov, matrix = self._run_cycles(5)
        for cycle, hb_event, shadows in matrix:
            self.assertIsNotNone(hb_event)
            self.assertEqual(hb_event["boot_id"], prov.boot_id)
            self.assertEqual(hb_event["cycle_id"], cycle)
            for shadow in shadows:
                self.assertEqual(shadow["boot_id"], hb_event["boot_id"])
                self.assertEqual(shadow["cycle_id"], hb_event["cycle_id"])

    def test_events_of_same_cycle_share_cycle_id_next_cycle_increments(self):
        _, matrix = self._run_cycles(4, events_per_cycle=5)
        cycle_ids = []
        for cycle, _hb, shadows in matrix:
            self.assertEqual({s["cycle_id"] for s in shadows}, {cycle})
            cycle_ids.append(cycle)
        self.assertEqual(cycle_ids, [1, 2, 3, 4])

    def test_simulated_restart_new_boot_cycle_restarts(self):
        prov1, matrix1 = self._run_cycles(3)
        prov2, matrix2 = self._run_cycles(2)  # nouveau provider = restart
        self.assertNotEqual(prov1.boot_id, prov2.boot_id)
        self.assertEqual(matrix1[-1][0], 3)
        self.assertEqual(matrix2[0][0], 1)  # repart selon le contrat documente
        self.assertEqual(matrix2[0][1]["cycle_id"], 1)

    def test_legacy_diff_zero_with_provenance(self):
        prov = RuntimeProvenance()
        prov.begin_cycle()
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, provenance=prov)
        event = copy.deepcopy(TRADE_EVENT)
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)
        stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
        self.assertEqual(deep_diff_count(snapshot, stripped), 0)

    def test_no_sensitive_data_in_provenance_fields(self):
        prov = RuntimeProvenance()
        prov.begin_cycle()
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, provenance=prov)
        out = enricher.enrich(copy.deepcopy(TRADE_EVENT))
        blob = json.dumps(out["identity_shadow"])
        for lit in SENSITIVE_LITERALS:
            self.assertNotIn(lit, blob)


class _TrapProvenance:
    """Leve si boot_id/cycle_id/snapshot est lu — prouve zero lecture en OFF."""

    @property
    def boot_id(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")

    @property
    def cycle_id(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")

    def snapshot(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")


class _BoomProvenance:
    boot_id = str(uuid.uuid4())
    cycle_id = 1

    def snapshot(self):
        raise RuntimeError("panne-provenance-simulee")


class OffAndFailureTests(unittest.TestCase):
    def test_identity_off_stays_strict_noop_no_provenance_read(self):
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=False, provenance=_TrapProvenance())
        event = copy.deepcopy(TRADE_EVENT)
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)  # le piege ne doit PAS se declencher
        self.assertIs(out, event)
        self.assertEqual(deep_diff_count(snapshot, out), 0)
        self.assertNotIn("identity_shadow", out)

    def test_enricher_provenance_error_confined_sanitized(self):
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, provenance=_BoomProvenance())
        event = copy.deepcopy(TRADE_EVENT)
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)  # ne leve jamais vers le caller
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["error"], "ENRICH_FAILED")
        self.assertEqual(shadow["error_type"], "RuntimeError")
        self.assertNotIn("panne-provenance", json.dumps(shadow))
        stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
        self.assertEqual(deep_diff_count(snapshot, stripped), 0)

    def test_heartbeat_provenance_error_confined(self):
        sink = []
        hb = make_heartbeat(sink, provenance=_BoomProvenance())
        result = hb.on_cycle_complete(mt5_connected=True)  # ne leve jamais
        self.assertIsNone(result)
        self.assertEqual(hb.error_count, 1)
        self.assertEqual(sink, [])

    def test_heartbeat_rejects_invalid_provenance_boot_id(self):
        class _Bad:
            boot_id = "pas-un-uuid"

        with self.assertRaises(ValueError):
            make_heartbeat([], provenance=_Bad())


class HistoricalBehaviorUnchangedTests(unittest.TestCase):
    """Sans provider injecte, le comportement pre-P1B est intact."""

    def test_heartbeat_without_provider_owns_boot_and_counter(self):
        sink = []
        hb = make_heartbeat(sink, provenance=None)
        uuid.UUID(hb.boot_id)
        for _ in range(7):
            hb.on_cycle_complete(mt5_connected=True)
        self.assertEqual(hb.cycle_id, 7)
        self.assertEqual(sink[-1]["cycle_id"], 7)
        self.assertEqual(
            set(sink[-1]),
            {"event_type", "created_at", "pid", "boot_id", "cycle_id",
             "mode", "producer", "mt5_connected"},
        )

    def test_enricher_without_provider_has_no_provenance_fields(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        out = enricher.enrich(copy.deepcopy(TRADE_EVENT))
        shadow = out["identity_shadow"]
        self.assertNotIn("boot_id", shadow)
        self.assertNotIn("cycle_id", shadow)

    def test_clock_regression_is_irrelevant_by_construction(self):
        # Le cycle_id est un ordinal logique : aucune horloge n'entre dans le
        # provider ; le throttle heartbeat reste sur horloge monotone.
        prov = RuntimeProvenance()
        before = [prov.begin_cycle() for _ in range(3)]
        # (une horloge murale qui recule n'a aucun point d'entree ici)
        after = [prov.begin_cycle() for _ in range(3)]
        self.assertEqual(before + after, [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
