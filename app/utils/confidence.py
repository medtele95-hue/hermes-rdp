from __future__ import annotations


def normalize_confidence(value: object) -> float:
    if value is None:
        return 0.0
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw <= 1.0:
        return round(raw * 100.0, 10)
    return raw
