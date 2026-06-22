"""Shared BTC entry gate — validates all conditions before a new HERMES BTC demo entry.

Applies to both BTC_SCALPING_AGENT and ORDER_FLOW_EXECUTION_AGENT.
No MT5 calls here. Order execution stays exclusively in app/mt5/demo_router.py.
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.logger import log

_BTC_SYMBOLS: frozenset[str] = frozenset({"BTCUSD#", "BTCUSD"})
_BTC_SCALPING = "BTC_SCALPING_AGENT"
_ORDER_FLOW = "ORDER_FLOW_EXECUTION_AGENT"
_BTC_STRATEGIES = frozenset({_BTC_SCALPING, _ORDER_FLOW})

# Default minimum confidence thresholds (match existing config defaults)
_DEFAULT_SCALPING_MIN = 55
_DEFAULT_ORDER_FLOW_MIN = 75

# CVD threshold for "strong opposing impulse"
_CVD_STRONG = 0.5


def evaluate_btc_entry_gate(
    symbol: str,
    strategy: str,
    direction: str,
    market_context: dict | None,
    settings: Any,
    open_btc_count: int,
    spread: float | None = None,
    max_spread: float | None = None,
    confidence: float | None = None,
    closed_pnl_today: float | None = None,
    recent_btc_results: list[dict] | None = None,
) -> dict:
    """Run all shared BTC entry checks and return PASS or BLOCK.

    Returns:
        {"decision": "PASS",  "score": float, "strategy": ..., "passed_checks": [...]}
        {"decision": "BLOCK", "reason": str,  "failed_check": str, "strategy": ...}
    """
    sym   = str(symbol   or "").upper().strip()
    strat = str(strategy or "").upper().strip()
    dirn  = str(direction or "").upper().strip()

    # A. Symbol must be BTC
    if sym not in _BTC_SYMBOLS:
        return _block("SYMBOL_NOT_BTC", "SYMBOL_CHECK", strat)

    # B. Direction must be BUY or SELL
    if dirn not in {"BUY", "SELL"}:
        return _block("DIRECTION_NOT_BUY_OR_SELL", "DIRECTION_CHECK", strat)

    # C. Demo safety — live trading must be off
    if getattr(settings, "allow_live_trading", False):
        return _block("LIVE_TRADING_NOT_ALLOWED", "DEMO_SAFETY_CHECK", strat)
    if not getattr(settings, "demo_only", True):
        return _block("DEMO_ONLY_DISABLED", "DEMO_SAFETY_CHECK", strat)

    # D. Hard max open positions
    max_open = int(getattr(settings, "old_btc_max_open_positions", 1) or 1)
    if open_btc_count >= max_open:
        return _block("HERMES_BTC_POSITION_ALREADY_OPEN", "OPEN_POSITION_CHECK", strat)

    # E. Minimum strategy confidence / score
    if confidence is not None:
        if strat == _BTC_SCALPING:
            min_conf = int(
                getattr(settings, "old_btc_entry_gate_scalping_min_confidence", None)
                or getattr(settings, "btc_scalping_min_confidence", _DEFAULT_SCALPING_MIN)
            )
            if confidence < min_conf:
                return _block(
                    f"BTC_SCALPING_CONFIDENCE_BELOW_THRESHOLD",
                    "CONFIDENCE_CHECK",
                    strat,
                )
        elif strat == _ORDER_FLOW:
            min_score = int(
                getattr(settings, "old_btc_entry_gate_order_flow_min_score", None)
                or getattr(settings, "order_flow_min_score", _DEFAULT_ORDER_FLOW_MIN)
            )
            if confidence < min_score:
                return _block(
                    f"ORDER_FLOW_SCORE_BELOW_THRESHOLD",
                    "CONFIDENCE_CHECK",
                    strat,
                )

    # F. Spread
    if spread is not None and max_spread is not None and max_spread > 0:
        if spread > max_spread:
            return _block("SPREAD_TOO_WIDE", "SPREAD_CHECK", strat)

    # G. Market confirmation (soft — only blocks when context present AND strong opposing impulse)
    require_conf = bool(getattr(settings, "old_btc_entry_gate_require_market_confirmation", True))
    if require_conf and market_context:
        conf_block = _market_confirmation_block(dirn, market_context)
        if conf_block:
            return _block(conf_block, "MARKET_CONFIRMATION_CHECK", strat)

    # H. Daily loss limit — block new entries when day's closed P&L ≤ -$5
    if closed_pnl_today is not None and closed_pnl_today <= -5.00:
        log.info(
            "[BTC_ENTRY_GUARD] status=BLOCK reason=DAILY_LOSS_LIMIT"
            " closed_pnl_today=%.2f limit=-5.00 exits_allowed=true",
            closed_pnl_today,
        )
        return _block("DAILY_LOSS_LIMIT", "DAILY_LOSS_CHECK", strat)

    # I. Loss streak — block for 90 min when last 2 HERMES BTC closed trades are both losses
    if recent_btc_results is not None and len(recent_btc_results) >= 2:
        last_two = recent_btc_results[-2:]
        if all(float(r.get("profit") or 0) < 0 for r in last_two):
            streak_ts = float(last_two[-1].get("close_time") or 0)
            elapsed_minutes = (time.time() - streak_ts) / 60.0
            if elapsed_minutes < 90.0:
                log.info(
                    "[BTC_ENTRY_GUARD] status=BLOCK reason=LOSS_STREAK"
                    " losses=2 cooldown_minutes=90 exits_allowed=true",
                )
                return _block("LOSS_STREAK", "LOSS_STREAK_CHECK", strat)

    # PASS
    score = _compute_score(confidence, market_context, dirn)
    return {
        "decision": "PASS",
        "score": score,
        "strategy": strat,
        "direction": dirn,
        "symbol": sym,
        "passed_checks": [
            "SYMBOL_CHECK", "DIRECTION_CHECK", "DEMO_SAFETY_CHECK",
            "OPEN_POSITION_CHECK", "CONFIDENCE_CHECK", "SPREAD_CHECK",
        ],
    }


def _market_confirmation_block(direction: str, ctx: dict) -> str | None:
    """Return a block reason if a strong opposing impulse exists; else None."""
    price   = _f(ctx.get("price") or ctx.get("last_price") or ctx.get("bid"))
    vwap    = _f(ctx.get("vwap"))
    poc     = _f(ctx.get("poc"))
    cvd     = _f(ctx.get("cvd_slope"))
    m1_mom  = _label(ctx.get("m1_momentum") or ctx.get("m1_signal"))
    m5_mom  = _label(ctx.get("m5_momentum") or ctx.get("m5_signal"))
    of_sig  = _label(ctx.get("order_flow_signal") or ctx.get("signal"))

    if direction == "BUY":
        above_vwap = price is not None and vwap is not None and price >= vwap
        above_poc  = price is not None and poc is not None and price >= poc
        m1_bull    = m1_mom in {"BULLISH", "BUY", "UP"}
        m5_bull    = m5_mom in {"BULLISH", "BUY", "UP"}
        of_buy     = of_sig in {"BUY", "BULLISH"}
        bearish_impulse = cvd is not None and cvd < -_CVD_STRONG
        if bearish_impulse and not any([above_vwap, above_poc, m1_bull, m5_bull, of_buy]):
            return "STRONG_BEARISH_IMPULSE_NO_BUY_CONFIRMATION"
    else:
        below_vwap = price is not None and vwap is not None and price <= vwap
        below_poc  = price is not None and poc is not None and price <= poc
        m1_bear    = m1_mom in {"BEARISH", "SELL", "DOWN"}
        m5_bear    = m5_mom in {"BEARISH", "SELL", "DOWN"}
        of_sell    = of_sig in {"SELL", "BEARISH"}
        bullish_impulse = cvd is not None and cvd > _CVD_STRONG
        if bullish_impulse and not any([below_vwap, below_poc, m1_bear, m5_bear, of_sell]):
            return "STRONG_BULLISH_IMPULSE_NO_SELL_CONFIRMATION"
    return None


def _compute_score(confidence: float | None, ctx: dict | None, direction: str) -> float:
    score = float(confidence or 0.0)
    if not ctx:
        return score
    m1 = _label(ctx.get("m1_momentum") or ctx.get("m1_signal"))
    m5 = _label(ctx.get("m5_momentum") or ctx.get("m5_signal"))
    if direction == "BUY":
        if m1 in {"BULLISH", "BUY", "UP"}:
            score += 3.0
        if m5 in {"BULLISH", "BUY", "UP"}:
            score += 3.0
    else:
        if m1 in {"BEARISH", "SELL", "DOWN"}:
            score += 3.0
        if m5 in {"BEARISH", "SELL", "DOWN"}:
            score += 3.0
    return min(score, 100.0)


def _block(reason: str, failed_check: str, strategy: str) -> dict:
    return {"decision": "BLOCK", "reason": reason, "failed_check": failed_check, "strategy": strategy}


def _f(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _label(value: object) -> str:
    return str(value or "").upper().strip()
