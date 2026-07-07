ultrathink

URGENT : BTC TRADE ALORS QU'IL DOIT ÊTRE HARD-DISABLED — ARRÊT IMMÉDIAT + AUTOPSIE

Constat SIMO : DEUX trades BTCUSD# aujourd'hui (−0.85 puis −1.12, lot 0.01) alors que l'invariant historique absolu est GOLD-ONLY, BTC hard-disabled sur tout chemin d'exécution. La reconstruction a perdu cet invariant.

Safepoint git avant. Tests après.

1) AUTOPSIE D'ABORD : dans les deals MT5 (history_deals_get), trouve les 2 trades BTCUSD du jour — magic ? commentaire ? Si magic=909002 → chemin de code HERMES : trace EXACTEMENT quel module/stratégie a émis et pourquoi l'allowlist ne l'a pas arrêté. Si magic différent de 909002 → source externe (le documenter, mais implémenter le verrou quand même).

2) VERROU ABSOLU — ALLOWLIST AU CHOKE-POINT : dans le routeur, au point unique order_send : if symbol not in SYMBOL_ALLOWLIST (=["GOLD#"]) → REJET + log [SYMBOL_BLOCKED] symbol=.. strategy=... AUCUN chemin de code ne peut envoyer un ordre sur un symbole hors allowlist — pas de flag contournable, une constante de config explicite.

3) DÉSACTIVER EN AMONT aussi : toute stratégie/module qui analyse ou candidate sur BTCUSD# → soit retirée du cycle (économie CPU), soit ses candidats marqués SHADOW-only. Le BTC ne doit plus JAMAIS atteindre le routeur.

4) S'il y a une position BTC encore OUVERTE : la fermer proprement via le routeur, log [BTC_FORCE_CLOSE reason=INVARIANT_RESTORED], pnl au dataset.

5) Test d'invariant permanent : candidat BTCUSD parfait simulé → [SYMBOL_BLOCKED], jamais d'order_send. + vérifier qu'AUCUN autre symbole non-GOLD ne peut passer (EURUSD, US100...).

6) MINI-AUDIT DES AUTRES INVARIANTS HISTORIQUES (pendant que tu y es — la reconstruction peut en avoir perdu d'autres) : vérifie la présence effective, avec preuve par test, de : (a) MAX_OPEN_TRADES_PER_SYMBOL=1 — aucun nouvel ordre GOLD tant qu'une position GOLD vit ; (b) lot figé 0.01 (LOT_HARD_CAP) ; (c) magic 909002 sur tous les ordres ; (d) SL/TP obligatoires sur tout ordre (jamais d'ordre nu). Tout invariant manquant → le restaurer immédiatement, même méthodologie.

LIVRABLE : qui a ouvert les 2 trades BTC (magic/chemin exact), le verrou en place et testé, résultat du mini-audit des 4 autres invariants (présents ou restaurés), confirmation GOLD-only absolu restauré.
