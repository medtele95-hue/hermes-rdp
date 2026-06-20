"""HERMES Setup Quality Tier — §4.0 of the Intelligent Entry Upgrade v1.3/v1.4.

Evaluates mandatory pillars and bonus pillars B1–B9 using the merged candidate
payload.  Mandatory pillar set is strategy-class-aware (v1.4):

  SMC_NATIVE    [M1, M2, M3, M4]  — full structural alignment required
  ORDER_FLOW_NATIVE  [OM1, OM2, OM3, OM4] — OF-specific requirements, no OB/OTE
  DEFAULT        [M1, M4]          — bias + OF agreement, no structure gate

Tier rules (all classes):
  A+     = ALL mandatory pass + ≥5 bonuses + OF-anchor (B1 or B9 for OF class;
           B1 for others)
  A      = ALL mandatory pass + ≥4 bonuses
  B+     = ALL mandatory pass + ≥3 bonuses  (half-size; require explicit opt-in)
  REJECT = any mandatory fails  OR  < 3 bonuses

SMC_NATIVE mandatory pillars:
  M1  Both H4 AND H1 trend agree with trade direction
  M2  Confirmed BOS/CHoCH in direction + aligned OB (fresh proxy)
  M3  Entry box non-empty — implied by M2 plus SMC OTE tag or confluence ≥ 45
  M4  ALL THREE order-flow signals agree: VWAP side + CVD slope + delta

ORDER_FLOW_NATIVE mandatory pillars (OM = Order-flow Mandatory):
  OM1 H4/H1 NOT clearly against direction (RANGE/UNKNOWN = pass)
  OM2 Price within OF_KEY_LEVEL_TOL_ATR × ATR of VWAP, VAH/VAL, HVN, or
      swept liquidity level
  OM3 ALL THREE OF signals agree (= M4)
  OM4 Real break (break_vol_ratio ≥ 1.5) OR validated absorption at level

DEFAULT mandatory pillars:
  M1  Both H4 AND H1 trend agree with trade direction
  M4  ALL THREE order-flow signals agree

Bonus pillars — count toward tier promotion (all classes):
  B1  Liquidity sweep before entry (swept EQH/EQL on H1)
  B2  FVG in direction aligned inside entry box
  B3  CVD divergence in trade direction
  B4  Impulse strength (impulse_score_m5 ≥ 60)
  B5  Deep discount (BUY) or premium (SELL) zone
  B6  High-liquidity session (London / New York / Overlap)
  B7  Clean path — risk_reward ≥ 2.0
  B8  Volume confirmation — HVN overlap + real break (from volume_engine)
  B9  Absorption confirmation — valid absorption in direction (from cvd_absorption)

Integration: called from setup_hunter._failed_gates() when
`hermes_setup_tier_enabled` is True in Settings.

BTC_SCALPING_AGENT is excluded — it has its own gate system.
"""
from __future__ import annotations

import math

from app.logger import log
from app.quant.volume_engine import price_near_hvn

# Strategies routed through order-flow pillars (OM1–OM4); no OB/OTE requirements
_ORDER_FLOW_NATIVE = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_ORDER_FLOW_CVD_VWAP",
})

# Strategies that require full SMC structural alignment (M1–M4)
_SMC_NATIVE = frozenset({
    "SIMO_ATM_BREAKOUT",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "AMD_FVG_IFVG_REVERSAL",
    "CRT_TBS_REVERSAL",
})

_SKIP_STRATEGIES = frozenset({"BTC_SCALPING_AGENT"})

# OM2 tolerance: price must be within this many ATRs of the key level
_OF_KEY_LEVEL_TOL_ATR: float = 0.25

# Tier thresholds
_BONUS_A_PLUS = 5
_BONUS_A = 4
_BONUS_B_PLUS = 3

# M4 thresholds
_CVD_MIN: float = 50.0

# Sessions that satisfy B6
_QUALITY_SESSIONS = frozenset({"LONDON", "NEW_YORK", "OVERLAP", "LONDON_NEW_YORK_OVERLAP"})


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_setup_quality(
    strategy: str,
    direction: str,
    payload: dict,
) -> dict:
    """Evaluate M1–M4 + B1–B9 and return a tier assessment dict.

    Returns:
        tier          — 'A+' | 'A' | 'B+' | 'REJECT'
        mandatory_ok  — bool: all M pillars passed
        failed_mandatory — list[str]: which M pillars failed
        bonus_count   — int: number of B pillars that passed
        bonus_passed  — dict[str, bool]: individual B pillar results
        reason        — human-readable summary string
    """
    if strategy in _SKIP_STRATEGIES or direction not in {"BUY", "SELL"}:
        return _tier_result("A+", [], {}, 9, "SKIP_STRATEGY_OR_INVALID_DIRECTION")

    # ── Mandatory pillars (strategy-class aware) ─────────────────────────────
    failed_m: list[str] = []

    if strategy in _ORDER_FLOW_NATIVE:
        # OM1 — H4/H1 not clearly against direction
        if not _om1_not_opposing(direction, payload):
            failed_m.append("OM1_HTF_AGAINST_DIRECTION")
        # OM2 — price near a key OF level
        if not _om2_near_key_level(direction, payload):
            failed_m.append("OM2_NOT_NEAR_KEY_OF_LEVEL")
        # OM3 — all three OF signals agree (= M4 logic)
        om3_pass, om3_skip = _m4_of_all_agree(direction, payload)
        if not om3_pass and not om3_skip:
            failed_m.append("OM3_OF_SIGNALS_INCOMPLETE")
        # OM4 — real break OR validated absorption
        if not _om4_real_participation(direction, payload):
            failed_m.append("OM4_NO_REAL_PARTICIPATION")

    elif strategy in _SMC_NATIVE:
        # Full SMC structural alignment
        if not _m1_bias_aligned(direction, payload):
            failed_m.append("M1_HTF_BIAS_NOT_ALIGNED")
        if not _m2_structure_and_ob(direction, payload):
            failed_m.append("M2_STRUCTURE_OB_UNCONFIRMED")
        if not _m3_entry_box(direction, payload):
            failed_m.append("M3_ENTRY_BOX_EMPTY")
        m4_pass, m4_skip = _m4_of_all_agree(direction, payload)
        if not m4_pass and not m4_skip:
            failed_m.append("M4_OF_SIGNALS_INCOMPLETE")

    else:
        # DEFAULT: bias alignment + OF agreement
        if not _m1_bias_aligned(direction, payload):
            failed_m.append("M1_HTF_BIAS_NOT_ALIGNED")
        m4_pass, m4_skip = _m4_of_all_agree(direction, payload)
        if not m4_pass and not m4_skip:
            failed_m.append("M4_OF_SIGNALS_INCOMPLETE")

    mandatory_ok = len(failed_m) == 0

    # ── Bonus pillars ────────────────────────────────────────────────────────
    bonus = {
        "B1": _b1_liquidity_sweep(direction, payload),
        "B2": _b2_fvg_aligned(direction, payload),
        "B3": _b3_cvd_divergence(direction, payload),
        "B4": _b4_impulse_strength(payload),
        "B5": _b5_deep_zone(direction, payload),
        "B6": _b6_quality_session(payload),
        "B7": _b7_clean_path(payload),
        "B8": _b8_volume_confirmed(direction, payload),
        "B9": _b9_absorption_confirmed(direction, payload),
    }
    bonus_count = sum(1 for v in bonus.values() if v)

    # ── Tier assignment ──────────────────────────────────────────────────────
    # A+ anchor: B1 OR B9 for OF-native (sweep or absorption); B1 only otherwise
    _of_native_anchor = bonus.get("B1") or bonus.get("B9")
    _a_plus_anchor = _of_native_anchor if strategy in _ORDER_FLOW_NATIVE else bonus.get("B1")

    if not mandatory_ok:
        tier = "REJECT"
        reason = f"mandatory_failed={','.join(failed_m)}"
    elif bonus_count < _BONUS_B_PLUS:
        tier = "REJECT"
        reason = f"bonus_count={bonus_count}<{_BONUS_B_PLUS}_required"
    elif bonus_count >= _BONUS_A_PLUS and _a_plus_anchor:
        tier = "A+"
        reason = f"bonus_count={bonus_count}_anchor_present"
    elif bonus_count >= _BONUS_A:
        tier = "A"
        reason = f"bonus_count={bonus_count}"
    else:
        tier = "B+"
        reason = f"bonus_count={bonus_count}_half_size"

    log.info(
        "[SETUP_TIER] strategy=%s direction=%s tier=%s bonus=%s/%s failed_m=%s",
        strategy, direction, tier, bonus_count, len(bonus), failed_m or "none",
    )

    return _tier_result(tier, failed_m, bonus, bonus_count, reason)


def tier_to_gate_failures(tier_result: dict) -> list[str]:
    """Convert tier result into gate failure strings for setup_hunter._failed_gates().

    Returns [] when the tier allows the trade, otherwise a list with one reason.
    B+ is allowed through as a near-miss (gated separately by size policy).
    """
    tier = tier_result.get("tier", "REJECT")
    if tier in {"A+", "A", "B+"}:
        return []
    return ["SETUP_TIER_REJECT"]


# ── ORDER_FLOW_NATIVE mandatory pillar checks (OM1–OM4) ──────────────────────

def _om1_not_opposing(direction: str, payload: dict) -> bool:
    """H4 and H1 must NOT be clearly against the direction.

    Looser than M1: RANGE/UNKNOWN is a pass. Only fails when both H4 and H1
    are explicitly named in the opposing direction.
    """
    h4 = str(payload.get("smc_h4_direction") or "").upper()
    h1 = str(payload.get("smc_h1_trend") or "").upper()
    if direction == "BUY":
        return not (h4 == "BEARISH" or h1 == "BEARISH")
    return not (h4 == "BULLISH" or h1 == "BULLISH")


def _om2_near_key_level(direction: str, payload: dict) -> bool:
    """Price is within _OF_KEY_LEVEL_TOL_ATR × ATR of a key OF level.

    Key levels checked: VWAP, VAH/VAL from OF snapshot, any HVN centroid
    from volume_engine, or swept liquidity (B1 signal is present).

    Fail-safe: returns True when entry price or ATR is unavailable.
    """
    entry = _f(payload.get("entry") or payload.get("price"))
    if entry is None:
        return True  # no price → fail-safe pass

    ve = payload.get("volume_engine")
    atr = _f(ve.get("atr") if isinstance(ve, dict) else None)
    if atr is None or atr <= 0:
        return True  # no ATR → fail-safe pass

    tol = _OF_KEY_LEVEL_TOL_ATR * atr

    of_data = _get_of_data(direction, payload)
    if isinstance(of_data, dict):
        vwap = _f(of_data.get("vwap"))
        if vwap is not None and abs(entry - vwap) <= tol:
            return True
        for key in ("vah", "value_area_high", "val", "value_area_low"):
            level = _f(of_data.get(key))
            if level is not None and abs(entry - level) <= tol:
                return True

    if isinstance(ve, dict):
        for hvn in (ve.get("hvn_levels") or []):
            hvn_f = _f(hvn)
            if hvn_f is not None and abs(entry - hvn_f) <= tol:
                return True

    # B1: swept liquidity level counts as a key level proximity signal
    if _b1_liquidity_sweep(direction, payload):
        return True

    return False


def _om4_real_participation(direction: str, payload: dict) -> bool:
    """Real break (break_vol_ratio ≥ 1.5) OR validated absorption at the level."""
    ve = payload.get("volume_engine")
    if isinstance(ve, dict) and ve.get("real_break"):
        return True
    return _b9_absorption_confirmed(direction, payload)


# ── SMC_NATIVE / DEFAULT mandatory pillar checks ──────────────────────────────

def _m1_bias_aligned(direction: str, payload: dict) -> bool:
    """Both H4 and H1 trend must agree with trade direction."""
    h4 = str(payload.get("smc_h4_direction") or "").upper()
    h1 = str(payload.get("smc_h1_trend") or "").upper()
    if h4 in ("", "UNKNOWN", "RANGE") or h1 in ("", "UNKNOWN", "RANGE"):
        # Missing data → pass (fail-safe)
        return True
    if direction == "BUY":
        return h4 == "BULLISH" and h1 == "BULLISH"
    return h4 == "BEARISH" and h1 == "BEARISH"


def _m2_structure_and_ob(direction: str, payload: dict) -> bool:
    """Confirmed BOS/CHoCH in direction + aligned OB active."""
    h1_break = str(payload.get("smc_h1_break_structure") or "").upper()
    h1_ob = str(payload.get("smc_h1_order_block") or "").upper()

    if not h1_break or h1_break == "NONE":
        return True  # no structure data → fail-safe pass

    if direction == "BUY":
        break_ok = h1_break in {"BOS_UP", "CHOCH_UP"}
    else:
        break_ok = h1_break in {"BOS_DOWN", "CHOCH_DOWN"}

    if not break_ok:
        return False

    if not h1_ob or h1_ob == "NONE":
        return True  # break confirmed, OB absent → pass (OB not guaranteed)

    if direction == "BUY":
        return h1_ob == "BULLISH_OB"
    return h1_ob == "BEARISH_OB"


def _m3_entry_box(direction: str, payload: dict) -> bool:
    """Entry box non-empty: OB ∩ OTE ∩ PD half (simplified proxy).

    Passes when:
    - any SMC OTE composite tag is True (direct match), OR
    - M2 passes AND smc_confluence_score ≥ 45
    """
    if not _m2_structure_and_ob(direction, payload):
        return False

    ote_tags = ("amd_bpr_ote", "ifvg_ote_sniper", "breaker_fvg_ote", "turtle_soup_ote")
    if any(payload.get(tag) is True for tag in ote_tags):
        return True

    score = _f(payload.get("smc_confluence_score")) or 0.0
    return score >= 45.0


def _m4_of_all_agree(direction: str, payload: dict) -> tuple[bool, bool]:
    """ALL THREE core OF signals must agree: VWAP side, CVD slope, delta.

    Returns (pass_bool, skip_bool).
    skip=True when no OF data is present → fail-safe (does not block M4).
    """
    of_data = _get_of_data(direction, payload)
    if not isinstance(of_data, dict) or not of_data:
        return False, True  # no data → skip (fail-safe pass)

    buy = direction == "BUY"
    checks: list[bool] = []

    price = _f(payload.get("entry") or payload.get("price"))
    vwap = _f(of_data.get("vwap"))
    if price is not None and vwap is not None:
        checks.append((buy and price > vwap) or (not buy and price < vwap))

    cvd_slope = _f(of_data.get("cvd_slope"))
    if cvd_slope is not None:
        checks.append((buy and cvd_slope > _CVD_MIN) or (not buy and cvd_slope < -_CVD_MIN))

    delta = _f(
        of_data.get("delta")
        or of_data.get("delta_proxy")
        or of_data.get("latest_delta")
    )
    if delta is not None:
        checks.append((buy and delta > 0) or (not buy and delta < 0))

    if not checks:
        return False, True  # no fields available → skip

    return all(checks), False


# ── Bonus pillar checks ───────────────────────────────────────────────────────

def _b1_liquidity_sweep(direction: str, payload: dict) -> bool:
    """H1 liquidity pool was swept before the entry signal."""
    liq = str(payload.get("smc_h1_liquidity") or "").upper()
    if direction == "BUY":
        # Swept sell-side / equal lows → reversal up expected
        return liq in {"SELL_SIDE", "EQUAL_LOWS"}
    return liq in {"BUY_SIDE", "EQUAL_HIGHS"}


def _b2_fvg_aligned(direction: str, payload: dict) -> bool:
    """H1 FVG present and aligned with trade direction."""
    fvg = str(payload.get("smc_h1_fvg") or "").upper()
    if direction == "BUY":
        return fvg == "BULLISH_FVG"
    return fvg == "BEARISH_FVG"


def _b3_cvd_divergence(direction: str, payload: dict) -> bool:
    """CVD divergence signal in trade direction (from any OF snapshot)."""
    of_data = _get_of_data(direction, payload)
    if not isinstance(of_data, dict):
        return False
    div = str(of_data.get("divergence") or "").lower()
    if direction == "BUY":
        return div in {"bull", "bullish"}
    return div in {"bear", "bearish"}


def _b4_impulse_strength(payload: dict) -> bool:
    """M5 impulse score ≥ 60 (pre-computed by hermes_5min_agent)."""
    score = _f(payload.get("impulse_score_m5"))
    return score is not None and score >= 60.0


def _b5_deep_zone(direction: str, payload: dict) -> bool:
    """Price is in deep discount (BUY) or deep premium (SELL) zone.

    Primary: Appendix A10 exact formula using pre-computed swing range.
      deep_discount = price < rngLo + (rngHi - rngLo) * 0.382
      deep_premium  = price > rngHi - (rngHi - rngLo) * 0.382

    Fallback (when swing range not pre-computed): CRT price zone, FIB OTE,
    or top-down reader price location fields from the payload.
    """
    entry = _f(payload.get("entry") or payload.get("price"))
    sh = _f(payload.get("pd_swing_high"))
    sl = _f(payload.get("pd_swing_low"))

    if entry is not None and sh is not None and sl is not None and sh > sl:
        rng = sh - sl
        threshold = rng * 0.382
        if direction == "BUY" and entry < sl + threshold:
            return True
        if direction == "SELL" and entry > sh - threshold:
            return True
        # If we have the swing range, use it as the authoritative check only
        return False

    # Fallback: payload-level signals when no swing range embedded
    zone = str(payload.get("crt_price_zone") or "").upper()
    if direction == "BUY" and zone == "DISCOUNT":
        return True
    if direction == "SELL" and zone == "PREMIUM":
        return True
    fib_zone = str(payload.get("fib_ote_zone") or "").upper()
    fib_bias = str(payload.get("fib_ote_bias") or "").upper()
    if fib_zone == "OTE":
        if direction == "BUY" and fib_bias == "BULLISH":
            return True
        if direction == "SELL" and fib_bias == "BEARISH":
            return True
    td = payload.get("top_down_reader") or {}
    if isinstance(td, dict):
        price_loc = str(td.get("price_location") or "").upper()
        if direction == "BUY" and "DISCOUNT" in price_loc:
            return True
        if direction == "SELL" and "PREMIUM" in price_loc:
            return True
    return False


def _b6_quality_session(payload: dict) -> bool:
    """Trade is in a high-liquidity session window."""
    session = str(payload.get("session_name") or payload.get("session") or "").upper()
    return any(qs in session for qs in _QUALITY_SESSIONS) or session in _QUALITY_SESSIONS


def _b7_clean_path(payload: dict) -> bool:
    """Risk:reward ≥ 2.0."""
    rr = _f(payload.get("risk_reward") or payload.get("reward_risk"))
    return rr is not None and rr >= 2.0


def _b8_volume_confirmed(direction: str, payload: dict) -> bool:
    """Volume confirmation: entry overlaps HVN AND last break was high volume."""
    ve = payload.get("volume_engine")
    if not isinstance(ve, dict) or not ve:
        return False
    if not ve.get("real_break"):
        return False
    hvn_levels = ve.get("hvn_levels") or []
    if not hvn_levels:
        return False
    entry = _f(payload.get("entry") or payload.get("price"))
    atr = _f(ve.get("atr"))
    if entry is None or not atr:
        # HVN list exists + real break confirmed but can't check overlap → count as pass
        return True
    return price_near_hvn(entry, hvn_levels, atr)


def _b9_absorption_confirmed(direction: str, payload: dict) -> bool:
    """Validated absorption in trade direction (buyers absorbing sells for BUY)."""
    ctx = payload.get("cvd_absorption")
    if not isinstance(ctx, dict) or not ctx.get("valid"):
        return False
    if direction == "BUY":
        return bool(ctx.get("bull_absorption"))
    return bool(ctx.get("bear_absorption"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_of_data(direction: str, payload: dict) -> dict | None:
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


def _tier_result(
    tier: str,
    failed_mandatory: list[str],
    bonus_passed: dict,
    bonus_count: int,
    reason: str,
) -> dict:
    return {
        "tier": tier,
        "mandatory_ok": len(failed_mandatory) == 0,
        "failed_mandatory": failed_mandatory,
        "bonus_count": bonus_count,
        "bonus_passed": bonus_passed,
        "reason": reason,
    }
