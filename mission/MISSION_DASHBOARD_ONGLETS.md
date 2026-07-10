# MISSION : DASHBOARD EN 4 ONGLETS — FIN DU SCROLL (UI SEULE)

Constat : même compacté, le dashboard reste « très très long » parce que ses sections sont
**toutes empilées et visibles en même temps**. Solution : les répartir en **4 onglets** (barre
en bas), un seul écran court affiché à la fois. **Présentation/UI uniquement**, dans la
continuité de `DASHBOARD_REDESIGN_REPORT.md`. On ne touche NI le moteur NI le dataset.

═══════════════════════════════════════════════════════════════
INVARIANTS ABSOLUS (identiques à la mission précédente)
═══════════════════════════════════════════════════════════════
1. **Un seul fichier modifié : `app/dashboard_api/static/index.html`.** Zéro fichier Python,
   zéro endpoint créé/modifié, zéro ligne dans le moteur / la logique de décision / le writer
   `DECISION_DATASET`.
2. **Réutilise les endpoints et le câblage existants à l'identique** (`/api/status`,
   `/api/today`, `/api/journal`, `/api/system`, `/api/senses`, `/api/audit`, et les POST
   `/api/action/...`). N'invente aucune donnée ni aucune logique backend.
3. **Garde le thème sombre**, mobile-first.
4. Le **bot de trading continue de tourner** — ni arrêté ni redémarré. Seul le process
   dashboard peut recharger (supervisor), comme la dernière fois.
5. **Gel intact** : reste en demo, bot en marche, dataset qui accumule. Changement d'écran, pas
   de comportement.
6. **Protections mode réel intactes** : aucun changement à `actions.py` / `security.py` ; même
   PIN, même `confirm_real_text == "REAL"`, même verrou maître `trading_authorized()` hors de
   portée du dashboard. Ne réduis aucune protection, n'active pas le live.
7. Safepoint git avant. Suite tests **VERTE** après (3373 passed / 0 failed — doit le rester).
   Rouge = rollback + STOP.

═══════════════════════════════════════════════════════════════
CE QU'IL FAUT FAIRE — DÉCOUPER EN 4 ONGLETS
═══════════════════════════════════════════════════════════════
Reprends le contenu déjà présent dans `index.html` et **répartis-le en 4 panneaux**, avec une
**barre d'onglets fixe en bas** (sticky/fixed, toujours visible). Bascule en pur JS côté client
(afficher le panneau actif, masquer les autres). **Onglet par défaut à l'ouverture : Live.**

**① Live** (défaut — l'essentiel de surveillance, doit tenir sur un écran sans scroll) :
- Équité + P&L du jour (2 gros chiffres, P&L coloré).
- Bandeau kill-switch (état + reprise + positions).
- Position(s) ouverte(s) depuis `/api/status.positions` (symbole, sens, flottant, badge
  Exit V2 `be_armed`, distance SL/TP).
- Santé en **rangée de puces** (watchdog = vrai `watchdog_heartbeat_age_seconds` ; bot = proxy
  MT5 honnêtement labellisé ; git/tests/médecin = « n/d » gris — **garde le libellé honnête de
  l'audit, ne fabrique rien**).

**② Capteurs** : grille 2 colonnes — EES vente/achat + bande, session, DXY, régime ATR,
prochaine news (`/api/senses`).

**③ Activité** : les 4-5 derniers trades (`/api/journal?page_size=5`), une ligne compacte
chacun. Le **journal complet paginé** (avec sa pagination existante) va **ici**, dans un
`<details>` replié en bas de cet onglet — pas sur Live.

**④ Contrôle** (commandes rangées du moins au plus dangereux) :
- Symboles : toggles gold# / btc# → `POST /api/action/symbols` (inchangé).
- Robot : start / stop / restart → **garde la confirmation obligatoire** sur stop et restart
  déjà ajoutée.
- Mode compte : demo / réel **verrouillé PIN**, isolé en style danger → `POST
  /api/action/account/switch` avec `{pin, target, confirm_real_text}` (inchangé).

Barre d'onglets : 4 items avec icône + libellé (Live / Capteurs / Activité / Contrôle),
l'onglet actif visuellement distinct. Rien d'autre ne change dans le câblage.

═══════════════════════════════════════════════════════════════
VÉRIFICATION
═══════════════════════════════════════════════════════════════
- `git diff --stat` : **seul `app/dashboard_api/static/index.html`** touché. Zéro Python, zéro
  moteur/décision, `decision_dataset.jsonl` diff vide.
- Suite complète : **0 failed** (toujours 3373 vert ; si le flake d'ordre connu apparaît, rejoue
  isolément pour confirmer, puis relance complet).
- Bot de trading ni arrêté ni redémarré (CreationDate inchangée). Toujours `mode=DEMO`.
- Les 4 onglets fonctionnent, Live par défaut, chaque onglet tient sur un écran sans scroll
  interminable. Toutes les commandes toujours câblées à leurs endpoints existants.

**Livrable : `DASHBOARD_TABS_REPORT.md`** — ce qui a changé (index.html only), preuve
`git diff --stat = dashboard only`, tests verts, confirmation que les 4 onglets marchent, que
Live est par défaut, et que toutes les protections (confirmation stop/restart, PIN mode réel)
sont intactes.

CLÔTURE : affiche dans le terminal — périmètre UI only, tests verts, bot intact, gel intact,
dataset non touché. Rappelle l'URL Tailscale pour recharger.

Rappel : on change l'**écran**, pas le **comportement**. Demo reste demo, bot en marche, dataset
qui se remplit. Les boutons « arrêter » et « passer en réel » sont là par design — rien ici ne
les déclenche.
