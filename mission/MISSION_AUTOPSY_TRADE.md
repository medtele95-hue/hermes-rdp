# MISSION : AUTOPSIE DU TRADE −20.08 $ (GOLD# BUY, ticket 379832386) — LECTURE SEULE

Question de SIMO : « je ne comprends pas ce genre de perte ». Objectif : expliquer CE trade
précis, chiffres à l'appui — pourquoi l'entrée, ce qu'a fait Exit V2 pendant sa vie, et si
la sortie est cohérente. On EXPLIQUE, on ne corrige rien.

═══════════════════════════════════════════════════════════════
CONTRAINTES ABSOLUES
═══════════════════════════════════════════════════════════════
1. **LECTURE SEULE.** Aucune modification de code, de seuil, d'exit. Aucun order_send.
   Bot ni arrêté ni redémarré. DECISION_DATASET non touché (lecture uniquement).
2. Script isolé si besoin : `tools/autopsy_379832386.py`. Livrable unique dans reports/.
3. GEL INTACT : cette mission ne débouche sur AUCUN changement, quelles que soient les
   conclusions. Les découvertes vont dans le rapport, point.

═══════════════════════════════════════════════════════════════
LE TRADE (d'après l'historique MT5 de SIMO)
═══════════════════════════════════════════════════════════════
- GOLD# BUY 0.01, ticket 379832386, magic 909002
- Ouvert 2026-07-10 07:13:07 (heure broker UTC+3 → ~04:13 UTC, session ASIE)
- Entrée 4119.74 · SL 4099.68 · TP 4148.86
- Fermé 2026-07-10 11:37:11 à 4099.66 → −20.08 $ (SL touché, ~4h24 de vie)

═══════════════════════════════════════════════════════════════
À RECONSTRUIRE (depuis DECISION_DATASET + hermes.log + deals/ticks MT5)
═══════════════════════════════════════════════════════════════
**A. L'ENTRÉE — pourquoi le système a pris ce BUY :**
- La ligne de décision complète : score confluence + décomposition (geo/SMC/MTFA/OF),
  grade, stratégie, EES_buy (score + bande) au moment de l'entrée, session, kill_zone,
  ATR percentile, spread, momentum_alignment, DXY.
- Le RR à l'entrée : TP 29.1 pts / SL 20.1 pts ≈ 1.45 — sous le gate RR_BELOW_1_5 ?
  Vérifier comment ce trade a passé le gate RR (calcul net_RR ? arrondi ? autre formule ?).
  Si le RR loggé ≥1.5, montrer le calcul exact du système. C'est le point le plus
  intéressant de l'autopsie.
- Le SL : 20 pts — cohérent avec quelle règle de placement (ATR × k ? structure ?) ?
  Noter l'ATR au moment de l'entrée pour situer sl_dist_atr.

**B. LA VIE DU TRADE (07:13 → 11:37 broker) — Exit V2 a-t-il eu sa chance :**
- Depuis les bougies M1 GOLD# : le MFE (excursion max favorable) — jusqu'où le trade
  est-il allé en profit avant de retourner ?
- Le breakeven Exit V2 s'est-il armé ? (logs [EXIT_V2] pour ce ticket : be_armed
  true/false, à quelle heure, à quel prix). Si jamais armé : le MFE a-t-il atteint le
  seuil d'armement ou pas ? (Si le prix n'a jamais assez monté → Exit V2 n'a rien à se
  reprocher, le trade était mauvais dès le départ. Si le MFE a dépassé le seuil et que
  BE ne s'est pas armé → LÀ il y a un sujet, à documenter précisément.)
- Le trailing a-t-il été actif à un moment ?

**C. LA SORTIE :**
- Close 4099.66 vs SL 4099.68 : 0.02 pt d'écart = slippage/spread normal ? Confirmer
  que c'est bien un BROKER_SL propre (pas un exit logiciel).
- Cross-check signe/valeur : P&L deals MT5 = −20.08 confirmé (règle du ticket 828907072).

**D. LE CONTEXTE (pour situer, pas pour juger) :**
- Ce trade dans la distribution : sl_dist_atr de ce trade vs les autres trades GOLD
  du dataset. Entrée en session ASIE à 04:13 UTC — combien de trades GOLD du dataset
  sont entrés en Asie, et leur win rate vs Londres/NY ? (Descriptif, pas de conclusion
  sur n petit.)
- Les 4 pertes BTC du même jour : confirmer en une ligne qu'elles suivent le pattern
  connu be_armed=False → BROKER_SL (chantier n°4 déjà noté, rien de neuf attendu).

═══════════════════════════════════════════════════════════════
LIVRABLE : `AUTOPSY_379832386.md`
═══════════════════════════════════════════════════════════════
Structure : ① le trade en 5 lignes ; ② l'entrée (features + le point RR 1.45 vs gate) ;
③ la vie (MFE, be_armed oui/non et pourquoi) ; ④ la sortie (SL propre confirmé) ;
⑤ verdict en 3 phrases MAX : « perte normale par conception » OU « anomalie précise
trouvée : [laquelle, où dans le code, SANS la corriger] ». Afficher le verdict dans le
terminal. Confirmer git status intact côté code.

Rappel : si une anomalie sort (ex. gate RR contourné), on la DOCUMENTE pour la
watch-list — on ne la corrige pas pendant le gel.
