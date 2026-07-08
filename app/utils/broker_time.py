# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_DATE.md (2026-07-08) — source de temps broker
UNIQUE, centralisée. 4e bug de la famille "fenêtre jour-broker calculée sur
une mauvaise date" (kill-switch a montré une fenêtre pointant sur un mois
passé à un moment donné le 2026-07-08, alors que le code appelant
`datetime.now(timezone.utc)` frais à chaque évaluation — investigation
n'a trouvé aucune valeur figée/mise en cache dans le code Python ; le
suspect le plus plausible est un aléa d'horloge système/reconnexion MT5,
transitoire, auto-corrigé par un redémarrage du bot. Que la cause soit un
bug de code ou un aléa système, la parade est la même : ne JAMAIS faire
confiance à une fenêtre calculée sans la vérifier contre l'heure murale, et
alerter fort si elle dérive.

Tout module qui a besoin de "quel jour est-on, côté broker" doit passer par
ce fichier — pas de recalcul local, pas de duplication (daily_killswitch.py,
app/dashboard_api/data.py migrés dans ce mission).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.logger import log

_FUTURE_TAIL_HOURS = 2.0
_STALE_WINDOW_ALERT_HOURS = 48.0


def broker_now_utc() -> datetime:
    """Point d'entrée UNIQUE pour "l'heure actuelle" partout où une fenêtre
    jour-broker est calculée. Toujours fraîche — jamais mise en cache,
    jamais dérivée d'un tick/deal historique. Centraliser ici permet de
    monkeypatcher un seul point pour les tests ET de garantir qu'aucun
    module ne réinvente sa propre lecture d'horloge divergente."""
    return datetime.now(timezone.utc)


def broker_day_window(
    now_utc: datetime | None = None,
    broker_utc_offset_hours: float = 3.0,
) -> tuple[datetime, datetime]:
    """[minuit broker, now + 2h], les deux en UTC. now_utc=None => heure
    murale fraîche via broker_now_utc(). Passer une valeur explicite reste
    possible (tests, remplecture historique) mais n'est JAMAIS le chemin
    utilisé par le trading live.

    Garde-fou anti-régression intégré : si la fenêtre calculée démarre à
    plus de 48h dans le passé par rapport à now_utc, une alerte CRITIQUE
    est loguée immédiatement — c'est exactement la signature du bug de
    cette mission (kill-switch aveugle sur une fenêtre historique morte) et
    ça doit être visible dès la première occurrence, plus jamais découvert
    a posteriori dans un rapport."""
    now_utc = now_utc if now_utc is not None else broker_now_utc()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    offset = timedelta(hours=float(broker_utc_offset_hours))
    broker_now = now_utc + offset
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = broker_midnight - offset
    end_utc = now_utc + timedelta(hours=_FUTURE_TAIL_HOURS)

    age_hours = (now_utc - start_utc).total_seconds() / 3600.0
    if age_hours > _STALE_WINDOW_ALERT_HOURS:
        log.critical(
            "[BROKER_TIME_GUARD] ALERTE CRITIQUE fenetre jour-broker perimee : "
            "now_utc_detected=%s window_start=%s age=%.1fh (> %.0fh) — "
            "verifier l'horloge systeme et/ou une source de temps figee en amont",
            now_utc.isoformat(), start_utc.isoformat(), age_hours, _STALE_WINDOW_ALERT_HOURS,
        )

    return start_utc, end_utc


def is_window_stale(start_utc: datetime, now_utc: datetime | None = None, max_age_hours: float = _STALE_WINDOW_ALERT_HOURS) -> bool:
    """Réutilisable hors broker_day_window pour tout code qui veut vérifier
    une fenêtre déjà calculée ailleurs (ex: tests, diagnostics)."""
    now_utc = now_utc if now_utc is not None else broker_now_utc()
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    age_hours = (now_utc - start_utc).total_seconds() / 3600.0
    return age_hours > max_age_hours
