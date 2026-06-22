# AUDIT HERMES MT5 — DASHBOARD FRONTEND
**Date** : 2026-06-22  |  **Source** : local_dashboard/src/

---

## TECH STACK

| Composant | Version/Config |
|---|---|
| Framework | React + TypeScript |
| Build | Vite |
| Style | Tailwind CSS (postcss.config.js, tailwind.config.js) |
| UI libs | d3 (charts), react-transition-group (animations) |
| Routing | Implicite (multi-pages dans src/pages/) |
| Data | Polling HTTP via hooks custom (src/api/usePolling) |

---

## PAGES EXISTANTES

| Page | Fichier | Données affichées |
|---|---|---|
| Command Center | CommandCenter.tsx | Backend health, MT5 status, Account, Safety, Cycle, Latest candidate |
| Quad Terminal | QuadTerminal.tsx | 4 terminaux simultanés |
| Live Markets | LiveMarkets.tsx | Marchés en temps réel |
| Strategy Engine | StrategyEngine.tsx | Détails moteur stratégie |
| Order Flow | OrderFlowPage.tsx | Données ORDER_FLOW_EXECUTION_AGENT |
| Risk & PnL | RiskPnL.tsx | Métriques de risque et P&L |
| Logs | LogsPage.tsx | Logs applicatifs |
| Settings | Settings.tsx | Paramètres dashboard |

---

## ANALYSE CommandCenter.tsx (page principale)

### Données affichées
1. **System Status** : `backend_status`, `mt5_connected`, `heartbeat_age_seconds`, `session_name`, `backend_started_at`
2. **Account** : `balance`, `equity`, `margin`, `free_margin`, `floating_pnl`, `closed_pnl_today`
3. **Safety State** : mode, `allow_live_trading` (hardcodé `false`), `demo_max_lot` (hardcodé `0.01`)
4. **Cycle State** : `cycle_status.last_status`, `analyzed`, `skipped`, `demo_orders`, `resolved_symbols`
5. **Latest Candidate** : `symbol`, `strategy`, `direction`, `grade`, `edge_score`, `demo_eligible`, `entry/sl/tp`, `rr`, `failed_gates`
6. **Setup Hunter** : `edge_ready_count`, `near_miss_count`, `accepted_candidates`
7. **Ingest Health** : `ingest_health.status` (LIVE/DEGRADED)

### Invariants de sécurité affichés (hard-codés)
```
ALLOW_LIVE_TRADING = false       ← hard-codé dans JSX (ligne 183)
DEMO_ONLY = true                 ← hard-codé
DEMO_MAX_LOT = 0.01              ← hard-codé
order_send → demo_router.py ONLY ← hard-codé
No execution endpoints           ← hard-codé
```
**Note** : Ces valeurs sont TEXTUELLES dans le JSX — elles ne proviennent pas de l'API.
Si la config change côté backend, le dashboard n'affiche pas l'alerte correctement.
[À CONFIRMER] : L'API expose-t-elle `allow_live_trading` en boolean ? Si oui, utiliser la vraie valeur.

---

## FINDINGS DASHBOARD

### D-01 [MOYENNE] — RR affiché = rr_target calculé, pas rr réel

**Ligne** : `CommandCenter.tsx:148` — `bestCand.rr != null ? bestCand.rr.toFixed(2)+':1'`

Le `rr` affiché vient du candidat (setup_hunter payload), qui est le `rr_target` pré-capping.
Pour BTC : rr_target affiché = 2.37–3.5, realized_rr = 2.0 max (caps F-04), RR effectif < 0.5 (F-07 QuickExit 1.50).
**Impact** : Monitoring trompeur — dashboard indique performance apparente > performance réelle.

### D-02 [BASSE] — Safety invariants hard-codés (non dynamiques)

**Ligne** : `CommandCenter.tsx:182-194` — strings statiques

Les 5 invariants de sécurité sont des strings en dur dans le JSX, pas des valeurs lues depuis l'API.
En production, si `allow_live_trading` passe à `True`, le dashboard affiche toujours `false`.
**Recommandation BAC-C** : Lire `allow_live_trading` depuis `/health` endpoint et afficher en rouge si True.

### D-03 [INFO] — edge_score affiché peut être null

**Ligne** : `CommandCenter.tsx:136-138` — `bestCand.edge_score != null ? ... : '—'`

Correctement géré (null → '—'). Confirmé que DEMO_ORDER ORDER_FLOW a `edge_score=None` dans les logs.

### D-04 [INFO] — near_miss_count affiché mais données non disponibles

**Ligne** : `CommandCenter.tsx:172` — `hunter?.near_miss_count ?? 0`

L'API retourne `near_miss_count` mais les candidats near-miss n'ont pas de `near_miss_reason`
(F-01 : les DEMO_SKIP events ont `near_miss_reason=None`). Afficheun nombre sans détail disponible.

---

## ÉTAT GÉNÉRAL DASHBOARD

| Aspect | État | Remarque |
|---|---|---|
| Lecture seule | ✓ CONFIRMÉ | Aucun endpoint d'exécution côté frontend |
| Safety badge DEMO ONLY | ✓ PRÉSENT | Ligne 37-41 CommandCenter |
| Métriques temps réel | ✓ POLLING | useHealth, useDashboardStatus, useAccountSnapshot |
| Ingest health affiché | ✓ PRÉSENT | LIVE/DEGRADED depuis API |
| RR affiché = réel | ✗ FAUX | Voir D-01 — rr_target ≠ rr effectif |
| Safety flags dynamiques | ✗ PARTIEL | allow_live_trading hardcodé en string |
| near_miss_reason | ✗ NULL | F-01 — données non propagées côté backend |
