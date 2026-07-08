# MATH_CORE_AUDIT — Mission 2 (GRAND_PLAN)
**Date** : 2026-07-08 | **Scope** : cœur mathématique/géométrique post-résurrection, post-pivot GOLD#+BTCUSD#+Exit V2 (mission1)

Cet audit part de zéro sur le code **actuel** (pas de confiance aveugle dans `audit/audit_*.md`, datés 2026-06-22, pré-résurrection). Les findings de cet ancien audit sont réutilisés uniquement après re-vérification ligne par ligne ; leur statut (toujours vrai / corrigé / obsolète) est noté partout où c'est pertinent.

---

## 1. VERDICT GÉOMÉTRIE — le fantôme, élucidé

**Correction au cadrage de la mission : il n'y a pas UN module géométrique, mais TROIS, avec des sorts différents.**

| Module | Calcule quoi | Statut réel aujourd'hui |
|---|---|---|
| `app/mt5/geometric_confluence.py` | Patterns harmoniques XABCD (Gartley/Bat/Butterfly/Crab/Shark/Cypher), Fibonacci, Gann, spirale dorée | Tourne chaque cycle, log `[GEO_SHADOW]` — **c'est LUI le `geometric_grade=D` permanent** |
| `app/mt5/geometric_engine_v2.py` | Même famille (ratios harmoniques + PRZ), version graduée avec sigmoïde | Appelé chaque cycle mais **alimenté avec des zéros en dur** (bug de plomberie, pas un mode SHADOW) |
| `app/quant/geometry_engine.py` | Canal de tendance, zone OTE, premium/discount, compression, impulsion, wedge | **DÉJÀ ACTIF EN LIVE** — alimente `_conf["score"]` (voir §3), sans lien avec les patterns harmoniques |

### Pourquoi `geometric_grade=D` permanent (module `geometric_confluence.py`)

Deux causes indépendantes qui se cumulent :
1. **Le bonus est forcé à 0** : `compute_geometric_bonus()` (`geometric_confluence.py:465-467`) retourne `0.0` dès que `mode != "ACTIVE"`. `GEOMETRIC_CONFLUENCE_MODE` vaut `"SHADOW"` par défaut (`config.py:210`) et **aucun override n'existe dans `.env`** (vérifié). Ce n'est PAS une branche morte comme le MTF originel — le calcul entier (swings, ratios, Fib, Gann, spirale) tourne réellement chaque cycle et log ses résultats ; seul le *bonus* est coupé.
2. **Le formule de score elle-même sous-récompense un pattern valide** : simulation main (voir détail complet dans le rapport de l'agent, reproductible via `python -c` sur `analyze_geometric_confluence`) — même avec un Gartley textbook (ratios exacts, qualité interne "A") détecté par le pipeline réel, le score composite plafonne à **65 (grade B, WEAK_CONFIRM)** dans le meilleur cas, et retombe à **D (WAIT)** dans le cas réaliste où le détecteur de swings (fractal, no-repaint) n'a pas encore confirmé le pivot D avant que le prix ait déjà bougé.

**Verdict : (a) sain mais jamais branché, ET la formule de score doit être révisée avant activation.**

Proposition chiffrée (RÉSERVÉ SIMO, non appliquée — seuils qui se recalibrent sur dataset) :
1. Corriger le ratio SHARK `D_XC=None` → `(0.836, 0.936)` et le plancher CRAB `D_XA` `1.568` → `1.618` (`geometric_confluence.py:68,76`) — ratios académiques manquants/trop larges, coût nul (SHADOW).
2. Revoir la formule composite (`geometric_confluence.py:681-694`) : monter le terme pattern harmonique de `45→55` pour grade A, et faire passer le cluster de zones de "3 hits→+20" à "2 hits→+20" — le triple alignement n'arrive quasiment jamais au moment exact de la confirmation du pivot.
3. Avant `ACTIVE`, backtester la formule révisée sur l'historique `[GEOMETRIC_CONFLUENCE]`/`[HARMONIC_PATTERN]` déjà loggé en SHADOW pour confirmer un taux de CONFIRM non-trivial.
4. Si activé : `GEOMETRIC_CONFLUENCE_MODE=ACTIVE` avec `GEOMETRIC_CONFIRM_BONUS` abaissé de 5.0 à 3.0 (rôle de départage, pas de signal primaire).

### `geometric_engine_v2.py` — pas juste débranché, CASSÉ

`setup_hunter._candidate_geometric_v2` (`setup_hunter.py:1116-1130`) lit des clés (`harmonic_score`, `prz_strength`, `prz_distance_atr`, `gann_confluence`, `vwap_score`) qu'**aucun code du dépôt n'écrit jamais** sous ces noms — confirmé par grep exhaustif. Résultat : `geometric_score()` reçoit systématiquement des zéros, indépendamment de `GEOMETRIC_MODE`. Aujourd'hui **inoffensif** (mode SHADOW par défaut). Mais **mine à retardement** : si `GEOMETRIC_MODE` passe un jour à `EXECUTION_FILTER`/`LIVE` (changement de config qui semble anodin), `final_trade_gate` bloquerait alors **100% des candidats** ORDER_FLOW (`NO_GEOMETRY`/`OF_GEO_INSUFFICIENT` systématiques) — l'inverse de l'effet recherché. Recommandation : réparer le branchement des clés, ou a minima interdire/alarmer si `GEOMETRIC_MODE` passe à LIVE sans données réelles.

### `geometry_engine.py` — déjà vivant, et c'est un problème d'échelle (voir §3)

Ce n'est pas "conceptuellement vide" : il tourne déjà en production et pèse pour 0-100 points, sans pondération, dans la confluence finale — voir l'incohérence dimensionnelle §3.

---

## 2. TABLEAU DES 18 (21) STRATÉGIES + NOMBRES MAGIQUES

**Préambule de portée critique** : deux verrous combinés déterminent ce qui peut réellement trader aujourd'hui sur GOLD#/BTCUSD# :
1. Verrou symbole (`demo_router.py:59`) : GOLD#+BTCUSD# uniquement.
2. Verrou rôle stratégie (`registry.py:26-56`) : seules les stratégies dans `ACTIVE_EXECUTION_STRATEGIES` ont `role=ENTRY` ; tout le reste est `OBSERVER` et ne peut jamais exécuter, quel que soit son score.

**Directement exécutables aujourd'hui** : `SIMO_ATM_BREAKOUT` (GOLD+BTC), `BTC_SCALPING_AGENT` (BTC), `GOLD_M1_M5_EMA_SWEEP_SCALPER` (GOLD), **`ORDER_FLOW_EXECUTION_AGENT`** (GOLD+BTC — `.env: ORDER_FLOW_EXECUTION_ENABLED=true`, **correction à l'audit préliminaire de l'agent qui l'avait cru désactivé par défaut config.py** ; confirmé actif par une vraie trace dataset, voir §4). Le reste n'entre qu'indirectement via `HERMES_STRATEGY_PACK_AGENT` (si un flag est levé) ou est structurellement inatteignable.

| # | Stratégie | Verdict | Note clé |
|---|---|---|---|
| 1 | SIMO_ATM_BREAKOUT | ARBITRAIRE | `confidence=65+f(ATR)*20` sans dérivation ; buffers SL/TP en points bruts (non ATR-scalés) partagés GOLD/BTC |
| 2 | BTC_SCALPING_AGENT | ARBITRAIRE | Score = constante du seuil appelant (pas de score gradué) ; pénalité `_compute_penalty` graduée mais bornes 80/50/20 non documentées |
| 3 | GOLD_M1_M5_EMA_SWEEP_SCALPER | RIGOUREUX (score) / **CASSÉ mineur** | Poids somment exactement à 100 ; mais `tp1` (scale-out 1.0R) calculé puis jamais transmis, seul `tp2` (1.5R) survit |
| 4 | GOLD_LIQUIDITY_HUNTER_PRO | ARBITRAIRE | SL = 30%×hauteur de zone (pas d'ATR, pas de cap) ; blend `0.6/0.4` étoiles non justifié |
| 5 | HERMES_STRATEGY_PACK_AGENT | ARBITRAIRE | 5 seuils empilés (75/B/1.5/55/C) sans base statistique, passthrough du reste |
| 6 | QUANT_STATISTICAL_PULLBACK | RIGOUREUX (signal+SL) / ARBITRAIRE (score) | Régression OLS + z-score + SL volatilité-scalé sains ; ~40% du score est non-discriminant (déjà garanti par les gates) |
| 7 | QUANT_PRO_REGIME_SWITCHING | RIGOUREUX / **CASSÉ** | Meilleure construction quant du dépôt (OLS/Kalman/OU/Hurst réels) MAIS deux seuils Hurst contradictoires (bonus à 0.50, hard-block à 0.90) → bonus de 15 pts non-discriminant |
| 8 | TREND_CONTINUATION_BREAKDOWN | ARBITRAIRE | Poids somment à 120 avant clamp(100), +5 inconditionnel non gated |
| 9 | CRT_TBS_REVERSAL | ARBITRAIRE | TP visé à 1.8R alors que le seuil de gate est 1.5R — cible et porte déconnectées |
| 10 | AMD_FVG_IFVG_REVERSAL | ARBITRAIRE / **CASSÉ-adjacent** | RR de gate (contexte, défaut 1.8) ≠ RR réellement implicite du SL/TP émis (ratio fixe 0.75/0.45=1.667) |
| 11 | FIB_OTE_RETEST | ARBITRAIRE | SL = swing opposé entier, aucun plafond ATR ; même déconnexion cible-1.8R/gate-1.5R |
| 12 | EMA_PULLBACK | ARBITRAIRE | Confidence fixe `0.74` (binaire, non gradué) → bonus de confirmation ailleurs binaire (+5/+0) |
| 13 | BREAKOUT_RETEST | ARBITRAIRE | Score binaire 0.68/0.42 ; entrée AU MARCHÉ malgré le nom ("retest" jamais implémenté) |
| 14 | SECOND_ENTRY | ARBITRAIRE | Score binaire 0.64/0.40 |
| 15 | SCALPING_AGENT (legacy) | ARBITRAIRE | Score binaire 0.58/0.35 |
| 16 | GOLD_ORDER_FLOW_CVD_VWAP | ARBITRAIRE / **CASSÉ** | Score = plancher +50 + comptage de booléens ; `_grade()` : palier "C" **inatteignable** (70-89 → toujours B) |
| 17 | **ORDER_FLOW_EXECUTION_AGENT** (actif, voir §4) | ARBITRAIRE / **CASSÉ (corrigé cette mission)** | Plancher +25 non conditionnel ; bonus FVG appliqué APRÈS la porte de score (incohérent avec les 4 autres bonus, appliqués avant) ; `_grade()` sans palier C ; **et le bug d'entrée FVG corrigé ci-dessous (§4)** |
| 18 | FIB_CONFLUENCE_EXECUTION_AGENT | ARBITRAIRE | Somme des poids = 110 avant clamp(100) ; terme `atr>0→+10` non-discriminant (déjà garanti) ; plafond effectif asymétrique GOLD (100) vs BTC (110, bonus volatilité additionnel) |
| 19 | GOLD_RANGE_BREAKOUT | RIGOUREUX (score) / inatteignable | Seule stratégie avec un vrai retest en 2 temps ; mais `gold_range_breakout_enabled` **n'existe pas** dans `Settings` → toujours `False`, mort par absence de câblage |
| 20 | EUR_EMA_RSI_ATR_CROSSOVER | **CASSÉ** | `confidence=1.0` inconditionnel — booléen déguisé en probabilité ; de toute façon inatteignable (EURUSD hors allowlist) |
| 21 | EXIT_V2 / HERMES_QUICK_EXIT_MANAGER | ARBITRAIRE (assumé) | Constantes USD fixes (`be_arm_usd=2.00` etc.), non scalées par symbole/volatilité — mais **documenté et intentionnel** (`exit_v2.py:1-15`), pas un oubli |

### Bugs CASSÉS consolidés (au-delà du bug de la §4)
- **F-1 (structurel)** : `_failed_gates()` contient ~40 lignes de logique de bypass pour QUANT_STATISTICAL_PULLBACK/QUANT_PRO_REGIME_SWITCHING (`setup_hunter.py:1181-1184,1424-1425`) qui ne peut **jamais** s'exécuter — ces deux stratégies sont classées OBSERVER par `registry.py`, donc `executable=False` avant même d'atteindre ce code. Code mort, pas dangereux, mais trompeur pour la maintenance.
- **F-3** : `_grade()` dans `order_flow_execution_agent.py` (90/75, pas de C) et `gold_order_flow_cvd_vwap.py` (90/80/70, C inatteignable) — palier manquant, cosmétique (le gating utilise le score brut, pas le grade) mais faux à l'affichage.
- **F-6** : `quant_pro_regime_switching.py` — bonus Hurst à 0.50 vs hard-block à 0.90 sur le même régime TREND → bonus toujours vrai quand atteint.
- **F-7** : `gold_m1m5_ema_sweep_scalper_strategy.py` — `tp1` calculé, jamais transmis à `setup_hunter`.

### Géométrie d'entrée (placement par rapport à la zone)
**Constat transversal** : à l'exception de `GOLD_RANGE_BREAKOUT` (seule stratégie avec un vrai "détecter → attendre le retest → entrer"), **toutes les stratégies entrent au marché sur la clôture de la bougie de signal**. Les noms contenant "RETEST" (`BREAKOUT_RETEST`, `FIB_OTE_RETEST`) décrivent une condition de proximité de zone vérifiée AU MOMENT de l'entrée, pas un vrai second temps d'exécution. Ceci corrobore directement l'observation de la mission ("trades morts-dès-l'entrée") : la plupart des stratégies achètent/vendent la mèche de la bougie qui vient de casser un niveau, sans laisser le marché revenir tester ce niveau avant d'engager le risque.

**Inventaire complet des nombres magiques** : catalogué fichier par fichier dans le rapport agent complet (conservé en annexe interne de session) ; couvre `setup_hunter.py`, `confirmation_matrix.py`, `ees.py`, `big_setup_detector.py`, `geometric_engine_v2.py`, `multi_timeframe_momentum.py`, et chaque stratégie individuelle — prêt à servir de base au futur moteur EV.

---

## 3. LA FORMULE DE CONFLUENCE — une page de mathématiques

**Trois scores de confluence indépendants** gatent le même trade à des étapes différentes du pipeline (redondance structurelle, pas un bug isolé) :

| Score | Fichier | Formule | Seuil | Gate quoi |
|---|---|---|---|---|
| A — `_btc_confluence` | `setup_hunter.py:1373` | `_btc_setup + pénalité(smc) + pénalité(mtfa)` | <55 | BTC_SCALPING_AGENT uniquement |
| **B — `FINAL_CONFLUENCE`** | `confluence_engine.py` + `main.py` | voir ci-dessous | 55/58/62/65 selon classe | `ROUTER_BLOCK reason=FINAL_CONFLUENCE_TOO_LOW` |
| C — `adaptive_confluence` | `adaptive_confluence_threshold.py` | combinaison linéaire pondérée séparée | `hard_avoid=45`, 55-65 par symbole | `demo_router.process_decision`, **après** B, sur le même trade |

Le score B (celui explicitement nommé dans la mission) déroulé intégralement :

```
FINAL_CONFLUENCE =
  min(100, max(0,
     round(
        geo_score(trend,impulse,ote_zone,pd_zone,compression,volatility,breakout_box,pattern)  # 0..100, geometry_engine.py
      + SMC_ADJ(smc_raw)   [PASS+10 / SOFT_FAIL−5 / STRONG_FAIL−15 ; ±override BTC-range ; ÷2 si OF-arbitrator≥90 ; clamp≥0 si OF-natif]
      + MTFA_ADJ(mtfa_raw) [mêmes règles]
      + OF_BONUS(...)      [OF-natif: A/A+→+15, B→+8, sinon 0  |  non-natif: signal+score≥60→+5, score<30→−5, sinon 0 ; BTC injecté jusqu'à +20]
     , 2)
  ))
  + geo_bonus  [= 0 sauf GEOMETRIC_CONFLUENCE_MODE="ACTIVE" ; défaut=SHADOW ⇒ terme nul aujourd'hui]
  , reclampé à 100
```

Seuil de blocage : `_conf_strat_aware` (`main.py:1055-1062`) est vrai par défaut UNIQUEMENT pour 4 stratégies câblées en dur (dont `ORDER_FLOW_EXECUTION_AGENT`, seuil 58) ; pour tout le reste, `hermes_confluence_strategy_aware=False` (défaut, `config.py:208`, **inchangé depuis le 22/06**) fait retomber le seuil réel à **55.0 fixe**, quelle que soit la classe SMC_NATIVE/DEFAULT théorique — confirme et actualise F-08 de l'ancien audit.

### Incohérence dimensionnelle — trouvée et chiffrée

**Les composantes ne sont PAS à la même échelle avant sommation.** `geo_score` couvre 0-100 en entier ; SMC/MTFA plafonnent chacun à ±15 ; l'order-flow bonus à −5/+15 (jusqu'à +20 cas BTC injecté). Conséquence directe et vérifiable :
- Un candidat avec **zéro** signal SMC/MTFA/OF (`+0+0+0`) mais `geo_score=90` obtient **90/100, grade A** — sans aucune confirmation structurelle, momentum ou order-flow. La géométrie n'est pas une couche de "confirmation" ici, elle **domine** la somme par construction.
- Inversement, un candidat SMC+MTFA+OF parfaits (`+10+10+15=35`) mais `geo_score=0` plafonne à **35/100** — sous TOUS les seuils (55-65), quelle que soit la classe de stratégie.
- `geo_bonus` (§1, patterns harmoniques) est une DEUXIÈME couche géométrique additive par-dessus la première (déjà dominante), pendant que SMC/MTFA n'apparaissent chacun qu'une fois.

Aucune normalisation par poids (contrairement au score C, `adaptive_confluence_threshold.weighted_confluence_score`, qui lui pondère correctement). C'est une incohérence réelle, pas une supposition — vérifiable en 5 minutes avec un candidat synthétique.

### Composantes mortes — statut réel

- **Géométrie (`geo_score`, étage 1)** : **PAS morte** — dominante, comme démontré ci-dessus.
- **Gann** : **structurellement mort** dans la somme `FINAL_CONFLUENCE`. Deux implémentations Gann existent (`geometric_confluence.py` et `geometric_engine_v2.py`, poids 0.05) ; ni l'une ni l'autre n'entre jamais dans `confluence_engine.raw_score` — seule la première peut, via `geo_bonus`, agir en mode ACTIVE (jamais atteint par défaut) ; la seconde ne peut que BLOQUER (jamais bonifier), et seulement si `GEOMETRIC_MODE∈{EXECUTION_FILTER,LIVE}` (jamais par défaut).
- **ML** : **totalement mort, pas presque-mort** — `ml_random_forest_confirmator.py` retourne littéralement `NOT_IMPLEMENTED` en dur, aucune branche de calcul n'existe. Poids réel = 0, toujours.

---

## 4. VALIDATION NUMÉRIQUE — 5 décisions réelles recalculées à la main

Décisions tirées de `app/data/decision_dataset.jsonl` (rows `outcome`, croisées avec `history_deals_get` MT5 réel pour confirmer le P&L). Formule RR : BUY→`(tp−entry)/(entry−sl)`, SELL→`(entry−tp)/(sl−entry)`. Formule risk% (C4, `demo_router._risk_pct`) : `((|entry−sl|/tick_size)×tick_value×lot)/equity×100`, vérifiée par round-trip (equity implicite retrouvée, réinjectée, recalcul identique à 8 décimales).

| # | Ticket | Symbole | Résultat | RR loggué | RR recalculé | Divergence |
|---|---|---|---|---|---|---|
| 1 (gagnant) | 375769227 | GOLD# BUY | +6.25 USD | 1.5 | **1.499999...** | Aucune — OK |
| 2 (gagnant) | 375815264 | BTCUSD# SELL | +0.51 USD | 1.5 | **1.499999...** | Aucune — OK |
| 3 (perdant) | 375596623 | BTCUSD# SELL | −1.69 USD | 1.5 | **1.499979...** | Aucune (arrondi) — OK |
| 4 (perdant) | 375777401 | GOLD# BUY | −8.57 USD | **1.5 (loggué)** | **1.8558 (réel)** | **DIVERGENCE — bug confirmé, voir ci-dessous** |
| 5 (refusé) | — (BLOCK) | GOLD# BUY | `reason=MAX_OPEN_TRADES_PER_SYMBOL` | 1.5 | **1.500000...** | Aucune — OK |

Le `risk_pct` (C4) a été vérifié par round-trip sur les 5 GOLD/BTC (equity implicite ≈ 9047-9073 USD, cohérente sur la fenêtre d'une heure de compte demo réel) — **formule correcte, aucune divergence trouvée**.

### Le bug (#4) — trouvé, root-causé, CORRIGÉ cette mission

`app/strategies/order_flow_execution_agent.py:205-206`. `_calc_sltp()` calcule `entry/sl/tp/rr` de façon géométriquement cohérente (RR exact par construction). Mais juste après, un ajustement AMD_FVG re-snappe `entry` sur le milieu du Fair Value Gap détecté (`entry = round(_fvg_mid, 5)`) **sans jamais recalculer `sl`/`tp`/`rr` en conséquence** — `sl` et `tp` restent figés sur l'ancien `entry`. Preuve directe sur le ticket 375777401 : `entry=4123.145` (5 décimales — signature exacte de `round(_fvg_mid,5)`, alors que GOLD# cote normalement à 2 décimales), `sl=4116.73`, `tp=4135.05` → RR réel 1.856, mais `rr=1.5` resté loggué (valeur calculée avant le snap). Le glissement d'entrée a rapproché le prix d'entrée du stop, réduisant le risque réel affiché sans que le système ne le sache — ce trade a perdu (-8.57 USD).

**Correction appliquée** (`app/strategies/order_flow_execution_agent.py:203-208`) : lorsque `entry` est re-snappé sur `_fvg_mid`, `sl` et `tp` sont maintenant translatés du même delta, préservant exactement les distances de risque/récompense (et donc le RR) déjà calculées par `_calc_sltp`. Aucun seuil ni logique de stratégie modifié — uniquement la cohérence géométrique entry/sl/tp restaurée. Tests : `tests/test_order_flow_execution_agent.py` 43/43 verts (aucun test n'exerçait ce chemin buggé) ; suite complète 3163 passed, 0 régression.

### Correction au cadrage : ORDER_FLOW_EXECUTION_AGENT est ACTIF, pas désactivé

Le rapport préliminaire (basé sur `config.py` seul) affirmait `order_flow_execution_enabled=False` par défaut → stratégie inatteignable. **Faux en pratique** : `.env:93` positionne `ORDER_FLOW_EXECUTION_ENABLED=true`, confirmé par une trace réelle (le ticket #4 ci-dessus, `strategy=ORDER_FLOW_EXECUTION_AGENT`, exécuté). Cette correction est importante : c'est la stratégie **la plus chargée en nombres magiques et en bugs CASSÉS** (§2, items 17) et elle trade réellement GOLD#/BTCUSD# aujourd'hui — pas un code mort.

### ATR — n'est PAS du Wilder (finding, non corrigé, RÉSERVÉ SIMO)

`app/utils/indicators.py:24-34`, fonction `atr()` : `tr.rolling(period).mean()` — une **moyenne mobile simple** du True Range, PAS le lissage de Wilder (RMA, `ATR_t = (ATR_{t-1}×13 + TR_t)/14`). Vérifié sur une série OHLC de référence à 15 barres : SMA-ATR = **0.5679**, Wilder-RMA = **0.5933** (écart ~4.3% sur cet échantillon). Cette fonction est LA source d'ATR utilisée partout : SL/TP de la quasi-totalité des stratégies (§2), `EES` (extension vs ATR), `geometry_engine.py` (compression/volatilité), `BtcDynamicExit` (`sl_usd = atr*1.5`). **Non corrigé cette mission** : c'est un changement qui déplacerait TOUS les SL/TP et seuils basés sur l'ATR system-wide — exactement le type de changement de stratégie/seuil que GRAND_PLAN interdit de faire sans mandat explicite. Documenté ici pour que SIMO tranche (renommer honnêtement en `atr_sma()`, ou migrer vers Wilder avec recalibration complète des seuils qui en dépendent).

---

## 5. RÉSERVÉ SIMO — composants ARBITRAIRES classés par impact, proposition de calibration

| Priorité | Composant | Proposition de calibration (donnée dataset) |
|---|---|---|
| **1 — Systémique** | ATR = SMA, pas Wilder (§4) | Si migration vers Wilder décidée : recalibrer tous les multiplicateurs ATR (`1.5`, `2.0`, `0.6`, etc.) sur le nouvel ATR avant tout déploiement — sinon tous les SL/TP se déplacent silencieusement |
| **2 — Systémique** | Incohérence d'échelle `geo_score` (0-100) vs SMC/MTFA (±15) dans `FINAL_CONFLUENCE` (§3) | Normaliser `geo_score` à un poids comparable (ex: ×0.35 comme le fait déjà `adaptive_confluence_threshold`) avant sommation, calibré sur la corrélation historique `geo_score`↔résultat réel du dataset |
| **3** | Formule composite `geometric_confluence.py` sous-récompense un pattern valide (§1) | Backtester la proposition §1 (poids 55/45, cluster 2-hits) sur les logs `[GEOMETRIC_CONFLUENCE]` déjà accumulés en SHADOW avant activation |
| **4** | Cible RR (1.8) vs seuil de gate RR (1.5) déconnectés dans CRT_TBS/AMD_FVG/FIB_OTE (§2) | Aligner la cible sur le seuil de gate, ou documenter explicitement l'écart voulu (marge de sécurité) — actuellement juste une incohérence non intentionnelle |
| **5** | Scores binaires (EMA_PULLBACK, BREAKOUT_RETEST, SECOND_ENTRY, SCALPING legacy, EUR_EMA_RSI_ATR) — confidence fixe, non graduée | Remplacer chaque `if triggered: return 0.68 else 0.42` par une fonction continue de la distance au signal (déjà calculée dans chaque fonction, juste jamais utilisée pour graduer) — c'est le prérequis direct du futur moteur EV |
| **6** | `hermes_confluence_strategy_aware=False` par défaut neutralise les seuils par classe (58/62/65) pour tout sauf 4 stratégies câblées en dur (§3) | Activer le flag globalement une fois les seuils 58/62/65 validés sur le dataset — actuellement une intention de conception jamais mise en œuvre |
| **7** | SL non plafonnés par ATR (GOLD_LIQUIDITY_HUNTER_PRO: 30%×zone ; FIB_OTE_RETEST: swing opposé entier ; GOLD_RANGE_BREAKOUT: largeur de range) | Ajouter un plafond `min(sl_distance, N×ATR)` par stratégie, calibré sur la distribution réelle MAE/MFE du dataset (déjà loggée par trade) |
| **8** | `GOLD_RANGE_BREAKOUT` inatteignable (flag jamais câblé) | Ajouter `gold_range_breakout_enabled` à `Settings` — c'est la SEULE stratégie du dépôt avec un vrai retest en 2 temps ; actuellement gaspillée |

---

## 6. BOTTOM-LINE (6 lignes)

Le cœur n'est **pas encore mathématiquement digne de confiance en l'état** : sur les 21 stratégies auditées, une seule (QUANT_STATISTICAL_PULLBACK/QUANT_PRO_REGIME_SWITCHING pour le signal, GOLD_RANGE_BREAKOUT pour le score) a une construction rigoureuse de bout en bout, et la stratégie réellement active en argent (ORDER_FLOW_EXECUTION_AGENT) contenait un bug confirmé sur un trade perdant réel — corrigé cette mission. La confluence finale mélange trois systèmes de score distincts et une échelle géométrique non normalisée qui peut, seule, faire passer ou échouer un trade indépendamment de toute confirmation SMC/MTFA/order-flow. L'ATR — brique de base de quasiment tout le dimensionnement de risque — n'est pas le Wilder RMA standard mais une moyenne mobile simple, sans que rien dans le code ne le signale. Le chemin vers un cœur 100% calculé (moteur EV) est concret : (1) réparer l'incohérence d'échelle de la confluence, (2) remplacer les ~10 scores binaires par des fonctions continues déjà calculables à partir des mêmes variables, (3) trancher ATR (Wilder ou SMA assumé et renommé) et recalibrer en conséquence, (4) puis seulement alimenter un moteur de poids appris sur le dataset — dans cet ordre, pas l'inverse, sinon le moteur EV apprendra sur des signaux structurellement biaisés en échelle.
