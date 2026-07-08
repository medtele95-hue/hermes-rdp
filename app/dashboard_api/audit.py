# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — action audit trail.

Every action (successful or refused) gets: a [DASHBOARD_ACTION] structured
log line (hermes.log), an append-only JSON line in
dashboard/actions_audit.log, and a best-effort Telegram push (fail-safe —
never blocks the action itself). Falls back to watchdog/.env's Telegram
credentials if dashboard/.env doesn't have its own.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app.logger import log

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_LOG_FILE = REPO_ROOT / "dashboard" / "actions_audit.log"
DASHBOARD_ENV_FILE = REPO_ROOT / "dashboard" / ".env"
WATCHDOG_ENV_FILE = REPO_ROOT / "watchdog" / ".env"


def _load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def record_action(action: str, result: str, detail: dict | None = None, notify: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat(),
        "action": action,
        "result": result,
        "detail": detail or {},
    }
    log.info("[DASHBOARD_ACTION] action=%s result=%s detail=%s", action, result, detail or {})
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("[DASHBOARD_ACTION] audit log write failed: %s", exc)
    if notify:
        send_telegram(f"HERMES DASHBOARD : {action} -> {result}" + (f" {detail}" if detail else ""))
    return entry


def read_recent_audit(limit: int = 50) -> list[dict]:
    if not AUDIT_LOG_FILE.exists():
        return []
    lines: list[dict] = []
    try:
        for line in AUDIT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return lines[-limit:]


def send_telegram(text: str) -> bool:
    cfg = _load_env(DASHBOARD_ENV_FILE)
    token = cfg.get("TELEGRAM_BOT_TOKEN") or _load_env(WATCHDOG_ENV_FILE).get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("TELEGRAM_CHAT_ID") or _load_env(WATCHDOG_ENV_FILE).get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:  # fail-safe: audit trail must never depend on network
        log.info("[DASHBOARD_ACTION] telegram unavailable: %s", str(exc)[:120])
        return False
