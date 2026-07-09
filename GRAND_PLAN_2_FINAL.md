# GRAND_PLAN_2 — RAPPORT FINAL (2026-07-09)

## État par mission

**Mission 1** (bug P&L kill-switch) — déjà livrée avant ce GRAND_PLAN_2 (mission FIX_KILLSWITCH_PNL antérieure). Confirmée toujours vraie pendant toute cette clôture : `[PNL_CROSS_CHECK] OK divergence=0.0000` à chaque cycle.

**Mission 2 + FIX_DASHBOARD_DATA** — ✅ LIVRÉE. Dashboard reconstruit en MT5-primary + dataset-enrichi, journal avec date/heure/durée/P&L réel/mode de clôture/filtres, total exact. Vérifié en direct (`+16.43` puis données live cohérentes), poussé sur GitHub.

**Mission 3** (Exit V2 BTC en %) — ✅ LIVRÉE, SIMO validé GO. BTC bascule sur seuils %-du-prix-d'entrée reproduisant exactement la protection relative de GOLD (0.05%/0.0025%/0.05%/0.03%). GOLD inchangé, vérifié par test qu'aucune config BTC ne peut jamais l'affecter. Preuve live : BTC `be_arm_usd_effective=0.3089` (6.5× plus facile à armer que l'ancien $2 fixe) vs GOLD `be_arm_usd_effective=2.0` inchangé.

**Mission 4** (auto-médecin autonome) — ✅ LIVRÉE ET ACTIVÉE, avec un bémol trouvé en production (voir plus bas). Infrastructure complète : safepoint→claude--dangerously-skip-permissions→tests→rollback-ou-push, distinction BUG/CHOIX, anti-acharnement, watchdog→auto-médecin sur alerte CRITIQUE/HAUTE. TEST DE FEU (c) exécuté réellement (pas seulement simulé) sur branche jetable — a lui-même trouvé et corrigé un vrai risque (`git clean -fd` supprimait des fichiers légitimes non-trackés).

## Cloture — vérification complète

`ETAT_SYSTEME.md` (à la racine) donne le tableau complet. Résumé : bot, watchdog, superviseurs, dashboard, backup, push GitHub, Tailscale serve tous vérifiés vivants et cohérents pendant cette clôture — avec 3 vrais bugs supplémentaires trouvés et corrigés en le faisant (pas seulement en le lisant) :

1. **Watchdog tournait avec du code périmé** (22h sans redémarrage, ratait tous les fixes du jour) — redémarré.
2. **Dashboard bloqué par une fuite de connexions** (~150 connexions TCP simultanées, plus aucune requête ne passait) — redémarré + durci (`timeout_keep_alive`, `limit_concurrency`).
3. **Un test de la suite touchait le vrai fichier de production** `position_closed_seen.json` du bot live (setUp() le supprimait à chaque run) — isolé en tmpdir.

## Auto-médecin en production réelle — ce qui a marché, ce qui a été trouvé et corrigé

La première ronde réelle (19:00, avant même le changement de cadence à 2h) a diagnostiqué et corrigé un vrai bug (kill-switch du watchdog, même famille 3h-MT5 que Mission 1, jamais migrée) — vérifié indépendamment, correct. Ce même test de feu a exposé et fait corriger deux failles de l'infrastructure elle-même : la session poussait sur GitHub avant que le wrapper ait fini de vérifier (corrigé : interdiction + forçage du remote au safepoint en cas de rollback), et un test cassait à tort sur de la contention fichier avec le bot live (corrigé : isolation).

Une deuxième ronde (02:36, cadence 2h) a trouvé et corrigé un deuxième vrai bug watchdog (`KILLSWITCH_PIERCED` déclenché dès qu'UNE position était ouverte n'importe quand dans la journée, sans vérifier l'ordre chronologique réel) — vérifié indépendamment par moi (3372 tests, 0 régression), commit `efb6b975` conservé.

**Problème trouvé et NON résolu avec certitude** : plusieurs rondes suivantes (02:55, 03:10) ont démarré puis se sont arrêtées prématurément (`SCHED_S_TASK_TERMINATED`, quelques minutes seulement) sans produire de rapport ni de commit, sans qu'aucune trace dans les logs (watchdog, superviseur, ni le déclencheur habituel) n'explique qui a lancé ces rondes ni pourquoi elles ont été coupées. Investigation menée : le déclenchement watchdog→auto-médecin n'a laissé aucune trace de son côté ; le superviseur n'a signalé aucun crash-loop ; le processus watchdog n'a jamais redémarré pendant cette fenêtre ; aucune trace système exploitable trouvée. **Par précaution, j'ai désactivé la tâche planifiée `HERMES_AUTO_MEDIC`** (`Disable-ScheduledTask`) le temps de comprendre ce pattern — le code lui-même reste correct et testé (le seul vrai bug trouvé en route, la limite d'exécution à 15 minutes trop courte, a été corrigé à 45 minutes), mais je préfère une pause volontaire à un système qui se redéclenche sans explication.

## Décisions RÉSERVÉ SIMO consolidées

Aucune cette clôture — tous les fixes (missions 2-4 + closure) étaient des bugs (écart au design), jamais des choix de seuil/stratégie/allowlist/risk-cap.

## Actions humaines restantes

1. **Décider si `HERMES_AUTO_MEDIC` doit être réactivé** (`Enable-ScheduledTask -TaskName HERMES_AUTO_MEDIC`) — je recommande d'observer d'abord si le pattern de déclenchement mystère se reproduit en lecture seule (logs `_automation_health.log`, `Get-ScheduledTaskInfo`) avant de le remettre en route autonome.
2. `mission/AUTOPSIE_TRADE.md` — nouvelle demande SIMO trouvée non liée à GRAND_PLAN_2 (rapport forensique sur un trade précis), committée pour la protéger, pas encore exécutée.
3. Remote git privé (mentionné dans une mémoire antérieure) — toujours en attente si applicable.

## Bottom-line (8 lignes)

Missions 1 à 3 : livrées et vérifiées en direct, aucune régression. Mission 4 : infrastructure livrée, testée mécaniquement (pas seulement en théorie), a already trouvé et corrigé 2 vrais bugs de production en conditions réelles. La clôture a trouvé et corrigé 3 bugs supplémentaires purement grâce à une vérification active (pas une simple lecture de logs). Le système de trading (bot, kill-switch, watchdog, dashboard, backup, Tailscale) est vérifié EN ORDRE et cohérent au moment de ce rapport. L'auto-médecin est actuellement DÉSACTIVÉ par précaution — un pattern de rondes qui démarrent et s'arrêtent sans log exploitable n'a pas pu être expliqué avec certitude dans le temps disponible, et je préfère un système en pause qu'un système qui agit sans que je comprenne pourquoi. **Le système n'est donc PAS encore intégralement autonome (NON)** — tout le reste tourne correctement en autonomie supervisée (superviseurs, watchdog, backups), mais la pièce spécifiquement demandée comme "réparation autonome complète" attend une explication du pattern de déclenchement avant réactivation. Recommandation : réactiver après avoir observé un cycle complet sans triggering mystérieux, ou après avoir ajouté un log plus détaillé côté Windows Task Scheduler pour capturer la prochaine occurrence.
