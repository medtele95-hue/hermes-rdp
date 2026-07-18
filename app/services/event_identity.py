"""EVENT_IDENTITY — pure identity layer for the HERMES canonical event schema.

T1.2B1 (2026-07-19) — PURE module, NO wiring. It is imported by NO production
runtime yet (integration is the separate T1.2B2 mission, behind a feature flag).

Design frozen in T1.1 / T1.2A:
- EVENT_ID_ALGORITHM = monotonic UUIDv7, RFC 9562, STDLIB ONLY (Python 3.11 has
  no ``uuid.uuid7``; this implementation is a drop-in until 3.14). k-sortable,
  PostgreSQL-uuid native, strictly monotonic in-process, clamped on clock
  regression, 12-bit intra-millisecond counter.
- ACCOUNT_IDENTITY_SCHEME = HMAC-SHA256(local_secret_key, canonical(login,
  broker_server)) -> pseudonymous ``account_scope_id`` + ``key_id`` (rotation).
- Idempotency keys use TYPED, LENGTH-PREFIXED framing (injection-proof): None,
  bool, int, float, str, bytes are all DISTINCT; NaN / +Inf / -Inf and complex
  types are rejected explicitly.

PURITY CONTRACT — this module NEVER:
- reads Git, environment variables, MT5, or any file;
- creates or reads a server_id, an HMAC key, or a broker mapping;
- logs, prints, or raises anything containing a login or an HMAC key.
All such values are INJECTED by the future wiring (T1.2B2 / T1.6).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

SCHEMA_NAME = "hermes.event"
SCHEMA_VERSION = "1.0"
ACCOUNT_SCOPE_PREFIX = "acct"
ACCOUNT_SCOPE_ALGORITHM = "HMAC-SHA256"
_ACCOUNT_SCOPE_VERSION = "v1"
_MIN_HMAC_KEY_BYTES = 32
_IDEMPOTENCY_HEX = 32  # 128 bits
_UUID7_VERSION = 0x7
_UUID7_VARIANT = 0b10
_COUNTER_MAX = 0x0FFF  # 12-bit rand_a used as an intra-millisecond counter


# --------------------------------------------------------------------------- #
# Exceptions (never carry a login or a key)
# --------------------------------------------------------------------------- #
class EventIdentityError(Exception):
    """Base error for the identity layer."""


class EventIdentityValidationError(EventIdentityError):
    """An identifier does not satisfy its format/version contract."""


class UnsupportedIdempotencyValue(EventIdentityError):
    """A value cannot be part of an idempotency key (wrong type / NaN / Inf)."""


# --------------------------------------------------------------------------- #
# Public immutable types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuntimeAnchor:
    """Boot-time provenance anchor. Values are INJECTED (never read here)."""

    git_commit: str
    git_branch: str
    source_dirty: bool
    tracked_config_dirty: bool
    worktree_dirty: bool
    deployment_id: str
    server_id: str
    bot_instance_id: str


@dataclass(frozen=True)
class AccountScope:
    """Pseudonymous account identity. Carries NO login and NO key material."""

    account_scope_id: str
    key_id: str
    algorithm: str = ACCOUNT_SCOPE_ALGORITHM


# --------------------------------------------------------------------------- #
# Monotonic UUIDv7 generator (RFC 9562)
# --------------------------------------------------------------------------- #
def _default_clock_ms() -> int:
    import time

    return int(time.time() * 1000)


def _default_random_bits(n: int) -> int:
    nbytes = (n + 7) // 8
    return int.from_bytes(os.urandom(nbytes), "big") & ((1 << n) - 1)


class MonotonicUUID7Generator:
    """Thread-safe, strictly monotonic UUIDv7 generator.

    Layout (128 bits, RFC 9562):
        bits 127..80 : unix_ts_ms (48)   -- milliseconds since epoch (clamped)
        bits  79..76 : version (4) = 0b0111
        bits  75..64 : rand_a  (12)      -- intra-ms counter (0..4095)
        bits  63..62 : variant (2) = 0b10
        bits  61.. 0 : rand_b  (62)      -- entropy

    Ordering: version/variant are constant, so the 128-bit integer sorts by
    (ms, counter, rand_b). ``(ms, counter)`` is strictly increasing per call,
    so successive UUIDs are strictly increasing -> k-sortable AND unique.

    Clock regression / same-ms burst: the logical timestamp never goes
    backwards. On a non-advancing clock the counter increments; on counter
    overflow (>4095 in one logical ms) the logical ms is advanced by 1 and the
    counter resets, so monotonicity is preserved without ever emitting a lower
    value. rand_b never participates in ordering, so its predictability cannot
    break uniqueness (the (ms, counter) pair already guarantees it).
    """

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        random_bits: Callable[[int], int] | None = None,
    ) -> None:
        self._clock_ms = clock_ms or _default_clock_ms
        self._random_bits = random_bits or _default_random_bits
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def new(self) -> uuid.UUID:
        with self._lock:
            ms = int(self._clock_ms())
            if ms > self._last_ms:
                self._last_ms = ms
                self._counter = 0
            else:
                # same millisecond OR clock went backwards -> clamp + count
                self._counter += 1
                if self._counter > _COUNTER_MAX:
                    self._last_ms += 1
                    self._counter = 0
                ms = self._last_ms
            counter = self._counter
            ms &= (1 << 48) - 1
        rand_b = self._random_bits(62) & ((1 << 62) - 1)
        value = (
            (ms << 80)
            | (_UUID7_VERSION << 76)
            | ((counter & _COUNTER_MAX) << 64)
            | (_UUID7_VARIANT << 62)
            | rand_b
        )
        return uuid.UUID(int=value)


def _is_uuid7(value: str) -> bool:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 7 and ((parsed.int >> 62) & 0b11) == _UUID7_VARIANT


# --------------------------------------------------------------------------- #
# Idempotency framing (typed, length-prefixed, injection-proof)
# --------------------------------------------------------------------------- #
# One distinct tag byte per Python type so None/bool/int/float/str/bytes never
# collide; each field is framed as <tag><byte_len>:<content_bytes>.
_TAG_NONE = b"N"
_TAG_BOOL = b"B"
_TAG_INT = b"i"
_TAG_FLOAT = b"f"
_TAG_STR = b"s"
_TAG_BYTES = b"y"


def _frame_part(value: object) -> bytes:
    if value is None:
        tag, content = _TAG_NONE, b""
    elif isinstance(value, bool):
        tag, content = _TAG_BOOL, (b"1" if value else b"0")
    elif isinstance(value, int):
        tag, content = _TAG_INT, str(value).encode("ascii")
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedIdempotencyValue("non-finite float rejected")
        # exact IEEE-754 big-endian bytes: deterministic, distinguishes 2.0
        # from 0.0 and from int 2 (different tag), no locale/repr dependence.
        tag, content = _TAG_FLOAT, struct.pack(">d", value)
    elif isinstance(value, str):
        tag, content = _TAG_STR, value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        tag, content = _TAG_BYTES, bytes(value)
    else:
        raise UnsupportedIdempotencyValue(
            "unsupported idempotency value type: %s" % type(value).__name__
        )
    return tag + str(len(content)).encode("ascii") + b":" + content


def _valid_category(category: str) -> bool:
    if not isinstance(category, str) or not (1 <= len(category) <= 40):
        return False
    if not (category[0].isascii() and category[0].isalpha() and category[0].isupper()):
        return False
    return all((c.isascii() and (c.isupper() or c.isdigit() or c == "_")) for c in category)


def build_idempotency_key(category: str, *parts: object) -> str:
    """Deterministic idempotency key ``<CATEGORY>:<32 hex>`` (128 bits).

    Framing is typed + length-prefixed: no field value can forge a boundary,
    and distinct Python types never collide. Raises ``UnsupportedIdempotencyValue``
    for NaN/Inf or unsupported types; ``EventIdentityValidationError`` for a
    malformed category.
    """
    if not _valid_category(category):
        raise EventIdentityValidationError("invalid idempotency category")
    payload = _frame_part(category) + b"".join(_frame_part(p) for p in parts)
    digest = hashlib.sha256(payload).hexdigest()[:_IDEMPOTENCY_HEX]
    return "%s:%s" % (category, digest)


# --------------------------------------------------------------------------- #
# Account scope (HMAC-SHA256, pure)
# --------------------------------------------------------------------------- #
def _normalize_broker_server(broker_server: str) -> str:
    if not isinstance(broker_server, str):
        raise EventIdentityError("broker_server must be a string")
    normalized = " ".join(broker_server.split()).upper()
    if not normalized:
        raise EventIdentityError("broker_server must not be empty")
    return normalized


def _normalize_login(login: str | int) -> str:
    if isinstance(login, bool) or not isinstance(login, (str, int)):
        raise EventIdentityError("login must be a non-bool str or int")
    text = str(login).strip()
    if not text:
        raise EventIdentityError("login must not be empty")
    return text


def derive_account_scope_id(
    *,
    key: bytes,
    key_id: str,
    login: str | int,
    broker_server: str,
) -> AccountScope:
    """Pure HMAC-SHA256 account pseudonym. Never returns/raises login or key.

    - ``key`` must be >= 32 bytes; ``key_id`` labels the key for rotation.
    - The login is canonicalized then framed; the broker_server is normalized
      (whitespace-collapsed, upper-cased). The result carries neither.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_HMAC_KEY_BYTES:
        raise EventIdentityError("HMAC key must be at least 32 bytes")
    if not isinstance(key_id, str) or not key_id:
        raise EventIdentityError("key_id is required")
    login_c = _normalize_login(login)
    server_c = _normalize_broker_server(broker_server)
    message = _frame_part(login_c) + _frame_part(server_c)
    digest = hmac.new(bytes(key), message, hashlib.sha256).hexdigest()[:_IDEMPOTENCY_HEX]
    scope_id = "%s-%s-%s" % (ACCOUNT_SCOPE_PREFIX, _ACCOUNT_SCOPE_VERSION, digest)
    return AccountScope(account_scope_id=scope_id, key_id=key_id, algorithm=ACCOUNT_SCOPE_ALGORITHM)


# --------------------------------------------------------------------------- #
# Validators (strict; a UUIDv4 is NOT accepted as a new correlation/event id)
# --------------------------------------------------------------------------- #
def validate_event_id(value: str) -> str:
    if not _is_uuid7(value):
        raise EventIdentityValidationError("event_id must be a UUIDv7")
    return str(value)


def validate_correlation_id(value: str) -> str:
    if not _is_uuid7(value):
        raise EventIdentityValidationError("correlation_id must be a UUIDv7")
    return str(value)


def validate_server_id(value: str) -> str:
    if isinstance(value, str) and value.startswith("srv-") and len(value) >= 12 and \
            all(c in "0123456789abcdef" for c in value[4:]):
        return value
    raise EventIdentityValidationError("server_id must match 'srv-<hex>'")


def validate_bot_instance_id(value: str) -> str:
    if isinstance(value, str) and value.startswith("bot-") and len(value) >= 5:
        return value
    raise EventIdentityValidationError("bot_instance_id must match 'bot-<token>'")


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or ":" not in value:
        raise EventIdentityValidationError("malformed idempotency key")
    category, _, digest = value.partition(":")
    if _valid_category(category) and len(digest) >= _IDEMPOTENCY_HEX and \
            all(c in "0123456789abcdef" for c in digest):
        return value
    raise EventIdentityValidationError("malformed idempotency key")


def validate_account_scope(value: str) -> str:
    prefix = "%s-%s-" % (ACCOUNT_SCOPE_PREFIX, _ACCOUNT_SCOPE_VERSION)
    if isinstance(value, str) and value.startswith(prefix):
        digest = value[len(prefix):]
        if len(digest) >= _IDEMPOTENCY_HEX and all(c in "0123456789abcdef" for c in digest):
            return value
    raise EventIdentityValidationError("malformed account_scope_id")


# --------------------------------------------------------------------------- #
# In-memory identity context (PURE: no disk, no env, no MT5, no Git)
# --------------------------------------------------------------------------- #
class EventIdentityContext:
    """Process-local identity provider. All persistent inputs are INJECTED.

    ``server_id`` and ``bot_instance_id`` are provided by the future wiring;
    this context neither creates nor persists them. ``sequence_number`` is
    per-instance (starts at 1); ``correlation_sequence`` is process-local for
    T1.2B1 (durable restart reconstruction lands with the T1.3 journal).
    """

    def __init__(
        self,
        *,
        server_id: str,
        bot_instance_id: str,
        account_hmac_key: bytes | None = None,
        account_key_id: str | None = None,
        uuid_generator: MonotonicUUID7Generator | None = None,
    ) -> None:
        self.server_id = validate_server_id(server_id)
        self.bot_instance_id = validate_bot_instance_id(bot_instance_id)
        if account_hmac_key is not None and (
            not isinstance(account_hmac_key, (bytes, bytearray))
            or len(account_hmac_key) < _MIN_HMAC_KEY_BYTES
        ):
            raise EventIdentityError("HMAC key must be at least 32 bytes")
        if account_hmac_key is not None and not account_key_id:
            raise EventIdentityError("account_key_id is required when a key is provided")
        self._account_hmac_key = bytes(account_hmac_key) if account_hmac_key is not None else None
        self._account_key_id = account_key_id
        self._uuid = uuid_generator or MonotonicUUID7Generator()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._corr_lock = threading.Lock()
        self._corr_seq: dict[str, int] = {}

    # --- identifiers ---
    def new_event_id(self) -> str:
        return str(self._uuid.new())

    def new_correlation_id(self) -> str:
        return str(self._uuid.new())

    # --- sequences ---
    def next_sequence_number(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def next_correlation_sequence(self, correlation_id: str) -> int:
        with self._corr_lock:
            n = self._corr_seq.get(correlation_id, 0) + 1
            self._corr_seq[correlation_id] = n
            return n

    def seed_correlation_sequence(self, correlation_id: str, last_observed: int) -> None:
        if not isinstance(last_observed, int) or isinstance(last_observed, bool) or last_observed < 0:
            raise EventIdentityError("last_observed must be a non-negative int")
        with self._corr_lock:
            current = self._corr_seq.get(correlation_id, 0)
            # never move an existing sequence backwards
            self._corr_seq[correlation_id] = max(current, last_observed)

    # --- account scope (delegates to the pure function with the injected key) ---
    def derive_account_scope_id(self, login: str | int, broker_server: str) -> AccountScope:
        if self._account_hmac_key is None or not self._account_key_id:
            raise EventIdentityError("no account HMAC key configured in this context")
        return derive_account_scope_id(
            key=self._account_hmac_key,
            key_id=self._account_key_id,
            login=login,
            broker_server=broker_server,
        )

    # --- idempotency ---
    def build_idempotency_key(self, category: str, *parts: object) -> str:
        return build_idempotency_key(category, *parts)

    # --- validators (pass-through for callers holding only a context) ---
    def validate_event_id(self, value: str) -> str:
        return validate_event_id(value)

    def validate_correlation_id(self, value: str) -> str:
        return validate_correlation_id(value)
