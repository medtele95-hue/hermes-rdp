from __future__ import annotations

import pandas as pd

from app.logger import log
from app.utils.confidence import normalize_confidence
from app.utils.indicators import enrich_indicators


BTC_SCALPING_AGENT = "BTC_SCALPING_AGENT"
LEGACY_SCALPING_AGENT = "SCALPING_AGENT"


def evaluate(symbol: str, df: pd.DataFrame) -> dict:
    name = LEGACY_SCALPING_AGENT
    enriched = enrich_indicators(df)
    if enriched.empty or len(enriched) < 25:
        return _result(symbol, name, "SKIP", 0.0, reason="Not enough candles", blocked_reason="INSUFFICIENT_DATA")

    row = enriched.iloc[-1]
    prev = enriched.iloc[-2]
    atr = max(float(row["atr"]) if pd.notna(row["atr"]) else 0.0, float(row["close"]) * 0.001)
    close = float(row["close"])
    rsi = float(row["rsi"]) if pd.notna(row["rsi"]) else 50.0

    if close > float(row["ema20"]) and float(prev["close"]) <= float(prev["ema20"]) and rsi < 68:
        return _result(symbol, name, "BUY", 0.58, close, close - atr, close + atr * 1.6, "Fast EMA reclaim scalp")
    if close < float(row["ema20"]) and float(prev["close"]) >= float(prev["ema20"]) and rsi > 32:
        return _result(symbol, name, "SELL", 0.58, close, close + atr, close - atr * 1.6, "Fast EMA rejection scalp")
    return _result(symbol, name, "WAIT", 0.35, reason="No scalp trigger")


def evaluate_btc_entry(
    symbol: str,
    df: pd.DataFrame,
    frames: dict | None = None,
    settings: object | None = None,
    max_spread: float | None = None,
) -> dict:
    relaxed = bool(getattr(settings, "btc_scalping_relaxed_demo_mode", True))
    min_confidence = int(getattr(settings, "btc_scalping_min_confidence", 55))
    allow_m5_momentum = bool(getattr(settings, "btc_scalping_allow_m5_momentum", True))
    allow_m1_breakout = bool(getattr(settings, "btc_scalping_allow_m1_breakout", True))
    spread_limit = _spread_limit(settings, max_spread)

    if not _is_btc_symbol(symbol):
        result = _btc_wait_payload(symbol, "SYMBOL_NOT_BTC", min_confidence, relaxed, spread_limit)
        result["blocked_reason"] = "BTC_SCALPING_AGENT_SYMBOL_NOT_BTC"
        _log_btc_scalping_result(result)
        return result

    m5_result = _evaluate_m5_momentum(symbol, df, min_confidence, relaxed, spread_limit) if allow_m5_momentum else None
    if relaxed and m5_result and m5_result["signal"] in {"BUY", "SELL"}:
        _log_btc_scalping_result(m5_result)
        return m5_result

    m1 = _frame(frames, "M1")
    m1_result = _evaluate_m1_breakout(symbol, m1, min_confidence, relaxed, spread_limit) if allow_m1_breakout else None
    if relaxed and m1_result and m1_result["signal"] in {"BUY", "SELL"}:
        _log_btc_scalping_result(m1_result)
        return m1_result

    pullback_result = _evaluate_micro_pullback(symbol, df, min_confidence, relaxed, spread_limit)
    if relaxed and pullback_result and pullback_result["signal"] in {"BUY", "SELL"}:
        _log_btc_scalping_result(pullback_result)
        return pullback_result

    legacy = evaluate(symbol, df)
    signal = str(legacy.get("signal") or "WAIT").upper()
    if signal == "SKIP":
        signal = "WAIT"
    result = dict(legacy)
    raw_confidence = result.get("confidence")
    normalized_confidence = normalize_confidence(raw_confidence)
    result["strategy"] = BTC_SCALPING_AGENT
    result["signal"] = signal
    result["confidence_raw"] = raw_confidence
    result["confidence"] = normalized_confidence
    result["normalized_confidence"] = normalized_confidence
    result["setup_score"] = normalized_confidence
    result["edge_score"] = normalized_confidence
    result["setup_hunter_score"] = normalized_confidence
    result["min_confidence"] = min_confidence
    result.update(_btc_dashboard_payload(result, relaxed, result.get("reason"), None, spread_limit))
    if signal in {"BUY", "SELL"} or not relaxed:
        _log_btc_scalping_result(result)
        return result

    details = {
        "legacy_reason": legacy.get("reason"),
        "m5_momentum": (m5_result or {}).get("reason") if isinstance(m5_result, dict) else "DISABLED",
        "m1_breakout": (m1_result or {}).get("reason") if isinstance(m1_result, dict) else "DISABLED_OR_MISSING",
        "micro_pullback": (pullback_result or {}).get("reason") if isinstance(pullback_result, dict) else "NO_MICRO_PULLBACK",
    }
    result = _btc_wait_payload(symbol, "NO_SCALP_TRIGGER", min_confidence, relaxed, spread_limit, details=details)
    _log_btc_scalping_result(result)
    return result


def _result(symbol: str, strategy: str, signal: str, confidence: float, entry=None, sl=None, tp=None, reason="", blocked_reason=None, **extra) -> dict:
    payload = {
        "symbol": symbol,
        "timeframe": "M5",
        "strategy": strategy,
        "signal": signal,
        "confidence": confidence,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "reason": reason,
        "blocked_reason": blocked_reason,
    }
    payload.update(extra)
    return payload


def _evaluate_m5_momentum(symbol: str, df: pd.DataFrame, min_confidence: int, relaxed: bool, spread_limit: float | None) -> dict:
    enriched = _with_ema9(df)
    if enriched.empty or len(enriched) < 10:
        return _btc_wait_payload(symbol, "NOT_ENOUGH_M5_CANDLES", min_confidence, relaxed, spread_limit)
    row = enriched.iloc[-1]
    recent = enriched.iloc[-10:-1]
    spread = _spread(row)
    if not _spread_ok(spread, spread_limit):
        return _btc_wait_payload(symbol, "SPREAD_HIGH", min_confidence, relaxed, spread_limit, spread=spread)
    body = abs(float(row["close"]) - float(row["open"]))
    avg_body = float(recent["body"].mean()) if "body" in recent and not recent.empty else 0.0
    if avg_body <= 0 or body <= avg_body:
        return _btc_wait_payload(symbol, "M5_BODY_NOT_STRONG", min_confidence, relaxed, spread_limit, spread=spread)
    close = float(row["close"])
    open_ = float(row["open"])
    ema9 = float(row["ema9"])
    atr = _atr_or_fallback(row, close)
    if close > open_ and close > ema9:
        return _trade_payload(symbol, "BUY", "M5_MOMENTUM_SCALP", min_confidence, close, atr, relaxed, "M5_MOMENTUM", spread, spread_limit)
    if close < open_ and close < ema9:
        return _trade_payload(symbol, "SELL", "M5_MOMENTUM_SCALP", min_confidence, close, atr, relaxed, "M5_MOMENTUM", spread, spread_limit)
    return _btc_wait_payload(symbol, "M5_MOMENTUM_NOT_ALIGNED", min_confidence, relaxed, spread_limit, spread=spread)


def _evaluate_m1_breakout(symbol: str, df: pd.DataFrame | None, min_confidence: int, relaxed: bool, spread_limit: float | None) -> dict:
    enriched = _with_ema9(df)
    if enriched.empty or len(enriched) < 7:
        return _btc_wait_payload(symbol, "NOT_ENOUGH_M1_CANDLES", min_confidence, relaxed, spread_limit)
    row = enriched.iloc[-1]
    recent = enriched.iloc[-6:-1]
    spread = _spread(row)
    if not _spread_ok(spread, spread_limit):
        return _btc_wait_payload(symbol, "SPREAD_HIGH", min_confidence, relaxed, spread_limit, spread=spread)
    close = float(row["close"])
    atr = _atr_or_fallback(row, close)
    if close > float(recent["high"].max()):
        return _trade_payload(symbol, "BUY", "M1_BREAKOUT_SCALP", min_confidence, close, atr, relaxed, "M1_BREAKOUT", spread, spread_limit)
    if close < float(recent["low"].min()):
        return _trade_payload(symbol, "SELL", "M1_BREAKOUT_SCALP", min_confidence, close, atr, relaxed, "M1_BREAKOUT", spread, spread_limit)
    return _btc_wait_payload(symbol, "M1_NO_BREAKOUT", min_confidence, relaxed, spread_limit, spread=spread)


def _evaluate_micro_pullback(symbol: str, df: pd.DataFrame, min_confidence: int, relaxed: bool, spread_limit: float | None) -> dict:
    enriched = _with_ema9(df)
    if enriched.empty or len(enriched) < 25:
        return _btc_wait_payload(symbol, "NOT_ENOUGH_PULLBACK_CANDLES", min_confidence, relaxed, spread_limit)
    row = enriched.iloc[-1]
    prev = enriched.iloc[-2]
    spread = _spread(row)
    if not _spread_ok(spread, spread_limit):
        return _btc_wait_payload(symbol, "SPREAD_HIGH", min_confidence, relaxed, spread_limit, spread=spread)
    close = float(row["close"])
    open_ = float(row["open"])
    atr = _atr_or_fallback(row, close)
    bullish_trend = float(row["ema9"]) > float(row["ema20"]) and float(row["ema20"]) >= float(enriched.iloc[-5]["ema20"])
    bearish_trend = float(row["ema9"]) < float(row["ema20"]) and float(row["ema20"]) <= float(enriched.iloc[-5]["ema20"])
    if bullish_trend and float(prev["close"]) <= float(prev["ema9"]) and close > open_ and close > float(row["ema9"]):
        return _trade_payload(symbol, "BUY", "MICRO_PULLBACK_CONTINUATION", min_confidence, close, atr, relaxed, "MICRO_PULLBACK", spread, spread_limit)
    if bearish_trend and float(prev["close"]) >= float(prev["ema9"]) and close < open_ and close < float(row["ema9"]):
        return _trade_payload(symbol, "SELL", "MICRO_PULLBACK_CONTINUATION", min_confidence, close, atr, relaxed, "MICRO_PULLBACK", spread, spread_limit)
    return _btc_wait_payload(symbol, "NO_MICRO_PULLBACK", min_confidence, relaxed, spread_limit, spread=spread)


def _is_btc_symbol(symbol: object) -> bool:
    return str(symbol or "").upper().startswith("BTCUSD")


def _log_btc_scalping_result(result: dict) -> None:
    decision = str(result.get("signal") or "WAIT").upper()
    reason = result.get("reason") or result.get("blocked_reason") or "BTC_SCALPING_AGENT"
    confidence = result.get("confidence_raw") if result.get("confidence_raw") is not None else result.get("confidence")
    log.info("[BTC_SCALPING_AGENT] decision=%s reason=%s confidence=%s", decision, reason, confidence)
    threshold = result.get("min_confidence")
    if threshold is not None:
        normalized = normalize_confidence(result.get("confidence"))
        status = "PASS" if decision in {"BUY", "SELL"} and normalized >= float(threshold) else "BLOCK"
        log.info("[BTC_SCALPING_CONF] raw=%s normalized=%s threshold=%s decision=%s", confidence, _compact_number(normalized), threshold, status)


def _trade_payload(symbol: str, side: str, reason: str, confidence: int, entry: float, atr: float, relaxed: bool, trigger_type: str, spread: float | None, spread_limit: float | None) -> dict:
    risk = max(float(atr), abs(entry) * 0.001)
    if side == "BUY":
        sl = entry - risk
        tp = entry + risk * 2.0
    else:
        sl = entry + risk
        tp = entry - risk * 2.0
    payload = _result(
        symbol,
        BTC_SCALPING_AGENT,
        side,
        normalize_confidence(confidence),
        entry=entry,
        sl=sl,
        tp=tp,
        reason=reason,
        risk_reward=2.0,
        reward_risk=2.0,
        confidence_raw=confidence,
        normalized_confidence=normalize_confidence(confidence),
        setup_score=normalize_confidence(confidence),
        edge_score=normalize_confidence(confidence),
        setup_hunter_score=normalize_confidence(confidence),
        min_confidence=confidence,
    )
    payload.update(_btc_dashboard_payload(payload, relaxed, reason, trigger_type, spread_limit, spread=spread))
    return payload


def _btc_wait_payload(symbol: str, reason: str, confidence: int, relaxed: bool, spread_limit: float | None, spread: float | None = None, details: dict | None = None) -> dict:
    payload = _result(symbol, BTC_SCALPING_AGENT, "WAIT", 0.0, reason=reason, confidence_raw=0.0, normalized_confidence=0.0, min_confidence=confidence)
    payload.update(_btc_dashboard_payload(payload, relaxed, reason, None, spread_limit, spread=spread, details=details))
    return payload


def _btc_dashboard_payload(payload: dict, relaxed: bool, reason: object, trigger_type: str | None, spread_limit: float | None, spread: float | None = None, details: dict | None = None) -> dict:
    decision = str(payload.get("signal") or "WAIT").upper()
    diag = {
        "enabled": True,
        "relaxed_demo_mode": relaxed,
        "decision": decision,
        "reason": reason,
        "confidence": payload.get("confidence"),
        "confidence_raw": payload.get("confidence_raw"),
        "normalized_confidence": payload.get("normalized_confidence"),
        "min_confidence": payload.get("min_confidence"),
        "trigger_type": trigger_type,
        "entry": payload.get("entry"),
        "sl": payload.get("sl"),
        "tp": payload.get("tp"),
        "spread": spread,
        "cooldown_active": False,
        "trades_today": 0,
    }
    if spread_limit is not None:
        diag["max_spread"] = spread_limit
    if details:
        diag["details"] = details
    return {
        "btc_scalping_agent": diag,
        "btc_scalping_decision": decision,
        "btc_scalping_reason": reason,
        "btc_scalping_trigger_type": trigger_type,
        "btc_scalping_confidence": payload.get("confidence"),
        "btc_scalping_confidence_raw": payload.get("confidence_raw"),
        "btc_scalping_normalized_confidence": payload.get("normalized_confidence"),
    }


def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 4)


def _with_ema9(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    enriched = enrich_indicators(df)
    if "ema9" not in enriched:
        enriched["ema9"] = enriched["close"].ewm(span=9, adjust=False).mean()
    return enriched


def _frame(frames: dict | None, key: str) -> pd.DataFrame | None:
    if not isinstance(frames, dict):
        return None
    frame = frames.get(key)
    return frame if isinstance(frame, pd.DataFrame) else None


def _spread(row: pd.Series) -> float | None:
    if "spread" not in row or pd.isna(row["spread"]):
        return None
    return float(row["spread"])


def _spread_ok(spread: float | None, spread_limit: float | None) -> bool:
    return spread_limit is None or spread is None or spread <= spread_limit


def _spread_limit(settings: object | None, max_spread: float | None) -> float | None:
    value = getattr(settings, "max_spread_btcusd", None) if settings is not None else None
    if value is None:
        value = max_spread
    if value is None and settings is not None:
        value = getattr(settings, "max_spread", None)
    return float(value) if value is not None else None


def _atr_or_fallback(row: pd.Series, close: float) -> float:
    atr = float(row["atr"]) if "atr" in row and pd.notna(row["atr"]) else 0.0
    return max(atr, abs(close) * 0.001)
