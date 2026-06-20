from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.mt5.demo_router import EVENTS_PATH, build_demo_report


def build_strategy_edge_report(symbols: list[str], hours: int, output: Path, events_path: Path | None = None) -> dict:
    settings = get_settings()
    report = build_demo_report(settings, events_path or EVENTS_PATH, hours=hours, now=_latest_event_clock(events_path))
    edge_ready = list(report.get("edge_ready_candidates") or [])
    near_misses = list(report.get("near_miss_candidates") or [])
    current = edge_ready + near_misses
    if symbols:
        normalized = {_normalize(symbol) for symbol in symbols}
        current = [event for event in current if _normalize(event.get("symbol") or event.get("broker_symbol")) in normalized]
        edge_ready = [event for event in edge_ready if _normalize(event.get("symbol") or event.get("broker_symbol")) in normalized]
        near_misses = [event for event in near_misses if _normalize(event.get("symbol") or event.get("broker_symbol")) in normalized]
    blocked_by_strategy = Counter(str(event.get("strategy") or "UNKNOWN") for event in current)
    blocked_by_symbol = Counter(str(event.get("symbol") or event.get("broker_symbol") or "UNKNOWN") for event in current)
    blocked_by_session = Counter(_event_session(event) for event in current)
    blocked = report.get("current_window_skip_reasons") or {}
    out = {
        "symbols": symbols,
        "hours": hours,
        "candidates_seen": len(current),
        "edge_ready_candidates": len(edge_ready),
        "near_miss_candidates": len(near_misses),
        "demo_orders": report.get("demo_trades_opened", 0),
        "blocked_by_reason": blocked,
        "blocked_by_session": dict(blocked_by_session),
        "blocked_by_symbol": dict(blocked_by_symbol),
        "blocked_by_strategy": dict(blocked_by_strategy),
        "best_near_miss": max(near_misses, key=lambda item: item.get("setup_hunter_score") or item.get("rr") or 0, default=None),
        "best_edge_strategy": _top_key(dict(blocked_by_strategy)),
        "best_symbol": _top_key(dict(blocked_by_symbol)),
        "latest_good_setup": report.get("latest_decision"),
        "latest_good_rr_but_blocked_reason": report.get("latest_good_rr_but_blocked_reason"),
        "sessions_with_most_valid_setups": sorted(blocked_by_session.items(), key=lambda item: item[1], reverse=True)[:5],
        "strategies_producing_too_many_false_candidates": _false_candidate_strategies(edge_ready, blocked_by_strategy),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "strategy_edge_report.json").write_text(json.dumps(out, indent=2, sort_keys=False), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTCUSD#,GOLD#,EURUSD")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("backtests/edge_report_latest"))
    args = parser.parse_args()
    symbols = [item.strip() for item in str(args.symbols or "").split(",") if item.strip()]
    report = build_strategy_edge_report(symbols, args.hours, args.output)
    print(json.dumps(report, indent=2, sort_keys=False))


def _top_key(payload: dict) -> str | None:
    if not payload:
        return None
    return max(payload, key=payload.get)


def _false_candidate_strategies(edge_ready: list[dict], blocked: Counter) -> dict:
    ready = Counter(str(event.get("strategy") or "UNKNOWN") for event in edge_ready)
    return {
        strategy: count
        for strategy, count in blocked.items()
        if count >= 5 and ready.get(strategy, 0) == 0
    }


def _normalize(symbol: object) -> str:
    return str(symbol or "").upper().replace("#", "")


def _event_session(event: dict) -> str:
    time_gate = event.get("time_gate") if isinstance(event.get("time_gate"), dict) else {}
    return str(time_gate.get("session_name") or "UNKNOWN")


def _latest_event_clock(events_path: Path | None):
    if events_path is None or not events_path.exists():
        return None
    latest = None
    for line in events_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        created_at = str(event.get("created_at") or "")
        if created_at.endswith("Z"):
            created_at = created_at[:-1] + "+00:00"
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            continue
        if latest is None or created > latest:
            latest = created
    return latest + timedelta(minutes=1) if latest else None


if __name__ == "__main__":
    main()
