# TEST_ISOLATION_REPORT — DemoKellyRouterSafetyTests

**Test-only. Zéro modification de logique.**

## Cause

`_active_symbols_subset()` (`app/mt5/demo_router.py:194-210`) est une couche de restriction **séparée et postérieure** à `SYMBOL_ALLOWLIST` (mission/DASHBOARD.md) : elle lit `ACTIVE_SYMBOLS_FILE` (`app/data/active_symbols.json`) directement sur disque, non mockée, et ne peut que RÉTRÉCIR l'allowlist, jamais l'élargir.

`DemoKellyRouterSafetyTests.setUp()` patchait déjà `SYMBOL_ALLOWLIST` pour inclure EURUSD/BTCUSD comme véhicules de test génériques — mais ce patch est antérieur à l'introduction de `_active_symbols_subset()` et ne le couvre pas. Le fichier réel de production ne liste actuellement que `["GOLD#"]` → `_active_symbols_subset()` exclut EURUSD quel que soit l'état de `SYMBOL_ALLOWLIST` → gate `DASHBOARD_SYMBOL_DISABLED` → tests rouges, y compris en isolation (confirmé : ce n'est pas un problème d'ordre d'exécution de la suite).

**Le comportement du code est correct** — un symbole désactivé côté dashboard ne doit pas router. Les tests étaient périmés (supposaient EURUSD actif sans isoler cette couche plus récente), pas le code.

## Fix

**Un seul fichier modifié : `tests/test_paper_learning_safety.py`** (17 lignes ajoutées, `DemoKellyRouterSafetyTests.setUp()`/`tearDown()`).

Ajout d'un patcher `active_symbols_patcher` pointant `app.mt5.demo_router.ACTIVE_SYMBOLS_FILE` vers un chemin qui n'existe jamais dans le `tempfile.TemporaryDirectory()` déjà créé par le test (`active_symbols_never_created.json`). `_active_symbols_subset()` a un comportement déjà documenté et testé pour ce cas : un fichier absent **échoue ouvert** vers `SYMBOL_ALLOWLIST` au complet (jamais fermé vers rien) — ce fix exploite donc un fallback déjà existant dans le code de production, il n'invente aucune donnée de test ni ne change ce que le test vérifie.

Aucune assertion modifiée. Aucun gate contourné par un mock artificiel — le chemin réel du code (`ACTIVE_SYMBOLS_FILE.exists() == False` → fail-open) est exercé tel quel.

## Preuve `git diff` = tests/ only

```
$ git diff --stat app/
app/data/backend_started_at.json  | 2 +-
app/data/news_calendar_cache.json | 2 +-
```
(les deux seuls fichiers `app/` touchés sont des caches auto-régénérés par les process live, sans rapport avec cette mission — vérifié tout au long de cette session, jamais modifiés à la main.)

`app/mt5/demo_router.py` et `app/data/active_symbols.json` : **diff vide, confirmé explicitement.**

Seul changement réel : `tests/test_paper_learning_safety.py` (+17/-0).

## Suite verte

- `DemoKellyRouterSafetyTests` isolé : **185 passed, 18 subtests passed** (0 failed).
- Suite complète, run 1/2 : **3373 passed, 2 skipped, 0 failed**.
- Suite complète, run 2/2 (relancée pour confirmer l'absence d'instabilité résiduelle) : **3373 passed, 2 skipped, 0 failed** — identique au run 1, aucune instabilité, ni l'ancien flake (`test_btc_fast_exit_daemon`, déjà corrigé au commit `1e4de88a`) ni un nouveau.

**0 failed. Suite verte, confirmée deux fois.**
