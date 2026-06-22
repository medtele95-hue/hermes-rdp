from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any

from app.logger import log

_state: dict[str, datetime] = {}
_event_state: dict[str, tuple[datetime, Any]] = {}
_lock = RLock()

THROTTLE_SECONDS = 300


def should_emit(key: str, interval_seconds: int = THROTTLE_SECONDS) -> bool:
    now = datetime.now(timezone.utc)
    with _lock:
        last = _state.get(key)
        if last is None or (now - last).total_seconds() >= interval_seconds:
            _state[key] = now
            return True
        return False


def log_event_throttled(
    key: str,
    message: str,
    min_interval_seconds: int = 60,
    state: Any = None,
) -> bool:
    """Log an INFO event on first use, interval expiry, or a state change."""
    now = datetime.now(timezone.utc)
    with _lock:
        previous = _event_state.get(key)
        should_log = (
            previous is None
            or previous[1] != state
            or (now - previous[0]).total_seconds() >= min_interval_seconds
        )
        if should_log:
            _event_state[key] = (now, state)
    if should_log:
        log.info(message)
    return should_log
