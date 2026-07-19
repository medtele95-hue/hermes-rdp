"""EVENT_IDENTITY_RUNTIME — SHADOW enrichment adapter (feature-flag OFF by default).

T1.2B2A (2026-07-19) — additive integration of the pure identity layer
(``app/services/event_identity.py``) into the running bot, gated by
``HERMES_EVENT_IDENTITY_ENABLED`` (OFF by default). This adapter is the ONLY
place that wires identity into runtime; the pure module stays free of I/O.

SHADOW contract (this mission):
- Flag OFF  -> ``enrich`` is a strict no-op: the legacy event is returned
  untouched, no id is generated, no field is added, no registry is built.
- Flag ON   -> ``enrich`` adds a SINGLE additive top-level key
  ``"identity_shadow"`` (a dict) to the event. No legacy key is read by any
  consumer, no legacy key is renamed or removed, no decision/order depends on
  it. A single namespaced key is used (not ~11 flat keys) so it can NEVER
  collide with any of the ~200 legacy business keys and is trivially strippable.
- ``enrich`` NEVER raises toward strategy/execution: on any internal failure it
  attaches a sanitized diagnostic (no secret) and returns the event.

Durability note: ``correlation_sequence`` is PROCESS-LOCAL only (see the pure
module). The lifecycle registry is in-memory, bounded (LRU + TTL), never
persisted. Durable restart reconstruction and broker ticket<->correlation
mapping are explicitly OUT OF SCOPE here (T1.3+ / future broker-mapping mission).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable

from app.services.event_identity import (
    EventIdentityContext,
    MonotonicUUID7Generator,
)

IDENTITY_FLAG_ENV = "HERMES_EVENT_IDENTITY_ENABLED"
IDENTITY_SHADOW_VERSION = "1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Registry bounds (in-memory only, never persisted).
DEFAULT_MAX_ENTRIES = 4096
DEFAULT_ACTIVE_TTL_SECONDS = 900.0        # 15 min inactivity -> new lifecycle
DEFAULT_TERMINAL_GRACE_SECONDS = 60.0     # keep terminal lifecycles briefly
DEFAULT_CAUSATION_MAX = 4096

# --------------------------------------------------------------------------- #
# Correlation scope (SHADOW, T1.2B2A-R1)
# --------------------------------------------------------------------------- #
# The correlation is DELIBERATELY PARTIAL. The strict lifecycle key REQUIRES the
# legacy setup_id, so two independent setups (distinct setup_id) NEVER share a
# correlation_id, even within the TTL window. The TTL is a memory-cleanup rule
# ONLY -- it never defines business identity and never merges two setup_ids.
# Because the legacy setup_id is minted once per (symbol, cycle) (main.py:899),
# a correlation_id groups the events of ONE cycle's decision, NOT a multi-cycle
# lifecycle. This is honest partial correlation, not end-to-end.
LIFECYCLE_KEY_VERSION = "legacy-setup-cycle-v1"
CORRELATION_SCOPE = "LEGACY_SETUP_CYCLE"

# Close / reconciliation events have no reliable setup_id and cannot be linked
# without the (future) durable broker ticket<->correlation mapping.
_CLOSE_EVENT_TYPES = frozenset({
    "POSITION_SYNC", "POSITION_CLOSED", "EXIT_V2_CLOSE", "EXIT_V2_SHADOW",
    "QUICK_EXIT_CLOSE", "QUICK_EXIT_SLTP", "RESCUE_CLOSE", "WEEKEND_FLAT",
    "NEWS_PRECLOSE",
})


def is_identity_enabled(getenv: Callable[[str], "str | None"] | None = None) -> bool:
    """Read the feature flag. Unknown/absent -> False. Read once at boot (cached
    by the caller); never call this per-event."""
    if getenv is None:
        import os

        getenv = os.environ.get
    raw = getenv(IDENTITY_FLAG_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE_VALUES


class _Entry:
    __slots__ = ("correlation_id", "created_at", "last_seen", "terminal", "terminal_at", "count")

    def __init__(self, correlation_id: str, now: float) -> None:
        self.correlation_id = correlation_id
        self.created_at = now
        self.last_seen = now
        self.terminal = False
        self.terminal_at = 0.0
        self.count = 1


class LifecycleRegistry:
    """Thread-safe, bounded (LRU + TTL) map lifecycle_key -> correlation_id.

    Reuses the correlation_id for the same active lifecycle; mints a new one for
    a distinct lifecycle, a different symbol/strategy/direction, or after the
    active TTL elapses. Terminal lifecycles are dropped after a short grace.
    Purely in-memory: no I/O, no persistence, no MT5. Never grows unbounded.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        active_ttl_seconds: float = DEFAULT_ACTIVE_TTL_SECONDS,
        terminal_grace_seconds: float = DEFAULT_TERMINAL_GRACE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max = int(max_entries)
        self._ttl = float(active_ttl_seconds)
        self._grace = float(terminal_grace_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._map: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self.evictions = 0
        self.expirations = 0

    def _expired(self, e: _Entry, now: float) -> bool:
        if e.terminal and (now - e.terminal_at) > self._grace:
            return True
        return (now - e.last_seen) > self._ttl

    def _cleanup(self, now: float) -> None:
        stale = [k for k, e in self._map.items() if self._expired(e, now)]
        for k in stale:
            del self._map[k]
            self.expirations += 1

    def resolve(self, lifecycle_key: tuple, factory: Callable[[], str]) -> str:
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            e = self._map.get(lifecycle_key)
            if e is not None and not self._expired(e, now):
                e.last_seen = now
                e.count += 1
                self._map.move_to_end(lifecycle_key)
                return e.correlation_id
            correlation_id = factory()
            self._map[lifecycle_key] = _Entry(correlation_id, now)
            self._map.move_to_end(lifecycle_key)
            while len(self._map) > self._max:
                self._map.popitem(last=False)  # LRU eviction (oldest last_seen)
                self.evictions += 1
            return correlation_id

    def mark_terminal(self, lifecycle_key: tuple) -> None:
        with self._lock:
            e = self._map.get(lifecycle_key)
            if e is not None:
                e.terminal = True
                e.terminal_at = self._clock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


class CausationCache:
    """Bounded (LRU) map correlation_id -> last event_id, for SHADOW causation."""

    def __init__(self, *, max_entries: int = DEFAULT_CAUSATION_MAX) -> None:
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._map: "OrderedDict[str, str]" = OrderedDict()

    def get(self, correlation_id: str) -> "str | None":
        with self._lock:
            v = self._map.get(correlation_id)
            if v is not None:
                self._map.move_to_end(correlation_id)
            return v

    def set(self, correlation_id: str, event_id: str) -> None:
        with self._lock:
            self._map[correlation_id] = event_id
            self._map.move_to_end(correlation_id)
            while len(self._map) > self._max:
                self._map.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


def _norm(value: object) -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text else None


class IdentityShadowEnricher:
    """Additive SHADOW enricher. Adds one ``identity_shadow`` key; never mutates
    a legacy key; never raises toward the trading path."""

    def __init__(
        self,
        *,
        context: EventIdentityContext,
        registry: LifecycleRegistry | None = None,
        causation_cache: CausationCache | None = None,
        enabled: bool = False,
    ) -> None:
        self._ctx = context
        # `is not None`, NOT `or`: LifecycleRegistry/CausationCache define
        # __len__, so an empty injected instance is falsy and `or` would silently
        # discard it for a fresh default (losing the injected clock/TTL/bounds).
        self._registry = registry if registry is not None else LifecycleRegistry()
        self._causation = causation_cache if causation_cache is not None else CausationCache()
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _resolve_lifecycle(self, event: dict, lifecycle_context: "dict | None"):
        """Return (lifecycle_key | None, unresolved_reason_code | None).

        STRICT key = (LIFECYCLE_KEY_VERSION, legacy_setup_id, broker_symbol,
        strategy, direction). The setup_id is MANDATORY: two distinct setup_ids
        never collapse into one correlation_id. Never joins by symbol/strategy/
        direction/TTL alone, and never by ticket.
        """
        src = lifecycle_context or event
        event_type = str(event.get("event_type") or "")
        # Close/reconciliation events: no setup lifecycle, no broker mapping yet.
        if event_type in _CLOSE_EVENT_TYPES:
            return None, "BROKER_MAPPING_NOT_AVAILABLE"
        setup_id = src.get("setup_id")
        setup_id = str(setup_id).strip() if setup_id is not None else ""
        if not setup_id:
            return None, "SETUP_ID_MISSING"
        broker_symbol = _norm(src.get("broker_symbol"))
        strategy = _norm(src.get("strategy") or src.get("strategy_id"))
        direction = _norm(src.get("direction"))
        if not (broker_symbol and strategy and direction):
            return None, "LIFECYCLE_FIELDS_MISSING"
        # setup_id is a KEY COMPONENT only; correlation_id itself is a fresh
        # UUIDv7 (never the setup_id).
        return (LIFECYCLE_KEY_VERSION, setup_id, broker_symbol, strategy, direction), None

    def enrich(
        self,
        event: dict,
        *,
        lifecycle_context: "dict | None" = None,
        causation_id: "str | None" = None,
        terminal: bool = False,
    ) -> dict:
        # Flag OFF -> strict no-op (identical object, zero id, zero field).
        if not self._enabled:
            return event
        try:
            lifecycle_key, unresolved_reason = self._resolve_lifecycle(event, lifecycle_context)
            event_id = self._ctx.new_event_id()
            sequence_number = self._ctx.next_sequence_number()
            correlation_id: "str | None" = None
            correlation_sequence: "int | None" = None
            if lifecycle_key is not None:
                correlation_id = self._registry.resolve(lifecycle_key, self._ctx.new_correlation_id)
                correlation_sequence = self._ctx.next_correlation_sequence(correlation_id)
                # Causation is only ever within the SAME strict key (same setup
                # cycle) -> no cross-setup, cross-strategy or cross-direction link.
                if causation_id is None:
                    causation_id = self._causation.get(correlation_id)
                self._causation.set(correlation_id, event_id)
            else:
                causation_id = None  # never fabricate causation for unresolved events
            shadow = {
                "identity_shadow_version": IDENTITY_SHADOW_VERSION,
                "identity_mode": "SHADOW",
                "event_id": event_id,
                "correlation_id": correlation_id,
                "correlation_scope": CORRELATION_SCOPE,
                "correlation_quality": ("PARTIAL" if correlation_id else "UNRESOLVED"),
                "unresolved_reason_code": (None if correlation_id else unresolved_reason),
                "lifecycle_key_version": LIFECYCLE_KEY_VERSION,
                "causation_id": causation_id,
                "sequence_number": sequence_number,
                "correlation_sequence": correlation_sequence,
                "correlation_sequence_durability": "PROCESS_LOCAL",
                "durable_across_restart": False,
                "end_to_end_complete": False,
                "terminal_status": "TERMINAL_STATUS_UNCERTAIN",
                "legacy_setup_id": event.get("setup_id"),
                "legacy_trace_id": (event.get("trace_id") or None),
                "bot_instance_id": self._ctx.bot_instance_id,
                "server_id_state": "UNPROVISIONED",
            }
            # Single additive top-level key: cannot collide with legacy keys.
            event["identity_shadow"] = shadow
        except Exception as exc:  # SHADOW: never propagate to the trading path
            event["identity_shadow"] = {
                "identity_shadow_version": IDENTITY_SHADOW_VERSION,
                "identity_mode": "SHADOW",
                "error": "ENRICH_FAILED",
                "error_type": type(exc).__name__,  # type name only, no secret
            }
        return event


def maybe_build_identity_enricher(
    *,
    getenv: Callable[[str], "str | None"] | None = None,
    bot_instance_id: "str | None" = None,
) -> "IdentityShadowEnricher | None":
    """Boot factory. Returns None when the flag is OFF (production default): no
    instance, no registry, no id. Only builds a SHADOW enricher when the flag is
    explicitly ON. Uses a syntactically-valid PLACEHOLDER server_id purely to
    satisfy the context constructor; the emitted ``server_id_state`` stays
    ``UNPROVISIONED`` (no real server_id is created or persisted here)."""
    if not is_identity_enabled(getenv):
        return None
    import os

    instance = bot_instance_id or ("bot-shadow-%d" % os.getpid())
    context = EventIdentityContext(
        server_id="srv-" + "0" * 16,   # placeholder (UNPROVISIONED), not a real id
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )
    return IdentityShadowEnricher(context=context, enabled=True)
