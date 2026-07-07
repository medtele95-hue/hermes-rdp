"""SUPER-EYES — pure observation features. ZERO decisional effect.

Four senses, every one fail-soft (a dead sense returns available=False and
never raises into the trading path):

a) Real tick CVD on GOLD#: mt5.copy_ticks_from, tick-rule on the midprice
   ((bid+ask)/2; flat tick inherits the previous direction). Session
   cumulative delta + m5/m15 slopes + price/CVD divergence.
   Budget < 200 ms (measured and logged).
b) DXY proxy (XM has no USDX): ICE weights EURUSD 57.6 / USDJPY 13.6 /
   GBPUSD 11.9, renormalized. EURUSD and GBPUSD enter inverted (USD is the
   quote), USDJPY direct. change_m15/change_h1, trend, gold_dxy_divergence
   (gold and DXY moving the SAME way is anomalous).
c) D1/W1 levels: PDH/PDL/PDC, daily open, weekly open, week high/low,
   SIGNED distances in points and in ATR units.
d) Mining canary: NEM.N + B.N equal-weight composite -> hui_change_h1/d1,
   ratio trend, divergence (strict extremes only). NOTE: these symbols are
   ABSENT on this XM demo server (checked 2026-07-07) and US equity CFDs
   only quote during US hours — the sense reports available=False until a
   broker carries them. Documented limitation, fail-soft by design.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from app.logger import log

# ICE DXY weights for the pairs XM actually quotes, renormalized:
# EUR 57.6 + JPY 13.6 + GBP 11.9 = 83.1
_DXY_WEIGHTS = {"EURUSD": -0.576 / 0.831, "USDJPY": 0.136 / 0.831, "GBPUSD": -0.119 / 0.831}

_BUDGET_MS = 200.0


# ── a) real tick CVD ────────────────────────────────────────────────────────

def cvd_from_ticks(ticks: list, now_epoch: float | None = None) -> dict:
    """Tick-rule CVD on the midprice. Pure function over tick rows
    (dicts or objects exposing bid/ask/volume/time)."""
    mids: list[tuple[float, float, float]] = []  # (epoch, mid, volume)
    for tick in ticks or []:
        bid = _num(_get(tick, "bid"))
        ask = _num(_get(tick, "ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        epoch = _num(_get(tick, "time")) or 0.0
        volume = _num(_get(tick, "volume")) or 1.0
        mids.append((epoch, (bid + ask) / 2.0, volume if volume > 0 else 1.0))
    if len(mids) < 3:
        return {"available": False, "reason": "INSUFFICIENT_TICKS"}

    cvd = 0.0
    direction = 0
    series: list[tuple[float, float]] = []  # (epoch, cvd)
    prev_mid = mids[0][1]
    for epoch, mid, volume in mids[1:]:
        if mid > prev_mid:
            direction = 1
        elif mid < prev_mid:
            direction = -1
        # equal mid: tick-rule inherits the previous direction
        cvd += direction * volume
        series.append((epoch, cvd))
        prev_mid = mid

    now_epoch = now_epoch if now_epoch is not None else (series[-1][0] if series else 0.0)
    slope_m5 = _cvd_slope(series, now_epoch, 300)
    slope_m15 = _cvd_slope(series, now_epoch, 900)
    price_change = mids[-1][1] - mids[0][1]
    divergence = None
    if price_change > 0 and cvd < 0:
        divergence = "bear"
    elif price_change < 0 and cvd > 0:
        divergence = "bull"
    return {
        "available": True,
        "cvd_session": round(cvd, 2),
        "cvd_slope_m5": slope_m5,
        "cvd_slope_m15": slope_m15,
        "cvd_divergence": divergence,
        "ticks_used": len(mids),
    }


def _cvd_slope(series: list[tuple[float, float]], now_epoch: float, window_seconds: float) -> float | None:
    window = [value for epoch, value in series if epoch >= now_epoch - window_seconds]
    if len(window) < 2:
        return None
    return round(window[-1] - window[0], 2)


# ── b) DXY proxy ────────────────────────────────────────────────────────────

def dxy_proxy(pair_changes: dict[str, dict[str, float | None]], gold_change_m15: float | None = None) -> dict:
    """pair_changes: {"EURUSD": {"m15": pct, "h1": pct}, ...} in percent."""
    out: dict = {"available": True}
    for horizon in ("m15", "h1"):
        total = 0.0
        seen = 0
        for pair, weight in _DXY_WEIGHTS.items():
            change = _num((pair_changes.get(pair) or {}).get(horizon))
            if change is None:
                continue
            total += weight * change
            seen += 1
        out[f"dxy_change_{horizon}"] = round(total, 5) if seen == len(_DXY_WEIGHTS) else None
    if out["dxy_change_m15"] is None and out["dxy_change_h1"] is None:
        return {"available": False, "reason": "PAIR_DATA_MISSING"}
    h1 = out.get("dxy_change_h1")
    out["dxy_trend"] = "UP" if (h1 or 0) > 0 else ("DOWN" if (h1 or 0) < 0 else "FLAT")
    gold = _num(gold_change_m15)
    m15 = out.get("dxy_change_m15")
    divergence = None
    if gold is not None and m15 is not None and gold != 0 and m15 != 0:
        # gold and DXY normally move inversely — same-direction is anomalous
        if (gold > 0) == (m15 > 0):
            divergence = "SAME_DIRECTION_ANOMALY"
        else:
            divergence = "NORMAL_INVERSE"
    out["gold_dxy_divergence"] = divergence
    return out


# ── c) D1/W1 levels ─────────────────────────────────────────────────────────

def d1_w1_levels(d1_rows: list[dict] | None, w1_rows: list[dict] | None, price: float | None, atr: float | None, point: float | None = None) -> dict:
    if not d1_rows or len(d1_rows) < 2 or price is None:
        return {"available": False, "reason": "D1_DATA_MISSING"}
    prev_day = d1_rows[-2]
    today = d1_rows[-1]
    levels = {
        "pdh": _num(prev_day.get("high")),
        "pdl": _num(prev_day.get("low")),
        "pdc": _num(prev_day.get("close")),
        "daily_open": _num(today.get("open")),
    }
    if w1_rows and len(w1_rows) >= 1:
        week = w1_rows[-1]
        levels["weekly_open"] = _num(week.get("open"))
        levels["week_high"] = _num(week.get("high"))
        levels["week_low"] = _num(week.get("low"))
    out: dict = {"available": True, **levels}
    for name, level in levels.items():
        if level is None:
            out[f"dist_{name}_pts"] = None
            out[f"dist_{name}_atr"] = None
            continue
        distance = price - level  # SIGNED: positive = price above the level
        out[f"dist_{name}_pts"] = round(distance / point, 1) if point and point > 0 else round(distance, 5)
        out[f"dist_{name}_atr"] = round(distance / atr, 3) if atr and atr > 0 else None
    return out


# ── d) mining canary ────────────────────────────────────────────────────────

def mining_canary(miner_changes: dict[str, dict[str, float | None]] | None, gold_change_h1: float | None = None) -> dict:
    """miner_changes: {"NEM.N": {"h1": pct, "d1": pct}, "B.N": {...}} or None
    when the symbols are absent (this XM server carries neither NEM.N nor
    B.N — documented; US-hours quoting only)."""
    if not miner_changes:
        return {"available": False, "reason": "MINER_SYMBOLS_ABSENT"}
    h1_values = [_num((v or {}).get("h1")) for v in miner_changes.values()]
    d1_values = [_num((v or {}).get("d1")) for v in miner_changes.values()]
    h1_values = [v for v in h1_values if v is not None]
    d1_values = [v for v in d1_values if v is not None]
    if not h1_values and not d1_values:
        return {"available": False, "reason": "MINER_DATA_MISSING"}
    out: dict = {"available": True}
    out["hui_change_h1"] = round(sum(h1_values) / len(h1_values), 5) if h1_values else None
    out["hui_change_d1"] = round(sum(d1_values) / len(d1_values), 5) if d1_values else None
    gold = _num(gold_change_h1)
    hui = out.get("hui_change_h1")
    out["hui_ratio_trend"] = None
    out["hui_divergence"] = None
    if gold is not None and hui is not None:
        out["hui_ratio_trend"] = "MINERS_LEAD" if abs(hui) > abs(gold) else "GOLD_LEADS"
        # strict extremes only: both moves must be significant
        if abs(hui) >= 0.5 and abs(gold) >= 0.2 and (hui > 0) != (gold > 0):
            out["hui_divergence"] = "BEARISH_FOR_GOLD" if hui < 0 else "BULLISH_FOR_GOLD"
    return out


# ── collector (live MT5, fail-soft, budget-logged) ──────────────────────────

def collect_market_eyes(settings: object = None, mt5_module=None, gold_symbol: str = "GOLD#") -> dict:
    """Collect the four senses from live MT5. Every sense fail-soft.
    Returns a FLAT dict of scalar features prefixed eyes_*."""
    started = time.perf_counter()
    if mt5_module is None:
        try:
            import MetaTrader5 as mt5_module
        except Exception:
            return {"eyes_available": False, "eyes_reason": "MT5_UNAVAILABLE"}

    out: dict = {"eyes_available": True}

    # a) CVD from real ticks
    try:
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(minutes=30)
        raw_ticks = mt5_module.copy_ticks_from(gold_symbol, since, 20000, getattr(mt5_module, "COPY_TICKS_ALL", -1))
        ticks = list(raw_ticks) if raw_ticks is not None else []
        cvd = cvd_from_ticks([{"bid": t["bid"], "ask": t["ask"], "time": t["time"], "volume": t["volume"]} if isinstance(t, dict) else {"bid": t[1], "ask": t[2], "time": t[0], "volume": t[3] if len(t) > 3 else 1} for t in ticks])
    except Exception as exc:
        cvd = {"available": False, "reason": f"CVD_ERROR:{str(exc)[:80]}"}
    _merge(out, "cvd", cvd)

    # b) DXY proxy from pair rates
    try:
        pair_changes = {}
        for pair in ("EURUSD", "USDJPY", "GBPUSD"):
            pair_changes[pair] = {
                "m15": _pct_change(mt5_module, pair, getattr(mt5_module, "TIMEFRAME_M15", 15), 1),
                "h1": _pct_change(mt5_module, pair, getattr(mt5_module, "TIMEFRAME_H1", 16385), 1),
            }
        gold_m15 = _pct_change(mt5_module, gold_symbol, getattr(mt5_module, "TIMEFRAME_M15", 15), 1)
        dxy = dxy_proxy(pair_changes, gold_change_m15=gold_m15)
    except Exception as exc:
        dxy = {"available": False, "reason": f"DXY_ERROR:{str(exc)[:80]}"}
    _merge(out, "dxy", dxy)

    # c) D1/W1 levels
    try:
        d1 = _rates_rows(mt5_module, gold_symbol, getattr(mt5_module, "TIMEFRAME_D1", 16408), 3)
        w1 = _rates_rows(mt5_module, gold_symbol, getattr(mt5_module, "TIMEFRAME_W1", 32769), 2)
        tick = mt5_module.symbol_info_tick(gold_symbol)
        price = None
        if tick is not None:
            bid = _num(getattr(tick, "bid", None))
            ask = _num(getattr(tick, "ask", None))
            price = (bid + ask) / 2.0 if bid and ask else None
        info = mt5_module.symbol_info(gold_symbol)
        point = _num(getattr(info, "point", None)) if info else None
        atr = None
        if d1 and len(d1) >= 2:
            trs = [row["high"] - row["low"] for row in d1]
            atr = sum(trs) / len(trs)
        levels = d1_w1_levels(d1, w1, price, atr, point)
    except Exception as exc:
        levels = {"available": False, "reason": f"LEVELS_ERROR:{str(exc)[:80]}"}
    _merge(out, "lvl", levels)

    # d) mining canary — NEM.N / B.N absent on this XM server: fail-soft
    try:
        miner_changes = {}
        for miner in ("NEM.N", "B.N"):
            info = mt5_module.symbol_info(miner)
            if info is None:
                continue
            miner_changes[miner] = {
                "h1": _pct_change(mt5_module, miner, getattr(mt5_module, "TIMEFRAME_H1", 16385), 1),
                "d1": _pct_change(mt5_module, miner, getattr(mt5_module, "TIMEFRAME_D1", 16408), 1),
            }
        gold_h1 = _pct_change(mt5_module, gold_symbol, getattr(mt5_module, "TIMEFRAME_H1", 16385), 1)
        canary = mining_canary(miner_changes or None, gold_change_h1=gold_h1)
    except Exception as exc:
        canary = {"available": False, "reason": f"CANARY_ERROR:{str(exc)[:80]}"}
    _merge(out, "hui", canary)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    out["eyes_elapsed_ms"] = round(elapsed_ms, 1)
    level = log.warning if elapsed_ms > _BUDGET_MS else log.info
    level(
        "[MARKET_EYES] cvd=%s dxy=%s levels=%s canary=%s elapsed_ms=%.1f budget_ms=%.0f",
        out.get("eyes_cvd_available"), out.get("eyes_dxy_available"),
        out.get("eyes_lvl_available"), out.get("eyes_hui_available"), elapsed_ms, _BUDGET_MS,
    )
    return out


def _merge(out: dict, prefix: str, sense: dict) -> None:
    out[f"eyes_{prefix}_available"] = bool(sense.get("available"))
    for key, value in sense.items():
        if key == "available":
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            out[f"eyes_{prefix}_{key}" if not key.startswith(prefix) else f"eyes_{key}"] = value


def _rates_rows(mt5_module, symbol: str, timeframe: int, count: int) -> list[dict] | None:
    rates = mt5_module.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        return None
    rows = []
    for row in rates:
        try:
            rows.append({"open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])})
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return rows or None


def _pct_change(mt5_module, symbol: str, timeframe: int, bars_back: int) -> float | None:
    rates = mt5_module.copy_rates_from_pos(symbol, timeframe, 0, bars_back + 1)
    if rates is None or len(rates) < bars_back + 1:
        return None
    try:
        old = float(rates[0]["close"])
        new = float(rates[-1]["close"])
        return round((new - old) / old * 100.0, 5) if old else None
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _get(obj: object, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _num(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
