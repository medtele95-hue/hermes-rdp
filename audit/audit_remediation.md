# AUDIT HERMES MT5 — PLAN DE REMÉDIATION
**Date** : 2026-06-22  |  **Branche** : feature/geo-confluence-hardening

---

## LÉGENDE
- **BAC-A** : Action immédiate — correctif sûr, sans changement de comportement intentionnel
- **BAC-B** : Décision requise — changement de comportement, valider avec l'utilisateur
- **BAC-C** : Améliorations futures — observabilité, qualité de code, non-bloquant

---

## BAC-A — CORRECTIFS IMMÉDIATS (2 items)

### BAC-A-01 — F-14 : Aligner le fallback btc_exit_arbiter_enabled

**Finding** : F-14 [MOYENNE]  
**Fichier** : `app/mt5/btc_fast_exit_daemon.py:305`  
**Risque** : Dans des tests/configs partielles, arbiter=False (SL Engine active, comportement divergeant)

```diff
# app/mt5/btc_fast_exit_daemon.py:305
- _arbiter_enabled = getattr(self.settings, "btc_exit_arbiter_enabled", False) is True
+ _arbiter_enabled = getattr(self.settings, "btc_exit_arbiter_enabled", True) is True
```

**Justification** : `config.py:185` déclare `btc_exit_arbiter_enabled: bool = True` (défaut).
Le fallback doit correspondre au défaut intentionnel. Un `getattr(..., False)` force l'ancien
comportement (pré-arbiter) lors de configs partielles ou de mocks de test.

**Impact** : Alignement de comportement entre production et tests. Aucun changement de comportement
si `settings` est un objet `Settings` complet (déjà `True`).

**Statut de la ligne actuelle** : `btc_fast_exit_daemon.py:305` — confirmé via lecture directe.

---

### BAC-A-02 — F-08 : Logger le seuil confluence dans [ROUTER_BLOCK]

**Finding** : F-08 [MOYENNE]  
**Fichier** : `app/main.py:1296`  
**Risque** : Le seuil réel appliqué est invisible dans les logs — décisions ambiguës

```diff
# app/main.py:1296
- log.info(
-     "[ROUTER_BLOCK] symbol=%s strategy=%s reason=FINAL_CONFLUENCE_TOO_LOW score=%s grade=%s",
-     broker_symbol, handoff_strategy, _final_conf_score, _final_conf_grade,
- )
+ log.info(
+     "[ROUTER_BLOCK] symbol=%s strategy=%s reason=FINAL_CONFLUENCE_TOO_LOW score=%s grade=%s threshold=%s strat_aware=%s",
+     broker_symbol, handoff_strategy, _final_conf_score, _final_conf_grade,
+     _conf_threshold, _conf_strat_aware,
+ )
```

**Justification** : Avec `hermes_confluence_strategy_aware=False` (défaut), le seuil est toujours 55.0
mais pourrait passer à 58/65/62 si activé. Les logs actuels n'exposent pas le seuil utilisé,
rendant le dépannage des ROUTER_BLOCK impossible sans lecture du code.

**Impact** : Observabilité uniquement. Aucun changement de logique.

---

## BAC-B — DÉCISIONS REQUISES (3 items)

### BAC-B-01 — F-05 : SL Engine désactivé en mode DYNAMIC

**Finding** : F-05 [CRITIQUE]  
**Fichier** : `app/mt5/btc_fast_exit_daemon.py:321-325`  
**État actuel** : SL Engine ne s'exécute JAMAIS pour BTC en mode DYNAMIC (cas normal atr=40-60)

**Condition actuelle (ligne 325)** :
```python
and (not _arbiter_enabled or _exit_authority == "SWING")
# = (not True or "DYNAMIC" == "SWING") = False → SL Engine skipped
```

**Option B-01-A — Activer SL Engine pour DYNAMIC** (recommandée) :
```diff
# app/mt5/btc_fast_exit_daemon.py:325
- and (not _arbiter_enabled or _exit_authority == "SWING")
+ and (not _arbiter_enabled or _exit_authority in {"SWING", "DYNAMIC"})
```
**Effet** : SL Engine s'exécute pour DYNAMIC (throttle 30s). Trailing stop actif pour BTC normal.
**Risque** : Changement de comportement réel — SL pourrait être resserré avant rescue.

**Option B-01-B — Désactiver l'arbiter pour restaurer le comportement pré-arbiter** :
```diff
# config.py:185
- btc_exit_arbiter_enabled: bool = True
+ btc_exit_arbiter_enabled: bool = False
```
**Effet** : Retour à l'ancien chemin (SL Engine actif, pas de sélection QUICK/DYNAMIC/SWING).

**Option B-01-C — Laisser tel quel (décision de conception)** :
Si DYNAMIC exit (tp=6.0 + rescue=0.45) est suffisant, le SL Engine est redondant.
À valider : est-ce intentionnel que le SL Engine ne fonctionne pas en mode DYNAMIC ?

**DÉCISION REQUISE** : A / B / C (défaut recommandé : A)

---

### BAC-B-02 — F-07 : Synchronisation QuickExitManager ↔ FastExitDaemon

**Finding** : F-07 [CRITIQUE]  
**Fichier** : `config.py:86` + `app/mt5/demo_router.py` + `app/mt5/btc_fast_exit_daemon.py`  
**État actuel** : QuickExitManager (tp=1.50) clôture AVANT le daemon DYNAMIC (tp=6.0)
**Preuve** : RESCUE_CLOSE 01:15:18 — strategy=HERMES_QUICK_EXIT_MANAGER

**Option B-02-A — Hausser quick_exit_tp_usd pour aligner avec DYNAMIC** :
```diff
# config.py:86
- quick_exit_tp_usd: float = 1.50
+ quick_exit_tp_usd: float = 6.00  # align with BtcDynamicExit tp_usd cap
```
**Effet** : QuickExitManager ne clôture plus avant le daemon. RR effectif = 2.0 (6.0/3.0).
**Risque** : Trades restent ouverts plus longtemps, exposition augmentée.

**Option B-02-B — Désactiver QuickExitManager pour BTC** :
```python
# demo_router.py : conditionner QuickExitManager selon _exit_authority
if _exit_authority == "QUICK":
    # appliquer QuickExitManager
    ...
```
**Effet** : QuickExitManager ne s'applique que si l'arbiter a sélectionné QUICK (atr<20).
**Risque** : Changement architectural — vérifier demo_router.py intégralement.

**Option B-02-C — Laisser tel quel (capture rapide intentionnelle)** :
Si tp=1.50 est une stratégie de capture rapide démonstrative, garder tel quel.
Mais documenter dans les métriques que RR affiché ≠ RR réel.

**DÉCISION REQUISE** : A / B / C

---

### BAC-B-03 — F-03 : Ajouter ratio D_XC au pattern SHARK

**Finding** : F-03 [HAUTE]  
**Fichier** : `app/mt5/geometric_confluence.py:71-77`  
**État actuel** : SHARK D_XC=None — ratio D/C académique (0.886) non vérifié

**Patch proposé** :
```diff
# geometric_confluence.py:71-77 (dans _SPECS dict)
  "SHARK": {
      "AB_XA": (1.080, 1.668),
      "BC_AB": (1.132, 1.932),
      "CD_BC": (1.618, 2.236),
-     "D_XA": None,
-     "D_XC": None,
+     "D_XA": None,
+     "D_XC": (0.836, 0.936),  # academic: 0.886 ±0.05
  },
```

**Effet** : Les patterns SHARK sont maintenant filtrés sur le ratio D/C (0.836-0.936).
Faux positifs SHARK réduits. Mode SHADOW : bonus toujours 0 (pas d'impact sur routage actuel).

**Risque** : SHADOW mode → aucun impact sur exécution. Risque quasi-nul.
En mode ACTIVE (futur) : détection SHARK plus stricte, moins de signaux.

**DÉCISION REQUISE** : Appliquer le patch oui/non (recommandé : OUI)

---

## BAC-C — AMÉLIORATIONS FUTURES (3 items)

### BAC-C-01 — F-06 : Logger la source de _cscore dans le daemon

**Fichier** : `app/mt5/btc_fast_exit_daemon.py:203`  
```diff
- _cscore = float(_intel.get("confluence_score") or 50.0)
+ _cscore = float(_intel.get("confluence_score") or 50.0)
+ _cscore_source = "btc_intelligence" if _intel.get("confluence_score") else "FALLBACK_50"
+ log.debug("[BTC_DAEMON_CSCORE] ticket=%s cscore=%.1f source=%s", ticket, _cscore, _cscore_source)
```

---

### BAC-C-02 — F-01 : Propager near_miss_reason dans les events DEMO_SKIP

**Fichier** : `app/mt5/demo_router.py` — section DEMO_SKIP event  
**Action** : Transmettre les `failed_gates` du candidat à `near_miss_reason` et `eligibility_block_reasons`
dans l'event DEMO_SKIP émis par le routeur.

---

### BAC-C-03 — F-17 : Guard geo_mode avant _OF_MIN_GEOMETRIC_SCORE

**Fichier** : `app/agents/setup_hunter.py:~1175`  
**Situation** : `_OF_MIN_GEOMETRIC_SCORE=50.0` est appliqué mais le score géométrique est 0
en mode SHADOW (geometric_confluence.py:23: `GEOMETRIC_CONFLUENCE_MODE="SHADOW"` → bonus=0).
En SHADOW, le check `geo_score < 50` bloque inutilement l'ORDER_FLOW si aucun signal géo.

**Action** : Ajouter un guard conditionnel sur `GEOMETRIC_CONFLUENCE_MODE != "SHADOW"` avant le check.

---

## PRIORITÉ D'EXÉCUTION

| Priorité | ID | Effort | Risque | Blocage live |
|---|---|---|---|---|
| 1 | BAC-A-01 (F-14) | 1 ligne | Très faible | NON |
| 2 | BAC-A-02 (F-08) | 2 lignes | Très faible | NON |
| 3 | BAC-B-01 (F-05) | 1 ligne | Moyen | POTENTIEL |
| 4 | BAC-B-02 (F-07) | Décision config | Moyen | POTENTIEL |
| 5 | BAC-B-03 (F-03) | 1 ligne | Faible (SHADOW) | NON |
| 6 | BAC-C-01/02/03 | Faible | Très faible | NON |
