# DASHBOARD_REDESIGN_REPORT — Part B (présentation seule)

## Ce qui a changé

**Un seul fichier modifié : `app/dashboard_api/static/index.html`** (HTML+CSS+JS inline réécrits). Aucun fichier Python touché, aucun endpoint créé ou modifié.

### Monitoring (haut de page, compacté)
- **Équité + P&L du jour** : 2 gros chiffres côte à côte (`s-equity`, `t-net`), P&L coloré vert/rouge via la classe `pos`/`neg` déjà existante.
- **Bandeau kill-switch** : état (déclenché/OK), pertes/max, **heure de reprise calculée côté client** (reset jour broker = 21:00 UTC, convention déjà établie dans `app/utils/broker_time.py::broker_day_window`, **aucune nouvelle donnée backend** — pur calcul d'affichage à partir d'une constante déjà documentée) et nombre de positions ouvertes (depuis `/api/status.positions`).
- **Capteurs** : grille 2 colonnes de pastilles (EES vente/achat + bande sain/prudence/extrême, session, DXY, régime ATR, prochaine news) — tous des champs déjà exposés par `/api/senses`, rien d'ajouté.
- **Activité récente — FIX LONGUEUR n°1** : les 4-5 derniers trades du jour (`/api/journal?page=1&page_size=5`), une ligne compacte chacun (heure/symbole/sens/P&L). Le journal complet paginé n'est **pas supprimé** : il reste disponible, replié par défaut, dans un `<details>` en bas de page.
- **Système — FIX LONGUEUR n°2** : la section devient une rangée de puces (bot/watchdog/git/tests/médecin). Conformément au gap documenté dans `DASHBOARD_AUDIT.md` :
  - **watchdog** : vraie donnée (`watchdog_heartbeat_age_seconds` < 90s = vert).
  - **bot** : aucun endpoint n'expose l'état vivant du process bot séparé — affiché honnêtement comme proxy **"bot (MT5)"**, sourcé sur `mt5_connected` (connexion MT5 du process **dashboard**, pas du bot), avec ce libellé explicite pour ne pas prêter à confusion.
  - **git / tests / médecin** : **aucune donnée disponible** → puces grises "n/d", avec une note explicite ("aucun endpoint ne les expose actuellement") plutôt que fabriquées vertes/rouges.
  - Détail texte affiché uniquement si un point est rouge ou n/d (pas de bruit visuel quand tout va bien).

### Commandes (rangées du moins au plus dangereux)
1. **Symboles (risque faible)** : cases à cocher GOLD#/BTCUSD# → `POST /api/action/symbols` (inchangé).
2. **Robot (risque moyen)** : Start / Stop / Restart. **Confirmation obligatoire ajoutée sur Stop et Restart** (absente avant) : premier tap → bouton passe en état rouge "Confirmer ?" pendant 4s, second tap dans ce délai déclenche réellement l'action ; sans second tap, retour à l'état normal, rien n'est envoyé. Anti-faux-doigt mobile pur JS côté client, aucun changement d'endpoint.
3. **Mode compte (le plus dangereux)** : isolé dans sa propre section au style "danger" (bordure rouge, fond distinct). Câblage **strictement identique** à l'existant : `POST /api/action/account/switch` avec `{pin, target, confirm_real_text}}`, le champ texte "REAL" n'apparaît que si la cible REAL est sélectionnée — même mécanique, juste restylée.

## Preuve : `git diff` = dashboard uniquement

```
git diff --stat -- app/dashboard_api/
 app/dashboard_api/static/index.html | 340 +++++++++++++++++++++++++-----------
 1 file changed, 235 insertions(+), 105 deletions(-)
```

Un seul fichier touché dans tout le repo relève de la présentation dashboard. Le `git diff --stat` global montre en plus des fichiers auto-générés préexistants et sans rapport (`HERMES_STATE.md`, `app/data/backend_started_at.json`, `app/data/news_calendar_cache.json`, `demo_pilot_events.jsonl`, fixtures `tests/__tmp_bloc*/*.jsonl`) — tous déjà en cours d'écriture par le bot/les tests avant cette mission, aucun n'a été modifié par ce travail.

**`app/data/decision_dataset.jsonl` (le vrai `DECISION_DATASET`, `app/services/decision_dataset.py:40`) : diff vide, confirmé.**

Zéro ligne modifiée dans `app/main.py`, `app/services/`, `app/mt5/`, `app/dashboard_api/data.py`, `app/dashboard_api/actions.py`, `app/dashboard_api/security.py`, `app/dashboard_api/audit.py`, ou tout fichier moteur/décision.

## Tests

Suite complète relancée deux fois après les changements :
- 1ère exécution : `1 failed, 3372 passed` — `test_multi_timeframe_momentum.py::test_final_gate_logs_traceable_components`.
- Rejoué isolément : **passe instantanément** (0.19s) → confirme un flake d'ordre d'exécution préexistant, sans rapport avec un fichier HTML statique.
- 2ème exécution complète : **`3373 passed, 0 failed, 2 skipped`** — baseline confirmée verte.

## Process bot / dataset / gel

- **Bot de trading (`app.main`, PID 4708)** : ni arrêté ni redémarré. `CreationDate` inchangée avant/après (09/07/2026 05:41:21).
- **Watchdog (PID 4984)** : ni arrêté ni redémarré.
- **Process dashboard uniquement** (PID 8700 → nouveau PID 11516 après reload) : arrêté puis relancé automatiquement par `HERMES_DASHBOARD_SUPERVISOR` en ~45s, comme prévu et annoncé par la mission (invariant #3).
- `/api/status` répond correctement sur le nouveau process, position GOLD# ouverte toujours visible, `mt5_connected=true`.
- Compte toujours **DEMO** (`"mode":"DEMO"`), `active_profile":"DEMO"`.
- `DECISION_DATASET` (`app/data/decision_dataset.jsonl`) : diff vide, confirmé ci-dessus.

## Confirmations mission

- [x] **FIX LONGUEUR n°1** (Activité plafonnée à 4-5 lignes) : fait, journal complet toujours accessible en `<details>` replié.
- [x] **FIX LONGUEUR n°2** (Système en rangée de puces, détail seulement si rouge) : fait, avec gap git/tests/médecin signalé honnêtement (n/d gris) plutôt que fabriqué.
- [x] **Commandes rangées par danger** (Symboles < Robot < Mode) : fait, section Mode visuellement isolée en rouge.
- [x] **Confirmation obligatoire stop/restart** : ajoutée (absente avant cette mission).
- [x] **Protections mode réel intactes** : aucun changement à `actions.py`/`security.py` ; même endpoint, même corps de requête, même exigence de texte "REAL" exact ; le verrou souverain `trading_authorized()` (hors de portée du dashboard) reste inchangé et `allow_live_trading` reste `False` par défaut.
- [x] **Aucune nouvelle logique backend de trading inventée** : zéro modification hors `index.html`.
