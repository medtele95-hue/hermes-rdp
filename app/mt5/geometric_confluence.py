"""Geometric Confluence Layer — HERMES v1.6

Analysis-only module. No MT5 import. No direct order execution. No lot changes.

Modes
-----
SHADOW (default): runs full analysis, logs everything, final_bonus = 0.
ACTIVE:           applies bonus to final confluence for OF/SMC-exempt paths.

Guarantee: this module never emits [FIB_CONFLUENCE] (that tag belongs to the
FIB_CONFLUENCE_EXECUTION_AGENT). Fib output uses [GEO_FIB] only.
"""
from __future__ import annotations

import math

from app.config import GEOMETRIC_FIB_HIT_TOLERANCE
from app.logger import log

# ---------------------------------------------------------------------------
# Module-level config defaults (overridden per-call via function parameters)
# ---------------------------------------------------------------------------
GEOMETRIC_CONFLUENCE_MODE: str = "SHADOW"
GEOMETRIC_CONFIRM_BONUS: float = 5.0
GEOMETRIC_RATIO_TOLERANCE: float = 0.05

SCHEMA_VERSION: str = "geometric_confluence.v1"

# ---------------------------------------------------------------------------
# Fibonacci level sets
# ---------------------------------------------------------------------------
_RET_LEVELS  = (0.236, 0.382, 0.5, 0.618, 0.786, 0.886, 1.0)
_EXT_LEVELS  = (1.272, 1.618, 2.0, 2.618, 3.618, 4.236)
_ABCD_LEVELS = (1.0, 1.272, 1.618, 2.0, 2.618)

# ---------------------------------------------------------------------------
# Harmonic pattern ratio specifications
# Each ratio is (min, max).  None = unchecked.
# D_XA  = abs(D - A) / abs(A - X)   (works for retrace AND extension)
# D_XC  = abs(D - C) / abs(C - X)   (Cypher only)
# ---------------------------------------------------------------------------
_SPECS: dict[str, dict] = {
    "GARTLEY": {
        "AB_XA": (0.568, 0.668),   # 0.618 ± tol
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.272, 1.618),
        "D_XA":  (0.736, 0.836),   # 0.786 ± tol
        "D_XC":  None,
    },
    "BUTTERFLY": {
        "AB_XA": (0.736, 0.836),   # 0.786 ± tol
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.618, 2.618),
        "D_XA":  (1.27, 1.618),
        "D_XC":  None,
    },
    "BAT": {
        "AB_XA": (0.332, 0.550),   # 0.382-0.5 ± tol
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.618, 2.618),
        "D_XA":  (0.836, 0.936),   # 0.886 ± tol
        "D_XC":  None,
    },
    "CRAB": {
        "AB_XA": (0.332, 0.668),   # 0.382-0.618 ± tol
        "BC_AB": (0.382, 0.886),
        "CD_BC": (2.568, 3.668),   # 2.618-3.618 ± tol
        "D_XA":  (1.568, 1.952),   # 1.618-1.902 ± tol
        "D_XC":  None,
    },
    "SHARK": {
        "AB_XA": (1.080, 1.668),   # 1.13-1.618 ± tol
        "BC_AB": (1.568, 2.290),   # 1.618-2.24 ± tol
        "CD_BC": (0.836, 1.180),   # 0.886-1.13 ± tol
        "D_XA":  None,
        "D_XC":  None,
    },
    "CYPHER": {
        "AB_XA": (0.332, 0.668),   # 0.382-0.618 ± tol
        "BC_AB": (1.080, 1.464),   # 1.13-1.414 ± tol
        "CD_BC": (1.222, 2.050),   # 1.272-2.0 ± tol
        "D_XA":  None,
        "D_XC":  (0.736, 0.836),   # 0.786 ± tol
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _in_range(value: float, rng: tuple[float, float] | None) -> bool:
    if rng is None:
        return True
    return rng[0] <= value <= rng[1]


def _detect_swing_points(rates, window: int = 5) -> list[tuple[int, float, str]]:
    """Swing highs/lows from closed candles only (iloc[:-1] — no repaint).

    Returns list of (bar_index, price, 'H'|'L') sorted ascending by bar_index.
    """
    if rates is None or getattr(rates, "empty", True):
        return []
    if len(rates) < window * 2 + 2:
        return []
    try:
        df = rates.iloc[:-1]          # drop last (current/incomplete) candle
        highs = df["high"].to_numpy(dtype=float)
        lows  = df["low"].to_numpy(dtype=float)
    except Exception:
        return []
    n = len(highs)
    points: list[tuple[int, float, str]] = []
    for i in range(window, n - window):
        lo = max(0, i - window)
        hi = min(n, i + window + 1)
        if highs[i] >= highs[lo:hi].max():
            points.append((i, float(highs[i]), "H"))
        if lows[i]  <= lows[lo:hi].min():
            points.append((i, float(lows[i]),  "L"))
    points.sort(key=lambda p: p[0])
    return points


def _alternating_swings(
    points: list[tuple[int, float, str]],
) -> list[tuple[int, float, str]]:
    """Collapse consecutive same-type swings, keeping the most extreme."""
    if not points:
        return []
    result: list[tuple[int, float, str]] = [points[0]]
    for p in points[1:]:
        if p[2] == result[-1][2]:
            if p[2] == "H" and p[1] > result[-1][1]:
                result[-1] = p
            elif p[2] == "L" and p[1] < result[-1][1]:
                result[-1] = p
        else:
            result.append(p)
    return result


def _extract_xabcd(
    swing_points: list[tuple[int, float, str]],
    direction: str,
) -> tuple[float, float, float, float, float] | None:
    """Extract (X, A, B, C, D) prices from the last 5 alternating swing points.

    BUY  → expects L-H-L-H-L sequence (D is a potential reversal up)
    SELL → expects H-L-H-L-H sequence (D is a potential reversal down)
    Falls back to either polarity when direction is WAIT/unknown.
    """
    alt = _alternating_swings(swing_points)
    if len(alt) < 5:
        return None
    last5 = alt[-5:]
    types = tuple(p[2] for p in last5)
    buy_seq  = ("L", "H", "L", "H", "L")
    sell_seq = ("H", "L", "H", "L", "H")
    if direction == "BUY" and types == buy_seq:
        return tuple(p[1] for p in last5)   # type: ignore[return-value]
    if direction == "SELL" and types == sell_seq:
        return tuple(p[1] for p in last5)   # type: ignore[return-value]
    # direction-agnostic fallback (WAIT or no match)
    if types in (buy_seq, sell_seq):
        return tuple(p[1] for p in last5)   # type: ignore[return-value]
    return None


def _harmonic_ratios(
    X: float, A: float, B: float, C: float, D: float,
) -> dict[str, float]:
    """Compute standardized harmonic ratios for an XABCD swing sequence."""
    eps = 1e-10
    xa = abs(A - X)
    ab = abs(B - A)
    bc = abs(C - B)
    cd = abs(D - C)
    xc = abs(C - X)
    return {
        "XA":    round(xa, 6),
        "AB":    round(ab, 6),
        "BC":    round(bc, 6),
        "CD":    round(cd, 6),
        "XC":    round(xc, 6),
        "AB_XA": round(ab / max(xa, eps), 6),
        "BC_AB": round(bc / max(ab, eps), 6),
        "CD_BC": round(cd / max(bc, eps), 6),
        # D_XA: distance of D from A relative to XA leg
        "D_XA":  round(abs(D - A) / max(xa, eps), 6),
        # D_XC: Cypher — D retrace of XC leg measured from C
        "D_XC":  round(abs(D - C) / max(xc, eps), 6),
    }


def _validate_harmonic(ratios: dict) -> tuple[bool, str, float]:
    """Check ratios against every pattern spec.

    Returns (valid, best_pattern_name, quality_0_to_1).
    """
    best_pattern = "NONE"
    best_quality = 0.0
    for name, spec in _SPECS.items():
        checks = [
            (ratios["AB_XA"], spec["AB_XA"]),
            (ratios["BC_AB"], spec["BC_AB"]),
            (ratios["CD_BC"], spec["CD_BC"]),
            (ratios["D_XA"],  spec["D_XA"]),
            (ratios["D_XC"],  spec["D_XC"]),
        ]
        total  = sum(1 for _, r in checks if r is not None)
        passed = sum(1 for v, r in checks if r is not None and _in_range(v, r))
        if total == 0:
            continue
        if passed < total:
            continue
        quality = passed / total
        if quality > best_quality:
            best_quality = quality
            best_pattern = name
    return (best_pattern != "NONE"), best_pattern, best_quality


def _harmonic_quality_grade(quality: float) -> str:
    if quality >= 0.95:
        return "A"
    if quality >= 0.80:
        return "B"
    if quality >= 0.60:
        return "C"
    return "D"


def _detect_best_harmonic(
    swing_points: list[tuple[int, float, str]],
    direction: str,
) -> tuple[str, str, float, dict]:
    """Return (pattern, quality_grade, harmonic_score_0_100, key_ratios)."""
    xabcd = _extract_xabcd(swing_points, direction)
    if xabcd is None:
        return "NONE", "D", 0.0, {}
    X, A, B, C, D = xabcd
    ratios = _harmonic_ratios(X, A, B, C, D)
    valid, pattern, quality = _validate_harmonic(ratios)
    if not valid:
        return "NONE", "D", 0.0, {}
    grade = _harmonic_quality_grade(quality)
    score = round(quality * 100, 1)
    key_ratios = {k: ratios[k] for k in ("AB_XA", "BC_AB", "CD_BC", "D_XA", "D_XC")}
    return pattern, grade, score, key_ratios


# ---------------------------------------------------------------------------
# Fibonacci helpers
# ---------------------------------------------------------------------------

def _fib_levels(high: float, low: float) -> tuple[list[float], list[float]]:
    """Return (retracement_prices, extension_prices) for a swing."""
    rng = high - low
    if rng <= 0:
        return [], []
    rets = [round(high - rng * phi, 8) for phi in _RET_LEVELS]
    exts = [round(low  + rng * phi, 8) for phi in _EXT_LEVELS]
    return rets, exts


def _nearest_level(price: float, levels: list[float]) -> tuple[float, float]:
    """Return (closest_level, distance)."""
    if not levels:
        return 0.0, float("inf")
    best_lvl  = levels[0]
    best_dist = abs(price - levels[0])
    for lvl in levels[1:]:
        d = abs(price - lvl)
        if d < best_dist:
            best_dist = d
            best_lvl  = lvl
    return best_lvl, best_dist


def _check_hit(price: float, levels: list[float], tol: float) -> tuple[bool, float | None]:
    lvl, dist = _nearest_level(price, levels)
    if dist <= tol:
        return True, lvl
    return False, None


def _phi_for_level(hit_price: float, high: float, low: float, ext: bool) -> float | None:
    rng = high - low
    if rng <= 0 or hit_price is None:
        return None
    pool = _EXT_LEVELS if ext else _RET_LEVELS
    if ext:
        raw = (hit_price - low) / rng
    else:
        raw = (high - hit_price) / rng
    return min(pool, key=lambda p: abs(p - raw))


def _abcd_projections(A: float, B: float, C: float) -> list[float]:
    """D projection levels: C ± (B - A) * k for each k in _ABCD_LEVELS."""
    ba = B - A   # signed
    out: list[float] = []
    for k in _ABCD_LEVELS:
        out.append(round(C + ba * k, 8))
        out.append(round(C - ba * k, 8))
    return out


# ---------------------------------------------------------------------------
# Swing range helpers
# ---------------------------------------------------------------------------

def _recent_swing_range(rates, window: int = 5) -> tuple[float, float]:
    """Return (swing_high, swing_low) from closed candles."""
    if rates is None or getattr(rates, "empty", True) or len(rates) < window * 2 + 2:
        return 0.0, 0.0
    try:
        df     = rates.iloc[:-1]
        highs  = df["high"].to_numpy(dtype=float)
        lows   = df["low"].to_numpy(dtype=float)
        n      = len(highs)
        sh     = 0.0
        sl_val = float("inf")
        for i in range(window, n - window):
            lo = max(0, i - window)
            hi = min(n, i + window + 1)
            if highs[i] >= highs[lo:hi].max():
                sh = max(sh, highs[i])
            if lows[i] <= lows[lo:hi].min():
                sl_val = min(sl_val, lows[i])
        return sh, (sl_val if sl_val < float("inf") else 0.0)
    except Exception:
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Gann (lightweight)
# ---------------------------------------------------------------------------

def _gann_angle_hit(price: float, high: float, low: float) -> tuple[bool, str]:
    """Approximate 1x1, 1x2, 2x1 Gann angle proximity.  Weak confluence only."""
    if high <= low or high <= 0:
        return False, "NONE"
    rng = high - low
    tol = rng * 0.02          # 2 % of range
    angles = {
        "1x1": low + rng * 0.500,
        "1x2": low + rng * 0.333,
        "2x1": low + rng * 0.667,
    }
    for label, lvl in angles.items():
        if abs(price - lvl) <= tol:
            return True, label
    return False, "NONE"


def _square_of_9_hit(price: float, high: float) -> bool:
    """Square of 9: price near a ring boundary (sqrt spacing ≈ 0.25)."""
    if high <= 0 or price <= 0:
        return False
    try:
        diff = abs(math.sqrt(price) - math.sqrt(high)) % 0.25
        return diff <= 0.05 or diff >= 0.20
    except Exception:
        return False


def _time_price_square_hit(price: float, high: float, low: float, bar_count: int) -> bool:
    """Price lies on a 'square' level given bar count as the time unit."""
    if high <= low or bar_count <= 0:
        return False
    pts_per_bar = (high - low) / bar_count
    if pts_per_bar <= 0:
        return False
    frac = ((price - low) / pts_per_bar) % 1.0
    return frac <= 0.05 or frac >= 0.95


# ---------------------------------------------------------------------------
# Fibonacci / golden spiral
# ---------------------------------------------------------------------------

def _spiral_confluence(price: float, high: float, low: float) -> tuple[bool, float | None]:
    """Golden ratio spiral projection at low + rng/phi^n and high - rng/phi^n."""
    if high <= low:
        return False, None
    rng = high - low
    phi = (1 + math.sqrt(5)) / 2
    tol = rng * GEOMETRIC_FIB_HIT_TOLERANCE
    for n in (1, 2):
        for base, sign in ((low, 1), (high, -1)):
            lvl = base + sign * rng / (phi ** n)
            if abs(price - lvl) <= tol:
                return True, round(lvl, 8)
    return False, None


# ---------------------------------------------------------------------------
# Confluence zone clustering
# ---------------------------------------------------------------------------

def _cluster_levels(levels: list[float], tol: float) -> list[list[float]]:
    """Group nearby price levels.  Returns list of clusters."""
    clean = sorted({round(v, 8) for v in levels if v > 0})
    if not clean:
        return []
    clusters: list[list[float]] = [[clean[0]]]
    for lvl in clean[1:]:
        if abs(lvl - clusters[-1][-1]) <= tol:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return clusters


# ---------------------------------------------------------------------------
# Scoring and grading
# ---------------------------------------------------------------------------

def _geometric_grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _geometric_decision(score: float, direction: str, setup_dir: str) -> tuple[str, str]:
    """Return (decision, reason).  REJECT is reserved for confirmed contradictions."""
    aligned = not setup_dir or setup_dir in ("", "WAIT") or direction == setup_dir
    if score >= 75 and aligned:
        return "CONFIRM", "GEOMETRIC_STRUCTURE_ALIGNED"
    if score >= 60:
        return "WEAK_CONFIRM", "MODERATE_GEOMETRIC_CONFLUENCE"
    return "WAIT", "INSUFFICIENT_GEOMETRIC_CONFLUENCE"


# ---------------------------------------------------------------------------
# Bonus computation (called from main.py)
# ---------------------------------------------------------------------------

def compute_geometric_bonus(
    geo_result: dict,
    is_smc_hard_blocked: bool = False,
    confirm_bonus: float = GEOMETRIC_CONFIRM_BONUS,
) -> float:
    """Return the confluence bonus to apply.

    Rules
    -----
    * SHADOW mode → always 0.
    * SMC confirmation-matrix hard-block → always 0 (bonus cannot override
      CONFIRMATION_MATRIX_HARD_BLOCK; this invariant is enforced here).
    * REJECT → 0 and no hard-block (REJECT is observational only).
    * CONFIRM  → confirm_bonus.
    * WEAK_CONFIRM → confirm_bonus / 2.
    * WAIT / anything else → 0.
    """
    if not geo_result:
        return 0.0
    mode = str(geo_result.get("mode") or "SHADOW").upper()
    if mode != "ACTIVE":
        return 0.0
    if is_smc_hard_blocked:
        return 0.0
    decision = str(geo_result.get("decision") or "WAIT").upper()
    if decision == "CONFIRM":
        return float(confirm_bonus)
    if decision == "WEAK_CONFIRM":
        return float(confirm_bonus) / 2.0
    return 0.0          # WAIT, REJECT, or unknown — no bonus, no block


# ---------------------------------------------------------------------------
# Null / error result
# ---------------------------------------------------------------------------

def _null_result(symbol: str, direction: str, mode: str, reason: str) -> dict:
    return {
        "enabled": True,
        "symbol": symbol,
        "direction": direction,
        "mode": mode,
        "harmonic_pattern": "NONE",
        "harmonic_quality": "D",
        "harmonic_score": 0,
        "harmonic_ratios": {},
        "fib_retracement_hit": False,
        "fib_retracement_level": None,
        "fib_extension_hit": False,
        "fib_extension_level": None,
        "abcd_projection_hit": False,
        "abcd_projection_level": None,
        "gann_angle_hit": False,
        "gann_angle": "NONE",
        "square_of_9_hit": False,
        "time_price_square_hit": False,
        "spiral_confluence_hit": False,
        "golden_spiral_projection": None,
        "confluence_zone_count": 0,
        "confluence_zone_price": None,
        "distance_to_confluence_pts": None,
        "geometric_score": 0,
        "geometric_grade": "D",
        "decision": "WAIT",
        "reason": reason,
        "schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_geometric_confluence(
    symbol: str,
    direction: str,
    rates_m1,
    rates_m5,
    rates_m15,
    rates_h1,
    setup_context: dict | None = None,
    mode: str = GEOMETRIC_CONFLUENCE_MODE,
    ratio_tolerance: float = GEOMETRIC_RATIO_TOLERANCE,
) -> dict:
    """Full geometric confluence analysis.  No MT5 calls.  No direct execution.

    Uses iloc[:-1] on every rates frame — closed candles only, no repaint.

    Parameters
    ----------
    symbol, direction : str
    rates_m1/m5/m15/h1 : pandas DataFrames with open/high/low/close columns
    setup_context : optional best_candidate dict from setup_hunter
    mode : "SHADOW" | "ACTIVE"  (default from module constant)
    ratio_tolerance : override for harmonic ratio tolerance band
    """
    mode      = str(mode or "SHADOW").upper()
    direction = str(direction or "").upper()
    symbol    = str(symbol or "")
    setup_dir = str((setup_context or {}).get("direction") or "").upper()

    # ── Primary rates: H1 for swing patterns, M5 for Fib/current price ──────
    primary = rates_h1 if (
        rates_h1 is not None
        and not getattr(rates_h1, "empty", True)
        and len(rates_h1) >= 15
    ) else rates_m15

    fib_rates = rates_m5 if (
        rates_m5 is not None
        and not getattr(rates_m5, "empty", True)
        and len(rates_m5) >= 12
    ) else primary

    # ── Current price from latest closed candle ───────────────────────────
    current_price = 0.0
    for _r in (rates_m1, rates_m5, rates_m15, rates_h1):
        if _r is not None and not getattr(_r, "empty", True) and len(_r) > 1:
            try:
                current_price = float(_r.iloc[-2]["close"])  # last CLOSED candle
                if current_price > 0:
                    break
            except Exception:
                pass

    if current_price <= 0:
        return _null_result(symbol, direction, mode, "NO_PRICE_DATA")

    # ── Swing detection ──────────────────────────────────────────────────
    sw_win = 5 if primary is not None and not getattr(primary, "empty", True) and len(primary) >= 50 else 3
    swing_pts = _detect_swing_points(primary, window=sw_win)
    alt_swings = _alternating_swings(swing_pts)
    log.info(
        "[GEOMETRIC_SWINGS] symbol=%s detected_swings=%s swings=%s",
        symbol,
        len(alt_swings),
        [(index, round(price, 6), kind) for index, price, kind in alt_swings[-5:]],
    )

    # ── Swing high/low for Fib reference range ───────────────────────────
    sh, sl = _recent_swing_range(fib_rates, window=5)
    if sh <= 0 or sl <= 0 or sh <= sl:
        # Fallback: full candle range
        try:
            _df = fib_rates.iloc[:-1] if fib_rates is not None else None
            if _df is not None and len(_df) >= 2:
                sh = float(_df["high"].max())
                sl = float(_df["low"].min())
        except Exception:
            pass
    if sh <= 0 or sl <= 0 or sh <= sl:
        return _null_result(symbol, direction, mode, "INSUFFICIENT_SWING_DATA")

    fib_tol = (sh - sl) * GEOMETRIC_FIB_HIT_TOLERANCE

    # ── Harmonic pattern ─────────────────────────────────────────────────
    # Rebuild specs with caller-supplied tolerance (in case it differs from default)
    if ratio_tolerance != GEOMETRIC_RATIO_TOLERANCE:
        t = ratio_tolerance
        _active_specs = {
            "GARTLEY":   {"AB_XA": (0.618-t, 0.618+t), "BC_AB": (0.382, 0.886), "CD_BC": (1.272, 1.618), "D_XA": (0.786-t, 0.786+t), "D_XC": None},
            "BUTTERFLY": {"AB_XA": (0.786-t, 0.786+t), "BC_AB": (0.382, 0.886), "CD_BC": (1.618, 2.618), "D_XA": (1.27, 1.618),        "D_XC": None},
            "BAT":       {"AB_XA": (0.382-t, 0.5+t),   "BC_AB": (0.382, 0.886), "CD_BC": (1.618, 2.618), "D_XA": (0.886-t, 0.886+t),   "D_XC": None},
            "CRAB":      {"AB_XA": (0.382-t, 0.618+t), "BC_AB": (0.382, 0.886), "CD_BC": (2.618-t, 3.618+t), "D_XA": (1.618-t, 1.902+t), "D_XC": None},
            "SHARK":     {"AB_XA": (1.13-t, 1.618+t),  "BC_AB": (1.618-t, 2.24+t), "CD_BC": (0.886-t, 1.13+t), "D_XA": None, "D_XC": None},
            "CYPHER":    {"AB_XA": (0.382-t, 0.618+t), "BC_AB": (1.13-t, 1.414+t), "CD_BC": (1.272-t, 2.0+t), "D_XA": None, "D_XC": (0.786-t, 0.786+t)},
        }
        # Temporarily replace module-level specs (local to this call path)
        _saved = dict(_SPECS)
        _SPECS.update(_active_specs)
        harm_pattern, harm_grade, harm_score, harm_ratios = _detect_best_harmonic(swing_pts, direction)
        _SPECS.clear()
        _SPECS.update(_saved)
    else:
        harm_pattern, harm_grade, harm_score, harm_ratios = _detect_best_harmonic(swing_pts, direction)
    log.info(
        "[GEOMETRIC_RATIOS] symbol=%s pattern=%s quality=%s ratios=%s",
        symbol, harm_pattern, harm_grade, harm_ratios,
    )

    # ── Fibonacci ─────────────────────────────────────────────────────────
    ret_lvls, ext_lvls = _fib_levels(sh, sl)
    fib_ret_hit, fib_ret_price = _check_hit(current_price, ret_lvls, fib_tol)
    fib_ext_hit, fib_ext_price = _check_hit(current_price, ext_lvls, fib_tol)
    fib_ret_phi = _phi_for_level(fib_ret_price, sh, sl, False) if fib_ret_hit else None
    fib_ext_phi = _phi_for_level(fib_ext_price, sh, sl, True)  if fib_ext_hit else None

    # ── ABCD projection ───────────────────────────────────────────────────
    abcd_hit   = False
    abcd_phi: float | None = None
    abcd_price: float | None = None
    if len(alt_swings) >= 3:
        A_sw = alt_swings[-3][1]
        B_sw = alt_swings[-2][1]
        C_sw = alt_swings[-1][1]
        projs = _abcd_projections(A_sw, B_sw, C_sw)
        abcd_hit, abcd_price = _check_hit(current_price, projs, fib_tol)
        if abcd_hit and abcd_price is not None:
            ab_leg = abs(B_sw - A_sw)
            if ab_leg > 0:
                cd_frac = abs(abcd_price - C_sw) / ab_leg
                abcd_phi = min(_ABCD_LEVELS, key=lambda p: abs(p - cd_frac))

    # ── Gann (lightweight) ────────────────────────────────────────────────
    gann_hit, gann_angle_str = _gann_angle_hit(current_price, sh, sl)
    sq9_hit = _square_of_9_hit(current_price, sh)
    try:
        _bar_n = len(fib_rates.iloc[:-1]) if fib_rates is not None and not getattr(fib_rates, "empty", True) else 50
    except Exception:
        _bar_n = 50
    tp_sq_hit = _time_price_square_hit(current_price, sh, sl, _bar_n)

    # ── Golden spiral ─────────────────────────────────────────────────────
    spiral_hit, spiral_price = _spiral_confluence(current_price, sh, sl)

    # ── Confluence zone clustering ─────────────────────────────────────────
    zone_levels: list[float] = []
    if fib_ret_price is not None:  zone_levels.append(fib_ret_price)
    if fib_ext_price is not None:  zone_levels.append(fib_ext_price)
    if abcd_price    is not None and abcd_hit:   zone_levels.append(abcd_price)
    if spiral_price  is not None and spiral_hit: zone_levels.append(spiral_price)
    if harm_pattern != "NONE" and len(alt_swings) >= 5:
        zone_levels.append(alt_swings[-1][1])   # harmonic D price

    cluster_tol  = (sh - sl) * 0.005
    clusters     = _cluster_levels(zone_levels, cluster_tol)
    best_count   = 0
    best_zone_price: float | None = None
    for cl in clusters:
        if len(cl) > best_count:
            best_count = len(cl)
            best_zone_price = round(sum(cl) / len(cl), 8)
    dist_pts = abs(current_price - best_zone_price) if best_zone_price is not None else None

    # ── Composite score ───────────────────────────────────────────────────
    score = 0.0
    if harm_pattern != "NONE":
        score += {"A": 45, "B": 40, "C": 35, "D": 30}.get(harm_grade, 30)
    if fib_ret_hit:  score += 10
    if fib_ext_hit:  score += 10
    if abcd_hit:     score += 15
    if best_count >= 3:
        score += 20
    elif best_count == 2:
        score += 10
    if gann_hit:    score += 5
    if sq9_hit:     score += 5
    if spiral_hit:  score += 5
    score = min(100.0, score)

    grade    = _geometric_grade(score)

    # Fix 3: Neutral geometry in H4 RANGE regime — no harmonic pattern → score 5.0
    # Without this, range markets always score 0 (no pattern) which kills composite.
    # smc_h4_direction is the authoritative string from the SMC tagger; h4_main_bias is a float
    # score from top_down_market_reader and must NOT be used for this string comparison.
    _h4_bias_ctx = str(
        (setup_context or {}).get("smc_h4_direction")
        or (setup_context or {}).get("h4_bias")
        or ""
    ).upper()
    if _h4_bias_ctx == "RANGE" and harm_pattern == "NONE" and grade == "D":
        score = 5.0
        grade = _geometric_grade(score)  # still D but score is neutral, not 0
        log.info(
            "[GEO_RANGE_NEUTRAL] symbol=%s h4_bias=RANGE pattern=NONE grade=D score_applied=5.0",
            symbol,
        )

    decision, reason = _geometric_decision(score, direction, setup_dir)

    # ── Structured logging ────────────────────────────────────────────────
    log.info(
        "[GEOMETRIC_CONFLUENCE] symbol=%s direction=%s score=%.1f grade=%s"
        " decision=%s pattern=%s confluence_count=%d mode=%s",
        symbol, direction, score, grade, decision,
        harm_pattern, best_count, mode,
    )
    if harm_pattern != "NONE":
        log.info(
            "[HARMONIC_PATTERN] symbol=%s pattern=%s quality=%s ratios=%s",
            symbol, harm_pattern, harm_grade, harm_ratios,
        )
    log.info(
        "[GEO_FIB] symbol=%s retracement=%s extension=%s abcd=%s",
        symbol,
        fib_ret_phi  if fib_ret_hit  else None,
        fib_ext_phi  if fib_ext_hit  else None,
        abcd_phi     if abcd_hit     else None,
    )
    log.info(
        "[GANN_CONFLUENCE] symbol=%s angle=%s square9=%s",
        symbol, gann_angle_str, str(sq9_hit).lower(),
    )

    return {
        "enabled": True,
        "symbol": symbol,
        "direction": direction,
        "mode": mode,
        "harmonic_pattern": harm_pattern,
        "harmonic_quality": harm_grade,
        "harmonic_score": harm_score,
        "harmonic_ratios": harm_ratios,
        "fib_retracement_hit": fib_ret_hit,
        "fib_retracement_level": fib_ret_phi,
        "fib_extension_hit": fib_ext_hit,
        "fib_extension_level": fib_ext_phi,
        "abcd_projection_hit": abcd_hit,
        "abcd_projection_level": abcd_phi,
        "gann_angle_hit": gann_hit,
        "gann_angle": gann_angle_str,
        "square_of_9_hit": sq9_hit,
        "time_price_square_hit": tp_sq_hit,
        "spiral_confluence_hit": spiral_hit,
        "golden_spiral_projection": spiral_price,
        "confluence_zone_count": best_count,
        "confluence_zone_price": best_zone_price,
        "distance_to_confluence_pts": round(dist_pts, 6) if dist_pts is not None else None,
        "geometric_score": round(score, 1),
        "geometric_grade": grade,
        "decision": decision,
        "reason": reason,
        "schema_version": SCHEMA_VERSION,
    }
