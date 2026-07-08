# -*- coding: utf-8 -*-
"""HERMES — snapshot hebdo pour Cowork (AUTOMATION.md script 3).

Lecture seule. Produit C:\\hermes-reports\\semaine_YYYY-WW\\ : extrait du
dataset de la semaine, tous les bilans quotidiens de la semaine, alertes
watchdog, stats brutes pre-calculees, et COWORK_BRIEF.md (observation pure,
aucune recommandation). Tache planifiee HERMES_WEEKLY_SNAPSHOT, dimanche
12:00.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

REPORTS_DIR = Path(r"C:\hermes-reports")
DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
WATCHDOG_ALERTS = REPO_ROOT / "watchdog" / "WATCHDOG_ALERTS.log"
HEALTH_LOG = REPORTS_DIR / "_automation_health.log"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat(status: str, detail: str = "") -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with HEALTH_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{_now_utc().isoformat()} [weekly_snapshot] {status} {detail}\n".rstrip() + "\n")


def _iso_week_label(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _load_dataset_rows_since(start: datetime) -> list[dict]:
    rows = []
    if not DATASET_FILE.exists():
        return rows
    with DATASET_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            created = row.get("created_at") or row.get("closed_at") or ""
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= start:
                rows.append(row)
    return rows


def _session_of(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "ASIA"
    if 7 <= hour_utc < 13:
        return "LONDON"
    if 13 <= hour_utc < 21:
        return "NEWYORK"
    return "LATE"


def _precomputed_stats(rows: list[dict]) -> dict:
    outcomes = [r for r in rows if r.get("row_type") == "outcome" and r.get("pnl_reconciled") is not None]
    by_symbol_strategy = Counter()
    by_session = Counter()
    exit_reasons = Counter()
    net_by_symbol = Counter()
    for o in outcomes:
        exit_reasons[str(o.get("outcome") or "UNKNOWN")] += 1
        net_by_symbol[str(o.get("symbol") or "UNKNOWN")] += float(o.get("pnl_reconciled") or 0.0)
        opened = o.get("opened_at")
        if opened:
            try:
                hour = datetime.fromisoformat(str(opened).replace("Z", "+00:00")).hour
                by_session[_session_of(hour)] += 1
            except ValueError:
                pass
    for row in rows:
        if row.get("row_type") == "decision" and row.get("decision") == "PASS":
            key = f"{row.get('symbol')}::{row.get('strategy')}"
            by_symbol_strategy[key] += 1
    return {
        "trades_by_session": dict(by_session),
        "trades_by_symbol_strategy": dict(by_symbol_strategy.most_common(20)),
        "exit_reason_distribution": dict(exit_reasons),
        "net_pnl_by_symbol": {k: round(v, 2) for k, v in net_by_symbol.items()},
        "total_outcomes": len(outcomes),
        "total_wins": sum(1 for o in outcomes if float(o["pnl_reconciled"]) > 0),
    }


def build_snapshot() -> Path:
    now = _now_utc()
    week_start = now - timedelta(days=7)
    week_label = _iso_week_label(now)
    out_dir = REPORTS_DIR / f"semaine_{week_label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_dataset_rows_since(week_start)
    (out_dir / "dataset_extract.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    bilans_dir = out_dir / "bilans_quotidiens"
    bilans_dir.mkdir(exist_ok=True)
    for p in REPORTS_DIR.glob("bilan_*.md"):
        try:
            date_part = p.stem.replace("bilan_", "")
            day = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if day >= week_start - timedelta(days=1):
                shutil.copy2(p, bilans_dir / p.name)
        except ValueError:
            continue

    watchdog_alerts_week = []
    if WATCHDOG_ALERTS.exists():
        cutoff = week_start.strftime("%Y-%m-%d")
        try:
            for line in WATCHDOG_ALERTS.read_text(encoding="utf-8").splitlines():
                if line[:10] >= cutoff:
                    watchdog_alerts_week.append(line)
        except OSError:
            pass
    (out_dir / "watchdog_alerts_week.log").write_text("\n".join(watchdog_alerts_week), encoding="utf-8")

    stats = _precomputed_stats(rows)
    (out_dir / "stats_precalculees.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    prev_snapshot_dir = None
    prev_week = now - timedelta(days=7)
    prev_label = _iso_week_label(prev_week)
    candidate = REPORTS_DIR / f"semaine_{prev_label}"
    if candidate.exists() and candidate != out_dir:
        prev_snapshot_dir = candidate

    brief = f"""# COWORK_BRIEF — semaine {week_label}

Analyse ce dossier : win rate par session/stratégie/bande EES, patterns émergents,
comparaison avec la semaine précédente ({f"dossier {prev_snapshot_dir.name} disponible" if prev_snapshot_dir else "aucune semaine précédente disponible"}),
5 observations chiffrées, AUCUNE recommandation de modification — observation pure.

## Contenu du dossier
- `dataset_extract.jsonl` — lignes du dataset décisionnel de la semaine ({len(rows)} lignes)
- `bilans_quotidiens/` — bilans quotidiens de la semaine
- `watchdog_alerts_week.log` — alertes watchdog de la semaine ({len(watchdog_alerts_week)} lignes)
- `stats_precalculees.json` — trades par session/stratégie, distribution des exits, P&L net par symbole

## Chiffres bruts pré-calculés
- Trades clôturés cette semaine : {stats['total_outcomes']} ({stats['total_wins']} gagnants)
- Répartition par session : {stats['trades_by_session']}
- Distribution des sorties : {stats['exit_reason_distribution']}
- P&L net par symbole : {stats['net_pnl_by_symbol']}
"""
    (out_dir / "COWORK_BRIEF.md").write_text(brief, encoding="utf-8")
    return out_dir


def main() -> None:
    try:
        out_dir = build_snapshot()
        _heartbeat("OK", f"snapshot={out_dir}")
        print(f"[weekly_snapshot] OK -> {out_dir}")
    except Exception as exc:
        import traceback
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        error_path = REPORTS_DIR / f"weekly_snapshot_ERROR_{_now_utc().strftime('%Y-%m-%dT%H%M%S')}.md"
        error_path.write_text(f"# ERREUR snapshot hebdo\n\n```\n{traceback.format_exc()}\n```\n", encoding="utf-8")
        _heartbeat("ERROR", str(exc)[:200])
        print(f"[weekly_snapshot] ERROR -> {error_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
