# FIX_KILLSWITCH_DATE_REPORT — mission/FIX_KILLSWITCH_DATE.md, exécutée le 2026-07-08

## VOLET 1 — bug de date kill-switch

### Verdict : le kill-switch n'a jamais été aveugle en production

Le rapport de bug citait :
```
[DAILY_KILLSWITCH] triggered=False losses=0/6 policy=DEMO window=[2026-05-31T21:00:00+00:00 -> 2026-06-01T12:00:00+00:00]
```

**Cause racine exacte, reproduite empiriquement** : `tests/test_bloc6_killswitch.py::TestRouterIntegration::test_router_blocks_order_when_killswitch_triggered` passe délibérément `now=datetime(2026, 6, 1, 10, 0, ...)` comme fixture pour exercer le chemin d'intégration du router. Ce repo n'avait **aucune isolation entre le log de test et le log de production** — exécuter ce seul test écrit une ligne `[DAILY_KILLSWITCH]` réelle dans `logs/hermes.log`, au moment réel où le test tourne, avec exactement la fenêtre citée dans le rapport de bug. Confirmé : **3199 occurrences** de cette même fenêtre factice déjà accumulées dans `hermes.log` par des runs de test passés, mélangées aux vraies lignes du bot live.

Investigation exhaustive du code AVANT de trouver ceci (`daily_killswitch.py`, `demo_router.py`, `time_engine.py`) : `now_utc = now or datetime.now(timezone.utc)`, fraîche à CHAQUE évaluation, aucune valeur figée/mise en cache trouvée nulle part dans le code de production.

### Ce qui a quand même été durci (défense en profondeur, quel que soit le vrai coupable)

| Fichier | Changement |
|---|---|
| `tests/conftest.py` (nouveau) | **Le vrai fix.** `HERMES_LOG_FILE` redirigé vers un fichier isolé (`tests/__tmp_test_hermes.log`) avant tout import — plus aucune pollution possible, vérifié empiriquement (même test relancé, `hermes.log` inchangé). |
| `app/utils/broker_time.py` (nouveau) | Source de temps broker UNIQUE et centralisée (`broker_now_utc()`, `broker_day_window()`, `is_window_stale()`). Garde-fou anti-régression : alerte `[BROKER_TIME_GUARD]` CRITIQUE si la fenêtre calculée dérive de plus de 48h par rapport à l'horloge murale **indépendante** — pas par rapport à elle-même (un premier jet de ce garde-fou comparait la fenêtre à la valeur qui a servi à la calculer, donc toujours cohérent par construction et **incapable de jamais se déclencher** ; trouvé par son propre test, corrigé). |
| `app/services/daily_killswitch.py` | Migré vers la source centralisée. Log de contrôle renforcé : `now_utc_detected=` affiché explicitement à côté de la fenêtre calculée. |
| `app/dashboard_api/data.py` | Duplication `_broker_day_window`/`_now_utc` supprimée, délègue à la source centralisée. |
| `app/services/time_engine.py` | `_broker_time_estimate()` durci : un tick/bougie MT5 périmé de plus de 6h (ex. reconnexion terminal renvoyant un objet caché ancien) est désormais rejeté au profit de l'horloge murale — trouvé pendant l'audit "autres modules affectés" demandé par la mission. |
| `app/mt5/demo_router.py`, `app/services/protected_calendar.py` | Audités (weekend/blackout) : déjà corrects, même motif `now or datetime.now(timezone.utc)` fraîche. Aucun changement nécessaire. |

### Preuve live que le kill-switch voit maintenant le bon jour

Bot redémarré à 12:47 (aucune position ouverte au moment du redémarrage, vérifié avant) pour charger le nouveau code. Log réel capturé à 12:49:43 :
```
[DAILY_KILLSWITCH] now_utc_detected=2026-07-08T11:49:43.758772+00:00 triggered=True reason=DAILY_KILLSWITCH_MAX_LOSSES losses=6/6 daily_pnl=-37.14 dd_pct=0.409 policy=DEMO window=[2026-07-07T21:00:00+00:00 -> 2026-07-08T13:49:43.758772+00:00]
```
`now_utc_detected` = heure murale réelle, fenêtre ancrée sur juillet 7→8 (aujourd'hui), kill-switch correctement déclenché (6/6 pertes du jour, déjà actif depuis ce matin — le kill-switch fonctionnait déjà avant cette mission, ce n'était jamais "aveugle").

### Ce qui n'a PAS été touché

`logs/hermes.log` existant (64 MB, ~3199 lignes de pollution historique) n'a pas été tronqué — ce sont des données de production, pas à un agent de les détruire unilatéralement. La rotation automatique déjà en place (10 MB × 5 fichiers) le nettoiera naturellement avec le temps. Toute analyse future de `hermes.log` antérieure à ce commit doit garder en tête que des lignes `[DAILY_KILLSWITCH]` avec des dates de fantaisie (mai/juin, fixtures de test) peuvent y figurer.

### Tests

50 tests nouveaux/corrigés dédiés à cette mission (`test_broker_time.py`, `test_time_engine_stale_tick.py`, extension de `test_bloc6_killswitch.py`). Suite complète : **3319 passed, 0 regression**.

---

## VOLET 2 — audit comportement BTC (lecture seule, décision réservée SIMO)

### Qui fermait les trades BTC ?

**`HERMES_RESCUE_EXIT`** (`app/mt5/demo_router.py::_rescue_close`, mécanisme "Smart Rescue Quick Exit" historique, BTC-only) — **PAS Exit V2**. 7 des 9 clôtures BTC du jour portent ce commentaire, toutes entre le 07/07 23:04 et le 08/07 05:32 (heure broker), micro-gains ($0.10 à $0.71) correspondant exactement au profil décrit par SIMO.

### C'est déjà corrigé

Commit `c8f6d1ef` (mission1 GRAND_PLAN, "Exit V2 autorité unique", 2026-07-08 02:29:54 UTC) a ajouté un garde `if _exit_v2_symbol(symbol): skip` à **trois** endroits (`_rescue_close`/Smart Rescue ligne 721, `_rescue_close`/Market Danger ligne 814, et le routage positif principal vers Exit V2 ligne 624) — `_exit_v2_symbol` est un alias direct de `is_exit_v2_symbol()` qui couvre GOLD# **et** BTCUSD# (décision SIMO du 2026-07-08). Le commit a atterri dans le repo à 02:29:54 UTC ; le process bot alors en cours tournait avec l'ancien code jusqu'à son redémarrage suivant. Dernière clôture RESCUE : 05:32:13 broker (≈02:32 UTC), **quelques minutes après le commit**. Depuis (10+ heures de trading live à l'heure de ce rapport), **zéro** clôture RESCUE sur BTC — vérifié directement dans l'historique MT5. 50 tests existants (`test_smart_rescue_quick_exit.py`) verrouillent déjà ce garde.

Les 6 trades que SIMO a observés sont le dernier sursaut de l'ancien code, pas un problème actif.

### max_tp_usd (capper argent) sur BTC ?

Non — `MAX_MONEY_TP_ENABLED=false` dans `.env` actuellement : le capper est désactivé globalement, pour GOLD comme pour BTC. Pas un facteur.

### Exit V2 est-il à la bonne échelle pour BTC ? (question ouverte, chiffrée)

Oui, actif sur BTC depuis le fix (mêmes paramètres que GOLD : BE +$2.00, trailing gap $1.20) — mais **ces seuils sont des dollars fixes, pas adaptés au prix**. Calcul exact sur les spécifications de contrat réelles (0.01 lot) :

| Symbole | Prix actuel | Mouvement de prix pour +$2 | En % du prix |
|---|---|---|---|
| GOLD# | 4063.27 | $2.00 | **0.049%** |
| BTCUSD# | 62336.50 | $200.00 | **0.321%** |

Le seuil de $2 demande un mouvement **~6.5× plus grand en pourcentage** sur BTC que sur GOLD pour armer la protection (BE floor / trailing). Concrètement : une position BTC reste "nue" (sans protection Exit V2 armée) plus longtemps, en termes relatifs, qu'une position GOLD équivalente.

**Proposition pour SIMO (non appliquée)** :
1. **Adapter Exit V2 pour BTC** : seuils en % du prix (ou en multiple d'ATR) au lieu de dollars fixes — le plus propre, mais touche `ExitV2Config`/`evaluate_exit_v2` (actuellement symbol-agnostic par construction).
2. **Resserrer/ajuster les seuils $ spécifiques à BTC** sans changer l'architecture (garder $ mais différencier GOLD vs BTC dans la config).
3. **Ne rien changer maintenant** : le fix du 08/07 est très récent (10h de recul), seulement 2 trades BTC légitimes depuis (les 2 SL hits post-fix) — échantillon trop petit pour recalibrer sereinement. Laisser tourner 1-2 semaines sous le régime uniforme actuel, mesurer l'edge réel via le dataset (déjà tagué `symbol=BTCUSD#`), puis décider avec des chiffres.

Recommandation de l'agent (à confirmer par SIMO) : **option 3**, pour la même raison que l'option "ne pas modifier le trading BTC" du mandat de cette mission — mesurer avant de recalibrer.

### Fréquence — sur-trading ?

Le "6 trades/heure" observé par SIMO était la signature du bug Smart Rescue (seuil `rescue_min_profit_usd=0.08` très bas, fermeture quasi-systématique dès un profit minime). **Depuis le fix, la fréquence BTC est quasi nulle** : 2 clôtures en ~10h (les 2 SL hits), contre 7 clôtures RESCUE dans la seule fenêtre 23h04-05h32. Pas de sur-trading actif à surveiller aujourd'hui — à re-vérifier une fois plus de volume accumulé sous le régime Exit V2.

### Livrable

- Fermeture : Smart Rescue (residu), **déjà corrigé** par une mission antérieure du jour, vérifié 10h+ sans récidive.
- Échelle : seuils Exit V2 non adaptés au prix BTC (6.5× plus de mouvement relatif requis), chiffré ci-dessus — proposition présentée, **aucune modification appliquée**.
- TP capper : désactivé globalement, non pertinent actuellement.
- Fréquence : normale depuis le fix ; l'anomalie observée par SIMO ne se reproduit plus.
