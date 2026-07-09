# DATASET_HYGIENE — audit contamination self-test/health-check

**État : PROPRE. Aucune action nécessaire. Rien modifié.**

## Étape 1 — Audit (lecture seule)

**Existe-t-il déjà un flag distinguant un cycle RÉEL d'un cycle SYNTHÉTIQUE ?** Non — `app/services/decision_dataset.py::build_decision_row()`/`record_decision()` aplatissent sans condition tout événement reçu, sans notion de provenance. Ce n'était en réalité pas nécessaire (voir plus bas).

**Le writer est-il atteignable par autre chose que le cycle réel ?** Recherche exhaustive de tous les appelants de `record_decision`/`record_outcome` dans `app/` :

- `record_decision()` : appelé à **exactement 3 endroits**, tous dans `DemoRouter.process_decision()` (`app/mt5/demo_router.py:424,451,461`) — le point d'entrée unique du cycle de décision réel.
- `record_outcome()` : appelé à **1 endroit**, en interne par `OutcomeTracker.update()` (`app/services/decision_dataset.py:317`), lui-même invoqué uniquement depuis `DemoRouter.process_quick_exits()`.
- **Aucun autre fichier, module, script d'outillage ou routine de diagnostic n'appelle ces méthodes.**

Le self-test/health-check périodique identifié dans le rapport 24h (`[GOLD_CANDIDATE] entry=100.0 sl=98.0/102.0 tp=104.0/96.0`, ticket=701/702/555, `symbol=EURUSD`, `bid=None ask=None`) exerce des fonctions de gate/stratégie individuelles pour vérifier qu'elles ne plantent pas — il ne passe jamais par `DemoRouter.process_decision()`/`process_quick_exits()`, donc structurellement **il ne peut pas atteindre le writer**, avec ou sans flag.

**Combien de lignes portent la signature synthétique ?**

Trois vérifications indépendantes, sur l'intégralité de l'historique du dataset (3395 lignes, `2026-07-07T02:15:43` → `2026-07-09T04:46:21`, pas seulement la fenêtre du rapport 24h) :

1. **Marqueurs synthétiques exacts** (`entry` ∈ {100.0, 98.0, 102.0, 104.0, 96.0} ou `ticket` ∈ {701, 702, 555}, les valeurs confirmées du self-test dans les logs bruts) : **0 ligne sur 3395 (0.0%)**.
2. **Recoupement temporel** : les 1326 secondes de rafale self-test identifiées dans `logs/hermes.log` (même heuristique que le rapport 24h : `bid=None ask=None` ou `symbol=EURUSD`) recoupées avec l'horodatage de chaque ligne du dataset (tolérance ±1s) : **46 lignes (1.36%) partagent une seconde avec une rafale self-test ailleurs dans le log** — mais toutes ont `ticket=None` (cohérent avec une vraie décision REFUSÉE réelle) et aucune n'a une valeur `entry` synthétique. Sur un flux réel de ~50 refus/heure, une coïncidence de seconde avec une rafale self-test ponctuelle ailleurs dans le log est statistiquement attendue sans lien causal — vérifié explicitement, pas supposé.
3. **`symbol=EURUSD` isolé** (556 lignes sur l'historique complet) : prix d'entrée réalistes (ex. 1.14319), incompatibles avec le marqueur synthétique `entry=100.0` — cohérent avec de vraies décisions historiques sur EURUSD, d'avant le verrouillage de l'allowlist GOLD#+BTCUSD#, pas du bruit self-test.

## Étape 2 — Décision

**PROPRE.** Aucun cycle synthétique n'atteint `decision_dataset.jsonl`, confirmé par la structure du code (writer non atteignable hors cycle réel) ET empiriquement (0 ligne sur 3395 avec un marqueur synthétique exact). **Aucun fix appliqué.** `app/services/decision_dataset.py` : intact, `git diff` vide.

## Fiabilité du dataset

Le dataset est fiable **depuis sa première ligne exploitable, `2026-07-07T02:15:43 UTC`** — aucune contamination trouvée à aucun moment de son historique, pas seulement à partir d'aujourd'hui.
