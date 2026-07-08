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

    Garde-fou anti-régression intégré : la fenêtre calculée est comparée à
    l'horloge murale INDÉPENDANTE (broker_now_utc(), jamais à now_utc
    lui-même — comparer une fenêtre à la valeur qui a servi à la calculer
    est toujours cohérent PAR CONSTRUCTION et ne peut jamais rien détecter,
    piège dans lequel une première version de ce garde-fou est tombée
    pendant cette mission, révélé par son propre test). Si l'écart dépasse
    48h, une alerte CRITIQUE est loguée immédiatement — c'est exactement la
    signature du bug de cette mission (kill-switch aveugle sur une fenêtre
    historique morte) et ça doit être visible dès la première occurrence,
    plus jamais découvert a posteriori dans un rapport."""
    now_utc = now_utc if now_utc is not None else broker_now_utc()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    offset = timedelta(hours=float(broker_utc_offset_hours))
    broker_now = now_utc + offset
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = broker_midnight - offset
    end_utc = now_utc + timedelta(hours=_FUTURE_TAIL_HOURS)

    wall_clock = broker_now_utc()
    age_hours = (wall_clock - start_utc).total_seconds() / 3600.0
    if age_hours > _STALE_WINDOW_ALERT_HOURS:
        log.critical(
            "[BROKER_TIME_GUARD] ALERTE CRITIQUE fenetre jour-broker perimee : "
            "now_utc_detected=%s wall_clock=%s window_start=%s age=%.1fh (> %.0fh) — "
            "verifier l'horloge systeme et/ou une source de temps figee en amont",
            now_utc.isoformat(), wall_clock.isoformat(), start_utc.isoformat(),
            age_hours, _STALE_WINDOW_ALERT_HOURS,
        )

    return start_utc, end_utc


def to_mt5_query_bounds(
    start_utc: datetime,
    end_utc: datetime,
    broker_utc_offset_hours: float = 3.0,
) -> tuple[datetime, datetime]:
    """mission/FIX_KILLSWITCH_PNL.md (2026-07-08) — converts TRUE-UTC
    instants into the broker-wall-clock-SHAPED naive datetimes that
    mt5.history_deals_get() actually compares against.

    Root cause found this mission: MT5 stamps deal.time using the broker
    server's OWN wall clock (UTC+3 for XM), not true UTC — verified
    empirically: fromtimestamp(tick.time, tz=utc) read "16:47:30" while the
    genuine system UTC instant was "13:47:29", a consistent +3h offset. The
    MT5 Python API does not correct for this and does not respect tzinfo:
    it compares the raw clock FIELDS of whatever you pass against the raw
    clock fields of deal.time. A bare `start_utc.replace(tzinfo=None)`
    (stripping tzinfo without shifting the clock fields) silently queries
    3 real hours too early — pulling in the last ~3h of the PREVIOUS
    broker day and mislabeling them as "today". This was the exact
    mechanism behind the kill-switch counting 6 losses (including 3 from
    the prior broker day) while CYCLE_SUMMARY, whose narrower window
    happened not to cross that boundary, correctly showed only 3.

    Every call site that passes a day-window boundary (built via
    broker_day_window(), which returns genuine TRUE-UTC instants) to
    mt5.history_deals_get() MUST convert through this function first —
    never pass broker_day_window()'s output to MT5 directly."""
    offset = timedelta(hours=float(broker_utc_offset_hours))
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=timezone.utc)
    return (start_utc + offset).replace(tzinfo=None), (end_utc + offset).replace(tzinfo=None)


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
