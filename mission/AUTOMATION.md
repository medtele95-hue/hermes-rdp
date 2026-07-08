ultrathink

MISSION : AUTOMATISATION COMPLÈTE DU QUOTIDIEN HERMES — SIMO NE FAIT PLUS RIEN À LA MAIN

Objectif : toutes les tâches journalières de surveillance et de reporting deviennent des scripts planifiés Windows. SIMO lit des rapports, il ne tape plus de commandes.

Safepoint git avant. Tests après. Tous les scripts en LECTURE SEULE sur les fichiers du bot (aucune modification du trading), rapports écrits dans C:\hermes-reports\ (créer le dossier).

═══════════════════════════════
SCRIPT 1 — BILAN QUOTIDIEN (chaque jour 21:30, après le reset kill-switch)
═══════════════════════════════
scripts/daily_report.py → C:\hermes-reports\bilan_YYYY-MM-DD.md :
- Trades du jour depuis les deals MT5 (source de vérité) : ticket, direction, entry/exit, P&L réel, durée, mode de fermeture (Exit V2 trailing / SL / TP / autre — depuis les événements du dataset).
- Cumuls : net du jour, net depuis le début, win rate courant, nombre de trades total (progression vers les 50).
- Signaux refusés du jour : EES_EXTREME_BLOCK, NEWS_BLACKOUT, ORDER_ABORT, SYMBOL_BLOCKED, kill-switch (comptés depuis le dataset/logs).
- Santé : état kill-switch (x/6), DD du jour, taille dataset, dernier backup, alertes watchdog du jour (lire WATCHDOG_ALERTS.log).
- 3 lignes de synthèse en tête : P&L, événement notable, anomalie éventuelle.
- BONUS : si le bot Telegram du watchdog est configuré, envoyer les 3 lignes de synthèse en notification.

═══════════════════════════════
SCRIPT 2 — BACKUP + GIT PUSH (chaque jour 09:00, renforcer l'existant)
═══════════════════════════════
- Vérifier/compléter la tâche backup du Bloc 11d : copie datée data+env+reports vers backups\YYYY-MM-DD\ ET vers le dossier synchronisé Google Drive (détecter le chemin Google Drive de la machine, ex: G:\Mon Drive\ ou C:\Users\Admin\Mon Drive\ — le documenter ; si introuvable, l'écrire dans le rapport pour que SIMO le branche).
- git add/commit/push automatique quotidien SI un remote existe. S'il n'existe pas : ALERTE dans le bilan quotidien "REMOTE GIT MANQUANT — action SIMO requise" chaque jour jusqu'à ce que ce soit fait.
- Rotation : garder 14 jours de backups locaux, purger au-delà (jamais toucher au dataset vivant).

═══════════════════════════════
SCRIPT 3 — SNAPSHOT HEBDO POUR COWORK (dimanche 12:00)
═══════════════════════════════
scripts/weekly_snapshot.py → C:\hermes-reports\semaine_YYYY-WW\ :
- Copie de travail : dataset de la semaine (extrait jsonl), tous les bilans quotidiens, alertes watchdog, stats brutes pré-calculées (trades par session, par stratégie, distribution des exits).
- Un fichier COWORK_BRIEF.md à la racine du dossier : "Analyse ce dossier : win rate par session/stratégie/bande EES, patterns émergents, comparaison avec la semaine précédente, 5 observations chiffrées, AUCUNE recommandation de modification — observation pure."
→ SIMO n'a plus qu'à ouvrir Cowork le dimanche et pointer ce dossier.

═══════════════════════════════
SCRIPT 4 — HERMES_STATE.md (mis à jour à chaque bilan quotidien)
═══════════════════════════════
Un fichier d'état vivant à la racine du repo : version/tags actuels, invariants actifs, métriques cumulées, derniers changements, alertes ouvertes. C'est le fichier que Claude Code lira en début de toute future mission (contexte permanent) et que SIMO peut coller à son advisor.

═══════════════════════════════
PLANIFICATION
═══════════════════════════════
- Tâches planifiées Windows (schtasks) pour les scripts 1, 2, 3 — installées, testées, survivant au reboot.
- Chaque script : fail-safe (une erreur → il écrit l'erreur dans son rapport et se termine proprement, jamais de crash silencieux) + heartbeat dans C:\hermes-reports\_automation_health.log.
- Exécuter chaque script UNE FOIS immédiatement pour valider (le bilan d'aujourd'hui doit sortir).

LIVRABLE : AUTOMATION_REPORT.md — les 4 scripts, les tâches planifiées installées (preuve schtasks), le premier bilan quotidien réel généré, le chemin Google Drive détecté ou manquant, et le mode d'emploi 5 lignes pour SIMO (où lire quoi, quand).
