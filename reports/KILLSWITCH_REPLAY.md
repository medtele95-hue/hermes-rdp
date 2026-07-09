# KILLSWITCH_REPLAY — 2026-07-09 (lecture seule, simulation)

Fenetre analysee : **2026-07-08T21:00:00+00:00** -> **2026-07-09T21:00:00+00:00** (jour broker UTC+3).
Premier declenchement kill-switch : **2026-07-09T09:25:16.759042+00:00** (session **LONDRES**, 7e perte -- 6 pertes deja atteintes juste avant).

## Population retenue

- Blocages bruts `DAILY_KILLSWITCH_MAX_LOSSES` apres declenchement : **460**
- Apres filtre contrefactuel (auraient passe TOUT le reste) : **307** retenus, **153** ecartes

Raisons d'exclusion (un signal peut cumuler plusieurs raisons) :

| raison | count |
|---|---|
| CONFLUENCE_TOO_LOW | 151 |
| SYMBOL_TRADE_COOLDOWN | 2 |

- Deduplication : 286 re-evaluations consecutives du meme setup (meme symbole+sens+strategie, ecart <= 10 min) fusionnees -> **21 opportunites de trade distinctes** a rejouer.

## Opportunites rejouees

| heure UTC | session | symbole | sens | entree | SL | TP | 1er touche | R | $ simule |
|---|---|---|---|---|---|---|---|---|---|
| 09:31:59 | LONDRES | BTCUSD# | BUY | 62871.05 | 61661.77 | 64684.97 | STILL_OPEN | -0.04 | -0.51 |
| 09:47:41 | LONDRES | BTCUSD# | BUY | 62838.80 | 61690.25 | 64561.63 | STILL_OPEN | -0.02 | -0.19 |
| 10:42:49 | LONDRES | BTCUSD# | SELL | 62869.80 | 62932.67 | 62775.50 | TP_HIT | 1.50 | 0.94 |
| 11:02:06 | LONDRES | BTCUSD# | BUY | 62822.00 | 62759.18 | 62947.64 | SL_HIT | -1.00 | -0.63 |
| 11:24:56 | LONDRES | BTCUSD# | SELL | 62729.00 | 62791.67 | 62635.00 | TP_HIT | 1.50 | 0.94 |
| 11:47:58 | LONDRES | BTCUSD# | BUY | 62667.80 | 61632.50 | 64220.75 | STILL_OPEN | 0.15 | 1.52 |
| 12:06:04 | OVERLAP | BTCUSD# | SELL | 62712.00 | 62774.67 | 62618.00 | SL_HIT | -1.00 | -0.63 |
| 12:16:36 | OVERLAP | BTCUSD# | SELL | 62757.80 | 62820.56 | 62663.66 | SL_HIT | -1.00 | -0.63 |
| 12:17:07 | OVERLAP | GOLD# | SELL | 4110.98 | 4115.09 | 4104.81 | SL_HIT | -1.00 | -4.11 |
| 12:29:55 | OVERLAP | BTCUSD# | BUY | 62797.70 | 61603.41 | 64589.13 | STILL_OPEN | 0.02 | 0.22 |
| 12:42:56 | OVERLAP | GOLD# | SELL | 4114.82 | 4118.93 | 4108.65 | TP_HIT | 1.50 | 6.17 |
| 13:20:40 | OVERLAP | BTCUSD# | SELL | 62797.95 | 62860.68 | 62703.86 | TP_HIT | 1.50 | 0.94 |
| 14:00:09 | OVERLAP | BTCUSD# | BUY | 63031.50 | 61574.22 | 65217.42 | STILL_OPEN | -0.14 | -2.11 |
| 14:04:43 | OVERLAP | GOLD# | BUY | 4132.46 | 4048.73 | 4258.05 | STILL_OPEN | -0.03 | -2.37 |
| 14:15:52 | OVERLAP | BTCUSD# | SELL | 62815.10 | 62877.92 | 62720.88 | TP_HIT | 1.50 | 0.94 |
| 14:38:17 | OVERLAP | BTCUSD# | BUY | 62825.20 | 61574.43 | 64701.36 | STILL_OPEN | -0.00 | -0.05 |
| 14:51:49 | OVERLAP | BTCUSD# | SELL | 62893.90 | 62956.79 | 62799.56 | SL_HIT | -1.00 | -0.63 |
| 14:57:46 | OVERLAP | BTCUSD# | BUY | 62957.50 | 61603.25 | 64988.87 | STILL_OPEN | -0.10 | -1.37 |
| 15:20:18 | OVERLAP | BTCUSD# | SELL | 63035.15 | 63098.16 | 62940.64 | SL_HIT | -1.00 | -0.63 |
| 15:51:46 | OVERLAP | BTCUSD# | SELL | 62971.30 | 63034.27 | 62876.84 | TP_HIT | 1.50 | 0.94 |
| 16:16:24 | NEW_YORK | GOLD# | BUY | 4129.37 | 4045.59 | 4255.03 | STILL_OPEN | 0.01 | 0.72 |

## Agregats par session

| session | N | win rate | P&L simule $ |
|---|---|---|---|
| LONDRES | 6 | 50.0% | 2.07 |
| OVERLAP | 14 | 35.7% | -3.32 |
| NEW_YORK | 1 | 100.0% | 0.72 |

## Comparaison reel vs contrefactuel

- **Reel** (avec kill-switch, tel que le jour broker s'est deroule jusqu'a 2026-07-09T16:19:02.431588+00:00 UTC -- derniere lecture daily_killswitch.daily_pnl disponible, le jour broker n'est pas necessairement termine) : **7.63 $**
- **P&L simule des opportunites bloquees** : **-0.53 $** (9G/12P, win rate 42.9%)
- **Contrefactuel** (reel + simule, si le kill-switch avait ete eteint apres 6 pertes) : **7.10 $**

**Meilleure opportunite simulee** : GOLD# SELL @ 12:42:56 UTC (OVERLAP), TP_HIT, 6.17 $.
**Pire opportunite simulee** : GOLD# SELL @ 12:17:07 UTC (OVERLAP), SL_HIT, -4.11 $.

## Caveats (obligatoires)

- **C'est une SIMULATION sur bougies historiques, pas la realite.** Ces trades n'ont jamais ete ouverts.
- Utilise le SL/TP **du signal au moment du blocage** -- n'imite PAS le trailing dynamique d'Exit V2 (le vrai resultat aurait pu differer, dans un sens comme dans l'autre, si le trade avait vraiment ete pris et gere par Exit V2).
- Suppose un remplissage au prix du signal, avec un spread **estime** (aucune valeur de spread brute n'est loguee sur les lignes refusees) : GOLD# ~0.30$, BTCUSD# ~25.00$ (valeurs conservatrices, coherentes avec les spreads live observes cette session) -- et que 0.01 lot ne bouge pas le marche.
- **Un seul jour de donnees ne prouve rien sur le futur.** C'est un indice mesure, pas une loi.
- **9/21 opportunites sont encore "STILL_OPEN"** au moment ou ce rapport a ete genere (le jour broker n'etait pas termine) -- valorisees au prix M1 le plus recent disponible, PAS a une cloture definitive. Leur contribution au P&L total bouge avec le marche a chaque nouvelle execution de ce script tant que le jour n'est pas clos -- confirme empiriquement : deux executions a quelques minutes d'intervalle pendant la construction de ce rapport ont donne des totaux differents. Le resultat marginal ci-dessous en tient compte, mais re-executer ce script apres 21:00 UTC (jour broker clos) donnerait un chiffre definitif plutot qu'une estimation en mouvement.

## Verdict

Resultat **marginal, ni nettement vert ni nettement rouge** (-0.53 $ simules sur 21 opportunites, win rate 42.9% -- quasiment un tirage a pile ou face, et un seul gros trade (le meilleur ou le pire, 4.11 $) suffirait a inverser le signe). **Aucun verdict tranche sur cette seule journee** : ni l'intuition de SIMO ni le statu quo ne sont mesurablement valides ou invalides ici. Rejouer sur plusieurs jours additionnels de kill-switch declenche avant de trancher.