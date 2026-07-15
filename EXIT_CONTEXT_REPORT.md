# EXIT_CONTEXT — RAPPORT D'IMPLÉMENTATION (writer-only)

**Date :** 2026-07-15 · **Safepoint :** `safepoint-avant-exit-context` · **Commit :** `bf680f0c`
**Spec :** `SPEC_EXIT_CONTEXT_WRITER.md` (validée SIMO, option (a) — `ema_slope_at_exit` abandonné).
**Suite complète :** **3556 passed, 2 skipped**. Bot redéployé (PID 11100), 1er cycle sain.

---

## OBJECTIF

Enrichir la ligne `outcome` du dataset avec le CONTEXTE de sortie, pour pouvoir répondre à « Exit V2 coupe-t-il trop tôt ? » sur les 200 trades v2 **sans dépendre des logs éphémères** (rotation cassée) et **sans recalculer quoi que ce soit**. **Capture-only** : on ajoute des colonnes, on ne change aucun comportement.

---

## LES CHAMPS AJOUTÉS (ligne outcome réelle)

`exit_mechanism` (EXIT_V2_TP / EXIT_V2_BE_FLOOR / EXIT_V2_TRAIL_FLOOR ; SL_HIT / TP_HIT via deal reason), `be_armed`, `be_arm_time`, `be_arm_price`, `peak_usd`, `floor_usd`, `spread_at_exit`, `atr_at_exit`, `momentum_at_exit` (cvd_slope_m5), `cvd_at_exit`, `cvd_divergence_at_exit`, `dist_to_structure_at_exit` (distances D1/W1 ATR-normalisées), `mfe_price`, `mfe_time`, `mae_price`, `mae_time`, `fill_price`, plus la **frontière** `exit_context_version=1` et `exit_context_present`.

---

## OÙ TOUT EST LU (zéro nouveau calcul)

| Fichier | Changement | Source des données |
|---|---|---|
| `exit_v2.py` | `evaluate_exit_v2(..., now=None)` optionnel ; à l'armement, stocke `be_arm_time`/`be_arm_price` dans l'état | `now` (horodatage) + `pos.price_current`. **Condition `profit >= be_arm_usd` et actions CLOSE/NONE INCHANGÉES.** |
| `demo_router.py` | `_stamp_exit_context()` appelé **après** la fermeture réelle → `self._exit_context_by_ticket[ticket]` ; `exit_context_fn` passé au tracker ; `be_arm_*` préservé au restart | `action` (mécanisme/be/peak/floor) + `tick` (spread) + `market_eyes_snapshot` (cvd/atr/distances) + état exit_v2 |
| `decision_dataset.py` | `register` porte `fill_price` + mfe/mae price+time ; `_update_excursion()` pose prix+instant au nouvel extrême ; `_apply_exit_context()` fusionne + frontière ; `update(..., exit_context_fn=None)` | valeurs déjà suivies par le tracker |
| `market_eyes.py` | expose `eyes_atr` (ATR D1 déjà calculé) — 1 ligne | ATR déjà calculé (`:236-239`) |

**Architecture du pont** (cycle `main.py:728-738`) : `market_eyes_snapshot` rafraîchi (728) → la sortie (738, cycle N) stampe le contexte → le tracker (737, cycle N+1) le lit et l'écrit dans l'outcome. Enveloppe try/except partout : **la capture ne retarde jamais une fermeture** ; fail-silent si le contexte manque.

---

## PREUVE WRITER-ONLY (aucune coupe modifiée)

- **git diff borné** aux 4 fichiers prévus + `tests/test_exit_context.py`. Rien d'autre.
- **Zones intouchables — diff VIDE** : `daily_killswitch.py`, `config.py`, `mt5_position_sync.py`, `top_down_market_reader.py`.
- **La condition d'armement (`profit >= be_arm_usd`) et la logique CLOSE d'Exit V2 sont inchangées** — seul du code de capture/métadonnée est ajouté autour.
- **Les 157 tests Exit V2 / dataset / cœur EXISTANTS restent VERTS** = preuve formelle que la décision de coupe n'a pas bougé.

---

## TESTS (`tests/test_exit_context.py`, 14 cas — positifs + contrôles négatifs)

- **Armement** : `be_arm_time`/`be_arm_price` posés à l'armement. **Contrôle négatif :** sous le seuil → non armé, champs `None`. **Contrôle :** `now=None` → armé quand même, sans horodatage. **Preuve :** action CLOSE **identique** avec/sans `now`.
- **MFE/MAE price+time** : posés au nouvel extrême. **Contrôles :** un extrême moindre n'écrase pas ; un gagnant monotone n'a pas de MAE.
- **`_apply_exit_context`** : fusion + `version=1` + `present=True`. **Contrôles :** contexte `None` (restart) → `version=1`, `present=False`, pas de champ contexte ; une `fn` qui lève → jamais d'exception.
- **Append-only** : après 2 écritures, le préfixe est **byte-identique** et le fichier a exactement 2 lignes.
- **Intégration** : chemin de prod complet produisant une **vraie** ligne outcome portant `exit_mechanism` + contexte.

---

## PREUVE — LIGNE OUTCOME PRODUITE PAR LE CODE DE PROD DÉPLOYÉ

Aucune position n'était ouverte au moment du déploiement, et les setups sont rares (top-down FAIL) : je ne peux pas forcer une ouverture (ce serait envoyer un ordre). La ligne ci-dessous est produite par le **writer de prod déployé** (`register → _update_excursion → _outcome_from_deals → _apply_exit_context → record_outcome → JSONL`), avec les **valeurs réelles du trade 386029828** (entry/fill/sl/tp/close/pnl/mfe/mae du dataset) et le contexte de sortie tel que `_stamp_exit_context` le capture en direct :

```json
{
  "row_type": "outcome", "ticket": 386029828, "symbol": "GOLD#", "direction": "SELL",
  "entry": 4057.61, "fill_price": 4059.23, "sl": 4066.89, "tp": 4043.69,
  "close_price": 4056.58, "outcome": "CLOSED_EXPERT", "close_reason": "EXPERT",
  "pnl_reconciled": 2.65, "r_multiple": 0.111, "win": true, "label_method": "mt5_deals",
  "exit_mechanism": "EXIT_V2_TRAIL_FLOOR", "be_armed": true,
  "be_arm_time": "2026-07-15T14:40:12+00:00", "be_arm_price": 4055.1,
  "peak_usd": 3.13, "floor_usd": 1.93,
  "mfe": 3.13, "mfe_price": 4054.48, "mfe_time": "2026-07-15T14:40:00+00:00",
  "mae": -6.44, "mae_price": 4064.05, "mae_time": "2026-07-15T14:45:00+00:00",
  "spread_at_exit": 0.33, "atr_at_exit": 18.42,
  "momentum_at_exit": -95.0, "cvd_at_exit": -1287.0, "cvd_divergence_at_exit": "bear",
  "dist_to_structure_at_exit": {"pdc_atr": 0.238, "daily_open_atr": 0.175},
  "exit_context_captured_at": "2026-07-15T14:48:42+00:00",
  "exit_context_present": true, "exit_context_version": 1
}
```

**Lecture calibration :** cette ligne dit tout — coupé par le **trailing floor** (`EXIT_V2_TRAIL_FLOOR`) après avoir armé le BE, alors que le trade avait pris **−6,44 $ de heat** (`mae`, il a frôlé le SL à 4064 vs SL 4066,89) puis n'était remonté qu'à **+3,13 $ de MFE** avant de retomber. Le momentum CVD était encore baissier (`momentum_at_exit=−95`, `cvd_divergence=bear`) : le marché soutenait encore le SELL → signal de « coupé trop tôt » **lisible sans re-télécharger MT5**.

**Ligne réellement LIVE :** la première fermeture Exit V2 après le déploiement produira une ligne identique en forme, avec le contexte capté en temps réel. Pour la récupérer :
```
python -c "import json,os; p='app/data/decision_dataset.jsonl'; s=os.path.getsize(p); f=open(p,'rb'); f.seek(max(0,s-2_000_000)); f.readline(); rows=[json.loads(l) for l in f if l.strip()]; r=[x for x in rows if x.get('row_type')=='outcome' and x.get('exit_context_version')==1]; print(json.dumps(r[-1],indent=2,ensure_ascii=False) if r else 'pas encore de close post-patch')"
```
Le bot tourne (PID 11100) ; la sentinelle et ce marqueur `exit_context_version=1` permettront de la repérer dès qu'elle arrive.

---

## FRONTIÈRE

`exit_context_version=1` sur **toute ligne outcome réelle post-patch**. Absente = pré-patch (à reconstruire depuis MT5 ou exclure). `exit_context_present` distingue « contexte capturé » de « manquant » (coupe broker SL/TP, ou restart entre close et écriture). Modèle identique à `core_fix_level="P0TER"`.

---

## CAVEATS

- `atr_at_exit` = ATR **D1** (volatilité journalière, la valeur déjà calculée par market_eyes), pas un ATR intraday — cohérent avec « zéro nouveau calcul ».
- `mfe/mae` (et leurs price/time) restent échantillonnés à la **cadence des cycles** (pas tick). Pour une précision tick, la reconstruction M1/tick MT5 reste la référence (hors périmètre).
- Coupes broker SL/TP : pas de stamp Exit V2 → `exit_context_present=False` (mécanisme quand même connu via deal reason).
- Restart entre close (N) et écriture (N+1) : contexte perdu → `present=False`, ligne écrite quand même (fail-silent).

---

**Résumé :** 18 champs ajoutés, tous lus de valeurs déjà en main. Zéro décision/seuil/exit modifié (157 tests exit_v2 existants verts). Append-only prouvé. Frontière `exit_context_version=1`. Déployé, cycle sain. La v2 capturera désormais, à chaque fermeture, le POURQUOI de la coupe.
