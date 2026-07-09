# RAPPORT_24H — analyse HERMES (lecture seule)

Fenetre : **2026-07-08T04:15:34.813880+00:00** -> **2026-07-09T04:15:34.813880+00:00** (UTC vrai) soit **2026-07-08T07:15:34.813880+00:00** -> **2026-07-09T07:15:34.813880+00:00** (heure broker UTC+3).

_Note methodologique : 143371 lignes de logs (1092 secondes distinctes) ont ete exclues de cette analyse car identifiees comme une rafale self-test/health-check periodique (positions et prix synthetiques, symbol=EURUSD hors allowlist, bid/ask/point=None) qui rejoue un cycle de decision factice plusieurs fois par heure — non filtree, elle aurait pu polluer la correlation EES/score avec de fausses lectures pour GOLD#/ORDER_FLOW_EXECUTION_AGENT. Le P&L (deals MT5, magic=909002) et les decisions (decision_dataset.jsonl) n'etaient pas affectes par cette contamination — verifie explicitement avant de produire ce rapport._

## 1. Resume executif

- 1217 setups evalues sur 24h : **20 executes**, **1197 refuses**.
- Win rate sur les executes : **55.00%** (11 gagnants / 8 perdants / 1 neutres).
- **P&L net reel (verite MT5, deals magic=909002)** : **-13.87 $**.
- Raison de refus dominante : `failed_gate=DAILY_KILLSWITCH_MAX_LOSSES` (703/1197 refus, 58.7%).
- **Important** : sur les 703 refus kill-switch, **543 (77.2%)** portent un daily_pnl calcule AVANT une recalibration du P&L observee a 18:10:06 UTC dans cette fenetre (valeurs jusqu'a -44.85$/-37.14$, sans le champ de recoupement pnl_cross_check_divergence) ; seuls **160** refus portent le calcul recoupe (divergence=0.0). Voir 4. pour le detail — a interpreter avec l'analyste avant de conclure sur le taux de refus kill-switch de la journee.
- **Verdict en une phrase** : journee negative (-13.87 $ reel sur 20 trades, 55.00% de win rate), 1197 setups ecartes majoritairement par `failed_gate=DAILY_KILLSWITCH_MAX_LOSSES`.

## 2. Setups executes

| date/heure UTC | broker | symbole | sens | strategie | score | grade | geo/smc/mtfa/of | EES sell | EES buy | Exit V2 | P&L reel $ | R |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 07-08 14:10:52 | 07-08 17:10:52 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.9 | C | 48/50/50/85 | 38.7 (SAIN) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 2.73 | 0.02 |
| 07-08 14:19:49 | 07-08 17:19:49 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 63.5 | C | 62/50/50/85 | - (?) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 14.88 | 0.12 |
| 07-08 14:49:47 | 07-08 17:49:47 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.5 | C | 42/50/50/95 | 19.8 (SAIN) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] | 0.41 | 0.02 |
| 07-08 14:55:50 | 07-08 17:55:50 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 60.4 | C | 50/50/50/85 | - (?) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 4.53 | 0.03 |
| 07-08 15:03:21 | 07-08 18:03:21 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 58.1 | C | 40/50/50/85 | - (?) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 4.98 | 0.03 |
| 07-08 15:21:58 | 07-08 18:21:58 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.7 | C | 55/50/50/85 | - (?) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 0.30 | 0.00 |
| 07-08 15:23:09 | 07-08 18:23:09 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 63.3 | C | 43/50/50/100 | 25.2 (SAIN) | - (?) | BROKER_SL | -1.77 | -0.09 |
| 07-08 15:30:46 | 07-08 18:30:46 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 63.4 | C | 52/50/50/93 | 40.0 (PRUDENCE) | - (?) | NEWS_PRECLOSE (FOMC Meeting Minutes) | -42.85 | -0.31 |
| 07-08 15:42:35 | 07-08 18:42:35 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 65.3 | B | 47/50/55/100 | 31.2 (SAIN) | - (?) | BROKER_SL | -1.59 | -0.08 |
| 07-08 16:35:55 | 07-08 19:35:55 | BTCUSD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 59.3 | C | 33/50/50/95 | 7.6 (SAIN) | 19.8 (SAIN) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] | 1.58 | 0.43 |
| 07-09 00:00:19 | 07-09 03:00:19 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 59.3 | C | 45/50/50/85 | - (?) | 17.7 (SAIN) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | -0.17 | -0.01 |
| 07-09 00:22:55 | 07-09 03:22:55 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 63.8 | C | 45/50/50/100 | 37.3 (SAIN) | 10.5 (SAIN) | BROKER_SL | -1.19 | -0.26 |
| 07-09 00:23:24 | 07-09 03:23:24 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.3 | C | 41/50/50/88 | 27.7 (SAIN) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | -0.54 | -0.01 |
| 07-09 00:29:30 | 07-09 03:29:30 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 62.5 | C | 52/50/50/90 | - (?) | - (?) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 3.04 | 0.05 |
| 07-09 00:33:37 | 07-09 03:33:37 | BTCUSD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 66.7 | B | 63/50/50/95 | 23.2 (SAIN) | 13.8 (SAIN) | BROKER_SL | -1.32 | -0.18 |
| 07-09 00:52:45 | 07-09 03:52:45 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 64.1 | C | 48/50/55/95 | 21.6 (SAIN) | 3.4 (SAIN) | BROKER_SL | -1.56 | -0.32 |
| 07-09 02:06:29 | 07-09 05:06:29 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 62.9 | C | 42/50/50/100 | - (?) | 26.4 (SAIN) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 1.96 | 0.03 |
| 07-09 02:12:39 | 07-09 05:12:39 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 61.9 | C | 38/50/50/100 | - (?) | 34.1 (SAIN) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] [trailing] | 2.11 | 0.04 |
| 07-09 02:25:12 | 07-09 05:25:12 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 62.7 | C | 41/50/50/100 | - (?) | 30.4 (SAIN) | POSITION_ENCORE_OUVERTE | 0.00 | 0.00 |
| 07-09 02:44:30 | 07-09 05:44:30 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 68.1 | B | 52/60/50/100 | 40.0 (PRUDENCE) | 5.0 (SAIN) | EXIT_V2 (EXIT_V2_TRAIL_FLOOR) [BE armed] | 0.60 | 0.11 |

## 3. Setups refuses

Sur 1197 refus, 1089 sont des blocages **structurels et repetitifs** (quota kill-switch ou position deja ouverte sur le symbole -- le meme etat de compte reevalue a chaque cycle, pas un jugement different par setup) : agreges en 4., detail complet ligne par ligne dans le CSV. Les 108 refus restants ci-dessous portent une vraie decision differenciee par setup :

| date/heure UTC | broker | symbole | sens | strategie | score | grade | EES sell | EES buy | raison |
|---|---|---|---|---|---|---|---|---|---|
| 07-08 04:20:41 | 07-08 07:20:41 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 04:30:18 | 07-08 07:30:18 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 04:38:47 | 07-08 07:38:47 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 38.1 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 05:49:28 | 07-08 08:49:28 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 05:54:57 | 07-08 08:54:57 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 05:56:33 | 07-08 08:56:33 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 06:36:13 | 07-08 09:36:13 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 06:41:29 | 07-08 09:41:29 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 06:42:57 | 07-08 09:42:57 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 6.6 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 06:49:12 | 07-08 09:49:12 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 06:54:58 | 07-08 09:54:58 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 0.0 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 07:01:10 | 07-08 10:01:10 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 07:05:03 | 07-08 10:05:03 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | 38.4 (SAIN) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 07:07:53 | 07-08 10:07:53 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 07:11:52 | 07-08 10:11:52 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 07:17:00 | 07-08 10:17:00 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 08:47:00 | 07-08 11:47:00 | BTCUSD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 27.8 | D | - (?) | 20.6 (SAIN) | failed_gate=RR_BELOW_1_5 |
| 07-08 08:47:20 | 07-08 11:47:20 | BTCUSD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 33.8 | D | - (?) | 20.6 (SAIN) | failed_gate=RR_BELOW_1_5 |
| 07-08 09:38:39 | 07-08 12:38:39 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:25:34 | 07-08 13:25:34 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:33:47 | 07-08 13:33:47 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:41:29 | 07-08 13:41:29 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:43:59 | 07-08 13:43:59 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:48:35 | 07-08 13:48:35 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:53:21 | 07-08 13:53:21 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 10:59:12 | 07-08 13:59:12 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:04:56 | 07-08 14:04:56 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 16.4 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:10:29 | 07-08 14:10:29 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:14:03 | 07-08 14:14:03 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:32:19 | 07-08 14:32:19 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:34:58 | 07-08 14:34:58 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:36:15 | 07-08 14:36:15 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:40:51 | 07-08 14:40:51 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:45:53 | 07-08 14:45:53 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 11:56:02 | 07-08 14:56:02 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 13:36:30 | 07-08 16:36:30 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 13:56:27 | 07-08 16:56:27 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 13:58:20 | 07-08 16:58:20 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:00:15 | 07-08 17:00:15 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:02:10 | 07-08 17:02:10 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:06:07 | 07-08 17:06:07 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:12:20 | 07-08 17:12:20 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:38:09 | 07-08 17:38:09 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:44:12 | 07-08 17:44:12 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:51:45 | 07-08 17:51:45 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:54:10 | 07-08 17:54:10 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 14:59:51 | 07-08 17:59:51 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 15:07:38 | 07-08 18:07:38 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 15:11:19 | 07-08 18:11:19 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 15:16:51 | 07-08 18:16:51 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 15:20:26 | 07-08 18:20:26 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 17:50:26 | 07-08 20:50:26 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 69.7 | B | 29.3 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:51:10 | 07-08 20:51:10 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 64.5 | C | 17.7 (SAIN) | 40.0 (PRUDENCE) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:51:35 | 07-08 20:51:35 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 64.5 | C | 17.7 (SAIN) | 40.0 (PRUDENCE) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:51:58 | 07-08 20:51:58 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 64.5 | C | 17.7 (SAIN) | 40.0 (PRUDENCE) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:52:22 | 07-08 20:52:22 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 69.7 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:52:46 | 07-08 20:52:46 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 72.3 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:53:14 | 07-08 20:53:14 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 69.7 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:53:39 | 07-08 20:53:39 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 72.5 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:54:07 | 07-08 20:54:07 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 72.7 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:54:22 | 07-08 20:54:22 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.6 | C | 29.3 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:54:38 | 07-08 20:54:38 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 73.3 | B | 17.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:55:13 | 07-08 20:55:13 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 70.6 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:55:27 | 07-08 20:55:27 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.5 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:55:38 | 07-08 20:55:38 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 70.6 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:55:53 | 07-08 20:55:53 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.9 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:56:06 | 07-08 20:56:06 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 70.8 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:56:17 | 07-08 20:56:17 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.3 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:56:28 | 07-08 20:56:28 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 70.8 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:56:40 | 07-08 20:56:40 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.5 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:56:50 | 07-08 20:56:50 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 70.8 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:57:02 | 07-08 20:57:02 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 62.0 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:57:23 | 07-08 20:57:23 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.5 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:57:36 | 07-08 20:57:36 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 65.9 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:57:49 | 07-08 20:57:49 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.6 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:58:03 | 07-08 20:58:03 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 65.8 | B | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:58:14 | 07-08 20:58:14 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.5 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:58:37 | 07-08 20:58:37 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.6 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:59:00 | 07-08 20:59:00 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.4 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:59:21 | 07-08 20:59:21 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.4 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 17:59:42 | 07-08 20:59:42 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.4 | C | 34.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:00:19 | 07-08 21:00:19 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 62.0 | C | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:00:36 | 07-08 21:00:36 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 62.4 | C | 23.4 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:02:01 | 07-08 21:02:01 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 58.4 | C | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:02:32 | 07-08 21:02:32 | GOLD# | BUY | ORDER_FLOW_EXECUTION_AGENT | 59.1 | C | - (?) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:04:17 | 07-08 21:04:17 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 18:07:10 | 07-08 21:07:10 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 60.1 | C | 22.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:07:25 | 07-08 21:07:25 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.4 | C | 19.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:07:42 | 07-08 21:07:42 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 60.2 | C | 22.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:07:57 | 07-08 21:07:57 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 61.9 | C | 19.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:08:16 | 07-08 21:08:16 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 60.4 | C | 22.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:08:35 | 07-08 21:08:35 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.6 | C | 19.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:08:51 | 07-08 21:08:51 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | 19.7 (SAIN) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-08 18:08:55 | 07-08 21:08:55 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 60.1 | C | 22.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:09:29 | 07-08 21:09:29 | BTCUSD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 66.8 | B | 22.8 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-08 18:09:48 | 07-08 21:09:48 | GOLD# | SELL | ORDER_FLOW_EXECUTION_AGENT | 59.6 | C | 19.7 (SAIN) | - (?) | failed_gate=NEWS_BLACKOUT |
| 07-09 01:12:06 | 07-09 04:12:06 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:16:29 | 07-09 04:16:29 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:21:42 | 07-09 04:21:42 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:35:27 | 07-09 04:35:27 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:45:44 | 07-09 04:45:44 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 25.5 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:50:33 | 07-09 04:50:33 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:54:27 | 07-09 04:54:27 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 01:59:18 | 07-09 04:59:18 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 02:01:38 | 07-09 05:01:38 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 02:05:24 | 07-09 05:05:24 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 02:09:01 | 07-09 05:09:01 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | 26.4 (SAIN) | failed_gate=GOLD_ANALYSIS_ONLY |
| 07-09 02:21:48 | 07-09 05:21:48 | GOLD# | SELL | GOLD_LIQUIDITY_HUNTER_PRO | 67.5 | None | - (?) | - (?) | failed_gate=GOLD_ANALYSIS_ONLY |

## 4. Agregats

- N evalues=1217 · N executes=20 · N refuses=1197

**Refus par raison :**

| raison | count | % |
|---|---|---|
| failed_gate=DAILY_KILLSWITCH_MAX_LOSSES | 703 | 58.7% |
| failed_gate=MAX_OPEN_TRADES_PER_SYMBOL | 386 | 32.2% |
| failed_gate=GOLD_ANALYSIS_ONLY | 63 | 5.3% |
| failed_gate=NEWS_BLACKOUT | 43 | 3.6% |
| failed_gate=RR_BELOW_1_5 | 2 | 0.2% |

**Detail des refus kill-switch — ecart de calcul P&L detecte (regle de signe obligatoire de cette mission) :**

- 543 refus avec un `daily_pnl` calcule AVANT l'apparition du champ de recoupement `pnl_cross_check_divergence` dans cette fenetre (transition nette observee a 2026-07-08T18:10:06 UTC) — valeurs vues jusqu'a -44.85$ / -37.14$.
- 160 refus avec le calcul recoupe actif (`pnl_cross_check_divergence=0.0`).
- **Ecart_affichage** : les refus du premier groupe reposaient sur un daily_pnl dont cette meme session a par ailleurs identifie et corrige le mode de calcul (fenetre de requete MT5 decalee de 3h) — la valeur affichee a l'epoque n'est PAS necessairement le P&L reel de la journee. Fait, pas une recommandation : a discuter avec l'analyste avant de conclure sur le taux reel de blocage kill-switch de cette fenetre de 24h.

**Win rate exécutés** : 55.00% (11G / 8P)

**P&L net reel (verite MT5)** : -13.87 $

**P&L par grade :**

| grade | P&L $ | N trades |
|---|---|---|
| B | -2.31 | 3 |
| C | -11.56 | 17 |

**P&L par session :**

| session | P&L $ | N trades |
|---|---|---|
| ASIA_MAIN | 2.93 | 10 |
| OVERLAP | -16.80 | 10 |

**P&L par symbole :**

| symbole | P&L $ | N trades |
|---|---|---|
| BTCUSD# | -4.84 | 8 |
| GOLD# | -9.03 | 12 |

**Setups refuses en bande EES EXTREME** : 2/1197. Estimation directionnelle honnête (pas une promesse) : sans données d'exécution réelle pour ces setups jamais pris, il est impossible de dire s'ils auraient perdu ou gagné — la bande EXTREME est un signal d'épuisement, pas une garantie de retournement immédiat. Aucune reconstruction contrefactuelle de P&L n'est faite ici, seul le décompte est factuel.

**Meilleur trade** : GOLD# SELL @ 07-08 14:19:49 UTC, strategie ORDER_FLOW_EXECUTION_AGENT, score 63.5 grade C, P&L reel 14.88 $, sortie EXIT_V2 (EXIT_V2_TRAIL_FLOOR).
**Pire trade** : GOLD# SELL @ 07-08 15:30:46 UTC, strategie ORDER_FLOW_EXECUTION_AGENT, score 63.4 grade C, P&L reel -42.85 $, sortie NEWS_PRECLOSE (FOMC Meeting Minutes).

## 5. Top 3 observations factuelles

1. Le blocage dominant sur cette fenetre est `failed_gate=DAILY_KILLSWITCH_MAX_LOSSES` (703 refus sur 1197, 58.7%).
2. 6/20 clotures sur cette fenetre ne viennent PAS d'Exit V2 (SL/TP broker, NEWS_PRECLOSE, WEEKEND_FLAT, ou autre) — voir colonne 'Exit V2' du tableau 2..

_Aucune recommandation de changement de systeme, aucun nouveau seuil propose — on decrit, on ne prescrit pas._