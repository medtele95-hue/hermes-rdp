from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.logger import log
from app.utils.candles import closed_frame


STRATEGY = "GOLD_ORDER_FLOW_CVD_VWAP"
ENTRY_STRATEGY = "GOLD_ORDER_FLOW_CVD_VWAP_STRATEGY"
READER_STRATEGY = "ORDER_FLOW_READER"
SOURCE = "docs/strategies/order_flow_mt5.py"
GOLD_SYMBOLS = {"XAUUSD", "XAUUSD#", "GOLD", "GOLD#", "GOLDCASH#"}


@dataclass(frozen=True)
class OrderFlowConfig:
    vp_bins: int = 60
    value_area_pct: float = 0.70
    divergence_lookback: int = 24
    swing_lookback: int = 12
    level_proximity_pct: float = 0.0018
    min_confirmations: int = 3
    rr_ratio: float = 2.0
    sl_buffer_pct: float = 0.0008
    min_confidence: int = 70


def evaluate(symbol: str, frames: dict[str, Any] | None, context: dict | None = None, settings: object | None = None) -> dict:
    reader_snapshot = evaluate_reader(symbol, frames, context, settings)
    if not bool(getattr(settings, "gold_order_flow_execution_enabled", False)):
        return _reader_signal_from_snapshot(reader_snapshot, "ORDER_FLOW_ENTRY_DISABLED")
    if not _is_gold_symbol(symbol):
        return _reader_signal_from_snapshot(reader_snapshot, "GOLD_ORDER_FLOW_SYMBOL_NOT_GOLD")
    try:
        cfg = OrderFlowConfig(
            min_confidence=int(getattr(settings, "gold_order_flow_min_confidence", 70)),
        )
        frame_map = frames or {}
        candles_source = frame_map.get("M5")
        if candles_source is None:
            candles_source = frame_map.get("m5")
        candles = closed_frame(_normalize_candles(candles_source))
        ticks = (frames or {}).get("TICKS") or (frames or {}).get("ticks") or (context or {}).get("ticks")
        result = evaluate_gold_order_flow(symbol, candles, ticks=ticks, config=cfg)
    except Exception as exc:
        log.exception("[STRATEGY] %s decision=WAIT reason=GOLD_ORDER_FLOW_INTERNAL_ERROR error=%s", STRATEGY, exc)
        result = _payload(symbol, "WAIT", "GOLD_ORDER_FLOW_INTERNAL_ERROR")
    missing = required_entry_fields_missing(result, int(getattr(settings, "gold_order_flow_min_confidence", 70)))
    if missing:
        result = {
            **result,
            "decision": "WAIT",
            "status": "OBSERVE_ONLY",
            "side": None,
            "block_reason": "ORDER_FLOW_REQUIRED_FIELDS_MISSING",
            "missing_required_fields": missing,
            "warnings": list(dict.fromkeys((result.get("warnings") or []) + ["ORDER_FLOW_REQUIRED_FIELDS_MISSING"])),
        }
    return _signal_from_result(symbol, result)


def evaluate_reader(symbol: str, frames: dict[str, Any] | None, context: dict | None = None, settings: object | None = None) -> dict:
    if settings is not None and not bool(getattr(settings, "order_flow_reader_enabled", True)):
        return _reader_snapshot(symbol, None, "DISABLED", "ORDER_FLOW_READER_DISABLED", warnings=["ORDER_FLOW_READER_DISABLED"])
    frame_map = frames or {}
    candles_source = frame_map.get("M5")
    if candles_source is None:
        candles_source = frame_map.get("m5")
    # P0-TER (2026-07-14) : le snapshot est ancre sur la derniere bougie M5
    # CLOTUREE. `price` d'ici devient l'entree de ORDER_FLOW, et vah/val (profil
    # de volume) fixent le SL : les ancrer sur la bougie en cours faisait bouger
    # entry/SL/TP/RR a chaque tick, sur un prix qui pouvait refluer avant la
    # cloture. Le verdict top-down est desormais calcule sur les memes bougies :
    # decision et execution partagent enfin la meme vue du marche.
    candles = closed_frame(_normalize_candles(candles_source))
    if candles is None or candles.empty:
        return _reader_snapshot(symbol, None, "WAIT", "NO_CANDLES", warnings=["ORDER_FLOW_MISSING"])
    ticks = frame_map.get("TICKS") or frame_map.get("ticks") or (context or {}).get("ticks")
    df = compute_vwap(compute_delta_cvd(candles, ticks))
    cfg = OrderFlowConfig(min_confidence=int(getattr(settings, "gold_order_flow_min_confidence", 70)) if settings is not None else 70)
    profile = volume_profile(df, cfg) or {}
    levels = {"vwap": _last_float(df["vwap"]), **profile}
    divergence = detect_divergence(df, cfg)
    price = _last_float(df["close"])
    cvd = _last_float(df["cvd"]) if "cvd" in df.columns else None
    delta = _last_float(df["delta"]) if "delta" in df.columns else None
    confidence = 0
    if price is not None and all(levels.get(key) is not None for key in ("vwap", "poc", "vah", "val")):
        confidence += 35
    if cvd is not None and delta is not None:
        confidence += 25
    if divergence:
        confidence += 25
    hits = near_levels(price, levels, cfg) if price is not None else []
    if hits:
        confidence += 15
    signal = "BUY" if divergence == "bull" else "SELL" if divergence == "bear" else "WAIT"
    buy_pressure = max(0.0, float(delta or 0.0))
    sell_pressure = abs(min(0.0, float(delta or 0.0)))
    return _reader_snapshot(
        symbol,
        {
            "price": _round_price(price),
            "vwap": _round_price(levels.get("vwap")),
            "poc": _round_price(levels.get("poc")),
            "vah": _round_price(levels.get("vah")),
            "val": _round_price(levels.get("val")),
            "cvd_proxy": _to_float(cvd),
            "cvd_slope": _cvd_slope(df),
            "delta_proxy": _to_float(delta),
            "buy_pressure": round(buy_pressure, 5),
            "sell_pressure": round(sell_pressure, 5),
            "divergence": divergence,
            "confidence": int(min(100, confidence)),
            "signal": signal,
        },
        "OBSERVE_ONLY",
        "ORDER_FLOW_READER_OBSERVE_ONLY",
        warnings=["ORDER_FLOW_OBSERVE_ONLY_INTELLIGENCE"],
    )


def evaluate_observe_only(
    symbol: str,
    frames: dict[str, Any] | None,
    context: dict | None = None,
    settings: object | None = None,
) -> dict:
    """ORDER_FLOW_READER observe-only snapshot for any symbol (e.g. US100Cash# via SIMO path).

    Unlike evaluate(), this never attempts entry execution and never routes to DemoRouter.
    Returns an ORDER_FLOW_READER OBSERVE_ONLY payload with honest history-ready status.
    """
    reader_snap = evaluate_reader(symbol, frames, context, settings)
    status = reader_snap.get("status")
    levels_ready = all(
        reader_snap.get(k) is not None for k in ("poc", "vah", "val", "vwap")
    )
    if status == "DISABLED":
        reason = "ORDER_FLOW_READER_DISABLED"
        order_flow_status = "DISABLED"
    elif status == "OBSERVE_ONLY" and levels_ready:
        reason = "ORDER_FLOW_OBSERVE_ONLY"
        order_flow_status = "OBSERVE_ONLY"
    else:
        # Candles missing or volume profile incomplete — history not yet ready
        reason = "ORDER_FLOW_HISTORY_NOT_READY"
        order_flow_status = "HISTORY_NOT_READY"
    payload = _reader_signal_from_snapshot(reader_snap, reason)
    payload["order_flow_status"] = order_flow_status
    payload["latest_decision"] = "WAIT"
    payload["latest_reason"] = reason
    payload["route_status"] = "OBSERVE_ONLY"
    return payload


def evaluate_gold_order_flow(symbol: str, candles: Any, ticks: Any = None, config: OrderFlowConfig | None = None) -> dict:
    cfg = config or OrderFlowConfig()
    if not _is_gold_symbol(symbol):
        return _payload(symbol, "WAIT", "GOLD_ORDER_FLOW_SYMBOL_NOT_GOLD")
    df = _normalize_candles(candles)
    if df is None or len(df) < max(50, cfg.divergence_lookback, cfg.swing_lookback + 2):
        return _payload(symbol, "WAIT", "GOLD_ORDER_FLOW_NOT_ENOUGH_CANDLES")
    df = compute_delta_cvd(df, ticks)
    df = compute_vwap(df)
    profile = volume_profile(df, cfg)
    if not profile:
        return _payload(symbol, "WAIT", "GOLD_ORDER_FLOW_VOLUME_PROFILE_INVALID")
    levels = {"vwap": _last_float(df["vwap"]), **profile}
    signal = generate_signal(symbol, df, levels, cfg)
    if not signal:
        div = detect_divergence(df, cfg)
        price = _last_float(df["close"])
        hits = near_levels(price, levels, cfg) if price is not None else []
        reason = "GOLD_ORDER_FLOW_NO_DIVERGENCE" if div is None else "GOLD_ORDER_FLOW_NOT_NEAR_KEY_LEVEL" if not hits else "GOLD_ORDER_FLOW_CONFIRMATION_STACK_INCOMPLETE"
        return _payload(symbol, "WAIT", reason, levels=levels, divergence=div, key_level_hits=hits, df=df)
    return signal


def compute_delta_cvd(df: pd.DataFrame, ticks: Any = None) -> pd.DataFrame:
    out = df.copy()
    out["delta"] = 0.0
    tick_df = _normalize_ticks(ticks)
    if tick_df is not None and not tick_df.empty and "time" in tick_df.columns:
        tick_df = tick_df.copy()
        tick_df["dt"] = pd.to_datetime(tick_df["time"], unit="s", errors="coerce") if np.issubdtype(tick_df["time"].dtype, np.number) else pd.to_datetime(tick_df["time"], errors="coerce")
        tick_df = tick_df.dropna(subset=["dt"])
        if not tick_df.empty:
            for col in ("bid", "ask", "last", "volume", "volume_real"):
                if col not in tick_df.columns:
                    tick_df[col] = 0.0
            tick_df["mid"] = (pd.to_numeric(tick_df["bid"], errors="coerce").fillna(0.0) + pd.to_numeric(tick_df["ask"], errors="coerce").fillna(0.0)) / 2.0
            tick_df["pref"] = np.where(pd.to_numeric(tick_df["last"], errors="coerce").fillna(0.0) > 0, tick_df["last"], tick_df["mid"])
            raw_direction = np.sign(pd.Series(tick_df["pref"]).diff().fillna(0.0))
            direction = pd.Series(raw_direction).replace(0, np.nan).ffill().fillna(0.0).values
            volume_real = pd.to_numeric(tick_df["volume_real"], errors="coerce").fillna(0.0)
            volume = pd.to_numeric(tick_df["volume"], errors="coerce").fillna(0.0)
            vol = np.where(volume_real > 0, volume_real, np.where(volume > 0, volume, 1.0))
            tick_df["signed_volume"] = np.where(direction > 0, vol, np.where(direction < 0, -vol, 0.0))
            bins = pd.to_datetime(out["dt"])
            grouped = tick_df.set_index("dt")["signed_volume"].groupby(pd.Grouper(freq=_infer_freq(out))).sum()
            out["delta"] = pd.to_datetime(out["dt"]).map(grouped).fillna(0.0).astype(float)
            out["cvd"] = out["delta"].cumsum()
            return out
    close = pd.to_numeric(out["close"], errors="coerce")
    volume = pd.to_numeric(out["tick_volume"], errors="coerce").fillna(1.0).replace(0, 1.0)
    direction = np.sign(close.diff().fillna(0.0)).replace(0, np.nan).ffill().fillna(0.0)
    out["delta"] = direction * volume
    out["cvd"] = out["delta"].cumsum()
    return out


def compute_vwap(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    typ = (out["high"] + out["low"] + out["close"]) / 3.0
    vol = out["tick_volume"].replace(0, 1.0)
    day = pd.to_datetime(out["dt"]).dt.date
    out["vwap"] = (typ * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
    return out


def volume_profile(df: pd.DataFrame, cfg: OrderFlowConfig) -> dict[str, float] | None:
    lo = _to_float(df["low"].min())
    hi = _to_float(df["high"].max())
    if lo is None or hi is None or hi <= lo:
        return None
    bins = np.linspace(lo, hi, cfg.vp_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2.0
    profile = np.zeros(cfg.vp_bins)
    for _, row in df.iterrows():
        b_lo = np.searchsorted(bins, row["low"], side="right") - 1
        b_hi = np.searchsorted(bins, row["high"], side="right") - 1
        b_lo = max(0, min(int(b_lo), cfg.vp_bins - 1))
        b_hi = max(0, min(int(b_hi), cfg.vp_bins - 1))
        span = b_hi - b_lo + 1
        profile[b_lo : b_hi + 1] += float(row["tick_volume"]) / max(1, span)
    total = float(profile.sum())
    if total <= 0:
        return None
    poc_idx = int(np.argmax(profile))
    target = total * cfg.value_area_pct
    covered = float(profile[poc_idx])
    lo_i = hi_i = poc_idx
    while covered < target and (lo_i > 0 or hi_i < cfg.vp_bins - 1):
        down = profile[lo_i - 1] if lo_i > 0 else -1
        up = profile[hi_i + 1] if hi_i < cfg.vp_bins - 1 else -1
        if up >= down:
            hi_i += 1
            covered += float(profile[hi_i])
        else:
            lo_i -= 1
            covered += float(profile[lo_i])
    return {"poc": float(centers[poc_idx]), "vah": float(centers[hi_i]), "val": float(centers[lo_i])}


def detect_divergence(df: pd.DataFrame, cfg: OrderFlowConfig) -> str | None:
    rec = df.tail(cfg.divergence_lookback)
    if len(rec) < 6 or "cvd" not in rec.columns:
        return None
    half = len(rec) // 2
    first, second = rec.iloc[:half], rec.iloc[half:]
    if second["low"].min() < first["low"].min() and second["cvd"].min() > first["cvd"].min():
        return "bull"
    if second["high"].max() > first["high"].max() and second["cvd"].max() < first["cvd"].max():
        return "bear"
    return None


def near_levels(price: float, levels: dict[str, float | None], cfg: OrderFlowConfig) -> list[str]:
    prox = price * cfg.level_proximity_pct
    return [name for name, level in levels.items() if level is not None and abs(price - float(level)) <= prox]


def generate_signal(symbol: str, df: pd.DataFrame, levels: dict[str, float | None], cfg: OrderFlowConfig) -> dict | None:
    price = _last_float(df["close"])
    if price is None:
        return None
    divergence = detect_divergence(df, cfg)
    if divergence is None:
        return None
    side = "BUY" if divergence == "bull" else "SELL"
    confirmations = [f"divergence_{divergence}"]
    hits = near_levels(price, levels, cfg)
    if not hits:
        return None
    confirmations.append("zone:" + "+".join(hits))
    cvd_slope = _last_float(df["cvd"]) - _to_float(df["cvd"].iloc[-6]) if len(df) > 6 else None
    if cvd_slope is not None and ((side == "BUY" and cvd_slope > 0) or (side == "SELL" and cvd_slope < 0)):
        confirmations.append("cvd_aligned")
    vwap = levels.get("vwap")
    if vwap is not None and ((side == "BUY" and price > vwap) or (side == "SELL" and price < vwap)):
        confirmations.append("vwap_side")
    last_delta = _last_float(df["delta"])
    if last_delta is not None and ((side == "BUY" and last_delta > 0) or (side == "SELL" and last_delta < 0)):
        confirmations.append("delta_confirm")
    if len(confirmations) < cfg.min_confirmations:
        return None
    swing = df.tail(cfg.swing_lookback)
    buffer = price * cfg.sl_buffer_pct
    if side == "BUY":
        sl = float(swing["low"].min()) - buffer
        risk = price - sl
        tp = price + cfg.rr_ratio * risk
    else:
        sl = float(swing["high"].max()) + buffer
        risk = sl - price
        tp = price - cfg.rr_ratio * risk
    if risk <= 0:
        return None
    confidence = min(100, 50 + len(confirmations) * 10)
    decision = side if confidence >= cfg.min_confidence else "WAIT"
    reason = None if decision in {"BUY", "SELL"} else "GOLD_ORDER_FLOW_CONFIDENCE_BELOW_70"
    return _payload(
        symbol,
        decision,
        reason,
        confidence=confidence,
        entry=price,
        sl=sl,
        tp=tp,
        rr=cfg.rr_ratio,
        levels=levels,
        divergence=divergence,
        key_level_hits=hits,
        confirmations=confirmations,
        df=df,
    )


def _signal_from_result(symbol: str, result: dict) -> dict:
    decision = str(result.get("decision") or "WAIT").upper()
    signal = decision if decision in {"BUY", "SELL"} else "WAIT"
    confidence = int(result.get("confidence") or 0)
    side = signal if signal in {"BUY", "SELL"} else None
    status = result.get("status") or ("ORDER_READY" if signal in {"BUY", "SELL"} else "WAIT")
    reasoning = _reasoning_payload(symbol, result, status=status, side=side)
    payload = {
        "symbol": symbol,
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "setup_type": STRATEGY,
        "strategy_status": "ACTIVE",
        "strategy_role": "ENTRY_STRATEGY",
        "timeframe": "M5",
        "status": status,
        "side": side,
        "signal": signal,
        "direction": signal,
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "risk_reward": result.get("rr"),
        "reward_risk": result.get("rr"),
        "confidence": confidence,
        "m15_confirmation": signal in {"BUY", "SELL"},
        "m1_entry_confirmation": signal in {"BUY", "SELL"},
        "m15_confirmation_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m1_trigger_status": "PASS" if signal in {"BUY", "SELL"} else "FAIL",
        "m15_confirmation_reason": STRATEGY,
        "m1_trigger_reason": STRATEGY,
        "big_setup_grade": _grade(confidence),
        "setup_hunter_grade": _grade(confidence),
        "grade": _grade(confidence),
        "gold_order_flow_cvd_vwap": result,
        "gold_order_flow_signal": signal,
        "gold_order_flow_score": confidence,
        "gold_order_flow_reason": result.get("block_reason") or f"GOLD_ORDER_FLOW_{signal}",
        "reason": result.get("block_reason") or f"GOLD_ORDER_FLOW_{signal}",
        "order_flow_entry_enabled": True,
        "order_flow_required_fields_valid": not bool(result.get("missing_required_fields")),
        "missing_required_fields": result.get("missing_required_fields") or [],
        "raw_payload": reasoning,
        **reasoning,
    }
    _log_order_flow_payload(symbol, result, status)
    log.info("[STRATEGY] %s decision=%s confidence=%s reason=%s", STRATEGY, signal, confidence, payload["reason"])
    log.info("[ORDER_FLOW_PAYLOAD_EMITTED] strategy=%s symbol=%s status=%s", STRATEGY, symbol, status)
    return payload


def _reader_signal_from_snapshot(snapshot: dict, reason: str | None = None) -> dict:
    payload = dict(snapshot)
    payload.update(
        {
            "strategy": READER_STRATEGY,
            "strategy_id": READER_STRATEGY,
            "setup_type": READER_STRATEGY,
            "strategy_status": "ACTIVE",
            "strategy_role": "OBSERVER_ONLY",
            "signal": "WAIT",
            "direction": "WAIT",
            "side": None,
            "status": "OBSERVE_ONLY",
            "reason": reason or payload.get("reason") or "ORDER_FLOW_READER_OBSERVE_ONLY",
            "order_flow_reader": payload,
            "order_flow_snapshot": payload,
            "order_flow_entry_enabled": False,
            "raw_payload": payload,
        }
    )
    _log_order_flow_payload(str(payload.get("broker_symbol") or payload.get("symbol") or ""), payload, "OBSERVE_ONLY")
    log.info("[STRATEGY] %s decision=WAIT confidence=%s reason=%s", READER_STRATEGY, payload.get("confidence"), payload["reason"])
    log.info("[ORDER_FLOW_PAYLOAD_EMITTED] strategy=%s symbol=%s status=OBSERVE_ONLY", READER_STRATEGY, payload.get("broker_symbol") or payload.get("symbol"))
    return payload


def _reader_snapshot(symbol: str, values: dict | None, status: str, reason: str, warnings: list[str] | None = None) -> dict:
    values = values or {}
    return {
        "symbol": _canonical_symbol(symbol),
        "broker_symbol": str(symbol or "").upper(),
        "timeframe": "M5",
        "price": values.get("price"),
        "vwap": values.get("vwap"),
        "poc": values.get("poc"),
        "vah": values.get("vah"),
        "val": values.get("val"),
        "cvd_proxy": values.get("cvd_proxy"),
        "cvd_slope": values.get("cvd_slope"),
        "delta_proxy": values.get("delta_proxy"),
        "latest_delta": values.get("delta_proxy"),
        "buy_pressure": values.get("buy_pressure"),
        "sell_pressure": values.get("sell_pressure"),
        "divergence": values.get("divergence"),
        "confidence": values.get("confidence", 0),
        "signal": values.get("signal") or "WAIT",
        "status": status,
        "warnings": warnings or [],
        "source": SOURCE,
        "mode": "OBSERVE_ONLY",
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stale_after_seconds": 60,
        "observe_only_notice": "ORDER FLOW = OBSERVE-ONLY INTELLIGENCE. IT DOES NOT BLOCK TRADES.",
    }


def required_entry_fields_missing(result: dict, min_confidence: int = 70) -> list[str]:
    missing: list[str] = []
    required = {
        "price": result.get("price") or result.get("entry"),
        "vwap": result.get("vwap"),
        "poc": result.get("poc"),
        "vah": result.get("vah"),
        "val": result.get("val"),
        "cvd_proxy": result.get("cvd_proxy") if result.get("cvd_proxy") is not None else result.get("cvd"),
        "delta_proxy": result.get("delta_proxy") if result.get("delta_proxy") is not None else result.get("latest_delta"),
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "rr": result.get("rr"),
    }
    for key, value in required.items():
        if _to_float(value) is None:
            missing.append(key)
    return list(dict.fromkeys(missing))


def _payload(
    symbol: str,
    decision: str,
    block_reason: str | None,
    *,
    confidence: int = 0,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    rr: float | None = None,
    levels: dict[str, float | None] | None = None,
    divergence: str | None = None,
    key_level_hits: list[str] | None = None,
    confirmations: list[str] | None = None,
    df: pd.DataFrame | None = None,
) -> dict:
    last = df.iloc[-1] if isinstance(df, pd.DataFrame) and not df.empty else {}
    out = {
        "enabled": True,
        "mode": "ENTRY_STRATEGY",
        "strategy": STRATEGY,
        "strategy_id": STRATEGY,
        "source": SOURCE,
        "symbol": symbol,
        "timeframe": "M5",
        "status": "ORDER_READY" if decision in {"BUY", "SELL"} else "WAIT",
        "side": decision if decision in {"BUY", "SELL"} else None,
        "decision": decision,
        "confidence": confidence,
        "min_confidence": 70,
        "price": _round_price(entry if entry is not None else (_last_float(df["close"]) if isinstance(df, pd.DataFrame) and not df.empty else None)),
        "entry": _round_price(entry),
        "sl": _round_price(sl),
        "tp": _round_price(tp),
        "rr": rr,
        "volume_profile": {key: _round_price(value) for key, value in (levels or {}).items() if key in {"poc", "vah", "val"}},
        "poc": _round_price((levels or {}).get("poc")),
        "vah": _round_price((levels or {}).get("vah")),
        "val": _round_price((levels or {}).get("val")),
        "vwap": _round_price((levels or {}).get("vwap")),
        "delta": _to_float(last.get("delta")) if isinstance(last, pd.Series) else None,
        "latest_delta": _to_float(last.get("delta")) if isinstance(last, pd.Series) else None,
        "delta_proxy": _to_float(last.get("delta")) if isinstance(last, pd.Series) else None,
        "cvd": _to_float(last.get("cvd")) if isinstance(last, pd.Series) else None,
        "cvd_proxy": _to_float(last.get("cvd")) if isinstance(last, pd.Series) else None,
        "cvd_slope": _cvd_slope(df),
        "buy_pressure": max(0.0, _to_float(last.get("delta")) or 0.0) if isinstance(last, pd.Series) else None,
        "sell_pressure": abs(min(0.0, _to_float(last.get("delta")) or 0.0)) if isinstance(last, pd.Series) else None,
        "divergence": divergence,
        "key_level_hits": key_level_hits or [],
        "confirmations": confirmations or [],
        "block_reason": block_reason,
        "router_decision": None,
        "demo_gate_reason": None,
        "mt5_order_flow_warning": "MT5_TICK_DELTA_PROXY_NOT_REAL_ORDER_BOOK",
        "warnings": [],
    }
    return out


def _reasoning_payload(symbol: str, result: dict, *, status: str, side: str | None) -> dict:
    return {
        "strategy_id": STRATEGY,
        "symbol": symbol,
        "timeframe": result.get("timeframe") or "M5",
        "status": status,
        "side": side,
        "confidence": result.get("confidence"),
        "price": result.get("price"),
        "entry": result.get("entry"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "poc": result.get("poc"),
        "vah": result.get("vah"),
        "val": result.get("val"),
        "vwap": result.get("vwap"),
        "cvd_slope": result.get("cvd_slope"),
        "latest_delta": result.get("latest_delta"),
        "cvd_proxy": result.get("cvd_proxy"),
        "delta_proxy": result.get("delta_proxy"),
        "divergence": result.get("divergence"),
        "block_reason": result.get("block_reason"),
        "missing_required_fields": result.get("missing_required_fields") or [],
        "router_decision": result.get("router_decision"),
        "demo_gate_reason": result.get("demo_gate_reason"),
        "mt5_order_flow_warning": result.get("mt5_order_flow_warning") or "MT5_TICK_DELTA_PROXY_NOT_REAL_ORDER_BOOK",
    }


def _log_order_flow_payload(symbol: str, result: dict, status: str) -> None:
    log.info(
        "[ORDER_FLOW] symbol=%s POC=%s VAH=%s VAL=%s VWAP=%s CVD_SLOPE=%s DELTA=%s DIVERGENCE=%s",
        symbol,
        result.get("poc"),
        result.get("vah"),
        result.get("val"),
        result.get("vwap"),
        result.get("cvd_slope"),
        result.get("latest_delta"),
        result.get("divergence"),
    )


def _cvd_slope(df: pd.DataFrame | None) -> float | None:
    if not isinstance(df, pd.DataFrame) or "cvd" not in df.columns or len(df) <= 6:
        return None
    current = _last_float(df["cvd"])
    previous = _to_float(df["cvd"].iloc[-6])
    if current is None or previous is None:
        return None
    return round(current - previous, 5)


def _normalize_candles(candles: Any) -> pd.DataFrame | None:
    if candles is None:
        return None
    if isinstance(candles, dict):
        candles = candles.get("candles") or candles.get("data") or candles
    try:
        df = candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(candles)
    except (TypeError, ValueError):
        return None
    if df.empty:
        return None
    column_map = {
        "time": "time",
        "datetime": "time",
        "date": "time",
        "open": "open",
        "o": "open",
        "high": "high",
        "h": "high",
        "low": "low",
        "l": "low",
        "close": "close",
        "c": "close",
        "tick_volume": "tick_volume",
        "volume": "tick_volume",
        "v": "tick_volume",
    }
    out = pd.DataFrame()
    for source, target in column_map.items():
        if target in out.columns:
            continue
        matches = [col for col in df.columns if str(col).lower() == source]
        if matches:
            out[target] = df[matches[0]]
    required = {"open", "high", "low", "close"}
    if not required.issubset(out.columns):
        return None
    if "tick_volume" not in out.columns:
        out["tick_volume"] = 1.0
    if "time" not in out.columns:
        out["dt"] = pd.date_range(end=pd.Timestamp.utcnow(), periods=len(out), freq="5min")
    else:
        out["dt"] = pd.to_datetime(out["time"], unit="s", errors="coerce") if np.issubdtype(out["time"].dtype, np.number) else pd.to_datetime(out["time"], errors="coerce")
        if out["dt"].isna().all():
            out["dt"] = pd.date_range(end=pd.Timestamp.utcnow(), periods=len(out), freq="5min")
    for col in ("open", "high", "low", "close", "tick_volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return out


def _normalize_ticks(ticks: Any) -> pd.DataFrame | None:
    if ticks is None:
        return None
    if isinstance(ticks, dict):
        ticks = ticks.get("ticks") or ticks.get("data") or ticks
    df = ticks.copy() if isinstance(ticks, pd.DataFrame) else pd.DataFrame(ticks)
    return None if df.empty else df


def _infer_freq(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return "5min"
    delta = pd.to_datetime(df["dt"]).diff().dropna().dt.total_seconds().median()
    minutes = max(1, int(round((delta or 300) / 60)))
    return f"{minutes}min"


def _is_gold_symbol(symbol: object) -> bool:
    normalized = str(symbol or "").upper().strip()
    return normalized in GOLD_SYMBOLS or normalized.startswith("GOLD") or normalized.startswith("XAUUSD")


def _canonical_symbol(symbol: object) -> str:
    normalized = str(symbol or "").upper().strip()
    if normalized.startswith("BTCUSD"):
        return "BTCUSD"
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return "GOLD"
    if normalized.startswith("EURUSD"):
        return "EURUSD"
    return normalized.replace("#", "")


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "B"
    return "D"


def _round_price(value: object) -> float | None:
    number = _to_float(value)
    return round(number, 5) if number is not None else None


def _last_float(series: pd.Series) -> float | None:
    if series is None or len(series) == 0:
        return None
    return _to_float(series.iloc[-1])


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
