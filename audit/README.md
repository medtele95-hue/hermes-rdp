# AUDIT HERMES MT5 — INDEX
**Date** : 2026-06-22  |  **Branche** : feature/geo-confluence-hardening  |  **Auditeur** : Claude Code (Sonnet 4.6)

---

## FICHIERS D'AUDIT

| Fichier | Contenu |
|---|---|
| [audit_findings.md](audit_findings.md) | 17 findings complets (F-01 à F-17) avec sévérité, preuves, impact USD |
| [audit_formules.md](audit_formules.md) | 9 formules mathématiques vérifiées numériquement (FORMULE-01 à 09) |
| [audit_remediation.md](audit_remediation.md) | Plan BAC A/B/C avec diffs exacts pour les correctifs |
| [audit_strategies.md](audit_strategies.md) | Inventaire complet des stratégies + modules NOT_IMPLEMENTED |
| [audit_dashboard.md](audit_dashboard.md) | Audit frontend local_dashboard/ — état et findings D-01 à D-04 |
| [audit_live_readiness.md](audit_live_readiness.md) | Checklist live readiness — 5 questions binaires |

---

## RÉSUMÉ CRITIQUE

```
FINDINGS CRITIQUES (2) :
  F-05 — SL Engine désactivé en mode DYNAMIC (btc_fast_exit_daemon.py:325)
  F-07 — QuickExitManager (tp=1.50) court-circuite FastExitDaemon (tp=6.0)

FINDINGS HAUTS (2) :
  F-04 — rr_target toujours fictif pour BTC (sl/tp caps invariants)
  F-03 — SHARK D_XC=None (ratio académique 0.886 manquant)

FINDINGS MOYENS (4) : F-06, F-08, F-09, F-14, F-17
FINDINGS BAS/INFO (9) : F-01, F-02, F-10, F-11, F-12, F-13, F-15, F-16

VERDICT LIVE : ❌ PAS PRÊT — F-05 + F-07 doivent être résolus
VERDICT DEMO : ✓ OPÉRATIONNEL
```

---

## ACTIONS IMMÉDIATES (BAC-A, sans décision)

1. **BAC-A-01** (F-14) : `btc_fast_exit_daemon.py:305` — changer `False` → `True` (fallback arbiter)
2. **BAC-A-02** (F-08) : `main.py:1296` — ajouter `threshold=%s strat_aware=%s` au log ROUTER_BLOCK

## DÉCISIONS REQUISES (BAC-B)

3. **BAC-B-01** (F-05) : SL Engine condition — ajouter `"DYNAMIC"` à la condition ou désactiver l'arbiter
4. **BAC-B-02** (F-07) : `quick_exit_tp_usd=1.50` vs DYNAMIC `tp=6.0` — hausser/désactiver/laisser ?
5. **BAC-B-03** (F-03) : Ajouter `D_XC=(0.836,0.936)` au pattern SHARK (impact nul en SHADOW)
