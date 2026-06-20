from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app.agents.big_setup_detector import BigSetupDetector
from app.agents.hermes_5min_agent import _frame_context
from app.agents.safety_guard import SafetyGuard
from app.agents.smc_confluence_tagger import SMCConfluenceTagger
from app.config import Settings, get_settings
from app.strategies import ema_pullback
from app.tools.mt5_backtest_lab import (
    BacktestConfig,
    _csv_arg,
    _final_action,
    _float,
    _import_mt5,
    _mt5_timeframe,
    _risk_reward,
    _utc_dt,
    classify_session,
    evaluate_strategy,
    normalize_rates,
    simulate_trade,
)


SANDBOX_STRATEGIES = ["BREAKOUT_RETEST", "CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"]
OUTPUT_FILES = [
    "sandbox_events.jsonl",
    "sandbox_trades.csv",
    "sandbox_summary.json",
    "sandbox_strategy_stats.csv",
    "sandbox_big_setup_stats.csv",
    "sandbox_skip_reasons.csv",
]
GRADE_RANK = {"UNKNOWN": 0, "C": 1, "B": 2, "A": 3, "A_PLUS": 4}
MIN_CANDLES = {"M1": 100, "M5": 100, "M15": 50, "H1": 50, "H4": 20}


@dataclass
class SandboxConfig:
    symbol: str
    output: Path
    duration_hours: float
    poll_seconds: int
    risk_percent: float
    max_open_trades: int
    strategies: list[str]
    account_equity: float = 10000.0
    max_holding_candles: int = 72
    verbose_diag: bool = False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    cfg = SandboxConfig(
        symbol=args.symbol or settings.btc_weekend_sandbox_symbol,
        output=Path(args.output or settings.btc_weekend_sandbox_output),
        duration_hours=args.duration_hours,
        poll_seconds=args.poll_seconds or settings.btc_weekend_sandbox_poll_seconds,
        risk_percent=args.risk_percent or settings.btc_weekend_sandbox_risk_percent,
        max_open_trades=settings.btc_weekend_sandbox_max_open_trades,
        strategies=_sandbox_strategies(settings),
        verbose_diag=args.verbose_diag,
    )
    if args.diag_only:
        run_diag_only(cfg)
        return 0
    run_sandbox(cfg, settings)
    return 0


def run_sandbox(cfg: SandboxConfig, settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    cfg.output.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    trades: list[dict] = []
    open_trades = 0
    broker_symbol = cfg.symbol
    last_diag_state: str | None = None
    end_at = datetime.now(timezone.utc) + timedelta(hours=cfg.duration_hours)
    while True:
        frames, broker_symbol, diagnostics = fetch_latest_btc_frames(cfg.symbol, broker_symbol=broker_symbol)
        frames["__diagnostics__"] = diagnostics
        data_status = _data_status(frames)
        diag_state = _diag_state(data_status)
        if _should_print_diagnostics(last_diag_state, diag_state, cfg.verbose_diag):
            _print_full_diagnostics(diagnostics)
        last_diag_state = diag_state
        cycle = evaluate_cycle(cfg.symbol, frames, settings, cfg, open_trades, broker_symbol=broker_symbol)
        events.extend(cycle["events"])
        if cycle["trade"]:
            trades.append(cycle["trade"])
            open_trades = min(cfg.max_open_trades, open_trades + 1)
        _append_events(cfg.output / "sandbox_events.jsonl", cycle["events"])
        write_outputs(cfg.output, events, trades)
        _print_cycle_line(cycle, trades, open_trades)
        _print_strategy_scoreboard(cycle)
        if cfg.duration_hours <= 0 or datetime.now(timezone.utc) >= end_at:
            break
        time.sleep(max(1, cfg.poll_seconds))
    return {"events": events, "trades": trades, "summary": summarize(events, trades)}


def run_diag_only(cfg: SandboxConfig) -> dict:
    cfg.output.mkdir(parents=True, exist_ok=True)
    frames, resolved, diagnostics = fetch_latest_btc_frames(cfg.symbol)
    frames["__diagnostics__"] = diagnostics
    _print_full_diagnostics(diagnostics)
    reason = _missing_data_reason(frames)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": cfg.symbol,
        "broker_symbol": resolved,
        "sandbox_decision": "WAIT",
        "sandbox_reason": reason or "DIAG_ONLY",
        "price": _latest_price(frames),
        "data_status": _data_status(frames),
        "diagnostics": diagnostics,
    }
    _write_jsonl(cfg.output / "sandbox_events.jsonl", [event])
    summary = {
        "diag_only": True,
        "sandbox_trades": 0,
        "symbol": cfg.symbol,
        "broker_symbol": resolved,
        "price": event["price"],
        "data_status": event["data_status"],
        "reason": event["sandbox_reason"],
        "diagnostics": diagnostics,
        "local_output_only": True,
    }
    (cfg.output / "sandbox_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[BTC_SANDBOX_DIAG] symbol={cfg.symbol} broker_symbol={resolved} price={event['price']} data={event['data_status']} reason={event['sandbox_reason']}")
    return {"events": [event], "trades": [], "summary": summary}


def evaluate_cycle(
    symbol: str,
    frames: dict[str, pd.DataFrame],
    settings: Settings,
    cfg: SandboxConfig,
    open_trades: int = 0,
    now: datetime | None = None,
    broker_symbol: str | None = None,
) -> dict:
    events: list[dict] = []
    guard = SafetyGuard(settings)
    smc = SMCConfluenceTagger(settings)
    big_detector = BigSetupDetector()
    m5 = frames.get("M5", pd.DataFrame())
    current_time = now or (_utc_dt(m5.iloc[-1]["time"]) if not m5.empty else datetime.now(timezone.utc))
    context = _sandbox_context(frames)
    ema_confirmation = _ema_confirmation(symbol, frames, settings)
    chosen_event: dict | None = None
    chosen_trade: dict | None = None
    cycle_price = _latest_price(frames)
    cycle_smc = smc.evaluate(symbol, frames, None)
    cycle_big = big_detector.evaluate({**context, **cycle_smc, "symbol": symbol, "signal": "WAIT", "direction": "WAIT"})
    missing_reason = _missing_data_reason(frames)
    if missing_reason:
        for strategy in _active_cycle_strategies(cfg):
            event = _event(
                symbol,
                strategy,
                current_time,
                {**cycle_smc, "strategy": strategy, "signal": "WAIT", "entry": cycle_price, "reason": missing_reason},
                {},
                cycle_big,
                "WAIT",
                missing_reason,
                ema_confirmation,
                broker_symbol=broker_symbol,
                price=cycle_price,
            )
            events.append(event)
        chosen = _choose_cycle_event(events)
        return {
            "events": events,
            "trade": None,
            "chosen": chosen,
            "best": _best_candidate_event(events),
            "price": cycle_price,
            "broker_symbol": broker_symbol,
            "data_status": _data_status(frames),
        }

    for strategy in _active_cycle_strategies(cfg):
        signal = evaluate_strategy(symbol, strategy, frames, context, settings)
        if signal.get("signal") not in {"BUY", "SELL"}:
            wait_payload = {**signal, **cycle_smc, "entry": signal.get("entry") or cycle_price}
            event = _event(
                symbol,
                strategy,
                current_time,
                wait_payload,
                {},
                cycle_big,
                "WAIT",
                signal.get("reason") or _data_reason(frames),
                ema_confirmation,
                broker_symbol=broker_symbol,
                price=cycle_price,
            )
            events.append(event)
            continue
        decision = dict(context)
        decision.update(signal)
        decision["symbol"] = symbol
        decision["direction"] = signal.get("signal")
        decision["risk_reward"] = _risk_reward(signal)
        main_guard = guard.evaluate(decision, latest_risk_diag={"risk_diag_status": "OK"}, now=current_time)
        smc_tags = smc.evaluate(symbol, frames, signal.get("signal"))
        decision.update(smc_tags)
        decision["safety_guard_status"] = "PASS"
        decision["safety_guard_reason"] = "BTC_WEEKEND_SANDBOX_OVERRIDE"
        decision["ema_confirmation"] = ema_confirmation
        big = big_detector.evaluate(decision)
        decision.update(big)
        effective_open_trades = open_trades + (1 if chosen_trade is not None else 0)
        sandbox_action, reason = sandbox_decision(decision, main_guard, settings, cfg, effective_open_trades)
        event = _event(
            symbol,
            strategy,
            current_time,
            decision,
            main_guard,
            big,
            sandbox_action,
            reason,
            ema_confirmation,
            broker_symbol=broker_symbol,
            price=cycle_price,
        )
        events.append(event)
        if sandbox_action == "ENTER_SANDBOX" and chosen_trade is None:
            bt_cfg = BacktestConfig(
                symbols=[symbol],
                days=1,
                timeframes=["M5", "M15", "H1", "H4"],
                strategies=[strategy],
                output=cfg.output,
                risk_percent=cfg.risk_percent,
                account_equity=cfg.account_equity,
                max_holding_candles=cfg.max_holding_candles,
            )
            trade = simulate_trade(symbol, strategy, decision, m5.tail(cfg.max_holding_candles + 2).reset_index(drop=True), settings, bt_cfg, current_time)
            if trade is None:
                event["sandbox_decision"] = "SKIP_SANDBOX"
                event["sandbox_reason"] = "MISSING_SL_TP"
            else:
                event["pnl_r"] = trade.get("pnl_r")
                event["pnl_estimated_money"] = trade.get("pnl_money")
                trade.update(event)
                chosen_trade = trade
                chosen_event = event
    if chosen_event is None:
        chosen_event = _choose_cycle_event(events)
    return {
        "events": events,
        "trade": chosen_trade,
        "chosen": chosen_event,
        "best": _best_candidate_event(events),
        "price": cycle_price,
        "broker_symbol": broker_symbol,
        "data_status": _data_status(frames),
    }


def sandbox_decision(
    decision: dict,
    main_guard: dict,
    settings: Settings,
    cfg: SandboxConfig,
    open_trades: int,
) -> tuple[str, str]:
    if str(decision.get("strategy") or "").upper() == "EMA_PULLBACK":
        return "SKIP_SANDBOX", "EMA_CONFIRMATION_ONLY"
    if open_trades >= cfg.max_open_trades:
        return "SKIP_SANDBOX", "MAX_OPEN_SANDBOX_TRADES"
    if decision.get("sl") is None or decision.get("tp") is None:
        return "SKIP_SANDBOX", "MISSING_SL_TP"
    if (_risk_reward(decision) or 0.0) < settings.btc_weekend_sandbox_min_rr:
        return "SKIP_SANDBOX", "RR_BELOW_SANDBOX_MIN"
    if str(decision.get("risk_diag_status") or "OK").upper() != "OK":
        return "SKIP_SANDBOX", "RISK_DIAG_NOT_OK"
    if settings.btc_weekend_sandbox_require_smc_pass and str(decision.get("smc_confluence_status") or "").upper() != "PASS":
        return "SKIP_SANDBOX", "SMC_NOT_PASS"
    if (_float(decision.get("smc_confluence_score")) or 0.0) < settings.btc_weekend_sandbox_min_smc_score:
        return "SKIP_SANDBOX", "SMC_SCORE_BELOW_SANDBOX_MIN"
    if not _grade_ok(str(decision.get("big_setup_grade") or "UNKNOWN"), settings.btc_weekend_sandbox_require_big_setup_grade):
        return "SKIP_SANDBOX", "BIG_SETUP_GRADE_BELOW_SANDBOX_MIN"
    if (_float(decision.get("spread")) or 0.0) > settings.max_spread_for_symbol(str(decision.get("symbol") or "")):
        return "SKIP_SANDBOX", "SPREAD_NOT_OK"
    if _strategy_score(decision) < settings.new_strategies_min_score:
        return "SKIP_SANDBOX", "STRATEGY_SCORE_BELOW_MIN"
    return "ENTER_SANDBOX", "SANDBOX_FILTERS_PASS"


def fetch_latest_btc_frames(symbol: str, broker_symbol: str | None = None) -> tuple[dict[str, pd.DataFrame], str, dict]:
    mt5 = _import_mt5()
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package is unavailable.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        diagnostics = {"requested_symbol": symbol}
        resolution = _resolve_btc_symbol_details(mt5, broker_symbol or symbol)
        diagnostics.update(resolution)
        resolved = resolution.get("resolved_symbol")
        if not resolved:
            resolution = _resolve_btc_symbol_details(mt5, symbol)
            diagnostics.update(resolution)
            resolved = resolution.get("resolved_symbol")
        if not resolved:
            diagnostics["reason"] = "SYMBOL_NOT_FOUND"
            return {key: pd.DataFrame() for key in MIN_CANDLES}, symbol, diagnostics
        diagnostics["requested_symbol"] = symbol
        diagnostics["resolved_symbol"] = resolved
        _enrich_symbol_diagnostics(mt5, resolved, diagnostics)
        counts = {"M1": 500, "M5": 240, "M15": 160, "H1": 120, "H4": 120}
        out = {}
        candle_diags = {}
        for timeframe, count in counts.items():
            last_error = None
            rates = mt5.copy_rates_from_pos(resolved, _mt5_timeframe(mt5, timeframe), 0, count)
            frame = normalize_rates(rates)
            if frame.empty:
                last_error = _last_error(mt5)
            if frame.empty:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=max(3, count // 24))
                frame = normalize_rates(mt5.copy_rates_range(resolved, _mt5_timeframe(mt5, timeframe), start, end))
                if frame.empty and last_error is None:
                    last_error = _last_error(mt5)
            out[timeframe] = frame
            candle_diags[timeframe] = _candle_diag(frame, count, last_error)
        diagnostics["candles"] = candle_diags
        diagnostics["reason"] = _diagnostic_reason(diagnostics)
        return out, resolved, diagnostics
    finally:
        mt5.shutdown()


def _resolve_btc_symbol(mt5, requested: str) -> str | None:
    return _resolve_btc_symbol_details(mt5, requested).get("resolved_symbol")


def _resolve_btc_symbol_details(mt5, requested: str) -> dict:
    requested = str(requested or "").strip()
    result = {
        "requested_symbol": requested,
        "resolved_symbol": None,
        "symbol_select_result": False,
        "symbol_found": False,
    }
    if not requested:
        result["reason"] = "SYMBOL_NOT_FOUND"
        return result
    candidates = _symbol_candidates(requested)
    symbols = mt5.symbols_get()
    names = [item.name for item in symbols] if symbols else []
    names_upper = {name.upper(): name for name in names}
    for candidate in candidates:
        resolved = names_upper.get(candidate.upper())
        if resolved and mt5.symbol_select(resolved, True):
            result.update({"resolved_symbol": resolved, "symbol_select_result": True, "symbol_found": True})
            return result
    requested_base = requested.upper().replace("#", "")
    starts = [name for name in names if name.upper().replace("#", "").startswith(requested_base)]
    for candidate in starts:
        if mt5.symbol_select(candidate, True):
            result.update({"resolved_symbol": candidate, "symbol_select_result": True, "symbol_found": True})
            return result
    selected = mt5.symbol_select(requested, True)
    if selected:
        result.update({"resolved_symbol": requested, "symbol_select_result": True, "symbol_found": True})
        return result
    result["reason"] = "SYMBOL_NOT_SELECTED" if requested in names else "SYMBOL_NOT_FOUND"
    return result


def _symbol_candidates(requested: str) -> list[str]:
    base = requested.replace("#", "")
    if requested.upper().endswith("#"):
        candidates = [requested, base, f"{base}#", f"{base}.", f"{base}m", f"{base}micro"]
    else:
        candidates = [f"{base}#", requested, base, f"{base}.", f"{base}m", f"{base}micro"]
    candidates.extend(["BTCUSD#", "BTCUSD", "BTCUSD.", "BTCUSDm", "BTCUSDmicro"])
    return list(dict.fromkeys([item for item in candidates if item]))


def write_outputs(output: Path, events: list[dict], trades: list[dict]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "sandbox_events.jsonl", events)
    _write_csv(output / "sandbox_trades.csv", trades)
    summary = summarize(events, trades)
    (output / "sandbox_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "sandbox_strategy_stats.csv", _dict_rows("strategy", _group_trade_stats(trades, "strategy")))
    _write_csv(output / "sandbox_big_setup_stats.csv", _dict_rows("big_setup_grade", _group_trade_stats(trades, "big_setup_grade")))
    _write_csv(output / "sandbox_skip_reasons.csv", _skip_reason_rows(events))


def summarize(events: list[dict], trades: list[dict]) -> dict:
    wins = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    pnl = sum(_float(t.get("pnl_estimated_money") or t.get("pnl_money")) or 0.0 for t in trades)
    gross_win = sum(_float(t.get("pnl_estimated_money") or t.get("pnl_money")) or 0.0 for t in wins)
    gross_loss = abs(sum(_float(t.get("pnl_estimated_money") or t.get("pnl_money")) or 0.0 for t in losses))
    return {
        "events": len(events),
        "sandbox_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 6) if trades else None,
        "pnl_estimated_money": round(pnl, 6),
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "skip_reasons": dict(Counter(e.get("sandbox_reason") for e in events if e.get("sandbox_decision") != "ENTER_SANDBOX")),
        "main_guard_blocks_recorded": len([e for e in events if e.get("main_safety_guard_would_block")]),
        "data_reasons": dict(Counter(e.get("sandbox_reason") for e in events if str(e.get("sandbox_reason") or "").startswith(("NO_MT5", "SYMBOL_", "MT5_COPY", "INSUFFICIENT")))),
        "local_output_only": True,
    }


def _sandbox_context(frames: dict[str, pd.DataFrame]) -> dict:
    context = _frame_context(frames)
    m5 = frames.get("M5", pd.DataFrame())
    if not m5.empty:
        context.update({"spread": _float(m5.iloc[-1].get("spread")) or 0.0, "risk_diag_status": "OK", "risk_reward": 1.8, "reward_risk": 1.8})
    return context


def _latest_price(frames: dict[str, pd.DataFrame]) -> float | None:
    diagnostics = frames.get("__diagnostics__") if isinstance(frames, dict) else None
    if isinstance(diagnostics, dict):
        tick = diagnostics.get("tick") or {}
        bid = _float(tick.get("bid"))
        ask = _float(tick.get("ask"))
        if bid is not None and ask is not None:
            return round((bid + ask) / 2.0, 8)
        if bid is not None:
            return bid
        if ask is not None:
            return ask
    m5 = frames.get("M5", pd.DataFrame())
    if m5.empty:
        return None
    return _float(m5.iloc[-1].get("close"))


def _data_status(frames: dict[str, pd.DataFrame]) -> str:
    counts = {key: len(value) for key, value in frames.items()}
    required = ["M1", "M5", "M15", "H1", "H4"]
    missing = [key for key in required if counts.get(key, 0) == 0]
    if missing:
        return "MISSING_" + ",".join(missing)
    insufficient = [key for key in required if counts.get(key, 0) < MIN_CANDLES[key]]
    if insufficient:
        return "INSUFFICIENT_" + ",".join(f"{key}={counts.get(key, 0)}/{MIN_CANDLES[key]}" for key in insufficient)
    return "OK:" + ",".join(f"{key}={counts.get(key, 0)}" for key in required)


def _data_reason(frames: dict[str, pd.DataFrame]) -> str:
    status = _data_status(frames)
    if status.startswith("MISSING_M1"):
        return "MISSING_M1_CONTEXT"
    if status.startswith("MISSING_"):
        return status
    if status.startswith("INSUFFICIENT_M1"):
        return "INSUFFICIENT_M1_CANDLES"
    if status.startswith("INSUFFICIENT_"):
        return "INSUFFICIENT_CANDLES"
    return "NO_SANDBOX_SIGNAL"


def _missing_data_reason(frames: dict[str, pd.DataFrame]) -> str | None:
    diagnostics = frames.get("__diagnostics__") if isinstance(frames, dict) else None
    if isinstance(diagnostics, dict) and diagnostics.get("reason") in {"SYMBOL_NOT_FOUND", "SYMBOL_NOT_SELECTED", "MT5_COPY_RATES_EMPTY", "NO_MT5_CANDLES_FOR_SYMBOL"}:
        return str(diagnostics["reason"])
    if len(frames.get("M1", pd.DataFrame())) == 0:
        return "MISSING_M1_CONTEXT"
    if len(frames.get("M1", pd.DataFrame())) < MIN_CANDLES["M1"]:
        return "INSUFFICIENT_M1_CANDLES"
    required = ["M5", "M15", "H1", "H4"]
    missing = [key for key in required if len(frames.get(key, pd.DataFrame())) == 0]
    if missing:
        return "NO_MT5_CANDLES_FOR_SYMBOL"
    insufficient = [key for key in required if len(frames.get(key, pd.DataFrame())) < MIN_CANDLES[key]]
    if insufficient:
        return "INSUFFICIENT_CANDLES"
    return None


def _enrich_symbol_diagnostics(mt5, resolved: str, diagnostics: dict) -> None:
    info = mt5.symbol_info(resolved)
    tick = mt5.symbol_info_tick(resolved)
    diagnostics["symbol_info_available"] = info is not None
    diagnostics["symbol_visible"] = bool(getattr(info, "visible", False)) if info is not None else False
    diagnostics["symbol_trade_mode"] = getattr(info, "trade_mode", None) if info is not None else None
    diagnostics["tick"] = _tick_dict(tick)


def _tick_dict(tick) -> dict:
    if tick is None:
        return {"available": False, "bid": None, "ask": None, "time": None}
    data = tick._asdict() if hasattr(tick, "_asdict") else {
        "bid": getattr(tick, "bid", None),
        "ask": getattr(tick, "ask", None),
        "time": getattr(tick, "time", None),
    }
    return {"available": True, "bid": _float(data.get("bid")), "ask": _float(data.get("ask")), "time": data.get("time")}


def _print_symbol_diagnostics(diagnostics: dict) -> None:
    tick = diagnostics.get("tick") or {}
    print(
        "[BTC_SANDBOX_SYMBOL] requested_symbol=%s resolved_symbol=%s symbol_select=%s "
        "symbol_info=%s visible=%s trade_mode=%s tick_bid=%s tick_ask=%s tick_time=%s reason=%s"
        % (
            diagnostics.get("requested_symbol"),
            diagnostics.get("resolved_symbol"),
            diagnostics.get("symbol_select_result"),
            diagnostics.get("symbol_info_available"),
            diagnostics.get("symbol_visible"),
            diagnostics.get("symbol_trade_mode"),
            tick.get("bid"),
            tick.get("ask"),
            tick.get("time"),
            diagnostics.get("reason"),
        )
    )


def _candle_diag(frame: pd.DataFrame, requested_bars: int, last_error: object = None) -> dict:
    if frame.empty:
        return {
            "requested_bars": requested_bars,
            "received_bars": 0,
            "first_candle_time": None,
            "last_candle_time": None,
            "last_close": None,
            "last_error": last_error,
        }
    return {
        "requested_bars": requested_bars,
        "received_bars": len(frame),
        "first_candle_time": _utc_dt(frame.iloc[0]["time"]).isoformat(),
        "last_candle_time": _utc_dt(frame.iloc[-1]["time"]).isoformat(),
        "last_close": _float(frame.iloc[-1].get("close")),
        "last_error": last_error,
    }


def _print_candle_diag(timeframe: str, diag: dict) -> None:
    print(
        "[BTC_SANDBOX_CANDLES] timeframe=%s requested=%s received=%s first=%s last=%s last_close=%s last_error=%s"
        % (
            timeframe,
            diag.get("requested_bars"),
            diag.get("received_bars"),
            diag.get("first_candle_time"),
            diag.get("last_candle_time"),
            diag.get("last_close"),
            diag.get("last_error"),
        )
    )


def _diagnostic_reason(diagnostics: dict) -> str | None:
    if not diagnostics.get("resolved_symbol"):
        return diagnostics.get("reason") or "SYMBOL_NOT_FOUND"
    if not diagnostics.get("symbol_select_result"):
        return "SYMBOL_NOT_SELECTED"
    candles = diagnostics.get("candles") or {}
    if candles and all((row.get("received_bars") or 0) == 0 for row in candles.values()):
        return "NO_MT5_CANDLES_FOR_SYMBOL"
    if any((row.get("received_bars") or 0) == 0 for row in candles.values()):
        return "MT5_COPY_RATES_EMPTY"
    return None


def _last_error(mt5):
    try:
        return mt5.last_error()
    except Exception:
        return None


def _diag_state(data_status: str) -> str:
    return "OK" if str(data_status or "").startswith("OK:") else "ERROR"


def _should_print_diagnostics(previous_state: str | None, current_state: str, verbose_diag: bool = False) -> bool:
    return bool(verbose_diag or previous_state is None or previous_state != current_state)


def _print_full_diagnostics(diagnostics: dict) -> None:
    _print_symbol_diagnostics(diagnostics)
    for timeframe in ["M1", "M5", "M15", "H1", "H4"]:
        diag = (diagnostics.get("candles") or {}).get(timeframe)
        if diag:
            _print_candle_diag(timeframe, diag)


def _ema_confirmation(symbol: str, frames: dict[str, pd.DataFrame], settings: Settings) -> bool:
    if not settings.btc_weekend_sandbox_ema_confirmation_only:
        return False
    signal = ema_pullback.evaluate(symbol, frames.get("M5", pd.DataFrame()))
    return signal.get("signal") in {"BUY", "SELL"}


def _event(
    symbol: str,
    strategy: str,
    timestamp: datetime,
    decision: dict,
    main_guard: dict,
    big: dict,
    sandbox_action: str,
    reason: str | None,
    ema_confirmation: bool,
    broker_symbol: str | None = None,
    price: float | None = None,
) -> dict:
    rr = _risk_reward(decision)
    main_reason = str(main_guard.get("safety_guard_reason") or "")
    main_would_block = str(main_guard.get("safety_guard_status") or "").upper() == "BLOCK"
    signal = decision.get("signal") or decision.get("direction") or "WAIT"
    strategy_reason = decision.get("reason") or decision.get("blocked_reason") or reason
    return {
        "timestamp": timestamp.isoformat(),
        "price": price if price is not None else decision.get("entry"),
        "symbol": symbol,
        "broker_symbol": broker_symbol,
        "strategy": strategy,
        "signal": signal,
        "direction": signal,
        "strategy_score": _strategy_score(decision),
        "strategy_reason": strategy_reason,
        "entry": decision.get("entry"),
        "sl": decision.get("sl"),
        "tp": decision.get("tp"),
        "rr": rr,
        "smc_score": decision.get("smc_confluence_score"),
        "smc_status": decision.get("smc_confluence_status"),
        "big_setup_score": big.get("big_setup_score") or decision.get("big_setup_score"),
        "big_setup_grade": big.get("big_setup_grade") or decision.get("big_setup_grade"),
        "big_setup_tags": big.get("big_setup_tags") or decision.get("big_setup_tags") or [],
        "ema_confirmation": bool(ema_confirmation),
        "main_safety_guard_would_block": main_would_block,
        "main_safety_guard_reason": main_reason if main_would_block else None,
        "sandbox_decision": sandbox_action,
        "sandbox_reason": reason,
        "pnl_r": None,
        "pnl_estimated_money": None,
    }


def _print_cycle_line(cycle: dict, trades: list[dict], open_trades: int) -> None:
    chosen = cycle.get("chosen") or {}
    best = cycle.get("best") or chosen
    pnl = sum(_float(t.get("pnl_estimated_money") or t.get("pnl_money")) or 0.0 for t in trades)
    price = cycle.get("price") or chosen.get("price") or chosen.get("entry") or "NA"
    reason = chosen.get("sandbox_reason") or _compact_reason(chosen, cycle)
    print(
        "[BTC_SANDBOX] price=%s best=%s score=%s grade=%s final=%s reason=%s open=%s pnl=%.2f"
        % (
            price,
            best.get("strategy") or "WAIT",
            _format_score(best.get("strategy_score")),
            best.get("big_setup_grade") or "UNKNOWN",
            chosen.get("sandbox_decision") or "WAIT",
            reason,
            open_trades,
            pnl,
        )
    )


def _print_strategy_scoreboard(cycle: dict) -> None:
    events = _scoreboard_events(cycle.get("events") or [])
    print("[BTC_SANDBOX_STRATEGIES]")
    for strategy in SANDBOX_STRATEGIES:
        event = next((row for row in events if row.get("strategy") == strategy), None) or {}
        print(
            "%s score=%s signal=%s decision=%s reason=%s"
            % (
                strategy,
                _format_score(event.get("strategy_score")),
                event.get("signal") or event.get("direction") or "WAIT",
                event.get("sandbox_decision") or "WAIT",
                event.get("sandbox_reason") or event.get("strategy_reason") or "NO_VALID_SIGNAL",
            )
        )


def _compact_reason(chosen: dict, cycle: dict) -> str:
    reason = str(chosen.get("sandbox_reason") or "")
    if reason:
        return reason
    data_status = str(cycle.get("data_status") or "")
    if data_status.startswith("MISSING_M1"):
        return "MISSING_M1_CONTEXT"
    if data_status.startswith("INSUFFICIENT_M1"):
        return "INSUFFICIENT_M1_CANDLES"
    if str(chosen.get("sandbox_decision") or "").upper() in {"WAIT", "SKIP_SANDBOX"}:
        return "NO_VALID_SIGNAL"
    return "OK"


def _group_trade_stats(trades: list[dict], field: str) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(field) or "UNKNOWN")].append(trade)
    return {key: _trade_stats(rows) for key, rows in grouped.items()}


def _trade_stats(rows: list[dict]) -> dict:
    pnl_field = lambda row: _float(row.get("pnl_estimated_money") or row.get("pnl_money")) or 0.0
    wins = [row for row in rows if row.get("result") == "WIN"]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len([row for row in rows if row.get("result") == "LOSS"]),
        "win_rate": round(len(wins) / len(rows), 6) if rows else None,
        "pnl_estimated_money": round(sum(pnl_field(row) for row in rows), 6),
    }


def _strategy_score(decision: dict) -> float:
    for field in ("crt_tbs_score", "amd_fvg_score", "fib_ote_score"):
        score = _float(decision.get(field))
        if score is not None:
            return score
    return (_float(decision.get("confidence")) or 0.0) * 100.0


def _active_cycle_strategies(cfg: SandboxConfig) -> list[str]:
    configured = [strategy for strategy in cfg.strategies if strategy in SANDBOX_STRATEGIES]
    return configured or list(SANDBOX_STRATEGIES)


def _scoreboard_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("strategy") in SANDBOX_STRATEGIES]


def _best_candidate_event(events: list[dict]) -> dict | None:
    candidates = _scoreboard_events(events)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda event: (
            _float(event.get("strategy_score")) or 0.0,
            _float(event.get("big_setup_score")) or 0.0,
            1 if event.get("sandbox_decision") == "ENTER_SANDBOX" else 0,
        ),
    )


def _choose_cycle_event(events: list[dict]) -> dict | None:
    entered = next((event for event in events if event.get("sandbox_decision") == "ENTER_SANDBOX"), None)
    return entered or _best_candidate_event(events)


def _skip_reason_rows(events: list[dict]) -> list[dict]:
    counts = Counter(
        (
            str(event.get("strategy") or "UNKNOWN"),
            str(event.get("sandbox_reason") or event.get("strategy_reason") or "UNKNOWN"),
        )
        for event in events
        if event.get("sandbox_decision") != "ENTER_SANDBOX"
    )
    return [{"strategy": strategy, "sandbox_reason": reason, "count": count} for (strategy, reason), count in sorted(counts.items())]


def _format_score(value: object) -> str:
    score = _float(value)
    if score is None:
        return "NA"
    if float(score).is_integer():
        return str(int(score))
    return f"{score:.2f}"


def _grade_ok(actual: str, required: str) -> bool:
    return GRADE_RANK.get(actual.upper(), 0) >= GRADE_RANK.get(required.upper(), 3)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _append_events(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _dict_rows(name: str, rows: dict) -> list[dict]:
    return [{name: key, **value} for key, value in rows.items()]


def _sandbox_strategies(settings: Settings) -> list[str]:
    return [item for item in _csv_arg(settings.btc_weekend_sandbox_strategies) if item in SANDBOX_STRATEGIES]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-only BTC weekend sandbox for HERMES.")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--duration-hours", type=float, default=12.0)
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument("--diag-only", action="store_true")
    parser.add_argument("--verbose-diag", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
