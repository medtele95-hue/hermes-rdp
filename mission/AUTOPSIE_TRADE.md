ultrathink

MISSION : RAPPORT FORENSIQUE COMPLET DU TRADE GOLD# TICKET 377299478 (READ-ONLY)

SIMO veut comprendre ce setup en profondeur. Trade : GOLD# SELL, ticket 377299478, ouvert 2026-07-08 18:30:49, fermé 20:50:20 (~2h20), entry 4024.17, SL 4162.55, TP 3821.85, fermé à 4067.02, P&L -42.85 (-1.06%).

Produis reports/AUTOPSIE_377299478.md — reconstitution COMPLÈTE depuis les logs (logs/hermes.log), le dataset (app/data/decision_dataset.jsonl) et les deals MT5. AUCUNE modification de code.

═══════════════════════════════════════
1. LA DÉCISION D'ENTRÉE (pourquoi ce trade a été pris)
═══════════════════════════════════════
- Stratégie émettrice exacte + timestamp de la décision.
- Le contexte marché au moment T : h4_bias, h1_bias, régime (ATR percentile), session, kill_zone active ?
- La confluence : score final + décomposition des 4 composantes normalisées (geo/smc/mtfa/of) avec leurs poids — quelle composante a fait passer ce trade ?
- Les bandes EES au moment de l'entrée : ees_sell et ees_buy (valeur + band). L'EES-SELL a-t-il pénalisé ? (un SELL doit être vérifié pour l'épuisement vendeur)
- momentum_alignment : le SELL était-il ALIGNED / NEUTRAL / AGAINST le momentum M1/M5 ?
- Les 7 sens : DXY (montait/baissait ?), niveaux D1/W1 proches, canari minières, divergences.
- Verdict : cette entrée était-elle géométriquement/statistiquement défendable, ou un setup faible qui a passé les gates ?

═══════════════════════════════════════
2. LA GÉOMÉTRIE SL/TP (le SL de 138 points est-il normal ?)
═══════════════════════════════════════
- Comment le SL 4162.55 a été calculé (138 points d'entry) ? C'est 2-3x plus large qu'un SL GOLD typique. Basé sur quoi (ATR ? structure ? sweep) ?
- L'ATR au moment de l'entrée (Wilder, cœur v2) : le SL était-il un multiple raisonnable de l'ATR, ou anormalement large ?
- Le RR : (entry-TP)/(SL-entry) = (4024.17-3821.85)/(4162.55-4024.17) = 202.32/138.38 = RR ~1.46. Cohérent avec le design (RR>=1.0) mais avec un TP TRÈS loin (202 points).
- Le risque réel : 138 points x valeur du point x 0.01 lot / équité = quel % ? Comment ce trade a-t-il passé le risk check C4 (cap 10%) avec un SL si large ? Vérifie le log [LIVE_RISK_CHECK] de ce trade.

═══════════════════════════════════════
3. LA VIE DU TRADE (2h20 de tenue)
═══════════════════════════════════════
- Trajectoire : MFE (max favorable, le trade a-t-il été en profit ?) et MAE (max adverse).
- Exit V2 : a-t-il armé le BE ? (le trade a-t-il atteint +2$ à un moment ?) Y a-t-il eu des TRAIL_MOVE ? Tous les événements [EXIT_V2] pour ce ticket.
- Le prix a atteint 4067 (perte) mais le SL était à 4162 — le trade n'était donc PAS près du SL.

═══════════════════════════════════════
4. LA FERMETURE (LA question clé)
═══════════════════════════════════════
- QUI a fermé ce trade à 4067.02 / -42.85 ? Ce n'est PAS le SL (4162) ni le TP (3821). Options :
  * Exit V2 (une clôture de protection ?) → improbable sur un perdant non armé
  * Le kill-switch / une fermeture forcée de fin de journée
  * WEEKEND_FLAT (on est mercredi, non applicable) ou NEWS_FLAT (FOMC ? mais c'était à 18h UTC)
  * Une fermeture manuelle de SIMO (reason=CLIENT dans le deal MT5 ?)
  * Un closer résiduel
- Cherche le deal de fermeture MT5 et sa raison, + le log au moment de 20:50:20.
- Si c'est une fermeture automatique inattendue → c'est peut-être un bug ou un comportement à comprendre.

═══════════════════════════════════════
5. VERDICT ET LEÇON
═══════════════════════════════════════
- Ce trade était-il : (a) un bon setup malchanceux, (b) un setup faible qui n'aurait pas dû passer, (c) une entrée correcte mais un SL mal dimensionné, (d) une fermeture anormale ?
- MFE/MAE : mort dès l'entrée (MAE immédiat) ou d'abord favorable ?
- Y a-t-il quelque chose à corriger (RÉSERVÉ SIMO si ça touche un seuil/la logique) ou est-ce une perte normale dans les stats ?

LIVRABLE : le rapport complet + bottom-line 6 lignes : pourquoi ce trade a été pris, pourquoi le SL si large, qui l'a fermé et pourquoi à -42.85, et si c'est normal ou anormal.
