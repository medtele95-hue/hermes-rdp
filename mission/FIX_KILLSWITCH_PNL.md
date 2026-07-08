ultrathink

URGENT : CONTRADICTION P&L — LE KILL-SWITCH A COUPÉ LE TRADING SUR UN CHIFFRE FAUX

Constat prouvé par les logs du 2026-07-08 :
- [CYCLE_SUMMARY] closed_pnl=13.28 floating_pnl=0.0 (répété sur tous les cycles) → le P&L RÉEL du jour est +13.28 (POSITIF), cohérent avec l'historique MT5 de SIMO (14 gagnants, 3 perdants -2.45/-8.57/-1.52, net positif).
- [DAILY_KILLSWITCH] triggered=True losses=6/6 daily_pnl=-37.14 → le kill-switch a coupé le trading en pensant que la journée est à -37.14.

CONTRADICTION DIRECTE : deux modules du même bot calculent un P&L du jour opposé (+13.28 vs -37.14). Le kill-switch s'est déclenché (6/6, trading arrêté) sur un chiffre FAUX alors que la journée est en réalité profitable. C'est probablement de la même famille que la divergence P&L historique (+38 affiché vs -104 réel de la semaine passée).

Safepoint git avant. Tests après. C'est prioritaire : le kill-switch bloque à tort le trading d'une journée gagnante.

DIAGNOSTIC :
1. Compare les DEUX calculs de P&L du jour, côte à côte, sur la même fenêtre broker [2026-07-07T21:00 -> now] :
   - Celui de CYCLE_SUMMARY (closed_pnl=13.28) : d'où vient-il ? Quelle source, quels deals ?
   - Celui du kill-switch (daily_pnl=-37.14) : d'où vient-il ? Quelle source, quels deals, quel comptage ?
2. Identifie POURQUOI ils divergent de ~50$. Hypothèses à tester :
   - Le kill-switch compte les pertes en valeur ABSOLUE sans soustraire les gains (compte 6 pertes brutes au lieu du net)
   - Le kill-switch et CYCLE_SUMMARY lisent des sources différentes (l'un les deals MT5 history, l'autre un compteur interne / le dataset / un cache)
   - Un problème de fenêtre : le kill-switch inclut des deals hors de la fenêtre du jour, ou double-compte
   - Un problème de signe (les swaps/commissions comptés du mauvais côté)
   - Le "losses=6/6" compte-t-il des trades qui ne sont PAS des pertes nettes ? (ex : un trade fermé à +0.10 mais après avoir été négatif ?)
3. Vérifie précisément : QUELS sont les 6 trades que le kill-switch considère comme "pertes" ? Liste-les avec leur P&L réel MT5. Si certains sont en réalité gagnants ou neutres, le compteur de pertes est cassé.

FIX :
- Le kill-switch DOIT utiliser la MÊME source de vérité que CYCLE_SUMMARY : les deals MT5 fermés (history_deals_get) dans la fenêtre broker du jour, P&L NET réel (profit + swap + commission).
- Le compteur de "pertes" doit compter les trades dont le P&L NET réel est < 0, pas une autre définition.
- Le daily_pnl du kill-switch doit égaler exactement le closed_pnl de CYCLE_SUMMARY (même fenêtre, même source). Les deux doivent afficher +13.28 aujourd'hui.
- Log de contrôle : afficher côte à côte cycle_summary_pnl et killswitch_daily_pnl à chaque évaluation, avec une ASSERTION qui alerte si l'écart dépasse 0.01 (détection automatique de toute divergence future).

CONSÉQUENCE IMMÉDIATE : si le fix confirme que la journée est à +13.28 avec moins de 6 vraies pertes, le kill-switch ne devrait PAS être déclenché → le bot doit pouvoir retrader aujourd'hui (dans les limites du vrai quota). Documenter l'état après fix.

TEST D'INVARIANT : simuler une journée avec 14 gagnants et 3 perdants (net positif) → le kill-switch compte 3 pertes (pas 6), daily_pnl positif, PAS de déclenchement. Simuler 6 vraies pertes nettes → déclenchement correct.

LIVRABLE : la source exacte de chaque calcul, la cause de la divergence de 50$, la liste des "6 pertes" avec leur vrai P&L, le fix unifiant les deux sur les deals MT5, et l'état du kill-switch après correction (déclenché à tort ou vraiment 6 pertes ?).
