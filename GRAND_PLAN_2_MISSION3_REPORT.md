# GRAND_PLAN_2 — Mission 3 : Exit V2 à l'échelle BTC

**Décision** : SIMO validé GO (mission text). Appliqué.

**Calibration** : les seuils Exit V2 pour BTCUSD# passent en % du prix d'entrée, calibrés pour reproduire EXACTEMENT la protection relative de GOLD ($2/$4000=0.05%) : `btc_be_arm_pct=0.05`, `btc_be_floor_pct=0.0025`, `btc_trail_start_pct=0.05`, `btc_trail_gap_pct=0.03`. Converti en $ effectif via les tick specs propres à chaque position (`trade_tick_value`/`trade_tick_size`/`volume`), calculé une fois à l'ouverture (prix d'entrée fixe, jamais le prix courant). GOLD reste à 100% sur ses seuils $ fixes — vérifié par test qu'aucune configuration BTC ne peut jamais les affecter.

**Preuve live** (bot redémarré, 2 positions déjà ouvertes, SL/TP broker intacts pendant le redémarrage) :
```
BTCUSD# threshold_scale=PCT be_arm_usd_effective=0.3089 (entrée 61778.0 — soit 6.5x plus facile à armer que l'ancien $2 fixe)
GOLD#   threshold_scale=USD be_arm_usd_effective=2.0 (inchangé)
```
Confirmé identique côté `/api/status` du dashboard (bug trouvé et corrigé au passage : le snapshot affichait encore les valeurs `cfg.*` fixes au lieu des valeurs effectives par position pour `be_floor_usd`/`trail_gap_usd`).

**Dataset** : les champs `threshold_scale`/`be_arm_usd_effective`/`trail_start_usd_effective`/`be_floor_usd_effective`/`trail_gap_usd_effective` transitent automatiquement par le pipeline event→dataset existant (spread dans `action` → `_quick_exit_event`'s `raw_payload.quick_exit` → aplatissement générique de `decision_dataset.py`) — aucune modification de pipeline nécessaire, disponibles dès la prochaine clôture BTC réelle sous Exit V2.

**Tests** : 10 tests dédiés (équivalence relative GOLD/BTC vérifiée au pourcentage près, fallback sans `symbol_info`, GOLD inchangé même avec config BTC absurde, contenu du log). Suite complète : 3366 passed, 0 régression.
