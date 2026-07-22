"""M02-P1A — tests laboratoire OFFLINE de l'enricher Identity SHADOW.

Tests NOUVEAUX, additifs, hors production :
- aucun .env, aucun bail, aucun process HERMES, aucun MT5, aucun reseau ;
- configuration laboratoire explicite (constructeurs directs) ;
- aucun fichier production ecrit (enrich = zero I/O, prouve par open() piege).

Ces tests sont supprimables sans trace (rollback M02-P1A) : ils ne modifient
aucune suite existante et n'en affaiblissent aucune.
"""
from __future__ import annotations

import copy
import json
import socket
import sys
import time
import unittest
from unittest import mock

from app.services.event_identity import (
    EventIdentityContext,
    MonotonicUUID7Generator,
    validate_correlation_id,
    validate_event_id,
)
from app.services.event_identity_runtime import (
    _CLOSE_EVENT_TYPES,
    IdentityShadowEnricher,
    LifecycleRegistry,
)

# Evenements synthetiques calques sur le schema reel demo_pilot_events.jsonl.
REALISTIC_EVENTS = [
    {"event_type": "SYSTEM_HEARTBEAT", "boot_id": "be9b30ee-0000-0000-0000-000000000000",
     "cycle_id": 4702, "mode": "READ_ONLY", "mt5_connected": True, "pid": 10940,
     "producer": "hermes_main_loop", "created_at": "2026-07-21T12:58:17+00:00"},
    {"event_type": "DEMO_SKIP", "setup_id": "3f0c1a2e-1111-2222-3333-444455556666",
     "broker_symbol": "GOLD#", "strategy": "GOLD_RANGE_BREAKOUT", "direction": "SELL",
     "status": "BLOCK", "reason": "EES_BAND", "edge_score": 100, "ees_score": 29.5,
     "eligibility_block_reasons": [], "demo_eligible": True, "ticket": None},
    {"event_type": "DEMO_ORDER", "setup_id": "3f0c1a2e-aaaa-bbbb-cccc-ddddeeeeffff",
     "broker_symbol": "GOLD#", "strategy": "GOLD_RANGE_BREAKOUT", "direction": "BUY",
     "ticket": 300001, "entry": 2412.5, "sl": 2405.0, "tp": 2430.0,
     "raw_payload": {"volume": 0.01, "magic_number": 909002}},
    {"event_type": "EXIT_V2_CLOSE", "ticket": 300001, "symbol": "GOLD#",
     "action": "CLOSE", "reason": "TRAIL", "profit_usd": 4.2, "peak_usd": 6.1},
    {"event_type": "POSITION_SYNC", "ticket": 300001, "symbol": "GOLD#",
     "close_reason": "SL", "pnl": -1.9},
    {"event_type": "QUICK_EXIT_DEMO_ONLY_BLOCK", "setup_id": None, "ticket": None,
     "status": "BLOCK", "reason": "ACCOUNT_NOT_DEMO", "mode": "DEMO_QUICK_EXIT",
     "raw_payload": {"demo_only": True, "quick_exit": {"reason": "ACCOUNT_NOT_DEMO"}}},
]

SENSITIVE_LITERALS = ("111000999", "canary-user")


def lab_context(instance="bot-lab-m02p1a-tests"):
    """Configuration laboratoire explicite : aucun env, aucun bail, aucun I/O."""
    return EventIdentityContext(
        server_id="srv-" + "0" * 16,
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )


def deep_diff(a, b, path="$"):
    diffs = []
    if type(a) is not type(b):
        return ["%s TYPE" % path]
    if isinstance(a, dict):
        for k in a.keys() ^ b.keys():
            diffs.append("%s KEY %r" % (path, k))
        for k in a.keys() & b.keys():
            diffs.extend(deep_diff(a[k], b[k], "%s.%s" % (path, k)))
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            return ["%s LEN" % path]
        for i, (x, y) in enumerate(zip(a, b)):
            diffs.extend(deep_diff(x, y, "%s[%d]" % (path, i)))
        return diffs
    if a != b:
        diffs.append("%s VALUE" % path)
    return diffs


class _TrapGenerator(MonotonicUUID7Generator):
    def new(self):
        raise AssertionError("ID genere alors que le mode OFF l'interdit")


class OffNoOpStrictTests(unittest.TestCase):
    def test_off_returns_identical_object_no_id_no_key(self):
        ctx = EventIdentityContext(
            server_id="srv-" + "0" * 16, bot_instance_id="bot-off",
            uuid_generator=_TrapGenerator(),
        )
        enricher = IdentityShadowEnricher(context=ctx, enabled=False)
        for fx in REALISTIC_EVENTS:
            event = copy.deepcopy(fx)
            snapshot = copy.deepcopy(event)
            out = enricher.enrich(event)
            self.assertIs(out, event)
            self.assertEqual(deep_diff(snapshot, out), [])
            self.assertNotIn("identity_shadow", out)


class OnAdditiveStrictTests(unittest.TestCase):
    def setUp(self):
        self.enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)

    def test_single_additive_key_and_legacy_diff_zero(self):
        for fx in REALISTIC_EVENTS:
            event = copy.deepcopy(fx)
            snapshot = copy.deepcopy(event)
            out = self.enricher.enrich(event)
            self.assertIs(out, event)
            self.assertIn("identity_shadow", out)
            stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
            self.assertEqual(deep_diff(snapshot, stripped), [],
                             "mutation legacy detectee sur %s" % fx["event_type"])

    def test_uuid7_validity_uniqueness_monotonicity(self):
        seen = set()
        prev_seq = 0
        prev_id = ""
        for fx in REALISTIC_EVENTS * 50:  # 300 evenements
            out = self.enricher.enrich(copy.deepcopy(fx))
            shadow = out["identity_shadow"]
            eid = shadow["event_id"]
            validate_event_id(eid)
            self.assertNotIn(eid, seen)
            seen.add(eid)
            self.assertGreater(eid, prev_id)  # UUIDv7 k-triable en str
            prev_id = eid
            self.assertGreater(shadow["sequence_number"], prev_seq)
            prev_seq = shadow["sequence_number"]
            if shadow["correlation_id"] is not None:
                validate_correlation_id(shadow["correlation_id"])

    def test_unresolved_events_are_honest(self):
        for fx in REALISTIC_EVENTS:
            out = self.enricher.enrich(copy.deepcopy(fx))
            shadow = out["identity_shadow"]
            et = fx["event_type"]
            sid = fx.get("setup_id")
            setup_id_missing = sid is None or not str(sid).strip()
            if et in _CLOSE_EVENT_TYPES:
                self.assertEqual(shadow["correlation_quality"], "UNRESOLVED")
                self.assertEqual(shadow["unresolved_reason_code"],
                                 "BROKER_MAPPING_NOT_AVAILABLE")
                self.assertIsNone(shadow["correlation_id"])
                self.assertIsNone(shadow["causation_id"])
            elif setup_id_missing:
                self.assertEqual(shadow["correlation_quality"], "UNRESOLVED")
                self.assertEqual(shadow["unresolved_reason_code"], "SETUP_ID_MISSING")
        # honnetete structurelle : jamais durable, jamais bout-en-bout en SHADOW
        out = self.enricher.enrich(copy.deepcopy(REALISTIC_EVENTS[1]))
        shadow = out["identity_shadow"]
        self.assertFalse(shadow["durable_across_restart"])
        self.assertFalse(shadow["end_to_end_complete"])


class _BoomRegistry(LifecycleRegistry):
    def resolve_with_sequence(self, *_a, **_k):
        raise RuntimeError("secret-interne-simule")


class FailureConfinementTests(unittest.TestCase):
    def test_internal_error_confined_and_sanitized(self):
        enricher = IdentityShadowEnricher(
            context=lab_context(), enabled=True, registry=_BoomRegistry())
        event = copy.deepcopy(REALISTIC_EVENTS[1])
        snapshot = copy.deepcopy(event)
        out = enricher.enrich(event)  # ne doit pas lever
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["error"], "ENRICH_FAILED")
        self.assertEqual(shadow["error_type"], "RuntimeError")
        self.assertNotIn("secret-interne-simule", json.dumps(shadow))
        stripped = {k: v for k, v in out.items() if k != "identity_shadow"}
        self.assertEqual(deep_diff(snapshot, stripped), [])


class SensitiveDataAbsentTests(unittest.TestCase):
    def test_shadow_never_contains_sensitive_literals(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        for fx in REALISTIC_EVENTS:
            out = enricher.enrich(copy.deepcopy(fx))
            blob = json.dumps(out["identity_shadow"])
            for lit in SENSITIVE_LITERALS:
                self.assertNotIn(lit, blob)

    def test_context_repr_hides_key(self):
        ctx = EventIdentityContext(
            server_id="srv-" + "0" * 16, bot_instance_id="bot-x",
            account_hmac_key=b"k" * 32, account_key_id="key-1",
        )
        self.assertNotIn("k" * 32, repr(ctx))


class NoIoNoMt5NoNetworkTests(unittest.TestCase):
    def test_enrich_performs_zero_file_io(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        events = [copy.deepcopy(fx) for fx in REALISTIC_EVENTS]
        with mock.patch("builtins.open", side_effect=AssertionError("FILE_IO_FORBIDDEN")):
            for e in events:
                enricher.enrich(e)  # aucun open() => aucun fichier production

    def test_enrich_performs_zero_network(self):
        enricher = IdentityShadowEnricher(context=lab_context(), enabled=True)
        with mock.patch.object(socket, "socket",
                               side_effect=AssertionError("NETWORK_FORBIDDEN")):
            enricher.enrich(copy.deepcopy(REALISTIC_EVENTS[2]))

    def test_identity_modules_never_reference_mt5(self):
        # NB: la session pytest partagee charge MetaTrader5 via conftest/d'autres
        # suites ; l'absence GLOBALE est prouvee par le harness standalone
        # (scratchpad/m02_p1a/harness.py -> mt5_loaded=false). Ici on prouve la
        # propriete qui appartient a CES modules : aucune reference MT5.
        import inspect

        import app.services.event_identity as pure
        import app.services.event_identity_runtime as runtime

        for module in (pure, runtime):
            self.assertNotIn("MetaTrader5", inspect.getsource(module))
            for name, value in vars(module).items():
                self.assertFalse(
                    getattr(value, "__name__", "").startswith("MetaTrader5"),
                    "reference MT5 inattendue: %s" % name,
                )


class PerformanceSanityTests(unittest.TestCase):
    """Seuils volontairement LARGES : garde-fou de degenerescence, pas un SLA."""

    def test_off_and_on_costs_are_sane(self):
        pool = [copy.deepcopy(REALISTIC_EVENTS[i % len(REALISTIC_EVENTS)])
                for i in range(2000)]
        off = IdentityShadowEnricher(context=lab_context(), enabled=False)
        t0 = time.perf_counter()
        for e in pool:
            off.enrich(e)
        off_mean = (time.perf_counter() - t0) / len(pool)
        pool = [copy.deepcopy(REALISTIC_EVENTS[i % len(REALISTIC_EVENTS)])
                for i in range(2000)]
        on = IdentityShadowEnricher(context=lab_context(), enabled=True)
        t0 = time.perf_counter()
        for e in pool:
            on.enrich(e)
        on_mean = (time.perf_counter() - t0) / len(pool)
        self.assertLess(off_mean, 0.001)   # < 1 ms/evenement (mesure ~0.4 us)
        self.assertLess(on_mean, 0.005)    # < 5 ms/evenement (mesure ~90 us)


if __name__ == "__main__":
    unittest.main()
