# AUDIT HERMES MT5 — FORMULES MATHÉMATIQUES VÉRIFIÉES
**Date** : 2026-06-22  |  **Source** : lecture intégrale des fichiers source

---

## FORMULE-01 — rr_target (btc_dynamic_exit.py:74)
```
Source     : app/mt5/btc_dynamic_exit.py:74
Expression : rr_target = 1.8 + (confluence_score / 100.0) * 1.7
Range      : [1.8 (score=0), 3.5 (score=100)]

Test 1 (valeur réelle score=33.67) : 1.8 + (33.67/100)*1.7 = 1.8 + 0.5724 = 2.3724   [OK]
Test 2 (valeur réelle score=50.0)  : 1.8 + (50/100)*1.7   = 1.8 + 0.85   = 2.65      [OK]
Test 3 (edge case score=0)         : 1.8 + 0 = 1.8                                    [OK plancher]
Test 4 (edge case score=100)       : 1.8 + 1.7 = 3.5                                  [OK plafond]
Test 5 (edge case score<0)         : valeur hors spec — pas de garde (min non appliqué)
Verdict    : CORRECTE mathématiquement — TROMPEUSE (voir sl_usd caps ci-dessous)
```

## FORMULE-02 — sl_usd cap (btc_dynamic_exit.py:75)
```
Source     : app/mt5/btc_dynamic_exit.py:75
Expression : sl_usd = max(0.8, min(3.0, atr * 1.5))
Cap haut actif quand : atr > 3.0/1.5 = 2.0 → TOUJOURS pour BTC (atr M5 ≈ 40-60)
Cap bas actif quand  : atr < 0.8/1.5 = 0.533

Test 1 (atr=41.4493) : 41.4493*1.5 = 62.17 → min(3.0,62.17)=3.0 → max(0.8,3.0)=3.0  [CAPÉ HAUT]
Test 2 (atr=2.0)     : 2.0*1.5 = 3.0 → seuil exact du cap haut
Test 3 (atr=0.5)     : 0.5*1.5 = 0.75 → max(0.8,0.75)=0.8                            [CAPÉ BAS]
Test 4 (atr=0)       : 0 → max(0.8,0) = 0.8                                           [CAPÉ BAS]
Verdict    : CORRECTE — BTC TOUJOURS dans le cap haut (sl_usd = 3.0 invariant)
```

## FORMULE-03 — tp_usd cap (btc_dynamic_exit.py:76)
```
Source     : app/mt5/btc_dynamic_exit.py:76-77
Expression : tp_usd = max(1.0, min(6.0, sl_usd * rr_target))
             realized_rr = round(tp_usd / sl_usd, 4)
Cap haut tp actif quand: rr_target > 6.0/sl_usd
  Pour sl=3.0: rr > 2.0 → cap actif pour score > 11.76 (TOUJOURS en pratique)

Test 1 (sl=3.0, rr=2.65) : sl*rr=7.95 → min(6.0,7.95)=6.0 → max(1.0,6.0)=6.0      [CAPÉ]
                            realized_rr = 6.0/3.0 = 2.0                                [≠ rr_target!]
Test 2 (sl=3.0, rr=2.0)  : sl*rr=6.0 → exactement au cap → tp=6.0
Test 3 (sl=0.8, rr=2.65) : sl*rr=2.12 → pas de cap → tp=2.12 → realized_rr=2.12/0.8=2.65 ✓
Test 4 (sl=3.0, score=0) : rr=1.8 → tp=3.0*1.8=5.4 → tp=5.4 → realized_rr=1.8 = rr_target ✓
Verdict    : CORRECTE — TROMPEUSE: BTC toujours capé → realized_rr=2.0 ≠ rr_target affiché
             NB: QuickExitManager (tp=1.50) clôture AVANT daemon (tp=6.0) → RR effectif ≠ 2.0
```

## FORMULE-04 — SL Méthode A - BtcSlEngine (btc_sl_engine.py:131-135)
```
Source     : app/mt5/btc_sl_engine.py:131-135
Expression : SELL → sl = entry + atr * 1.5
             BUY  → sl = entry - atr * 2.0
Multiplicateurs : 1.5 (SELL), 2.0 (BUY) — asymétrie intentionnelle

Test 1 (SELL, entry=63852.50, atr=41.4493) :
   63852.50 + 41.4493 * 1.5 = 63852.50 + 62.17395 = 63914.67395                     [✓ ATTENDU]
Test 2 (BUY, entry=63852.50, atr=41.4493)  :
   63852.50 - 41.4493 * 2.0 = 63852.50 - 82.8986  = 63769.6014
Test 3 (atr=0) : sl = entry (SL au prix d'entrée — risque théorique nul)             [OK edge]
Test 4 (SELL) : vérification anti-inversion (btc_sl_engine.py:209)
   si new_sl <= entry_price → ANTI_INVERSION → remplace par HARD_CAP (MAX_SL=8 USD)
Verdict    : CORRECTE
```

## FORMULE-05 — rescue_threshold dynamique (btc_fast_exit_daemon.py:381)
```
Source     : app/mt5/btc_fast_exit_daemon.py:381
Expression : _rescue_threshold = max(0.05, _lock * 0.3)
             où _lock = sl_usd * 0.5 (depuis BtcDynamicExit.FALLBACK ou compute)

Pour BTC (sl_usd=3.0 → _lock=1.5) :
Test 1 (sl=3.0) : _lock=1.5 → max(0.05, 1.5*0.3) = max(0.05, 0.45) = 0.45 USD      [✓ ATTENDU]
Test 2 (sl=0.8) : _lock=0.4 → max(0.05, 0.4*0.3) = max(0.05, 0.12) = 0.12 USD
Test 3 (sl=0)   : _lock=0   → max(0.05, 0) = 0.05 USD                                [OK plancher]
NB: Si was_neg=True et profit >= 0.45 → NEGATIVE_THEN_TINY_POSITIVE close
    Si was_neg=False: pas de rescue (attend TP=6.0 ou trail)
Verdict    : CORRECTE — mais valeur 0.45 N'EST PAS une constante hard-codée
```

## FORMULE-06 — Pénalité _compute_penalty SMC/MTFA (setup_hunter.py:1044-1053)
```
Source     : app/agents/setup_hunter.py:1044-1053
Expression : if score >= 80.0: return +10.0   (FULL_BONUS)
             if score >= 50.0: return 0.0     (NEUTRAL)
             if score >= 20.0: return -5.0    (SOFT)
             else:             return -15.0 * (1.0 + (20.0 - score) / 20.0)
             Note: la fonction est documentée pour BTC_SCALPING_AGENT seulement
             (ligne 1246: _btc_confluence = _btc_setup + _compute_penalty(smc) + _compute_penalty(mtfa))

Test 1 (score=80) : retourne +10.0
Test 2 (score=60) : retourne 0.0
Test 3 (score=30) : retourne -5.0
Test 4 (score=0)  : -15.0 * (1 + 20/20) = -15.0 * 2.0 = -30.0
Test 5 (score=20) : -15.0 * (1 + 0/20)  = -15.0 * 1.0 = -15.0
Verdict    : CORRECTE — pénalité progressive (non-flat) pour scores < 20
```

## FORMULE-07 — Pénalités confirmation_matrix.py (confirmation_matrix.py:36)
```
Source     : app/agents/confirmation_matrix.py:36
Expression : _ADJUSTMENTS = {"PASS": 10.0, "SOFT_FAIL": -5.0, "STRONG_FAIL": -15.0}
             SMC PASS   >= 70  | SOFT_FAIL 40-69 | STRONG_FAIL < 40
             MTFA PASS  >= 60  | SOFT_FAIL 35-59 | STRONG_FAIL < 35
             hard_block: STRONG_FAIL pour ORDER_FLOW (toujours) ou autres si setup<75 ET rr<1.5

Test ORDER_FLOW, smc_score=0, setup=50, rr=1.4 :
   smc_status=STRONG_FAIL → hard_block=True (OF toujours hard-bloqué sur STRONG_FAIL) ✓
Test autres stratégies, smc_score=30, setup=80, rr=2.0 :
   smc_status=STRONG_FAIL, confluence_ok=True, rr_ok=True → hard_block=False ✓
Verdict    : CORRECTE — FLAT (non-proportionnel) par conception
```

## FORMULE-08 — Ratios harmoniques patterns (geometric_confluence.py:42-85)
```
Source     : app/mt5/geometric_confluence.py:42-85
Comparaison aux références académiques (Carney "Harmonic Trading"):

GARTLEY  : AB_XA=(0.568,0.668) → acad=0.618 ✓ (±0.05)
           BC_AB=(0.382,0.886)  → acad=0.382-0.886 ✓
           CD_BC=(1.272,1.618)  → acad=1.13-1.618 ≈ ok (min légèrement haut)
           D_XA=(0.736,0.836)   → acad=0.786 ✓ (±0.05)
           D_XC=None            → acad=N/A ✓

BAT      : AB_XA=(0.332,0.550) → acad=0.382-0.500 ≈ ok (légèrement large)
           D_XA=(0.836,0.936)  → acad=0.886 ✓

BUTTERFLY: D_XA=(1.27,1.618)   → acad=1.272 ou 1.618 ✓ (range couvre les deux)
           AB_XA=(0.736,0.836) → acad=0.786 ✓

CRAB     : CD_BC=(2.568,3.668) → acad=2.618-3.618 ✓
           D_XA=(1.568,1.952)  → acad>=1.618 (range trop large vers le bas: 1.568 < 1.618)
           [FINDING-10: CRAB D_XA min trop faible]

SHARK    : AB_XA=(1.080,1.668) → acad=1.13-1.618 ≈ ok
           D_XC=None           → acad=0.886 MANQUANT [FINDING-03]
           D_XA=None           → acad: variable, souvent pas de spec fixe ✓

CYPHER   : D_XC=(0.736,0.836)  → acad=0.786 ✓
           AB_XA=(0.332,0.668) → acad=0.382-0.618 ≈ ok

GEOMETRIC_FIB_HIT_TOLERANCE = 0.015 (1.5%) — plus strict que standard (souvent 5%)
```

## FORMULE-09 — dollars_to_price conversion (btc_sl_engine.py:71-81)
```
Source     : app/mt5/btc_sl_engine.py:71-81
Expression : price_diff = sl_dollars / lot  (lot=0.01 par défaut)
             BUY:  sl_price = entry_price - price_diff
             SELL: sl_price = entry_price + price_diff

Test (entry=63852.50, sl_dollars=3.0, lot=0.01, BUY) :
   price_diff = 3.0 / 0.01 = 300.0 points
   sl_price = 63852.50 - 300.0 = 63552.50

NB: Pour BTC 0.01 lot, 1 USD P&L = 1 price point. Formule cohérente.
Verdict    : CORRECTE
```
