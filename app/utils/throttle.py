from __future__ import annotations

from datetime import datetime, timezone

_state: dict[str, datetime] = {}

THROTTLE_SECONDS = 300


def should_emit(key: str, interval_seconds: int = THROTTLE_SECONDS) -> bool:
    now = datetime.now(timezone.utc)
    last = _state.get(key)
    if last is None or (now - last).total_seconds() >= interval_seconds:
        _state[key] = now
        return True
    return False
