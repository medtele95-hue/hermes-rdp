from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.config import Settings
from app.logger import log


STRATEGY = "GOLD_LIQUIDITY_HUNTER_PRO"
SOURCE = "docs/strategies/GOLD_LiquidityHunter_Algorithm.md"


@dataclass
class GoldZone:
    side: str
    top: float
    bottom: float
    pivot_price: float
    pivot_index: int
    pivot_size: float
    capacity: float
    avg_vol_at_pivot: float
    atr_at_pivot: float
    deltas: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    volume_traded: float = 0.0
    test_count: int = 0
    sweep_count: int = 0
    was_hit: bool = False
    status: str = "ACTIVE"
    health_pct: float = 100.0

    def to_payload(self) -> dict:
        return {
            "side": self.side,
            "top": self.top,
            "bottom": self.bottom,
            "pivot_price": self.pivot_price,
            "pivot_index": self.pivot_index,
            "pivot_size": self.pivot_size,
            "capacity": self.capacity,
            "volume_traded": self.volume_traded,
            "test_count": self.test_count,
            "sweep_count": self.sweep_count,
            "health_pct": self.health_pct,
            "deltas": list(self.deltas),
            "status": self.status,
        }


def evaluate(symbol: str, frames: dict[str, Any], context: dict | None, settings: Settings) -> dict:
    enabled = bool(getattr(settings, "gold_liquidity_strategy_enabled", False))
    if not enabled:
        return _wait_signal(symbol, "GOLD_LIQUIDITY_STRATEGY_DISABLED", enabled=False)
    if not _is_gold_symbol(symbol):
        return _wait_signal(symbol, "GOLD_LIQUIDITY_SYMBOL_NOT_GOLD")
    try:
        df = _closed_frame((frames or {}).get("M5"))
        result = evaluate_gold_liquidity(symbol, df, settings, context or {})
    except Exception as exc:
        result = _payload(symbol, "WAIT", "GOLD_LIQUIDITY_INTERNAL_ERROR")
        result["error"] = str(exc)
    _log_result(result)
    return _signal_from_result(symbol, result)


def evaluate_gold_liquidity(symbol: str, candles: pd.DataFrame, settings: Settings, context: dict | None = None) -> dict:
    df = _normalize_candles(candles)
    if df.empty:
        return _payload(symbol, "WAIT", "GOLD_LIQUIDITY_NO_VALID_CANDLES")
    length = int(getattr(settings, "gold_pivot_length", 15))
    if len(df) < max(120, length * 2 + 20):
        return _payload(symbol, "WAIT", "GOLD_NOT_ENOUGH_CLOSED_CANDLES")

    atr = atr14(df)
    relaxed = _relaxed_state(settings, context or {})
    avg_vol = df["volume"].rolling(50, min_periods=1).mean()
    zones: dict[str, list[GoldZone]] = {"BSL": [], "SSL": []}
    latest: dict | None = None
    start = max(1, length * 2)
    for i in range(start, len(df)):
        k = i - length
        if is_confirmed_pivot_high(df, k, length):
            zone = create_bsl_zone(df, k, float(atr.iloc[k]), float(avg_vol.iloc[k]), settings)
            _add_zone(zones["BSL"], zone, int(getattr(settings, "gold_max_zones_per_side", 10)))
        if is_confirmed_pivot_low(df, k, length):
            zone = create_ssl_zone(df, k, float(atr.iloc[k]), float(avg_vol.iloc[k]), settings)
            _add_zone(zones["SSL"], zone, int(getattr(settings, "gold_max_zones_per_side", 10)))
        row = df.iloc[i]
        range_high = float(df["high"].iloc[max(0, i - 99) : i + 1].max())
        range_low = float(df["low"].iloc[max(0, i - 99) : i + 1].min())
        range_mid = (range_high + range_low) / 2.0
        for side in ("BSL", "SSL"):
            for zone in list(zones[side]):
                if zone.status == "SWEPT":
                    continue
                candidate = process_zone_bar(zone, row, range_mid, settings, relaxed)
                if candidate:
                    latest = candidate

    nearest_bsl = _nearest_zone(zones["BSL"])
    nearest_ssl = _nearest_zone(zones["SSL"])
    if latest is None:
        return _payload(
            symbol,
            "WAIT",
            "NO_SWEEP",
            nearest_bsl=nearest_bsl,
            nearest_ssl=nearest_ssl,
        )
    payload = _payload(
        symbol,
        latest["decision"],
        latest["block_reason"],
        nearest_bsl=nearest_bsl,
        nearest_ssl=nearest_ssl,
        active_zone=latest["zone"].to_payload(),
        sweep_detected=True,
        sweep_side=latest["sweep_side"],
        reversal_signal=latest["signal"],
        zone_stars=latest["stars"],
        zone_health_pct=latest["zone"].health_pct,
        premium_discount=latest["premium_discount"],
        sweep_count=latest["zone"].sweep_count,
        test_count=latest["zone"].test_count,
        liquidity_score=latest["score"],
        directional_confirmation=latest["direction"] if latest["decision"] in {"BUY", "SELL"} else "NONE",
        delta_proxy=latest["delta_proxy"],
        rr_plan=latest["rr_plan"],
        relaxed_mode_active=relaxed["active"],
        relaxed_reason=relaxed["reason"],
        hours_without_setup=relaxed["hours_without_setup"],
        strict_threshold=int(settings.gold_min_zone_stars),
        relaxed_threshold=int(getattr(settings, "gold_liquidity_relaxed_zone_stars", 2)),
    )
    return payload


def process_zone_bar(zone: GoldZone, row: pd.Series, range_mid: float, settings: Settings, relaxed: dict | None = None) -> dict | None:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    volume = float(row["volume"])
    bar_range = high - low
    bar_delta = delta_proxy(row)
    hit = False
    if bar_range > 0:
        for idx, bounds in enumerate(zone_quadrants(zone)):
            ov = overlap_amount(high, low, bounds[1], bounds[0])
            if ov > 0:
                hit = True
                ov_ratio = ov / bar_range
                zone.deltas[idx] += bar_delta * ov_ratio
                zone.volume_traded += volume * ov_ratio
    if hit and not zone.was_hit:
        zone.test_count += 1
    zone.was_hit = hit
    swept = high > zone.top if zone.side == "BSL" else low < zone.bottom
    if not swept:
        zone.health_pct = health_pct(zone.volume_traded, zone.capacity)
        return None

    zone.sweep_count += 1
    zone.status = "SWEPT"
    signal = eval_reversal(zone, row)
    premium_discount = premium_discount_zone(zone, range_mid)
    direction = "SELL" if zone.side == "BSL" else "BUY"
    rr_plan = rr_plan_for_zone(zone, direction, close, float(getattr(settings, "gold_min_rr", 2.0)))
    rr = rr_plan.get("rr")
    stars = strength_stars(zone.volume_traded, zone.avg_vol_at_pivot, zone.pivot_size, zone.atr_at_pivot)
    score = liquidity_score(zone, signal, premium_discount, direction, stars, rr, bar_delta, volume)
    block_reason = gold_liquidity_block_reason(zone.side, direction, signal, premium_discount, stars, score, rr, settings, relaxed or {})
    decision = direction if block_reason is None else "BLOCK" if signal in set(settings.gold_allowed_signal_list) else "WAIT"
    return {
        "decision": decision,
        "block_reason": block_reason,
        "sweep_side": zone.side,
        "signal": signal,
        "direction": direction,
        "premium_discount": premium_discount,
        "stars": stars,
        "score": score,
        "zone": zone,
        "delta_proxy": {
            "bar_delta": bar_delta,
            "outer_delta": zone.deltas[3 if zone.side == "BSL" else 0],
            "total_delta": sum(zone.deltas),
            "abs_total_delta": sum(abs(item) for item in zone.deltas),
            "tick_volume_proxy": volume,
        },
        "rr_plan": rr_plan,
    }


def gold_liquidity_block_reason(
    side: str,
    direction: str,
    signal: str,
    premium_discount: str,
    stars: int,
    score: int,
    rr: float | None,
    settings: Settings,
    relaxed: dict | None = None,
) -> str | None:
    allowed = set(settings.gold_allowed_signal_list)
    if signal in set(settings.gold_observer_signal_list):
        return f"GOLD_SIGNAL_{signal}_OBSERVER_ONLY"
    if signal not in allowed:
        return "GOLD_LIQUIDITY_WAIT"
    if side == "BSL" and direction != "SELL":
        return "GOLD_BSL_REQUIRES_SELL"
    if side == "SSL" and direction != "BUY":
        return "GOLD_SSL_REQUIRES_BUY"
    if side == "BSL" and premium_discount != "PREMIUM":
        return "GOLD_BSL_REQUIRES_PREMIUM"
    if side == "SSL" and premium_discount != "DISCOUNT":
        return "GOLD_SSL_REQUIRES_DISCOUNT"
    min_stars = int(getattr(settings, "gold_liquidity_relaxed_zone_stars", 2)) if (relaxed or {}).get("active") else int(settings.gold_min_zone_stars)
    if stars < min_stars:
        return "GOLD_ZONE_STARS_TOO_LOW"
    if score < int(settings.gold_min_liquidity_score):
        return "GOLD_LIQUIDITY_SCORE_TOO_LOW"
    if rr is None or rr < float(settings.gold_min_rr):
        return "GOLD_RR_BELOW_2"
    return None


def is_confirmed_pivot_high(df: pd.DataFrame, k: int, length: int) -> bool:
    if k - length < 0 or k + length >= len(df):
        return False
    high = float(df.iloc[k]["high"])
    window = pd.to_numeric(df["high"].iloc[k - length : k + length + 1], errors="coerce")
    return high == float(window.max())


def is_confirmed_pivot_low(df: pd.DataFrame, k: int, length: int) -> bool:
    if k - length < 0 or k + length >= len(df):
        return False
    low = float(df.iloc[k]["low"])
    window = pd.to_numeric(df["low"].iloc[k - length : k + length + 1], errors="coerce")
    return low == float(window.min())


def create_bsl_zone(df: pd.DataFrame, k: int, atr_value: float, avg_vol: float, settings: Settings) -> GoldZone:
    row = df.iloc[k]
    top = float(row["high"])
    bottom = max(float(row["close"]), float(row["open"]))
    min_height = atr_value * float(settings.gold_atr_zone_thickness)
    if top - bottom < min_height:
        bottom = top - min_height
    return GoldZone("BSL", top, bottom, top, k, top - bottom, avg_vol * float(settings.gold_zone_capacity), avg_vol, atr_value)


def create_ssl_zone(df: pd.DataFrame, k: int, atr_value: float, avg_vol: float, settings: Settings) -> GoldZone:
    row = df.iloc[k]
    bottom = float(row["low"])
    top = min(float(row["close"]), float(row["open"]))
    min_height = atr_value * float(settings.gold_atr_zone_thickness)
    if top - bottom < min_height:
        top = bottom + min_height
    return GoldZone("SSL", top, bottom, bottom, k, top - bottom, avg_vol * float(settings.gold_zone_capacity), avg_vol, atr_value)


def zones_overlap(new: GoldZone, old: GoldZone) -> bool:
    return not (new.top < old.bottom or new.bottom > old.top)


def zone_quadrants(zone: GoldZone) -> list[tuple[float, float]]:
    height = zone.pivot_size / 4.0 if zone.pivot_size > 0 else 0.0
    return [(zone.bottom + idx * height, zone.bottom + (idx + 1) * height) for idx in range(4)]


def delta_proxy(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    if high - low == 0:
        return 0.0
    return float(row["volume"]) * (float(row["close"]) - float(row["open"])) / (high - low)


def overlap_amount(bar_high: float, bar_low: float, quadrant_top: float, quadrant_bottom: float) -> float:
    top = min(bar_high, quadrant_top)
    bottom = max(bar_low, quadrant_bottom)
    return top - bottom if top > bottom else 0.0


def health_pct(volume_traded: float, capacity: float) -> float:
    if capacity <= 0:
        return 0.0
    return max(0.0, 100.0 - volume_traded / capacity * 100.0)


def eval_reversal(zone: GoldZone, row: pd.Series) -> str:
    abs_total_delta = sum(abs(item) for item in zone.deltas)
    if abs_total_delta <= 0:
        return "NONE"
    outer_idx = 3 if zone.side == "BSL" else 0
    outer_delta = zone.deltas[outer_idx]
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    volume = float(row["volume"])
    bar_delta = delta_proxy(row)
    is_sweeping = high > zone.top if zone.side == "BSL" else low < zone.bottom
    closes_inside = zone.bottom <= close <= zone.top
    mid = (zone.top + zone.bottom) / 2.0
    closes_rej = close < mid if zone.side == "BSL" else close > mid
    ratio = abs(outer_delta) / abs_total_delta
    if is_sweeping and ((zone.side == "BSL" and outer_delta < 0) or (zone.side == "SSL" and outer_delta > 0)):
        if ratio > 0.2:
            return "ABS"
    if is_sweeping and ratio < 0.1:
        return "EXH"
    if closes_inside and ratio > 0.6 and ((zone.side == "BSL" and outer_delta > 0) or (zone.side == "SSL" and outer_delta < 0)):
        return "DIV"
    body_ratio = 0.0 if volume <= 0 else abs(bar_delta) / volume
    if is_sweeping and closes_rej and ((zone.side == "BSL" and bar_delta < 0) or (zone.side == "SSL" and bar_delta > 0)) and body_ratio > 0.2:
        return "REJ"
    return "NONE"


def premium_discount_zone(zone: GoldZone, range_mid: float) -> str:
    zone_mid = (zone.top + zone.bottom) / 2.0
    return "PREMIUM" if zone_mid > range_mid else "DISCOUNT"


def strength_stars(volume_traded: float, avg_vol: float, pivot_size: float, atr_value: float) -> int:
    vol_score = min(volume_traded / avg_vol if avg_vol > 0 else 0.0, 3.0) / 3.0
    piv_score = min(pivot_size / atr_value if atr_value > 0 else 0.0, 3.0) / 3.0
    combined = 0.6 * vol_score + 0.4 * piv_score
    return min(round(combined * 4) + 1, 5)


def liquidity_score(zone: GoldZone, signal: str, premium_discount: str, direction: str, stars: int, rr: float | None, bar_delta: float, volume: float) -> int:
    score = 0
    if stars >= 3:
        score += 20
    if zone.test_count <= 1:
        score += 15
    if zone.sweep_count > 0:
        score += 20
    if signal in {"ABS", "REJ"}:
        score += 20
    if (direction == "SELL" and premium_discount == "PREMIUM") or (direction == "BUY" and premium_discount == "DISCOUNT"):
        score += 10
    if (direction == "SELL" and bar_delta < 0) or (direction == "BUY" and bar_delta > 0):
        score += 10
    if rr is not None and rr >= 2:
        score += 5
    return max(0, min(100, score))


def rr_plan_for_zone(zone: GoldZone, direction: str, entry: float, rr_target: float = 2.0) -> dict:
    sl_dist = zone.top - zone.bottom
    if direction == "SELL":
        sl = zone.top + sl_dist * 0.3
        risk = sl - entry
        tp = entry - risk * rr_target
    else:
        sl = zone.bottom - sl_dist * 0.3
        risk = entry - sl
        tp = entry + risk * rr_target
    rr = None if risk <= 0 else abs(tp - entry) / abs(entry - sl)
    return {"entry": round(entry, 8), "sl": round(sl, 8), "tp": round(tp, 8), "rr": round(rr, 6) if rr is not None and math.isfinite(rr) else None}


def atr14(df: pd.DataFrame) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False).mean().fillna(high - low)


def _add_zone(zones: list[GoldZone], new: GoldZone, capacity: int) -> None:
    for old in list(zones):
        if old.status == "SWEPT" or not zones_overlap(new, old):
            continue
        if new.side == "BSL":
            if new.pivot_price > old.top:
                zones.remove(old)
                break
            return
        if new.pivot_price < old.bottom:
            zones.remove(old)
            break
        return
    zones.append(new)
    active = [zone for zone in zones if zone.status != "SWEPT"]
    if len(active) > capacity:
        oldest = active[0]
        zones.remove(oldest)


def _nearest_zone(zones: list[GoldZone]) -> dict:
    active = [zone for zone in zones if zone.status != "SWEPT"]
    return active[-1].to_payload() if active else {}


def _normalize_candles(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        df = value.copy()
    elif isinstance(value, dict):
        nested = None
        for key in ("candles", "data", "rows"):
            candidate = value.get(key)
            if candidate is not None:
                nested = candidate
                break
        df = pd.DataFrame(nested if nested is not None else value)
    elif isinstance(value, (list, tuple)):
        df = pd.DataFrame(value)
    else:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    aliases = {
        "time": "time",
        "candle_time": "time",
        "timestamp": "time",
        "date": "time",
        "open": "open",
        "o": "open",
        "high": "high",
        "h": "high",
        "low": "low",
        "l": "low",
        "close": "close",
        "c": "close",
        "volume": "volume",
        "tick_volume": "volume",
        "real_volume": "volume",
        "v": "volume",
    }
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        rename[col] = aliases.get(key, col)
    df = df.rename(columns=rename)
    df = df.loc[:, ~df.columns.duplicated()].copy()
    required = ["open", "high", "low", "close"]
    if any(col not in df.columns for col in required):
        return pd.DataFrame()
    if "volume" not in df.columns:
        df["volume"] = 0.0
    out = df[required + ["volume"]].copy()
    for col in out.columns:
        series = out.loc[:, col]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        out[col] = pd.to_numeric(series, errors="coerce")
    return out.dropna().reset_index(drop=True)


def _closed_frame(value: Any) -> pd.DataFrame:
    df = _normalize_candles(value)
    if len(df) > 1:
        return df.iloc[:-1].copy().reset_index(drop=True)
    return df


def _payload(
    symbol: str,
    decision: str,
    block_reason: str | None,
    nearest_bsl: dict | None = None,
    nearest_ssl: dict | None = None,
    active_zone: dict | None = None,
    sweep_detected: bool = False,
    sweep_side: str = "NONE",
    reversal_signal: str = "NONE",
    zone_stars: int = 0,
    zone_health_pct: float = 0.0,
    premium_discount: str = "NONE",
    sweep_count: int = 0,
    test_count: int = 0,
    liquidity_score: int = 0,
    directional_confirmation: str = "NONE",
    delta_proxy: dict | None = None,
    rr_plan: dict | None = None,
    relaxed_mode_active: bool = False,
    relaxed_reason: str | None = None,
    hours_without_setup: float | None = None,
    strict_threshold: int | None = None,
    relaxed_threshold: int | None = None,
) -> dict:
    return {
        "wsp_enabled": False,
        "enabled": True,
        "mode": "ENTRY_STRATEGY",
        "role": "GOLD_ENTRY_STRATEGY",
        "strategy": STRATEGY,
        "source": SOURCE,
        "symbol": symbol,
        "decision": decision,
        "nearest_bsl": nearest_bsl or {},
        "nearest_ssl": nearest_ssl or {},
        "active_zone": active_zone or {},
        "sweep_detected": bool(sweep_detected),
        "sweep_side": sweep_side,
        "reversal_signal": reversal_signal,
        "zone_stars": zone_stars,
        "zone_health_pct": zone_health_pct,
        "premium_discount": premium_discount,
        "sweep_count": sweep_count,
        "test_count": test_count,
        "liquidity_score": liquidity_score,
        "directional_confirmation": directional_confirmation,
        "delta_proxy": delta_proxy or {},
        "rr_plan": rr_plan or {"entry": None, "sl": None, "tp": None, "rr": None},
        "relaxed_mode_active": bool(relaxed_mode_active),
        "relaxed_reason": relaxed_reason,
        "hours_without_setup": hours_without_setup,
        "strict_threshold": strict_threshold,
        "relaxed_threshold": relaxed_threshold,
        "block_reason": block_reason,
        "warnings": [],
    }


def _signal_from_result(symbol: str, result: dict) -> dict:
    decision = str(result.get("decision") or "WAIT").upper()
    rr_plan = result.get("rr_plan") if isinstance(result.get("rr_plan"), dict) else {}
    signal = decision if decision in {"BUY", "SELL"} else "WAIT"
    score = int(result.get("liquidity_score") or 0)
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "signal": signal,
        "direction": signal,
        "entry": rr_plan.get("entry"),
        "sl": rr_plan.get("sl"),
        "tp": rr_plan.get("tp"),
        "risk_reward": rr_plan.get("rr"),
        "reward_risk": rr_plan.get("rr"),
        "confidence": round(score / 100.0, 4),
        "m15_confirmation": signal in {"BUY", "SELL"},
        "m1_entry_confirmation": signal in {"BUY", "SELL"},
        "m15_confirmation_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m1_trigger_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m15_confirmation_reason": "GOLD_LIQUIDITY_HUNTER_PRO",
        "m1_trigger_reason": "GOLD_LIQUIDITY_HUNTER_PRO",
        "big_setup_grade": _grade(score),
        "setup_hunter_grade": _grade(score),
        "grade": _grade(score),
        "gold_liquidity_hunter": result,
        "gold_liquidity_signal": result.get("reversal_signal"),
        "gold_liquidity_score": score,
        "gold_liquidity_reason": result.get("block_reason") or f"GOLD_LIQUIDITY_{signal}",
        "relaxed_mode_active": bool(result.get("relaxed_mode_active")),
        "relaxed_reason": result.get("relaxed_reason"),
        "hours_without_setup": result.get("hours_without_setup"),
        "strict_threshold": result.get("strict_threshold"),
        "relaxed_threshold": result.get("relaxed_threshold"),
        "reason": result.get("block_reason") or f"GOLD_LIQUIDITY_{signal}",
    }


def _wait_signal(symbol: str, reason: str, enabled: bool = True) -> dict:
    result = _payload(symbol, "WAIT", reason)
    result["enabled"] = enabled
    _log_result(result)
    return _signal_from_result(symbol, result)


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def _is_gold_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().replace("#", "")
    return normalized.startswith("GOLD") or normalized.startswith("XAUUSD")


def _relaxed_state(settings: Settings, context: dict) -> dict:
    threshold = int(getattr(settings, "gold_m1m5_relaxed_after_hours_no_setup", 24))
    hours = _finite(context.get("gold_liquidity_hours_without_setup") or context.get("hours_without_setup"))
    ignore_setup_wait = bool(getattr(settings, "demo_ignore_all_time_blocks", False) or getattr(settings, "demo_ignore_setup_wait_hours", False))
    active = bool(getattr(settings, "gold_m1m5_relaxed_demo_mode", True)) and (ignore_setup_wait or (hours is not None and hours >= threshold))
    reason = "DURATION_BLOCKS_DISABLED_BY_USER_ORDER" if active and ignore_setup_wait else "NO_SETUP_24H" if active else None
    if active and ignore_setup_wait:
        log.info("[DURATION_GATE] decision=PASS reason=DURATION_BLOCKS_DISABLED_BY_USER_ORDER")
    return {"active": active, "reason": reason, "hours_without_setup": hours}


def _finite(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _log_result(result: dict) -> None:
    decision = result.get("decision")
    reason = result.get("block_reason")
    if decision in {"BUY", "SELL"}:
        log.info(
            "[GOLD_LIQUIDITY] mode=ENTRY_STRATEGY decision=%s signal=%s stars=%s score=%s rr=%s",
            decision,
            result.get("reversal_signal"),
            result.get("zone_stars"),
            result.get("liquidity_score"),
            (result.get("rr_plan") or {}).get("rr") if isinstance(result.get("rr_plan"), dict) else None,
        )
    else:
        log.info("[GOLD_LIQUIDITY] mode=ENTRY_STRATEGY decision=%s reason=%s", decision, reason or "NONE")
