"""SYSTEM_HEARTBEAT — émetteur infrastructure de preuve de vie (A0.3-R6B1).

Prouve UNE seule chose : la boucle principale (`run_cycle`) termine réellement
des cycles. L'audit A0.3-R6A a montré que `account_diagnostics` n'est pas un
heartbeat — c'est un champ conditionnel des événements DEMO_SKIP /
DEMO_ORDER_READY, émis seulement quand un setup routeable atteint le routeur
DEMO. La boucle peut donc être vivante sans plus jamais l'émettre.

Règles de conception :
- appelé UNIQUEMENT depuis le fil principal, en toute fin de `run_cycle()`
  (jamais depuis un thread daemon : un thread qui survit à une boucle bloquée
  ne doit jamais fabriquer une fausse preuve de vie) ;
- aucune logique trading : pas de décision, pas de risque, pas d'exécution ;
- écrit via le writer JSONL générique du routeur (`_record_event`) — même
  verrou, même rotation, même fichier `demo_pilot_events.jsonl` ;
- fail-soft : un échec d'écriture est compté et journalisé (filtré), jamais
  propagé au cycle, jamais transformé en heartbeat « réussi » ;
- flag OFF (défaut) : retour immédiat, zéro événement, zéro état muté.

`mt5_connected` est une information d'état embarquée, PAS une preuve de vie :
la liveness HERMES et la santé MT5 restent deux jugements distincts côté
Control Tower.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from app.logger import log
from app.utils.throttle import log_event_throttled

SYSTEM_HEARTBEAT_EVENT_TYPE = "SYSTEM_HEARTBEAT"
SYSTEM_HEARTBEAT_PRODUCER = "hermes_main_loop"

# R6B1-R1 — convention UNIQUE du champ `mode` : les trois etats reels de
# HERMES, derives des settings. Jamais de valeur libre ou vide ; producteur
# et validateur CT partagent exactement cet ensemble.
SYSTEM_HEARTBEAT_ALLOWED_MODES = frozenset({"READ_ONLY", "DEMO", "LIVE_DISABLED"})


class SystemHeartbeatEmitter:
    """Émetteur sans état trading. Une instance = un processus = un boot_id.

    - ``boot_id`` : UUID généré UNE fois à la construction (au démarrage du
      processus) ; jamais réutilisé après restart.
    - ``cycle_id`` : entier strictement croissant au sein d'un même boot_id ;
      un reset n'est possible qu'avec un nouveau boot_id (nouveau processus).
    - throttle : horloge monotone (insensible aux sauts NTP) ; le premier
      cycle terminé émet toujours, puis au plus une émission par
      ``interval_seconds``.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: float,
        record_event: Callable[[dict], None],
        mode: str,
        now_monotonic: Callable[[], float] = time.monotonic,
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        interval = float(interval_seconds)
        if interval <= 0:
            raise ValueError(
                "SYSTEM_HEARTBEAT interval_seconds doit etre > 0 (recu %r)" % (interval_seconds,)
            )
        mode_value = str(mode).strip()
        if mode_value not in SYSTEM_HEARTBEAT_ALLOWED_MODES:
            raise ValueError(
                "SYSTEM_HEARTBEAT mode invalide %r (attendu: %s)"
                % (mode, "|".join(sorted(SYSTEM_HEARTBEAT_ALLOWED_MODES)))
            )
        self.enabled = bool(enabled)
        self.interval_seconds = interval
        self._record_event = record_event
        self._mode = mode_value
        self._now_monotonic = now_monotonic
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self.boot_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.cycle_id = 0
        self.emitted_count = 0
        self.error_count = 0
        self._last_emit_monotonic: float | None = None

    def build_event(self, *, mt5_connected: bool) -> dict:
        """Schéma strict — exactement les champs du contrat A0.3-R6B1."""
        return {
            "event_type": SYSTEM_HEARTBEAT_EVENT_TYPE,
            "created_at": self._now_utc().isoformat(),
            "pid": self.pid,
            "boot_id": self.boot_id,
            "cycle_id": self.cycle_id,
            "mode": self._mode,
            "producer": SYSTEM_HEARTBEAT_PRODUCER,
            "mt5_connected": bool(mt5_connected),
        }

    def on_cycle_complete(self, *, mt5_connected: bool) -> dict | None:
        """À appeler depuis le fil principal quand le cycle est réellement
        terminé. Retourne l'événement émis, ou None (flag OFF, throttle,
        échec d'écriture). Ne lève JAMAIS vers le cycle."""
        if not self.enabled:
            return None
        try:
            self.cycle_id += 1
            now_mono = self._now_monotonic()
            if (
                self._last_emit_monotonic is not None
                and (now_mono - self._last_emit_monotonic) < self.interval_seconds
            ):
                return None
            event = self.build_event(mt5_connected=mt5_connected)
            self._record_event(event)
            # L'état « émis » n'est avancé QU'APRÈS une écriture réussie :
            # un échec ne fabrique jamais un heartbeat réussi, et le cycle
            # suivant retente immédiatement.
            self._last_emit_monotonic = now_mono
            self.emitted_count += 1
            return event
        except Exception as exc:
            self.error_count += 1
            log_event_throttled(
                "SYSTEM_HEARTBEAT_WRITE_ERROR",
                "[SYSTEM_HEARTBEAT] write_failed count=%s error=%s"
                % (self.error_count, str(exc)[:160]),
                state=type(exc).__name__,
            )
            return None


def build_system_heartbeat_emitter(settings, record_event: Callable[[dict], None]) -> SystemHeartbeatEmitter:
    """Construction fail-safe pour le boot du bot : une config invalide
    désactive l'émetteur (fail-closed côté heartbeat — la Control Tower verra
    ABSENT/STALE et bloquera) sans jamais empêcher le trading de démarrer."""
    if settings.read_only:
        mode = "READ_ONLY"
    elif settings.demo_trading and settings.demo_only:
        mode = "DEMO"
    else:
        mode = "LIVE_DISABLED"
    try:
        return SystemHeartbeatEmitter(
            enabled=settings.hermes_system_heartbeat_enabled,
            interval_seconds=settings.system_heartbeat_interval_seconds,
            record_event=record_event,
            mode=mode,
        )
    except ValueError as exc:
        log.error("[SYSTEM_HEARTBEAT] disabled reason=CONFIG_INVALID error=%s", exc)
        return SystemHeartbeatEmitter(
            enabled=False, interval_seconds=60, record_event=record_event, mode=mode
        )
