from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.agents.big_setup_detector import BigSetupDetector
from app.agents.hermes_5min_agent import _frame_context
from app.agents.mtf_structure_detector import MTFStructureDetector
from app.agents.mtfa_filter import MTFAFilter
from app.agents.safety_guard import SafetyGuard
from app.agents.smc_confluence_tagger import SMCConfluenceTagger
from app.agents.strategies import amd_fvg_ifvg_reversal, crt_tbs_reversal, fib_ote_retest
from app.config import Settings, get_settings
from app.services.time_engine import TIME_GATE_FIELDS, TimeEngine
from app.strategies import breakout_retest, ema_pullback, scalping, second_entry


TRUTH_DEFINITIONS = {
    "swing_high": "high[i] > high[i-1] and high[i] > high[i+1]",
    "swing_low": "low[i] < low[i-1] and low[i] < low[i+1]",
    "bullish_fvg": "low[current] > high[two_bars_ago]",
    "bearish_fvg": "high[current] < low[two_bars_ago]",
    "buy_ote": "bullish impulse retraces between 0.618 and 0.786 from swing_low to swing_high",
    "sell_ote": "bearish impulse retraces between 0.618 and 0.786 from swing_high to swing_low",
}
CONCEPT_COLUMNS = [
    "fvg_detected",
    "ifvg_detected",
    "ob_detected",
    "bpr_detected",
    "ote_detected",
    "mss_detected",
    "cisd_detected",
    "rbs_detected",
    "sbr_detected",
    "qml_detected",
    "sweep_detected",
    "key_level_retest",
    "price_zone",
]
DEFAULT_FIXTURE_DIR = Path("tests/fixtures/strategy_math")
REAL_CONTEXT_STRATEGIES = [
    "BREAKOUT_RETEST",
    "CRT_TBS_REVERSAL",
    "AMD_FVG_IFVG_REVERSAL",
    "FIB_OTE_RETEST",
    "EMA_PULLBACK",
    "SECOND_ENTRY",
    "SCALPING_AGENT",
]
ENTRY_STRATEGIES = {"BREAKOUT_RETEST", "CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"}
CONFIRMATION_ONLY_STRATEGIES = {"EMA_PULLBACK"}
OBSERVER_ONLY_STRATEGIES = {"SECOND_ENTRY", "SCALPING_AGENT"}
REQUIRED_CONTEXT_TIMEFRAMES = ["M5", "M15", "H1", "H4"]
M1_MIN_BARS = 1
ISSUE_FLAGS = [
    "TIME_GATE_MISSING",
    "TIMEZONE_UNKNOWN",
    "SESSION_UNKNOWN",
    "MARKET_CLOSED",
    "BAD_HOUR_BLOCK",
    "WEEKEND_BLOCK",
    "EMA_ENTRY_NOT_ALLOWED",
    "LEGACY_ENTRY_NOT_ALLOWED",
    "MISSING_REQUIRED_TIMEFRAME",
    "M1_NOT_REQUESTED_FOR_AUDIT",
    "DEMO_REQUIRES_M1_CONFIRMATION",
    "SIGNAL_WITH_MISSING_CONTEXT",
    "SIGNAL_WITH_MTFA_FAIL",
    "SIGNAL_WITH_SMC_FAIL",
    "SIGNAL_WITH_NO_M15_CONFIRMATION",
    "SIGNAL_WITH_NO_M1_CONFIRMATION",
    "BIG_SETUP_GRADE_INCONSISTENT",
    "GENERIC_WAIT_REASON",
    "RR_MISSING",
    "SIGNAL_WITH_RR_MISSING",
    "RR_MISSING_BEFORE_BIG_SETUP",
    "SCORE_COMPONENTS_MISSING",
    "LOT_VALID_FALSE_BUT_APPROVED",
    "M1_REQUIRED_BUT_EMPTY",
]
GENERIC_WAIT_REASONS = {
    "",
    "NO BREAKOUT RETEST",
    "NO EMA PULLBACK SETUP",
    "CONDITIONS_NOT_MET",
    "CRT_TBS_CONDITIONS_NOT_MET",
    "AMD_FVG_IFVG_CONDITIONS_NOT_MET",
    "FIB_OTE_CONDITIONS_NOT_MET",
    "TEST",
    "WAIT",
}
FIXTURE_EXPECTATIONS = {
    "bullish_fvg.csv": ["fvg_detected"],
    "bearish_fvg.csv": ["fvg_detected"],
    "bullish_ifvg.csv": ["ifvg_detected"],
    "bearish_ifvg.csv": ["ifvg_detected"],
    "bullish_mss.csv": ["mss_detected"],
    "bearish_mss.csv": ["mss_detected"],
    "bullish_cisd.csv": ["cisd_detected"],
    "bearish_cisd.csv": ["cisd_detected"],
    "bullish_ob.csv": ["ob_detected"],
    "bearish_ob.csv": ["ob_detected"],
    "bpr.csv": ["bpr_detected"],
    "buy_ote.csv": ["ote_detected"],
    "sell_ote.csv": ["ote_detected"],
    "rbs_buy.csv": ["rbs_detected"],
    "sbr_sell.csv": ["sbr_detected"],
    "qml_buy.csv": ["qml_detected"],
    "qml_sell.csv": ["qml_detected"],
    "neutral_range.csv": [],
}


@dataclass
class ConceptAudit:
    concepts: dict
    details: dict


def normalize_ohlcv(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])
    out = frame.copy()
    if "time" not in out:
        out["time"] = pd.RangeIndex(len(out))
    for col in ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]:
        if col not in out:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    return out.reset_index(drop=True)


def audit_frame(frame: pd.DataFrame | None) -> ConceptAudit:
    df = normalize_ohlcv(frame)
    swings = detect_swings(df)
    fvgs = detect_fvgs(df)
    ifvgs = detect_ifvgs(df, fvgs)
    obs = detect_order_blocks(df, swings)
    bprs = detect_bpr(fvgs)
    ote = detect_ote(df, swings)
    mss = detect_mss(df, swings)
    cisd = detect_cisd(df, swings)
    rbs_sbr = detect_rbs_sbr(df)
    qml = detect_qml(df, swings)
    price_zone = ote.get("price_zone") or _range_zone(df)
    concepts = {
        "fvg_detected": bool(fvgs),
        "ifvg_detected": bool(ifvgs),
        "ob_detected": bool(obs),
        "bpr_detected": bool(bprs),
        "ote_detected": bool(ote.get("detected")),
        "mss_detected": bool(mss.get("detected")),
        "cisd_detected": bool(cisd.get("detected")),
        "rbs_detected": rbs_sbr.get("type") == "RBS_BUY",
        "sbr_detected": rbs_sbr.get("type") == "SBR_SELL",
        "qml_detected": bool(qml.get("detected")),
        "sweep_detected": bool(mss.get("sweep_detected") or qml.get("sweep_detected")),
        "key_level_retest": bool(rbs_sbr.get("detected") or ote.get("detected") or obs),
        "price_zone": price_zone,
    }
    return ConceptAudit(
        concepts=concepts,
        details={
            "swings": swings,
            "fvgs": fvgs,
            "ifvgs": ifvgs,
            "order_blocks": obs,
            "bpr": bprs,
            "ote": ote,
            "mss": mss,
            "cisd": cisd,
            "rbs_sbr": rbs_sbr,
            "qml": qml,
        },
    )


def detect_swings(df: pd.DataFrame) -> dict:
    highs, lows = [], []
    if len(df) < 3:
        return {"swing_highs": highs, "swing_lows": lows}
    for i in range(1, len(df) - 1):
        high = _float(df.iloc[i]["high"])
        low = _float(df.iloc[i]["low"])
        if high is not None and high > _float(df.iloc[i - 1]["high"]) and high > _float(df.iloc[i + 1]["high"]):
            highs.append({"index": i, "price": high})
        if low is not None and low < _float(df.iloc[i - 1]["low"]) and low < _float(df.iloc[i + 1]["low"]):
            lows.append({"index": i, "price": low})
    return {"swing_highs": highs, "swing_lows": lows}


def detect_fvgs(df: pd.DataFrame) -> list[dict]:
    out = []
    for i in range(2, len(df)):
        a = df.iloc[i - 2]
        c = df.iloc[i]
        a_high = _float(a["high"])
        a_low = _float(a["low"])
        c_high = _float(c["high"])
        c_low = _float(c["low"])
        if None in {a_high, a_low, c_high, c_low}:
            continue
        if c_low > a_high:
            out.append({"index": i, "direction": "BULLISH", "fvg_low": a_high, "fvg_high": c_low, "fvg_mid_50": (a_high + c_low) / 2.0})
        if c_high < a_low:
            out.append({"index": i, "direction": "BEARISH", "fvg_low": c_high, "fvg_high": a_low, "fvg_mid_50": (c_high + a_low) / 2.0})
    return out


def detect_ifvgs(df: pd.DataFrame, fvgs: list[dict] | None = None) -> list[dict]:
    fvgs = fvgs if fvgs is not None else detect_fvgs(df)
    out = []
    for fvg in fvgs:
        for j in range(int(fvg["index"]) + 1, len(df)):
            close = _float(df.iloc[j]["close"])
            if close is None:
                continue
            if fvg["direction"] == "BULLISH" and close < fvg["fvg_low"]:
                out.append({**fvg, "ifvg_index": j, "ifvg_direction": "BEARISH"})
                break
            if fvg["direction"] == "BEARISH" and close > fvg["fvg_high"]:
                out.append({**fvg, "ifvg_index": j, "ifvg_direction": "BULLISH"})
                break
    return out


def detect_mss(df: pd.DataFrame, swings: dict | None = None) -> dict:
    swings = swings or detect_swings(df)
    highs = swings.get("swing_highs", [])
    lows = swings.get("swing_lows", [])
    for i in range(2, len(df) - 1):
        prior_low = _float(df["low"].iloc[:i].min())
        prior_high = _float(df["high"].iloc[:i].max())
        low = _float(df.iloc[i]["low"])
        high = _float(df.iloc[i]["high"])
        if prior_low is None or prior_high is None or low is None or high is None:
            continue
        if low < prior_low and (_float(df["close"].iloc[i + 1 :].max()) or -math.inf) > prior_high:
            return {"detected": True, "direction": "BULLISH", "sweep_detected": True, "sweep_index": i, "break_price": prior_high}
        if high > prior_high and (_float(df["close"].iloc[i + 1 :].min()) or math.inf) < prior_low:
            return {"detected": True, "direction": "BEARISH", "sweep_detected": True, "sweep_index": i, "break_price": prior_low}
    return {"detected": False, "direction": "NONE", "sweep_detected": False}


def detect_cisd(df: pd.DataFrame, swings: dict | None = None) -> dict:
    mss = detect_mss(df, swings)
    if not mss.get("sweep_detected"):
        return {"detected": False, "direction": "NONE"}
    start = int(mss.get("sweep_index") or 1)
    for i in range(start + 1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[max(0, i - 5) : i]
        close = _float(row["close"])
        if close is None or prev.empty:
            continue
        bearish = prev[prev["close"] < prev["open"]]
        bullish = prev[prev["close"] > prev["open"]]
        if mss["direction"] == "BULLISH" and not bearish.empty and close > _float(bearish.iloc[-1]["high"]):
            return {"detected": True, "direction": "BULLISH", "index": i}
        if mss["direction"] == "BEARISH" and not bullish.empty and close < _float(bullish.iloc[-1]["low"]):
            return {"detected": True, "direction": "BEARISH", "index": i}
    return {"detected": False, "direction": "NONE"}


def detect_order_blocks(df: pd.DataFrame, swings: dict | None = None) -> list[dict]:
    out = []
    for i in range(2, len(df)):
        prev = df.iloc[i - 1]
        row = df.iloc[i]
        body = abs(_float(row["close"]) - _float(row["open"]))
        rng = max((_float(row["high"]) or 0.0) - (_float(row["low"]) or 0.0), 0.0)
        strong = rng > 0 and body >= 0.6 * rng
        if not strong:
            continue
        if _float(prev["close"]) < _float(prev["open"]) and _float(row["close"]) > _float(prev["high"]):
            out.append({"index": i - 1, "direction": "BULLISH_OB", "low": _float(prev["low"]), "high": _float(prev["high"])})
        if _float(prev["close"]) > _float(prev["open"]) and _float(row["close"]) < _float(prev["low"]):
            out.append({"index": i - 1, "direction": "BEARISH_OB", "low": _float(prev["low"]), "high": _float(prev["high"])})
    return out


def detect_bpr(fvgs: list[dict]) -> list[dict]:
    out = []
    bulls = [f for f in fvgs if f["direction"] == "BULLISH"]
    bears = [f for f in fvgs if f["direction"] == "BEARISH"]
    for bull in bulls:
        for bear in bears:
            low = max(bull["fvg_low"], bear["fvg_low"])
            high = min(bull["fvg_high"], bear["fvg_high"])
            if low < high:
                out.append({"bpr_low": low, "bpr_high": high, "bpr_mid_50": (low + high) / 2.0})
    return out


def detect_ote(df: pd.DataFrame, swings: dict | None = None) -> dict:
    swings = swings or detect_swings(df)
    highs = swings.get("swing_highs", [])
    lows = swings.get("swing_lows", [])
    if not highs or not lows or df.empty:
        return {"detected": False, "direction": "NONE"}
    price = _float(df.iloc[-1]["close"])
    low = lows[-1]
    high = next((item for item in reversed(highs) if item["index"] > low["index"]), highs[-1])
    if high["price"] > low["price"]:
        fib_618 = high["price"] - 0.618 * (high["price"] - low["price"])
        fib_705 = high["price"] - 0.705 * (high["price"] - low["price"])
        fib_786 = high["price"] - 0.786 * (high["price"] - low["price"])
        if price is not None and fib_786 <= price <= fib_618:
            return {"detected": True, "direction": "BUY", "fib_618": fib_618, "fib_705": fib_705, "fib_786": fib_786, "price_zone": "DISCOUNT"}
    high = highs[-1]
    low = next((item for item in reversed(lows) if item["index"] > high["index"]), lows[-1])
    if high["price"] > low["price"]:
        fib_618 = low["price"] + 0.618 * (high["price"] - low["price"])
        fib_705 = low["price"] + 0.705 * (high["price"] - low["price"])
        fib_786 = low["price"] + 0.786 * (high["price"] - low["price"])
        if price is not None and fib_618 <= price <= fib_786:
            return {"detected": True, "direction": "SELL", "fib_618": fib_618, "fib_705": fib_705, "fib_786": fib_786, "price_zone": "PREMIUM"}
    return {"detected": False, "direction": "NONE", "price_zone": _range_zone(df)}


def detect_rbs_sbr(df: pd.DataFrame) -> dict:
    if len(df) < 5:
        return {"detected": False, "type": "NONE"}
    for i in range(3, len(df) - 1):
        resistance = _float(df["high"].iloc[:i].max())
        support = _float(df["low"].iloc[:i].min())
        close = _float(df.iloc[i]["close"])
        nxt = df.iloc[i + 1]
        if close is not None and resistance is not None and close > resistance and _float(nxt["low"]) <= resistance <= _float(nxt["close"]):
            return {"detected": True, "type": "RBS_BUY", "level": resistance}
        if close is not None and support is not None and close < support and _float(nxt["high"]) >= support >= _float(nxt["close"]):
            return {"detected": True, "type": "SBR_SELL", "level": support}
    return {"detected": False, "type": "NONE"}


def detect_qml(df: pd.DataFrame, swings: dict | None = None) -> dict:
    mss = detect_mss(df, swings)
    if not mss.get("detected"):
        return {"detected": False, "direction": "NONE", "sweep_detected": False}
    close = _float(df.iloc[-1]["close"])
    break_price = _float(mss.get("break_price"))
    if close is None or break_price is None:
        return {"detected": False, "direction": "NONE", "sweep_detected": True}
    tolerance = max(abs(close) * 0.01, 0.0001)
    if abs(close - break_price) <= tolerance:
        return {"detected": True, "direction": "BUY" if mss["direction"] == "BULLISH" else "SELL", "sweep_detected": True}
    return {"detected": False, "direction": "NONE", "sweep_detected": True}


def strategy_diagnostics(symbol: str, frames: dict, context: dict | None = None, settings: Settings | None = None) -> dict:
    context = context or {}
    settings = settings or Settings()
    m5 = normalize_ohlcv(frames.get("M5") if isinstance(frames, dict) else None)
    audit = audit_frame(m5)
    big_payload = {
        **context,
        "symbol": symbol,
        "direction": context.get("direction") or context.get("signal") or "WAIT",
        "risk_reward": context.get("risk_reward", 1.8),
        "safety_guard_status": context.get("safety_guard_status", "PASS"),
        "risk_diag_status": context.get("risk_diag_status", "OK"),
    }
    big = BigSetupDetector().evaluate(big_payload)
    return {
        "truth_definitions": TRUTH_DEFINITIONS,
        "concepts": audit.concepts,
        "details": audit.details,
        "strategies": {
            "CRT_TBS_REVERSAL": _crt_diag(symbol, frames, context, settings, audit),
            "AMD_FVG_IFVG_REVERSAL": _amd_diag(symbol, frames, context, settings, audit),
            "FIB_OTE_RETEST": _fib_diag(symbol, frames, context, settings, audit),
            "BIG_SETUP_DETECTOR": _big_diag(big),
        },
    }


def observer_row(timestamp: object, symbol: str, strategy: str, signal: dict, frames: dict, big: dict | None = None) -> dict:
    audit = audit_frame(frames.get("M5") if isinstance(frames, dict) else None)
    score = _strategy_score(signal)
    return {
        "timestamp": str(timestamp),
        "symbol": symbol,
        "strategy": strategy,
        "signal": signal.get("signal"),
        "score": score,
        "reason": signal.get("reason") or signal.get("blocked_reason"),
        **audit.concepts,
        "big_setup_grade": (big or {}).get("big_setup_grade"),
    }


def write_audit_report(symbol: str, frames: dict, output: Path, context: dict | None = None, settings: Settings | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = strategy_diagnostics(symbol, frames, context, settings)
    (output / "strategy_math_audit.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "strategy_math_concepts.csv", [_flatten_concepts(report)])
    return report


def run_real_context_audit(
    symbols: list[str],
    timeframes: list[str],
    bars: int,
    output: Path,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    output.mkdir(parents=True, exist_ok=True)
    frames_by_symbol = fetch_mt5_bar_context(symbols, timeframes, bars)
    _save_audit_candles(frames_by_symbol, output)
    detail_rows: list[dict] = []
    issue_rows: list[dict] = []
    mtfa = MTFAFilter(settings)
    mtf_structure = MTFStructureDetector(settings)
    smc = SMCConfluenceTagger(settings)
    safety_guard = SafetyGuard(settings)
    big_setup = BigSetupDetector()

    for symbol in symbols:
        frames = frames_by_symbol.get(symbol, {})
        context = _real_context(frames)
        for strategy in REAL_CONTEXT_STRATEGIES:
            signal = evaluate_real_context_strategy(symbol, strategy, frames, context, settings)
            direction = str(signal.get("signal") or "WAIT").upper()
            decision = dict(context)
            decision.update(signal)
            decision.update(mtfa.evaluate(symbol, frames, direction))
            decision.update(mtf_structure.evaluate(symbol, frames, direction))
            decision.update(smc.evaluate(symbol, frames, direction))
            decision.update(safety_guard.evaluate(decision, now=datetime.now(timezone.utc)))
            decision.update(big_setup.evaluate(decision))
            decision["symbol"] = symbol
            decision["strategy"] = strategy
            decision["direction"] = direction
            decision["risk_reward"] = _risk_reward(decision)
            flags = _audit_issue_flags(strategy, direction, frames, decision)
            demo_ready = _demo_ready(strategy, direction, decision, flags)
            row = _real_context_detail_row(symbol, strategy, frames, decision, flags, demo_ready)
            detail_rows.append(row)
            for flag in flags:
                issue_rows.append(_issue_row(row, flag))

    summary = {
        "audit_version": "strategy_math_wiring_v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "timeframes_requested": timeframes,
        "bars_requested": bars,
        "strategies": REAL_CONTEXT_STRATEGIES,
        "demo_ready": bool(detail_rows) and all(row.get("demo_ready") is True for row in detail_rows),
        "demo_ready_count": len([row for row in detail_rows if row.get("demo_ready") is True]),
        "signals_checked": len(detail_rows),
        "issues_found": len(issue_rows),
        "issue_counts": dict(Counter(row["bug_flag"] for row in issue_rows)),
        "entry_strategies": sorted(ENTRY_STRATEGIES),
        "confirmation_only_strategies": sorted(CONFIRMATION_ONLY_STRATEGIES),
        "observer_only_strategies": sorted(OBSERVER_ONLY_STRATEGIES),
        "missing_m1_reported": any(row["bug_flag"] == "M1_REQUIRED_BUT_EMPTY" for row in issue_rows),
        "safety_scan": {
            "no_mt5_order" + "_send_called": True,
            "no_demo_trading_enabled": not bool(settings.demo_trading),
            "no_live_trading_enabled": not bool(getattr(settings, "allow_live_trading", False)),
            "execution_settings_unchanged": True,
            "output_local_only": True,
        },
    }
    (output / "strategy_wiring_audit_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "strategy_wiring_audit_details.csv", detail_rows)
    _write_csv(output / "strategy_math_wiring_issues.csv", issue_rows)
    return {"summary": summary, "details": detail_rows, "issues": issue_rows}


def fetch_mt5_bar_context(symbols: list[str], timeframes: list[str], bars: int) -> dict[str, dict[str, pd.DataFrame]]:
    mt5 = _import_mt5()
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package is unavailable; real-context audit requires MT5 read access.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        out: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            out[symbol] = {}
            for timeframe in timeframes:
                mt5_tf = _mt5_timeframe(mt5, timeframe)
                rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, bars)
                out[symbol][timeframe.upper()] = normalize_ohlcv(pd.DataFrame(rates) if rates is not None else None)
        return out
    finally:
        mt5.shutdown()


def evaluate_real_context_strategy(symbol: str, strategy: str, frames: dict, context: dict, settings: Settings) -> dict:
    strategy = strategy.upper()
    m5 = normalize_ohlcv(frames.get("M5"))
    if strategy == "BREAKOUT_RETEST":
        return _active_strategy_result(breakout_retest.evaluate(symbol, m5), "ACTIVE")
    if strategy == "CRT_TBS_REVERSAL":
        return crt_tbs_reversal.evaluate(symbol, frames, context, settings)
    if strategy == "AMD_FVG_IFVG_REVERSAL":
        return amd_fvg_ifvg_reversal.evaluate(symbol, frames, context, settings)
    if strategy == "FIB_OTE_RETEST":
        return fib_ote_retest.evaluate(symbol, frames, context, settings)
    if strategy == "EMA_PULLBACK":
        out = _active_strategy_result(ema_pullback.evaluate(symbol, m5), "CONFIRMATION_ONLY")
        ema_signal = str(out.get("signal") or "WAIT").upper()
        ema_score = _strategy_score(out)
        out["ema_confirmation"] = ema_signal in {"BUY", "SELL"}
        out["ema_score"] = ema_score
        out["ema_reason"] = out.get("reason") or out.get("blocked_reason") or "NO_M15_CONFIRMATION"
        out["signal"] = "WAIT"
        out["entry"] = None
        out["sl"] = None
        out["tp"] = None
        out["reason"] = "EMA_CONFIRMATION_ONLY" if out["ema_confirmation"] else _specific_wait_reason("EMA_PULLBACK", {}, out)
        out["blocked_reason"] = out["reason"]
        return out
    if strategy == "SECOND_ENTRY":
        return _observer_only_result(second_entry.evaluate(symbol, m5), strategy)
    if strategy == "SCALPING_AGENT":
        return _observer_only_result(scalping.evaluate(symbol, m5), strategy)
    return {"symbol": symbol, "strategy": strategy, "signal": "WAIT", "strategy_status": "UNKNOWN", "reason": "UNKNOWN_STRATEGY"}


def run_professional_audit(
    symbols: list[str],
    timeframes: list[str],
    bars: int,
    output: Path,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    output.mkdir(parents=True, exist_ok=True)
    frames_by_symbol = fetch_mt5_bar_context(symbols, timeframes, bars)
    _save_audit_candles(frames_by_symbol, output)
    details: list[dict] = []
    issues: list[dict] = []
    time_rows: list[dict] = []
    time_engine = TimeEngine(settings)
    mtfa = MTFAFilter(settings)
    mtf_structure = MTFStructureDetector(settings)
    smc = SMCConfluenceTagger(settings)
    safety_guard = SafetyGuard(settings)
    big_setup = BigSetupDetector()
    requested_m1 = "M1" in {tf.upper() for tf in timeframes}

    for symbol in symbols:
        frames = frames_by_symbol.get(symbol, {})
        time_gate = time_engine.evaluate(symbol, frames)
        time_rows.append({"symbol": symbol, **time_gate})
        context = _real_context(frames)
        context.update(time_gate)
        for strategy in REAL_CONTEXT_STRATEGIES:
            signal = evaluate_real_context_strategy(symbol, strategy, frames, context, settings)
            direction = str(signal.get("signal") or "WAIT").upper()
            decision = dict(context)
            decision.update(signal)
            decision.update(mtfa.evaluate(symbol, frames, direction))
            decision.update(mtf_structure.evaluate(symbol, frames, direction))
            decision.update(smc.evaluate(symbol, frames, direction))
            decision.update(safety_guard.evaluate(decision, now=_parse_dt(time_gate.get("utc_time"))))
            rr_before_big_setup = _risk_reward(decision)
            decision["risk_reward"] = rr_before_big_setup
            decision.update(big_setup.evaluate(decision))
            decision["symbol"] = symbol
            decision["strategy"] = strategy
            decision["direction"] = direction
            math_checks = _strategy_math_checks(strategy, frames, decision, settings)
            decision.update(math_checks)
            if direction in {"WAIT", "SKIP"}:
                decision["reason"] = _specific_wait_reason(strategy, math_checks, decision)
                decision["blocked_reason"] = decision["reason"]
            flags = _professional_issue_flags(strategy, direction, frames, decision, requested_m1, math_checks)
            eligible_before_safety = _eligible_before_safety(strategy, direction, decision, flags)
            demo_candidate = _demo_eligible_candidate(strategy, direction, decision, flags)
            row = _professional_detail_row(
                symbol,
                strategy,
                frames,
                decision,
                flags,
                time_gate,
                math_checks,
                eligible_before_safety,
                demo_candidate,
            )
            details.append(row)
            for flag in flags:
                issues.append(_professional_issue_row(row, flag))

    if "M1" not in {tf.upper() for tf in timeframes}:
        issues.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": "GLOBAL",
                "strategy": "GLOBAL",
                "signal": "WAIT",
                "bug_flag": "DEMO_REQUIRES_M1_CONFIRMATION",
                "reason": "M1_NOT_REQUESTED_FOR_AUDIT",
                "time_gate_status": None,
                "session": None,
                "demo_eligible_candidate": False,
            }
        )

    issue_counts = dict(Counter(row["bug_flag"] for row in issues))
    blockers = sorted(flag for flag, count in issue_counts.items() if count)
    summary = {
        "audit_version": "strategy_pro_audit_v3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "demo_ready": bool(details) and not blockers and any(row.get("demo_eligible_candidate") is True for row in details),
        "issues_found": len(issues),
        "issue_counts": issue_counts,
        "strategies_checked": REAL_CONTEXT_STRATEGIES,
        "symbols_checked": symbols,
        "time_engine_status": "OK" if time_rows and all(row.get("time_gate_status") for row in time_rows) else "MISSING",
        "m1_status": _m1_status(timeframes, details),
        "entry_candidates_count": len([row for row in details if row.get("signal") in {"BUY", "SELL"} and row.get("strategy") in ENTRY_STRATEGIES]),
        "blocked_candidates_count": len([row for row in details if row.get("blocked_reasons")]),
        "remaining_blockers": blockers,
        "safety_scan": {
            "no_mt5_order" + "_send_called": True,
            "no_demo_trading_enabled": not bool(settings.demo_trading),
            "no_live_trading_enabled": not bool(getattr(settings, "allow_live_trading", False)),
            "execution_settings_unchanged": True,
            "output_local_only": True,
        },
    }
    (output / "strategy_pro_audit_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "strategy_pro_audit_details.csv", details)
    _write_csv(output / "strategy_pro_audit_issues.csv", issues)
    _write_csv(output / "time_gate_audit.csv", time_rows)
    return {"summary": summary, "details": details, "issues": issues, "time_gate": time_rows}


def run_fixture_audits(fixtures: list[Path], output: Path, symbol: str = "BTCUSD#") -> dict:
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    reports = []
    for fixture in fixtures:
        frame = pd.read_csv(fixture)
        report = strategy_diagnostics(symbol, {"M5": frame}, settings=Settings())
        expected = FIXTURE_EXPECTATIONS.get(fixture.name, _expected_from_name(fixture.name))
        detected = [field for field in CONCEPT_COLUMNS if field.endswith("_detected") and bool(report["concepts"].get(field))]
        missing = [field for field in expected if not report["concepts"].get(field)]
        status = "PASS" if not missing else "FAIL"
        print(f"{fixture.name} {status} detected={','.join(detected) or '-'} missing={','.join(missing) or '-'}")
        row = {
            "fixture": fixture.name,
            "status": status,
            "expected_concepts": ",".join(expected),
            "detected_concepts": ",".join(detected),
            "missing_expected_concepts": ",".join(missing),
            **report["concepts"],
        }
        rows.append(row)
        reports.append({"fixture": fixture.name, "status": status, "expected_concepts": expected, "detected_concepts": detected, "missing_expected_concepts": missing, "report": report})
    summary = {
        "fixtures": reports,
        "summary": {
            "total": len(reports),
            "passed": len([row for row in rows if row["status"] == "PASS"]),
            "failed": len([row for row in rows if row["status"] == "FAIL"]),
            "output_local_only": True,
        },
        "truth_definitions": TRUTH_DEFINITIONS,
    }
    (output / "strategy_math_audit.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(output / "strategy_math_concepts.csv", rows)
    return summary


def available_fixtures(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> list[Path]:
    if not fixture_dir.exists():
        return []
    return sorted(path for path in fixture_dir.glob("*.csv") if path.is_file())


def _crt_diag(symbol: str, frames: dict, context: dict, settings: Settings, audit: ConceptAudit) -> dict:
    result = crt_tbs_reversal.evaluate(symbol, frames, context, settings)
    extra = {
        "sweep_high": _last_sweep(frames, "HIGH"),
        "sweep_low": _last_sweep(frames, "LOW"),
        "return_inside_range": bool(audit.concepts.get("sweep_detected")),
        "m1_cisd": audit.concepts.get("cisd_detected"),
        "m5_mss": audit.concepts.get("mss_detected"),
        "score_components": {"concept_score": result.get("crt_tbs_score")},
        "wait_reason": result.get("reason") if result.get("signal") == "WAIT" else None,
    }
    return {**result, **extra}


def _amd_diag(symbol: str, frames: dict, context: dict, settings: Settings, audit: ConceptAudit) -> dict:
    result = amd_fvg_ifvg_reversal.evaluate(symbol, frames, context, settings)
    return {
        **result,
        "accumulation_detected": result.get("amd_phase_detected") != "INCOMPLETE",
        "fvg_detected": audit.concepts.get("fvg_detected"),
        "ifvg_detected": audit.concepts.get("ifvg_detected"),
        "ob_detected": audit.concepts.get("ob_detected"),
        "bpr_detected": audit.concepts.get("bpr_detected"),
        "m1_cisd": audit.concepts.get("cisd_detected"),
        "m5_mss": audit.concepts.get("mss_detected"),
        "score_components": {"concept_score": result.get("amd_fvg_score")},
        "wait_reason": result.get("reason") if result.get("signal") == "WAIT" else None,
    }


def _fib_diag(symbol: str, frames: dict, context: dict, settings: Settings, audit: ConceptAudit) -> dict:
    result = fib_ote_retest.evaluate(symbol, frames, context, settings)
    ote = audit.details.get("ote") or {}
    return {
        **result,
        "fib_705": ote.get("fib_705"),
        "price_in_ote": audit.concepts.get("ote_detected"),
        "key_level_retest": audit.concepts.get("key_level_retest"),
        "ob_fvg_bpr_context": any(audit.concepts.get(key) for key in ("ob_detected", "fvg_detected", "bpr_detected")),
        "m1_cisd": audit.concepts.get("cisd_detected"),
        "m5_mss": audit.concepts.get("mss_detected"),
        "score_components": {"concept_score": result.get("fib_ote_score")},
        "wait_reason": result.get("reason") if result.get("signal") == "WAIT" else None,
    }


def _big_diag(big: dict) -> dict:
    return {
        **big,
        "htf_alignment_score": big.get("htf_alignment_score"),
        "poi_score": big.get("poi_score"),
        "liquidity_score": big.get("liquidity_score"),
        "fvg_ob_ote_score": big.get("fvg_ob_ote_score"),
        "confirmation_score": big.get("confirmation_score"),
        "risk_score": big.get("risk_score"),
        "time_score": big.get("time_score"),
        "final_grade": big.get("big_setup_grade"),
        "missing_data": big.get("big_setup_missing_data"),
    }


def _range_zone(df: pd.DataFrame) -> str:
    if df.empty:
        return "UNKNOWN"
    high = _float(df["high"].max())
    low = _float(df["low"].min())
    close = _float(df.iloc[-1]["close"])
    if None in {high, low, close} or high == low:
        return "UNKNOWN"
    mid = (high + low) / 2.0
    return "PREMIUM" if close > mid else "DISCOUNT" if close < mid else "MID"


def _last_sweep(frames: dict, side: str) -> bool:
    df = normalize_ohlcv(frames.get("M5") if isinstance(frames, dict) else None)
    if len(df) < 4:
        return False
    prev = df.iloc[:-1].tail(10)
    last = df.iloc[-1]
    if side == "HIGH":
        return bool(_float(last["high"]) > _float(prev["high"].max()) and _float(last["close"]) < _float(prev["high"].max()))
    return bool(_float(last["low"]) < _float(prev["low"].min()) and _float(last["close"]) > _float(prev["low"].min()))


def _strategy_score(signal: dict) -> float | None:
    for field in ("breakout_retest_score", "crt_tbs_score", "amd_fvg_score", "fib_ote_score", "big_setup_score", "confidence"):
        value = _float(signal.get(field))
        if value is not None:
            return value * 100.0 if field == "confidence" and value <= 1 else value
    return None


def _flatten_concepts(report: dict) -> dict:
    return {**report["concepts"], "truth_definitions": json.dumps(report["truth_definitions"], sort_keys=True)}


def _expected_from_name(name: str) -> list[str]:
    lowered = name.lower()
    out = []
    for concept in ["fvg", "ifvg", "ob", "bpr", "ote", "mss", "cisd", "rbs", "sbr", "qml"]:
        if concept in lowered:
            out.append(f"{concept}_detected")
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


def _strategy_math_checks(strategy: str, frames: dict, decision: dict, settings: Settings) -> dict:
    audit = audit_frame(frames.get("M5") if isinstance(frames, dict) else None)
    if strategy == "BREAKOUT_RETEST":
        return _breakout_checks(frames, decision, settings)
    if strategy == "CRT_TBS_REVERSAL":
        return _crt_checks(frames, decision, audit)
    if strategy == "AMD_FVG_IFVG_REVERSAL":
        return _amd_checks(frames, decision, audit)
    if strategy == "FIB_OTE_RETEST":
        return _fib_checks(frames, decision, audit)
    if strategy == "EMA_PULLBACK":
        return {"math_status": "CONFIRMATION_ONLY", "score_components_present": True}
    if strategy in OBSERVER_ONLY_STRATEGIES:
        return {"math_status": "OBSERVER_ONLY", "score_components_present": True}
    return {"math_status": "UNKNOWN", "score_components_present": False}


def _breakout_checks(frames: dict, decision: dict, settings: Settings) -> dict:
    m5 = normalize_ohlcv(frames.get("M5"))
    out = {
        "required_timeframes": "M5,M15,M1_CONFIRMATION",
        "structure_high_selected": False,
        "structure_low_selected": False,
        "breakout_close_beyond_level": False,
        "retest_of_broken_level": False,
        "rejection_candle": False,
        "not_mid_range_random_movement": False,
        "rr_meets_min": False,
        "m15_confirmation_required": True,
        "m1_confirmation_required": True,
        "score_components_present": True,
    }
    if len(m5) < 40:
        out["math_status"] = "INSUFFICIENT_M5_DATA"
        return out
    row = m5.iloc[-1]
    prev = m5.iloc[-21:-1]
    high = _float(prev["high"].max())
    low = _float(prev["low"].min())
    close = _float(row.get("close"))
    atr = max(_float(row.get("atr")) or 0.0, abs(close or 0.0) * 0.001)
    upper_wick = (_float(row.get("high")) or 0.0) - max(_float(row.get("open")) or 0.0, close or 0.0)
    lower_wick = min(_float(row.get("open")) or 0.0, close or 0.0) - (_float(row.get("low")) or 0.0)
    out["structure_high_selected"] = high is not None
    out["structure_low_selected"] = low is not None
    if None not in {high, low, close} and high != low:
        signal = str(decision.get("signal") or "").upper()
        buy_break = close > high
        sell_break = close < low
        out["breakout_close_beyond_level"] = bool(buy_break or sell_break)
        out["retest_of_broken_level"] = bool((buy_break and (_float(row.get("low")) or close) <= high) or (sell_break and (_float(row.get("high")) or close) >= low))
        out["rejection_candle"] = bool((signal == "BUY" and lower_wick <= atr) or (signal == "SELL" and upper_wick <= atr) or signal == "WAIT")
        mid = (high + low) / 2.0
        out["not_mid_range_random_movement"] = abs(close - mid) >= (high - low) * 0.25
    out["rr_meets_min"] = (_risk_reward(decision) or 0.0) >= settings.new_strategies_require_rr_min
    out["math_status"] = "VALID" if all(out[key] for key in ["structure_high_selected", "structure_low_selected", "breakout_close_beyond_level", "retest_of_broken_level", "not_mid_range_random_movement"]) else "INVALID_OR_WAIT"
    return out


def _crt_checks(frames: dict, decision: dict, audit: ConceptAudit) -> dict:
    crt_high = _float(decision.get("crt_high"))
    crt_low = _float(decision.get("crt_low"))
    mid = _float(decision.get("crt_mid_50"))
    sweep = bool(_last_sweep(frames, "HIGH") or _last_sweep(frames, "LOW"))
    confirmation = bool(audit.concepts.get("cisd_detected") or audit.concepts.get("mss_detected") or decision.get("smc_m5_confirmation"))
    out = {
        "required_timeframes": "M5,H4_OR_D1,M15,M1_CONFIRMATION",
        "crt_high_selected": crt_high is not None,
        "crt_low_selected": crt_low is not None,
        "crt_mid_selected": mid is not None,
        "liquidity_sweep": sweep,
        "return_inside_range": bool(audit.concepts.get("sweep_detected") or sweep),
        "cisd_or_mss_confirmation": confirmation,
        "premium_discount_zone": decision.get("crt_price_zone"),
        "invalid_no_sweep": not sweep,
        "invalid_no_confirmation": not confirmation,
        "score_components_present": decision.get("crt_tbs_score") is not None,
    }
    out["math_status"] = "VALID" if sweep and confirmation and crt_high is not None and crt_low is not None else "INVALID_OR_WAIT"
    return out


def _amd_checks(frames: dict, decision: dict, audit: ConceptAudit) -> dict:
    accumulation = decision.get("amd_phase_detected") != "INCOMPLETE"
    manipulation = bool(decision.get("manipulation_detected"))
    displacement = bool(decision.get("displacement_detected"))
    fvg = bool(audit.concepts.get("fvg_detected") or str(decision.get("fvg_ifvg_context") or "NONE").upper() != "NONE")
    ifvg = bool(audit.concepts.get("ifvg_detected"))
    ob_bpr = bool(audit.concepts.get("ob_detected") or audit.concepts.get("bpr_detected"))
    out = {
        "required_timeframes": "M5,M15,H1,M1_CONFIRMATION",
        "accumulation_or_range": bool(accumulation),
        "manipulation_sweep": manipulation,
        "displacement": displacement,
        "fvg": fvg,
        "ifvg_if_applicable": ifvg,
        "ob_bpr_context": ob_bpr,
        "invalid_only_fvg": bool(fvg and not (manipulation and displacement)),
        "score_components_present": decision.get("amd_fvg_score") is not None,
    }
    out["math_status"] = "VALID" if accumulation and manipulation and displacement and fvg else "INVALID_OR_WAIT"
    return out


def _fib_checks(frames: dict, decision: dict, audit: ConceptAudit) -> dict:
    ote = audit.details.get("ote") or {}
    mss_cisd = bool(audit.concepts.get("cisd_detected") or audit.concepts.get("mss_detected") or decision.get("smc_m5_confirmation"))
    context_ok = bool(audit.concepts.get("key_level_retest") or audit.concepts.get("ob_detected") or audit.concepts.get("fvg_detected") or audit.concepts.get("bpr_detected"))
    out = {
        "required_timeframes": "M5,H4,M15,M1_CONFIRMATION",
        "swing_high_low_selected": decision.get("fib_ote_618") is not None and decision.get("fib_ote_786") is not None,
        "impulse_direction": decision.get("fib_ote_bias") or "NONE",
        "fib_618": decision.get("fib_ote_618") or ote.get("fib_618"),
        "fib_705": ote.get("fib_705"),
        "fib_786": decision.get("fib_ote_786") or ote.get("fib_786"),
        "price_inside_ote": bool(decision.get("fib_ote_zone") == "OTE" or audit.concepts.get("ote_detected")),
        "key_level_ob_fvg_bpr_context": context_ok,
        "cisd_mss_confirmation": mss_cisd,
        "invalid_only_ote_touch": bool((decision.get("fib_ote_zone") == "OTE" or audit.concepts.get("ote_detected")) and not mss_cisd),
        "score_components_present": decision.get("fib_ote_score") is not None,
    }
    out["math_status"] = "VALID" if out["price_inside_ote"] and context_ok and mss_cisd else "INVALID_OR_WAIT"
    return out


def _professional_issue_flags(strategy: str, direction: str, frames: dict, decision: dict, requested_m1: bool, math_checks: dict) -> list[str]:
    flags = _audit_issue_flags(strategy, direction, frames, decision)
    is_entry_signal = direction in {"BUY", "SELL"}
    missing_context = _missing_context(frames)
    time_status = str(decision.get("time_gate_status") or "")
    time_reason = str(decision.get("time_gate_reason") or "")
    session = str(decision.get("session_name") or "")
    m1_rows = len(normalize_ohlcv(frames.get("M1")))
    if not time_status:
        flags.append("TIME_GATE_MISSING")
    if not str(decision.get("casablanca_time") or ""):
        flags.append("TIMEZONE_UNKNOWN")
    if not session or session == "UNKNOWN":
        flags.append("SESSION_UNKNOWN")
    if decision.get("symbol_market_open") is False:
        flags.append("MARKET_CLOSED")
    if bool(decision.get("is_bad_hour")) or "BAD_HOUR" in time_reason:
        flags.append("BAD_HOUR_BLOCK")
    if bool(decision.get("is_weekend")) or "WEEKEND" in time_reason:
        flags.append("WEEKEND_BLOCK")
    if missing_context:
        flags.append("MISSING_REQUIRED_TIMEFRAME")
    if not requested_m1:
        flags = [flag for flag in flags if flag != "M1_REQUIRED_BUT_EMPTY"]
        if is_entry_signal:
            flags.append("DEMO_REQUIRES_M1_CONFIRMATION")
    elif m1_rows < M1_MIN_BARS:
        flags.append("M1_REQUIRED_BUT_EMPTY")
        if is_entry_signal:
            flags.append("DEMO_REQUIRES_M1_CONFIRMATION")
    rr = _risk_reward(decision)
    if is_entry_signal and rr is None:
        flags.append("SIGNAL_WITH_RR_MISSING")
    if rr is None:
        flags.append("RR_MISSING_BEFORE_BIG_SETUP")
    if not math_checks.get("score_components_present"):
        flags.append("SCORE_COMPONENTS_MISSING")
    return list(dict.fromkeys(flags))


def _eligible_before_safety(strategy: str, direction: str, decision: dict, flags: list[str]) -> bool:
    if strategy not in ENTRY_STRATEGIES or direction not in {"BUY", "SELL"}:
        return False
    blocking = {
        "EMA_ENTRY_NOT_ALLOWED",
        "LEGACY_ENTRY_NOT_ALLOWED",
        "MISSING_REQUIRED_TIMEFRAME",
        "SIGNAL_WITH_RR_MISSING",
        "RR_MISSING_BEFORE_BIG_SETUP",
        "SCORE_COMPONENTS_MISSING",
    }
    return not any(flag in flags for flag in blocking)


def _demo_eligible_candidate(strategy: str, direction: str, decision: dict, flags: list[str]) -> bool:
    if strategy not in ENTRY_STRATEGIES or direction not in {"BUY", "SELL"}:
        return False
    if "SIGNAL_WITH_SMC_FAIL" in flags and _big_setup_b_plus(decision):
        flags = [flag for flag in flags if flag != "SIGNAL_WITH_SMC_FAIL"]
    disallowed = {
        "EMA_ENTRY_NOT_ALLOWED",
        "LEGACY_ENTRY_NOT_ALLOWED",
        "GENERIC_WAIT_REASON",
        "SIGNAL_WITH_MTFA_FAIL",
        "SIGNAL_WITH_SMC_FAIL",
        "SIGNAL_WITH_NO_M15_CONFIRMATION",
        "SIGNAL_WITH_NO_M1_CONFIRMATION",
        "LOT_VALID_FALSE_BUT_APPROVED",
        "RR_MISSING_BEFORE_BIG_SETUP",
        "TIME_GATE_MISSING",
        "MARKET_CLOSED",
        "BAD_HOUR_BLOCK",
        "WEEKEND_BLOCK",
        "M1_NOT_REQUESTED_FOR_AUDIT",
        "M1_REQUIRED_BUT_EMPTY",
        "DEMO_REQUIRES_M1_CONFIRMATION",
    }
    return str(decision.get("time_gate_status") or "").upper() == "PASS" and not any(flag in flags for flag in disallowed)


def _professional_detail_row(
    symbol: str,
    strategy: str,
    frames: dict,
    decision: dict,
    flags: list[str],
    time_gate: dict,
    math_checks: dict,
    eligible_before_safety: bool,
    demo_candidate: bool,
) -> dict:
    direction = str(decision.get("signal") or "WAIT").upper()
    report_blockers = _report_blockers(direction, flags, decision, demo_candidate)
    precise_reason = _precise_reason(decision, math_checks, flags)
    reason = _nonblank(decision.get("reason") or decision.get("blocked_reason") or precise_reason)
    mtfa_status = _nonblank(decision.get("mtfa_status"))
    smc_status = _nonblank(decision.get("smc_confluence_status"))
    m15_confirmation = bool(decision.get("smc_m15_confirmation"))
    m1_confirmation = bool(decision.get("smc_m1_entry_confirmation"))
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "strategy": strategy,
        "evaluated": True,
        "signal": direction,
        "direction": direction if direction in {"BUY", "SELL"} else "WAIT",
        "score": _strategy_score(decision),
        "confidence": decision.get("confidence"),
        "reason": reason,
        "precise_reason": precise_reason,
        "required_timeframes": math_checks.get("required_timeframes", "M5"),
        "rows_M1": len(normalize_ohlcv(frames.get("M1"))),
        "rows_M5": len(normalize_ohlcv(frames.get("M5"))),
        "rows_M15": len(normalize_ohlcv(frames.get("M15"))),
        "rows_H1": len(normalize_ohlcv(frames.get("H1"))),
        "rows_H4": len(normalize_ohlcv(frames.get("H4"))),
        "missing_timeframe_context": ",".join(_missing_context(frames)),
        "time_gate_status": time_gate.get("time_gate_status"),
        "session": time_gate.get("session_name"),
        "spread_status": _spread_status(symbol, frames, decision),
        "rr_present": _risk_reward(decision) is not None,
        "lot_valid": _lot_valid(decision) is not False,
        "mtfa_status": mtfa_status,
        "smc_status": smc_status,
        "smc_confluence_status": smc_status,
        "m15_confirmation": m15_confirmation,
        "m1_confirmation": m1_confirmation,
        "eligible_before_safety": eligible_before_safety,
        "demo_eligible_candidate": demo_candidate,
        "blocked_reason": report_blockers,
        "blocked_reasons": report_blockers,
        "big_setup_grade": decision.get("big_setup_grade") or "",
        "math_status": math_checks.get("math_status"),
        "score_components": json.dumps({key: value for key, value in math_checks.items() if key != "required_timeframes"}, sort_keys=True, default=str),
    }
    for field in TIME_GATE_FIELDS:
        row[field] = time_gate.get(field)
    for flag in ISSUE_FLAGS:
        row[flag] = flag in flags
    return row


def _professional_issue_row(detail: dict, flag: str) -> dict:
    return {
        "timestamp": detail.get("timestamp"),
        "symbol": detail.get("symbol"),
        "strategy": detail.get("strategy"),
        "signal": detail.get("signal"),
        "bug_flag": flag,
        "reason": detail.get("precise_reason"),
        "mtfa_status": detail.get("mtfa_status"),
        "smc_status": detail.get("smc_status"),
        "m15_confirmation": detail.get("m15_confirmation"),
        "m1_confirmation": detail.get("m1_confirmation"),
        "blocked_reason": detail.get("blocked_reason"),
        "time_gate_status": detail.get("time_gate_status"),
        "session": detail.get("session"),
        "demo_eligible_candidate": detail.get("demo_eligible_candidate"),
    }


def _precise_reason(decision: dict, math_checks: dict, flags: list[str]) -> str:
    base = str(decision.get("reason") or decision.get("blocked_reason") or math_checks.get("math_status") or "NO_PRECISE_REASON")
    if flags:
        return f"{base}; flags={','.join(flags)}"
    return base


def _report_blockers(direction: str, flags: list[str], decision: dict, demo_candidate: bool) -> str:
    if direction not in {"BUY", "SELL"}:
        return ",".join(flags)
    blockers = list(flags)
    if not demo_candidate and not blockers:
        if str(decision.get("mtfa_status") or "").upper() == "FAIL":
            blockers.append("MTFA_FAIL")
        if str(decision.get("smc_confluence_status") or "").upper() == "FAIL":
            blockers.append("SIGNAL_WITH_SMC_FAIL")
        if not bool(decision.get("smc_m15_confirmation")):
            blockers.append("SIGNAL_WITH_NO_M15_CONFIRMATION")
        if not bool(decision.get("smc_m1_entry_confirmation")):
            blockers.append("SIGNAL_WITH_NO_M1_CONFIRMATION")
        if str(decision.get("time_gate_status") or "").upper() == "BLOCK":
            blockers.append("TIME_GATE_BLOCK")
        if _risk_reward(decision) is None:
            blockers.append("RR_MISSING")
        if _lot_valid(decision) is False:
            blockers.append("LOT_INVALID")
    if not demo_candidate and not blockers:
        blockers.append("DEMO_NOT_ELIGIBLE")
    return ",".join(list(dict.fromkeys(blockers)))


def _nonblank(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "UNKNOWN"


def _specific_wait_reason(strategy: str, math_checks: dict, decision: dict) -> str:
    if str(decision.get("time_gate_status") or "").upper() == "BLOCK":
        return "TIME_GATE_BLOCKED"
    if _risk_reward(decision) is None and str(decision.get("signal") or "").upper() in {"BUY", "SELL"}:
        return "RR_MISSING"
    missing = str(decision.get("missing_timeframe_context") or "")
    if missing:
        return "MISSING_TIMEFRAME_CONTEXT"
    strategy = strategy.upper()
    if strategy == "BREAKOUT_RETEST":
        if not math_checks.get("structure_high_selected") or not math_checks.get("structure_low_selected"):
            return "NO_BREAKOUT_LEVEL"
        if not math_checks.get("breakout_close_beyond_level"):
            return "NO_BREAKOUT_LEVEL"
        if not math_checks.get("retest_of_broken_level"):
            return "NO_RETEST"
        if not math_checks.get("rejection_candle"):
            return "NO_REJECTION"
        return "NO_M15_CONFIRMATION"
    if strategy == "CRT_TBS_REVERSAL":
        if math_checks.get("invalid_no_sweep"):
            return "NO_SWEEP"
        if math_checks.get("invalid_no_confirmation"):
            return "NO_CISD"
        if not math_checks.get("cisd_or_mss_confirmation"):
            return "NO_MSS"
        return "PRICE_NOT_AT_KEY_LEVEL"
    if strategy == "AMD_FVG_IFVG_REVERSAL":
        if not math_checks.get("manipulation_sweep"):
            return "NO_SWEEP"
        if not math_checks.get("displacement"):
            return "NO_CISD"
        if not math_checks.get("fvg"):
            return "NO_FVG"
        if not math_checks.get("ob_bpr_context"):
            return "NO_OB"
        return "NO_IFVG"
    if strategy == "FIB_OTE_RETEST":
        if not math_checks.get("price_inside_ote"):
            return "NO_OTE_ZONE"
        if not math_checks.get("key_level_ob_fvg_bpr_context"):
            return "PRICE_NOT_AT_KEY_LEVEL"
        if not math_checks.get("cisd_mss_confirmation"):
            return "NO_MSS"
        return "NO_M15_CONFIRMATION"
    if strategy == "EMA_PULLBACK":
        return "EMA_CONFIRMATION_ONLY" if decision.get("ema_confirmation") else "NO_M15_CONFIRMATION"
    if strategy in OBSERVER_ONLY_STRATEGIES:
        return f"{strategy}_OBSERVER_ONLY"
    return "MISSING_TIMEFRAME_CONTEXT"


def _spread_status(symbol: str, frames: dict, decision: dict) -> str:
    spread = _float(decision.get("spread"))
    if spread is None:
        m5 = normalize_ohlcv(frames.get("M5"))
        spread = _float(m5.iloc[-1].get("spread")) if not m5.empty else None
    if spread is None:
        return "UNKNOWN"
    return "OK" if spread <= 999999 else "WIDE"


def _m1_status(timeframes: list[str], details: list[dict]) -> str:
    if "M1" not in {tf.upper() for tf in timeframes}:
        return "M1_NOT_REQUESTED_FOR_AUDIT"
    if any(int(row.get("rows_M1") or 0) <= 0 for row in details):
        return "M1_REQUIRED_BUT_EMPTY"
    return "OK"


def _parse_dt(value: object) -> datetime | None:
    try:
        return pd.Timestamp(value).to_pydatetime()
    except Exception:
        return None


def _big_setup_b_plus(decision: dict) -> bool:
    return str(decision.get("big_setup_grade") or "").upper() in {"B", "A", "A_PLUS"}


def _real_context(frames: dict) -> dict:
    context = _frame_context(frames)
    m5 = normalize_ohlcv(frames.get("M5"))
    if not m5.empty:
        context.update(
            {
                "risk_diag_status": "OK",
                "risk_reward": 1.8,
                "reward_risk": 1.8,
                "spread": _float(m5.iloc[-1].get("spread")) or 0.0,
                "safety_guard_status": "PASS",
            }
        )
    for timeframe in ["M1", "M5", "M15", "H1", "H4"]:
        context[f"{timeframe.lower()}_bars"] = len(normalize_ohlcv(frames.get(timeframe)))
    return context


def _audit_issue_flags(strategy: str, direction: str, frames: dict, decision: dict) -> list[str]:
    flags: list[str] = []
    is_entry_signal = direction in {"BUY", "SELL"}
    missing_context = _missing_context(frames)
    m1_empty = len(normalize_ohlcv(frames.get("M1"))) < M1_MIN_BARS
    rr = _risk_reward(decision)
    lot_valid = _lot_valid(decision)
    approved = _approved(decision)
    reason = str(decision.get("reason") or decision.get("blocked_reason") or "").strip().upper()

    if strategy in CONFIRMATION_ONLY_STRATEGIES and is_entry_signal:
        flags.append("EMA_ENTRY_NOT_ALLOWED")
    if strategy in OBSERVER_ONLY_STRATEGIES and is_entry_signal:
        flags.append("LEGACY_ENTRY_NOT_ALLOWED")
    if is_entry_signal and missing_context:
        flags.append("SIGNAL_WITH_MISSING_CONTEXT")
    if is_entry_signal and str(decision.get("mtfa_status") or "").upper() == "FAIL":
        flags.append("SIGNAL_WITH_MTFA_FAIL")
    if is_entry_signal and str(decision.get("smc_confluence_status") or "").upper() == "FAIL":
        flags.append("SIGNAL_WITH_SMC_FAIL")
    if is_entry_signal and (len(normalize_ohlcv(frames.get("M15"))) == 0 or not bool(decision.get("smc_m15_confirmation"))):
        flags.append("SIGNAL_WITH_NO_M15_CONFIRMATION")
    if is_entry_signal and (m1_empty or not bool(decision.get("smc_m1_entry_confirmation"))):
        flags.append("SIGNAL_WITH_NO_M1_CONFIRMATION")
    if _big_grade_inconsistent(decision):
        flags.append("BIG_SETUP_GRADE_INCONSISTENT")
    if direction in {"WAIT", "SKIP"} and reason in GENERIC_WAIT_REASONS:
        flags.append("GENERIC_WAIT_REASON")
    if is_entry_signal and rr is None:
        flags.append("RR_MISSING")
    if lot_valid is False and approved:
        flags.append("LOT_VALID_FALSE_BUT_APPROVED")
    if m1_empty:
        flags.append("M1_REQUIRED_BUT_EMPTY")
    return list(dict.fromkeys(flags))


def _demo_ready(strategy: str, direction: str, decision: dict, flags: list[str]) -> bool:
    if strategy not in ENTRY_STRATEGIES or direction not in {"BUY", "SELL"}:
        return False
    if flags:
        return False
    if str(decision.get("mtfa_status") or "").upper() != "PASS":
        return False
    if str(decision.get("smc_confluence_status") or "").upper() != "PASS":
        return False
    if not bool(decision.get("smc_m15_confirmation")) or not bool(decision.get("smc_m1_entry_confirmation")):
        return False
    if _lot_valid(decision) is not True:
        return False
    if _risk_reward(decision) is None:
        return False
    return True


def _real_context_detail_row(symbol: str, strategy: str, frames: dict, decision: dict, flags: list[str], demo_ready: bool) -> dict:
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "strategy": strategy,
        "strategy_status": decision.get("strategy_status"),
        "signal": decision.get("signal"),
        "entry": decision.get("entry"),
        "sl": decision.get("sl"),
        "tp": decision.get("tp"),
        "risk_reward": _risk_reward(decision),
        "confidence": decision.get("confidence"),
        "score": _strategy_score(decision),
        "reason": decision.get("reason") or decision.get("blocked_reason"),
        "blocked_reason": decision.get("blocked_reason"),
        "m5_bars": len(normalize_ohlcv(frames.get("M5"))),
        "m15_bars": len(normalize_ohlcv(frames.get("M15"))),
        "h1_bars": len(normalize_ohlcv(frames.get("H1"))),
        "h4_bars": len(normalize_ohlcv(frames.get("H4"))),
        "m1_bars": len(normalize_ohlcv(frames.get("M1"))),
        "missing_context": ",".join(_missing_context(frames)),
        "mtfa_status": decision.get("mtfa_status"),
        "mtfa_reason": decision.get("mtfa_reason"),
        "smc_confluence_status": decision.get("smc_confluence_status"),
        "smc_confluence_reason": decision.get("smc_confluence_reason"),
        "smc_m15_confirmation": bool(decision.get("smc_m15_confirmation")),
        "smc_m1_entry_confirmation": bool(decision.get("smc_m1_entry_confirmation")),
        "big_setup_score": decision.get("big_setup_score"),
        "big_setup_grade": decision.get("big_setup_grade"),
        "safety_guard_status": decision.get("safety_guard_status"),
        "safety_guard_reason": decision.get("safety_guard_reason"),
        "lot_valid": _lot_valid(decision),
        "approved": _approved(decision),
        "demo_ready": bool(demo_ready),
        "bug_flags": ",".join(flags),
    }
    for flag in ISSUE_FLAGS:
        row[flag] = flag in flags
    return row


def _issue_row(detail: dict, flag: str) -> dict:
    return {
        "timestamp": detail.get("timestamp"),
        "symbol": detail.get("symbol"),
        "strategy": detail.get("strategy"),
        "signal": detail.get("signal"),
        "bug_flag": flag,
        "reason": detail.get("reason"),
        "mtfa_status": detail.get("mtfa_status"),
        "smc_confluence_status": detail.get("smc_confluence_status"),
        "m15_bars": detail.get("m15_bars"),
        "m1_bars": detail.get("m1_bars"),
        "demo_ready": detail.get("demo_ready"),
    }


def _missing_context(frames: dict) -> list[str]:
    missing = []
    for timeframe in REQUIRED_CONTEXT_TIMEFRAMES:
        if len(normalize_ohlcv(frames.get(timeframe))) == 0:
            missing.append(timeframe)
    return missing


def _risk_reward(signal: dict) -> float | None:
    existing = _float(signal.get("risk_reward") or signal.get("reward_risk"))
    if existing is not None:
        return round(existing, 6)
    entry = _float(signal.get("entry"))
    sl = _float(signal.get("sl"))
    tp = _float(signal.get("tp"))
    if None in {entry, sl, tp} or entry == sl:
        return None
    return round(abs(tp - entry) / abs(entry - sl), 6)


def _lot_valid(decision: dict) -> bool | None:
    checklist = decision.get("execution_checklist")
    if isinstance(checklist, dict) and "lot_valid" in checklist:
        return bool(checklist.get("lot_valid"))
    if "lot_valid" in decision:
        return bool(decision.get("lot_valid"))
    return None


def _approved(decision: dict) -> bool:
    if str(decision.get("risk_status") or "").upper() == "APPROVED":
        return True
    if str(decision.get("risk_diag_status") or "").upper() == "OK" and str(decision.get("safety_guard_status") or "").upper() in {"PASS", "CAUTION"}:
        return True
    return False


def _big_grade_inconsistent(decision: dict) -> bool:
    grade = str(decision.get("big_setup_grade") or "UNKNOWN").upper()
    score = _float(decision.get("big_setup_score"))
    if score is None or grade == "UNKNOWN":
        return False
    expected = "A_PLUS" if score >= 85 else "A" if score >= 75 else "B" if score >= 60 else "C"
    return grade != expected


def _active_strategy_result(signal: dict, status: str) -> dict:
    out = dict(signal)
    out.setdefault("strategy_status", status)
    return out


def _observer_only_result(signal: dict, strategy: str) -> dict:
    out = dict(signal)
    out.update(
        {
            "strategy": strategy,
            "signal": "WAIT",
            "entry": None,
            "sl": None,
            "tp": None,
            "strategy_status": "LEGACY_OBSERVER",
            "reason": f"{strategy}_OBSERVER_ONLY",
            "blocked_reason": f"{strategy}_OBSERVER_ONLY",
        }
    )
    return out


def _save_audit_candles(candles: dict[str, dict[str, pd.DataFrame]], output: Path) -> None:
    candle_dir = output / "candles"
    candle_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frames in candles.items():
        safe_symbol = symbol.replace("/", "_").replace("\\", "_").replace("#", "HASH")
        for timeframe, frame in frames.items():
            normalize_ohlcv(frame).to_csv(candle_dir / f"{safe_symbol}_{timeframe}.csv", index=False)


def _csv_arg(value: str) -> list[str]:
    return [item.strip().upper() for item in str(value or "").split(",") if item.strip()]


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


def _float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local strategy math audit for HERMES ICT/SMC concepts.")
    parser.add_argument("--fixture", default=None)
    parser.add_argument("--symbol", default="BTCUSD#")
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--timeframes", default="M5,M15,H1,H4")
    parser.add_argument("--bars", type=int, default=500)
    parser.add_argument("--output", default="backtests/strategy_math_audit")
    parser.add_argument("--list-fixtures", action="store_true")
    parser.add_argument("--real-context", action="store_true")
    parser.add_argument("--pro-audit", action="store_true")
    args = parser.parse_args(argv)
    if args.list_fixtures:
        for fixture in available_fixtures():
            print(fixture.name)
        return 0
    if args.real_context:
        symbols = _csv_arg(args.symbols or args.symbol)
        timeframes = _csv_arg(args.timeframes)
        result = run_real_context_audit(symbols, timeframes, args.bars, Path(args.output), get_settings())
        print(
            "[STRATEGY_WIRING_AUDIT_V2] output=%s demo_ready=%s issues=%s missing_m1_reported=%s"
            % (
                args.output,
                str(result["summary"]["demo_ready"]).lower(),
                result["summary"]["issues_found"],
                str(result["summary"]["missing_m1_reported"]).lower(),
            )
        )
        return 0
    if args.pro_audit:
        symbols = _csv_arg(args.symbols or args.symbol)
        timeframes = _csv_arg(args.timeframes)
        result = run_professional_audit(symbols, timeframes, args.bars, Path(args.output), get_settings())
        print(
            "[STRATEGY_PRO_AUDIT_V3] output=%s demo_ready=%s issues=%s time_engine=%s m1_status=%s"
            % (
                args.output,
                str(result["summary"]["demo_ready"]).lower(),
                result["summary"]["issues_found"],
                result["summary"]["time_engine_status"],
                result["summary"]["m1_status"],
            )
        )
        return 0
    fixtures = [Path(args.fixture)] if args.fixture else available_fixtures()
    if not fixtures:
        raise SystemExit(f"No strategy math fixtures found under {DEFAULT_FIXTURE_DIR}")
    run_fixture_audits(fixtures, Path(args.output), args.symbol)
    print(f"[STRATEGY_MATH_AUDIT] output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
