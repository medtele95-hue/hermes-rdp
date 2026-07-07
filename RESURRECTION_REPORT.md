# RESURRECTION_REPORT — HERMES sur nouveau serveur

Date : 2026-07-07 · Compte : demo XM 345297734 (XMGlobal-MT5 10, équité ~9 122 USD) · Machine : Windows 10 Pro (serveur neuf)

---

## 1. Voie empruntée + fragments post-26-juin

**VOIE B — reconstruction intégrale.** La copie du 26 juin n'a **aucun remote git et aucun tag** : les tags de la semaine perdue (`fix-mtf-structure-v1`, `exit-v2-activated-20260703`, `ks-policy-done`, `hui-done`) n'existaient nulle part. Restauration directe impossible.

Fragments trouvés dans la copie et **préservés** (commit `3771489`, baseline forensique) :
- Travail non commité du 23-26 juin : `app/config.py` (+`eurusd_broker_symbol`) et `.env` — commités tels quels.
- Données runtime jusqu'au **26 juin 18:21** (`demo_pilot_events.jsonl` + ~100 `.bak` horaires). **Rien** entre le 30 juin et le 7 juillet.
- Environnement : serveur nu (ni Git ni Python) — Git 2.47.1 (MinGit, `C:\Tools\MinGit`) et Python 3.11.9 installés, dépendances + MT5 5.0.5735 opérationnels. Horloge : −0,63 s vs NTP (service w32time désactivé, activation = action admin manuelle pour SIMO). Symboles : `GOLD#` OK (sélectionné), `BTCUSD#` OK, **`NEM.N`/`B.N` absents chez ce serveur XM** (voir bloc 10d), `US100Cash#` OK.

## 2. Tableau bloc par bloc

| Bloc | Commit | Tag | Tests | Preuve de fonctionnement |
|---|---|---|---|---|
| 0 Baseline forensique | `3771489` | `safepoint-pre-bloc-1` | suite réparée → verte (2 996) | tests aux chemins `C:/hermes-mt5-agent` codés en dur réparés |
| 1 Détecteurs (MTF dir, SMC 2/3, ATR) | `b4b9294` | `fix-mtf-structure-v1` | +18 | boot : `[MTF_DIR] h4_bias=BEARISH direction=SELL` — 15 émissions, plus jamais 100 % WAIT |
| 2 Géométrie SLTP + anti-penny-grab | `f58e0fc` | `bloc2-sltp-geometry` | +12 | boot : `[OF_SLTP] verdict=VALID … rr=1.5` ; max_tp mort (`max_money_tp_enabled=false`, GOLD hard-exclu) |
| 3 ADAPTIVE_ACCOUNT_POLICY | `522884a` | `bloc3-adaptive-policy` | +18 | boot : `[ADAPTIVE_POLICY] level=DEMO`, `[ORDER_AUTH] ALLOW`, `[ACCOUNT_PROFILE]` 4 symboles TRADABLE, zéro hardcode |
| 4 Exit V2 + mort du parasite | `9a488fd` | `exit-v2-activated` | +14 | boot : `[EXIT_V2] … mode=ACTIVE` gérant le ticket 374512428 ; CLOSE-BASED (aucun chemin SL-modify, prouvé par test source) |
| 5 EES BUY+SELL | `6325f62` | `bloc5-ees-active` | +13 | boot : `[EES] side=SELL score=60.0 band=PRUDENCE penalty=-12.0` ; symétrie BUY/SELL prouvée sur séries miroir |
| 6 Kill-switch blindé | `485745b` | `ks-policy-done` | +12 | boot : `[DAILY_KILLSWITCH] losses=0/6 window=[2026-07-06T21:00Z → now+2h]` — fenêtre broker, relecture deals, quotas par politique |
| 7 Calendrier protégé | `7ef5a20` | `bloc7-calendar` | +16 | boot : `[NEWS_CALENDAR] events=75 high_usd=2 next=2026-07-08T18:00Z` ; weekend/blackout/pré-close news testés |
| 8 Exécution au tick | `28be4cb` | `bloc8-exec-tick` | +6 | ordre réel : `[EXEC_QUALITY] slippage_vs_tick=0.0 spread_at_send=30 fill_latency_ms=133.74`, deviation=50 |
| 9 DECISION_DATASET | `b2042b7` | `bloc9-dataset` | +15 | 7 lignes écrites pendant le boot, **319 features/ligne**, momentum_alignment shadow, outcome tracker MFE/MAE |
| 10 SUPER-EYES | `1122d8b` | `bloc10-super-eyes` | +13 | boot : `[MARKET_EYES] cvd=True dxy=True levels=True canary=False` (~190-230 ms ; 1er appel 773 ms, loggé vs budget) |
| 11 Hygiène + résilience | `cb9ccb4` | `bloc11-hygiene`, `hui-done` | +9 | verrou `[SINGLE_INSTANCE] lock_acquired`, anti-boucle persistant, Lovable purgé, backup testé |

Méthodologie tenue : safepoint (tag) avant chaque bloc, **suite intégrale verte après chaque bloc** — état final : **3 121 passed, 0 failed** (+146 tests créés). Aucun rollback nécessaire.

## 3. Verdict bridge + backup

**Bridge : résidu, ne pas réinstaller.** `HermesBridge` (.mq5/.ex5) et `/api/mt5/poll-commands` n'existent **nulle part** : ni dans le repo, ni dans `C:\Program Files\MetaTrader 5\MQL5\Experts`, ni dans `%APPDATA%\MetaQuotes` (vérifié 2026-07-07). C'était le monitoring de l'ancien serveur. Le monitoring local vit sur `http://127.0.0.1:8000`. Documenté dans `docs/BACKUP_SIMO.md`.

**Backup : en place et testé.**
- `scripts/backup_daily.ps1` : copie datée `backups/AAAA-MM-JJ/` (data JSONL + .env + reports, rétention 30 j) + `git push --all --tags` vers `origin` quand configuré. **Testé : 32 fichiers copiés dans `backups/2026-07-07/`.**
- Tâche planifiée Windows `HERMES_DAILY_BACKUP` créée (quotidienne 21:30) et vérifiée « Prêt ».
- **Remote git privé : la seule étape restante, réservée à SIMO** (nécessite ses identifiants GitHub) — procédure 3 étapes dans `docs/BACKUP_SIMO.md`, plus la consigne hebdo de copie hors-machine vers Drive. Le script poussera automatiquement dès que `origin` existera.

## 4. Ce qui est définitivement perdu

- **Le dataset de la semaine 30 juin → 7 juillet** (décisions, outcomes, events) : aucune trace sur cette machine ; l'ancien serveur était le seul porteur. Perdu sauf si son disque est récupérable.
- L'historique git de la semaine perdue (commits/tags originaux) : reconstruit fonctionnellement, pas historiquement.
- Rien d'autre : le code du 26 juin, ses données et les fragments non commités sont intégralement préservés et versionnés.

## 5. Boot complet prouvé (2026-07-07, ~08:13 UTC)

```
[SINGLE_INSTANCE]  lock_acquired …hermes_instance_345297734_909002.lock
[ADAPTIVE_POLICY]  level=DEMO reason=TRADE_MODE_DEMO exploration_executable=True max_losses_per_day=6
[ACCOUNT_PROFILE]  symbol=BTCUSD# tradable=True volume_min=0.01 … (GOLD#, EURUSD, US100Cash# idem — 4/4 TRADABLE)
[NEWS_CALENDAR]    loaded events=75 high_usd=2 next_high_usd=2026-07-08T18:00:00+00:00
[MTF_DIR]          symbol=BTCUSD h4_bias=BEARISH h4_zone=NONE direction=SELL   (15 émissions — fini le 100 % WAIT)
[EES]              side=SELL score=60.0 band=PRUDENCE penalty=-12.0            (14 évaluations BUY et SELL)
[MARKET_EYES]      cvd=True dxy=True levels=True canary=False elapsed_ms≈190-230
[DAILY_KILLSWITCH] triggered=False losses=0/6 window=[2026-07-06T21:00Z → now+2h] policy=DEMO
[ORDER_AUTH]       decision=ALLOW reason=ORDER_AUTH_DEMO_OK level=DEMO
[OF_SLTP]          verdict=VALID direction=BUY entry=4138.52 sl=4125.91 tp=4157.43 rr=1.5
[EXEC_QUALITY]     slippage_vs_tick=0.0 spread_at_send=30 fill_latency_ms=133.74
[DEMO_ORDER]       status=ORDER_CONFIRMED ticket=374512428 GOLD# BUY 0.01
[EXIT_V2]          ticket=374512428 action=NONE reason=EXIT_V2_HOLD mode=ACTIVE account=DEMO
[CYCLE_SUMMARY]    analyzed=4 demo_orders=1 open=1  (4 cycles complets suivis)
dataset            7 lignes écrites, 319 features/ligne
```
Le cycle complet a été suivi jusqu'au bout : détection → scores → autorisation → ordre au tick → gestion Exit V2 → dataset. La position de vérification a ensuite été refermée manuellement (−3,19 $ demo) pour laisser le compte flat. Suite intégrale : **3 121 verts**.

Notes de vigilance : (1) `[MARKET_EYES]` flotte autour du budget 200 ms (~10 ms attendus était optimiste — la lecture de 20 000 ticks domine) ; sans effet décisionnel, dépassements loggés. (2) Un boot sur deux a essuyé un `IPC send failed` transitoire de MT5 à `symbols_get` — le second boot est passé ; à surveiller, un retry au boot serait un durcissement futur. (3) Canari minières indisponible chez XM (symboles absents) — fail-soft documenté.

## 6. Bottom-line (GO/NO-GO)

1. Le système du 7 juillet au soir est **fonctionnellement reconstruit** : les 11 blocs validés en production sont réimplémentés, testés (3 121 verts) et prouvés au boot réel.
2. Les détecteurs voient (MTF_DIR émet des directions), la géométrie SLTP est saine (plancher RR ≥ 1.0 au choke-point), le penny-grab est mort.
3. Exit V2 est l'autorité unique sur GOLD, CLOSE-BASED, le parasite QUICK_EXIT ne touche plus GOLD.
4. EES garde les deux flancs (BUY et SELL, symétrie prouvée), le kill-switch est stateless sur fenêtre broker, le calendrier protège weekend et news.
5. Chaque décision part au dataset (319 features) avec outcome tracker — la collecte peut reprendre immédiatement.
6. Différence assumée vs l'ancien monde : politique adaptive à 3 niveaux au lieu du double-pin, Lovable purgé du sync, verrou anti-double-instance.
7. Il reste UNE action humaine : SIMO crée le remote git privé (3 commandes, `docs/BACKUP_SIMO.md`) — jusque-là le backup est local uniquement.
8. **GO pour reprendre la phase collecte** sur ce demo dès maintenant ; les sessions LDN/NY de demain sont couvertes (prochain HIGH USD : 2026-07-08 18:00 UTC, déjà dans le bouclier).
