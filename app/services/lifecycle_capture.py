"""LIFECYCLE_CAPTURE — DORMANT hook service, broker discriminant capture
(M02-P1D-P2, plan P1D-P0 points d/e).

LABORATOIRE / DORMANT : this module is imported by NO production runtime yet.
`app/mt5/demo_router.py` exposes a single confined hook (`attach_lifecycle_
capture` / the `self._lifecycle_capture` guard in `_record_event`) that stays
None by default — attaching an instance of `LifecycleCaptureService` is
reserved for a future, separate activation mission. As long as nothing
attaches it, `_record_event` is byte-identical to legacy.

ROLE : turn the same demo_router execution events that already flow through
`_record_event` into writes against a durable `LifecycleIdentityStore`
(M02-P1C): DEMO_ORDER fills create + bind a lifecycle to a strict broker
reference; close events (EXIT_V2_CLOSE / POSITION_SYNC / QUICK_EXIT_* /
RESCUE_CLOSE) resolve that same reference and mark the lifecycle CLOSED. The
broker reference is ALWAYS the composite one built by
``lifecycle_identity_store.try_build_broker_ref`` — the ticket alone is never
sufficient (recycled tickets are discriminated by position_identifier and/or
opened_at, both sourced from the injected ``position_lookup``).

PURITY CONTRACT — this module NEVER:
- imports MT5, demo_router, or anything network/broker-facing;
- reads an environment variable or a file directly (the store does its own
  disk I/O; this module only calls its public API);
- reads a wall clock (``clock_utc_iso`` is an injected callable — no hidden
  ``datetime.now()``);
- derives the ``account_scope_id`` (a pseudonym) itself — it is injected,
  already built by the account-scope derivation owned by a separate mission.

FAIL-SILENT BY DESIGN (toward the trading path): every public entry point
(``on_event``, the resolver returned by ``make_lifecycle_resolver``) never
raises. Internal failures are counted (sanitized: exception TYPE name only,
never a message) and exposed through ``diagnostics()``. Store conflicts that
are legitimate idempotent no-ops (ALREADY_BOUND / ALREADY_CLOSED) count as
success, not as errors.
"""
from __future__ import annotations

import threading
from typing import Callable

from app.services.lifecycle_identity_store import (
    LifecycleIdentityStore,
    LifecycleStoreError,
    try_build_broker_ref,
)

_OPEN_EVENT_TYPES = frozenset({"DEMO_ORDER"})
_CLOSE_EVENT_TYPES_EXACT = frozenset({"EXIT_V2_CLOSE", "POSITION_SYNC", "RESCUE_CLOSE"})
_CLOSE_EVENT_PREFIX = "QUICK_EXIT_"

_REASON_REF_BUILD_FAILED = "REF_BUILD_FAILED"
_REASON_RESOLVE_FAILED = "RESOLVE_FAILED"
_REASON_TICKET_MISSING = "TICKET_MISSING"
_REASON_EVENT_INVALID = "EVENT_INVALID"


def _is_close_event_type(event_type: str) -> bool:
    return event_type in _CLOSE_EVENT_TYPES_EXACT or event_type.startswith(_CLOSE_EVENT_PREFIX)


def _strict_ticket(value: object) -> "int | None":
    # bool excluded: type(True) is bool, never int here (same discipline as
    # the store's own _strict_int).
    if type(value) is not int or value <= 0:
        return None
    return value


def _clean_setup_id(value: object) -> "str | None":
    if isinstance(value, str) and value.strip():
        return value
    return None


def _clean_correlation_id(shadow: dict) -> "str | None":
    value = shadow.get("correlation_id")
    return value if isinstance(value, str) and value else None


def _clean_boot_id(shadow: dict) -> "str | None":
    value = shadow.get("boot_id")
    return value if isinstance(value, str) and value else None


def _clean_cycle_id(shadow: dict) -> "int | None":
    value = shadow.get("cycle_id")
    if type(value) is int and value >= 0:
        return value
    return None


class LifecycleCaptureService:
    """DORMANT capture service. See module docstring for the full contract."""

    def __init__(
        self,
        store: LifecycleIdentityStore,
        *,
        account_scope_id: str,
        magic: int,
        position_lookup: "Callable[[int], dict | None] | None" = None,
        clock_utc_iso: "Callable[[], str]",
    ) -> None:
        if clock_utc_iso is None:
            # No hidden wall clock: an omitted clock is a caller bug, not a
            # silently-defaulted datetime.now().
            raise ValueError("clock_utc_iso is required (no implicit wall clock)")
        self._store = store
        self._account_scope_id = account_scope_id
        self._magic = magic
        self._position_lookup = position_lookup
        self._clock_utc_iso = clock_utc_iso
        self._lock = threading.Lock()
        self._created = 0
        self._bound = 0
        self._closed = 0
        self._unresolved: dict = {}
        self._errors: dict = {}

    # ------------------------------------------------------------- counters
    def _count_error(self, exc: BaseException) -> None:
        name = type(exc).__name__
        with self._lock:
            self._errors[name] = self._errors.get(name, 0) + 1

    def _count_unresolved(self, reason: "str | None") -> None:
        key = reason or _REASON_REF_BUILD_FAILED
        with self._lock:
            self._unresolved[key] = self._unresolved.get(key, 0) + 1

    # ------------------------------------------------------------- ref build
    def _lookup_position(self, ticket: int) -> "dict | None":
        if self._position_lookup is None:
            return None
        try:
            info = self._position_lookup(ticket)
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    def _build_ref(self, event: dict, ticket: int) -> "tuple[dict | None, str | None]":
        """Same reconstruction for the open path, the close path, and the
        resolver — the only way a close can ever find the lifecycle bound at
        open time is if both sides build the identical composite key."""
        broker_symbol = event.get("broker_symbol") or event.get("symbol")
        direction = event.get("direction")
        info = self._lookup_position(ticket)
        opened_at = info.get("opened_at") if info else None
        position_identifier = info.get("position_identifier") if info else None
        return try_build_broker_ref(
            account_scope_id=self._account_scope_id,
            ticket=ticket,
            broker_symbol=broker_symbol,
            opened_at=opened_at,
            magic=self._magic,
            position_identifier=position_identifier,
            direction=direction,
        )

    # ------------------------------------------------------------- routing
    def on_event(self, event: dict) -> None:
        """Never raises. Routes by event_type; events without a valid ticket
        are silently ignored (not even counted as unresolved — there is
        nothing to resolve)."""
        try:
            if not isinstance(event, dict):
                return
            event_type = event.get("event_type")
            if not isinstance(event_type, str):
                return
            ticket = _strict_ticket(event.get("ticket"))
            if ticket is None:
                return
            if event_type in _OPEN_EVENT_TYPES:
                self._handle_open(event, ticket)
            elif _is_close_event_type(event_type):
                self._handle_close(event, ticket)
        except Exception as exc:  # never propagate toward the trading path
            self._count_error(exc)

    def _handle_open(self, event: dict, ticket: int) -> None:
        shadow = event.get("identity_shadow")
        shadow = shadow if isinstance(shadow, dict) else {}
        try:
            record = self._store.create_lifecycle(
                correlation_id=_clean_correlation_id(shadow),
                created_at_utc=self._clock_utc_iso(),
                boot_id=_clean_boot_id(shadow),
                cycle_id=_clean_cycle_id(shadow),
                setup_id=_clean_setup_id(event.get("setup_id")),
            )
        except LifecycleStoreError as exc:
            self._count_error(exc)
            return
        with self._lock:
            self._created += 1
        ref, reason = self._build_ref(event, ticket)
        if ref is None:
            self._count_unresolved(reason)
            return
        try:
            outcome = self._store.bind_broker_position(record["lifecycle_id"], ref)
        except LifecycleStoreError as exc:
            self._count_error(exc)
            return
        if outcome in ("BOUND", "ALREADY_BOUND"):
            with self._lock:
                self._bound += 1

    def _handle_close(self, event: dict, ticket: int) -> None:
        ref, reason = self._build_ref(event, ticket)
        if ref is None:
            self._count_unresolved(reason)
            return
        try:
            record, reason2 = self._store.resolve_by_broker_position(ref)
        except LifecycleStoreError as exc:
            self._count_error(exc)
            return
        if record is None:
            self._count_unresolved(reason2 or _REASON_RESOLVE_FAILED)
            return
        try:
            outcome = self._store.mark_closed(
                record["lifecycle_id"], closed_at_utc=self._clock_utc_iso()
            )
        except LifecycleStoreError as exc:
            self._count_error(exc)
            return
        if outcome in ("CLOSED", "ALREADY_CLOSED"):
            with self._lock:
                self._closed += 1

    # ------------------------------------------------------------- resolver
    def make_lifecycle_resolver(self) -> "Callable[[dict], dict]":
        """Returns callable(event) -> dict compatible with the
        ``lifecycle_resolver`` kwarg of ``IdentityShadowEnricher``: reuses the
        exact same reference reconstruction and store lookup as the close
        path. Never raises — internal failures degrade to
        ``{"reason": ...}``."""

        def _resolver(event: dict) -> dict:
            try:
                if not isinstance(event, dict):
                    return {"reason": _REASON_EVENT_INVALID}
                ticket = _strict_ticket(event.get("ticket"))
                if ticket is None:
                    return {"reason": _REASON_TICKET_MISSING}
                ref, reason = self._build_ref(event, ticket)
                if ref is None:
                    return {"reason": reason or _REASON_REF_BUILD_FAILED}
                record, reason2 = self._store.resolve_by_broker_position(ref)
                if record is None:
                    return {"reason": reason2 or _REASON_RESOLVE_FAILED}
                return {
                    "lifecycle_id": record["lifecycle_id"],
                    "correlation_id": record.get("correlation_id"),
                }
            except Exception as exc:
                return {"reason": "RESOLVER_ERROR:%s" % type(exc).__name__}

        return _resolver

    # ------------------------------------------------------------- diag
    def diagnostics(self) -> dict:
        with self._lock:
            return {
                "created": self._created,
                "bound": self._bound,
                "closed": self._closed,
                "unresolved": dict(self._unresolved),
                "errors": dict(self._errors),
            }
