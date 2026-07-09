# MISSION : BACKTEST DES SIGNAUX BLOQUÉS PAR LE KILL-SWITCH — LECTURE SEULE

Question à trancher : quand le kill-switch s'arrête (~midi, après 6 pertes en session
asiatique), est-ce que les heures verrouillées ensuite (Londres / NY) avaient réellement
du **profit à prendre** — ou auraient-elles **continué à saigner** ? On mesure. On ne parie pas.

Ce n'est PAS une mission de fix. On rejoue l'histoire. On ne touche à rien.

═══════════════════════════════════════════════════════════════
CONTRAINTES ABSOLUES
═══════════════════════════════════════════════════════════════
1. **LECTURE SEULE.** Aucune modification du code, du kill-switch, d'aucun seuil.
   Aucun `order_send`. Aucun commit de code. Le bot en cours n'est ni touché ni redémarré.
2. **Le DECISION_DATASET ne doit PAS être modifié** (ni écrit, ni ré-écrit). On le LIT.
3. Script d'analyse isolé : `tools/killswitch_replay.py`. Il lit logs + historique MT5,
   il n'écrit que les 2 livrables dans `reports/`.
4. GOLD# / lot 0.01 / magic 909002 = référence, on ne fait qu'analyser.

═══════════════════════════════════════════════════════════════
SOURCES
═══════════════════════════════════════════════════════════════
A. `DECISION_DATASET` + `hermes.log` — tous les signaux évalués, avec leurs features
   (direction, prix de décision, SL, TP, score/décomposition, grade, EES, session,
   spread, RR, raison de blocage `failed_gate=...`, horodatage).
B. Historique de prix MT5 via `mt5.copy_rates_range` (M1 de préférence, M5 sinon) pour
   GOLD# — pour rejouer ce que chaque signal aurait donné contre le prix réel qui a suivi.

Fenêtre : le(s) jour(s) où le kill-switch s'est déclenché dans la période dataset propre
(depuis 2026-07-07). Traite chaque jour concerné séparément.

═══════════════════════════════════════════════════════════════
ÉTAPE 1 — CONSTRUIRE LA BONNE POPULATION (le piège méthodologique)
═══════════════════════════════════════════════════════════════
On ne veut PAS « tous les signaux marqués kill-switch » bruts. On veut le **vrai
contrefactuel** : les signaux qui auraient été PRIS si le kill-switch avait été éteint.

1. Repère l'instant exact où le kill-switch s'est déclenché ce jour-là (premier
   `failed_gate=DAILY_KILLSWITCH_MAX_LOSSES`). Note l'heure et la session.
2. Prends tous les signaux bloqués par le kill-switch APRÈS cet instant.
3. **Filtre contrefactuel** : pour chacun, ré-évalue les AUTRES gates à partir des features
   déjà loguées (confluence ≥ seuil de sa classe, RR OK, spread OK, pas de news blackout,
   pas de MAX_OPEN qui l'aurait bloqué de toute façon). Ne garde que ceux qui **auraient
   passé tout le reste** — c.-à-d. que SEUL le kill-switch les a arrêtés. C'est la seule
   population honnête. Documente combien ont été écartés parce qu'ils auraient échoué ailleurs.
4. **Déduplication** : le bot ré-évalue le même setup à chaque cycle. Collapse les
   ré-évaluations consécutives du même setup (même direction, prix/niveaux quasi identiques,
   proches dans le temps) en **UNE seule opportunité de trade**. On rejoue des trades, pas
   des ticks. Documente la règle de dédup utilisée.

═══════════════════════════════════════════════════════════════
ÉTAPE 2 — REJOUER CHAQUE OPPORTUNITÉ
═══════════════════════════════════════════════════════════════
Pour chaque opportunité retenue :
- Entrée = prix de décision logué, sens = direction loguée, SL/TP = ceux du signal.
- Marche en avant dans les bougies M1 MT5 depuis l'horodatage de décision.
- Détermine le **premier touché** : TP (gain) ou SL (perte), en tenant compte du **spread**
  (utilise le spread logué du signal, ou une estimation prudente).
- Si ni TP ni SL touché avant le reset broker (21:00 UTC) → marque « encore ouverte » et
  valorise à la clôture de la fenêtre (mark-to-close).
- Calcule le résultat **à 0.01 lot** (mêmes conditions que le live) : R multiple et $.

═══════════════════════════════════════════════════════════════
CAVEATS À ÉCRIRE NOIR SUR BLANC DANS LE RAPPORT (honnêteté obligatoire)
═══════════════════════════════════════════════════════════════
- C'est une **SIMULATION** sur bougies historiques, pas la réalité. Les trades n'ont jamais
  été ouverts.
- Elle utilise le SL/TP du signal — elle **n'imite pas** le trailing dynamique d'Exit V2
  (le vrai résultat pourrait donc différer, dans un sens comme dans l'autre). Signale-le.
- Elle suppose un remplissage au prix du signal + spread, et que 0.01 lot ne bouge pas le marché.
- **Un ou deux jours de données ne prouvent rien sur le futur.** C'est un indice mesuré, pas une loi.

═══════════════════════════════════════════════════════════════
ÉTAPE 3 — LE VERDICT
═══════════════════════════════════════════════════════════════
Calcule et présente :
- P&L simulé total des opportunités bloquées après le kill-switch (le « manque à gagner »
  OU le « désastre évité »).
- Ventilé **par session** (Asie / Londres / NY) — c'est le cœur de la question : les heures
  verrouillées Londres+NY étaient-elles vertes ou rouges ?
- Win rate simulé, meilleur et pire trade rejoué.
- **La comparaison directe** : ce que la journée a RÉELLEMENT fait avec le kill-switch
  (+7,63 $ ce jour-là) vs ce qu'elle AURAIT fait si les signaux d'après-midi avaient été
  autorisés (réel + simulé).
- **Le verdict en une phrase** :
  - si les heures bloquées étaient nettement vertes → l'intuition de SIMO est **mesurément
    validée** → on conçoit un kill-switch par-session pour la phase post-gel, chiffres à l'appui.
  - si elles auraient continué à saigner → le stop journalier est **vindiqué** → sujet clos.

═══════════════════════════════════════════════════════════════
LIVRABLES
═══════════════════════════════════════════════════════════════
1. `KILLSWITCH_REPLAY.md` — population retenue (+ combien écartés et pourquoi), méthodo de
   dédup, tableau des opportunités rejouées (heure | session | sens | entrée | SL/TP |
   premier touché | R | $ simulé), les agrégats par session, la comparaison réel vs
   contrefactuel, les caveats, et le verdict.
2. `KILLSWITCH_REPLAY.csv` — une ligne par opportunité rejouée.

À la fin : affiche le verdict dans le terminal, et confirme `git status` côté code = intact
(seuls le script isolé + les 2 livrables dans `reports/`). Le DECISION_DATASET n'a pas été touché.

Rappel : cette mission **répond** à une question, elle ne change **rien**. Le kill-switch reste
tel quel en live. Si les données donnent raison à l'intuition, le redesign se fera plus tard,
en phase calibration — avec la preuve, pas le hunch.
