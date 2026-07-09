# KILLSWITCH_REPLAY — multi-jours (lecture seule, simulation)

Jours analyses : **2026-07-08, 2026-07-09**. 2026-07-07 est exclu : verifie, zero declenchement kill-switch ce jour-la, rien a rejouer.

## Jour 2026-07-08

Fenetre : **2026-07-07T21:00:00+00:00** -> **2026-07-08T21:00:00+00:00** (jour broker UTC+3).
Declenchement kill-switch retenu : **2026-07-08T17:50:20+00:00** (session **NEW_YORK**) -- declencheur RECALCULE independamment depuis MT5 (fenetre corrigee) -- le declencheur LOGUE ce jour-la (04:38:36 UTC) est invalide, issu du bug de fenetre de requete 3h corrige depuis, voir mission/DATASET.md.

- Blocages bruts apres declenchement : **160**
- Apres filtre contrefactuel : **134** retenus, **26** ecartes

| raison d'exclusion | count |
|---|---|
| CONFLUENCE_TOO_LOW | 26 |

- Dedup : 126 re-evaluations fusionnees -> **8 opportunites** rejouees.

| heure UTC | session | symbole | sens | entree | SL | TP | 1er touche | R | $ simule |
|---|---|---|---|---|---|---|---|---|---|
| 18:10:06 | NEW_YORK | BTCUSD# | SELL | 62193.90 | 63059.89 | 60894.92 | STILL_OPEN | -0.01 | -0.10 |
| 18:11:03 | NEW_YORK | GOLD# | SELL | 4067.70 | 4138.49 | 3961.51 | STILL_OPEN | -0.16 | -11.19 |
| 18:25:31 | NEW_YORK | BTCUSD# | SELL | 62243.25 | 63065.26 | 61010.23 | STILL_OPEN | 0.05 | 0.39 |
| 19:15:33 | NEW_YORK | BTCUSD# | SELL | 62100.10 | 62993.38 | 60760.18 | STILL_OPEN | -0.12 | -1.04 |
| 20:00:15 | NEW_YORK | GOLD# | SELL | 4079.70 | 4137.06 | 3993.66 | STILL_OPEN | -0.00 | -0.00 |
| 20:07:48 | NEW_YORK | BTCUSD# | SELL | 62245.40 | 62878.16 | 61296.26 | STILL_OPEN | -0.00 | -0.00 |
| 20:20:20 | NEW_YORK | BTCUSD# | SELL | 62168.65 | 62879.33 | 61102.63 | STILL_OPEN | -0.00 | -0.00 |
| 20:30:55 | NEW_YORK | BTCUSD# | SELL | 62254.70 | 62878.17 | 61319.50 | STILL_OPEN | -0.00 | -0.00 |

| session | N | win rate | P&L simule $ |
|---|---|---|---|
| NEW_YORK | 8 | 12.5% | -11.94 |

- **Reel** (P&L recalcule independamment depuis MT5 pour ce jour broker, pas la valeur loguee par daily_killswitch qui peut etre celle du bug pre-fix) : **-3.10 $** (6 pertes reelles)
- **P&L simule des opportunites bloquees** : **-11.94 $** (1G/3P, win rate 12.5%)
- **Contrefactuel** (reel + simule) : **-15.04 $**
- Meilleure opportunite : BTCUSD# SELL @ 18:25:31 UTC, STILL_OPEN, 0.39 $.
- Pire opportunite : GOLD# SELL @ 18:11:03 UTC, STILL_OPEN, -11.19 $.

## Jour 2026-07-09

Fenetre : **2026-07-08T21:00:00+00:00** -> **2026-07-09T21:00:00+00:00** (jour broker UTC+3).
Declenchement kill-switch retenu : **2026-07-09T09:25:16.759042+00:00** (session **LONDRES**) -- declencheur logue directement (fiable : pnl_cross_check_divergence=0.0 des la premiere occurrence).

- Blocages bruts apres declenchement : **475**
- Apres filtre contrefactuel : **322** retenus, **153** ecartes

| raison d'exclusion | count |
|---|---|
| CONFLUENCE_TOO_LOW | 151 |
| SYMBOL_TRADE_COOLDOWN | 2 |

- Dedup : 300 re-evaluations fusionnees -> **22 opportunites** rejouees.

| heure UTC | session | symbole | sens | entree | SL | TP | 1er touche | R | $ simule |
|---|---|---|---|---|---|---|---|---|---|
| 09:31:59 | LONDRES | BTCUSD# | BUY | 62871.05 | 61661.77 | 64684.97 | STILL_OPEN | -0.12 | -1.47 |
| 09:47:41 | LONDRES | BTCUSD# | BUY | 62838.80 | 61690.25 | 64561.63 | STILL_OPEN | -0.10 | -1.15 |
| 10:42:49 | LONDRES | BTCUSD# | SELL | 62869.80 | 62932.67 | 62775.50 | TP_HIT | 1.50 | 0.94 |
| 11:02:06 | LONDRES | BTCUSD# | BUY | 62822.00 | 62759.18 | 62947.64 | SL_HIT | -1.00 | -0.63 |
| 11:24:56 | LONDRES | BTCUSD# | SELL | 62729.00 | 62791.67 | 62635.00 | TP_HIT | 1.50 | 0.94 |
| 11:47:58 | LONDRES | BTCUSD# | BUY | 62667.80 | 61632.50 | 64220.75 | STILL_OPEN | 0.05 | 0.56 |
| 12:06:04 | OVERLAP | BTCUSD# | SELL | 62712.00 | 62774.67 | 62618.00 | SL_HIT | -1.00 | -0.63 |
| 12:16:36 | OVERLAP | BTCUSD# | SELL | 62757.80 | 62820.56 | 62663.66 | SL_HIT | -1.00 | -0.63 |
| 12:17:07 | OVERLAP | GOLD# | SELL | 4110.98 | 4115.09 | 4104.81 | SL_HIT | -1.00 | -4.11 |
| 12:29:55 | OVERLAP | BTCUSD# | BUY | 62797.70 | 61603.41 | 64589.13 | STILL_OPEN | -0.06 | -0.74 |
| 12:42:56 | OVERLAP | GOLD# | SELL | 4114.82 | 4118.93 | 4108.65 | TP_HIT | 1.50 | 6.17 |
| 13:20:40 | OVERLAP | BTCUSD# | SELL | 62797.95 | 62860.68 | 62703.86 | TP_HIT | 1.50 | 0.94 |
| 14:00:09 | OVERLAP | BTCUSD# | BUY | 63031.50 | 61574.22 | 65217.42 | STILL_OPEN | -0.21 | -3.08 |
| 14:04:43 | OVERLAP | GOLD# | BUY | 4132.46 | 4048.73 | 4258.05 | STILL_OPEN | -0.00 | -0.03 |
| 14:15:52 | OVERLAP | BTCUSD# | SELL | 62815.10 | 62877.92 | 62720.88 | TP_HIT | 1.50 | 0.94 |
| 14:38:17 | OVERLAP | BTCUSD# | BUY | 62825.20 | 61574.43 | 64701.36 | STILL_OPEN | -0.08 | -1.02 |
| 14:51:49 | OVERLAP | BTCUSD# | SELL | 62893.90 | 62956.79 | 62799.56 | SL_HIT | -1.00 | -0.63 |
| 14:57:46 | OVERLAP | BTCUSD# | BUY | 62957.50 | 61603.25 | 64988.87 | STILL_OPEN | -0.17 | -2.34 |
| 15:20:18 | OVERLAP | BTCUSD# | SELL | 63035.15 | 63098.16 | 62940.64 | SL_HIT | -1.00 | -0.63 |
| 15:51:46 | OVERLAP | BTCUSD# | SELL | 62971.30 | 63034.27 | 62876.84 | TP_HIT | 1.50 | 0.94 |
| 16:16:24 | NEW_YORK | GOLD# | BUY | 4129.37 | 4045.59 | 4255.03 | STILL_OPEN | 0.04 | 3.06 |
| 16:43:41 | NEW_YORK | BTCUSD# | SELL | 62738.90 | 62899.56 | 62497.92 | STILL_OPEN | 0.10 | 0.15 |

| session | N | win rate | P&L simule $ |
|---|---|---|---|
| LONDRES | 6 | 50.0% | -0.81 |
| OVERLAP | 14 | 28.6% | -4.85 |
| NEW_YORK | 2 | 100.0% | 3.21 |

- **Reel** (P&L recalcule independamment depuis MT5 pour ce jour broker, pas la valeur loguee par daily_killswitch qui peut etre celle du bug pre-fix) : **7.63 $** (7 pertes reelles)
- **P&L simule des opportunites bloquees** : **-2.45 $** (9G/13P, win rate 40.9%)
- **Contrefactuel** (reel + simule) : **5.18 $**
- Meilleure opportunite : GOLD# SELL @ 12:42:56 UTC, TP_HIT, 6.17 $.
- Pire opportunite : GOLD# SELL @ 12:17:07 UTC, SL_HIT, -4.11 $.

## Combine — tous les jours exploitables

- **30 opportunites** rejouees au total sur 2 jour(s) (2026-07-08, 2026-07-09).
- **P&L simule combine** : **-14.39 $** (10G/16P, win rate combine 33.3%)
- **Reel combine** (somme des P&L reels recalcules par jour) : **4.53 $**
- **Contrefactuel combine** : **-9.86 $**

| session | N | win rate | P&L simule $ |
|---|---|---|---|
| LONDRES | 6 | 50.0% | -0.81 |
| OVERLAP | 14 | 28.6% | -4.85 |
| NEW_YORK | 10 | 30.0% | -8.73 |

**Meilleure opportunite (tous jours)** : 2026-07-09 GOLD# SELL @ 12:42:56 UTC, TP_HIT, 6.17 $.
**Pire opportunite (tous jours)** : 2026-07-08 GOLD# SELL @ 18:11:03 UTC, STILL_OPEN, -11.19 $.

## Caveats (obligatoires)

- **C'est une SIMULATION sur bougies historiques, pas la realite.** Ces trades n'ont jamais ete ouverts.
- Utilise le SL/TP **du signal au moment du blocage** -- n'imite PAS le trailing dynamique d'Exit V2 (le vrai resultat aurait pu differer, dans un sens comme dans l'autre, si le trade avait vraiment ete pris et gere par Exit V2).
- Suppose un remplissage au prix du signal, avec un spread **estime** (aucune valeur de spread brute n'est loguee sur les lignes refusees) : GOLD# ~0.30$, BTCUSD# ~25.00$ -- et que 0.01 lot ne bouge pas le marche.
- **2026-07-08 utilise un declenchement RECALCULE independamment** (17:50:20 UTC, pas la valeur loguee 04:38:36 UTC issue du bug de fenetre MT5 corrige depuis) -- voir la section de ce jour pour la justification complete. 2026-07-09 utilise le declenchement logue directement, deja verifie fiable.
- **Deux jours de donnees restent peu.** C'est un indice mesure, pas une loi -- surtout que l'un des deux jours (07-08) a un declenchement tres tardif (17:50 UTC), laissant peu d'heures a rejouer ce jour-la.
- **8 opportunite(s) sur un jour deja termine** (broker day clos avant maintenant) n'ont touche ni TP ni SL avant la cloture de leur jour respectif -- valorisees a la derniere bougie M1 disponible CE jour-la (une valeur historique fixe, verifiee stable sur deux executions successives de ce script, pas une estimation en mouvement).
- **10 opportunite(s) sur un jour encore en cours** (2026-07-09, pas encore a 21:00 UTC) sont valorisees au prix M1 le plus recent disponible, PAS une cloture definitive -- confirme empiriquement : leur contribution bouge d'une execution du script a l'autre tant que ce jour n'est pas clos. Re-executer ce script apres 21:00 UTC le jour meme donnerait un chiffre stable pour 07-09 aussi.

## Verdict

Meme combine sur 2 jours, resultat **marginal, ni nettement vert ni nettement rouge** (-14.39 $ simules sur 30 opportunites, win rate 33.3%). **Toujours aucun verdict tranche** : ni l'intuition de SIMO ni le statu quo ne sont mesurablement valides ou invalides par ces donnees. Il faudra accumuler d'autres jours de declenchement kill-switch avant de trancher.