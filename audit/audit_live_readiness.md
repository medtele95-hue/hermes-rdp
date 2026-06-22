# AUDIT HERMES MT5 — CHECKLIST LIVE READINESS
**Date** : 2026-06-22  |  **Mode actuel** : DEMO_PILOT_48H

---

## RÉPONSES AUX 5 QUESTIONS BINAIRES

---

### Q1 — La garde d'exécution (`_failed_gates`) bloque-t-elle correctement toutes les stratégies non prêtes ?

**RÉPONSE : OUI — avec réserves**

La garde d'exécution fonctionne correctement dans le chemin nominal. Les exceptions documentées :

1. **ORDER_FLOW path** (`order_flow_exec_ready`) : checks plus stricts (grade ≥ B, score ≥ 65, CM non-HARD). Correct.
2. **strategy_ready_without_confluence** : bypass intentionnel CM + M15/M1 pour 10 stratégies nommées. Voulu par conception.
3. **_of_high_quality_legacy_bypass** (F-09) : si setup_score≥90 AND grade=A → grade/score checks OF bypassed. Semi-intentionnel (legacy).
4. **LOVABLE_BTC_OLD_SYSTEM profil** : bypass HARD_BLOCK CM pour BTC_SCALPING + bypass FINAL_CONFLUENCE pour ORDER_FLOW. Mode optionnel — désactivé par défaut.
5. **order_flow_execution_enabled=False (défaut)** : ORDER_FLOW ne peut pas s'exécuter sauf si activé.

**Verdict** : La garde protège correctement les cas non intentionnels. Les bypasses existants sont explicitement codifiés.

---

### Q2 — Le SL engine protège-t-il les positions BTC ouvertes ?

**RÉPONSE : NON — BLOQUÉ PAR BUG F-05**

**État actuel** : Le SL Engine ne s'exécute **jamais** pour BTC avec `btc_exit_arbiter_enabled=True` (défaut) et `atr_value` dans la plage 20-60 (cas normal BTC) :

```
btc_fast_exit_daemon.py:325
condition: (not _arbiter_enabled or _exit_authority == "SWING")
         = (not True or "DYNAMIC" == "SWING") = False
→ SL Engine.run() JAMAIS appelé pour BTC normal
```

La protection SL pour BTC repose uniquement sur :
- `rescue_threshold = 0.45 USD` (via daemon DYNAMIC — si pas court-circuité)
- `trail_start = 4.8 USD profit` → trailing stop
- `QuickExitManager tp=1.50 USD` (clôture en premier — F-07)

**Risque live** : Sans SL Engine actif, le SL price ne se resserre jamais. Position exposée à la hauteur du SL initial (3.0 USD équivalent prix = 300 points pour lot=0.01).

**BLOQUANT pour live** : OUI — corriger BAC-B-01 avant activation live.

---

### Q3 — Le routage des ordres est-il sûr (pas d'ordre live possible) ?

**RÉPONSE : OUI — VÉRIFIÉ**

Preuves directes :
1. `config.py:155` : `allow_live_trading: bool = False`
2. `demo_router.py` : toutes les sorties passent par `demo_router.sl_engine_apply_modification` ou l'émission d'events DEMO_ORDER (pas de `order_send` MT5 direct)
3. `btc_sl_engine.py:274` : route vers `demo_router.sl_engine_apply_modification`
4. Dashboard CommandCenter.tsx:90 : badge `allow_live_trading = false` (+ confirmation textuelle)
5. `DEMO_PILOT_48H` mode actif — les events sont `DEMO_ORDER`, pas `LIVE_ORDER`

Aucune exécution live possible dans la configuration actuelle.

---

### Q4 — Les métriques de performance (RR, PnL) sont-elles fiables ?

**RÉPONSE : NON — TROMPEUSES pour BTC**

**Problèmes identifiés** :

| Métrique | Valeur affichée | Valeur réelle | Écart |
|---|---|---|---|
| `rr_target` (dynamic_exit) | 2.37–3.50 | 2.0 max (capé) | Trompeur |
| `realized_rr` (daemon) | 2.0 | Variable (<1.0) | Trompeur |
| RR effectif (exécuté) | 2.0 (dashboard) | tp_quick/sl_price = ~0.5 | Très trompeur |
| `confluence_score` daemon | score réel | 50.0 (fallback si btc_intel absent) | Divergent |

**Cause** : QuickExitManager (tp=1.50) clôture avant FastExitDaemon (tp=6.0). RR réel d'une clôture QuickExit ≈ 1.50/sl_dollars_equiv.

Pour `entry=63852.50, sl=63552.50` (300pts), `tp_quick=63852.50+150pts` :
- Profit Quick = +1.50 USD pour lot 0.01
- sl_usd = 3.00 USD (300pts × 0.01 lot)
- RR effectif = 1.50 / 3.00 = **0.50** (pas 2.0 comme affiché)

**Verdict** : Les métriques BTC actuelles sous-estiment le risque réel et surestiment la performance.

---

### Q5 — Le système est-il prêt pour une activation live (allow_live_trading=True) ?

**RÉPONSE : NON — 2 blocages critiques non résolus**

| Criticité | Finding | Blocage live | Statut |
|---|---|---|---|
| CRITIQUE | F-05 : SL Engine désactivé (DYNAMIC) | OUI | Non corrigé |
| CRITIQUE | F-07 : QuickExit court-circuite DYNAMIC (RR fictif) | OUI | Non corrigé |
| HAUTE | F-04 : rr_target fictif (métriques trompeuses) | PARTIEL | Conséquence de F-07 |
| HAUTE | F-03 : SHARK D_XC manquant | NON (SHADOW mode) | Non bloquant en SHADOW |
| MOYENNE | F-14 : arbiter_enabled fallback mismatch | NON | Correctif BAC-A |
| INFO | F-11/F-12 : stubs ML/SMC | NON | Stubs neutres |

**Conditions MINIMALES avant activation live** :
1. ✗ Résoudre F-05 (BAC-B-01) : SL Engine doit être actif pour BTC DYNAMIC
2. ✗ Résoudre F-07 (BAC-B-02) : Synchroniser QuickExit TP avec FastExitDaemon TP
3. ✗ Valider F-04 : Vérifier que realized_rr = rr effectif dans les logs live
4. ✓ F-14 (BAC-A-01) : Appliquer le correctif 1 ligne (fallback=True) — faible risque

**Verdict global** : DEMO est sûr. Passage en LIVE requiert résolution F-05 + F-07.

---

## RÉSUMÉ EXÉCUTIF

```
┌─────────────────────────────────────────────────────────────────┐
│  HERMES MT5 — ÉTAT LIVE READINESS  (2026-06-22)                │
├─────────────────────────────────────────────────────────────────┤
│  Mode actuel       : DEMO_PILOT_48H — allow_live_trading=False │
│  Ordres live       : IMPOSSIBLE (gardé par config + demo_router)│
│                                                                  │
│  SL Engine BTC     : ✗ DÉSACTIVÉ (bug F-05 — BAC-B-01 requis) │
│  RR affiché/réel   : ✗ DIVERGENT (F-04/F-07 — décision BAC-B) │
│  Gates d'exécution : ✓ CORRECT (bypasses intentionnels codifiés)│
│  Routage sécurisé  : ✓ CORRECT (demo_router seul path)         │
│                                                                  │
│  VERDICT LIVE     : ❌ PAS PRÊT — 2 CRITIQUES NON RÉSOLUS     │
│  VERDICT DEMO     : ✓ OPÉRATIONNEL                             │
└─────────────────────────────────────────────────────────────────┘
```
