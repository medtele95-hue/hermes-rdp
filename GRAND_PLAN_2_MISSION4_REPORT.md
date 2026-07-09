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

## Activation — confirmée par SIMO, appliquée

SIMO a choisi "Activer maintenant (toutes les 2h)" — exactement la spécification d'origine de la mission. Appliqué :
- Tâche planifiée `HERMES_AUTO_MEDIC` : déclencheur remplacé (2×/jour → toutes les 2h, `RepetitionInterval=PT2H`, `RepetitionDuration=P3650D` soit ~10 ans, effectivement indéfini). Vérifié : `NextRunTime` confirmé à +2h de l'activation.
- L'action de la tâche (`scripts/auto_medic.ps1`) est inchangée par cette étape — déjà mise à jour avec le nouveau scope autonome dans les commits précédents de cette mission, déjà poussée sur GitHub.
- Premier round réel non encore observé au moment de ce rapport (prochain déclenchement dans ~2h) — le résultat de cette première ronde autonome sera visible dans `logs/auto_medic_audit.log` et sur Telegram si configuré.

C'était le point précis où le système cesse d'avoir un humain dans la boucle pour chaque changement de code, remplacé par le filet safepoint→tests→rollback — décision explicitement confirmée par SIMO avant activation, pas prise seul.

## Première ronde réelle (19:00, avant même le changement de cadence) — analysée et corrigée

Le déclencheur 19:00 pré-existant (avant mon changement de cadence à 2h) a suffi à exercer le nouveau script pour de vrai. Résultat, analysé en détail après coup :

1. **Le fix lui-même était correct** : la session a trouvé un vrai bug (le kill-switch du watchdog interrogeait MT5 avec une fenêtre 3h trop tôt — même famille que `FIX_KILLSWITCH_PNL.md`, jamais migrée dans `watchdog/hermes_watchdog.py`), l'a corrigé avec un diff minimal, a ajouté un test de régression, a respecté la LIGNE ROUGE. Vérifié indépendamment par moi : merge propre, 3371 passed, 0 régression réelle.
2. **Faille trouvée n°1 — la session a poussé elle-même sur GitHub**, alors que le wrapper prévoyait de le faire lui-même après SA propre vérification. Le wrapper a ensuite vu échouer 2 tests (contention fichier avec le bot live, sans rapport avec le fix) et déclenché un rollback *local* — mais le commit était déjà sur origin, rendant ce rollback cosmétique côté GitHub. Corrigé : (a) instruction explicite dans `AUTO_MEDIC_MISSION.md` interdisant à la session de pousser, (b) `auto_medic.ps1` ne fait plus confiance à l'instruction seule — il vérifie après coup si origin a dépassé le safepoint et force le remote à y revenir si besoin.
3. **Faille trouvée n°2 — la cause des 2 échecs qui ont déclenché ce rollack** : `tests/test_paper_learning_safety.py::Mt5PositionSyncTests` résolvait par défaut le VRAI chemin de production `app/data/position_closed_seen.json` (la majorité de ses tests ne passaient pas de chemin isolé), le supprimait dans `setUp()` et écrivait dedans — en concurrence avec le bot live. Pas juste flaky : risque réel de faire perdre au bot son propre suivi d'idempotence à chaque run de la suite complète, manuel ou automatique. Corrigé : isolation complète via répertoire temporaire, plus jamais le fichier réel touché. Vérifié : 33/33 tests passent, fichier de production confirmé intact après le run.

Ces deux failles n'auraient probablement jamais été trouvées sans un vrai passage en production — exactement la valeur d'avoir testé mécaniquement plutôt que de simuler en théorie seule.
