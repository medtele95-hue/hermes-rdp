"""EVENT_JOURNAL — append-only DORMANT journal (M03-P1, laboratoire).

Reponse structurelle au cout O(n) de ``LifecycleIdentityStore`` (chaque
mutation y reecrit l'INTEGRALITE du fichier JSON, ~22-70 ms mesures en
P1C) et a sa dette ``durable_across_restart=false`` : un journal
append-only n'ecrit jamais que la NOUVELLE ligne, jamais tout le fichier.

MISSION 100% LABORATOIRE : ce module n'est cable nulle part en
production (aucun import par main.py / demo_router.py / lifecycle_capture.py).
Il ne modifie ni ne remplace ``LifecycleIdentityStore`` — ``rebuild_lifecycle_state``
en est un PONT en lecture seule qui rejoue un journal fictif pour produire un
etat equivalent, sans jamais toucher le store reel.

FORMAT DE FICHIER (segments JSONL) :
- un journal est une SUITE de segments ``path``, ``path.1``, ``path.2``, ... ;
  seul le DERNIER segment recoit des ecritures, les precedents sont clos et
  ne sont JAMAIS modifies (rotation quand ``max_segment_bytes`` est depasse) ;
- premiere ligne de chaque segment = un HEADER JSON
  ``{schema, schema_version, segment_index, prev_segment_tail_sha, writer_token}``
  (``prev_segment_tail_sha`` = sha256 hex complet de la derniere ligne du
  segment precedent, ``"GENESIS"`` pour le segment 0 — c'est le chainage
  INTER-segments) ;
- chaque ligne suivante est une ENVELOPPE JSON
  ``{seq, prev_line_sha256_16, payload}`` ou ``seq`` est un compteur GLOBAL
  monotone (1..N sur tout le journal, jamais reutilise, jamais reinitialise
  a une rotation) et ``prev_line_sha256_16`` est les 16 premiers hex du
  sha256 de la ligne brute PRECEDENTE ecrite dans le meme segment (le header
  compte comme "ligne precedente" pour la toute premiere enveloppe) — c'est
  le chainage INTRA-segment ;
- serialisation deterministe (``sort_keys``, separateurs compacts) ; une
  ligne est ecrite en UNE fois (``write`` + ``flush`` (+``fsync`` selon
  politique)) ; ``append()`` n'ouvre JAMAIS le fichier autrement qu'en mode
  ``"a"`` strict et ne fait JAMAIS de ``seek`` arriere — la SEULE exception
  au niveau du module entier est la RECUPERATION DE DEMARRAGE (voir
  ``JournalWriter.__init__``) qui peut tronquer une queue de ligne
  INCOMPLETE (jamais acquittee : ni flush ni fsync termines) laissee par un
  crash — jamais une ligne valide et complete n'est modifiee ou supprimee.

GARDE SINGLE-WRITER : un fichier sidecar ``<path>.lock`` est cree en
``O_EXCL`` et porte le ``writer_token`` (uuid4 hex par defaut, injectable).
Aucune verification de PID (labo) : un lock present, quel que soit son
``writer_token``, fait echouer l'ouverture avec ``OwnershipError`` — fail
closed, jamais d'ecrasement silencieux d'un writer potentiellement vivant.
Le lock n'est libere que par ``close()`` du writer qui l'a cree (token
identique) ; un lock orphelin reste orphelin tant qu'aucun humain ne le
supprime — ce module ne le fait jamais pour un autre proprietaire.

REPRISE : a l'ouverture d'un journal existant, le dernier segment est
rejoue integralement pour restaurer ``seq`` et la chaine de hash — jamais de
re-sequencage a 1. Une queue de derniere ligne incomplete (crash pendant un
``append``) est traitee EXACTEMENT comme le lecteur la traite
(``TORN_TAIL_QUARANTINED``) : elle est ecartee (jamais reinterpretee comme
valide), le fichier est tronque a la fin de la derniere ligne VALIDE, et
l'ecriture reprend proprement apres. Toute autre anomalie (ligne corrompue
NON en position de queue, sequence non monotone, chaine de hash rompue,
en-tete invalide) fait echouer l'ouverture avec ``JournalCorruptedError`` —
fail closed, l'ancien contenu n'est jamais touche.

ETAT POISONED (empoisonnement terminal, irreversible) : toute exception d'E/S
levee pendant une ecriture reelle (``write``/``flush``/``fsync`` dans
``append()``, dans ``_create_new_segment()`` -- creation du tout premier
segment OU d'un segment de rotation -- ou dans ``_rotate()``) marque
l'instance ``_poisoned`` (avec une raison sanitisee, jamais de contenu
sensible, exposee via ``resume_report()``/les proprietes ``poisoned`` et
``poison_reason``). Cet etat n'est JAMAIS reinitialise sur l'instance : la
garde est verifiee EN TETE de ``append()`` (et de toute autre operation
d'ecriture future), AVANT toute tentative d'E/S, et leve immediatement
``JournalPoisonedError`` -- symetrique a la garde ``_closed``. La SEULE issue
est ``close()`` (qui reste toujours possible, meme empoisonne, pour liberer
le lock) suivi de la reouverture d'une NOUVELLE instance : le chemin de
reprise decrit ci-dessus (rejeu + troncature de queue non acquittee) fait
alors autorite et a deja fait ses preuves sans perte de donnee -- aucune
tentative d'auto-reparation n'est jamais faite sur l'instance empoisonnee
elle-meme.

Semantique exacte E/S <-> acquittement : sur un echec ``flush()`` ou
``fsync()`` (par opposition a ``write()``, qui reste toujours 100% propre :
rien n'atteint jamais le disque quand seul ``write()`` est en cause), les
octets de la ligne fautee PEUVENT deja etre durables sur disque (cas
``fsync`` : ``write()``+``flush()`` ont deja reellement complete avant que
seul ``fsync()`` leve) ou le devenir plus tard via un ``close()`` normal qui
vide le tampon Python restant (cas ``flush``) -- alors meme que
``append()`` n'a JAMAIS retourne de succes pour cette ligne (l'instance est
empoisonnee immediatement, avant tout retour). Au rebuild (fermeture puis
reouverture), si cette derniere ligne s'avere structurellement complete et
valide (JSON bien forme, ``seq`` et chaine de hash corrects), elle est
ACCEPTEE par le scan de reprise exactement comme une ligne normale --
c'est le comportement de reprise deja prouve ci-dessus, inchange par
l'empoisonnement. Aucune double-ecriture du meme ``seq`` n'est jamais
possible : l'empoisonnement interdit toute autre ecriture depuis la MEME
instance apres la panne, donc aucune ligne suivante ne peut jamais entrer en
collision avec celle-ci. AUCUN changement de format sur disque.

PURETE D'ENVIRONNEMENT : stdlib uniquement (``json``, ``hashlib``, ``os``,
``threading``, ``uuid``, ``pathlib``) ; aucune lecture de fichier ``.env`` ni
des variables d'environnement du process, aucun reseau, aucun MT5 ; chemin et
``allowed_dir`` INJECTES ; AUCUNE horloge n'est lue par defaut (``uuid4``
s'appuie sur ``os.urandom``, jamais sur l'heure) — tout timestamp appartient
au ``payload`` de l'appelant.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

JOURNAL_SCHEMA_NAME = "hermes.event_journal"
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_MAX_SEGMENT_BYTES_DEFAULT = 8 * 1024 * 1024

_HEADER_FIELDS = frozenset({
    "schema", "schema_version", "segment_index", "prev_segment_tail_sha",
    "writer_token",
})
_ENVELOPE_FIELDS = frozenset({"seq", "prev_line_sha256_16", "payload"})
_HEX16 = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# Exceptions (jamais de contenu sensible : uniquement des codes sanitises)
# --------------------------------------------------------------------------- #
class JournalError(Exception):
    """Base. ``reason_code`` = diagnostic machine sanitise."""

    reason_code = "JOURNAL_ERROR"


class ValidationError(JournalError):
    reason_code = "VALIDATION_FAILED"

    def __init__(self, reason_code: str = "VALIDATION_FAILED"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class OwnershipError(JournalError):
    reason_code = "OWNERSHIP_CONFLICT"

    def __init__(self, reason_code: str = "OWNERSHIP_CONFLICT"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class JournalCorruptedError(JournalError):
    reason_code = "JOURNAL_CORRUPTED"

    def __init__(self, reason_code: str = "JOURNAL_CORRUPTED"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class JournalPoisonedError(JournalError):
    """Leve par toute operation d'ecriture (``append()`` en tete, avant tout
    I/O) sur une instance ``JournalWriter`` deja empoisonnee par une panne
    d'E/S anterieure. Terminal : la seule issue est ``close()`` puis une
    NOUVELLE instance. Voir la section "ETAT POISONED" du docstring du
    module."""

    reason_code = "JOURNAL_POISONED"

    def __init__(self, reason_code: str = "JOURNAL_POISONED"):
        super().__init__(reason_code or "JOURNAL_POISONED")
        self.reason_code = reason_code or "JOURNAL_POISONED"


# --------------------------------------------------------------------------- #
# Segments : nommage et scan bas niveau (partage lecteur/ecrivain)
# --------------------------------------------------------------------------- #
def _segment_path(base: Path, index: int) -> Path:
    if index == 0:
        return base
    return Path(str(base) + "." + str(index))


def _list_segments(base: Path) -> "list[Path]":
    if not base.exists():
        return []
    segs = [base]
    i = 1
    while True:
        p = Path(str(base) + "." + str(i))
        if not p.exists():
            break
        segs.append(p)
        i += 1
    return segs


def _valid_header_struct(header: object) -> bool:
    if not isinstance(header, dict) or set(header.keys()) != _HEADER_FIELDS:
        return False
    if header["schema"] != JOURNAL_SCHEMA_NAME:
        return False
    if header["schema_version"] != JOURNAL_SCHEMA_VERSION:
        return False
    if type(header["segment_index"]) is not int or header["segment_index"] < 0:
        return False
    if not isinstance(header["prev_segment_tail_sha"], str) or not header["prev_segment_tail_sha"]:
        return False
    if not isinstance(header["writer_token"], str) or not header["writer_token"]:
        return False
    return True


def _valid_envelope_struct(env: object) -> bool:
    if not isinstance(env, dict) or set(env.keys()) != _ENVELOPE_FIELDS:
        return False
    if type(env["seq"]) is not int or env["seq"] <= 0:
        return False
    h = env["prev_line_sha256_16"]
    if not isinstance(h, str) or len(h) != 16 or any(c not in _HEX16 for c in h):
        return False
    if not isinstance(env["payload"], dict):
        return False
    return True


def _scan_journal(segments: "list[Path]") -> dict:
    """Scan bas niveau PARTAGE par ``JournalReader.verify/replay`` et par la
    reprise de ``JournalWriter``. Ne leve jamais : toute anomalie est
    reportee dans le resultat (fail-closed cote appelant)."""
    errors: "list[str]" = []
    intact = True
    total_records = 0
    tail_status = "CLEAN"
    prev_tail_full = "GENESIS"
    last_line_hash_full = "GENESIS"
    global_seq = 0
    payloads: "list[tuple[int, dict]]" = []
    valid_end_offset_last_segment = 0
    n = len(segments)
    stop = False
    for i, seg_path in enumerate(segments):
        if stop:
            break
        is_last_segment = i == n - 1
        raw = seg_path.read_bytes()
        ends_with_nl = raw.endswith(b"\n")
        raw_lines = raw.split(b"\n")
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        if not raw_lines:
            errors.append("SEGMENT_EMPTY:%d" % i)
            intact = False
            break
        header_raw = raw_lines[0]
        valid_end_offset = len(header_raw) + 1
        try:
            header = json.loads(header_raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            errors.append("HEADER_INVALID_JSON:%d" % i)
            intact = False
            break
        if not _valid_header_struct(header) or header["segment_index"] != i \
                or header["prev_segment_tail_sha"] != prev_tail_full:
            errors.append("HEADER_INVALID:%d" % i)
            intact = False
            break
        last_line_hash_full = hashlib.sha256(header_raw).hexdigest()
        body = raw_lines[1:]
        n_body = len(body)
        for j, raw_line in enumerate(body):
            is_last_line_overall = is_last_segment and j == n_body - 1
            if is_last_line_overall and not ends_with_nl:
                # queue jamais acquittee (pas de \n final) -> quarantaine,
                # jamais interpretee comme valide meme si elle parse.
                tail_status = "TORN_TAIL_QUARANTINED"
                stop = True
                break
            try:
                env = json.loads(raw_line.decode("utf-8"))
                ok = _valid_envelope_struct(env)
            except (ValueError, UnicodeDecodeError):
                env = None
                ok = False
            if not ok:
                errors.append("RECORD_INVALID:%d:%d" % (i, j))
                intact = False
                stop = True
                break
            expected_seq = global_seq + 1
            if env["seq"] != expected_seq:
                errors.append("SEQ_NOT_MONOTONIC:%d:%d" % (i, j))
                intact = False
                stop = True
                break
            if env["prev_line_sha256_16"] != last_line_hash_full[:16]:
                errors.append("HASH_CHAIN_BROKEN:%d:%d" % (i, j))
                intact = False
                stop = True
                break
            global_seq = expected_seq
            total_records += 1
            payloads.append((expected_seq, env["payload"]))
            last_line_hash_full = hashlib.sha256(raw_line).hexdigest()
            valid_end_offset += len(raw_line) + 1
        if i == n - 1:
            valid_end_offset_last_segment = valid_end_offset
        prev_tail_full = last_line_hash_full
    return {
        "intact": intact,
        "errors": errors,
        "segments": n,
        "records": total_records,
        "tail_status": tail_status,
        "last_seq": global_seq,
        "last_line_hash_full": last_line_hash_full,
        "last_segment_index": (n - 1) if n else -1,
        "payloads": payloads,
        "valid_end_offset_last_segment": valid_end_offset_last_segment,
    }


# --------------------------------------------------------------------------- #
# JournalWriter
# --------------------------------------------------------------------------- #
class JournalWriter:
    """Ecrivain append-only, single-writer, thread-safe. Voir contrat du module."""

    def __init__(
        self,
        path: "Path | str",
        *,
        allowed_dir: "Path | str | None" = None,
        fsync_per_append: bool = True,
        max_segment_bytes: int = JOURNAL_MAX_SEGMENT_BYTES_DEFAULT,
        writer_token: "str | None" = None,
    ) -> None:
        self._path = Path(path)
        self._allowed_dir = Path(allowed_dir) if allowed_dir is not None else self._path.parent
        self._fsync = bool(fsync_per_append)
        self._max_segment_bytes = int(max_segment_bytes)
        if self._max_segment_bytes <= 0:
            raise ValidationError("MAX_SEGMENT_BYTES_INVALID")
        self._writer_token = writer_token or uuid.uuid4().hex
        if not isinstance(self._writer_token, str) or not self._writer_token:
            raise ValidationError("WRITER_TOKEN_INVALID")
        self._lock_path = Path(str(self._path) + ".lock")
        self._thread_lock = threading.RLock()
        self._fh = None
        self._closed = False
        self._poisoned = False
        self._poison_reason: "str | None" = None
        self._segment_index = 0
        self._current_bytes = 0
        self._current_record_count = 0
        self._last_line_hash_full = "GENESIS"
        self._seq = 0
        self._resume_report = {
            "resumed": False,
            "torn_tail_truncated": False,
            "truncated_bytes": 0,
            "resume_seq": 0,
            "segment_index": 0,
        }
        self._check_path()
        self._acquire_lock()
        try:
            self._init_segments()
        except Exception:
            self._release_lock()
            raise

    # ------------------------------------------------------------- garde
    def _check_path(self) -> None:
        if self._path.is_symlink():
            raise ValidationError("JOURNAL_PATH_SYMLINK")
        if self._path.parent.resolve() != self._allowed_dir.resolve():
            raise ValidationError("JOURNAL_PATH_OUTSIDE_ALLOWED_DIR")

    def _acquire_lock(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise OwnershipError("JOURNAL_LOCK_HELD") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"writer_token": self._writer_token}))
        except OSError as exc:
            try:
                os.unlink(self._lock_path)
            except OSError:
                pass
            raise OwnershipError("JOURNAL_LOCK_WRITE_FAILED") from exc

    def _release_lock(self) -> None:
        """Best-effort : ne libere QUE le lock que ce writer possede (meme
        writer_token). Un lock orphelin (autre proprietaire) n'est jamais
        touche par ce chemin."""
        try:
            if self._lock_path.exists():
                obj = json.loads(self._lock_path.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and obj.get("writer_token") == self._writer_token:
                    self._lock_path.unlink()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------- init/reprise
    def _init_segments(self) -> None:
        segs = _list_segments(self._path)
        if not segs:
            self._create_new_segment(index=0, prev_tail_sha="GENESIS")
            self._seq = 0
            self._resume_report = {
                "resumed": False,
                "torn_tail_truncated": False,
                "truncated_bytes": 0,
                "resume_seq": 0,
                "segment_index": 0,
            }
            return
        scan = _scan_journal(segs)
        if not scan["intact"]:
            raise JournalCorruptedError(",".join(scan["errors"]) or "JOURNAL_CORRUPTED")
        last_index = scan["last_segment_index"]
        seg_path = _segment_path(self._path, last_index)
        torn = scan["tail_status"] == "TORN_TAIL_QUARANTINED"
        truncated_bytes = 0
        if torn:
            # SEULE exception a "jamais de seek arriere" : on tronque une
            # queue jamais acquittee (ni flush ni fsync termines cote
            # ancien writer). Aucune ligne valide n'est touchee. Observable
            # ci-dessous via resume_report() -- jamais silencieuse.
            original_size = seg_path.stat().st_size
            valid_end = scan["valid_end_offset_last_segment"]
            with open(seg_path, "r+b") as fh:
                fh.truncate(valid_end)
            current_bytes = valid_end
            truncated_bytes = original_size - valid_end
        else:
            current_bytes = seg_path.stat().st_size
        self._attach_segment(
            index=last_index,
            current_bytes=current_bytes,
            last_line_hash_full=scan["last_line_hash_full"],
        )
        self._seq = scan["last_seq"]
        self._resume_report = {
            "resumed": True,
            "torn_tail_truncated": torn,
            "truncated_bytes": truncated_bytes,
            "resume_seq": self._seq,
            "segment_index": last_index,
        }

    def _poison(self, reason: str) -> None:
        """Marque l'instance TERMINALEMENT empoisonnee (jamais reinitialise).
        Idempotent : la PREMIERE raison gagne, jamais ecrasee par une panne
        secondaire survenant pendant le nettoyage best-effort qui suit."""
        if not self._poisoned:
            self._poisoned = True
            self._poison_reason = reason

    def _create_new_segment(self, index: int, prev_tail_sha: str) -> None:
        header = {
            "schema": JOURNAL_SCHEMA_NAME,
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "segment_index": index,
            "prev_segment_tail_sha": prev_tail_sha,
            "writer_token": self._writer_token,
        }
        header_text = json.dumps(header, sort_keys=True, separators=(",", ":"))
        header_bytes = header_text.encode("utf-8")
        path = _segment_path(self._path, index)
        fh = None
        try:
            fh = open(path, "a", encoding="utf-8", newline="")
            fh.write(header_text + "\n")
            fh.flush()
            if self._fsync:
                os.fsync(fh.fileno())
        except Exception as exc:
            # GAP 1/GAP 2 (corriges) : toute panne d'E/S ici (creation du
            # tout premier segment OU d'un segment de rotation) empoisonne
            # l'instance -- plus jamais d'ecriture depuis CETTE instance --
            # puis nettoie au mieux un segment orphelin resté VIDE (jamais un
            # segment partiellement ecrit : si des octets reels sont deja
            # durables, ce n'est plus "vide" et on ne le touche jamais). Si
            # le nettoyage lui-meme echoue, le comportement fail-closed
            # EXISTANT est inchange : la reouverture suivante leve toujours
            # ``JournalCorruptedError("SEGMENT_EMPTY:<index>")``, jamais un
            # chargement silencieux.
            self._poison("SEGMENT_CREATE_FAILED:%s" % type(exc).__name__)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            try:
                if path.exists() and path.stat().st_size == 0:
                    path.unlink()
            except OSError:
                pass
            raise
        self._fh = fh
        self._segment_index = index
        self._current_bytes = len(header_bytes) + 1
        self._current_record_count = 0
        self._last_line_hash_full = hashlib.sha256(header_bytes).hexdigest()

    def _attach_segment(self, index: int, current_bytes: int, last_line_hash_full: str) -> None:
        path = _segment_path(self._path, index)
        self._fh = open(path, "a", encoding="utf-8", newline="")
        self._segment_index = index
        self._current_bytes = current_bytes
        # sentinelle conservatrice : un segment repris est traite comme
        # "non vide" pour la garde anti-boucle de rotation (voir append()),
        # meme s'il ne contenait en realite que le header.
        self._current_record_count = 1
        self._last_line_hash_full = last_line_hash_full

    def _rotate(self) -> None:
        try:
            self._fh.close()
        except Exception as exc:
            self._poison("ROTATE_CLOSE_FAILED:%s" % type(exc).__name__)
            raise
        prev_tail = self._last_line_hash_full
        self._create_new_segment(index=self._segment_index + 1, prev_tail_sha=prev_tail)

    # ------------------------------------------------------------- API
    def append(self, payload: dict) -> dict:
        """Ajoute une ligne. Serialise ENTIEREMENT avant d'ecrire (jamais
        d'ecriture partielle) ; jamais de seek arriere ; thread-safe.

        GARDE POISONED : verifiee EN TETE, AVANT toute tentative d'E/S. Si
        une panne d'E/S anterieure a deja empoisonne l'instance (voir la
        section "ETAT POISONED" du docstring du module), leve immediatement
        ``JournalPoisonedError`` sans jamais toucher le disque ni l'etat
        interne. Toute panne d'E/S survenant ICI (ecriture/rotation) marque a
        son tour l'instance empoisonnee avant de re-lever l'exception
        d'origine -- ``self._seq`` n'avance JAMAIS sur un ``append()`` qui
        leve, empoisonne ou non."""
        if self._poisoned:
            raise JournalPoisonedError(self._poison_reason)
        if self._closed:
            raise ValidationError("WRITER_CLOSED")
        if not isinstance(payload, dict):
            raise ValidationError("PAYLOAD_NOT_DICT")
        payload_copy = dict(payload)
        with self._thread_lock:
            if self._poisoned:  # re-verifie sous verrou (course inter-threads)
                raise JournalPoisonedError(self._poison_reason)
            next_seq = self._seq + 1
            envelope = {
                "seq": next_seq,
                "prev_line_sha256_16": self._last_line_hash_full[:16],
                "payload": payload_copy,
            }
            try:
                line_text = json.dumps(
                    envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValidationError("PAYLOAD_NOT_SERIALIZABLE") from exc
            line_bytes = line_text.encode("utf-8")
            prospective = self._current_bytes + len(line_bytes) + 1
            try:
                if self._current_record_count > 0 and prospective > self._max_segment_bytes:
                    self._rotate()
                    envelope["prev_line_sha256_16"] = self._last_line_hash_full[:16]
                    line_text = json.dumps(
                        envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    line_bytes = line_text.encode("utf-8")
                self._fh.write(line_text + "\n")
                self._fh.flush()
                if self._fsync:
                    os.fsync(self._fh.fileno())
            except Exception as exc:
                # GAP 2 (corrige) : plus jamais de retour "succes" silencieux
                # sur l'append SUIVANT -- l'instance est empoisonnee ICI,
                # avant tout retour a l'appelant.
                self._poison("APPEND_IO_FAILED:%s" % type(exc).__name__)
                raise
            self._current_bytes += len(line_bytes) + 1
            self._current_record_count += 1
            self._last_line_hash_full = hashlib.sha256(line_bytes).hexdigest()
            self._seq = next_seq
            return dict(envelope)

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._fh is not None:
                    self._fh.close()
            finally:
                self._release_lock()

    def __enter__(self) -> "JournalWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def segment_index(self) -> int:
        return self._segment_index

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def writer_token(self) -> str:
        return self._writer_token

    @property
    def poisoned(self) -> bool:
        """Etat terminal courant (voir "ETAT POISONED" dans le docstring du
        module) : lu EN DIRECT (jamais fige), contrairement au reste de
        ``resume_report()``."""
        return self._poisoned

    @property
    def poison_reason(self) -> "str | None":
        """Raison sanitisee de l'empoisonnement (``None`` tant que
        l'instance n'est pas empoisonnee) ; jamais de contenu sensible,
        uniquement un code diagnostique."""
        return self._poison_reason

    def resume_report(self) -> dict:
        """Observabilite de la reprise ET de l'etat poisoned courant :
        ``{resumed, torn_tail_truncated, truncated_bytes, resume_seq,
        segment_index, poisoned, poison_reason}``. Les 5 premiers champs
        sont PURS (figes juste apres ``_init_segments`` dans ``__init__``,
        jamais recalcules ensuite) ; un journal neuf donne des valeurs
        neutres (``resumed=False``, ``truncated_bytes=0``). Une troncature
        de queue non acquittee (crash) N'EST JAMAIS SILENCIEUSE : elle est
        toujours visible ici (``torn_tail_truncated=True`` + le nombre exact
        d'octets ecartes). ``poisoned``/``poison_reason`` sont au contraire
        lus EN DIRECT a chaque appel (voir les proprietes ``poisoned``/
        ``poison_reason``), puisque l'empoisonnement peut survenir a tout
        moment apres l'ouverture. Rien ici n'affecte le format sur disque ni
        le comportement d'ecriture — lecture seule d'un etat deja fige plus
        un instantane de l'etat poisoned."""
        report = dict(self._resume_report)
        report["poisoned"] = self._poisoned
        report["poison_reason"] = self._poison_reason
        return report


# --------------------------------------------------------------------------- #
# JournalReader
# --------------------------------------------------------------------------- #
class JournalReader:
    """Lecteur read-only. Voir contrat du module pour ``verify``/``replay``."""

    def __init__(self, path_base: "Path | str") -> None:
        self._path = Path(path_base)

    def _segments(self) -> "list[Path]":
        return _list_segments(self._path)

    def verify(self) -> dict:
        segs = self._segments()
        if not segs:
            return {"intact": True, "segments": 0, "records": 0,
                     "tail_status": "CLEAN", "errors": []}
        r = _scan_journal(segs)
        return {
            "intact": r["intact"],
            "segments": r["segments"],
            "records": r["records"],
            "tail_status": r["tail_status"],
            "errors": r["errors"],
        }

    def replay(self) -> "list[dict]":
        """Rejoue les payloads dans l'ordre ``seq``. Leve
        ``JournalCorruptedError`` si le journal n'est pas intact (une queue
        ``TORN_TAIL_QUARANTINED`` seule n'empeche PAS le replay des
        enregistrements precedents, qui restent intacts)."""
        segs = self._segments()
        if not segs:
            return []
        r = _scan_journal(segs)
        if not r["intact"]:
            raise JournalCorruptedError(",".join(r["errors"]) or "JOURNAL_CORRUPTED")
        return [payload for _seq, payload in r["payloads"]]


# --------------------------------------------------------------------------- #
# Pont laboratoire : reconstruction lifecycle depuis le journal
# --------------------------------------------------------------------------- #
# Import autorise (coherence des regles d'idempotence/conflit) ; ce module ne
# modifie JAMAIS lifecycle_identity_store.py.
from app.services.lifecycle_identity_store import (  # noqa: E402
    broker_ref_key,
    try_build_broker_ref as _try_build_broker_ref,  # noqa: F401  (reexport documentaire)
)

_CREATE_FIELDS = frozenset({
    "op", "lifecycle_id", "correlation_id", "created_at_utc", "boot_id",
    "cycle_id", "setup_id",
})
_BIND_FIELDS = frozenset({"op", "lifecycle_id", "ref"})
_CLOSE_FIELDS = frozenset({"op", "lifecycle_id", "closed_at_utc"})


class _ReplayConflict(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _apply_create(payload: dict, lifecycles: dict) -> None:
    if set(payload.keys()) != _CREATE_FIELDS:
        raise _ReplayConflict("CREATE_PAYLOAD_INVALID")
    lid = payload.get("lifecycle_id")
    if not isinstance(lid, str) or not lid:
        raise _ReplayConflict("LIFECYCLE_ID_INVALID")
    if lid in lifecycles:
        raise _ReplayConflict("LIFECYCLE_ALREADY_EXISTS")
    lifecycles[lid] = {
        "lifecycle_id": lid,
        "correlation_id": payload.get("correlation_id"),
        "boot_id": payload.get("boot_id"),
        "cycle_id": payload.get("cycle_id"),
        "setup_id": payload.get("setup_id"),
        "state": "OPEN",
        "created_at_utc": payload.get("created_at_utc"),
        "closed_at_utc": None,
        "broker_ref": None,
        "broker_key": None,
    }


def _apply_bind(payload: dict, lifecycles: dict, broker_index: dict) -> None:
    if set(payload.keys()) != _BIND_FIELDS:
        raise _ReplayConflict("BIND_PAYLOAD_INVALID")
    lid = payload.get("lifecycle_id")
    record = lifecycles.get(lid)
    if record is None:
        raise _ReplayConflict("LIFECYCLE_UNKNOWN")
    ref = payload.get("ref")
    if not isinstance(ref, dict):
        raise _ReplayConflict("BROKER_REF_INVALID")
    key = broker_ref_key(ref)
    existing_owner = broker_index.get(key)
    if record["broker_key"] == key and existing_owner == lid:
        return  # ALREADY_BOUND : no-op valide, jamais un conflit
    if existing_owner is not None and existing_owner != lid:
        raise _ReplayConflict("REFERENCE_ALREADY_BOUND")
    if record["broker_key"] is not None and record["broker_key"] != key:
        raise _ReplayConflict("LIFECYCLE_ALREADY_BOUND")
    record["broker_ref"] = dict(ref)
    record["broker_key"] = key
    broker_index[key] = lid


def _apply_mark_closed(payload: dict, lifecycles: dict) -> None:
    if set(payload.keys()) != _CLOSE_FIELDS:
        raise _ReplayConflict("CLOSE_PAYLOAD_INVALID")
    lid = payload.get("lifecycle_id")
    record = lifecycles.get(lid)
    if record is None:
        raise _ReplayConflict("LIFECYCLE_UNKNOWN")
    if record["state"] == "CLOSED":
        return  # ALREADY_CLOSED : no-op valide, closed_at jamais ecrase
    record["state"] = "CLOSED"
    record["closed_at_utc"] = payload.get("closed_at_utc")


def rebuild_lifecycle_state(reader: JournalReader) -> dict:
    """PONT LABO (lecture seule) : rejoue un journal d'operations
    ``create``/``bind``/``mark_closed`` (memes champs que
    ``LifecycleIdentityStore``) et reconstruit ``{lifecycles, broker_index}``
    en appliquant EXACTEMENT les memes regles d'idempotence/conflit que le
    store (``broker_ref_key`` importe pour coherence). Ne construit et ne
    modifie JAMAIS un ``LifecycleIdentityStore`` reel. Fail-closed par
    construction : un payload malforme ou un conflit est COMPTE et IGNORE,
    jamais un ecrasement, jamais une exception qui remonte a l'appelant."""
    payloads = reader.replay()
    lifecycles: dict = {}
    broker_index: dict = {}
    conflicts: "list[dict]" = []
    applied = 0
    ignored = 0
    for payload in payloads:
        op = payload.get("op") if isinstance(payload, dict) else None
        lid_hint = payload.get("lifecycle_id") if isinstance(payload, dict) else None
        try:
            if op == "create":
                _apply_create(payload, lifecycles)
            elif op == "bind":
                _apply_bind(payload, lifecycles, broker_index)
            elif op == "mark_closed":
                _apply_mark_closed(payload, lifecycles)
            else:
                raise _ReplayConflict("UNKNOWN_OP")
            applied += 1
        except _ReplayConflict as exc:
            conflicts.append({"op": op, "lifecycle_id": lid_hint, "reason": exc.reason})
            ignored += 1
        except Exception as exc:  # jamais de crash sur un payload malforme
            conflicts.append({
                "op": op, "lifecycle_id": lid_hint,
                "reason": "MALFORMED_PAYLOAD:%s" % type(exc).__name__,
            })
            ignored += 1
    return {
        "lifecycles": lifecycles,
        "broker_index": broker_index,
        "conflicts": conflicts,
        "applied": applied,
        "ignored": ignored,
    }
