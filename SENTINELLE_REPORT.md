# SENTINELLE HERMES — RAPPORT

**Date :** 2026-07-15 · **Safepoint :** `safepoint-avant-sentinelle`
**Nature :** veilleur 24/24 LECTURE SEULE, script Python déterministe (zéro LLM).
**Suite complète :** **3542 passed, 2 skipped** (dont 42 tests sentinelle).

---

## CE QUE C'EST

Un script indépendant qui, toutes les 10 min, lit l'état de HERMES (dataset, logs, historique MT5) et envoie **une** alerte Telegram groupée uniquement quand une règle **certaine et chiffrée** casse. C'est l'antidote au « tout est vert » qui avait caché 61 outcomes perdus : il pose les bonnes questions tout seul, en continu, même quand SIMO dort.

**Il surveille, il n'agit jamais.** Aucun `order_send`, aucune position touchée, aucun fichier de prod modifié. Il n'écrit que dans `sentinelle/`.

---

## LES 13 RÈGLES IMPLÉMENTÉES (que des faits certains)

| # | Règle | Sévérité | Source de vérité |
|---|---|---|---|
| A1 | RR **au fill** < 1,5 | 🔴 | dataset (`exec_quality.fill_price` vs sl/tp), tolérance 0,01 |
| A2 | SL/TP du mauvais côté | 🔴 | dataset (géométrie entry/sl/tp) |
| A3 | **Exécuté malgré `fallback_decision=BLOCK`** | 🔴 **prioritaire** | dataset — la faille exacte de la perte v1 |
| A4 | Position nue (sans SL ou TP) | 🔴 | MT5 `positions_get` (magic 909002) |
| A5 | Marqueur pré-P0TER (`collection_version≠2` ou `core_fix_level` absent) | 🟠 | dataset |
| B1 | Deal fermé sans outcome > 15 min | 🔴 | MT5 deals ↔ dataset outcomes |
| B2 | Couverture outcomes 24h < 90 % | 🟠 | dataset (min. 5 trades, anti-bruit) |
| B3 | Dataset figé > 2h en session | 🟠 | dataset mtime + session + kill-switch |
| B4 | Compteur de pertes bloqué à 0 malgré des pertes | 🔴 | MT5 deals vs log `[DAILY_KILLSWITCH]` |
| C1 | Position orpheline (absente du dataset) | 🔴 | MT5 `positions_get` ↔ tickets connus |
| C2 | Écart P&L dataset vs deal MT5 > 0,05 $ | 🔴 | dataset `pnl_reconciled` vs deals MT5 |
| D1 | `hermes.log` muet > 20 min | 🟠 | mtime du log |

Chaque règle touchant MT5/prix se calcule sur des **deals réels** (P&L = profit+commission+swap, fenêtre jour-broker UTC+3, même logique que `daily_killswitch`). En cas de donnée manquante ou de MT5 indisponible : **on n'alerte pas** (fail-open — « mieux vaut rater une subtilité que crier au loup »).

---

## ARCHITECTURE

- **`sentinelle/rules.py`** — les règles PURES : chaque `check_*` prend des faits déjà rassemblés + un `now` explicite, renvoie des `Anomaly`. Aucune I/O, aucun MT5, aucune horloge interne → testable en isolation.
- **`sentinelle/sentinelle.py`** — l'orchestrateur : rassemble les faits, applique les règles, gère l'anti-bruit et Telegram. **Indépendant de `app`** : n'importe rien du code de trading (importer `app.utils.broker_time` aurait tiré `app.logger`, qui attache un handler sur `logs/hermes.log` de prod — les 2 helpers broker-time sont donc réimplémentés localement, math identique).
- **`sentinelle/state.json`** — anti-bruit (offset dataset, tickets connus, fenêtre roulante 48h, signatures d'alertes, heartbeat). Runtime, gitignoré.
- **`sentinelle/sentinelle.log`** — journal de la sentinelle. Runtime, gitignoré.

**Lecture efficace :** le dataset fait ~156 Mo. La sentinelle lit uniquement les **octets ajoutés depuis la dernière passe** (offset persisté), et maintient une fenêtre roulante 48h en état — coût O(nouvelles lignes) par passe, pas O(fichier). Détection de rotation/troncature incluse. Premier run complet : 3,7 s.

---

## ANTI-BRUIT (un gardien qui crie trop est ignoré)

- **Prime au 1er run** : le premier lancement baseline l'historique connu-sain **sans envoyer aucune alerte** (les A-règles ne voient que les trades postérieurs). Un vrai problème actuel ressurgit à la passe suivante.
- **Cooldown 6h par signature** (`règle:sujet`, ex. `A3:374512428`) : une même anomalie n'est pas re-alertée à chaque passe.
- **Groupage** : plusieurs anomalies dans une passe → **un seul** message Telegram.
- **Purge** : une signature qui disparaît est oubliée → ré-alertable si elle réapparaît.
- **Heartbeat quotidien** (désactivable via `SENTINELLE_HEARTBEAT=0`) : `✅ Sentinelle : RAS, X trades v2 24h, outcomes Y%` — 1×/jour, seulement s'il n'y a pas d'alerte fraîche.
- **`SENTINELLE_DRY_RUN=1`** : vérifie l'installation sans rien envoyer (logue ce qui partirait).

---

## FORMAT TELEGRAM

Préfixe distinct des alertes système : **`🔴 ALERTE SENTINELLE`** (critique) / **`🟠 SENTINELLE`** (non-critique). Chaque ligne est autoportante (QUOI, OÙ, le CHIFFRE, l'heure). Réutilise le bot Telegram existant (token+chat_id lus en lecture seule dans `watchdog/.env`, **jamais loggés, jamais committés**), avec la même validation TLS truststore que le watchdog.

---

## PREUVE : LE 1er RUN N'ALERTE PAS À TORT

- **Passe 1 (prime)** : `[BASELINE] état initial sain, aucune anomalie`. 108 tickets connus, offset posé. 3,7 s. Bot (PID 4892) **survit** à l'accès MT5 concurrent (le watchdog établissait déjà le précédent d'un 2e lecteur MT5 ; la sentinelle est le 3e, toutes les 10 min).
- **Passe 2 (évaluation réelle)** : `[RAS] 0 anomalie, executed_24h=7 outcomes24h=7` → **couverture 100 %**, aucune fausse alerte sur l'état courant sain.
- **Run réel déclenché par le planificateur** (`Start-ScheduledTask`, LastResult=**0**) : `[RAS] 0 anomalie` + heartbeat `✅ Sentinelle : RAS, 6 trades v2 24h, outcomes 100%` **envoyé** — pipeline complet validé de bout en bout (scheduler → lecture MT5 → évaluation → Telegram), aucune erreur MT5/TLS.

---

## TÂCHE PLANIFIÉE

`HERMES_SENTINELLE` créée : **RunAs=Admin, LogonType=Interactive, RunLevel=Highest**, déclencheur **toutes les 10 min** + déclencheur **à l'ouverture de session** (repart au reboot via l'auto-logon déjà en place). Limite d'exécution 5 min, redémarrage sur échec.

**Écart assumé avec la lettre de la mission** : la mission demandait « Run whether user is logged on or not ». Or **toutes** les tâches HERMES (dont le watchdog, qui lit MT5 exactement comme la sentinelle) tournent en **Interactive** — c'est une contrainte technique : `mt5.initialize()` ne peut atteindre le terminal MT5 que depuis la session interactive de l'utilisateur (une tâche en session 0 échouerait à lire MT5). L'auto-logon rend cette tâche interactive permanente (24/24). C'est le pattern éprouvé du watchdog ; le copier garantit l'accès MT5.

---

## VÉRIFICATION FINALE

- **git diff borné** : uniquement `sentinelle/` (code) + `tests/test_sentinelle.py` + `SENTINELLE_REPORT.md` + `.gitignore` (runtime sentinelle) + la tâche planifiée. **Aucun fichier de code de trading touché** (`app/`, `config.py` : intacts).
- **Lecture seule prouvée** : la sentinelle n'a écrit que dans `sentinelle/state.json` et `sentinelle/sentinelle.log` (vérifié par `git status` : les seuls fichiers de prod « modifiés » sont les artefacts runtime que le **bot** écrit en continu — `demo_pilot_events.jsonl`, `app/data/*` —, jamais la sentinelle).
- **Suite complète** : 3542 passed, 2 skipped. 42 tests sentinelle (chaque règle A/B/C/D : un cas qui DOIT alerter + un contrôle négatif qui NE DOIT PAS, sur données synthétiques).
- **Bot non perturbé** : PID 4892 vivant tout du long.

---

## APRÈS

La sentinelle complète le kill-switch (qui arrête) et le watchdog (qui surveille le process) : elle vérifie que les **décisions** et la **collecte** sont saines — ce qu'aucun autre composant ne faisait. Calibrée serré. Elle tournera désormais toutes les 10 min ; SIMO recevra un `✅ RAS` par jour et une `🔴 ALERTE SENTINELLE` uniquement quand un fait certain casse.
