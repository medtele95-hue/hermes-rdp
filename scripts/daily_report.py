# -*- coding: utf-8 -*-
"""HERMES — bilan quotidien (AUTOMATION.md script 1).

Lecture seule sur les fichiers du bot + connexion MT5 en lecture seule
(source de verite pour les trades du jour). Ecrit
C:\\hermes-reports\\bilan_YYYY-MM-DD.md. Fail-safe : toute exception est
capturee et ecrite dans le rapport plutot que de crasher silencieusement.
Tache planifiee HERMES_DAILY_REPORT, chaque jour a 21:30 (apres le reset
kill-switch), lancee par scripts/run_daily_report.ps1.
"""
from __future__ import annotations

import json
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

REPORTS_DIR = Path(r"C:\hermes-reports")
DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
WATCHDOG_ALERTS = REPO_ROOT / "watchdog" / "WATCHDOG_ALERTS.log"
BACKUPS_DIR = REPO_ROOT / "backups"
HEALTH_LOG = REPORTS_DIR / "_automation_health.log"

MAGIC_HARD = 909002
BROKER_UTC_OFFSET_HOURS = 3.0
MAX_LOSSES_PER_DAY = 6

REFUSED_REASONS = ("EES_EXTREME_BLOCK", "NEWS_BLACKOUT", "ORDER_ABORT", "SYMBOL_BLOCKED")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat(status: str, detail: str = "") -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{_now_utc().isoformat()} [daily_report] {status} {detail}".strip()
    with HEALTH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def broker_day_window(now_utc: datetime) -> tuple[datetime, datetime]:
    offset = timedelta(hours=BROKER_UTC_OFFSET_HOURS)
    broker_now = now_utc + offset
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return broker_midnight - offset, now_utc + timedelta(hours=2)


def _load_dataset() -> list[dict]:
    rows = []
    if not DATASET_FILE.exists():
        return rows
    with DATASET_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _outcome_for_ticket(rows: list[dict], ticket: int) -> dict | None:
    for row in rows:
        if row.get("row_type") == "outcome" and row.get("ticket") == ticket:
            return row
    return None


def _today_deals() -> list:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return []
    if not mt5.initialize():
        return []
    try:
        start, end = broker_day_window(_now_utc())
        deals = mt5.history_deals_get(start.replace(tzinfo=None), end.replace(tzinfo=None))
        return [d for d in (deals or []) if int(getattr(d, "magic", 0) or 0) == MAGIC_HARD]
    finally:
        mt5.shutdown()


def _account_info() -> dict:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {}
    if not mt5.initialize():
        return {}
    try:
        acc = mt5.account_info()
        if acc is None:
            return {}
        return {"equity": acc.equity, "balance": acc.balance}
    finally:
        mt5.shutdown()


def _closed_trades_today(deals: list, dataset_rows: list[dict]) -> list[dict]:
    opens = {}
    closes = []
    for d in deals:
        if int(getattr(d, "entry", -1) or 0) == 0:
            opens[d.position_id] = d
        elif int(getattr(d, "entry", -1) or 0) == 1:
            closes.append(d)
    trades = []
    for d in closes:
        net = float(d.profit or 0) + float(d.commission or 0) + float(d.swap or 0)
        open_deal = opens.get(d.position_id)
        duration = None
        if open_deal is not None:
            duration = int(d.time) - int(open_deal.time)
        outcome_row = _outcome_for_ticket(dataset_rows, int(d.position_id))
        trades.append({
            "ticket": int(d.position_id),
            "symbol": d.symbol,
            "direction": "BUY" if int(getattr(open_deal, "type", d.type) or 0) == 0 else "SELL",
            "exit_price": float(d.price),
            "entry_price": float(open_deal.price) if open_deal is not None else None,
            "net": round(net, 2),
            "duration_s": duration,
            "close_mode": (outcome_row or {}).get("outcome") or "INCONNU",
            "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
        })
    trades.sort(key=lambda t: t["time"])
    return trades


EVENTS_FILE = REPO_ROOT / "app" / "data" / "demo_pilot_events.jsonl"


def _daily_killswitch_snapshot() -> dict:
    if not EVENTS_FILE.exists():
        return {}
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ks = row.get("daily_killswitch") or {}
        if ks.get("losses_today") is not None:
            return {
                "consecutive_losses": ks.get("losses_today"),
                "daily_pnl": ks.get("daily_pnl"),
                "drawdown_pct": ks.get("drawdown_pct"),
                "triggered": ks.get("triggered"),
            }
    return {}


def _refused_signals_today(dataset_rows: list[dict], today: str) -> Counter:
    counts = Counter()
    for row in dataset_rows:
        if row.get("row_type") != "decision":
            continue
        created = str(row.get("created_at") or "")
        if not created.startswith(today):
            continue
        reason = str(row.get("reason") or "")
        for code in REFUSED_REASONS:
            if code in reason:
                counts[code] += 1
        if row.get("decision") == "BLOCK" and not reason:
            counts["OTHER_BLOCK"] += 1
    return counts


def _watchdog_alerts_today(today: str) -> list[str]:
    if not WATCHDOG_ALERTS.exists():
        return []
    lines = []
    try:
        for line in WATCHDOG_ALERTS.read_text(encoding="utf-8").splitlines():
            if line.startswith(today):
                lines.append(line)
    except OSError:
        pass
    return lines


def _backup_status() -> dict:
    status_file = BACKUPS_DIR / "_status.json"
    if not status_file.exists():
        return {}
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _last_backup() -> str:
    if not BACKUPS_DIR.exists():
        return "AUCUN"
    dated = sorted(
        (p.name for p in BACKUPS_DIR.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-"),
        reverse=True,
    )
    return dated[0] if dated else "AUCUN"


def _all_time_stats(dataset_rows: list[dict]) -> dict:
    outcomes = [r for r in dataset_rows if r.get("row_type") == "outcome" and r.get("pnl_reconciled") is not None]
    total = len(outcomes)
    wins = sum(1 for r in outcomes if float(r["pnl_reconciled"]) > 0)
    net_all_time = sum(float(r["pnl_reconciled"]) for r in outcomes)
    return {
        "total_trades": total,
        "wins": wins,
        "win_rate": round(wins / total * 100.0, 1) if total else 0.0,
        "net_all_time": round(net_all_time, 2),
    }


def _send_telegram(text: str) -> bool:
    env_file = REPO_ROOT / "watchdog" / ".env"
    if not env_file.exists():
        return False
    cfg = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    token, chat_id = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def build_report() -> Path:
    now = _now_utc()
    today_broker = (now + timedelta(hours=BROKER_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")
    dataset_rows = _load_dataset()
    deals = _today_deals()
    trades = _closed_trades_today(deals, dataset_rows)
    account = _account_info()
    all_time = _all_time_stats(dataset_rows)
    refused = _refused_signals_today(dataset_rows, today_broker)
    watchdog_alerts = _watchdog_alerts_today(today_broker)
    killswitch = _daily_killswitch_snapshot()
    last_backup = _last_backup()
    backup_status = _backup_status()
    dataset_lines = len(dataset_rows)

    net_today = round(sum(t["net"] for t in trades), 2)
    wins_today = sum(1 for t in trades if t["net"] > 0)
    losses_today = sum(1 for t in trades if t["net"] < 0)

    anomaly = "aucune"
    if killswitch.get("consecutive_losses") is not None and killswitch["consecutive_losses"] >= MAX_LOSSES_PER_DAY:
        anomaly = f"KILL-SWITCH ATTEINT ({killswitch['consecutive_losses']}/{MAX_LOSSES_PER_DAY})"
    elif watchdog_alerts:
        anomaly = f"{len(watchdog_alerts)} alerte(s) watchdog aujourd'hui"
    elif last_backup != today_broker and last_backup != (now.strftime("%Y-%m-%d")):
        anomaly = f"dernier backup daté {last_backup} (pas aujourd'hui)"

    notable = "RAS"
    if trades:
        best = max(trades, key=lambda t: t["net"])
        worst = min(trades, key=lambda t: t["net"])
        notable = f"meilleur trade {best['symbol']} {best['net']:+.2f} USD, pire {worst['symbol']} {worst['net']:+.2f} USD"

    summary = [
        f"P&L du jour : {net_today:+.2f} USD ({wins_today}W/{losses_today}L sur {len(trades)} trades) | P&L cumulé (dataset) : {all_time['net_all_time']:+.2f} USD",
        f"Notable : {notable}",
        f"Anomalie : {anomaly}",
    ]

    lines = []
    lines.append(f"# BILAN QUOTIDIEN HERMES — {today_broker} (heure broker XM, UTC+{BROKER_UTC_OFFSET_HOURS:.0f})")
    lines.append("")
    lines.append("## Synthèse")
    for s in summary:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("## Trades du jour (source : MT5 history_deals_get)")
    if trades:
        lines.append("| Ticket | Symbole | Sens | Entrée | Sortie | Net USD | Durée | Clôture |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in trades:
            dur = f"{t['duration_s']//60}m{t['duration_s']%60}s" if t["duration_s"] is not None else "?"
            lines.append(
                f"| {t['ticket']} | {t['symbol']} | {t['direction']} | {t['entry_price']} | {t['exit_price']} | "
                f"{t['net']:+.2f} | {dur} | {t['close_mode']} |"
            )
    else:
        lines.append("Aucun trade fermé aujourd'hui.")
    lines.append("")
    lines.append("## Cumuls")
    lines.append(f"- Net du jour : {net_today:+.2f} USD")
    lines.append(f"- Net depuis le début (dataset, tout-temps) : {all_time['net_all_time']:+.2f} USD")
    lines.append(f"- Win rate courant : {all_time['win_rate']}% ({all_time['wins']}/{all_time['total_trades']})")
    lines.append(f"- Trades total (progression vers 50) : {all_time['total_trades']}/50")
    if account:
        lines.append(f"- Équité compte : {account.get('equity')} | Balance : {account.get('balance')}")
    lines.append("")
    lines.append("## Signaux refusés aujourd'hui")
    if refused:
        for code, count in refused.most_common():
            lines.append(f"- {code} : {count}")
    else:
        lines.append("Aucun signal refusé catégorisé aujourd'hui (ou dataset non à jour).")
    lines.append("")
    lines.append("## Santé")
    lines.append(f"- Kill-switch : {killswitch.get('consecutive_losses', '?')}/{MAX_LOSSES_PER_DAY}")
    lines.append(f"- Drawdown du jour : {killswitch.get('drawdown_pct', '?')}%")
    lines.append(f"- Taille dataset (decision_dataset.jsonl) : {dataset_lines} lignes")
    lines.append(f"- Dernier backup daté : {last_backup}")
    lines.append(f"- Alertes watchdog aujourd'hui : {len(watchdog_alerts)}")
    for a in watchdog_alerts[:10]:
        lines.append(f"  - {a}")
    if backup_status and not backup_status.get("remote_ok", False):
        lines.append("- ⚠️ **REMOTE GIT MANQUANT — action SIMO requise** (voir docs/BACKUP_SIMO.md)")
    if backup_status and not backup_status.get("google_drive"):
        lines.append("- ⚠️ Google Drive non détecté pour le miroir de backup — SIMO doit installer/brancher Google Drive Desktop")
    lines.append("")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"bilan_{today_broker}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    _send_telegram("📊 HERMES bilan " + today_broker + "\n" + "\n".join(summary))

    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from update_hermes_state import update_state  # noqa: E402
        update_state()
    except Exception as exc:
        _heartbeat("HERMES_STATE_UPDATE_FAILED", str(exc)[:200])

    return out_path


def main() -> None:
    try:
        out_path = build_report()
        _heartbeat("OK", f"report={out_path}")
        print(f"[daily_report] OK -> {out_path}")
    except Exception as exc:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        error_path = REPORTS_DIR / f"bilan_ERROR_{_now_utc().strftime('%Y-%m-%dT%H%M%S')}.md"
        error_path.write_text(
            f"# ERREUR bilan quotidien\n\n{_now_utc().isoformat()}\n\n```\n{traceback.format_exc()}\n```\n",
            encoding="utf-8",
        )
        _heartbeat("ERROR", str(exc)[:200])
        print(f"[daily_report] ERROR -> {error_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
