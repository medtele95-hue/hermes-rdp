"""BTC Setup Intelligence Engine — shared quality scoring for all HERMES BTC entries.

Evaluates each BTC setup candidate using all available HERMES confirmations
and returns a structured decision with grade, score, exit profile, and entry mode.

Applies to: BTC_SCALPING_AGENT and ORDER_FLOW_EXECUTION_AGENT (and all 13+ strategies
when operating on BTCUSD# with magic=909002 and comment containing HERMES).

No MT5 calls. No order execution. Purely analytical.
All execution remains exclusively in app/mt5/demo_router.py.
"""
from __future__ import annotations

import math
from typing import Any

from app.logger import log
from app.mt5.btc_market_narrator import BtcMarketNarrator, BtcNarratorInput

# ─── Grade thresholds ────────────────────────────────────────────────────────
_GRADE_APLUS_MIN = 85
_GRADE_A_MIN = 75
_GRADE_B_MIN = 65
_GRADE_C_MIN = 55

# ─── Known BTC strategy names ────────────────────────────────────────────────
_BTC_SCALPING = "BTC_SCALPING_AGENT"
_ORDER_FLOW = "ORDER_FLOW_EXECUTION_AGENT"

# Adaptive mode thresholds (mirrors btc_performance_memory constants)
_WIN_STREAK_CONFIDENT = 3

# Minimum score for grade B to be eligible for narrative upgrade to A
_STRONG_B_THRESHOLD = 70.0

# CVD slope thresholds
_CVD_STRONG = 0.3
_CVD_MILD = 0.1

# Delta strongly directional threshold
_DELTA_STRONG = 500.0

# VWAP/POC reclaim zone (within 0.1% is considered "at level")
_LEVEL_RECLAIM_ZONE = 0.001

# Spread: fraction of max_spread above which we penalise
_SPREAD_WARNING_RATIO = 0.70
_SPREAD_HARD_RATIO = 0.95

# Score weights (must sum to 100)
_W_SESSION    = 8
_W_SPREAD     = 8
_W_ORDER_FLOW = 15
_W_CVD        = 8
_W_DELTA      = 5
_W_VWAP       = 10
_W_POC        = 10
_W_M1         = 10
_W_M5         = 10
_W_REJECTION  = 5
_W_SMC_MTFA   = 6
_W_VOLATILITY = 5
# Total: 100


def evaluate_btc_setup_intelligence(
    symbol: str,
    strategy: str,
    direction: str,
    candidate: dict | None,
    market_context: dict | None,
    strategy_context: dict | None,
    recent_performance: dict | None,
    narrator_input: BtcNarratorInput | None = None,
) -> dict:
    """Evaluate a BTC setup and return quality decision.

    Args:
        symbol: broker symbol, e.g. "BTCUSD#"
        strategy: strategy name
        direction: "BUY" or "SELL"
        candidate: trade candidate dict (entry, sl, tp, confidence, smc_score, etc.)
        market_context: live market data (vwap, poc, cvd_slope, delta, m1/m5 momentum, etc.)
        strategy_context: strategy-specific extras (session, volatility, etc.)
        recent_performance: output of BtcPerformanceMemory.get_performance_summary()

    Returns dict with keys:
        decision: "PASS" | "BLOCK" | "WAIT"
        setup_quality_score: 0-100
        grade: "A+" | "A" | "B" | "C" | "D"
        reasons: list of scored factor names
        entry_mode: "AGGRESSIVE" | "NORMAL" | "DEFENSIVE" | "PAUSED"
        confidence_multiplier: float
        exit_profile: "FAST_POSITIVE" | "NORMAL_SCALP" | "HOLD_IF_STRONG"
    """
    ctx = dict(market_context or {})
    cand = dict(candidate or {})
    s_ctx = dict(strategy_context or {})
    perf = dict(recent_performance or {})

    dirn = str(direction or "").upper().strip()
    strat = str(strategy or "").upper().strip()

    if dirn not in {"BUY", "SELL"}:
        return _result(0, "D", "BLOCK", "NORMAL", 0.3, "FAST_POSITIVE",
                       ["INVALID_DIRECTION"])

    reasons: list[str] = []
    score = 0.0

    # ── 1. Session quality ────────────────────────────────────────────────────
    session = str(
        s_ctx.get("session") or ctx.get("session") or cand.get("session") or ""
    ).upper()
    sess_score = _session_score(session)
    score += sess_score * _W_SESSION
    if sess_score >= 1.0:
        reasons.append("SESSION_GOOD")
    elif sess_score >= 0.5:
        reasons.append("SESSION_OK")
    else:
        reasons.append("SESSION_WEAK")

    # ── 2. Spread acceptable ──────────────────────────────────────────────────
    spread = _f(ctx.get("spread"))
    max_sp = _f(ctx.get("max_spread") or s_ctx.get("max_spread"))
    spread_frac = _spread_fraction(spread, max_sp)
    sp_score = _spread_score(spread_frac)
    score += sp_score * _W_SPREAD
    if sp_score >= 1.0:
        reasons.append("SPREAD_OK")
    elif sp_score >= 0.5:
        reasons.append("SPREAD_WARNING")
    else:
        reasons.append("SPREAD_TOO_WIDE")

    # ── 3. Order flow direction ───────────────────────────────────────────────
    of_sig = _label(
        ctx.get("order_flow_signal") or ctx.get("signal") or cand.get("order_flow_signal")
    )
    of_score = _order_flow_score(of_sig, dirn)
    score += of_score * _W_ORDER_FLOW
    if of_score >= 1.0:
        reasons.append(f"ORDER_FLOW_{dirn}")
    elif of_score >= 0.5:
        reasons.append("ORDER_FLOW_NEUTRAL")
    else:
        reasons.append("ORDER_FLOW_AGAINST")

    # ── 4. CVD slope ─────────────────────────────────────────────────────────
    cvd = _f(ctx.get("cvd_slope") or ctx.get("cvd_proxy"))
    cvd_score = _cvd_score(cvd, dirn)
    score += cvd_score * _W_CVD
    if cvd_score >= 1.0:
        reasons.append("CVD_STRONG_ALIGNED")
    elif cvd_score >= 0.6:
        reasons.append("CVD_MILD_ALIGNED")
    elif cvd_score >= 0.25:
        reasons.append("CVD_NEUTRAL")
    else:
        reasons.append("CVD_OPPOSING")

    # ── 5. Delta direction ────────────────────────────────────────────────────
    delta = _f(ctx.get("delta_proxy") or ctx.get("latest_delta") or ctx.get("delta"))
    delta_score = _delta_score(delta, dirn)
    score += delta_score * _W_DELTA
    if delta_score >= 1.0:
        reasons.append("DELTA_ALIGNED")
    elif delta_score >= 0.4:
        reasons.append("DELTA_NEUTRAL")
    else:
        reasons.append("DELTA_OPPOSING")

    # ── 6. VWAP relation ─────────────────────────────────────────────────────
    price = _f(ctx.get("price") or ctx.get("last_price") or ctx.get("bid") or cand.get("entry"))
    vwap = _f(ctx.get("vwap"))
    vwap_score = _level_relation_score(price, vwap, dirn)
    score += vwap_score * _W_VWAP
    if vwap_score >= 1.0:
        reasons.append("PRICE_ABOVE_VWAP" if dirn == "BUY" else "PRICE_BELOW_VWAP")
    elif vwap_score >= 0.5:
        reasons.append("PRICE_AT_VWAP_RECLAIM")
    else:
        reasons.append("PRICE_AGAINST_VWAP")

    # ── 7. POC relation ───────────────────────────────────────────────────────
    poc = _f(ctx.get("poc"))
    poc_score = _level_relation_score(price, poc, dirn)
    score += poc_score * _W_POC
    if poc_score >= 1.0:
        reasons.append("PRICE_ABOVE_POC" if dirn == "BUY" else "PRICE_BELOW_POC")
    elif poc_score >= 0.5:
        reasons.append("PRICE_AT_POC_RECLAIM")
    else:
        reasons.append("PRICE_AGAINST_POC")

    # ── 8. M1 momentum ───────────────────────────────────────────────────────
    m1 = _label(ctx.get("m1_momentum") or ctx.get("m1_signal"))
    m1_score = _momentum_score(m1, dirn)
    score += m1_score * _W_M1
    if m1_score >= 1.0:
        reasons.append(f"M1_MOMENTUM_{dirn}")
    elif m1_score >= 0.3:
        reasons.append("M1_MOMENTUM_NEUTRAL")
    else:
        reasons.append(f"M1_MOMENTUM_AGAINST_{dirn}")

    # ── 9. M5 momentum ───────────────────────────────────────────────────────
    m5 = _label(ctx.get("m5_momentum") or ctx.get("m5_signal"))
    m5_score = _momentum_score(m5, dirn)
    score += m5_score * _W_M5
    if m5_score >= 1.0:
        reasons.append(f"M5_MOMENTUM_{dirn}")
    elif m5_score >= 0.3:
        reasons.append("M5_MOMENTUM_NEUTRAL")
    else:
        reasons.append(f"M5_MOMENTUM_AGAINST_{dirn}")

    # ── 10. No strong rejection ───────────────────────────────────────────────
    rejection_score = _rejection_score(ctx, dirn)
    score += rejection_score * _W_REJECTION
    if rejection_score >= 1.0:
        reasons.append("NO_STRONG_REJECTION")
    else:
        reasons.append("STRONG_REJECTION_DETECTED")

    # ── 11. SMC / MTFA confirmation ───────────────────────────────────────────
    smc_score = _f(cand.get("smc_score"))
    mtfa_score = _f(cand.get("mtfa_score"))
    smc_status = _label(cand.get("smc_calibrated_status") or cand.get("smc_status"))
    mtfa_status = _label(cand.get("mtfa_calibrated_status") or cand.get("mtfa_status"))
    smc_mtfa_bonus, smc_hard_block = _smc_mtfa_score(smc_score, mtfa_score, smc_status, mtfa_status)
    score += smc_mtfa_bonus * _W_SMC_MTFA
    if smc_hard_block:
        reasons.append("SMC_MTFA_HARD_BLOCK")
    elif smc_mtfa_bonus >= 1.0:
        reasons.append("SMC_MTFA_CONFIRMED")
    elif smc_mtfa_bonus >= 0.5:
        reasons.append("SMC_MTFA_PARTIAL")
    else:
        reasons.append("SMC_MTFA_WEAK")

    # ── 12. Volatility acceptable ─────────────────────────────────────────────
    vol_ok = _label(
        s_ctx.get("volatility_status") or ctx.get("volatility_status") or cand.get("volatility_status")
    )
    vol_score = _volatility_score(vol_ok)
    score += vol_score * _W_VOLATILITY
    if vol_score >= 1.0:
        reasons.append("VOLATILITY_ACCEPTABLE")
    elif vol_score >= 0.4:
        reasons.append("VOLATILITY_ELEVATED")
    else:
        reasons.append("VOLATILITY_TOO_HIGH")

    # ── Bonus: RR valid ───────────────────────────────────────────────────────
    rr = _f(cand.get("rr"))
    if rr is not None and rr >= 1.5:
        reasons.append("RR_VALID")
    elif rr is not None:
        score = max(0.0, score - 3.0)
        reasons.append("RR_BELOW_1_5")

    # ── Cap score ─────────────────────────────────────────────────────────────
    final_score = round(min(100.0, max(0.0, score)), 1)
    grade = _grade(final_score)

    # ── SMC hard block penalty (keep score but override to D) ─────────────────
    if smc_hard_block:
        grade = "D"
        final_score = min(final_score, 50.0)

    # ── Narrative integration (narrator_input=None → fully backward-compatible) ─
    narrative = None
    if narrator_input is not None:
        narrative = BtcMarketNarrator().narrate(narrator_input)

        # Blocking verdict takes absolute priority over score
        if narrative.verdict_blocks_entry:
            return {
                "decision": "BLOCK",
                "setup_quality_score": final_score,
                "grade": grade,
                "reasons": reasons + [f"NARRATIVE_BLOCK:{narrative.verdict}"],
                "entry_mode": "DEFENSIVE",
                "confidence_multiplier": 1.0,
                "exit_profile": "FAST_POSITIVE",
                "narrative_verdict": narrative.verdict,
                "narrative_coherence": narrative.coherence,
                "narrative_confidence": narrative.confidence,
                "strong_confirmations": narrative.strong_confirmations,
                "silent_risks": narrative.silent_risks,
                "strategy": strat,
                "direction": dirn,
                "schema_version": "btc_intel.v2",
            }

        # Grade upgrade: strong B → A when narrative is very confident
        if (
            grade == "B"
            and final_score >= _STRONG_B_THRESHOLD
            and narrative.verdict_upgrades_b_to_a
            and narrative.coherence >= 0.85
            and narrative.confidence >= 0.80
        ):
            grade = "A"
            reasons.append(f"NARRATIVE_UPGRADE_B_TO_A:coherence={narrative.coherence:.2f}")

        # Grade downgrade: A/A+ → lower when market is dangerous/undecided + low coherence
        elif (
            grade in ("A+", "A")
            and narrative.verdict in ("MARKET_IS_DANGEROUS", "MARKET_IS_UNDECIDED")
            and narrative.coherence < 0.55
        ):
            grade = "B" if grade == "A" else "A"
            reasons.append(f"NARRATIVE_DOWNGRADE:{narrative.verdict}")

        # Silent risks added to reasons for traceability
        reasons.extend([f"RISK:{r}" for r in narrative.silent_risks])

    # ── Determine entry mode & decision ──────────────────────────────────────
    mode = str(perf.get("adaptive_mode", "NORMAL"))
    is_paused = bool(perf.get("is_paused"))
    win_streak = int(perf.get("win_streak", 0) or 0)
    requires_aplus = bool(perf.get("requires_aplus"))

    if is_paused:
        entry_mode = "PAUSED"
        decision = "BLOCK"
        reasons.append("ENTRY_PAUSED_DEFENSIVE")
    elif mode == "DEFENSIVE":
        entry_mode = "DEFENSIVE"
        # Only A+ passes in defensive mode
        if grade == "A+":
            decision = "PASS"
        else:
            decision = "BLOCK"
            reasons.append(f"DEFENSIVE_MODE_REQUIRES_APLUS_GOT_{grade}")
    elif requires_aplus:
        entry_mode = "DEFENSIVE"
        if grade == "A+":
            decision = "PASS"
        else:
            decision = "BLOCK"
            reasons.append(f"POST_PAUSE_REQUIRES_APLUS_GOT_{grade}")
    elif mode == "CONFIDENT" or win_streak >= _WIN_STREAK_CONFIDENT:
        entry_mode = "AGGRESSIVE"
        # B (strong) also passes in confident mode
        if grade in ("A+", "A"):
            decision = "PASS"
        elif grade == "B":
            decision = "PASS"
            reasons.append("B_ALLOWED_WIN_STREAK")
        else:
            decision = "BLOCK"
            reasons.append(f"GRADE_{grade}_BELOW_THRESHOLD_EVEN_IN_CONFIDENT_MODE")
    else:
        entry_mode = "NORMAL"
        if grade in ("A+", "A"):
            decision = "PASS"
        elif grade == "B":
            decision = "WAIT"
            reasons.append("B_GRADE_WAIT_NEED_WIN_STREAK")
        else:
            decision = "BLOCK"
            reasons.append(f"GRADE_{grade}_BELOW_THRESHOLD")

    # ── Confidence multiplier ─────────────────────────────────────────────────
    multiplier = {
        "A+": 1.2,
        "A":  1.0,
        "B":  0.85,
        "C":  0.5,
        "D":  0.3,
    }.get(grade, 1.0)

    # ── Exit profile ──────────────────────────────────────────────────────────
    of_strong_support = of_score >= 1.0 and (cvd_score >= 0.6 or m5_score >= 1.0)
    if grade == "A+" and of_strong_support and win_streak >= 2:
        exit_profile = "HOLD_IF_STRONG"
    elif grade == "A":
        exit_profile = "NORMAL_SCALP"
    else:
        exit_profile = "FAST_POSITIVE"

    # ── Narrative exit profile override (conservative direction only) ────────
    if narrative is not None:
        if narrative.suggested_exit_profile == "FAST_POSITIVE":
            exit_profile = "FAST_POSITIVE"
        elif narrative.suggested_exit_profile == "HOLD_IF_STRONG" and grade == "A+":
            exit_profile = "HOLD_IF_STRONG"

    log.info(
        "[BTC_SETUP_INTELLIGENCE] strategy=%s direction=%s score=%.1f "
        "grade=%s decision=%s entry_mode=%s exit_profile=%s reasons=%s",
        strat, dirn, final_score, grade, decision, entry_mode, exit_profile,
        reasons[:6],
    )

    return {
        "decision": decision,
        "setup_quality_score": final_score,
        "grade": grade,
        "reasons": reasons,
        "entry_mode": entry_mode,
        "confidence_multiplier": multiplier,
        "exit_profile": exit_profile,
        "strategy": strat,
        "direction": dirn,
        "narrative_verdict": narrative.verdict if narrative else "N/A",
        "narrative_coherence": narrative.coherence if narrative else 0.0,
        "narrative_confidence": narrative.confidence if narrative else 0.0,
        "strong_confirmations": narrative.strong_confirmations if narrative else [],
        "silent_risks": narrative.silent_risks if narrative else [],
    }


# ─── Arbiter: pick the single best BTC setup ─────────────────────────────────

def select_best_btc_setup(candidates: list[dict]) -> dict | None:
    """Select the single best HERMES BTC setup from multiple strategy candidates.

    Rules:
    - Never selects more than one BTC setup
    - Only selects A or A+ grade setups (unless recent_performance allows B)
    - Prefers highest setup_quality_score
    - Ties broken by strategy recent performance (best_strategy_now)
    - Returns None when no acceptable setup exists
    """
    if not candidates:
        log.info(
            "[BTC_SETUP_ARBITER] candidates=0 selected=NONE reason=NO_CANDIDATES"
        )
        return None

    # Filter to only PASS decisions
    eligible = [c for c in candidates if c.get("intelligence", {}).get("decision") == "PASS"]

    if not eligible:
        log.info(
            "[BTC_SETUP_ARBITER] candidates=%s selected=NONE reason=NO_HIGH_QUALITY_SETUP",
            len(candidates),
        )
        return None

    # Sort by score desc, then grade rank, then exit profile preference
    _grade_rank = {"A+": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    _exit_rank = {"HOLD_IF_STRONG": 3, "NORMAL_SCALP": 2, "FAST_POSITIVE": 1}

    def _sort_key(c: dict) -> tuple:
        intel = c.get("intelligence", {})
        score = float(intel.get("setup_quality_score", 0) or 0)
        grade_r = _grade_rank.get(intel.get("grade", "D"), 0)
        exit_r = _exit_rank.get(intel.get("exit_profile", "FAST_POSITIVE"), 1)
        return (score, grade_r, exit_r)

    ranked = sorted(eligible, key=_sort_key, reverse=True)
    best = ranked[0]

    intel = best.get("intelligence", {})
    log.info(
        "[BTC_SETUP_ARBITER] candidates=%s selected=%s score=%.1f grade=%s reason=HIGHEST_QUALITY",
        len(candidates),
        best.get("strategy", "UNKNOWN"),
        intel.get("setup_quality_score", 0),
        intel.get("grade", "?"),
    )
    return best


# ─── Scoring helpers ─────────────────────────────────────────────────────────

def _session_score(session: str) -> float:
    """Return 0-1 score for session quality."""
    if not session:
        return 0.5   # unknown — give benefit of doubt
    # Best sessions for BTC liquidity
    if any(k in session for k in ("LONDON_NY", "OVERLAP", "NY_OPEN", "LONDON_OPEN")):
        return 1.0
    if any(k in session for k in ("LONDON", "NEW_YORK", "NY", "US_OPEN")):
        return 0.9
    if any(k in session for k in ("ACTIVE", "OPEN", "TRADING", "ASIA")):
        return 0.6
    if any(k in session for k in ("CLOSED", "QUIET", "FLAT", "OFF")):
        return 0.0
    return 0.5


def _spread_fraction(spread: float | None, max_spread: float | None) -> float | None:
    if spread is None or max_spread is None or max_spread <= 0:
        return None
    return spread / max_spread


def _spread_score(frac: float | None) -> float:
    if frac is None:
        return 0.75   # no data — moderate benefit of doubt
    if frac <= _SPREAD_WARNING_RATIO:
        return 1.0
    if frac <= _SPREAD_HARD_RATIO:
        # Linear interpolation from 0.5 to 1.0 between WARNING and HARD ratio
        t = (_SPREAD_HARD_RATIO - frac) / (_SPREAD_HARD_RATIO - _SPREAD_WARNING_RATIO)
        return round(0.5 + 0.5 * t, 3)
    return 0.0


def _order_flow_score(sig: str, direction: str) -> float:
    if not sig:
        return 0.5
    if direction == "BUY":
        if sig in {"BUY", "BULLISH", "LONG"}:
            return 1.0
        if sig in {"WAIT", "NEUTRAL", "MIXED"}:
            return 0.5
        return 0.0
    else:
        if sig in {"SELL", "BEARISH", "SHORT"}:
            return 1.0
        if sig in {"WAIT", "NEUTRAL", "MIXED"}:
            return 0.5
        return 0.0


def _cvd_score(cvd: float | None, direction: str) -> float:
    if cvd is None:
        return 0.5
    if direction == "BUY":
        if cvd >= _CVD_STRONG:
            return 1.0
        if cvd >= _CVD_MILD:
            return 0.75
        if cvd >= 0.0:
            return 0.5
        if cvd >= -_CVD_MILD:
            return 0.25
        return 0.0
    else:  # SELL
        if cvd <= -_CVD_STRONG:
            return 1.0
        if cvd <= -_CVD_MILD:
            return 0.75
        if cvd <= 0.0:
            return 0.5
        if cvd <= _CVD_MILD:
            return 0.25
        return 0.0


def _delta_score(delta: float | None, direction: str) -> float:
    if delta is None:
        return 0.5
    if direction == "BUY":
        if delta >= _DELTA_STRONG:
            return 1.0
        if delta >= 0:
            return 0.75
        if delta >= -_DELTA_STRONG:
            return 0.4
        return 0.0
    else:
        if delta <= -_DELTA_STRONG:
            return 1.0
        if delta <= 0:
            return 0.75
        if delta <= _DELTA_STRONG:
            return 0.4
        return 0.0


def _level_relation_score(price: float | None, level: float | None, direction: str) -> float:
    """Score price position relative to VWAP or POC."""
    if price is None or level is None or level == 0:
        return 0.5   # no data
    diff_frac = (price - level) / level

    if direction == "BUY":
        if diff_frac > _LEVEL_RECLAIM_ZONE:
            return 1.0    # clearly above
        if diff_frac >= -_LEVEL_RECLAIM_ZONE:
            return 0.5    # at/reclaiming
        return 0.0        # below
    else:
        if diff_frac < -_LEVEL_RECLAIM_ZONE:
            return 1.0    # clearly below
        if diff_frac <= _LEVEL_RECLAIM_ZONE:
            return 0.5    # at/rejecting
        return 0.0        # above


def _momentum_score(signal: str, direction: str) -> float:
    if not signal:
        return 0.3
    if direction == "BUY":
        if signal in {"BULLISH", "BUY", "UP", "STRONG_BUY"}:
            return 1.0
        if signal in {"NEUTRAL", "SIDEWAYS", "MIXED"}:
            return 0.3
        return 0.0
    else:
        if signal in {"BEARISH", "SELL", "DOWN", "STRONG_SELL"}:
            return 1.0
        if signal in {"NEUTRAL", "SIDEWAYS", "MIXED"}:
            return 0.3
        return 0.0


def _rejection_score(ctx: dict, direction: str) -> float:
    """Return 1.0 if no strong rejection detected, 0.0 if rejection seen."""
    wick_label = _label(ctx.get("wick_rejection") or ctx.get("rejection_signal"))
    if not wick_label:
        return 1.0
    if direction == "BUY" and "BEARISH" in wick_label:
        return 0.0
    if direction == "SELL" and "BULLISH" in wick_label:
        return 0.0
    return 1.0


def _smc_mtfa_score(
    smc: float | None,
    mtfa: float | None,
    smc_status: str,
    mtfa_status: str,
) -> tuple[float, bool]:
    """Return (normalized_score 0-1, hard_block bool)."""
    hard_block = (
        "STRONG_FAIL" in smc_status or "STRONG_FAIL" in mtfa_status
    )
    if hard_block:
        return (0.0, True)

    smc_norm = 0.5
    if smc is not None:
        smc_norm = min(1.0, max(0.0, smc / 100.0))

    mtfa_norm = 0.5
    if mtfa is not None:
        mtfa_norm = min(1.0, max(0.0, mtfa / 100.0))

    combined = (smc_norm + mtfa_norm) / 2.0
    return (combined, False)


def _volatility_score(status: str) -> float:
    if not status:
        return 0.75
    if status in {"LOW", "NORMAL", "ACCEPTABLE", "OK"}:
        return 1.0
    if status in {"ELEVATED", "MODERATE", "MEDIUM"}:
        return 0.4
    if status in {"HIGH", "EXTREME", "VERY_HIGH", "TOO_HIGH"}:
        return 0.0
    return 0.75


# ─── Grade + result helpers ───────────────────────────────────────────────────

def _grade(score: float) -> str:
    if score >= _GRADE_APLUS_MIN:
        return "A+"
    if score >= _GRADE_A_MIN:
        return "A"
    if score >= _GRADE_B_MIN:
        return "B"
    if score >= _GRADE_C_MIN:
        return "C"
    return "D"


def _result(
    score: float,
    grade: str,
    decision: str,
    entry_mode: str,
    multiplier: float,
    exit_profile: str,
    reasons: list[str],
) -> dict:
    return {
        "decision": decision,
        "setup_quality_score": round(score, 1),
        "grade": grade,
        "reasons": reasons,
        "entry_mode": entry_mode,
        "confidence_multiplier": multiplier,
        "exit_profile": exit_profile,
        "narrative_verdict": "N/A",
        "narrative_coherence": 0.0,
        "narrative_confidence": 0.0,
        "strong_confirmations": [],
        "silent_risks": [],
    }


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
