from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from app.config import Settings
from app.utils.broker_time import broker_now_utc


TIME_GATE_FIELDS = [
    "utc_time",
    "casablanca_time",
    "broker_time_estimate",
    "broker_utc_offset_hours",
    "local_hour",
    "utc_hour",
    "broker_hour",
    "weekday",
    "is_weekend",
    "symbol_market_open",
    "session_name",
    "asia_window",
    "asia_trading_allowed",
    "asia_block_reason",
    "is_bad_hour",
    "is_news_blackout",
    "time_gate_status",
    "time_gate_reason",
]

USER_DISABLED_TIME_BLOCK_REASONS = {
    "ASIA_PREOPEN_BAD_LIQUIDITY",
    "ASIA_LATE_STRICT_MODE",
    "BAD_LIQUIDITY_HOUR",
    "BTC_BAD_HOUR_BLOCK",
    "BTC_BAD_HOUR_BLOCKED",
    "BTC_WEEKEND_ANALYSIS_ONLY",
    "OFF_HOURS",
    "ROLLOVER_BLOCK",
    "SESSION_NOT_ALLOWED",
    "WAITING_FOR_SESSION",
}
USER_DISABLED_TIME_BLOCK_LOG_REASONS = [
    "BAD_LIQUIDITY_HOUR",
    "WAITING_FOR_SESSION",
    "BTC_BAD_HOUR_BLOCK",
    "OFF_HOURS",
]


@dataclass(frozen=True)
class TimeEngine:
    settings: Settings

    def evaluate(
        self,
        symbol: str,
        frames: dict | None = None,
        tick: dict | object | None = None,
        now: datetime | None = None,
        news_blackout: bool | None = None,
    ) -> dict:
        utc_dt = _as_utc(now) or broker_now_utc()
        local_zone = _zone(getattr(self.settings, "timezone_local", None) or self.settings.report_timezone)
        local_dt = utc_dt.astimezone(local_zone)
        broker_dt = _broker_time_estimate(frames or {}, tick, utc_dt)
        broker_offset = round((broker_dt.utcoffset() or timedelta()).total_seconds() / 3600.0, 2)
        broker_hour = broker_dt.hour
        session = _session_by_utc(utc_dt)
        weekend = local_dt.weekday() >= 5
        asia_window = _asia_window_by_local(local_dt)
        if asia_window != "NONE":
            session = asia_window
        market_open = _symbol_market_open(symbol, local_dt, broker_dt, tick)
        bad_hour = _is_bad_hour(symbol, local_dt.hour, self.settings)
        status, reason = _time_gate(symbol, local_dt, market_open, bad_hour, news_blackout, self.settings, asia_window, tick=tick)
        effective_bad_hour = bool(bad_hour and asia_window != "ASIA_MAIN")
        ignored_time_blocks = False
        ignored_time_block_reasons: list[str] = []
        if _demo_ignore_time_blocks(self.settings) and market_open and reason in USER_DISABLED_TIME_BLOCK_REASONS:
            ignored_time_blocks = True
            ignored_time_block_reasons = list(dict.fromkeys([reason, *USER_DISABLED_TIME_BLOCK_LOG_REASONS]))
            status = "PASS"
            reason = "TIME_BLOCKS_DISABLED_BY_USER_ORDER"
            effective_bad_hour = False
            print(
                "[TIME_GATE] decision=PASS reason=TIME_BLOCKS_DISABLED_BY_USER_ORDER ignored=%s"
                % ",".join(USER_DISABLED_TIME_BLOCK_LOG_REASONS)
            )
            print("[TIME_GATE_OVERRIDE] symbol=%s reason=USER_DISABLED_TIME_BLOCKS" % symbol)
        if session == "WEEKEND" or weekend:
            session = "WEEKEND"
        asia_block_reason = None
        if asia_window in {"ASIA_PREOPEN", "ASIA_LATE"}:
            asia_block_reason = reason
        elif asia_window == "ASIA_MAIN" and status != "PASS":
            asia_block_reason = reason
        out = {
            "utc_time": utc_dt.isoformat(),
            "casablanca_time": local_dt.isoformat(),
            "broker_time_estimate": broker_dt.isoformat(),
            "broker_utc_offset_hours": broker_offset,
            "local_hour": local_dt.hour,
            "utc_hour": utc_dt.hour,
            "broker_hour": broker_hour,
            "weekday": local_dt.strftime("%A").upper(),
            "is_weekend": weekend,
            "symbol_market_open": market_open,
            "session_name": session,
            "asia_window": asia_window,
            "asia_trading_allowed": bool(asia_window == "ASIA_MAIN" and status == "PASS" and not weekend),
            "asia_block_reason": asia_block_reason,
            "is_bad_hour": effective_bad_hour,
            "is_news_blackout": bool(news_blackout) if news_blackout is not None else "unknown",
            "time_gate_status": status,
            "time_gate_reason": reason,
            "ignored_time_blocks": ignored_time_blocks,
            "ignored_time_block_reasons": ignored_time_block_reasons,
        }
        print(
            "[TIME_GATE] symbol=%s utc=%s local=%s broker=%s session=%s market_open=%s bad_hour=%s decision=%s reason=%s"
            % (
                symbol,
                out["utc_time"],
                out["casablanca_time"],
                out["broker_time_estimate"],
                out["session_name"],
                out["symbol_market_open"],
                out["is_bad_hour"],
                out["time_gate_status"],
                out["time_gate_reason"],
            )
        )
        print(
            "[SESSION_DIAG] utc_hour=%s local_hour=%s broker_hour=%s session=%s time_gate=%s"
            % (
                out["utc_hour"],
                out["local_hour"],
                out["broker_hour"],
                out["session_name"],
                out["time_gate_status"],
            )
        )
        print(
            "[TIME_GATE] session=%s decision=%s reason=%s"
            % (
                out["session_name"],
                out["time_gate_status"],
                out["time_gate_reason"],
            )
        )
        return out


def _time_gate(
    symbol: str,
    local_dt: datetime,
    market_open: bool,
    bad_hour: bool,
    news_blackout: bool | None,
    settings: Settings,
    asia_window: str = "NONE",
    tick: dict | object | None = None,
) -> tuple[str, str]:
    normalized = _symbol(symbol)
    weekend = local_dt.weekday() >= 5
    if news_blackout is True:
        return "BLOCK", "NEWS_BLACKOUT"
    if asia_window == "ASIA_PREOPEN":
        return "BLOCK", "ASIA_PREOPEN_BAD_LIQUIDITY"
    if asia_window == "ASIA_LATE":
        return "BLOCK", "ASIA_LATE_STRICT_MODE"
    if _is_crypto_symbol(normalized):
        crypto_247 = getattr(settings, "crypto_24_7_enabled", True)
        if weekend and crypto_247:
            # Crypto on weekend: use broker tick status, not day-of-week alone.
            if tick is None or not _tick_available(tick):
                return "BLOCK", "NO_RECENT_TICK"
            if _tick_broker_closed(tick):
                return "BLOCK", "BROKER_SESSION_CLOSED"
            if _tick_tradable(tick):
                reason = "BTC_24_7_ALLOWED" if normalized.startswith("BTCUSD") else "CRYPTO_24_7_ALLOWED"
                return "PASS", reason
            return "BLOCK", "NO_RECENT_TICK"
        # Crypto on weekday OR crypto_24_7 disabled: use classic BTC logic.
        if weekend and getattr(settings, "btc_weekend_analysis_only", True):
            return "BLOCK", "BTC_WEEKEND_ANALYSIS_ONLY"
        if bad_hour and settings.bad_hour_analysis_only and asia_window != "ASIA_MAIN":
            return "BLOCK", "BTC_BAD_HOUR_BLOCK"
        return "PASS", "ASIA_MAIN_ALLOWED" if asia_window == "ASIA_MAIN" else "TIME_GATE_PASS"
    if weekend:
        return "BLOCK", "WEEKEND_MARKET_CLOSED"
    if not market_open:
        return "BLOCK", "MARKET_CLOSED"
    if bad_hour and asia_window != "ASIA_MAIN":
        return "BLOCK", "BAD_LIQUIDITY_HOUR"
    if _rollover_minutes(local_dt) < max(0, int(getattr(settings, "rollover_block_minutes", 30))):
        return "BLOCK", "ROLLOVER_BLOCK"
    return "PASS", "ASIA_MAIN_ALLOWED" if asia_window == "ASIA_MAIN" else "TIME_GATE_PASS"


def _session_by_utc(utc_dt: datetime) -> str:
    if utc_dt.weekday() >= 5:
        return "WEEKEND"
    minute = utc_dt.hour * 60 + utc_dt.minute
    if _in_window(minute, "00:00", "07:00"):
        return "ASIA"
    if _in_window(minute, "07:00", "13:00"):
        return "LONDON"
    if _in_window(minute, "13:00", "17:00"):
        return "OVERLAP"
    if _in_window(minute, "17:00", "21:00"):
        return "NEW_YORK"
    return "OFF_HOURS"


def _asia_window_by_local(local_dt: datetime) -> str:
    minute = local_dt.hour * 60 + local_dt.minute
    if _in_window(minute, "22:00", "01:00"):
        return "ASIA_PREOPEN"
    if _in_window(minute, "01:00", "06:00"):
        return "ASIA_MAIN"
    if _in_window(minute, "06:00", "08:00"):
        return "ASIA_LATE"
    return "NONE"


def _symbol_market_open(symbol: str, local_dt: datetime, broker_dt: datetime, tick: dict | object | None) -> bool:
    normalized = _symbol(symbol)
    if normalized.startswith("BTCUSD"):
        return _tick_available(tick) or True
    if local_dt.weekday() >= 5:
        return False
    return True


def _is_bad_hour(symbol: str, local_hour: int, settings: Settings) -> bool:
    normalized = _symbol(symbol)
    if normalized.startswith("BTCUSD"):
        return local_hour in _int_set(getattr(settings, "btc_bad_hours", None) or settings.btc_bad_hours_local)
    if normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return local_hour in _int_set(getattr(settings, "gold_bad_hours", "0,1,22,23"))
    return local_hour in _int_set(getattr(settings, "fx_bad_hours", "0,1,22,23"))


_STALE_TICK_HOURS = 6.0  # mission/FIX_KILLSWITCH_DATE.md (2026-07-08)


def _broker_time_estimate(frames: dict, tick: dict | object | None, utc_dt: datetime) -> datetime:
    """Estimates the broker's live clock from a tick/candle timestamp when
    available (more precise than a naive UTC+offset guess). Hardened
    2026-07-08: an MT5 reconnect can hand back a cached tick/candle object
    from well before the disconnect — trusting its timestamp blindly would
    silently mis-classify session/asia-window/bad-hour for as long as the
    stale data lingers. Any candidate more than _STALE_TICK_HOURS away from
    the wall clock (utc_dt) is rejected and the next, more conservative
    source is tried — utc_dt itself is always fresh (see
    app.utils.broker_time) and is therefore never rejected."""
    tick_time = _extract_time(tick)
    if tick_time is not None and not _is_stale(tick_time, utc_dt):
        return tick_time
    for timeframe in ("M1", "M5", "M15", "H1", "H4"):
        frame = frames.get(timeframe) if isinstance(frames, dict) else None
        frame_time = _last_frame_time(frame)
        if frame_time is not None and not _is_stale(frame_time, utc_dt):
            return frame_time
    return utc_dt


def _is_stale(candidate: datetime, utc_dt: datetime) -> bool:
    return abs((utc_dt - candidate).total_seconds()) / 3600.0 > _STALE_TICK_HOURS


def _last_frame_time(frame: object) -> datetime | None:
    if frame is None or getattr(frame, "empty", True) or "time" not in getattr(frame, "columns", []):
        return None
    try:
        return _as_utc(frame.iloc[-1].get("time"))
    except Exception:
        return None


def _extract_time(tick: dict | object | None) -> datetime | None:
    if tick is None:
        return None
    value = None
    if isinstance(tick, dict):
        value = tick.get("time_msc") or tick.get("time")
    else:
        value = getattr(tick, "time_msc", None) or getattr(tick, "time", None)
    return _as_utc(value)


def _as_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            raw = float(value)
            if raw > 10_000_000_000:
                raw /= 1000.0
            dt = datetime.fromtimestamp(raw, tz=timezone.utc)
        else:
            ts = pd.Timestamp(value)
            dt = ts.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _rollover_minutes(local_dt: datetime) -> int:
    rollover = local_dt.replace(hour=22, minute=0, second=0, microsecond=0)
    return int(abs((local_dt - rollover).total_seconds()) // 60)


def _in_window(minute: int, start: str, end: str) -> bool:
    start_min = _minute(start)
    end_min = _minute(end)
    if start_min <= end_min:
        return start_min <= minute < end_min
    return minute >= start_min or minute < end_min


def _minute(value: str) -> int:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return hour * 60 + minute


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Africa/Casablanca")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Casablanca")


def _int_set(value: str) -> set[int]:
    out = set()
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.add(int(item))
        except ValueError:
            continue
    return out


def _demo_ignore_time_blocks(settings: Settings) -> bool:
    if not getattr(settings, "allow_time_block_override", False):
        return False
    return bool(
        getattr(settings, "demo_ignore_all_time_blocks", False)
        or getattr(settings, "demo_ignore_session_blocks", False)
        or getattr(settings, "demo_ignore_bad_hour_blocks", False)
    )


def _tick_available(tick: dict | object | None) -> bool:
    return tick is not None


def _tick_tradable(tick: dict | object | None) -> bool:
    """Return True when broker is serving a positive bid price for the symbol."""
    if tick is None:
        return False
    bid = tick.get("bid") if isinstance(tick, dict) else getattr(tick, "bid", None)
    if bid is None:
        return False
    try:
        return float(bid) > 0
    except (TypeError, ValueError):
        return False


def _tick_broker_closed(tick: dict | object | None) -> bool:
    """Return True when broker returns a tick but bid==0 (session closed, CFD disabled)."""
    if tick is None:
        return False
    bid = tick.get("bid") if isinstance(tick, dict) else getattr(tick, "bid", None)
    if bid is None:
        return False
    try:
        return float(bid) == 0.0
    except (TypeError, ValueError):
        return False


def _is_crypto_symbol(normalized: str) -> bool:
    """Return True for BTC/ETH crypto symbols. normalized is upper-case, # removed."""
    return normalized.startswith(("BTCUSD", "ETHUSD"))


def _symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("#", "")
