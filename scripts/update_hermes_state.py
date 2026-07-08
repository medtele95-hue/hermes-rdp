# -*- coding: utf-8 -*-
"""HERMES — HERMES_STATE.md (AUTOMATION.md script 4).

Fichier d'etat vivant a la racine du repo, mis a jour a chaque bilan
quotidien (appele depuis scripts/daily_report.py) ou a la main via
`python scripts/update_hermes_state.py`. Lecture seule sur le bot, source de
verite = git (tags/commits) + dataset + watchdog. C'est le fichier que Claude
Code doit lire en debut de toute future mission.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_FILE = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
WATCHDOG_ALERTS = REPO_ROOT / "watchdog" / "WATCHDOG_ALERTS.log"
STATE_FILE = REPO_ROOT / "HERMES_STATE.md"
GIT = r"C:\Tools\MinGit\cmd\git.exe" if Path(r"C:\Tools\MinGit\cmd\git.exe").exists() else "git"


def _git(*args: str) -> str:
    try:
        out = subprocess.run([GIT, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def _last_n_commits(n: int = 8) -> list[str]:
    out = _git("log", f"-{n}", "--oneline")
    return out.splitlines() if out else []


def _tags() -> list[str]:
    out = _git("tag", "--sort=-creatordate")
    return out.splitlines()[:10] if out else []


def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD") or "?"


def _dataset_line_count() -> int:
    if not DATASET_FILE.exists():
        return 0
    with DATASET_FILE.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def _all_time_pnl() -> tuple[int, float]:
    if not DATASET_FILE.exists():
        return 0, 0.0
    total, net = 0, 0.0
    with DATASET_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("row_type") == "outcome" and row.get("pnl_reconciled") is not None:
                total += 1
                net += float(row["pnl_reconciled"])
    return total, round(net, 2)


def _open_watchdog_alerts() -> list[str]:
    if not WATCHDOG_ALERTS.exists():
        return []
    try:
        lines = WATCHDOG_ALERTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [l for l in lines if "[CRITIQUE]" in l or "[HAUTE]" in l][-10:]


def update_state() -> Path:
    now = datetime.now(timezone.utc)
    total, net = _all_time_pnl()
    lines = [
        "# HERMES_STATE — état vivant",
        f"_Mis à jour automatiquement le {now.isoformat()} par scripts/update_hermes_state.py_",
        "",
        "## Version",
        f"- Branche : `{_current_branch()}`",
        "- Derniers tags :",
    ]
    for tag in _tags():
        lines.append(f"  - {tag}")
    lines.append("- Derniers commits :")
    for c in _last_n_commits():
        lines.append(f"  - {c}")
    lines += [
        "",
        "## Invariants actifs (verrou GRAND_PLAN mission1)",
        "- SYMBOL_ALLOWLIST = (GOLD#, BTCUSD#) — tout autre symbole → [SYMBOL_BLOCKED]",
        "- Lot fixe 0.01, magic 909002, SL/TP obligatoires",
        "- MAX_OPEN = 1 PAR symbole, kill-switch partagé (6 pertes/jour, DD 3%)",
        "- Exit V2 = autorité de sortie unique GOLD#+BTCUSD# (parasites QUICK_EXIT/Smart Rescue neutralisés)",
        "- HERMES_LOG_FILE permanent (logs/hermes.log, RotatingFileHandler)",
        "- Watchdog indépendant actif (tâche planifiée HERMES_WATCHDOG, mode urgence OFF par défaut)",
        "",
        "## Métriques cumulées (dataset, tout-temps)",
        f"- Trades clôturés enregistrés : {total} (objectif pilote 50)",
        f"- P&L net cumulé : {net:+.2f} USD",
        f"- Taille dataset : {_dataset_line_count()} lignes",
        "",
        "## Alertes ouvertes (watchdog, dernières CRITIQUE/HAUTE)",
    ]
    alerts = _open_watchdog_alerts()
    if alerts:
        for a in alerts:
            lines.append(f"- {a}")
    else:
        lines.append("- Aucune")
    lines += [
        "",
        "## Actions humaines (RÉSERVÉ SIMO) en attente",
    ]
    if _git("remote").strip():
        lines.append("- Remote git privé configuré (voir `git remote -v`) — backup quotidien pousse automatiquement.")
    else:
        lines.append("- Remote git privé toujours à créer (voir docs/BACKUP_SIMO.md) — tant qu'il est absent, le backup quotidien alerte chaque jour.")
    lines += [
        "- Décider de l'activation du MODE URGENCE du watchdog (watchdog/.env, WATCHDOG_EMERGENCY_CLOSE).",
        "- Configurer Telegram (watchdog/.env) pour recevoir les alertes watchdog + le bilan quotidien en push.",
    ]
    STATE_FILE.write_text("\n".join(lines), encoding="utf-8")
    return STATE_FILE


def main() -> None:
    path = update_state()
    print(f"[update_hermes_state] OK -> {path}")


if __name__ == "__main__":
    main()
