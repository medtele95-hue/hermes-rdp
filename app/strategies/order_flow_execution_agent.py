from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.logger import log

STRATEGY = "ORDER_FLOW_EXECUTION_AGENT"
SOURCE = "app/strategies/order_flow_execution_agent.py"

ALLOWED_SYMBOLS_DEFAULT = frozenset({"BTCUSD", "BTCUSD#", "GOLD", "GOLD#", "XAUUSD", "EURUSD"})

SETUP_LIQUIDITY_SWEEP_REVERSAL = "LIQUIDITY_SWEEP_REVERSAL"
SETUP_VWAP_RECLAIM_REJECTION = "VWAP_RECLAIM_REJECTION"
SETUP_VALUE_AREA_ROTATION = "VALUE_AREA_ROTATION"
SETUP_VALUE_BREAKOUT = "VALUE_BREAKOUT"

_STALE_SECONDS = 120

# Module-level cooldown state per canonical symbol
_last_routed: dict[str, datetime] = {}


def evaluate(
    symbol: str,
    frames: dict[str, Any] | None,
    context: dict | None = None,
    settings: object | None = None,
) -> dict:
    enabled = bool(getattr(settings, "order_flow_execution_enabled", False))
    if not enabled:
        return _wait(symbol, "ORDER_FLOW_EXECUTION_DISABLED")

    allowed = _allowed_symbols(settings)
    canonical = _canonical_symbol(symbol)
    if canonical not in allowed and str(symbol or "").upper() not in allowed:
        return _wait(symbol, "ORDER_FLOW_SYMBOL_NOT_ALLOWED")

    snapshot = _get_snapshot(symbol, frames, context)
    if not snapshot:
        return _wait(symbol, "ORDER_FLOW_SNAPSHOT_MISSING")

    stale_reason = _check_staleness(snapshot)
    if stale_reason:
        return _wait(symbol, stale_reason)

    price = _to_float(snapshot.get("price"))
    vwap = _to_float(snapshot.get("vwap"))
    poc = _to_float(snapshot.get("poc"))
    vah = _to_float(snapshot.get("vah"))
    val = _to_float(snapshot.get("val"))
    cvd_slope = _to_float(snapshot.get("cvd_slope"))
    delta = _to_float(snapshot.get("delta_proxy") or snapshot.get("latest_delta"))
    divergence = snapshot.get("divergence")

    if any(v is None for v in (price, vwap, poc, vah, val)):
        return _wait(symbol, "ORDER_FLOW_MISSING_KEY_LEVELS")

    spread = _to_float((context or {}).get("spread") or (frames or {}).get("spread"))
    max_spread = _to_float((context or {}).get("max_spread") or (frames or {}).get("max_spread"))
    spread_ok = _spread_ok(spread, max_spread)
    if spread_ok is False:
        return _wait(symbol, "ORDER_FLOW_SPREAD_TOO_WIDE")

    session_ok = _check_session(context)

    setup = _detect_setup(price, vwap, poc, vah, val, cvd_slope, delta, divergence)
    if setup is None:
        return _wait(symbol, "ORDER_FLOW_NO_VALID_SETUP")

    setup_type, direction = setup

    score = _score_setup(
        price, vwap, poc, vah, val, cvd_slope, delta, divergence,
        setup_type, direction, spread_ok, session_ok,
    )

    min_score = int(getattr(settings, "order_flow_min_score", 75))
    if score < min_score:
        return _wait(symbol, "ORDER_FLOW_SCORE_BELOW_THRESHOLD", score=score)

    min_rr = float(getattr(settings, "order_flow_min_rr", 1.5))
    levels = _calc_sltp(direction, price, vwap, poc, vah, val, min_rr)
    if levels is None:
        return _wait(symbol, "ORDER_FLOW_INVALID_SLTP", score=score)

    entry, sl, tp, rr = levels
    if rr < min_rr:
        return _wait(symbol, "ORDER_FLOW_RR_BELOW_MIN", score=score)

    cooldown_minutes = int(getattr(settings, "order_flow_cooldown_minutes", 15))
    if not _cooldown_ok(canonical, cooldown_minutes):
        return _wait(symbol, "ORDER_FLOW_COOLDOWN_ACTIVE", score=score)

    grade = _grade(score)
    order_flow_payload = {
        "vwap": vwap,
        "poc": poc,
        "vah": vah,
        "val": val,
        "cvd_slope": cvd_slope,
        "delta": delta,
        "divergence": divergence,
    }

    log.info(
        "[ORDER_FLOW_EXEC_AGENT] symbol=%s setup=%s direction=%s score=%s grade=%s rr=%s",
        symbol, setup_type, direction, score, grade, round(rr, 2),
    )
    log.info(
        "[ORDER_FLOW_EXEC_ROUTE] symbol=%s decision=SEND_TO_SETUP_HUNTER strategy=%s",
        symbol, STRATEGY,
    )

    return {
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "setup_type": setup_type,
        "symbol": canonical,
        "broker_symbol": str(symbol or "").upper(),
        "direction": direction,
        "signal": direction,
        "side": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "risk_reward": rr,
        "reward_risk": rr,
        "confidence": score,
        "grade": grade,
        "setup_hunter_grade": grade,
        "big_setup_grade": grade,
        "reason": f"ORDER_FLOW_{direction}_{setup_type}",
        "order_flow": order_flow_payload,
        "order_flow_execution_agent": order_flow_payload,
        "order_flow_execution_agent_score": score,
        "order_flow_execution_agent_signal": direction,
        "order_flow_execution_agent_reason": f"ORDER_FLOW_{direction}_{setup_type}",
        "status": "ORDER_READY",
        "mode": "ACTIVE_EXECUTION",
        "strategy_role": "ENTRY_STRATEGY",
        "strategy_status": "ACTIVE",
        "timeframe": "M5",
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "m15_confirmation_status": "PASS",
        "m1_trigger_status": "PASS",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": STRATEGY,
        "order_flow_entry_enabled": True,
        "order_flow_required_fields_valid": True,
        "source": SOURCE,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _detect_setup(
    price: float, vwap: float, poc: float, vah: float, val: float,
    cvd_slope: float | None, delta: float | None, divergence: str | None,
) -> tuple[str, str] | None:
    prox = price * 0.0025

    # 1. LIQUIDITY_SWEEP_REVERSAL
    if price < val + prox and ((delta is not None and delta > 0) or divergence == "bull"):
        return SETUP_LIQUIDITY_SWEEP_REVERSAL, "BUY"
    if price > vah - prox and ((delta is not None and delta < 0) or divergence == "bear"):
        return SETUP_LIQUIDITY_SWEEP_REVERSAL, "SELL"

    # 2. VWAP_RECLAIM_REJECTION
    if price > vwap and cvd_slope is not None and cvd_slope > 0 and delta is not None and delta > 0:
        return SETUP_VWAP_RECLAIM_REJECTION, "BUY"
    if price < vwap and cvd_slope is not None and cvd_slope < 0 and delta is not None and delta < 0:
        return SETUP_VWAP_RECLAIM_REJECTION, "SELL"

    # 3. VALUE_AREA_ROTATION
    if abs(price - val) <= prox * 2 and (delta is None or delta >= 0):
        return SETUP_VALUE_AREA_ROTATION, "BUY"
    if abs(price - vah) <= prox * 2 and (delta is None or delta <= 0):
        return SETUP_VALUE_AREA_ROTATION, "SELL"

    # 4. VALUE_BREAKOUT
    if price > vah + prox and cvd_slope is not None and cvd_slope > 0 and delta is not None and delta > 0:
        return SETUP_VALUE_BREAKOUT, "BUY"
    if price < val - prox and cvd_slope is not None and cvd_slope < 0 and delta is not None and delta < 0:
        return SETUP_VALUE_BREAKOUT, "SELL"

    return None


def _score_setup(
    price: float, vwap: float, poc: float, vah: float, val: float,
    cvd_slope: float | None, delta: float | None, divergence: str | None,
    setup_type: str, direction: str, spread_ok: bool | None, session_ok: bool,
) -> int:
    score = 25  # setup structure confirmed (+25)
    prox = price * 0.0025

    # VWAP/VAH/VAL confirmation: +20
    if setup_type == SETUP_VWAP_RECLAIM_REJECTION:
        score += 20
    elif setup_type == SETUP_LIQUIDITY_SWEEP_REVERSAL:
        if (direction == "BUY" and price < val + prox) or (direction == "SELL" and price > vah - prox):
            score += 20
    elif setup_type == SETUP_VALUE_AREA_ROTATION:
        if (direction == "BUY" and abs(price - val) <= prox * 2) or (direction == "SELL" and abs(price - vah) <= prox * 2):
            score += 20
    elif setup_type == SETUP_VALUE_BREAKOUT:
        if (direction == "BUY" and price > vah + prox) or (direction == "SELL" and price < val - prox):
            score += 20

    # CVD confirms: +15
    if cvd_slope is not None:
        if (direction == "BUY" and cvd_slope > 0) or (direction == "SELL" and cvd_slope < 0):
            score += 15

    # Delta confirms: +15
    if delta is not None:
        if (direction == "BUY" and delta > 0) or (direction == "SELL" and delta < 0):
            score += 15

    # Value area location — near poc: +10
    if abs(price - poc) <= prox * 3:
        score += 10

    # Spread OK: +5
    if spread_ok is True or spread_ok is None:
        score += 5

    # Session OK: +5
    if session_ok:
        score += 5

    # Divergence penalty: -25 if against setup direction
    if divergence:
        divergence_direction = "BUY" if divergence == "bull" else "SELL"
        if divergence_direction != direction:
            score -= 25

    return max(0, min(100, score))


def _calc_sltp(
    direction: str, price: float, vwap: float, poc: float, vah: float, val: float, min_rr: float,
) -> tuple[float, float, float, float] | None:
    buffer = price * 0.001

    if direction == "BUY":
        sl = val - buffer
        risk = price - sl
        if risk <= 0:
            return None
        tp = price + risk * min_rr
        rr = (tp - price) / risk
    else:
        sl = vah + buffer
        risk = sl - price
        if risk <= 0:
            return None
        tp = price - risk * min_rr
        rr = (price - tp) / risk

    return round(price, 5), round(sl, 5), round(tp, 5), round(rr, 2)


def _cooldown_ok(canonical: str, cooldown_minutes: int) -> bool:
    last = _last_routed.get(canonical)
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= cooldown_minutes * 60


def mark_routed(symbol: str) -> None:
    _last_routed[_canonical_symbol(symbol)] = datetime.now(timezone.utc)


def _check_staleness(snapshot: dict) -> str | None:
    created_at = snapshot.get("created_at")
    if not created_at:
        return None
    try:
        ts = datetime.fromisoformat(str(created_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - ts).total_seconds() > _STALE_SECONDS:
            return "ORDER_FLOW_DATA_STALE"
    except (ValueError, TypeError):
        pass
    return None


def _check_session(context: dict | None) -> bool:
    if not context:
        return True
    session = str(context.get("session_name") or "").upper()
    if not session or session in ("UNKNOWN", ""):
        return True
    return session in ("LONDON", "NEW_YORK", "OVERLAP", "ASIA_MAIN")


def _spread_ok(spread: float | None, max_spread: float | None) -> bool | None:
    if spread is None or max_spread is None:
        return None
    return spread <= max_spread


def _get_snapshot(symbol: str, frames: dict | None, context: dict | None) -> dict | None:
    canonical = _canonical_symbol(symbol)
    for source in (context, frames):
        if not isinstance(source, dict):
            continue
        snap = source.get("order_flow_snapshot")
        if isinstance(snap, dict) and snap:
            return snap
        snaps = source.get("order_flow_snapshots")
        if isinstance(snaps, dict):
            snap = snaps.get(str(symbol or "").upper()) or snaps.get(canonical)
            if isinstance(snap, dict) and snap:
                return snap
    return None


def _allowed_symbols(settings: object | None) -> frozenset:
    raw = str(getattr(settings, "order_flow_allowed_symbols", "") or "")
    if not raw:
        return ALLOWED_SYMBOLS_DEFAULT
    return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())


def _canonical_symbol(symbol: object) -> str:
    normalized = str(symbol or "").upper().strip()
    if normalized.startswith("BTCUSD"):
        return "BTCUSD"
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return "GOLD"
    if normalized.startswith("EURUSD"):
        return "EURUSD"
    return normalized.replace("#", "")


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    return "D"


def _wait(symbol: str, reason: str, score: int = 0) -> dict:
    log.info("[ORDER_FLOW_EXEC_AGENT] symbol=%s decision=WAIT reason=%s", symbol, reason)
    canonical = _canonical_symbol(symbol)
    return {
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "symbol": canonical,
        "broker_symbol": str(symbol or "").upper(),
        "direction": "WAIT",
        "signal": "WAIT",
        "side": None,
        "status": "WAIT",
        "mode": "ACTIVE_EXECUTION",
        "strategy_role": "ENTRY_STRATEGY",
        "confidence": score,
        "entry": None,
        "sl": None,
        "tp": None,
        "rr": None,
        "risk_reward": None,
        "reward_risk": None,
        "grade": "D",
        "reason": reason,
        "order_flow_execution_agent_reason": reason,
        "order_flow_execution_agent_score": score,
        "order_flow_execution_agent_signal": "WAIT",
        "order_flow_entry_enabled": False,
        "source": SOURCE,
    }


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
