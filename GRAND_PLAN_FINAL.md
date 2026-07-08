# GRAND_PLAN_FINAL — clôture des 5 missions (2026-07-08)

## État de chaque mission

| Mission | Statut | Livrable |
|---|---|---|
| 1 — FIX_BTC (allowlist GOLD#+BTCUSD#) | **Livrée** | `FIX_BTC_REPORT.md`, tag `mission1-fix-btc-allowlist` — allowlist verrouillée, Exit V2 autorité unique, HERMES_LOG_FILE en dur, autopsies (dont ticket 375071163) |
| 2 — MATH_CORE_AUDIT | **Livrée** | `MATH_CORE_AUDIT.md`, tag `mission2-math-core-audit` — 21 stratégies auditées, 1 bug CASSÉ trouvé sur trade réel et corrigé (RR invalidé par un snap FVG dans ORDER_FLOW_EXECUTION_AGENT), ATR non-Wilder documenté (RÉSERVÉ SIMO) |
| 3 — WATCHDOG | **Livrée** | `WATCHDOG_REPORT.md`, tag `mission3-watchdog` — 9 vérifications, tâche planifiée active, première ronde réelle RAS |
| 4 — AUTOMATION | **Livrée** | `AUTOMATION_REPORT.md`, tag `mission4-automation` — 4 scripts + 3 tâches planifiées, premier bilan réel généré |
| 5 — AUTOPILOT | **Livrée (portée réduite sur COUCHE 2, décision SIMO explicite)** | `AUTOPILOT_REPORT.md`, tag `mission5-autopilot` — superviseur de process + auto-médecin **diagnostic seul** (pas d'agent auto-committant, décision demandée et tranchée par SIMO en session) |

Aucune mission bloquée. Un fichier de mission manquant n'a pas eu lieu (les 5 étaient
présents). Un rouge de test n'a eu lieu à aucun bloc (chaque commit = suite complète
verte avant de continuer).

## Verdict de l'audit mathématique (5 lignes)

Le cœur n'est pas encore mathématiquement digne de confiance en l'état : sur 21
stratégies, une seule construction est rigoureuse de bout en bout, et la stratégie
réellement active en argent (ORDER_FLOW_EXECUTION_AGENT) contenait un bug confirmé sur
un trade perdant réel — corrigé cette mission. La confluence finale mélange trois
systèmes de score distincts, dont un terme géométrique non normalisé qui peut, seul,
faire passer ou échouer un trade indépendamment de toute confirmation SMC/MTFA/order-flow.
L'ATR — brique de base de tout le dimensionnement de risque — est une moyenne mobile
simple, pas le Wilder RMA standard, sans que rien ne le signale (RÉSERVÉ SIMO, non
corrigé : changement systémique qui déplacerait tous les SL/TP). Le chemin vers un
cœur 100% calculé est concret et documenté dans `MATH_CORE_AUDIT.md` §6.

## Invariants actifs

- `SYMBOL_ALLOWLIST = (GOLD#, BTCUSD#)` — tout autre symbole → `[SYMBOL_BLOCKED]`.
- Lot fixe 0.01, magic 909002, SL/TP obligatoires, MAX_OPEN=1 **par symbole**.
- Kill-switch partagé (6 pertes/jour, DD 3%) — déclenché aujourd'hui en fonctionnement
  normal (voir `auto_medic_2026-07-08T064228.md` problème #3).
- Exit V2 = autorité de sortie unique GOLD#+BTCUSD# (QUICK_EXIT/Smart Rescue neutralisés).
- `HERMES_LOG_FILE` permanent, désormais avec `SafeRotatingFileHandler` (mission5 —
  un rollover en échec ne rend plus le logger silencieux).
- Watchdog indépendant actif (9 vérifications, mode urgence OFF par défaut).
- Superviseur de process actif (bot + gardien du watchdog, anti-boucle 3/heure).
- Auto-médecin diagnostic seul actif (07:00/19:00), aucune capacité d'écriture.

## Actions RÉSERVÉ SIMO consolidées (toutes missions)

**Mathématique/stratégie (mission2)** :
1. ATR = moyenne mobile simple, pas Wilder — trancher renommage honnête vs migration + recalibration complète.
2. Incohérence d'échelle dans `FINAL_CONFLUENCE` (géométrie 0-100 domine SMC/MTFA plafonnés à ±15).
3. Formule composite `geometric_confluence.py` sous-récompense un pattern harmonique valide (plafond B même textbook) — proposition chiffrée en attente avant toute activation `ACTIVE`.
4. Cibles RR déconnectées des seuils de gate (CRT_TBS/AMD_FVG/FIB_OTE : cible 1.8R vs porte 1.5R).
5. ~10 stratégies à score binaire (EMA_PULLBACK, BREAKOUT_RETEST, etc.) — prérequis du futur moteur EV.
6. `hermes_confluence_strategy_aware=False` par défaut neutralise les seuils 58/62/65 par classe.
7. SL non plafonnés par ATR sur 3 stratégies (risque de stop disproportionné).
8. `GOLD_RANGE_BREAKOUT` — seule stratégie avec un vrai retest, jamais câblée (`gold_range_breakout_enabled` absent de `Settings`).

**Infrastructure (missions 3-5)** :
9. Watchdog `WATCHDOG_EMERGENCY_CLOSE` — activer ou non le mode urgence (fermeture auto d'une position sur symbole interdit).
10. Auto-médecin : rester diagnostic-seul en permanence, ou reconsidérer une portée élargie plus tard avec des garde-fous **codés** (pas seulement des instructions au prompt) si un besoin réel émerge.

## Actions humaines restantes (2-3, comme demandé)

1. **Remote git privé** — toujours absent. Tant que ce n'est pas fait, le bilan
   quotidien et `HERMES_STATE.md` continuent d'alerter chaque jour (voir `docs/BACKUP_SIMO.md`).
2. **Telegram** — `watchdog/.env` n'a toujours pas de token/chat_id. Tout (watchdog,
   bilan, auto-médecin, superviseur) reste fail-safe silencieux jusque-là ; rien n'est
   perdu (tout vit dans les fichiers `.md`/`.log`), mais SIMO ne reçoit rien sur son
   téléphone tant que ce n'est pas configuré.
3. **Décisions RÉSERVÉ SIMO ci-dessus** — en particulier l'ATR et la formule de
   confluence (impact direct sur chaque trade), à trancher avant toute recalibration
   du moteur de décision.

## Clôture technique

Bot redémarré proprement : process `python -m app.main` actif (PID courant, log
`logs/hermes.log` en écriture continue confirmée), watchdog actif, superviseur actif,
6 tâches planifiées installées et vérifiées (`HERMES_WATCHDOG`, `HERMES_BOT_SUPERVISOR`,
`HERMES_DAILY_BACKUP`, `HERMES_DAILY_REPORT`, `HERMES_WEEKLY_SNAPSHOT`, `HERMES_AUTO_MEDIC`).
Telegram non configuré → ce rapport et tous les bilans attendent dans
`C:\hermes-reports\` (également mirorés sur `G:\Mon Drive\HERMES_backups\`).
