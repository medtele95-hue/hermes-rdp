"""TRACEABILITY_AUDIT — DORMANT end-to-end traceability auditor (M08-P1).

MISSION 100% LABORATOIRE : this module is imported by NO production runtime
(no import from main.py / demo_router.py / lifecycle_capture.py /
event_identity_runtime.py / system_heartbeat.py). It never touches MT5,
never opens a network connection, never reads an environment variable or a
wall clock. Every input is either a plain, caller-INJECTED snapshot (same
discipline as M06's ``Reconciler``) or a plain file PATH that this module
reads in a strictly READ-ONLY fashion via the M02/M03 primitives already
built for that purpose.

ROLE : for one "world" (a lifecycle store + an ORDER_INTENT journal + a PnL
ledger journal, any subset of which may be absent), prove or disprove EVERY
link of the end-to-end chain

    provenance -> identity -> lifecycle -> intent -> PnL

and report, per chain (rooted at a lifecycle_id, or at an orphan intent_id /
orphan PnL lifecycle_id when no real lifecycle resolves), which links are
``VERIFIED``, ``BROKEN``, or ``NOT_APPLICABLE`` — plus aggregate coverage
percentages and a single typed verdict. ``TraceabilityAuditor`` is PURE
DETECTION ONLY: it never mutates any input and never corrects an anomaly —
same spirit as M05's ``reconcile_intent_lifecycle_pnl`` and M06's
``Reconciler``, applied to a different lens (per-link chain tracing rather
than restart-anomaly typing).

INPUT SHAPES — ``TraceabilityAuditor`` NEVER holds a live
store/book/ledger/writer instance (same discipline as M06's ``Reconciler``:
"rejects live instances"). For each of the three data sources it accepts
EITHER a read-only file path OR an already-assembled plain snapshot (never
both for the same source):

- lifecycle store: ``store_path`` (a ``LifecycleIdentityStore`` JSON file) OR
  ``store_state = {"lifecycles": {lifecycle_id: record, ...}}`` (same record
  shape as a ``LifecycleIdentityStore`` record). Neither
  ``LifecycleIdentityStore`` nor ``OrderIntentBook`` exposes a "dump
  everything" API (a debt already documented by M06's ``Reconciler`` — out of
  scope to fix here). For the path form, this module builds a SHORT-LIVED
  ``LifecycleIdentityStore(path=...)`` instance purely to let the store's own
  strict on-disk validation run, copies its already-validated
  ``_lifecycles`` mapping (the only way to enumerate every record — no
  public alternative exists), and immediately discards the instance: no
  write method is ever called on it. If ``store_path``/``store_state`` are
  both absent, ``journal_paths["lifecycle_ops"]`` (a M03 journal of
  ``create``/``bind``/``mark_closed`` operations, same shape consumed by
  ``event_journal.rebuild_lifecycle_state``) is used instead, when given.
  All three absent -> an empty lifecycle universe (not an error).
- order intents: ``journal_paths["intents"]`` (a M04 ``OrderIntentBook``
  journal) OR ``intent_book_state = {"intents": [snapshot, ...]}`` (same
  shape as ``OrderIntentBook.get()``/``active_intents()`` entries, ALL states
  included). The path form NEVER instantiates ``OrderIntentBook`` (which
  would reopen the journal for writing after replay, per its own
  ``rebuild_from_journal`` contract, and still exposes no bulk-enumeration
  API): it replays the journal directly via ``JournalReader`` and applies a
  minimal, defensive, non-raising interpretation of the
  ``intent_created``/``intent_transition`` payload shapes documented in
  ``order_intent.py`` — a malformed record is skipped, never raised (this
  module reports brokenness, it does not require strict validation
  guarantees the way ``OrderIntentBook`` itself does).
- PnL ledger: ``journal_paths["pnl"]`` (a M05 ``PnlLedger`` journal) OR
  ``ledger_state = {"lifecycles": {lifecycle_id: [entry, ...]}}`` (exact
  ``PnlLedger.snapshot()`` shape). The path form NEVER instantiates
  ``PnlLedger`` either — it replays the journal directly via
  ``JournalReader`` and filters ``op == "pnl_computed"``, mirroring
  ``PnlLedger.rebuild()``'s own logic without needing a writer-holding
  instance at all.

LIVE-INSTANCE REJECTION (same discipline as M06): passing an actual
``LifecycleIdentityStore``/``OrderIntentBook``/``PnlLedger``/``JournalWriter``
instance anywhere a plain path or a plain dict snapshot is expected raises
``LiveInstanceRejectedError`` immediately — fail fast on API misuse, never a
silent accidental write surface.

CHAIN MODEL — ``audit()`` returns
``{chains: [{chain_id, links: [{from, to, status, reason, ...}], complete}],
coverage: {...}, broken_links: [...], verdict, report_digest}``:

- one chain per journal given (``chain_id = "JOURNAL:<key>"``), a single link
  reusing ``JournalReader.verify()`` verbatim (fail-closed, surfaced, never
  re-implemented) ;
- one chain per lifecycle_id known to the store, with SIX links: intent ->
  lifecycle (do any intents resolve here, and do they all carry
  ``provenance.boot_id``/``provenance.cycle_id``?), lifecycle -> correlation
  (``correlation_id`` absent is ``NOT_APPLICABLE``, present must be a valid
  UUIDv7 via ``event_identity.validate_correlation_id``, reused verbatim),
  lifecycle -> broker_ref (``None``/``None`` is an honest ``VERIFIED``
  absence; a complete, internally-consistent ref re-derived via
  ``try_build_broker_ref``/``broker_ref_key``, reused verbatim from M02, is
  ``VERIFIED``; anything inconsistent is ``BROKEN``), lifecycle -> creation
  provenance (``boot_id``/``cycle_id`` both absent is ``NOT_APPLICABLE``, both
  present and well-formed is ``VERIFIED``, exactly one present is
  ``BROKEN``), lifecycle -> timestamps (``closed_at_utc`` absent is
  ``NOT_APPLICABLE`` — still OPEN; present and ``>= created_at_utc`` is
  ``VERIFIED``; otherwise ``BROKEN``), lifecycle -> pnl_ledger (not CLOSED is
  ``NOT_APPLICABLE``; CLOSED with >=1 ledger entry is ``VERIFIED``; CLOSED
  with zero is ``BROKEN``) ;
- one orphan chain per intent whose link is unambiguously broken
  (``chain_id = "INTENT:<intent_id>"``): an EXPLICIT ``links.lifecycle_id``
  that resolves nowhere in the store, or a ``FILLED`` intent with no
  resolvable lifecycle at all (mirrors M05's
  ``INTENT_FILLED_SANS_LIFECYCLE`` naming spirit, not reimplemented from
  that module — this is a fresh, narrower check scoped to chain tracing) ;
- one orphan chain per PnL ledger entry whose ``lifecycle_id`` is absent
  from the store (``chain_id = "PNL:<lifecycle_id>"``) ;
- one transversal chain (``chain_id = "TRANSVERSAL_IDS"``) checking that no
  value is used as BOTH an ``intent_id`` and a ``lifecycle_id`` — the one
  cross-module identity-space collision this module considers a bug (a
  ``lifecycle_id`` doubling as a ``PnlLedger`` key is the CORRECT, INTENDED
  join, never flagged here).

A chain is ``complete`` iff none of its links is ``BROKEN`` (``VERIFIED``/
``NOT_APPLICABLE`` both count as "nothing wrong here"). ``broken_links`` is
every ``BROKEN`` link across every chain, flattened, each tagged with its
owning ``chain_id`` and a ``severity`` (``CRITICAL``/``WARN``, a fixed
per-reason table). ``verdict``: ``BROKEN`` if the store could not be read, if
any given journal is not intact, or if any broken link is ``CRITICAL``;
``PARTIAL_TRACE`` if there is at least one ``WARN``-only broken link;
``FULL_TRACE`` otherwise. An EMPTY world (nothing given at all) is
documented, BY CONVENTION, as ``FULL_TRACE`` with 100% coverage everywhere
(vacuously true — there is nothing to break).

DETERMINISM : ``audit()`` never reads a clock, never uses randomness, sorts
``chains``/``broken_links`` by canonical JSON before returning, and includes
a ``report_digest`` (sha256 hex, first 16 chars, of the canonical report with
``report_digest`` itself excluded) — two calls with the same (possibly
differently-ordered-in-memory) inputs always produce a byte-identical report.

``audit_production_events(events_jsonl_path, *, max_lines=None)`` is a
SEPARATE, unrelated read-only entry point: a tail-safe photograph of a REAL
production events JSONL file's CURRENT traceability posture (percentage of
events carrying ``identity_shadow``, percentage of ``SYSTEM_HEARTBEAT``
events carrying both ``boot_id``/``cycle_id``, presence of ``ticket``/
``setup_id`` per event type). A corrupted line is counted, NEVER raised.
Plain ``json``/``pathlib`` — this is the app's own JSONL event-log format,
not a M03 journal (no header, no hash chain), so ``JournalReader`` does not
apply here.

PURITY CONTRACT — this module NEVER:
- reads an environment variable, a ``.env`` file, the wall clock, or MT5;
- performs network I/O;
- calls a WRITE method of any store/journal it reads (``create_lifecycle``,
  ``bind_broker_position``, ``mark_closed``, ``JournalWriter.append``,
  ``OrderIntentBook.create_intent``/``transition``,
  ``PnlLedger.compute_and_record``) — structurally impossible for the
  snapshot-form inputs (plain dicts), and never attempted for the path-form
  inputs either (only ``JournalReader``, never a ``JournalWriter``, and the
  one short-lived ``LifecycleIdentityStore`` built for enumeration is
  discarded immediately after copying its state).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.services.event_identity import EventIdentityValidationError, validate_correlation_id
from app.services.event_journal import JournalReader, JournalWriter, rebuild_lifecycle_state
from app.services.lifecycle_identity_store import (
    LifecycleIdentityStore,
    LifecycleStoreError,
    broker_ref_key,
    try_build_broker_ref,
)
from app.services.lifecycle_pnl import PnlLedger
from app.services.order_intent import OrderIntentBook

REPORT_SCHEMA_NAME = "hermes.traceability_audit_report"
REPORT_SCHEMA_VERSION = 1
PROD_SNAPSHOT_SCHEMA_NAME = "hermes.traceability_audit.production_snapshot"
PROD_SNAPSHOT_SCHEMA_VERSION = 1

STATUS_VERIFIED = "VERIFIED"
STATUS_BROKEN = "BROKEN"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

VERDICT_FULL_TRACE = "FULL_TRACE"
VERDICT_PARTIAL_TRACE = "PARTIAL_TRACE"
VERDICT_BROKEN = "BROKEN"

_REASON_SEVERITY = {
    "JOURNAL_CORRUPTED": "CRITICAL",
    "STORE_UNREADABLE": "CRITICAL",
    "PNL_ORPHAN_LIFECYCLE": "CRITICAL",
    "DUPLICATE_ID_CROSS_MODULE": "CRITICAL",
    "TIMESTAMPS_INCOHERENT": "CRITICAL",
    "BROKER_REF_INCONSISTENT": "CRITICAL",
    "INTENT_LIFECYCLE_UNRESOLVED": "CRITICAL",
    "INTENT_FILLED_WITHOUT_LIFECYCLE": "CRITICAL",
    "INTENT_PROVENANCE_MISSING": "WARN",
    "CORRELATION_ID_INVALID": "WARN",
    "PROVENANCE_INCONSISTENT": "WARN",
    "CLOSED_WITHOUT_PNL": "WARN",
}

_JOURNAL_PATH_KEYS = frozenset({"intents", "pnl", "lifecycle_ops"})
_INTENT_FILLED_STATE = "FILLED"
_LIFECYCLE_CLOSED_STATE = "CLOSED"


# --------------------------------------------------------------------------- #
# Exceptions (sanitized: reason_code only, never a raw value/secret)
# --------------------------------------------------------------------------- #
class TraceabilityAuditError(Exception):
    """Base. ``reason_code`` = sanitized machine diagnostic."""

    reason_code = "TRACEABILITY_AUDIT_ERROR"

    def __init__(self, reason_code: str = "TRACEABILITY_AUDIT_ERROR"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class AuditInputError(TraceabilityAuditError):
    reason_code = "AUDIT_INPUT_INVALID"


class LiveInstanceRejectedError(TraceabilityAuditError):
    reason_code = "LIVE_INSTANCE_REJECTED"


# --------------------------------------------------------------------------- #
# Small, side-effect-free helpers
# --------------------------------------------------------------------------- #
def _reject_live_instance(value: object, field_name: str) -> None:
    if isinstance(value, (LifecycleIdentityStore, OrderIntentBook, PnlLedger, JournalWriter)):
        raise LiveInstanceRejectedError(
            "%s_MUST_BE_PLAIN_NOT_LIVE_INSTANCE:%s" % (field_name, type(value).__name__)
        )


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest16(obj: object) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()[:16]


def _parse_iso_utc(value: object) -> "datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _is_valid_uuid_any(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _dewrap_opened_at(canonical: object) -> "str | int | None":
    """Reverses the ``iso:``/``msc:`` canonical wrapping applied by
    ``lifecycle_identity_store.try_build_broker_ref`` -- a small local copy
    of the same tiny, pure transform already used (privately) inside
    ``lifecycle_pnl.py``; not worth importing a private symbol for."""
    if not isinstance(canonical, str):
        return None
    if canonical.startswith("iso:"):
        return canonical[len("iso:"):]
    if canonical.startswith("msc:"):
        try:
            return int(canonical[len("msc:"):])
        except ValueError:
            return None
    return None


def _link(from_: str, to_: str, status: str, reason: str, **fields: object) -> dict:
    out = {"from": from_, "to": to_, "status": status, "reason": reason}
    out.update(fields)
    return out


# --------------------------------------------------------------------------- #
# Read-only ingestion (never a write method, never a lingering live instance)
# --------------------------------------------------------------------------- #
def _read_store_lifecycles(store_path, allowed_dir) -> "tuple[dict, bool, str | None]":
    """Returns ``(lifecycles, readable, error_reason)``. Never raises. See
    the module docstring for why a short-lived ``LifecycleIdentityStore`` is
    built and immediately discarded here."""
    try:
        store = LifecycleIdentityStore(path=store_path, allowed_dir=allowed_dir)
    except LifecycleStoreError as exc:
        return {}, False, getattr(exc, "reason_code", "STORE_UNREADABLE")
    except OSError as exc:
        return {}, False, "STORE_UNREADABLE:%s" % type(exc).__name__
    lifecycles = {lid: dict(rec) for lid, rec in store._lifecycles.items()}  # noqa: SLF001 (see module docstring)
    return lifecycles, True, None


def _read_intents_from_journal(path) -> dict:
    """Pure, minimal, defensive replay of a M04 ORDER_INTENT journal via
    ``JournalReader`` only -- never instantiates ``OrderIntentBook``. See
    module docstring."""
    intents: "dict[str, dict]" = {}
    for payload in JournalReader(path).replay():
        if not isinstance(payload, dict):
            continue
        op = payload.get("op")
        if op == "intent_created":
            intent_id = payload.get("intent_id")
            if not isinstance(intent_id, str) or not intent_id or intent_id in intents:
                continue
            intents[intent_id] = {
                "intent_id": intent_id,
                "state": payload.get("state"),
                "provenance": payload.get("provenance"),
                "links": payload.get("links"),
                "created_at_utc": payload.get("created_at_utc"),
            }
        elif op == "intent_transition":
            intent_id = payload.get("intent_id")
            record = intents.get(intent_id) if isinstance(intent_id, str) else None
            if record is not None:
                record["state"] = payload.get("to_state")
    return intents


def _read_pnl_ledger_from_journal(path) -> dict:
    """Pure replay of a M05 PnL ledger journal -- mirrors
    ``PnlLedger.rebuild()``'s own filtering logic without ever holding a
    ``PnlLedger`` (writer-capable) instance."""
    ledger: "dict[str, list]" = {}
    for payload in JournalReader(path).replay():
        if not isinstance(payload, dict) or payload.get("op") != "pnl_computed":
            continue
        lifecycle_id = payload.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            continue
        ledger.setdefault(lifecycle_id, []).append(dict(payload))
    return ledger


# --------------------------------------------------------------------------- #
# Per-link checks
# --------------------------------------------------------------------------- #
def _correlation_link(record: dict) -> "tuple[str, str]":
    correlation_id = record.get("correlation_id")
    if correlation_id is None:
        return STATUS_NOT_APPLICABLE, "CORRELATION_ID_ABSENT"
    try:
        validate_correlation_id(correlation_id)
    except EventIdentityValidationError:
        return STATUS_BROKEN, "CORRELATION_ID_INVALID"
    return STATUS_VERIFIED, "CORRELATION_ID_VALID"


def _broker_ref_link(record: dict) -> "tuple[str, str]":
    ref = record.get("broker_ref")
    key = record.get("broker_key")
    if ref is None and key is None:
        return STATUS_VERIFIED, "BROKER_REF_HONESTLY_ABSENT"
    if not isinstance(ref, dict) or not isinstance(key, str) or not key:
        return STATUS_BROKEN, "BROKER_REF_INCONSISTENT"
    opened_at = _dewrap_opened_at(ref.get("opened_at"))
    if opened_at is None:
        return STATUS_BROKEN, "BROKER_REF_INCONSISTENT"
    rebuilt, _reason = try_build_broker_ref(
        account_scope_id=ref.get("account_scope_id"), ticket=ref.get("ticket"),
        broker_symbol=ref.get("broker_symbol"), opened_at=opened_at, magic=ref.get("magic"),
        position_identifier=ref.get("position_identifier"), direction=ref.get("direction"),
    )
    if rebuilt is None or broker_ref_key(rebuilt) != key:
        return STATUS_BROKEN, "BROKER_REF_INCONSISTENT"
    return STATUS_VERIFIED, "BROKER_REF_COMPLETE"


def _provenance_link(record: dict) -> "tuple[str, str]":
    boot_id = record.get("boot_id")
    cycle_id = record.get("cycle_id")
    if boot_id is None and cycle_id is None:
        return STATUS_NOT_APPLICABLE, "PROVENANCE_ABSENT"
    if boot_id is None or cycle_id is None:
        return STATUS_BROKEN, "PROVENANCE_INCONSISTENT"
    if not _is_valid_uuid_any(boot_id):
        return STATUS_BROKEN, "PROVENANCE_INCONSISTENT"
    if isinstance(cycle_id, bool) or not isinstance(cycle_id, int) or cycle_id < 0:
        return STATUS_BROKEN, "PROVENANCE_INCONSISTENT"
    return STATUS_VERIFIED, "PROVENANCE_PRESENT"


def _timestamps_link(record: dict) -> "tuple[str, str]":
    closed_at = record.get("closed_at_utc")
    if closed_at is None:
        return STATUS_NOT_APPLICABLE, "STILL_OPEN"
    created_dt = _parse_iso_utc(record.get("created_at_utc"))
    closed_dt = _parse_iso_utc(closed_at)
    if created_dt is None or closed_dt is None or closed_dt < created_dt:
        return STATUS_BROKEN, "TIMESTAMPS_INCOHERENT"
    return STATUS_VERIFIED, "TIMESTAMPS_COHERENT"


def _pnl_link(lifecycle_id: str, record: dict, ledger: dict) -> "tuple[str, str]":
    if record.get("state") != _LIFECYCLE_CLOSED_STATE:
        return STATUS_NOT_APPLICABLE, "LIFECYCLE_NOT_CLOSED"
    if ledger.get(lifecycle_id):
        return STATUS_VERIFIED, "PNL_ENTRY_PRESENT"
    return STATUS_BROKEN, "CLOSED_WITHOUT_PNL"


def _resolve_intent_lifecycle_id(intent: dict, lifecycles_by_correlation: dict) -> "str | None":
    links = intent.get("links") if isinstance(intent.get("links"), dict) else {}
    lifecycle_id = links.get("lifecycle_id")
    if lifecycle_id is not None:
        return lifecycle_id
    correlation_id = links.get("correlation_id")
    if correlation_id is not None:
        return lifecycles_by_correlation.get(correlation_id)
    return None


# --------------------------------------------------------------------------- #
# TraceabilityAuditor -- PURE, detection only, never mutates/writes anything
# --------------------------------------------------------------------------- #
class TraceabilityAuditor:
    """PURE, read-only, end-to-end chain tracer. See module docstring for
    the full contract and the exact input shapes. Never holds a live
    store/book/ledger/writer instance."""

    def __init__(
        self,
        *,
        store_path: "Path | str | None" = None,
        journal_paths: "dict | None" = None,
        store_state: "dict | None" = None,
        intent_book_state: "dict | None" = None,
        ledger_state: "dict | None" = None,
        allowed_dir: "Path | str | None" = None,
    ) -> None:
        _reject_live_instance(store_path, "STORE_PATH")
        _reject_live_instance(store_state, "STORE_STATE")
        _reject_live_instance(intent_book_state, "INTENT_BOOK_STATE")
        _reject_live_instance(ledger_state, "LEDGER_STATE")

        if store_path is not None and store_state is not None:
            raise AuditInputError("STORE_PATH_AND_STORE_STATE_MUTUALLY_EXCLUSIVE")
        if store_path is not None and not isinstance(store_path, (str, Path)):
            raise AuditInputError("STORE_PATH_INVALID_TYPE")
        if store_state is not None and (
            not isinstance(store_state, dict) or set(store_state.keys()) != {"lifecycles"}
            or not isinstance(store_state["lifecycles"], dict)
        ):
            raise AuditInputError("STORE_STATE_INVALID_SHAPE")

        if intent_book_state is not None and (
            not isinstance(intent_book_state, dict) or set(intent_book_state.keys()) != {"intents"}
            or not isinstance(intent_book_state["intents"], list)
        ):
            raise AuditInputError("INTENT_BOOK_STATE_INVALID_SHAPE")

        if ledger_state is not None and (
            not isinstance(ledger_state, dict) or set(ledger_state.keys()) != {"lifecycles"}
            or not isinstance(ledger_state["lifecycles"], dict)
        ):
            raise AuditInputError("LEDGER_STATE_INVALID_SHAPE")

        if journal_paths is not None:
            if not isinstance(journal_paths, dict):
                raise AuditInputError("JOURNAL_PATHS_NOT_DICT")
            unknown = set(journal_paths.keys()) - _JOURNAL_PATH_KEYS
            if unknown:
                raise AuditInputError("JOURNAL_PATHS_UNKNOWN_KEYS:%s" % ",".join(sorted(unknown)))
            for key, value in journal_paths.items():
                _reject_live_instance(value, "JOURNAL_PATHS[%s]" % key)
                if not isinstance(value, (str, Path)):
                    raise AuditInputError("JOURNAL_PATHS[%s]_INVALID_TYPE" % key)
            if "intents" in journal_paths and intent_book_state is not None:
                raise AuditInputError("INTENTS_PATH_AND_STATE_MUTUALLY_EXCLUSIVE")
            if "pnl" in journal_paths and ledger_state is not None:
                raise AuditInputError("PNL_PATH_AND_STATE_MUTUALLY_EXCLUSIVE")
            if "lifecycle_ops" in journal_paths and (store_path is not None or store_state is not None):
                raise AuditInputError("LIFECYCLE_OPS_PATH_AND_STORE_MUTUALLY_EXCLUSIVE")

        self._store_path = Path(store_path) if store_path is not None else None
        self._journal_paths = dict(journal_paths) if journal_paths else {}
        self._store_state = store_state
        self._intent_book_state = intent_book_state
        self._ledger_state = ledger_state
        self._allowed_dir = allowed_dir

    # ------------------------------------------------------------- ingestion
    def _ingest(self) -> "tuple[dict, dict, dict, dict, bool, bool, str | None]":
        journal_results: "dict[str, dict]" = {}
        for key in ("intents", "pnl", "lifecycle_ops"):
            path = self._journal_paths.get(key)
            if path is not None:
                journal_results[key] = JournalReader(path).verify()
        journal_integrity = all(r["intact"] for r in journal_results.values()) if journal_results else True

        store_readable = True
        store_error: "str | None" = None
        if self._store_state is not None:
            lifecycles = {lid: dict(rec) for lid, rec in self._store_state["lifecycles"].items()}
        elif self._store_path is not None:
            lifecycles, store_readable, store_error = _read_store_lifecycles(self._store_path, self._allowed_dir)
        elif "lifecycle_ops" in self._journal_paths and journal_results["lifecycle_ops"]["intact"]:
            rebuilt = rebuild_lifecycle_state(JournalReader(self._journal_paths["lifecycle_ops"]))
            lifecycles = dict(rebuilt["lifecycles"])
        else:
            lifecycles = {}

        if self._intent_book_state is not None:
            intents = {
                i["intent_id"]: i for i in self._intent_book_state["intents"]
                if isinstance(i, dict) and isinstance(i.get("intent_id"), str) and i.get("intent_id")
            }
        elif "intents" in self._journal_paths and journal_results["intents"]["intact"]:
            intents = _read_intents_from_journal(self._journal_paths["intents"])
        else:
            intents = {}

        if self._ledger_state is not None:
            ledger = {lid: list(entries) for lid, entries in self._ledger_state["lifecycles"].items()}
        elif "pnl" in self._journal_paths and journal_results["pnl"]["intact"]:
            ledger = _read_pnl_ledger_from_journal(self._journal_paths["pnl"])
        else:
            ledger = {}

        return lifecycles, intents, ledger, journal_results, journal_integrity, store_readable, store_error

    # ------------------------------------------------------------- API
    def audit(self) -> dict:
        lifecycles, intents, ledger, journal_results, journal_integrity, store_readable, store_error = (
            self._ingest()
        )

        chains: "list[dict]" = []

        for key in ("intents", "pnl", "lifecycle_ops"):
            if key in journal_results:
                intact = journal_results[key]["intact"]
                status = STATUS_VERIFIED if intact else STATUS_BROKEN
                reason = "JOURNAL_INTACT" if intact else "JOURNAL_CORRUPTED"
                chains.append({
                    "chain_id": "JOURNAL:%s" % key,
                    "links": [_link("journal", "integrity", status, reason, journal=key,
                                     errors=list(journal_results[key].get("errors", [])))],
                    "complete": intact,
                })

        lifecycles_by_correlation = {
            rec.get("correlation_id"): lid for lid, rec in lifecycles.items() if rec.get("correlation_id")
        }

        intents_by_lifecycle: "dict[str, list]" = {}
        intents_linked = 0
        for intent in intents.values():
            resolved = _resolve_intent_lifecycle_id(intent, lifecycles_by_correlation)
            if resolved is not None and resolved in lifecycles:
                intents_linked += 1
                intents_by_lifecycle.setdefault(resolved, []).append(intent)

        for lid, rec in sorted(lifecycles.items()):
            links: "list[dict]" = []

            linked_intents = intents_by_lifecycle.get(lid, [])
            if not linked_intents:
                links.append(_link("intent", "lifecycle", STATUS_NOT_APPLICABLE, "NO_INTENT_LINKED"))
            else:
                missing_prov = any(
                    not isinstance(i.get("provenance"), dict)
                    or i["provenance"].get("boot_id") is None
                    or i["provenance"].get("cycle_id") is None
                    for i in linked_intents
                )
                if missing_prov:
                    links.append(_link("intent", "lifecycle", STATUS_BROKEN, "INTENT_PROVENANCE_MISSING"))
                else:
                    links.append(_link("intent", "lifecycle", STATUS_VERIFIED, "INTENT_PROVENANCE_PRESENT"))

            corr_status, corr_reason = _correlation_link(rec)
            links.append(_link("lifecycle", "correlation_id", corr_status, corr_reason))

            broker_status, broker_reason = _broker_ref_link(rec)
            links.append(_link("lifecycle", "broker_ref", broker_status, broker_reason))

            prov_status, prov_reason = _provenance_link(rec)
            links.append(_link("lifecycle", "creation_provenance", prov_status, prov_reason))

            ts_status, ts_reason = _timestamps_link(rec)
            links.append(_link("lifecycle", "timestamps", ts_status, ts_reason))

            pnl_status, pnl_reason = _pnl_link(lid, rec, ledger)
            links.append(_link("lifecycle", "pnl_ledger", pnl_status, pnl_reason))

            complete = all(l["status"] != STATUS_BROKEN for l in links)
            chains.append({"chain_id": lid, "links": links, "complete": complete})

        for intent_id, intent in sorted(intents.items()):
            links_field = intent.get("links") if isinstance(intent.get("links"), dict) else {}
            explicit_lid = links_field.get("lifecycle_id")
            resolved = _resolve_intent_lifecycle_id(intent, lifecycles_by_correlation)
            if explicit_lid is not None and explicit_lid not in lifecycles:
                chains.append({
                    "chain_id": "INTENT:%s" % intent_id,
                    "links": [_link("intent", "lifecycle", STATUS_BROKEN, "INTENT_LIFECYCLE_UNRESOLVED",
                                     intent_id=intent_id, lifecycle_id=explicit_lid)],
                    "complete": False,
                })
            elif intent.get("state") == _INTENT_FILLED_STATE and (resolved is None or resolved not in lifecycles):
                chains.append({
                    "chain_id": "INTENT:%s" % intent_id,
                    "links": [_link("intent", "lifecycle", STATUS_BROKEN, "INTENT_FILLED_WITHOUT_LIFECYCLE",
                                     intent_id=intent_id)],
                    "complete": False,
                })

        for lid in sorted(ledger.keys()):
            if lid not in lifecycles:
                chains.append({
                    "chain_id": "PNL:%s" % lid,
                    "links": [_link("pnl_ledger", "lifecycle", STATUS_BROKEN, "PNL_ORPHAN_LIFECYCLE",
                                     lifecycle_id=lid)],
                    "complete": False,
                })

        collisions = sorted(set(intents.keys()) & set(lifecycles.keys()))
        if collisions:
            transversal_links = [
                _link("intent_id", "lifecycle_id", STATUS_BROKEN, "DUPLICATE_ID_CROSS_MODULE", id_value=cid)
                for cid in collisions
            ]
            transversal_complete = False
        else:
            transversal_links = [_link("intent_id", "lifecycle_id", STATUS_VERIFIED, "NO_CROSS_MODULE_COLLISION")]
            transversal_complete = True
        chains.append({"chain_id": "TRANSVERSAL_IDS", "links": transversal_links, "complete": transversal_complete})

        chains.sort(key=lambda c: c["chain_id"])

        broken_links: "list[dict]" = []
        for chain in chains:
            for l in chain["links"]:
                if l["status"] == STATUS_BROKEN:
                    entry = dict(l)
                    entry["chain_id"] = chain["chain_id"]
                    entry["severity"] = _REASON_SEVERITY.get(l["reason"], "WARN")
                    broken_links.append(entry)
        broken_links.sort(key=_canonical_json)

        closed_lifecycles = [lid for lid, rec in lifecycles.items() if rec.get("state") == _LIFECYCLE_CLOSED_STATE]
        closed_with_pnl = sum(1 for lid in closed_lifecycles if ledger.get(lid))
        lifecycles_with_pnl_pct = (
            100.0 if not closed_lifecycles else round(100.0 * closed_with_pnl / len(closed_lifecycles), 4)
        )
        intents_linked_pct = 100.0 if not intents else round(100.0 * intents_linked / len(intents), 4)
        chains_complete = sum(1 for c in chains if c["complete"])
        chains_complete_pct = 100.0 if not chains else round(100.0 * chains_complete / len(chains), 4)

        coverage = {
            "intents_total": len(intents),
            "intents_linked_pct": intents_linked_pct,
            "lifecycles_total": len(lifecycles),
            "lifecycles_closed_total": len(closed_lifecycles),
            "lifecycles_with_pnl_pct": lifecycles_with_pnl_pct,
            "journal_integrity": journal_integrity,
            "store_readable": store_readable,
            "chains_total": len(chains),
            "chains_complete_pct": chains_complete_pct,
        }

        if not store_readable or not journal_integrity:
            verdict = VERDICT_BROKEN
        elif any(b["severity"] == "CRITICAL" for b in broken_links):
            verdict = VERDICT_BROKEN
        elif broken_links:
            verdict = VERDICT_PARTIAL_TRACE
        else:
            verdict = VERDICT_FULL_TRACE

        report = {
            "schema": REPORT_SCHEMA_NAME,
            "schema_version": REPORT_SCHEMA_VERSION,
            "chains": chains,
            "coverage": coverage,
            "broken_links": broken_links,
            "verdict": verdict,
            "store_error": store_error,
        }
        report["report_digest"] = _digest16(report)
        return report


# --------------------------------------------------------------------------- #
# audit_production_events -- separate, unrelated read-only entry point
# --------------------------------------------------------------------------- #
_HEARTBEAT_TYPE = "SYSTEM_HEARTBEAT"


def _empty_production_snapshot(path_str: str, max_lines: "int | None") -> dict:
    report = {
        "schema": PROD_SNAPSHOT_SCHEMA_NAME,
        "schema_version": PROD_SNAPSHOT_SCHEMA_VERSION,
        "source_path": path_str,
        "max_lines_applied": max_lines,
        "lines_total": 0,
        "lines_parsed": 0,
        "lines_corrupted": 0,
        "by_event_type": {},
        "identity_shadow": {"present_count": 0, "present_pct": 0.0, "expected_off": True},
        "system_heartbeat": {"total": 0, "with_boot_and_cycle": 0, "with_boot_and_cycle_pct": 0.0},
        "identity_fields_by_event_type": {},
    }
    report["digest"] = _digest16(report)
    return report


def audit_production_events(events_jsonl_path: "Path | str", *, max_lines: "int | None" = None) -> dict:
    """Tail-safe, read-only photograph of a REAL production events JSONL
    file's CURRENT traceability posture. See module docstring. Never raises,
    never writes, never reads a clock. ``max_lines`` (optional) caps how many
    lines are read from the START of the file."""
    path = Path(events_jsonl_path)
    if not path.exists() or not path.is_file():
        return _empty_production_snapshot(str(path), max_lines)

    lines_total = 0
    lines_parsed = 0
    lines_corrupted = 0
    by_event_type: "dict[str, int]" = {}
    identity_shadow_present = 0
    heartbeat_total = 0
    heartbeat_with_boot_cycle = 0
    identity_fields: "dict[str, dict]" = {}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                if max_lines is not None and lines_total >= max_lines:
                    break
                lines_total += 1
                stripped = raw_line.strip()
                if not stripped:
                    lines_corrupted += 1
                    continue
                try:
                    event = json.loads(stripped)
                except (ValueError, TypeError):
                    lines_corrupted += 1
                    continue
                if not isinstance(event, dict):
                    lines_corrupted += 1
                    continue
                lines_parsed += 1

                event_type = event.get("event_type")
                type_key = event_type if isinstance(event_type, str) and event_type else "UNKNOWN"
                by_event_type[type_key] = by_event_type.get(type_key, 0) + 1

                if "identity_shadow" in event:
                    identity_shadow_present += 1

                if type_key == _HEARTBEAT_TYPE:
                    heartbeat_total += 1
                    if event.get("boot_id") is not None and event.get("cycle_id") is not None:
                        heartbeat_with_boot_cycle += 1

                bucket = identity_fields.setdefault(
                    type_key, {"sample_count": 0, "ticket_present": 0, "setup_id_present": 0})
                bucket["sample_count"] += 1
                if "ticket" in event:
                    bucket["ticket_present"] += 1
                if "setup_id" in event:
                    bucket["setup_id_present"] += 1
    except OSError:
        return _empty_production_snapshot(str(path), max_lines)

    identity_fields_by_event_type = {
        t: {
            "sample_count": b["sample_count"],
            "ticket_present_pct": round(100.0 * b["ticket_present"] / b["sample_count"], 4),
            "setup_id_present_pct": round(100.0 * b["setup_id_present"] / b["sample_count"], 4),
        }
        for t, b in identity_fields.items()
    }

    report = {
        "schema": PROD_SNAPSHOT_SCHEMA_NAME,
        "schema_version": PROD_SNAPSHOT_SCHEMA_VERSION,
        "source_path": str(path),
        "max_lines_applied": max_lines,
        "lines_total": lines_total,
        "lines_parsed": lines_parsed,
        "lines_corrupted": lines_corrupted,
        "by_event_type": by_event_type,
        "identity_shadow": {
            "present_count": identity_shadow_present,
            "present_pct": round(100.0 * identity_shadow_present / lines_parsed, 4) if lines_parsed else 0.0,
            "expected_off": True,
        },
        "system_heartbeat": {
            "total": heartbeat_total,
            "with_boot_and_cycle": heartbeat_with_boot_cycle,
            "with_boot_and_cycle_pct": (
                round(100.0 * heartbeat_with_boot_cycle / heartbeat_total, 4) if heartbeat_total else 0.0
            ),
        },
        "identity_fields_by_event_type": identity_fields_by_event_type,
    }
    report["digest"] = _digest16(report)
    return report
