# MISSION P0-TER — RAPPORT

**Date :** 2026-07-14 · **Safepoint :** `safepoint-avant-p0ter` (sur `1043f20f`)
**Commits :** `9294158e` (FIX 1) · `8b557023` (FIX 2)
**Suite complète :** **3500 passed, 2 skipped** après chaque commit.
**Objet :** tuer le look-ahead devenu bloquant + assainir le writer, avant que la collecte v2 n'accumule du bruit.

---

## CE QUI A CHANGÉ, EN UNE PHRASE

On a changé **QUAND** les données sont lues (bougies clôturées, plus la bougie en cours) et **COMMENT** elles sont écrites (labels du bon côté du spread, ATR de régime en Wilder). **Aucun seuil de trading, aucun gate, aucune décision n'a été touché.** Zones intouchables (kill-switch, config, position_sync, choke-point, caps, plancher RR) : diff vide, prouvé.

---

## FIX 1 — LE TOP-DOWN NE VOIT PLUS LE FUTUR

### Le problème (rappel de l'audit)
`copy_rates_from_pos(symbol, tf, 0, count)` livre la bougie **en cours** en `.iloc[-1]`. Le lecteur top-down l'utilisait pour **tout** son verdict (sweep, BOS, FVG, order blocks, m15_confirmation, m1_trigger, prix courant), et le snapshot order-flow y ancrait entry/SL/TP. Depuis FIX 1 de P0-BIS, ce verdict est **bloquant** : il était donc à la fois bloquant et repeignant. Le filtre `decision_time`, censé filtrer, valait toujours `None` en production — du code zombie.

### Le correctif
Le patron `_closed_frame` (retrait de la dernière bougie) existait déjà dans 6 modules du repo, tous corrects. Il devient partagé : **`app/utils/candles.closed_frame`**. Appliqué à 4 endroits :

| Site | Fichier | Ce qui est corrigé |
|---|---|---|
| **Point de passage unique** | `top_down_market_reader._prepare_frames` | la bougie en cours est retirée une fois pour toutes ; les ~20 sites `.iloc[-1]` en aval désignent la dernière bougie **clôturée**. Un futur site ne peut plus réintroduire le repaint. |
| Ancrage entry/SL/TP | `gold_order_flow_cvd_vwap` (snapshot + entrée) | `price`, `vwap`, cvd/delta et le profil de volume (poc/vah/val, qui **fixe le SL structurel**) ne repeignent plus. |
| Blocage dur MSS M1 | `order_flow_execution_agent._check_mss_m1` | un MSS « confirmé » par un close inexistant ne peut plus lever le blocage. |
| Bonus de score FVG | `order_flow_execution_agent._detect_fvg_bonus` | un gap qui se referme dans la bougie ne donne plus +10. |

`decision_time` n'est plus un zombie : documenté comme cutoff « as-of » optionnel (replay), et l'ordre des deux opérations est prouvé sans double retrait.

### Preuve par les tests
`tests/test_p0ter_lookahead.py` — 14 tests (T-LA1 à T-LA4 + `closed_frame` + FVG). **Rejoués contre le code d'avant dans un worktree au safepoint : 7 ROUGES**, 7 verts (les contrôles négatifs, qui doivent passer dans les deux mondes). Ils capturent donc réellement le bug. Fixtures top-down et MSS mises à jour : elles posaient leur motif **sur** la bougie en cours (un monde qui n'existe pas en production) ; elles portent désormais une bougie en cours contradictoire, ce qui les **renforce**.

### REPLAY D'IMPACT (lecture seule) — l'ampleur du bruit retiré

`tools/replay_p0ter_impact.py` reconstitue, à la minute près, ce que
`copy_rates_from_pos(...,0,...)` renvoyait à chaque instant de décision (bougie en
cours reconstruite depuis les M1 écoulées, 300 bougies/timeframe comme en
production), puis compare le verdict **ancien** (lecteur voyant la bougie en cours)
au verdict **nouveau** (l'ignorant), à entry/SL/TP identiques.

**GOLD#, 480 verdicts rejoués (240 instants × BUY/SELL), lecture seule :**

| | Combien | Part |
|---|---|---|
| **Verdicts qui basculent** (ALLOW_DEMO / WAIT / AVOID) | **76 / 480** | **15,8 %** |
| dont `ALLOW_DEMO → WAIT` (un ordre qui **partait** sur du bruit) | 42 | 8,8 % |
| dont `WAIT → ALLOW_DEMO` (un ordre légitime qu'on **ratait**) | 34 | 7,1 % |

Détail des confirmations qui repeignaient (une confirmation = un composant du verdict) :

| Composant | Repeint sur |
|---|---|
| `m5_context` | 228 / 480 (47,5 %) |
| `entry_readiness_score` | 190 / 480 (39,6 %) |
| `m15_confirmation` (confirmation **dure**) | 95 / 480 (19,8 %) |
| `m1_trigger` (confirmation **dure**) | 43 / 480 (9,0 %) |
| `bos_choch` | 19 / 480 (4,0 %) |
| `liquidity_sweep` | 1 / 480 (0,2 %) |

Environ **un verdict sur six** dépendait de la bougie non close — dont près de la moitié dans le sens qui **envoyait** un ordre. C'est le bruit exact retiré de la collecte v2.


**Lecture :** chaque verdict qui bascule est, depuis FIX 1 de P0-BIS, un ordre pris — ou refusé — sur du bruit intra-bougie. Le sens le plus grave est `ALLOW_DEMO → WAIT` : des ordres qui **partaient** sur un setup qui n'existait pas encore à la clôture.

---

## FIX 2 — LE WRITER N'EMPOISONNE PLUS LA CALIBRATION

Pur enregistrement, zéro effet décisionnel — mais c'est ce dataset qui calibrera la v2.

**A. Labels du bon côté du spread.** Les sorties **virtuelles** testaient la touche TP/SL au **mid**. Un BUY se solde au bid, un SELL se rachète à l'ask ; le mid est optimiste des deux côtés (TP touché un demi-spread trop tôt, SL évité un demi-spread trop tard — GOLD 0,15 ; BTC 11,25) → le dataset **sur-étiquetait les WIN**. `demo_router` transmet désormais `{bid, ask}` ; le tracker choisit le côté. Chaque ligne porte `label_method` : `bid_ask` (juste) / `mid` (ancien) / `mt5_deals` (vérité broker, positions réelles).

**B. ATR de régime en Wilder.** `atr_percentile` était un SMA équipondéré alors que les 14 sites ATR du cœur sont en Wilder : le percentile classait une série d'une **autre nature** que l'ATR qui décide. Remplacé par `regime.atr_percentile_wilder` (bougies clôturées). L'ancienne colonne **cesse d'être écrite** — les deux ne coexistent jamais. La fonction SMA est conservée, non appelée, pour que les 10 486 lignes v1 restent lisibles.

**C. Fin des colonnes fantômes et du piège d'unité.** `atr` était `None` sur **10 605/10 605** lignes (l'événement ne l'a jamais porté) — c'est cette absence, et non un piège d'unité, qui tuait `spread_to_atr` (`None` sur **9 953/9 953**). Le writer calcule l'ATR lui-même (Wilder, M5 clôturées). Le spread porte son unité dans son nom (`spread_points`), sa provenance est écrite (`spread_source` = AT_SEND / M5_CANDLE, pour couvrir aussi les décisions **refusées**), et la conversion points → prix se fait à **un seul endroit** via le `point` du broker. Sans `point`, on renvoie `None` : deviner est exactement ce qui aurait produit le facteur ×100. La clé ambiguë `event["spread"]` n'est pas ressuscitée.

### Preuve par les tests
`tests/test_p0ter_writer.py` — 14 tests. Le pivot (T-W1) : un SELL GOLD dont le **mid ne voyait rien** (SL intact à 4009,925) mais dont **l'ask a touché le SL** (4010,15) → `SL_HIT`, `label_method=bid_ask`. Contrôle du contrôle : la même cotation passée en float (ancien mid) ne déclenche **rien**. T-W2 : Wilder ≠ SMA sur un choc de volatilité. T-W3 : 30 points × 0,01 = 0,30 en prix, **pas** ×100.

### Borne temporelle (demandée par la mission)
`core_fix_level = "P0TER"` sur chaque nouvelle ligne. **Absent = v2 pré-P0-TER** (décisions repeintes + labels au mid) : la calibration pourra exclure ou pondérer ces lignes **sans arithmétique de timestamp**. Le dataset v1 n'est pas touché (append-only strict).

---

## EFFET DE BORD TROUVÉ EN ROUTE — isolation d'un fichier de config vivant

`test_gold_only_invariant` et `test_coeur_p0` lisaient `app/data/active_symbols.json`, un fichier que le **dashboard réécrit en production** (toggle sans redémarrage). Constaté en direct : un toggle « GOLD seul » à 22:57 a fait virer **6 tests au ROUGE** sans qu'une ligne de code bouge. L'inverse est pire : un test vert ne prouvait rien. `conftest.py` pointe désormais ce fichier vers un chemin inexistant (fail-open documenté). **Le fichier de production n'est ni lu, ni écrit, ni déplacé** — vérifié par hash avant/après suite. Même classe de bug que l'isolation du log déjà présente dans ce `conftest.py`.

**À noter pour SIMO :** ce fichier vaut actuellement `["GOLD#"]`. Tant qu'il reste ainsi, le bot **ne trade que GOLD** — BTCUSD# est désactivé côté dashboard. Si la collecte v2 doit inclure BTC, il faut réactiver les deux symboles dans le dashboard.

---

## VÉRIFICATION FINALE

- **Zones intouchables** — diff vide depuis le safepoint : `daily_killswitch.py`, `config.py`, `mt5_position_sync.py`. Aucune ligne ajoutée à `demo_router.py` ne touche `LOT_HARD_CAP`, `MAGIC_HARD`, `SYMBOL_ALLOWLIST`, `_execution_invariants_block`, `capped_lot`, `order_send`.
- **Suite complète** : 3500 passed, 2 skipped. Audit géo/math (23 tests) mis à jour et vert : les deux constats devenus faux (`atr_percentile` SMA, tracker au mid) sont retournés en **preuves de correction**.
- **Bot redémarré** : voir ci-dessous.

### Redémarrage — 1er cycle sain (preuves en production)

Bot arrêté (PID 2276) et relancé (**PID 4892**, `python -m app.main`) à 00:11, **zéro position ouverte** au moment du redémarrage (rien perturbé ; les SL/TP sont de toute façon posés broker depuis P0-BIS). Premier cycle propre, aucun traceback, GOLD# et BTCUSD# analysés, 7+ cycles enchaînés.

**Preuve FIX 2 en production — la colonne ressuscitée, à la bonne unité :**
```
[FINAL_GATE] symbol=GOLD# ... spread_price=0.32 spread_to_atr=0.143831 atr_status=VALID
```
`spread_to_atr` valait `None` sur 9 953/9 953 lignes v1 ; il est maintenant **vivant et de bon ordre de grandeur (~0,14)** — la conversion points→prix fonctionne, pas de facteur ×100 (qui aurait donné ~14).

**Preuve FIX 1 — l'order-flow émet sur bougies M5 clôturées :**
```
[ORDER_FLOW_PAYLOAD_EMITTED] strategy=ORDER_FLOW_READER symbol=GOLD# status=OBSERVE_ONLY
```
Verdicts calculés sur bougies clôturées (démontré par 14 tests + le replay 15,8 %). Exit V2 : état persisté présent (`app/data/exit_v2_state.json`, 30 961 o), rechargé au boot sans erreur (FIX 3 de P0-BIS), aucune position à armer.

**Timestamp de reprise de la collecte v2 sur système véridique : 2026-07-15 00:11 UTC+1** (les lignes portent `core_fix_level="P0TER"` à partir de cet instant).

---

## APRÈS CETTE MISSION

Plus aucun chantier connu ne touche ni les décisions ni l'enregistrement. La v2 mesure enfin exactement le système construit. Tout le reste (P1 exécution, P2, calibration TP/SL, régimes, Monte Carlo) attend les 200 trades v2 — désormais collectés sur un système véridique.
