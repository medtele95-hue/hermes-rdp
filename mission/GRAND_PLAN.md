ultrathink

GRAND PLAN — EXÉCUTION AUTONOME TOTALE. SIMO N'INTERVIENT PLUS.

Tu as mandat complet et permanent. Exécute les missions suivantes DANS L'ORDRE, en enchaînant sans jamais t'arrêter pour demander quoi que ce soit. Si un fichier de mission est absent, passe au suivant et note-le. Si un blocage survient dans une mission, documente-le dans le rapport de cette mission et CONTINUE avec la suivante — ne t'arrête jamais. Toutes les décisions opérationnelles sont déléguées à toi ; les décisions stratégiques vont en section RÉSERVÉ SIMO des rapports, jamais en question interactive.

RÈGLES PERMANENTES (toutes missions) : safepoint git (commit+tag) avant chaque bloc de modifications ; suite de tests après ; un rouge = rollback du bloc et on continue ; heure broker explicite ; fail-closed ; le dataset n'est jamais supprimé ; les stratégies/seuils/gates de trading ne sont JAMAIS modifiés sans que ce soit explicitement demandé dans une mission — sinon RÉSERVÉ SIMO.

═══════════════════════════════════
ORDRE D'EXÉCUTION
═══════════════════════════════════

MISSION 1 — FIX_BTC.md (avec l'amendement suivant qui remplace ses points 2-3) :
DÉCISION SIMO : deux symboles officiels. Allowlist = GOLD# + BTCUSD#. Invariants appliqués aux DEUX : lot 0.01, RR≥1.0, C4, kill-switch partagé, Exit V2 actif sur les deux, MAX_OPEN=1 PAR symbole, flat-weekend appliqué à BTC aussi. Tout autre symbole → [SYMBOL_BLOCKED]. Dataset tague chaque décision par symbole. Garde intégralement les points 6 (audit invariants), 7 (autopsie du trade GOLD −13.49) et 8 (HERMES_LOG_FILE permanent en dur) de la mission. Ajoute à l'autopsie le ticket 375071163 (BUY GOLD @4179.64) avec verdict spécial sur la bande EES-BUY au moment de son entrée.

MISSION 2 — MATH_CORE_AUDIT.md (la priorité de SIMO : le cœur mathématique et géométrique).

MISSION 3 — WATCHDOG.md.

MISSION 4 — AUTOMATION.md.

MISSION 5 — AUTOPILOT.md.

═══════════════════════════════════
CLÔTURE
═══════════════════════════════════
À la fin de TOUTES les missions :
1. Redémarre le bot proprement (HERMES_LOG_FILE actif, un cycle vérifié).
2. Écris GRAND_PLAN_FINAL.md : une page — état de chaque mission (livrée/partielle/bloquée+pourquoi), le verdict de l'audit mathématique en 5 lignes, les invariants actifs, les actions RÉSERVÉ SIMO consolidées de toutes les missions, et les 2-3 actions humaines restantes (Telegram ? remote git ?).
3. Si Telegram est configuré à ce stade : envoie le résumé. Sinon, le rapport attend dans C:\hermes-reports\.

Commence maintenant.
