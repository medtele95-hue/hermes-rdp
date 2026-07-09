# NEWS_GAP_VERDICT — trade GOLD# SELL 2026-07-08 15:30:46 UTC (-42.85 $)

**Lecture seule. Aucune modification.**

## Timestamp exact de l'event

Source : `app/data/news_calendar_cache.json` (cache ForexFactory réellement utilisé par HERMES, `app.services.protected_calendar.NewsCalendar`). Seul événement `HIGH` impact `USD` du 2026-07-08 :

```
2026-07-08T18:00:00+00:00  USD  High  FOMC Meeting Minutes
```

Aucun autre événement `HIGH`/`USD` ce jour-là (16 événements au total sur la journée, tous les autres sont `Low`/`Medium` ou d'une autre devise — vérifié exhaustivement, pas seulement l'événement qui a fini par déclencher la clôture).

## Écart

- Entrée : `2026-07-08T15:30:46.641030+00:00` (UTC vrai, `decision_dataset.jsonl`, confirmé par le deal MT5 d'ouverture).
- Événement : `2026-07-08T18:00:00+00:00`.
- **Écart : 2h 29min 14s (149.23 minutes)**, entrée AVANT l'événement.
- Fenêtres de blocage configurées : `news_blackout_window_minutes=10` (entrée) et `news_preclose_window_minutes=10` (pré-clôture) — **toutes deux à 10 minutes** (`app/config.py:124-125`).

149 minutes est ~15× la fenêtre de 10 minutes, dans les deux sens.

## Vérification de cohérence entre les deux mécanismes

- **Blocage d'entrée** : `app/mt5/demo_router.py:1290`, dans `DemoRouter.evaluate()` — `self.news_calendar.news_blackout(now_dt)`. Bloque toute entrée si un événement `HIGH USD` (n'importe lequel, pas seulement les "majors") tombe dans `[event-10min, event+10min]`, fenêtre **symétrique**.
- **Pré-clôture de sortie** : `app/mt5/demo_router.py:568`, dans `DemoRouter.process_quick_exits()` — `self.news_calendar.major_preclose_event(_cal_now)`. Ferme les positions magic=909002 non-armées si un événement dont le titre contient NFP/FOMC/CPI arrive dans `[now, now+10min]`, fenêtre **future uniquement**.
- **Les deux lisent la MÊME instance `NewsCalendar`, le MÊME cache, la MÊME liste `high_usd_events()`** — aucune divergence de source de données trouvée. La seule différence est le filtre (tous les HIGH USD vs. seulement les "majors" nommés) et la direction de la fenêtre (symétrique vs. future uniquement) — sans incidence ici puisque l'écart réel (149 min) dépasse largement les deux fenêtres, dans les deux sens.
- Preuve indirecte que le blocage d'entrée fonctionnait normalement ce jour-là : 43 refus `NEWS_BLACKOUT` recensés dans la fenêtre du rapport 24h, concentrés entre 17:50 et 18:09 UTC — exactement `[18:00-10min, 18:00+10min]`, la fenêtre attendue autour de ce même événement FOMC. Le mécanisme d'entrée a bloqué les setups qui, eux, tombaient réellement dans la fenêtre ; il n'a pas bloqué celui de 15:30:46 parce que celui-ci n'y était pas.

## Verdict

**TUNING** — pas un bug. L'entrée à 15:30:46 était légitimement hors de la fenêtre ±10 minutes de tout événement `HIGH USD` ce jour-là. Le marché s'est simplement dégradé sur les ~2h29 de vie du trade (voir `reports/AUTOPSIE_377299478.md` pour le détail MFE/MAE), et la position — jamais armée — a été fermée par précaution 9min42 avant la publication FOMC, un mécanisme de protection indépendant et distinct du blocage d'entrée, qui a fonctionné exactement comme conçu.

Élargir la fenêtre de blocage d'entrée pour capturer ce cas précis serait du **tuning sur un seul trade** — explicitement hors périmètre de cette mission. **Aucune modification apportée.**
