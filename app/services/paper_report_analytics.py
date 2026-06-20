from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agents.big_setup_detector import observer_report_sections
from app.config import Settings


STRATEGIES = ["EMA_PULLBACK", "BREAKOUT_RETEST", "SECOND_ENTRY", "SCALPING_AGENT"]
SYMBOLS = ["BTCUSD", "GOLD#", "EURUSD"]
SESSIONS = ["ASIA", "LONDON", "NEW_YORK", "OVERLAP", "OFF_SESSION"]


def build_time_stats(samples: list[dict], settings: Settings) -> dict:
    trades = [_normalize_trade(sample) for sample in samples if _is_closed_paper_trade(sample)]
    trades = [trade for trade in trades if trade is not None]
    local_tz = _zone(settings.report_timezone)
    session_windows = _session_windows(settings)

    hourly_utc_groups: dict[int, list[dict]] = defaultdict(list)
    hourly_local_groups: dict[int, list[dict]] = defaultdict(list)
    session_groups: dict[str, list[dict]] = {session: [] for session in SESSIONS}
    strategy_groups: dict[str, list[dict]] = {strategy: [] for strategy in STRATEGIES}
    symbol_groups: dict[str, list[dict]] = {symbol: [] for symbol in SYMBOLS}

    for trade in trades:
        utc_dt = trade["dt_utc"]
        local_dt = utc_dt.astimezone(local_tz)
        trade["hour_utc"] = utc_dt.hour
        trade["hour_local"] = local_dt.hour
        trade["session"] = _session_for(local_dt.time(), session_windows)
        hourly_utc_groups[trade["hour_utc"]].append(trade)
        hourly_local_groups[trade["hour_local"]].append(trade)
        session_groups[trade["session"]].append(trade)
        if trade["strategy"] in strategy_groups:
            strategy_groups[trade["strategy"]].append(trade)
        if trade["symbol"] in symbol_groups:
            symbol_groups[trade["symbol"]].append(trade)

    hourly_stats_utc = [
        {"hour_utc": f"{hour:02d}:00", **_performance(hourly_utc_groups.get(hour, []), include_best=True)}
        for hour in range(24)
    ]
    hourly_stats_local = [
        {"hour_local": f"{hour:02d}:00", **_performance(hourly_local_groups.get(hour, []), include_best=False)}
        for hour in range(24)
    ]
    session_stats = [
        {"session": session, **_performance(session_groups.get(session, []), include_best=True, include_profit_factor=True)}
        for session in SESSIONS
    ]

    return {
        "timezone": settings.report_timezone,
        "hourly_stats_utc": hourly_stats_utc,
        "hourly_stats_local": hourly_stats_local,
        "session_stats": session_stats,
        "strategy_time_stats": _entity_time_stats(strategy_groups, "strategy"),
        "symbol_time_stats": _entity_time_stats(symbol_groups, "symbol"),
        "time_recommendations": _recommendations(hourly_stats_local, session_stats),
    }


def clean_report_samples(samples: list[dict], settings: Settings) -> tuple[list[dict], dict]:
    clean_start = _parse_dt(settings.report_clean_start_at)
    clean = []
    excluded_before = 0
    excluded_recovery = 0
    excluded_missed = 0
    for sample in samples:
        if not _is_closed_paper_trade(sample):
            clean.append(sample)
            continue
        raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
        recovery_reason = str(
            sample.get("recovery_reason")
            or raw_payload.get("recovery_reason")
            or sample.get("reason")
            or sample.get("close_reason")
            or ""
        ).upper()
        recovery_close = bool(sample.get("recovery_close") or raw_payload.get("recovery_close") or recovery_reason.startswith("RECOVERY_"))
        missed_exit = bool(sample.get("missed_exit") or raw_payload.get("missed_exit"))
        opened_at = _parse_dt(sample.get("opened_at")) or _parse_dt(sample.get("created_at"))
        before_clean_start = clean_start is not None and opened_at is not None and opened_at < clean_start

        if before_clean_start:
            excluded_before += 1
        if recovery_close:
            excluded_recovery += 1
        if missed_exit:
            excluded_missed += 1
        if before_clean_start or recovery_close or missed_exit:
            continue
        clean.append(sample)
    return clean, {
        "excluded_before_clean_start_count": excluded_before,
        "excluded_recovery_trades_count": excluded_recovery,
        "excluded_missed_exit_count": excluded_missed,
    }


def manual_adjusted_report(samples: list[dict], settings: Settings) -> dict:
    excluded_ids = _excluded_trade_ids(settings)
    adjusted = []
    excluded = []
    for sample in samples:
        if _is_closed_paper_trade(sample) and _sample_ids(sample) & excluded_ids:
            excluded.append(sample)
            continue
        adjusted.append(sample)

    raw_closed = [sample for sample in samples if _is_closed_paper_trade(sample)]
    adjusted_closed = [sample for sample in adjusted if _is_closed_paper_trade(sample)]
    excluded_pnl = round(sum(_float(sample.get("pnl")) or 0.0 for sample in excluded), 6)
    return {
        "excluded_ids": sorted(excluded_ids),
        "adjusted_samples": adjusted,
        "excluded_outlier_summary": [_outlier_row(sample) for sample in excluded],
        "biggest_losses": [_outlier_row(sample) for sample in _biggest_losses(raw_closed)],
        "adjusted_summary": {
            **_prefixed_summary(raw_closed, "raw"),
            **_prefixed_summary(adjusted_closed, "adjusted"),
            "excluded_manual_count": len(excluded),
            "excluded_manual_pnl": excluded_pnl,
        },
        "strategy_adjusted_stats": _entity_adjusted_stats(raw_closed, adjusted_closed, excluded, "strategy", STRATEGIES),
        "symbol_adjusted_stats": _entity_adjusted_stats(raw_closed, adjusted_closed, excluded, "symbol", SYMBOLS),
    }


def build_observer_summaries(samples: list[dict], adjusted_samples: list[dict] | None = None) -> dict:
    adjusted_samples = adjusted_samples if adjusted_samples is not None else samples
    smc_samples = [sample for sample in samples if _smc_status(sample) is not None]
    out = {
        "smc_tagged_samples_count": len(smc_samples),
        "smc_pass_count": sum(1 for sample in smc_samples if _smc_status(sample) == "PASS"),
        "smc_fail_count": sum(1 for sample in smc_samples if _smc_status(sample) == "FAIL"),
        "smc_top_pass_setups": _smc_top_pass_setups(smc_samples),
        "smc_performance_raw": _smc_performance(samples),
        "smc_performance_adjusted": _smc_performance(adjusted_samples),
        "risk_diag_mismatch_count": sum(1 for sample in samples if _risk_diag_status(sample) == "MISMATCH"),
        "risk_diag_top_mismatches": _risk_diag_top_mismatches(samples),
    }
    out.update(observer_report_sections(adjusted_samples))
    out["safety_guard_summary"] = _safety_guard_summary(samples)
    out["strategy_replacement_summary"] = _strategy_replacement_summary(samples)
    out["new_strategy_performance"] = _new_strategy_performance(samples)
    return out


def format_adjusted_report_tables(report: dict) -> str:
    summary = report.get("adjusted_summary", {})
    lines = [
        "RAW PERFORMANCE",
        f"Trades  PnL  WinRate",
        f"{summary.get('raw_trades_closed', 0):>6}  {_signed(summary.get('raw_pnl')):>6}  {_pct(summary.get('raw_win_rate')):>7}",
        "",
        "ADJUSTED PERFORMANCE",
        "Trades  PnL  WinRate  ProfitFactor  Expectancy",
        (
            f"{summary.get('adjusted_trades_closed', 0):>6}  {_signed(summary.get('adjusted_pnl')):>6}  "
            f"{_pct(summary.get('adjusted_win_rate')):>7}  {_num(summary.get('adjusted_profit_factor')):>12}  "
            f"{_num(summary.get('adjusted_expectancy'))}"
        ),
        "",
        "EXCLUDED OUTLIERS",
        "Id  Symbol  Strategy  PnL  Reason  OpenedAt",
    ]
    excluded = report.get("excluded_outlier_summary") or []
    if excluded:
        for row in excluded:
            lines.append(
                f"{row.get('id') or '-'}  {row.get('symbol') or '-'}  {row.get('strategy') or '-'}  "
                f"{_signed(row.get('pnl'))}  {row.get('reason') or '-'}  {row.get('opened_at') or row.get('created_at') or '-'}"
            )
    else:
        lines.append("-")
    lines.extend(["", "BIGGEST LOSSES", "Id  PaperTradeId  Symbol  Strategy  PnL  Reason  OpenedAt"])
    for row in report.get("biggest_losses") or []:
        lines.append(
            f"{row.get('id') or '-'}  {row.get('paper_trade_id') or '-'}  {row.get('symbol') or '-'}  "
            f"{row.get('strategy') or '-'}  {_signed(row.get('pnl'))}  {row.get('reason') or '-'}  "
            f"{row.get('opened_at') or row.get('created_at') or '-'}"
        )
    if not report.get("biggest_losses"):
        lines.append("-")
    return "\n".join(lines)


def format_time_stats_tables(time_stats: dict) -> str:
    lines = ["TIME PERFORMANCE", "Hour  Trades  WinRate  PnL  BestStrategy  WorstStrategy"]
    for row in time_stats.get("hourly_stats_local", []):
        if int(row.get("trades") or 0) <= 0:
            continue
        lines.append(
            f"{row['hour_local']:>5}  {row['trades']:>6}  {_pct(row.get('win_rate')):>7}  {_signed(row.get('pnl')):>6}  "
            f"{row.get('best_strategy') or '-':<13}  {row.get('worst_strategy') or '-'}"
        )
    lines.extend(["", "SESSION PERFORMANCE", "Session  Trades  WinRate  PnL  ProfitFactor"])
    for row in time_stats.get("session_stats", []):
        if int(row.get("trades") or 0) <= 0:
            continue
        lines.append(
            f"{row['session']:<11}  {row['trades']:>6}  {_pct(row.get('win_rate')):>7}  {_signed(row.get('pnl')):>6}  "
            f"{_num(row.get('profit_factor'))}"
        )
    return "\n".join(lines)


def _entity_time_stats(groups: dict[str, list[dict]], entity_field: str) -> dict:
    out = {}
    for name, trades in groups.items():
        hourly = [
            {"hour_local": f"{hour:02d}:00", **_performance([trade for trade in trades if trade.get("hour_local") == hour])}
            for hour in range(24)
        ]
        session = [
            {"session": session_name, **_performance([trade for trade in trades if trade.get("session") == session_name], include_profit_factor=True)}
            for session_name in SESSIONS
        ]
        out[name] = {
            "hourly_performance": hourly,
            "session_performance": session,
            "best_hour": _best_period(hourly, "hour_local", highest=True),
            "worst_hour": _best_period(hourly, "hour_local", highest=False),
            "best_session": _best_period(session, "session", highest=True),
            "worst_session": _best_period(session, "session", highest=False),
        }
    return out


def _performance(trades: list[dict], include_best: bool = False, include_profit_factor: bool = False) -> dict:
    wins = [trade for trade in trades if trade["result"] == "WIN"]
    losses = [trade for trade in trades if trade["result"] == "LOSS"]
    pnl = round(sum(trade["pnl"] for trade in trades), 6)
    gross_profit = sum(trade["pnl"] for trade in trades if trade["pnl"] > 0)
    gross_loss = abs(sum(trade["pnl"] for trade in trades if trade["pnl"] < 0))
    out = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": pnl,
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_pnl": round(pnl / len(trades), 6) if trades else 0,
        "confidence": "LOW_SAMPLE" if len(trades) < 5 else "NORMAL",
    }
    if include_profit_factor:
        out["profit_factor"] = round(gross_profit / gross_loss, 6) if gross_loss > 0 else None
    if include_best:
        out.update(
            {
                "best_strategy": _best_key(trades, "strategy", True),
                "worst_strategy": _best_key(trades, "strategy", False),
                "best_symbol": _best_key(trades, "symbol", True),
                "worst_symbol": _best_key(trades, "symbol", False),
            }
        )
    return out


def _recommendations(hourly_rows: list[dict], session_rows: list[dict]) -> dict:
    reliable_hours = [row for row in hourly_rows if row["trades"] >= 5]
    reliable_sessions = [row for row in session_rows if row["trades"] >= 5]
    best_hours = [row["hour_local"] for row in sorted(reliable_hours, key=lambda item: item["pnl"], reverse=True)[:3] if row_positive(row)]
    avoid_hours = [row["hour_local"] for row in sorted(reliable_hours, key=lambda item: item["pnl"])[:3] if row_negative(row)]
    best_sessions = [row["session"] for row in sorted(reliable_sessions, key=lambda item: item["pnl"], reverse=True)[:2] if row_positive(row)]
    worst_sessions = [row["session"] for row in sorted(reliable_sessions, key=lambda item: item["pnl"])[:2] if row_negative(row)]
    notes = []
    if not reliable_hours:
        notes.append("No hour has at least 5 closed PAPER trades; treat hourly conclusions as LOW_SAMPLE.")
    if not reliable_sessions:
        notes.append("No session has at least 5 closed PAPER trades; treat session conclusions as LOW_SAMPLE.")
    notes.append("Report-only analytics; no trading blocks or strategy changes are applied.")
    return {
        "best_hours_to_trade": best_hours,
        "hours_to_avoid": avoid_hours,
        "best_sessions": best_sessions,
        "worst_sessions": worst_sessions,
        "notes": notes,
    }


def row_positive(row: dict) -> bool:
    return (row.get("pnl") or 0) > 0


def row_negative(row: dict) -> bool:
    return (row.get("pnl") or 0) < 0


def _best_period(rows: list[dict], field: str, highest: bool) -> str | None:
    reliable = [row for row in rows if row.get("trades", 0) >= 5]
    if not reliable:
        return None
    chooser = max if highest else min
    return chooser(reliable, key=lambda item: item.get("pnl", 0)).get(field)


def _best_key(trades: list[dict], key: str, highest: bool) -> str | None:
    totals: dict[str, float] = {}
    for trade in trades:
        name = str(trade.get(key) or "")
        if not name:
            continue
        totals[name] = totals.get(name, 0.0) + trade["pnl"]
    if not totals:
        return None
    return (max if highest else min)(totals, key=totals.get)


def _normalize_trade(sample: dict) -> dict | None:
    dt = _trade_datetime(sample)
    pnl = _float(sample.get("pnl"))
    if dt is None or pnl is None:
        return None
    return {
        "dt_utc": dt,
        "strategy": str(sample.get("strategy") or "UNKNOWN"),
        "symbol": str(sample.get("symbol") or "UNKNOWN"),
        "result": str(sample.get("result") or "").upper(),
        "pnl": pnl,
    }


def _trade_datetime(sample: dict) -> datetime | None:
    for field in ("closed_at", "opened_at", "date_time", "created_at"):
        parsed = _parse_dt(sample.get(field))
        if parsed is not None:
            return parsed
    return None


def _is_closed_paper_trade(sample: dict) -> bool:
    if sample.get("sample_type") != "PAPER_CLOSE":
        return False
    if _float(sample.get("pnl")) is None:
        return False
    return str(sample.get("result") or "").upper() in {"WIN", "LOSS"}


def _excluded_trade_ids(settings: Settings) -> set[str]:
    return {item.strip() for item in str(settings.report_excluded_trade_ids or "").split(",") if item.strip()}


def _sample_ids(sample: dict) -> set[str]:
    raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
    return {
        str(value)
        for value in [
            sample.get("id"),
            sample.get("paper_trade_id"),
            raw_payload.get("id"),
            raw_payload.get("paper_trade_id"),
        ]
        if value not in {None, ""}
    }


def _prefixed_summary(samples: list[dict], prefix: str) -> dict:
    pnls = [_float(sample.get("pnl")) or 0.0 for sample in samples]
    wins = [sample for sample in samples if str(sample.get("result") or "").upper() == "WIN"]
    losses = [sample for sample in samples if str(sample.get("result") or "").upper() == "LOSS"]
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    total_pnl = round(sum(pnls), 6)
    out = {
        f"{prefix}_trades_closed": len(samples),
        f"{prefix}_pnl": total_pnl,
        f"{prefix}_win_rate": round(len(wins) / len(samples), 4) if samples else None,
    }
    if prefix == "adjusted":
        out.update(
            {
                "adjusted_wins": len(wins),
                "adjusted_losses": len(losses),
                "adjusted_profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
                "adjusted_expectancy": round(total_pnl / len(samples), 6) if samples else 0,
            }
        )
    return out


def _entity_adjusted_stats(raw: list[dict], adjusted: list[dict], excluded: list[dict], field: str, known: list[str]) -> dict:
    names = set(known)
    names.update(str(sample.get(field) or "UNKNOWN") for sample in raw)
    out = {}
    for name in sorted(names):
        raw_group = [sample for sample in raw if str(sample.get(field) or "UNKNOWN") == name]
        adjusted_group = [sample for sample in adjusted if str(sample.get(field) or "UNKNOWN") == name]
        excluded_group = [sample for sample in excluded if str(sample.get(field) or "UNKNOWN") == name]
        out[name] = {
            "raw_pnl": round(sum(_float(sample.get("pnl")) or 0.0 for sample in raw_group), 6),
            "adjusted_pnl": round(sum(_float(sample.get("pnl")) or 0.0 for sample in adjusted_group), 6),
            "raw_win_rate": _win_rate(raw_group),
            "adjusted_win_rate": _win_rate(adjusted_group),
            "excluded_count": len(excluded_group),
        }
    return out


def _win_rate(samples: list[dict]) -> float | None:
    if not samples:
        return None
    wins = sum(1 for sample in samples if str(sample.get("result") or "").upper() == "WIN")
    return round(wins / len(samples), 4)


def _outlier_row(sample: dict) -> dict:
    raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
    return {
        "id": sample.get("id") or raw_payload.get("id") or sample.get("paper_trade_id") or raw_payload.get("paper_trade_id"),
        "paper_trade_id": sample.get("paper_trade_id") or raw_payload.get("paper_trade_id") or sample.get("id") or raw_payload.get("id"),
        "symbol": sample.get("symbol"),
        "strategy": sample.get("strategy"),
        "pnl": _float(sample.get("pnl")) or 0.0,
        "reason": sample.get("reason") or sample.get("close_reason") or sample.get("recovery_reason") or raw_payload.get("recovery_reason"),
        "opened_at": sample.get("opened_at") or raw_payload.get("opened_at"),
        "created_at": sample.get("created_at") or raw_payload.get("created_at"),
    }


def _biggest_losses(samples: list[dict], limit: int = 10) -> list[dict]:
    losses = [sample for sample in samples if (_float(sample.get("pnl")) or 0.0) < 0]
    return sorted(losses, key=lambda sample: _float(sample.get("pnl")) or 0.0)[:limit]


def _smc_status(sample: dict) -> str | None:
    raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
    status = sample.get("smc_confluence_status") or raw_payload.get("smc_confluence_status")
    return str(status).upper() if status not in {None, ""} else None


def _smc_score(sample: dict) -> float:
    raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
    return _float(sample.get("smc_confluence_score")) or _float(raw_payload.get("smc_confluence_score")) or 0.0


def _smc_top_pass_setups(samples: list[dict], limit: int = 10) -> list[dict]:
    passed = [sample for sample in samples if _smc_status(sample) == "PASS"]
    out = []
    for sample in sorted(passed, key=_smc_score, reverse=True)[:limit]:
        raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
        out.append(
            {
                "symbol": sample.get("symbol") or raw_payload.get("symbol"),
                "strategy": sample.get("strategy") or raw_payload.get("strategy"),
                "sample_type": sample.get("sample_type"),
                "direction": sample.get("direction") or sample.get("signal") or raw_payload.get("direction"),
                "smc_confluence_score": _smc_score(sample),
                "smc_confluence_reason": sample.get("smc_confluence_reason") or raw_payload.get("smc_confluence_reason"),
            }
        )
    return out


def _smc_performance(samples: list[dict]) -> dict:
    groups = {"PASS": [], "FAIL": [], "NOT_APPLICABLE": []}
    for sample in samples:
        if not _is_closed_paper_trade(sample):
            continue
        status = _smc_status(sample)
        if status in groups:
            groups[status].append(sample)
    return {status.lower(): _closed_summary(group) for status, group in groups.items()}


def _closed_summary(samples: list[dict]) -> dict:
    pnl = round(sum(_float(sample.get("pnl")) or 0.0 for sample in samples), 6)
    wins = sum(1 for sample in samples if str(sample.get("result") or "").upper() == "WIN")
    losses = sum(1 for sample in samples if str(sample.get("result") or "").upper() == "LOSS")
    return {
        "trades": len(samples),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "win_rate": round(wins / len(samples), 4) if samples else None,
    }


def _risk_diag_status(sample: dict) -> str | None:
    raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
    status = sample.get("risk_diag_status") or raw_payload.get("risk_diag_status")
    return str(status).upper() if status not in {None, ""} else None


def _risk_diag_top_mismatches(samples: list[dict], limit: int = 10) -> list[dict]:
    mismatches = [sample for sample in samples if _risk_diag_status(sample) == "MISMATCH"]
    def magnitude(sample: dict) -> float:
        raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
        return abs(_float(sample.get("risk_diag_mismatch_percent")) or _float(raw_payload.get("risk_diag_mismatch_percent")) or 0.0)

    out = []
    for sample in sorted(mismatches, key=magnitude, reverse=True)[:limit]:
        raw_payload = sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}
        out.append(
            {
                "id": sample.get("id") or sample.get("paper_trade_id") or raw_payload.get("paper_trade_id"),
                "symbol": sample.get("symbol") or raw_payload.get("risk_diag_symbol"),
                "broker_symbol": sample.get("broker_symbol") or raw_payload.get("risk_diag_broker_symbol"),
                "strategy": sample.get("strategy"),
                "expected_risk_percent": sample.get("risk_diag_expected_risk_percent") or raw_payload.get("risk_diag_expected_risk_percent"),
                "realized_risk_percent": sample.get("risk_diag_realized_risk_percent") or raw_payload.get("risk_diag_realized_risk_percent"),
                "mismatch_percent": sample.get("risk_diag_mismatch_percent") or raw_payload.get("risk_diag_mismatch_percent"),
                "pnl": sample.get("pnl"),
            }
        )
    return out


def _safety_guard_summary(samples: list[dict]) -> dict:
    guarded = [sample for sample in samples if sample.get("safety_guard_status") or _raw(sample).get("safety_guard_status")]
    blocked = [sample for sample in guarded if _safety_status(sample) == "BLOCK"]
    caution = [sample for sample in guarded if _safety_status(sample) == "CAUTION"]
    allowed = [sample for sample in guarded if _safety_status(sample) == "PASS"]
    return {
        "blocked_count": len(blocked),
        "caution_count": len(caution),
        "allowed_count": len(allowed),
        "blocked_by_reason": dict(Counter(_safety_reason(sample) for sample in blocked)),
        "blocked_by_symbol": dict(Counter(str(sample.get("symbol") or _raw(sample).get("symbol") or "UNKNOWN") for sample in blocked)),
        "blocked_by_strategy": dict(Counter(str(sample.get("strategy") or _raw(sample).get("strategy") or "UNKNOWN") for sample in blocked)),
        "blocked_by_hour": dict(Counter(str(sample.get("safety_guard_local_hour") or _raw(sample).get("safety_guard_local_hour") or "UNKNOWN") for sample in blocked)),
    }


def _strategy_replacement_summary(samples: list[dict]) -> dict:
    strategies = {str(sample.get("strategy") or _raw(sample).get("strategy") or "UNKNOWN") for sample in samples}
    status_by_strategy: dict[str, Counter] = defaultdict(Counter)
    skips: Counter = Counter()
    for sample in samples:
        strategy = str(sample.get("strategy") or _raw(sample).get("strategy") or "UNKNOWN")
        status = str(sample.get("strategy_status") or _raw(sample).get("strategy_status") or "UNKNOWN")
        status_by_strategy[strategy][status] += 1
        if sample.get("sample_type") == "PAPER_SKIP":
            skips[strategy] += 1
    return {
        "active_strategies": [strategy for strategy in STRATEGIES + ["CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"] if strategy not in {"SECOND_ENTRY", "SCALPING_AGENT"}],
        "legacy_observer_strategies": ["SECOND_ENTRY", "SCALPING_AGENT"],
        "new_strategy_performance": _new_strategy_performance(samples),
        "new_strategy_skips": {strategy: skips[strategy] for strategy in ["CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"]},
        "strategy_status_by_strategy": {strategy: dict(counter) for strategy, counter in status_by_strategy.items() if strategy in strategies},
    }


def _new_strategy_performance(samples: list[dict]) -> dict:
    out = {}
    for strategy in ["CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"]:
        group = [sample for sample in samples if str(sample.get("strategy") or _raw(sample).get("strategy") or "") == strategy]
        closed = [sample for sample in group if _is_closed_paper_trade(sample)]
        wins = [sample for sample in closed if str(sample.get("result") or "").upper() == "WIN"]
        losses = [sample for sample in closed if str(sample.get("result") or "").upper() == "LOSS"]
        gross_profit = sum((_float(sample.get("pnl")) or 0.0) for sample in closed if (_float(sample.get("pnl")) or 0.0) > 0)
        gross_loss = abs(sum((_float(sample.get("pnl")) or 0.0) for sample in closed if (_float(sample.get("pnl")) or 0.0) < 0))
        score_field = {"CRT_TBS_REVERSAL": "crt_tbs_score", "AMD_FVG_IFVG_REVERSAL": "amd_fvg_score", "FIB_OTE_RETEST": "fib_ote_score"}[strategy]
        scores = [_float(sample.get(score_field) or _raw(sample).get(score_field)) for sample in group]
        grades = [str(sample.get("big_setup_grade") or _raw(sample).get("big_setup_grade") or "") for sample in group if sample.get("big_setup_grade") or _raw(sample).get("big_setup_grade")]
        out[strategy] = {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed), 4) if closed else None,
            "pnl": round(sum(_float(sample.get("pnl")) or 0.0 for sample in closed), 6),
            "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss else None,
            "average_score": _avg(scores),
            "average_big_setup_grade": Counter(grades).most_common(1)[0][0] if grades else None,
            "safety_blocks": sum(1 for sample in group if _safety_status(sample) == "BLOCK"),
            "main_skip_reasons": dict(Counter(str(sample.get("reason") or sample.get("reason_for_skip") or "") for sample in group if sample.get("sample_type") == "PAPER_SKIP").most_common(5)),
        }
    return out


def _raw(sample: dict) -> dict:
    return sample.get("raw_payload") if isinstance(sample.get("raw_payload"), dict) else {}


def _safety_status(sample: dict) -> str:
    return str(sample.get("safety_guard_status") or _raw(sample).get("safety_guard_status") or "").upper()


def _safety_reason(sample: dict) -> str:
    return str(sample.get("safety_guard_reason") or _raw(sample).get("safety_guard_reason") or "UNKNOWN")


def _avg(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _session_windows(settings: Settings) -> dict[str, tuple[time, time]]:
    return {
        "OVERLAP": (_parse_time(settings.overlap_session_start), _parse_time(settings.overlap_session_end)),
        "ASIA": (_parse_time(settings.asia_session_start), _parse_time(settings.asia_session_end)),
        "LONDON": (_parse_time(settings.london_session_start), _parse_time(settings.london_session_end)),
        "NEW_YORK": (_parse_time(settings.new_york_session_start), _parse_time(settings.new_york_session_end)),
    }


def _session_for(value: time, windows: dict[str, tuple[time, time]]) -> str:
    for session in ("OVERLAP", "ASIA", "LONDON", "NEW_YORK"):
        start, end = windows[session]
        if _in_window(value, start, end):
            return session
    return "OFF_SESSION"


def _in_window(value: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= value < end
    return value >= start or value < end


def _parse_time(value: str) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return time(0, 0)


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: object) -> str:
    numeric = _float(value)
    return "-" if numeric is None else f"{round(numeric * 100):.0f}%"


def _signed(value: object) -> str:
    numeric = _float(value) or 0.0
    return f"{numeric:+.0f}"


def _num(value: object) -> str:
    numeric = _float(value)
    return "-" if numeric is None else f"{numeric:.2f}"
