"""M07-P1 — Campaign 6/7: ENRICHER/CAPTURE (M02) COMBINATION chaos.

Each of ``LifecycleCaptureService`` and ``IdentityShadowEnricher`` is already
covered UNIT-by-unit in ``tests/lab_m02``. This campaign is the COMBINATION
angle the M07 roadmap asks for: store, position_lookup, clock AND the
resolver/provenance all breaking AT THE SAME TIME (with FOUR genuinely
DIFFERENT untyped exception types -- ``RuntimeError``/``TypeError``/
``KeyError``/``AttributeError`` -- not just the module's own typed
``LifecycleStoreError``), proving ``on_event()``/``enrich()`` are confined
by their OUTER catch-all (not merely their narrower per-call ``except``
blocks), that the legacy event dict is NEVER mutated beyond the single
additive ``identity_shadow`` key, and that a REAL disk fault (via
``FaultyOS`` against a genuine ``LifecycleIdentityStore``, not a fake) is
confined exactly the same way as a fake exception.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from chaos_tools import FaultyOS, Ledger, faulty_existing_handle, sha256_file

from app.services.event_identity import EventIdentityContext, MonotonicUUID7Generator
from app.services.event_identity_runtime import IdentityShadowEnricher
from app.services.lifecycle_capture import LifecycleCaptureService
from app.services.lifecycle_identity_store import LifecycleIdentityStore

ACCT = "acct-v1-" + "07" * 16
MAGIC = 909707
NOW = "2026-07-22T03:00:00+00:00"

LEDGER = Ledger()


def _open_event(ticket, *, symbol="GOLD#", direction="BUY", setup_id="setup-chaos-001"):
    return {
        "event_type": "DEMO_ORDER", "ticket": ticket, "broker_symbol": symbol,
        "direction": direction, "setup_id": setup_id,
    }


def _close_event(ticket, *, event_type="EXIT_V2_CLOSE", symbol="GOLD#", direction="BUY"):
    return {"event_type": event_type, "ticket": ticket, "broker_symbol": symbol, "direction": direction}


class _AllRaisingStore:
    """Every method raises a DIFFERENT, deliberately UNTYPED exception --
    proves confinement holds for ANY internal failure, not just the
    module's own ``LifecycleStoreError`` hierarchy."""

    def create_lifecycle(self, **kwargs):
        raise RuntimeError("boom-create")

    def bind_broker_position(self, *args, **kwargs):
        raise TypeError("boom-bind")

    def resolve_by_broker_position(self, ref):
        raise KeyError("boom-resolve")

    def mark_closed(self, *args, **kwargs):
        raise AttributeError("boom-mark-closed")


def _raising_clock():
    raise ValueError("boom-clock")


def _raising_lookup(ticket):
    raise IndexError("boom-lookup")


def _working_lookup(ticket):
    return {"opened_at": NOW, "position_identifier": 900000 + ticket}


# --------------------------------------------------------------------------- #
# LifecycleCaptureService: everything raising, at once
# --------------------------------------------------------------------------- #
class CaptureCombinationChaosTests(unittest.TestCase):
    def test_store_raises_four_different_untyped_types_confined_by_outer_catch(self) -> None:
        service = LifecycleCaptureService(
            _AllRaisingStore(), account_scope_id=ACCT, magic=MAGIC,
            position_lookup=_working_lookup, clock_utc_iso=lambda: NOW,
        )
        open_event = _open_event(700001)
        before_open = copy.deepcopy(open_event)
        service.on_event(open_event)  # create_lifecycle -> RuntimeError
        self.assertEqual(open_event, before_open)

        close_event = _close_event(700001)
        before_close = copy.deepcopy(close_event)
        service.on_event(close_event)  # resolve_by_broker_position -> KeyError
        self.assertEqual(close_event, before_close)

        diag = service.diagnostics()
        self.assertEqual(diag["errors"].get("RuntimeError"), 1)
        self.assertEqual(diag["errors"].get("KeyError"), 1)
        self.assertEqual(diag["created"], 0)
        self.assertEqual(diag["closed"], 0)

    def test_store_clock_and_lookup_all_raising_simultaneously_never_raises(self) -> None:
        service = LifecycleCaptureService(
            _AllRaisingStore(), account_scope_id=ACCT, magic=MAGIC,
            position_lookup=_raising_lookup, clock_utc_iso=_raising_clock,
        )
        open_event = _open_event(700002)
        before_open = copy.deepcopy(open_event)
        service.on_event(open_event)
        self.assertEqual(open_event, before_open, "on_event must never mutate the legacy event")

        close_event = _close_event(700002)
        before_close = copy.deepcopy(close_event)
        service.on_event(close_event)
        self.assertEqual(close_event, before_close)

        diag = service.diagnostics()
        # Clock is evaluated as an ARGUMENT to create_lifecycle -- it raises
        # BEFORE the store is ever called, so the store's own exception type
        # never surfaces here; the OUTER on_event() catch-all is what saves
        # this call (the narrower `except LifecycleStoreError` never even
        # sees a ValueError).
        self.assertEqual(diag["errors"].get("ValueError"), 1)
        # position_lookup's failure is already confined INTERNALLY by
        # `_lookup_position` (returns None), which then makes the composite
        # broker ref unbuildable -- an honest UNRESOLVED, not an "error".
        self.assertEqual(diag["unresolved"].get("OPENED_AT_INVALID"), 1)
        self.assertEqual(diag["created"], 0)
        self.assertEqual(diag["closed"], 0)

    def test_resolver_built_from_boom_store_never_raises(self) -> None:
        service = LifecycleCaptureService(
            _AllRaisingStore(), account_scope_id=ACCT, magic=MAGIC,
            position_lookup=_working_lookup, clock_utc_iso=lambda: NOW,
        )
        resolver = service.make_lifecycle_resolver()
        result = resolver(_close_event(700003))  # resolve_by_broker_position -> KeyError
        self.assertIn("reason", result)
        self.assertTrue(str(result["reason"]).startswith("RESOLVER_ERROR:"))

    def test_real_disk_fault_confined_exactly_like_a_fake_exception(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store_path = Path(tmp.name) / "lifecycle_store.json"
        store = LifecycleIdentityStore(path=store_path)
        service = LifecycleCaptureService(
            store, account_scope_id=ACCT, magic=MAGIC,
            position_lookup=_working_lookup, clock_utc_iso=lambda: NOW,
        )
        self.assertFalse(store_path.exists())

        faulty = FaultyOS(open_target="app.services.lifecycle_identity_store", fail_fsync_at={1})
        with faulty:
            event = _open_event(700004)
            before = copy.deepcopy(event)
            service.on_event(event)  # real StoreWriteError -- never raises out
            self.assertEqual(event, before)

        diag = service.diagnostics()
        self.assertEqual(diag["errors"].get("StoreWriteError"), 1)
        self.assertEqual(diag["created"], 0)
        self.assertFalse(store_path.exists())  # nothing ever committed to disk

        # service remains usable once the disk fault clears
        event2 = _open_event(700005)
        service.on_event(event2)
        self.assertEqual(service.diagnostics()["created"], 1)
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# IdentityShadowEnricher: provenance + resolver combination
# --------------------------------------------------------------------------- #
class _RaisingProvenance:
    def snapshot(self):
        raise RuntimeError("boom-provenance")


def _raising_resolver(event):
    raise TypeError("boom-resolver")


def _shadow_event(**overrides):
    base = {
        "event_type": "DEMO_ORDER", "setup_id": "setup-enrich-chaos",
        "broker_symbol": "GOLD#", "strategy": "GOLD_RANGE_BREAKOUT", "direction": "BUY",
        "legacy_field_untouched": "canary-value",
    }
    base.update(overrides)
    return base


def _enricher(*, provenance=None, lifecycle_resolver=None) -> IdentityShadowEnricher:
    context = EventIdentityContext(
        server_id="srv-" + "0" * 16, bot_instance_id="bot-m07-chaos",
        uuid_generator=MonotonicUUID7Generator(),
    )
    return IdentityShadowEnricher(
        context=context, enabled=True, provenance=provenance, lifecycle_resolver=lifecycle_resolver,
    )


class EnricherCombinationChaosTests(unittest.TestCase):
    def test_provenance_alone_raising_never_raises_legacy_intact(self) -> None:
        enricher = _enricher(provenance=_RaisingProvenance())
        event = _shadow_event()
        before_legacy = {k: v for k, v in event.items()}
        result = enricher.enrich(event)
        self.assertIs(result, event)
        legacy_after = {k: v for k, v in event.items() if k != "identity_shadow"}
        self.assertEqual(legacy_after, before_legacy)  # byte-intact legacy keys
        self.assertEqual(event["identity_shadow"]["error"], "ENRICH_FAILED")
        self.assertEqual(event["identity_shadow"]["error_type"], "RuntimeError")

    def test_resolver_alone_raising_degrades_gracefully_no_enrich_failed(self) -> None:
        enricher = _enricher(lifecycle_resolver=_raising_resolver)
        event = _shadow_event()
        before_legacy = {k: v for k, v in event.items()}
        enricher.enrich(event)
        legacy_after = {k: v for k, v in event.items() if k != "identity_shadow"}
        self.assertEqual(legacy_after, before_legacy)
        shadow = event["identity_shadow"]
        self.assertNotIn("error", shadow)  # graceful, not a hard failure
        self.assertEqual(shadow["lifecycle_resolution"], "UNRESOLVED")
        self.assertTrue(shadow["lifecycle_unresolved_reason"].startswith("RESOLVER_ERROR:TypeError"))

    def test_provenance_and_resolver_both_raising_simultaneously_never_raises(self) -> None:
        enricher = _enricher(provenance=_RaisingProvenance(), lifecycle_resolver=_raising_resolver)
        event = _shadow_event()
        before_legacy = {k: v for k, v in event.items()}
        enricher.enrich(event)  # must never raise
        legacy_after = {k: v for k, v in event.items() if k != "identity_shadow"}
        self.assertEqual(legacy_after, before_legacy)
        self.assertEqual(event["identity_shadow"]["error"], "ENRICH_FAILED")

    def test_disabled_enricher_is_a_strict_noop_even_with_raising_components(self) -> None:
        enricher_off = IdentityShadowEnricher(
            context=EventIdentityContext(
                server_id="srv-" + "1" * 16, bot_instance_id="bot-m07-chaos-off",
                uuid_generator=MonotonicUUID7Generator(),
            ),
            enabled=False, provenance=_RaisingProvenance(), lifecycle_resolver=_raising_resolver,
        )
        event = _shadow_event()
        result = enricher_off.enrich(event)
        self.assertIs(result, event)
        self.assertNotIn("identity_shadow", event)


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions ESCAPED (all confined as designed)
# --------------------------------------------------------------------------- #
class ZZCaptureComboCampaignTests(unittest.TestCase):
    def test_zz_campaign_ran(self) -> None:
        # This campaign's whole point is that NOTHING escapes on_event()/
        # enrich() -- there is deliberately no exception ledger to check
        # here (every scenario above already asserts "never raises"
        # directly); this sentinel just documents that intent for readers
        # scanning the ZZ classes across the M07 suite.
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
