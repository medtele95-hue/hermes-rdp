# DASHBOARD_TABS_REPORT — dashboard en 4 onglets (présentation seule)

## Ce qui a changé

**Un seul fichier modifié : `app/dashboard_api/static/index.html`.** Le contenu précédent (compacté par `DASHBOARD_REDESIGN_REPORT.md`) est réparti en **4 panneaux** (`.tab-panel`), basculés en pur JS côté client (`switchTab(name)` : affiche le panneau ciblé, masque les autres — zéro requête réseau supplémentaire au changement d'onglet). Une **barre d'onglets fixe en bas** (`#tabbar`, `position: fixed; bottom: 0`) avec 4 boutons icône+libellé reste toujours visible. **Aucune donnée, endpoint ou logique backend nouvelle** — uniquement une réorganisation DOM/CSS/JS d'affichage.

### ① Live (onglet par défaut à l'ouverture)
- Équité + P&L jour (2 gros chiffres).
- Bandeau kill-switch (état + reprise + positions), identique à avant.
- **Positions ouvertes** : chaque position dans sa propre carte, avec symbole/sens/badge BE/flottant **+ distance SL/TP ajoutée** (`Δ SL` / `Δ TP`, calculée côté client depuis `current`/`sl`/`tp` déjà fournis par `/api/status`, aucune nouvelle donnée backend).
- **Santé** (renommé depuis « Système ») : rangée de puces bot(MT5)/watchdog/git/tests/médecin, libellés et logique honnête identiques à l'audit (git/tests/médecin restent « n/d » gris, rien fabriqué).

### ② Capteurs
Grille 2 colonnes EES vente/achat + bande, session, DXY, régime ATR, prochaine news — reprise à l'identique depuis `/api/senses`.

### ③ Activité
- Les 4-5 derniers trades, une ligne compacte chacun (`/api/journal?page_size=5`), identique à avant.
- **Journal complet paginé** (filtres, pagination existante) déplacé ici dans un `<details>` replié par défaut — plus sur l'onglet Live.

### ④ Contrôle (rangé du moins au plus dangereux, inchangé dans l'ordre et le câblage)
1. PIN.
2. Symboles (GOLD#/BTCUSD#) → `POST /api/action/symbols`.
3. Robot start/stop/restart, **confirmation obligatoire conservée** sur stop et restart (le mécanisme anti-faux-doigt à 2 taps de la mission précédente n'a pas été touché).
4. Mode compte, isolé en style danger → `POST /api/action/account/switch` avec `{pin, target, confirm_real_text}`, identique.
5. Journal d'audit récent déplacé ici (dans un `<details>` replié) — logiquement rattaché aux commandes plutôt qu'à la surveillance.

## Preuve : `git diff --stat` = dashboard only

```
git diff --stat -- app/dashboard_api/
 app/dashboard_api/static/index.html | 309 ++++++++++++++++++++++--------------
 1 file changed, 191 insertions(+), 118 deletions(-)
```

`app/data/decision_dataset.jsonl` : diff vide, confirmé. Zéro fichier Python touché, zéro ligne dans le moteur/logique de décision/`actions.py`/`data.py`/`security.py`/`audit.py`.

## Tests

Suite complète relancée **4 fois** au total pendant cette mission (par prudence, suite à deux échecs consécutifs du même test) :
1. Avec le changement : `1 failed` (`test_multi_timeframe_momentum.py::test_final_gate_logs_traceable_components`), rejoué isolément → passe en 0.19s.
2. Avec le changement, relancé : `1 failed`, même test.
3. **`git stash` (retour au HTML d'avant mission, baseline propre)** : `3373 passed, 0 failed` — confirme que le flake n'est **pas** causé par ce changement.
4. `git stash pop` (changement restauré) + relance : `3373 passed, 0 failed`.
5. Relance supplémentaire de confirmation : `3373 passed, 0 failed`.

**Conclusion tests : le flake `test_final_gate_logs_traceable_components` est un flake d'ordre d'exécution préexistant et intermittent, reproduit à l'identique avec ou sans ce changement — aucun lien causal avec `index.html` (fichier HTML statique, zéro chemin de code Python en commun avec ce test de logique momentum).** État final stable : **3373 passed, 0 failed, 2 skipped.**

## Bot / dataset / gel

- **Bot de trading (`app.main`, PID 4708)** : `CreationDate` inchangée (09/07/2026 05:41:21) du début à la fin de la mission — jamais arrêté ni redémarré.
- **Watchdog (PID 4984)** : inchangé.
- **Dashboard uniquement** : arrêté et relancé par `HERMES_DASHBOARD_SUPERVISOR` (nouveau PID 14660), comme annoncé et autorisé par l'invariant #4 de la mission.
- `/api/status` répond correctement après reload : `mode: DEMO`, équité cohérente.
- `decision_dataset.jsonl` : diff vide, confirmé ci-dessus.

## Confirmations mission

- [x] **4 onglets fonctionnels** : Live / Capteurs / Activité / Contrôle, bascule pur JS, un seul panneau visible à la fois.
- [x] **Live par défaut** à l'ouverture (`class="tab-panel active"` sur `#tab-live`, `class="tab-btn active"` sur le bouton Live).
- [x] **Chaque onglet tient sur un écran court** : Live = 4 blocs courts (équité/PnL, kill-switch, positions, santé) ; Capteurs = 1 grille ; Activité = 5 lignes + détail replié ; Contrôle = 4 commandes + audit replié.
- [x] **Toutes les commandes câblées aux endpoints existants**, aucun changement de câblage.
- [x] **Confirmation obligatoire stop/restart** conservée à l'identique.
- [x] **Protections mode réel intactes** : aucun changement à `actions.py`/`security.py`, même endpoint, même PIN, même `confirm_real_text == "REAL"`, verrou maître `trading_authorized()` toujours hors de portée du dashboard.
- [x] **Aucune nouvelle logique backend** : zéro fichier hors `index.html` modifié.
