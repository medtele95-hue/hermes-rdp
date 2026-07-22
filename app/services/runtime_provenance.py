"""RUNTIME_PROVENANCE — source canonique unique de provenance runtime (M02-P1B).

Module PUR (contrat identique a event_identity.py) : ce module NE fait JAMAIS
d'I/O, ne lit ni Git ni variable d'environnement, ne touche ni MT5 ni le
reseau, et n'a aucune dependance strategie/risque/execution. Stdlib seulement
(uuid, threading).

Probleme resolu : avant M02-P1B, ``boot_id``/``cycle_id`` n'existaient que
dans ``SystemHeartbeatEmitter`` (compteur prive) et l'enricher Identity SHADOW
n'avait AUCUNE provenance — deux producteurs ne pouvaient pas etre relies au
meme boot ni au meme cycle logique. Ce module fournit LA source partagee que
les deux consomment par INJECTION EXPLICITE (jamais de singleton implicite,
jamais de lecture d'env).

CONTRAT boot_id / cycle_id :
- ``boot_id`` : UUID (chaine canonique) genere UNE SEULE fois a la
  construction — une instance = un processus = un boot_id ; un restart est
  represente par une NOUVELLE instance (nouveau boot_id, jamais reutilise).
  Un ``boot_id`` injecte est valide (uuid.UUID) puis normalise.
- ``cycle_id`` : entier >= 0, strictement croissant, JAMAIS decremente.
  Il demarre a 0 (« aucun cycle commence ») ; ``begin_cycle()`` — appele
  exactement UNE fois par cycle logique, par le proprietaire de la boucle —
  l'incremente et retourne la nouvelle valeur (premier cycle = 1, aligne sur
  le contrat historique du heartbeat). Apres restart, le compteur repart a 0
  SOUS LE NOUVEAU boot_id : la paire (boot_id, cycle_id) reste globalement
  unique et non ambigue.
- Aucune horloge n'intervient : le cycle_id est un ordinal logique, pas un
  timestamp — une horloge murale qui recule est sans effet par construction.
- Thread-safe : compteur sous verrou ; ``snapshot()`` retourne une paire
  (boot_id, cycle_id) coherente lue sous le meme verrou.

Wiring (HORS PERIMETRE M02-P1B, documente pour la mission suivante) : le
proprietaire de la boucle (run_cycle) appellera ``begin_cycle()`` en TETE de
cycle, puis heartbeat et enricher LISENT ``snapshot()`` — le heartbeat de fin
de cycle N et les evenements SHADOW du cycle N portent donc le meme N.
"""
from __future__ import annotations

import threading
import uuid


class RuntimeProvenanceError(Exception):
    """Erreur de construction/validation du provider (jamais de secret)."""


class RuntimeProvenance:
    """Provider process-local de provenance runtime. Voir contrat du module."""

    __slots__ = ("_boot_id", "_lock", "_cycle_id")

    def __init__(self, *, boot_id: "str | None" = None) -> None:
        if boot_id is None:
            self._boot_id = str(uuid.uuid4())
        else:
            try:
                self._boot_id = str(uuid.UUID(str(boot_id)))
            except (ValueError, AttributeError, TypeError):
                raise RuntimeProvenanceError("boot_id injecte invalide (UUID requis)")
        self._lock = threading.Lock()
        self._cycle_id = 0

    @property
    def boot_id(self) -> str:
        """UUID canonique, immuable pour toute la vie de l'instance."""
        return self._boot_id

    @property
    def cycle_id(self) -> int:
        """Dernier cycle commence (0 = aucun). Ne decroit jamais."""
        with self._lock:
            return self._cycle_id

    def begin_cycle(self) -> int:
        """Demarre le cycle logique suivant. A appeler exactement une fois par
        cycle, par le proprietaire de la boucle. Retourne le nouveau cycle_id
        (>= 1), strictement croissant, jamais decremente."""
        with self._lock:
            self._cycle_id += 1
            return self._cycle_id

    def snapshot(self) -> "tuple[str, int]":
        """Paire (boot_id, cycle_id) coherente, lue atomiquement."""
        with self._lock:
            return self._boot_id, self._cycle_id
