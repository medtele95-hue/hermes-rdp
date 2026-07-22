"""M02-P1D-P2 — tests laboratoire : capture DORMANTE des discriminants broker.

Prouve (1) que le hook confine dans demo_router._record_event est DORMANT par
construction (analyse de source AST — jamais d'import de app.mt5.demo_router,
le module tire MetaTrader5 a l'import et instancier DemoKellyRouter est lourd),
(2) que LifecycleCaptureService route correctement DEMO_ORDER/close vers le
store durable sans jamais resoudre par ticket seul et sans jamais lever vers
l'appelant, (3) que les deux fichiers touches compilent.

Aucun .env, aucun MT5, aucun reseau : le store passe par
tempfile.TemporaryDirectory. Toutes les valeurs (tickets, symboles, comptes)
sont 100% fictives.
"""
from __future__ import annotations

import ast
import json
import py_compile
import tempfile
import unittest
from pathlib import Path

from app.services.event_identity import EventIdentityContext
from app.services.event_identity_runtime import IdentityShadowEnricher
from app.services.lifecycle_capture import LifecycleCaptureService
from app.services.lifecycle_identity_store import LifecycleIdentityStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROUTER_PY = REPO_ROOT / "app" / "mt5" / "demo_router.py"
LIFECYCLE_CAPTURE_PY = REPO_ROOT / "app" / "services" / "lifecycle_capture.py"

ACCT = "acct-v1-" + "cd" * 16
MAGIC = 909111
T1 = "2026-07-21T10:00:00+00:00"
T2 = "2026-07-21T14:30:00+00:00"
NOW = "2026-07-21T15:00:00+00:00"

# Canary literals — must never leak into diagnostics().
CANARY_TICKET = 111000999
CANARY_SYMBOL = "canary-user"


def _clock(value: str = NOW):
    return lambda: value


class _FakePositionLookup:
    """Test double for the future MT5-backed position_lookup: a plain dict
    keyed by ticket, mutated by the test to simulate ticket recycling."""

    def __init__(self):
        self._by_ticket: dict = {}

    def set(self, ticket: int, *, opened_at: str, position_identifier: int):
        self._by_ticket[ticket] = {
            "opened_at": opened_at,
            "position_identifier": position_identifier,
        }

    def clear(self, ticket: int):
        self._by_ticket.pop(ticket, None)

    def __call__(self, ticket: int):
        return self._by_ticket.get(ticket)


class _BoomStore:
    """Fake store whose every method raises a PLAIN exception (not a
    LifecycleStoreError subclass) — proves on_event confines ANY internal
    failure, not just the store's own typed errors."""

    def create_lifecycle(self, **kwargs):
        raise RuntimeError("boom-create")

    def bind_broker_position(self, *args, **kwargs):
        raise RuntimeError("boom-bind")

    def resolve_by_broker_position(self, ref):
        raise RuntimeError("boom-resolve")

    def mark_closed(self, *args, **kwargs):
        raise RuntimeError("boom-mark-closed")


def _make_store(tmp_path: Path) -> LifecycleIdentityStore:
    return LifecycleIdentityStore(path=tmp_path / "lifecycle_store.json")


class _ServiceCtx:
    """Store + service + fake lookup, all on a temp dir, one per test."""

    def __init__(self, **service_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self.tmp.name))
        self.lookup = _FakePositionLookup()
        kwargs = dict(
            account_scope_id=ACCT,
            magic=MAGIC,
            position_lookup=self.lookup,
            clock_utc_iso=_clock(),
        )
        kwargs.update(service_overrides)
        self.service = LifecycleCaptureService(self.store, **kwargs)


def _open_event(ticket, *, symbol="GOLD#", direction="BUY", setup_id="setup-fixed-001"):
    return {
        "event_type": "DEMO_ORDER",
        "ticket": ticket,
        "broker_symbol": symbol,
        "direction": direction,
        "setup_id": setup_id,
    }


def _close_event(ticket, *, event_type="EXIT_V2_CLOSE", symbol="GOLD#", direction="BUY"):
    return {
        "event_type": event_type,
        "ticket": ticket,
        "broker_symbol": symbol,
        "direction": direction,
    }


# --------------------------------------------------------------------------- #
# (1) DORMANCY — source analysis of demo_router.py, no import
# --------------------------------------------------------------------------- #
class DemoRouterDormancySourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = DEMO_ROUTER_PY.read_text(encoding="utf-8-sig")
        cls.tree = ast.parse(cls.source, filename=str(DEMO_ROUTER_PY))
        cls.router_class = next(
            node
            for node in ast.walk(cls.tree)
            if isinstance(node, ast.ClassDef) and node.name == "DemoKellyRouter"
        )

    def _find_method(self, name: str) -> ast.FunctionDef:
        for node in self.router_class.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("%s introuvable dans DemoKellyRouter" % name)

    def test_init_sets_lifecycle_capture_none_by_default(self):
        init = self._find_method("__init__")
        found = False
        for node in ast.walk(init):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "_lifecycle_capture"
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(node.value, ast.Constant)
                and node.value.value is None
            ):
                found = True
                break
        self.assertTrue(found, "self._lifecycle_capture = None introuvable dans __init__")

    def test_attach_lifecycle_capture_method_exists_and_sets_attribute(self):
        method = self._find_method("attach_lifecycle_capture")
        args = [a.arg for a in method.args.args]
        self.assertEqual(args, ["self", "capture"])
        found = False
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign):
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "_lifecycle_capture"
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(node.value, ast.Name)
                and node.value.id == "capture"
            ):
                found = True
                break
        self.assertTrue(found, "attach_lifecycle_capture ne fait pas self._lifecycle_capture = capture")

    def test_record_event_hook_is_guarded_none_and_wrapped_try_except(self):
        method = self._find_method("_record_event")
        guard = None
        for node in method.body:
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Attribute)
                and node.test.left.attr == "_lifecycle_capture"
                and isinstance(node.test.left.value, ast.Name)
                and node.test.left.value.id == "self"
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.IsNot)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value is None
            ):
                guard = node
                break
        self.assertIsNotNone(guard, "if self._lifecycle_capture is not None: introuvable")
        # The guard body must be exactly a try/except that calls on_event and
        # never re-raises (bare `except Exception: pass`-shaped).
        self.assertEqual(len(guard.body), 1)
        try_node = guard.body[0]
        self.assertIsInstance(try_node, ast.Try)
        self.assertEqual(len(try_node.handlers), 1)
        handler = try_node.handlers[0]
        self.assertIsInstance(handler.type, ast.Name)
        self.assertEqual(handler.type.id, "Exception")
        for stmt in handler.body:
            self.assertNotIsInstance(stmt, ast.Raise, "le handler ne doit jamais re-lever")
        # The try body must call `self._lifecycle_capture.on_event(...)` and
        # must NOT reassign `event` (the identity enricher path does reassign
        # `event`; the capture hook must never alter it).
        call_found = False
        for stmt in try_node.body:
            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "on_event"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_lifecycle_capture"
                ):
                    call_found = True
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "event":
                            self.fail("le hook de capture ne doit jamais reassigner `event`")
        self.assertTrue(call_found, "self._lifecycle_capture.on_event(event) introuvable")

    def test_record_event_hook_runs_after_identity_enrichment_before_write(self):
        # Ordering proof via source position (single occurrence of each anchor
        # within _record_event's body — safe to locate by substring index).
        method = self._find_method("_record_event")
        start = method.lineno
        end = method.body[-1].end_lineno
        lines = self.source.splitlines()[start - 1 : end]
        snippet = "\n".join(lines)
        idx_enrich = snippet.index("_identity_enricher.enrich(event)")
        idx_capture = snippet.index("_lifecycle_capture.on_event(event)")
        idx_write = snippet.index("self.events_path.open(")
        self.assertLess(idx_enrich, idx_capture, "capture doit venir APRES l'enrichissement identite")
        self.assertLess(idx_capture, idx_write, "capture doit venir AVANT l'ecriture JSONL")

    def test_no_other_change_to_record_event_signature(self):
        method = self._find_method("_record_event")
        args = [a.arg for a in method.args.args]
        self.assertEqual(args, ["self", "event"])


class CompileTests(unittest.TestCase):
    def test_py_compile_demo_router(self):
        py_compile.compile(str(DEMO_ROUTER_PY), doraise=True)

    def test_py_compile_lifecycle_capture(self):
        py_compile.compile(str(LIFECYCLE_CAPTURE_PY), doraise=True)


# --------------------------------------------------------------------------- #
# (2) SERVICE — routing, ref discipline, idempotence, errors, diagnostics
# --------------------------------------------------------------------------- #
class FillThenCloseTests(unittest.TestCase):
    def test_fill_then_close_creates_binds_and_closes(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(555, opened_at=T1, position_identifier=9001)

        ctx.service.on_event(_open_event(555))
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 1)
        self.assertEqual(diag["bound"], 1)
        self.assertEqual(diag["closed"], 0)
        self.assertEqual(diag["unresolved"], {})
        self.assertEqual(diag["errors"], {})

        ctx.service.on_event(_close_event(555))
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 1)
        self.assertEqual(diag["bound"], 1)
        self.assertEqual(diag["closed"], 1)
        self.assertEqual(diag["unresolved"], {})
        self.assertEqual(diag["errors"], {})

    def test_close_event_types_all_route_through_quick_exit_prefix_and_exact_set(self):
        for event_type in (
            "EXIT_V2_CLOSE",
            "POSITION_SYNC",
            "RESCUE_CLOSE",
            "QUICK_EXIT_CLOSE",
            "QUICK_EXIT_SLTP",
        ):
            ctx = _ServiceCtx()
            ctx.lookup.set(600, opened_at=T1, position_identifier=9101)
            ctx.service.on_event(_open_event(600))
            ctx.service.on_event(_close_event(600, event_type=event_type))
            diag = ctx.service.diagnostics()
            self.assertEqual(diag["closed"], 1, "event_type=%s" % event_type)
            self.assertEqual(diag["errors"], {}, "event_type=%s" % event_type)


class TicketRecyclingTests(unittest.TestCase):
    def test_recycled_ticket_discriminated_by_position_identifier_and_opened_at(self):
        ctx = _ServiceCtx()
        ticket = 777

        ctx.lookup.set(ticket, opened_at=T1, position_identifier=9001)
        ctx.service.on_event(_open_event(ticket, direction="BUY", setup_id="s1"))
        ref1, reason1 = _build_ref_for_assert(ctx, ticket)
        self.assertIsNone(reason1)
        record1, _ = ctx.store.resolve_by_broker_position(ref1)
        self.assertIsNotNone(record1)
        lifecycle_1 = record1["lifecycle_id"]

        ctx.service.on_event(_close_event(ticket, direction="BUY"))

        # Recycled: same ticket, new position (different opened_at/pid/direction).
        ctx.lookup.set(ticket, opened_at=T2, position_identifier=9002)
        ctx.service.on_event(_open_event(ticket, direction="SELL", setup_id="s2"))
        ref2, reason2 = _build_ref_for_assert(ctx, ticket, direction="SELL")
        self.assertIsNone(reason2)
        record2, _ = ctx.store.resolve_by_broker_position(ref2)
        self.assertIsNotNone(record2)
        lifecycle_2 = record2["lifecycle_id"]

        self.assertNotEqual(lifecycle_1, lifecycle_2)
        record1_final = ctx.store.get_lifecycle(lifecycle_1)
        self.assertEqual(record1_final["state"], "CLOSED")
        self.assertEqual(record2["state"], "OPEN")

        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 2)
        self.assertEqual(diag["bound"], 2)
        self.assertEqual(diag["closed"], 1)
        self.assertEqual(diag["errors"], {})


def _build_ref_for_assert(ctx, ticket, *, direction="BUY", symbol="GOLD#"):
    from app.services.lifecycle_identity_store import try_build_broker_ref

    info = ctx.lookup(ticket)
    return try_build_broker_ref(
        account_scope_id=ACCT,
        ticket=ticket,
        broker_symbol=symbol,
        opened_at=info["opened_at"],
        magic=MAGIC,
        position_identifier=info["position_identifier"],
        direction=direction,
    )


class IncompleteRefTests(unittest.TestCase):
    def test_open_with_missing_symbol_is_unresolved_not_exception(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(801, opened_at=T1, position_identifier=9201)
        event = _open_event(801)
        del event["broker_symbol"]
        ctx.service.on_event(event)  # must not raise
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 1)  # lifecycle created, but never bound
        self.assertEqual(diag["bound"], 0)
        self.assertEqual(diag["errors"], {})
        self.assertEqual(diag["unresolved"].get("SYMBOL_MISSING"), 1)

    def test_open_without_position_lookup_hit_is_unresolved(self):
        ctx = _ServiceCtx()
        # No ctx.lookup.set(...) => position_lookup returns None => opened_at
        # missing => try_build_broker_ref fails => UNRESOLVED, no ticket-only
        # fallback, and no exception.
        ctx.service.on_event(_open_event(802))
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 1)
        self.assertEqual(diag["bound"], 0)
        self.assertEqual(diag["unresolved"].get("OPENED_AT_INVALID"), 1)
        self.assertEqual(diag["errors"], {})

    def test_close_with_missing_symbol_is_unresolved_never_resolves_by_ticket_alone(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(803, opened_at=T1, position_identifier=9203)
        ctx.service.on_event(_open_event(803))
        close = _close_event(803)
        del close["broker_symbol"]
        ctx.service.on_event(close)  # must not raise, must not resolve
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["closed"], 0)
        self.assertEqual(diag["errors"], {})
        self.assertIn("SYMBOL_MISSING", diag["unresolved"])

    def test_close_for_unknown_broker_ref_is_unresolved(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(804, opened_at=T1, position_identifier=9204)
        # No matching open was ever recorded: resolve must fail cleanly.
        ctx.service.on_event(_close_event(804))
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["closed"], 0)
        self.assertEqual(diag["errors"], {})
        self.assertIn("BROKER_REF_UNKNOWN", diag["unresolved"])


class IdempotenceTests(unittest.TestCase):
    def test_duplicate_close_event_is_idempotent_no_exception_closed_at_preserved(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(900, opened_at=T1, position_identifier=9301)
        ctx.service.on_event(_open_event(900))
        ctx.service.on_event(_close_event(900))

        ref, reason = _build_ref_for_assert(ctx, 900)
        self.assertIsNone(reason)
        record_before, _ = ctx.store.resolve_by_broker_position(ref)
        closed_at_first = record_before["closed_at_utc"]

        # Fire the identical close event again with a DIFFERENT clock value —
        # if the second mark_closed were not idempotent this would overwrite
        # closed_at_utc; the store contract forbids that (ALREADY_CLOSED).
        service_2nd_clock = LifecycleCaptureService(
            ctx.store,
            account_scope_id=ACCT,
            magic=MAGIC,
            position_lookup=ctx.lookup,
            clock_utc_iso=_clock("2026-07-21T23:59:59+00:00"),
        )
        service_2nd_clock.on_event(_close_event(900))  # must not raise

        record_after, _ = ctx.store.resolve_by_broker_position(ref)
        self.assertEqual(record_after["closed_at_utc"], closed_at_first)
        self.assertEqual(record_after["state"], "CLOSED")

    def test_duplicate_open_event_does_not_crash_and_confines_conflict(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(901, opened_at=T1, position_identifier=9302)
        event = _open_event(901)
        ctx.service.on_event(event)
        ctx.service.on_event(dict(event))  # same ticket/ref replayed
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 2)  # a fresh lifecycle IS minted each call
        self.assertEqual(diag["bound"], 1)  # the second bind conflicts, confined
        self.assertGreaterEqual(sum(diag["errors"].values()), 1)


class BoomStoreConfinementTests(unittest.TestCase):
    def test_any_internal_exception_type_is_confined_and_counted(self):
        service = LifecycleCaptureService(
            _BoomStore(),
            account_scope_id=ACCT,
            magic=MAGIC,
            position_lookup=_FakePositionLookup(),
            clock_utc_iso=_clock(),
        )
        service.on_event(_open_event(950))  # must not raise
        service.on_event(_close_event(950))  # must not raise
        diag = service.diagnostics()
        self.assertGreaterEqual(diag["errors"].get("RuntimeError", 0), 1)
        self.assertEqual(diag["created"], 0)
        self.assertEqual(diag["bound"], 0)
        self.assertEqual(diag["closed"], 0)

    def test_malformed_event_never_raises(self):
        ctx = _ServiceCtx()
        for bad_event in (None, [], "not-a-dict", {"event_type": 123}, {"event_type": "DEMO_ORDER"}):
            ctx.service.on_event(bad_event)  # must never raise
        diag = ctx.service.diagnostics()
        self.assertEqual(diag["created"], 0)


class ResolverEnricherCompatibilityTests(unittest.TestCase):
    def test_resolver_output_consumed_correctly_by_identity_shadow_enricher(self):
        ctx = _ServiceCtx()
        ctx.lookup.set(1010, opened_at=T1, position_identifier=9401)
        ctx.service.on_event(_open_event(1010, direction="BUY"))

        ref, reason = _build_ref_for_assert(ctx, 1010)
        self.assertIsNone(reason)
        expected_record, _ = ctx.store.resolve_by_broker_position(ref)
        self.assertIsNotNone(expected_record)

        context = EventIdentityContext(server_id="srv-" + "ab" * 4, bot_instance_id="bot-lab-p1d-p2")
        enricher = IdentityShadowEnricher(
            context=context, enabled=True, lifecycle_resolver=ctx.service.make_lifecycle_resolver()
        )
        close_event = _close_event(1010, direction="BUY")
        out = enricher.enrich(dict(close_event))
        shadow = out["identity_shadow"]
        self.assertEqual(shadow["lifecycle_id"], expected_record["lifecycle_id"])
        self.assertEqual(shadow["lifecycle_resolution"], "RESOLVED")

    def test_resolver_returns_reason_when_unresolved_never_raises(self):
        ctx = _ServiceCtx()
        resolver = ctx.service.make_lifecycle_resolver()
        out = resolver(_close_event(1011))  # nothing bound for this ticket
        self.assertIsNone(out.get("lifecycle_id"))
        self.assertIn("reason", out)

    def test_resolver_never_raises_on_malformed_input(self):
        ctx = _ServiceCtx()
        resolver = ctx.service.make_lifecycle_resolver()
        for bad in (None, [], "nope", {"ticket": "not-an-int"}):
            out = resolver(bad)
            self.assertIn("reason", out)


class DiagnosticsSanitizationTests(unittest.TestCase):
    def test_diagnostics_never_leak_canary_ticket_or_symbol(self):
        ctx = _ServiceCtx()
        # No position_lookup hit configured for the canary ticket -> UNRESOLVED,
        # exercising the exact same path that would otherwise be tempted to
        # echo the offending value back in a reason string.
        event = _open_event(CANARY_TICKET, symbol=CANARY_SYMBOL, setup_id="setup-canary")
        ctx.service.on_event(event)
        diag = ctx.service.diagnostics()
        serialized = json.dumps(diag)
        self.assertNotIn(str(CANARY_TICKET), serialized)
        self.assertNotIn(CANARY_SYMBOL, serialized)
        self.assertEqual(diag["created"], 1)
        self.assertEqual(set(diag.keys()), {"created", "bound", "closed", "unresolved", "errors"})

    def test_diagnostics_error_keys_are_exception_type_names_only(self):
        service = LifecycleCaptureService(
            _BoomStore(),
            account_scope_id=ACCT,
            magic=MAGIC,
            clock_utc_iso=_clock(),
        )
        service.on_event(_open_event(CANARY_TICKET, symbol=CANARY_SYMBOL))
        diag = service.diagnostics()
        serialized = json.dumps(diag)
        self.assertNotIn(str(CANARY_TICKET), serialized)
        self.assertNotIn(CANARY_SYMBOL, serialized)
        self.assertNotIn("boom-create", serialized)  # exception MESSAGE never leaks
        self.assertEqual(diag["errors"], {"RuntimeError": 1})


class ConstructorContractTests(unittest.TestCase):
    def test_clock_utc_iso_is_mandatory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            with self.assertRaises(ValueError):
                LifecycleCaptureService(
                    store, account_scope_id=ACCT, magic=MAGIC, clock_utc_iso=None
                )

    def test_position_lookup_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            service = LifecycleCaptureService(
                store, account_scope_id=ACCT, magic=MAGIC, clock_utc_iso=_clock()
            )
            service.on_event(_open_event(1099))  # no lookup configured at all
            diag = service.diagnostics()
            self.assertEqual(diag["created"], 1)
            self.assertEqual(diag["bound"], 0)
            self.assertIn("OPENED_AT_INVALID", diag["unresolved"])


if __name__ == "__main__":
    unittest.main()
