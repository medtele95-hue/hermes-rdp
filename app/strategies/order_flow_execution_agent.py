from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.logger import log

STRATEGY = "ORDER_FLOW_EXECUTION_AGENT"
SOURCE = "app/strategies/order_flow_execution_agent.py"

ALLOWED_SYMBOLS_DEFAULT = frozenset({"BTCUSD", "BTCUSD#", "GOLD", "GOLD#", "XAUUSD", "EURUSD"})

SETUP_LIQUIDITY_SWEEP_REVERSAL = "LIQUIDITY_SWEEP_REVERSAL"
SETUP_VWAP_RECLAIM_REJECTION = "VWAP_RECLAIM_REJECTION"
SETUP_VALUE_AREA_ROTATION = "VALUE_AREA_ROTATION"
SETUP_VALUE_BREAKOUT = "VALUE_BREAKOUT"

# ── Planned setups (NOT active — observation only) ───────────────────────────
# AMD_FVG_IFVG_REVERSAL
#   Concept  : ICT AMD cycle (Accumulation / Manipulation / Distribution).
#              Entry at an unmitigated Fair Value Gap (FVG) or Inverse FVG (IFVG)
#              that forms during the Manipulation leg, targeting the Distribution leg.
#   Trigger  : Price sweeps a session high/low (manipulation), then retraces into
#              an FVG/IFVG on M1–M5; MSS M1 confirms the reversal.
#   Filters  : Kill zone required (London Open or NY Open); H1 bias must align.
#   Risk     : SL beyond the swept liquidity level; TP at opposing session extreme.
#   Status   : OBSERVATION_ONLY — requires FVG detector (not yet implemented).
#
# FIB_OTE_RETEST
#   Concept  : Optimal Trade Entry (OTE) — ICT retracement into the 62–79 % Fibonacci
#              zone of the most recent significant swing, with a confirmed MSS on M1.
#   Trigger  : Swing identified on H1 or M15; price retraces to OTE zone; M1 MSS
#              confirms rejection; CVD divergence or delta spike adds confluence.
#   Filters  : H1 bias alignment; kill zone preferred but not mandatory.
#   Risk     : SL beyond swing high/low; TP at 127–162 % Fibonacci extension.
#   Status   : OBSERVATION_ONLY — requires Fibonacci swing detector (not yet implemented).
# ─────────────────────────────────────────────────────────────────────────────

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

    # MSS M1 — Market Structure Shift confirmation after liquidity sweep
    _m1_df = (frames or {}).get("M1")
    _mss = _check_mss_m1(_m1_df, direction)
    _mss_close: float | None = None
    _mss_recent_level: float | None = None
    if _m1_df is not None and not getattr(_m1_df, "empty", True) and len(_m1_df) >= 5:
        try:
            _mss_close = float(_m1_df.iloc[-1]["close"])
            if direction == "SELL":
                _mss_recent_level = float(_m1_df.iloc[-4:-1]["low"].min())
            else:
                _mss_recent_level = float(_m1_df.iloc[-4:-1]["high"].max())
        except (KeyError, TypeError, ValueError):
            pass
    if _mss is False and score < 85:
        log.info(
            "[MSS_M1] symbol=%s direction=%s mss_confirmed=False m1_close=%s"
            " recent_level=%s score=%s action=WAIT",
            symbol, direction, _mss_close, _mss_recent_level, score,
        )
        return _wait(symbol, "ORDER_FLOW_MSS_NOT_CONFIRMED", score=score)
    if _mss is True:
        score = min(100, score + 8)
    log.info(
        "[MSS_M1] symbol=%s direction=%s mss_confirmed=%s m1_close=%s"
        " recent_level=%s bonus=%s",
        symbol, direction, _mss, _mss_close, _mss_recent_level,
        8 if _mss is True else 0,
    )

    # H1 Bias Filter — blocks entries that contradict the H1 trend
    # Prefer h1_bias from context (explicitly passed); fallback to frames["H1"] same logic as MTFA
    _h1_bias_ctx = str((context or {}).get("h1_bias") or "").upper() or None
    _h1_bias_computed = _compute_h1_bias((frames or {}).get("H1"))
    _h1_bias = _h1_bias_ctx or _h1_bias_computed
    _h1_source = "context" if _h1_bias_ctx else ("frames_H1" if _h1_bias_computed else "none")
    _h1_conflict = (
        (_h1_bias == "BEARISH" and direction == "BUY")
        or (_h1_bias == "BULLISH" and direction == "SELL")
    )
    _h1_aligned = (
        (_h1_bias == "BULLISH" and direction == "BUY")
        or (_h1_bias == "BEARISH" and direction == "SELL")
    )
    if _h1_conflict:
        log.info(
            "[H1_BIAS] symbol=%s direction=%s h1_bias_source=%s h1_bias_value=%s action=WAIT",
            symbol, direction, _h1_source, _h1_bias,
        )
        return _wait(symbol, "ORDER_FLOW_H1_BIAS_CONFLICT", score=score)
    if _h1_aligned:
        score = min(100, score + 5)
    log.info(
        "[H1_BIAS] symbol=%s direction=%s h1_bias_source=%s h1_bias_value=%s aligned=%s bonus=%s",
        symbol, direction, _h1_source, _h1_bias, _h1_aligned, 5 if _h1_aligned else 0,
    )

    # Kill Zone bonus — high-volume BTC sessions
    _kz_active, _kz_name = _in_kill_zone(context)
    if _kz_active:
        score = min(100, score + 6)
    log.info(
        "[KILL_ZONE] symbol=%s zone=%s active=%s bonus=%s",
        symbol, _kz_name, _kz_active, 6 if _kz_active else 0,
    )

    # SFP — Swing Failure Pattern (M15)
    _m15_df = (frames or {}).get("M15")
    _sfp = _check_sfp(_m15_df, direction)
    if _sfp is True:
        score = min(100, score + 12)
    elif _sfp is False:
        score = max(0, score - 5)
    log.info(
        "[SFP] symbol=%s direction=%s sfp_confirmed=%s bonus=%s",
        symbol, direction, _sfp,
        12 if _sfp is True else (-5 if _sfp is False else 0),
    )

    min_score = int(getattr(settings, "order_flow_min_score", 75))
    if score < min_score:
        return _wait(symbol, "ORDER_FLOW_SCORE_BELOW_THRESHOLD", score=score)

    min_rr = float(getattr(settings, "order_flow_min_rr", 1.5))
    levels = _calc_sltp(direction, price, vwap, poc, vah, val, min_rr)
    if levels is None:
        log.info(
            "[OF_SLTP] symbol=%s verdict=INVALID direction=%s price=%s val=%s vah=%s",
            symbol, direction, price, val, vah,
        )
        return _wait(symbol, "ORDER_FLOW_INVALID_SLTP", score=score)

    entry, sl, tp, rr = levels
    log.info(
        "[OF_SLTP] symbol=%s verdict=%s direction=%s entry=%s sl=%s tp=%s rr=%s",
        symbol, "VALID" if rr >= min_rr else "RR_BELOW_MIN", direction, entry, sl, tp, rr,
    )
    if rr < min_rr:
        return _wait(symbol, "ORDER_FLOW_RR_BELOW_MIN", score=score)

    # AMD_FVG — active unmitigated FVG bonus + entry adjustment
    _m5_df = (frames or {}).get("M5")
    _fvg_bonus, _fvg_mid = _detect_fvg_bonus(_m5_df, direction)
    if _fvg_bonus > 0:
        score = min(100, score + _fvg_bonus)
    if _fvg_mid is not None and abs(entry - _fvg_mid) < entry * 0.005:
        _fvg_shift = round(_fvg_mid, 5) - entry
        entry = round(_fvg_mid, 5)
        sl = round(sl + _fvg_shift, 5)
        tp = round(tp + _fvg_shift, 5)
    log.info(
        "[AMD_FVG] symbol=%s direction=%s fvg_active=%s fvg_mid=%s bonus=%s",
        symbol, direction, _fvg_bonus > 0, _fvg_mid, _fvg_bonus,
    )

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
        "m1_entry_confirmation": _mss is not False,
        "m15_confirmation_status": "PASS",
        "m1_trigger_status": "PASS" if _mss is not False else "FAIL",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": (
            "MSS_M1_CONFIRMED" if _mss is True
            else "MSS_M1_NOT_CONFIRMED_OVERRIDE" if _mss is False
            else STRATEGY
        ),
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

    # Anchor the SL on the far side of BOTH the value level and the current
    # price. Anchoring on VAL/VAH alone put the SL on the wrong side whenever
    # price swept beyond the level - rejecting exactly the best sweep setups.
    if direction == "BUY":
        sl = min(val, price) - buffer
        risk = price - sl
        if risk <= 0:
            return None
        tp = price + risk * min_rr
        rr = (tp - price) / risk
    else:
        sl = max(vah, price) + buffer
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


def _check_mss_m1(m1_df: object, direction: str) -> bool | None:
    """
    Market Structure Shift on M1 — confirms reversal after a liquidity sweep.
    SELL: last M1 close < minimum low of the 3 previous candles (Lower Low).
    BUY : last M1 close > maximum high of the 3 previous candles (Higher High).
    Returns True (confirmed), False (not confirmed), or None (insufficient data).
    """
    if m1_df is None or getattr(m1_df, "empty", True) or len(m1_df) < 5:
        return None
    try:
        last_close = float(m1_df.iloc[-1]["close"])
        if direction == "SELL":
            recent_low = float(m1_df.iloc[-4:-1]["low"].min())
            return last_close < recent_low
        if direction == "BUY":
            recent_high = float(m1_df.iloc[-4:-1]["high"].max())
            return last_close > recent_high
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _compute_h1_bias(h1_df: object) -> str | None:
    """Mirror of MTFAFilter._h1_bias() — 12-candle window, excludes live candle."""
    if h1_df is None or getattr(h1_df, "empty", True):
        return None
    try:
        closed = h1_df.iloc[:-1]
        tail = closed.tail(12)
        if len(tail) < 6:
            return "NEUTRAL"
        prev = tail.iloc[:-1]
        last = tail.iloc[-1]
        prev_high = float(prev["high"].tail(5).max())
        prev_low = float(prev["low"].tail(5).min())
        close = float(last["close"])
        if close > prev_high:
            return "BULLISH"
        if close < prev_low:
            return "BEARISH"
        return "NEUTRAL"
    except (KeyError, TypeError, ValueError):
        return None


_KILL_ZONES: tuple[tuple[str, int, int], ...] = (
    ("LONDON_OPEN",    7,  9),
    ("NY_OPEN",       12, 14),
    ("ASIAN_REVERSAL", 1,  3),
)


def _in_kill_zone(context: dict | None) -> tuple[bool, str | None]:
    """
    Returns (True, zone_name) if the current UTC hour falls inside a BTC kill zone,
    (False, None) otherwise.  Prefers 'utc_hour' from context; falls back to datetime.now(UTC).
    """
    utc_hour_raw = (context or {}).get("utc_hour")
    if utc_hour_raw is not None:
        try:
            hour = int(utc_hour_raw)
        except (TypeError, ValueError):
            hour = datetime.now(timezone.utc).hour
    else:
        hour = datetime.now(timezone.utc).hour
    for name, start, end in _KILL_ZONES:
        if start <= hour < end:
            return True, name
    return False, None


def _check_sfp(m15_df: object, direction: str) -> bool | None:
    """
    Swing Failure Pattern on M15 — wick beyond recent extreme, close reverses.
    SELL: last closed candle wick > recent 13-candle high AND close < that high AND vol > avg*1.3
    BUY : last closed candle wick < recent 13-candle low  AND close > that low  AND vol > avg*1.3
    Returns True (SFP confirmed), False (sweep no reversal), None (no sweep — neutral).
    """
    if m15_df is None or getattr(m15_df, "empty", True) or len(m15_df) < 16:
        return None
    try:
        closed = m15_df.iloc[:-1]
        last = closed.iloc[-1]
        prev = closed.iloc[-14:-1]
        vol_col = "tick_volume" if "tick_volume" in closed.columns else "volume"
        vol_last = float(last.get(vol_col) or 0)
        vol_avg = float(prev[vol_col].mean()) if vol_col in prev.columns else 0.0
        vol_ok = vol_avg > 0 and vol_last > vol_avg * 1.3
        # Sweep must clear the level by a fraction of ATR so micro-wick noise
        # (sub-tolerance pokes) does not register as a liquidity sweep.
        atr_margin = 0.0
        atr_val = _atr_m15(closed)
        if atr_val is not None and atr_val > 0:
            # COEUR_V2 chantier 2 (2026-07-08): _atr_m15() migrated SMA->Wilder
            # RMA. Rescaled by 1/1.025855 (measured Wilder/SMA ratio,
            # GOLD#+BTCUSD# M5 30j blended) to preserve the same effective
            # margin — see COEUR_V2_REPORT.md chantier 2.
            atr_margin = 0.09748 * atr_val
        if direction == "SELL":
            recent_high = float(prev["high"].max())
            sweep = float(last["high"]) > recent_high + atr_margin
            sfp = sweep and float(last["close"]) < recent_high and vol_ok
            return True if sfp else (False if sweep else None)
        if direction == "BUY":
            recent_low = float(prev["low"].min())
            sweep = float(last["low"]) < recent_low - atr_margin
            sfp = sweep and float(last["close"]) > recent_low and vol_ok
            return True if sfp else (False if sweep else None)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _atr_m15(df: object, period: int = 14) -> float | None:
    """Last ATR value on a closed-candle frame; None when unusable. Wilder RMA
    (COEUR_V2 chantier 2, was SMA — see COEUR_V2_REPORT.md)."""
    if df is None or getattr(df, "empty", True) or len(df) < 4:
        return None
    try:
        from app.utils.indicators import atr_last
        effective = min(period, len(df) - 1)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        out = atr_last(pd.DataFrame({"high": high, "low": low, "close": close}), period=effective)
        return out if out is not None and math.isfinite(out) else None
    except (KeyError, TypeError, ValueError):
        return None


def _detect_fvg_bonus(m5_df: object, direction: str) -> tuple[int, float | None]:
    """
    Detect active unmitigated FVG on M5 (3-candle pattern, same logic as AMD_FVG module).
    BULLISH FVG: candle[-1].low > candle[-3].high  (gap up — buy-side)
    BEARISH FVG: candle[-1].high < candle[-3].low  (gap down — sell-side)
    Returns (bonus, fvg_midpoint): bonus=10 if FVG matches direction, else (0, None).
    """
    if m5_df is None or getattr(m5_df, "empty", True) or len(m5_df) < 3:
        return 0, None
    try:
        a = m5_df.iloc[-3]
        c = m5_df.iloc[-1]
        a_high = float(a["high"])
        a_low  = float(a["low"])
        c_low  = float(c["low"])
        c_high = float(c["high"])
        if direction == "BUY" and c_low > a_high:
            return 10, round((a_high + c_low) / 2, 5)
        if direction == "SELL" and c_high < a_low:
            return 10, round((a_low + c_high) / 2, 5)
    except (KeyError, TypeError, ValueError):
        return 0, None
    return 0, None
