# FIX_DASHBOARD_DATA_REPORT — 2026-07-08

**Source unifiée** : `/api/today` et `/api/journal` lisent désormais depuis `_mt5_journal_entries()` (nouveau, `app/dashboard_api/data.py`) — les deals MT5 fermés (`history_deals_get` via `to_mt5_query_bounds`, le fix de la mission P&L) restent la source de vérité, enrichis par `decision_dataset.jsonl` réconcilié (close_mode, stratégie) quand une correspondance existe, sinon fallback sur le commentaire du deal MT5 lui-même.

**daily_pnl confirmé +16.43** (le montant a évolué depuis le +13.70 de référence — de nouveaux trades ont clôturé entretemps) : `/api/today`, `/api/journal` et le `daily_pnl` du bot en direct affichent exactement la même valeur, cross-check `PNL_CROSS_CHECK OK divergence=0.0000` confirmé dans les logs. Le -37 n'était pas un nouveau bug : le process dashboard tournait avec l'ancien code, jamais redémarré depuis le fix — redémarré, vérifié en direct.

**Journal daté et prouvé** : exemple réel — `GOLD# SELL, ouvert 08/07 14:10 → fermé 08/07 14:17 (broker), durée 7min, entrée 4061.51 → sortie 4058.78, +2.73 $, TP_HIT`. Bug trouvé et corrigé au passage : les dates du journal MT5 subissaient le même décalage +3h que le kill-switch (nouvelle fonction inverse `from_mt5_deal_time()`), et le journal ne montrait que 10 trades sur 19 réels (les lignes `outcome` du dataset sont diluées par le paper-trading) — désormais complet par construction (MT5 = source de vérité).

**Accès iPhone Tailscale confirmé fonctionnel** : `https://6a4c09c70e4590e.tail35b030.ts.net/api/status` → 200, testé après redémarrage du dashboard.
