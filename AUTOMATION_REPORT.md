# AUTOMATION_REPORT — Mission 4 (GRAND_PLAN)

## Les 4 scripts

Tous en **lecture seule** sur les fichiers du bot (dataset, events, logs, MT5 `history_deals_get`/`account_info`) — aucun n'écrit dans `app/data/` ni ne passe d'ordre. Fail-safe : toute exception est capturée, écrite dans le rapport (`bilan_ERROR_*.md` / `weekly_snapshot_ERROR_*.md`) et dans le heartbeat, jamais de crash silencieux.

1. **`scripts/daily_report.py`** → `C:\hermes-reports\bilan_YYYY-MM-DD.md`. Trades du jour depuis `mt5.history_deals_get` (source de vérité, fenêtre "jour broker" UTC+3), croisés avec `decision_dataset.jsonl` (rows `outcome`) pour le mode de clôture (SL_HIT/TP_HIT/...). Cumuls (net jour, net tout-temps, win rate, progression vers 50). Signaux refusés (EES_EXTREME_BLOCK, NEWS_BLACKOUT, ORDER_ABORT, SYMBOL_BLOCKED). Santé (kill-switch x/6 et drawdown depuis `demo_pilot_events.jsonl.daily_killswitch`, taille dataset, dernier backup, alertes watchdog du jour lues dans `WATCHDOG_ALERTS.log`). 3 lignes de synthèse en tête. Envoie ces 3 lignes en push Telegram si `watchdog/.env` est configuré (fail-safe si absent). Appelle `update_hermes_state.py` en fin de course.
2. **`scripts/backup_daily.ps1`** (Bloc 11d renforcé) : copie datée locale (inchangé) **+ miroir automatique vers Google Drive** si un dossier synchronisé est détecté **+ `git push` si un remote `origin` existe, sinon écrit un statut consommé par le bilan quotidien** (`backups/_status.json`) qui affiche l'alerte "REMOTE GIT MANQUANT" tant que ce n'est pas réglé. Rotation resserrée de 30 à **14 jours** (spec mission). Écriture de log durcie contre les collisions de fichier transitoires (retry 5×200ms).
3. **`scripts/weekly_snapshot.py`** → `C:\hermes-reports\semaine_YYYY-WW\` : extrait `jsonl` de la semaine, copie de tous les bilans quotidiens de la semaine, alertes watchdog de la semaine, `stats_precalculees.json` (trades par session/stratégie, distribution des sorties, P&L net par symbole), et `COWORK_BRIEF.md` avec le texte d'instruction exact demandé ("AUCUNE recommandation de modification — observation pure"), référençant le dossier de la semaine précédente s'il existe.
4. **`scripts/update_hermes_state.py`** → `HERMES_STATE.md` (racine du repo) : branche/tags/derniers commits (source git), invariants actifs (texte fixe reflétant le verrou mission1), métriques cumulées (dataset), alertes watchdog ouvertes, actions RÉSERVÉ SIMO en attente. Appelé automatiquement à chaque bilan quotidien ; exécutable seul.

## Tâches planifiées installées (preuve `Get-ScheduledTask`)

| Tâche | Déclencheur | État | Prochain lancement |
|---|---|---|---|
| `HERMES_DAILY_BACKUP` | Quotidien 09:00 | Ready | 2026-07-08 09:00:00 |
| `HERMES_DAILY_REPORT` | Quotidien 21:30 | Ready | 2026-07-08 21:30:30 |
| `HERMES_WEEKLY_SNAPSHOT` | Dimanche 12:00 | Ready | 2026-07-12 12:00:00 |
| `HERMES_WATCHDOG` (mission3, rappel) | Au boot | Running | — |

Les trois tâches survivent au reboot (déclencheurs `-Daily`/`-Weekly`/`-AtStartup`, pas de dépendance à une session interactive ouverte). `HERMES_DAILY_BACKUP` existait déjà (Bloc 11d) et a été **replanifiée** de 21:30 (qui faisait doublon avec le bilan) vers 09:00 comme demandé par la mission.

## Premier bilan réel généré

`C:\hermes-reports\bilan_2026-07-08.md` — exécuté immédiatement pour validation (hors planification) :
- P&L du jour : **-45.90 USD** (13W/6L sur 19 trades) | P&L cumulé dataset : -69.06 USD
- 19 trades du jour listés (ticket, sens, entrée/sortie MT5 réelles, P&L net, durée, mode de clôture)
- Kill-switch **5/6**, drawdown du jour **0.508%**
- 2 alertes watchdog du jour reprises (BOT_STALLED, déjà résolues)
- 75 `ORDER_ABORT` comptés dans les signaux refusés du jour
- Alerte "REMOTE GIT MANQUANT" présente (le remote n'existe toujours pas)

`scripts/weekly_snapshot.py` et `scripts/update_hermes_state.py` ont également été exécutés une fois avec succès (`C:\hermes-reports\semaine_2026-W28\`, `HERMES_STATE.md` à la racine).

## Chemin Google Drive

**Détecté** : `G:\Mon Drive`. Le backup du jour a été effectivement miroité vers `G:\Mon Drive\HERMES_backups\2026-07-08\` (confirmé dans `backups/backup.log`). Aucune action SIMO requise sur ce point.

## Mode d'emploi (5 lignes, pour SIMO)

1. Chaque soir à 21:30 : lire `C:\hermes-reports\bilan_YYYY-MM-DD.md` — 3 lignes de synthèse en haut suffisent la plupart du temps.
2. Chaque dimanche : ouvrir `C:\hermes-reports\semaine_YYYY-WW\` dans Cowork et pointer `COWORK_BRIEF.md`.
3. `HERMES_STATE.md` à la racine du repo = photo d'état à jour à chaque bilan — c'est le fichier à coller à un advisor ou à faire lire à Claude Code en début de mission.
4. Si un bilan affiche "REMOTE GIT MANQUANT" : action encore en attente, voir `docs/BACKUP_SIMO.md`.
5. Toute anomalie (script en erreur, tâche qui ne tourne pas) se voit dans `C:\hermes-reports\_automation_health.log`.
