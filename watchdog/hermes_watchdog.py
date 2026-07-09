# -*- coding: utf-8 -*-
"""HERMES WATCHDOG — chien de garde indépendant (surveillance + alertes, JAMAIS de trading).

Process INDÉPENDANT du bot : il survit à un crash du bot et le surveille de
l'extérieur. Boucle toutes les 60 secondes. Connexion MT5 en LECTURE SEULE
(positions_get, history_deals_get, account_info) + lecture des fichiers du bot
(dataset, logs, backups). AUCUN order_send, AUCUNE modification de fichier du
bot — à UNE exception près : le MODE URGENCE (WATCHDOG_EMERGENCY_CLOSE=true
dans watchdog/.env) qui autorise UNIQUEMENT la clôture d'une position sur
symbole interdit (vérification 1), loggée [WATCHDOG_FORCE_CLOSE].

Les 10 vérifications (chaque cycle) :
 1. SYMBOLES      : position/deal du jour hors allowlist (GOLD#, BTCUSD#) -> CRITIQUE
 2. MAX_OPEN      : plus d'1 position HERMES simultanée PAR symbole        -> CRITIQUE
 3. IDENTITÉ      : position lot != 0.01 ou magic != 909002                -> CRITIQUE
 4. RISQUE NU     : position sans SL ou sans TP                            -> CRITIQUE
 5. PERTE FLOT.   : floating loss totale > 3% équité                       -> HAUTE
 6. HEARTBEAT BOT : dataset/log non modifié > 10 min en heures de marché   -> HAUTE
 7. KILL-SWITCH   : pertes du jour broker > quota ET nouveaux trades       -> CRITIQUE
 8. MACHINE       : disque < 5 GB ; backup du jour manquant après 10h      -> ALERTE
 9. DOUBLE INSTANCE : plus d'un process python du bot                      -> CRITIQUE
 10. DASHBOARD HB   : heartbeat dashboard périmé (process optionnel séparé) -> INFO

Heure broker explicite : les deals MT5 sont stampés heure broker (XM = UTC+3).
La fenêtre "jour broker" = [minuit broker, now+2h] — même convention armored
que le kill-switch du bot.

Anti-spam : une même alerte non résolue ne repart que toutes les 30 minutes
(état persisté dans watchdog/alert_state.json).

Fail-safe : Telegram indisponible -> l'alerte vit quand même dans
WATCHDOG_ALERTS.log et le watchdog continue. Toute exception d'un check est
capturée : le gardien ne meurt jamais d'un check cassé.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.utils.broker_time import to_mt5_query_bounds  # noqa: E402

WATCHDOG_DIR = Path(__file__).resolve().parent
REPO_ROOT = WATCHDOG_DIR.parent
ALERTS_LOG = WATCHDOG_DIR / "WATCHDOG_ALERTS.log"
HEARTBEAT_FILE = WATCHDOG_DIR / "heartbeat.txt"
ALERT_STATE_FILE = WATCHDOG_DIR / "alert_state.json"
ENV_FILE = WATCHDOG_DIR / ".env"

DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
EVENTS_FILE = REPO_ROOT / "app" / "data" / "demo_pilot_events.jsonl"
BOT_LOG_FILE = REPO_ROOT / "logs" / "hermes.log"
BACKUPS_DIR = REPO_ROOT / "backups"
DASHBOARD_HEARTBEAT_FILE = REPO_ROOT / "logs" / "dashboard_heartbeat.txt"

# ── Invariants surveillés (miroir du routeur ; import best-effort) ──────────
SYMBOL_ALLOWLIST = ("GOLD#", "BTCUSD#")
LOT_HARD_CAP = 0.01
MAGIC_HARD = 909002
try:  # source de vérité si importable — fail-safe sinon
    sys.path.insert(0, str(REPO_ROOT))
    from app.mt5.demo_router import LOT_HARD_CAP as _LOT, MAGIC_HARD as _MAGIC, SYMBOL_ALLOWLIST as _ALLOW  # noqa: E402
    SYMBOL_ALLOWLIST, LOT_HARD_CAP, MAGIC_HARD = tuple(_ALLOW), float(_LOT), int(_MAGIC)
except Exception:
    pass

BROKER_UTC_OFFSET_HOURS = 3.0   # XM : deals stampés UTC+3 (explicite, jamais deviné)
MAX_LOSSES_PER_DAY = 6          # politique DEMO du bot (daily_killswitch)
MAX_FLOATING_LOSS_PCT = 3.0
BOT_STALL_MINUTES = 10
DASHBOARD_STALL_MINUTES = 5  # mission/DASHBOARD.md — le dashboard bat toutes les 30s
MIN_FREE_DISK_GB = 5.0
CYCLE_SECONDS = 60
RESEND_MINUTES = 30

CRITICAL, HIGH, INFO = "CRITIQUE", "HAUTE", "INFO"


# ── Config watchdog/.env (parse manuel, zéro dépendance) ────────────────────
def load_env(path: Path = ENV_FILE) -> dict:
    cfg = {}
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    cfg[key.strip()] = value.strip()
    except OSError:
        pass
    return cfg


# ── Alerte : fichier (toujours) + console + Telegram (best-effort) ──────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class AlertManager:
    def __init__(self, cfg: dict, state_file: Path = ALERT_STATE_FILE):
        self.cfg = cfg
        self.state_file = state_file
        try:
            self.state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def _should_send(self, key: str) -> bool:
        last = self.state.get(key)
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (_now_utc() - last_dt) >= timedelta(minutes=RESEND_MINUTES)

    def _mark_sent(self, key: str) -> None:
        self.state[key] = _now_utc().isoformat()
        try:
            self.state_file.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        except OSError:
            pass

    def clear_resolved(self, active_keys: set) -> None:
        stale = [k for k in self.state if k not in active_keys]
        for k in stale:
            self.state.pop(k, None)
        if stale:
            try:
                self.state_file.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            except OSError:
                pass

    def alert(self, level: str, key: str, message: str) -> None:
        stamp = _now_utc().isoformat()
        line = f"{stamp} [{level}] {key} :: {message}"
        print(line, flush=True)
        try:
            with ALERTS_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"{stamp} [ERREUR] alerts_log inécrivable: {exc}", flush=True)
        if level in {CRITICAL, HIGH} and self._should_send(key):
            if self.send_telegram(f"🐕 WATCHDOG [{level}]\n{message}"):
                pass  # anti-spam marqué même si Telegram absent : le fichier fait foi
            self._mark_sent(key)
            # GRAND_PLAN_2 mission4 (2026-07-08, SIMO validé GO) : le
            # watchdog déclenche l'auto-médecin sur toute alerte CRITIQUE/
            # HAUTE — même fenêtre anti-spam que Telegram (30 min) pour ne
            # pas relancer une session claude -p en boucle sur un problème
            # non résolu ; se re-déclenche naturellement si le problème
            # persiste au-delà de cette fenêtre.
            self.trigger_auto_medic(f"{level}:{key}")

    def trigger_auto_medic(self, reason: str) -> None:  # pragma: no cover - thin OS call
        try:
            import subprocess
            subprocess.run(
                ["schtasks", "/run", "/tn", "HERMES_AUTO_MEDIC"],
                capture_output=True, timeout=15,
            )
            print(f"[WATCHDOG] auto_medic_triggered reason={reason}", flush=True)
        except Exception as exc:
            print(f"[WATCHDOG] auto_medic_trigger_failed reason={reason} error={str(exc)[:120]}", flush=True)

    def send_telegram(self, text: str) -> bool:
        token = self.cfg.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = self.cfg.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return False
        try:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as exc:  # fail-safe : jamais bloquant
            print(f"[WATCHDOG] telegram_indisponible: {str(exc)[:120]}", flush=True)
            return False


# ── Fenêtre jour broker (même convention armored que le kill-switch) ────────
def broker_day_window(now_utc: datetime) -> tuple[datetime, datetime]:
    offset = timedelta(hours=BROKER_UTC_OFFSET_HOURS)
    broker_now = now_utc + offset
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return broker_midnight - offset, now_utc + timedelta(hours=2)


def market_hours_now(now_utc: datetime) -> bool:
    """Heures de marché : GOLD ferme le week-end (vendredi 21:00 UTC -> dimanche 22:00 UTC),
    BTC cote 24/7 mais le bot est flat-weekend -> pas d'exigence de heartbeat le week-end."""
    wd = now_utc.weekday()
    if wd == 5:  # samedi
        return False
    if wd == 4 and now_utc.hour >= 21:
        return False
    if wd == 6 and now_utc.hour < 22:
        return False
    return True


# ── Les 9 vérifications. Chacune retourne [(level, key, message), ...] ──────
def check_1_symbols(positions, deals) -> list:
    alerts = []
    for p in positions or []:
        sym = str(getattr(p, "symbol", "") or "")
        if int(getattr(p, "magic", 0) or 0) == MAGIC_HARD and sym not in SYMBOL_ALLOWLIST:
            alerts.append((CRITICAL, f"SYMBOL_POSITION_{sym}",
                           f"Position OUVERTE sur symbole INTERDIT {sym} (ticket {getattr(p, 'ticket', '?')}, "
                           f"allowlist={list(SYMBOL_ALLOWLIST)})"))
    seen = set()
    for d in deals or []:
        sym = str(getattr(d, "symbol", "") or "")
        if (int(getattr(d, "magic", 0) or 0) == MAGIC_HARD and sym and sym not in SYMBOL_ALLOWLIST
                and sym not in seen):
            seen.add(sym)
            alerts.append((CRITICAL, f"SYMBOL_DEAL_{sym}",
                           f"Deal du jour (heure broker) sur symbole INTERDIT {sym} "
                           f"(ticket {getattr(d, 'ticket', '?')})"))
    return alerts


def check_2_max_open(positions) -> list:
    counts = {}
    for p in positions or []:
        if int(getattr(p, "magic", 0) or 0) == MAGIC_HARD:
            sym = str(getattr(p, "symbol", "") or "")
            counts[sym] = counts.get(sym, 0) + 1
    return [(CRITICAL, f"MAX_OPEN_{sym}", f"{n} positions HERMES simultanées sur {sym} (max 1 PAR symbole)")
            for sym, n in counts.items() if n > 1]


def check_3_identity(positions) -> list:
    alerts = []
    for p in positions or []:
        magic = int(getattr(p, "magic", 0) or 0)
        sym = str(getattr(p, "symbol", "") or "")
        comment = str(getattr(p, "comment", "") or "")
        vol = float(getattr(p, "volume", 0) or 0)
        ticket = getattr(p, "ticket", "?")
        if magic == MAGIC_HARD and abs(vol - LOT_HARD_CAP) > 1e-9:
            alerts.append((CRITICAL, f"LOT_{ticket}",
                           f"Position HERMES {ticket} ({sym}) avec lot {vol} != {LOT_HARD_CAP}"))
        if magic != MAGIC_HARD and "HERMES" in comment.upper():
            alerts.append((CRITICAL, f"MAGIC_{ticket}",
                           f"Position {ticket} ({sym}) comment HERMES mais magic {magic} != {MAGIC_HARD} "
                           f"— position inconnue sur le compte"))
    return alerts


def check_4_naked(positions) -> list:
    alerts = []
    for p in positions or []:
        if int(getattr(p, "magic", 0) or 0) != MAGIC_HARD:
            continue
        sl = float(getattr(p, "sl", 0) or 0)
        tp = float(getattr(p, "tp", 0) or 0)
        if sl <= 0 or tp <= 0:
            alerts.append((CRITICAL, f"NAKED_{getattr(p, 'ticket', '?')}",
                           f"Position HERMES {getattr(p, 'ticket', '?')} ({getattr(p, 'symbol', '?')}) "
                           f"SANS {'SL' if sl <= 0 else 'TP'} (sl={sl}, tp={tp})"))
    return alerts


def check_5_floating(positions, account) -> list:
    if not account:
        return []
    equity = float(getattr(account, "equity", 0) or 0)
    if equity <= 0:
        return []
    floating = sum(float(getattr(p, "profit", 0) or 0) for p in positions or []
                   if int(getattr(p, "magic", 0) or 0) == MAGIC_HARD)
    if floating < 0 and abs(floating) / equity * 100.0 > MAX_FLOATING_LOSS_PCT:
        return [(HIGH, "FLOATING_LOSS",
                 f"Perte flottante {floating:.2f} = {abs(floating)/equity*100:.2f}% de l'équité "
                 f"({equity:.2f}) > {MAX_FLOATING_LOSS_PCT}%")]
    return []


def check_6_bot_heartbeat(now_utc: datetime) -> list:
    if not market_hours_now(now_utc):
        return []
    freshest = None
    for f in (DATASET_FILE, EVENTS_FILE, BOT_LOG_FILE):
        try:
            if f.exists():
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                freshest = max(freshest, mtime) if freshest else mtime
        except OSError:
            continue
    if freshest is None:
        return [(HIGH, "BOT_FILES_MISSING", "Aucun fichier du bot lisible (dataset/events/log)")]
    age_min = (now_utc - freshest).total_seconds() / 60.0
    if age_min > BOT_STALL_MINUTES:
        return [(HIGH, "BOT_STALLED",
                 f"Bot possiblement mort : aucun fichier modifié depuis {age_min:.0f} min "
                 f"(> {BOT_STALL_MINUTES} min, heures de marché)")]
    return []


def check_10_dashboard_heartbeat(now_utc: datetime) -> list:
    """mission/DASHBOARD.md — le dashboard est un process séparé et
    OPTIONNEL : s'il n'a jamais démarré, ce n'est pas une erreur (pas
    d'alerte). S'il a démarré puis s'est arrêté sans passer par l'action
    dashboard stop (qui ne touche pas ce fichier), le heartbeat devient
    périmé — alerte INFO seulement, jamais HAUTE/CRITIQUE : l'absence du
    dashboard n'affecte jamais le trading (voir app/mt5/demo_router.py,
    aucun couplage)."""
    if not DASHBOARD_HEARTBEAT_FILE.exists():
        return []
    try:
        stamp = datetime.fromisoformat(DASHBOARD_HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        return []
    age_min = (now_utc - stamp).total_seconds() / 60.0
    if age_min > DASHBOARD_STALL_MINUTES:
        return [(INFO, "DASHBOARD_STALLED",
                 f"Dashboard possiblement arrêté : heartbeat périmé depuis {age_min:.0f} min "
                 f"(> {DASHBOARD_STALL_MINUTES} min) — le bot n'est pas affecté")]
    return []


def check_7_killswitch(deals, now_utc: datetime) -> list:
    losses = 0
    last_open_time = None
    trades = []
    for d in deals or []:
        if int(getattr(d, "magic", 0) or 0) != MAGIC_HARD:
            continue
        entry = int(getattr(d, "entry", -1) or 0)
        if entry == 1:  # DEAL_ENTRY_OUT
            net = (float(getattr(d, "profit", 0) or 0) + float(getattr(d, "commission", 0) or 0)
                   + float(getattr(d, "swap", 0) or 0))
            trades.append(net)
            if net < 0:
                losses += 1
        elif entry == 0:  # DEAL_ENTRY_IN
            t = int(getattr(d, "time", 0) or 0)
            last_open_time = max(last_open_time or 0, t)
    if losses <= MAX_LOSSES_PER_DAY:
        return []
    # quota dépassé : de nouveaux trades s'ouvrent-ils APRÈS le quota ?
    if last_open_time:
        return [(CRITICAL, "KILLSWITCH_PIERCED",
                 f"KILL-SWITCH PERCÉ : {losses} pertes jour broker (> quota {MAX_LOSSES_PER_DAY}) "
                 f"et des ouvertures existent — vérifier immédiatement")]
    return [(HIGH, "KILLSWITCH_QUOTA",
             f"{losses} pertes jour broker > quota {MAX_LOSSES_PER_DAY} (aucune ouverture récente vue)")]


def check_8_machine(now_utc: datetime) -> list:
    alerts = []
    try:
        free_gb = shutil.disk_usage(str(REPO_ROOT)).free / (1024 ** 3)
        if free_gb < MIN_FREE_DISK_GB:
            alerts.append((HIGH, "DISK_LOW", f"Disque : {free_gb:.1f} GB libres (< {MIN_FREE_DISK_GB} GB)"))
    except OSError:
        pass
    local_now = datetime.now()
    if local_now.hour >= 10:
        today_tag = local_now.strftime("%Y-%m-%d")
        try:
            has_today = BACKUPS_DIR.exists() and any(
                p.name.startswith(today_tag) for p in BACKUPS_DIR.iterdir() if p.is_dir()
            )
        except OSError:
            has_today = False
        if not has_today:
            alerts.append((HIGH, "BACKUP_MISSING", f"backups/ sans sous-dossier daté {today_tag} après 10h"))
    return alerts


def check_9_double_instance() -> list:
    """Compte les process python dont la ligne de commande exécute le bot (app/main.py ou app.main)."""
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        bot_pids = []
        for line in out.splitlines():
            low = line.lower()
            if ("app/main.py" in low or "app\\main.py" in low or "app.main" in low) and "watchdog" not in low:
                bot_pids.append(line.strip().split(",")[-1])
        if len(bot_pids) > 1:
            return [(CRITICAL, "DOUBLE_INSTANCE",
                     f"{len(bot_pids)} process python exécutent le bot (PIDs {bot_pids}) — risque d'ordres doublés")]
    except Exception:
        pass
    return []


# ── MODE URGENCE : une seule action autorisée, désactivée par défaut ────────
def emergency_close_forbidden(mt5_module, positions, alerts_mgr: AlertManager, cfg: dict) -> None:
    """SI (et seulement si) WATCHDOG_EMERGENCY_CLOSE=true : ferme les positions
    HERMES sur symbole hors allowlist. Tout le reste : alerte seulement."""
    if str(cfg.get("WATCHDOG_EMERGENCY_CLOSE", "false")).lower() != "true":
        return
    for p in positions or []:
        sym = str(getattr(p, "symbol", "") or "")
        if int(getattr(p, "magic", 0) or 0) != MAGIC_HARD or sym in SYMBOL_ALLOWLIST:
            continue
        ticket = int(getattr(p, "ticket", 0) or 0)
        tick = mt5_module.symbol_info_tick(sym)
        if not tick:
            continue
        is_buy = int(getattr(p, "type", 0) or 0) == 0
        request = {
            "action": getattr(mt5_module, "TRADE_ACTION_DEAL", 1),
            "symbol": sym,
            "volume": float(getattr(p, "volume", 0) or 0),
            "type": getattr(mt5_module, "ORDER_TYPE_SELL", 1) if is_buy else getattr(mt5_module, "ORDER_TYPE_BUY", 0),
            "position": ticket,
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": 50,
            "magic": MAGIC_HARD,
            "comment": "WATCHDOG_FORCE_CLOSE",
        }
        result = mt5_module.order_send(request)
        retcode = getattr(result, "retcode", None)
        alerts_mgr.alert(CRITICAL, f"FORCE_CLOSE_{ticket}",
                         f"[WATCHDOG_FORCE_CLOSE] ticket={ticket} symbol={sym} retcode={retcode} "
                         f"profit={getattr(p, 'profit', '?')}")


# ── Cycle unique (injectable pour les tests) ────────────────────────────────
def run_cycle(mt5_module, alerts_mgr: AlertManager, cfg: dict, now_utc: datetime | None = None) -> list:
    now_utc = now_utc or _now_utc()
    all_alerts = []
    positions, deals, account = [], [], None
    try:
        positions = list(mt5_module.positions_get() or [])
        start, end = broker_day_window(now_utc)
        q_start, q_end = to_mt5_query_bounds(start, end, BROKER_UTC_OFFSET_HOURS)
        deals = list(mt5_module.history_deals_get(q_start, q_end) or [])
        account = mt5_module.account_info()
    except Exception as exc:
        all_alerts.append((HIGH, "MT5_UNREADABLE", f"MT5 illisible : {str(exc)[:150]}"))

    checks = (
        lambda: check_1_symbols(positions, deals),
        lambda: check_2_max_open(positions),
        lambda: check_3_identity(positions),
        lambda: check_4_naked(positions),
        lambda: check_5_floating(positions, account),
        lambda: check_6_bot_heartbeat(now_utc),
        lambda: check_7_killswitch(deals, now_utc),
        lambda: check_8_machine(now_utc),
        lambda: check_9_double_instance(),
        lambda: check_10_dashboard_heartbeat(now_utc),
    )
    for check in checks:
        try:
            all_alerts.extend(check() or [])
        except Exception as exc:  # un check cassé ne tue jamais le gardien
            all_alerts.append((INFO, "CHECK_ERROR", f"check en erreur : {str(exc)[:150]}"))

    for level, key, message in all_alerts:
        alerts_mgr.alert(level, key, message)
    alerts_mgr.clear_resolved({key for _, key, _ in all_alerts})

    # mode urgence (défaut : OFF)
    if any(key.startswith("SYMBOL_POSITION_") for _, key, _ in all_alerts):
        try:
            emergency_close_forbidden(mt5_module, positions, alerts_mgr, cfg)
        except Exception as exc:
            alerts_mgr.alert(HIGH, "EMERGENCY_ERROR", f"mode urgence en erreur : {str(exc)[:150]}")

    # heartbeat du gardien lui-même
    try:
        HEARTBEAT_FILE.write_text(now_utc.isoformat(), encoding="utf-8")
    except OSError:
        pass
    return all_alerts


def main() -> None:
    once = "--once" in sys.argv
    cfg = load_env()
    alerts_mgr = AlertManager(cfg)
    print(f"[WATCHDOG] démarrage allowlist={list(SYMBOL_ALLOWLIST)} lot={LOT_HARD_CAP} magic={MAGIC_HARD} "
          f"emergency_close={cfg.get('WATCHDOG_EMERGENCY_CLOSE', 'false')} cycle={CYCLE_SECONDS}s", flush=True)

    import MetaTrader5 as mt5
    while True:
        started = time.monotonic()
        if not mt5.initialize():
            alerts_mgr.alert(HIGH, "MT5_INIT_FAIL", f"mt5.initialize a échoué : {mt5.last_error()}")
        else:
            try:
                alerts = run_cycle(mt5, alerts_mgr, cfg)
                if not alerts:
                    print(f"{_now_utc().isoformat()} [OK] RAS — 9 vérifications passées", flush=True)
            finally:
                mt5.shutdown()
        if once:
            break
        time.sleep(max(5.0, CYCLE_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
