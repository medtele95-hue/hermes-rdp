from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.config import Settings


STRATEGY = "QUANT_STATISTICAL_PULLBACK"


def evaluate(symbol: str, frames: dict[str, Any], context: dict | None, settings: Settings) -> dict:
    if not settings.hermes_quant_strategy_enabled:
        return _wait(symbol, "QUANT_STRATEGY_DISABLED")
    if str(settings.hermes_quant_strategy_role or "").upper() != "ENTRY_STRATEGY":
        return _wait(symbol, "QUANT_STRATEGY_ROLE_NOT_ENTRY")
    df = _closed_frame(frames.get("M5"))
    reg_period = int(settings.hermes_quant_reg_period)
    z_period = int(settings.hermes_quant_z_period)
    stdev_period = int(settings.hermes_quant_stdev_period)
    required = max(reg_period, z_period, stdev_period)
    if len(df) < required:
        return _wait(symbol, "QUANT_NOT_ENOUGH_CLOSED_CANDLES")

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < required:
        return _wait(symbol, "QUANT_NO_CLOSED_CANDLES")

    regression = linear_regression(closes.tail(reg_period).tolist())
    z_stats = z_score(closes.tail(z_period).tolist())
    stdev_vol = sample_stdev(closes.tail(stdev_period).tolist())
    if z_stats["stdev"] <= 0 or stdev_vol <= 0:
        return _wait(symbol, "QUANT_INVALID_STDEV", regression, z_stats, stdev_vol)

    signal = "WAIT"
    if regression["slope"] > 0 and regression["r2"] >= settings.hermes_quant_min_r2 and z_stats["z"] <= -settings.hermes_quant_z_entry:
        signal = "BUY"
    elif regression["slope"] < 0 and regression["r2"] >= settings.hermes_quant_min_r2 and z_stats["z"] >= settings.hermes_quant_z_entry:
        signal = "SELL"
    if signal == "WAIT":
        reason = "QUANT_R2_BELOW_MIN" if regression["r2"] < settings.hermes_quant_min_r2 else "QUANT_PULLBACK_CONDITION_NOT_MET"
        return _wait(symbol, reason, regression, z_stats, stdev_vol)

    entry = float(closes.iloc[-1])
    stop_distance = stdev_vol * settings.hermes_quant_sl_stdev_mult
    if signal == "BUY":
        sl = entry - stop_distance
        tp = entry + abs(entry - sl) * settings.hermes_quant_min_rr
    else:
        sl = entry + stop_distance
        tp = entry - abs(sl - entry) * settings.hermes_quant_min_rr
    rr = reward_risk(signal, entry, sl, tp)
    valid = _valid_sl_tp(signal, entry, sl, tp) and rr is not None and rr >= settings.hermes_quant_min_rr
    if not valid:
        return _wait(symbol, "QUANT_INVALID_SL_TP_RR", regression, z_stats, stdev_vol)

    score = quant_score(signal, regression["slope"], regression["r2"], z_stats["z"], stdev_vol, rr, settings)
    grade = _grade(score)
    if score < settings.hermes_quant_min_score:
        signal = "WAIT"
        reason = "QUANT_SCORE_BELOW_MIN"
    else:
        reason = "QUANT_STATISTICAL_PULLBACK_BUY" if signal == "BUY" else "QUANT_STATISTICAL_PULLBACK_SELL"
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": signal,
        "direction": signal,
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "risk_reward": round(rr, 6),
        "reward_risk": round(rr, 6),
        "confidence": round(score / 100.0, 4),
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "m15_confirmation_status": "PASS",
        "m1_trigger_status": "PASS",
        "m15_confirmation_reason": "QUANT_48H_EXPLORATION",
        "m1_trigger_reason": "QUANT_48H_EXPLORATION",
        "big_setup_grade": grade,
        "setup_hunter_grade": grade,
        "grade": grade,
        "quant_slope": regression["slope"],
        "quant_r2": regression["r2"],
        "quant_z_score": z_stats["z"],
        "quant_mean": z_stats["mean"],
        "quant_stdev": z_stats["stdev"],
        "quant_signal": signal,
        "quant_score": score,
        "quant_reason": reason,
        "reason": reason,
    }


def linear_regression(values: list[float]) -> dict[str, float]:
    y = [float(item) for item in values]
    n = len(y)
    if n == 0:
        return {"slope": 0.0, "intercept": 0.0, "r2": 0.0}
    x = list(range(n))
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    denominator = n * sum_x2 - sum_x * sum_x
    slope = 0.0 if denominator == 0 else (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    mean_y = sum_y / n
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y))
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    r2 = 0.0 if ss_tot == 0 else 1 - ss_res / ss_tot
    return {"slope": slope, "intercept": intercept, "r2": max(0.0, min(1.0, r2))}


def z_score(values: list[float]) -> dict[str, float]:
    clean = [float(item) for item in values]
    if not clean:
        return {"mean": 0.0, "stdev": 0.0, "z": 0.0}
    mean = sum(clean) / len(clean)
    stdev = sample_stdev(clean)
    z = 0.0 if stdev <= 0 else (clean[-1] - mean) / stdev
    return {"mean": mean, "stdev": stdev, "z": z}


def sample_stdev(values: list[float]) -> float:
    clean = [float(item) for item in values]
    if len(clean) < 2:
        return 0.0
    mean = sum(clean) / len(clean)
    return math.sqrt(sum((item - mean) ** 2 for item in clean) / len(clean))


def reward_risk(direction: str, entry: float, sl: float, tp: float) -> float | None:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if not math.isfinite(rr):
        return None
    if not _valid_sl_tp(direction, entry, sl, tp):
        return None
    return rr


def quant_score(signal: str, slope: float, r2: float, z: float, stdev_vol: float, rr: float | None, settings: Settings) -> int:
    score = 0
    if (signal == "BUY" and slope > 0) or (signal == "SELL" and slope < 0):
        score += 30
    if r2 >= settings.hermes_quant_min_r2:
        score += 25
    if (signal == "BUY" and z <= -settings.hermes_quant_z_entry) or (signal == "SELL" and z >= settings.hermes_quant_z_entry):
        score += 25
    if stdev_vol > 0:
        score += 10
    if rr is not None and rr >= settings.hermes_quant_min_rr:
        score += 10
    return max(0, min(100, score))


def _closed_frame(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    df = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if df.empty or "close" not in df:
        return pd.DataFrame()
    if len(df) > 1:
        return df.iloc[:-1].copy()
    return df.copy()


def _valid_sl_tp(direction: str, entry: float, sl: float, tp: float) -> bool:
    if direction == "BUY":
        return sl < entry < tp
    if direction == "SELL":
        return tp < entry < sl
    return False


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def _wait(symbol: str, reason: str, regression: dict | None = None, z_stats: dict | None = None, stdev_vol: float | None = None) -> dict:
    regression = regression or {"slope": None, "r2": None}
    z_stats = z_stats or {"mean": None, "stdev": None, "z": None}
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": "WAIT",
        "direction": "WAIT",
        "entry": None,
        "sl": None,
        "tp": None,
        "risk_reward": None,
        "confidence": 0.0,
        "quant_slope": regression.get("slope"),
        "quant_r2": regression.get("r2"),
        "quant_z_score": z_stats.get("z"),
        "quant_mean": z_stats.get("mean"),
        "quant_stdev": stdev_vol if stdev_vol is not None else z_stats.get("stdev"),
        "quant_signal": "WAIT",
        "quant_score": 0,
        "quant_reason": reason,
        "reason": reason,
    }
