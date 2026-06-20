from __future__ import annotations

from typing import TypedDict

from app.logger import log


class TradeCandidate(TypedDict, total=False):
    """Canonical execution candidate contract.

    All fields except setup_type and reason are required for a valid
    execution candidate.  validate_candidate() enforces the contract.
    """

    strategy: str
    symbol: str
    broker_symbol: str
    direction: str       # "BUY" or "SELL"
    entry: float
    sl: float
    tp: float
    rr: float
    confidence: float    # 0–100
    grade: str           # "A+", "A", "B", "C", "D"
    setup_type: str      # optional descriptive tag
    reason: str          # optional descriptive reason
    mode: str            # "ACTIVE_EXECUTION"
    route_allowed: bool  # True for execution candidates
    demo_eligible: bool  # True for execution candidates


_VALID_DIRECTIONS: frozenset[str] = frozenset({"BUY", "SELL"})
_VALID_GRADES: frozenset[str] = frozenset({"A+", "A", "B", "C", "D"})
_NUMERIC_FIELDS: tuple[str, ...] = ("entry", "sl", "tp", "rr")

_REQUIRED_FIELDS: tuple[str, ...] = (
    "strategy",
    "symbol",
    "broker_symbol",
    "direction",
    "entry",
    "sl",
    "tp",
    "rr",
    "confidence",
    "grade",
    "mode",
    "route_allowed",
    "demo_eligible",
)


def validate_candidate(c: dict) -> tuple[bool, str | None, str | None]:
    """Validate a candidate dict against the canonical execution contract.

    Returns (valid, reason, field).
    On success: (True, None, None).
    On failure: (False, "MISSING_FIELD"|"INVALID_FIELD", field_name).

    Only validated candidates should be routed to SetupHunter → DemoRouter.
    """
    sym = str(c.get("symbol") or "")
    strat = str(c.get("strategy") or "")

    # 1. Presence check for all required fields.
    for field in _REQUIRED_FIELDS:
        if c.get(field) is None:
            log.warning(
                "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=MISSING_FIELD field=%s",
                sym, strat, field,
            )
            return False, "MISSING_FIELD", field

    # 2. direction must be BUY or SELL.
    direction = str(c.get("direction") or "").upper()
    if direction not in _VALID_DIRECTIONS:
        log.warning(
            "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=INVALID_FIELD field=direction value=%s",
            sym, strat, direction,
        )
        return False, "INVALID_FIELD", "direction"

    # 3. confidence must be a float in [0, 100].
    confidence = c.get("confidence")
    try:
        conf_val = float(confidence)  # type: ignore[arg-type]
        if not (0.0 <= conf_val <= 100.0):
            raise ValueError("out of range")
    except (TypeError, ValueError):
        log.warning(
            "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=INVALID_FIELD field=confidence value=%s",
            sym, strat, confidence,
        )
        return False, "INVALID_FIELD", "confidence"

    # 4. grade must be one of the canonical set.
    grade = str(c.get("grade") or "").upper().replace("A_PLUS", "A+")
    if grade not in _VALID_GRADES:
        log.warning(
            "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=INVALID_FIELD field=grade value=%s",
            sym, strat, c.get("grade"),
        )
        return False, "INVALID_FIELD", "grade"

    # 5. entry / sl / tp / rr must be finite numbers.
    for field in _NUMERIC_FIELDS:
        val = c.get(field)
        try:
            num = float(val)  # type: ignore[arg-type]
            if num != num:  # NaN guard
                raise ValueError("NaN")
        except (TypeError, ValueError):
            log.warning(
                "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=INVALID_FIELD field=%s value=%s",
                sym, strat, field, val,
            )
            return False, "INVALID_FIELD", field

    # 6. route_allowed must be True; demo_eligible is checked by the router filter.
    if c.get("route_allowed") is not True:
        log.warning(
            "[CANDIDATE_REJECT] symbol=%s strategy=%s reason=INVALID_FIELD field=route_allowed value=%s",
            sym, strat, c.get("route_allowed"),
        )
        return False, "INVALID_FIELD", "route_allowed"

    # demo_eligible is intentionally NOT validated here: setup_hunter._failed_gates()
    # may legitimately set it False when confirmations are pending. The executable_ready
    # filter in setup_hunter already gates on demo_eligible — checking it here causes
    # spurious INVALID_FIELD logs for every waiting candidate.

    return True, None, None
