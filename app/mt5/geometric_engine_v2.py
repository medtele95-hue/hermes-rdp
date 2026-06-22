"""Pure geometric analysis primitives for HERMES geometric engine v2.

This module has no MT5 dependency and cannot route or size orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from app.logger import log, utc_now_iso
from app.utils.throttle import log_event_throttled


MIN_GEOMETRIC_QUALITY = 50.0
PRZ_WINDOW_ATR_MULTIPLIER = 0.5
RATIO_ERROR_CAP = 2.0
DEFAULT_RATIO_TOLERANCE = 0.10
MAX_CAPITAL_RISK_PCT = 1.0
SPREAD_KILLER_ATR_MULTIPLIER = 2.0
BTC_WEEKEND_SPREAD_ATR_MULTIPLIER = 0.5
ORDER_FLOW_OVERRIDE_SCORE = 90.0
MTFA_TREND_STRENGTH_LIMIT = 0.7
MIN_EXECUTION_GEOMETRY = 50.0

RATIO_WEIGHTS = {
    "AB_XA": 0.25,
    "BC_AB": 0.20,
    "CD_BC": 0.25,
    "D_XA": 0.30,
    "D_XC": 0.30,
    "D_OX": 0.30,
}

HARMONIC_SPECS = {
    "GARTLEY": {"AB_XA": (0.618, 0.618), "BC_AB": (0.382, 0.886),
                "CD_BC": (1.272, 1.618), "D_XA": (0.786, 0.786)},
    "BAT": {"AB_XA": (0.382, 0.500), "BC_AB": (0.382, 0.886),
            "CD_BC": (1.618, 2.618), "D_XA": (0.886, 0.886)},
    "BUTTERFLY": {"AB_XA": (0.786, 0.786), "BC_AB": (0.382, 0.886),
                  "CD_BC": (1.618, 2.618), "D_XA": (1.270, 1.618)},
    "CRAB": {"AB_XA": (0.382, 0.618), "BC_AB": (0.382, 0.886),
             "CD_BC": (2.618, 3.618), "D_XA": (1.618, 1.902)},
    "SHARK": {"AB_XA": (1.130, 1.618), "BC_AB": (1.618, 2.240),
              "CD_BC": (0.886, 1.130), "D_OX": (0.886, 1.130)},
    "CYPHER": {"AB_XA": (0.382, 0.618), "BC_AB": (1.130, 1.414),
               "CD_BC": (1.272, 2.000), "D_XC": (0.786, 0.786)},
}

PATTERN_PRZ_RATIOS = {
    "GARTLEY": {"retracements": (0.786,), "extensions": (1.272, 1.618)},
    "BAT": {"retracements": (0.886,), "extensions": (1.618, 2.618)},
    "BUTTERFLY": {"retracements": (1.270, 1.618), "extensions": (1.618, 2.618)},
    "CRAB": {"retracements": (1.618, 1.902), "extensions": (2.618, 3.618)},
    "SHARK": {"retracements": (0.886, 1.130), "extensions": (0.886, 1.130)},
    "CYPHER": {"retracements": (0.786,), "extensions": (1.272, 2.000)},
}

MODE_THRESHOLDS = {
    "SHADOW": (0.0, 0.0, 0.0, math.inf),
    "BONUS": (50.0, 50.0, 30.0, 1.5),
    "SOFT_CONFIRM": (60.0, 60.0, 40.0, 1.0),
    "EXECUTION_FILTER": (70.0, 65.0, 50.0, 0.75),
    "LIVE": (75.0, 70.0, 55.0, 0.50),
}

CONFLUENCE_WEIGHTS = {
    "geometry": 0.25,
    "order_flow": 0.20,
    "structure": 0.20,
    "environment": 0.10,
    "momentum": 0.15,
    "gann": 0.10,
}


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    type: str
    strength: float


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score(payload: Mapping[str, object] | None, *keys: str) -> float:
    source = payload or {}
    for key in keys:
        if source.get(key) is not None:
            return max(0.0, min(100.0, _number(source.get(key))))
    return 0.0


def _is_valid_atr(value: object) -> bool:
    atr_value = _number(value, float("nan"))
    return math.isfinite(atr_value) and atr_value > 0.0


def _calculate_atr_from_candles(recent_candles: list[dict] | None, period: int = 14) -> float | None:
    if not recent_candles or period <= 0:
        return None
    rows: list[dict] = [row for row in recent_candles if isinstance(row, dict)]
    if len(rows) < period + 1:
        return None
    try:
        highs = [float(row["high"]) for row in rows]
        lows = [float(row["low"]) for row in rows]
        closes = [float(row["close"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(value) for value in highs + lows + closes):
        return None
    true_ranges: list[float] = []
    previous_close = closes[0]
    for index in range(1, len(rows)):
        high = highs[index]
        low = lows[index]
        tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(tr)
        previous_close = closes[index]
    if len(true_ranges) < period:
        return None
    atr_value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr_value = ((atr_value * (period - 1)) + tr) / period
    return atr_value


def _resolve_atr_metrics(
    atr: float | None,
    recent_candles: list[dict] | None = None,
    *,
    period: int = 14,
) -> dict:
    raw_atr = _number(atr, float("nan"))
    if _is_valid_atr(raw_atr):
        return {"atr": float(raw_atr), "atr_status": "VALID", "atr_source": "mt5"}
    fallback_atr = _calculate_atr_from_candles(recent_candles, period=period)
    if fallback_atr is not None and _is_valid_atr(fallback_atr):
        return {"atr": float(fallback_atr), "atr_status": "VALID", "atr_source": "candles"}
    if recent_candles:
        return {"atr": None, "atr_status": "INVALID", "atr_source": "fallback"}
    return {"atr": None, "atr_status": "UNAVAILABLE", "atr_source": "none"}


def detect_swings(
    highs: list[float], lows: list[float], closes: list[float],
    atr: float, min_swing_atr: float = 1.0, lookback: int = 5,
) -> list[Swing]:
    """Return alternating, ATR-qualified centered-window swing points."""
    if len(highs) != len(lows) or len(highs) != len(closes):
        return []
    if atr <= 0 or min_swing_atr <= 0 or lookback < 3 or lookback % 2 == 0:
        return []
    half_window = lookback // 2
    candidates: list[Swing] = []
    for index in range(half_window, len(highs) - half_window):
        high_window = highs[index - half_window:index + half_window + 1]
        low_window = lows[index - half_window:index + half_window + 1]
        if highs[index] == max(high_window):
            strength = (highs[index] - min(low_window)) / atr
            candidates.append(Swing(index, float(highs[index]), "HIGH", round(strength, 6)))
        if lows[index] == min(low_window):
            strength = (max(high_window) - lows[index]) / atr
            candidates.append(Swing(index, float(lows[index]), "LOW", round(strength, 6)))

    alternating: list[Swing] = []
    for candidate in sorted(candidates, key=lambda item: (item.index, item.type)):
        if candidate.strength < min_swing_atr:
            continue
        if alternating and candidate.type == alternating[-1].type:
            more_extreme = (
                candidate.price > alternating[-1].price
                if candidate.type == "HIGH"
                else candidate.price < alternating[-1].price
            )
            if more_extreme:
                alternating[-1] = candidate
            continue
        if alternating and abs(candidate.price - alternating[-1].price) < min_swing_atr * atr:
            continue
        alternating.append(candidate)
    return alternating


def calculate_ratio_error(
    actual_ratios: dict, specs: dict, tolerance: dict,
) -> tuple[float, dict]:
    """Return weighted normalized ratio error and per-ratio diagnostics."""
    weighted_error = 0.0
    weight_total = 0.0
    details: dict[str, dict] = {}
    for ratio_name, bounds in specs.items():
        if ratio_name not in actual_ratios:
            return RATIO_ERROR_CAP, {"missing_ratio": ratio_name}
        actual = _number(actual_ratios[ratio_name])
        lower, upper = float(bounds[0]), float(bounds[1])
        center = (lower + upper) / 2.0
        denominator = max(abs(center), 1e-12)
        error = abs(actual - center) / denominator
        if lower != upper and actual < lower:
            error += (lower - actual) / denominator
        elif lower != upper and actual > upper:
            error += (actual - upper) / denominator
        ratio_tolerance = max(_number(tolerance.get(ratio_name), DEFAULT_RATIO_TOLERANCE), 1e-12)
        normalized = min(error / ratio_tolerance, RATIO_ERROR_CAP)
        weight = RATIO_WEIGHTS.get(ratio_name, 0.0)
        weighted_error += weight * normalized
        weight_total += weight
        details[ratio_name] = {
            "actual": actual,
            "ideal": center,
            "error": error,
            "normalized_error": normalized,
            "weight": weight,
        }
    if weight_total <= 0:
        return RATIO_ERROR_CAP, details
    return weighted_error / weight_total, details


def classify_harmonic_pattern(
    X: float, A: float, B: float, C: float, D: float,
    atr: float, tolerance_map: dict,
) -> dict | None:
    """Classify XABCD using graduated ratio error instead of binary hits."""
    xa, ab, bc, cd, xc = abs(A - X), abs(B - A), abs(C - B), abs(D - C), abs(C - X)
    if atr <= 0 or min(xa, ab, bc, cd, xc) <= 0:
        return None
    actual_ratios = {
        "AB_XA": ab / xa,
        "BC_AB": bc / ab,
        "CD_BC": cd / bc,
        "D_XA": abs(D - A) / xa,
        "D_XC": abs(D - C) / xc,
        "D_OX": abs(D - X) / xa,
    }
    best: dict | None = None
    for pattern_name, specs in HARMONIC_SPECS.items():
        error, details = calculate_ratio_error(actual_ratios, specs, tolerance_map)
        quality = max(0.0, 100.0 * (1.0 - error))
        candidate = {
            "pattern": pattern_name,
            "quality": round(quality, 4),
            "direction": "BULLISH" if D < C else "BEARISH",
            "ratios": {key: round(actual_ratios[key], 6) for key in specs},
            "ratio_error": round(error, 6),
            "ratio_details": details,
        }
        if best is None or candidate["quality"] > best["quality"]:
            best = candidate
    if best is None or best["quality"] < MIN_GEOMETRIC_QUALITY:
        return None
    return best


def _closest(candidates: list[float], target: float) -> float:
    return min(candidates, key=lambda value: abs(value - target))


def calculate_prz_cluster(
    X: float, A: float, B: float, C: float, D: float,
    current_price: float, atr: float, pattern_name: str,
) -> dict:
    """Build a pattern-filtered potential reversal zone cluster."""
    if atr <= 0 or pattern_name.upper() not in PATTERN_PRZ_RATIOS:
        return {"levels": [], "hits": 0, "density_score": 0.0, "proximity_score": 0.0,
                "prz_center": None, "distance_atr": math.inf, "prz_strength": 0.0}
    ratios = PATTERN_PRZ_RATIOS[pattern_name.upper()]
    xa = abs(A - X)
    bc = abs(C - B)
    ab = abs(B - A)
    xa_direction = 1.0 if A >= X else -1.0
    bc_direction = 1.0 if C >= B else -1.0
    retracement_levels = [A - xa_direction * xa * ratio for ratio in ratios["retracements"]]
    extension_levels = [C - bc_direction * bc * ratio for ratio in ratios["extensions"]]
    abcd_levels = [C - bc_direction * ab * projection for projection in (1.0, 1.272, 1.618)]
    theoretical_candidates = retracement_levels + extension_levels
    theoretical_d = _closest(theoretical_candidates, D) if theoretical_candidates else D
    levels = retracement_levels + extension_levels + abcd_levels + [theoretical_d]
    window = PRZ_WINDOW_ATR_MULTIPLIER * atr
    hit_levels = [level for level in levels if abs(current_price - level) <= window]
    total_levels = len(levels)
    density = (len(hit_levels) / total_levels) * 100.0 if total_levels else 0.0
    proximity = (
        sum(1.0 - abs(current_price - level) / window for level in hit_levels) / total_levels * 100.0
        if total_levels and window > 0 else 0.0
    )
    prz_center = sum(hit_levels) / len(hit_levels) if hit_levels else sum(levels) / total_levels
    distance_atr = abs(current_price - prz_center) / atr
    if distance_atr <= 0.25:
        penalty = 0.0
    elif distance_atr <= 0.50:
        penalty = 0.15
    elif distance_atr <= 1.00:
        penalty = 0.35
    else:
        penalty = 1.0
    strength = (density * 0.6 + proximity * 0.4) * (1.0 - penalty)
    return {
        "levels": [round(level, 6) for level in levels],
        "hit_levels": [round(level, 6) for level in hit_levels],
        "hits": len(hit_levels),
        "density_score": round(density, 4),
        "proximity_score": round(proximity, 4),
        "prz_center": round(prz_center, 6),
        "distance_atr": round(distance_atr, 6),
        "distance_penalty": penalty,
        "prz_strength": round(max(0.0, min(100.0, strength)), 4),
    }


def geometric_score(
    pattern: dict | None, prz: dict | None,
    htf_alignment: float, gann_confluence: float, vwap_score: float,
    mode: str,
) -> dict:
    """Combine geometric evidence and apply mode-specific quality requirements."""
    normalized_mode = str(mode or "SHADOW").upper()
    if normalized_mode == "EXEC_FILTER":
        normalized_mode = "EXECUTION_FILTER"
    if normalized_mode not in MODE_THRESHOLDS:
        normalized_mode = "SHADOW"
    harmonic_quality = _score(pattern, "quality", "harmonic_quality", "harmonic_score")
    prz_strength = _score(prz, "prz_strength", "strength")
    distance_atr = _number((prz or {}).get("distance_atr"), math.inf)
    score = (
        harmonic_quality * 0.40
        + prz_strength * 0.35
        + max(0.0, min(100.0, htf_alignment)) * 0.15
        + max(0.0, min(100.0, gann_confluence)) * 0.05
        + max(0.0, min(100.0, vwap_score)) * 0.05
    )
    min_geo, min_quality, min_prz, max_distance = MODE_THRESHOLDS[normalized_mode]
    blockers = []
    if score < min_geo:
        blockers.append("GEO_SCORE_BELOW_MODE_MIN")
    if harmonic_quality < min_quality:
        blockers.append("HARMONIC_QUALITY_BELOW_MODE_MIN")
    if prz_strength < min_prz:
        blockers.append("PRZ_STRENGTH_BELOW_MODE_MIN")
    if distance_atr > max_distance:
        blockers.append("PRZ_DISTANCE_ABOVE_MODE_MAX")
    if score >= 80.0:
        decision = "SETUP_HUNTER"
    elif score >= 70.0:
        decision = "DEMO"
    elif score >= 60.0:
        decision = "WAIT"
    elif normalized_mode in {"EXECUTION_FILTER", "LIVE"}:
        decision = "BLOCK"
    else:
        decision = "WAIT"
    if blockers and normalized_mode in {"EXECUTION_FILTER", "LIVE"}:
        decision = "BLOCK"
    return {
        "mode": normalized_mode,
        "score": round(score, 4),
        "decision": decision,
        "passes_mode": not blockers,
        "blockers": blockers,
        "harmonic_quality": harmonic_quality,
        "prz_strength": prz_strength,
        "distance_atr": distance_atr,
    }


def sigmoid_weight(score: float) -> float:
    if score < 25.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-0.1 * (score - 40.0)))


def resolve_spread_metrics(
    spread_value: float | None,
    atr: float,
    *,
    spread_points: float | None = None,
    point: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    max_spread: float | None = None,
    atr_status: str | None = None,
    atr_source: str | None = None,
) -> dict:
    """Normalize spread inputs into price units without mixing point units into ATR checks."""
    spread_input = _number(spread_value, 0.0)
    spread_points_value = _number(spread_points, float("nan"))
    point_value = _number(point, 0.0)
    bid_value = _number(bid, float("nan"))
    ask_value = _number(ask, float("nan"))

    has_tick = math.isfinite(bid_value) and math.isfinite(ask_value) and ask_value >= bid_value and ask_value > 0.0
    has_points = math.isfinite(spread_points_value)

    if has_tick:
        spread_price = abs(ask_value - bid_value)
        raw_spread_points = spread_price / point_value if point_value > 0.0 else spread_points_value if has_points else spread_input
    elif has_points and point_value > 0.0:
        raw_spread_points = spread_points_value
        spread_price = spread_points_value * point_value
    elif point_value > 0.0 and math.isfinite(spread_input):
        raw_spread_points = spread_input
        spread_price = spread_input * point_value
    elif has_points:
        raw_spread_points = spread_points_value
        spread_price = spread_points_value
    else:
        raw_spread_points = spread_input
        spread_price = spread_input

    spread_price = max(0.0, float(spread_price))
    atr_value = _number(atr, float("nan"))
    atr_is_valid = math.isfinite(atr_value) and atr_value > 0.0
    spread_to_atr = spread_price / atr_value if atr_is_valid else None
    broker_spread_status = "OK"
    if max_spread is not None:
        broker_limit = _number(max_spread, 0.0)
        if math.isfinite(raw_spread_points) and raw_spread_points > broker_limit:
            broker_spread_status = "MAX_SPREAD"

    if atr_is_valid:
        final_spread_gate = "BLOCK" if spread_price > SPREAD_KILLER_ATR_MULTIPLIER * atr_value else "PASS"
    else:
        final_spread_gate = "BLOCK" if broker_spread_status != "OK" else "ATR_UNAVAILABLE"
    return {
        "spread_input": spread_input,
        "raw_spread_points": None if not math.isfinite(raw_spread_points) else float(raw_spread_points),
        "point": point_value if point_value > 0.0 else None,
        "bid": bid_value if math.isfinite(bid_value) else None,
        "ask": ask_value if math.isfinite(ask_value) else None,
        "spread_price": spread_price,
        "spread_to_atr": spread_to_atr,
        "atr": None if not atr_is_valid else float(atr_value),
        "atr_status": (atr_status or ("VALID" if atr_is_valid else "INVALID")).upper(),
        "atr_source": (atr_source or ("mt5" if atr_is_valid else "none")).lower(),
        "broker_spread_status": broker_spread_status,
        "final_spread_gate": final_spread_gate,
        "max_spread": None if max_spread is None else _number(max_spread, 0.0),
    }


def final_trade_gate(
    geometric_result: dict, order_flow: dict,
    smc_result: dict, mtfa_result: dict,
    spread_usd: float, atr: float,
    session: str, symbol: str,
    capital_risk_pct: float, mode: str,
    *,
    strategy: str | None = None,
    spread_points: float | None = None,
    point: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    max_spread: float | None = None,
    cycle_id: str | None = None,
    momentum_result: dict | None = None,
    recent_candles: list[dict] | None = None,
) -> dict:
    """Return an analysis-only final verdict; this function never routes orders."""
    normalized_mode = str(mode or "SHADOW").upper()
    geo = _score(geometric_result, "score", "geometric_score")
    flow = _score(order_flow, "score", "order_flow_score", "confidence")
    smc = _score(smc_result, "score", "smc_score")
    mtfa = _score(mtfa_result, "score", "mtfa_score")
    trend_strength = _number(mtfa_result.get("trend_strength"))
    smc_status = str(smc_result.get("status") or smc_result.get("smc_status") or "").upper()
    mtfa_status = str(mtfa_result.get("status") or mtfa_result.get("mtfa_status") or "").upper()
    canonical_symbol = str(symbol or "").upper().rstrip("#")
    normalized_session = str(session or "").upper()
    atr_metrics = _resolve_atr_metrics(atr, recent_candles=recent_candles)
    resolved_atr = atr_metrics["atr"] if atr_metrics["atr"] is not None else 0.0
    spread_metrics = resolve_spread_metrics(
        spread_usd,
        resolved_atr,
        spread_points=spread_points,
        point=point,
        bid=bid,
        ask=ask,
        max_spread=max_spread,
        atr_status=str(atr_metrics["atr_status"]),
        atr_source=str(atr_metrics["atr_source"]),
    )
    spread_price = float(spread_metrics["spread_price"])
    spread_to_atr_raw = spread_metrics["spread_to_atr"]
    spread_to_atr = float(spread_to_atr_raw) if spread_to_atr_raw is not None else None
    broker_spread_status = str(spread_metrics["broker_spread_status"])
    atr_status = str(spread_metrics["atr_status"])
    atr_source = str(spread_metrics["atr_source"])
    atr_value = spread_metrics["atr"]

    hard_blocks: list[str] = []
    if atr_value is not None and spread_price > SPREAD_KILLER_ATR_MULTIPLIER * float(atr_value):
        hard_blocks.append("SPREAD_KILLER")
    elif atr_value is None and broker_spread_status != "OK":
        hard_blocks.append("SPREAD_TOO_HIGH")
    if normalized_session == "WEEKEND" and canonical_symbol != "BTCUSD":
        hard_blocks.append("WEEKEND_CLOSED")
    if normalized_session == "WEEKEND" and canonical_symbol == "BTCUSD" and atr_value is not None and spread_price > BTC_WEEKEND_SPREAD_ATR_MULTIPLIER * float(atr_value):
        hard_blocks.append("BTC_WEEKEND_SPREAD")
    if capital_risk_pct > MAX_CAPITAL_RISK_PCT:
        hard_blocks.append("RISK_EXCEEDED")
    if smc_status == "STRONG_FAIL" and flow < ORDER_FLOW_OVERRIDE_SCORE:
        hard_blocks.append("SMC_STRONG_FAIL_NO_FLOW")
    if mtfa_status == "STRONG_FAIL" and trend_strength > MTFA_TREND_STRENGTH_LIMIT:
        hard_blocks.append("MTFA_CONTRA_TREND")
    if normalized_mode in {"EXEC_FILTER", "EXECUTION_FILTER", "LIVE"} and geo < MIN_EXECUTION_GEOMETRY:
        hard_blocks.append("NO_GEOMETRY")

    if spread_to_atr is not None:
        spread_score = max(0.0, 100.0 - spread_to_atr * 50.0)
    else:
        spread_score = 100.0 if broker_spread_status == "OK" else 0.0
    session_score = 100.0 if normalized_session in {"LONDON", "NY", "NEW_YORK", "OVERLAP"} else 60.0 if normalized_session == "ASIA" else 30.0 if normalized_session == "WEEKEND" else 50.0
    environment = (spread_score + session_score) / 2.0
    structure = (smc + mtfa) / 2.0
    momentum = (
        _score(momentum_result, "momentum_score", "score")
        if momentum_result is not None
        else _score(order_flow, "volume_score", "volume")
    )
    gann = _score(geometric_result, "gann_confluence", "gann_score")
    components = {
        "geometry": geo,
        "order_flow": flow,
        "structure": structure,
        "environment": environment,
        "momentum": momentum,
        "gann": gann,
    }
    base_score = sum(
        CONFLUENCE_WEIGHTS[name] * value * sigmoid_weight(value)
        for name, value in components.items()
    )
    strong_alignment = geo >= 70.0 and flow >= 70.0 and structure >= 70.0
    moderate_alignment = geo >= 50.0 and flow >= 50.0 and structure >= 50.0
    if momentum_result is not None:
        strong_alignment = strong_alignment and momentum >= 70.0
        moderate_alignment = moderate_alignment and momentum >= 50.0
    alignment_bonus = 15.0 if strong_alignment else 5.0 if moderate_alignment else 0.0
    conflict_penalty = 0.0
    if smc < 30.0 and flow > 80.0:
        conflict_penalty += 20.0
    if geo > 80.0 and environment < 30.0:
        conflict_penalty += 15.0
    if momentum_result and momentum_result.get("aligned") is False:
        conflict_penalty += 10.0
    final_confluence = max(0.0, min(100.0, base_score + alignment_bonus - conflict_penalty))

    if hard_blocks:
        decision, size_multiplier = "BLOCK", 0.0
    elif final_confluence >= 90.0:
        decision, size_multiplier = ("LIVE" if normalized_mode == "LIVE" else "DEMO"), 1.0
    elif final_confluence >= 80.0:
        decision, size_multiplier = "DEMO", 1.0
    elif final_confluence >= 70.0:
        decision, size_multiplier = "DEMO", 0.5
    elif final_confluence >= 45.0:
        decision, size_multiplier = "WAIT", 0.0
    else:
        decision, size_multiplier = "BLOCK", 0.0
    result = {
        "decision": decision,
        "hard_block": bool(hard_blocks),
        "hard_block_reasons": hard_blocks,
        "final_confluence": round(final_confluence, 4),
        "size_multiplier": size_multiplier,
        "alignment_bonus": alignment_bonus,
        "conflict_penalty": conflict_penalty,
        "component_scores": {key: round(value, 4) for key, value in components.items()},
        "capital_risk_pct": capital_risk_pct,
        "spread_price": round(spread_price, 6),
        "spread_to_atr": round(spread_to_atr, 6) if spread_to_atr is not None else None,
        "broker_spread_status": broker_spread_status,
        "atr_status": atr_status,
        "atr_source": atr_source,
        "final_spread_gate": spread_metrics["final_spread_gate"],
    }
    final_reason = ",".join(hard_blocks) if hard_blocks else "SCORE_THRESHOLD"
    cycle_key = cycle_id or utc_now_iso()
    audit_state = {
        "cycle_id": cycle_key,
        "decision": decision,
        "reason": final_reason,
        "atr_status": atr_status,
        "atr_source": atr_source,
        "spread_price": result["spread_price"],
        "spread_to_atr": result["spread_to_atr"],
        "broker_spread_status": broker_spread_status,
        "smc_status": smc_status,
        "mtfa_status": mtfa_status,
    }
    atr_audit_key = f"ATR_AUDIT:{symbol}:{strategy or 'UNKNOWN'}:{cycle_key}"
    log_event_throttled(
        atr_audit_key,
        f"[ATR_AUDIT] symbol={symbol} atr={atr_value if atr_value is not None else atr} "
        f"atr_status={atr_status} source={atr_source}",
        min_interval_seconds=60,
        state={"atr": atr_value, "atr_status": atr_status, "atr_source": atr_source, "cycle_id": cycle_key},
    )
    spread_audit_key = f"SPREAD_UNIT_AUDIT:{symbol}:{strategy or 'UNKNOWN'}:{cycle_key}"
    log_event_throttled(
        spread_audit_key,
        (
            f"[SPREAD_UNIT_AUDIT] symbol={symbol} bid={spread_metrics['bid']} ask={spread_metrics['ask']} "
            f"raw_spread_points={spread_metrics['raw_spread_points']} point={spread_metrics['point']} "
            f"spread_price={result['spread_price']} atr={atr_value if atr_value is not None else None} "
            f"atr_status={atr_status} spread_to_atr={result['spread_to_atr']} max_spread={spread_metrics['max_spread']} "
            f"broker_spread_status={broker_spread_status} final_spread_gate={spread_metrics['final_spread_gate']}"
        ),
        min_interval_seconds=60,
        state={
            "bid": spread_metrics["bid"],
            "ask": spread_metrics["ask"],
            "raw_spread_points": spread_metrics["raw_spread_points"],
            "point": spread_metrics["point"],
            "spread_price": result["spread_price"],
            "atr": atr_value,
            "atr_status": atr_status,
            "spread_to_atr": result["spread_to_atr"],
            "max_spread": spread_metrics["max_spread"],
            "broker_spread_status": broker_spread_status,
            "final_spread_gate": spread_metrics["final_spread_gate"],
            "cycle_id": cycle_key,
        },
    )
    log_event_throttled(
        f"FINAL_GATE:{symbol}:{strategy or 'UNKNOWN'}:{cycle_key}:{decision}:{final_reason}",
        (
            f"[FINAL_GATE] symbol={symbol} strategy={strategy or 'UNKNOWN'} "
            f"decision={decision} reason={final_reason} components={result['component_scores']} "
            f"mode={normalized_mode} spread_price={result['spread_price']} "
            f"spread_to_atr={result['spread_to_atr']} atr_status={atr_status} "
            f"broker_spread_status={broker_spread_status} final_spread_gate={spread_metrics['final_spread_gate']} "
            f"session={normalized_session} smc_status={smc_status} mtfa_status={mtfa_status} "
            f"time={utc_now_iso()}"
        ),
        min_interval_seconds=60,
        state=audit_state,
    )
    return result
