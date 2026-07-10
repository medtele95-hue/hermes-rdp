# MISSION : DASHBOARD PAGE BLANCHE — RESTAURER D'ABORD, RÉPARER ENSUITE (UI SEULE)

Symptôme : depuis la refonte onglets (commit 02144703), le dashboard affiche une **page
blanche partout** (téléphone ET PC). Diagnostic probable : erreur JavaScript fatale dans le
nouveau `index.html` qui empêche tout rendu. Priorité : SIMO doit retrouver son écran de
surveillance TOUT DE SUITE, puis on répare les onglets proprement.

═══════════════════════════════════════════════════════════════
INVARIANTS (identiques aux missions dashboard précédentes)
═══════════════════════════════════════════════════════════════
1. **Un seul fichier modifiable : `app/dashboard_api/static/index.html`.** Zéro Python,
   zéro endpoint, zéro moteur/décision/dataset.
2. Bot de trading ni arrêté ni redémarré. Seul le process dashboard peut recharger.
3. Suite tests verte après (3373/0). Toujours DEMO. Gel intact.

═══════════════════════════════════════════════════════════════
ÉTAPE 1 — RESTAURER IMMÉDIATEMENT (avant tout diagnostic)
═══════════════════════════════════════════════════════════════
- Restaure `index.html` à sa dernière version fonctionnelle :
  `git checkout 34d2c014 -- app/dashboard_api/static/index.html`
  (34d2c014 = la version compacte pré-onglets, qui s'affichait correctement).
- Vérifie que la page se charge à nouveau (curl http://127.0.0.1:8010/ retourne le HTML,
  et si possible vérifie qu'il n'y a pas d'erreur JS évidente au chargement).
- Commit : « rollback dashboard: restore working compact version (page blanche onglets) ».
→ À partir d'ici SIMO a un dashboard qui marche (long mais fonctionnel).

═══════════════════════════════════════════════════════════════
ÉTAPE 2 — DIAGNOSTIQUER LA VERSION ONGLETS (la version cassée est dans 02144703)
═══════════════════════════════════════════════════════════════
- Récupère la version onglets depuis git (sans l'activer) et trouve l'erreur fatale.
  Pistes classiques de page blanche : erreur de syntaxe JS (script qui casse tout le
  rendu), référence à un élément DOM inexistant AVANT le rendu, fetch initial qui throw
  sans catch et bloque l'affichage, CSS qui masque tout (body/panels en display:none par
  défaut sans que le JS d'init tourne).
- Reproduis en local si possible (ouvrir le HTML, lire la console).
- Identifie LA cause précise — pas de réécriture à l'aveugle.

═══════════════════════════════════════════════════════════════
ÉTAPE 3 — RÉPARER ET RÉACTIVER LES ONGLETS
═══════════════════════════════════════════════════════════════
- Corrige la cause dans la version onglets et remets-la en place, avec ces règles de
  robustesse (leçon de la panne) :
  - Le contenu doit être **visible par défaut même si le JS échoue** : panneau Live
    visible en HTML pur (pas de display:none initial sur Live), les autres panneaux
    masqués ; le JS ne fait que basculer.
  - Tout le JS d'init dans un try/catch ; les fetchs avec .catch qui affichent un
    message d'erreur À L'ÉCRAN (« API injoignable ») au lieu de tout casser.
  - Pas de dépendance externe (tout inline, comme avant).
- Vérifie : la page se charge (curl + inspection), les 4 onglets basculent, Live par
  défaut, mobile OK (viewport meta présent).
- Commit : « fix dashboard onglets: [cause précise] + fail-safe rendering ».

═══════════════════════════════════════════════════════════════
VÉRIFICATION FINALE
═══════════════════════════════════════════════════════════════
- `git diff` entre le safepoint et HEAD : ne touche QUE `index.html`.
- Suite complète : 0 failed.
- Bot intact (PID/CreationDate inchangés), DEMO, `decision_dataset.jsonl` diff vide.
- Affiche dans le terminal : la CAUSE de la page blanche (une phrase), et confirme que
  la version onglets réparée est en ligne.

**Livrable : `DASHBOARD_FIX_REPORT.md`** — la cause exacte, le fix, les protections
fail-safe ajoutées, preuves (diff/tests/bot intact).

Si l'étape 3 s'avère plus compliquée que prévu (cause introuvable rapidement) : STOP après
l'étape 1 + rapport — SIMO garde la version compacte fonctionnelle, et on réparera les
onglets dans une session dédiée. Un dashboard qui marche > des onglets cassés.
