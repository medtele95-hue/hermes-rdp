# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — PIN authentication, lockout, and rate limiting.

The PIN gates every ACTION endpoint only (never the read endpoints — those
stay open on the Tailscale-only network, per mission spec §3.4). Stored as
a salted PBKDF2-HMAC-SHA256 hash in dashboard/.env, never in plaintext.
5 wrong attempts locks all actions for 15 minutes + a Telegram alert
(caller's responsibility — see app.dashboard_api.audit). A separate
token-bucket limits actions to 1 per 5 seconds regardless of PIN
correctness, independent of the lockout counter.

First-boot UX: if dashboard/.env has no PIN configured yet, a random
6-digit PIN is generated, hashed, and the ONE-TIME plaintext is returned to
the caller (server startup log only — never written to disk in plaintext,
never served over HTTP).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_ENV_FILE = REPO_ROOT / "dashboard" / ".env"

_PBKDF2_ITERATIONS = 200_000
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_SECONDS = 15 * 60
_RATE_LIMIT_SECONDS = 5.0

_lock = threading.Lock()
_state = {
    "failures": 0,
    "locked_until": 0.0,
    "last_action_at": 0.0,
}


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


def _hash_pin(pin: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return digest.hex()


def ensure_pin_configured(path: Path | None = None) -> str | None:
    """Call once at server startup. Returns the plaintext PIN if one was
    just generated (log it once, prominently, then discard) — None if a PIN
    was already configured (nothing new to show)."""
    if path is None:
        path = DASHBOARD_ENV_FILE
    cfg = _load_env(path)
    if cfg.get("DASHBOARD_PIN_HASH") and cfg.get("DASHBOARD_PIN_SALT"):
        return None
    pin = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_bytes(16)
    pin_hash = _hash_pin(pin, salt)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = [
            l for l in path.read_text(encoding="utf-8").splitlines()
            if not l.startswith("DASHBOARD_PIN_HASH=") and not l.startswith("DASHBOARD_PIN_SALT=")
        ]
    existing_lines += [f"DASHBOARD_PIN_HASH={pin_hash}", f"DASHBOARD_PIN_SALT={salt.hex()}"]
    path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
    return pin


def is_locked_out() -> tuple[bool, float]:
    with _lock:
        remaining = _state["locked_until"] - time.monotonic()
        return remaining > 0, max(0.0, remaining)


def verify_pin(pin: str, path: Path | None = None) -> bool:
    if path is None:
        path = DASHBOARD_ENV_FILE
    cfg = _load_env(path)
    stored_hash = cfg.get("DASHBOARD_PIN_HASH", "")
    stored_salt_hex = cfg.get("DASHBOARD_PIN_SALT", "")
    if not stored_hash or not stored_salt_hex:
        return False
    try:
        salt = bytes.fromhex(stored_salt_hex)
    except ValueError:
        return False
    candidate = _hash_pin(pin, salt)
    return hmac.compare_digest(candidate, stored_hash)


def check_and_record_attempt(pin: str) -> tuple[bool, str]:
    """Verify a PIN and update the lockout counter. Returns (ok, reason)."""
    locked, remaining = is_locked_out()
    if locked:
        return False, f"LOCKED_OUT_{int(remaining)}s"
    ok = verify_pin(pin)
    with _lock:
        if ok:
            _state["failures"] = 0
            return True, "OK"
        _state["failures"] += 1
        if _state["failures"] >= _LOCKOUT_THRESHOLD:
            _state["locked_until"] = time.monotonic() + _LOCKOUT_SECONDS
            _state["failures"] = 0
            return False, "LOCKOUT_TRIGGERED"
        return False, f"WRONG_PIN_{_state['failures']}_OF_{_LOCKOUT_THRESHOLD}"


def check_rate_limit() -> tuple[bool, float]:
    """Returns (allowed, retry_after_seconds). Independent of PIN/lockout —
    throttles the action endpoints themselves, 1 per 5 seconds."""
    with _lock:
        now = time.monotonic()
        elapsed = now - _state["last_action_at"]
        if elapsed < _RATE_LIMIT_SECONDS:
            return False, round(_RATE_LIMIT_SECONDS - elapsed, 1)
        _state["last_action_at"] = now
        return True, 0.0


def reset_state_for_tests() -> None:
    with _lock:
        _state["failures"] = 0
        _state["locked_until"] = 0.0
        _state["last_action_at"] = 0.0
