"""PROTECTED CALENDAR — weekend flat + post-weekend blackout + news shield.

All rules are expressed in EXPLICIT UTC (three historical timezone bugs in
this project). Broker time is derived, never assumed.

a) Weekend flat:
   - no NEW entry from Friday 18:00 UTC,
   - EVERYTHING closed at Friday 20:30 UTC ([WEEKEND_FLAT]).
b) Post-weekend blackout: no entry Sunday -> Monday 03:00 UTC
   ([POST_WEEKEND_BLACKOUT]).
c) News shield ([NEWS_BLACKOUT]):
   - weekly ForexFactory feed, local cache, refreshed at boot + daily,
   - FAIL-SAFE: dead feed -> warning, trading continues,
   - HIGH impact USD: no entry inside [event-10min, event+10min],
   - NON-armed positions closed 10 min before configured majors
     (NFP/FOMC/CPI); armed positions keep their lock.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.logger import log

DEFAULT_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "news_calendar_cache.json"

_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6
_MONDAY = 0


# ── Weekend rules (pure UTC functions) ──────────────────────────────────────

def weekend_entry_block(now_utc: datetime) -> str | None:
    """Entry-block reason inside the protected weekend window, else None."""
    now_utc = _as_utc(now_utc)
    weekday = now_utc.weekday()
    if weekday == _FRIDAY and (now_utc.hour, now_utc.minute) >= (18, 0):
        return "WEEKEND_FLAT_NO_ENTRY"
    if weekday == _SATURDAY:
        return "WEEKEND_FLAT_NO_ENTRY"
    if weekday == _SUNDAY:
        return "POST_WEEKEND_BLACKOUT"
    if weekday == _MONDAY and now_utc.hour < 3:
        return "POST_WEEKEND_BLACKOUT"
    return None


def weekend_flat_close_due(now_utc: datetime) -> bool:
    """True when EVERYTHING must be flat (Friday from 20:30 UTC onwards)."""
    now_utc = _as_utc(now_utc)
    if now_utc.weekday() == _FRIDAY and (now_utc.hour, now_utc.minute) >= (20, 30):
        return True
    return now_utc.weekday() in {_SATURDAY, _SUNDAY}


def to_broker_time(dt_utc: datetime, broker_utc_offset_hours: float) -> datetime:
    """Calendar (UTC) -> broker clock. Explicit, tested, no guessing."""
    return _as_utc(dt_utc) + timedelta(hours=float(broker_utc_offset_hours))


# ── News shield ─────────────────────────────────────────────────────────────

class NewsCalendar:
    def __init__(
        self,
        settings: object = None,
        cache_path: Path | None = None,
        fetch_fn=None,
        feed_url: str | None = None,
    ) -> None:
        self.settings = settings
        self.cache_path = Path(cache_path or getattr(settings, "news_cache_path", "") or DEFAULT_CACHE_PATH)
        self.feed_url = feed_url or str(getattr(settings, "news_feed_url", "") or DEFAULT_FEED_URL)
        self._fetch_fn = fetch_fn
        self._events: list[dict] | None = None
        self._fetched_at: datetime | None = None
        self._attempted_at: datetime | None = None

    # -- refresh & cache ----------------------------------------------------
    def refresh(self, now_utc: datetime | None = None, force: bool = False) -> bool:
        """Refresh from the feed (boot + daily). FAIL-SAFE: on error, keep
        the cache, warn, and let trading continue."""
        now_utc = _as_utc(now_utc or datetime.now(timezone.utc))
        if self._events is None:
            self._load_cache()
        if not force and self._fetched_at is not None and (now_utc - self._fetched_at) < timedelta(hours=24):
            return False
        if not force and self._attempted_at is not None and (now_utc - self._attempted_at) < timedelta(hours=1):
            return False  # failed recently — retry hourly, never hammer the feed
        self._attempted_at = now_utc
        try:
            raw = self._fetch()
            events = _parse_feed(raw)
        except Exception as exc:
            log.warning(
                "[NEWS_CALENDAR] refresh_failed error=%s -> FAIL_SAFE (cache kept, trading continues)",
                str(exc)[:200],
            )
            return False
        self._events = events
        self._fetched_at = now_utc
        self._save_cache(now_utc)
        highs = self.high_usd_events()
        log.info(
            "[NEWS_CALENDAR] loaded events=%s high_usd=%s next_high_usd=%s",
            len(events), len(highs),
            min((e["time_utc"].isoformat() for e in highs if e["time_utc"] >= now_utc), default=None),
        )
        return True

    def _fetch(self):
        if self._fetch_fn is not None:
            return self._fetch_fn(self.feed_url)
        import requests
        response = requests.get(self.feed_url, timeout=10)
        response.raise_for_status()
        return response.json()

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._events = [
                {**event, "time_utc": datetime.fromisoformat(event["time_utc"])}
                for event in payload.get("events", [])
            ]
            fetched = payload.get("fetched_at")
            self._fetched_at = datetime.fromisoformat(fetched) if fetched else None
        except Exception:
            self._events = []
            self._fetched_at = None

    def _save_cache(self, now_utc: datetime) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": now_utc.isoformat(),
                "events": [
                    {**event, "time_utc": event["time_utc"].isoformat()}
                    for event in (self._events or [])
                ],
            }
            self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            log.warning("[NEWS_CALENDAR] cache_write_failed error=%s", str(exc)[:200])

    # -- queries --------------------------------------------------------------
    def events(self) -> list[dict]:
        if self._events is None:
            self._load_cache()
        return list(self._events or [])

    def high_usd_events(self) -> list[dict]:
        return [
            event for event in self.events()
            if str(event.get("impact") or "").upper() == "HIGH"
            and str(event.get("country") or "").upper() == "USD"
        ]

    def news_blackout(self, now_utc: datetime, window_minutes: int | None = None) -> dict | None:
        """HIGH USD event inside [event-10, event+10] -> the event, else None."""
        now_utc = _as_utc(now_utc)
        window = timedelta(minutes=int(
            window_minutes
            if window_minutes is not None
            else getattr(self.settings, "news_blackout_window_minutes", 10)
        ))
        for event in self.high_usd_events():
            if abs(now_utc - event["time_utc"]) <= window:
                return event
        return None

    def major_preclose_event(self, now_utc: datetime, window_minutes: int | None = None) -> dict | None:
        """Configured major (NFP/FOMC/CPI) starting within the next N minutes."""
        now_utc = _as_utc(now_utc)
        window = timedelta(minutes=int(
            window_minutes
            if window_minutes is not None
            else getattr(self.settings, "news_preclose_window_minutes", 10)
        ))
        raw_titles = str(getattr(self.settings, "news_major_titles", "") or "NON-FARM,NFP,FOMC,CPI")
        majors = [t.strip().upper() for t in raw_titles.split(",") if t.strip()]
        for event in self.high_usd_events():
            title = str(event.get("title") or "").upper()
            if not any(major in title for major in majors):
                continue
            delta = event["time_utc"] - now_utc
            if timedelta(0) <= delta <= window:
                return event
        return None


def _parse_feed(raw: object) -> list[dict]:
    events = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        stamp = item.get("date")
        try:
            parsed = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue
        events.append(
            {
                "title": item.get("title"),
                "country": item.get("country"),
                "impact": item.get("impact"),
                "time_utc": _as_utc(parsed),
            }
        )
    return events


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
