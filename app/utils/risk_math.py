from __future__ import annotations


def kelly_fraction(probability: float, reward_risk: float) -> float:
    if reward_risk <= 0:
        return 0.0
    kelly = probability - ((1 - probability) / reward_risk)
    return max(0.0, kelly)


def fractional_kelly(probability: float, reward_risk: float, fraction: float = 0.25) -> float:
    return kelly_fraction(probability, reward_risk) * fraction


def reward_risk(entry: float | None, sl: float | None, tp: float | None, signal: str) -> float:
    if entry is None or sl is None or tp is None:
        return 0.0
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return reward / risk
