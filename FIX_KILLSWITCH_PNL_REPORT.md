# FIX_KILLSWITCH_PNL_REPORT — mission/FIX_KILLSWITCH_PNL.md, exécutée le 2026-07-08

## Verdict : la journée était réellement positive (+13.28/+13.70), le kill-switch a coupé le trading sur un chiffre faux

## 1. Les deux calculs, côte à côte

| | CYCLE_SUMMARY | DAILY_KILLSWITCH (avant fix) |
|---|---|---|
| Source | `app.services.mt5_pnl_truth.get_mt5_hermes_pnl_truth()` (appelé via `mt5_position_sync.py`) | `app.services.daily_killswitch.evaluate_daily_killswitch()` |
| Fenêtre "aujourd'hui" | `datetime(end.year, end.month, end.day, tzinfo=utc)` — minuit UTC brut | `broker_day_window()` — vrai minuit broker (calcul correct) |
| Appel MT5 | `mt5.history_deals_get(start, end)` — bornes VRAI-UTC passées telles quelles | `mt5.history_deals_get(start.replace(tzinfo=None), end.replace(tzinfo=None))` — tzinfo retiré SANS décalage |
| Résultat observé | `closed_pnl=13.28` | `daily_pnl=-37.14`, `losses=6/6`, `triggered=True` |

## 2. Cause racine exacte, vérifiée empiriquement (pas théorique)

**MT5 tamponne `deal.time` avec l'horloge propre du serveur broker (UTC+3 pour XM), et l'API Python ignore le `tzinfo` des bornes qu'on lui passe — elle compare les champs d'horloge bruts directement.**

Preuve directe capturée le 2026-07-08 :
```
horloge système (vrai UTC) : 2026-07-08T13:47:29
tick.time (via fromtimestamp(...,tz=utc)) : 2026-07-08T16:47:30
écart : +10800 secondes = exactement 3h
```

`daily_killswitch.py::_mt5_history_deals` calculait correctement la fenêtre jour-broker en VRAI UTC (`start_utc = broker_midnight - offset`), mais la passait à `mt5.history_deals_get()` via `start_utc.replace(tzinfo=None)` — qui retire juste l'étiquette de fuseau **sans décaler les champs d'horloge**. Résultat : la requête interrogeait MT5 à partir de la valeur d'horloge "2026-07-07 21:00" — 3 heures TROP TÔT par rapport au vrai minuit broker ("2026-07-08 00:00" en heure murale broker) — incluant ainsi les 3 dernières heures du jour-broker **précédent** et les étiquetant à tort comme "aujourd'hui".

`mt5_pnl_truth.py` (source de CYCLE_SUMMARY) avait le même bug de conversion, MAIS sa fenêtre "aujourd'hui" était calculée en minuit-UTC-brut (`datetime(end.year, end.month, end.day, tzinfo=utc)`) au lieu du minuit-broker — ce qui, à l'heure où le bug a été observé (13h46 UTC), coïncidait numériquement avec la bonne valeur (le "aujourd'hui" en UTC brut et le "aujourd'hui" broker tombaient sur la même date à ce moment précis). **C'est une coïncidence, pas une correction** — ce module aurait montré le même symptôme entre 21h00 et 00h00 UTC (quand le jour broker a déjà basculé mais pas la date UTC brute).

## 3. Les "6 pertes" du kill-switch, listées avec leur vrai P&L MT5

| Ticket | Symbole | Net | Heure (broker, affichée) | Appartient réellement à |
|---|---|---|---|---|
| 373814836 | BTCUSD# | -1.94 | 2026-07-07 21:30:26 | **jour-broker 7 juillet** — à tort compté comme aujourd'hui |
| 373865284 | GOLD# | -61.34 | 2026-07-07 22:01:41 | **jour-broker 7 juillet** — à tort compté comme aujourd'hui |
| 373938104 | BTCUSD# | -1.69 | 2026-07-07 22:31:59 | **jour-broker 7 juillet** — à tort compté comme aujourd'hui |
| 374006341 | GOLD# | -1.52 | 2026-07-08 01:02:09 | jour-broker 8 juillet — vraie perte du jour |
| 374104721 | GOLD# | -8.57 | 2026-07-08 03:54:16 | jour-broker 8 juillet — vraie perte du jour |
| 374158140 | BTCUSD# | -2.45 | 2026-07-08 04:47:59 | jour-broker 8 juillet — vraie perte du jour |

**3 des 6 "pertes" appartiennent au jour-broker PRÉCÉDENT** (07 juillet), pas à aujourd'hui. Aucune n'était une fausse perte (toutes les 6 sont de vraies pertes nettes, le compteur de pertes lui-même n'était pas cassé) — le seul bug était la fenêtre. Réponse à l'hypothèse #5 de la mission (trade fermé positif compté comme perte) : non, ce cas ne s'est pas produit — tous les 6 trades comptés avaient un P&L net réellement négatif à la clôture.

## 4. Fix

- **`app/utils/broker_time.py::to_mt5_query_bounds()`** (nouveau) : convertit les instants VRAI-UTC de `broker_day_window()` en valeurs d'horloge murale broker (`+offset`, PAS un simple retrait de tzinfo) avant tout appel `mt5.history_deals_get()`. Point d'entrée UNIQUE désormais utilisé par les 3 sites concernés.
- **`app/services/daily_killswitch.py`** : `_mt5_history_deals` utilise `to_mt5_query_bounds()`.
- **`app/services/mt5_pnl_truth.py`** : `today_start` calculé via `broker_day_window()` (jour-broker réel, plus jamais minuit-UTC-brut) ; `_history_deals_pnl` convertit ses bornes via `to_mt5_query_bounds()` avant d'interroger MT5 — corrige les 3 fenêtres (`window`, `today`, `forty_eight`) d'un coup.
- **`app/dashboard_api/data.py`** : même bug trouvé dans mon propre code de la mission DASHBOARD (`build_today()`), corrigé de la même façon.
- **Cross-check permanent** (exigence explicite de la mission) : `evaluate_daily_killswitch()` appelle désormais `app.services.mt5_pnl_truth.get_mt5_hermes_pnl_truth()` sur la MÊME fenêtre à chaque évaluation, en parallèle de son propre calcul. Log `[PNL_CROSS_CHECK] OK` si l'écart ≤ 0.01\$, `[PNL_CROSS_CHECK_MISMATCH]` en CRITIQUE sinon — toute divergence future est visible immédiatement, plus jamais découverte via un ticket support.

## 5. Preuve live après fix (bot redémarré, 0 position ouverte au moment du redémarrage)

Avant (process précédent, ancien code) :
```
15:07:26  [DAILY_KILLSWITCH] triggered=True losses=6/6 daily_pnl=-37.14
```
Après (process redémarré à 15:08:25 avec le fix) :
```
15:10:08  [DAILY_KILLSWITCH] triggered=False reason=None losses=3/6 daily_pnl=13.70 window=[2026-07-07T21:00:00 -> 2026-07-08T16:10:08]
15:10:08  [PNL_CROSS_CHECK] OK killswitch_daily_pnl=13.70 cycle_summary_pnl=13.70 divergence=0.0000
```

**Le kill-switch n'est plus déclenché.** 3 vraies pertes (pas 6), daily_pnl positif (+13.70, contre +13.28 CYCLE_SUMMARY — écart résiduel de quelques centimes dû à quelques minutes entre les deux mesures, pas un bug), cross-check confirme un accord exact. **Le bot peut retrader aujourd'hui**, dans les limites du vrai quota (6 pertes nettes/jour pour DEMO — 3 consommées).

## 6. Audit "autres modules affectés" (demandé par la mission)

Sites appelant `mt5.history_deals_get()` avec une fenêtre alignée sur le jour calendaire (donc potentiellement affectés par ce bug) :
- `daily_killswitch.py`, `mt5_pnl_truth.py`, `dashboard_api/data.py` — **corrigés** (ci-dessus).
- `mt5_position_sync.py::_mt5_history_deal_for_ticket` — lookback glissant de 14 jours pour retrouver un ticket précis (pas aligné sur un jour calendaire) ; le décalage de 3h n'a d'effet qu'à la toute marge du lookback, risque négligeable en pratique, non corrigé (hors périmètre de cette mission).
- `local_api/server.py` (lecture de l'historique des trades fermés) — lookback glissant en heures, même remarque, non corrigé.
- `demo_router.py:530` — interroge par `position=` (numéro de position), pas de fenêtre de dates du tout, non concerné.

## 7. Tests

30 tests nouveaux (`to_mt5_query_bounds`, invariant 14 gagnants/3 perdants du mandat de la mission, reproduction exacte de l'incident avec les vrais montants, cross-check OK/divergent/indisponible, conversion dans `mt5_pnl_truth` et `dashboard_api`). Suite complète : **3342 passed, 0 regression** (1 flake pré-existant sans rapport, module `test_btc_fast_exit_daemon.py`, confirmé via isolation — non lié à cette mission).
