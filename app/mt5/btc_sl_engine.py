"""HERMES SL ENGINE v1 — geometric trailing SL/TP modification for BTC positions.

Computes SL via five methods (ATR, Swing, VWAP, Fibonacci, Market Profile) and applies the
most protective result and applies it via demo_router.sl_engine_apply_modification.

Guardrails:
- Only modifies SL trailing — never widens against the position.
- Quick-Exit and Rescue conditions always take priority (enforced in run()).
- Fail-closed on every MT5 call.
- DEMO only. No direct MT5 order sending — routed exclusively through demo_router.py.
"""
from __future__ import annotations

import time

import MetaTrader5 as mt5

from app.config import (
    BTC_SL_BREAKEVEN_BUFFER_ATR,
    BTC_SL_TRAIL_GAP_ATR,
    BTC_SL_TRAIL_START_ATR,
)
from app.logger import log

MAX_SL_DOLLARS: float = 8.0
_MIN_SL_CHANGE: float = 0.01
_LOG_THROTTLE_S: float = 5.0


def _method_e_market_profile(
    entry_price: float,
    direction: str,
    atr: float,
    poc: float | None,
    vah: float | None,
    val: float | None,
    vwap: float | None = None,
) -> float | None:
    """SL from Market Profile auction levels (POC/VAH/VAL). Returns None when unavailable."""
    if poc is None or vah is None or val is None:
        log.debug("[SL_ENGINE_SKIP_E] reason=MARKET_PROFILE_UNAVAILABLE")
        return None

    vah_f = float(vah)
    val_f = float(val)
    dir_upper = direction.upper()
    price = float(entry_price)

    if dir_upper == "SELL":
        if val_f < price < vah_f:       # Balance Day
            sl = vah_f + atr * 0.45
        elif price > vah_f:              # Trend Day haussier
            sl = vah_f + atr * 0.2
        elif price < val_f:              # Trend Day baissier
            sl = val_f + atr * 0.2
        else:
            sl = vah_f + atr * 0.3
    else:  # BUY
        if val_f < price < vah_f:       # Balance Day
            sl = val_f - atr * 0.45
        elif price > vah_f:              # Trend Day haussier
            sl = vah_f - atr * 0.2
        elif price < val_f:              # Trend Day baissier
            sl = val_f - atr * 0.2
        else:
            sl = val_f - atr * 0.3

    return sl


def dollars_to_price(
    entry_price: float,
    sl_dollars: float,
    direction: str,
    lot: float = 0.01,
) -> float:
    """Convert a USD P&L distance to a price level for SL placement."""
    price_diff = sl_dollars / lot
    if direction.upper() == "BUY":
        return entry_price - price_diff
    return entry_price + price_diff  # SELL


def calculate_tp_price(
    entry_price: float,
    sl_price: float,
    direction: str,
    rr: float = 2.65,
) -> float:
    """Compute TP price from SL distance × RR ratio (max RR capped at 8.0)."""
    sl_dist = abs(entry_price - sl_price)
    tp_dist = sl_dist * min(rr, 8.0)
    if direction.upper() == "BUY":
        return entry_price + tp_dist
    return entry_price - tp_dist  # SELL


class BtcSlEngine:
    """Computes and applies trailing SL/TP to live HERMES BTC positions."""

    def __init__(self) -> None:
        # Throttle for repeated SL_ENGINE_SKIP logs: key → last log monotonic time
        self._skip_throttle: dict[str, float] = {}

    def _should_log_skip(self, key: str) -> bool:
        _now = time.monotonic()
        _last = self._skip_throttle.get(key)
        if _last is None or _now - _last >= _LOG_THROTTLE_S:
            self._skip_throttle[key] = _now
            return True
        return False

    def _calculate_sl_price(
        self,
        entry_price: float,
        direction: str,
        atr: float,
        candles_m5: list[dict] | None = None,
        vwap: float | None = None,
        poc: float | None = None,
        vah: float | None = None,
        val: float | None = None,
        current_price: float | None = None,
    ) -> tuple[float, str]:
        """Return (sl_price, method_name) for the most protective viable SL."""
        dir_upper = direction.upper()
        candles = candles_m5 or []
        methods: dict[str, float] = {}

        # Method A — ATR multiplier asymétrique (2.0 × SELL, 4.0 × BUY)
        sl_mult = 2.0 if dir_upper == "SELL" else 4.0
        if dir_upper == "SELL":
            methods["A"] = entry_price + atr * sl_mult
        else:
            methods["A"] = entry_price - atr * sl_mult

        if current_price is not None and float(current_price) > 0.0:
            price = float(current_price)
            favorable_move = entry_price - price if dir_upper == "SELL" else price - entry_price
            if favorable_move >= BTC_SL_TRAIL_START_ATR * atr:
                if dir_upper == "SELL":
                    breakeven = entry_price - BTC_SL_BREAKEVEN_BUFFER_ATR * atr
                    trailing = price + BTC_SL_TRAIL_GAP_ATR * atr
                    methods["A_TRAILING"] = min(breakeven, trailing)
                else:
                    breakeven = entry_price + BTC_SL_BREAKEVEN_BUFFER_ATR * atr
                    trailing = price - BTC_SL_TRAIL_GAP_ATR * atr
                    methods["A_TRAILING"] = max(breakeven, trailing)

        # Method B — Swing structure (last 5 of the 20 most-recent candles)
        if len(candles) >= 5:
            last_5 = candles[-5:]
            if dir_upper == "SELL":
                swing = max(float(c.get("high", 0) or 0) for c in last_5)
                methods["B"] = swing + atr * 0.3
            else:
                swing = min(
                    float(c.get("low", entry_price) or entry_price) for c in last_5
                )
                methods["B"] = swing - atr * 0.3

        # Method C — VWAP deviation (skip if no vwap)
        if vwap is not None and float(vwap) > 0:
            v = float(vwap)
            if dir_upper == "SELL":
                methods["C"] = v + 2.0 * atr
            else:
                methods["C"] = v - 2.0 * atr

        # Method D — Fibonacci swing (last 50 candles)
        if len(candles) >= 10:
            fib = candles[-50:]
            if dir_upper == "SELL":
                swing_high = max(float(c.get("high", 0) or 0) for c in fib)
                methods["D"] = swing_high * 1.001
            else:
                swing_low = min(
                    float(c.get("low", entry_price) or entry_price) for c in fib
                )
                methods["D"] = swing_low * 0.999

        # Method E — Market Profile (POC/VAH/VAL auction levels)
        sl_e = _method_e_market_profile(entry_price, direction, atr, poc, vah, val)
        if sl_e is not None:
            methods["E"] = sl_e

        # Fallback when no methods computed
        if not methods:
            cap = dollars_to_price(entry_price, MAX_SL_DOLLARS, direction)
            return cap, "HARD_CAP_FALLBACK"

        # Select most protective (tightest) SL
        if dir_upper == "SELL":
            best_method = min(methods, key=lambda k: methods[k])
        else:
            best_method = max(methods, key=lambda k: methods[k])
        sl_price = methods[best_method]

        # Hard cap: max MAX_SL_DOLLARS loss
        cap_sl = dollars_to_price(entry_price, MAX_SL_DOLLARS, direction)
        if dir_upper == "SELL" and sl_price > cap_sl:
            sl_price = cap_sl
            best_method = "HARD_CAP"
        elif dir_upper == "BUY" and sl_price < cap_sl:
            sl_price = cap_sl
            best_method = "HARD_CAP"

        # Anti-inversion guard
        if best_method != "A_TRAILING" and dir_upper == "SELL" and sl_price <= entry_price:
            sl_price = cap_sl
            best_method = "ANTI_INVERSION"
        elif best_method != "A_TRAILING" and dir_upper == "BUY" and sl_price >= entry_price:
            sl_price = cap_sl
            best_method = "ANTI_INVERSION"

        return sl_price, best_method

    def apply_sl_tp_to_mt5(
        self,
        ticket: int,
        sl_price: float,
        tp_price: float | None = None,
        symbol: str = "BTCUSD#",
    ) -> dict:
        """Apply SL/TP modification to an open MT5 position. Fail-closed."""
        try:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                log.warning("[SL_ENGINE_SKIP] ticket=%s reason=POSITION_NOT_FOUND", ticket)
                return {"applied": False, "reason": "POSITION_NOT_FOUND"}

            position = positions[0]
            current_sl = float(getattr(position, "sl", 0) or 0)
            current_tp = float(getattr(position, "tp", 0) or 0)
            new_sl = round(sl_price, 2)
            new_tp = round(tp_price, 2) if tp_price is not None else current_tp

            # Skip if SL not materially changed
            if abs(new_sl - current_sl) < _MIN_SL_CHANGE:
                if self._should_log_skip(f"{ticket}:SL_UNCHANGED:{round(new_sl, 2)}"):
                    log.info(
                        "[SL_ENGINE_SKIP] ticket=%s reason=SL_UNCHANGED sl=%.2f",
                        ticket, new_sl,
                    )
                return {"applied": False, "reason": "SL_UNCHANGED"}

            # SL trailing guard — never widen
            if current_sl > 0:
                pos_dir = "BUY" if int(getattr(position, "type", 1)) == 0 else "SELL"
                if pos_dir == "SELL" and new_sl > current_sl:
                    if self._should_log_skip(f"{ticket}:SL_WOULD_WIDEN:{round(new_sl, 2)}:{round(current_sl, 2)}"):
                        log.info(
                            "[SL_ENGINE_SKIP] ticket=%s reason=SL_WOULD_WIDEN"
                            " sl_new=%.2f sl_current=%.2f",
                            ticket, new_sl, current_sl,
                        )
                    return {"applied": False, "reason": "SL_WOULD_WIDEN"}
                elif pos_dir == "BUY" and new_sl < current_sl:
                    if self._should_log_skip(f"{ticket}:SL_WOULD_WIDEN:{round(new_sl, 2)}:{round(current_sl, 2)}"):
                        log.info(
                            "[SL_ENGINE_SKIP] ticket=%s reason=SL_WOULD_WIDEN"
                            " sl_new=%.2f sl_current=%.2f",
                            ticket, new_sl, current_sl,
                        )
                    return {"applied": False, "reason": "SL_WOULD_WIDEN"}

            log.info(
                "[SL_ENGINE_MODIFY] ticket=%s sl_old=%.2f sl_new=%.2f"
                " tp_old=%.2f tp_new=%.2f",
                ticket, current_sl, new_sl, current_tp, new_tp,
            )

            # Delegate to demo_router — position modification routed exclusively there
            from app.mt5.demo_router import sl_engine_apply_modification
            mod_result = sl_engine_apply_modification(ticket, new_sl, new_tp, symbol)
            success = bool(mod_result.get("success"))
            retcode = mod_result.get("retcode")
            comment = str(mod_result.get("comment") or "")

            log.info(
                "[SL_ENGINE_RESULT] ticket=%s retcode=%s comment=%s applied=%s",
                ticket, retcode, comment, success,
            )
            return {
                "applied": success,
                "sl": new_sl,
                "tp": new_tp,
                "retcode": retcode,
                "reason": "DONE" if success else (comment or "MODIFY_FAILED"),
            }

        except Exception as exc:
            log.warning("[SL_ENGINE_ERROR] ticket=%s error=%s", ticket, str(exc)[:200])
            return {"applied": False, "reason": str(exc)[:200]}

    def run(
        self,
        ticket: int,
        entry_price: float,
        direction: str,
        atr: float,
        current_profit: float,
        min_seen_profit: float,
        positive_count: int,
        be_buffer_usd: float,
        exit_mode: str,
        vwap: float | None = None,
        candles_m5: list[dict] | None = None,
        poc: float | None = None,
        vah: float | None = None,
        val: float | None = None,
        current_price: float | None = None,
    ) -> dict:
        """Orchestrate SL/TP modification. Returns result dict with applied/reason/sl/tp/method."""
        _empty = {"applied": False, "sl": None, "tp": None, "method": None}

        # Condition 1 — Quick Exit rescue mode active
        if min_seen_profit < -0.50 and current_profit > 0:
            if self._should_log_skip(f"{ticket}:RESCUE_MODE_ACTIVE"):
                log.info("[SL_ENGINE_SKIP] ticket=%s reason=RESCUE_MODE_ACTIVE", ticket)
            return {**_empty, "reason": "RESCUE_MODE_ACTIVE"}

        # Condition 2 — Positive candidates present (Quick Exit owns the position)
        if positive_count > 0:
            if self._should_log_skip(f"{ticket}:POSITIVE_CANDIDATES_PRESENT"):
                log.info("[SL_ENGINE_SKIP] ticket=%s reason=POSITIVE_CANDIDATES_PRESENT", ticket)
            return {**_empty, "reason": "POSITIVE_CANDIDATES_PRESENT"}

        # Condition 4 — Breakeven zone (too close to flat)
        if abs(current_profit) < be_buffer_usd:
            if self._should_log_skip(f"{ticket}:BREAKEVEN_ZONE"):
                log.info("[SL_ENGINE_SKIP] ticket=%s reason=BREAKEVEN_ZONE", ticket)
            return {**_empty, "reason": "BREAKEVEN_ZONE"}

        if atr <= 0:
            if self._should_log_skip(f"{ticket}:ATR_ZERO_OR_NEGATIVE"):
                log.warning("[SL_ENGINE_SKIP] ticket=%s reason=ATR_ZERO_OR_NEGATIVE", ticket)
            return {**_empty, "reason": "ATR_ZERO_OR_NEGATIVE"}

        sl_price, method = self._calculate_sl_price(
            entry_price, direction, atr, candles_m5, vwap, poc, vah, val, current_price
        )
        log.info(
            "[SL_ENGINE_METHOD_WIN] method=%s sl=%.2f ticket=%s",
            method, sl_price, ticket,
        )

        # Condition 3 — Dynamic exit owns TP; SL engine only touches SL
        tp_for_modify: float | None = None
        if exit_mode == "dynamic":
            log.debug(
                "[SL_ENGINE_TP_SKIP] ticket=%s reason=DYNAMIC_EXIT_OWNS_TP", ticket
            )
        else:
            tp_for_modify = calculate_tp_price(entry_price, sl_price, direction)

        result = self.apply_sl_tp_to_mt5(ticket, sl_price, tp_for_modify)
        result["method"] = method
        result["sl"] = sl_price
        result["tp"] = tp_for_modify  # None in dynamic mode
        return result
