# MISSION : RESTAURER LA SUITE VERTE — ISOLATION DES TESTS (TEST-ONLY)

Objectif : rendre la suite complète VERTE en isolant `DemoKellyRouterSafetyTests` de la
config symboles live, pour que le garde-fou « rouge = rollback » soit fiable AVANT la
mission DATASET. C'est un fix **d'hygiène de test**, pas un fix de trading.

═══════════════════════════════════════════════════════════════
CONTEXTE
═══════════════════════════════════════════════════════════════
Après le commit `1e4de88a` (fix flaky), 15-21 tests de
`tests/test_paper_learning_safety.py::DemoKellyRouterSafetyTests` échouent — y compris en
isolation. Cause tracée : `_active_symbols_subset()` (dans `app/mt5/demo_router.py`) lit le
fichier de production `app/data/active_symbols.json` **directement du disque, non mocké**.
Ces tests génériques utilisent EURUSD comme symbole véhicule, mais le fichier réel ne liste
que `["GOLD#"]` → EURUSD est bloqué (+ gate `DASHBOARD_SYMBOL_DISABLED`) → tests rouges.

Le comportement du code est CORRECT (un symbole désactivé ne doit pas router). Ce sont les
tests qui sont **périmés / non isolés** : ils supposent EURUSD actif. On les rend hermétiques
à la config live, sans changer ce qu'ils vérifient.

═══════════════════════════════════════════════════════════════
INVARIANTS ABSOLUS
═══════════════════════════════════════════════════════════════
1. **ZÉRO modification de logique.** `app/mt5/demo_router.py` NE DOIT PAS être modifié.
   `app/data/active_symbols.json` NE DOIT PAS être modifié. Aucun gate, seuil, ou chemin de
   décision touché.
2. **Modifications autorisées : UNIQUEMENT** des fichiers sous `tests/` (et au besoin un
   `conftest.py` / fixture de test). RIEN dans `app/`.
3. GOLD# intouchable. Bot en cours pas redémarré.
4. Safepoint git avant. Suite complète après.

═══════════════════════════════════════════════════════════════
TÂCHE
═══════════════════════════════════════════════════════════════
- Isole `DemoKellyRouterSafetyTests` de la config symboles live. Approche au choix, la plus
  minimale, mais **test-side uniquement** :
  - soit **mocker / patcher** la source lue par `_active_symbols_subset()` (le read de
    `ACTIVE_SYMBOLS_FILE` / `active_symbols.json`) dans ces tests, pour qu'ils fournissent
    leur propre set de symboles incluant le véhicule de test ;
  - soit patcher au niveau test le gate `DASHBOARD_SYMBOL_DISABLED` pour que le symbole
    véhicule soit considéré actif dans le périmètre du test.
- **Ne change PAS les assertions** ni ce que ces tests vérifient. Tu ne fais que les rendre
  indépendants de la config live. Leur intention de sécurité doit rester identique.
- Si un `conftest.py` / fixture partagé est le bon endroit, utilise-le, mais garde le scope
  minimal (ne casse pas d'autres tests).

═══════════════════════════════════════════════════════════════
VÉRIFICATION
═══════════════════════════════════════════════════════════════
- `git diff` ne touche QUE `tests/` (et éventuellement `conftest.py`). **Zéro ligne dans `app/`.**
- Suite complète : les 15-21 échecs disparaissent, **0 failed**, aucun nouvel échec introduit.
- Relance 2× pour confirmer qu'il ne reste aucune instabilité (ni le flaky d'origine, ni
  les nouveaux).

LIVRABLE : `TEST_ISOLATION_REPORT.md` — la cause, ce qui a été mocké (fichiers de test
listés), la preuve `git diff = tests/ only`, et la confirmation suite verte (X passed, 0 failed).

Puis : on lance DATASET sur une base propre ET verte — le garde-fou sera fiable.
