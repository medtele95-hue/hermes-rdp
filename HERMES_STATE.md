# HERMES_STATE — état vivant
_Mis à jour automatiquement le 2026-07-08T09:38:09.353033+00:00 par scripts/update_hermes_state.py_

## Version
- Branche : `feature/geo-confluence-hardening`
- Derniers tags :
  - coeur-v2-complete-20260708
  - coeur-v2-chantier3-range-breakout-strategy-aware
  - coeur-v2-chantier2-atr-wilder
  - coeur-v2-chantier1-confluence
  - grand-plan-complete-20260708
  - mission5-autopilot
  - mission4-automation
  - mission2-math-core-audit
  - mission3-watchdog
  - safepoint-pre-math-core-audit
- Derniers commits :
  - f8009c86 chore: untrack generated report_*.txt dumps (add to .gitignore) before GitHub push cleanup
  - 8b5cbaed chore: commit COEUR_V2 mission spec + runtime state before git history cleanup
  - 00e386d4 gitignore: exclure les gros fichiers de donnees du repo
  - 80006f04 coeur-v2 cloture: COEUR_V2_REPORT.md, bot redemarre, cycle verifie
  - feea9724 coeur-v2 cloture: core_version=2 sur chaque nouvelle ligne du dataset
  - b94428b4 coeur-v2 chantier3: GOLD_RANGE_BREAKOUT active + strategy_aware par defaut
  - 736dd46f coeur-v2 chantier2: ATR migre SMA->Wilder RMA + recalibration (10 sites)
  - 9d5593d5 coeur-v2 chantier1: FINAL_CONFLUENCE normalisee (somme ponderee 0-100)

## Invariants actifs (verrou GRAND_PLAN mission1)
- SYMBOL_ALLOWLIST = (GOLD#, BTCUSD#) — tout autre symbole → [SYMBOL_BLOCKED]
- Lot fixe 0.01, magic 909002, SL/TP obligatoires
- MAX_OPEN = 1 PAR symbole, kill-switch partagé (6 pertes/jour, DD 3%)
- Exit V2 = autorité de sortie unique GOLD#+BTCUSD# (parasites QUICK_EXIT/Smart Rescue neutralisés)
- HERMES_LOG_FILE permanent (logs/hermes.log, RotatingFileHandler)
- Watchdog indépendant actif (tâche planifiée HERMES_WATCHDOG, mode urgence OFF par défaut)

## Métriques cumulées (dataset, tout-temps)
- Trades clôturés enregistrés : 22 (objectif pilote 50)
- P&L net cumulé : -69.06 USD
- Taille dataset : 1942 lignes

## Alertes ouvertes (watchdog, dernières CRITIQUE/HAUTE)
- 2026-07-08T04:19:03.569182+00:00 [HAUTE] BOT_STALLED :: Bot possiblement mort : aucun fichier modifié depuis 11 min (> 10 min, heures de marché)
- 2026-07-08T04:20:03.556748+00:00 [HAUTE] BOT_STALLED :: Bot possiblement mort : aucun fichier modifié depuis 12 min (> 10 min, heures de marché)

## Actions humaines (RÉSERVÉ SIMO) en attente
- Remote git privé configuré (voir `git remote -v`) — backup quotidien pousse automatiquement.
- Décider de l'activation du MODE URGENCE du watchdog (watchdog/.env, WATCHDOG_EMERGENCY_CLOSE).
- Configurer Telegram (watchdog/.env) pour recevoir les alertes watchdog + le bilan quotidien en push.