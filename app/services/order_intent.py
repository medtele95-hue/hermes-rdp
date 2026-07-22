"""ORDER_INTENT — DORMANT laboratory book for order-intent identity (M04-P1).

MISSION 100% LABORATOIRE : ce module n'est cable nulle part en production
(aucun import par main.py / demo_router.py / lifecycle_capture.py). Il ne
soumet RIEN a MT5 et n'appelle jamais l'API MT5 d'envoi ou de verification
d'ordre, meme en simulation.

Une "intention d'ordre" (``ORDER_INTENT``) est l'identite d'une decision de
trading FRAPPEE AVANT toute soumission broker : elle existe pour que deux
appels equivalents (meme cycle de decision, meme setup, meme symbole,
direction, volume, magic, sl, tp) ne produisent jamais deux soumissions
distinctes (anti-double-envoi), et pour que l'etat d'une intention
(``CREATED -> SUBMITTED -> {FILLED|REJECTED|CANCELLED|EXPIRED}``) soit
suivi par une machine a etats stricte, fail-closed, persistee en
append-only via ``app.services.event_journal`` (M03).

CE MODULE NE SOUMET JAMAIS RIEN A UN BROKER : ``OrderIntentBook`` frappe et
suit des INTENTIONS. La soumission (l'appel reel a l'API MT5 d'envoi
d'ordres) est la responsabilite d'une future mission explicitement gatee ;
aucune methode de ce module n'appelle, ne simule, ni ne prepare un appel
broker.

PURETE (M02/M03) — ce module NEVER :
- ne lit aucune variable d'environnement ni fichier ``.env``, ne lit l'heure
  systeme, n'ouvre de connexion reseau ou MT5 ;
- ``created_at_utc``/``at_utc`` (transitions) sont TOUJOURS INJECTES par
  l'appelant, jamais lus depuis une horloge locale ;
- n'importe que des symboles de LECTURE depuis
  ``app.services.event_identity`` (``MonotonicUUID7Generator``,
  ``build_idempotency_key``, ``validate_event_id``) et
  ``app.services.event_journal`` (``JournalWriter``, ``JournalReader``) —
  la persistance et l'identite restent la propriete exclusive de M02/M03,
  ce module ne les reimplemente pas.

MACHINE A ETATS (stricte, fail-closed) :
    CREATED -> SUBMITTED -> {FILLED | REJECTED | CANCELLED | EXPIRED}
Les 4 etats terminaux n'ont AUCUNE transition sortante. Toute transition
non listee ci-dessus (y compris depuis/vers un etat inconnu, ou sur un
``intent_id`` inexistant) leve ``IntentTransitionError`` — l'etat en
memoire et le journal ne sont JAMAIS modifies avant que TOUTES les
verifications aient reussi (jamais d'ecrasement partiel). Re-transitionner
vers l'etat DEJA courant est un no-op idempotent (``ALREADY_<STATE>``, zero
ecriture journal).

DEDUPLICATION (anti-double-envoi) : ``idempotency_key`` est deterministe
(``build_idempotency_key("ORDER_INTENT", symbol, direction, volume, magic,
sl, tp, setup_id, cycle_id)``). Tant qu'une intention NON-terminale existe
deja pour cette cle, un nouvel appel a ``create_intent`` avec la MEME cle
renvoie l'intention EXISTANTE (``deduplicated: True``) au lieu d'en creer
une seconde : il ne peut jamais exister deux intentions actives pour la
meme intention. Une fois l'intention devenue terminale, la cle est
liberee : une nouvelle tentative legitime (retry apres REJECTED/EXPIRED/
CANCELLED, ou nouveau FILLED) cree une intention neuve.
"""
from __future__ import annotations

import math
import threading
from typing import Any

from app.services.event_identity import (
    MonotonicUUID7Generator,
    build_idempotency_key,
    validate_event_id,
)
from app.services.event_journal import JournalReader, JournalWriter

# --------------------------------------------------------------------------- #
# Exceptions (fail-closed ; jamais de secret/compte reel dans un message)
# --------------------------------------------------------------------------- #
class OrderIntentError(Exception):
    """Base error for the order-intent laboratory book."""


class IntentValidationError(OrderIntentError):
    """A ``create_intent``/``transition`` argument fails strict validation."""


class IntentTransitionError(OrderIntentError):
    """An illegal state transition was attempted.

    Fail-closed: raised BEFORE any in-memory state or journal mutation, so
    an illegal transition never overwrites the existing state.
    """


class IntentNotFoundError(IntentTransitionError):
    """``transition()`` targeted an ``intent_id`` that does not exist."""


class IntentReplayError(OrderIntentError):
    """``rebuild_from_journal`` found a structurally invalid/corrupted
    ``ORDER_INTENT`` payload (distinct from the lower-level hash-chain
    corruption already detected by ``JournalReader`` itself)."""


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
STATE_CREATED = "CREATED"
STATE_SUBMITTED = "SUBMITTED"
STATE_FILLED = "FILLED"
STATE_REJECTED = "REJECTED"
STATE_CANCELLED = "CANCELLED"
STATE_EXPIRED = "EXPIRED"

_TERMINAL_STATES = frozenset({STATE_FILLED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED})
_ALL_STATES = frozenset({STATE_CREATED, STATE_SUBMITTED} | _TERMINAL_STATES)
_TRANSITIONS: "dict[str, frozenset[str]]" = {
    STATE_CREATED: frozenset({STATE_SUBMITTED}),
    STATE_SUBMITTED: frozenset({STATE_FILLED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED}),
    STATE_FILLED: frozenset(),
    STATE_REJECTED: frozenset(),
    STATE_CANCELLED: frozenset(),
    STATE_EXPIRED: frozenset(),
}

_DETAIL_MAX_LEN = 200

# --------------------------------------------------------------------------- #
# Request validation (strict types, no silent conversion, bool != int)
# --------------------------------------------------------------------------- #
_REQUIRED_REQUEST_FIELDS = frozenset({"symbol", "direction", "volume", "magic"})
_OPTIONAL_REQUEST_FIELDS = frozenset({"sl", "tp", "price"})
_ALL_REQUEST_FIELDS = _REQUIRED_REQUEST_FIELDS | _OPTIONAL_REQUEST_FIELDS
_DIRECTIONS = frozenset({"BUY", "SELL"})

_PROVENANCE_FIELDS = frozenset({"boot_id", "cycle_id"})
_LINKS_FIELDS = frozenset({"setup_id", "correlation_id", "lifecycle_id"})

_INTENT_CREATED_PAYLOAD_FIELDS = frozenset({
    "op", "intent_id", "idempotency_key", "state", "request", "provenance",
    "links", "created_at_utc", "history",
})
_INTENT_TRANSITION_PAYLOAD_FIELDS = frozenset({
    "op", "intent_id", "from_state", "to_state", "at_utc", "detail",
})


def _is_plain_finite_float(value: object) -> bool:
    return type(value) is float and math.isfinite(value)


def _validate_request(request: object) -> "dict[str, Any]":
    if not isinstance(request, dict):
        raise IntentValidationError("REQUEST_NOT_DICT")
    keys = set(request.keys())
    missing = _REQUIRED_REQUEST_FIELDS - keys
    if missing:
        raise IntentValidationError("REQUEST_MISSING_FIELDS:%s" % ",".join(sorted(missing)))
    unknown = keys - _ALL_REQUEST_FIELDS
    if unknown:
        raise IntentValidationError("REQUEST_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))

    symbol = request["symbol"]
    if not isinstance(symbol, str) or not symbol:
        raise IntentValidationError("SYMBOL_INVALID")

    direction = request["direction"]
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise IntentValidationError("DIRECTION_INVALID")

    volume = request["volume"]
    if not _is_plain_finite_float(volume) or volume <= 0:
        raise IntentValidationError("VOLUME_INVALID")

    magic = request["magic"]
    if type(magic) is not int or magic < 0:
        raise IntentValidationError("MAGIC_INVALID")

    normalized: "dict[str, Any]" = {
        "symbol": symbol,
        "direction": direction,
        "volume": volume,
        "magic": magic,
    }
    for optional_field in ("sl", "tp", "price"):
        if optional_field in request:
            value = request[optional_field]
            if not _is_plain_finite_float(value):
                raise IntentValidationError("%s_INVALID" % optional_field.upper())
            normalized[optional_field] = value
        else:
            normalized[optional_field] = None
    return normalized


def _validate_idempotency_part(value: object, field_name: str) -> object:
    """setup_id / cycle_id: None, non-bool int, finite float, or non-empty str."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise IntentValidationError("%s_INVALID" % field_name)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IntentValidationError("%s_INVALID" % field_name)
        return value
    if isinstance(value, str):
        if not value:
            raise IntentValidationError("%s_INVALID" % field_name)
        return value
    raise IntentValidationError("%s_INVALID" % field_name)


def _validate_provenance(provenance: object) -> "dict[str, Any] | None":
    if provenance is None:
        return None
    if not isinstance(provenance, dict):
        raise IntentValidationError("PROVENANCE_NOT_DICT")
    unknown = set(provenance.keys()) - _PROVENANCE_FIELDS
    if unknown:
        raise IntentValidationError("PROVENANCE_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))
    boot_id = provenance.get("boot_id")
    if boot_id is not None and (not isinstance(boot_id, str) or not boot_id):
        raise IntentValidationError("BOOT_ID_INVALID")
    cycle_id = _validate_idempotency_part(provenance.get("cycle_id"), "CYCLE_ID")
    return {"boot_id": boot_id, "cycle_id": cycle_id}


def _validate_links(links: object) -> "dict[str, Any] | None":
    if links is None:
        return None
    if not isinstance(links, dict):
        raise IntentValidationError("LINKS_NOT_DICT")
    unknown = set(links.keys()) - _LINKS_FIELDS
    if unknown:
        raise IntentValidationError("LINKS_UNKNOWN_FIELDS:%s" % ",".join(sorted(unknown)))
    setup_id = _validate_idempotency_part(links.get("setup_id"), "SETUP_ID")
    correlation_id = links.get("correlation_id")
    if correlation_id is not None:
        try:
            correlation_id = validate_event_id(correlation_id)
        except Exception as exc:  # EventIdentityValidationError, not imported (see module docstring)
            raise IntentValidationError("CORRELATION_ID_INVALID") from exc
    lifecycle_id = links.get("lifecycle_id")
    if lifecycle_id is not None and (not isinstance(lifecycle_id, str) or not lifecycle_id):
        raise IntentValidationError("LIFECYCLE_ID_INVALID")
    return {"setup_id": setup_id, "correlation_id": correlation_id, "lifecycle_id": lifecycle_id}


def _sanitize_detail(detail: object) -> "str | None":
    if detail is None:
        return None
    if not isinstance(detail, str):
        raise IntentValidationError("DETAIL_NOT_STR")
    cleaned_chars = [ch if ch.isprintable() else " " for ch in detail]
    cleaned = " ".join("".join(cleaned_chars).split())
    return cleaned[:_DETAIL_MAX_LEN]


def _require_nonempty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntentValidationError("%s_INVALID" % field_name)
    return value


# --------------------------------------------------------------------------- #
# OrderIntentBook
# --------------------------------------------------------------------------- #
class OrderIntentBook:
    """Laboratory book of order intentions, backed by an M03 append-only
    journal. See module docstring for the full contract.

    THIS CLASS NEVER SUBMITS ANYTHING TO A BROKER. It only creates, tracks
    and transitions the IDENTITY of an intention to place an order — no
    method here calls, simulates, or prepares a call to the MT5 order-send
    or order-check API. Broker submission belongs to a separate, explicitly
    gated future mission.
    """

    def __init__(
        self,
        journal_path,
        *,
        uuid_generator: MonotonicUUID7Generator,
        allowed_dir=None,
    ) -> None:
        if uuid_generator is None:
            raise IntentValidationError("UUID_GENERATOR_REQUIRED")
        self._uuid = uuid_generator
        self._writer = JournalWriter(journal_path, allowed_dir=allowed_dir)
        self._lock = threading.RLock()
        self._intents: "dict[str, dict]" = {}
        self._active_by_key: "dict[str, str]" = {}
        self._diag = {
            "created": 0,
            "deduplicated": 0,
            "transitions": 0,
            "illegal_refused": 0,
            "by_state": {state: 0 for state in _ALL_STATES},
        }

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._writer.close()

    def __enter__(self) -> "OrderIntentBook":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------- snapshot
    @staticmethod
    def _snapshot(record: dict, **extra: Any) -> dict:
        out = {
            "intent_id": record["intent_id"],
            "idempotency_key": record["idempotency_key"],
            "state": record["state"],
            "request": dict(record["request"]),
            "provenance": dict(record["provenance"]) if record["provenance"] is not None else None,
            "links": dict(record["links"]) if record["links"] is not None else None,
            "created_at_utc": record["created_at_utc"],
            "history": [dict(h) for h in record["history"]],
        }
        out.update(extra)
        return out

    # ------------------------------------------------------------- API
    def create_intent(
        self,
        request: dict,
        *,
        created_at_utc: str,
        provenance: "dict | None" = None,
        links: "dict | None" = None,
    ) -> dict:
        _require_nonempty_str(created_at_utc, "CREATED_AT_UTC")
        normalized_request = _validate_request(request)
        normalized_provenance = _validate_provenance(provenance)
        normalized_links = _validate_links(links)

        setup_id = normalized_links["setup_id"] if normalized_links else None
        cycle_id = normalized_provenance["cycle_id"] if normalized_provenance else None
        idempotency_key = build_idempotency_key(
            "ORDER_INTENT",
            normalized_request["symbol"],
            normalized_request["direction"],
            normalized_request["volume"],
            normalized_request["magic"],
            normalized_request["sl"],
            normalized_request["tp"],
            setup_id,
            cycle_id,
        )

        with self._lock:
            existing_id = self._active_by_key.get(idempotency_key)
            if existing_id is not None:
                self._diag["deduplicated"] += 1
                return self._snapshot(self._intents[existing_id], deduplicated=True)

            intent_id = validate_event_id(str(self._uuid.new()))
            history = [{"state": STATE_CREATED, "at_utc": created_at_utc}]
            record = {
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
                "state": STATE_CREATED,
                "request": dict(normalized_request),
                "provenance": dict(normalized_provenance) if normalized_provenance else None,
                "links": dict(normalized_links) if normalized_links else None,
                "created_at_utc": created_at_utc,
                "history": history,
            }
            payload = {
                "op": "intent_created",
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
                "state": STATE_CREATED,
                "request": dict(normalized_request),
                "provenance": record["provenance"],
                "links": record["links"],
                "created_at_utc": created_at_utc,
                "history": [dict(h) for h in history],
            }
            self._writer.append(payload)
            self._intents[intent_id] = record
            self._active_by_key[idempotency_key] = intent_id
            self._diag["created"] += 1
            self._diag["by_state"][STATE_CREATED] += 1
            return self._snapshot(record, deduplicated=False)

    def transition(
        self,
        intent_id: str,
        new_state: str,
        *,
        at_utc: str,
        detail: "str | None" = None,
    ) -> dict:
        _require_nonempty_str(intent_id, "INTENT_ID")
        _require_nonempty_str(at_utc, "AT_UTC")
        if new_state not in _ALL_STATES:
            raise IntentValidationError("STATE_UNKNOWN:%s" % new_state)
        sanitized_detail = _sanitize_detail(detail)

        with self._lock:
            record = self._intents.get(intent_id)
            if record is None:
                self._diag["illegal_refused"] += 1
                raise IntentNotFoundError("INTENT_UNKNOWN:%s" % intent_id)

            current_state = record["state"]
            if new_state == current_state:
                return self._snapshot(record, transition_result="ALREADY_%s" % current_state)

            allowed = _TRANSITIONS.get(current_state, frozenset())
            if new_state not in allowed:
                self._diag["illegal_refused"] += 1
                raise IntentTransitionError(
                    "ILLEGAL_TRANSITION:%s->%s" % (current_state, new_state))

            payload = {
                "op": "intent_transition",
                "intent_id": intent_id,
                "from_state": current_state,
                "to_state": new_state,
                "at_utc": at_utc,
                "detail": sanitized_detail,
            }
            self._writer.append(payload)

            record["state"] = new_state
            record["history"].append({"state": new_state, "at_utc": at_utc})
            self._diag["by_state"][current_state] -= 1
            self._diag["by_state"][new_state] += 1
            self._diag["transitions"] += 1
            if new_state in _TERMINAL_STATES:
                key = record["idempotency_key"]
                if self._active_by_key.get(key) == intent_id:
                    del self._active_by_key[key]

            return self._snapshot(record, transition_result="APPLIED")

    def get(self, intent_id: str) -> "dict | None":
        with self._lock:
            record = self._intents.get(intent_id)
            return None if record is None else self._snapshot(record)

    def active_intents(self) -> "list[dict]":
        with self._lock:
            return [
                self._snapshot(record)
                for record in self._intents.values()
                if record["state"] not in _TERMINAL_STATES
            ]

    def diagnostics(self) -> dict:
        with self._lock:
            return {
                "created": self._diag["created"],
                "deduplicated": self._diag["deduplicated"],
                "transitions": self._diag["transitions"],
                "illegal_refused": self._diag["illegal_refused"],
                "by_state": dict(self._diag["by_state"]),
            }

    # ------------------------------------------------------------- replay
    @classmethod
    def rebuild_from_journal(
        cls,
        journal_path,
        *,
        uuid_generator: MonotonicUUID7Generator,
        allowed_dir=None,
    ) -> "OrderIntentBook":
        """Replay a whole ORDER_INTENT journal into a fresh in-memory state,
        then reopen it for writing (the underlying ``JournalWriter`` resumes
        ``seq``/hash-chain continuity on its own — see ``event_journal``).

        Fail-closed: a low-level hash-chain/segment corruption raises
        ``JournalCorruptedError`` (propagated unchanged from
        ``JournalReader.replay()``); a structurally invalid ORDER_INTENT
        payload (unknown op, missing/extra fields, duplicate intent_id,
        transition inconsistent with the recorded state) raises
        ``IntentReplayError``. Neither case leaves a partially-rebuilt book:
        the exception is raised before the caller receives an instance.
        """
        payloads = JournalReader(journal_path).replay()
        book = cls(journal_path, uuid_generator=uuid_generator, allowed_dir=allowed_dir)
        try:
            for payload in payloads:
                book._apply_replayed_payload(payload)
        except Exception:
            book.close()
            raise
        return book

    def _apply_replayed_payload(self, payload: object) -> None:
        if not isinstance(payload, dict) or "op" not in payload:
            raise IntentReplayError("PAYLOAD_NOT_DICT_OR_NO_OP")
        op = payload.get("op")
        if op == "intent_created":
            self._replay_intent_created(payload)
        elif op == "intent_transition":
            self._replay_intent_transition(payload)
        else:
            raise IntentReplayError("UNKNOWN_OP:%s" % op)

    def _replay_intent_created(self, payload: dict) -> None:
        if set(payload.keys()) != _INTENT_CREATED_PAYLOAD_FIELDS:
            raise IntentReplayError("INTENT_CREATED_PAYLOAD_INVALID")
        intent_id = payload.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise IntentReplayError("INTENT_ID_INVALID")
        if intent_id in self._intents:
            raise IntentReplayError("DUPLICATE_INTENT_ID")
        state = payload.get("state")
        if state != STATE_CREATED:
            raise IntentReplayError("CREATED_STATE_INVALID")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise IntentReplayError("IDEMPOTENCY_KEY_INVALID")
        request = payload.get("request")
        if not isinstance(request, dict):
            raise IntentReplayError("REQUEST_INVALID")
        provenance = payload.get("provenance")
        if provenance is not None and not isinstance(provenance, dict):
            raise IntentReplayError("PROVENANCE_INVALID")
        links = payload.get("links")
        if links is not None and not isinstance(links, dict):
            raise IntentReplayError("LINKS_INVALID")
        history = payload.get("history")
        if not isinstance(history, list) or not history:
            raise IntentReplayError("HISTORY_INVALID")

        record = {
            "intent_id": intent_id,
            "idempotency_key": idempotency_key,
            "state": state,
            "request": dict(request),
            "provenance": dict(provenance) if provenance is not None else None,
            "links": dict(links) if links is not None else None,
            "created_at_utc": payload.get("created_at_utc"),
            "history": [dict(h) for h in history],
        }
        self._intents[intent_id] = record
        self._active_by_key[idempotency_key] = intent_id
        self._diag["created"] += 1
        self._diag["by_state"][STATE_CREATED] += 1

    def _replay_intent_transition(self, payload: dict) -> None:
        if set(payload.keys()) != _INTENT_TRANSITION_PAYLOAD_FIELDS:
            raise IntentReplayError("INTENT_TRANSITION_PAYLOAD_INVALID")
        intent_id = payload.get("intent_id")
        record = self._intents.get(intent_id) if isinstance(intent_id, str) else None
        if record is None:
            raise IntentReplayError("UNKNOWN_INTENT_IN_TRANSITION")
        from_state = payload.get("from_state")
        to_state = payload.get("to_state")
        if from_state != record["state"]:
            raise IntentReplayError("FROM_STATE_MISMATCH")
        if to_state not in _TRANSITIONS.get(from_state, frozenset()):
            raise IntentReplayError("ILLEGAL_TRANSITION_IN_JOURNAL")
        at_utc = payload.get("at_utc")
        if not isinstance(at_utc, str) or not at_utc:
            raise IntentReplayError("AT_UTC_INVALID")

        record["state"] = to_state
        record["history"].append({"state": to_state, "at_utc": at_utc})
        self._diag["by_state"][from_state] -= 1
        self._diag["by_state"][to_state] += 1
        self._diag["transitions"] += 1
        if to_state in _TERMINAL_STATES:
            key = record["idempotency_key"]
            if self._active_by_key.get(key) == intent_id:
                del self._active_by_key[key]
