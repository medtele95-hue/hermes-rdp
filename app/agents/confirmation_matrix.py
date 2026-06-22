"""Phase 3A: SMC/MTFA confirmation calibration matrix.

Recalibrates SMC and MTFA from absolute hard blockers into professional
confirmation scores with PASS / SOFT_FAIL / STRONG_FAIL tiers.

Thresholds
----------
SMC:  PASS >= 70, SOFT_FAIL 40-69, STRONG_FAIL < 40
MTFA: PASS >= 60, SOFT_FAIL 35-59, STRONG_FAIL < 35

Routing rules
-------------
SOFT_FAIL   -> warning + score penalty only; never hard-blocks.
STRONG_FAIL -> hard-blocks by default.
  Exception (§4.6): ORDER_FLOW_NATIVE strategies with of_score >= 90 convert
  STRONG_FAIL from hard-block to soft penalty only ([SMC_ARBITRATOR_OF_OVERRIDE]).

Confluence adjustments
----------------------
PASS:        +10
SOFT_FAIL:    -5
STRONG_FAIL: -15
"""
from __future__ import annotations

from app.logger import log

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_SMC_PASS_THRESHOLD = 70
_SMC_SOFT_FAIL_THRESHOLD = 40
_MTFA_PASS_THRESHOLD = 60
_MTFA_SOFT_FAIL_THRESHOLD = 35

_ADJUSTMENTS = {"PASS": 10.0, "SOFT_FAIL": -5.0, "STRONG_FAIL": -15.0}

# §4.6 — strategies that run on order-flow signals; STRONG_FAIL waivable at high OF score
_ORDER_FLOW_NATIVE = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "BTC_SCALPING_AGENT",
    "GOLD_ORDER_FLOW_CVD_VWAP",
})


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def smc_calibrated_status(score: float) -> str:
    """Return PASS / SOFT_FAIL / STRONG_FAIL for an SMC score."""
    if score >= _SMC_PASS_THRESHOLD:
        return "PASS"
    if score >= _SMC_SOFT_FAIL_THRESHOLD:
        return "SOFT_FAIL"
    return "STRONG_FAIL"


def mtfa_calibrated_status(score: float) -> str:
    """Return PASS / SOFT_FAIL / STRONG_FAIL for an MTFA score."""
    if score >= _MTFA_PASS_THRESHOLD:
        return "PASS"
    if score >= _MTFA_SOFT_FAIL_THRESHOLD:
        return "SOFT_FAIL"
    return "STRONG_FAIL"


def smc_confluence_adjustment(status: str) -> float:
    """Return the confluence score delta for a given SMC calibrated status."""
    return _ADJUSTMENTS.get(status, 0.0)


def mtfa_confluence_adjustment(status: str) -> float:
    """Return the confluence score delta for a given MTFA calibrated status."""
    return _ADJUSTMENTS.get(status, 0.0)


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate(
    symbol: str,
    strategy: str,
    smc_score: float,
    mtfa_score: float,
    setup_score: float,
    rr: float | None,
    of_score: float = 0.0,
) -> dict:
    """Evaluate the confirmation matrix and emit required structured logs.

    Args:
        symbol:      instrument symbol
        strategy:    strategy name
        smc_score:   raw SMC confluence score 0-100
        mtfa_score:  raw MTFA score 0-100
        setup_score: candidate setup/edge score 0-100 (proxy for total confluence)
        rr:          risk/reward ratio, or None if not computed
        of_score:    order-flow agent score 0-100 (used for §4.6 override)

    Returns a dict with:
        smc_calibrated_status   PASS | SOFT_FAIL | STRONG_FAIL
        mtfa_calibrated_status  PASS | SOFT_FAIL | STRONG_FAIL
        hard_block              bool
        hard_block_reason       str
    """
    smc_status = smc_calibrated_status(smc_score)
    mtfa_status = mtfa_calibrated_status(mtfa_score)

    rr_ok = rr is not None and rr >= 1.5
    confluence_ok = setup_score >= 75

    # §4.6 — ORDER_FLOW_NATIVE + of_score >= 90 converts STRONG_FAIL hard-block to soft penalty
    _of_native = str(strategy or "").upper() in _ORDER_FLOW_NATIVE
    _of_override = _of_native and float(of_score or 0.0) >= 90.0
    if _of_override:
        log.info(
            "[SMC_ARBITRATOR_OF_OVERRIDE] symbol=%s strategy=%s"
            " smc_status=%s mtfa_status=%s of_score=%.1f"
            " → converting hard_block to soft_penalty",
            symbol, strategy, smc_status, mtfa_status, float(of_score),
        )
        smc_hard = False
        mtfa_hard = False
    else:
        order_flow = str(strategy or "").upper() == "ORDER_FLOW_EXECUTION_AGENT"
        smc_hard = smc_status == "STRONG_FAIL" and (order_flow or not (confluence_ok and rr_ok))
        mtfa_hard = mtfa_status == "STRONG_FAIL" and (order_flow or not (confluence_ok and rr_ok))
    hard_block = smc_hard or mtfa_hard

    reasons: list[str] = []
    if _of_override:
        reasons.append("OF_OVERRIDE_APPLIED")
    elif smc_hard:
        reasons.append("SMC_STRONG_FAIL")
    if mtfa_hard:
        reasons.append("MTFA_STRONG_FAIL")
    hard_block_reason = ",".join(reasons) if reasons else "NONE"

    log.info(
        "[CONFIRMATION_MATRIX] symbol=%s strategy=%s smc_status=%s"
        " mtfa_status=%s hard_block=%s reason=%s",
        symbol,
        strategy,
        smc_status,
        mtfa_status,
        str(hard_block).lower(),
        hard_block_reason,
    )

    if smc_status == "SOFT_FAIL" or mtfa_status == "SOFT_FAIL":
        log.info(
            "[CONFIRMATION_WARNING] symbol=%s strategy=%s"
            " reason=SMC_MTFA_SOFT_FAIL_SCORE_PENALTY_ONLY",
            symbol,
            strategy,
        )

    if hard_block:
        log.info(
            "[CONFIRMATION_BLOCK] symbol=%s strategy=%s"
            " reason=%s",
            symbol,
            strategy,
            hard_block_reason,
        )

    return {
        "smc_calibrated_status": smc_status,
        "mtfa_calibrated_status": mtfa_status,
        "hard_block": hard_block,
        "hard_block_reason": hard_block_reason,
    }
