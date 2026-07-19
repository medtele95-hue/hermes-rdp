"""T1.2B2A-R1 — integration tests for the SHADOW identity enrichment adapter
(app/services/event_identity_runtime.py), with the CORRECTED strict lifecycle
key.

Correlation is PARTIAL by design: the strict key requires the legacy setup_id,
so two independent setups (distinct setup_id) never share a correlation_id, even
inside the TTL window. The TTL is memory cleanup only. Close/exit events with no
reliable setup_id are UNRESOLVED (no join by symbol/strategy/direction/TTL,
never by ticket).

Pure: no MT5, no disk (except the S2->S4 propagation test using a temp events
file), no network, no secret. Values fictitious. The pure module and its unit
tests are untouched.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from app.services.event_identity import EventIdentityContext, MonotonicUUID7Generator
from app.services.event_identity_runtime import (
    CausationCache,
    IdentityShadowEnricher,
    LifecycleRegistry,
    is_identity_enabled,
    maybe_build_identity_enricher,
)


def _ctx(instance="bot-shadow-test") -> EventIdentityContext:
    return EventIdentityContext(
        server_id="srv-" + "0" * 16,
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )


def _enricher(enabled: bool, registry=None) -> IdentityShadowEnricher:
    return IdentityShadowEnricher(context=_ctx(), registry=registry, enabled=enabled)


def _dec(setup_id, symbol="GOLD#", strategy="GOLD_ORDER_FLOW", direction="BUY", etype="DEMO_ORDER_READY"):
    return {"event_type": etype, "broker_symbol": symbol, "symbol": symbol.rstrip("#"),
            "strategy": strategy, "direction": direction, "setup_id": setup_id}


def _corr(enr, ev):
    return enr.enrich(ev)["identity_shadow"]["correlation_id"]


def _legacy_corpus(n: int) -> list[dict]:
    types = ["DEMO_ORDER_READY", "DEMO_SKIP", "DEMO_ORDER", "DEMO_ORDER_FAILED",
             "POSITION_SYNC", "EXIT_V2_CLOSE", "QUICK_EXIT_CLOSE", "RESCUE_CLOSE",
             "WEEKEND_FLAT", "NEWS_PRECLOSE", "PAPER_OPEN", "PAPER_CLOSE"]
    syms = ["GOLD#", "BTCUSD#"]
    strats = ["GOLD_ORDER_FLOW", "BTC_SCALPING"]
    dirs = ["BUY", "SELL"]
    out = []
    for i in range(n):
        out.append({
            "event_type": types[i % len(types)],
            "broker_symbol": syms[i % 2], "symbol": syms[i % 2].rstrip("#"),
            "strategy": strats[i % 2], "direction": dirs[i % 2],
            "setup_id": (str(uuid.uuid4()) if i % 3 else None),
            "trace_id": ("" if i % 4 else str(uuid.uuid4())),
            "reason": None if i % 2 else "MAX_SPREAD",
            "payload": {"nested": {"unicode": "éà中🚀", "n": i}, "list": [1, 2, None]},
            "created_at": "2026-07-19T00:00:%02d+00:00" % (i % 60),
        })
    return out


# --------------------------------------------------------------------------- #
# Defect reproduction: the OLD key merges independent setups; the NEW one doesn't
# --------------------------------------------------------------------------- #
class TestDefectReproduction(unittest.TestCase):
    def test_old_key_would_merge_new_key_separates(self):
        f = lambda: str(uuid.uuid4())
        # OLD flawed key (no setup_id) — two independent setups 7 min apart share
        # the SAME TTL entry -> falsely merged. This reproduces the defect.
        now = [0.0]
        reg_old = LifecycleRegistry(max_entries=100, active_ttl_seconds=900.0,
                                    terminal_grace_seconds=60.0, clock=lambda: now[0])
        old_key = ("GOLD#", "STRATEGY_A", "BUY", None)   # <-- setup_id absent
        a = reg_old.resolve(old_key, f)
        now[0] = 7 * 60.0                                 # +7 min, still < TTL
        b = reg_old.resolve(old_key, f)
        self.assertEqual(a, b, "reproduces the defect: old key merges two setups")

        # NEW enricher: distinct setup_ids -> distinct correlation_ids.
        enr = _enricher(enabled=True)
        ca = _corr(enr, _dec("setup-cycle-A", strategy="STRATEGY_A"))
        cb = _corr(enr, _dec("setup-cycle-B", strategy="STRATEGY_A"))
        self.assertIsNotNone(ca)
        self.assertNotEqual(ca, cb, "fixed: distinct setup_ids never merge")


# --------------------------------------------------------------------------- #
# Adversarial correlation scenarios (Phase 9)
# --------------------------------------------------------------------------- #
class TestAdversarial(unittest.TestCase):
    def test_all_scenarios(self):
        enr = _enricher(enabled=True)
        # 1. same setup + symbol/strategy/direction -> same correlation_id
        c1a = _corr(enr, _dec("S1"))
        c1b = _corr(enr, _dec("S1"))
        self.assertEqual(c1a, c1b)
        # 2/3/4. different setup_id, rest identical, various deltas -> different
        base_c = _corr(enr, _dec("Sx"))
        self.assertNotEqual(base_c, _corr(enr, _dec("Sy")))   # ~1s
        self.assertNotEqual(base_c, _corr(enr, _dec("Sz")))   # ~7min (no time in key)
        self.assertNotEqual(base_c, _corr(enr, _dec("Sw")))   # ~14min
        # 5. same setup, different strategy -> different
        self.assertNotEqual(_corr(enr, _dec("S5", strategy="A")),
                            _corr(enr, _dec("S5", strategy="B")))
        # 6. same setup, opposite direction -> different
        self.assertNotEqual(_corr(enr, _dec("S6", direction="BUY")),
                            _corr(enr, _dec("S6", direction="SELL")))
        # 7. same setup, different symbol -> different
        self.assertNotEqual(_corr(enr, _dec("S7", symbol="GOLD#")),
                            _corr(enr, _dec("S7", symbol="BTCUSD#")))
        # 8/9. setup_id absent / empty -> None
        self.assertIsNone(_corr(enr, _dec(None)))
        self.assertIsNone(_corr(enr, _dec("   ")))
        # 10. position/exit with ticket but no mapping -> None
        pos = enr.enrich({"event_type": "POSITION_SYNC", "broker_symbol": "GOLD#", "ticket": 42})
        self.assertIsNone(pos["identity_shadow"]["correlation_id"])
        self.assertEqual(pos["identity_shadow"]["unresolved_reason_code"], "BROKER_MAPPING_NOT_AVAILABLE")

    def test_ttl_expiry_new_id_but_never_merges_setups(self):
        now = [0.0]
        reg = LifecycleRegistry(max_entries=100, active_ttl_seconds=100.0,
                                terminal_grace_seconds=10.0, clock=lambda: now[0])
        enr = IdentityShadowEnricher(context=_ctx(), registry=reg, enabled=True)
        a = _corr(enr, _dec("S-ttl"))
        now[0] = 101.0                       # TTL expiry -> new id for same key
        self.assertNotEqual(a, _corr(enr, _dec("S-ttl")))

    def test_causation_only_within_same_strict_key(self):
        enr = _enricher(enabled=True)
        s1_first = enr.enrich(_dec("SC1"))["identity_shadow"]
        s1_second = enr.enrich(_dec("SC1"))["identity_shadow"]
        self.assertIsNone(s1_first["causation_id"])
        self.assertEqual(s1_second["causation_id"], s1_first["event_id"])
        # a different setup must NOT inherit causation from the previous one
        other = enr.enrich(_dec("SC2"))["identity_shadow"]
        self.assertIsNone(other["causation_id"])

    def test_never_ticket_or_setup_as_correlation(self):
        enr = _enricher(enabled=True)
        sh = enr.enrich({**_dec("the-setup", etype="DEMO_ORDER"), "ticket": 77})["identity_shadow"]
        self.assertNotEqual(sh["correlation_id"], "the-setup")
        self.assertNotEqual(sh["correlation_id"], "77")
        self.assertEqual(sh["legacy_setup_id"], "the-setup")
        self.assertEqual(uuid.UUID(sh["correlation_id"]).version, 7)

    def test_concurrency_8_threads_same_setup(self):
        reg = LifecycleRegistry(max_entries=10_000, active_ttl_seconds=1e9,
                                terminal_grace_seconds=1e9, clock=time.monotonic)
        enr = IdentityShadowEnricher(context=_ctx(), registry=reg, enabled=True)
        seen = [set() for _ in range(8)]
        errs = []

        def w(k):
            try:
                for i in range(10_000):
                    seen[k].add(_corr(enr, _dec("S%d" % (i % 100))))
            except Exception as exc:  # pragma: no cover
                errs.append(repr(exc))

        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        union = set().union(*seen)
        self.assertEqual(len(union), 100)   # exactly 100 distinct setups -> 100 ids
        self.assertLessEqual(len(reg), 100)


# --------------------------------------------------------------------------- #
# Flag OFF differential (Phase 10)
# --------------------------------------------------------------------------- #
class TestFlagOffDifferential(unittest.TestCase):
    def test_flag_off_strict_noop_20k(self):
        enr = _enricher(enabled=False)
        for ev in _legacy_corpus(20_000):
            before = copy.deepcopy(ev)
            out = enr.enrich(ev)
            self.assertIs(out, ev)
            self.assertEqual(out, before)
            self.assertNotIn("identity_shadow", out)

    def test_flag_off_generates_no_id(self):
        class _Boom(MonotonicUUID7Generator):
            def new(self):
                raise AssertionError("no id when flag OFF")
        ctx = EventIdentityContext(server_id="srv-" + "0" * 16, bot_instance_id="bot-x1",
                                   uuid_generator=_Boom())
        enr = IdentityShadowEnricher(context=ctx, enabled=False)
        self.assertNotIn("identity_shadow", enr.enrich(_dec("S1")))


# --------------------------------------------------------------------------- #
# Shadow behaviour on a large corpus (Phase 11)
# --------------------------------------------------------------------------- #
class TestShadow50k(unittest.TestCase):
    def test_50k_no_false_merge_and_additive_only(self):
        # large, non-expiring registry so stability reflects the KEY, not LRU/TTL
        reg = LifecycleRegistry(max_entries=100_000, active_ttl_seconds=1e9,
                                terminal_grace_seconds=1e9, clock=lambda: 0.0)
        enr = IdentityShadowEnricher(context=_ctx(), registry=reg, enabled=True)
        corr_by_setup: dict[tuple, str] = {}
        for i in range(50_000):
            # rotate setups, symbols, strategies, directions; some unresolved
            if i % 7 == 0:
                ev = {"event_type": "EXIT_V2_CLOSE", "broker_symbol": "GOLD#", "ticket": i}
                out = enr.enrich(ev)
                sh = out["identity_shadow"]
                self.assertIsNone(sh["correlation_id"])
                self.assertEqual(sh["correlation_quality"], "UNRESOLVED")
                self.assertEqual(sh["unresolved_reason_code"], "BROKER_MAPPING_NOT_AVAILABLE")
                continue
            setup = "setup-%d" % (i % 5000)
            sym = "GOLD#" if i % 2 else "BTCUSD#"
            strat = "A" if i % 3 else "B"
            direction = "BUY" if i % 2 else "SELL"
            ev = _dec(setup, symbol=sym, strategy=strat, direction=direction)
            legacy_before = copy.deepcopy(ev)
            out = enr.enrich(ev)
            sh = out["identity_shadow"]
            # legacy keys intact
            for k, v in legacy_before.items():
                self.assertEqual(out[k], v)
            self.assertEqual(sh["correlation_quality"], "PARTIAL")
            self.assertEqual(sh["correlation_scope"], "LEGACY_SETUP_CYCLE")
            self.assertFalse(sh["durable_across_restart"])
            self.assertFalse(sh["end_to_end_complete"])
            self.assertNotEqual(sh["correlation_id"], setup)  # never the setup_id
            # STRICT: the full key (setup+sym+strat+dir) maps to exactly one id
            fullkey = (setup, sym, strat, direction)
            if fullkey in corr_by_setup:
                self.assertEqual(corr_by_setup[fullkey], sh["correlation_id"])
            else:
                # a correlation_id must map back to a single full key (no merge)
                self.assertNotIn(sh["correlation_id"], corr_by_setup.values())
                corr_by_setup[fullkey] = sh["correlation_id"]


# --------------------------------------------------------------------------- #
# Registry mechanics
# --------------------------------------------------------------------------- #
class TestRegistry(unittest.TestCase):
    def test_max_entries_lru_bounded(self):
        reg = LifecycleRegistry(max_entries=100, active_ttl_seconds=1e9,
                                terminal_grace_seconds=1e9, clock=lambda: 0.0)
        f = lambda: str(uuid.uuid4())
        for i in range(1000):
            reg.resolve(("K", str(i)), f)
        self.assertLessEqual(len(reg), 100)
        self.assertGreaterEqual(reg.evictions, 900)

    def test_100k_synthetic_bounded(self):
        reg = LifecycleRegistry(max_entries=2048, active_ttl_seconds=1e9,
                                terminal_grace_seconds=1e9, clock=lambda: 0.0)
        f = lambda: str(uuid.uuid4())
        for i in range(100_000):
            reg.resolve(("S%d" % (i % 50000),), f)
        self.assertLessEqual(len(reg), 2048)

    def test_terminal_grace_then_new(self):
        now = [0.0]
        reg = LifecycleRegistry(max_entries=100, active_ttl_seconds=1e9,
                                terminal_grace_seconds=10.0, clock=lambda: now[0])
        f = lambda: str(uuid.uuid4())
        k = ("K",)
        a = reg.resolve(k, f)
        reg.mark_terminal(k)
        now[0] = 20.0
        self.assertNotEqual(a, reg.resolve(k, f))


class TestCausationCache(unittest.TestCase):
    def test_bounded_lru(self):
        c = CausationCache(max_entries=100)
        for i in range(1000):
            c.set("c%d" % i, "e%d" % i)
        self.assertLessEqual(len(c), 100)
        self.assertIsNone(c.get("c0"))
        self.assertEqual(c.get("c999"), "e999")


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
class TestFailureIsolation(unittest.TestCase):
    def test_uuid_raise_no_propagation_sanitized(self):
        class _Boom(MonotonicUUID7Generator):
            def new(self):
                raise RuntimeError("boom-with-no-secret")
        ctx = EventIdentityContext(server_id="srv-" + "0" * 16, bot_instance_id="bot-x1",
                                   uuid_generator=_Boom())
        enr = IdentityShadowEnricher(context=ctx, enabled=True)
        ev = {**_dec("S"), "sl": 1.0}
        out = enr.enrich(ev)
        self.assertEqual(out["identity_shadow"]["error"], "ENRICH_FAILED")
        self.assertEqual(out["sl"], 1.0)
        self.assertNotIn("boom-with-no-secret", str(out["identity_shadow"]))


# --------------------------------------------------------------------------- #
# Feature flag
# --------------------------------------------------------------------------- #
class TestFeatureFlag(unittest.TestCase):
    def test_parsing(self):
        for on in ("1", "true", "TRUE", "Yes", "on", " on "):
            self.assertTrue(is_identity_enabled(lambda k, v=on: v))
        for off in (None, "", "0", "false", "off", "maybe", "2"):
            self.assertFalse(is_identity_enabled(lambda k, v=off: v))

    def test_factory_off_none(self):
        self.assertIsNone(maybe_build_identity_enricher(getenv=lambda k: None))
        self.assertIsNone(maybe_build_identity_enricher(getenv=lambda k: "0"))

    def test_factory_on(self):
        enr = maybe_build_identity_enricher(getenv=lambda k: "1", bot_instance_id="bot-x1")
        self.assertIsNotNone(enr)
        self.assertTrue(enr.enabled)


# --------------------------------------------------------------------------- #
# Benchmark (loose invariants)
# --------------------------------------------------------------------------- #
class TestBenchmark(unittest.TestCase):
    def test_off_cheaper_on_bounded(self):
        off = _enricher(enabled=False)
        on = _enricher(enabled=True)
        n = 50_000
        t0 = time.perf_counter()
        for _ in range(n):
            off.enrich(_dec("S"))
        off_dt = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(n):
            on.enrich(_dec("S"))
        on_dt = time.perf_counter() - t0
        print("\n  [bench] OFF %.0f ns/ev | ON(same setup) %.0f ns/ev (n=%d)"
              % (off_dt / n * 1e9, on_dt / n * 1e9, n))
        self.assertLess(off_dt, on_dt)
        self.assertLess(on_dt / n, 0.001)


# --------------------------------------------------------------------------- #
# Real S2 -> S4 propagation through the router (Phase 7)
# --------------------------------------------------------------------------- #
class TestS2S4Propagation(unittest.TestCase):
    def test_record_then_ingest_share_enriched_object(self):
        try:
            from app.config import Settings
            from app.mt5.demo_router import DemoKellyRouter
        except Exception as exc:  # pragma: no cover
            self.skipTest("router import unavailable: %r" % exc)
        try:
            tmp = tempfile.TemporaryDirectory()
            r = DemoKellyRouter(Settings(), events_path=Path(tmp.name) / "ev.jsonl")
        except Exception as exc:  # pragma: no cover
            self.skipTest("router construction unavailable: %r" % exc)
        try:
            self.assertTrue(hasattr(r, "attach_identity_enricher"))
            self.assertIsNone(r._identity_enricher)          # no-op default (flag OFF)
            r.attach_identity_enricher(_enricher(enabled=True))
            event = _dec("s1", etype="DEMO_ORDER")

            r._record_event(event)                           # enrich in place + write S2
            self.assertIn("identity_shadow", event)          # caller object mutated
            eid = event["identity_shadow"]["event_id"]
            seq = event["identity_shadow"]["sequence_number"]

            ingest = r._ingest_event(event)                  # S4 payload, same object
            self.assertIs(ingest["data"], event)
            self.assertEqual(ingest["data"]["identity_shadow"]["event_id"], eid)

            # S2 line written to disk carries the same event_id
            line = json.loads(Path(r.events_path).read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(line["identity_shadow"]["event_id"], eid)

            # single enrichment: a fresh event increments sequence exactly by 1
            event2 = _dec("s2", etype="DEMO_ORDER")
            r._record_event(event2)
            self.assertEqual(event2["identity_shadow"]["sequence_number"], seq + 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
