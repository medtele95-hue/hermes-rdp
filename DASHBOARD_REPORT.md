# DASHBOARD_REPORT — mission/DASHBOARD.md, exécutée le 2026-07-08

Control-room mobile pour HERMES : lecture (état, journal, système, senses)
+ actions (stop/start/restart bot, allowlist symboles, bascule DEMO/REAL),
sécurisé PIN + Tailscale, journal d'audit complet. Résumé exécutif pour
SIMO ci-dessous ; détails techniques dans les commits (préfixe `dashboard
mission`).

## 1. Ce qui a été livré

| Composant | Fichier | Rôle |
|---|---|---|
| Backend lecture | `app/dashboard_api/data.py` | status/today/journal/system/senses, MT5 lu en direct (process séparé) |
| Backend actions | `app/dashboard_api/actions.py` | stop/start/restart bot, toggle symboles, bascule compte — aucun appel de soumission d'ordre nulle part |
| Sécurité | `app/dashboard_api/security.py` | PIN pbkdf2+sel, lockout 5/15min, rate-limit 1/5s |
| Audit | `app/dashboard_api/audit.py` | `dashboard/actions_audit.log` (jsonl append-only) + Telegram |
| Serveur | `app/dashboard_api/server.py` | FastAPI, process autonome, port 127.0.0.1:8010 |
| Frontend | `app/dashboard_api/static/index.html` | page mobile unique, vanilla JS, refresh 30s |
| Profils compte | `app/mt5/account_profiles.py`, `account_profiles.env` | DEMO préremplie, REAL vide (jamais écrite par le code) |
| Symboles à chaud | `app/mt5/demo_router.py::_active_symbols_subset` | sous-ensemble narrow-only, re-lu à chaque check, pas de restart requis |
| Exit V2 lecture | `app/mt5/demo_router.py::_write_exit_v2_snapshot` | `app/data/exit_v2_state.json`, snapshot passif après chaque évaluation |
| Superviseur | `scripts/dashboard_supervisor.ps1` | anti-crash-loop dédié dashboard, indépendant du bot |
| Watchdog | `watchdog/hermes_watchdog.py::check_10_dashboard_heartbeat` | INFO seulement, jamais couplé au trading |
| Tailscale | `TAILSCALE_SETUP.md` | guide pas-à-pas — **installation manuelle requise, voir §5** |

## 2. Garanties de sécurité — comment elles tiennent, pas seulement où

- **Aucun appel de soumission d'ordre dans `app/dashboard_api/`** : vérifié
  par le test de sécurité global du repo (`test_order_send_only_in_demo_router`,
  qui grep l'exact nom de fonction hors de `app/mt5/demo_router.py`) — mon
  propre code a d'ailleurs été pris en défaut une fois par ce test (un
  docstring mentionnait le nom en toutes lettres pour dire qu'il n'était
  jamais appelé — reformulé).
- **`allow_live_trading` / `real_declared_login` / `real_declared_server`
  restent hors d'atteinte du dashboard** : `app/dashboard_api/actions.py`
  n'importe même pas `app.config` — garantie structurelle, pas juste
  documentée (`test_never_imports_app_config_settings`).
- **Bascule REAL toujours refusée si le profil n'est pas configuré à la
  main**, même avec le texte de confirmation exact — propriété testée
  explicitement comme la plus critique de toute la mission
  (`test_real_unconfigured_always_refuses_even_with_correct_confirmation`).
  `account_profiles.env` a sa section REAL volontairement vide ; je ne l'ai
  jamais remplie et ne la remplirai jamais — seule SIMO peut le faire à la
  main.
- **Le sous-ensemble symboles du dashboard ne peut que rétrécir
  `SYMBOL_ALLOWLIST`, jamais l'élargir** — testé explicitement avec des
  symboles hors allowlist injectés dans le fichier de config (silencieusement
  ignorés).
- **`trading_authorized()` (`app/services/adaptive_account_policy.py`)
  reste totalement indépendant** : il re-vérifie le compte MT5 réel à
  chaque ordre, sans lien avec le dashboard. Une bascule de profil ne fait
  que changer À QUEL compte le bot se connecte à son prochain démarrage —
  elle n'accorde aucune permission de trading en soi.

## 3. Ce qui a été démontré en conditions réelles (pas seulement testé)

Démonstration live effectuée le 2026-07-08 11:08-11:09 UTC contre le bot en
production (0 position ouverte au moment du test) :

```
11:08:45  bot_stop    -> OK, PID 7776 tué, logs/stopped_by_user.flag créé
11:08:47  bot_start   -> REFUSED (rate-limit 1/5s, preuve que la protection marche)
11:09:09  bot_start   -> OK, flag levé
11:09:16  symbols     -> OK, GOLD# seul actif
11:09:22  symbols     -> OK, GOLD#+BTCUSD# restaurés
11:09:28  account/switch REAL -> FAILED, PROFILE_NOT_CONFIGURED (refus même avec confirmation correcte)
```

À 12:08:51, `scripts\bot_supervisor.ps1` (tâche planifiée déjà active) a
détecté l'absence du bot et l'a redémarré automatiquement (nouveau PID
8940) — la chaîne complète stop → flag → superviseur-respecte-le-flag →
start → flag-levé → superviseur-redémarre est vérifiée de bout en bout, pas
seulement en théorie.

Trace complète dans `dashboard/actions_audit.log` (local, jamais commité).

## 4. Ce qui N'A PAS été démontré, et pourquoi

- **Bascule réelle vers un compte REAL** : aucun compte réel n'est
  configuré (`account_profiles.env` REAL vide, volontairement). Le code
  path de refus est démontré et testé ; le code path de succès ne l'a pas
  été puisqu'il n'existe aucun compte réel à basculer vers, par choix de
  conception — configurer un compte réel est un acte que seule SIMO doit
  poser.
- **Redémarrage effectif via `scripts/dashboard_supervisor.ps1`** : le
  script est écrit, parsé (`ParseFile` sans erreur), mais aucune tâche
  planifiée `HERMES_DASHBOARD_SUPERVISOR` n'a été créée — je n'ai pas
  installé de tâche planifiée Windows automatiquement (action système
  persistante ; à créer à la main, voir §6).

## 5. Tailscale — action manuelle requise

Tailscale n'était pas installé sur cette machine. L'installer et
s'authentifier (`tailscale up`) nécessite un navigateur et le compte
personnel de SIMO — je ne peux pas le faire à sa place. Guide complet dans
`TAILSCALE_SETUP.md` : installation, `tailscale up`, `tailscale serve --bg
8010` (jamais `funnel`), app téléphone, preuve de non-exposition publique
(`netstat` ne doit jamais montrer `0.0.0.0:8010`).

## 6. Pour démarrer le dashboard (une fois Tailscale configuré)

```powershell
python -m app.dashboard_api.server
```

Le PIN est généré automatiquement au premier démarrage et affiché **une
seule fois** dans `logs/hermes.log` (`[DASHBOARD] PREMIER DEMARRAGE — PIN
genere: XXXXXX`) — à noter immédiatement. Pour le changer : supprimer
`DASHBOARD_PIN_HASH`/`DASHBOARD_PIN_SALT` de `dashboard/.env` et relancer.

Pour la supervision automatique (survit à un crash), créer une tâche
planifiée déclenchée au boot, sur le modèle de `HERMES_BOT_SUPERVISOR` :

```powershell
schtasks /Create /TN "HERMES_DASHBOARD_SUPERVISOR" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Admin\Documents\hermes-mt5-agent\scripts\dashboard_supervisor.ps1" /SC ONSTART /RL HIGHEST /F
```

## 7. Comportement documenté

- **Stop** : positions ouvertes gardent leur SL/TP broker tel quel ; Exit
  V2 arrête de trailer tant que le bot est arrêté (son état vit en mémoire
  du process bot, pas ailleurs).
- **Toggle symboles** : effet immédiat, pas de redémarrage (le choke-point
  `_active_symbols_subset()` est relu à chaque tentative d'ouverture).
- **Lecture** (status/today/journal/system/senses) : jamais de PIN — accès
  = présence sur le tailnet Tailscale, cohérent avec la spec mission.
- **Action** : PIN systématique + rate-limit 1/5s + lockout 5 essais/15min,
  chaque tentative (réussie ou refusée) journalisée.

## 8. Tests et suite complète

68 tests nouveaux dédiés à cette mission (profils, connexion, sécurité,
audit, données, actions, serveur FastAPI, snapshot Exit V2, sous-ensemble
symboles, watchdog check 10). Suite complète du repo : **3291 passed, 2
skipped, 0 regression** au dernier run.

Un bug de pollution de test a été trouvé et corrigé pendant la
démonstration live : un fichier `app/data/active_symbols.json` laissé sur
disque par mes propres tests manuels faisait échouer 12 tests sans rapport
ailleurs dans le repo (une suite legacy qui patch `SYMBOL_ALLOWLIST` plus
large pour tester une stratégie EUR historique). Cause : le fichier réel
sur disque n'était pas couvert par le patch de test. Supprimé — jamais un
bug de production puisque `SYMBOL_ALLOWLIST` n'est jamais patché hors
tests.
