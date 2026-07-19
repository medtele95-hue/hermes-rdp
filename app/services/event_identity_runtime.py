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

import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.services.event_identity import (
    EventIdentityContext,
    MonotonicUUID7Generator,
)

IDENTITY_FLAG_ENV = "HERMES_EVENT_IDENTITY_ENABLED"
IDENTITY_SHADOW_VERSION = "1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# --------------------------------------------------------------------------- #
# Activation lease (T1.2B2B2A1, 2026-07-19)
# --------------------------------------------------------------------------- #
# SHADOW activation is granted by an EXPIRING runtime lease (a strictly-validated
# JSON file read ONCE at boot), never a permanent boolean flag: an expired or
# absent lease can never re-activate SHADOW after a crash/restart/reboot. The
# lease does NOT disable an already-running process mid-observation; immediate
# deactivation = delete the lease + controlled bot restart. Posture is FAIL-OFF:
# any lease problem (missing/invalid/expired/commit-mismatch/IO) leaves SHADOW
# OFF and never blocks the legacy bot or creates exposure.
IDENTITY_LEASE_FILENAME = "identity_shadow_activation.json"
IDENTITY_LEASE_SCHEMA_VERSION = 1
MAX_IDENTITY_SHADOW_LEASE_SECONDS = 7200  # 2 h ceiling; a real lease is shorter
IDENTITY_LEASE_MAX_BYTES = 16 * 1024
_REQUIRED_LEASE_FIELDS = frozenset({
    "schema_version", "enabled", "mode", "activation_id", "created_at_utc",
    "expires_at_utc", "expected_git_commit", "requested_by", "reason",
})
_GIT_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class IdentityActivationDecision:
    """Diagnostic result of activation resolution. Carries NO lease body and NO
    secret. ``source`` in {OFF, ENVIRONMENT, RUNTIME_LEASE}."""

    enabled: bool
    source: str
    reason_code: str
    activation_id: "str | None" = None
    expires_at_utc: "str | None" = None
    expected_git_commit: "str | None" = None


def _off(reason_code: str) -> IdentityActivationDecision:
    return IdentityActivationDecision(False, "OFF", reason_code)


def _parse_utc(value: object) -> "datetime | None":
    """Parse a strict ISO-8601 UTC-aware timestamp. Naive (no tz) -> None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _validate_lease_semantics(obj, current_git_commit, now_utc) -> IdentityActivationDecision:
    if not isinstance(obj, dict):
        return _off("LEASE_SCHEMA_INVALID")
    if set(obj.keys()) != _REQUIRED_LEASE_FIELDS:  # no missing / no unknown field
        return _off("LEASE_SCHEMA_INVALID")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != IDENTITY_LEASE_SCHEMA_VERSION:
        return _off("LEASE_SCHEMA_INVALID")
    if type(obj["enabled"]) is not bool:            # 1 / "true" are NOT bool
        return _off("LEASE_SCHEMA_INVALID")
    if obj["enabled"] is not True:                  # explicitly disabled lease
        return _off("OFF_DEFAULT")
    if obj["mode"] != "SHADOW":
        return _off("LEASE_SCHEMA_INVALID")
    aid = obj["activation_id"]
    if not isinstance(aid, str):
        return _off("LEASE_SCHEMA_INVALID")
    try:
        uuid.UUID(aid)
    except (ValueError, AttributeError, TypeError):
        return _off("LEASE_SCHEMA_INVALID")
    for field in ("requested_by", "reason"):
        if not isinstance(obj[field], str) or not obj[field].strip():
            return _off("LEASE_SCHEMA_INVALID")
    commit = obj["expected_git_commit"]
    if not isinstance(commit, str) or not _GIT_COMMIT_RE.match(commit):
        return _off("LEASE_SCHEMA_INVALID")
    created = _parse_utc(obj["created_at_utc"])
    expires = _parse_utc(obj["expires_at_utc"])
    if created is None or expires is None:
        return _off("LEASE_SCHEMA_INVALID")
    if created > expires:
        return _off("LEASE_SCHEMA_INVALID")
    if (expires - created).total_seconds() > MAX_IDENTITY_SHADOW_LEASE_SECONDS:
        return _off("LEASE_DURATION_EXCEEDED")
    if created > now_utc:                            # window not started yet
        return _off("LEASE_NOT_YET_VALID")
    if expires <= now_utc:
        return _off("LEASE_EXPIRED")
    if not isinstance(current_git_commit, str) or not _GIT_COMMIT_RE.match(current_git_commit or "") \
            or current_git_commit != commit:
        return _off("LEASE_COMMIT_MISMATCH")         # includes "unknown" commit
    return IdentityActivationDecision(True, "RUNTIME_LEASE", "LEASE_VALID", aid, obj["expires_at_utc"], commit)


def evaluate_identity_activation(
    *,
    env_value: "str | None",
    lease_path: "Path | str | None",
    current_git_commit: str,
    now_utc: datetime,
    allowed_dir: "Path | str | None" = None,
) -> IdentityActivationDecision:
    """Deterministic, injectable activation resolver. Precedence: a truthy env
    var -> ENVIRONMENT (kept for compatibility); else a valid non-expired lease
    -> RUNTIME_LEASE; else OFF. FAIL-OFF: never raises, never returns the lease
    body/secret. Read ONCE at boot (never per-event)."""
    try:
        if env_value is not None and str(env_value).strip().lower() in _TRUE_VALUES:
            return IdentityActivationDecision(True, "ENVIRONMENT", "ENV_ENABLED")
        if lease_path is None:
            return _off("OFF_DEFAULT")
        p = Path(lease_path)
        try:
            if p.is_symlink():                       # no symlink / reparse target
                return _off("LEASE_PATH_INVALID")
            if not p.exists():
                return _off("LEASE_MISSING")
            if not p.is_file():
                return _off("LEASE_PATH_INVALID")
            if allowed_dir is not None and p.resolve().parent != Path(allowed_dir).resolve():
                return _off("LEASE_PATH_INVALID")
            if p.stat().st_size > IDENTITY_LEASE_MAX_BYTES:
                return _off("LEASE_SCHEMA_INVALID")
            raw = p.read_text(encoding="utf-8")
        except PermissionError:
            return _off("LEASE_PERMISSION_DENIED")
        except FileNotFoundError:
            return _off("LEASE_MISSING")
        except (UnicodeError, UnicodeDecodeError):
            return _off("LEASE_INVALID_JSON")
        except OSError:
            return _off("LEASE_PERMISSION_DENIED")
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return _off("LEASE_INVALID_JSON")
        return _validate_lease_semantics(obj, current_git_commit, now_utc)
    except Exception:
        return _off("LEASE_SCHEMA_INVALID")          # last-resort FAIL-OFF

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

    def _resolve_entry(self, lifecycle_key: tuple, factory: Callable[[], str]) -> _Entry:
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            e = self._map.get(lifecycle_key)
            if e is not None and not self._expired(e, now):
                e.last_seen = now
                e.count += 1
                self._map.move_to_end(lifecycle_key)
                return e
            e = _Entry(factory(), now)
            self._map[lifecycle_key] = e
            self._map.move_to_end(lifecycle_key)
            while len(self._map) > self._max:
                self._map.popitem(last=False)  # LRU eviction (oldest last_seen)
                self.evictions += 1
            return e

    def resolve(self, lifecycle_key: tuple, factory: Callable[[], str]) -> str:
        return self._resolve_entry(lifecycle_key, factory).correlation_id

    def resolve_with_sequence(self, lifecycle_key: tuple, factory: Callable[[], str]) -> "tuple[str, int]":
        """T1.2B2B2A1-R2 — the correlation_sequence LIVES IN the bounded entry
        (``count``: 1 on creation, +1 per resolve). Same LRU/TTL policy: an
        evicted/expired lifecycle drops its sequence with it, ``clear()`` frees
        everything, and there is NO second unbounded per-correlation table."""
        e = self._resolve_entry(lifecycle_key, factory)
        return e.correlation_id, e.count

    def mark_terminal(self, lifecycle_key: tuple) -> None:
        with self._lock:
            e = self._map.get(lifecycle_key)
            if e is not None:
                e.terminal = True
                e.terminal_at = self._clock()

    def clear(self) -> None:
        """Drop all entries (used once on lease expiry). Thread-safe, no I/O."""
        with self._lock:
            self._map.clear()

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

    def clear(self) -> None:
        """Drop all entries (used once on lease expiry). Thread-safe, no I/O."""
        with self._lock:
            self._map.clear()

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
        expiry_monotonic: "float | None" = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._ctx = context
        # `is not None`, NOT `or`: LifecycleRegistry/CausationCache define
        # __len__, so an empty injected instance is falsy and `or` would silently
        # discard it for a fresh default (losing the injected clock/TTL/bounds).
        self._registry = registry if registry is not None else LifecycleRegistry()
        self._causation = causation_cache if causation_cache is not None else CausationCache()
        self._enabled = bool(enabled)
        # T1.2B2B2A1-R1 — runtime expiry. When built from a RUNTIME_LEASE, the
        # deadline is a MONOTONIC instant (immune to wall-clock jumps after boot).
        # Once reached, this live process self-deactivates: enrich() becomes a
        # strict no-op and the registry/cache are cleared ONCE. State is
        # one-way ACTIVE -> EXPIRED; re-activation requires a new lease + a
        # controlled bot restart. None => no runtime expiry (e.g. ENVIRONMENT).
        self._expiry_monotonic = expiry_monotonic
        self._monotonic = monotonic_clock or time.monotonic
        self._state_lock = threading.Lock()
        self._expired = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def expired(self) -> bool:
        return self._expired

    def _expire_once(self) -> None:
        """One-time, idempotent, thread-safe cleanup on lease expiry. No I/O."""
        with self._state_lock:
            if self._expired:
                return
            self._expired = True
            try:
                self._registry.clear()
                self._causation.clear()
            except Exception:
                pass

    def _is_expired(self) -> bool:
        """Cheap per-event check. No I/O, no subprocess, no getenv. After the
        first expiry it is a single bool read (~`if expired: return event`)."""
        if self._expiry_monotonic is None:
            return False
        if self._expired:
            return True
        if self._monotonic() >= self._expiry_monotonic:
            self._expire_once()
            return True
        return False

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
        # Lease expired in THIS live process -> strict no-op. No new event_id,
        # correlation_id, sequence, registry/cache access, or identity_shadow;
        # the legacy event is returned intact. One-way ACTIVE -> EXPIRED.
        if self._is_expired():
            return event
        try:
            lifecycle_key, unresolved_reason = self._resolve_lifecycle(event, lifecycle_context)
            event_id = self._ctx.new_event_id()
            sequence_number = self._ctx.next_sequence_number()
            correlation_id: "str | None" = None
            correlation_sequence: "int | None" = None
            if lifecycle_key is not None:
                # T1.2B2B2A1-R2 — the sequence comes from the BOUNDED registry
                # entry, NOT from context.next_correlation_sequence(): that pure-
                # module store is a plain unbounded dict that would grow with
                # every lifecycle seen during the lease and survive expiry. The
                # pure API stays available for other uses/tests; the runtime
                # SHADOW path must only hold bounded per-correlation state.
                correlation_id, correlation_sequence = self._registry.resolve_with_sequence(
                    lifecycle_key, self._ctx.new_correlation_id
                )
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


def _default_lease_path() -> Path:
    # app/services/event_identity_runtime.py -> repo/app/data/<lease>
    return Path(__file__).resolve().parents[1] / "data" / IDENTITY_LEASE_FILENAME


def _read_git_commit_safe() -> str:
    """Single boot-time git read (short timeout, no exception propagated). Any
    failure -> 'unknown' (which the lease resolver treats as a commit mismatch,
    i.e. OFF). Never called per-event."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, timeout=5, text=True,
        )
        commit = (out.stdout or "").strip()
        return commit if _GIT_COMMIT_RE.match(commit) else "unknown"
    except Exception:
        return "unknown"


def resolve_identity_activation(
    *,
    getenv: Callable[[str], "str | None"] | None = None,
    lease_path: "Path | str | None" = None,
    current_git_commit: "str | None" = None,
    now_utc: "datetime | None" = None,
) -> IdentityActivationDecision:
    """Boot-time activation decision (diagnostic). Reads env + lease ONCE."""
    import os

    env_value = (getenv or os.environ.get)(IDENTITY_FLAG_ENV)
    # Default (production) path is pinned to app/data and its allowed dir is
    # enforced. An explicitly injected path is a trusted code-level override
    # (tests / future flexibility) whose own parent is the allowed dir.
    if lease_path is None:
        path = _default_lease_path()
        allowed = path.parent
    else:
        path = lease_path
        allowed = Path(lease_path).parent
    commit = current_git_commit if current_git_commit is not None else _read_git_commit_safe()
    when = now_utc or datetime.now(timezone.utc)
    return evaluate_identity_activation(
        env_value=env_value, lease_path=path, current_git_commit=commit,
        now_utc=when, allowed_dir=allowed,
    )


def maybe_build_identity_enricher(
    *,
    getenv: Callable[[str], "str | None"] | None = None,
    bot_instance_id: "str | None" = None,
    lease_path: "Path | str | None" = None,
    current_git_commit: "str | None" = None,
    now_utc: "datetime | None" = None,
    monotonic_clock: Callable[[], float] | None = None,
) -> "IdentityShadowEnricher | None":
    """Boot factory. Returns None when SHADOW is OFF (production default): no
    instance, no registry, no id. Builds a SHADOW enricher only when the
    activation decision (env OR a valid non-expired runtime lease) is enabled.
    For a RUNTIME_LEASE, the lease's ``expires_at_utc`` is converted to a
    MONOTONIC deadline so the live process self-deactivates at expiry (no
    restart needed, immune to wall-clock jumps). ENVIRONMENT source has no
    runtime expiry (no false expiry invented). Uses a placeholder server_id."""
    import os

    mono = monotonic_clock or time.monotonic
    mono_now = mono()
    when = now_utc or datetime.now(timezone.utc)
    decision = resolve_identity_activation(
        getenv=getenv, lease_path=lease_path,
        current_git_commit=current_git_commit, now_utc=when,
    )
    if not decision.enabled:
        return None
    expiry_monotonic: "float | None" = None
    if decision.source == "RUNTIME_LEASE" and decision.expires_at_utc:
        expires = _parse_utc(decision.expires_at_utc)
        if expires is not None:
            remaining = (expires - when).total_seconds()
            expiry_monotonic = mono_now + max(0.0, remaining)
    instance = bot_instance_id or ("bot-shadow-%d" % os.getpid())
    context = EventIdentityContext(
        server_id="srv-" + "0" * 16,   # placeholder (UNPROVISIONED), not a real id
        bot_instance_id=instance,
        uuid_generator=MonotonicUUID7Generator(),
    )
    return IdentityShadowEnricher(
        context=context, enabled=True,
        expiry_monotonic=expiry_monotonic, monotonic_clock=mono,
    )
