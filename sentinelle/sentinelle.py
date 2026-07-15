# -*- coding: utf-8 -*-
"""SENTINELLE HERMES — le veilleur 24/24 (LECTURE SEULE, script deterministe).

Rassemble les FAITS (dataset, logs, historique MT5), applique les regles pures de
`rules.py`, et envoie UN message Telegram groupe quand une regle certaine casse.

INVARIANTS (mission/MISSION_SENTINELLE.md) :
- LECTURE SEULE stricte sur HERMES : aucun order_send, aucune position touchee,
  aucun fichier de prod modifie. N'ecrit QUE dans sentinelle/ (etat + log).
- Pas un agent IA : regles codees en dur, zero appel LLM.
- Ne perturbe pas le bot : MT5 en lecture, init/shutdown court (le watchdog fait
  deja tourner un 2e lecteur MT5 concurrent en prod — precedent etabli).
- Anti-bruit : etat persistant, cooldown de re-alerte, message groupe, prime au
  1er run (aucune alerte sur l'historique connu-sain).

Lancee toutes les 10 min par la tache planifiee HERMES_SENTINELLE.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import truststore

# La console Windows (cp1252) ne sait pas encoder les emojis (✅ 🔴 🟠). On force
# stdout en UTF-8 pour que les logs/prints ne plantent jamais dessus.
try:  # pragma: no cover - dependant de l'OS
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SENTINELLE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SENTINELLE_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from sentinelle import rules  # noqa: E402  (paquet local, pur)
from sentinelle.rules import RED, ORANGE, Anomaly  # noqa: E402

# INDEPENDANCE TOTALE de l'app : on N'IMPORTE RIEN de `app` — importer
# app.utils.broker_time tirerait app.logger, qui attache un handler fichier sur
# logs/hermes.log (le log de PROD). La sentinelle n'ecrit QUE dans sentinelle/ :
# on reimplemente donc localement les deux helpers broker-time (math identique a
# app/utils/broker_time.py, sans le garde-fou qui logue cote prod).
_BROKER_FUTURE_TAIL_H = 2.0


def _broker_day_window(now_utc: datetime, offset_h: float) -> tuple[datetime, datetime]:
    """[minuit broker, now + 2h], en UTC — copie de app.utils.broker_time."""
    offset = timedelta(hours=float(offset_h))
    broker_now = now_utc + offset
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return broker_midnight - offset, now_utc + timedelta(hours=_BROKER_FUTURE_TAIL_H)


def _to_mt5_query_bounds(start_utc: datetime, end_utc: datetime, offset_h: float) -> tuple[datetime, datetime]:
    """TRUE-UTC -> bornes en heure-murale-broker naïves que MT5 compare vraiment
    (deal.time est stampe en heure serveur UTC+3) — copie de app.utils.broker_time."""
    offset = timedelta(hours=float(offset_h))
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=timezone.utc)
    return (start_utc + offset).replace(tzinfo=None), (end_utc + offset).replace(tzinfo=None)

# ── chemins (lecture prod / ecriture sentinelle uniquement) ─────────────────
DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
BOT_LOG_FILE = REPO_ROOT / "logs" / "hermes.log"
WATCHDOG_ENV = REPO_ROOT / "watchdog" / ".env"
ROOT_ENV = REPO_ROOT / ".env"

STATE_FILE = SENTINELLE_DIR / "state.json"
SENTINELLE_LOG = SENTINELLE_DIR / "sentinelle.log"

MAGIC = 909002
BROKER_OFFSET_H = 3.0
RESEND_HOURS = 6.0            # cooldown de re-alerte d'une meme anomalie
WINDOW_KEEP_HOURS = 48.0     # profondeur de la fenetre roulante persistee
COVERAGE_WINDOW_HOURS = 24.0


# ═══ utilitaires I/O + temps ════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(msg: str) -> None:
    line = f"{_now().isoformat()} {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass  # le veilleur ne meurt JAMAIS d'un probleme de console
    try:
        with SENTINELLE_LOG.open("a", encoding="utf-8") as f:  # fichier : toujours UTF-8
            f.write(line + "\n")
    except OSError:
        pass


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _load_env() -> dict:
    """Creds Telegram : lus en LECTURE SEULE, jamais loggues, jamais committes."""
    cfg: dict = {}
    for path in (ROOT_ENV, WATCHDOG_ENV):
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        cfg.setdefault(k.strip(), v.strip())
        except OSError:
            pass
    # override par variables d'environnement si presentes
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SENTINELLE_HEARTBEAT"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        _log(f"[ETAT] sauvegarde impossible: {exc}")


# ═══ lecture incrementale du dataset (le fichier fait ~80 Mo) ════════════════

def read_dataset(state: dict) -> tuple[list[dict], list[dict], dict, datetime | None, bool]:
    """Lit UNIQUEMENT les octets ajoutes depuis la derniere passe (offset persiste).

    Met a jour, dans `state` : window_executed (48h), window_outcomes (48h),
    known_tickets, dataset_offset, last_decision_time.
    Renvoie (new_executed_cette_passe, executed_24h, outcomes_by_ticket,
    last_decision_time, first_run).
    """
    now = _now()
    first_run = not bool(state.get("primed"))
    offset = int(state.get("dataset_offset", 0))
    known = set(state.get("known_tickets", []))
    win_exec: list[dict] = list(state.get("window_executed", []))
    win_out: dict = dict(state.get("window_outcomes", {}))
    last_dec = _parse_dt(state.get("last_decision_time"))
    new_executed: list[dict] = []

    if not DATASET_FILE.exists():
        _log("[DATASET] introuvable — regles dataset ignorees")
        return [], [], win_out, last_dec, first_run

    size = DATASET_FILE.stat().st_size
    if size < offset:            # rotation/troncature -> on repart proprement
        _log(f"[DATASET] rotation detectee (taille {size} < offset {offset}) -> re-prime")
        offset, first_run = 0, True
        win_exec, win_out, known = [], {}, set()

    consumed = offset
    try:
        with DATASET_FILE.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # ligne partielle (ecriture en cours) : on s'arrete avant
                consumed += len(raw)
                try:
                    row = json.loads(raw.decode("utf-8", "ignore"))
                except (json.JSONDecodeError, ValueError):
                    continue
                _ingest_row(row, known, win_exec, win_out, new_executed, first_run)
                rt = row.get("row_type")
                if rt == "decision":
                    rdt = _parse_dt(row.get("recorded_at"))
                    if rdt and (last_dec is None or rdt > last_dec):
                        last_dec = rdt
    except OSError as exc:
        _log(f"[DATASET] lecture impossible: {exc}")
        return [], [], win_out, last_dec, first_run

    # prune 48h
    keep_after = now - timedelta(hours=WINDOW_KEEP_HOURS)
    win_exec = [r for r in win_exec if (_parse_dt(r.get("recorded_at")) or now) >= keep_after]
    win_out = {
        k: v for k, v in win_out.items()
        if (_parse_dt(v.get("closed_at")) or now) >= keep_after
    }

    state["dataset_offset"] = consumed
    state["known_tickets"] = sorted(known)
    state["window_executed"] = win_exec
    state["window_outcomes"] = win_out
    state["last_decision_time"] = last_dec.isoformat() if last_dec else None

    cutoff_24h = now - timedelta(hours=COVERAGE_WINDOW_HOURS)
    executed_24h = [r for r in win_exec if (_parse_dt(r.get("recorded_at")) or now) >= cutoff_24h]
    return new_executed, executed_24h, win_out, last_dec, first_run


def _ingest_row(row: dict, known: set, win_exec: list, win_out: dict,
                new_executed: list, first_run: bool) -> None:
    rt = row.get("row_type")
    if rt == "decision":
        executed = (
            row.get("order_success") is True
            and str(row.get("event_type") or "").upper() == "DEMO_ORDER"
            and row.get("ticket") is not None
        )
        if executed:
            rec = {
                "ticket": str(row.get("ticket")),
                "recorded_at": row.get("recorded_at"),
                "symbol": row.get("symbol") or row.get("broker_symbol"),
                "direction": row.get("direction"),
                "entry": row.get("entry"),
                "exec_quality.fill_price": row.get("exec_quality.fill_price"),
                "sl": row.get("sl"),
                "tp": row.get("tp"),
                "collection_version": row.get("collection_version"),
                "core_fix_level": row.get("core_fix_level"),
                "fallback_decision": row.get("fallback_decision"),
                "fallback_block_reason": row.get("fallback_block_reason"),
                "fallback_reason": row.get("fallback_reason"),
            }
            known.add(rec["ticket"])
            win_exec.append(rec)
            if not first_run:
                new_executed.append(rec)
    elif rt == "outcome":
        t = row.get("ticket")
        if t is not None and not row.get("virtual"):
            win_out[str(t)] = {
                "pnl_reconciled": row.get("pnl_reconciled"),
                "pnl_source": row.get("pnl_source"),
                "closed_at": row.get("closed_at"),
                "outcome": row.get("outcome"),
            }


# ═══ MT5 (best-effort, lecture seule, init/shutdown court) ══════════════════

def gather_mt5() -> dict:
    """Positions ouvertes + deals du jour (magic 909002). Si MT5 indisponible,
    renvoie des faits vides -> les regles MT5 ne concluent pas (fail-open : on
    n'alerte JAMAIS sur l'indisponibilite MT5, c'est le domaine du watchdog)."""
    facts = {
        "open_positions": [], "closing_deals": [], "mt5_pnl_by_ticket": {},
        "deals_losses_today": None, "mt5_ok": False,
    }
    try:
        import MetaTrader5 as mt5
    except Exception as exc:
        _log(f"[MT5] module indisponible: {str(exc)[:120]}")
        return facts
    try:
        if not mt5.initialize():
            _log(f"[MT5] initialize a echoue: {mt5.last_error()}")
            return facts
        now = _now()
        # positions ouvertes du bot
        for p in (mt5.positions_get() or []):
            if int(getattr(p, "magic", -1) or -1) != MAGIC:
                continue
            facts["open_positions"].append({
                "ticket": str(getattr(p, "ticket", "")),
                "symbol": getattr(p, "symbol", ""),
                "sl": getattr(p, "sl", None),
                "tp": getattr(p, "tp", None),
                "magic": getattr(p, "magic", None),
                "volume": getattr(p, "volume", None),
            })
        # deals des dernieres 24h + jour broker
        win_start = now - timedelta(hours=COVERAGE_WINDOW_HOURS)
        q_start, q_end = _to_mt5_query_bounds(win_start, now + timedelta(hours=2), BROKER_OFFSET_H)
        deals = mt5.history_deals_get(q_start, q_end) or []

        pnl_by_ticket: dict = {}
        for d in deals:
            if int(getattr(d, "magic", -1) or -1) != MAGIC:
                continue
            pos_id = str(getattr(d, "position_id", "") or "")
            net = (
                (rules._f(getattr(d, "profit", 0)) or 0.0)
                + (rules._f(getattr(d, "commission", 0)) or 0.0)
                + (rules._f(getattr(d, "swap", 0)) or 0.0)
            )
            pnl_by_ticket[pos_id] = pnl_by_ticket.get(pos_id, 0.0) + net
            if int(getattr(d, "entry", -1) or 0) == 1:  # DEAL_ENTRY_OUT = cloture
                ct = getattr(d, "time", None)
                close_time = datetime.fromtimestamp(int(ct), tz=timezone.utc) if ct else None
                # temps broker (UTC+3) -> vrai UTC
                if close_time is not None:
                    close_time = close_time - timedelta(hours=BROKER_OFFSET_H)
                facts["closing_deals"].append({
                    "position_id": pos_id, "close_time": close_time,
                    "profit_net": round(net, 2),
                })
        facts["mt5_pnl_by_ticket"] = {k: round(v, 2) for k, v in pnl_by_ticket.items()}

        # pertes du JOUR broker (meme logique que daily_killswitch)
        d_start, d_end = _broker_day_window(now, BROKER_OFFSET_H)
        qd_start, qd_end = _to_mt5_query_bounds(d_start, d_end, BROKER_OFFSET_H)
        day_deals = mt5.history_deals_get(qd_start, qd_end) or []
        losses = 0
        for d in day_deals:
            if int(getattr(d, "magic", -1) or -1) != MAGIC:
                continue
            if int(getattr(d, "entry", -1) or 0) != 1:
                continue
            net = (
                (rules._f(getattr(d, "profit", 0)) or 0.0)
                + (rules._f(getattr(d, "commission", 0)) or 0.0)
                + (rules._f(getattr(d, "swap", 0)) or 0.0)
            )
            if net < 0:
                losses += 1
        facts["deals_losses_today"] = losses
        facts["mt5_ok"] = True
    except Exception as exc:
        _log(f"[MT5] lecture echouee: {str(exc)[:160]}")
    finally:
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except Exception:
            pass
    return facts


# ═══ log kill-switch (B4 / B3) + activite log (D1) ══════════════════════════

_KS_RE = re.compile(
    r"\[DAILY_KILLSWITCH\] now_utc_detected=(\S+) triggered=(\w+) reason=(\S+) "
    r"losses=(\d+)/(\d+) daily_pnl=(-?\d+\.\d+)"
)


def parse_killswitch(now: datetime) -> dict:
    """Derniere ligne [DAILY_KILLSWITCH] : compteurs rapportes + fraicheur +
    etat triggered. now_utc_detected est en UTC explicite dans le log."""
    res = {"logged_losses": None, "logged_daily_pnl": None,
           "killswitch_log_age_min": None, "killswitch_triggered": False}
    try:
        if not BOT_LOG_FILE.exists():
            return res
        tail = _tail_lines(BOT_LOG_FILE, 4000)
        last = None
        for line in tail:
            if "[DAILY_KILLSWITCH]" in line:
                m = _KS_RE.search(line)
                if m:
                    last = m
        if last is None:
            return res
        detected = _parse_dt(last.group(1))
        res["killswitch_triggered"] = (last.group(2).lower() == "true")
        res["logged_losses"] = int(last.group(4))
        res["logged_daily_pnl"] = float(last.group(6))
        if detected is not None:
            res["killswitch_log_age_min"] = (now - detected).total_seconds() / 60.0
    except Exception as exc:
        _log(f"[KILLSWITCH_LOG] parse echoue: {str(exc)[:120]}")
    return res


def _tail_lines(path: Path, n: int) -> list[str]:
    """Derniere fenetre du log sans tout charger (fichier volumineux)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 65536
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        return data.decode("utf-8", "ignore").splitlines()[-n:]
    except OSError:
        return []


def log_mtime() -> datetime | None:
    try:
        if BOT_LOG_FILE.exists():
            return datetime.fromtimestamp(BOT_LOG_FILE.stat().st_mtime, tz=timezone.utc)
    except OSError:
        pass
    return None


def market_open(now: datetime) -> bool:
    """GOLD ferme le week-end (vendredi 21:00 UTC -> dimanche 22:00 UTC) ; le bot
    est flat-weekend. Hors de cette fenetre : marche ouvert."""
    wd, hour = now.weekday(), now.hour       # lundi=0 .. dimanche=6
    if wd == 5:                               # samedi
        return False
    if wd == 4 and hour >= 21:                 # vendredi soir
        return False
    if wd == 6 and hour < 22:                   # dimanche avant reouverture
        return False
    return True


# ═══ Telegram (truststore : meme methode TLS que le watchdog) ═══════════════

def send_telegram(text: str, cfg: dict) -> bool:
    # SENTINELLE_DRY_RUN=1 : on n'envoie RIEN (verification d'installation), on
    # logue seulement ce qui serait parti. Le fichier fait foi.
    if str(os.environ.get("SENTINELLE_DRY_RUN", "")).strip() in ("1", "true", "True"):
        _log(f"[DRY_RUN] message NON envoye :: {text.splitlines()[0]}")
        return False
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _log("[TELEGRAM] token/chat_id absents -> alerte fichier seulement")
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status == 200
    except Exception as exc:
        _log(f"[TELEGRAM] indisponible: {str(exc)[:120]}")
        return False


def _format_alert(anomalies: list[Anomaly], now: datetime) -> str:
    has_red = any(a.severity == RED for a in anomalies)
    header = "🔴 ALERTE SENTINELLE" if has_red else "🟠 SENTINELLE"
    lines = [header]
    for a in sorted(anomalies, key=lambda x: (x.severity != RED, x.rule)):
        tag = "🔴" if a.severity == RED else "🟠"
        lines.append(f"{tag} [{a.rule}] {a.message}")
    lines.append(f"— {now.strftime('%H:%M UTC %d/%m')}")
    return "\n".join(lines)


# ═══ boucle principale (une passe) ══════════════════════════════════════════

def run_once() -> int:
    now = _now()
    cfg = _load_env()
    state = _load_state()
    alert_state: dict = dict(state.get("alert_state", {}))

    new_executed, executed_24h, outcomes_by_ticket, last_dec, first_run = read_dataset(state)
    mt5_facts = gather_mt5()
    ks = parse_killswitch(now)

    facts = {
        "new_executed": new_executed,
        "executed_24h": executed_24h,
        "outcomes_by_ticket": outcomes_by_ticket,
        "open_positions": mt5_facts["open_positions"],
        "closing_deals": mt5_facts["closing_deals"],
        "mt5_pnl_by_ticket": mt5_facts["mt5_pnl_by_ticket"],
        "deals_losses_today": mt5_facts["deals_losses_today"],
        "known_tickets": set(state.get("known_tickets", [])),
        "last_decision_time": last_dec,
        "log_mtime": log_mtime(),
        "market_open": market_open(now),
        "killswitch_triggered": ks["killswitch_triggered"],
        "logged_losses": ks["logged_losses"],
        "logged_daily_pnl": ks["logged_daily_pnl"],
        "killswitch_log_age_min": ks["killswitch_log_age_min"],
    }

    anomalies = rules.evaluate_all(facts, now)

    if first_run:
        # PRIME : on baseline l'etat connu-sain sans crier. Les anomalies
        # eventuelles sont loggees pour inspection, pas envoyees ; un vrai
        # probleme actuel ressurgira a la passe suivante (regles d'etat).
        if anomalies:
            _log(f"[BASELINE] {len(anomalies)} anomalie(s) sur l'historique — SUPPRIMEES (1er run) :")
            for a in anomalies:
                _log(f"[BASELINE]   [{a.rule}] {a.message}")
        else:
            _log("[BASELINE] etat initial sain, aucune anomalie")
        state["primed"] = True
        state["alert_state"] = alert_state
        _save_state(state)
        _log(f"[SENTINELLE] baseline initialise : {len(state.get('known_tickets', []))} tickets connus, "
             f"offset={state.get('dataset_offset')}")
        return 0

    # anti-bruit : ne garder que les anomalies hors cooldown
    fresh: list[Anomaly] = []
    for a in anomalies:
        last_sent = _parse_dt(alert_state.get(a.signature))
        if last_sent is None or (now - last_sent) >= timedelta(hours=RESEND_HOURS):
            fresh.append(a)
            alert_state[a.signature] = now.isoformat()
    # purge des signatures resolues (plus presentes) pour permettre une re-alerte future
    active_sigs = {a.signature for a in anomalies}
    alert_state = {k: v for k, v in alert_state.items() if k in active_sigs}

    if fresh:
        msg = _format_alert(fresh, now)
        sent = send_telegram(msg, cfg)
        _log(f"[ALERTE] {len(fresh)} anomalie(s) {'envoyees' if sent else 'NON envoyees (telegram off)'} :")
        for a in fresh:
            _log(f"[ALERTE]   [{a.rule}] {a.message}")
    else:
        _log(f"[RAS] {len(anomalies)} anomalie(s) active(s) (toutes en cooldown), 0 nouvelle. "
             f"executed_24h={len(executed_24h)} outcomes24h="
             f"{sum(1 for r in executed_24h if str(r.get('ticket')) in outcomes_by_ticket)}")

    _maybe_heartbeat(cfg, state, now, executed_24h, outcomes_by_ticket, fresh)

    state["alert_state"] = alert_state
    _save_state(state)
    return 0


def _maybe_heartbeat(cfg: dict, state: dict, now: datetime,
                     executed_24h: list, outcomes: dict, fresh: list) -> None:
    """Message de vie 1x/jour (desactivable via SENTINELLE_HEARTBEAT=0). Envoye
    seulement s'il n'y a PAS d'alerte fraiche (une alerte prouve deja qu'elle vit)."""
    if str(cfg.get("SENTINELLE_HEARTBEAT", "1")).strip() not in ("1", "true", "True"):
        return
    today = now.strftime("%Y-%m-%d")
    if state.get("last_heartbeat_date") == today or fresh:
        return
    n = len(executed_24h)
    with_oc = sum(1 for r in executed_24h if str(r.get("ticket")) in outcomes)
    pct = (100.0 * with_oc / n) if n else 100.0
    send_telegram(
        f"✅ Sentinelle : RAS. {n} trade(s) v2 sur 24h, outcomes {pct:.0f}%. {now.strftime('%d/%m %H:%M UTC')}",
        cfg,
    )
    state["last_heartbeat_date"] = today
    _log(f"[HEARTBEAT] resume quotidien envoye ({n} trades, {pct:.0f}% outcomes)")


if __name__ == "__main__":
    try:
        sys.exit(run_once())
    except Exception as exc:  # le veilleur ne meurt jamais d'une passe cassee
        _log(f"[FATAL] passe interrompue: {str(exc)[:200]}")
        sys.exit(0)
