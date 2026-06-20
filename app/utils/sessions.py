from __future__ import annotations

from datetime import datetime


def trading_session(dt: datetime) -> str:
    hour = dt.hour
    if 0 <= hour < 7:
        return "Asia"
    if 7 <= hour < 13:
        return "London"
    if 13 <= hour < 21:
        return "New York"
    return "Asia"
