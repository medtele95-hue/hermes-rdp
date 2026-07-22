"""RECONCILIATION_LAB — DORMANT restart-reconciliation laboratory (M06-P1).

MISSION 100% LABORATOIRE: this module is imported by NO production runtime
(no import from main.py / demo_router.py / lifecycle_capture.py /
event_identity_runtime.py / system_heartbeat.py). It never touches MT5,
never opens a network connection, never reads an environment variable or a
wall clock. Every input is INJECTED by the caller as a plain mapping / a
pre-built ``BrokerSnapshot`` / snapshots already produced by the M02-M05
modules this file reads from.

ROLE: after a restart, join four independent sources of truth --

    lifecycle store (M02-P1C ``LifecycleIdentityStore``)
    order-intent book (M04 ``OrderIntentBook``)
    PnL ledger (M05 ``PnlLedger``)
    broker reality (an INJECTED ``BrokerSnapshot`` fixture, NEVER MT5)

-- and produce a single, deterministic, typed anomaly report. ``Reconciler``
is PURE DETECTION ONLY: it never mutates any input and never corrects an
anomaly (it only types the anomaly and attaches a SUGGESTED action as text --
that text is never executed by this module or by anything it calls). A
future mission owns correction.

``store`` INPUT SHAPE -- neither ``LifecycleIdentityStore`` nor
``OrderIntentBook`` exposes a "dump everything" method (by design: M02/M04
are dormant labs with no such API, and this module MUST NOT add one --
touching those files is out of scope for M06). ``Reconciler`` therefore
never holds a live store/book/ledger instance; callers (typically
``RestartSimulator``, or production code in a later mission) assemble plain,
JSON-shaped snapshots first:

    store = {
        "lifecycles": {lifecycle_id: record, ...},   # same shape as every
                                                       # LifecycleIdentityStore
                                                       # record (dict) --
                                                       # gathered by the
                                                       # caller via repeated
                                                       # ``store.get_lifecycle``
        "journal_rebuilt": {"lifecycles": {...}} | None,  # OPTIONAL: the
                                                       # same shape, produced
                                                       # by replaying a
                                                       # PARALLEL M03 journal
                                                       # through
                                                       # ``event_journal.
                                                       # rebuild_lifecycle_state``.
                                                       # ``None`` disables
                                                       # STORE_JOURNAL_DIVERGENCE.
    }
    intent_book_state = {"intents": [snapshot, ...]}  # ALL intents
                                                       # (terminal states
                                                       # included -- unlike
                                                       # ``active_intents()``),
                                                       # each shaped like
                                                       # ``OrderIntentBook.get()``.
    ledger_state = PnlLedger.snapshot()               # exact M05 shape.

ANOMALY TAXONOMY (each entry: ``{type, severity, suggested_action, ...}``,
``severity`` in ``{CRITICAL, WARN, INFO}``):

- ``ORPHAN_BROKER_POSITION`` -- a broker position with no OPEN lifecycle
  bound to it. Matching is by the FULL composite broker reference (account
  scope + ticket + position_identifier + symbol + opened_at + magic +
  direction), reusing ``lifecycle_identity_store.try_build_broker_ref`` /
  ``broker_ref_key`` -- the ticket is NEVER sufficient alone.
- ``UNMATCHABLE_BROKER_POSITION`` -- a position with NO
  ``position_identifier`` whose ticket is ALSO used by a DIFFERENT bound
  OPEN lifecycle that DOES carry a ``position_identifier`` (a recycled
  ticket): the discriminants on hand are insufficient to safely claim
  "orphan" -- reported as a distinct, honest anomaly instead of guessing.
- ``STALE_OPEN_LIFECYCLE`` -- an OPEN, BOUND lifecycle with no matching
  broker position AND no matching EXIT deal anywhere in
  ``snapshot.recent_deals`` (reuses ``lifecycle_pnl.LifecyclePnl.compute``
  for the deal-matching side, never reimplemented). An OPEN, UNBOUND
  lifecycle is not evaluated by this check (nothing to look up yet --
  ``ORPHAN_BROKER_POSITION`` is what surfaces that story, from the other
  side, once the broker DOES show a position for it).
- ``INTENT_STUCK_SUBMITTED`` -- any intent still in state ``SUBMITTED``
  (the only genuinely "in flight" state -- see ``order_intent``'s state
  machine). ``age_seconds`` is computed against the injected ``as_of_utc``
  when ``created_at_utc`` is present and parseable, else ``None``.
- ``STORE_JOURNAL_DIVERGENCE`` -- ``store["lifecycles"]`` (the durable
  store's own state) disagrees, for any lifecycle_id, with
  ``store["journal_rebuilt"]["lifecycles"]`` (a parallel M03 journal replay
  of the same operations). ANY difference is CRITICAL: the two are supposed
  to be the exact same history seen from two angles. Skipped entirely when
  ``journal_rebuilt`` is ``None``.
- ``LEDGER_LIFECYCLE_GAP`` -- a CLOSED lifecycle with zero ledger entries
  (``subtype="CLOSED_NO_PNL"``, WARN) or a ledger entry whose lifecycle_id
  is absent from the store (``subtype="PNL_NO_LIFECYCLE"``, CRITICAL). This
  reuses (never reimplements) ``lifecycle_pnl.reconcile_intent_lifecycle_pnl``
  for the underlying cross-layer join and re-types its
  ``LIFECYCLE_CLOSED_SANS_PNL`` / ``PNL_SANS_LIFECYCLE`` findings under this
  M06 name. That same join also contributes its OTHER anomaly types
  unchanged: ``INTENT_FILLED_SANS_LIFECYCLE``, ``INTENT_LIFECYCLE_UNKNOWN``,
  ``VOLUME_MISMATCH``.

A fully consistent input set produces ``anomalies=[]`` and
``verdict="CLEAN"``. Otherwise ``verdict`` is ``"CRITICAL"`` if any anomaly
is CRITICAL, else ``"WARN"``.

DETERMINISM: ``reconcile()`` never reads a clock, never uses randomness, and
sorts its anomaly list by canonical JSON representation before returning --
so two calls with the SAME (possibly differently-ordered-in-memory) inputs
always produce byte-identical output, including a ``digest`` field
(``sha256`` hex, first 16 chars, of the canonical JSON of the report with
the ``digest`` field itself excluded).

``RestartSimulator`` is a labor-saving harness for the SIX scenarios this
mission ships: it builds a small world with the REAL M02-M05 modules inside
a caller-supplied temp directory, closes every writer/instance (simulating a
process crash / restart), reopens fresh instances from the SAME files
(``LifecycleIdentityStore``, ``JournalReader`` + ``rebuild_lifecycle_state``,
``OrderIntentBook.rebuild_from_journal``, ``PnlLedger`` -- whose constructor
already replays its journal), assembles the plain-dict snapshots above, runs
``Reconciler.reconcile()`` and returns the report. No real restart, no MT5,
no network; every timestamp is a fixed, documented, 100% fictional
constant -- ``RestartSimulator`` reads no clock either. Its internal
``MonotonicUUID7Generator`` instances are seeded with a fixed clock/entropy
function so lifecycle/intent identifiers are themselves reproducible run to
run (a bonus on top of ``Reconciler``'s own determinism, not a requirement
of it).

PURITY CONTRACT (this module) -- NEVER:
- reads an environment variable, a ``.env`` file, the wall clock, or MT5;
- performs network I/O;
- calls the MT5 order-send or order-check API (even simulated);
- calls a WRITE method of any store it is handed (``create_lifecycle``,
  ``bind_broker_position``, ``mark_closed``, ``JournalWriter.append``,
  ``OrderIntentBook.create_intent``/``transition``,
  ``PnlLedger.compute_and_record``) from inside ``Reconciler.reconcile()``
  -- structurally impossible besides, since ``Reconciler`` never holds a
  live store/book/ledger reference, only the plain dict/dataclass snapshots
  documented above.
``RestartSimulator`` DOES call those write methods, but only during its own
"build the world" phase, strictly BEFORE the simulated restart -- never
during/after ``Reconciler.reconcile()``.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalReader, JournalWriter, rebuild_lifecycle_state
from app.services.lifecycle_identity_store import (
    LifecycleIdentityStore,
    broker_ref_key,
    try_build_broker_ref,
)
from app.services.lifecycle_pnl import (
    DEAL_KIND_EXIT,
    DealRecord,
    DealValidationError,
    LifecyclePnl,
    PnlLedger,
    reconcile_intent_lifecycle_pnl,
)
from app.services.order_intent import OrderIntentBook

# --------------------------------------------------------------------------- #
# Anomaly types / severities / verdicts
# --------------------------------------------------------------------------- #
ANOMALY_ORPHAN_BROKER_POSITION = "ORPHAN_BROKER_POSITION"
ANOMALY_UNMATCHABLE_BROKER_POSITION = "UNMATCHABLE_BROKER_POSITION"
ANOMALY_STALE_OPEN_LIFECYCLE = "STALE_OPEN_LIFECYCLE"
ANOMALY_INTENT_STUCK_SUBMITTED = "INTENT_STUCK_SUBMITTED"
ANOMALY_STORE_JOURNAL_DIVERGENCE = "STORE_JOURNAL_DIVERGENCE"
ANOMALY_LEDGER_LIFECYCLE_GAP = "LEDGER_LIFECYCLE_GAP"
# Passed through unchanged from ``reconcile_intent_lifecycle_pnl`` (M05).
ANOMALY_INTENT_FILLED_SANS_LIFECYCLE = "INTENT_FILLED_SANS_LIFECYCLE"
ANOMALY_INTENT_LIFECYCLE_UNKNOWN = "INTENT_LIFECYCLE_UNKNOWN"
ANOMALY_VOLUME_MISMATCH = "VOLUME_MISMATCH"
# ``reconcile_intent_lifecycle_pnl`` types re-typed into LEDGER_LIFECYCLE_GAP.
_M05_LEDGER_GAP_SUBTYPES = {
    "LIFECYCLE_CLOSED_SANS_PNL": "CLOSED_NO_PNL",
    "PNL_SANS_LIFECYCLE": "PNL_NO_LIFECYCLE",
}

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARN = "WARN"
SEVERITY_INFO = "INFO"

VERDICT_CLEAN = "CLEAN"
VERDICT_WARN = "WARN"
VERDICT_CRITICAL = "CRITICAL"

_SEVERITY_BY_TYPE = {
    ANOMALY_ORPHAN_BROKER_POSITION: SEVERITY_CRITICAL,
    ANOMALY_UNMATCHABLE_BROKER_POSITION: SEVERITY_WARN,
    ANOMALY_STALE_OPEN_LIFECYCLE: SEVERITY_CRITICAL,
    ANOMALY_INTENT_STUCK_SUBMITTED: SEVERITY_WARN,
    ANOMALY_STORE_JOURNAL_DIVERGENCE: SEVERITY_CRITICAL,
    ANOMALY_INTENT_FILLED_SANS_LIFECYCLE: SEVERITY_CRITICAL,
    ANOMALY_INTENT_LIFECYCLE_UNKNOWN: SEVERITY_WARN,
    ANOMALY_VOLUME_MISMATCH: SEVERITY_WARN,
}
_ACTION_BY_TYPE = {
    ANOMALY_ORPHAN_BROKER_POSITION: (
        "Investigate the broker position with no matching OPEN lifecycle; "
        "bind it to an existing lifecycle or open a new one manually before "
        "any automated action touches it."
    ),
    ANOMALY_UNMATCHABLE_BROKER_POSITION: (
        "The broker position has no position_identifier and its ticket is "
        "shared with a different bound lifecycle (recycled ticket); resolve "
        "manually from broker terminal history before trusting any match."
    ),
    ANOMALY_STALE_OPEN_LIFECYCLE: (
        "The lifecycle is OPEN but no broker position or closing deal "
        "accounts for it; verify against the broker terminal and close or "
        "rebind it manually."
    ),
    ANOMALY_INTENT_STUCK_SUBMITTED: (
        "The order intent has been SUBMITTED with no terminal outcome; "
        "check broker order status and transition it manually to "
        "FILLED/REJECTED/CANCELLED/EXPIRED."
    ),
    ANOMALY_STORE_JOURNAL_DIVERGENCE: (
        "The lifecycle store and its journal-rebuilt mirror disagree; treat "
        "the durable store as authoritative and inspect the journal for a "
        "torn or missing operation."
    ),
    ANOMALY_INTENT_FILLED_SANS_LIFECYCLE: (
        "The intent reports FILLED with no resolvable lifecycle; verify the "
        "fill against broker history and create/bind the missing lifecycle "
        "manually."
    ),
    ANOMALY_INTENT_LIFECYCLE_UNKNOWN: (
        "The intent links to a lifecycle_id absent from the store; verify "
        "the reference is not stale or corrupted."
    ),
    ANOMALY_VOLUME_MISMATCH: (
        "Multiple intents linked to the same lifecycle declare different "
        "volumes; reconcile the true filled volume from broker history."
    ),
}
_LEDGER_GAP_ACTION = {
    "CLOSED_NO_PNL": (
        "The lifecycle is CLOSED but has no PnL ledger entry; recompute and "
        "record its PnL from broker deal history."
    ),
    "PNL_NO_LIFECYCLE": (
        "The ledger has PnL entries for a lifecycle_id absent from the "
        "store; investigate for a corrupted or foreign ledger record."
    ),
}
_LEDGER_GAP_SEVERITY = {"CLOSED_NO_PNL": SEVERITY_WARN, "PNL_NO_LIFECYCLE": SEVERITY_CRITICAL}

REPORT_SCHEMA_NAME = "hermes.reconciliation_report"
REPORT_SCHEMA_VERSION = 1

_STORE_FIELDS = frozenset({"lifecycles", "journal_rebuilt"})
_JOURNAL_REBUILT_FIELDS = frozenset({"lifecycles"})
_POSITION_FIELDS = frozenset({
    "ticket", "position_identifier", "symbol", "direction", "volume", "opened_at", "magic",
})
_SNAPSHOT_REQUIRED_FIELDS = frozenset({"account_scope_id", "positions"})
_SNAPSHOT_OPTIONAL_FIELDS = frozenset({"recent_deals"})
_SNAPSHOT_ALL_FIELDS = _SNAPSHOT_REQUIRED_FIELDS | _SNAPSHOT_OPTIONAL_FIELDS
_DIRECTIONS = frozenset({"BUY", "SELL"})


# --------------------------------------------------------------------------- #
# Exceptions (sanitized: reason_code only, never a raw value/secret)
# --------------------------------------------------------------------------- #
class ReconciliationLabError(Exception):
    """Base. ``reason_code`` = sanitized machine diagnostic."""

    reason_code = "RECONCILIATION_LAB_ERROR"

    def __init__(self, reason_code: str = "RECONCILIATION_LAB_ERROR"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class SnapshotValidationError(ReconciliationLabError):
    reason_code = "SNAPSHOT_VALIDATION_FAILED"


class ReconciliationInputError(ReconciliationLabError):
    reason_code = "RECONCILIATION_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Strict, side-effect-free coercion helpers (never a silent conversion)
# --------------------------------------------------------------------------- #
def _strict_int(value: object) -> bool:
    return type(value) is int  # bool excluded: type(True) is bool, not int


def _coerce_positive_decimal(value: object, field_name: str) -> Decimal:
    """Same discipline as ``lifecycle_pnl._coerce_decimal`` plus a ``> 0``
    floor: ``Decimal``/non-bool ``int``/non-empty ``str`` accepted, ``float``
    and ``bool`` rejected outright (never a float round-trip into money)."""
    if isinstance(value, bool):
        raise SnapshotValidationError("%s_INVALID" % field_name)
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        if not value.strip():
            raise SnapshotValidationError("%s_INVALID" % field_name)
        try:
            d = Decimal(value)
        except InvalidOperation:
            raise SnapshotValidationError("%s_INVALID" % field_name) from None
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        raise SnapshotValidationError("%s_INVALID" % field_name)
    if not d.is_finite() or d <= 0:
        raise SnapshotValidationError("%s_INVALID" % field_name)
    return d


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


def _canonical_iso_utc(value: object, field_name: str) -> str:
    dt = _parse_iso_utc(value)
    if dt is None:
        raise SnapshotValidationError("%s_INVALID" % field_name)
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# BrokerSnapshot -- strict, INJECTED (never MT5)
# --------------------------------------------------------------------------- #
def _validate_position(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise SnapshotValidationError("POSITION_NOT_DICT")
    keys = set(raw.keys())
    missing = _POSITION_FIELDS - keys
    if missing:
        raise SnapshotValidationError("POSITION_MISSING_FIELDS:%s" % ",".join(sorted(missing)))
    unknown = keys - _POSITION_FIELDS
    if unknown:
        raise SnapshotValidationError("POSITION_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))

    ticket = raw["ticket"]
    if not _strict_int(ticket) or ticket <= 0:
        raise SnapshotValidationError("TICKET_INVALID")

    position_identifier = raw["position_identifier"]
    if position_identifier is not None and (
        not _strict_int(position_identifier) or position_identifier <= 0
    ):
        raise SnapshotValidationError("POSITION_IDENTIFIER_INVALID")

    symbol = raw["symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise SnapshotValidationError("SYMBOL_INVALID")
    symbol = symbol.strip().upper()

    direction = raw["direction"]
    if direction not in _DIRECTIONS:
        raise SnapshotValidationError("DIRECTION_INVALID")

    volume = _coerce_positive_decimal(raw["volume"], "VOLUME")
    opened_at = _canonical_iso_utc(raw["opened_at"], "OPENED_AT")

    magic = raw["magic"]
    if not _strict_int(magic) or magic < 0:
        raise SnapshotValidationError("MAGIC_INVALID")

    return {
        "ticket": ticket,
        "position_identifier": position_identifier,
        "symbol": symbol,
        "direction": direction,
        "volume": volume,
        "opened_at": opened_at,
        "magic": magic,
    }


@dataclass(frozen=True)
class BrokerSnapshot:
    """Strictly-validated, INJECTED broker reality (never fetched from MT5
    by this module). ``recent_deals`` reuses ``lifecycle_pnl.DealRecord``'s
    exact validation rules -- not reimplemented here."""

    account_scope_id: str
    positions: "tuple[dict, ...]"
    recent_deals: "tuple[DealRecord, ...]"

    @classmethod
    def from_mapping(cls, raw: object) -> "BrokerSnapshot":
        if not isinstance(raw, dict):
            raise SnapshotValidationError("SNAPSHOT_NOT_DICT")
        keys = set(raw.keys())
        missing = _SNAPSHOT_REQUIRED_FIELDS - keys
        if missing:
            raise SnapshotValidationError("SNAPSHOT_MISSING_FIELDS:%s" % ",".join(sorted(missing)))
        unknown = keys - _SNAPSHOT_ALL_FIELDS
        if unknown:
            raise SnapshotValidationError("SNAPSHOT_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))

        account_scope_id = raw["account_scope_id"]
        if not isinstance(account_scope_id, str) or not account_scope_id.startswith("acct-v1-"):
            raise SnapshotValidationError("ACCOUNT_SCOPE_ID_INVALID")

        positions_raw = raw["positions"]
        if not isinstance(positions_raw, list):
            raise SnapshotValidationError("POSITIONS_NOT_LIST")
        positions = tuple(_validate_position(p) for p in positions_raw)

        deals_raw = raw.get("recent_deals")
        if deals_raw is None:
            deals: "tuple[DealRecord, ...]" = ()
        else:
            if not isinstance(deals_raw, list):
                raise SnapshotValidationError("RECENT_DEALS_NOT_LIST")
            built = []
            for d in deals_raw:
                if isinstance(d, DealRecord):
                    built.append(d)
                    continue
                try:
                    built.append(DealRecord.from_mapping(d))
                except DealValidationError as exc:
                    raise SnapshotValidationError("RECENT_DEAL_INVALID:%s" % exc.reason_code) from exc
            deals = tuple(built)

        return cls(account_scope_id=account_scope_id, positions=positions, recent_deals=deals)


# --------------------------------------------------------------------------- #
# JSON canonicalization + digest
# --------------------------------------------------------------------------- #
def _json_default(obj: object):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError("not JSON-canonicalizable: %r" % type(obj))


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _digest16(obj: object) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()[:16]


def _anomaly(type_: str, severity: str, suggested_action: str, **fields: object) -> dict:
    out = {"type": type_, "severity": severity, "suggested_action": suggested_action}
    out.update(fields)
    return out


# --------------------------------------------------------------------------- #
# Reconciler -- PURE, detection only, never mutates/writes anything
# --------------------------------------------------------------------------- #
class Reconciler:
    """PURE, read-only join of store + intents + ledger + broker snapshot.
    See module docstring for the full contract and the ``store`` input
    shape. Never holds a live store/book/ledger instance -- only plain
    dicts/dataclasses -- so it is structurally incapable of writing
    anywhere."""

    def __init__(
        self,
        store: dict,
        intent_book_state: dict,
        ledger_state: dict,
        snapshot: BrokerSnapshot,
        *,
        as_of_utc: str,
    ) -> None:
        if not isinstance(store, dict) or set(store.keys()) != _STORE_FIELDS:
            raise ReconciliationInputError("STORE_STATE_INVALID")
        lifecycles = store["lifecycles"]
        if not isinstance(lifecycles, dict):
            raise ReconciliationInputError("STORE_LIFECYCLES_INVALID")
        journal_rebuilt = store["journal_rebuilt"]
        if journal_rebuilt is not None:
            if not isinstance(journal_rebuilt, dict) or set(journal_rebuilt.keys()) != _JOURNAL_REBUILT_FIELDS:
                raise ReconciliationInputError("JOURNAL_REBUILT_INVALID")
            if not isinstance(journal_rebuilt["lifecycles"], dict):
                raise ReconciliationInputError("JOURNAL_REBUILT_LIFECYCLES_INVALID")

        if not isinstance(intent_book_state, dict) or "intents" not in intent_book_state:
            raise ReconciliationInputError("INTENT_BOOK_STATE_INVALID")
        intents = intent_book_state["intents"]
        if not isinstance(intents, list):
            raise ReconciliationInputError("INTENTS_NOT_LIST")

        if not isinstance(ledger_state, dict) or "lifecycles" not in ledger_state:
            raise ReconciliationInputError("LEDGER_STATE_INVALID")
        ledger_lifecycles = ledger_state["lifecycles"]
        if not isinstance(ledger_lifecycles, dict):
            raise ReconciliationInputError("LEDGER_LIFECYCLES_INVALID")

        if not isinstance(snapshot, BrokerSnapshot):
            raise ReconciliationInputError("SNAPSHOT_NOT_BROKER_SNAPSHOT")

        if _parse_iso_utc(as_of_utc) is None:
            raise ReconciliationInputError("AS_OF_UTC_INVALID")

        self._lifecycles = lifecycles
        self._journal_rebuilt = journal_rebuilt
        self._intents = intents
        self._ledger_state = ledger_state
        self._ledger_lifecycles = ledger_lifecycles
        self._snapshot = snapshot
        self._as_of_utc = as_of_utc

    # ------------------------------------------------------------- helpers
    def _key_to_open_lifecycle(self) -> dict:
        """``broker_key -> lifecycle_id`` for OPEN lifecycles only -- derived
        straight from ``lifecycles`` (each record already carries its own
        ``broker_key``), never from a separate broker_index the store
        doesn't expose."""
        out = {}
        for lid, rec in self._lifecycles.items():
            if isinstance(rec, dict) and rec.get("state") == "OPEN" and rec.get("broker_key"):
                out[rec["broker_key"]] = lid
        return out

    def _position_ref(self, position: dict) -> "tuple[dict | None, str | None]":
        return try_build_broker_ref(
            account_scope_id=self._snapshot.account_scope_id,
            ticket=position["ticket"],
            broker_symbol=position["symbol"],
            opened_at=position["opened_at"],
            magic=position["magic"],
            position_identifier=position["position_identifier"],
            direction=position["direction"],
        )

    def _is_ambiguous_recycled_ticket(self, position: dict) -> bool:
        """A position with no ``position_identifier`` is UNMATCHABLE (not a
        confident ORPHAN) if a DIFFERENT bound OPEN lifecycle shares the
        same (ticket, symbol, magic, direction) but DOES carry a
        ``position_identifier`` -- the classic recycled-ticket case the
        composite reference exists to discriminate."""
        for rec in self._lifecycles.values():
            if not isinstance(rec, dict) or rec.get("state") != "OPEN":
                continue
            ref = rec.get("broker_ref")
            if not isinstance(ref, dict) or ref.get("position_identifier") is None:
                continue
            if (
                ref.get("ticket") == position["ticket"]
                and ref.get("broker_symbol") == position["symbol"]
                and ref.get("magic") == position["magic"]
                and ref.get("direction") == position["direction"]
            ):
                return True
        return False

    # ------------------------------------------------------------- checks
    def _check_broker_positions(self) -> "tuple[list[dict], set[str], set[tuple]]":
        anomalies: "list[dict]" = []
        matched_keys: "set[str]" = set()
        # (ticket, symbol, magic, direction) tuples covered by an HONEST
        # UNMATCHABLE finding -- a bound lifecycle sharing that tuple is
        # NOT also flagged STALE: the ambiguity is already reported once,
        # from the position's side, rather than twice for the same fact.
        ambiguous_tuples: "set[tuple]" = set()
        key_to_lid = self._key_to_open_lifecycle()
        for position in self._snapshot.positions:
            if position["position_identifier"] is None and self._is_ambiguous_recycled_ticket(position):
                ambiguous_tuples.add((
                    position["ticket"], position["symbol"], position["magic"], position["direction"],
                ))
                anomalies.append(_anomaly(
                    ANOMALY_UNMATCHABLE_BROKER_POSITION,
                    _SEVERITY_BY_TYPE[ANOMALY_UNMATCHABLE_BROKER_POSITION],
                    _ACTION_BY_TYPE[ANOMALY_UNMATCHABLE_BROKER_POSITION],
                    ticket=position["ticket"], symbol=position["symbol"],
                    opened_at=position["opened_at"],
                ))
                continue
            ref, reason = self._position_ref(position)
            if ref is None:
                # Structurally unbuildable (should not happen -- the
                # position already passed strict validation) -- still
                # honest rather than silently dropped.
                anomalies.append(_anomaly(
                    ANOMALY_UNMATCHABLE_BROKER_POSITION,
                    _SEVERITY_BY_TYPE[ANOMALY_UNMATCHABLE_BROKER_POSITION],
                    _ACTION_BY_TYPE[ANOMALY_UNMATCHABLE_BROKER_POSITION],
                    ticket=position["ticket"], symbol=position["symbol"], reason=reason,
                ))
                continue
            key = broker_ref_key(ref)
            lid = key_to_lid.get(key)
            if lid is not None:
                matched_keys.add(key)
                continue
            anomalies.append(_anomaly(
                ANOMALY_ORPHAN_BROKER_POSITION,
                _SEVERITY_BY_TYPE[ANOMALY_ORPHAN_BROKER_POSITION],
                _ACTION_BY_TYPE[ANOMALY_ORPHAN_BROKER_POSITION],
                ticket=position["ticket"], position_identifier=position["position_identifier"],
                symbol=position["symbol"], opened_at=position["opened_at"],
            ))
        return anomalies, matched_keys, ambiguous_tuples

    def _check_stale_open_lifecycles(self, matched_keys: "set[str]", ambiguous_tuples: "set[tuple]") -> "list[dict]":
        anomalies: "list[dict]" = []
        deals = list(self._snapshot.recent_deals)
        for lid, rec in self._lifecycles.items():
            if not isinstance(rec, dict) or rec.get("state") != "OPEN":
                continue
            if rec.get("broker_ref") is None:
                continue  # unbound OPEN -- nothing to look up (see docstring)
            if rec.get("broker_key") in matched_keys:
                continue  # healthy, currently open at the broker
            ref = rec["broker_ref"]
            ref_tuple = (ref["ticket"], ref["broker_symbol"], ref["magic"], ref["direction"])
            if ref_tuple in ambiguous_tuples:
                continue  # already reported once, honestly, as UNMATCHABLE
            pnl = LifecyclePnl.compute(rec, deals)
            has_exit = any(t["kind"] == DEAL_KIND_EXIT for t in pnl["timeline"])
            if has_exit:
                continue  # closing deal seen -- a different story (ledger gap)
            anomalies.append(_anomaly(
                ANOMALY_STALE_OPEN_LIFECYCLE,
                _SEVERITY_BY_TYPE[ANOMALY_STALE_OPEN_LIFECYCLE],
                _ACTION_BY_TYPE[ANOMALY_STALE_OPEN_LIFECYCLE],
                lifecycle_id=lid,
            ))
        return anomalies

    def _check_intents_stuck(self) -> "list[dict]":
        anomalies: "list[dict]" = []
        as_of = _parse_iso_utc(self._as_of_utc)
        for intent in self._intents:
            if not isinstance(intent, dict) or intent.get("state") != "SUBMITTED":
                continue
            created_at = _parse_iso_utc(intent.get("created_at_utc"))
            age_seconds = None
            if created_at is not None and as_of is not None:
                age_seconds = (as_of - created_at).total_seconds()
            anomalies.append(_anomaly(
                ANOMALY_INTENT_STUCK_SUBMITTED,
                _SEVERITY_BY_TYPE[ANOMALY_INTENT_STUCK_SUBMITTED],
                _ACTION_BY_TYPE[ANOMALY_INTENT_STUCK_SUBMITTED],
                intent_id=intent.get("intent_id"), age_seconds=age_seconds,
            ))
        return anomalies

    def _check_store_journal_divergence(self) -> "list[dict]":
        if self._journal_rebuilt is None:
            return []
        anomalies: "list[dict]" = []
        journal_lifecycles = self._journal_rebuilt["lifecycles"]
        all_ids = set(self._lifecycles) | set(journal_lifecycles)
        for lid in all_ids:
            left = self._lifecycles.get(lid)
            right = journal_lifecycles.get(lid)
            if _canonical_json(left) != _canonical_json(right):
                anomalies.append(_anomaly(
                    ANOMALY_STORE_JOURNAL_DIVERGENCE,
                    _SEVERITY_BY_TYPE[ANOMALY_STORE_JOURNAL_DIVERGENCE],
                    _ACTION_BY_TYPE[ANOMALY_STORE_JOURNAL_DIVERGENCE],
                    lifecycle_id=lid,
                    in_store=left is not None, in_journal_rebuilt=right is not None,
                ))
        return anomalies

    def _check_cross_layer_join(self) -> "list[dict]":
        joined = reconcile_intent_lifecycle_pnl(
            {"intents": self._intents},
            {"lifecycles": self._lifecycles},
            self._ledger_state,
        )
        anomalies: "list[dict]" = []
        for a in joined["anomalies"]:
            m05_type = a.get("type")
            if m05_type in _M05_LEDGER_GAP_SUBTYPES:
                subtype = _M05_LEDGER_GAP_SUBTYPES[m05_type]
                rest = {k: v for k, v in a.items() if k != "type"}
                anomalies.append(_anomaly(
                    ANOMALY_LEDGER_LIFECYCLE_GAP,
                    _LEDGER_GAP_SEVERITY[subtype],
                    _LEDGER_GAP_ACTION[subtype],
                    subtype=subtype, **rest,
                ))
            elif m05_type in _SEVERITY_BY_TYPE:
                rest = {k: v for k, v in a.items() if k != "type"}
                anomalies.append(_anomaly(
                    m05_type, _SEVERITY_BY_TYPE[m05_type], _ACTION_BY_TYPE[m05_type], **rest,
                ))
            # else: unknown M05 anomaly type -- structurally impossible given
            # M05's own fixed taxonomy; never silently dropped in practice,
            # but also never allowed to crash a PURE detector.
        return anomalies

    # ------------------------------------------------------------- API
    def reconcile(self) -> dict:
        broker_anomalies, matched_keys, ambiguous_tuples = self._check_broker_positions()
        anomalies: "list[dict]" = []
        anomalies.extend(broker_anomalies)
        anomalies.extend(self._check_stale_open_lifecycles(matched_keys, ambiguous_tuples))
        anomalies.extend(self._check_intents_stuck())
        anomalies.extend(self._check_store_journal_divergence())
        anomalies.extend(self._check_cross_layer_join())

        # Deterministic ordering regardless of input dict iteration order.
        anomalies.sort(key=_canonical_json)

        counts: "dict[str, int]" = {}
        for a in anomalies:
            counts[a["type"]] = counts.get(a["type"], 0) + 1

        if any(a["severity"] == SEVERITY_CRITICAL for a in anomalies):
            verdict = VERDICT_CRITICAL
        elif anomalies:
            verdict = VERDICT_WARN
        else:
            verdict = VERDICT_CLEAN

        report = {
            "schema": REPORT_SCHEMA_NAME,
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_as_of_utc": self._as_of_utc,
            "verdict": verdict,
            "anomalies": anomalies,
            "counts": counts,
            "checked": {
                "positions": len(self._snapshot.positions),
                "recent_deals": len(self._snapshot.recent_deals),
                "lifecycles": len(self._lifecycles),
                "intents": len(self._intents),
                "ledger_lifecycles": len(self._ledger_lifecycles),
            },
        }
        report["digest"] = _digest16(report)
        return report


# --------------------------------------------------------------------------- #
# RestartSimulator -- labo harness, real M02-M05 modules, no real restart
# --------------------------------------------------------------------------- #
SCENARIO_CLEAN_SHUTDOWN = "clean_shutdown"
SCENARIO_CRASH_AFTER_FILL_BEFORE_BIND = "crash_after_fill_before_bind"
SCENARIO_CRASH_BETWEEN_CLOSE_AND_PNL = "crash_between_close_and_pnl"
SCENARIO_JOURNAL_TAIL_TORN = "journal_tail_torn"
SCENARIO_BROKER_POSITION_UNKNOWN = "broker_position_unknown"
SCENARIO_INTENT_SUBMITTED_NO_OUTCOME = "intent_submitted_no_outcome"

_ACCOUNT_SCOPE = "acct-v1-" + "f6" * 16
_MAGIC = 909333
_T_OPEN = "2026-07-22T03:00:00+00:00"
_T_FILL = "2026-07-22T03:00:01+00:00"
_T_EXIT = "2026-07-22T03:15:00+00:00"
_T_CLOSE = "2026-07-22T03:15:01+00:00"
_T_SUBMIT = "2026-07-22T03:00:00+00:00"
_T_AS_OF = "2026-07-22T03:20:00+00:00"
_FIXED_CLOCK_MS = 1784350800000  # 100% fictional, fixed -- never a real wall-clock read


def _fixed_uuid_generator() -> MonotonicUUID7Generator:
    return MonotonicUUID7Generator(clock_ms=lambda: _FIXED_CLOCK_MS, random_bits=lambda n: 0)


def _intent_request(**overrides) -> dict:
    base = {"symbol": "GOLD#", "direction": "BUY", "volume": 0.10, "magic": _MAGIC}
    base.update(overrides)
    return base


def _deal(deal_id, position_id, ticket, kind, at_utc, price="2000.00", profit="0", volume="0.10"):
    return DealRecord.from_mapping({
        "deal_id": deal_id, "position_id": position_id, "ticket": ticket, "kind": kind,
        "volume": volume, "price": price, "profit": profit, "commission": "0", "swap": "0",
        "fee": "0", "at_utc": at_utc, "symbol": "GOLD#",
    })


def _position(ticket, position_identifier, opened_at=_T_OPEN, direction="BUY"):
    return {
        "ticket": ticket, "position_identifier": position_identifier, "symbol": "GOLD#",
        "direction": direction, "volume": "0.10", "opened_at": opened_at, "magic": _MAGIC,
    }


class RestartSimulator:
    """See module docstring. Every scenario builds a small world with the
    REAL M02/M03/M04/M05 modules, tears every writer/instance down, reopens
    fresh instances from disk, and runs ``Reconciler`` against the result."""

    SCENARIO_NAMES = frozenset({
        SCENARIO_CLEAN_SHUTDOWN,
        SCENARIO_CRASH_AFTER_FILL_BEFORE_BIND,
        SCENARIO_CRASH_BETWEEN_CLOSE_AND_PNL,
        SCENARIO_JOURNAL_TAIL_TORN,
        SCENARIO_BROKER_POSITION_UNKNOWN,
        SCENARIO_INTENT_SUBMITTED_NO_OUTCOME,
    })

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ------------------------------------------------------------- mirror
    @staticmethod
    def _do_create(store: LifecycleIdentityStore, mirror: JournalWriter, *, created_at_utc: str) -> dict:
        rec = store.create_lifecycle(correlation_id=None, created_at_utc=created_at_utc)
        mirror.append({
            "op": "create", "lifecycle_id": rec["lifecycle_id"], "correlation_id": rec["correlation_id"],
            "created_at_utc": rec["created_at_utc"], "boot_id": rec["boot_id"],
            "cycle_id": rec["cycle_id"], "setup_id": rec["setup_id"],
        })
        return rec

    @staticmethod
    def _do_bind(store: LifecycleIdentityStore, mirror: JournalWriter, lifecycle_id: str, ref: dict) -> None:
        store.bind_broker_position(lifecycle_id, ref)
        mirror.append({"op": "bind", "lifecycle_id": lifecycle_id, "ref": ref})

    @staticmethod
    def _do_close(store: LifecycleIdentityStore, mirror: JournalWriter, lifecycle_id: str, closed_at_utc: str) -> None:
        store.mark_closed(lifecycle_id, closed_at_utc=closed_at_utc)
        mirror.append({"op": "mark_closed", "lifecycle_id": lifecycle_id, "closed_at_utc": closed_at_utc})

    @staticmethod
    def _tear_last_line(path: Path) -> None:
        """Simulates a crash mid-``append``: cuts the LAST journal line
        short and drops its trailing newline, exactly what
        ``event_journal``'s own ``TORN_TAIL_QUARANTINED`` recovery path is
        built to detect and quarantine on the next open/replay."""
        raw = path.read_bytes()
        assert raw.endswith(b"\n")
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        last = lines.pop()
        torn = last[: max(1, len(last) // 2)]
        new_raw = b"\n".join(lines) + (b"\n" if lines else b"") + torn
        path.write_bytes(new_raw)

    # ------------------------------------------------------------- API
    def run_scenario(self, tmpdir: "Path | str", scenario_spec: str) -> dict:
        if scenario_spec not in self.SCENARIO_NAMES:
            raise ReconciliationLabError("UNKNOWN_SCENARIO:%s" % scenario_spec)
        builder = getattr(self, "_scenario_" + scenario_spec)
        with self._lock:
            return builder(Path(tmpdir))

    # ------------------------------------------------------------- scenarios
    def _paths(self, root: Path) -> dict:
        return {
            "store": root / "lifecycle_store.json",
            "journal": root / "lifecycle_journal.jsonl",
            "intents": root / "order_intent.jsonl",
            "ledger": root / "pnl_ledger.jsonl",
        }

    def _restart_and_reconcile(
        self, paths: dict, *, lifecycle_ids: list, intent_ids: list,
        snapshot: BrokerSnapshot, as_of_utc: str,
    ) -> dict:
        """Phase 3: reopen fresh instances from disk only (no in-memory
        state survives), gather plain-dict snapshots, run the pure
        ``Reconciler``."""
        fresh_store = LifecycleIdentityStore(path=paths["store"])
        lifecycles = {lid: fresh_store.get_lifecycle(lid) for lid in lifecycle_ids}
        rebuilt = rebuild_lifecycle_state(JournalReader(paths["journal"]))
        journal_rebuilt = {"lifecycles": rebuilt["lifecycles"]}

        fresh_book = OrderIntentBook.rebuild_from_journal(
            paths["intents"], uuid_generator=_fixed_uuid_generator())
        try:
            intents = [fresh_book.get(iid) for iid in intent_ids]
        finally:
            fresh_book.close()

        fresh_ledger = PnlLedger(paths["ledger"])
        try:
            ledger_state = fresh_ledger.snapshot()
        finally:
            fresh_ledger.close()

        store_state = {"lifecycles": lifecycles, "journal_rebuilt": journal_rebuilt}
        reconciler = Reconciler(
            store_state, {"intents": intents}, ledger_state, snapshot, as_of_utc=as_of_utc)
        return reconciler.reconcile()

    def _scenario_clean_shutdown(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])

        rec = self._do_create(store, mirror, created_at_utc=_T_OPEN)
        lid = rec["lifecycle_id"]
        ref, reason = try_build_broker_ref(
            account_scope_id=_ACCOUNT_SCOPE, ticket=100001, broker_symbol="GOLD#",
            opened_at=_T_OPEN, magic=_MAGIC, position_identifier=200001, direction="BUY")
        assert ref is not None, reason
        self._do_bind(store, mirror, lid, ref)

        deals = [
            _deal(1, 200001, 100001, "ENTRY", _T_OPEN),
            _deal(2, 200001, 100001, "EXIT", _T_EXIT, price="2015.00", profit="15.00"),
        ]
        pnl_result = ledger.compute_and_record(store.get_lifecycle(lid), deals)
        assert pnl_result["ledger_status"] == "RECORDED"
        self._do_close(store, mirror, lid, _T_CLOSE)

        intent = book.create_intent(_intent_request(), created_at_utc=_T_SUBMIT,
                                     links={"lifecycle_id": lid, "correlation_id": None, "setup_id": None})
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=_T_SUBMIT)
        book.transition(iid, "FILLED", at_utc=_T_FILL)

        mirror.close()
        book.close()
        ledger.close()

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE, "positions": [], "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[lid], intent_ids=[iid], snapshot=snapshot, as_of_utc=_T_AS_OF)

    def _scenario_crash_after_fill_before_bind(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])

        rec = self._do_create(store, mirror, created_at_utc=_T_OPEN)
        lid = rec["lifecycle_id"]
        # CRASH HERE: broker fill happened, bind_broker_position() never ran.

        intent = book.create_intent(_intent_request(), created_at_utc=_T_SUBMIT,
                                     links={"lifecycle_id": lid, "correlation_id": None, "setup_id": None})
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=_T_SUBMIT)
        book.transition(iid, "FILLED", at_utc=_T_FILL)

        mirror.close()
        book.close()
        ledger.close()

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE,
            "positions": [_position(100002, 200002)],
            "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[lid], intent_ids=[iid], snapshot=snapshot, as_of_utc=_T_AS_OF)

    def _scenario_crash_between_close_and_pnl(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])

        rec = self._do_create(store, mirror, created_at_utc=_T_OPEN)
        lid = rec["lifecycle_id"]
        ref, reason = try_build_broker_ref(
            account_scope_id=_ACCOUNT_SCOPE, ticket=100003, broker_symbol="GOLD#",
            opened_at=_T_OPEN, magic=_MAGIC, position_identifier=200003, direction="BUY")
        assert ref is not None, reason
        self._do_bind(store, mirror, lid, ref)
        self._do_close(store, mirror, lid, _T_CLOSE)
        # CRASH HERE: ledger.compute_and_record() never ran.

        intent = book.create_intent(_intent_request(), created_at_utc=_T_SUBMIT,
                                     links={"lifecycle_id": lid, "correlation_id": None, "setup_id": None})
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=_T_SUBMIT)
        book.transition(iid, "FILLED", at_utc=_T_FILL)

        mirror.close()
        book.close()
        ledger.close()

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE, "positions": [], "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[lid], intent_ids=[iid], snapshot=snapshot, as_of_utc=_T_AS_OF)

    def _scenario_journal_tail_torn(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])

        rec = self._do_create(store, mirror, created_at_utc=_T_OPEN)
        lid = rec["lifecycle_id"]
        ref, reason = try_build_broker_ref(
            account_scope_id=_ACCOUNT_SCOPE, ticket=100004, broker_symbol="GOLD#",
            opened_at=_T_OPEN, magic=_MAGIC, position_identifier=200004, direction="BUY")
        assert ref is not None, reason
        self._do_bind(store, mirror, lid, ref)

        deals = [
            _deal(1, 200004, 100004, "ENTRY", _T_OPEN),
            _deal(2, 200004, 100004, "EXIT", _T_EXIT, price="2015.00", profit="15.00"),
        ]
        ledger.compute_and_record(store.get_lifecycle(lid), deals)
        # Real store IS closed durably; the mirror journal's "mark_closed"
        # line is written too, then TORN before the restart, simulating a
        # crash that completed the store write but not the journal mirror.
        self._do_close(store, mirror, lid, _T_CLOSE)

        intent = book.create_intent(_intent_request(), created_at_utc=_T_SUBMIT,
                                     links={"lifecycle_id": lid, "correlation_id": None, "setup_id": None})
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=_T_SUBMIT)
        book.transition(iid, "FILLED", at_utc=_T_FILL)

        mirror.close()
        book.close()
        ledger.close()

        self._tear_last_line(paths["journal"])

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE, "positions": [], "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[lid], intent_ids=[iid], snapshot=snapshot, as_of_utc=_T_AS_OF)

    def _scenario_broker_position_unknown(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])
        mirror.close()
        book.close()
        ledger.close()

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE,
            "positions": [_position(100005, 200005)],
            "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[], intent_ids=[], snapshot=snapshot, as_of_utc=_T_AS_OF)

    def _scenario_intent_submitted_no_outcome(self, root: Path) -> dict:
        paths = self._paths(root)
        store = LifecycleIdentityStore(path=paths["store"])
        mirror = JournalWriter(path=paths["journal"])
        book = OrderIntentBook(paths["intents"], uuid_generator=_fixed_uuid_generator())
        ledger = PnlLedger(paths["ledger"])

        intent = book.create_intent(_intent_request(), created_at_utc=_T_SUBMIT, links=None)
        iid = intent["intent_id"]
        book.transition(iid, "SUBMITTED", at_utc=_T_SUBMIT)
        # CRASH HERE: no FILLED/REJECTED/CANCELLED/EXPIRED ever arrived.

        mirror.close()
        book.close()
        ledger.close()

        snapshot = BrokerSnapshot.from_mapping({
            "account_scope_id": _ACCOUNT_SCOPE, "positions": [], "recent_deals": [],
        })
        return self._restart_and_reconcile(
            paths, lifecycle_ids=[], intent_ids=[iid], snapshot=snapshot, as_of_utc=_T_AS_OF)
