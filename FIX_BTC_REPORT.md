# FIX_BTC_REPORT — Mission 1 du GRAND_PLAN (amendée : allowlist GOLD# + BTCUSD#)

Date : 2026-07-08 · Branche : `feature/geo-confluence-hardening` · Safepoint : `safepoint-grand-plan-start` (d8598e5)
Toutes les heures de trades ci-dessous sont en **heure broker XM (UTC+3)**, telle que stampée dans les deals MT5.

## 1. Autopsie — qui a ouvert les trades BTC (point 1)

**Constat élargi : ce ne sont pas 2 mais 10 trades BTCUSD# le 2026-07-07**, tous `magic=909002`,
comment `HERMES_DEMO_KELL` → **chemin de code HERMES confirmé** (aucune source externe).

Les deux trades du constat SIMO :

| Position | Sens | Entrée (broker) | Prix | Sortie | P&L | Stratégie (dataset) |
|---|---|---|---|---|---|---|
| 374544909 | SELL | 07-07 11:36 | 63043.6 | SL 63128.8 | **−0.85** | ORDER_FLOW_EXECUTION_AGENT |
| 374758325 | SELL | 07-07 14:32 | 63131.0 | SL 63243.1 | **−1.12** | ORDER_FLOW_EXECUTION_AGENT |

Les 8 autres : 3 pertes SL (−1.94, −1.69), 5 fermetures `HERMES_RESCUE_EX` (+0.16 à +0.92).

**Pourquoi l'allowlist ne les a pas arrêtés** : elle n'existait pas encore. Les trades datent du
07-07 en journée ; le verrou `SYMBOL_ALLOWLIST` a été posé par la session FIX_BTC v1 le 08-07 à
02:47 (commit aa9a546). La reconstruction avait perdu l'invariant ; `.env` portait encore
`SYMBOLS=BTCUSD,GOLD#,EURUSD` et le registry autorisait 5 stratégies sur BTC.

## 2. Le verrou — pivot vers la décision SIMO : DEUX symboles officiels

`app/mt5/demo_router.py` : **`SYMBOL_ALLOWLIST = ("GOLD#", "BTCUSD#")`** — constante en dur au
choke-point unique (`_execution_invariants_block`, appelé dans `_send_order` et
`_send_pending_order`, les deux seuls chemins vers `mt5.order_send`). Tout autre symbole →
`[SYMBOL_BLOCKED] ... reason=SYMBOL_ALLOWLIST_INVARIANT`. Pas de flag, pas de contournement.

Réactivation amont propre : `SYMBOLS=GOLD#,BTCUSD#`, `HERMES_MAIN_SYMBOLS=GOLD#,BTCUSD#`,
`HERMES_TRADE_SYMBOLS` + `ORDER_FLOW_ALLOWED_SYMBOLS` incluent BTCUSD#/BTCUSD ;
`ALLOWED_BTC_EXECUTION_STRATEGIES` restauré (SIMO_ATM, BTC_SCALPING, FIB_CONFLUENCE,
ORDER_FLOW, STRATEGY_PACK). EUR/US100 restent hors cycle et hors allowlist.

### Invariants appliqués aux DEUX symboles (avec preuve par test)

| Invariant | Mécanisme | Preuve |
|---|---|---|
| Allowlist stricte | choke-point, constante en dur | `test_gold_only_invariant.py` : BTCUSD (sans #), EURUSD, US100, GOLD, XAUUSD… → SYMBOL_BLOCKED ; GOLD# et BTCUSD# passent ; e2e : candidat parfait hors allowlist n'atteint JAMAIS order_send |
| Lot 0.01 | `LOT_HARD_CAP` forcé au choke-point | lot 0.55 → forcé 0.01 (test) |
| Magic 909002 | `MAGIC_HARD` forcé au choke-point | magic étranger/absent → forcé (test) |
| SL/TP obligatoires | rejet `NAKED_ORDER_BLOCKED` | sl/tp 0/None → bloqué (test) |
| MAX_OPEN=1 **par symbole** | `positions_get(symbol=...)` au choke-point | une position GOLD ne bloque PAS un ordre BTC, et réciproquement (nouveau test) |
| RR ≥ 1.0 | `RR_FLOOR_BELOW_1_0` sur la requête FINALE, marché + pending | déjà en place, symbol-agnostic (demo_router:3004, 3202) |
| Kill-switch partagé | `evaluate_daily_killswitch` compte par magic, tous symboles | déjà partagé par construction ; fail-closed si historique illisible |
| Exit V2 sur les deux | `is_exit_v2_symbol` (GOLD*, XAUUSD*, BTCUSD*) route GOLD **et** BTC vers Exit V2 ; parasites QUICK_EXIT / Smart Rescue / Market Danger neutralisés sur les symboles officiels | `test_bloc4_exit_v2.py` (BTC → EXIT_V2_CLOSE trailing) + `test_smart_rescue_quick_exit.py` (rescue ne touche plus BTC) |
| Flat-weekend BTC aussi | `weekend_flat_close_due` ferme TOUTES les positions HERMES (symbol-agnostic) ; `weekend_entry_block` bloque toute entrée dès vendredi 18:00 UTC | code existant vérifié, s'applique à BTC qui cote le week-end |
| Dataset tagué par symbole | chaque ligne decision/outcome porte `symbol` + `broker_symbol` | vérifié sur les lignes réelles |

**Position BTC ouverte (point 4)** : aucune au moment de l'audit (1 seule position : GOLD# SELL
375829522) → pas de force-close nécessaire.

## 3. Autopsie du trade GOLD −13.49 (point 7)

Position 374738999 — SELL GOLD# @4140.69, 07-07 14:21 broker, stratégie
ORDER_FLOW_EXECUTION_AGENT, RR 1.5, SL 4154.06 touché à 14:58 → **−13.49**.
- **EES-SELL recalculé à l'entrée : 0.0 (bande SAIN)** — l'entrée ne chassait pas un extrême.
- **MAE −14.02 / MFE +0.70** : le trade n'a quasiment jamais respiré — mort-dès-l'entrée.
- Verdict : perte « propre » (SL respecté, taille conforme) mais **géométrie d'entrée
  défaillante** (entrée au marché sans retest, le prix est parti immédiatement contre).
  → alimenter l'analyse entry→zone de la Mission 2.

## 4. Autopsie du ticket 375071163 — VERDICT SPÉCIAL EES-BUY (amendement)

BUY GOLD# @4179.64, 07-07 16:43 broker, ORDER_FLOW_EXECUTION_AGENT, grade A, edge 94.9,
confluence finale 75.73. Fermé par SL @4118.30 le 07-07 22:01 → **−61.34 $** (la plus grosse
perte récente du compte). MAE −68.48 / MFE +0.27 : mort-dès-l'entrée, 5h18 d'agonie.

**Verdict EES-BUY** (recalcul indépendant sur les bougies M5 confirmées au moment de l'entrée,
avec le contexte CVD journalisé — divergence bear, cvd_slope −22) :

> **EES-BUY = 65.0 → bande EXTREME → `EES_EXTREME_BLOCK` obligatoire.**
> Composantes : extension 5.47×ATR au-dessus du creux opposé (40/40 pts, plafonné),
> divergence order-flow contre l'achat (15/15), 2 clôtures consécutives (10/30), climax 0.
> C'est **l'achat de sommet caractérisé** que l'EES a été conçu pour tuer (précédent validé :
> −40.86 à 73.7 EXTREME).

**Pourquoi le blocage n'a pas eu lieu** : le trade est passé en mode `DEMO_MICRO_DISCOVERY` avec
`exploration_override=MICRO_DISCOVERY_CONFLUENCE_SAMPLE` alors que le top-down était en
`TOP_DOWN_READER_BLOCK`. Et surtout : **aucun champ `ees_*` n'existe dans les 1557 lignes du
decision_dataset** (alors que demo_pilot_events en contient 319) — l'EES est soit affamé de
données (`recent_candles`) sur ce chemin, soit calculé mais non persisté et non contraignant à
ce point du flux. Le trou exact est instruit en Mission 2 (candidat CASSÉ).

## 5. HERMES_LOG_FILE permanent en dur (point 8)

`app/logger.py` : `RotatingFileHandler` vers **`logs/hermes.log`** (chemin par défaut EN DUR,
10 MB × 5, UTF-8). `HERMES_LOG_FILE` (env) peut déplacer le fichier mais **ne peut pas le
désactiver**. Fail-safe : fichier inécrivable → console maintenue, jamais de crash. Preuve :
écriture vérifiée en conditions réelles. (Caveat mineur : les runs pytest écrivent aussi dans
ce fichier ; la rotation borne l'impact.)

## 6. Tests

**Suite complète : 3143 passed, 0 failed** (+128 subtests). Corrections de contrats de tests
hérités documentées dans les commits : l'ancien monde « Exit V2 = GOLD-only » et « 4 symboles
par défaut » remplacé par le contrat 2-symboles ; harnais quick-exit legacy déplacé sur EURUSD ;
`ALLOWED_DEMO_SYMBOLS` (support harnais, PAS une autorisation de trade) restauré — 125 tests
étaient rouges depuis la v1 à cause de cette restriction, le verrou réel étant au choke-point.

## RÉSERVÉ SIMO

1. **Micro-discovery a acheté un sommet EXTREME (−61.34)** : l'override
   `MICRO_DISCOVERY_CONFLUENCE_SAMPLE` outrepasse le top-down bloqué ET n'est pas soumis à
   l'EES (qui n'a de toute façon pas fonctionné — voir Mission 2). Proposition chiffrée : une
   fois l'alimentation EES réparée, exiger `ees_band != EXTREME` pour TOUT override
   exploration/micro-discovery (zéro exception). Décision de gate → à toi.
2. **Seuils BTC hérités** (`BTCUSD_MIN_CONFLUENCE=65`, bad hours, cooldown 20 min…) réactivés
   tels quels avec le pivot — aucune modification faite. Si tu veux des seuils BTC plus stricts
   le temps de constituer le dataset BTC, dis-le.
3. **`.env` de combat très permissif** (`DEMO_IGNORE_ALL_TIME_BLOCKS=true`,
   `DEMO_MAX_TRADES_PER_DAY_TOTAL=999`) — hérité, non touché, mais c'est lui qui donne à
   micro-discovery son espace. À revoir avec le point 1.
