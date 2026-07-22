"""M02-P1D-P1 — tests laboratoire : wiring DORMANT provenance + identite.

Prouve que le forwarding ``provenance``/``lifecycle_resolver`` a travers
``maybe_build_identity_enricher()`` et ``build_system_heartbeat_emitter()``
est correct, que le flag OFF reste un strict no-op (aucune lecture de la
provenance avant la decision d'activation), et que ``app/main.py`` cable
``RuntimeProvenance`` + ``begin_cycle()`` exactement comme specifie par le
wiring_plan P1D-P0 (points a/b/c) — verifie par ANALYSE DE SOURCE (ast),
jamais en important ``app.main`` (le module tire MetaTrader5 a l'import).

Aucun .env, aucun bail, aucun MT5, aucun reseau, aucun fichier production.
"""
from __future__ import annotations

import ast
import statistics
import time
import unittest
import uuid
from pathlib import Path

from app.services.event_identity_runtime import maybe_build_identity_enricher
from app.services.runtime_provenance import RuntimeProvenance
from app.services.system_heartbeat import (
    SystemHeartbeatEmitter,
    build_system_heartbeat_emitter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "app" / "main.py"

TRADE_EVENT = {
    "event_type": "DEMO_SKIP",
    "setup_id": "3f0c1a2e-1111-2222-3333-444455556666",
    "broker_symbol": "GOLD#",
    "strategy": "GOLD_RANGE_BREAKOUT",
    "direction": "SELL",
    "status": "BLOCK",
}

IDENTITY_FLAG = "HERMES_EVENT_IDENTITY_ENABLED"


def _getenv_off(_key):
    return None


def _getenv_on(key):
    return "1" if key == IDENTITY_FLAG else None


class _SettingsStub:
    """Minimal settings surface consumed by build_system_heartbeat_emitter."""

    def __init__(self, **overrides):
        self.read_only = True
        self.demo_trading = False
        self.demo_only = False
        self.hermes_system_heartbeat_enabled = True
        self.system_heartbeat_interval_seconds = 0.001
        for k, v in overrides.items():
            setattr(self, k, v)


class _TrapProvenance:
    """Leve si boot_id/cycle_id/snapshot est lu — prouve que le flag OFF ne
    touche jamais la provenance avant la decision d'activation."""

    @property
    def boot_id(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")

    @property
    def cycle_id(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")

    def snapshot(self):
        raise AssertionError("PROVENANCE_READ_FORBIDDEN_WHEN_OFF")


class FactoryOffUnchangedTests(unittest.TestCase):
    def test_off_returns_none_no_provenance_or_resolver_touched(self):
        result = maybe_build_identity_enricher(
            getenv=_getenv_off,
            provenance=_TrapProvenance(),
            lifecycle_resolver=lambda e: (_ for _ in ()).throw(
                AssertionError("RESOLVER_CALL_FORBIDDEN_WHEN_OFF")
            ),
        )
        self.assertIsNone(result)


class FactoryForwardingTests(unittest.TestCase):
    def test_factory_forwards_provenance_into_enricher(self):
        prov = RuntimeProvenance()
        prov.begin_cycle()
        enricher = maybe_build_identity_enricher(getenv=_getenv_on, provenance=prov)
        self.assertIsNotNone(enricher)
        out = enricher.enrich(dict(TRADE_EVENT))
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["boot_id"], prov.boot_id)
        self.assertEqual(shadow["cycle_id"], prov.cycle_id)
        uuid.UUID(shadow["boot_id"])

    def test_factory_without_provenance_has_no_provenance_fields(self):
        enricher = maybe_build_identity_enricher(getenv=_getenv_on)
        self.assertIsNotNone(enricher)
        out = enricher.enrich(dict(TRADE_EVENT))
        shadow = out["identity_shadow"]
        self.assertNotIn("boot_id", shadow)
        self.assertNotIn("cycle_id", shadow)

    def test_factory_forwards_lifecycle_resolver(self):
        calls = []

        def resolver(event):
            calls.append(event.get("event_type"))
            return {"lifecycle_id": "lc-fake-001", "correlation_id": None}

        enricher = maybe_build_identity_enricher(getenv=_getenv_on, lifecycle_resolver=resolver)
        self.assertIsNotNone(enricher)
        out = enricher.enrich(dict(TRADE_EVENT))
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["lifecycle_id"], "lc-fake-001")
        self.assertEqual(shadow["lifecycle_resolution"], "RESOLVED")
        self.assertEqual(calls, ["DEMO_SKIP"])

    def test_factory_without_resolver_has_no_lifecycle_fields(self):
        enricher = maybe_build_identity_enricher(getenv=_getenv_on)
        out = enricher.enrich(dict(TRADE_EVENT))
        shadow = out["identity_shadow"]
        self.assertNotIn("lifecycle_id", shadow)
        self.assertNotIn("lifecycle_resolution", shadow)


class BuilderForwardingTests(unittest.TestCase):
    def test_builder_forwards_provenance_boot_id(self):
        prov = RuntimeProvenance()
        sink = []
        emitter = build_system_heartbeat_emitter(_SettingsStub(), sink.append, provenance=prov)
        self.assertEqual(emitter.boot_id, prov.boot_id)
        self.assertTrue(emitter.enabled)

    def test_builder_without_provenance_keeps_historical_behavior(self):
        sink = []
        emitter = build_system_heartbeat_emitter(_SettingsStub(), sink.append)
        uuid.UUID(emitter.boot_id)  # own clean UUID, historical behavior
        self.assertTrue(emitter.enabled)

    def test_builder_invalid_provenance_fails_safe_disabled_no_exception(self):
        class _BadProvenance:
            boot_id = "pas-un-uuid"

        sink = []
        emitter = build_system_heartbeat_emitter(
            _SettingsStub(), sink.append, provenance=_BadProvenance()
        )
        self.assertFalse(emitter.enabled)
        uuid.UUID(emitter.boot_id)  # last-resort clean UUID, never raises


class OracleHeartbeatDifferentialTests(unittest.TestCase):
    """Coeur de la preuve DORMANT : le cablage ne change RIEN a la sequence
    observable de cycle_id ni au schema d'evenement, cycle par cycle."""

    def test_baseline_vs_wired_identical_cycle_sequence(self):
        n_cycles = 10
        expected_keys = {
            "event_type", "created_at", "pid", "boot_id", "cycle_id",
            "mode", "producer", "mt5_connected",
        }

        # BASELINE : sans provenance, compteur interne (comportement historique).
        baseline_sink = []
        clock_b = {"t": 0.0}

        def mono_b():
            clock_b["t"] += 1.0
            return clock_b["t"]

        baseline = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=0.001, record_event=baseline_sink.append,
            mode="READ_ONLY", now_monotonic=mono_b,
        )

        # CABLE : provenance partagee + begin_cycle() par cycle (contrat M02-P1D).
        prov = RuntimeProvenance()
        wired_sink = []
        clock_w = {"t": 0.0}

        def mono_w():
            clock_w["t"] += 1.0
            return clock_w["t"]

        wired = SystemHeartbeatEmitter(
            enabled=True, interval_seconds=0.001, record_event=wired_sink.append,
            mode="READ_ONLY", now_monotonic=mono_w, provenance=prov,
        )

        for _ in range(n_cycles):
            baseline.on_cycle_complete(mt5_connected=True)
            prov.begin_cycle()  # proprietaire de la boucle : 1 begin_cycle/cycle
            wired.on_cycle_complete(mt5_connected=True)

        self.assertEqual(len(baseline_sink), n_cycles)
        self.assertEqual(len(wired_sink), n_cycles)
        base_cycles = [e["cycle_id"] for e in baseline_sink]
        wired_cycles = [e["cycle_id"] for e in wired_sink]
        self.assertEqual(base_cycles, list(range(1, n_cycles + 1)))
        self.assertEqual(wired_cycles, base_cycles)
        for event in baseline_sink + wired_sink:
            self.assertEqual(set(event), expected_keys)
        uuid.UUID(baseline.boot_id)
        uuid.UUID(wired.boot_id)
        self.assertEqual(wired.boot_id, prov.boot_id)
        self.assertNotEqual(wired.boot_id, baseline.boot_id)  # 2 process distincts


class MainPySourceWiringTests(unittest.TestCase):
    """Analyse STATIQUE de app/main.py — jamais d'import (le module tire
    MetaTrader5 a l'import, interdit dans ce labo)."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(MAIN_PY))

    def test_runtime_provenance_instantiated_exactly_once(self):
        count = sum(
            1
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RuntimeProvenance"
        )
        self.assertEqual(count, 1)

    def _find_run_cycle(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_cycle":
                return node
        self.fail("run_cycle introuvable dans app/main.py")

    def test_begin_cycle_is_first_statement_of_run_cycle_body(self):
        fn = self._find_run_cycle()
        body = list(fn.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]  # skip a docstring, if any (robustness only)
        self.assertTrue(body, "run_cycle a un corps vide")
        first = body[0]
        self.assertIsInstance(first, ast.Expr)
        call = first.value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "begin_cycle")
        self.assertIsInstance(call.func.value, ast.Attribute)
        self.assertEqual(call.func.value.attr, "runtime_provenance")
        self.assertIsInstance(call.func.value.value, ast.Name)
        self.assertEqual(call.func.value.value.id, "self")

    def test_provenance_forwarded_at_both_call_sites(self):
        self.assertEqual(self.source.count("provenance=self.runtime_provenance"), 2)
        self.assertIn(
            "maybe_build_identity_enricher(provenance=self.runtime_provenance)", self.source
        )
        idx = self.source.index("build_system_heartbeat_emitter(")
        snippet = self.source[idx: idx + 300]
        self.assertIn("provenance=self.runtime_provenance", snippet)

    def test_runtime_provenance_import_present(self):
        self.assertIn(
            "from app.services.runtime_provenance import RuntimeProvenance", self.source
        )


class BeginCycleBenchTests(unittest.TestCase):
    def test_begin_cycle_median_under_100_microseconds(self):
        prov = RuntimeProvenance()
        n = 5000
        samples = []
        for _ in range(n):
            t0 = time.perf_counter()
            prov.begin_cycle()
            samples.append(time.perf_counter() - t0)
        median = statistics.median(samples)
        self.assertLess(median, 100e-6, "begin_cycle median=%.2f us" % (median * 1e6))


if __name__ == "__main__":
    unittest.main()
