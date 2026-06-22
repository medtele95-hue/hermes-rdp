# AUDIT HERMES MT5 — FINDINGS COMPLETS
**Date** : 2026-06-22  |  **Auditeur** : Claude Code (Sonnet 4.6)  |  **Branche** : feature/geo-confluence-hardening

---

## TABLEAU DES FINDINGS

| ID | Fichier:Ligne | Description | Sévérité | Statut | Reproduit numériquement |
|---|---|---|---|---|---|
| F-01 | demo_pilot_events.jsonl | DEMO_SKIP sans near_miss_reason ni block_reason | BASSE | BUG | OUI (17 events null) |
| F-02 | geometric_confluence.py:213 | Bug _validate_harmonic D_XC — DÉJÀ CORRIGÉ | INFO | CORRIGÉ | OUI |
| F-03 | geometric_confluence.py:71-77 | SHARK: D_XC=None (ratio académique manquant) | HAUTE | CONCEPTION | OUI |
| F-04 | btc_dynamic_exit.py:74-77 | rr_target toujours fictif pour BTC (sl/tp caps) | HAUTE | COMPORTEMENT | OUI (calc) |
| F-05 | btc_fast_exit_daemon.py:321-325 | SL Engine jamais actif en mode DYNAMIC (arbiter=True) | CRITIQUE | BUG | OUI (logique) |
| F-06 | btc_fast_exit_daemon.py:190-209 | Confluence score daemon = 50.0 (hardcodé fallback) | MOYENNE | COMPORTEMENT | OUI |
| F-07 | lovable_btc_old_system.py + main.py | QuickExitManager (tp=1.50) court-circuite DYNAMIC exit (tp=6.0) | CRITIQUE | ARCHITECTURE | OUI (RESCUE_CLOSE log) |
| F-08 | main.py:1265-1317 | FINAL_CONFLUENCE_TOO_LOW — seuil non documenté dans .env | MOYENNE | CONCEPTION | OUI (55-65 selon classe) |
| F-09 | setup_hunter.py:1172 | _of_high_quality_legacy_bypass: score≥90 AND grade=A bypasse grade/score checks OF | MOYENNE | CONCEPTION | OUI |
| F-10 | geometric_confluence.py:55-77 | CRAB D_XA range (1.568-1.952) trop large vs académique | BASSE | CONCEPTION | À CONFIRMER |
| F-11 | ml_random_forest_confirmator.py | ML_RF stub NOT_IMPLEMENTED — toujours neutre | INFO | MANQUANT | OUI (stub) |
| F-12 | smc_orderblock_liquidity_narrator.py | SMC_OB stub: smc_ob_avoid=False toujours | INFO | MANQUANT | OUI (stub) |
| F-13 | demo_pilot_events.jsonl | RESCUE_CLOSE à 01:15:18 — QuickExitManager gagne (pas FastExitDaemon) | INFO | OBSERVÉ | OUI (log) |
| F-14 | config.py:185 vs btc_fast_exit_daemon.py:305 | btc_exit_arbiter_enabled défaut=True mais getattr fallback=False | MOYENNE | BUG | OUI (code) |
| F-15 | setup_hunter.py:1298 | ORDER_FLOW_EXECUTION_AGENT absent de strategy_ready_without_confluence | INFO | COMPORTEMENT_ATTENDU | OUI |
| F-16 | tests/test_setup_audit.py + test_strategy_continuity.py | 3 tests FAIL connus | BASSE | TEST | OUI |
| F-17 | setup_hunter.py:61 | _OF_MIN_GEOMETRIC_SCORE=50 appliqué seulement si geo_mode ≠ SHADOW | MOYENNE | COMPORTEMENT | OUI |

---

## DÉTAIL DES FINDINGS CRITIQUES

---

### #FINDING-05 [CRITIQUE] — SL Engine jamais actif en mode DYNAMIC
```
Fichier    : app/mt5/btc_fast_exit_daemon.py:305-325
Description: Quand btc_exit_arbiter_enabled=True (défaut config.py:185) et atr∈[20,60]
             (cas normal BTC), select_exit_manager retourne "DYNAMIC".
             Condition SL Engine: `not _arbiter_enabled or _exit_authority == "SWING"`
             = `not True or "DYNAMIC" == "SWING"` = False
             → SL Engine.run() NE S'EXÉCUTE JAMAIS pour BTC normal.
Preuve     : btc_fast_exit_daemon.py:322: condition = False → skip SL Engine
             config.py:185: btc_exit_arbiter_enabled: bool = True (défaut)
             btc_exit_arbiter.py:18: atr_value=41.4 → "DYNAMIC" (dans 20-60)
Impact USD : SL Engine ne resserre jamais le filet → position gérée uniquement par
             tp_usd=6.0 et rescue_threshold=0.45 du daemon DYNAMIC
             Mais en pratique, QuickExitManager (tp=1.50) clôture d'abord → F-07
Fréquence  : À chaque tick BTC avec atr 20-60 (SYSTÉMATIQUE)
Action     : BAC-A — corriger la condition (voir patch dans audit_remediation.md)
```

---

### #FINDING-07 [CRITIQUE] — QuickExitManager court-circuite FastExitDaemon
```
Fichier    : app/mt5/demo_router.py:241-350 + btc_fast_exit_daemon.py
Description: Deux gestionnaires de sortie actifs simultanément pour BTC :
             1. QuickExitManager (demo_router main cycle ~5-20s) : tp_usd=1.50
             2. FastExitDaemon (thread 250ms) DYNAMIC : tp_usd=6.0
             QuickExitManager clôture à +1.50 USD AVANT le daemon à +6.0 USD.
             PREUVE DIRECTE : event RESCUE_CLOSE 01:15:18 strategy=HERMES_QUICK_EXIT_MANAGER
             (trade ouvert 01:12:56, durée=2m22s, fermé par QuickExitManager)
Preuve     : demo_pilot_events.jsonl ligne RESCUE_CLOSE: strategy=HERMES_QUICK_EXIT_MANAGER
             config.py:86: quick_exit_tp_usd: float = 1.50
             btc_dynamic_exit.py:76: tp_usd = min(6.0, ...) = 6.0
Impact USD : rr_target=2.65 affiché mais RR RÉEL ≈ 1.50/sl_usd_price_equiv (< 1.0)
             Performance affichée > performance réelle — monitoring trompeur
Fréquence  : À chaque clôture profitable BTC SYSTÉMATIQUE
Action     : BAC-B décision : désactiver quick_exit_tp_usd ou le synchroniser avec dynamic TP
```

---

### #FINDING-04 [HAUTE] — rr_target toujours fictif pour BTC
```
Fichier    : app/mt5/btc_dynamic_exit.py:74-77
Description: La confluence_score injectée dans rr_target est sans effet sur BTC.
             sl_usd = max(0.8, min(3.0, atr*1.5)) → TOUJOURS 3.0 pour BTC (atr M5 ≈ 40-60 >> 2.0)
             tp_usd = max(1.0, min(6.0, 3.0*rr_target)) → TOUJOURS 6.0 pour score > 11.76
             realized_rr = tp_usd/sl_usd = 6.0/3.0 = 2.0 INVARIANT
             rr_target loggué (2.37-3.5) est calculé mais jamais atteint (caps)
             ET QuickExitManager ferme à 1.50 avant le daemon à 6.0 (F-07)
Preuve calc: atr=41.4493 → sl=3.0 (capé) ; score=50 → rr=2.65 → tp=7.95 → capé à 6.0
             MAIS QuickExitManager tp_usd=1.50 clôture en premier
Impact USD : rr affiché dans dashboard = 2.0 ou 2.65, rr RÉEL dépend de QuickExit = <1.0
Fréquence  : TOUS les trades BTC
Action     : BAC-B — synchro tp_usd FastExitDaemon ↔ QuickExitManager
```

---

### #FINDING-03 [HAUTE] — SHARK D_XC=None (ratio académique manquant)
```
Fichier    : app/mt5/geometric_confluence.py:71-77
Description: Pattern SHARK définit D_XC=None et D_XA=None.
             Académiquement : SHARK requiert D_XC=0.886 (ratio distinctif).
             Sans ce check, des patterns non-SHARK peuvent être classifiés SHARK.
Preuve     : Spec SHARK lignes 71-77: D_XA=None, D_XC=None → seulement 3 ratios vérifiés/5
             Académique (Carney): SHARK D/C = 0.886 ± 5% → range (0.836, 0.936)
Impact USD : Faux positifs SHARK → score harmonique gonflé (+pattern=15 via geometry_engine)
             → entrées sur patterns invalides en mode ACTIVE/LIVE
Fréquence  : Chaque détection SHARK (fréquence faible mais réel)
Action     : BAC-B décision — ajouter D_XC=(0.836,0.936) au SHARK
```

---

### #FINDING-14 [MOYENNE] — btc_exit_arbiter_enabled default mismatch
```
Fichier    : app/config.py:185 vs app/mt5/btc_fast_exit_daemon.py:305
Description: config.py Settings définit btc_exit_arbiter_enabled: bool = True (défaut)
             MAIS btc_fast_exit_daemon.py utilise getattr(self.settings, ..., False)
             Le fallback est False, pas True. Si l'attribut manque → arbiter DISABLED.
Preuve     : config.py:185: btc_exit_arbiter_enabled: bool = True
             daemon:305: getattr(self.settings, "btc_exit_arbiter_enabled", False) is True
             Si settings n'est pas un objet Settings complet, fallback=False
Impact USD : Dans des tests ou configs partielles: arbiter=False →
             SL Engine actif au lieu de DYNAMIC exit → comportement différent de production
Fréquence  : Tests avec settings mocks, configs partielles
Action     : BAC-A — aligner le fallback sur True
```

---

### #FINDING-06 [MOYENNE] — Daemon confluence_score hardcodé 50.0
```
Fichier    : app/mt5/btc_fast_exit_daemon.py:190-209
Description: Le daemon récupère la confluence_score depuis btc_intelligence (local state).
             Si unavailable: _cscore = 50.0 (fallback hardcodé).
             Cette valeur est passée à BtcDynamicExit.compute(confluence_score=50.0).
             MAIS le routeur a utilisé une score différente (ex: 33.67 ou ≠ 50).
Preuve     : daemon:203: _cscore = float(_intel.get("confluence_score") or 50.0)
Impact USD : En pratique NULS (sl/tp toujours capés pour BTC, F-04).
             Divergence conceptuelle entre score routeur et score daemon.
Fréquence  : Chaque tick sans btc_intelligence disponible
Action     : BAC-C — logger la source de _cscore dans daemon pour traçabilité
```

---

### #FINDING-08 [MOYENNE] — Seuil FINAL_CONFLUENCE_TOO_LOW variable et non loggué
```
Fichier    : app/main.py:1265-1279
Description: Le seuil de confluence n'est PAS une constante mais calculé selon strategy_class.
             ORDER_FLOW_NATIVE: 58.0 | SMC_NATIVE: 65.0 | DEFAULT: 62.0
             Quand hermes_confluence_strategy_aware=False (défaut!): _conf_threshold=55.0
             Le seuil utilisé N'EST PAS loggué dans [ROUTER_BLOCK].
Preuve     : main.py:1270: if _conf_strat_aware and bool(_conf):
             config.py:176: hermes_confluence_strategy_aware: bool = False (DÉFAUT!)
             → _conf_threshold = 55.0 pour TOUTES les stratégies par défaut
Fait confirmé: DEMO_ORDER pour OF BTCUSD# : score_breakdown_sum≈33, threshold≈55 → blocked?
             MAIS trade a quand même été exécuté! → FINAL_CONFLUENCE_TOO_LOW n'a pas bloqué
             → Soit: _conf={} (evaluate_confluence a échoué) ou _old_btc_route_bypass=True
Impact USD : Décisions ambiguës — le seuil réel est inconnu à la lecture des logs seuls
Fréquence  : Chaque cycle
Action     : BAC-A — logger _conf_threshold dans [ROUTER_BLOCK] et [FINAL_VERDICT]
```

---

### #FINDING-09 [MOYENNE] — OF legacy bypass non documenté
```
Fichier    : app/agents/setup_hunter.py:1172-1192
Description: Un second bypass pour ORDER_FLOW_EXECUTION_AGENT: si setup_score>=90 AND grade=A
             alors les checks ORDER_FLOW_CONFLUENCE_GRADE_BELOW_B et ORDER_FLOW_CONFLUENCE_SCORE_BELOW_65
             sont ignorés. Ce bypass permet d'exécuter un trade OF même si:
             - confluence_grade < B  (ex: grade=C mais score=91 et grade="A")
             - confluence_score < 65
Preuve     : setup_hunter.py:1172: _of_high_quality_legacy_bypass = _of_setup_score >= 90.0 and _of_grade == "A"
             La condition utilise _of_grade = payload.get("final_confluence_grade") OR "D"
             → si final_confluence_grade absent: grade="D" → _of_high_quality_legacy_bypass=False ✓
Impact USD : Trades OF potentiellement sans confluence suffisante si grade=A avec score>=90
Fréquence  : Conditionnel (score>=90 AND grade=A)
Action     : BAC-B — décision: maintenir ou supprimer ce legacy bypass
```

---

## FINDINGS MODULE NOT_IMPLEMENTED

### ML_RF_CONFIRMATOR (F-11)
- **Stub** : retourne toujours `ml_status=UNAVAILABLE, ml_reason=NOT_IMPLEMENTED`
- **Impact** : Zéro — `UNAVAILABLE` est strictement neutre (setup_hunter.py:407)
- **Décision** : `_ml_status` est loggué mais n'influence aucune décision
- **Risque live** : Aucun (garde neutre par conception)

### SMC_OB_NARRATOR, SMC_OB_AVOID, SMC_OB_ENTRY_WINDOW (F-12)
- **Stub** : `smc_ob_avoid=False` (ne bloque jamais), `smc_ob_entry_window=None`
- **Impact** : Pas de filtrage des orderblocks proches → entrées possibles dans des OB défavorables
- **Risque live** : MOYEN — en live, certains OB peuvent agir comme zones de résistance/support

---

## STATUT DES FINDINGS CONNUS (prompt original)

| Finding | Validé | Résultat |
|---|---|---|
| Exemption confluence OF (setup_hunter.py:1104-1121) | ✓ CONFIRMÉ | ligne 1139-1208 — OF a ses propres checks plus stricts |
| Pénalités SMC/MTFA forfaitaires | ✓ CONFIRMÉ | confirmation_matrix.py:36 → ±10/±5/±15 flat |
| Bug `_validate_harmonic` D_XC | ✓ CORRIGÉ | ligne 213 — déjà patché dans code actuel |
| Méthode A = filet fixe | ✓ CONFIRMÉ | btc_sl_engine.py:131-135 |
| `rr_target` pré-capping loggué comme `rr` | ✓ CONFIRMÉ | + réalisé dans caps (F-04) |
| `pattern=15` vient de geometry_engine.py:532 | ✓ CONFIRMÉ | ligne 532 exacte |
| Sorties concurrentes OLD_BTC vs DYNAMIC | ✓ RÉSOLU | QuickExitManager gagne (F-07) |
| Seuil FINAL_CONFLUENCE_TOO_LOW non loggué | ✓ CONFIRMÉ | 55.0 par défaut (F-08) |
| Deux scores divergents (33.67 vs 50.0) | ✓ CONFIRMÉ | mais sans effet (F-04, F-06) |
| LOVABLE_INGEST circuit breaker | À CONFIRMER | ingest_client.py circuit breaker logic à vérifier |
| POSITION_SYNC fallback local | À CONFIRMER | mt5_position_sync.py à lire |
