# AUTOPILOT_REPORT — Mission 5 (GRAND_PLAN)

## Décision de cadrage sur COUCHE 2 (auto-médecin) — soumise à SIMO, tranchée

La mission demandait un auto-médecin invoquant `claude -p --dangerously-skip-permissions`
deux fois par jour, autorisé à **committer et pousser ses propres correctifs** sur le
bot de trading, sans supervision — une portée de risque très différente du reste de
cette mission (superviseur de process en lecture/redémarrage seul, rapports en lecture
seule). Avant de l'implémenter tel quel, la question a été posée explicitement : SIMO a
choisi **« auto-médecin diagnostic seul »**. C'est ce qui a été livré : l'auto-médecin
peut lire (`Read`/`Grep`/`Glob` uniquement, jamais `Write`/`Edit`/`Bash`), diagnostiquer,
et proposer des correctifs chiffrés — mais ne peut ni écrire, ni committer, ni pousser
quoi que ce soit lui-même. **Vérifié empiriquement, pas supposé** : un test a montré
qu'un appel `Write` en mode `--permission-mode dontAsk --allowedTools "Read,Grep,Glob"`
est refusé et le process se termine proprement (aucun fichier créé, code retour
non-zéro) — c'est le comportement fail-closed attendu.

## Les 4 couches installées

### COUCHE 1 — Superviseur de process (`scripts/bot_supervisor.ps1`)
Process indépendant du bot (même motif que `watchdog/hermes_watchdog.py`), boucle
60s : (a) vérifie qu'un process `python -m app.main` tourne, le relance sinon avec
`HERMES_LOG_FILE` actif ; (b) vérifie que `watchdog/heartbeat.txt` a moins de 5 min
(gardien du gardien), relance la tâche `HERMES_WATCHDOG` sinon. Anti-boucle : budget de
3 restarts/heure PAR cible (`logs/supervisor_state.json`), au-delà → alerte CRITIQUE
Telegram + déclenchement de `HERMES_AUTO_MEDIC` + suspension 30 min. Log `[AUTO_RESTART]`
à chaque redémarrage. Tâche planifiée `HERMES_BOT_SUPERVISOR` (déclencheur au boot,
`RestartCount=999`).

### COUCHE 2 — Auto-médecin diagnostic seul (`scripts/auto_medic.ps1` + `AUTO_MEDIC_MISSION.md`)
`claude -p` en `--permission-mode dontAsk --allowedTools "Read,Grep,Glob"` (jamais
`--dangerously-skip-permissions`). Le wrapper PowerShell (privilèges normaux, hors de
portée de l'IA) capture la réponse texte finale et écrit
`C:\hermes-reports\auto_medic_[horodatage].md` lui-même — l'IA ne touche jamais au
disque. Budget de sécurité : si le même diagnostic se répète 3 rondes de suite sans
RAS, alerte CRITIQUE Telegram "intervention SIMO/advisor requise" et arrête d'insister.
Tâche planifiée `HERMES_AUTO_MEDIC` (07:00 et 19:00) + déclenchable par le superviseur
en cas de crash-loop.

### COUCHE 3 — Rapport du soir sur téléphone
`scripts/daily_report.py` envoie désormais le message Telegram structuré exact demandé :
```
📊 HERMES [date] : P&L jour, trades (G/P), cumul vers les 50
🛡️ Refus du jour : compteurs par raison
🐕 Watchdog : RAS ou N alerte(s)
🔧 Auto-médecin : dernier diagnostic du jour ou RAS
⚠️ RÉSERVÉ SIMO : décisions en attente ou RAS
```
Le rapport complet reste en `.md` dans `C:\hermes-reports\`, lisible depuis le
téléphone via le miroir Google Drive (`G:\Mon Drive\HERMES_backups\`, mission4).

### COUCHE 4 — Test de feu (preuve réelle, pas simulée)

**(a) Tuer le bot → le superviseur le relance seul.** Exécuté réellement : PID du bot
tué (`Stop-Process -Force`), au cycle suivant (~60-90s) le superviseur a détecté
l'absence, loggé `[AUTO_RESTART] bot HERMES (app.main) redemarre a 05:37`, et un
nouveau process est apparu. Répété une seconde fois après le fix du logger (§ci-dessous)
pour valider la nouvelle logique anti-double-instance — succès, `logs/hermes.log`
recommence à s'écrire immédiatement. `logs/supervisor_state.json` confirme
l'accumulation correcte des restarts pour le budget anti-boucle.

**(b) Alerte watchdog factice → l'auto-médecin la traite.** Deux vraies alertes
`BOT_STALLED` (04:19/04:20 UTC, issues du test (a) avant que le superviseur n'existe)
étaient présentes dans `watchdog/WATCHDOG_ALERTS.log` au moment du run de l'auto-médecin
— il les a lues et documentées comme "problème #2" de son diagnostic (voir ci-dessous).
Test équivalent à une alerte factice : l'auto-médecin ne distingue pas une alerte réelle
d'une alerte de test, il lit le fichier tel quel.

**(c) Lancer `scripts/auto_medic.ps1` manuellement une fois.** Exécuté réellement.
Résultat : `C:\hermes-reports\auto_medic_2026-07-08T064228.md` produit, 5 problèmes
diagnostiqués (voir détail ci-dessous), **aucune écriture/modification tentée** par
l'IA (confirmé par le fait que le rapport ne contient que des propositions de diff,
jamais appliquées).

**Bonus non prévu par la mission mais découvert par le test de feu lui-même** : le
premier run réel de l'auto-médecin a diagnostiqué un vrai bug — `logs/hermes.log` figé
en silence depuis 05:02:31 UTC alors que le bot restait vivant (rotation de log échouée
pendant le double-restart du test (a), un ancien process tenant encore le fichier
verrouillé). C'est exactement le scénario COUCHE 2 point (b)/(e) de la mission. Comme
il s'agit d'un bug technique (pas une stratégie/un seuil/une politique de risque) et que
cette session dispose des permissions normales, **le correctif proposé par l'auto-médecin
a été appliqué directement** (pas par l'auto-médecin lui-même, par cette session) :
- `app/logger.py` : `SafeRotatingFileHandler` — un échec de `doRollover()` (ex: fichier
  verrouillé) n'éteint plus le logger, il continue d'écrire sans rotation et log
  l'échec sur stderr au lieu de mourir en silence.
- `scripts/bot_supervisor.ps1` (`Start-Bot`) : tue proprement toute instance `app.main`
  résiduelle avant d'en relancer une nouvelle, pour ne plus jamais avoir deux process
  tenant `logs/hermes.log` ouvert simultanément pendant un rollover.
- Tests : `tests/test_logger.py` (2 tests, la régression exacte diagnostiquée) + suite
  complète 3165 passed (+2), 0 régression. Bot redémarré proprement avec le correctif
  actif — `logs/hermes.log` s'écrit à nouveau en continu (vérifié).

## Périmètre exact de l'auto-médecin

**Peut** : lire `WATCHDOG_ALERTS.log`, `logs/hermes.log`, `HERMES_STATE.md`, le dataset,
tout fichier du dépôt (`Read`/`Grep`/`Glob`) ; diagnostiquer et classer les problèmes
(bot mort, boucle de logs, invariant percé, disque plein, fichier corrompu) ; proposer
des correctifs chiffrés (diff en texte) dans la section RÉSERVÉ SIMO de son rapport.

**Ne peut pas** (techniquement bloqué, pas seulement instruit de ne pas faire) : écrire,
éditer, ou supprimer un fichier ; exécuter une commande (`Bash`) ; committer ou pousser
sur git ; envoyer une requête réseau (`WebFetch`). Vérifié par test direct (§ci-dessus).
Toute correction — y compris purement technique — attend une session Claude Code
invoquée normalement par SIMO (ou par une future mission), jamais appliquée sans
supervision.

## Guide SIMO (5 lignes)

1. Chaque soir, le message Telegram (5 lignes) suffit à savoir si tout va bien ; le
   `.md` complet dans `C:\hermes-reports\` (via Google Drive) donne le détail si besoin.
2. Deux fois par jour (07:00/19:00), un rapport `auto_medic_*.md` apparaît dans le même
   dossier — c'est un diagnostic, jamais une action déjà prise.
3. **Action attendue de SIMO, cas 1** : la section RÉSERVÉ SIMO d'un bilan ou d'un
   diagnostic contient une proposition de correctif ou une décision stratégique —
   SIMO l'examine et, s'il est d'accord, demande à une session Claude Code de l'appliquer.
4. **Action attendue de SIMO, cas 2** : une alerte Telegram CRITIQUE dit "non résolu
   après 3 rondes" ou "crash-loop" — c'est le seul signal d'urgence réelle, tout le
   reste est informatif.
5. Rien d'autre ne requiert d'intervention manuelle : redémarrages, backups, rapports
   sont désormais entièrement automatiques.

## Actions humaines encore pendantes (rappel consolidé)

- **Remote git privé** : toujours absent (`git remote` vide) — le bilan quotidien et
  `HERMES_STATE.md` continuent d'alerter chaque jour tant que ce n'est pas réglé
  (voir `docs/BACKUP_SIMO.md`).
- **Telegram** : `watchdog/.env` n'a toujours pas de token/chat_id renseignés — tant que
  ce n'est pas fait, tous les push (watchdog, bilan, auto-médecin, superviseur) restent
  fail-safe silencieux (les fichiers `.md`/`.log` contiennent tout quand même).
- **Mode urgence watchdog** (`WATCHDOG_EMERGENCY_CLOSE`) : décision à prendre, toujours
  `false` par défaut (mission3).
- **Google Drive** : déjà détecté et fonctionnel (`G:\Mon Drive`) — aucune action requise.
