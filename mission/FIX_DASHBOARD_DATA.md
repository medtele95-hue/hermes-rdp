ultrathink

MISSION : DASHBOARD — VRAIES DONNÉES + JOURNAL AVEC DATE ET HEURE (suite du GRAND_PLAN_2 Mission 2)

Constat SIMO : le dashboard (control room, Tailscale https://6a4c09c70e4590e.tail35b030.ts.net) affiche encore daily_pnl=-37 (l'ancienne valeur buguée) alors que la Mission 1 a corrigé le calcul dans le bot (vrai P&L = +13.70, losses=3/6). Le journal affiche des infos fausses/périmées. Et SIMO veut DATE + HEURE sur chaque trade du journal.

Safepoint git avant. Tests après. Push GitHub à la fin.

═══════════════════════════════════════
PROBLÈME 1 — LE DASHBOARD LIT UNE MAUVAISE SOURCE / NE SE RAFRAÎCHIT PAS
═══════════════════════════════════════
- Le dashboard affiche -37 : il lit soit un cache périmé, soit une source différente de la vérité corrigée en Mission 1 (mt5_pnl_truth / to_mt5_query_bounds).
- FIX : le dashboard_api doit lire le P&L, les pertes et le kill-switch depuis EXACTEMENT la même source corrigée que le bot utilise maintenant (mt5_pnl_truth.py avec to_mt5_query_bounds de broker_time.py). Le daily_pnl affiché doit égaler celui du bot (+13.70 actuellement), losses=3/6, triggered=False.
- Le dashboard ne peut pas ouvrir sa propre connexion MT5 (2e connexion impossible) : il lit le cache que le bot expose (app/local_api/state) OU les deals via une lecture partagée non conflictuelle — trouve la méthode qui fonctionne et qui donne la VRAIE valeur en temps réel.
- Rafraîchissement : le cache doit se mettre à jour à chaque cycle du bot (max 30-60s de retard), pas figé au démarrage.
- Vérifie sur TOUTES les valeurs : équité, balance, mode DEMO, positions ouvertes + état Exit V2, EES sell/buy, DXY, régime, kill-switch (x/6 + vrai daily_pnl), net du jour. Aucune valeur ne doit être "—" ou fausse si le bot tourne.

═══════════════════════════════════════
PROBLÈME 2 — LE JOURNAL DE TRADES : DATE + HEURE + VRAIES DONNÉES
═══════════════════════════════════════
- Source de vérité : les deals fermés MT5 (history_deals_get) via la lecture partagée, réconciliés avec le dataset. PAS un compteur interne, PAS des valeurs périmées.
- Pour CHAQUE trade du journal, afficher :
  * DATE ET HEURE d'ouverture (format clair : 2026-07-08 14:32:15, en heure broker ET/OU locale — indiquer laquelle)
  * DATE ET HEURE de fermeture
  * Durée de tenue
  * Symbole (GOLD# / BTCUSD#)
  * Direction (BUY/SELL)
  * Prix entrée / prix sortie
  * P&L réel net (profit+swap+commission), coloré vert/rouge
  * Mode de fermeture (Exit V2 trailing / SL / TP / autre)
  * Stratégie émettrice
- Tri : plus récent en haut par défaut.
- Filtres : Tous / Gagnants / Perdants / GOLD / BTC.
- Le total net affiché en haut du journal doit correspondre exactement au P&L réel réconcilié (cohérent avec le +13.70 du jour).
- IMPORTANT sur les dates : utilise to_mt5_query_bounds / broker_time.py pour que les timestamps soient corrects (le bug timezone de la Mission 1 affectait aussi potentiellement l'affichage des dates — vérifie que chaque trade est daté du bon jour).

═══════════════════════════════════════
VÉRIFICATION
═══════════════════════════════════════
- Redémarre le dashboard (ou recharge son cache).
- Ouvre la page via Tailscale et confirme : daily_pnl = +13.70 (plus -37), journal avec date+heure correctes sur chaque trade, total du journal cohérent, filtres fonctionnels.
- Confirme que l'URL Tailscale fonctionne toujours pour l'iPhone de SIMO (tailscale serve actif).

LIVRABLE : 5 lignes — la source unifiée du dashboard, confirmation que daily_pnl affiche +13.70, le journal avec date/heure prouvé sur un exemple réel, et que l'accès iPhone Tailscale fonctionne.
