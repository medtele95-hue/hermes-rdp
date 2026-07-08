ultrathink

MISSION : HERMES AUTOPILOT — SYSTÈME ENTIÈREMENT AUTONOME. SIMO NE FAIT QUE LIRE LES RAPPORTS SUR SON TÉLÉPHONE.

Décision SIMO : feu vert total pour l'automatisation complète. Claude vérifie, Claude audite, Claude intervient, Claude répare. SIMO reçoit les rapports le soir sur Telegram et n'intervient plus jamais manuellement, sauf décisions stratégiques.

PRÉREQUIS : cette mission suppose FIX_BTC (avec la décision 2-symboles GOLD#+BTCUSD#), WATCHDOG et AUTOMATION livrés. Si l'un manque, exécute-le d'abord.

Safepoint git avant. Tests après.

═══════════════════════════════════
COUCHE 1 — SUPERVISEUR DE PROCESS (le bot ne meurt jamais)
═══════════════════════════════════
- Tâche planifiée Windows au boot + surveillance : si le process du bot est mort → redémarrage automatique avec HERMES_LOG_FILE défini, log [AUTO_RESTART] + notification Telegram "bot redémarré à HH:MM, raison détectée : X".
- Anti-boucle : max 3 restarts/heure ; au-delà → STOP + alerte CRITIQUE Telegram "bot en crash-loop, intervention auto-médecin déclenchée".
- Pareil pour le watchdog lui-même (le gardien du gardien : une tâche planifiée vérifie son heartbeat).

═══════════════════════════════════
COUCHE 2 — L'AUTO-MÉDECIN (Claude Code planifié, le cœur de l'autonomie)
═══════════════════════════════════
- Script scripts/auto_medic.ps1 lancé par tâche planifiée 2x/jour (07:00 et 19:00) + déclenchable par le superviseur en cas de crash-loop :
  → il invoque : claude -p (mode non-interactif) avec --dangerously-skip-permissions, sur une mission de diagnostic écrite dans AUTO_MEDIC_MISSION.md.
- Contenu d'AUTO_MEDIC_MISSION.md (crée-le) :
  « Lis WATCHDOG_ALERTS.log, les derniers logs du bot, l'état git, le dataset. Diagnostique tout problème. Tu es AUTORISÉ à réparer seul, avec safepoint git avant chaque fix + tests après : (a) bot mort/crash-loop → identifier la cause dans les logs, corriger si c'est un bug clair (import manquant, chemin, exception répétée), redémarrer ; (b) boucles de logs / spam → fix idempotence ; (c) invariant percé détecté par le watchdog (symbole interdit, lot, magic, SL manquant) → restaurer l'invariant immédiatement ; (d) disque plein → purger les fichiers temporaires/vieux backups (jamais le dataset vivant) ; (e) fichier corrompu non-critique → réparer ou isoler.
  Tu n'es PAS AUTORISÉ à : modifier stratégies, seuils, gates, politique de risque, quotas kill-switch, ou tout paramètre de trading — ces sujets vont dans la section RÉSERVÉ SIMO du rapport avec ton diagnostic et ta recommandation chiffrée.
  Termine TOUJOURS par : écrire C:\hermes-reports\auto_medic_[date_heure].md (diagnostic, actions prises avec commits, section RÉSERVÉ SIMO) + envoyer le résumé 5 lignes sur Telegram. »
- Budget de sécurité : l'auto-médecin ne peut pas tourner plus de 2 fois de suite sur le même problème sans le résoudre → au 3e échec, alerte CRITIQUE Telegram "problème X non résolu automatiquement, intervention SIMO/advisor requise" et il n'insiste plus.

═══════════════════════════════════
COUCHE 3 — LE RAPPORT DU SOIR SUR TÉLÉPHONE (l'interface unique de SIMO)
═══════════════════════════════════
- Renforcer le bilan quotidien 21:30 (Script 1 d'AUTOMATION) pour qu'il envoie sur Telegram un message structuré :
  📊 HERMES [date] : P&L jour, trades (G/P), cumul vers les 50
  🛡️ Refus du jour : EES/news/abort/killswitch (compteurs)
  🐕 Watchdog : RAS ou alertes
  🔧 Auto-médecin : actions du jour ou RAS
  ⚠️ RÉSERVÉ SIMO : les décisions qui t'attendent (ou RAS)
- Le rapport complet reste en .md dans hermes-reports (lisible via Google Drive depuis le téléphone).

═══════════════════════════════════
COUCHE 4 — LE TEST DE FEU (preuve d'autonomie)
═══════════════════════════════════
- Simule et prouve : (a) tuer le process du bot → superviseur le relance seul + Telegram ; (b) créer une alerte watchdog factice → l'auto-médecin la traite à sa prochaine ronde ; (c) lancer manuellement scripts/auto_medic.ps1 une fois → le rapport auto_medic sort et le Telegram arrive.
- Documenter dans le livrable ce qui a été prouvé.

═══════════════════════════════════
LIVRABLE : AUTOPILOT_REPORT.md
═══════════════════════════════════
1. Les 4 couches installées avec preuves (tâches planifiées listées, test de feu réussi).
2. Le périmètre exact de l'auto-médecin (ce qu'il peut/ne peut pas toucher).
3. Guide SIMO 5 lignes : ce qui arrive sur son téléphone, quand, et les 2 seules situations où on attend une action de lui (RÉSERVÉ SIMO + alerte critique non résolue).
4. Rappel des actions humaines encore pendantes (remote git ? Telegram configuré ?).
