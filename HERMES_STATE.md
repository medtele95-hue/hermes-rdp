# HERMES_STATE — état vivant
_Mis à jour automatiquement le 2026-07-08T04:28:51.220202+00:00 par scripts/update_hermes_state.py_

## Version
- Branche : `feature/geo-confluence-hardening`
- Derniers tags :
  - mission2-math-core-audit
  - mission3-watchdog
  - safepoint-pre-math-core-audit
  - mission1-fix-btc-allowlist
  - safepoint-grand-plan-start
  - resurrection-complete-20260707
  - bloc11-hygiene
  - hui-done
  - bloc10-super-eyes
  - safepoint-pre-bloc-11
- Derniers commits :
  - 0a8ffce mission2 (GRAND_PLAN): MATH_CORE_AUDIT - audit formule par formule, 1 bug CASSE corrige
  - c338a1b mission3 (GRAND_PLAN): WATCHDOG independant, 9 verifications, tache planifiee
  - 20d3b2e mission1 (GRAND_PLAN): pivot allowlist GOLD#+BTCUSD#, Exit V2 autorite unique, HERMES_LOG_FILE permanent
  - d8598e5 safepoint: avant GRAND_PLAN (missions 1-5, pivot allowlist GOLD#+BTCUSD#)
  - aa9a546 safepoint: verrou GOLD-only (etat intermediaire mission FIX_BTC v1, avant pivot BTC-allow)
  - f5cc776 safepoint: avant mission FIX_BTC (verrou GOLD-only)
  - 5d8323b final: RESURRECTION_REPORT + verified boot proof
  - cb9ccb4 bloc11: hygiene + resilience (anti-loop, sync purge, bridge, backup, lock)

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
- Taille dataset : 1644 lignes

## Alertes ouvertes (watchdog, dernières CRITIQUE/HAUTE)
- 2026-07-08T04:19:03.569182+00:00 [HAUTE] BOT_STALLED :: Bot possiblement mort : aucun fichier modifié depuis 11 min (> 10 min, heures de marché)
- 2026-07-08T04:20:03.556748+00:00 [HAUTE] BOT_STALLED :: Bot possiblement mort : aucun fichier modifié depuis 12 min (> 10 min, heures de marché)

## Actions humaines (RÉSERVÉ SIMO) en attente
- Remote git privé toujours à créer (voir docs/BACKUP_SIMO.md) — tant qu'il est absent, le backup quotidien alerte chaque jour.
- Décider de l'activation du MODE URGENCE du watchdog (watchdog/.env, WATCHDOG_EMERGENCY_CLOSE).
- Configurer Telegram (watchdog/.env) pour recevoir les alertes watchdog + le bilan quotidien en push.