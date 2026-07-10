# DASHBOARD_AUDIT — Part A (lecture seule)

## Fichiers

| Fichier | Rôle |
|---|---|
| `app/dashboard_api/server.py` | App FastAPI, toutes les routes, `uvicorn.run(..., 127.0.0.1, port=8010)` |
| `app/dashboard_api/data.py` | Constructeurs de données pour les routes GET (lecture MT5/fichiers, jamais d'écriture trading) |
| `app/dashboard_api/actions.py` | Handlers des routes POST (process control + écriture config, **aucun `order_send` nulle part dans ce fichier** — vérifié, cité dans son propre docstring) |
| `app/dashboard_api/security.py` | PIN (PBKDF2-HMAC-SHA256), rate limit, lockout |
| `app/dashboard_api/audit.py` | Journal d'audit append-only + Telegram best-effort |
| `app/dashboard_api/static/index.html` | La page unique (HTML+CSS+JS inline) |

## Endpoints exacts

**Lecture (sans PIN, réseau Tailscale uniquement) :**
- `GET /api/status` → `data.build_status()`
- `GET /api/today` → `data.build_today()`
- `GET /api/journal?page=1&page_size=20&symbol=&result=` → `data.build_journal(...)`
- `GET /api/system` → `data.build_system()`
- `GET /api/senses?symbol=GOLD#` → `data.build_senses(...)`
- `GET /api/audit?limit=50` → `audit.read_recent_audit(...)`
- `GET /` → sert `static/index.html`

**Action (PIN + rate-limit 1/5s + lockout 5 essais/15min, corps JSON `{pin, ...}`) :**
- `POST /api/action/bot/stop` → `actions.action_bot_stop()`
- `POST /api/action/bot/start` → `actions.action_bot_start()`
- `POST /api/action/bot/restart` → `actions.action_bot_restart()`
- `POST /api/action/symbols` → corps `{pin, gold_active, btc_active}` → `actions.action_symbols_toggle(...)`
- `POST /api/action/account/switch` → corps `{pin, target, confirm_real_text}` → `actions.action_account_switch(...)`

## Champs réellement disponibles par endpoint

**`/api/status`** : `equity, balance, floating_pnl, mode (DEMO/REAL/UNKNOWN), active_profile, account_login, account_server, positions[] (ticket, symbol, direction, entry, current, floating_usd, sl, tp, exit_v2{be_armed, active_floor_usd, mode,...}), mt5_connected, generated_at`.

**`/api/today`** : `trades[], net_today_usd, wins, losses, refused_by_reason{}, generated_at`.

**`/api/journal`** : `entries[] (ticket, symbol, direction, entry, exit, sl, tp, pnl_usd, opened_at, closed_at, duration_seconds, close_mode, strategy, confluence_at_entry, mae, mfe), page, page_size, total, total_pages, net_total_usd`.

**`/api/system`** : `kill_switch{losses_today, max_losses_per_day, drawdown_pct, triggered}, watchdog_heartbeat_age_seconds, supervisor_bot_restarts_last_hour, core_version, dataset_lines, last_backup, open_alerts_today[], active_profile, generated_at`.

**`/api/senses`** : `ees_buy{side,score,band,penalty,blocked,reason}, ees_sell{...}, dxy{trend,change_h1,change_m15,gold_divergence}, atr_percentile, session, next_high_news{title,country,time_utc}, generated_at`.

**`/api/audit`** : `entries[] (ts, action, result, detail)`.

### Gap confirmé : pas de champ dédié "bot vivant", "git", "tests", "auto-médecin"

Aucun endpoint actuel n'expose un booléen direct "le process bot tourne", "l'état git", "la suite de tests", ou "l'auto-médecin". Les seuls proxies disponibles :
- **watchdog** : `watchdog_heartbeat_age_seconds` (fiable).
- **bot** : rien de direct. `mt5_connected` (dans `/api/status`) reflète la connexion MT5 **du process dashboard lui-même**, pas celle du bot (process séparé). `supervisor_bot_restarts_last_hour` compte les redémarrages, pas l'état courant.
- **git / tests / auto-médecin** : **aucune donnée exposée, nulle part.**

Pour la Part B, je n'affiche que ce qui existe réellement (watchdog + un proxy MT5 honnêtement labellisé) et je marque git/tests/médecin comme non disponibles plutôt que de les fabriquer — conformément à l'invariant de la mission.

## Structure HTML actuelle et coupables de longueur

8 sections `<section>`, **toutes dépliées en permanence, aucune n'est collapsible** : 1.État, 2.Aujourd'hui, 3.Senses, 4.Système, 5.Journal, 6.PIN, 7.Contrôle bot, 8.Bascule compte — plus un `<details>` (déjà replié par défaut) pour le journal d'audit.

Coupables confirmés :
1. **8 sections toujours dépliées empilées verticalement** — pas de logique de compaction, chaque section prend sa pleine hauteur même quand tout va bien.
2. **Journal (section 5)** : cartes multi-lignes (tête + 2-3 lignes de méta + pills) × jusqu'à 10 par page — la plus verbeuse de toutes les sections, largement responsable de la hauteur totale de page.
3. **PAS une table système à N lignes** (contrairement à l'hypothèse de départ de la mission) : la section 4 "Système" actuelle est déjà relativement compacte (6 lignes `.row` + alertes) — pas le coupable principal, mais reste une section séparée non-condensée.

## Mécanisme d'authentification PIN

`app/dashboard_api/security.py` : PIN à 6 chiffres, haché PBKDF2-HMAC-SHA256 (200 000 itérations, sel aléatoire 16 octets), stocké dans `dashboard/.env` (`DASHBOARD_PIN_HASH`/`DASHBOARD_PIN_SALT`, jamais en clair sur disque). 5 échecs → verrouillage 15 minutes de **toutes** les actions. Limite de fréquence indépendante : 1 action/5s. Chaque tentative (réussie ou non) est journalisée (`audit.py`) et notifiée Telegram.

## État de protection du mode réel — vérifié, pas supposé

`actions.action_account_switch(target, confirm_real_text)` :
1. `target` doit être un profil `VALID_PROFILES` connu.
2. Pour `target=REAL` : exige `confirm_real_text == "REAL"` (texte exact), sinon refus.
3. Le profil cible doit être **pré-configuré** (`profile.configured`), sinon refus.
4. Refuse si le compte **actuel** a des positions ouvertes magic=909002 (fail-safe : refuse aussi si l'état ne peut pas être vérifié).
5. Bascule uniquement **quel profil MT5 le bot utilisera** — ne touche à rien d'autre.

**Verrou souverain, hors de portée du dashboard, vérifié dans `app/services/adaptive_account_policy.py::trading_authorized()`** : cette fonction est ré-appelée **indépendamment à chaque ordre**, et exige `settings.allow_live_trading == True` (lu depuis `.env`, **jamais écrit par le dashboard, par `actions.py`, ni par aucun fichier de ce module**) ET une correspondance exacte `login`/`server` avec `settings.real_declared_login`/`real_declared_server`. Vérifié en direct : `allow_live_trading` **n'est pas défini dans `.env`** → tombe sur le défaut de code `app/config.py:66` → **`False`**. Le live reste désactivé au niveau maître, quelle que soit l'action dashboard.

**Conclusion du point de contrôle** : aucun risque détecté. Pas de code partagé avec le moteur de décision (`actions.py`/`data.py` ne contiennent aucun `order_send`, vérifié par recherche exhaustive). La bascule réel est déjà protégée par PIN + confirmation texte + vérification de positions + un verrou maître totalement hors de portée du dashboard. **Aucun signal d'alerte — Part B peut procéder en périmètre présentation strict, sans aucune modification à `actions.py`, `data.py`, `security.py`, `audit.py`, ni au moteur.**
