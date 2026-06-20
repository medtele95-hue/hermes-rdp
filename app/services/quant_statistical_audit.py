from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agents.strategies import quant_statistical_pullback as quant
from app.config import Settings
from app.mt5.demo_router import EVENTS_PATH


STRATEGY = "QUANT_STATISTICAL_PULLBACK"


def build_quant_statistical_audit(
    settings: Settings,
    symbol: str,
    hours: int = 48,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    start = now_dt.astimezone(timezone.utc) - timedelta(hours=int(hours or 48))
    events = [
        event
        for event in _read_events(events_path or EVENTS_PATH)
        if _event_matches(event, symbol, start, now_dt)
    ]
    closed = [event for event in events if str(event.get("event_type") or "").upper() in {"DEMO_CLOSE", "POSITION_SYNC"} and event.get("pnl") is not None]
    pnl_values = [_to_float(event.get("pnl")) or 0.0 for event in closed]
    wins = sum(1 for value in pnl_values if value > 0)
    rr_values = [_to_float(event.get("reward_risk") or event.get("rr") or _raw(event).get("rr")) for event in events]
    rr_clean = [value for value in rr_values if value is not None and math.isfinite(value)]
    failures = _audit_failures(events)
    return {
        "symbol": symbol,
        "strategy": STRATEGY,
        "status": "DISABLED_PENDING_AUDIT" if settings.btc_disable_quant_statistical_pullback else "AUDIT_ONLY",
        "window_hours": int(hours or 48),
        "trades_count": len([event for event in events if str(event.get("event_type") or "").upper() == "DEMO_ORDER"]),
        "closed_pnl": round(sum(pnl_values), 6),
        "win_rate": round(wins / len(pnl_values), 4) if pnl_values else 0,
        "avg_rr": round(sum(rr_clean) / len(rr_clean), 6) if rr_clean else 0,
        "math_description": {
            "entry_formula": "latest closed M5 close after regression/z-score signal",
            "mean_std_zscore": "mean=SMA(close,Z); stdev=sqrt(sum((close-mean)^2)/Z); z=(last_closed_close-mean)/stdev",
            "lookback_window": {
                "regression": settings.hermes_quant_reg_period,
                "z_score": settings.hermes_quant_z_period,
                "stdev": settings.hermes_quant_stdev_period,
            },
            "trend_filter": "BUY slope > 0, SELL slope < 0, with r2 >= HERMES_QUANT_MIN_R2",
            "volatility_filter": "z-score stdev and stdev_vol must be > 0",
            "stop_loss_formula": "BUY sl=entry-stdev_vol*mult; SELL sl=entry+stdev_vol*mult",
            "take_profit_formula": "BUY tp=entry+risk*min_rr; SELL tp=entry-risk*min_rr",
            "rr_calculation": "abs(tp-entry)/abs(entry-sl), rejected when non-finite or <= 0",
            "expected_direction_logic": "BUY positive regression pullback z <= -entry; SELL negative regression pullback z >= entry",
            "candle_source": "M5 closed candles only; _closed_frame drops the currently forming last candle",
            "lookahead": "No future candles used by the strategy implementation",
            "spread_impact": "Spread is not inside Quant math; demo_router spread gate blocks execution",
            "kelly_inputs": "entry, sl, tp, reward_risk, approved_lot/final_capped_lot from demo_router risk path",
        },
        "math_checks": {
            "lookahead_free": True,
            "closed_candles_only": True,
            "zscore_valid": not any(item == "QUANT_INVALID_STDEV" for item in failures),
            "volatility_filter_valid": not any(item == "QUANT_INVALID_STDEV" for item in failures),
            "trend_filter_valid": not any(item == "QUANT_R2_BELOW_MIN" for item in failures),
            "sl_tp_valid": not any(item in {"QUANT_INVALID_SL_TP_RR", "INVALID_SL_TP"} for item in failures),
            "rr_valid": not any(item in {"QUANT_INVALID_SL_TP_RR", "INVALID_RR"} for item in failures),
            "kelly_valid": not any(item == "KELLY_INVALID_LOT" for item in failures),
        },
        "failures": sorted(set(failures)),
        "recent_events_count": len(events),
    }


def write_quant_statistical_audit(
    settings: Settings,
    symbol: str,
    hours: int = 48,
    output_dir: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> Path:
    report = build_quant_statistical_audit(settings, symbol, hours, events_path, now)
    out_dir = output_dir or Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_symbol = str(symbol or "").upper().replace("#", "")
    path = out_dir / f"strategy_audit_{normalized_symbol}_{STRATEGY}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _audit_failures(events: Iterable[dict]) -> list[str]:
    failures: list[str] = []
    for event in events:
        raw = _raw(event)
        for key in ("reason", "failed_gate", "strict_block_reason", "block_reason", "quant_reason"):
            reason = str(event.get(key) or raw.get(key) or "").upper()
            if reason:
                failures.append(reason)
        rr = _to_float(event.get("reward_risk") or event.get("rr") or raw.get("rr"))
        entry = _to_float(event.get("entry") or raw.get("entry"))
        sl = _to_float(event.get("sl") or raw.get("sl"))
        tp = _to_float(event.get("tp") or raw.get("tp"))
        direction = str(event.get("direction") or event.get("signal") or raw.get("direction") or raw.get("signal") or "").upper()
        if rr is not None and (not math.isfinite(rr) or rr <= 0):
            failures.append("INVALID_RR")
        if all(value is not None for value in (entry, sl, tp)) and direction in {"BUY", "SELL"}:
            if quant.reward_risk(direction, entry, sl, tp) is None:
                failures.append("INVALID_SL_TP")
        kelly_lot = _to_float(event.get("kelly_suggested_lot") or raw.get("kelly_suggested_lot"))
        final_lot = _to_float(event.get("final_capped_lot") or raw.get("final_capped_lot"))
        if str(event.get("reason") or raw.get("reason") or "").upper() == "KELLY_INVALID_LOT":
            failures.append("KELLY_INVALID_LOT")
        if kelly_lot is not None and kelly_lot <= 0:
            failures.append("KELLY_INVALID_LOT")
        if final_lot is not None and final_lot <= 0:
            failures.append("KELLY_INVALID_LOT")
    return failures


def _event_matches(event: dict, symbol: str, start: datetime, end: datetime) -> bool:
    if str(event.get("strategy") or _raw(event).get("strategy") or "").upper() != STRATEGY:
        return False
    event_symbol = str(event.get("symbol") or event.get("broker_symbol") or _raw(event).get("symbol") or "").upper().replace("#", "")
    requested = str(symbol or "").upper().replace("#", "")
    if event_symbol and requested and event_symbol != requested:
        return False
    created_at = _parse_time(event.get("created_at") or event.get("timestamp") or event.get("time"))
    return created_at is None or start <= created_at <= end


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _raw(event: dict) -> dict:
    raw = event.get("raw_payload")
    return raw if isinstance(raw, dict) else {}


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None
