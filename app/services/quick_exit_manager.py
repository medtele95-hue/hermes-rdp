from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuickExitConfig:
    tp_usd: float = 1.50
    lock_usd: float = 0.80
    be_buffer_usd: float = 0.10
    trail_start_usd: float = 1.00
    trail_gap_usd: float = 0.60
    magic_number: int = 909002
    demo_only: bool = True


def manage_quick_exit_position(pos: Any, tick: Any, symbol_info: Any, cfg: QuickExitConfig, state: dict) -> dict:
    ticket = getattr(pos, "ticket", None)
    symbol = str(getattr(pos, "symbol", "") or "")
    volume = _float(getattr(pos, "volume", None))
    entry = _float(getattr(pos, "price_open", None))
    if not symbol or ticket is None or volume is None or volume <= 0 or entry is None or tick is None or symbol_info is None:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "QUICK_EXIT_POSITION_DATA_INVALID"}

    side = _position_side(pos)
    if side not in {"BUY", "SELL"}:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "QUICK_EXIT_POSITION_SIDE_UNKNOWN"}

    exit_price = _exit_price(side, tick)
    value_per_price_unit = _value_per_price_unit(symbol_info, volume)
    if exit_price is None:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "QUICK_EXIT_TICK_MISSING"}
    if value_per_price_unit is None or value_per_price_unit <= 0:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "QUICK_EXIT_SYMBOL_SPEC_INVALID"}

    direction = 1 if side == "BUY" else -1
    profit_usd = (exit_price - entry) * direction * value_per_price_unit
    st = state.setdefault(int(ticket), {"peak_usd": profit_usd, "be_done": False})
    st["peak_usd"] = max(float(st.get("peak_usd") or profit_usd), profit_usd)

    base = {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit_price": exit_price,
        "volume": volume,
        "profit_usd": round(profit_usd, 2),
        "peak_usd": round(float(st["peak_usd"]), 2),
        "magic": getattr(pos, "magic", None),
    }
    if profit_usd >= cfg.tp_usd:
        return {**base, "action": "CLOSE_TP", "reason": "QUICK_EXIT_TP_USD_REACHED"}

    current_sl = _float(getattr(pos, "sl", 0.0)) or 0.0
    current_tp = _float(getattr(pos, "tp", 0.0)) or 0.0
    digits = int(_float(getattr(symbol_info, "digits", 5)) or 5)
    new_sl = current_sl
    action = "HOLD"
    reason = "QUICK_EXIT_HOLD"

    if not bool(st.get("be_done")) and profit_usd >= cfg.lock_usd:
        be_dist = _price_distance_for_usd(cfg.be_buffer_usd, value_per_price_unit)
        be_price = entry + direction * be_dist
        if _improves_sl(side, be_price, current_sl):
            new_sl = be_price
            st["be_done"] = True
            action = "MOVE_BREAKEVEN"
            reason = "QUICK_EXIT_BREAKEVEN_LOCK"

    if float(st["peak_usd"]) >= cfg.trail_start_usd:
        lock_usd = float(st["peak_usd"]) - cfg.trail_gap_usd
        if lock_usd > cfg.be_buffer_usd:
            trail_dist = _price_distance_for_usd(lock_usd, value_per_price_unit)
            trail_price = entry + direction * trail_dist
            if _improves_sl(side, trail_price, new_sl):
                new_sl = trail_price
                action = "TRAIL_SL"
                reason = "QUICK_EXIT_TRAILING_LOCK"

    if action in {"MOVE_BREAKEVEN", "TRAIL_SL"} and new_sl and new_sl != current_sl:
        return {
            **base,
            "action": action,
            "reason": reason,
            "new_sl": round(new_sl, digits),
            "current_sl": current_sl,
            "current_tp": current_tp,
        }
    return {**base, "action": "HOLD", "reason": "QUICK_EXIT_HOLD", "current_sl": current_sl, "current_tp": current_tp}


def _position_side(pos: Any) -> str | None:
    value = getattr(pos, "type", None)
    if value == 0 or str(value).upper() in {"BUY", "POSITION_TYPE_BUY"}:
        return "BUY"
    if value == 1 or str(value).upper() in {"SELL", "POSITION_TYPE_SELL"}:
        return "SELL"
    return None


def _exit_price(side: str, tick: Any) -> float | None:
    return _float(getattr(tick, "bid", None) if side == "BUY" else getattr(tick, "ask", None))


def _value_per_price_unit(symbol_info: Any, volume: float) -> float | None:
    tick_value = _float(getattr(symbol_info, "trade_tick_value", None))
    tick_size = _float(getattr(symbol_info, "trade_tick_size", None))
    if tick_value is not None and tick_size not in {None, 0}:
        return tick_value / tick_size * volume
    contract_size = _float(getattr(symbol_info, "trade_contract_size", None))
    if contract_size is not None and contract_size > 0:
        return contract_size * volume
    return None


def _price_distance_for_usd(usd: float, value_per_price_unit: float) -> float:
    return float(usd) / value_per_price_unit


def _improves_sl(side: str, proposed_sl: float, current_sl: float) -> bool:
    if side == "BUY":
        return current_sl == 0 or proposed_sl > current_sl
    return current_sl == 0 or proposed_sl < current_sl


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# §5 — Dynamic Quick-Exit: R-multiple state machine
# ---------------------------------------------------------------------------

_PHASE_OPEN = "P0_OPEN"
_PHASE_SECURE = "P1_SECURE"
_PHASE_LOCK = "P2_LOCK"
_PHASE_RUNNER = "P3_RUNNER"

_T_MAX_SECONDS: float = 1800.0   # 30-minute time stop
_R_FLOOR_TIME: float = 0.3       # time stop only if R_now >= 0.3
_R_SECURE: float = 0.6           # breakeven trigger
_R_LOCK: float = 1.0             # chandelier trailing trigger
_R_RUNNER: float = 1.5           # tight trailing trigger


def hermes_dynamic_exit(
    pos: Any,
    tick: Any,
    symbol_info: Any,
    cfg: QuickExitConfig,
    state: dict,
    exit_context: dict | None = None,
) -> dict:
    """§5 R-multiple state machine exit manager.

    Replaces the USD-based quick_exit for ORDER_FLOW_NATIVE strategies.
    State keys per ticket: phase, peak_price, be_done, initial_sl.
    """
    ticket = getattr(pos, "ticket", None)
    symbol = str(getattr(pos, "symbol", "") or "")
    volume = _float(getattr(pos, "volume", None))
    entry = _float(getattr(pos, "price_open", None))
    entry_time_sec = _float(getattr(pos, "time", None))

    if not symbol or ticket is None or volume is None or volume <= 0 or entry is None or tick is None:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "DYNAMIC_EXIT_DATA_INVALID"}

    side = _position_side(pos)
    if side not in {"BUY", "SELL"}:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "DYNAMIC_EXIT_SIDE_UNKNOWN"}

    direction = 1 if side == "BUY" else -1
    exit_price = _exit_price(side, tick)
    if exit_price is None:
        return {"ticket": ticket, "symbol": symbol, "action": "SKIP", "reason": "DYNAMIC_EXIT_TICK_MISSING"}

    current_sl = _float(getattr(pos, "sl", 0.0)) or 0.0
    current_tp = _float(getattr(pos, "tp", 0.0)) or 0.0
    digits = int(_float(getattr(symbol_info, "digits", 2)) or 2) if symbol_info is not None else 2

    # Initialise per-ticket state on first sight
    st = state.setdefault(int(ticket), {
        "phase": _PHASE_OPEN,
        "peak_price": exit_price,
        "be_done": False,
        "initial_sl": current_sl,
    })
    # Track peak price (direction-aware: highest bid for BUY, lowest ask for SELL)
    if side == "BUY":
        st["peak_price"] = max(float(st.get("peak_price") or exit_price), exit_price)
    else:
        st["peak_price"] = min(float(st.get("peak_price") or exit_price), exit_price)
    peak_price = float(st["peak_price"])

    # Derive initial R from stored initial SL (or current SL if first tick)
    initial_sl = _float(st.get("initial_sl")) or current_sl
    if initial_sl and initial_sl != current_sl and current_sl != 0.0:
        # Only update initial_sl once (on the first tick where sl is set)
        if not _float(st.get("initial_sl")):
            st["initial_sl"] = current_sl
            initial_sl = current_sl
    initial_R = abs(entry - initial_sl) if initial_sl else 0.0

    if initial_R <= 0.0:
        # Can't compute R-multiples without a valid initial SL — fall back to HOLD
        return {"ticket": ticket, "symbol": symbol, "action": "HOLD", "reason": "DYNAMIC_EXIT_NO_INITIAL_R",
                "current_sl": current_sl, "current_tp": current_tp, "exit_price": exit_price}

    # Current R-multiple
    R_now = (exit_price - entry) * direction / initial_R

    base = {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit_price": exit_price,
        "current_sl": current_sl,
        "current_tp": current_tp,
        "R_now": round(R_now, 3),
        "phase": st.get("phase", _PHASE_OPEN),
        "peak_price": round(peak_price, digits),
        "initial_R": round(initial_R, digits),
        "magic": getattr(pos, "magic", None),
    }

    # --- Danger score (§5.4) ---
    danger = _danger_score(exit_context, initial_R, entry, exit_price, direction, tick)
    base["danger_score"] = danger
    if danger >= 4:
        return {**base, "action": "CLOSE_DANGER", "reason": f"DANGER_SCORE_{danger}"}

    # --- Time stop (§5.5) ---
    if entry_time_sec is not None and R_now >= _R_FLOOR_TIME:
        now_sec = time.time()
        elapsed = now_sec - entry_time_sec
        if elapsed >= _T_MAX_SECONDS:
            return {**base, "action": "CLOSE_TIME_STOP", "reason": "TIME_STOP_EXPIRED",
                    "elapsed_seconds": round(elapsed)}

    # --- Phase state machine ---
    new_sl = current_sl
    action = "HOLD"
    reason = "DYNAMIC_EXIT_HOLD"

    # P1 SECURE: breakeven once R_now >= 0.6
    if R_now >= _R_SECURE and not bool(st.get("be_done")):
        spread_half = abs(_float(getattr(tick, "ask", exit_price) or exit_price) -
                         _float(getattr(tick, "bid", exit_price) or exit_price)) * 0.5
        be_price = entry + direction * max(spread_half, initial_R * 0.05)
        if _improves_sl(side, be_price, current_sl):
            new_sl = be_price
            st["be_done"] = True
            st["phase"] = _PHASE_SECURE
            action = "MOVE_BREAKEVEN"
            reason = "DYNAMIC_EXIT_BREAKEVEN_R06"

    # P2 LOCK: chandelier trailing at R_now >= 1.0
    if R_now >= _R_LOCK:
        m_eff = max(1.2, 3.0 - 0.25 * R_now)
        trail_sl = peak_price - direction * m_eff * initial_R
        if _improves_sl(side, trail_sl, new_sl):
            new_sl = trail_sl
            if st.get("phase") not in {_PHASE_LOCK, _PHASE_RUNNER}:
                st["phase"] = _PHASE_LOCK
            action = "TRAIL_SL"
            reason = f"DYNAMIC_EXIT_CHANDELIER_M{round(m_eff, 2)}"

    # P3 RUNNER: tight trailing at R_now >= 1.5
    if R_now >= _R_RUNNER:
        trail_sl_tight = peak_price - direction * 1.2 * initial_R
        if _improves_sl(side, trail_sl_tight, new_sl):
            new_sl = trail_sl_tight
            st["phase"] = _PHASE_RUNNER
            action = "TRAIL_SL"
            reason = "DYNAMIC_EXIT_RUNNER_M1.2"

    if action in {"MOVE_BREAKEVEN", "TRAIL_SL"} and new_sl and new_sl != current_sl:
        return {
            **base,
            "action": action,
            "reason": reason,
            "new_sl": round(new_sl, digits),
            "phase": st.get("phase", _PHASE_OPEN),
        }
    return {**base, "action": "HOLD", "reason": "DYNAMIC_EXIT_HOLD"}


def _danger_score(
    ctx: dict | None,
    initial_R: float,
    entry: float,
    exit_price: float,
    direction: int,
    tick: Any,
) -> int:
    """Compute danger score from exit_context signals (max 9, exit at >= 4)."""
    if not ctx:
        return 0
    score = 0
    if ctx.get("cvd_flip"):
        score += 1
    if ctx.get("adverse_delta"):
        score += 2
    if ctx.get("ob_invalidated"):
        score += 1
    if ctx.get("opposing_fvg"):
        score += 1
    if ctx.get("choch"):
        score += 2
    # ATR shock: compare current_atr vs baseline_atr from context
    current_atr = _float(ctx.get("current_atr")) or 0.0
    baseline_atr = _float(ctx.get("baseline_atr")) or current_atr
    if current_atr > 0 and baseline_atr > 0 and current_atr > 2.0 * baseline_atr:
        score += 1
    if ctx.get("approaching_pool"):
        score += 1
    # §5.3 B9: validated absorption against open position = strong reversal signal
    _absorption_ctx = ctx.get("cvd_absorption")
    if isinstance(_absorption_ctx, dict) and _absorption_ctx.get("valid"):
        from app.quant.absorption_detector import absorption_against_position as _absorption_against
        _pos_dir = "BUY" if direction > 0 else "SELL"
        if _absorption_against(_pos_dir, _absorption_ctx):
            score += 3
    return score
