"""
HERMES Strategies Pack
======================

Drop this file into your HERMES backend, for example:
    backend/strategies/hermes_strategies.py

Purpose:
    - Generate trading SIGNALS only.
    - No order execution.
    - No broker login.
    - No live trading calls.
    - Designed for DEMO / backtest / forward-test first.

Compatible with:
    - Python 3.10+
    - MT5 candle payloads from MetaTrader5.copy_rates_*
    - Supabase / Lovable dashboard payloads

Main usage:
    from hermes_strategies import HermesStrategyEngine

    engine = HermesStrategyEngine()
    result = engine.analyze(
        symbol="XAUUSD",
        candles=mt5_rates_as_dicts,
        tick={"bid": 2350.10, "ask": 2350.25},
        timeframe="M5",
    )

    best_signal = result["best_signal"]
    all_signals = result["signals"]

Important:
    This file returns signals like BUY / SELL / WAIT / AVOID.
    Execution must stay in your broker-safe HERMES risk/execution layer.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import math
import statistics


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

Side = str  # "BUY", "SELL", "WAIT", "AVOID"


@dataclass
class Candle:
    time: Optional[Any]
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 0.0)

    @property
    def upper_wick(self) -> float:
        return max(self.high - max(self.open, self.close), 0.0)

    @property
    def lower_wick(self) -> float:
        return max(min(self.open, self.close) - self.low, 0.0)

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


@dataclass
class StrategySignal:
    strategy: str
    symbol: str
    timeframe: str
    side: Side
    score: float
    confidence: float
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    reasons: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["score"] = round(float(self.score), 2)
        data["confidence"] = round(float(self.confidence), 4)
        for key in ("entry", "stop_loss", "take_profit", "risk_reward"):
            if data[key] is not None:
                data[key] = round(float(data[key]), 8)
        return data


@dataclass
class EngineConfig:
    min_score_to_trade: float = 65.0
    min_confidence_to_trade: float = 0.60
    default_rr: float = 1.8
    stop_atr_multiplier: float = 1.4
    lookback: int = 120
    swing_lookback: int = 20
    range_lookback: int = 48
    max_spread_points_default: float = 45.0

    # Spread limits are broker/symbol dependent. Adjust to your broker suffixes.
    max_spread_points_by_symbol: Dict[str, float] = field(default_factory=lambda: {
        "EURUSD": 25,
        "GBPUSD": 30,
        "USDJPY": 30,
        "XAUUSD": 80,
        "GOLD": 80,
        "BTCUSD": 5000,
        "ETHUSD": 800,
        "US100": 250,
        "NAS100": 250,
        "JP225": 800,
    })

    # UTC sessions. Change if your strategy must avoid Asia / news hours.
    session_hours_utc: Tuple[int, ...] = tuple(range(7, 21))

    # Set to True if you want all symbols accepted by the strategy layer.
    allow_all_symbols: bool = True

    # Optional hard list. If not empty and allow_all_symbols=False, only these pass.
    allowed_symbols: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default


def _clean_symbol(symbol: str) -> str:
    """Normalize broker suffix symbols like XAUUSD#, US100Cash#, EURUSDm."""
    s = (symbol or "").upper().strip()
    for suffix in ("#", ".M", ".PRO", "M", "_I", "-ECN"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace("CASH", "")
    return s


def normalize_candles(raw: Sequence[Any], limit: Optional[int] = None) -> List[Candle]:
    """
    Accepts:
        - list[dict] from MT5 converted rates
        - list[object] with attributes
        - pandas/numpy records converted to dict-like rows

    Expected keys:
        time, open, high, low, close, tick_volume or real_volume or volume
    """
    if raw is None:
        return []

    items = list(raw)
    if limit:
        items = items[-limit:]

    candles: List[Candle] = []

    for row in items:
        if isinstance(row, Candle):
            candles.append(row)
            continue

        def get(name: str, default: Any = None) -> Any:
            if isinstance(row, dict):
                return row.get(name, default)
            return getattr(row, name, default)

        candles.append(
            Candle(
                time=get("time"),
                open=_to_float(get("open")),
                high=_to_float(get("high")),
                low=_to_float(get("low")),
                close=_to_float(get("close")),
                volume=_to_float(
                    get("tick_volume", get("real_volume", get("volume", 0.0)))
                ),
            )
        )

    return [
        c for c in candles
        if c.high >= c.low
        and c.open > 0
        and c.high > 0
        and c.low > 0
        and c.close > 0
    ]


def closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def highs(candles: Sequence[Candle]) -> List[float]:
    return [c.high for c in candles]


def lows(candles: Sequence[Candle]) -> List[float]:
    return [c.low for c in candles]


def volumes(candles: Sequence[Candle]) -> List[float]:
    return [c.volume for c in candles]


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    if len(series) < period:
        return None
    return series[-1]


def true_ranges(candles: Sequence[Candle]) -> List[float]:
    if len(candles) < 2:
        return []
    trs: List[float] = []
    prev_close = candles[0].close
    for c in candles[1:]:
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(max(tr, 0.0))
        prev_close = c.close
    return trs


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    trs = true_ranges(candles)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []

    recent = values[-(period + 1):]
    for i in range(1, len(recent)):
        diff = recent[i] - recent[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def slope(values: Sequence[float], period: int = 10) -> float:
    if len(values) < period:
        return 0.0
    y = list(values[-period:])
    x = list(range(period))
    x_mean = sum(x) / period
    y_mean = sum(y) / period
    numerator = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(period))
    denominator = sum((x[i] - x_mean) ** 2 for i in range(period)) or 1.0
    return numerator / denominator


def percentile_position(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (value - low) / (high - low)))


def get_entry(symbol: str, candles: Sequence[Candle], tick: Optional[Dict[str, Any]], side: Side) -> Optional[float]:
    if not candles:
        return None
    last_close = candles[-1].close
    if not tick:
        return last_close

    bid = _to_float(tick.get("bid"), 0.0)
    ask = _to_float(tick.get("ask"), 0.0)

    if side == "BUY" and ask > 0:
        return ask
    if side == "SELL" and bid > 0:
        return bid
    return last_close


def infer_point(symbol: str, tick: Optional[Dict[str, Any]] = None) -> float:
    if tick:
        point = _to_float(tick.get("point"), 0.0)
        if point > 0:
            return point

    s = _clean_symbol(symbol)
    if "JPY" in s:
        return 0.001
    if "XAU" in s or "GOLD" in s:
        return 0.01
    if "BTC" in s or "ETH" in s:
        return 0.01
    if "US100" in s or "NAS100" in s or "JP225" in s:
        return 0.1
    return 0.00001


def spread_points(symbol: str, tick: Optional[Dict[str, Any]]) -> Optional[float]:
    if not tick:
        return None
    if "spread_points" in tick:
        sp = _to_float(tick.get("spread_points"), -1.0)
        return sp if sp >= 0 else None

    bid = _to_float(tick.get("bid"), 0.0)
    ask = _to_float(tick.get("ask"), 0.0)
    point = infer_point(symbol, tick)
    if bid > 0 and ask > 0 and point > 0:
        return abs(ask - bid) / point
    return None


def max_spread_for_symbol(symbol: str, config: EngineConfig) -> float:
    s = _clean_symbol(symbol)
    for key, max_sp in config.max_spread_points_by_symbol.items():
        if key in s:
            return float(max_sp)
    return float(config.max_spread_points_default)


def is_session_ok(now: Optional[datetime], config: EngineConfig) -> bool:
    if not config.session_hours_utc:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).hour in config.session_hours_utc


def spread_score(symbol: str, tick: Optional[Dict[str, Any]], config: EngineConfig) -> Tuple[int, str]:
    sp = spread_points(symbol, tick)
    if sp is None:
        return 0, "spread unknown"

    max_sp = max_spread_for_symbol(symbol, config)
    if sp <= max_sp * 0.55:
        return 5, f"spread good {sp:.1f}/{max_sp:.1f}"
    if sp <= max_sp:
        return 2, f"spread acceptable {sp:.1f}/{max_sp:.1f}"
    return -20, f"spread too high {sp:.1f}/{max_sp:.1f}"


def session_score(now: Optional[datetime], config: EngineConfig) -> Tuple[int, str]:
    if is_session_ok(now, config):
        return 5, "session ok"
    return -10, "session outside preferred hours"


def rr_from_prices(side: Side, entry: Optional[float], sl: Optional[float], tp: Optional[float]) -> Optional[float]:
    if entry is None or sl is None or tp is None:
        return None
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return None
    return reward / risk


def make_sl_tp(
    side: Side,
    entry: Optional[float],
    candles: Sequence[Candle],
    config: EngineConfig,
    structural_sl: Optional[float] = None,
    rr: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if entry is None or side not in ("BUY", "SELL"):
        return None, None, None

    current_atr = atr(candles, 14)
    if not current_atr or current_atr <= 0:
        current_atr = max(candles[-1].range, abs(candles[-1].close - candles[-1].open))

    stop_distance = current_atr * config.stop_atr_multiplier

    if structural_sl is not None:
        if side == "BUY" and structural_sl < entry:
            stop_distance = max(entry - structural_sl, stop_distance * 0.65)
        elif side == "SELL" and structural_sl > entry:
            stop_distance = max(structural_sl - entry, stop_distance * 0.65)

    rr_value = rr or config.default_rr

    if side == "BUY":
        sl = entry - stop_distance
        tp = entry + stop_distance * rr_value
    else:
        sl = entry + stop_distance
        tp = entry - stop_distance * rr_value

    return sl, tp, rr_from_prices(side, entry, sl, tp)


def wait_signal(strategy: str, symbol: str, timeframe: str, reasons: Optional[List[str]] = None) -> StrategySignal:
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side="WAIT",
        score=0.0,
        confidence=0.0,
        entry=None,
        stop_loss=None,
        take_profit=None,
        risk_reward=None,
        reasons=reasons or ["conditions not ready"],
        tags=["wait"],
    )


def avoid_signal(strategy: str, symbol: str, timeframe: str, reasons: Optional[List[str]] = None) -> StrategySignal:
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side="AVOID",
        score=-100.0,
        confidence=0.0,
        entry=None,
        stop_loss=None,
        take_profit=None,
        risk_reward=None,
        reasons=reasons or ["risk filter blocked"],
        tags=["avoid"],
    )


def recent_swing_high(candles: Sequence[Candle], lookback: int = 20, exclude_last: int = 1) -> Optional[float]:
    if len(candles) < lookback + exclude_last:
        return None
    window = candles[-(lookback + exclude_last): -exclude_last]
    return max(c.high for c in window)


def recent_swing_low(candles: Sequence[Candle], lookback: int = 20, exclude_last: int = 1) -> Optional[float]:
    if len(candles) < lookback + exclude_last:
        return None
    window = candles[-(lookback + exclude_last): -exclude_last]
    return min(c.low for c in window)


def bullish_rejection(c: Candle, min_wick_ratio: float = 0.45) -> bool:
    if c.range <= 0:
        return False
    return c.lower_wick / c.range >= min_wick_ratio and c.close >= c.open


def bearish_rejection(c: Candle, min_wick_ratio: float = 0.45) -> bool:
    if c.range <= 0:
        return False
    return c.upper_wick / c.range >= min_wick_ratio and c.close <= c.open


def momentum_ok(candles: Sequence[Candle], side: Side, period: int = 8) -> bool:
    if len(candles) < period + 1:
        return False
    s = slope(closes(candles), period)
    if side == "BUY":
        return s > 0
    if side == "SELL":
        return s < 0
    return False


def trend_direction(candles: Sequence[Candle]) -> str:
    c = closes(candles)
    e20 = ema(c, 20)
    e50 = ema(c, 50)
    if e20 is None or e50 is None:
        return "FLAT"
    if e20 > e50 and slope(c, 12) > 0:
        return "UP"
    if e20 < e50 and slope(c, 12) < 0:
        return "DOWN"
    return "FLAT"


def volume_spike(candles: Sequence[Candle], period: int = 20, multiplier: float = 1.3) -> bool:
    vols = [v for v in volumes(candles) if v > 0]
    if len(vols) < period + 1:
        return False
    avg = sum(vols[-(period + 1):-1]) / period
    return avg > 0 and vols[-1] >= avg * multiplier


def detect_fvg(candles: Sequence[Candle]) -> Optional[Dict[str, Any]]:
    """
    Simple 3-candle fair value gap detector:
        Bullish FVG: candle[-1].low > candle[-3].high
        Bearish FVG: candle[-1].high < candle[-3].low
    """
    if len(candles) < 3:
        return None

    a, b, c = candles[-3], candles[-2], candles[-1]

    if c.low > a.high:
        return {
            "type": "bullish",
            "low": a.high,
            "high": c.low,
            "mid": (a.high + c.low) / 2.0,
        }

    if c.high < a.low:
        return {
            "type": "bearish",
            "low": c.high,
            "high": a.low,
            "mid": (c.high + a.low) / 2.0,
        }

    return None


def price_in_zone(price: float, low: float, high: float, tolerance: float = 0.0) -> bool:
    return low - tolerance <= price <= high + tolerance


# ---------------------------------------------------------------------------
# Strategy 1: Liquidity Sweep Reversal
# ---------------------------------------------------------------------------

def strategy_liquidity_sweep_reversal(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Reversal after sweeping recent high/low and closing back inside.
    Scoring based on the HERMES idea:
        sweep +25
        wick +15
        close-back +15
        MSS/momentum +20
        FVG/order-block context +15
        session +5
        spread +5
    """
    config = config or EngineConfig()
    strategy = "Liquidity Sweep Reversal"
    candles = list(candles)

    if len(candles) < max(35, config.swing_lookback + 5):
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    last = candles[-1]
    prev_high = recent_swing_high(candles, config.swing_lookback)
    prev_low = recent_swing_low(candles, config.swing_lookback)

    if prev_high is None or prev_low is None:
        return wait_signal(strategy, symbol, timeframe)

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    swept_high = last.high > prev_high and last.close < prev_high
    swept_low = last.low < prev_low and last.close > prev_low

    if swept_low:
        side = "BUY"
        score += 25
        structural_sl = last.low
        reasons.append(f"swept liquidity low {prev_low:.5f} and closed back above")
        if bullish_rejection(last, 0.40):
            score += 15
            reasons.append("bullish rejection wick")
        if last.close > prev_low:
            score += 15
            reasons.append("close-back confirmed")
        if momentum_ok(candles, "BUY", 6) or last.close > candles[-2].high:
            score += 20
            reasons.append("micro market structure shift up")

    elif swept_high:
        side = "SELL"
        score += 25
        structural_sl = last.high
        reasons.append(f"swept liquidity high {prev_high:.5f} and closed back below")
        if bearish_rejection(last, 0.40):
            score += 15
            reasons.append("bearish rejection wick")
        if last.close < prev_high:
            score += 15
            reasons.append("close-back confirmed")
        if momentum_ok(candles, "SELL", 6) or last.close < candles[-2].low:
            score += 20
            reasons.append("micro market structure shift down")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["no valid liquidity sweep"])

    fvg = detect_fvg(candles[-8:])
    if fvg:
        if (side == "BUY" and fvg["type"] == "bullish") or (side == "SELL" and fvg["type"] == "bearish"):
            score += 10
            reasons.append(f"{fvg['type']} FVG context")
        else:
            score += 3
            reasons.append("opposite FVG nearby; reduced conviction")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=1.8)

    confidence = max(0.0, min(1.0, score / 100.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 45 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["liquidity", "sweep", "reversal"],
        metadata={
            "prev_swing_high": prev_high,
            "prev_swing_low": prev_low,
            "swept_high": swept_high,
            "swept_low": swept_low,
        },
    )


# ---------------------------------------------------------------------------
# Strategy 2: Stop Hunt Continuation
# ---------------------------------------------------------------------------

def strategy_stop_hunt_continuation(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Continuation setup:
        - Higher timeframe-like local trend by EMA20/EMA50.
        - Price hunts opposite side liquidity.
        - Then closes back with trend.
    """
    config = config or EngineConfig()
    strategy = "Stop Hunt Continuation"
    candles = list(candles)

    if len(candles) < 60:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    last = candles[-1]
    trend = trend_direction(candles)
    prev_high = recent_swing_high(candles, config.swing_lookback)
    prev_low = recent_swing_low(candles, config.swing_lookback)

    if prev_high is None or prev_low is None:
        return wait_signal(strategy, symbol, timeframe)

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    if trend == "UP" and last.low < prev_low and last.close > prev_low:
        side = "BUY"
        score += 25
        structural_sl = last.low
        reasons.append("uptrend + stop hunt below range")
        if last.close > last.open:
            score += 10
            reasons.append("bullish close after hunt")
        if last.close > ema(closes(candles), 20):
            score += 15
            reasons.append("reclaimed EMA20")
        if volume_spike(candles):
            score += 10
            reasons.append("volume spike on hunt")

    elif trend == "DOWN" and last.high > prev_high and last.close < prev_high:
        side = "SELL"
        score += 25
        structural_sl = last.high
        reasons.append("downtrend + stop hunt above range")
        if last.close < last.open:
            score += 10
            reasons.append("bearish close after hunt")
        e20 = ema(closes(candles), 20)
        if e20 is not None and last.close < e20:
            score += 15
            reasons.append("rejected EMA20")
        if volume_spike(candles):
            score += 10
            reasons.append("volume spike on hunt")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, [f"no continuation hunt; trend={trend}"])

    if momentum_ok(candles, side, 8):
        score += 15
        reasons.append("momentum aligned with continuation")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=2.0)

    confidence = max(0.0, min(1.0, score / 95.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 55 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["stop-hunt", "continuation", trend.lower()],
        metadata={"trend": trend, "prev_high": prev_high, "prev_low": prev_low},
    )


# ---------------------------------------------------------------------------
# Strategy 3: Range Liquidity Rotation
# ---------------------------------------------------------------------------

def strategy_range_liquidity_rotation(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Range strategy:
        - Detect relatively sideways market.
        - Buy lower third / sell upper third after rejection.
        - Targets middle/opposite side of range.
    """
    config = config or EngineConfig()
    strategy = "Range Liquidity Rotation"
    candles = list(candles)

    lb = config.range_lookback
    if len(candles) < lb + 5:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    window = candles[-lb:]
    range_high = max(c.high for c in window)
    range_low = min(c.low for c in window)
    range_mid = (range_high + range_low) / 2.0
    range_size = range_high - range_low
    current_atr = atr(candles, 14) or 0.0

    if range_size <= 0 or current_atr <= 0:
        return wait_signal(strategy, symbol, timeframe)

    # If range is too wide relative to ATR or trend is too strong, avoid this range system.
    trend = trend_direction(candles)
    price_pos = percentile_position(candles[-1].close, range_low, range_high)
    compact_range = range_size <= current_atr * 9.0

    if not compact_range or trend not in ("FLAT", "UP", "DOWN"):
        return wait_signal(strategy, symbol, timeframe, ["market not range-like"])

    last = candles[-1]
    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None
    custom_tp: Optional[float] = None

    if price_pos <= 0.28 and (bullish_rejection(last, 0.35) or last.close > last.open):
        side = "BUY"
        structural_sl = min(last.low, range_low)
        custom_tp = range_mid
        score += 35
        reasons.append("price in lower third of range")
        if last.low <= range_low + current_atr * 0.35:
            score += 15
            reasons.append("range low liquidity touched")
        if bullish_rejection(last):
            score += 15
            reasons.append("bullish rejection at range low")

    elif price_pos >= 0.72 and (bearish_rejection(last, 0.35) or last.close < last.open):
        side = "SELL"
        structural_sl = max(last.high, range_high)
        custom_tp = range_mid
        score += 35
        reasons.append("price in upper third of range")
        if last.high >= range_high - current_atr * 0.35:
            score += 15
            reasons.append("range high liquidity touched")
        if bearish_rejection(last):
            score += 15
            reasons.append("bearish rejection at range high")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["range detected but no edge zone rejection"])

    if volume_spike(candles, 20, 1.2):
        score += 5
        reasons.append("volume confirmation")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=1.4)

    # Prefer range midpoint TP if it still gives a positive reward.
    if entry is not None and custom_tp is not None:
        if side == "BUY" and custom_tp > entry:
            tp = custom_tp
        elif side == "SELL" and custom_tp < entry:
            tp = custom_tp
        rr = rr_from_prices(side, entry, sl, tp)

    confidence = max(0.0, min(1.0, score / 90.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 55 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["range", "liquidity", "rotation"],
        metadata={
            "range_high": range_high,
            "range_low": range_low,
            "range_mid": range_mid,
            "price_position": price_pos,
            "atr": current_atr,
        },
    )


# ---------------------------------------------------------------------------
# Strategy 4: Trend Momentum Scalper
# ---------------------------------------------------------------------------

def strategy_trend_momentum_scalper(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Fast scalping trend-following:
        - EMA20/EMA50 direction
        - RSI confirms momentum
        - Pullback/reclaim near EMA20
        - Volume/ATR confirmation
    """
    config = config or EngineConfig()
    strategy = "Trend Momentum Scalper"
    candles = list(candles)

    if len(candles) < 70:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    c = closes(candles)
    last = candles[-1]
    e20 = ema(c, 20)
    e50 = ema(c, 50)
    current_rsi = rsi(c, 14)
    current_atr = atr(candles, 14)

    if e20 is None or e50 is None or current_rsi is None or not current_atr:
        return wait_signal(strategy, symbol, timeframe)

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []

    if e20 > e50 and last.close > e20 and 52 <= current_rsi <= 72:
        side = "BUY"
        score += 25
        reasons.append("EMA20 above EMA50")
        score += 15
        reasons.append(f"RSI bullish momentum {current_rsi:.1f}")
        if candles[-2].low <= e20 <= max(candles[-2].high, last.high):
            score += 15
            reasons.append("pullback/reclaim near EMA20")
        if slope(c, 12) > 0:
            score += 15
            reasons.append("positive close slope")

    elif e20 < e50 and last.close < e20 and 28 <= current_rsi <= 48:
        side = "SELL"
        score += 25
        reasons.append("EMA20 below EMA50")
        score += 15
        reasons.append(f"RSI bearish momentum {current_rsi:.1f}")
        if candles[-2].high >= e20 >= min(candles[-2].low, last.low):
            score += 15
            reasons.append("pullback/rejection near EMA20")
        if slope(c, 12) < 0:
            score += 15
            reasons.append("negative close slope")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["trend momentum conditions not aligned"])

    if volume_spike(candles, 20, 1.2):
        score += 8
        reasons.append("volume spike")
    if current_atr > 0 and last.range >= current_atr * 0.7:
        score += 7
        reasons.append("candle range confirms momentum")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    structural_sl = min(c.low for c in candles[-5:]) if side == "BUY" else max(c.high for c in candles[-5:])
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=1.6)

    confidence = max(0.0, min(1.0, score / 95.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 58 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["scalping", "trend", "momentum"],
        metadata={"ema20": e20, "ema50": e50, "rsi": current_rsi, "atr": current_atr},
    )


# ---------------------------------------------------------------------------
# Strategy 5: RSI Mean Reversion
# ---------------------------------------------------------------------------

def strategy_rsi_mean_reversion(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Mean reversion:
        - RSI oversold/overbought
        - Price extended from EMA20
        - Rejection candle
    """
    config = config or EngineConfig()
    strategy = "RSI Mean Reversion"
    candles = list(candles)

    if len(candles) < 45:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    c = closes(candles)
    last = candles[-1]
    current_rsi = rsi(c, 14)
    e20 = ema(c, 20)
    current_atr = atr(candles, 14)

    if current_rsi is None or e20 is None or not current_atr:
        return wait_signal(strategy, symbol, timeframe)

    distance = abs(last.close - e20)
    extended = distance >= current_atr * 0.8

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    if current_rsi <= 30 and last.close < e20 and extended and bullish_rejection(last, 0.35):
        side = "BUY"
        structural_sl = last.low
        score += 30
        reasons.append(f"RSI oversold {current_rsi:.1f}")
        score += 15
        reasons.append("price extended below EMA20")
        score += 15
        reasons.append("bullish rejection candle")

    elif current_rsi >= 70 and last.close > e20 and extended and bearish_rejection(last, 0.35):
        side = "SELL"
        structural_sl = last.high
        score += 30
        reasons.append(f"RSI overbought {current_rsi:.1f}")
        score += 15
        reasons.append("price extended above EMA20")
        score += 15
        reasons.append("bearish rejection candle")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["no RSI mean reversion setup"])

    # Mean reversion works better in non-strong trend. Penalize extreme trend.
    trend = trend_direction(candles)
    if trend == "FLAT":
        score += 10
        reasons.append("flat trend supports mean reversion")
    else:
        score -= 5
        reasons.append(f"trend={trend}; mean reversion reduced")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr_value = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=1.25)

    # Prefer EMA20 as conservative target when useful.
    if entry is not None:
        if side == "BUY" and e20 > entry:
            tp = e20
        elif side == "SELL" and e20 < entry:
            tp = e20

    rr = rr_from_prices(side, entry, sl, tp)
    confidence = max(0.0, min(1.0, score / 85.0))

    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 52 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["rsi", "mean-reversion"],
        metadata={"rsi": current_rsi, "ema20": e20, "atr": current_atr, "trend": trend},
    )


# ---------------------------------------------------------------------------
# Strategy 6: Breakout Retest
# ---------------------------------------------------------------------------

def strategy_breakout_retest(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Breakout + retest:
        - Recent consolidation high/low breaks.
        - Current/last candle retests level and closes in breakout direction.
    """
    config = config or EngineConfig()
    strategy = "Breakout Retest"
    candles = list(candles)

    if len(candles) < 55:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    lb = min(30, len(candles) - 5)
    prior = candles[-(lb + 3):-3]
    if len(prior) < 15:
        return wait_signal(strategy, symbol, timeframe)

    level_high = max(c.high for c in prior)
    level_low = min(c.low for c in prior)
    current_atr = atr(candles, 14) or candles[-1].range

    breakout_candle = candles[-2]
    retest_candle = candles[-1]

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    broke_up = breakout_candle.close > level_high
    retested_up = retest_candle.low <= level_high + current_atr * 0.25 and retest_candle.close > level_high

    broke_down = breakout_candle.close < level_low
    retested_down = retest_candle.high >= level_low - current_atr * 0.25 and retest_candle.close < level_low

    if broke_up and retested_up:
        side = "BUY"
        structural_sl = min(retest_candle.low, level_high - current_atr * 0.3)
        score += 35
        reasons.append("bullish breakout then retest")
        if trend_direction(candles) == "UP":
            score += 15
            reasons.append("trend aligned up")
        if retest_candle.close > retest_candle.open:
            score += 10
            reasons.append("bullish retest candle")

    elif broke_down and retested_down:
        side = "SELL"
        structural_sl = max(retest_candle.high, level_low + current_atr * 0.3)
        score += 35
        reasons.append("bearish breakout then retest")
        if trend_direction(candles) == "DOWN":
            score += 15
            reasons.append("trend aligned down")
        if retest_candle.close < retest_candle.open:
            score += 10
            reasons.append("bearish retest candle")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["no breakout-retest confirmation"])

    if volume_spike(candles, 20, 1.15):
        score += 8
        reasons.append("volume confirms breakout/retest")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=2.0)

    confidence = max(0.0, min(1.0, score / 90.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 58 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["breakout", "retest"],
        metadata={"level_high": level_high, "level_low": level_low},
    )


# ---------------------------------------------------------------------------
# Strategy 7: FVG / Order Block Retest
# ---------------------------------------------------------------------------

def strategy_fvg_order_block_retest(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Lightweight FVG/OB style signal:
        - Search a recent fair value gap.
        - Wait for price to retest zone.
        - Trade in the direction of the FVG if trend/momentum agrees.
    """
    config = config or EngineConfig()
    strategy = "FVG Order Block Retest"
    candles = list(candles)

    if len(candles) < 70:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    recent = candles[-30:]
    fvg_found: Optional[Dict[str, Any]] = None
    fvg_index = None

    # Find latest FVG within recent window.
    for i in range(3, len(recent) + 1):
        local = detect_fvg(recent[:i])
        if local:
            fvg_found = local
            fvg_index = i
    if not fvg_found:
        return wait_signal(strategy, symbol, timeframe, ["no recent FVG"])

    last = candles[-1]
    current_atr = atr(candles, 14) or last.range
    tolerance = current_atr * 0.20
    in_zone = price_in_zone(last.close, fvg_found["low"], fvg_found["high"], tolerance) or price_in_zone(
        last.low if fvg_found["type"] == "bullish" else last.high,
        fvg_found["low"],
        fvg_found["high"],
        tolerance,
    )

    if not in_zone:
        return wait_signal(strategy, symbol, timeframe, ["FVG exists but price not retesting zone"])

    side: Side = "BUY" if fvg_found["type"] == "bullish" else "SELL"
    score = 30
    reasons = [f"{fvg_found['type']} FVG retest"]

    trend = trend_direction(candles)
    if side == "BUY" and trend == "UP":
        score += 15
        reasons.append("trend aligned up")
    elif side == "SELL" and trend == "DOWN":
        score += 15
        reasons.append("trend aligned down")
    else:
        score -= 5
        reasons.append(f"trend not fully aligned: {trend}")

    if (side == "BUY" and bullish_rejection(last, 0.30)) or (side == "SELL" and bearish_rejection(last, 0.30)):
        score += 15
        reasons.append("rejection candle inside zone")

    if momentum_ok(candles, side, 8):
        score += 10
        reasons.append("short-term momentum aligned")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    structural_sl = fvg_found["low"] - current_atr * 0.35 if side == "BUY" else fvg_found["high"] + current_atr * 0.35
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=2.0)

    confidence = max(0.0, min(1.0, score / 90.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 55 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["fvg", "order-block", "retest"],
        metadata={"fvg": fvg_found, "fvg_index": fvg_index, "trend": trend},
    )


# ---------------------------------------------------------------------------
# Strategy 8: Fibonacci Golden Pullback
# ---------------------------------------------------------------------------

def strategy_fibonacci_golden_pullback(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Fibonacci pullback strategy:
        - Detect trend impulse over lookback.
        - Wait for pullback into 0.382 - 0.618 golden zone.
        - Need rejection/momentum confirmation.
    """
    config = config or EngineConfig()
    strategy = "Fibonacci Golden Pullback"
    candles = list(candles)

    if len(candles) < 80:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    window = candles[-55:]
    impulse_high = max(c.high for c in window[:-3])
    impulse_low = min(c.low for c in window[:-3])
    impulse = impulse_high - impulse_low
    if impulse <= 0:
        return wait_signal(strategy, symbol, timeframe)

    trend = trend_direction(candles)
    last = candles[-1]
    current_atr = atr(candles, 14) or last.range

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    if trend == "UP":
        # Retracement from high down to golden zone.
        fib_382 = impulse_high - impulse * 0.382
        fib_618 = impulse_high - impulse * 0.618
        zone_low, zone_high = min(fib_382, fib_618), max(fib_382, fib_618)
        in_golden_zone = price_in_zone(last.close, zone_low, zone_high, current_atr * 0.20) or price_in_zone(
            last.low, zone_low, zone_high, current_atr * 0.20
        )
        if in_golden_zone and bullish_rejection(last, 0.30):
            side = "BUY"
            structural_sl = min(last.low, zone_low - current_atr * 0.25)
            score += 35
            reasons.append("uptrend pullback into 0.382-0.618 golden zone")
            score += 15
            reasons.append("bullish rejection in golden zone")

    elif trend == "DOWN":
        # Retracement from low up to golden zone.
        fib_382 = impulse_low + impulse * 0.382
        fib_618 = impulse_low + impulse * 0.618
        zone_low, zone_high = min(fib_382, fib_618), max(fib_382, fib_618)
        in_golden_zone = price_in_zone(last.close, zone_low, zone_high, current_atr * 0.20) or price_in_zone(
            last.high, zone_low, zone_high, current_atr * 0.20
        )
        if in_golden_zone and bearish_rejection(last, 0.30):
            side = "SELL"
            structural_sl = max(last.high, zone_high + current_atr * 0.25)
            score += 35
            reasons.append("downtrend pullback into 0.382-0.618 golden zone")
            score += 15
            reasons.append("bearish rejection in golden zone")

    else:
        return wait_signal(strategy, symbol, timeframe, ["no clear trend for Fibonacci pullback"])

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["price not confirmed in Fibonacci golden zone"])

    if momentum_ok(candles, side, 6):
        score += 10
        reasons.append("momentum aligned after pullback")

    if volume_spike(candles, 20, 1.15):
        score += 5
        reasons.append("volume confirmation")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=2.2)

    confidence = max(0.0, min(1.0, score / 90.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 58 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["fibonacci", "golden-zone", "pullback"],
        metadata={
            "trend": trend,
            "impulse_high": impulse_high,
            "impulse_low": impulse_low,
            "impulse": impulse,
        },
    )


# ---------------------------------------------------------------------------
# Strategy 9: Volume Spike Reversal
# ---------------------------------------------------------------------------

def strategy_volume_spike_reversal(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Reversal after abnormal tick volume + long wick.
    Works only if broker provides useful tick_volume.
    """
    config = config or EngineConfig()
    strategy = "Volume Spike Reversal"
    candles = list(candles)

    if len(candles) < 45:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    last = candles[-1]
    current_atr = atr(candles, 14) or last.range
    if not volume_spike(candles, 20, 1.6):
        return wait_signal(strategy, symbol, timeframe, ["no volume spike"])

    side: Side = "WAIT"
    score = 25
    reasons = ["abnormal tick volume spike"]
    structural_sl: Optional[float] = None

    if bullish_rejection(last, 0.45) and last.range >= current_atr * 0.85:
        side = "BUY"
        structural_sl = last.low
        score += 30
        reasons.append("bullish rejection with large range")
    elif bearish_rejection(last, 0.45) and last.range >= current_atr * 0.85:
        side = "SELL"
        structural_sl = last.high
        score += 30
        reasons.append("bearish rejection with large range")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["volume spike but no wick rejection"])

    # Better when at local extreme.
    prev_high = recent_swing_high(candles, 20)
    prev_low = recent_swing_low(candles, 20)
    if side == "BUY" and prev_low is not None and last.low <= prev_low + current_atr * 0.25:
        score += 10
        reasons.append("near local low")
    if side == "SELL" and prev_high is not None and last.high >= prev_high - current_atr * 0.25:
        score += 10
        reasons.append("near local high")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=1.7)

    confidence = max(0.0, min(1.0, score / 90.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 57 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["volume", "spike", "reversal"],
        metadata={"last_volume": last.volume, "atr": current_atr},
    )


# ---------------------------------------------------------------------------
# Strategy 10: ATR Volatility Breakout
# ---------------------------------------------------------------------------

def strategy_atr_volatility_breakout(
    symbol: str,
    candles: Sequence[Candle],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
    now: Optional[datetime] = None,
) -> StrategySignal:
    """
    Volatility expansion:
        - Previous candles compressed.
        - Current candle closes outside compression range.
        - ATR/range expansion confirms.
    """
    config = config or EngineConfig()
    strategy = "ATR Volatility Breakout"
    candles = list(candles)

    if len(candles) < 65:
        return wait_signal(strategy, symbol, timeframe, ["not enough candles"])

    compression = candles[-22:-2]
    trigger = candles[-1]
    if len(compression) < 15:
        return wait_signal(strategy, symbol, timeframe)

    comp_high = max(c.high for c in compression)
    comp_low = min(c.low for c in compression)
    comp_range = comp_high - comp_low
    current_atr = atr(candles, 14) or trigger.range
    avg_range = statistics.mean([c.range for c in compression if c.range > 0]) if compression else 0.0

    compressed = comp_range <= current_atr * 6.0 and avg_range <= current_atr * 0.9
    if not compressed:
        return wait_signal(strategy, symbol, timeframe, ["no compression before breakout"])

    side: Side = "WAIT"
    score = 0
    reasons: List[str] = []
    structural_sl: Optional[float] = None

    if trigger.close > comp_high and trigger.range >= current_atr * 0.9:
        side = "BUY"
        structural_sl = min(trigger.low, comp_high - current_atr * 0.25)
        score += 35
        reasons.append("bullish volatility breakout after compression")
        if trigger.close > trigger.open:
            score += 10
            reasons.append("bullish trigger candle")
    elif trigger.close < comp_low and trigger.range >= current_atr * 0.9:
        side = "SELL"
        structural_sl = max(trigger.high, comp_low + current_atr * 0.25)
        score += 35
        reasons.append("bearish volatility breakout after compression")
        if trigger.close < trigger.open:
            score += 10
            reasons.append("bearish trigger candle")

    if side == "WAIT":
        return wait_signal(strategy, symbol, timeframe, ["compression found but no confirmed breakout"])

    trend = trend_direction(candles)
    if (side == "BUY" and trend == "UP") or (side == "SELL" and trend == "DOWN"):
        score += 15
        reasons.append(f"trend aligned {trend}")
    elif trend == "FLAT":
        score += 5
        reasons.append("flat-to-breakout regime")

    if volume_spike(candles, 20, 1.2):
        score += 10
        reasons.append("volume expansion")

    sess_add, sess_reason = session_score(now, config)
    spr_add, spr_reason = spread_score(symbol, tick, config)
    score += sess_add + spr_add
    reasons.extend([sess_reason, spr_reason])

    if spr_add < 0:
        return avoid_signal(strategy, symbol, timeframe, reasons)

    entry = get_entry(symbol, candles, tick, side)
    sl, tp, rr = make_sl_tp(side, entry, candles, config, structural_sl=structural_sl, rr=2.0)

    confidence = max(0.0, min(1.0, score / 95.0))
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        side=side if score >= 60 else "WAIT",
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        reasons=reasons,
        tags=["atr", "volatility", "breakout"],
        metadata={"compression_high": comp_high, "compression_low": comp_low, "atr": current_atr},
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class HermesStrategyEngine:
    """
    Central strategy runner for HERMES.

    Returns:
        {
            "symbol": ...,
            "timeframe": ...,
            "best_signal": {...},
            "signals": [...],
            "risk_notes": [...],
            "engine_version": "..."
        }
    """

    engine_version = "2026.06.13-strategies-pack-v1"

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig()
        self.strategies = [
            strategy_liquidity_sweep_reversal,
            strategy_stop_hunt_continuation,
            strategy_range_liquidity_rotation,
            strategy_trend_momentum_scalper,
            strategy_rsi_mean_reversion,
            strategy_breakout_retest,
            strategy_fvg_order_block_retest,
            strategy_fibonacci_golden_pullback,
            strategy_volume_spike_reversal,
            strategy_atr_volatility_breakout,
        ]

    def symbol_allowed(self, symbol: str) -> bool:
        if self.config.allow_all_symbols:
            return True
        if not self.config.allowed_symbols:
            return False
        clean = _clean_symbol(symbol)
        return any(_clean_symbol(s) == clean or _clean_symbol(s) in clean for s in self.config.allowed_symbols)

    def analyze(
        self,
        symbol: str,
        candles: Sequence[Any],
        tick: Optional[Dict[str, Any]] = None,
        timeframe: str = "M5",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        normalized = normalize_candles(candles, limit=self.config.lookback)
        risk_notes: List[str] = []

        if not self.symbol_allowed(symbol):
            sig = avoid_signal("Engine Risk Filter", symbol, timeframe, ["symbol not allowed by config"])
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "best_signal": sig.to_dict(),
                "signals": [sig.to_dict()],
                "risk_notes": ["symbol blocked"],
                "engine_version": self.engine_version,
            }

        if len(normalized) < 35:
            sig = wait_signal("Engine", symbol, timeframe, ["not enough candles for engine"])
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "best_signal": sig.to_dict(),
                "signals": [sig.to_dict()],
                "risk_notes": ["need at least 35 candles, recommended 120"],
                "engine_version": self.engine_version,
            }

        sp = spread_points(symbol, tick)
        max_sp = max_spread_for_symbol(symbol, self.config)
        if sp is not None and sp > max_sp:
            risk_notes.append(f"spread above max: {sp:.1f}>{max_sp:.1f}")

        signals: List[StrategySignal] = []
        for fn in self.strategies:
            try:
                sig = fn(
                    symbol=symbol,
                    candles=normalized,
                    tick=tick,
                    timeframe=timeframe,
                    config=self.config,
                    now=now,
                )
                signals.append(sig)
            except Exception as exc:
                signals.append(
                    StrategySignal(
                        strategy=getattr(fn, "__name__", "unknown_strategy"),
                        symbol=symbol,
                        timeframe=timeframe,
                        side="WAIT",
                        score=0.0,
                        confidence=0.0,
                        entry=None,
                        stop_loss=None,
                        take_profit=None,
                        risk_reward=None,
                        reasons=[f"strategy error: {type(exc).__name__}: {exc}"],
                        tags=["error"],
                    )
                )

        best = self.select_best_signal(signals)

        # Final engine-level threshold gate.
        if best.side in ("BUY", "SELL") and (
            best.score < self.config.min_score_to_trade
            or best.confidence < self.config.min_confidence_to_trade
        ):
            best = StrategySignal(
                strategy=f"Engine Gate / {best.strategy}",
                symbol=symbol,
                timeframe=timeframe,
                side="WAIT",
                score=best.score,
                confidence=best.confidence,
                entry=best.entry,
                stop_loss=best.stop_loss,
                take_profit=best.take_profit,
                risk_reward=best.risk_reward,
                reasons=[
                    f"best setup below threshold: score={best.score:.1f}, confidence={best.confidence:.2f}",
                    *best.reasons,
                ],
                tags=[*best.tags, "threshold-gated"],
                metadata=best.metadata,
            )

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "best_signal": best.to_dict(),
            "signals": [s.to_dict() for s in sorted(signals, key=lambda x: x.score, reverse=True)],
            "risk_notes": risk_notes,
            "engine_version": self.engine_version,
        }

    @staticmethod
    def select_best_signal(signals: Sequence[StrategySignal]) -> StrategySignal:
        tradable = [s for s in signals if s.side in ("BUY", "SELL")]
        if tradable:
            # Prefer high score, then good RR, then confidence.
            return max(
                tradable,
                key=lambda s: (
                    s.score,
                    s.risk_reward or 0.0,
                    s.confidence,
                ),
            )

        avoid = [s for s in signals if s.side == "AVOID"]
        if avoid:
            return max(avoid, key=lambda s: s.score)

        if signals:
            return max(signals, key=lambda s: s.score)

        return wait_signal("Engine", "UNKNOWN", "UNKNOWN", ["no strategies returned"])

    def supabase_payload(
        self,
        result: Dict[str, Any],
        account_id: Optional[str] = None,
        mode: str = "DEMO",
    ) -> Dict[str, Any]:
        """
        Optional helper for your Supabase ai_decisions table.

        Suggested columns:
            symbol text
            timeframe text
            decision text
            score numeric
            confidence numeric
            reasoning jsonb
            mode text
            account_id text nullable
        """
        best = result.get("best_signal", {})
        return {
            "account_id": account_id,
            "symbol": result.get("symbol"),
            "timeframe": result.get("timeframe"),
            "decision": best.get("side", "WAIT"),
            "score": best.get("score", 0),
            "confidence": best.get("confidence", 0),
            "mode": mode,
            "reasoning": {
                "best_signal": best,
                "signals": result.get("signals", []),
                "risk_notes": result.get("risk_notes", []),
                "engine_version": result.get("engine_version"),
            },
        }


def analyze_symbol(
    symbol: str,
    candles: Sequence[Any],
    tick: Optional[Dict[str, Any]] = None,
    timeframe: str = "M5",
    config: Optional[EngineConfig] = None,
) -> Dict[str, Any]:
    """Convenience function for old backend style."""
    return HermesStrategyEngine(config=config).analyze(
        symbol=symbol,
        candles=candles,
        tick=tick,
        timeframe=timeframe,
    )


# ---------------------------------------------------------------------------
# Minimal self-test / example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Tiny synthetic example. Replace with MT5 rates in your backend.
    sample: List[Dict[str, float]] = []
    price = 100.0
    for i in range(130):
        drift = 0.04 if i < 100 else -0.02
        open_ = price
        close = price + drift + (0.15 if i % 9 == 0 else -0.03)
        high = max(open_, close) + 0.20
        low = min(open_, close) - 0.20
        if i == 128:
            low -= 1.4  # fake liquidity sweep low
        if i == 129:
            close = open_ + 0.8
            high = close + 0.2
            low = open_ - 0.7
        sample.append({
            "time": i,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": 100 + i,
        })
        price = close

    engine = HermesStrategyEngine()
    output = engine.analyze(
        symbol="XAUUSD",
        candles=sample,
        tick={"bid": sample[-1]["close"] - 0.03, "ask": sample[-1]["close"] + 0.03, "point": 0.01},
        timeframe="M5",
    )

    import json
    print(json.dumps(output["best_signal"], indent=2))
