# MISSION : RAPPORT ANALYTIQUE 24H — LECTURE SEULE, ZÉRO MODIFICATION

Objectif : produire un rapport **complet et lisible pour humain** de TOUS les setups
HERMES des dernières 24 heures (exécutés ET refusés), croisé avec la réalité MT5,
pour que SIMO et son analyste les passent en revue **ensemble**, un par un.

Ce n'est **PAS** une mission de fix. On regarde. On ne touche à rien.

═══════════════════════════════════════════════════════════════
CONTRAINTES ABSOLUES
═══════════════════════════════════════════════════════════════
1. **LECTURE SEULE.** Aucune modification du code HERMES. Aucun `order_send`.
   Aucun changement de seuil. Aucun commit de code. Aucun redémarrage du bot.
2. **Ne touche pas au bot en cours.** Il continue de tourner pendant l'analyse.
   Ne le stoppe pas, ne le relance pas, ne modifie aucune tâche planifiée.
3. Script d'analyse **isolé** : `tools/analyse_24h.py`, hors de tout chemin de décision.
   Il lit des fichiers et l'historique MT5, il n'écrit que le rapport.
4. GOLD# intouchable par principe (ici on ne fait de toute façon qu'analyser).

═══════════════════════════════════════════════════════════════
SOURCES DE VÉRITÉ
═══════════════════════════════════════════════════════════════
A. **hermes.log** (via `HERMES_LOG_FILE`) — toutes les décisions des dernières 24h :
   setups évalués, scores de confluence, EES, décision (exécuté/refusé) + raison.
B. **Historique deals MT5** (`magic == 909002`) — la vérité brute : deals ouverts/fermés,
   P&L RÉEL par ticket.

   ⚠️ **RÈGLE DE SIGNE OBLIGATOIRE** : le P&L de chaque trade se calcule **UNIQUEMENT
   depuis les deals MT5**, jamais depuis l'affichage HERMES. Rappel du bug déjà rencontré
   (ticket 828907072 : HERMES affichait +38.72 alors que MT5 réel = −104.48). Si un écart
   HERMES↔MT5 est détecté sur un ticket, le **signaler explicitement** dans le rapport
   (colonne « écart_affichage »), et retenir la valeur MT5 comme vérité.

Fenêtre : dernières **24 heures glissantes** (préciser la borne exacte UTC + heure broker
en tête de rapport).

═══════════════════════════════════════════════════════════════
POUR CHAQUE SETUP (exécuté ET refusé) — colonnes à extraire
═══════════════════════════════════════════════════════════════
- horodatage (UTC + heure broker UTC+3)
- symbole, sens (BUY / SELL)
- score de confluence + **décomposition Cœur V2** (geo 25 / SMC 25 / MTFA 20 / OF 30)
- grade (A / B / C / …)
- EES_sell + EES_buy au moment de la décision, avec leur bande (SAIN / PRUDENCE / EXTREME)
- session + `kill_zone_active`
- régime : ATR percentile, `spread_to_atr`
- lecture marché : `momentum_alignment` (vote 3 voix), DXY trend, miners canary (si dispo)
- **DÉCISION** :
  - si **REFUSÉ** → la raison exacte du log (ex. `CONFIRMATION_MATRIX_HARD_BLOCK`,
    `EES_EXTREME_BLOCK`, `MAX_OPEN_TRADES_PER_SYMBOL`, news blackout ±10min,
    abort tick (>300 pts), kill-switch, spread blowout, symbol non autorisé…)
  - si **EXÉCUTÉ** → ticket, prix entrée, prix sortie, **P&L réel (deals MT5)**,
    R multiple, comportement Exit V2 (breakeven armé oui/non ? trailing actif ?
    type de sortie : SL / TP / trailing / manuel)

═══════════════════════════════════════════════════════════════
AGRÉGATS (le tableau de bord du rapport)
═══════════════════════════════════════════════════════════════
- N setups évalués · N exécutés · N refusés
- refus **ventilés par raison** (compte + %)  ← on veut voir le blocage dominant
- win rate sur les exécutés (G / P)
- **P&L net RÉEL** (somme des deals MT5) — préciser « vérité MT5, pas affichage »
- P&L **par grade** (les A gagnent-ils vraiment plus que les B/C ?)
- P&L **par session** (Londres / NY / Asie)
- P&L **par symbole** (GOLD vs BTC, si BTC actif)
- P&L **par bande EES** — et une note : les setups refusés en `EES_EXTREME` auraient-ils
  perdu si on les avait pris ? (estimation directionnelle honnête, pas une promesse)
- le **meilleur** et le **pire** trade des 24h, avec leur contexte complet

═══════════════════════════════════════════════════════════════
LIVRABLES
═══════════════════════════════════════════════════════════════
1. **RAPPORT_24H.md** — LISIBLE, pas un dump brut. Structure imposée :
   ① Résumé exécutif (≤ 8 lignes) : période, N setups, exécutés/refusés, win rate,
      P&L réel, verdict en UNE phrase.
   ② Tableau markdown des **exécutés** : heure | sens | score | grade | EES | Exit V2 | P&L réel | R
   ③ Tableau markdown des **refus** : heure | sens | score | grade | EES | raison
      (pour voir ce qu'on a évité / raté)
   ④ Les agrégats ci-dessus (P&L par grade / session / EES / symbole).
   ⑤ Top 3 observations **factuelles** — ce que les chiffres disent, **sans**
      recommandation de changer le système, sans nouveau seuil. On décrit, on ne prescrit pas.

2. **RAPPORT_24H.csv** — le même en brut, une ligne par setup, toutes colonnes,
   pour analyse fine ensuite.

À la fin : **affiche le résumé exécutif (①) directement dans le terminal** et confirme
qu'aucun fichier de code n'a été modifié (`git status` propre côté code — seuls les
2 livrables dans un dossier reports/).

Rappel : ce rapport sert à **REGARDER**, pas à agir. Aucun changement de code, aucun seuil
touché. On lira les setups ensemble après.
