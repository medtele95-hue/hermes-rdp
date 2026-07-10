# DASHBOARD_FIX_REPORT — page blanche onglets

## Résumé exécutif

**Étape 1 (restauration d'urgence) : faite, vérifiée, en ligne.** SIMO a de nouveau un dashboard fonctionnel (version compacte pré-onglets).

**Étapes 2/3 (diagnostic précis + réparation des onglets) : STOP volontaire**, conformément à la clause de la mission (« Si l'étape 3 s'avère plus compliquée que prévu : STOP après l'étape 1 + rapport »). Cause précise **non confirmée** malgré une analyse structurelle poussée — voir détail ci-dessous. Aucune réécriture à l'aveugle n'a été tentée.

## Étape 1 — Restauration (faite)

- `git show 34d2c014:app/dashboard_api/static/index.html` récupéré et réappliqué (le `git checkout` direct a échoué : `unable to unlink old ... Invalid argument` — le fichier était verrouillé par le process dashboard lui-même ; le process a été arrêté puis relancé par `HERMES_DASHBOARD_SUPERVISOR`, ce qui a libéré le verrou).
- **Incident d'encodage rencontré et corrigé pendant l'extraction** : `git show ... > fichier` via la redirection texte PowerShell corrompait les caractères accentués (`—` devenait `â€"`, UTF-8 mal réinterprété par le pipeline PowerShell). Contourné en utilisant `cmd /c "git show ... > fichier"` (redirection brute, sans réinterprétation d'encodage) — vérifié caractère par caractère après coup.
- **Vérifié `git diff 34d2c014 -- index.html` = 0 ligne** : le fichier restauré est strictement identique à la version compacte qui fonctionnait.
- Dashboard rechargé (nouveau PID, ancien process arrêté proprement) ; `curl http://127.0.0.1:8010/` confirmé **HTTP 200, 25400 octets**, contenu vérifié = version compacte (`sec-system` présent, `tabbar` absent).
- Commit `b71a6d44` : « rollback dashboard: restore working compact version (page blanche onglets) ».

## Étape 2 — Diagnostic de la version onglets (02144703)

Version récupérée depuis git (**jamais activée sur le dashboard live**) et analysée hors-ligne selon les 4 pistes citées par la mission :

| Piste | Résultat |
|---|---|
| Erreur de syntaxe JS | **Aucune trouvée.** Comptage programmatique : accolades `{}` 122/122, parenthèses `()` 314/314, crochets `[]` 9/9, backticks pairs (46, nombre pair) — tout équilibré. Un seul `<script>`/`</script>`, aucune sous-chaîne `</script` littérale à l'intérieur qui aurait pu fermer prématurément la balise. |
| Référence DOM avant rendu | Les deux appels JS de haut niveau (`$('acc-target').addEventListener(...)`, `document.querySelectorAll('#j-filters ...')`) ciblent des éléments présents dans le HTML statique, qui précède le `<script>` en fin de `<body>` — la référence existe bien au moment de l'exécution. |
| Fetch initial sans catch | `refreshAll()` a bien un `try/catch` englobant (hérité de la version compacte, inchangé) ; un throw dans un fetch async ne bloque de toute façon jamais le rendu HTML déjà parsé (le script tourne après coup, en fin de `<body>`). |
| CSS qui masque tout | Vérifié ligne par ligne : `.tab-panel { display: none; }` est bien **scopé à la classe**, pas au `body` ; `.tab-panel.active { display: block; }` a la spécificité CSS suffisante pour l'emporter ; le panneau `Live` porte `class="tab-panel active"` **directement dans le HTML statique** (pas ajouté par JS) — donc même JS totalement cassé, Live devrait rester visible par pur CSS. Aucune règle globale `body`/`html { display: none }` trouvée. |
| Structure HTML | `div` 63/63, `section` 10/10, `nav` 1/1, `button` 16/16 — tous équilibrés. |
| Intégrité de transmission serveur | Fichier servi isolément sur un port de test (8099, complètement séparé du dashboard live) : `HTTP 200`, `Content-Length` correct, `Content-Type: text/html` correct — aucune corruption ni erreur serveur. |

**Aucun défaut structurel ou syntaxique confirmé.**

### Tentatives de rendu réel (Chromium headless)

Deux méthodes tentées pour obtenir la preuve visuelle/console demandée par la mission (« ouvrir le HTML, lire la console ») :
1. `msedge --headless --screenshot=...` (méthode déjà tentée avec succès partiel dans une mission précédente pour d'autres besoins) : `ExitCode 0` mais **aucun fichier produit**, y compris après plusieurs variantes de flags (`--no-remote`, `--headless=old`, profils isolés).
2. `msedge --headless --dump-dom` (nouvelle tentative cette mission, contre le fichier local ET contre une copie servie en HTTP isolé) : `ExitCode 0` mais **stdout vide**, aucune sortie capturée malgré une configuration correcte (process isolé, profil neuf, `--virtual-time-budget`).

**Conclusion sur l'outillage** : Chromium headless sur cette machine ne produit fiablement aucune sortie exploitable (ni capture d'écran, ni dump DOM), quel que soit le fichier ciblé — limitation d'environnement déjà rencontrée lors d'une mission précédente, pas spécifique à cette version d'`index.html`. Impossible d'obtenir une preuve console directe dans le temps disponible sans installer un outillage supplémentaire (Playwright, refusé/reporté précédemment faute de demande explicite).

## Étape 3 — NON tentée

Conformément à l'instruction explicite de la mission (« pas de réécriture à l'aveugle » + clause de sortie si la cause n'est pas trouvée rapidement), **aucune correction n'a été appliquée à la version onglets**. La cause précise de la page blanche rapportée par SIMO reste non confirmée par les outils disponibles dans cette session.

**Hypothèse la plus probable, non vérifiée** : compte tenu de l'absence de tout défaut détectable dans le fichier lui-même (syntaxe, structure, transmission serveur tous propres), le symptôme pourrait provenir d'un facteur externe au fichier — cache navigateur agressif sur le téléphone/PC de SIMO servant une version partiellement transférée, une erreur transitoire du process dashboard au moment précis du déploiement (avant le rechargement complet), ou un comportement spécifique au navigateur de SIMO non reproductible ici. **Non confirmé — à vérifier en priorité dans la session dédiée via la console développeur réelle de SIMO (F12 sur PC, ou un partage d'écran/capture d'erreur).**

## Vérification finale

- `git diff` entre le safepoint (commit mission `e5b68cae`) et HEAD : **uniquement `app/dashboard_api/static/index.html`** touché (118 insertions, 191 suppressions — net retour à la version compacte).
- `decision_dataset.jsonl` : diff vide, confirmé.
- Bot de trading (`app.main`, PID 4708) : `CreationDate` inchangée (09/07/2026 05:41:21) — jamais arrêté ni redémarré pendant cette mission.
- Mode : DEMO, confirmé via `/api/status` après rechargement du dashboard.
- **Suite de tests : 7 échecs présents, mais confirmés SANS RAPPORT avec ce changement.** Cause identifiée avec certitude dans le log de test lui-même : `[WEEKEND_FLAT] action=CLOSE_ALL ... now_utc=2026-07-10T21:42:...+00:00` — l'heure UTC réelle au moment de l'exécution des tests (vendredi 21:42 UTC) est passée la frontière `WEEKEND_FLAT` (21:00 UTC), et ces tests (`test_bloc4_exit_v2.py::TestRouterGoldAuthority`, `test_smart_rescue_quick_exit.py`) n'isolent pas `datetime.utcnow()` — ils sont sensibles à l'heure réelle, un défaut d'hermétisme de suite préexistant, pas une régression introduite ici. Confirmé par ré-exécution isolée (échoue seul, avec le même log `WEEKEND_FLAT`) et par un test avec `git stash` (échoue identiquement quel que soit le contenu d'`index.html`, un fichier HTML statique ne pouvant structurellement pas influencer un gate de trading Python). **Aucun rapport possible avec le fichier modifié dans cette mission.** À signaler séparément pour la watch-list (tests non hermétiques au temps réel), hors périmètre de cette mission UI.

## CAUSE (une phrase)

**Non confirmée** — la version onglets ne présente aucun défaut de syntaxe, de structure HTML/CSS ou de transmission serveur détectable par analyse statique et test de service isolé ; l'outillage de rendu headless disponible sur cette machine n'a pas pu produire de preuve console pour trancher, et l'hypothèse la plus probable (cache client ou incident transitoire de déploiement) reste à vérifier via la console développeur réelle de SIMO.

## État final

**La version onglets réparée N'EST PAS en ligne** — SIMO dispose de la version compacte fonctionnelle (identique à `34d2c014`, vérifiée bit-à-bit), restaurée et confirmée servie par le dashboard. Les onglets attendent une session dédiée avec accès à la console développeur réelle pour un diagnostic concluant.
