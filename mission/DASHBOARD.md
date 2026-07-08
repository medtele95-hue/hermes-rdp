ultrathink

MISSION : DASHBOARD DE CONTRÔLE HERMES — ÉCRAN + TÉLÉCOMMANDE SÉCURISÉE SUR TÉLÉPHONE

Décision SIMO : le dashboard n'est plus lecture seule — c'est une salle de contrôle mobile avec actions. Sécurité maximale exigée : Tailscale obligatoire (jamais d'exposition publique), PIN pour toute action, double confirmation pour le réel, journal complet des actions.

Safepoint git avant. Tests après. Le dashboard est un process séparé (s'il meurt, le bot ne le sent pas).

═══════════════════════════════════════
ÉTAPE 1 — BACKEND FastAPI (lecture + actions)
═══════════════════════════════════════
ENDPOINTS LECTURE (JSON, cache 10-30s) :
- /api/status : équité, balance, floating, positions ouvertes (symbole, sens, entry, floating, état Exit V2 : armed/peak/lock, distance SL/TP)
- /api/today : trades du jour (deals MT5 réels : P&L, mode de fermeture), refus par type (EES/news/abort/killswitch/symbol)
- /api/journal : JOURNAL DE TRADES complet paginé — chaque trade historique : date, symbole, sens, entry/exit, SL/TP, P&L réel, durée, mode de fermeture, stratégie émettrice, confluence à l'entrée, bandes EES à l'entrée (croiser deals MT5 + dataset). Filtres : date, symbole, gagnant/perdant.
- /api/system : kill-switch x/quota + DD, heartbeats bot/watchdog, core_version, dernier backup, alertes ouvertes, MODE ACTUEL (compte connecté : demo/real, login masqué, symboles actifs)
- /api/senses : EES deux bords (valeur+bande), DXY trend, ATR percentile, session, prochain news HIGH

ENDPOINTS ACTIONS (POST, tous protégés — voir Étape 3) :
- /api/action/bot/stop : arrêt PROPRE du bot (signal au process, les positions ouvertes restent avec leur SL/TP broker et Exit V2 s'arrête de trailer — documenter ce comportement clairement dans l'UI : "les positions gardent leur SL/TP serveur")
- /api/action/bot/start : démarrage via le superviseur existant (HERMES_LOG_FILE garanti)
- /api/action/bot/restart
- /api/action/symbols : activer/désactiver GOLD# et/ou BTCUSD# dans l'allowlist runtime (écrit dans la config, le bot la recharge — implémenter un rechargement de l'allowlist sans redémarrage si possible, sinon restart automatique). Toujours au moins 1 symbole actif ou bot en pause.
- /api/action/account/switch : bascule demo ↔ réel. MÉCANIQUE : écrit le profil de connexion cible dans la config (les DEUX profils — demo actuel et réel déclaré — sont prédéfinis dans account_profiles.env, JAMAIS saisis depuis le dashboard), arrête le bot proprement, vérifie qu'aucune position n'est ouverte sur le compte quitté (sinon REFUS avec message "fermez ou attendez la clôture des positions"), redémarre sur le compte cible. Le pin preflight REAL reste souverain : si le réel n'est pas le compte déclaré, le bot reste flat — le dashboard ne peut RIEN outrepasser.
- Chaque action → log [DASHBOARD_ACTION] action=.. result=.. + notification Telegram immédiate si configuré + entrée dans un fichier dashboard/actions_audit.log (append-only).

═══════════════════════════════════════
ÉTAPE 2 — LA PAGE MOBILE (une page, mobile-first, auto-refresh 30s)
═══════════════════════════════════════
Sections dans l'ordre :
① Bandeau : équité + P&L jour + BADGE DE MODE bien visible (DEMO vert / RÉEL rouge vif — impossible de se tromper de compte d'un coup d'œil)
② Positions ouvertes avec état Exit V2
③ PANNEAU DE CONTRÔLE : boutons Start/Stop/Restart (état du bot visible : RUNNING/STOPPED/HALTED-killswitch) ; toggles GOLD ✓/✗ et BTC ✓/✗ ; bouton "Basculer vers RÉEL/DEMO" (rouge, à part)
④ Compteurs jour (trades G/P, kill-switch x/quota, DD)
⑤ Les sens (EES bandes colorées, DXY, régime, session, prochain news)
⑥ JOURNAL DE TRADES : liste scrollable des N derniers trades (date, symbole, sens, P&L coloré, mode de fermeture, stratégie) + filtres simples (tous/gagnants/perdants, GOLD/BTC) + tap sur un trade → détail complet (confluence, EES, exec quality)
⑦ Derniers événements système (10 lignes)
⑧ Santé (bot/watchdog/backup/core_version)

═══════════════════════════════════════
ÉTAPE 3 — SÉCURITÉ (non négociable)
═══════════════════════════════════════
1. Tailscale : installer, configurer ; le serveur n'écoute QUE sur localhost + interface Tailscale. Preuve qu'aucun port public n'est exposé. Guide 3 étapes téléphone pour SIMO (app Tailscale + même compte + URL, ajout à l'écran d'accueil).
2. PIN à 6 chiffres (dashboard/.env, hashé) exigé pour TOUTE action POST. Saisie dans l'UI à chaque action (pas de session mémorisée). 5 échecs → actions verrouillées 15 min + alerte Telegram.
3. Bascule vers RÉEL : PIN + taper le mot REAL en toutes lettres dans un champ de confirmation + rappel à l'écran de l'équité du compte cible. Bascule vers DEMO : PIN seul.
4. La lecture reste accessible sans PIN (c'est l'écran) ; seules les actions sont protégées.
5. Rate limiting sur les endpoints d'action (1 action / 5 secondes).

═══════════════════════════════════════
ÉTAPE 4 — INTÉGRATION
═══════════════════════════════════════
- Tâche planifiée HERMES_DASHBOARD (boot + restart si mort, max 3/heure).
- Le watchdog vérifie son heartbeat (alerte INFO si mort).
- Le stop/start du bot via dashboard doit coexister proprement avec le superviseur (le superviseur ne doit PAS relancer un bot que SIMO a explicitement arrêté depuis le dashboard — flag d'état intentionnel stopped_by_user, respecté par le superviseur, levé par le start).

LIVRABLE : DASHBOARD_REPORT.md — URL Tailscale exacte, guide téléphone 3 étapes, PIN par défaut à changer, démonstration de chaque action avec son log d'audit, preuve zéro exposition publique, preuve que le pin preflight REAL reste souverain, et le comportement documenté du stop (positions conservent SL/TP broker).
