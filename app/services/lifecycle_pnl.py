"""LIFECYCLE_PNL — DORMANT laboratory: broker deals -> lifecycle PnL truth
(M05-P1).

MISSION 100% LABORATOIRE : this module is imported by NO production runtime
(no import from main.py / demo_router.py / lifecycle_capture.py /
event_identity_runtime.py / system_heartbeat.py). It never touches MT5,
never opens a network connection, never reads an environment variable or a
wall clock. Every deal fed into it is INJECTED by the caller as a plain
mapping (or a pre-built ``DealRecord``) — this module never fetches a deal
from a broker terminal, a history request, or any I/O of its own.

ROLE : link the full life of a position to its PnL truth, purely and
replayably:

    order intent (M04, optional/read-only shape reference)
        -> lifecycle identity (M02-P1C ``LifecycleIdentityStore`` record)
        -> broker deals (INJECTED fixtures, never MT5)
        -> aggregated PnL + timeline (``LifecyclePnl.compute``)
        -> durable, idempotent ledger (``PnlLedger``, M03 append-only)
        -> cross-layer anomaly detection (``reconcile_intent_lifecycle_pnl``)

MONEY DISCIPLINE — this module NEVER uses ``float`` for an amount:
- ``DealRecord.volume/price/profit/commission/swap/fee`` are ``Decimal``
  only. A raw mapping may supply them as a ``Decimal`` instance, a plain
  (non-bool) ``int`` (``Decimal(int)`` is always exact), or a non-empty
  ``str`` converted EXPLICITLY via ``Decimal(str_value)`` (documented,
  never a float round-trip). A Python ``float`` or a ``bool`` is REJECTED
  outright for any of these fields — silently accepting a float would let
  IEEE-754 imprecision leak into a dollar amount.
- The four USD aggregates (``gross_profit``, ``commission``, ``swap``,
  ``fees``) and ``net_profit`` are summed as EXACT ``Decimal`` (no
  intermediate rounding per deal line) and quantized to 2 decimal places
  EXACTLY ONCE, at the very end of the aggregation, using
  ``ROUND_HALF_EVEN`` (banker's rounding — the Python ``decimal`` module's
  own default rounding, applied here explicitly and documented rather than
  left implicit). Per-deal-line amounts kept in ``timeline`` stay at full
  (unrounded) precision — they are raw broker data, not a presented total.
  ``volume_entered``/``volume_exited``/``entry_avg_price``/
  ``exit_avg_price`` are NOT USD amounts (lot size / instrument price) and
  are therefore left at full ``Decimal`` precision, unquantized.

MATCHING (deal -> lifecycle), NEVER by ticket alone:
- a lifecycle's ``broker_ref`` (as produced by
  ``lifecycle_identity_store.LifecycleIdentityStore``) is a composite:
  ``account_scope_id + ticket + position_identifier(optional) +
  broker_symbol + opened_at + magic + direction`` ; ``DealRecord`` only
  carries ``deal_id, position_id, ticket, symbol`` as discriminants (a
  deal has no ``account_scope_id``/``magic``/``opened_at`` of its own) ;
- when the lifecycle's ``broker_ref`` HAS a ``position_identifier``, a deal
  matches iff its ``symbol`` equals ``broker_ref.broker_symbol`` AND its
  ``position_id`` equals ``broker_ref.position_identifier`` — the deal's
  ``ticket`` is NEVER consulted in this branch, so a deal that happens to
  share the SAME ticket but a DIFFERENT ``position_id`` is an orphan
  (ticket recycling is exactly the scenario ``position_identifier`` exists
  to discriminate) ;
- when ``position_identifier`` is absent, a deal matches iff its
  ``symbol`` AND its ``ticket`` both equal the lifecycle's ``broker_ref``
  fields — two fields together, still never ticket alone ;
- a lifecycle with no ``broker_ref`` (``None``, legitimately unbound) never
  matches any deal — every deal offered to ``LifecyclePnl.compute`` is then
  counted as orphaned with reason ``NO_BROKER_REF``.
- defense in depth: ``LifecyclePnl.compute`` never trusts a caller-supplied
  ``lifecycle_record`` blindly (it may be a stale or hand-built fixture,
  not necessarily a fresh read from a live store). It re-derives the ref's
  canonical key via ``lifecycle_identity_store.try_build_broker_ref`` +
  ``broker_ref_key`` (both imported READ-ONLY) and compares it to the
  record's own ``broker_key``; any mismatch degrades the ref to "absent"
  and every deal is orphaned with ``integrity=MISMATCH_BROKER_REF`` rather
  than silently trusting a possibly-corrupted binding.

PURITY CONTRACT — this module NEVER:
- reads an environment variable, a ``.env`` file, the wall clock, or MT5;
- performs network I/O;
- imports anything beyond: stdlib (``hashlib``, ``json``, ``threading``,
  ``dataclasses.dataclass``, ``datetime.{datetime,timezone}``,
  ``decimal.{Decimal,InvalidOperation,ROUND_HALF_EVEN}``),
  ``app.services.event_journal`` (``JournalReader``, ``JournalWriter`` —
  M03's append-only primitives, reused, never reimplemented) and
  ``app.services.lifecycle_identity_store`` (``broker_ref_key``,
  ``try_build_broker_ref`` — READ-ONLY reuse of the store's own composite
  reference algebra, never a write against a real store).
``app.services.order_intent`` is intentionally NOT imported: this module
never depends on ``OrderIntentBook`` at runtime. ``reconcile_intent_lifecycle_pnl``
instead documents the exact plain-``dict``/``list`` shape it expects for an
intent snapshot (identical in shape to what
``OrderIntentBook.get()``/``active_intents()`` already return), so a caller
that DOES hold a real book can pass its snapshots straight through, while a
test can build the same fixtures with zero coupling to that module.

DURABILITY (``PnlLedger``) : every ``compute_and_record`` call journals
EXACTLY one ``{op: "pnl_computed", lifecycle_id, net_profit (str(Decimal)),
computed_over_deals, deals_digest}`` record through a M03
``JournalWriter`` — append-only, same fail-closed/single-writer/hash-chain
guarantees as every other M02/M03 module. ``deals_digest`` is the first 16
hex chars of the SHA-256 of the deals list in CANONICAL (sorted, fully
Decimal-as-str) form. Recomputing the SAME ``(lifecycle_id, deals_digest)``
never writes a second line — it returns ``ledger_status="ALREADY_COMPUTED"``
(idempotence). A DIFFERENT ``deals_digest`` for the SAME ``lifecycle_id``
(new deals arrived, or a genuine recompute) always appends a NEW, distinct,
versioned entry — the old entry is NEVER overwritten or deleted.
``rebuild()`` replays the whole journal from scratch and rebuilds the
in-memory dedup index; it is itself pure/idempotent (calling it twice in a
row yields byte-identical state) and is also run once automatically at
``PnlLedger.__init__`` so a reopened ledger immediately knows what was
already computed before the restart.

``reconcile_intent_lifecycle_pnl`` is a PURE, read-only, in-memory join. It
never mutates any of its three input snapshots and never corrects an
anomaly — it only TYPES and REPORTS them (a future M06 owns correction).
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from app.services.event_journal import JournalReader, JournalWriter
from app.services.lifecycle_identity_store import broker_ref_key, try_build_broker_ref

USD_QUANT = Decimal("0.01")
PNL_ROUNDING = ROUND_HALF_EVEN  # banker's rounding; applied ONCE, at the end

DEAL_KIND_ENTRY = "ENTRY"
DEAL_KIND_EXIT = "EXIT"
DEAL_KIND_ADJUSTMENT = "ADJUSTMENT"
_ALLOWED_KINDS = frozenset({DEAL_KIND_ENTRY, DEAL_KIND_EXIT, DEAL_KIND_ADJUSTMENT})

_DEAL_FIELDS = frozenset({
    "deal_id", "position_id", "ticket", "kind", "volume", "price",
    "profit", "commission", "swap", "fee", "at_utc", "symbol",
})

LEDGER_OP = "pnl_computed"
_LEDGER_PAYLOAD_FIELDS = frozenset({
    "op", "lifecycle_id", "net_profit", "computed_over_deals", "deals_digest",
})

_INTENT_STATE_FILLED = "FILLED"


# --------------------------------------------------------------------------- #
# Exceptions (sanitized: reason_code only, never a raw value/secret)
# --------------------------------------------------------------------------- #
class LifecyclePnlError(Exception):
    """Base. ``reason_code`` = sanitized machine diagnostic."""

    reason_code = "LIFECYCLE_PNL_ERROR"

    def __init__(self, reason_code: str = "LIFECYCLE_PNL_ERROR"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class DealValidationError(LifecyclePnlError):
    reason_code = "DEAL_VALIDATION_FAILED"


# --------------------------------------------------------------------------- #
# Strict, side-effect-free coercion helpers
# --------------------------------------------------------------------------- #
def _coerce_strict_int(value: object, field_name: str) -> int:
    """Only a real, non-bool, positive ``int`` is accepted — never a numeric
    string (identifiers are never coerced from text, unlike money fields)."""
    if type(value) is not int or value <= 0:
        raise DealValidationError("%s_INVALID" % field_name)
    return value


def _coerce_decimal(value: object, field_name: str) -> Decimal:
    """Accepts a ``Decimal`` as-is, a non-bool plain ``int`` (exact), or a
    non-empty ``str`` converted EXPLICITLY via ``Decimal(str_value)`` — never
    via a float round-trip. ``float``/``bool`` are explicitly REJECTED:
    money is never float in this module."""
    if isinstance(value, bool):
        raise DealValidationError("%s_INVALID" % field_name)
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        if not value.strip():
            raise DealValidationError("%s_INVALID" % field_name)
        try:
            d = Decimal(value)  # Decimal(str) explicit -- never Decimal(float)
        except InvalidOperation:
            raise DealValidationError("%s_INVALID" % field_name) from None
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        raise DealValidationError("%s_INVALID" % field_name)
    if not d.is_finite():
        raise DealValidationError("%s_INVALID" % field_name)
    return d


def _parse_iso_utc(value: object) -> "str | None":
    """Returns the canonical UTC ``isoformat()`` string, or ``None`` if
    ``value`` is not a non-empty, timezone-aware ISO-8601 string."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(USD_QUANT, rounding=PNL_ROUNDING)


# --------------------------------------------------------------------------- #
# DealRecord — strict validation, never a silent conversion
# --------------------------------------------------------------------------- #
def _validate_deal_invariants(
    *, deal_id: object, position_id: object, ticket: object, kind: object,
    volume: object, price: object, profit: object, commission: object,
    swap: object, fee: object, at_utc: object, symbol: object,
) -> None:
    """Invariant check assuming ALREADY-typed python values. Runs on EVERY
    ``DealRecord`` construction path (``from_mapping`` and the raw
    dataclass constructor both funnel through ``__post_init__``), so a
    ``DealRecord`` instance is trustworthy by construction everywhere else
    in this module."""
    if type(deal_id) is not int or deal_id <= 0:
        raise DealValidationError("DEAL_ID_INVALID")
    if type(position_id) is not int or position_id <= 0:
        raise DealValidationError("POSITION_ID_INVALID")
    if type(ticket) is not int or ticket <= 0:
        raise DealValidationError("TICKET_INVALID")
    if kind not in _ALLOWED_KINDS:
        raise DealValidationError("KIND_INVALID")
    for value, name in ((volume, "VOLUME"), (price, "PRICE")):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise DealValidationError("%s_INVALID" % name)
    for value, name in (
        (profit, "PROFIT"), (commission, "COMMISSION"), (swap, "SWAP"), (fee, "FEE"),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise DealValidationError("%s_INVALID" % name)
    # at_utc must already be the canonical isoformat() fixpoint -- re-parsing
    # a canonical string is a no-op; anything else was hand-crafted/foreign.
    if not isinstance(at_utc, str) or _parse_iso_utc(at_utc) != at_utc:
        raise DealValidationError("AT_UTC_INVALID")
    if not isinstance(symbol, str) or not symbol.strip():
        raise DealValidationError("SYMBOL_INVALID")


@dataclass(frozen=True)
class DealRecord:
    """One broker deal line (fill), strictly validated. See module
    docstring for the money/typing discipline. Construct via
    ``DealRecord.from_mapping`` for raw external input (str/int money
    coercion) — the raw dataclass constructor is also strictly validated
    (``__post_init__``) but expects ALREADY-typed values (``Decimal`` for
    money, canonical ISO string for ``at_utc``)."""

    deal_id: int
    position_id: int
    ticket: int
    kind: str
    volume: Decimal
    price: Decimal
    profit: Decimal
    commission: Decimal
    swap: Decimal
    fee: Decimal
    at_utc: str
    symbol: str

    def __post_init__(self) -> None:
        _validate_deal_invariants(
            deal_id=self.deal_id, position_id=self.position_id, ticket=self.ticket,
            kind=self.kind, volume=self.volume, price=self.price, profit=self.profit,
            commission=self.commission, swap=self.swap, fee=self.fee,
            at_utc=self.at_utc, symbol=self.symbol,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> "DealRecord":
        """Strict field-set + type coercion from an external mapping (e.g. a
        JSON-decoded fixture). Exactly the 12 fields of ``_DEAL_FIELDS`` are
        allowed — missing or extra fields are rejected."""
        if not isinstance(raw, dict):
            raise DealValidationError("DEAL_NOT_DICT")
        keys = set(raw.keys())
        missing = _DEAL_FIELDS - keys
        if missing:
            raise DealValidationError("DEAL_MISSING_FIELDS:%s" % ",".join(sorted(missing)))
        unknown = keys - _DEAL_FIELDS
        if unknown:
            raise DealValidationError("DEAL_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))

        deal_id = _coerce_strict_int(raw["deal_id"], "DEAL_ID")
        position_id = _coerce_strict_int(raw["position_id"], "POSITION_ID")
        ticket = _coerce_strict_int(raw["ticket"], "TICKET")

        kind = raw["kind"]
        if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
            raise DealValidationError("KIND_INVALID")

        volume = _coerce_decimal(raw["volume"], "VOLUME")
        price = _coerce_decimal(raw["price"], "PRICE")
        profit = _coerce_decimal(raw["profit"], "PROFIT")
        commission = _coerce_decimal(raw["commission"], "COMMISSION")
        swap = _coerce_decimal(raw["swap"], "SWAP")
        fee = _coerce_decimal(raw["fee"], "FEE")

        canonical_at = _parse_iso_utc(raw["at_utc"])
        if canonical_at is None:
            raise DealValidationError("AT_UTC_INVALID")

        symbol = raw["symbol"]
        if not isinstance(symbol, str) or not symbol.strip():
            raise DealValidationError("SYMBOL_INVALID")

        return cls(
            deal_id=deal_id, position_id=position_id, ticket=ticket, kind=kind,
            volume=volume, price=price, profit=profit, commission=commission,
            swap=swap, fee=fee, at_utc=canonical_at, symbol=symbol,
        )


# --------------------------------------------------------------------------- #
# Matching (deal -> lifecycle), never by ticket alone
# --------------------------------------------------------------------------- #
def _dewrap_opened_at(canonical: object) -> "str | int | None":
    """Reverses the ``iso:``/``msc:`` canonical wrapping applied by
    ``lifecycle_identity_store.try_build_broker_ref`` so the SAME function
    can be used to re-derive (and verify) an already-stored ref."""
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


def _extract_verified_broker_ref(lifecycle_record: dict) -> "tuple[dict | None, bool]":
    """Defense in depth: never trust ``lifecycle_record['broker_ref']``
    blindly. Returns ``(ref_or_None, integrity_ok)``.

    - Legitimately unbound (``broker_ref`` is ``None`` and so is
      ``broker_key``) -> ``(None, True)`` -- NOT a corruption.
    - Structurally invalid, unparsable, or whose re-derived
      ``broker_ref_key`` disagrees with the stored ``broker_key`` ->
      ``(None, False)`` -- treated as corrupted, every deal orphaned.
    - Otherwise -> ``(raw_ref, True)`` (the ORIGINAL ref is returned, never
      the rebuilt one, so no re-derivation drift ever reaches matching).
    """
    raw_ref = lifecycle_record.get("broker_ref")
    stored_key = lifecycle_record.get("broker_key")
    if raw_ref is None and stored_key is None:
        return None, True
    if not isinstance(raw_ref, dict) or not isinstance(stored_key, str) or not stored_key:
        return None, False
    opened_at = _dewrap_opened_at(raw_ref.get("opened_at"))
    if opened_at is None:
        return None, False
    rebuilt, _reason = try_build_broker_ref(
        account_scope_id=raw_ref.get("account_scope_id"),
        ticket=raw_ref.get("ticket"),
        broker_symbol=raw_ref.get("broker_symbol"),
        opened_at=opened_at,
        magic=raw_ref.get("magic"),
        position_identifier=raw_ref.get("position_identifier"),
        direction=raw_ref.get("direction"),
    )
    if rebuilt is None:
        return None, False
    if broker_ref_key(rebuilt) != stored_key:
        return None, False
    return raw_ref, True


def _deal_matches_ref(deal: DealRecord, ref: "dict | None") -> "tuple[bool, str | None]":
    if ref is None:
        return False, "NO_BROKER_REF"
    symbol = ref.get("broker_symbol")
    if symbol is not None and deal.symbol != symbol:
        return False, "SYMBOL_MISMATCH"
    position_identifier = ref.get("position_identifier")
    if position_identifier is not None:
        # ticket is NEVER consulted here -- position_identifier alone
        # discriminates recycled tickets.
        if deal.position_id != position_identifier:
            return False, "POSITION_ID_MISMATCH"
        return True, None
    ticket = ref.get("ticket")
    if deal.ticket != ticket:
        return False, "TICKET_MISMATCH"
    return True, None


def _weighted_avg(deals: "list[DealRecord]") -> "Decimal | None":
    total_volume = sum((d.volume for d in deals), Decimal("0"))
    if total_volume == 0:
        return None
    total_notional = sum((d.price * d.volume for d in deals), Decimal("0"))
    return total_notional / total_volume


# --------------------------------------------------------------------------- #
# LifecyclePnl — pure aggregator, no instance state
# --------------------------------------------------------------------------- #
class LifecyclePnl:
    """Pure aggregator of ``DealRecord`` objects against one
    ``LifecycleIdentityStore`` record. Stateless by design — see module
    docstring for the full matching/rounding contract."""

    @staticmethod
    def compute(lifecycle_record: dict, deals: "list[DealRecord]") -> dict:
        if not isinstance(lifecycle_record, dict):
            raise LifecyclePnlError("LIFECYCLE_RECORD_NOT_DICT")
        lifecycle_id = lifecycle_record.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise LifecyclePnlError("LIFECYCLE_ID_INVALID")
        if not isinstance(deals, list):
            raise LifecyclePnlError("DEALS_NOT_LIST")
        for d in deals:
            if not isinstance(d, DealRecord):
                raise LifecyclePnlError("DEALS_MUST_BE_DEALRECORD")

        broker_ref, ref_integrity_ok = _extract_verified_broker_ref(lifecycle_record)

        matched: "list[DealRecord]" = []
        rejected: "list[dict]" = []
        for d in deals:
            ok, reason = _deal_matches_ref(d, broker_ref if ref_integrity_ok else None)
            if ok:
                matched.append(d)
            else:
                rejected.append({"deal_id": d.deal_id, "reason": reason})

        zero = Decimal("0")
        gross_profit = sum((d.profit for d in matched), zero)
        commission = sum((d.commission for d in matched), zero)
        swap = sum((d.swap for d in matched), zero)
        fees = sum((d.fee for d in matched), zero)
        net_profit_exact = gross_profit + commission + swap + fees

        entries = [d for d in matched if d.kind == DEAL_KIND_ENTRY]
        exits = [d for d in matched if d.kind == DEAL_KIND_EXIT]
        volume_entered = sum((d.volume for d in entries), zero)
        volume_exited = sum((d.volume for d in exits), zero)

        timeline = [
            {
                "deal_id": d.deal_id, "kind": d.kind, "at_utc": d.at_utc,
                "price": d.price, "volume": d.volume, "profit": d.profit,
                "commission": d.commission, "swap": d.swap, "fee": d.fee,
            }
            for d in sorted(matched, key=lambda d: (d.at_utc, d.deal_id))
        ]

        orphan_deals = len(rejected)
        if not ref_integrity_ok:
            integrity = "MISMATCH_BROKER_REF"
        elif orphan_deals > 0:
            integrity = "MISMATCH_ORPHAN_DEALS"
        else:
            integrity = "OK"

        return {
            "lifecycle_id": lifecycle_id,
            "gross_profit": _quantize_usd(gross_profit),
            "commission": _quantize_usd(commission),
            "swap": _quantize_usd(swap),
            "fees": _quantize_usd(fees),
            "net_profit": _quantize_usd(net_profit_exact),
            "volume_entered": volume_entered,
            "volume_exited": volume_exited,
            "entry_avg_price": _weighted_avg(entries),
            "exit_avg_price": _weighted_avg(exits),
            "timeline": timeline,
            "flags": {
                "fully_closed": volume_exited == volume_entered,
                "orphan_deals": orphan_deals,
                "integrity": integrity,
            },
            "rejected": rejected,
        }


# --------------------------------------------------------------------------- #
# PnlLedger — durable, idempotent, append-only (M03)
# --------------------------------------------------------------------------- #
def _deals_digest(deals: "list[DealRecord]") -> str:
    """SHA-256 (first 16 hex chars) of the deals list in CANONICAL form:
    sorted tuples, every ``Decimal`` rendered via ``str()`` (never
    ``float()``), serialized with ``sort_keys``-equivalent determinism."""
    canonical = sorted(
        (
            d.deal_id, d.position_id, d.ticket, d.kind, str(d.volume), str(d.price),
            str(d.profit), str(d.commission), str(d.swap), str(d.fee), d.at_utc, d.symbol,
        )
        for d in deals
    )
    payload = json.dumps(canonical, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class PnlLedger:
    """Durable, idempotent record of every PnL computation, backed by a M03
    append-only journal. See module docstring for the full contract."""

    def __init__(
        self,
        journal_path,
        *,
        allowed_dir=None,
        writer_token: "str | None" = None,
    ) -> None:
        self._path = journal_path
        self._writer = JournalWriter(journal_path, allowed_dir=allowed_dir, writer_token=writer_token)
        self._lock = threading.RLock()
        self._index: "dict[str, list[dict]]" = {}
        self._seen_digests: "set[tuple[str, str]]" = set()
        self.rebuild()

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._writer.close()

    def __enter__(self) -> "PnlLedger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------- replay
    def rebuild(self) -> dict:
        """Replays the WHOLE journal via a FRESH ``JournalReader`` and
        rebuilds the in-memory dedup index from scratch. Pure/idempotent:
        calling it twice yields byte-identical state. Never raises on a
        structurally foreign payload (skipped, counted as ``ignored``) — a
        low-level hash-chain corruption still propagates unchanged from
        ``JournalReader.replay()`` (``JournalCorruptedError``), fail-closed."""
        with self._lock:
            payloads = JournalReader(self._path).replay()
            index: "dict[str, list[dict]]" = {}
            seen: "set[tuple[str, str]]" = set()
            applied = 0
            ignored = 0
            for payload in payloads:
                if not isinstance(payload, dict) or set(payload.keys()) != _LEDGER_PAYLOAD_FIELDS:
                    ignored += 1
                    continue
                if payload.get("op") != LEDGER_OP:
                    ignored += 1
                    continue
                lifecycle_id = payload.get("lifecycle_id")
                deals_digest = payload.get("deals_digest")
                net_profit = payload.get("net_profit")
                computed_over_deals = payload.get("computed_over_deals")
                valid = (
                    isinstance(lifecycle_id, str) and lifecycle_id
                    and isinstance(deals_digest, str) and deals_digest
                    and isinstance(net_profit, str)
                    and type(computed_over_deals) is int and computed_over_deals >= 0
                )
                if not valid:
                    ignored += 1
                    continue
                entry = {
                    "lifecycle_id": lifecycle_id, "net_profit": net_profit,
                    "computed_over_deals": computed_over_deals, "deals_digest": deals_digest,
                }
                index.setdefault(lifecycle_id, []).append(entry)
                seen.add((lifecycle_id, deals_digest))
                applied += 1
            self._index = index
            self._seen_digests = seen
            return {"applied": applied, "ignored": ignored, "lifecycles": len(index)}

    # ------------------------------------------------------------- API
    def compute_and_record(self, lifecycle_record: dict, deals: "list[DealRecord]") -> dict:
        """Computes via ``LifecyclePnl.compute`` then journals the result
        (dedup by ``(lifecycle_id, deals_digest)``). Returns
        ``{ledger_status: "RECORDED"|"ALREADY_COMPUTED", pnl, deals_digest}``.
        ``ALREADY_COMPUTED`` writes NOTHING to the journal (true no-op)."""
        if not isinstance(lifecycle_record, dict):
            raise LifecyclePnlError("LIFECYCLE_RECORD_NOT_DICT")
        lifecycle_id = lifecycle_record.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise LifecyclePnlError("LIFECYCLE_ID_INVALID")

        pnl = LifecyclePnl.compute(lifecycle_record, deals)
        deals_digest = _deals_digest(deals)

        with self._lock:
            key = (lifecycle_id, deals_digest)
            if key in self._seen_digests:
                return {"ledger_status": "ALREADY_COMPUTED", "pnl": pnl, "deals_digest": deals_digest}
            payload = {
                "op": LEDGER_OP,
                "lifecycle_id": lifecycle_id,
                "net_profit": str(pnl["net_profit"]),
                "computed_over_deals": len(deals),
                "deals_digest": deals_digest,
            }
            self._writer.append(payload)
            self._index.setdefault(lifecycle_id, []).append(dict(payload))
            self._seen_digests.add(key)
            return {"ledger_status": "RECORDED", "pnl": pnl, "deals_digest": deals_digest}

    def history(self, lifecycle_id: str) -> "list[dict]":
        with self._lock:
            return [dict(e) for e in self._index.get(lifecycle_id, [])]

    def snapshot(self) -> dict:
        """``{"lifecycles": {lifecycle_id: [entry, ...]}}`` — the exact
        shape expected as ``ledger_state`` by
        ``reconcile_intent_lifecycle_pnl``."""
        with self._lock:
            return {
                "lifecycles": {
                    lid: [dict(e) for e in entries] for lid, entries in self._index.items()
                }
            }


# --------------------------------------------------------------------------- #
# reconcile_intent_lifecycle_pnl — pure, in-memory, detection only
# --------------------------------------------------------------------------- #
def reconcile_intent_lifecycle_pnl(
    intent_book_state: dict, store_state: dict, ledger_state: dict,
) -> dict:
    """PURE in-memory join across three independent snapshots. Never
    mutates any input, never corrects an anomaly (a future M06 owns
    correction) -- detection only.

    Expected shapes:
    - ``intent_book_state = {"intents": [snapshot, ...]}`` where each
      ``snapshot`` has the same shape as
      ``order_intent.OrderIntentBook.get()``/``active_intents()`` entries:
      ``{intent_id, state, request: {volume, ...}, links:
      {lifecycle_id, correlation_id, setup_id} | None, ...}``.
    - ``store_state = {"lifecycles": {lifecycle_id: record}}`` where each
      ``record`` has the same shape as a
      ``lifecycle_identity_store.LifecycleIdentityStore`` record
      (``state`` in ``{"OPEN", "CLOSED"}``, ``correlation_id``, ...) --
      identical to the ``"lifecycles"`` key of
      ``event_journal.rebuild_lifecycle_state()``'s return value.
    - ``ledger_state = {"lifecycles": {lifecycle_id: [entry, ...]}}`` --
      exactly ``PnlLedger.snapshot()``'s return shape.

    Anomaly types detected:
    - ``INTENT_FILLED_SANS_LIFECYCLE`` : a ``FILLED`` intent whose link
      never resolves to a known lifecycle (missing link, or a link that
      points nowhere in ``store_state``).
    - ``INTENT_LIFECYCLE_UNKNOWN`` : ANY intent (any state) whose
      ``links.lifecycle_id`` points to a lifecycle absent from
      ``store_state`` (broader than the FILLED-only case above).
    - ``VOLUME_MISMATCH`` : two or more intents linked to the SAME
      lifecycle declare different ``request.volume`` values.
    - ``LIFECYCLE_CLOSED_SANS_PNL`` : a ``CLOSED`` lifecycle with zero
      ledger entries.
    - ``PNL_SANS_LIFECYCLE`` : a ledger entry references a lifecycle_id
      absent from ``store_state``.
    A healthy, fully-consistent input set produces zero anomalies.
    """
    if not isinstance(intent_book_state, dict) or "intents" not in intent_book_state:
        raise LifecyclePnlError("INTENT_BOOK_STATE_INVALID")
    if not isinstance(store_state, dict) or "lifecycles" not in store_state:
        raise LifecyclePnlError("STORE_STATE_INVALID")
    if not isinstance(ledger_state, dict) or "lifecycles" not in ledger_state:
        raise LifecyclePnlError("LEDGER_STATE_INVALID")

    intents = intent_book_state["intents"]
    lifecycles = store_state["lifecycles"]
    ledger_lifecycles = ledger_state["lifecycles"]
    if not isinstance(intents, list) or not isinstance(lifecycles, dict) \
            or not isinstance(ledger_lifecycles, dict):
        raise LifecyclePnlError("RECONCILE_INPUT_SHAPE_INVALID")

    by_correlation = {
        record.get("correlation_id"): lid
        for lid, record in lifecycles.items()
        if isinstance(record, dict) and record.get("correlation_id")
    }

    anomalies: "list[dict]" = []
    volume_by_lifecycle: "dict[str, list[tuple[object, object]]]" = {}

    for intent in intents:
        if not isinstance(intent, dict):
            continue
        intent_id = intent.get("intent_id")
        links = intent.get("links") if isinstance(intent.get("links"), dict) else {}
        lifecycle_id = links.get("lifecycle_id")
        if lifecycle_id is None:
            correlation_id = links.get("correlation_id")
            lifecycle_id = by_correlation.get(correlation_id) if correlation_id else None

        if lifecycle_id is not None:
            request = intent.get("request") if isinstance(intent.get("request"), dict) else {}
            volume_by_lifecycle.setdefault(lifecycle_id, []).append((intent_id, request.get("volume")))
            if links.get("lifecycle_id") is not None and lifecycle_id not in lifecycles:
                anomalies.append({
                    "type": "INTENT_LIFECYCLE_UNKNOWN",
                    "intent_id": intent_id, "lifecycle_id": lifecycle_id,
                })

        if intent.get("state") == _INTENT_STATE_FILLED:
            if lifecycle_id is None or lifecycle_id not in lifecycles:
                anomalies.append({
                    "type": "INTENT_FILLED_SANS_LIFECYCLE",
                    "intent_id": intent_id, "lifecycle_id": lifecycle_id,
                })

    for lifecycle_id, pairs in volume_by_lifecycle.items():
        distinct = {volume for _intent_id, volume in pairs if volume is not None}
        if len(distinct) > 1:
            anomalies.append({
                "type": "VOLUME_MISMATCH",
                "lifecycle_id": lifecycle_id,
                "volumes": sorted(str(v) for v in distinct),
            })

    for lifecycle_id, record in lifecycles.items():
        if isinstance(record, dict) and record.get("state") == "CLOSED":
            if not ledger_lifecycles.get(lifecycle_id):
                anomalies.append({"type": "LIFECYCLE_CLOSED_SANS_PNL", "lifecycle_id": lifecycle_id})

    for lifecycle_id in ledger_lifecycles:
        if lifecycle_id not in lifecycles:
            anomalies.append({"type": "PNL_SANS_LIFECYCLE", "lifecycle_id": lifecycle_id})

    counts: "dict[str, int]" = {}
    for anomaly in anomalies:
        counts[anomaly["type"]] = counts.get(anomaly["type"], 0) + 1

    return {
        "anomalies": anomalies,
        "counts": counts,
        "checked": {
            "intents": len(intents),
            "lifecycles": len(lifecycles),
            "ledger_lifecycles": len(ledger_lifecycles),
        },
    }
