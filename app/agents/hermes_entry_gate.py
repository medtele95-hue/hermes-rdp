"""
HERMES Entry Gates §4.1–4.3 — strategy-aware pre-trade filter.

§4.1  Three hard gates (structure-first):
  Gate A — Bias  : H4 direction must not clearly oppose the trade.
  Gate B — Structure: Last BOS/CHoCH must not be against the trade.
  Gate C — OB zone : Direction-aligned order block must be active
            (strict mode — SMC_NATIVE strategies only).

§4.2  Entry box (OB_zone ∩ OTE_band ∩ PD_half): Gates A+B+C passing together
      implies a non-empty entry box.  Precise OTE intersection requires candle
      frames and is handled at the strategy level (not duplicated here).

§4.3  Order-flow confirmation (ORDER_FLOW_NATIVE strategies only):
      Require ≥ 2 of 4: VWAP position, CVD_SLOPE sign, DELTA sign, DIVERGENCE.

Integration: called from setup_hunter._failed_gates() when the feature flag
`hermes_entry_gates_enabled` is True in Settings.

BTC_SCALPING_AGENT is explicitly excluded — it has its own gate system in
setup_hunter (btc_scalping_ready + confirmation_matrix).

All functions are pure (no side effects beyond logging) and are easily testable.
"""
from __future__ import annotations

import math

from app.logger import log

# ── Strategy sets (mirrors confluence_engine._ORDER_FLOW_NATIVE) ──────────
_ORDER_FLOW_NATIVE = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_ORDER_FLOW_CVD_VWAP",
})
_SMC_NATIVE = frozenset({
    "SIMO_ATM_BREAKOUT",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
})
# BTC_SCALPING_AGENT: existing btc_scalping_ready + confirmation_matrix in
# setup_hunter already performs the necessary gating — skip here.
_SKIP_STRATEGIES = frozenset({"BTC_SCALPING_AGENT"})

# §4.3 config (matches spec §7 defaults)
_CVD_MIN: float = 50.0
_OF_CONFIRM_REQUIRED: int = 2


# ── Public API ───────────────────────────────────────────────────────────────

def evaluate_entry_gates(strategy: str, direction: str, payload: dict) -> list[str]:
    """Return a list of failed gate reason strings; empty = all gates pass.

    Args:
        strategy:  Canonical strategy name (upper-case).
        direction: "BUY" or "SELL".
        payload:   Merged candidate dict from setup_hunter._candidate().
    """
    if strategy in _SKIP_STRATEGIES:
        return []
    if direction not in {"BUY", "SELL"}:
        return []

    if strategy in _ORDER_FLOW_NATIVE:
        return _check_of_confirmation(strategy, direction, payload)

    if strategy in _SMC_NATIVE:
        return _check_smc_gates(direction, payload, strict=True)

    # Default strategies: Gates A + B only (Gate C needs an active OB, which
    # is not guaranteed for non-SMC strategies).
    return _check_smc_gates(direction, payload, strict=False)


# ── Gate A — Bias ────────────────────────────────────────────────────────────

def check_gate_a(direction: str, payload: dict) -> str | None:
    """H4 direction must not clearly oppose the trade direction.

    Returns a failure reason string or None (pass).
    RANGE/UNKNOWN bias is ignored (no data → don't block).
    Only blocks when H4 conviction is clear (smc_confluence_score > 50).
    """
    h4 = str(payload.get("smc_h4_direction") or "").upper()
    if h4 in ("", "UNKNOWN", "RANGE"):
        return None

    buy = direction == "BUY"
    against = (buy and h4 == "BEARISH") or (not buy and h4 == "BULLISH")
    if not against:
        return None

    score = _f(payload.get("smc_confluence_score")) or 0.0
    if score > 50.0:
        log.info(
            "[ENTRY_GATE_A] direction=%s h4=%s smc_score=%.0f → BLOCK",
            direction, h4, score,
        )
        return "ENTRY_GATE_A_BIAS_AGAINST"
    return None


# ── Gate B — Structure event ──────────────────────────────────────────────────

def check_gate_b(direction: str, payload: dict) -> str | None:
    """Last confirmed H1 BOS/CHoCH must not be against the trade direction.

    Returns a failure reason string or None (pass).
    No structure event (NONE / missing) is treated as a pass.
    """
    h1_break = str(payload.get("smc_h1_break_structure") or "").upper()
    if not h1_break or h1_break == "NONE":
        return None

    buy = direction == "BUY"
    against = (buy and h1_break in {"BOS_DOWN", "CHOCH_DOWN"}) or \
              (not buy and h1_break in {"BOS_UP", "CHOCH_UP"})
    if against:
        log.info(
            "[ENTRY_GATE_B] direction=%s h1_break=%s → BLOCK",
            direction, h1_break,
        )
        return "ENTRY_GATE_B_STRUCTURE_AGAINST"
    return None


# ── Gate C — OB zone ──────────────────────────────────────────────────────────

def check_gate_c(direction: str, payload: dict) -> str | None:
    """H1 order block type must align with the trade direction.

    Returns a failure reason string or None (pass).
    No OB (NONE / missing) is treated as a pass (no data → don't block).
    """
    ob = str(payload.get("smc_h1_order_block") or "").upper()
    if not ob or ob == "NONE":
        return None

    buy = direction == "BUY"
    against = (buy and ob == "BEARISH_OB") or (not buy and ob == "BULLISH_OB")
    if against:
        log.info(
            "[ENTRY_GATE_C] direction=%s ob=%s → BLOCK",
            direction, ob,
        )
        return "ENTRY_GATE_C_OB_AGAINST"
    return None


# ── SMC gates wrapper ─────────────────────────────────────────────────────────

def _check_smc_gates(direction: str, payload: dict, *, strict: bool) -> list[str]:
    failures: list[str] = []
    a = check_gate_a(direction, payload)
    if a:
        failures.append(a)
    b = check_gate_b(direction, payload)
    if b:
        failures.append(b)
    if strict:
        c = check_gate_c(direction, payload)
        if c:
            failures.append(c)
    return failures


# ── §4.3 Order-flow confirmation ─────────────────────────────────────────────

def check_of_confirmation(strategy: str, direction: str, payload: dict) -> list[str]:
    """Require ≥ 2 of 4 order-flow confirmations for ORDER_FLOW_NATIVE strategies.

    Returns a list with one failure reason if the count is too low; else [].
    If no OF data is present at all, the check is skipped (fail-safe pass).
    """
    return _check_of_confirmation(strategy, direction, payload)


def _check_of_confirmation(strategy: str, direction: str, payload: dict) -> list[str]:
    of_data = _get_of_data(strategy, payload)
    if not isinstance(of_data, dict) or not of_data:
        return []  # no data → fail-safe pass

    buy = direction == "BUY"
    confirmations = 0
    any_data = False

    # 1. VWAP position: price reclaimed VWAP from below (BUY) or rejected from above (SELL)
    price = _f(payload.get("entry") or payload.get("price"))
    vwap = _f(of_data.get("vwap"))
    if price and vwap:
        any_data = True
        if (buy and price > vwap) or (not buy and price < vwap):
            confirmations += 1

    # 2. CVD slope in trade direction
    cvd_slope = _f(of_data.get("cvd_slope"))
    if cvd_slope is not None:
        any_data = True
        if (buy and cvd_slope > _CVD_MIN) or (not buy and cvd_slope < -_CVD_MIN):
            confirmations += 1

    # 3. Delta sign agrees with direction
    delta = _f(
        of_data.get("delta")
        or of_data.get("delta_proxy")
        or of_data.get("latest_delta")
    )
    if delta is not None:
        any_data = True
        if (buy and delta > 0) or (not buy and delta < 0):
            confirmations += 1

    # 4. Volume divergence in trade direction
    divergence = str(of_data.get("divergence") or "").lower()
    if divergence:
        any_data = True
        if (buy and divergence == "bull") or (not buy and divergence == "bear"):
            confirmations += 1

    if not any_data:
        return []  # no meaningful OF fields → fail-safe pass

    if confirmations < _OF_CONFIRM_REQUIRED:
        log.info(
            "[ENTRY_GATE_OF] strategy=%s direction=%s confirmations=%s required=%s → BLOCK",
            strategy, direction, confirmations, _OF_CONFIRM_REQUIRED,
        )
        return ["ENTRY_GATE_OF_CONFIRMATION_INSUFFICIENT"]

    return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_of_data(strategy: str, payload: dict) -> dict | None:
    """Return the most relevant order-flow data dict from the candidate payload."""
    for key in (
        "order_flow",
        "order_flow_execution_agent",
        "gold_order_flow_cvd_vwap",
        "order_flow_snapshot",
    ):
        val = payload.get(key)
        if isinstance(val, dict) and val:
            return val
    return None


def _f(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
