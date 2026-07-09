# MISSION : SÉCURISER LE DATASET (fondation moteur EV ⑤) + DIAGNOSTIC NEWS

Objectif : garantir que `DECISION_DATASET` repart **propre** à partir d'aujourd'hui
(prérequis absolu de la calibration p(win) du moteur EV), et trancher en lecture seule
si l'outlier news (−42.85, FOMC minutes) est un **bug** ou du **tuning**.

Principe directeur : **vérifier avant de modifier.** On lit d'abord. On ne corrige que
si c'est réellement cassé, et uniquement là où c'est sûr.

═══════════════════════════════════════════════════════════════
INVARIANTS ABSOLUS
═══════════════════════════════════════════════════════════════
1. **Logique de trade INTOUCHABLE.** Aucun seuil de décision modifié, aucun changement
   au chemin de décision, à l'exit, au confluence, aux gates. Aucun `order_send`.
2. GOLD# intouchable. Lot 0.01. Magic 909002.
3. Bot en cours : ne pas le redémarrer sauf strict nécessaire — et si nécessaire, l'annoncer
   dans le rapport avant/après.
4. Le SEUL fichier que cette mission a le droit de modifier (Part B, et seulement si contaminé) :
   **le writer de `DECISION_DATASET`**. Rien d'autre dans `app/`.
5. Safepoint git AVANT toute écriture. Tests complets (invariants + régression) APRÈS.
   Un test rouge = rollback immédiat + STOP + rapport.

═══════════════════════════════════════════════════════════════
PART A — DIAGNOSTIC NEWS  (LECTURE SEULE, ZÉRO MODIFICATION)
═══════════════════════════════════════════════════════════════
Le trade perdant : GOLD# SELL, `ts_utc = 2026-07-08 15:30:46`, fermé par
`NEWS_PRECLOSE (FOMC Meeting Minutes)`, −42.85 $, `be_armed=False`.

1. Récupère le **timestamp exact** de l'event « FOMC Meeting Minutes » du 2026-07-08
   depuis la source calendrier réellement utilisée par HERMES (cache/feed ForexFactory).
2. Calcule l'écart entre cet event et l'heure d'entrée (15:30:46 UTC + heure broker UTC+3).
3. Verdict :
   - **BUG** si l'entrée était DANS la fenêtre ±10 min qui aurait dû la bloquer, alors qu'un
     SELL a quand même été ouvert. Cela signifie que le **blackout d'entrée** et le
     **pré-close de sortie** ne lisent pas la même liste/fenêtre d'events.
     → Documente PRÉCISÉMENT où : quelle fonction fait le blackout d'entrée, quelle fonction
       fait le pré-close, et pourquoi l'une a laissé passer ce que l'autre a rattrapé.
       **NE CORRIGE PAS** — on veut juste la localisation exacte pour décider ensuite.
   - **TUNING** si l'entrée était légitimement HORS ±10 min (le marché a bougé sur
     l'anticipation, pas de règle violée). → Alors ce n'est PAS un bug : élargir la fenêtre
     serait du tuning sur un seul trade. **ON NE TOUCHE À RIEN.**

LIVRABLE A : `NEWS_GAP_VERDICT.md` — timestamp FOMC, écart en minutes, verdict
**BUG** ou **TUNING**, et si BUG la localisation code précise (fichiers/fonctions), sans correctif.

═══════════════════════════════════════════════════════════════
PART B — HYGIÈNE DATASET  (AUDIT d'abord, fix chirurgical SEULEMENT si contaminé)
═══════════════════════════════════════════════════════════════
Contexte : le rapport 24h a exclu ~143k lignes de cycles synthétiques (self-test / health-check
qui rejouent un cycle de décision factice plusieurs fois/heure). Question critique : ces cycles
entrent-ils dans `DECISION_DATASET` ? Si oui, la calibration p(win) du moteur EV est empoisonnée.

**ÉTAPE 1 — AUDIT (lecture seule) :**
- Lis le code du writer de `DECISION_DATASET`. Existe-t-il déjà un flag qui distingue un cycle
  RÉEL d'un cycle SYNTHÉTIQUE au moment de l'écriture ?
- Inspecte le dataset existant : combien de lignes portent la signature synthétique (même
  heuristique que celle utilisée pour les exclure du rapport) ? Donne le compte et le %.

**ÉTAPE 2 — DÉCISION :**
- **SI PROPRE** (aucun cycle synthétique n'atteint le dataset) → écris `DATASET_HYGIENE.md`
  confirmant « propre, aucune action nécessaire », et **STOP. Ne touche rien.**
- **SI CONTAMINÉ** → fix chirurgical **au writer UNIQUEMENT** :
  - Ajoute un champ `is_synthetic` (bool) à chaque ligne, déterminé **à la source** (le cycle
    self-test sait qu'il est synthétique — propage l'info, ne devine pas après coup).
  - Objectif minimal : que la calibration puisse filtrer `is_synthetic == False` proprement.
  - Lignes synthétiques déjà écrites : **marque-les** rétroactivement si possible (append-only —
    on marque, on ne supprime pas). Si impossible de marquer sans réécrire, documente la borne
    temporelle à partir de laquelle le dataset est fiable.
  - Ce fix ne touche QUE le writer. Il ne change AUCUNE décision de trade, AUCUN seuil, AUCUN exit.
  - Safepoint git avant. Tests complets après. Rouge = rollback.

LIVRABLE B : `DATASET_HYGIENE.md` — état (propre / contaminé), nb + % de lignes synthétiques,
ce qui a été taggé, la date à partir de laquelle le dataset est fiable, et **preuve que la logique
de trade est intacte** (`git diff` ne touche que le writer du dataset).

═══════════════════════════════════════════════════════════════
CLÔTURE
═══════════════════════════════════════════════════════════════
- Confirme en une phrase : « le compteur de calibration du moteur EV ⑤ démarre sur des données
  fiables à partir de [date/heure] ».
- Confirme `git status` côté logique de trade = intact (seul le writer du dataset a pu bouger,
  et seulement si contaminé).
- Affiche les deux verdicts (news + dataset) dans le terminal.

Rappel : on ne re-tune RIEN sur 20 trades. Cette mission sécurise la fondation et diagnostique —
elle ne « corrige » l'outlier ni ne touche au SL, au grade ou au BTC. Ces questions se trancheront
plus tard, avec le dataset propre, par les chiffres.
