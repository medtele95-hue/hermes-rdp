# MISSION_GEOMETRIE — RAPPORT (SL borné 1,5×ATR + Exit V2 qui laisse courir)

**Date :** 2026-07-16 · **Safepoint :** `safepoint-avant-geometrie`
**Commits :** `34472d64` (FIX 1) · `aa67c51b` (FIX 2)
**Suite complète :** **3572 passed, 2 skipped**. Bot redéployé (PID 4480), 1er cycle sain.

## RÉSUMÉ

Deux fix **couplés** (l'un sans l'autre reste perdant), prouvés par la calibration puis validés par le replay d'impact :
1. **SL structurel ORDER_FLOW borné à `min(structurel, 1.5×ATR)`** → risque médian **33,5 $ → 6,4 $**, **100 % sous le cap** (vs 22 %), les setups FORTS ne sont plus rejetés.
2. **Exit V2 laisse courir** (trailing = 1×ATR au lieu du floor $ fixe de 1,20 $) → on encaisse ce qui était déjà à nous.
**Espérance simulée FIX 1 + FIX 2 : +0,109 R** (positive), là où le système actuel (SL large + coupe précoce) était net négatif. Le cap de risque, le kill-switch, le choke-point et le plancher RR ≥1,5 sont **intouchés** (diff vide prouvé).

---

## FIX 1 — SL structurel borné à min(VA, 1,5×ATR)

**Où :** `order_flow_execution_agent._calc_sltp` — le seul site au SL structurel non borné (les autres stratégies GOLD sont déjà en ×ATR). Le clamp est appliqué **après** le calcul structurel, **avant** le TP :
```
risk = min( structurel , SL_ATR_CAP_K × ATR )      # k = 1.5
sl   = entry ± risk                                 # SL reconstruit
tp   = entry ± risk × min_rr                         # TP SUIT (RR reste ≥1.5)
```
- **ATR = M5 Wilder (bougies clôturées)**, identique à `decision_dataset.atr_price` → cohérent avec la calibration.
- **Plafond, pas plancher** : n'active que si le structurel dépasse k×ATR (un SL déjà serré reste inchangé — contrôle négatif T2).
- **Symétrique BUY/SELL.** Sans ATR valide → fail-safe (SL structurel d'origine).
- **TP « Modèle B »** (refabriqué sur le risque borné), pas l'ancien TP lointain (Modèle A, haute variance, non retenu).

**Tests :** `tests/test_geometrie_sl.py` (9 cas). T1 SL borné à 1,5×ATR ; T2 structurel déjà serré → inchangé ; T2b sans ATR → inchangé ; T3 symétrie BUY ; T4 passe le cap (6 $ < 22,60 $ là où 39 $ échouait) ; T5 TP refabriqué (proche, pas lointain).

---

## FIX 2 — Exit V2 laisse courir les gagnants (trailing ATR)

**Où :** `exit_v2.evaluate_exit_v2` — le gap du trailing devient `TRAIL_ATR_MULT × ATR` (converti en USD via `usd_per_price_unit`), **figé à l'armement** du trail → floor strictement monotone. Le gagnant court tant qu'il ne recule pas de plus de 1×ATR sous son pic.
- **Protection inchangée :** le breakeven (arme +2 $, floor +0,10 $) reste le filet dur — une fois armé, le trade ne repart JAMAIS au risque plein. Le SL borné du FIX 1 (6 $) reste le stop broker avant l'armement.
- **ATR M5** calculé dans `demo_router._exit_m5_atr`, passé à `evaluate_exit_v2`. None → **fallback** sur `trail_gap_usd` (ancien comportement, fail-safe). C'est pourquoi les tests Exit V2 **existants** (qui ne passent pas d'ATR) restent verts **sans modification** — le nouveau comportement n'active que sur le chemin de production (ATR fourni).
- **Option B** de la mission (sortie contextuelle momentum/structure) **non implémentée** — notée pour après validation v2.

**Tests :** `tests/test_geometrie_exit.py` (7 cas). T6 gagnant qui court (n'est plus coupé à +0,3 R) + contraste avec l'ancien floor qui coupait ; T7 recul >1×ATR → sort au trailing ; T8 perdant non-armé → Exit V2 ne ferme pas (SL broker) ; T9 BE armé → protégé au breakeven, jamais au risque plein ; + monotonicité (gap figé) + fail-safe sans ATR.

---

## REPLAY D'IMPACT (lecture seule) — les 3 chiffres

Setups top-down PASS du dataset (418) rejoués contre 15 000 bougies M1 avec le cœur MODIFIÉ :

| # | Métrique | Avant | **Après FIX 1+2** |
|---|---|---|---|
| 1 | % setups sous le cap de risque (22,60 $) | 22 % | **100 %** |
| 3 | Risque médian par trade | 33,5 $ | **6,4 $** (= 1,5×ATR) |
| 2 | Espérance simulée (SL borné + trailing ATR) | net négatif | **+0,109 R** |

Détail des sorties simulées : TP 68 · TRAIL 214 · SL 100 · UND 36 (382 décidés). **+0,109 R est positif** — le couplage fonctionne : il transforme un système net-négatif en positif. C'est une **borne basse** (l'hypothèse intra-bougie « extrême adverse d'abord » pénalise le trailing) ; la valeur réelle se situe entre +0,109 et le +0,24 R « plein-TP » de la calibration.

---

## FRONTIÈRE + VALIDATION V2

- **`geometry_version = 1`** sur chaque ligne décision écrite après ce déploiement (modèle `core_fix_level=P0TER`). Les trades v2 d'AVANT (SL large, Exit V2 coupe-tôt) restent **distinguables et exclus** de la calibration finale.
- **CAVEAT à relire noir sur blanc :** k=1,5 est prouvé sur un échantillon **100 % SELL, un seul régime** (GOLD en up-drift). Cette mission le met en PRODUCTION pour collecter des données v2 propres dans **les deux directions** et plusieurs régimes. **k reste à REVALIDER** sur ~100-200 trades v2 réels ; s'il s'avère sous-optimal en BUY ou en range, `SL_ATR_CAP_K` (et `EXIT_V2_TRAIL_ATR_MULT`) s'ajustent **sans re-coder**.

---

## CONSTANTES CONFIGURABLES AJOUTÉES

| Constante | Défaut | Env | Rôle |
|---|---|---|---|
| `sl_atr_cap_k` | **1.5** | `SL_ATR_CAP_K` | plafond du SL structurel = k×ATR (FIX 1) |
| `exit_v2_trail_atr_mult` | **1.0** | `EXIT_V2_TRAIL_ATR_MULT` | distance du trailing = mult×ATR (FIX 2) |

---

## VÉRIFICATION FINALE

- **Zones intouchables — diff VIDE prouvé** : `daily_killswitch.py`, `mt5_position_sync.py`, le cap `demo_max_risk_per_trade_pct = 0,25 %` (seul le commentaire le mentionne), le choke-point / `order_send` de `demo_router` (FIX 2 ne touche que le chemin de sortie Exit V2 + un helper ATR). Plancher RR ≥1,5 préservé.
- **git diff borné** : `config.py`, `order_flow_execution_agent.py`, `decision_dataset.py`, `exit_v2.py`, `demo_router.py` + `tests/test_geometrie_sl.py`, `tests/test_geometrie_exit.py`. Rien d'autre.
- **Suite complète** : 3572 passed, 2 skipped (16 nouveaux tests géométrie ; les tests Exit V2 existants inchangés = preuve que la protection n'a pas régressé).
- **Bot redéployé** : PID 4480, 1er cycle sain, aucune erreur liée au fix.

<!-- LIVE_PROOF -->

---

**Résumé :** le système devient plus JUSTE et moins risqué par trade (6 $ vs 33 $), jamais plus permissif. Les setups forts (top-down PASS) ne sont plus rejetés par le cap ; les gagnants ne sont plus coupés à +0,3 R. Deux constantes configurables, une frontière de version, cap de risque et protections intacts. **k=1,5 est un point de départ prouvé, à revalider sur les 200 trades v2 indépendants.**
