# MISSION P0-BIS — RAPPORT

**Date :** 2026-07-14
**Safepoint :** `safepoint-avant-p0bis` → `3be4e517`
**HEAD :** `856a3c7c` — 3 commits atomiques
**Suite :** **3469 passed, 2 skipped** (base 3447 + 22 tests neufs)
**Fenêtre :** kill-switch déclenché (6/6 pertes) pendant toute la mission — aucun impact sur la collecte.

---

## Résumé

Les 3 trous démontrés par le ticket 383260970 sont bouchés. Chacun a son commit, ses tests, et **son gate vu se déclencher**.

**Épilogue du ticket, survenu pendant la mission :** il s'est fermé à **−39,62 USD**, sur `NEWS_PRECLOSE / NON_ARMED_BEFORE_MAJOR` (bouclier news, Core CPI). Comportement correct — la position n'était pas armée. **Le nouveau cœur l'aurait bloqué à l'entrée** (RR 1,434 < 1,5).

### Impact combiné, replay à sec sur le dataset v1 (108 ordres)

| | Ordres bloqués | % |
|---|---|---|
| **FIX 1** (top-down AVOID sur ORDER_FLOW) | **28** | 26 % |
| **FIX 2** (RR final < 1,5) | **72** | 67 % |
| *recouvrement* | *18* | |
| **TOTAL P0-BIS** | **82 / 108** | **76 %** |

| | Trades | P&L | Winrate |
|---|---|---|---|
| Bloqués par P0-BIS | 81 | **−56,20 USD** | 66,7 % |
| Passeraient toujours | 26 | **+14,73 USD** | 53,8 % |
| **Total v1 réel** | 107 | **−41,47 USD** | |

**Le P&L v1 serait passé de −41,47 à +14,73 USD.**

> **⚠ IMPACT VOLUME : −76 %.** C'est massif, et je ne l'enrobe pas. Combiné à P0-A, la collecte v2 sera **beaucoup plus lente** : à ce rythme, atteindre 200 trades prendra environ **4 fois plus longtemps**. C'est le prix de la rigueur, l'économie le justifie — mais tu dois le décider en connaissance de cause. Voir §« Ce que tu dois arbitrer ».

---

## FIX 1 — ORDER_FLOW passe enfin sous contrôle (`0a9cb534`)

**Deux trous, pas un.**

**(a) Le court-circuit top-down.** `_first_block_reason` contenait :
```python
if (order_flow gates) and not strict_gold_order_flow_topdown:
    ...
    return None          # ← sortait AVANT le contrôle top-down
```
**Preuve dans les données :** `strict_block_reason = None` sur **les 107 ordres ORDER_FLOW exécutés** de v1. Le lecteur top-down n'était **jamais** consulté pour cette stratégie — alors qu'il disait **AVOID sur 28 d'entre eux**. Le même court-circuit existait dans `_final_demo_block_reason`. Les deux sont supprimés.

Un AVOID produit désormais `TOP_DOWN_READER_BLOCK`, **blocage dur** au sens de P0-A : aucun mode discovery ne peut plus l'effacer.

**(b) Le gate dédié était du code mort.** `_order_flow_exec_agent_block_reason` existait mais n'avait **aucun appelant** : `_symbol_gate_block_reason` faisait un `return None` sec. Ses règles — `order_flow_execution_enabled`, score ≥ 75, RR ≥ 1,5, time gate, cap MAX_OPEN par symbole — n'étaient **jamais** appliquées à la stratégie la plus active du système. Il est branché.

> **Honnêteté du constat.** Sur les 107 ordres ORDER_FLOW de v1, **le gate dédié seul n'aurait bloqué aucun trade** : le score est toujours ≥ 75 et le RR vaut toujours exactement 1,500 au signal. Sa valeur est **préventive** — il rend effectifs des seuils qui ne l'étaient pas. **C'est la suppression du court-circuit top-down qui bloque les 28.**

**`STRICT_GOLD_ORDER_FLOW_TOPDOWN` (= `false`)** ne pilote plus ce court-circuit. Son effet résiduel — et désormais son **seul** effet — est de relâcher les échecs **forts** de SMC/MTFA pour les stratégies order-flow (`_strong_confluence_fail_reason`). C'est un assouplissement de *qualité de setup*, pas un contournement du verdict top-down.

**Tests (8).** Chaque règle du gate est **vue se déclencher** (score < 75, RR < 1,5, cap MAX_OPEN, stratégie désactivée), plus un **contrôle négatif** : un signal conforme (score 100, RR 1,5, aucune position, time gate PASS) doit continuer à passer.

---

## FIX 2 — Les deux planchers RR sont unifiés (`36f6ba10`)

Le système avait **deux planchers qui ne regardaient pas les mêmes valeurs** :
- le gate du routeur exigeait `rr >= 1.5`, sur les valeurs de la **décision** ;
- le choke-point n'exigeait que `rr >= 1.0`, sur les valeurs **finales** (prix au tick d'envoi + SL/TP normalisés).

Entre les deux, un trou de 1,0 à 1,5.

**Ce n'était pas un cas limite.** Mesure sur le dataset v1 :
- le RR au signal vaut **exactement 1,500 sur 107 des 108 ordres** (le TP est calculé pour donner pile 1,5) ;
- la moindre dérive adverse entre le signal et l'envoi le fait donc passer sous 1,5 ;
- **72 des 108 ordres (67 %)** atterrissent dans le trou ;
- ces 72 ont perdu **−81,27 USD**, quand les 36 qui gardent RR ≥ 1,5 ont gagné **+39,80 USD**.

Le ticket 383260970 : **1,500 au signal → 1,434 au fill** (glissement de 2,91 points contre, SL/TP inchangés).

**Correctif.** Le plancher final est le **minimum de la stratégie** (1,5 par défaut, `order_flow_min_rr` pour ORDER_FLOW), évalué sur les valeurs finales avant envoi. Un RR dégradé est un blocage dur : **`RR_DEGRADED_AT_FILL`**, loggué avec **les deux valeurs** (signal + final + plancher) — un blocage qui ne dit pas d'où vient la dégradation est indiagnosticable.

Le **même plancher s'applique au chemin PENDING** : laisser une porte plus permissive ailleurs rouvrirait exactement le trou qu'on ferme.

**Renforcement, jamais assouplissement.** `_final_rr_floor` ne descend **jamais** sous 1,5, même si une stratégie déclarait un minimum plus permissif.

**Tests (8)**, dont **le scénario du ticket** (1,500 → 1,434 ⇒ bloqué) et un **contrôle négatif** (1,6 → 1,55 ⇒ passe).

> **Un test existant révélait le bug sans le savoir.** `test_max_money_tp_keeps_original_tp_when_already_below_two_usd` posait `entry=1.1 / sl=1.099 / tp=1.101` — soit un RR **géométrique de 1,0** — tout en **déclarant `reward_risk=2.0`**. Le jeu de données était incohérent avec lui-même, et personne ne l'avait vu : l'exploration lisait le RR *déclaré* (2,0 ✓), le choke-point lisait le RR *géométrique* (1,0 ✓ avec l'ancien plancher). **C'est exactement le trou que ce correctif ferme.** Le test en était une illustration involontaire.

---

## FIX 3 — Exit V2 survit aux redémarrages (`856a3c7c`)

`self._exit_v2_state` était un dict **en mémoire seule**, réinitialisé vide à chaque construction du routeur. Et `exit_v2.py` fait :
```python
st = state.setdefault(ticket, {"peak_usd": profit, "be_armed": False})
```
Après un redémarrage, le pic était donc **ré-amorcé sur le profit courant** et `be_armed` repassait à `False`.

**Un redémarrage DÉSARMAIT le plancher de break-even d'un gagnant déjà protégé.** Le gain verrouillé s'évaporait, en silence. `exit_v2_state.json` existait — mais il n'était **écrit** que pour le dashboard, **jamais relu**. Trois redémarrages ont eu lieu les 13 et 14/07.

> **Le chemin de nuisance concret, observé pendant cette mission.** Le ticket 383260970 a été fermé par le bouclier news sur `NON_ARMED_BEFORE_MAJOR` — une position **non armée** est fermée avant une news majeure. C'était correct pour lui (jamais en profit). **Mais avant le FIX 3, un gagnant ARMÉ, redémarré juste avant une news, aurait vu son `be_armed` remis à `False` — et aurait été fermé par ce même bouclier**, alors que son plancher devait le laisser courir. Le bug n'était pas théorique.

**Correctif.** `_load_exit_v2_state()` recharge le snapshot à la construction. Le chemin dérive désormais de `events_path`, comme `decision_dataset.jsonl` et `outcome_tracker_state.json` — en production c'est le même fichier qu'avant ; en test, chaque routeur a le sien.

**Ticket inconnu au boot** (position ouverte hors de la connaissance d'Exit V2) : ré-amorçage sur le profit courant. C'est le comportement d'origine, et **le seul raisonnable — on ne peut pas inventer un pic qu'on n'a jamais observé**. Documenté.

**Preuve en production, au redémarrage :**
```
[EXIT_V2] état restauré : 72 position(s), dont 44 avec break-even ARMÉ —
          un redémarrage ne désarme plus un gagnant protégé
```

**Tests (6)**, dont **le test** (armer un BE → simuler un redémarrage → `be_armed` et `peak_usd` restaurés) et des contrôles négatifs (snapshot absent / corrompu ⇒ le boot réussit).

> **Un rouge qui était une preuve.** Deux tests de `test_bloc4_exit_v2` ont rougi : leur dossier est partagé, et le pic écrit par un test (profit +5,00 ⇒ `peak 5.0, be_armed`) était rechargé par le suivant (profit +1,60), dont le plancher de trailing (5,0 − 1,2 = 3,8) déclenchait alors une fermeture. **Ce n'était pas un bug du correctif : c'était la démonstration qu'il fonctionne** — l'état traverse désormais la construction d'un routeur. Purge d'état ajoutée, comme pour le journal.

> **Effet de bord découvert :** le snapshot de production contenait **des tickets de TEST** (111, 701, 100001…) — les tests écrivaient dans le fichier réel. Le chemin dérivé de `events_path` met fin à cette pollution.

---

## Vérification finale

### Zones intouchables

| | |
|---|---|
| `app/services/daily_killswitch.py` | **DIFF VIDE** |
| `SYMBOL_ALLOWLIST` · `LOT_HARD_CAP` · `MAGIC_HARD` | **intacts** |
| `NAKED_ORDER_BLOCKED` · `LOT_INVALID_BLOCKED` | **intacts** |
| **Plancher RR du choke-point** | **RELEVÉ 1,0 → 1,5** — renforcement documenté et voulu (« renforcer = OK, assouplir = interdit ») |

### Bot redémarré — sain

| Contrôle | Résultat |
|---|---|
| PID | 2276 |
| Erreurs / Traceback | **0** |
| Cycles | normaux (`CYCLE_SUMMARY analyzed=2`) |
| **Exit V2** | **72 positions restaurées, 44 BE armés** |
| Outcome tracker | 1 trade restauré |
| Kill-switch | déclenché (6/6) — fenêtre sans impact, comme prévu |

### Reprise de la collecte v2 sur cœur complet

```
Bot démarré         : 2026-07-14 13:19:32 (local) = 12:19:32 UTC
1er cycle           : 2026-07-14 13:21:39 (local) = 12:21:39 UTC
```
*(La bascule `collection_version=2` avait été posée le 2026-07-14T00:30:11 UTC. Les lignes v2 écrites entre 00:30 et 12:19 proviennent du cœur P0 **sans** P0-BIS — elles restent marquées v2 mais n'ont pas les 3 correctifs de cette mission. Sur cette fenêtre, 4 ordres ont été exécutés.)*

---

## Ce que tu dois arbitrer

**Le volume de trades va chuter d'environ 76 %.** C'est le chiffre du replay, et il est cohérent avec la cause : le TP est calibré pour donner **exactement RR 1,5** au signal, donc **toute** dérive adverse fait échouer le plancher. Trois options :

1. **Accepter** — la collecte v2 sera lente (≈ 4× plus longue pour 200 trades), mais chaque trade respectera enfin les règles annoncées. L'économie du replay le justifie (**−41,47 → +14,73 USD**).

2. **Donner de la marge au TP** — si le TP était calibré à RR **1,7-1,8** au signal au lieu de 1,5, une dérive normale le laisserait au-dessus de 1,5 et le taux de blocage s'effondrerait. **C'est un changement de seuil de trading — hors périmètre de cette mission, et c'est à toi de le décider.** C'est, à mon avis, la vraie correction de fond : le système visait la limite exacte de son propre gate.

3. **Ré-ancrer SL/TP sur le prix réel** au lieu de bloquer — conserver les *distances* de risque/récompense et les recentrer sur le fill. Le RR resterait à 1,5 par construction. Plus élégant, mais c'est une modification du comportement d'exécution, pas un colmatage : à traiter dans une mission dédiée.

**Ma recommandation : option 1 maintenant** (le cœur doit d'abord être honnête), puis **évaluer l'option 2 sur les données v2** — quand tu sauras enfin ce que le système fait vraiment quand il applique ses propres règles.

---

*Aucune action sur les positions. Kill-switch et choke-point (hors renforcement RR documenté) non modifiés.*
