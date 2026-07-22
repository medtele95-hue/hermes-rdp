"""LIFECYCLE_IDENTITY_STORE — mapping durable lifecycle <-> position broker (M02-P1C).

Store LABORATOIRE, cable nulle part en production (aucun import par main.py /
demo_router.py). Il relie durablement :

    lifecycle_id canonique (UUIDv7)
    <-> reference broker composite stricte (JAMAIS le ticket seul)
    <-> correlation_id
    <-> provenance de creation (boot_id, cycle_id, setup_id)

CONTRAT lifecycle_id :
- UUIDv7 valide, genere UNE seule fois par vie de position (generateur
  monotone injecte) ; stable a travers les restarts (relu du disque, jamais
  re-minte) ; jamais recycle ; JAMAIS derive du ticket.

CONTRAT reference broker (BrokerPositionRef) :
- composite stricte : account_scope_id (pseudonyme HMAC, jamais un login) +
  ticket int > 0 + position_identifier int > 0 optionnel + broker_symbol
  canonique + opened_at (ISO-8601 UTC-aware OU epoch ms > 0) + magic int >= 0
  + direction optionnelle {BUY, SELL} ;
- le ticket SEUL est insuffisant par construction (aucune API de resolution
  par ticket) ; deux trades recyclant le meme ticket different par
  position_identifier et/ou opened_at ;
- discriminant manquant/invalide -> UNRESOLVED (helper non-levant
  ``try_build_broker_ref``) ; aucune conversion silencieuse de type (bool
  rejete comme int, chaines numeriques rejetees comme int).

DURABILITE (fail-closed, pre-M03) :
- fichier JSON versionne strict (schema_name/schema_version/generation/
  content_digest) ; champ inconnu, champ manquant, type invalide, digest ou
  generation incoherents -> StoreCorruptedError, l'ancien fichier est
  PRESERVE, JAMAIS de recreation silencieuse d'un store vide (seule
  l'ABSENCE du fichier cree un store neuf) ;
- ecriture atomique tmp -> flush -> fsync -> os.replace (modele T1-P0B) ;
  un echec d'ecriture preserve l'ancien fichier et NE commite PAS l'etat
  memoire (aucune ecriture partielle) ;
- compteur ``generation`` : avant chaque ecriture, la generation sur disque
  est relue ; si une autre instance a ecrit entre-temps ->
  ConcurrentWriterError("CONCURRENT_WRITER_DETECTED"), jamais d'ecrasement ;
- capacite bornee : un OPEN n'est JAMAIS supprime automatiquement ; les
  CLOSED ne sont purges que par appel EXPLICITE ``purge_closed`` (politique
  documentee, pas de purge automatique avant M03) ; capacite atteinte avec
  uniquement des OPEN -> StoreCapacityError("STORE_CAPACITY_REACHED").

PURETE D'ENVIRONNEMENT : aucune lecture .env, aucun reseau, aucun MT5 ;
chemin et generateur INJECTES ; horloges jamais lues (created_at/closed_at
injectes par l'appelant) -> une horloge murale qui recule est sans effet.
Compatible journal append-only M03 : chaque record est auto-suffisant
(identites + provenance + timestamps dans le record), la serialisation est
deterministe (sort_keys) et ``generation`` fournit l'ordre total des etats.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator

STORE_SCHEMA_NAME = "hermes.lifecycle_identity_store"
STORE_SCHEMA_VERSION = 1
STORE_MAX_BYTES_DEFAULT = 64 * 1024 * 1024
STORE_MAX_LIFECYCLES_DEFAULT = 10000
BROKER_REF_KEY_VERSION = "brkref-v1"

_ACCOUNT_SCOPE_RE = re.compile(r"\Aacct-v1-[0-9a-f]{32,}\Z")
_STATE_OPEN = "OPEN"
_STATE_CLOSED = "CLOSED"
_ALLOWED_STATES = frozenset({_STATE_OPEN, _STATE_CLOSED})
_ALLOWED_DIRECTIONS = frozenset({"BUY", "SELL"})
_TOP_LEVEL_FIELDS = frozenset({
    "schema_name", "schema_version", "generation", "content_digest",
    "lifecycles", "broker_index",
})
_RECORD_FIELDS = frozenset({
    "lifecycle_id", "correlation_id", "boot_id", "cycle_id", "setup_id",
    "state", "created_at_utc", "closed_at_utc", "broker_ref", "broker_key",
})
_REF_FIELDS = frozenset({
    "account_scope_id", "ticket", "position_identifier", "broker_symbol",
    "opened_at", "magic", "direction",
})


# --------------------------------------------------------------------------- #
# Exceptions (sanitisees : jamais de login, secret ou contenu de fichier)
# --------------------------------------------------------------------------- #
class LifecycleStoreError(Exception):
    """Base. ``reason_code`` = diagnostic machine sanitise."""

    reason_code = "STORE_ERROR"


class LifecycleValidationError(LifecycleStoreError):
    reason_code = "VALIDATION_FAILED"

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class StoreCorruptedError(LifecycleStoreError):
    reason_code = "STORE_CORRUPTED"

    def __init__(self, reason_code: str = "STORE_CORRUPTED"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class StoreWriteError(LifecycleStoreError):
    reason_code = "STORE_WRITE_FAILED"


class ConcurrentWriterError(LifecycleStoreError):
    reason_code = "CONCURRENT_WRITER_DETECTED"


class ConflictError(LifecycleStoreError):
    reason_code = "CONFLICT"

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class StoreCapacityError(LifecycleStoreError):
    reason_code = "STORE_CAPACITY_REACHED"


class UnknownLifecycleError(LifecycleStoreError):
    reason_code = "LIFECYCLE_UNKNOWN"


# --------------------------------------------------------------------------- #
# Validation stricte des types (aucune conversion silencieuse)
# --------------------------------------------------------------------------- #
def _is_uuid7(value: object) -> bool:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 7 and ((parsed.int >> 62) & 0b11) == 0b10


def _strict_int(value: object) -> bool:
    return type(value) is int  # bool est exclu (type(True) is bool)


def _parse_utc(value: object) -> "datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _validate_uuid(value: object) -> bool:
    try:
        uuid.UUID(str(value))
        return isinstance(value, str)
    except (ValueError, AttributeError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Reference broker composite stricte
# --------------------------------------------------------------------------- #
def try_build_broker_ref(
    *,
    account_scope_id: object,
    ticket: object,
    broker_symbol: object,
    opened_at: object,
    magic: object,
    position_identifier: object = None,
    direction: object = None,
) -> "tuple[dict | None, str | None]":
    """Construction NON-levante : (ref, None) ou (None, reason_code).

    Regles : types stricts, aucun discriminant devine, ticket seul jamais
    suffisant (les autres champs requis sont obligatoires)."""
    if not isinstance(account_scope_id, str) or not _ACCOUNT_SCOPE_RE.match(account_scope_id):
        return None, "ACCOUNT_SCOPE_INVALID"
    if not _strict_int(ticket) or ticket <= 0:
        return None, "TICKET_INVALID"
    if position_identifier is not None and (
        not _strict_int(position_identifier) or position_identifier <= 0
    ):
        return None, "POSITION_IDENTIFIER_INVALID"
    if not isinstance(broker_symbol, str) or not broker_symbol.strip():
        return None, "SYMBOL_MISSING"
    symbol = broker_symbol.strip().upper()
    if isinstance(opened_at, str):
        parsed = _parse_utc(opened_at)
        if parsed is None:
            return None, "OPENED_AT_INVALID"
        opened_canonical = "iso:%s" % parsed.isoformat()
    elif _strict_int(opened_at) and opened_at > 0:
        opened_canonical = "msc:%d" % opened_at
    else:
        return None, "OPENED_AT_INVALID"
    if not _strict_int(magic) or magic < 0:
        return None, "MAGIC_INVALID"
    if direction is not None and direction not in _ALLOWED_DIRECTIONS:
        return None, "DIRECTION_INVALID"
    ref = {
        "account_scope_id": account_scope_id,
        "ticket": ticket,
        "position_identifier": position_identifier,
        "broker_symbol": symbol,
        "opened_at": opened_canonical,
        "magic": magic,
        "direction": direction,
    }
    return ref, None


def broker_ref_key(ref: dict) -> str:
    """Cle durable deterministe. Le ticket n'est qu'UN composant parmi les
    discriminants — jamais une cle a lui seul."""
    return "|".join((
        BROKER_REF_KEY_VERSION,
        "acct=%s" % ref["account_scope_id"],
        "t=%d" % ref["ticket"],
        "pid=%s" % ("-" if ref["position_identifier"] is None else ref["position_identifier"]),
        "sym=%s" % ref["broker_symbol"],
        "opened=%s" % ref["opened_at"],
        "magic=%d" % ref["magic"],
        "dir=%s" % (ref["direction"] or "-"),
    ))


def _validate_ref_on_disk(ref: object) -> bool:
    if not isinstance(ref, dict) or set(ref.keys()) != _REF_FIELDS:
        return False
    if not isinstance(ref["account_scope_id"], str) or not _ACCOUNT_SCOPE_RE.match(ref["account_scope_id"]):
        return False
    if not _strict_int(ref["ticket"]) or ref["ticket"] <= 0:
        return False
    if ref["position_identifier"] is not None and (
        not _strict_int(ref["position_identifier"]) or ref["position_identifier"] <= 0
    ):
        return False
    if not isinstance(ref["broker_symbol"], str) or not ref["broker_symbol"]:
        return False
    if not isinstance(ref["opened_at"], str) or not (
        ref["opened_at"].startswith("iso:") or ref["opened_at"].startswith("msc:")
    ):
        return False
    if not _strict_int(ref["magic"]) or ref["magic"] < 0:
        return False
    if ref["direction"] is not None and ref["direction"] not in _ALLOWED_DIRECTIONS:
        return False
    return True


# --------------------------------------------------------------------------- #
# Store durable
# --------------------------------------------------------------------------- #
def _content_digest(generation: int, lifecycles: dict, broker_index: dict) -> str:
    payload = json.dumps(
        {"generation": generation, "lifecycles": lifecycles, "broker_index": broker_index},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LifecycleIdentityStore:
    """Store durable thread-safe. Voir contrat du module."""

    def __init__(
        self,
        *,
        path: "Path | str",
        allowed_dir: "Path | str | None" = None,
        max_bytes: int = STORE_MAX_BYTES_DEFAULT,
        max_lifecycles: int = STORE_MAX_LIFECYCLES_DEFAULT,
        uuid_generator: "MonotonicUUID7Generator | None" = None,
    ) -> None:
        self._path = Path(path)
        self._allowed_dir = Path(allowed_dir) if allowed_dir is not None else self._path.parent
        self._max_bytes = int(max_bytes)
        self._max = int(max_lifecycles)
        self._uuid = uuid_generator or MonotonicUUID7Generator()
        self._lock = threading.RLock()
        self._generation = 0
        self._lifecycles: dict = {}
        self._broker_index: dict = {}
        self._load_initial()

    # ------------------------------------------------------------- lecture
    def _check_path(self) -> None:
        if self._path.is_symlink():
            raise StoreCorruptedError("STORE_PATH_SYMLINK")
        # Garde appliquee que le fichier existe deja ou non : une premiere
        # ecriture ne doit jamais sortir du repertoire autorise.
        if self._path.parent.resolve() != self._allowed_dir.resolve():
            raise StoreCorruptedError("STORE_PATH_OUTSIDE_ALLOWED_DIR")

    def _read_disk_state(self) -> "dict | None":
        """Lit et valide strictement le fichier. None si absent (seul cas ou
        un store neuf est legitime). Toute autre anomalie -> StoreCorruptedError
        (l'ancien fichier reste sur disque, JAMAIS remplace par un vide)."""
        self._check_path()
        if not self._path.exists():
            return None
        if not self._path.is_file():
            raise StoreCorruptedError("STORE_PATH_NOT_A_FILE")
        if self._path.stat().st_size > self._max_bytes:
            raise StoreCorruptedError("STORE_TOO_LARGE")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StoreCorruptedError("STORE_READ_FAILED") from exc
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise StoreCorruptedError("STORE_INVALID_JSON") from exc
        if not isinstance(obj, dict) or set(obj.keys()) != _TOP_LEVEL_FIELDS:
            raise StoreCorruptedError("STORE_SCHEMA_INVALID")
        if obj["schema_name"] != STORE_SCHEMA_NAME or obj["schema_version"] != STORE_SCHEMA_VERSION:
            raise StoreCorruptedError("STORE_SCHEMA_INVALID")
        if not _strict_int(obj["generation"]) or obj["generation"] < 1:
            raise StoreCorruptedError("STORE_GENERATION_INVALID")
        lifecycles = obj["lifecycles"]
        broker_index = obj["broker_index"]
        if not isinstance(lifecycles, dict) or not isinstance(broker_index, dict):
            raise StoreCorruptedError("STORE_SCHEMA_INVALID")
        digest = _content_digest(obj["generation"], lifecycles, broker_index)
        if obj["content_digest"] != digest:
            raise StoreCorruptedError("STORE_DIGEST_MISMATCH")
        for lid, rec in lifecycles.items():
            if not isinstance(rec, dict) or set(rec.keys()) != _RECORD_FIELDS:
                raise StoreCorruptedError("RECORD_SCHEMA_INVALID")
            if rec["lifecycle_id"] != lid or not _is_uuid7(lid):
                raise StoreCorruptedError("RECORD_LIFECYCLE_ID_INVALID")
            if rec["correlation_id"] is not None and not _is_uuid7(rec["correlation_id"]):
                raise StoreCorruptedError("RECORD_CORRELATION_INVALID")
            if rec["boot_id"] is not None and not _validate_uuid(rec["boot_id"]):
                raise StoreCorruptedError("RECORD_BOOT_ID_INVALID")
            if rec["cycle_id"] is not None and (not _strict_int(rec["cycle_id"]) or rec["cycle_id"] < 0):
                raise StoreCorruptedError("RECORD_CYCLE_ID_INVALID")
            if rec["setup_id"] is not None and (
                not isinstance(rec["setup_id"], str) or not rec["setup_id"].strip()
            ):
                raise StoreCorruptedError("RECORD_SETUP_ID_INVALID")
            if rec["state"] not in _ALLOWED_STATES:
                raise StoreCorruptedError("RECORD_STATE_INVALID")
            if _parse_utc(rec["created_at_utc"]) is None:
                raise StoreCorruptedError("RECORD_CREATED_AT_INVALID")
            if rec["closed_at_utc"] is not None and _parse_utc(rec["closed_at_utc"]) is None:
                raise StoreCorruptedError("RECORD_CLOSED_AT_INVALID")
            if (rec["broker_ref"] is None) != (rec["broker_key"] is None):
                raise StoreCorruptedError("RECORD_BINDING_INCONSISTENT")
            if rec["broker_ref"] is not None:
                if not _validate_ref_on_disk(rec["broker_ref"]):
                    raise StoreCorruptedError("RECORD_BROKER_REF_INVALID")
                if broker_ref_key(rec["broker_ref"]) != rec["broker_key"]:
                    raise StoreCorruptedError("RECORD_BROKER_KEY_INVALID")
                if broker_index.get(rec["broker_key"]) != lid:
                    raise StoreCorruptedError("STORE_INDEX_INCONSISTENT")
        for key, lid in broker_index.items():
            rec = lifecycles.get(lid)
            if rec is None or rec.get("broker_key") != key:
                raise StoreCorruptedError("STORE_INDEX_INCONSISTENT")
        return obj

    def _load_initial(self) -> None:
        with self._lock:
            state = self._read_disk_state()
            if state is None:
                self._generation = 0
                self._lifecycles = {}
                self._broker_index = {}
            else:
                self._generation = state["generation"]
                self._lifecycles = state["lifecycles"]
                self._broker_index = state["broker_index"]

    def reload(self) -> None:
        """Relit le disque (fail-closed). Un fichier corrompu leve et laisse
        l'etat memoire precedent intact."""
        with self._lock:
            state = self._read_disk_state()
            if state is None:
                raise StoreCorruptedError("STORE_FILE_MISSING_ON_RELOAD")
            self._generation = state["generation"]
            self._lifecycles = state["lifecycles"]
            self._broker_index = state["broker_index"]

    # ------------------------------------------------------------- ecriture
    def _persist(self, lifecycles: dict, broker_index: dict) -> None:
        """Ecriture atomique + garde generation. Appelant sous verrou.
        En cas d'echec : ancien fichier preserve, etat memoire NON commite."""
        disk = None
        if self._path.exists():
            disk = self._read_disk_state()  # corrompu -> leve, on n'ecrase pas
        disk_generation = 0 if disk is None else disk["generation"]
        if disk_generation != self._generation:
            raise ConcurrentWriterError("CONCURRENT_WRITER_DETECTED")
        new_generation = self._generation + 1
        payload = {
            "schema_name": STORE_SCHEMA_NAME,
            "schema_version": STORE_SCHEMA_VERSION,
            "generation": new_generation,
            "content_digest": _content_digest(new_generation, lifecycles, broker_index),
            "lifecycles": lifecycles,
            "broker_index": broker_index,
        }
        text = json.dumps(payload, sort_keys=True, indent=1)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise StoreWriteError("STORE_WRITE_FAILED") from exc
        self._generation = new_generation
        self._lifecycles = lifecycles
        self._broker_index = broker_index

    # ------------------------------------------------------------- API
    def create_lifecycle(
        self,
        *,
        correlation_id: "str | None",
        created_at_utc: str,
        boot_id: "str | None" = None,
        cycle_id: "int | None" = None,
        setup_id: "str | None" = None,
    ) -> dict:
        """Cree un lifecycle (UUIDv7 neuf, jamais derive du ticket) et le
        persiste immediatement. Capacite atteinte -> StoreCapacityError,
        jamais d'ecrasement d'un OPEN."""
        if correlation_id is not None and not _is_uuid7(correlation_id):
            raise LifecycleValidationError("CORRELATION_ID_INVALID")
        if boot_id is not None and not _validate_uuid(boot_id):
            raise LifecycleValidationError("BOOT_ID_INVALID")
        if cycle_id is not None and (not _strict_int(cycle_id) or cycle_id < 0):
            raise LifecycleValidationError("CYCLE_ID_INVALID")
        if setup_id is not None and (not isinstance(setup_id, str) or not setup_id.strip()):
            raise LifecycleValidationError("SETUP_ID_INVALID")
        if _parse_utc(created_at_utc) is None:
            raise LifecycleValidationError("CREATED_AT_INVALID")
        with self._lock:
            if len(self._lifecycles) >= self._max:
                raise StoreCapacityError("STORE_CAPACITY_REACHED")
            lifecycle_id = str(self._uuid.new())
            record = {
                "lifecycle_id": lifecycle_id,
                "correlation_id": correlation_id,
                "boot_id": boot_id,
                "cycle_id": cycle_id,
                "setup_id": setup_id,
                "state": _STATE_OPEN,
                "created_at_utc": created_at_utc,
                "closed_at_utc": None,
                "broker_ref": None,
                "broker_key": None,
            }
            lifecycles = dict(self._lifecycles)
            lifecycles[lifecycle_id] = record
            self._persist(lifecycles, dict(self._broker_index))
            return dict(record)

    def bind_broker_position(self, lifecycle_id: str, ref: dict) -> str:
        """Lie une reference broker composite a un lifecycle.

        Idempotent : meme (lifecycle, reference) -> "ALREADY_BOUND" sans
        ecriture. Conflits refuses sans overwrite :
        - reference deja liee a un AUTRE lifecycle -> REFERENCE_ALREADY_BOUND ;
        - lifecycle deja lie a une AUTRE reference -> LIFECYCLE_ALREADY_BOUND.
        """
        if not _validate_ref_on_disk(ref):
            raise LifecycleValidationError("BROKER_REF_INVALID")
        key = broker_ref_key(ref)
        with self._lock:
            record = self._lifecycles.get(lifecycle_id)
            if record is None:
                raise UnknownLifecycleError("LIFECYCLE_UNKNOWN")
            existing_owner = self._broker_index.get(key)
            if record["broker_key"] == key and existing_owner == lifecycle_id:
                return "ALREADY_BOUND"  # no-op valide, aucun etat modifie
            if existing_owner is not None and existing_owner != lifecycle_id:
                raise ConflictError("REFERENCE_ALREADY_BOUND")
            if record["broker_key"] is not None and record["broker_key"] != key:
                raise ConflictError("LIFECYCLE_ALREADY_BOUND")
            lifecycles = dict(self._lifecycles)
            new_record = dict(record)
            new_record["broker_ref"] = dict(ref)
            new_record["broker_key"] = key
            lifecycles[lifecycle_id] = new_record
            broker_index = dict(self._broker_index)
            broker_index[key] = lifecycle_id
            self._persist(lifecycles, broker_index)
            return "BOUND"

    def resolve_by_broker_position(self, ref: dict) -> "tuple[dict | None, str | None]":
        """Resolution STRICTE par reference composite complete. Retourne
        (record, None) ou (None, reason). Jamais par ticket seul, jamais
        approximatif."""
        if not _validate_ref_on_disk(ref):
            return None, "BROKER_REF_INVALID"
        key = broker_ref_key(ref)
        with self._lock:
            lid = self._broker_index.get(key)
            if lid is None:
                return None, "BROKER_REF_UNKNOWN"
            return dict(self._lifecycles[lid]), None

    def get_lifecycle(self, lifecycle_id: str) -> "dict | None":
        with self._lock:
            record = self._lifecycles.get(lifecycle_id)
            return dict(record) if record is not None else None

    def mark_closed(self, lifecycle_id: str, *, closed_at_utc: str) -> str:
        """OPEN -> CLOSED (one-way). Idempotent : deja CLOSED -> no-op valide
        "ALREADY_CLOSED", le closed_at d'origine n'est JAMAIS ecrase."""
        if _parse_utc(closed_at_utc) is None:
            raise LifecycleValidationError("CLOSED_AT_INVALID")
        with self._lock:
            record = self._lifecycles.get(lifecycle_id)
            if record is None:
                raise UnknownLifecycleError("LIFECYCLE_UNKNOWN")
            if record["state"] == _STATE_CLOSED:
                return "ALREADY_CLOSED"
            lifecycles = dict(self._lifecycles)
            new_record = dict(record)
            new_record["state"] = _STATE_CLOSED
            new_record["closed_at_utc"] = closed_at_utc
            lifecycles[lifecycle_id] = new_record
            self._persist(lifecycles, dict(self._broker_index))
            return "CLOSED"

    def purge_closed(self, *, max_to_purge: int) -> int:
        """Politique de retention EXPLICITE (jamais automatique avant M03) :
        supprime au plus ``max_to_purge`` lifecycles CLOSED (les plus anciens
        par closed_at). Un OPEN n'est JAMAIS purge."""
        if not _strict_int(max_to_purge) or max_to_purge <= 0:
            raise LifecycleValidationError("PURGE_LIMIT_INVALID")
        with self._lock:
            closed = sorted(
                (r for r in self._lifecycles.values() if r["state"] == _STATE_CLOSED),
                key=lambda r: r["closed_at_utc"] or "",
            )
            victims = closed[:max_to_purge]
            if not victims:
                return 0
            lifecycles = dict(self._lifecycles)
            broker_index = dict(self._broker_index)
            for record in victims:
                del lifecycles[record["lifecycle_id"]]
                if record["broker_key"] is not None:
                    broker_index.pop(record["broker_key"], None)
            self._persist(lifecycles, broker_index)
            return len(victims)

    def snapshot_metadata(self) -> dict:
        with self._lock:
            open_count = sum(1 for r in self._lifecycles.values() if r["state"] == _STATE_OPEN)
            return {
                "schema_name": STORE_SCHEMA_NAME,
                "schema_version": STORE_SCHEMA_VERSION,
                "generation": self._generation,
                "lifecycles_total": len(self._lifecycles),
                "lifecycles_open": open_count,
                "lifecycles_closed": len(self._lifecycles) - open_count,
                "max_lifecycles": self._max,
                "path": str(self._path),
                "file_bytes": self._path.stat().st_size if self._path.exists() else 0,
            }
