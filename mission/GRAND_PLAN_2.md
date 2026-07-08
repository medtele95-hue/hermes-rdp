ultrathink

GRAND PLAN 2 — MISE EN ORDRE COMPLÈTE DU SYSTÈME. EXÉCUTION SÉQUENTIELLE, SANS S'ARRÊTER.

SIMO veut que tout soit en ordre. Exécute les missions ci-dessous DANS L'ORDRE, en enchaînant sans jamais demander de confirmation. Chaque blocage → documenté dans le rapport de la mission concernée, puis CONTINUE avec la suivante. Décisions opérationnelles déléguées ; décisions de stratégie (seuils, logique) → section RÉSERVÉ SIMO, jamais en question interactive.

RÈGLES PERMANENTES : safepoint git (commit+tag) avant chaque bloc ; tests complets après ; un rouge = rollback du bloc et on continue ; push GitHub après chaque mission réussie ; heure broker explicite ; fail-closed ; le dataset n'est jamais supprimé ; stratégies/seuils/gates de trading JAMAIS modifiés sauf demande explicite (sinon RÉSERVÉ SIMO).

═══════════════════════════════════════
MISSION 1 — RÉPARER LE BUG P&L DU KILL-SWITCH (le plus urgent)
═══════════════════════════════════════
Bug prouvé (logs 2026-07-08) : [CYCLE_SUMMARY] closed_pnl=+13.28 (P&L réel, cohérent MT5) MAIS [DAILY_KILLSWITCH] daily_pnl=-37.14 losses=6/6 → le kill-switch a coupé le trading à tort sur une journée GAGNANTE.
- Compare les deux calculs de P&L sur la fenêtre broker [hier 21:00 UTC -> now]. D'où vient chaque chiffre ?
- Cause probable : le kill-switch compte les pertes brutes sans compenser par les gains, OU compte des trades temporairement rouges (BTC flotte en négatif à cause du spread avant de fermer en micro-gain), OU lit une source différente de CYCLE_SUMMARY.
- Liste les 6 "pertes" avec leur vrai P&L MT5 — combien sont de vraies pertes nettes ?
- FIX : le kill-switch utilise la MÊME source que CYCLE_SUMMARY (deals MT5 fermés, P&L net réel profit+swap+commission). Compteur de pertes = trades au P&L net < 0. daily_pnl du kill-switch DOIT égaler closed_pnl de CYCLE_SUMMARY (+13.28 aujourd'hui).
- Log de contrôle avec assertion si écart > 0.01 entre les deux.
- Si la journée est réellement positive avec < 6 vraies pertes → le kill-switch ne doit plus être déclenché, débloquer le trading.
- Test d'invariant : 14 gagnants + 3 perdants nets → 3 pertes comptées, pas 6, pas de déclenchement.

═══════════════════════════════════════
MISSION 2 — BRANCHER LE DASHBOARD SUR LES BONNES DONNÉES
═══════════════════════════════════════
Le dashboard (control room, port 8010, accessible via Tailscale https://6a4c09c70e4590e.tail35b030.ts.net) affiche "MT5 déconnecté", Mode UNKNOWN, données à "—".
- Cause : le dashboard_api est un process séparé et ne peut pas ouvrir une 2e connexion MT5 en parallèle du bot (MT5 limite à 1 connexion Python).
- FIX : le dashboard NE DOIT PAS ouvrir sa propre connexion MT5. Il lit l'état depuis ce que le bot écrit déjà : le cache local que le bot expose (app/local_api/state, déjà servi sur port 8000 in-process), le dataset (app/data/decision_dataset.jsonl), les logs (logs/hermes.log). Fais lire dashboard_api depuis ces sources fichier/cache.
- Vérifie que s'affichent avec de VRAIES valeurs : équité, balance, mode (DEMO), positions ouvertes + état Exit V2, EES sell/buy, DXY, régime, kill-switch (x/6 + le vrai daily_pnl corrigé en Mission 1), journal de trades, derniers événements.
- Confirme que Tailscale serve reste actif et que l'URL fonctionne toujours pour l'iPhone de SIMO.

═══════════════════════════════════════
MISSION 3 — EXIT V2 À L'ÉCHELLE BTC (RÉSERVÉ SIMO validé : GO)
═══════════════════════════════════════
Découverte de l'audit précédent : le seuil BE d'Exit V2 est +2$ FIXE, ce qui sur BTC (~63000$) exige 0.32% de mouvement contre 0.05% sur GOLD (~4000$) — 6.5× plus dur, la protection breakeven s'arme trop tard sur BTC. La recherche marché confirme : les stops BTC doivent être en % du prix, pas en $ fixes.
- FIX : les seuils Exit V2 pour BTCUSD# passent en POURCENTAGE du prix d'entrée (BE et trailing), calibrés pour donner à BTC la MÊME protection RELATIVE que GOLD. Exemple de départ : BE armé à +0.05% du prix (équivalent relatif du +2$ sur GOLD à 4000$), trailing proportionnel. Documenter les valeurs choisies et leur équivalence avec GOLD.
- GOLD reste sur ses seuils $ actuels (inchangé). Seul BTC bascule en %.
- Log [EXIT_V2] doit montrer le seuil effectif (% et $ équivalent) par symbole.
- Test : position BTC simulée → BE s'arme au bon niveau relatif ; position GOLD → comportement inchangé.
- Le dataset tague les exits BTC pour mesurer l'amélioration.

═══════════════════════════════════════
MISSION 4 — AUTONOMIE TOTALE DE L'AUTO-MÉDECIN
═══════════════════════════════════════
SIMO veut un système autonome. L'auto-médecin passe de diagnostic-seul à RÉPARATION AUTONOME COMPLÈTE.
- Il répare SEUL, sans demander : crashs/crash-loops, bugs de calcul/logique (divergences P&L, compteurs faux, boucles, doublons dataset), bugs de données (corruption, désync, réconciliation), bugs d'infra (disque plein, ports, connexions MT5, services annexes).
- Sécurité par l'infrastructure (pas par permission) : safepoint git avant chaque fix → tests après → rollback auto si un test casse → push GitHub après succès → notification Telegram de chaque action → audit append-only.
- SEULE ligne rouge (ce sont des CHOIX, pas des bugs) : modifier seuils de trading, logique de stratégie, allowlist, risk cap. → détectés, documentés avec reco chiffrée, envoyés en DÉCISION SIMO sur Telegram, jamais appliqués seul. Distinction codée : BUG = un module agit autrement que son design (répare) ; CHOIX = changer le design (réserve SIMO).
- Fréquence : ronde toutes les 2h + déclenché par le watchdog (alerte CRITIQUE/HAUTE) + par le superviseur (crash-loop). claude -p non-interactif + --dangerously-skip-permissions.
- Anti-acharnement : max 3 tentatives/bug → sinon alerte CRITIQUE Telegram + documente les essais.
- Réécris AUTO_MEDIC_MISSION.md avec ce périmètre.
- TEST DE FEU : simule (a) divergence P&L → réparée seule ; (b) crash → superviseur relance + auto-médecin diagnostique ; (c) test qui casse après fix → rollback auto ; (d) changement de seuil simulé → classé DÉCISION SIMO, pas appliqué.

═══════════════════════════════════════
CLÔTURE — VÉRIFICATION QUE TOUT EST EN ORDRE
═══════════════════════════════════════
1. Redémarre le bot proprement (HERMES_LOG_FILE actif, un cycle vérifié).
2. Vérifie l'ORDRE COMPLET du système, chaque composant vivant et cohérent :
   - Bot : trade, log écrit, cœur v2 actif (CONFLUENCE_V2), kill-switch avec le BON daily_pnl (= CYCLE_SUMMARY).
   - Watchdog : tâche active, heartbeat récent.
   - Superviseur : tâche active.
   - Auto-médecin : mode réparation autonome, tâche active, première ronde OK.
   - Dashboard : accessible via Tailscale avec vraies données.
   - Backup quotidien + push GitHub : actifs.
   - Tailscale serve : actif.
3. Écris ETAT_SYSTEME.md : tableau de TOUS les composants (nom, rôle, état, tâche planifiée, dernière activité) + confirmation que chacun est en ordre.
4. Push GitHub final de tout.
5. Résumé Telegram si configuré, sinon dans hermes-reports.

LIVRABLE FINAL : GRAND_PLAN_2_FINAL.md — état de chaque mission (livrée/partielle/bloquée+pourquoi), le tableau ETAT_SYSTEME complet, les actions RÉSERVÉ SIMO consolidées, les actions humaines restantes (Telegram ?), et bottom-line 8 lignes : le système est-il entièrement en ordre et autonome OUI/NON.

Commence maintenant.
