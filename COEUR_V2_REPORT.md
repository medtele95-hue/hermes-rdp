# COEUR_V2_REPORT — les 3 chantiers systémiques (2026-07-08)

Mission mandatée par SIMO sur la base de `MATH_CORE_AUDIT.md` — RÉSERVÉ SIMO
n°1, 2, 6, 8 tranchés GO. Chaque chantier modifie intentionnellement le
comportement de trading (mandat explicite de la mission). Safepoint git avant
chaque chantier (tags ci-dessous), suite complète verte après chacun.

## Chantier 1 — Normaliser l'échelle de la confluence

**Avant** : `FINAL_CONFLUENCE = geo_score(0-100) + smc_contrib(±15) + mtfa_contrib(±15) + of_bonus(-5/+20)`
— la géométrie pouvait à elle seule porter le score à 90+ sans aucune
confirmation SMC/MTFA/order-flow ; un candidat parfaitement confirmé mais
géométriquement plat plafonnait à 35, sous tous les seuils.

**Après** : `FINAL_CONFLUENCE = (w_geo·geo_norm + w_smc·smc_norm + w_mtfa·mtfa_norm + w_of·of_norm) / Σw`,
chaque composante normalisée sur 0-100, poids par défaut `geo=0.25 / smc=0.25
/ mtfa=0.20 / of=0.30` (order-flow — la stratégie réellement active —
conserve le poids le plus fort). Poids configurables (`confluence_weight_*`
dans `Settings`/`.env`), destinés à être recalibrés par le futur moteur EV.
Les overrides existants (plancher BTC RANGE mode, arbitrage OF, clamp
OF-natif) sont traduits en domaine normalisé (planchers 40/35/50 au lieu de
deltas −5/0). `components` (delta-domain) et `legacy_score`/`legacy_grade`
sont préservés inchangés pour comparaison — **zéro test existant modifié pour
cette raison**, seule une magnitude de régression a été mise à jour (formule
change intentionnellement le comportement, couverture originale préservée en
parallèle via `legacy_score`). Log `[CONFLUENCE_V2]` avec les 4 composantes +
poids + total à chaque évaluation ; `new_confluence`/`old_confluence` (+
grades) capturés dans le dataset pour comparaison continue.

**Rejeu comparatif** (690 décisions GOLD#/BTCUSD# réelles, `geo_score`
retrouvé par résolution algébrique de l'ancienne formule — pas besoin des
OHLC bruts non stockés) :

| Métrique | Valeur |
|---|---|
| Décisions rejouées | 690 (23 exclues, score stocké au plafond 0/100 — résolution ambiguë) |
| Taux de passage AVANT (ancienne formule) | 97.5% |
| Taux de passage APRÈS (nouvelle formule) | 96.4% |
| Flips PASS→BLOCK | 8 (tous FIB_CONFLUENCE_EXECUTION_AGENT / BTC_SCALPING_AGENT — exactement les candidats géo-dominants sans confirmation prédits par l'audit) |
| Flips BLOCK→PASS | 0 (aucun dans cette fenêtre) |

Taux sain (ni collapse, ni explosion), effet correctif visible et ciblé.
ORDER_FLOW_EXECUTION_AGENT (671/690 lignes, la stratégie réellement active)
reste stable — son plancher neutre OF-natif absorbe le changement d'échelle.

## Chantier 2 — ATR : migration Wilder RMA + recalibration

**Découverte en creusant la mission** : l'audit n'avait examiné qu'un seul
fichier (`app/utils/indicators.py`). En réalité, **10 implémentations locales
indépendantes** du même bug SMA existaient dans le dépôt, jamais identifiées :
`gold_range_breakout.py`, `wsp_intelligence_overlay.py`,
`top_down_market_reader.py`, `eur_ema_rsi_atr_strategy.py`,
`smc_confluence_tagger.py`, `mtf_structure_detector.py`, `ees.py`,
`order_flow_execution_agent.py`, plus le fichier canonique. Une 11ᵉ,
`trend_continuation_breakdown.py`, avait un bug **distinct et plus grave** :
moyenne du range haut-bas sans jamais comparer au close précédent — ignore
les gaps, sous-estime la volatilité sur toute bougie gappée. Et **3
implémentations étaient déjà du Wilder correct** (`btc_dynamic_exit.py`,
`app/quant/volume_engine.py`, `app/quant/geometry_engine.py`) — non touchées.

**Fait** : `atr_wilder()`/`atr_sma()`/`atr()`=alias Wilder dans
`app/utils/indicators.py`, plus `atr_last()`/`atr_series_graceful()` (helpers
avec dégradation gracieuse SMA sur fenêtre courte) pour consolider les 9
duplicatas sur une base commune testée. `trend_continuation_breakdown.py`
corrigé en vrai True Range + Wilder — ses multiplicateurs (1.25/0.45/0.6/2.0)
**non recalibrés intentionnellement** : préserver un comportement qui était
réellement cassé n'a pas de sens, documenté dans le code.

**Mesure réelle** (MT5, GOLD#+BTCUSD# M5, 30 jours) : Wilder ~2.6-2.7% plus
élevé que SMA en moyenne (GOLD 1.02724, BTC 1.02447, blend 1.025855) — plus
faible que l'estimation initiale (~4.3% sur l'échantillon de référence de
l'audit), l'écart variant selon le régime de volatilité.

**Recalibration** (facteur inverse du ratio mesuré) :

| Stratégie/module | Multiplicateur(s) avant | Après | Ratio utilisé |
|---|---|---|---|
| EMA_PULLBACK | proximité 1.2, SL 1.5, TP 2.5 | 1.1697 / 1.4621 / 2.4369 | blend 1.025855 |
| BREAKOUT_RETEST | filtre mèche 1.0, SL 1.0, TP 2.0 | 0.9748 / 0.9748 / 1.9497 | blend |
| SECOND_ENTRY | SL 1.3, TP 2.0 | 1.2673 / 1.9497 | blend |
| SCALPING (legacy) | SL 1.0, TP 1.6 | 0.9748 / 1.5598 | blend |
| BTC_SCALPING_AGENT | risk = atr×1.0 | risk = atr×0.9761 | BTC 1.02447 |
| GOLD_RANGE_BREAKOUT | ratio range/ATR<2.5, retest 0.3×ATR, expansion 0.8×ATR | 2.4337 / 0.2920 / 0.7787 | GOLD 1.02724 |
| EES (extension/climax vs ATR) | — | ATR consommé rescalé ×0.9748 avant usage ; **seuils PRUDENCE/EXTREME et constantes de formule inchangés par construction** | blend |
| ORDER_FLOW_EXECUTION_AGENT (marge SFP) | 0.1×ATR | 0.09748×ATR | blend |
| smc_confluence_tagger / mtf_structure_detector (k×ATR_H4 des zones) | 0.5×ATR | 0.4874×ATR | blend |

**Preuve iso-comportement** (3 setups réels rejoués sur données MT5
actuelles) :

| Setup | ATR SMA | ATR Wilder | Distance avant | Distance après | Écart |
|---|---|---|---|---|---|
| GOLD# EMA_PULLBACK SL | 3.78286 | 3.79943 | 1.5×sma=5.67429 | 1.4621×wilder=5.55514 | −2.10% |
| GOLD# EMA_PULLBACK TP | 3.78286 | 3.79943 | 2.5×sma=9.45714 | 2.4369×wilder=9.25882 | −2.10% |
| BTCUSD# BREAKOUT_RETEST TP | 92.89286 | 92.98694 | 2.0×sma=185.786 | 1.9497×wilder=181.297 | −2.42% |

L'écart résiduel (~2-2.4%) reflète le caractère **régime-dépendant** du
ratio Wilder/SMA (confirmé empiriquement par l'audit et par cette mesure) —
la recalibration cible la moyenne, pas une conversion instantanée parfaite.
Bruit résiduel très inférieur au biais systématique corrigé (~2.6-4.3%).

## Chantier 3 — Câbler le vrai retest + seuils par classe

**a) GOLD_RANGE_BREAKOUT** (seule stratégie avec un vrai retest en 2 temps,
morte par flag manquant) : le câblage routeur/registry était **déjà en
place** (`ACTIVE_EXECUTION_STRATEGIES`, `ALLOWED_GOLD_EXECUTION_STRATEGIES`,
`demo_router._gold_range_breakout_block_reason`) — il ne manquait que
`gold_range_breakout_enabled` dans `Settings` (absent, toujours `False` via
`getattr` fallback). Ajouté, défaut `true`. Vérifié en live après redémarrage
(`[GOLD_ROUTER] allowed_candidates=...,GOLD_RANGE_BREAKOUT,...`). Son score
s'intègre à la confluence v2 via le même pipeline générique que toute
stratégie DEFAULT/SMC_NATIVE — aucune intégration spéciale nécessaire.

**b) `hermes_confluence_strategy_aware=true` par défaut** : les seuils par
classe (58 OF-native / 65 SMC-native / 62 défaut) n'étaient effectifs que
pour 4 stratégies câblées en dur ; tout le reste retombait sur un seuil plat
55.0. Rejeu comparatif sur 697 décisions réelles : **8 lignes seulement
réellement affectées** (SIMO_ATM_BREAKOUT:1, FIB_CONFLUENCE_EXECUTION_AGENT:7
— le reste est déjà OF-natif donc déjà strategy_aware), **0 flip observé**
dans cette fenêtre. Activé par mandat de mission (corrige une incohérence de
config — le mécanisme de seuil par classe existait déjà dans le code sans
être appliqué), pas parce que le rejeu montrait une urgence ; échantillon
trop petit pour une conclusion statistique de toute façon.

**c) Vraies entrées retest pour les autres stratégies** (noté, non
implémenté — RÉSERVÉ SIMO futur) : l'audit §2 avait montré que toutes les
autres stratégies entrent au marché sur la bougie de signal. `
GOLD_RANGE_BREAKOUT` est désormais la seule à démontrer une vraie entrée en 2
temps (détecter → attendre le retest → entrer). Une fois que son dataset aura
accumulé assez de décisions pour comparer retest-vs-marché en conditions
réelles, cette comparaison doit trancher si le chantier de refonte des
entrées (audit §2, "trades morts-dès-l'entrée") vaut la peine sur les autres
stratégies.

## Commits et tags

| Tag | Contenu |
|---|---|
| `coeur-v2-chantier1-confluence` | Normalisation de la confluence |
| `coeur-v2-chantier2-atr-wilder` | Migration ATR + recalibration (10 sites) |
| `coeur-v2-chantier3-range-breakout-strategy-aware` | GOLD_RANGE_BREAKOUT + strategy_aware |
| (ce commit) | `core_version=2` sur le dataset, clôture |

## Bottom-line (6 lignes)

Ce qui change concrètement au prochain candidat : la géométrie ne peut plus,
seule, porter un score au-dessus des seuils — elle doit être confirmée par
au moins une des trois autres sources (SMC/MTFA/order-flow) pour compter
pleinement ; chaque SL/TP basé sur l'ATR utilise désormais le lissage Wilder
standard au lieu d'une moyenne mobile simple sous-jacente incorrecte ;
GOLD_RANGE_BREAKOUT — la seule stratégie du dépôt avec un vrai retest en 2
temps — trade enfin en demo ; les seuils de confluence par classe de
stratégie s'appliquent désormais à tout le monde, pas seulement 4 stratégies
câblées en dur. Ce qui NE change PAS : les distances SL/TP effectives restent
les mêmes à ~2% près (recalibration mesurée empiriquement sur données réelles
GOLD#/BTCUSD# M5, pas devinée) ; les seuils EES et leur formule sont
inchangés par construction ; aucune politique de risque, quota kill-switch,
ou paramètre de lot/magic n'a été touché — tout reste dans le périmètre
mandaté par la mission.
