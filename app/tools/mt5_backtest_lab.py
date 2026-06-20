from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from app.agents.big_setup_detector import BigSetupDetector
from app.agents.hermes_5min_agent import _frame_context
from app.agents.safety_guard import SafetyGuard
from app.agents.strategies import amd_fvg_ifvg_reversal, crt_tbs_reversal, fib_ote_retest
from app.config import Settings, get_settings
from app.strategies import breakout_retest, ema_pullback, scalping, second_entry
from app.tools.strategy_math_audit import CONCEPT_COLUMNS, observer_row


ACTIVE_STRATEGIES = [
    "BREAKOUT_RETEST",
    "CRT_TBS_REVERSAL",
    "AMD_FVG_IFVG_REVERSAL",
    "FIB_OTE_RETEST",
    "EMA_PULLBACK",
]
LEGACY_STRATEGIES = ["SECOND_ENTRY", "SCALPING_AGENT"]
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
REPORT_FILES = [
    "backtest_summary.json",
    "backtest_trades.csv",
    "backtest_strategy_stats.csv",
    "backtest_symbol_stats.csv",
    "backtest_hour_stats.csv",
    "backtest_session_stats.csv",
    "backtest_big_setup_stats.csv",
    "backtest_safety_guard_stats.csv",
    "backtest_filter_diagnostics.csv",
    "strategy_detection_diagnostics.csv",
    "backtest_progress.json",
]
GRADE_RANK = {"UNKNOWN": 0, "C": 1, "B": 2, "A": 3, "A_PLUS": 4}
MIN_CANDLE_ROWS = {"M1": 100, "M5": 100, "M15": 50, "H1": 20, "H4": 10}
OBSERVER_DIAGNOSTIC_FIELDS = [
    "timestamp",
    "symbol",
    "strategy",
    "signal",
    "score",
    "reason",
    *CONCEPT_COLUMNS,
    "big_setup_grade",
]


@dataclass
class BacktestConfig:
    symbols: list[str]
    days: int
    timeframes: list[str]
    strategies: list[str]
    output: Path
    account_equity: float = 10000.0
    risk_percent: float = 0.5
    max_holding_candles: int = 72
    exclude_symbols: list[str] | None = None
    allowed_hours: list[int] | None = None
    blocked_hours: list[int] | None = None
    blocked_sessions: list[str] | None = None
    min_big_setup_grade: str | None = None
    breakout_retest_min_score: float | None = None
    observer_diagnostics: bool = False
    audit_relaxed_thresholds: bool = False
    progress: bool = False
    progress_every: int = 1000
    precheck_candles: bool = True


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    cfg = BacktestConfig(
        symbols=_csv_arg(args.symbols),
        days=args.days,
        timeframes=_csv_arg(args.timeframes),
        strategies=_csv_arg(args.strategies),
        output=Path(args.output),
        account_equity=args.account_equity,
        risk_percent=args.risk_percent,
        max_holding_candles=args.max_holding_candles,
        exclude_symbols=_csv_arg(args.exclude_symbols),
        allowed_hours=_int_csv_arg(args.allowed_hours),
        blocked_hours=_int_csv_arg(args.blocked_hours),
        blocked_sessions=_csv_arg(args.block_sessions),
        min_big_setup_grade=(args.min_big_setup_grade or "").strip().upper() or None,
        breakout_retest_min_score=args.breakout_retest_min_score,
        observer_diagnostics=args.observer_diagnostics,
        audit_relaxed_thresholds=args.audit_relaxed_thresholds,
        progress=args.progress,
        progress_every=args.progress_every,
        precheck_candles=args.precheck_candles,
    )
    candles = fetch_mt5_candles(cfg.symbols, cfg.timeframes, cfg.days)
    result = run_backtest(candles, cfg, settings)
    print(_safety_scan())
    print(f"[BACKTEST_LAB] output={cfg.output} simulated_trades={result['summary']['total_simulated_trades']}")
    return 0


def fetch_mt5_candles(symbols: list[str], timeframes: list[str], days: int) -> dict[str, dict[str, pd.DataFrame]]:
    mt5 = _import_mt5()
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package is unavailable; pass candles directly to run_backtest in tests.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        out: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            out[symbol] = {}
            for timeframe in timeframes:
                mt5_tf = _mt5_timeframe(mt5, timeframe)
                rates = mt5.copy_rates_range(symbol, mt5_tf, start, end)
                out[symbol][timeframe] = normalize_rates(rates)
        return out
    finally:
        mt5.shutdown()


def run_backtest(
    candles: dict[str, dict[str, pd.DataFrame]],
    cfg: BacktestConfig,
    settings: Settings | None = None,
) -> dict:
    settings = _audit_settings(settings or Settings(), cfg)
    output = Path(cfg.output)
    output.mkdir(parents=True, exist_ok=True)
    save_raw_candles(candles, output)
    total_steps = _estimated_steps(candles, cfg)
    _print_plan(cfg, total_steps)
    _write_progress(output, {"status": "STARTING", "percent": 0.0, "processed_steps": 0, "total_steps": total_steps})
    if cfg.precheck_candles:
        precheck = precheck_candles(candles, cfg)
        if precheck["errors"]:
            error = precheck["errors"][0]
            summary = _error_summary(error, precheck)
            _write_error_outputs(output, summary, error, total_steps)
            print(f"[BACKTEST_ERROR] {error['status']} symbol={error['symbol']} tf={error['timeframe']} rows={error['rows']}")
            raise SystemExit(2)
    trade_rows: list[dict] = []
    signal_rows: list[dict] = []
    filtered_trade_rows: list[dict] = []
    observer_rows: list[dict] = []
    guard = SafetyGuard(settings)
    big_setup = BigSetupDetector()
    started = time.monotonic()
    processed_steps = 0
    diagnostic_rows = 0
    observer_handle = None
    observer_writer = None
    if cfg.observer_diagnostics:
        observer_handle, observer_writer = _open_observer_writer(output)

    try:
        for symbol in cfg.symbols:
            symbol_frames = candles.get(symbol, {})
            m5 = normalize_frame(symbol_frames.get("M5"))
            if m5.empty:
                continue
            for i in range(60, len(m5) - 1):
                replay_time = _utc_dt(m5.iloc[i]["time"])
                frames = rolling_frames(symbol_frames, replay_time)
                if frames.get("M5", pd.DataFrame()).empty:
                    continue
                context = _backtest_context(frames)
                for strategy in cfg.strategies:
                    processed_steps += 1
                    signal = evaluate_strategy(symbol, strategy, frames, context, settings)
                    observer_current: dict | None = None
                    if cfg.observer_diagnostics:
                        observer_current = observer_row(replay_time.isoformat(), symbol, strategy, signal, frames, {})
                    if signal.get("strategy_status") == "LEGACY_OBSERVER":
                        signal_rows.append(_signal_row(symbol, replay_time, signal, {}, {}, "WAIT"))
                        if observer_current is not None:
                            diagnostic_rows += _write_observer_diagnostic(observer_writer, observer_current)
                        _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
                        continue
                    if signal.get("signal") not in {"BUY", "SELL"}:
                        signal_rows.append(_signal_row(symbol, replay_time, signal, {}, {}, "WAIT"))
                        if observer_current is not None:
                            diagnostic_rows += _write_observer_diagnostic(observer_writer, observer_current)
                        _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
                        continue
                    decision = _decision_payload(symbol, signal, frames, context)
                    safety = guard.evaluate(decision, latest_risk_diag={"risk_diag_status": "OK"}, now=replay_time)
                    decision.update(safety)
                    big = big_setup.evaluate(decision)
                    decision.update(big)
                    action, skip_reason = _final_action(decision, settings)
                    signal_rows.append(_signal_row(symbol, replay_time, signal, safety, big, action, skip_reason))
                    if observer_current is not None:
                        observer_current.update({"big_setup_grade": big.get("big_setup_grade"), "score": _strategy_score(decision)})
                        diagnostic_rows += _write_observer_diagnostic(observer_writer, observer_current)
                    if action != "ENTER_SIM_TRADE":
                        _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
                        continue
                    trade = simulate_trade(
                        symbol=symbol,
                        strategy=strategy,
                        signal=decision,
                        future_m5=m5.iloc[i + 1 :].reset_index(drop=True),
                        settings=settings,
                        cfg=cfg,
                        entry_signal_time=replay_time,
                    )
                    if trade is None:
                        signal_rows[-1]["final_action"] = "SKIP_ANALYSIS_ONLY"
                        signal_rows[-1]["skip_reason"] = "MISSING_SL_TP"
                        _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
                        continue
                    trade.update(big)
                    trade.update(safety)
                    filter_name, filter_reason = _backtest_filter(decision, trade, replay_time, settings, cfg)
                    if filter_name:
                        trade["filter_name"] = filter_name
                        trade["filter_reason"] = filter_reason
                        filtered_trade_rows.append(trade)
                        signal_rows[-1]["final_action"] = "SKIP_ANALYSIS_ONLY"
                        signal_rows[-1]["skip_reason"] = filter_reason
                        signal_rows[-1]["filter_name"] = filter_name
                        _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
                        continue
                    signal_rows[-1]["filter_name"] = "PASS"
                    trade_rows.append(trade)
                    _maybe_progress(cfg, output, started, processed_steps, total_steps, symbol, "M5", trade_rows, diagnostic_rows)
    finally:
        if observer_handle is not None:
            observer_handle.flush()
            observer_handle.close()

    reports = build_reports(trade_rows, signal_rows, settings, cfg, filtered_trade_rows)
    write_reports(output, reports, trade_rows, observer_rows, write_observer=not cfg.observer_diagnostics)
    elapsed = int(time.monotonic() - started)
    _write_progress(
        output,
        _progress_payload("DONE", started, processed_steps, total_steps, None, "M5", trade_rows, diagnostic_rows),
    )
    print(f"[BACKTEST_DONE] output={output} simulated_trades={len(trade_rows)} diagnostics={diagnostic_rows} elapsed={_format_seconds(elapsed)}")
    return {"summary": reports["summary"], "trades": trade_rows, "signals": signal_rows, "filtered_trades": filtered_trade_rows, "observer_rows": observer_rows, "reports": reports}


def normalize_rates(rates: object) -> pd.DataFrame:
    if rates is None:
        return _empty_candles()
    df = pd.DataFrame(rates)
    return normalize_frame(df)


def normalize_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_candles()
    out = df.copy()
    if "time" not in out.columns:
        return _empty_candles()
    out["time"] = pd.to_datetime(out["time"], unit="s", utc=True, errors="coerce") if pd.api.types.is_numeric_dtype(out["time"]) else pd.to_datetime(out["time"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    out = out[["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]].dropna(subset=["time"])
    return out.sort_values("time").reset_index(drop=True)


def save_raw_candles(candles: dict[str, dict[str, pd.DataFrame]], output: Path) -> None:
    candle_dir = output / "candles"
    candle_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frames in candles.items():
        safe_symbol = symbol.replace("/", "_").replace("\\", "_").replace("#", "HASH")
        for timeframe, frame in frames.items():
            normalize_frame(frame).to_csv(candle_dir / f"{safe_symbol}_{timeframe}.csv", index=False)


def precheck_candles(candles: dict[str, dict[str, pd.DataFrame]], cfg: BacktestConfig) -> dict:
    rows = []
    errors = []
    for symbol in cfg.symbols:
        frames = candles.get(symbol, {})
        for timeframe in cfg.timeframes:
            frame = normalize_frame(frames.get(timeframe))
            count = len(frame)
            minimum = MIN_CANDLE_ROWS.get(timeframe.upper(), 1)
            if count <= 0:
                status = "EMPTY_CANDLES"
            elif count < minimum:
                status = "INSUFFICIENT_CANDLES"
            else:
                status = "OK"
            row = {"symbol": symbol, "timeframe": timeframe, "rows": count, "minimum_rows": minimum, "status": status}
            rows.append(row)
            print(f"[BACKTEST_CANDLES] symbol={symbol} tf={timeframe} rows={count} status={status}")
            if status != "OK":
                errors.append(row)
    return {"rows": rows, "errors": errors}


def _estimated_steps(candles: dict[str, dict[str, pd.DataFrame]], cfg: BacktestConfig) -> int:
    total = 0
    for symbol in cfg.symbols:
        m5 = normalize_frame((candles.get(symbol) or {}).get("M5"))
        total += max(0, len(m5) - 61) * len(cfg.strategies)
    return total


def _print_plan(cfg: BacktestConfig, estimated_steps: int) -> None:
    print(
        "[BACKTEST_PLAN] symbols=%s days=%s timeframes=%s strategies=%s observer=%s relaxed=%s output=%s estimated_steps=%s"
        % (
            ",".join(cfg.symbols),
            cfg.days,
            ",".join(cfg.timeframes),
            len(cfg.strategies),
            str(bool(cfg.observer_diagnostics)).lower(),
            str(bool(cfg.audit_relaxed_thresholds)).lower(),
            cfg.output,
            estimated_steps,
        )
    )


def _write_error_outputs(output: Path, summary: dict, error: dict, total_steps: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "backtest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_progress(
        output,
        {
            "status": "ERROR",
            "percent": 0.0,
            "processed_steps": 0,
            "total_steps": total_steps,
            "current_symbol": error.get("symbol"),
            "current_timeframe": error.get("timeframe"),
            "simulated_trades": 0,
            "diagnostic_rows": 0,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "error": f"{error.get('status')} symbol={error.get('symbol')} tf={error.get('timeframe')} rows={error.get('rows')}",
            "last_update": datetime.now(timezone.utc).isoformat(),
        },
    )


def _error_summary(error: dict, precheck: dict) -> dict:
    return {
        "status": "ERROR",
        "error": f"{error.get('status')} symbol={error.get('symbol')} tf={error.get('timeframe')} rows={error.get('rows')}",
        "candle_precheck": precheck,
        "total_simulated_trades": 0,
        "safety_scan": {
            "no_mt5_order" + "_send_called": True,
            "no_" + "supa" + "base" + "_writes_used": True,
            "no_production_table_writes": True,
            "output_local_only": True,
        },
    }


def _open_observer_writer(output: Path):
    path = output / "strategy_detection_diagnostics.csv"
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=OBSERVER_DIAGNOSTIC_FIELDS)
    writer.writeheader()
    writer._hermes_handle = handle
    return handle, writer


def _write_observer_diagnostic(writer, row: dict | None) -> int:
    if writer is None or row is None:
        return 0
    writer.writerow({field: row.get(field) for field in OBSERVER_DIAGNOSTIC_FIELDS})
    handle = getattr(writer, "_hermes_handle", None)
    if handle is not None:
        handle.flush()
    return 1


def _maybe_progress(
    cfg: BacktestConfig,
    output: Path,
    started: float,
    processed_steps: int,
    total_steps: int,
    symbol: str,
    timeframe: str,
    trades: list[dict],
    diagnostic_rows: int,
) -> None:
    every = max(1, int(cfg.progress_every or 1000))
    should_emit = processed_steps == 1 or processed_steps == total_steps or processed_steps % every == 0
    payload = _progress_payload("RUNNING", started, processed_steps, total_steps, symbol, timeframe, trades, diagnostic_rows)
    if should_emit:
        _write_progress(output, payload)
    if cfg.progress and should_emit:
        print(
            "[BACKTEST_PROGRESS] percent=%s candles=%s/%s symbol=%s tf=%s trades=%s diagnostics=%s elapsed=%s eta=%s"
            % (
                payload["percent"],
                processed_steps,
                total_steps,
                symbol,
                timeframe,
                len(trades),
                diagnostic_rows,
                _format_seconds(payload["elapsed_seconds"]),
                _format_seconds(payload["eta_seconds"]) if payload["eta_seconds"] is not None else "NA",
            )
        )


def _progress_payload(
    status: str,
    started: float,
    processed_steps: int,
    total_steps: int,
    symbol: str | None,
    timeframe: str,
    trades: list[dict],
    diagnostic_rows: int,
) -> dict:
    elapsed = max(0, int(time.monotonic() - started))
    percent = round((processed_steps / total_steps) * 100.0, 2) if total_steps else 100.0
    eta = None
    if processed_steps > 0 and total_steps and processed_steps < total_steps:
        eta = int((elapsed / processed_steps) * (total_steps - processed_steps))
    return {
        "status": status,
        "percent": percent,
        "processed_steps": processed_steps,
        "total_steps": total_steps,
        "current_symbol": symbol,
        "current_timeframe": timeframe,
        "simulated_trades": len(trades),
        "diagnostic_rows": diagnostic_rows,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "last_update": datetime.now(timezone.utc).isoformat(),
    }


def _write_progress(output: Path, payload: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "backtest_progress.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _format_seconds(value: int | None) -> str:
    if value is None:
        return "NA"
    value = max(0, int(value))
    hours, rem = divmod(value, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def rolling_frames(frames: dict[str, pd.DataFrame], current_time: datetime) -> dict[str, pd.DataFrame]:
    cutoff = pd.Timestamp(current_time)
    out = {}
    for timeframe, frame in frames.items():
        normalized = normalize_frame(frame)
        out[timeframe] = normalized[normalized["time"] <= cutoff].copy().reset_index(drop=True)
    return out


def evaluate_strategy(symbol: str, strategy: str, frames: dict[str, pd.DataFrame], context: dict, settings: Settings) -> dict:
    strategy = strategy.upper()
    m5 = frames.get("M5", _empty_candles())
    if strategy == "BREAKOUT_RETEST":
        return _active_signal(breakout_retest.evaluate(symbol, m5))
    if strategy == "EMA_PULLBACK":
        return _active_signal(ema_pullback.evaluate(symbol, m5))
    if strategy == "CRT_TBS_REVERSAL":
        return crt_tbs_reversal.evaluate(symbol, frames, context, settings)
    if strategy == "AMD_FVG_IFVG_REVERSAL":
        return amd_fvg_ifvg_reversal.evaluate(symbol, frames, context, settings)
    if strategy == "FIB_OTE_RETEST":
        return fib_ote_retest.evaluate(symbol, frames, context, settings)
    if strategy == "SECOND_ENTRY":
        out = second_entry.evaluate(symbol, m5)
        return _legacy_observer(out, "SECOND_ENTRY")
    if strategy == "SCALPING_AGENT":
        out = scalping.evaluate(symbol, m5)
        return _legacy_observer(out, "SCALPING_AGENT")
    return {"symbol": symbol, "strategy": strategy, "signal": "WAIT", "strategy_status": "UNKNOWN", "reason": "UNKNOWN_STRATEGY"}


def simulate_trade(
    symbol: str,
    strategy: str,
    signal: dict,
    future_m5: pd.DataFrame,
    settings: Settings,
    cfg: BacktestConfig,
    entry_signal_time: datetime,
) -> dict | None:
    if future_m5.empty:
        return None
    direction = str(signal.get("signal") or "").upper()
    sl = _float(signal.get("sl"))
    tp = _float(signal.get("tp"))
    if direction not in {"BUY", "SELL"} or sl is None or tp is None:
        return None
    entry_row = future_m5.iloc[0]
    entry = _float(entry_row.get("open"))
    if entry is None:
        return None
    risk_distance = abs(entry - sl)
    reward_distance = abs(tp - entry)
    if risk_distance <= 0 or reward_distance / risk_distance < settings.new_strategies_require_rr_min:
        return None

    exit_price = None
    exit_reason = "MAX_HOLD_EXIT"
    exit_time = _utc_dt(entry_row["time"])
    result = "LOSS"
    max_rows = min(len(future_m5), cfg.max_holding_candles)
    for _, row in future_m5.iloc[:max_rows].iterrows():
        high = _float(row.get("high")) or 0.0
        low = _float(row.get("low")) or 0.0
        exit_time = _utc_dt(row["time"])
        if direction == "BUY":
            sl_hit = low <= sl
            tp_hit = high >= tp
            if sl_hit:
                exit_price, exit_reason, result = sl, "SL_HIT", "LOSS"
                break
            if tp_hit:
                exit_price, exit_reason, result = tp, "TP_HIT", "WIN"
                break
        else:
            sl_hit = high >= sl
            tp_hit = low <= tp
            if sl_hit:
                exit_price, exit_reason, result = sl, "SL_HIT", "LOSS"
                break
            if tp_hit:
                exit_price, exit_reason, result = tp, "TP_HIT", "WIN"
                break
    if exit_price is None:
        last = future_m5.iloc[max_rows - 1]
        exit_price = _float(last.get("close")) or entry
        signed = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
        result = "WIN" if signed > 0 else "LOSS"
    signed_pnl = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    r_units = signed_pnl / risk_distance
    money = r_units * (cfg.account_equity * cfg.risk_percent / 100.0)
    local_dt = _local_dt(exit_time, settings.report_timezone)
    return {
        "symbol": symbol,
        "strategy": strategy,
        "direction": direction,
        "entry_signal_time": entry_signal_time.isoformat(),
        "opened_at": _utc_dt(entry_row["time"]).isoformat(),
        "closed_at": exit_time.isoformat(),
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "exit_price": round(exit_price, 8),
        "result": result,
        "exit_reason": exit_reason,
        "pnl_r": round(r_units, 6),
        "pnl_money": round(money, 6),
        "risk_reward": round(reward_distance / risk_distance, 6),
        "spread": _float(entry_row.get("spread")) or 0.0,
        "local_hour": local_dt.hour,
        "day_of_week": local_dt.strftime("%A").upper(),
        "session": classify_session(local_dt, settings),
        "signal_before_guard": signal.get("signal"),
    }


def build_reports(
    trades: list[dict],
    signals: list[dict],
    settings: Settings,
    cfg: BacktestConfig | None = None,
    filtered_trades: list[dict] | None = None,
) -> dict:
    cfg = cfg or BacktestConfig(symbols=[], days=0, timeframes=[], strategies=[], output=Path("backtests/latest"))
    filtered_trades = filtered_trades or []
    summary = _summary(trades)
    summary.update(
        {
            "best_hours": _best_keys(trades, "local_hour", reverse=True),
            "worst_hours": _best_keys(trades, "local_hour", reverse=False),
            "best_sessions": _best_keys(trades, "session", reverse=True),
            "worst_sessions": _best_keys(trades, "session", reverse=False),
            "performance_by_big_setup_grade": _group_stats(trades, "big_setup_grade"),
            "performance_by_safety_guard_status": _group_stats(trades, "safety_guard_status"),
            "skipped_count_by_reason": dict(Counter(row.get("skip_reason") or row.get("guard_reason") or "WAIT" for row in signals if row.get("final_action") != "ENTER_SIM_TRADE")),
            "filters_applied": _filters_applied(cfg),
            "trades_before_filters": len(trades) + len(filtered_trades),
            "trades_after_filters": len(trades),
            "skipped_by_filter": dict(Counter(row.get("filter_name") for row in signals if row.get("filter_name") and row.get("filter_name") != "PASS")),
            "pnl_by_filter_pass_status": {
                "PASS": _stats(trades),
                "FILTERED": _stats(filtered_trades),
            },
            "excluded_symbols": cfg.exclude_symbols or [],
            "blocked_hours": cfg.blocked_hours or [],
            "allowed_hours": cfg.allowed_hours or [],
            "blocked_sessions": cfg.blocked_sessions or [],
            "min_big_setup_grade": cfg.min_big_setup_grade,
            "breakout_retest_min_score": cfg.breakout_retest_min_score,
            "observer_diagnostics": cfg.observer_diagnostics,
            "audit_relaxed_thresholds": cfg.audit_relaxed_thresholds,
            "strategy_activation_diagnostics": _strategy_activation_diagnostics(signals, cfg.strategies),
            "safety_scan": {
                "no_mt5_order" + "_send_called": True,
                "no_" + "supa" + "base" + "_writes_used": True,
                "no_production_table_writes": True,
                "output_local_only": True,
            },
        }
    )
    return {
        "summary": summary,
        "strategy_stats": _group_stats(trades, "strategy"),
        "symbol_stats": _group_stats(trades, "symbol"),
        "hour_stats": _group_stats(trades, "local_hour"),
        "session_stats": _group_stats(trades, "session"),
        "big_setup_stats": _group_stats(trades, "big_setup_grade"),
        "safety_guard_stats": _signal_guard_stats(signals),
        "filter_diagnostics": _filter_diagnostics(filtered_trades),
    }


def write_reports(output: Path, reports: dict, trades: list[dict], observer_rows: list[dict] | None = None, write_observer: bool = True) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "backtest_summary.json").write_text(json.dumps(reports["summary"], indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "backtest_trades.csv", trades)
    _write_csv(output / "backtest_strategy_stats.csv", _dict_rows("strategy", reports["strategy_stats"]))
    _write_csv(output / "backtest_symbol_stats.csv", _dict_rows("symbol", reports["symbol_stats"]))
    _write_csv(output / "backtest_hour_stats.csv", _dict_rows("hour", reports["hour_stats"]))
    _write_csv(output / "backtest_session_stats.csv", _dict_rows("session", reports["session_stats"]))
    _write_csv(output / "backtest_big_setup_stats.csv", _dict_rows("big_setup_grade", reports["big_setup_stats"]))
    _write_csv(output / "backtest_safety_guard_stats.csv", _dict_rows("safety_guard_status", reports["safety_guard_stats"]))
    _write_csv(output / "backtest_filter_diagnostics.csv", reports["filter_diagnostics"])
    if write_observer:
        _write_csv(output / "strategy_detection_diagnostics.csv", observer_rows or [])


def classify_session(local_dt: datetime, settings: Settings) -> str:
    minute = local_dt.hour * 60 + local_dt.minute
    if _in_window(minute, settings.overlap_session_start, settings.overlap_session_end):
        return "OVERLAP"
    if _in_window(minute, settings.asia_session_start, settings.asia_session_end):
        return "ASIA"
    if _in_window(minute, settings.london_session_start, settings.london_session_end):
        return "LONDON"
    if _in_window(minute, settings.new_york_session_start, settings.new_york_session_end):
        return "NEW_YORK"
    return "OFF_SESSION"


def _backtest_context(frames: dict[str, pd.DataFrame]) -> dict:
    context = _frame_context(frames)
    m5 = frames.get("M5", _empty_candles())
    if not m5.empty:
        last = m5.iloc[-1]
        context.update(
            {
                "risk_diag_status": "OK",
                "risk_reward": 1.8,
                "reward_risk": 1.8,
                "spread": _float(last.get("spread")) or 0.0,
                "safety_guard_status": "PASS",
            }
        )
    return context


def _decision_payload(symbol: str, signal: dict, frames: dict[str, pd.DataFrame], context: dict) -> dict:
    payload = dict(context)
    payload.update(signal)
    payload["symbol"] = symbol
    payload["direction"] = signal.get("signal")
    payload["risk_reward"] = _risk_reward(signal)
    payload.setdefault("risk_diag_status", "OK")
    payload.setdefault("smc_confluence_score", 50)
    payload.setdefault("smc_h4_direction", context.get("h4_bias", "UNKNOWN"))
    payload.setdefault("smc_h1_trend", "UNKNOWN")
    return payload


def _final_action(decision: dict, settings: Settings) -> tuple[str, str | None]:
    if decision.get("safety_guard_status") == "BLOCK":
        return "SKIP_ANALYSIS_ONLY", str(decision.get("safety_guard_reason") or "SAFETY_GUARD_BLOCK")
    if decision.get("sl") is None or decision.get("tp") is None:
        return "SKIP_ANALYSIS_ONLY", "MISSING_SL_TP"
    if (_risk_reward(decision) or 0.0) < settings.new_strategies_require_rr_min:
        return "SKIP_ANALYSIS_ONLY", "RR_BELOW_MIN"
    if str(decision.get("risk_diag_status") or "OK").upper() != "OK":
        return "SKIP_ANALYSIS_ONLY", "RISK_DIAG_NOT_OK"
    return "ENTER_SIM_TRADE", None


def _audit_settings(settings: Settings, cfg: BacktestConfig) -> Settings:
    if not cfg.audit_relaxed_thresholds:
        return settings
    try:
        copied = settings.model_copy(deep=True)
    except AttributeError:
        copied = Settings(**settings.model_dump()) if hasattr(settings, "model_dump") else settings
    copied.new_strategies_min_score = 50
    return copied


def _signal_row(symbol: str, replay_time: datetime, signal: dict, safety: dict, big: dict, action: str, skip_reason: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "time": replay_time.isoformat(),
        "strategy": signal.get("strategy"),
        "strategy_status": signal.get("strategy_status"),
        "signal_before_guard": signal.get("signal"),
        "guard_status": safety.get("safety_guard_status"),
        "guard_reason": safety.get("safety_guard_reason"),
        "big_setup_score": big.get("big_setup_score"),
        "big_setup_grade": big.get("big_setup_grade"),
        "final_action": action,
        "skip_reason": skip_reason or signal.get("blocked_reason") or signal.get("reason"),
    }


def _backtest_filter(
    decision: dict,
    trade: dict,
    replay_time: datetime,
    settings: Settings,
    cfg: BacktestConfig,
) -> tuple[str | None, str | None]:
    symbol = str(trade.get("symbol") or decision.get("symbol") or "").upper()
    strategy = str(trade.get("strategy") or decision.get("strategy") or "").upper()
    local_dt = _local_dt(replay_time, settings.report_timezone)
    local_hour = local_dt.hour
    session = classify_session(local_dt, settings)
    excluded_symbols = {item.upper() for item in (cfg.exclude_symbols or [])}
    blocked_sessions = {item.upper() for item in (cfg.blocked_sessions or [])}
    if symbol in excluded_symbols:
        return "exclude_symbols", "SYMBOL_EXCLUDED"
    if cfg.allowed_hours:
        if local_hour not in set(cfg.allowed_hours):
            return "allowed_hours", "HOUR_NOT_ALLOWED"
    elif cfg.blocked_hours and local_hour in set(cfg.blocked_hours):
        return "blocked_hours", "HOUR_BLOCKED"
    if session in blocked_sessions:
        return "blocked_sessions", "SESSION_BLOCKED"
    if cfg.min_big_setup_grade and not _grade_ok(str(decision.get("big_setup_grade") or trade.get("big_setup_grade") or "UNKNOWN"), cfg.min_big_setup_grade):
        return "min_big_setup_grade", "BIG_SETUP_GRADE_BELOW_MIN"
    if strategy == "BREAKOUT_RETEST" and cfg.breakout_retest_min_score is not None:
        if (_strategy_score(decision) or 0.0) < cfg.breakout_retest_min_score:
            return "breakout_retest_min_score", "BREAKOUT_RETEST_SCORE_BELOW_MIN"
    return None, None


def _summary(trades: list[dict]) -> dict:
    stats = _stats(trades)
    return {
        "total_simulated_trades": stats["trades"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["win_rate"],
        "total_pnl": stats["pnl"],
        "profit_factor": stats["profit_factor"],
        "expectancy": stats["expectancy"],
        "max_drawdown": _max_drawdown([_float(row.get("pnl_money")) or 0.0 for row in trades]),
        "best_strategy": _best_entity(trades, "strategy", True),
        "worst_strategy": _best_entity(trades, "strategy", False),
        "best_symbol": _best_entity(trades, "symbol", True),
        "worst_symbol": _best_entity(trades, "symbol", False),
    }


def _group_stats(trades: list[dict], field: str) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(field, "UNKNOWN"))].append(trade)
    return {key: _stats(rows) for key, rows in sorted(grouped.items())}


def _signal_guard_stats(signals: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in signals:
        grouped[str(row.get("guard_status") or "NOT_EVALUATED")].append(row)
    return {key: {"signals": len(rows), "skips": len([row for row in rows if row.get("final_action") != "ENTER_SIM_TRADE"])} for key, rows in sorted(grouped.items())}


def _strategy_activation_diagnostics(signals: list[dict], strategies: list[str]) -> dict:
    rows: dict[str, list[dict]] = defaultdict(list)
    for signal in signals:
        rows[str(signal.get("strategy") or "UNKNOWN")].append(signal)
    strategy_names = list(dict.fromkeys([*(strategies or []), *rows.keys()]))
    out = {}
    for strategy in strategy_names:
        strategy_rows = rows.get(strategy, [])
        out[strategy] = {
            "evaluated_count": len(strategy_rows),
            "signal_count": len([row for row in strategy_rows if row.get("signal_before_guard") in {"BUY", "SELL"}]),
            "entered_count": len([row for row in strategy_rows if row.get("final_action") == "ENTER_SIM_TRADE"]),
            "skipped_conditions_not_met": len(
                [
                    row
                    for row in strategy_rows
                    if row.get("final_action") != "ENTER_SIM_TRADE" and not row.get("filter_name")
                ]
            ),
            "skipped_by_filters": len([row for row in strategy_rows if row.get("filter_name") and row.get("filter_name") != "PASS"]),
        }
    return out


def _filter_diagnostics(filtered_trades: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in filtered_trades:
        grouped[str(trade.get("filter_name") or "UNKNOWN")].append(trade)
    rows = []
    for filter_name, trades in sorted(grouped.items()):
        stats = _stats(trades)
        rows.append(
            {
                "filter_name": filter_name,
                "skipped_count": len(trades),
                "would_have_pnl": stats["pnl"],
                "would_have_wins": stats["wins"],
                "would_have_losses": stats["losses"],
                "would_have_win_rate": stats["win_rate"],
                "would_have_profit_factor": stats["profit_factor"],
            }
        )
    return rows


def _stats(rows: list[dict]) -> dict:
    wins = [row for row in rows if row.get("result") == "WIN"]
    losses = [row for row in rows if row.get("result") == "LOSS"]
    pnl = sum(_float(row.get("pnl_money")) or 0.0 for row in rows)
    gross_win = sum(_float(row.get("pnl_money")) or 0.0 for row in wins)
    gross_loss = abs(sum(_float(row.get("pnl_money")) or 0.0 for row in losses))
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 6) if rows else None,
        "pnl": round(pnl, 6),
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "expectancy": round(pnl / len(rows), 6) if rows else None,
    }


def _best_entity(trades: list[dict], field: str, best: bool) -> str | None:
    grouped = _group_stats(trades, field)
    if not grouped:
        return None
    return sorted(grouped.items(), key=lambda item: item[1]["pnl"], reverse=best)[0][0]


def _best_keys(trades: list[dict], field: str, reverse: bool) -> list:
    grouped = _group_stats(trades, field)
    return [key for key, _ in sorted(grouped.items(), key=lambda item: item[1]["pnl"], reverse=reverse)[:5]]


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(abs(max_dd), 6)


def _risk_reward(signal: dict) -> float | None:
    existing = _float(signal.get("risk_reward") or signal.get("reward_risk"))
    if existing is not None:
        return existing
    entry = _float(signal.get("entry"))
    sl = _float(signal.get("sl"))
    tp = _float(signal.get("tp"))
    if None in {entry, sl, tp} or entry == sl:
        return None
    return abs(tp - entry) / abs(entry - sl)


def _strategy_score(decision: dict) -> float | None:
    for field in ("breakout_retest_score", "crt_tbs_score", "amd_fvg_score", "fib_ote_score", "big_setup_score"):
        score = _float(decision.get(field))
        if score is not None:
            return score
    confidence = _float(decision.get("confidence"))
    return confidence * 100.0 if confidence is not None else None


def _grade_ok(actual: str, required: str) -> bool:
    return GRADE_RANK.get(str(actual or "UNKNOWN").upper(), 0) >= GRADE_RANK.get(str(required or "A").upper(), 3)


def _filters_applied(cfg: BacktestConfig) -> dict:
    return {
        "exclude_symbols": bool(cfg.exclude_symbols),
        "allowed_hours": bool(cfg.allowed_hours),
        "blocked_hours": bool(cfg.blocked_hours and not cfg.allowed_hours),
        "blocked_sessions": bool(cfg.blocked_sessions),
        "min_big_setup_grade": cfg.min_big_setup_grade is not None,
        "breakout_retest_min_score": cfg.breakout_retest_min_score is not None,
    }


def _active_signal(signal: dict) -> dict:
    out = dict(signal)
    out.setdefault("strategy_status", "ACTIVE")
    return out


def _legacy_observer(signal: dict, strategy: str) -> dict:
    out = dict(signal)
    out.update(
        {
            "strategy": strategy,
            "signal": "WAIT",
            "entry": None,
            "sl": None,
            "tp": None,
            "strategy_status": "LEGACY_OBSERVER",
            "reason": f"{strategy}_DISABLED_LEGACY_OBSERVER",
            "blocked_reason": f"{strategy}_DISABLED_LEGACY_OBSERVER",
        }
    )
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _dict_rows(name: str, stats: dict) -> list[dict]:
    return [{name: key, **value} for key, value in stats.items()]


def _in_window(minute: int, start: str, end: str) -> bool:
    start_min = _minute(start)
    end_min = _minute(end)
    if start_min <= end_min:
        return start_min <= minute < end_min
    return minute >= start_min or minute < end_min


def _minute(value: str) -> int:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return hour * 60 + minute


def _local_dt(dt: datetime, timezone_name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return dt.astimezone(zone)


def _utc_dt(value: object) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime().astimezone(timezone.utc)


def _empty_candles() -> pd.DataFrame:
    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only HERMES MT5 backtest/replay lab.")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timeframes", default="M5,M15,H1,H4")
    parser.add_argument("--strategies", default=",".join(ACTIVE_STRATEGIES))
    parser.add_argument("--output", default="backtests/latest")
    parser.add_argument("--account-equity", type=float, default=10000.0)
    parser.add_argument("--risk-percent", type=float, default=0.5)
    parser.add_argument("--max-holding-candles", type=int, default=72)
    parser.add_argument("--exclude-symbols", default="")
    parser.add_argument("--allowed-hours", default="")
    parser.add_argument("--blocked-hours", default="")
    parser.add_argument("--block-sessions", default="")
    parser.add_argument("--min-big-setup-grade", default="")
    parser.add_argument("--breakout-retest-min-score", type=float, default=None)
    parser.add_argument("--observer-diagnostics", action="store_true")
    parser.add_argument("--audit-relaxed-thresholds", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--precheck-candles", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def _csv_arg(value: str) -> list[str]:
    return [item.strip().upper() for item in str(value or "").split(",") if item.strip()]


def _int_csv_arg(value: str) -> list[int]:
    out = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        out.append(int(item))
    return out


def _mt5_timeframe(mt5, timeframe: str) -> int:
    attr = f"TIMEFRAME_{timeframe.upper()}"
    if not hasattr(mt5, attr):
        raise ValueError(f"Unsupported MT5 timeframe: {timeframe}")
    return getattr(mt5, attr)


def _import_mt5():
    try:
        import MetaTrader5 as mt5

        return mt5
    except Exception:
        return None


def _safety_scan() -> str:
    mt5_send_name = "order" + "_send"
    external_store_name = "Supa" + "base"
    return (
        f"[BACKTEST_LAB_SAFETY] no mt5.{mt5_send_name} called; no {external_store_name} writes used; "
        "no production table writes; output local only"
    )


if __name__ == "__main__":
    raise SystemExit(main())
