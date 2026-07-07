"""BLOC 11e — single-instance lock.

The bot refuses to start when another instance is already running for the
SAME account + magic. Lock = JSON file keyed on login+magic holding the
owner PID; a stale lock (dead PID) is reclaimed automatically.
"""
from __future__ import annotations

import ctypes
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.logger import log

_LOCK_DIR = Path(__file__).resolve().parents[1] / "data"


class SingleInstanceError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        still_active = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(still_active))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and still_active.value == 259  # STILL_ACTIVE
    except Exception:
        # Non-Windows or query failure: assume alive (fail-closed — do not
        # steal a lock we cannot verify).
        return True


def lock_path(login: object, magic: object, lock_dir: Path | None = None) -> Path:
    return Path(lock_dir or _LOCK_DIR) / f"hermes_instance_{login}_{magic}.lock"


def acquire_single_instance_lock(login: object, magic: object, lock_dir: Path | None = None) -> Path:
    """Acquire the lock or raise SingleInstanceError. Returns the lock path."""
    path = lock_path(login, magic, lock_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            owner_pid = int(payload.get("pid") or 0)
        except Exception:
            owner_pid = 0
        if owner_pid and owner_pid != os.getpid() and _pid_alive(owner_pid):
            raise SingleInstanceError(
                f"HERMES already running for login={login} magic={magic} (pid={owner_pid}) — refusing to start"
            )
        log.warning("[SINGLE_INSTANCE] stale_lock_reclaimed path=%s dead_pid=%s", path, owner_pid)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "login": str(login),
                "magic": str(magic),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    log.info("[SINGLE_INSTANCE] lock_acquired path=%s pid=%s", path, os.getpid())
    return path


def release_single_instance_lock(login: object, magic: object, lock_dir: Path | None = None) -> None:
    try:
        path = lock_path(login, magic, lock_dir)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("pid") or 0) == os.getpid():
                path.unlink()
    except Exception:
        pass
