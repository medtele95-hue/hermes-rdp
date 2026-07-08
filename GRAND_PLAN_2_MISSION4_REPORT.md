# GRAND_PLAN_2 — Mission 4 : autonomie totale de l'auto-médecin

## Ce qui est livré et testé

- `AUTO_MEDIC_MISSION.md` réécrit : diagnostic-seul → réparation autonome. Distinction explicite BUG (répare seul) vs CHOIX (seuils/stratégie/allowlist/risk-cap — JAMAIS appliqué, toujours DÉCISION SIMO documentée et chiffrée).
- `scripts/auto_medic.ps1` réécrit : safepoint git (tag) avant chaque session `claude -p --dangerously-skip-permissions`, suite de tests complète après, rollback automatique si un seul test casse, push GitHub si succès, notification Telegram par ronde, journal append-only `logs/auto_medic_audit.log`, anti-acharnement (3 rollbacks d'affilée → suspension 2h + alerte CRITIQUE).
- `watchdog/hermes_watchdog.py` : toute alerte CRITIQUE/HAUTE déclenche désormais `HERMES_AUTO_MEDIC` via `schtasks /run` (même fenêtre anti-spam que Telegram, 30 min).
- 4 tests dédiés pour ce déclenchement + suite complète : 3370 passed, 0 régression.

## TEST DE FEU — résultats réels, pas seulement simulés en théorie

**(a) Divergence P&L → réparée seule** : déjà démontré concrètement plus tôt dans ce même GRAND_PLAN_2 (mission FIX_KILLSWITCH_PNL, exécutée par moi selon exactement la même discipline que celle demandée à l'auto-médecin — diagnostic causal précis, fix minimal, test ajouté, preuve live) — pas re-simulé séparément.

**(b) Crash → superviseur relance + auto-médecin diagnostique** : déjà vérifié plusieurs fois en direct dans cette session (`bot_supervisor.ps1` détecte et relance le bot, PID changé à chaque fois, confirmé via `supervisor.log`) ; le déclenchement crash-loop → `HERMES_AUTO_MEDIC` existait déjà dans `bot_supervisor.ps1`, inchangé.

**(c) Test qui casse après fix → rollback auto** : **exécuté réellement**, pas seulement raisonné — sur une branche jetable (`test-de-feu-scenario-c`), safepoint tag posé, régression intentionnelle réintroduite (le bug de garde-fou déjà trouvé et corrigé plus tôt dans ce GRAND_PLAN_2), suite de tests lancée (4 échecs confirmés, exit code 1 — exactement ce que le script détecte), `git reset --hard` vers le safepoint exécuté, bug confirmé disparu. **Mécanique de rollback prouvée correcte.**

**Incident réel trouvé pendant ce test** : `git clean -fd` (qui accompagnait le `git reset --hard` dans la version initiale du script) a supprimé les 3 fichiers `mission/*.md` de cette mission (`GRAND_PLAN_2.md`, `FIX_DASHBOARD_DATA.md`, `FIX_KILLSWITCH_PNL.md`) — ils n'avaient jamais été committés depuis leur création. Restaurés immédiatement depuis le contenu déjà lu en contexte, puis committés pour de bon. **`git clean -fd` retiré du script** : un rollback ne supprime plus jamais de fichier non-tracké automatiquement, seulement `git reset --hard` (qui n'affecte que les fichiers suivis par git) — les fichiers non-trackés résiduels sont listés dans le log de santé pour revue humaine. Ce test de feu a donc trouvé et corrigé un vrai bug de sécurité dans l'infrastructure elle-même avant toute mise en production.

**(d) Changement de seuil simulé → classé DÉCISION SIMO, pas appliqué** : non testé en conditions réelles (nécessiterait une session `claude -p --dangerously-skip-permissions` live — voir section suivante). Appuyé sur un précédent comportemental direct : dans cette même session, chaque changement de seuil (Exit V2 BTC, mission 3) a été explicitement subordonné à une autorisation écrite ("SIMO validé GO") avant toute modification — aucun seuil n'a été changé sans autorisation explicite nulle part dans cette session. `AUTO_MEDIC_MISSION.md` codifie cette même discipline en instruction explicite et répétée.

## PAS ENCORE FAIT — décision requise avant activation réelle

Ce qui est construit et testé mécaniquement (safepoint/rollback, déclenchement watchdog, infrastructure Telegram/audit) n'est **pas encore activé en production**. Restent :
1. Changer la cadence de la tâche planifiée `HERMES_AUTO_MEDIC` de 2×/jour à toutes les 2h (mission l'exige explicitement).
2. Laisser la tâche s'exécuter pour de vrai avec `--dangerously-skip-permissions` — c'est le point précis où le système cesse d'avoir un humain dans la boucle pour CHAQUE changement de code, remplacé par le filet safepoint→tests→rollback.

C'est un changement de nature, pas juste d'échelle, par rapport à tout ce qui a été fait cette session (chaque action jusqu'ici a été supervisée, une seule à la fois, revue avant/après). Je ne l'active pas sans confirmation explicite — voir question posée séparément.
