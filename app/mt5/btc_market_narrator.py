"""BTC Market Narrator — constructs a market narrative from existing pipeline data.

No MT5 calls. No direct trading. Pure synthesis of data already computed by
smc_confluence_tagger, mtfa_filter, confluence_engine, btc_exit_danger,
and geometry_engine.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.logger import log


# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BtcNarratorInput:
    """All data comes from the existing pipeline. No new MT5 calls needed."""

    # --- From smc_confluence_tagger ---
    smc_direction: str           # "BULLISH" | "BEARISH" | "NEUTRAL"
    smc_score: float             # 0.0 .. 1.0
    smc_order_block_bull: bool
    smc_order_block_bear: bool
    smc_fvg_bull: bool
    smc_fvg_bear: bool
    smc_liquidity_above: bool    # buy-side liquidity present
    smc_liquidity_below: bool    # sell-side liquidity present
    smc_inducement: bool         # stop hunt probable

    # --- From mtfa_filter ---
    mtfa_bias: str               # "BULL" | "BEAR" | "NEUTRAL"
    mtfa_score: float            # 0-100
    mtfa_h1_direction: str       # "BULLISH" | "BEARISH" | "NEUTRAL"
    mtfa_m15_liquidity_ok: bool
    mtfa_cisd_m5: bool           # Change In State of Delivery M5

    # --- From confluence_engine ---
    confluence_score: float      # 0-100
    confluence_grade: str        # "A+" | "A" | "B" | "C" | "D"
    order_flow_bonus: float      # bonus/malus as computed by confluence_engine

    # --- From btc_exit_danger ---
    danger_signal_count: int     # 0..N independent signals

    # --- From geometry_engine ---
    price_in_premium: bool
    price_in_discount: bool
    atr_m5: float
    impulse_score: float         # 0.0..1.0
    range_compression: float     # 0.0..1.0

    # --- From data_reader (candles already read by MT5DataReader) ---
    m1_last_body_pct: float
    m1_upper_wick_pct: float
    m1_lower_wick_pct: float
    m5_ema_stack: str            # "BULL" | "BEAR" | "MIXED"
    m5_last_close_vs_open: float

    # --- From order_flow_execution_agent (optional — None if unavailable) ---
    cvd_slope_m5: float | None
    delta_last: float | None
    dominant_side: str | None    # "BUYERS" | "SELLERS" | "CONTESTED" | None

    # --- Session context ---
    session_name: str            # "LONDON" | "NEW_YORK" | "ASIA" | "OVERLAP" | "OFF"
    session_quality: float       # 0.0..1.0

    # --- Direction ---
    direction: str               # "BUY" | "SELL"
    server_time_utc: int


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BtcNarrative:
    verdict: str
    # Possible values:
    #   "MARKET_WANTS_UP"       — strong convergence upward
    #   "MARKET_WANTS_DOWN"     — strong convergence downward
    #   "MARKET_IS_UNDECIDED"   — mixed signals, no conviction
    #   "MARKET_IS_DANGEROUS"   — danger detected, high risk
    #   "MARKET_IS_TRAPPING"    — inducement + liquidity sweep = probable trap
    #   "MARKET_IS_EXHAUSTED"   — strong recent impulse, likely end
    #   "MARKET_IS_COMPRESSING" — tight range, breakout imminent (direction unknown)

    coherence: float                  # 0.0..1.0 — all signals tell the same story?
    confidence: float                 # 0.0..1.0 — conviction in the verdict

    strong_confirmations: list        # what strongly confirms (sorted by weight)
    weak_points: list                 # what is missing or concerning
    silent_risks: list                # non-obvious risks

    verdict_blocks_entry: bool        # True forces BLOCK regardless of score
    verdict_upgrades_b_to_a: bool     # True when very strong narrative upgrades B → A
    suggested_exit_profile: str       # "FAST_POSITIVE" | "NORMAL_SCALP" | "HOLD_IF_STRONG"

    schema_version: str = "btc_narrator.v1"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLOCKING_VERDICTS: frozenset[str] = frozenset({
    "MARKET_IS_TRAPPING",
    "MARKET_IS_EXHAUSTED",
    "MARKET_IS_DANGEROUS",
})

_VALID_VERDICTS: frozenset[str] = frozenset({
    "MARKET_WANTS_UP",
    "MARKET_WANTS_DOWN",
    "MARKET_IS_UNDECIDED",
    "MARKET_IS_DANGEROUS",
    "MARKET_IS_TRAPPING",
    "MARKET_IS_EXHAUSTED",
    "MARKET_IS_COMPRESSING",
})

_VALID_EXIT_PROFILES: frozenset[str] = frozenset({
    "FAST_POSITIVE",
    "NORMAL_SCALP",
    "HOLD_IF_STRONG",
})


# ---------------------------------------------------------------------------
# Private alignment helpers
# ---------------------------------------------------------------------------

def _smc_aligned(smc_direction: str, direction: str) -> bool:
    if direction == "BUY":
        return smc_direction == "BULLISH"
    if direction == "SELL":
        return smc_direction == "BEARISH"
    return False


def _smc_contradicts(smc_direction: str, direction: str) -> bool:
    if direction == "BUY":
        return smc_direction == "BEARISH"
    if direction == "SELL":
        return smc_direction == "BULLISH"
    return False


def _mtfa_bias_aligned(mtfa_bias: str, direction: str) -> bool:
    if direction == "BUY":
        return mtfa_bias == "BULL"
    if direction == "SELL":
        return mtfa_bias == "BEAR"
    return False


def _mtfa_h1_contradicts(mtfa_h1_direction: str, direction: str) -> bool:
    if direction == "BUY":
        return mtfa_h1_direction == "BEARISH"
    if direction == "SELL":
        return mtfa_h1_direction == "BULLISH"
    return False


def _entry_timing_late(inp: BtcNarratorInput) -> bool:
    return inp.impulse_score >= 0.75 and inp.range_compression < 0.2


def _vwap_aligned_via_ema(inp: BtcNarratorInput) -> bool:
    """Use M5 EMA stack as VWAP proxy (bull stack ≈ price above VWAP)."""
    if inp.direction == "BUY":
        return inp.m5_ema_stack == "BULL"
    if inp.direction == "SELL":
        return inp.m5_ema_stack == "BEAR"
    return False


def _fvg_aligned(inp: BtcNarratorInput) -> bool:
    if inp.direction == "BUY":
        return inp.smc_fvg_bull
    if inp.direction == "SELL":
        return inp.smc_fvg_bear
    return False


def _ob_aligned(inp: BtcNarratorInput) -> bool:
    if inp.direction == "BUY":
        return inp.smc_order_block_bull
    if inp.direction == "SELL":
        return inp.smc_order_block_bear
    return False


# ---------------------------------------------------------------------------
# Coherence score builder
# ---------------------------------------------------------------------------

def _build_coherence_score(
    inp: BtcNarratorInput,
) -> tuple[float, list[str], list[str]]:
    confirmations: list[str] = []
    weak_points: list[str] = []
    total = 0.0
    max_total = 0.0

    def _check(condition: bool, weight: float, conf: str, weak: str) -> None:
        nonlocal total, max_total
        max_total += weight
        if condition:
            total += weight
            confirmations.append(conf)
        else:
            weak_points.append(weak)

    _check(_smc_aligned(inp.smc_direction, inp.direction),         0.20, "SMC_ALIGNED",        "SMC_AGAINST")
    _check(_mtfa_bias_aligned(inp.mtfa_bias, inp.direction),       0.18, "MTFA_ALIGNED",        "MTFA_MIXED")
    _check(inp.confluence_grade in ("A+", "A"),                    0.15, "CONFLUENCE_HIGH",     "CONFLUENCE_LOW")
    _check(inp.order_flow_bonus > 0,                               0.12, "ORDER_FLOW_POSITIVE", "ORDER_FLOW_NEGATIVE")
    _check(_vwap_aligned_via_ema(inp),                             0.12, "VWAP_ALIGNED",        "VWAP_AGAINST")

    # Price zone — label depends on direction
    if inp.direction == "BUY":
        _check(inp.price_in_discount, 0.08, "DISCOUNT_ZONE_BUY", "PREMIUM_ZONE_BUY")
    else:
        _check(inp.price_in_premium,  0.08, "PREMIUM_ZONE_SELL", "DISCOUNT_ZONE_SELL")

    # CVD — skip entirely (no weight added) if not available; no penalty
    if inp.cvd_slope_m5 is not None:
        cvd_ok = (
            (inp.direction == "BUY"  and inp.cvd_slope_m5 > 0) or
            (inp.direction == "SELL" and inp.cvd_slope_m5 < 0)
        )
        _check(cvd_ok, 0.07, "CVD_ALIGNED", "CVD_WEAK")

    _check(inp.session_quality >= 0.6, 0.10, "SESSION_QUALITY_OK", "SESSION_WEAK")
    _check(inp.mtfa_cisd_m5,           0.08, "CISD_M5_CONFIRM",    "CISD_ABSENT")
    _check(_fvg_aligned(inp),          0.06, "FVG_ALIGNED",        "FVG_ABSENT")
    _check(_ob_aligned(inp),           0.06, "OB_ALIGNED",         "OB_ABSENT")
    _check(inp.range_compression >= 0.5, 0.04, "COMPRESSION_PRESENT", "NO_COMPRESSION")

    coherence = total / max_total if max_total > 0.0 else 0.0
    return round(coherence, 6), confirmations, weak_points


# ---------------------------------------------------------------------------
# Silent risk builder
# ---------------------------------------------------------------------------

def _build_silent_risks(inp: BtcNarratorInput) -> list[str]:
    risks: list[str] = []
    if inp.smc_liquidity_above and inp.direction == "BUY":
        risks.append("LIQUIDITY_ABOVE_MAY_TRAP")
    if inp.impulse_score >= 0.6:
        risks.append("RECENT_IMPULSE_ENTRY_RISK")
    if inp.session_name == "ASIA":
        risks.append("LOW_LIQUIDITY_SESSION")
    if inp.range_compression < 0.2 and inp.impulse_score >= 0.5:
        risks.append("EXTENDED_MARKET_LATE_ENTRY")
    if inp.delta_last is not None and abs(inp.delta_last) < 0.05:
        risks.append("WEAK_DELTA_NO_CONVICTION")
    return risks


# ---------------------------------------------------------------------------
# Exit profile selector
# ---------------------------------------------------------------------------

def _select_exit_profile(
    inp: BtcNarratorInput,
    verdict: str,
    coherence: float,
) -> str:
    if verdict in _BLOCKING_VERDICTS:
        return "FAST_POSITIVE"
    if verdict in ("MARKET_WANTS_UP", "MARKET_WANTS_DOWN") and coherence >= 0.85:
        if inp.order_flow_bonus > 0 and (inp.cvd_slope_m5 or 0) > 0:
            return "HOLD_IF_STRONG"
        return "NORMAL_SCALP"
    if inp.confluence_grade in ("A+", "A"):
        return "NORMAL_SCALP"
    return "FAST_POSITIVE"


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _log_narrative(inp: BtcNarratorInput, n: BtcNarrative) -> None:
    log.info(
        "[BTC_MARKET_NARRATOR] direction=%s verdict=%s coherence=%.2f "
        "confidence=%.2f confirmations=%s weak=%s risks=%s upgrades_b=%s blocks=%s",
        inp.direction,
        n.verdict,
        n.coherence,
        n.confidence,
        n.strong_confirmations,
        n.weak_points,
        n.silent_risks,
        n.verdict_upgrades_b_to_a,
        n.verdict_blocks_entry,
    )


# ---------------------------------------------------------------------------
# Main narrator class
# ---------------------------------------------------------------------------

class BtcMarketNarrator:
    """Constructs a coherent market narrative from existing pipeline signals.

    No MT5 calls. All inputs come from smc_confluence_tagger, mtfa_filter,
    confluence_engine, btc_exit_danger, and geometry_engine.
    """

    def narrate(self, inp: BtcNarratorInput) -> BtcNarrative:
        try:
            return self._narrate_inner(inp)
        except Exception as exc:
            log.error("[BTC_MARKET_NARRATOR] narration error: %s", exc)
            return BtcNarrative(
                verdict="MARKET_IS_DANGEROUS",
                coherence=0.0,
                confidence=0.0,
                strong_confirmations=[],
                weak_points=["NARRATOR_ERROR"],
                silent_risks=[f"NARRATOR_EXCEPTION:{type(exc).__name__}"],
                verdict_blocks_entry=True,
                verdict_upgrades_b_to_a=False,
                suggested_exit_profile="FAST_POSITIVE",
            )

    def _narrate_inner(self, inp: BtcNarratorInput) -> BtcNarrative:
        # ── Step 1: detect blocking verdicts (priority order) ─────────────────
        is_trapping = (
            inp.smc_inducement
            and (
                (inp.smc_liquidity_above and inp.direction == "BUY")
                or (inp.smc_liquidity_below and inp.direction == "SELL")
            )
        )
        is_exhausted = (
            inp.impulse_score >= 0.80
            and inp.m1_last_body_pct >= 0.70
            and _entry_timing_late(inp)
        )
        is_dangerous = (
            inp.danger_signal_count >= 2
            or (_smc_contradicts(inp.smc_direction, inp.direction) and inp.smc_score >= 0.75)
            or (_mtfa_h1_contradicts(inp.mtfa_h1_direction, inp.direction) and inp.mtfa_score >= 70)
        )

        silent_risks = _build_silent_risks(inp)

        blocking_verdict: str | None = None
        if is_trapping:
            blocking_verdict = "MARKET_IS_TRAPPING"
        elif is_exhausted:
            blocking_verdict = "MARKET_IS_EXHAUSTED"
        elif is_dangerous:
            blocking_verdict = "MARKET_IS_DANGEROUS"

        if blocking_verdict is not None:
            coherence, confirmations, weak_points = _build_coherence_score(inp)
            narrative = BtcNarrative(
                verdict=blocking_verdict,
                coherence=round(coherence, 4),
                confidence=round(coherence * 0.5, 4),
                strong_confirmations=confirmations,
                weak_points=weak_points,
                silent_risks=silent_risks,
                verdict_blocks_entry=True,
                verdict_upgrades_b_to_a=False,
                suggested_exit_profile="FAST_POSITIVE",
            )
            _log_narrative(inp, narrative)
            return narrative

        # ── Step 2: build coherence score ─────────────────────────────────────
        coherence, confirmations, weak_points = _build_coherence_score(inp)

        # ── Step 3: determine verdict ──────────────────────────────────────────
        smc_ok  = _smc_aligned(inp.smc_direction, inp.direction)
        mtfa_ok = _mtfa_bias_aligned(inp.mtfa_bias, inp.direction)
        upgrades_b_to_a = False

        if coherence >= 0.78 and smc_ok and mtfa_ok:
            verdict    = "MARKET_WANTS_UP" if inp.direction == "BUY" else "MARKET_WANTS_DOWN"
            confidence = coherence
            upgrades_b_to_a = (
                coherence >= 0.85
                and inp.confluence_grade in ("A+", "A", "B")
            )
        elif coherence >= 0.55:
            verdict    = "MARKET_IS_UNDECIDED"
            confidence = coherence * 0.7
        elif inp.range_compression >= 0.65:
            verdict    = "MARKET_IS_COMPRESSING"
            confidence = 0.4
        else:
            verdict    = "MARKET_IS_UNDECIDED"
            confidence = coherence * 0.5

        # ── Step 4: exit profile ───────────────────────────────────────────────
        suggested_exit = _select_exit_profile(inp, verdict, coherence)

        narrative = BtcNarrative(
            verdict=verdict,
            coherence=round(coherence, 4),
            confidence=round(confidence, 4),
            strong_confirmations=confirmations,
            weak_points=weak_points,
            silent_risks=silent_risks,
            verdict_blocks_entry=False,
            verdict_upgrades_b_to_a=upgrades_b_to_a,
            suggested_exit_profile=suggested_exit,
        )
        _log_narrative(inp, narrative)
        return narrative
