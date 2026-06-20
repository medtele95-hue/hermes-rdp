from __future__ import annotations

from collections import Counter


BIG_SETUP_FIELDS = [
    "big_setup_score",
    "big_setup_grade",
    "big_setup_status",
    "big_setup_reason",
    "big_setup_tags",
    "big_setup_missing_data",
    "big_setup_direction_bias",
    "big_setup_is_premium",
    "big_setup_is_discount",
    "big_setup_risk_ok",
    "big_setup_time_ok",
    "big_setup_safety_ok",
]


class BigSetupDetector:
    def evaluate(self, payload: dict) -> dict:
        try:
            missing = _missing(payload)
            direction = _direction_bias(payload)
            tags = _tags(payload, direction)
            htf_alignment_score = _top_down_score(payload, direction)
            poi_score = _poi_score(payload)
            liquidity_score = _liquidity_score(payload)
            fvg_ob_ote_score = _fvg_ob_ote_score(payload)
            confirmation_score = _confirmation_score(payload)
            risk_score = _risk_score(payload)
            time_score = _time_score(payload)
            score = (
                htf_alignment_score
                + poi_score
                + liquidity_score
                + fvg_ob_ote_score
                + confirmation_score
                + risk_score
                + time_score
            )
            if missing:
                score = min(score, 74)
            score = max(0, min(100, score))
            return {
                "big_setup_score": score,
                "big_setup_grade": _grade(score),
                "big_setup_status": "TAG_ONLY",
                "big_setup_reason": _reason(score, missing, tags),
                "big_setup_tags": tags,
                "big_setup_missing_data": missing,
                "big_setup_direction_bias": direction,
                "big_setup_is_premium": _premium(payload),
                "big_setup_is_discount": _discount(payload),
                "big_setup_risk_ok": _risk_ok(payload),
                "big_setup_time_ok": _time_ok(payload),
                "big_setup_safety_ok": _safety_ok(payload),
                "htf_alignment_score": htf_alignment_score,
                "poi_score": poi_score,
                "liquidity_score": liquidity_score,
                "fvg_ob_ote_score": fvg_ob_ote_score,
                "confirmation_score": confirmation_score,
                "risk_score": risk_score,
                "time_score": time_score,
            }
        except Exception as exc:
            return {
                "big_setup_score": 0,
                "big_setup_grade": "UNKNOWN",
                "big_setup_status": "TAG_ONLY",
                "big_setup_reason": f"BIG_SETUP_ERROR:{exc}",
                "big_setup_tags": [],
                "big_setup_missing_data": ["ERROR"],
                "big_setup_direction_bias": "UNKNOWN",
                "big_setup_is_premium": None,
                "big_setup_is_discount": None,
                "big_setup_risk_ok": False,
                "big_setup_time_ok": False,
                "big_setup_safety_ok": False,
            }


def observer_report_sections(samples: list[dict]) -> dict:
    closed = [sample for sample in samples if sample.get("sample_type") == "PAPER_CLOSE" and _float(sample.get("pnl")) is not None]
    grades = ["A_PLUS", "A", "B", "C", "UNKNOWN"]
    return {
        "big_setup_summary": {
            "trades_by_grade": {grade: len([sample for sample in closed if _grade_of(sample) == grade]) for grade in grades},
            "pnl_by_grade": {grade: round(sum(_float(sample.get("pnl")) or 0.0 for sample in closed if _grade_of(sample) == grade), 6) for grade in grades},
            "win_rate_by_grade": {grade: _win_rate([sample for sample in closed if _grade_of(sample) == grade]) for grade in grades},
            "strategy_stats_by_grade": _entity_by_grade(closed, "strategy"),
            "symbol_stats_by_grade": _entity_by_grade(closed, "symbol"),
            "top_big_setup_tags": Counter(tag for sample in samples for tag in _list(sample.get("big_setup_tags"))).most_common(10),
            "biggest_wins_losses_with_grade": _wins_losses(closed),
        },
        "confidence_calibration": _confidence_calibration(closed),
    }


def _top_down_score(payload: dict, direction: str) -> int:
    h4 = str(payload.get("smc_h4_direction") or payload.get("h4_bias") or "").upper()
    h1 = str(payload.get("smc_h1_trend") or payload.get("h1_bias") or "").upper()
    if direction == "BUY" and h4 == "BULLISH" and h1 == "BULLISH":
        return 20
    if direction == "SELL" and h4 == "BEARISH" and h1 == "BEARISH":
        return 20
    if h4 in {"BULLISH", "BEARISH"} or h1 in {"BULLISH", "BEARISH"}:
        return 10
    return 0


def _poi_score(payload: dict) -> int:
    score = 0
    if payload.get("smc_h4_key_level_nearby") or payload.get("support_resistance_nearby"):
        score += 7
    if str(payload.get("smc_h4_supply_demand_zone") or payload.get("h4_zone") or "").upper() in {"SUPPLY", "DEMAND", "SUPPORT", "RESISTANCE"}:
        score += 8
    return min(15, score)


def _liquidity_score(payload: dict) -> int:
    if payload.get("turtle_soup_ote"):
        return 15
    if str(payload.get("smc_h1_liquidity") or "").upper() in {"BUY_SIDE", "SELL_SIDE", "EQUAL_HIGHS", "EQUAL_LOWS"}:
        return 12
    if payload.get("m15_liquidity"):
        return 8
    return 0


def _fvg_ob_ote_score(payload: dict) -> int:
    score = 0
    if str(payload.get("smc_h1_fvg") or "").upper() != "NONE" or payload.get("ifvg_ote_sniper") or payload.get("breaker_fvg_ote"):
        score += 6
    if str(payload.get("smc_h1_order_block") or "").upper() != "NONE" or payload.get("amd_bpr_ote"):
        score += 5
    if str(payload.get("fibonacci_context") or "").upper() in {"PREMIUM", "DISCOUNT"}:
        score += 4
    return min(15, score)


def _confirmation_score(payload: dict) -> int:
    score = 0
    if payload.get("smc_m15_confirmation") or payload.get("m15_confirmation"):
        score += 6
    if payload.get("smc_m5_confirmation") or payload.get("m5_cisd"):
        score += 5
    if payload.get("smc_m1_entry_confirmation") or payload.get("m1_entry_confirmation"):
        score += 4
    return min(15, score)


def _risk_score(payload: dict) -> int:
    score = 0
    if (_float(payload.get("risk_reward") or payload.get("reward_risk")) or 0.0) >= 1.5:
        score += 5
    if _risk_ok(payload):
        score += 5
    return score


def _time_score(payload: dict) -> int:
    score = 0
    if _time_ok(payload):
        score += 5
    if _safety_ok(payload):
        score += 5
    return score


def _tags(payload: dict, direction: str) -> list[str]:
    tags = []
    if _top_down_score(payload, direction) >= 15 and _poi_score(payload) >= 8 and _confirmation_score(payload) >= 6:
        tags.append("TOP_DOWN_HTF_POI_LTF_CONFIRMATION")
    if payload.get("turtle_soup_ote"):
        tags.extend(["CRT_TBS_COLORED_RANGE", "HVB_TURTLE_SOUP_REVERSAL"])
    if payload.get("ifvg_ote_sniper") or payload.get("amd_bpr_ote"):
        tags.append("AMD_FVG_IFVG")
    if str(payload.get("smc_h1_liquidity") or "").upper() in {"BUY_SIDE", "EQUAL_HIGHS"} and str(payload.get("smc_h1_fvg") or "").upper() == "BEARISH_FVG":
        tags.append("AMD_BUYER_TRAP_FVG_DISTRIBUTION")
    if str(payload.get("fibonacci_context") or "").upper() in {"PREMIUM", "DISCOUNT"}:
        tags.extend(["FIB_OTE_RETRACEMENT", "MSNR_OTE_FIB_SETUP"])
    if str(payload.get("trendline_channel_context") or "").upper() not in {"", "NONE"}:
        tags.append("HTF_CHANNEL_LIQUIDITY_CONTEXT")
    if _fvg_ob_ote_score(payload) >= 10 and _confirmation_score(payload) >= 8:
        tags.append("ORDERFLOW_SMC_CONFIRMATION")
    if payload.get("ema50_200_stoch_confirmation"):
        tags.append("SNIPER_MOMENTUM_CONFIRMATION")
    if payload.get("support_resistance_nearby"):
        tags.append("JACKSON_ZONE_CONTEXT")
    if (_float(payload.get("spread")) or 999999.0) <= 10:
        tags.append("VCE_COIL_EDGE")
    if payload.get("breaker_fvg_ote"):
        tags.append("JOHN_WICK_RANGE_REVERSAL")
    return list(dict.fromkeys(tags))


def _missing(payload: dict) -> list[str]:
    required = ["smc_confluence_score", "smc_h4_direction", "smc_h1_trend", "risk_reward", "safety_guard_status"]
    return [field for field in required if payload.get(field) in {None, "", "UNKNOWN"}]


def _direction_bias(payload: dict) -> str:
    direction = str(payload.get("direction") or payload.get("signal") or "").upper()
    if direction in {"BUY", "SELL", "WAIT"}:
        return direction
    h4 = str(payload.get("smc_h4_direction") or payload.get("h4_bias") or "").upper()
    if h4 == "BULLISH":
        return "BUY"
    if h4 == "BEARISH":
        return "SELL"
    return "UNKNOWN"


def _premium(payload: dict) -> bool | None:
    context = str(payload.get("fibonacci_context") or "").upper()
    if context == "PREMIUM":
        return True
    if context == "DISCOUNT":
        return False
    return None


def _discount(payload: dict) -> bool | None:
    context = str(payload.get("fibonacci_context") or "").upper()
    if context == "DISCOUNT":
        return True
    if context == "PREMIUM":
        return False
    return None


def _risk_ok(payload: dict) -> bool:
    return str(payload.get("risk_diag_status") or "OK").upper() == "OK" and str(payload.get("risk_status") or "APPROVED").upper() == "APPROVED"


def _time_ok(payload: dict) -> bool:
    return str(payload.get("safety_guard_status") or "PASS").upper() in {"PASS", "CAUTION"}


def _safety_ok(payload: dict) -> bool:
    return str(payload.get("safety_guard_status") or "PASS").upper() == "PASS"


def _grade(score: int) -> str:
    if score >= 85:
        return "A_PLUS"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    return "C"


def _reason(score: int, missing: list[str], tags: list[str]) -> str:
    if missing:
        return "MISSING_DATA:" + ",".join(missing)
    if score >= 75:
        return "HIGH_QUALITY_SETUP:" + ",".join(tags[:3])
    return "LOW_OR_MODERATE_CONFLUENCE"


def _grade_of(sample: dict) -> str:
    return str(sample.get("big_setup_grade") or "UNKNOWN")


def _entity_by_grade(samples: list[dict], field: str) -> dict:
    out: dict[str, dict[str, dict]] = {}
    for sample in samples:
        name = str(sample.get(field) or "UNKNOWN")
        grade = _grade_of(sample)
        out.setdefault(name, {}).setdefault(grade, {"trades": 0, "pnl": 0.0, "wins": 0})
        row = out[name][grade]
        row["trades"] += 1
        row["pnl"] = round(row["pnl"] + (_float(sample.get("pnl")) or 0.0), 6)
        if str(sample.get("result") or "").upper() == "WIN":
            row["wins"] += 1
    return out


def _confidence_calibration(samples: list[dict]) -> list[dict]:
    buckets = [(0, 0.5), (0.5, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]
    out = []
    for low, high in buckets:
        group = [sample for sample in samples if low <= (_confidence(sample.get("confidence")) or 0.0) < high]
        avg = round(sum(_confidence(sample.get("confidence")) or 0.0 for sample in group) / len(group), 4) if group else None
        win_rate = _win_rate(group)
        out.append(
            {
                "confidence_bucket": f"{low:.2f}-{high:.2f}",
                "trades": len(group),
                "win_rate": win_rate,
                "pnl": round(sum(_float(sample.get("pnl")) or 0.0 for sample in group), 6),
                "average_confidence": avg,
                "confidence_win_rate_mismatch": round((avg or 0.0) - (win_rate or 0.0), 4) if group else None,
            }
        )
    return out


def _wins_losses(samples: list[dict]) -> dict:
    ordered = sorted(samples, key=lambda sample: _float(sample.get("pnl")) or 0.0)
    return {
        "biggest_losses": [{"symbol": s.get("symbol"), "strategy": s.get("strategy"), "pnl": s.get("pnl"), "grade": _grade_of(s)} for s in ordered[:5]],
        "biggest_wins": [{"symbol": s.get("symbol"), "strategy": s.get("strategy"), "pnl": s.get("pnl"), "grade": _grade_of(s)} for s in reversed(ordered[-5:])],
    }


def _win_rate(samples: list[dict]) -> float | None:
    if not samples:
        return None
    wins = sum(1 for sample in samples if str(sample.get("result") or "").upper() == "WIN")
    return round(wins / len(samples), 4)


def _confidence(value: object) -> float | None:
    out = _float(value)
    if out is None:
        return None
    return out / 100.0 if out > 1 else out


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value: object) -> list:
    return value if isinstance(value, list) else []
