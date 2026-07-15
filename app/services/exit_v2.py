"""EXIT V2 — single exit authority for GOLD and BTCUSD#, CLOSE-BASED by design.

Replaces the HERMES_QUICK_EXIT parasite (TP money $1.50 / sneaky lock-SL
$0.80) which captured 100% of exits and starved every winner.

Design invariants:
- CLOSE-BASED: this engine NEVER modifies the SL. The protective levels are
  virtual (in-memory) and enforced by closing the position. The broker SL can
  therefore never be widened BY CONSTRUCTION.
- GOLD: BE armed at +$2.00 profit -> virtual floor at +$0.10 (BE + buffer).
  Trailing armed at +$2.00 peak -> virtual floor at peak - $1.20. UNCHANGED
  by mission GRAND_PLAN_2 mission3.
- BTCUSD#: mission GRAND_PLAN_2 mission3 (2026-07-08, SIMO validé GO) —
  thresholds are a PERCENTAGE OF ENTRY PRICE instead of flat dollars.
  $2 on GOLD at ~$4000 is 0.05% of price; at BTC's ~$63000, that same
  0.05% needs a $31.50 move to arm — 6.5x the absolute distance a flat $2
  demanded (verified live 2026-07-08, mission FIX_KILLSWITCH_PNL's VOLET2
  audit: BTC needed 0.32% of price to arm the OLD flat-$2 threshold vs
  GOLD's 0.05%). The percentage defaults below reproduce GOLD's *relative*
  protection on BTC: btc_be_arm_pct=0.05 (== $2/$4000), btc_be_floor_pct=
  0.0025 (== $0.10/$4000), btc_trail_start_pct=0.05, btc_trail_gap_pct=0.03
  (== $1.20/$4000). The percentage is applied to the position's ENTRY
  price (fixed at open, never the moving current price) and converted to
  an effective USD threshold via the symbol's own tick value/size — so
  each BTC position gets its own $-equivalent floor, logged explicitly.
- TP money disabled (tp_usd=0) — winners run.
- Fail-closed: any exception leaves the original SL/TP untouched.
- Account gate: DEMO=ACTIVE (closes execute), REAL=SHADOW (log-only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.logger import log

MODE_ACTIVE = "ACTIVE"
MODE_SHADOW = "SHADOW"


@dataclass(frozen=True)
class ExitV2Config:
    mode: str = MODE_ACTIVE
    tp_usd: float = 0.0            # 0 = money-TP disabled
    be_arm_usd: float = 2.00       # profit that arms the breakeven floor (GOLD, and BTC fallback)
    be_floor_usd: float = 0.10     # virtual floor once BE armed (BE + buffer)
    trail_start_usd: float = 2.00  # peak that arms the trailing floor
    trail_gap_usd: float = 1.20    # distance kept below the peak
    # mission GRAND_PLAN_2 mission3 (2026-07-08, SIMO validé GO): BTC-only,
    # percentage of entry price — see module docstring for the exact GOLD
    # equivalence each default reproduces. GOLD never reads these fields.
    btc_pct_thresholds_enabled: bool = True
    btc_be_arm_pct: float = 0.05
    btc_be_floor_pct: float = 0.0025
    btc_trail_start_pct: float = 0.05
    btc_trail_gap_pct: float = 0.03


def _usd_per_price_unit(pos: object, symbol_info: object) -> float | None:
    """USD profit change per 1.0 unit of price movement, for THIS position's
    volume — derived from the symbol's own tick value/size, exactly the
    conversion MT5 itself uses to compute pos.profit. None if symbol_info
    is unavailable (caller falls back to the flat $ thresholds)."""
    if symbol_info is None:
        return None
    tick_value = _to_float(getattr(symbol_info, "trade_tick_value", None))
    tick_size = _to_float(getattr(symbol_info, "trade_tick_size", None))
    volume = _to_float(getattr(pos, "volume", None))
    if not tick_value or not tick_size or not volume:
        return None
    return (tick_value / tick_size) * volume


def _effective_thresholds(pos: object, symbol_info: object, cfg: ExitV2Config) -> dict:
    """Resolves the four dollar thresholds this evaluation actually uses.
    GOLD (or BTC with pct thresholds disabled, or missing tick specs):
    the flat $ config values, unchanged. BTC with pct thresholds enabled:
    each threshold computed from % of entry price, converted to USD via
    this position's own tick value — always logged so the effective
    dollar amount is visible per symbol, per mission's explicit
    requirement."""
    symbol = str(getattr(pos, "symbol", "") or "")
    entry_price = _to_float(getattr(pos, "price_open", None))
    is_btc = is_btc_symbol(symbol)
    usd_per_unit = _usd_per_price_unit(pos, symbol_info) if is_btc else None

    if is_btc and cfg.btc_pct_thresholds_enabled and entry_price and usd_per_unit:
        return {
            "be_arm_usd": entry_price * (cfg.btc_be_arm_pct / 100.0) * usd_per_unit,
            "be_floor_usd": entry_price * (cfg.btc_be_floor_pct / 100.0) * usd_per_unit,
            "trail_start_usd": entry_price * (cfg.btc_trail_start_pct / 100.0) * usd_per_unit,
            "trail_gap_usd": entry_price * (cfg.btc_trail_gap_pct / 100.0) * usd_per_unit,
            "scale": "PCT",
            "be_arm_pct": cfg.btc_be_arm_pct,
            "trail_start_pct": cfg.btc_trail_start_pct,
        }
    return {
        "be_arm_usd": cfg.be_arm_usd,
        "be_floor_usd": cfg.be_floor_usd,
        "trail_start_usd": cfg.trail_start_usd,
        "trail_gap_usd": cfg.trail_gap_usd,
        "scale": "USD",
        "be_arm_pct": None,
        "trail_start_pct": None,
    }


def evaluate_exit_v2(pos: object, tick: object, symbol_info: object, cfg: ExitV2Config, state: dict, now: object = None) -> dict:
    """Pure decision function. Returns an action dict; never touches MT5.

    Actions: NONE / CLOSE (reason EXIT_V2_TP | EXIT_V2_BE_FLOOR |
    EXIT_V2_TRAIL_FLOOR). State per ticket: peak_usd, be_armed.

    SPEC_EXIT_CONTEXT_WRITER : `now` est OPTIONNEL et n'a AUCUN effet sur la
    decision — il ne sert qu'a horodater l'armement du breakeven (metadonnee
    `be_arm_time`/`be_arm_price` dans l'etat, pour la future calibration d'Exit
    V2). La condition d'armement et toutes les actions CLOSE/NONE sont
    strictement inchangees. `now=None` (appelants historiques) => be_arm_time None.
    """
    ticket = int(getattr(pos, "ticket", 0) or 0)
    profit = _to_float(getattr(pos, "profit", None))
    side = "BUY" if int(getattr(pos, "type", 0) or 0) == 0 else "SELL"
    if ticket <= 0 or profit is None:
        return {"action": "NONE", "reason": "EXIT_V2_UNREADABLE_POSITION", "ticket": ticket, "side": side}

    thresholds = _effective_thresholds(pos, symbol_info, cfg)
    be_arm_usd = thresholds["be_arm_usd"]
    be_floor_usd = thresholds["be_floor_usd"]
    trail_start_usd = thresholds["trail_start_usd"]
    trail_gap_usd = thresholds["trail_gap_usd"]

    st = state.setdefault(ticket, {"peak_usd": profit, "be_armed": False, "be_arm_time": None, "be_arm_price": None})
    st["peak_usd"] = max(_to_float(st.get("peak_usd")) or profit, profit)
    peak = st["peak_usd"]

    if not st["be_armed"] and profit >= be_arm_usd:
        st["be_armed"] = True
        # SPEC_EXIT_CONTEXT_WRITER : metadonnee CAPTURE-ONLY — on note QUAND et a
        # quel prix le breakeven s'est arme. La ligne ci-dessus (la DECISION
        # d'armer) est inchangee ; on n'ajoute qu'un horodatage.
        st["be_arm_time"] = now.isoformat() if now is not None and hasattr(now, "isoformat") else None
        st["be_arm_price"] = _to_float(getattr(pos, "price_current", None))
        log.info(
            "[EXIT_V2] action=BE_ARMED ticket=%s symbol=%s profit=%.2f floor=%.2f scale=%s be_arm_usd=%.4f be_arm_pct=%s",
            ticket, getattr(pos, "symbol", ""), profit, be_floor_usd,
            thresholds["scale"], thresholds["be_arm_usd"], thresholds["be_arm_pct"],
        )

    # Money TP (disabled when tp_usd == 0)
    if cfg.tp_usd and cfg.tp_usd > 0 and profit >= cfg.tp_usd:
        return _close(ticket, "EXIT_V2_TP", profit, peak, st["be_armed"], side=side)

    # Trailing floor: once the peak reached trail_start, protect peak - gap.
    # The trailing floor can only rise (peak is monotonic).
    floors: list[tuple[str, float]] = []
    if peak >= trail_start_usd:
        floors.append(("EXIT_V2_TRAIL_FLOOR", peak - trail_gap_usd))
    if st["be_armed"]:
        floors.append(("EXIT_V2_BE_FLOOR", be_floor_usd))

    if floors:
        reason, floor = max(floors, key=lambda item: item[1])
        if profit <= floor:
            return _close(ticket, reason, profit, peak, st["be_armed"], floor, side=side, thresholds=thresholds)

    return {
        "action": "NONE",
        "reason": "EXIT_V2_HOLD",
        "ticket": ticket,
        "side": side,
        "profit_usd": profit,
        "peak_usd": peak,
        "be_armed": st["be_armed"],
        "active_floor": max((f for _, f in floors), default=None),
        "threshold_scale": thresholds["scale"],
        "be_arm_usd_effective": round(be_arm_usd, 4),
        "trail_start_usd_effective": round(trail_start_usd, 4),
        "be_floor_usd_effective": round(be_floor_usd, 4),
        "trail_gap_usd_effective": round(trail_gap_usd, 4),
    }


def _close(
    ticket: int,
    reason: str,
    profit: float,
    peak: float,
    be_armed: bool,
    floor: float | None = None,
    side: str = "BUY",
    thresholds: dict | None = None,
) -> dict:
    return {
        "action": "CLOSE",
        "reason": reason,
        "ticket": ticket,
        "side": side,
        "profit_usd": profit,
        "peak_usd": peak,
        "be_armed": be_armed,
        "floor_usd": floor,
        "threshold_scale": (thresholds or {}).get("scale"),
        "be_arm_usd_effective": round((thresholds or {}).get("be_arm_usd", 0.0), 4) if thresholds else None,
        "trail_start_usd_effective": round((thresholds or {}).get("trail_start_usd", 0.0), 4) if thresholds else None,
        "be_floor_usd_effective": round((thresholds or {}).get("be_floor_usd", 0.0), 4) if thresholds else None,
        "trail_gap_usd_effective": round((thresholds or {}).get("trail_gap_usd", 0.0), 4) if thresholds else None,
    }


def is_gold_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().strip()
    return normalized.startswith("GOLD") or normalized.startswith("XAUUSD")


def is_btc_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().strip()
    return normalized.startswith("BTCUSD")


def is_exit_v2_symbol(symbol: object) -> bool:
    """Symbols whose exits belong EXCLUSIVELY to Exit V2.

    GRAND_PLAN 2026-07-08 (décision SIMO, deux symboles officiels) : Exit V2
    est l'autorité de sortie unique pour GOLD# ET BTCUSD#. Le moteur est
    money-based (USD) donc symbol-agnostic par construction.
    """
    normalized = str(symbol or "").upper().strip()
    return is_gold_symbol(normalized) or normalized.startswith("BTCUSD")


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
