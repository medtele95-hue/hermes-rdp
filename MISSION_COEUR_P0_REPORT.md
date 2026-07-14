# MISSION_COEUR_P0 — RAPPORT

**Date :** 2026-07-14
**Safepoint :** `safepoint-avant-coeur-p0` → `3655eb66`
**HEAD :** `25f2454c` — 9 commits atomiques
**Suite :** **3447 passed, 2 skipped** (base 3393 + 54 tests neufs)

---

## Résumé

Les 7 corrections sont livrées, chacune avec son commit et ses tests. Le bot tourne sur le cœur réparé.

**Le chiffre qui résume la mission** — replay à sec du dataset v1 contre le nouveau cœur :

| | Trades | P&L | Winrate |
|---|---|---|---|
| **Bloqués par le cœur réparé** | **28 / 104 (27 %)** | **−39,08 USD** | 70,4 % |
| Passeraient toujours | 76 | **−0,24 USD** | 64,0 % |
| **Total v1 réel** | 104 | **−39,32 USD** | |

**Les 28 trades bloqués le sont tous pour le même motif : `TOP_DOWN_READER_BLOCK`.** Le cœur réparé aurait évité **39,08 USD de pertes** — le P&L v1 serait passé de **−39,32 à −0,24 USD**.

Autrement dit : **les trades que le système avait raison de bloquer représentaient la totalité de la perte.** Le jugement était bon ; c'est l'override qui l'avait désarmé. C'est désormais corrigé.

**Attendu en v2 : environ −27 % de volume de trades.** C'est le prix de la rigueur, et c'est souhaitable.

---

## Correction par correction

### P0-A — Un override ne peut plus jamais effacer un blocage dur (`710ee333`)

**Le bug.** Les 4 chemins d'override (fallback adaptatif, micro-discovery, exploration, `old_btc_mode`) remettaient `reason = None` **sans regarder ce qui était bloqué**. Un seuil de confluence trop bas et un spread hors limite étaient traités identiquement.

**Le fix — défaut-dur.** `SOFT_OVERRIDABLE_BLOCK_REASONS` est une liste **fermée** : seuls les seuils de *qualité de setup* (confluence, SMC/MTFA, M15/M1, grade), le top-down **sans verdict** et les fenêtres horaires sont assouplissables. **Tout le reste est dur** — risque, exposition, structure, compte, marché, SL/TP, RR, spread, cooldown, safety guard, et `TOP_DOWN_READER_BLOCK`. Un motif ajouté demain sera **dur par défaut**.

**La distinction qui compte.** `_top_down_missing_reason` (`:2433`) retourne `None` quand le lecteur dit AVOID — l'AVOID est traité à part et produit `TOP_DOWN_READER_BLOCK`. C'est un **verdict de refus**, pas une donnée absente. `MISSING`/`FAIL`/`WAIT` sont, eux, exactement ce que le fallback existe pour remplacer. J'avais d'abord mis les quatre dans le même sac ; **20 tests existants m'ont attrapé** et m'ont fait affiner la frontière.

**Cinq points de sortie verrouillés, pas deux.** Le *remplacement* de `reason` l'est aussi : un motif dur remplacé par un motif micro-discovery soft serait redevenu effaçable au bloc suivant. J'aurais cru boucher un trou en en creusant un autre.

**Tests (8).** T1 : `TOP_DOWN_READER_BLOCK` + micro-discovery ALLOW ⇒ bloqué. Plus un **contrôle négatif obligatoire** : un motif soft doit rester assouplissable — sans lui, bloquer 100 % des ordres aurait fait passer tous les autres tests.

### P0-B — `route_to_demo` devient effectif (`2e3de105`)

`_should_route_to_demo` (`main.py`) ne lisait ni `route_to_demo` ni `decision`. Or **toute** la chaîne de garde amont exprime son refus dans ces deux champs sans toucher à `strategy`/`signal` : rejet du balanced_selector, `OLD_BTC_ENTRY_GATE=BLOCK`, `MT5_POSITION_READ_FAILED`, gate de confluence. Un `[ROUTER_HANDOFF] decision=BLOCK` était loggué… et l'ordre partait. Aucun filet en aval (`grep route_to_demo` dans `demo_router.py` : 0 occurrence).

**Tests (5)**, dont le scénario exact du rejet balanced_selector, et un contrôle négatif.

### P0-C — La confluence est fail-closed (`3b020306`)

`except Exception: pass` laissait `_conf = {}`. Or **tout** le FINAL CONFLUENCE GATE était conditionné par `bool(_conf)` : une exception dans le moteur **désarmait le gate entier**, en silence. Un setup grade D / score 0 routait sans contrôle.

Désormais : `_conf_engine_failed` force le blocage, **sans condition** — ni `research_allow` ni le bypass `old_btc` ne peuvent lever une *panne* : on ne sait simplement pas si le setup est bon. `log.critical` + alerte Telegram (thread daemon, cooldown par clé — zéro blocage de la boucle).

**Tests (4).**

### P0-D — Les deux stops de perte sont ressuscités (`543bcdb3`)

**Ils étaient structurellement morts.** `_stats()` dérivait `daily_loss_pct` et `consecutive_losses` d'événements `DEMO_CLOSE` **qu'aucun code du dépôt n'écrit** (vérifié sur 26 journaux : `DEMO_CLOSE = 0`). Dans le dataset : les deux compteurs valaient `0.0` et `0` sur **8 465 décisions sur 8 465**. Et `balance = 10000.0` était **codée en dur** (réelle ≈ 9 090).

**Le fix.** `risk_counters_from_mt5_deals()` : même source de vérité que le kill-switch — les **deals MT5**, fenêtre broker-day, bornes converties par `to_mt5_query_bounds`. Dénominateur = **equity réelle**. Rien en mémoire : l'historique est relu à chaque évaluation, donc ces compteurs survivent à un redémarrage **et** à une rotation de journal. **Fail-closed** : historique illisible ⇒ `RISK_COUNTERS_UNREADABLE` (motif dur).

**Preuve en production, après redémarrage :**
```
risk_counters_source     = MT5_HISTORY_DEALS
daily_demo_loss_pct      = 0.08077108     ← une VRAIE valeur (0.0 sur 8465/8465 avant)
risk_counters_equity     = 9075.03        ← l'equity RÉELLE (10000.0 en dur avant)
risk_counters_daily_pnl  = -7.33
```

**Le constat le plus parlant de la mission.** Deux tests existants — `test_daily_loss_stop_blocks_order` et `test_consecutive_losses_stop_blocks_order` — **écrivaient eux-mêmes des `DEMO_CLOSE`** dans un fichier d'événements. Ils passaient donc **au vert alors que la protection était morte en production** : ils fabriquaient une donnée que la réalité ne produit jamais. C'est l'illustration exacte de *« une suite verte n'est pas une preuve de sûreté »*.

**Effet de bord assumé :** avec 6 pertes réelles, le stop « 3 pertes consécutives » se déclenche maintenant **avant** le kill-switch (3 < 6). Le bot s'arrête plus tôt. C'est voulu.

**Tests (12)**, dont T4 : **chaque stop est vu se déclencher** sur des deals franchissant le seuil, plus des contrôles négatifs (un jour gagnant ne déclenche rien ; un gain récent casse la série).

### P0-E — MT5 muet ne lève plus aucun cap (`2cc1a51e`)

Trois sites faisaient `mt5.positions_get() or []` : `_demo_positions`, `_current_mt5_position_counts`, et le choke-point. **Quand MT5 est muet, tous les compteurs tombent à 0 en même temps** et tous les caps se lèvent ensemble.

Le commentaire du choke-point **assumait** ce fail-open — *« les gates amont portent déjà ce cap »*. **C'est faux** : les gates amont dérivent des mêmes `or []`. Et le `except (TypeError, ValueError, AttributeError)` n'attrapait rien : `positions_get()` renvoyant `None` ne lève pas, il passe par le `if positions:` falsy. Le fail-open était **silencieux**.

**Chemin de double ordre prouvé** : ordre N → `10012 TIMEOUT` → enregistré échec → cycle suivant, `positions_get` renvoie encore `None` (c'est la **même** dégradation MT5 qui a causé le timeout) → le cap saute → un **second ordre** part. Les deux événements sont corrélés positivement, pas indépendants.

Désormais : `MT5_UNAVAILABLE`, blocage dur, **double barrière** (gate amont + choke-point).

> **Tension avec l'invariant 1, que je signale plutôt que de l'enterrer.** La mission déclare le choke-point *intouchable*, mais P0-E demande explicitement de corriger `demo_router.py:287-299`, **qui est dedans**. J'ai tranché dans le sens de l'intention : je n'ai touché **que** la gestion du `None` de `positions_get`, ce qui **renforce** le garde. `SYMBOL_ALLOWLIST`, `LOT_HARD_CAP=0.01`, `MAGIC_HARD`, `NAKED_ORDER_BLOCKED` sont **exactement** ce qu'ils étaient — un test le prouve.

**Tests (6)**, dont un contrôle négatif : un compte **réellement** vide doit continuer à trader.

### P0-F — La rotation ne remet plus les compteurs à zéro (`7b91c042`)

Deux rotations coexistaient, et la mauvaise gagnait : **écriture** à 20 Mo sous verrou (correcte), **lecture** à 10 Mo qui renommait le fichier **et renvoyait `[]`, sans aucun verrou**. 10 < 20 : la rotation en lecture partait donc **toujours la première**. Le cycle où elle se produisait, le bot croyait démarrer une journée vierge.

**Preuves empiriques** : une dizaine de `.bak` de 10-11 Mo **tous datés du 2026-06-16** (01:33 → 07:27) — au moins **10 rotations en lecture cette seule nuit-là**. Et dans le dataset, `daily_demo_trades_total` ne dépasse **jamais 4**, alors que le bot a réellement exécuté **26 trades** en une journée.

**Aggravant** : `_load_events()` est aussi appelé par le **thread heartbeat toutes les 5 s**. La rotation pouvait donc être déclenchée par le **thread du dashboard** et effacer les compteurs du **thread de trading**.

Désormais : lecture pure. **Un lecteur ne mute pas ce qu'il lit.** Double barrière : depuis P0-D, les compteurs de perte ne passent plus du tout par ce fichier.

**Un test existant a dû être inversé** : `test_load_events_large_file_rotates_and_returns_empty` — **son nom même décrivait le bug**. Il verrouillait le comportement fautif.

**Tests (4)**, dont un contrôle négatif : le chemin d'écriture rote toujours.

### P0-G — Les 3 trous du tracker d'outcomes (`f42827ab`)

> **Ces trois trous sont dans un correctif que j'ai livré ce matin** (`746e7bdb`). L'audit les a pris en défaut. Je les traite au même niveau d'exigence que le reste — un correctif qui protège son auteur ne vaut rien.

1. **MT5 muet ⇒ fail-closed.** `close_info_from_deals → None` laissait `known_open=False` et le code **retombait dans la fermeture par touche de prix** : une ligne outcome **fausse** était écrite et le ticket sortait du suivi — le vrai label n'aurait alors **jamais** été écrit. Mon propre commentaire affirmait *« seuls les deals ferment un trade réel »* ; le code le contredisait exactement quand MT5 était muet.

2. **Fenêtre de doublon au redémarrage.** L'ordre était `append → pop → _save_state`. Un crash entre les deux laissait le ticket dans l'état → restauré au boot → refermé → **seconde ligne outcome**. Un P&L double-compté corrompt silencieusement **tous** les agrégats. Désormais : `pop` + `_save_state` **avant** l'append, et `record_outcome` **idempotent par ticket** (clé de dédup persistée). Le mode de défaillance résiduel s'inverse volontairement : une ligne **manquante** (détectable, backfillable) plutôt qu'un **doublon** (qui corrompt en silence).

3. **Labelling suspendu à un flag de sortie.** `tracker.update()` vivait dans `process_quick_exits`, **après ses trois retours anticipés**. Poser `QUICK_EXIT_ENABLED=false` aurait supprimé **tout** labelling du dataset, pour toujours, **sans un seul log**. Il ne tenait que par accident de configuration. Désormais : `update_outcome_tracker()`, appelée à chaque cycle depuis `main.py`.

**Tests (7).** Deux tests existants corrigés, dont un qui verrouillait le trou n°1.

### T9 — Les voyants de sécurité sont branchés (`e7fe1a25`)

`"demo_only": True` et `"allow_live_trading": False` étaient des **littéraux** à **sept** endroits — y compris dans `/local-api/audit-safety`, **l'endpoint dont le seul rôle est de prouver que le live est bloqué**. Si `ALLOW_LIVE_TRADING` passait à `true`, ils auraient continué d'affirmer le contraire.

**Tests (3)**, dont *le test qui manquait* : on active le live dans la config et on vérifie que le voyant **sait le signaler**.

### Collecte v2 (`25f2454c`)

> **La mission partait d'une prémisse fausse, et je la corrige plutôt que de l'appliquer.** Elle demandait de « rendre `core_version=2` explicite » pour marquer la bascule. Or `core_version` **est déjà explicitement à 2** depuis COEUR_V2 (08/07) : mesure sur le dataset v1 → **10 733 lignes le portent**, 1 683 non. Il ne peut donc **pas** séparer l'archive v1 de la collecte v2.

Champ distinct : `COLLECTION_VERSION = 2`, posé sur **chaque** ligne écrite. Le dataset v1 n'est pas touché.

**Bascule annoncée en production :**
```
[COLLECTE_V2] BASCULE — première ligne du cœur réparé écrite à 2026-07-14T00:30:11.722511+00:00
```

---

## Vérification finale

### Zones intouchables — **preuve explicite**

| | |
|---|---|
| `app/services/daily_killswitch.py` | **DIFF VIDE** |
| `SYMBOL_ALLOWLIST` | **intact** |
| `LOT_HARD_CAP` (0.01) | **intact** |
| `MAGIC_HARD` (909002) | **intact** |
| `NAKED_ORDER_BLOCKED` / `LOT_INVALID_BLOCKED` / `SYMBOL_BLOCKED` | **intacts** |

*(Vérifié en inspectant les lignes réellement changées du diff, pas par une empreinte fragile.)*

### Périmètre du diff

```
app/mt5/demo_router.py · app/main.py · app/services/decision_dataset.py
app/services/mt5_position_sync.py · app/local_api/server.py
+ tests (test_coeur_p0.py neuf, et 6 fichiers de tests existants ajustés)
```

### Tests

**3447 passed, 2 skipped.** 54 tests neufs. Chaque correction porte son **contrôle négatif** — sans lui, bloquer 100 % des ordres ferait passer tous les tests principaux et le correctif serait un faux succès.

**Un flake pré-existant a été élucidé** (`98f05ebe`) : `[FINAL_GATE]` passe par `log_event_throttled`, dont l'état est au niveau module et supprime une ré-émission identique dans les 60 s. Vérifié **sur le safepoint** avant de conclure : ce n'était pas une régression. *(Mon hypothèse précédente — une rotation du log de test — était fausse ; je la corrige ici.)*

### Bot redémarré sur le cœur réparé

| Contrôle | Résultat |
|---|---|
| PID | 10912 |
| Erreurs / Traceback | **0** |
| Cycles | normaux |
| **Tracker persistant** | **2 positions ouvertes RESTAURÉES** au boot — première preuve en production |
| Compteurs de risque | `MT5_HISTORY_DEALS`, equity réelle 9 075,03 |
| Bascule v2 | annoncée, horodatée |

---

## Ce qu'il faut savoir pour la suite

1. **Le dataset v1 (104 trades) est une archive.** Sa distribution ne reflète pas les règles du système — 100 % des trades passaient par un override. **Ne jamais mélanger v1 et v2 pour une calibration.** Le marqueur `collection_version` les sépare.

2. **Attendez-vous à ~27 % de trades en moins.** C'est le replay qui le dit, et c'est le but.

3. **Le point 4 des conditions REAL est désormais tenu** : les stops de perte ne sont plus morts — ils lisent les deals MT5 et affichent des valeurs réelles. Mais la condition de blocage absolue de l'audit demandait de **voir le gate se déclencher**. Les tests le prouvent en laboratoire ; **il faudra le voir en production** (une journée à 3 pertes consécutives) avant de considérer le filet comme éprouvé.

4. **Restent ouverts (P1/P2, hors périmètre de cette mission)** : les retcodes MT5 non traités (10010/10012 — un ordre accepté peut encore être noté échec), le daemon 250 ms qui contourne Exit V2 sur BTC, les conversions horaires +3 h dans `mt5_position_sync`, l'absence de verrou MT5 entre 4 threads, `GOLD_RANGE_BREAKOUT` morte, `ORDER_FLOW_EXECUTION_AGENT` sans gate.

**Verdict de l'audit — `CŒUR DANGEREUX POUR LE RÉEL` — reste valable** : cette mission a réparé la couche de décision, pas la couche d'exécution MT5. Le cœur est désormais **strict et cohérent avec ce qu'il annonce**. Il n'est pas encore prêt pour du capital réel.
