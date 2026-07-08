# WATCHDOG_REPORT — Mission 3 (GRAND_PLAN)

## Architecture

`watchdog/hermes_watchdog.py` est un **process Python indépendant** du bot :
il importe `SYMBOL_ALLOWLIST`, `LOT_HARD_CAP`, `MAGIC_HARD` depuis
`app/mt5/demo_router.py` (source de vérité, best-effort — fail-safe sur des
valeurs figées si l'import casse), ouvre sa **propre** connexion MT5 en
lecture seule (`positions_get`, `history_deals_get`, `account_info`), et ne
touche jamais aux fichiers du bot sauf via lecture. Boucle toutes les 60 s.
Aucun `order_send` hors MODE URGENCE (prouvé par test, voir plus bas).

Lancement : `watchdog/start_watchdog.ps1` (boucle infinie, relance le
process Python 30 s après tout arrêt/crash) + tâche planifiée Windows
**`HERMES_WATCHDOG`** (déclencheur : démarrage machine, `RestartCount=999`,
`RestartInterval=1 min`, tourne même sur batterie). Installée et **démarrée
maintenant** (PID PowerShell 3776 → PID Python 10392).

## Les 9 vérifications — toutes testées (`tests/test_watchdog.py`, 20 tests, tous verts)

| # | Vérification | Niveau | Test |
|---|---|---|---|
| 1 | Symboles hors allowlist (position ouverte + deal du jour) | CRITIQUE | `Check1SymbolTests` (2) |
| 2 | MAX_OPEN > 1 par symbole | CRITIQUE | `Check2MaxOpenTests` (2) |
| 3 | Identité : lot ≠ 0.01 / magic étranger avec comment HERMES | CRITIQUE | `Check3IdentityTests` (2) |
| 4 | Position sans SL ou sans TP | CRITIQUE | `Check4NakedTests` (2) |
| 5 | Perte flottante > 3% équité | HAUTE | `Check5FloatingTests` (2) |
| 6 | Heartbeat bot (dataset/log figé > 10 min en heures de marché) | HAUTE | `Check6HeartbeatTests` (2, incl. exemption week-end) |
| 7 | Kill-switch percé (pertes > quota broker + nouvelles ouvertures) | CRITIQUE | `Check7KillswitchTests` (2) |
| 8 | Santé machine (disque < 5 GB, backup du jour absent après 10h) | HAUTE | intégré au cycle réel |
| 9 | Double instance du bot (wmic) | CRITIQUE | intégré au cycle réel |

Preuve séparée et explicite : **`NoOrderOutsideEmergencyTests`** — hors mode
urgence, `order_send` n'est **jamais** appelé même face à une position sur
symbole interdit + une position nue (`test_no_order_send_without_emergency_flag`).
En mode urgence, **seule** la position sur symbole interdit est fermée, jamais
les autres violations (`test_emergency_true_closes_only_forbidden_symbol`).

Fail-safe supplémentaires testés :
- Telegram indisponible → l'alerte vit quand même dans `WATCHDOG_ALERTS.log`
  (`TelegramFailSafeTests.test_alert_survives_telegram_down`).
- Anti-spam 30 min sur une même clé d'alerte non résolue
  (`test_antispam_30_minutes`).
- Un check qui lève une exception ne tue jamais le gardien — capturé et loggé
  `CHECK_ERROR`, les 8 autres continuent (`CheckErrorNeverKillsGuardianTests`).

## Guide Telegram pas-à-pas (pour SIMO)

1. Dans Telegram, parler à **@BotFather** → `/newbot` → suivre les questions
   (nom, username se terminant par `bot`).
2. BotFather renvoie un **TOKEN** du type `123456789:AAH6h1s...`.
3. Envoyer n'importe quel message au nouveau bot (bouton **START**) — sans
   ça, Telegram ne connaît pas encore le chat.
4. Ouvrir `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` dans un
   navigateur : le champ `"chat":{"id": 123456789}` est le **CHAT_ID**.
5. Copier `watchdog/.env.example` → `watchdog/.env`, renseigner
   `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`, relancer le watchdog (la tâche
   planifiée le relance automatiquement, ou `Restart-ScheduledTask -TaskName
   HERMES_WATCHDOG`).
6. Tant que `.env` est vide, les alertes CRITIQUE/HAUTE vivent uniquement
   dans `watchdog/WATCHDOG_ALERTS.log` — rien n'est perdu, aucune action requise.

## MODE URGENCE (RÉSERVÉ SIMO — décision à prendre)

`WATCHDOG_EMERGENCY_CLOSE=false` par défaut dans `watchdog/.env.example`.
Si mis à `true` : **uniquement** sur une position ouverte sur un symbole hors
`(GOLD#, BTCUSD#)` (vérification 1), le watchdog envoie un `order_send` de
clôture dédié (`comment=WATCHDOG_FORCE_CLOSE`, loggé). Toute autre violation
(lot, nu, kill-switch percé, etc.) reste alerte-seule, quoi qu'il arrive.
**SIMO doit décider s'il active ce flag** — par défaut il est désactivé, donc
le watchdog n'a aujourd'hui **aucun** pouvoir d'action sur le compte.

## Commande de lancement / tâche planifiée

```
powershell -ExecutionPolicy Bypass -File watchdog\start_watchdog.ps1
```
Tâche planifiée Windows installée : **`HERMES_WATCHDOG`** (déclencheur au
boot machine, redémarre le script s'il crash). Vérifiée avec
`Get-ScheduledTask -TaskName HERMES_WATCHDOG`.

## Première ronde réelle (2026-07-08, ~03:59 UTC)

Watchdog lancé en direct (`--once` puis via la tâche planifiée) contre le
compte demo XM réel : **RAS — 9 vérifications passées**, aucune alerte,
`watchdog/WATCHDOG_ALERTS.log` vide (donc pas créé), `watchdog/heartbeat.txt`
écrit (`2026-07-08T03:59:16Z`). Le process tourne actuellement en tâche de
fond (PowerShell runner PID 3776 → process Python watchdog PID 10392).

## Tests

`tests/test_watchdog.py` : 20/20 verts. Suite complète du dépôt après ajout :
**3163 passed, 2 skipped** (vs 3143 avant cette mission — watchdog ajoute
20 tests, aucune régression ailleurs).
