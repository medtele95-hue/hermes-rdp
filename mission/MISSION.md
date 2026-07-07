ultrathink

═══════════════════════════════════════════════════════════════
MISSION MAÎTRE : RÉSURRECTION COMPLÈTE HERMES — NOUVEAU SERVEUR
═══════════════════════════════════════════════════════════════

CONTEXTE : L'ancien serveur est mort. Base disponible : copie du 26 JUIN dans C:\Users\Admin\Documents\hermes-mt5-agent — ANTÉRIEURE à une semaine de fixes critiques (30 juin → 7 juillet) qui ont transformé un système incapable de trader en système complet et rentable en sessions LDN/NY. MT5 est installé et connecté (compte demo XM). Tu as mandat complet : diagnostique, restaure ou reconstruis, vérifie, itère jusqu'au système complet. Chaque fix ci-dessous a été VALIDÉ EN PRODUCTION la semaine dernière — implémente fidèlement, adapte les noms aux fichiers réels de la base.

MÉTHODOLOGIE INVARIABLE : repo git propre dès le départ ; safepoint (commit+tag) AVANT chaque bloc ; suite de tests verte APRÈS chaque bloc ; un rouge = rollback du bloc ; heure BROKER explicite partout (3 bugs timezone historiques dans ce projet) ; fail-closed par défaut.

═══════════════════════════════════════
PHASE 0 — FORENSIQUE (avant toute modification)
═══════════════════════════════════════
1. git remote -v, git log --oneline -20, git tag dans la copie. Si un remote existe → fetch → chercher les tags : fix-mtf-structure-v1, exit-v2-activated-20260703, ks-policy-done, hui-done. S'ils existent → VOIE A (restauration directe du dernier état + .env recréé + tests + PHASE FINALE). Sinon → VOIE B (reconstruction, blocs 1-11).
2. Environnement : Python + dépendances, MT5 via account_info (trade_mode/login/équité), symboles présents : GOLD#, NEM.N, B.N, EURUSD/USDJPY/GBPUSD. Horloge Windows NTP.
3. Inventaire de la copie : fichiers modifiés APRÈS le 26 juin ? (reports/, data/, .env, logs) — tout fragment de la semaine perdue est précieux, liste-le.
4. SÉCURITÉ : la copie contient un double-pin compte live → elle reste flat sur ce demo. Diagnostic possible, mais elle ne trade pas telle quelle.

═══════════════════════════════════════
VOIE B — RECONSTRUCTION (11 blocs, ordre strict)
═══════════════════════════════════════

■ BLOC 1 — DÉTECTEURS CASSÉS (la cécité originelle)
a) MTF_STRUCTURE (~mtf_structure_detector.py L125-130) : bug prouvé = _direction exige bias=BULLISH (prix près des HAUTS) ET zone=SUPPORT (prix au PLUS-BAS) → mutuellement exclusifs → WAIT permanent (5943/5943 mesuré), m15_confirmation/m1_entry court-circuités, score plafonné 50. FIX : BULLISH = structure H4 haussière (HH/HL) + prix dans/retour vers zone DEMAND ou pullback vers support tenu ; BEARISH miroir (LL/LH + SUPPLY) ; WAIT si réellement contradictoire. m15/m1 atteignables, score sur toute l'échelle, log [MTF_DIR]. Test : breakout haussier textbook → BULLISH.
b) SMC_TAGGER (~L304-319,409) : breakout mèche simultané 3 TF = impossible (0/5943). FIX : 2 TF sur 3, close-corps OU mèche. PASS 60 inchangé.
c) Tolérances ATR-relatives : h4_zone ($8 fixe → k×ATR_H4), SFP en fraction d'ATR.

■ BLOC 2 — GÉOMÉTRIE SLTP + ANTI-PENNY-GRAB
Bug prouvé _calc_sltp : SL ancré VAL/VAH±buffer → un sweep au-delà du niveau met le SL du mauvais côté → rejet des meilleurs setups. FIX : min(val,price)-buffer / max(vah,price)+buffer. + PLANCHER RR≥1.0 au choke-point final, APRÈS toute modif de TP, tous chemins. + TUER max_tp_usd (max_money_tp_enabled=false, GOLD hard-exclu — il produisait TP 5$/SL 69$, RR 0.07). Log [OF_SLTP] avec verdict.

■ BLOC 3 — ADAPTIVE_ACCOUNT_POLICY (le mur des 1000 candidats)
Trois niveaux via trade_mode MT5, appliqués au BOOT ET PAR-ORDRE (trading_authorized — les deux étages, c'était le piège historique) :
- DEMO : aucun pin, politique complète, exploration/fallbacks EXÉCUTABLES (le garde exploration-shadow ne vaut que pour REAL).
- REAL_DECLARED (login+serveur=config) : politique stricte, exploration shadow.
- REAL_UNKNOWN : trade autorisé ultra-prudent (cap 2%, volume_min, kill-switch 1/jour, confluence ≥80).
Fail-closed → niveau 3. Module ACCOUNT_PROFILE au boot (specs réelles symbol_info, SL max finançable, TRADABLE, zéro hardcode). Logs [ACCOUNT_PROFILE]/[ORDER_AUTH]/[ADAPTIVE_POLICY].

■ BLOC 4 — EXIT V2 ACTIF + MORT DU PARASITE
Parasite historique HERMES_QUICK_EXIT (TP money 1.50$, lock-SL 0.80$ sournois) = 100% des sorties. FIX : skip total sur GOLD (couvrant TP money, lock-SL, trailing, dynamic exit) + Exit V2 ACTIF via le routeur (autorité unique), gate compte (DEMO=actif, REAL=shadow), design CLOSE-BASED (aucun chemin SL-modify → SL jamais élargi PAR CONSTRUCTION), BE armé +2.00$ (plancher BE+0.10), trailing 1.20$ sous le peak. .env : MODE=ACTIVE, TP_USD=0, TRAIL_START=2.0, GAP=1.2. Fail-closed : exception → SL/TP d'origine. Chaque action [EXIT_V2] → dataset.

■ BLOC 5 — EES-BUY ACTIF (tueur de chasse-sommets, validé sur un −40.86 réel à 73.7 EXTREME)
Miroir EXACT du chemin EES-SELL : garde direction=="BUY" dans setup_hunter._candidate, gradué (SAIN<40 rien, PRUDENCE 40-65 → −15 max, EXTREME ≥65 → EES_EXTREME_BLOCK), plomberie evaluate→_candidate. Chemin SELL INTOUCHÉ (test byte-identique). Observabilité : side=BUY|SELL partout, sell_score/buy_score→of_score.

■ BLOC 6 — KILL-SWITCH BLINDÉ
a) Fenêtre jour BROKER [minuit serveur, now+2h] — stamps UTC+3 jamais « dans le futur ». Reset 21:00 UTC. Test replay du blind-spot.
b) Compteur = relecture historique deals broker à CHAQUE évaluation (rien en mémoire) → survit à tout restart. Test restart ×2.
c) Quotas par politique : DEMO 6 pertes/jour + 3% DD (le premier stoppe) ; REAL_DECLARED 3/jour ; REAL_UNKNOWN 1/jour. Tag account_policy au dataset.

■ BLOC 7 — CALENDRIER PROTÉGÉ
a) Flat-weekend : aucune entrée après ven 18:00 UTC ; TOUT fermé ven 20:30 UTC ([WEEKEND_FLAT]).
b) Blackout post-weekend : aucune entrée dimanche → lundi 03:00 UTC ([POST_WEEKEND_BLACKOUT], refus au dataset avec outcome virtuel).
c) Bouclier news : flux ForexFactory hebdo, cache local, refresh quotidien+boot, FAIL-SAFE (flux mort → warning, trading continue). HIGH impact USD : aucune entrée [event−10, event+10] ([NEWS_BLACKOUT]) ; positions NON-armées fermées 10 min avant les majeurs (NFP/FOMC/CPI configurables) ; positions armées gardent leur lock. Timezone calendrier→broker testée.

■ BLOC 8 — EXÉCUTION AU TICK
Requête sur symbol_info_tick à l'INSTANT de l'envoi (BUY@ask, SELL@bid), deviation=50 pts. ABORT si fuite >300 pts depuis validation OU RR au tick <1.0 → [ORDER_ABORT_PRICE_MOVED], refus au dataset. [EXEC_QUALITY] par ordre : slippage_vs_tick, slippage_vs_request, spread_at_send, fill_latency → dataset.

■ BLOC 9 — DECISION_DATASET COMPLET
data/decision_dataset.jsonl append-only, schema_version. Chaque décision (exécutée OU refusée+outcome virtuel) : ~200 features (scores/composants, flags SMC/MTF, of_score, atr, spread_to_atr, net_RR...), ees_sell+band, ees_buy+band, exec quality, account_policy, session, régime (h4_bias, atr_percentile fenêtre 5000, kill_zone_active), momentum_alignment (3 votes : 5 dernières M1 fermées + signe cvd_slope + signe delta ; ≥2 = BULL/BEAR ; manquant vote 0 → NEUTRAL ; puis ALIGNED/NEUTRAL/AGAINST — SHADOW PUR, zéro effet décisionnel). Outcome tracker : MFE/MAE + pnl réconcilié deals MT5. Fail-silent intégral.

■ BLOC 10 — SUPER-EYES (features pures, zéro effet décisionnel)
a) CVD tick réel : copy_ticks_from GOLD#, tick-rule midprice, session+slopes m5/m15+divergence. Budget <200ms (attendu ~10ms).
b) DXY proxy ICE (pas d'USDX chez XM) : EURUSD/USDJPY/GBPUSD 57.6/13.6/11.9 renormalisé → change m15/h1, trend, gold_dxy_divergence.
c) Niveaux D1/W1 : PDH/PDL/PDC, daily/weekly open, week H/L, distances signées pts+ATR.
d) Canari minières : NEM.N + B.N composite équipondéré → hui_change_h1/d1, ratio_trend, divergence+sens (extrêmes stricts ; limite heures US documentée).

■ BLOC 11 — HYGIÈNE + RÉSILIENCE
a) Anti-boucle [POSITION_CLOSED] : recherche deal correcte, idempotence (set persistant), fallback provisoire corrigé par le deal réel — pnl RÉEL au dataset.
b) POSITION_SYNC lecture MT5 directe (par magic), appels Lovable purgés (flag off).
c) BRIDGE : chercher l'EA HermesBridge (.mq5/.ex5) et /api/mt5/poll-commands — rôle exact ? Si résidu de l'ancien monitoring → NE PAS réinstaller, documenter. Si utile → clé propre + guide 3 étapes pour SIMO.
d) BACKUP AUTOMATIQUE (leçon de la panne, OBLIGATOIRE) : remote git privé (créer si absent, documenter), push auto quotidien + après chaque tag, copie quotidienne data/*.jsonl+.env+reports/ vers dossier daté, tâche planifiée Windows, + instruction écrite pour SIMO (copie Drive hors-machine).
e) Verrou anti-double-instance : le bot refuse de démarrer si une instance tourne déjà (même compte+magic).

═══════════════════════════════════════
PHASE FINALE — BOOT DE VÉRIFICATION (preuve de chaque étage)
═══════════════════════════════════════
[ACCOUNT_PROFILE] demo détecté TRADABLE · [ADAPTIVE_POLICY] DEMO · [MTF_DIR] émet des directions (plus jamais 100% WAIT) · [OF_SLTP] VALID · [MARKET_EYES] 7 sens vivants · [NEWS_CALENDAR] chargé (prochains HIGH USD visibles) · [DAILY_KILLSWITCH] cohérent avec les deals du jour · Exit V2 ACTIVE · [EXEC_QUALITY] armé · dataset qui écrit · un cycle complet suivi · suite intégrale verte.

═══════════════════════════════════════
LIVRABLE : RESURRECTION_REPORT.md
═══════════════════════════════════════
1. Voie empruntée (A/B) + fragments post-26-juin trouvés dans la copie.
2. Tableau bloc par bloc : commit, tag, tests, preuve de fonctionnement.
3. Verdict bridge + backup en place et testé.
4. Ce qui est définitivement perdu (dataset historique ?).
5. Boot complet prouvé (les logs de chaque étage).
6. Bottom-line 8 lignes : état vs le 7 juillet au soir, et GO/NO-GO pour reprendre la phase collecte.
