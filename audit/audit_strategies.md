# AUDIT HERMES MT5 — INVENTAIRE DES STRATÉGIES
**Date** : 2026-06-22  |  **Source** : setup_hunter.py, config.py, demo_router.py

---

## CLASSIFICATION PAR VOIE D'EXÉCUTION

### Groupe 1 : ACTIVE_EXECUTION (voie dédiée ORDER_FLOW)
Ces stratégies empruntent la voie `order_flow_exec_ready` dans `_failed_gates()`.

| Stratégie | Condition d'activation | Min score | Grade min | Remarque |
|---|---|---|---|---|
| ORDER_FLOW_EXECUTION_AGENT | `order_flow_execution_enabled=True` | 75.0 | B | Bypass grade/score si legacy (score≥90 AND grade=A) |

**Source** : `setup_hunter.py:1139-1208`, `config.py:168` (`order_flow_execution_enabled: bool = False`)  
**Statut par défaut** : DÉSACTIVÉ (`order_flow_execution_enabled=False`)

---

### Groupe 2 : strategy_ready_without_confluence (voie directe — pas de CM général)
Ces stratégies bypassent le bloc `_general_cm_check` et les checks M15/M1 standard.

| Stratégie | Bypass activé par | Condition minimum | Source |
|---|---|---|---|
| STATISTICAL_QUANT_PREMIUM | `statistical_quant_enabled=True` | `quant_score >= 78` | setup_hunter.py:1283 |
| STATISTICAL_QUANT_RESEARCH | `statistical_quant_enabled=True` | `quant_score >= 65` | setup_hunter.py:1284 |
| GOLD_SCALPING_AGENT | `gold_scalping_enabled=True` | setup_score ≥ 65 | setup_hunter.py:1285 |
| GOLD_M1M5_BREAKOUT | `gold_m1m5_enabled=True` | — | setup_hunter.py:1286 |
| GOLD_ORDER_FLOW | `gold_order_flow_enabled=True` | — | setup_hunter.py:1287 |
| BTC_SCALPING_AGENT | `btc_scalping_enabled=True` | `_btc_confluence >= MIN_ROUTE_CONFIDENCE=75` | setup_hunter.py:1288 |
| BTC (LOVABLE bypass) | `lovable_btc_old_system_enabled=True` | profil LOVABLE actif | setup_hunter.py:1289 |
| SIMO_ATM_BREAKOUT | `simo_atm_enabled=True` | — | setup_hunter.py:1290 |
| STRATEGY_PACK_* | `strategy_pack_enabled=True` | — | setup_hunter.py:1291 |
| FIB_RETRACEMENT_* | `fib_retracement_enabled=True` | — | setup_hunter.py:1292 |

---

### Groupe 3 : Voie GÉNÉRALE (CM + M15/M1 requis)
Toutes les stratégies non incluses dans les groupes 1 et 2 passent par le chemin standard.

**Conditions minimales standard** :
- CM check (confirmation_matrix) non-HARD_BLOCK
- M15/M1 confirmation (si strict=True)
- `grade_ok` ou `setup_score >= 75` (mode strict)

---

## TABLEAU COMPLET DES STRATÉGIES

| ID | Nom | Statut config | Groupe | NOT_IMPL | Notes |
|---|---|---|---|---|---|
| 1 | ORDER_FLOW_EXECUTION_AGENT | DISABLED (défaut) | 1 | NON | F-09: legacy bypass score≥90+A |
| 2 | BTC_SCALPING_AGENT | À vérifier | 2 | NON | _btc_confluence min=75; lovable bypass |
| 3 | STATISTICAL_QUANT_PREMIUM | À vérifier | 2 | NON | quant_score≥78 requis |
| 4 | STATISTICAL_QUANT_RESEARCH | À vérifier | 2 | NON | quant_score≥65 |
| 5 | GOLD_SCALPING_AGENT | À vérifier | 2 | NON | — |
| 6 | GOLD_M1M5_BREAKOUT | À vérifier | 2 | NON | — |
| 7 | GOLD_ORDER_FLOW | À vérifier | 2 | NON | — |
| 8 | SIMO_ATM_BREAKOUT | À vérifier | 2 | NON | edge_score=100 observé (DEMO_ORDER log) |
| 9 | STRATEGY_PACK_* | À vérifier | 2 | NON | nom générique — pack bundlé |
| 10 | FIB_RETRACEMENT_* | À vérifier | 2 | NON | — |

---

## MODULES NOT_IMPLEMENTED (stubs)

| Module | Fichier | Rôle attendu | Comportement actuel | Impact |
|---|---|---|---|---|
| ML_RANDOM_FOREST_CONFIRMATOR | ml_random_forest_confirmator.py | Confirmation ML des setups | `ml_status=UNAVAILABLE` (neutre) | NUL — jamais bloquant |
| SMC_ORDERBLOCK_LIQUIDITY_NARRATOR | smc_orderblock_liquidity_narrator.py | Évitement des OrderBlocks | `smc_ob_avoid=False` toujours | MOYEN — OB non filtrés en live |

---

## PROFIL LOVABLE_BTC_OLD_SYSTEM

**Fichier** : `app/profiles/lovable_btc_old_system.py`  
**Activation** : `lovable_btc_old_system_enabled=True` dans config

**Effets** :
- `FINAL_CONFLUENCE_TOO_LOW` bypassé pour `ORDER_FLOW_EXECUTION_AGENT`
- `CONFIRMATION_MATRIX_HARD_BLOCK` bypassé pour `BTC_SCALPING_AGENT`
- Modes forcés : `BTC_SCALPING_AGENT → DEMO_ADAPTIVE_FALLBACK`, `ORDER_FLOW_EXECUTION_AGENT → DEMO_MICRO_DISCOVERY`

**Stratégies concernées** :
```python
OLD_BTC_STRATEGIES = frozenset({"BTC_SCALPING_AGENT", "ORDER_FLOW_EXECUTION_AGENT"})
```

---

## OBSERVATIONS DU LOG DEMO_PILOT_EVENTS.JSONL

| Timestamp | Stratégie | Event | Score | Résultat |
|---|---|---|---|---|
| 01:12:56 | ORDER_FLOW_EXECUTION_AGENT | DEMO_ORDER BUY BTCUSD# | breakdown_sum≈33 | Exécuté |
| 01:15:18 | HERMES_QUICK_EXIT_MANAGER | RESCUE_CLOSE | — | QuickExit TP=1.50 |
| 01:26:44 | SIMO_ATM_BREAKOUT | DEMO_ORDER BUY BTCUSD# | edge_score=100, setup_score=95 | Exécuté |
| Multiple | Diverses | DEMO_SKIP (17 events) | near_miss_reason=None | near_miss_reason non propagé (F-01) |
