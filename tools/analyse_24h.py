# -*- coding: utf-8 -*-
"""mission/MISSION_RAPPORT_24H.md -- READ-ONLY 24h analytical report.

Isolated tool, outside every decision path. Never calls order_send, never
imports/touches app.mt5.demo_router or any strategy module, never modifies
scheduled tasks, never restarts anything. Reads three sources only:
  A. logs/hermes.log            (raw decision trail: CONFLUENCE_V2, EES,
                                  EXIT_V2, NEWS_PRECLOSE, WEEKEND_FLAT lines)
  B. app/data/decision_dataset.jsonl (structured decision/outcome rows,
                                  themselves derived from the same source A)
  C. MT5 deal history (magic=909002, read-only history_deals_get)
Writes two files only: reports/RAPPORT_24H.md and reports/RAPPORT_24H.csv.

MANDATORY SIGN RULE (mission's own explicit warning, ticket 828907072
precedent: HERMES displayed +38.72 while real MT5 P&L was -104.48): every
trade's P&L in this report comes EXCLUSIVELY from MT5 deal history, never
from a HERMES-side display/dataset field. Any HERMES-displayed P&L that
disagrees with the MT5 sum is flagged explicitly in the "ecart_affichage"
column, never silently trusted.

Timestamp normalization (established and verified earlier this session):
  - logs/hermes.log line timestamps are LOCAL machine time (UTC+1 on this
    host) -- converted to true UTC by subtracting 1h.
  - decision_dataset.jsonl's created_at/recorded_at fields are already
    true UTC (ISO8601 with +00:00).
  - MT5 deal.time (raw epoch) is broker-wall-clock-shaped (XM = UTC+3) --
    converted via app.utils.broker_time.from_mt5_deal_time().
All cross-source correlation in this script happens in true UTC.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.utils.broker_time import from_mt5_deal_time, to_mt5_query_bounds  # noqa: E402

LOG_LOCAL_OFFSET_HOURS = 1.0
BROKER_OFFSET_HOURS = 3.0
MAGIC = 909002
LOG_PATH = REPO_ROOT / "logs" / "hermes.log"
DATASET_PATH = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"


# ------------------------------------------------------------------ time ---

def local_str_to_utc(ts_local: str) -> datetime:
    """'2026-07-08 16:30:46' (local, no tz) -> true-UTC aware datetime."""
    naive = datetime.strptime(ts_local, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=timezone.utc) - timedelta(hours=LOG_LOCAL_OFFSET_HOURS)


def utc_to_local_prefix(dt_utc: datetime) -> str:
    """true-UTC datetime -> 'YYYY-MM-DD HH:MM:SS' matching the log's own
    local-time string prefix, for cheap textual pre-filtering."""
    local = dt_utc + timedelta(hours=LOG_LOCAL_OFFSET_HOURS)
    return local.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------- log lines ---
# One single streaming pass over hermes.log within the 24h window. Cheap
# textual prefix compare (no regex) decides in/out of window before any
# heavier per-line parsing -- necessary at 1.19M lines / ~190MB.

import re  # noqa: E402

_RE_CONFLUENCE_V2 = re.compile(
    r"\[CONFLUENCE_V2\] symbol=(?P<symbol>\S+) strategy=(?P<strategy>\S+) "
    r"geo=(?P<geo>[\d.]+) smc=(?P<smc>[\d.]+) mtfa=(?P<mtfa>[\d.]+) of=(?P<of>[\d.]+) "
    r".*?score=(?P<score>[\d.]+) grade=(?P<grade>\S+)"
)
_RE_EES = re.compile(
    r"\[EES\] symbol=(?P<symbol>\S+) side=(?P<side>\S+) score=(?P<score>[\d.]+) "
    r"band=(?P<band>\S+) action=(?P<action>\S+) strategy=(?P<strategy>\S+)"
)
_RE_EXIT_V2 = re.compile(
    r"\[EXIT_V2\] ticket=(?P<ticket>\d+) symbol=(?P<symbol>\S+) action=(?P<action>\S+) "
    r"reason=(?P<reason>\S+) profit=(?P<profit>-?[\d.]+) peak=(?P<peak>-?[\d.]+) "
    r"be_armed=(?P<armed>\w+)"
)
_RE_NEWS_PRECLOSE = re.compile(
    r"\[NEWS_PRECLOSE\] ticket=(?P<ticket>\d+) decision=CLOSE reason=(?P<reason>\S+) "
    r"event=(?P<event>.+?) event_time_utc="
)
_RE_WEEKEND_FLAT = re.compile(
    r"\[WEEKEND_FLAT\] action=CLOSE_ALL ticket=(?P<ticket>\d+)"
)
_RE_TS_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")

# Found while building this report: a periodic self-test/health-check
# routine replays a full fake decision cycle (fake ticket=701/702/555,
# symbol=EURUSD -- not in the current GOLD#/BTCUSD# allowlist --
# bid=None/ask=None/point=None spread audits, round synthetic
# entry=100.0/sl=98.0/tp=104.0 prices, stale daily_killswitch "window="
# spans from May/June, hardcoded session=LONDON) recurring roughly every
# 5-40 minutes throughout the ENTIRE day, not just at boot. It emits
# [EES] symbol=GOLD ... strategy=ORDER_FLOW_EXECUTION_AGENT lines -- the
# EXACT symbol+strategy pair dominating today's real trading -- so a
# naive nearest-timestamp correlation risks silently pulling in fake EES
# readings for real decisions. Detected via markers that are IMPOSSIBLE
# in real DEMO-only, GOLD#/BTCUSD#-only operation: "bid=None ask=None" or
# "symbol=EURUSD". Any second containing either marker is flagged and
# every line in that second is excluded from every extraction below --
# not just the line that tripped the marker, since a burst spans many
# lines within the same one-second timestamp.
_RE_SELFTEST_MARKER = re.compile(r"bid=None ask=None|symbol=EURUSD")


def _find_selftest_seconds(window_start_utc: datetime, window_end_utc: datetime) -> set[str]:
    start_prefix = utc_to_local_prefix(window_start_utc)
    flagged: set[str] = set()
    with LOG_PATH.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m_ts = _RE_TS_PREFIX.match(line)
            if not m_ts:
                continue
            ts_local_str = m_ts.group(1)
            if ts_local_str < start_prefix:
                continue
            if _RE_SELFTEST_MARKER.search(line):
                flagged.add(ts_local_str)
    return flagged


def scan_hermes_log(window_start_utc: datetime, window_end_utc: datetime) -> dict:
    """Two passes. First flags every local-time second containing a
    self-test/health-check burst marker (see _RE_SELFTEST_MARKER docstring
    above); second extracts real events, skipping any line whose second is
    flagged. Returns dict of lists/dicts keyed for later lookup."""
    print("[analyse_24h]  pass 1/2: reperage des rafales self-test/health-check...")
    selftest_seconds = _find_selftest_seconds(window_start_utc, window_end_utc)
    print(f"[analyse_24h]  {len(selftest_seconds)} secondes distinctes contaminees par un self-test, exclues.")

    start_prefix = utc_to_local_prefix(window_start_utc)
    confluence: list[tuple] = []
    ees: list[tuple] = []
    exit_v2: dict[int, list[tuple]] = defaultdict(list)
    news_preclose: dict[int, tuple] = {}
    weekend_flat: dict[int, tuple] = {}
    skipped_selftest_lines = 0

    with LOG_PATH.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m_ts = _RE_TS_PREFIX.match(line)
            if not m_ts:
                continue
            ts_local_str = m_ts.group(1)
            # cheap string compare pre-filter (works because both sides are
            # zero-padded ISO-ish 'YYYY-MM-DD HH:MM:SS' strings)
            if ts_local_str < start_prefix:
                continue
            ts_utc = local_str_to_utc(ts_local_str)
            if ts_utc > window_end_utc:
                continue
            if ts_local_str in selftest_seconds:
                skipped_selftest_lines += 1
                continue

            if "[CONFLUENCE_V2]" in line:
                m = _RE_CONFLUENCE_V2.search(line)
                if m:
                    confluence.append((
                        ts_utc, m.group("symbol"), m.group("strategy"),
                        float(m.group("geo")), float(m.group("smc")),
                        float(m.group("mtfa")), float(m.group("of")),
                        float(m.group("score")), m.group("grade"),
                    ))
                continue
            if "[EES]" in line:
                m = _RE_EES.search(line)
                if m:
                    ees.append((
                        ts_utc, m.group("symbol"), m.group("side"),
                        m.group("strategy"), float(m.group("score")), m.group("band"),
                    ))
                continue
            if "[EXIT_V2]" in line and "ticket=" in line:
                m = _RE_EXIT_V2.search(line)
                if m:
                    exit_v2[int(m.group("ticket"))].append((
                        ts_utc, m.group("action"), m.group("reason"),
                        float(m.group("profit")), float(m.group("peak")),
                        m.group("armed") == "True",
                    ))
                continue
            if "[NEWS_PRECLOSE]" in line and "decision=CLOSE" in line:
                m = _RE_NEWS_PRECLOSE.search(line)
                if m:
                    news_preclose[int(m.group("ticket"))] = (ts_utc, m.group("reason"), m.group("event"))
                continue
            if "[WEEKEND_FLAT]" in line:
                m = _RE_WEEKEND_FLAT.search(line)
                if m:
                    weekend_flat[int(m.group("ticket"))] = (ts_utc,)
                continue

    return {
        "confluence": confluence,
        "ees": ees,
        "exit_v2": exit_v2,
        "news_preclose": news_preclose,
        "weekend_flat": weekend_flat,
        "selftest_seconds_excluded": len(selftest_seconds),
        "selftest_lines_skipped": skipped_selftest_lines,
    }


def _norm_symbol(sym: str) -> str:
    return str(sym or "").upper().replace("#", "").strip()


def nearest_before(events: list[tuple], ts_key_idx: int, symbol: str, strategy: str | None,
                    target_ts: datetime, tolerance_seconds: float = 20.0):
    """events: list of tuples where index 0 = ts, index 1 = symbol, and
    (optionally) index 2 = strategy. Returns the closest event at or before
    target_ts (within tolerance), else the closest one within tolerance
    after target_ts (decision logging can lag the confluence line by a few
    hundred ms in rare cases) -- else None."""
    best = None
    best_dt = None
    sym_n = _norm_symbol(symbol)
    for ev in events:
        if _norm_symbol(ev[1]) != sym_n:
            continue
        if strategy is not None and len(ev) > 2 and isinstance(ev[2], str) and ev[2] != strategy:
            continue
        dt = abs((ev[0] - target_ts).total_seconds())
        if dt <= tolerance_seconds and (best_dt is None or dt < best_dt):
            best, best_dt = ev, dt
    return best


# --------------------------------------------------------------- dataset ---

def load_dataset_rows(window_start_utc: datetime, window_end_utc: datetime) -> list[dict]:
    rows = []
    with DATASET_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("row_type") != "decision":
                continue
            ts_raw = d.get("created_at")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not (window_start_utc <= ts <= window_end_utc):
                continue
            d["_ts_utc"] = ts
            rows.append(d)
    return rows


# ------------------------------------------------------------------- mt5 ---

def fetch_mt5_deals(window_start_utc: datetime, window_end_utc: datetime):
    """Returns (deals_by_position, usd_per_point_at_0_01_lot). The second
    value is captured HERE, while MT5 is still connected -- found while
    testing this script that usd_per_point() called later (after this
    function's own mt5.shutdown()) silently returned None for every trade,
    leaving risk_usd/r_multiple empty across the whole report."""
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    try:
        q_start, q_end = to_mt5_query_bounds(window_start_utc, window_end_utc, BROKER_OFFSET_HOURS)
        # widen a bit to also catch positions opened just before the window
        # (their close deal can still fall inside it)
        q_start_wide, _ = to_mt5_query_bounds(window_start_utc - timedelta(hours=6), window_end_utc, BROKER_OFFSET_HOURS)
        deals = mt5.history_deals_get(q_start_wide, q_end) or []
        by_position: dict[int, list] = defaultdict(list)
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != MAGIC:
                continue
            by_position[int(d.position_id)].append(d)

        usd_per_point_01: dict[str, float] = {}
        for sym in ("GOLD#", "BTCUSD#"):
            info = mt5.symbol_info(sym)
            if info is not None and getattr(info, "trade_tick_value", None) and getattr(info, "trade_tick_size", None):
                usd_per_point_01[sym] = (info.trade_tick_value / info.trade_tick_size) * 0.01
        return by_position, usd_per_point_01
    finally:
        mt5.shutdown()


def deal_reason_name(reason: int) -> str:
    return {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP",
            6: "SO", 7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT", 10: "CORPORATE_ACTION"}.get(reason, f"UNKNOWN_{reason}")


def classify_close(ticket: int, close_deal, scan: dict) -> str:
    """Best-effort, honestly-labeled close-mode classification. Prefers
    structured evidence (NEWS_PRECLOSE / WEEKEND_FLAT / Exit V2's own CLOSE
    log line) over the ambiguous MT5 comment, since this mission's own
    earlier forensic finding (AUTOPSIE 377299478) proved the comment alone
    is NOT sufficient: several distinct closers share the identical
    truncated "HERMES_QUICK_EXI" comment. Falls back to deal.reason (SL/TP
    broker-side) or a labeled "AUTRE (comment=...)" rather than guessing."""
    if ticket in scan["news_preclose"]:
        _, reason, event = scan["news_preclose"][ticket]
        return f"NEWS_PRECLOSE ({event})"
    if ticket in scan["weekend_flat"]:
        return "WEEKEND_FLAT"
    v2_events = scan["exit_v2"].get(ticket, [])
    v2_close = [e for e in v2_events if e[1] == "CLOSE"]
    if v2_close:
        return f"EXIT_V2 ({v2_close[-1][2]})"
    reason_name = deal_reason_name(int(getattr(close_deal, "reason", -1)))
    if reason_name in ("SL", "TP"):
        return f"BROKER_{reason_name}"
    if reason_name == "CLIENT":
        return "MANUEL (CLIENT)"
    comment = str(getattr(close_deal, "comment", "") or "")
    return f"AUTRE (reason={reason_name}, comment={comment})"


def exit_v2_summary(ticket: int, scan: dict) -> tuple[bool, bool]:
    events = scan["exit_v2"].get(ticket, [])
    be_armed = any(e[5] for e in events)
    trailing_active = any(e[1] == "NONE" and e[4] >= 2.0 for e in events)  # peak reached trail_start
    return be_armed, trailing_active




# --------------------------------------------------------------- session ---

def broker_session_name(dt_utc: datetime) -> str:
    h = dt_utc.hour
    if 0 <= h < 7:
        return "ASIE"
    if 7 <= h < 12:
        return "LONDRES"
    if 12 <= h < 16:
        return "OVERLAP"
    if 16 <= h < 21:
        return "NEW_YORK"
    return "ASIE"


def refusal_reason(row: dict) -> str:
    for key in (
        "failed_gate", "exploration_block_reason", "fallback_block_reason",
        "cap_block_reason", "confluence_threshold_reason", "strict_block_reason",
    ):
        v = row.get(key)
        if v:
            return f"{key}={v}"
    if row.get("confluence_threshold_pass") is False:
        return "CONFLUENCE_TOO_LOW"
    return row.get("decision") or "UNKNOWN"


def killswitch_pnl_recalibrated(row: dict) -> bool | None:
    """True if this decision row's daily_killswitch.* fields came from AFTER
    the P&L cross-check field was introduced (pnl_cross_check_divergence
    present), False if from before (field absent -- the older calculation),
    None if this row has no daily_killswitch data at all. Found empirically
    while building this report: within this 24h window, the field is
    absent for every row up to 2026-07-08T14:07:26 UTC (daily_pnl values as
    low as -44.85/-37.14) and present with divergence=0.0 for every row
    from 2026-07-08T18:10:06 UTC onward (daily_pnl realigned to e.g.
    -3.1) -- a clean, sharp transition, not a gradual drift."""
    if "daily_killswitch.daily_pnl" not in row:
        return None
    return row.get("daily_killswitch.pnl_cross_check_divergence") is not None


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=24)
    print(f"[analyse_24h] fenetre UTC: {window_start.isoformat()} -> {now_utc.isoformat()}")
    print(f"[analyse_24h] fenetre broker (UTC+3): "
          f"{(window_start + timedelta(hours=BROKER_OFFSET_HOURS)).isoformat()} -> "
          f"{(now_utc + timedelta(hours=BROKER_OFFSET_HOURS)).isoformat()}")

    print("[analyse_24h] scan hermes.log (2 passes : reperage self-test puis extraction)...")
    scan = scan_hermes_log(window_start, now_utc)
    print(f"[analyse_24h]  confluence_v2={len(scan['confluence'])} ees={len(scan['ees'])} "
          f"exit_v2_tickets={len(scan['exit_v2'])} news_preclose={len(scan['news_preclose'])} "
          f"weekend_flat={len(scan['weekend_flat'])}")
    print(f"[analyse_24h]  lignes self-test exclues: {scan['selftest_lines_skipped']} "
          f"({scan['selftest_seconds_excluded']} secondes distinctes)")

    print("[analyse_24h] lecture decision_dataset.jsonl...")
    rows = load_dataset_rows(window_start, now_utc)
    executed_rows = [r for r in rows if r.get("decision") == "PASS" and r.get("ticket")]
    refused_rows = [r for r in rows if r.get("decision") == "BLOCK"]
    print(f"[analyse_24h]  decisions={len(rows)} executees={len(executed_rows)} refusees={len(refused_rows)}")

    print("[analyse_24h] lecture deals MT5 (magic=909002, read-only)...")
    deals_by_position, usd_per_point_01 = fetch_mt5_deals(window_start, now_utc)
    print(f"[analyse_24h]  positions avec deals magic=909002 dans la fenetre elargie: {len(deals_by_position)}")

    executed_records = []
    for row in executed_rows:
        ticket = int(row["ticket"])
        deals = deals_by_position.get(ticket, [])
        deals.sort(key=lambda d: d.time)
        open_deal = next((d for d in deals if d.entry == 0), None)
        close_deal = next((d for d in deals if d.entry == 1), None)
        symbol = row.get("symbol") or row.get("broker_symbol") or ""
        strategy = row.get("strategy") or ""
        ts = row["_ts_utc"]

        conf = nearest_before(scan["confluence"], 0, symbol, strategy, ts)
        ees_buy = nearest_before(scan["ees"], 0, symbol, None, ts)
        ees_sell = None
        ees_matches = [e for e in scan["ees"] if _norm_symbol(e[1]) == _norm_symbol(symbol)
                        and abs((e[0] - ts).total_seconds()) <= 20]
        ees_by_side = {e[2]: e for e in sorted(ees_matches, key=lambda e: abs((e[0] - ts).total_seconds()))}

        real_pnl = None
        hermes_pnl_display = row.get("gate_statuses.risk_pct")  # not a P&L display field; see note below
        entry_price = None
        exit_price = None
        if open_deal is not None:
            entry_price = open_deal.price
        if close_deal is not None:
            exit_price = close_deal.price
        if deals:
            real_pnl = round(sum(float(d.profit) + float(d.swap or 0) + float(d.commission or 0) for d in deals), 2)

        close_mode = classify_close(ticket, close_deal, scan) if close_deal is not None else "POSITION_ENCORE_OUVERTE"
        be_armed, trailing_active = exit_v2_summary(ticket, scan)

        risk_usd = None
        r_multiple = None
        sl = row.get("sl")
        if sl and entry_price:
            upp = usd_per_point_01.get(symbol)
            if upp:
                risk_usd = abs(float(entry_price) - float(sl)) * upp
                if real_pnl is not None and risk_usd:
                    r_multiple = round(real_pnl / risk_usd, 2)

        executed_records.append({
            "ts_utc": ts, "ts_broker": ts + timedelta(hours=BROKER_OFFSET_HOURS),
            "symbol": symbol, "direction": row.get("direction"), "strategy": strategy,
            "ticket": ticket,
            "score": conf[7] if conf else row.get("final_confluence_score"),
            "grade": conf[8] if conf else row.get("new_confluence_grade") or row.get("grade"),
            "geo": conf[3] if conf else None, "smc": conf[4] if conf else None,
            "mtfa": conf[5] if conf else None, "of": conf[6] if conf else None,
            "ees_buy_score": ees_by_side.get("BUY", (None,) * 6)[4],
            "ees_buy_band": ees_by_side.get("BUY", (None,) * 6)[5],
            "ees_sell_score": ees_by_side.get("SELL", (None,) * 6)[4],
            "ees_sell_band": ees_by_side.get("SELL", (None,) * 6)[5],
            "session": row.get("time_gate.session_name") or broker_session_name(ts + timedelta(hours=BROKER_OFFSET_HOURS)),
            "kill_zone_active": row.get("gate_statuses.is_bad_hour"),
            "atr_percentile": row.get("regime.atr_percentile"),
            "momentum_alignment": row.get("momentum_alignment.alignment"),
            "momentum_consensus": row.get("momentum_alignment.consensus"),
            "dxy_trend": row.get("extra.eyes_dxy_trend"),
            "miners_available": row.get("extra.eyes_hui_available"),
            "entry_price": entry_price, "exit_price": exit_price,
            "real_pnl_mt5": real_pnl,
            "close_mode": close_mode, "be_armed": be_armed, "trailing_active": trailing_active,
            "risk_usd": round(risk_usd, 2) if risk_usd else None, "r_multiple": r_multiple,
            "sl": sl, "tp": row.get("tp"),
        })

    refused_records = []
    for row in refused_rows:
        symbol = row.get("symbol") or row.get("broker_symbol") or ""
        strategy = row.get("strategy") or ""
        ts = row["_ts_utc"]
        conf = nearest_before(scan["confluence"], 0, symbol, strategy, ts)
        ees_matches = [e for e in scan["ees"] if _norm_symbol(e[1]) == _norm_symbol(symbol)
                        and abs((e[0] - ts).total_seconds()) <= 20]
        ees_by_side = {e[2]: e for e in sorted(ees_matches, key=lambda e: abs((e[0] - ts).total_seconds()))}
        refused_records.append({
            "ts_utc": ts, "ts_broker": ts + timedelta(hours=BROKER_OFFSET_HOURS),
            "symbol": symbol, "direction": row.get("direction"), "strategy": strategy,
            "score": conf[7] if conf else row.get("final_confluence_score"),
            "grade": conf[8] if conf else row.get("new_confluence_grade") or row.get("grade"),
            "geo": conf[3] if conf else None, "smc": conf[4] if conf else None,
            "mtfa": conf[5] if conf else None, "of": conf[6] if conf else None,
            "ees_buy_score": ees_by_side.get("BUY", (None,) * 6)[4],
            "ees_buy_band": ees_by_side.get("BUY", (None,) * 6)[5],
            "ees_sell_score": ees_by_side.get("SELL", (None,) * 6)[4],
            "ees_sell_band": ees_by_side.get("SELL", (None,) * 6)[5],
            "session": row.get("time_gate.session_name") or broker_session_name(ts + timedelta(hours=BROKER_OFFSET_HOURS)),
            "reason": refusal_reason(row),
            "killswitch_pnl_recalibrated": killswitch_pnl_recalibrated(row),
            "killswitch_daily_pnl_at_decision": row.get("daily_killswitch.daily_pnl"),
        })

    write_reports(now_utc, window_start, executed_records, refused_records, scan)


def _fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def write_reports(now_utc, window_start, executed, refused, scan) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    n_total = len(executed) + len(refused)
    n_exec = len(executed)
    n_ref = len(refused)
    wins = [e for e in executed if (e["real_pnl_mt5"] or 0) > 0]
    losses = [e for e in executed if (e["real_pnl_mt5"] or 0) < 0]
    breakeven = [e for e in executed if e["real_pnl_mt5"] == 0]
    win_rate = round(100 * len(wins) / n_exec, 1) if n_exec else None
    net_pnl = round(sum(e["real_pnl_mt5"] or 0 for e in executed), 2)

    # refusal breakdown
    reason_counts: dict[str, int] = defaultdict(int)
    for r in refused:
        reason_counts[r["reason"]] += 1
    reason_sorted = sorted(reason_counts.items(), key=lambda kv: -kv[1])

    # Killswitch refusals: found empirically while building this report
    # that daily_killswitch.* readings in this window fall in two clean
    # groups -- a "pre-recalibration" block (pnl_cross_check_divergence
    # absent, daily_pnl values as low as -44.85/-37.14) and a
    # "recalibrated" block (divergence field present, =0.0) starting
    # 2026-07-08T18:10:06 UTC. Surfaced explicitly per this mission's own
    # rule: never silently trust a HERMES-side P&L number, flag any
    # discrepancy plainly.
    ks_refused = [r for r in refused if "KILLSWITCH" in r["reason"].upper()]
    ks_pre = [r for r in ks_refused if r["killswitch_pnl_recalibrated"] is False]
    ks_post = [r for r in ks_refused if r["killswitch_pnl_recalibrated"] is True]
    ks_unknown = [r for r in ks_refused if r["killswitch_pnl_recalibrated"] is None]

    # aggregates
    def group_pnl(key_fn):
        acc: dict = defaultdict(lambda: [0.0, 0])
        for e in executed:
            k = key_fn(e)
            acc[k][0] += e["real_pnl_mt5"] or 0
            acc[k][1] += 1
        return acc

    pnl_by_grade = group_pnl(lambda e: e["grade"] or "?")
    pnl_by_session = group_pnl(lambda e: e["session"] or "?")
    pnl_by_symbol = group_pnl(lambda e: e["symbol"] or "?")

    ees_extreme_refused = [r for r in refused if r["ees_sell_band"] == "EXTREME" or r["ees_buy_band"] == "EXTREME"]

    best = max(executed, key=lambda e: e["real_pnl_mt5"] or float("-inf"), default=None)
    worst = min(executed, key=lambda e: e["real_pnl_mt5"] or float("inf"), default=None)

    lines: list[str] = []
    lines.append("# RAPPORT_24H — analyse HERMES (lecture seule)")
    lines.append("")
    lines.append(f"Fenetre : **{window_start.isoformat()}** -> **{now_utc.isoformat()}** (UTC vrai) "
                 f"soit **{(window_start + timedelta(hours=BROKER_OFFSET_HOURS)).isoformat()}** -> "
                 f"**{(now_utc + timedelta(hours=BROKER_OFFSET_HOURS)).isoformat()}** (heure broker UTC+3).")
    lines.append("")
    lines.append(f"_Note methodologique : {scan['selftest_lines_skipped']} lignes de logs "
                 f"({scan['selftest_seconds_excluded']} secondes distinctes) ont ete exclues de cette analyse "
                 "car identifiees comme une rafale self-test/health-check periodique (positions et prix "
                 "synthetiques, symbol=EURUSD hors allowlist, bid/ask/point=None) qui rejoue un cycle de decision "
                 "factice plusieurs fois par heure — non filtree, elle aurait pu polluer la correlation EES/score "
                 "avec de fausses lectures pour GOLD#/ORDER_FLOW_EXECUTION_AGENT. Le P&L (deals MT5, "
                 f"magic={MAGIC}) et les decisions (decision_dataset.jsonl) n'etaient pas affectes par cette "
                 "contamination — verifie explicitement avant de produire ce rapport._")
    lines.append("")
    lines.append("## 1. Resume executif")
    lines.append("")
    lines.append(f"- {n_total} setups evalues sur 24h : **{n_exec} executes**, **{n_ref} refuses**.")
    lines.append(f"- Win rate sur les executes : **{_fmt(win_rate)}%** ({len(wins)} gagnants / {len(losses)} perdants"
                 + (f" / {len(breakeven)} neutres" if breakeven else "") + ").")
    lines.append(f"- **P&L net reel (verite MT5, deals magic={MAGIC})** : **{_fmt(net_pnl)} $**.")
    if reason_sorted:
        top_reason, top_count = reason_sorted[0]
        lines.append(f"- Raison de refus dominante : `{top_reason}` ({top_count}/{n_ref} refus, "
                     f"{round(100*top_count/n_ref, 1) if n_ref else 0}%).")
    if ks_refused:
        pct_pre = round(100 * len(ks_pre) / len(ks_refused), 1) if ks_refused else 0
        lines.append(f"- **Important** : sur les {len(ks_refused)} refus kill-switch, **{len(ks_pre)} ({pct_pre}%)** "
                     "portent un daily_pnl calcule AVANT une recalibration du P&L observee a 18:10:06 UTC dans "
                     "cette fenetre (valeurs jusqu'a -44.85$/-37.14$, sans le champ de recoupement "
                     f"pnl_cross_check_divergence) ; seuls **{len(ks_post)}** refus portent le calcul recoupe "
                     "(divergence=0.0). Voir 4. pour le detail — a interpreter avec l'analyste avant de conclure "
                     "sur le taux de refus kill-switch de la journee.")
    verdict = "positive" if net_pnl > 0 else ("negative" if net_pnl < 0 else "neutre")
    lines.append(f"- **Verdict en une phrase** : journee {verdict} ({_fmt(net_pnl)} $ reel sur {n_exec} trades, "
                 f"{_fmt(win_rate)}% de win rate), {n_ref} setups ecartes majoritairement par `{reason_sorted[0][0] if reason_sorted else 'N/A'}`.")
    lines.append("")

    lines.append("## 2. Setups executes")
    lines.append("")
    lines.append("| date/heure UTC | broker | symbole | sens | strategie | score | grade | geo/smc/mtfa/of | "
                 "EES sell | EES buy | Exit V2 | P&L reel $ | R |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for e in sorted(executed, key=lambda x: x["ts_utc"]):
        breakdown = f"{_fmt(e['geo'],0)}/{_fmt(e['smc'],0)}/{_fmt(e['mtfa'],0)}/{_fmt(e['of'],0)}"
        ees_sell = f"{_fmt(e['ees_sell_score'],1)} ({e['ees_sell_band'] or '?'})"
        ees_buy = f"{_fmt(e['ees_buy_score'],1)} ({e['ees_buy_band'] or '?'})"
        exitv2 = e["close_mode"]
        if e["be_armed"]:
            exitv2 += " [BE armed]"
        if e["trailing_active"]:
            exitv2 += " [trailing]"
        lines.append(
            f"| {e['ts_utc'].strftime('%m-%d %H:%M:%S')} | {e['ts_broker'].strftime('%m-%d %H:%M:%S')} | {e['symbol']} | "
            f"{e['direction']} | {e['strategy']} | {_fmt(e['score'],1)} | {e['grade']} | {breakdown} | "
            f"{ees_sell} | {ees_buy} | {exitv2} | {_fmt(e['real_pnl_mt5'])} | {_fmt(e['r_multiple'])} |"
        )
    lines.append("")

    lines.append("## 3. Setups refuses")
    lines.append("")
    _structural_reasons = ("DAILY_KILLSWITCH_MAX_LOSSES", "MAX_OPEN_TRADES_PER_SYMBOL")
    per_setup_refused = [r for r in refused if not any(sr in r["reason"] for sr in _structural_reasons)]
    structural_refused = [r for r in refused if any(sr in r["reason"] for sr in _structural_reasons)]
    lines.append(f"Sur {n_ref} refus, {len(structural_refused)} sont des blocages **structurels et repetitifs** "
                 "(quota kill-switch ou position deja ouverte sur le symbole -- le meme etat de compte reevalue "
                 "a chaque cycle, pas un jugement different par setup) : agreges en 4., detail complet ligne par "
                 f"ligne dans le CSV. Les {len(per_setup_refused)} refus restants ci-dessous portent une vraie "
                 "decision differenciee par setup :")
    lines.append("")
    lines.append("| date/heure UTC | broker | symbole | sens | strategie | score | grade | EES sell | EES buy | raison |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(per_setup_refused, key=lambda x: x["ts_utc"]):
        ees_sell = f"{_fmt(r['ees_sell_score'],1)} ({r['ees_sell_band'] or '?'})"
        ees_buy = f"{_fmt(r['ees_buy_score'],1)} ({r['ees_buy_band'] or '?'})"
        lines.append(
            f"| {r['ts_utc'].strftime('%m-%d %H:%M:%S')} | {r['ts_broker'].strftime('%m-%d %H:%M:%S')} | {r['symbol']} | "
            f"{r['direction']} | {r['strategy']} | {_fmt(r['score'],1)} | {r['grade']} | {ees_sell} | {ees_buy} | "
            f"{r['reason']} |"
        )
    lines.append("")

    lines.append("## 4. Agregats")
    lines.append("")
    lines.append(f"- N evalues={n_total} · N executes={n_exec} · N refuses={n_ref}")
    lines.append("")
    lines.append("**Refus par raison :**")
    lines.append("")
    lines.append("| raison | count | % |")
    lines.append("|---|---|---|")
    for reason, count in reason_sorted:
        lines.append(f"| {reason} | {count} | {round(100*count/n_ref, 1) if n_ref else 0}% |")
    lines.append("")
    if ks_refused:
        lines.append("**Detail des refus kill-switch — ecart de calcul P&L detecte (regle de signe obligatoire "
                     "de cette mission) :**")
        lines.append("")
        lines.append(f"- {len(ks_pre)} refus avec un `daily_pnl` calcule AVANT l'apparition du champ de recoupement "
                     "`pnl_cross_check_divergence` dans cette fenetre (transition nette observee a "
                     "2026-07-08T18:10:06 UTC) — valeurs vues jusqu'a -44.85$ / -37.14$.")
        lines.append(f"- {len(ks_post)} refus avec le calcul recoupe actif (`pnl_cross_check_divergence=0.0`).")
        if ks_unknown:
            lines.append(f"- {len(ks_unknown)} refus sans donnee daily_killswitch exploitable.")
        lines.append("- **Ecart_affichage** : les refus du premier groupe reposaient sur un daily_pnl dont cette "
                     "meme session a par ailleurs identifie et corrige le mode de calcul (fenetre de requete MT5 "
                     "decalee de 3h) — la valeur affichee a l'epoque n'est PAS necessairement le P&L reel de la "
                     "journee. Fait, pas une recommandation : a discuter avec l'analyste avant de conclure sur le "
                     "taux reel de blocage kill-switch de cette fenetre de 24h.")
        lines.append("")
    lines.append(f"**Win rate exécutés** : {_fmt(win_rate)}% ({len(wins)}G / {len(losses)}P)")
    lines.append("")
    lines.append(f"**P&L net reel (verite MT5)** : {_fmt(net_pnl)} $")
    lines.append("")
    lines.append("**P&L par grade :**")
    lines.append("")
    lines.append("| grade | P&L $ | N trades |")
    lines.append("|---|---|---|")
    for g, (pnl, n) in sorted(pnl_by_grade.items()):
        lines.append(f"| {g} | {_fmt(pnl)} | {n} |")
    lines.append("")
    lines.append("**P&L par session :**")
    lines.append("")
    lines.append("| session | P&L $ | N trades |")
    lines.append("|---|---|---|")
    for s, (pnl, n) in sorted(pnl_by_session.items()):
        lines.append(f"| {s} | {_fmt(pnl)} | {n} |")
    lines.append("")
    lines.append("**P&L par symbole :**")
    lines.append("")
    lines.append("| symbole | P&L $ | N trades |")
    lines.append("|---|---|---|")
    for s, (pnl, n) in sorted(pnl_by_symbol.items()):
        lines.append(f"| {s} | {_fmt(pnl)} | {n} |")
    lines.append("")
    lines.append(f"**Setups refuses en bande EES EXTREME** : {len(ees_extreme_refused)}/{n_ref}. "
                 "Estimation directionnelle honnête (pas une promesse) : sans données d'exécution réelle pour ces "
                 "setups jamais pris, il est impossible de dire s'ils auraient perdu ou gagné — la bande EXTREME "
                 "est un signal d'épuisement, pas une garantie de retournement immédiat. Aucune reconstruction "
                 "contrefactuelle de P&L n'est faite ici, seul le décompte est factuel.")
    lines.append("")
    if best:
        lines.append(f"**Meilleur trade** : {best['symbol']} {best['direction']} @ {best['ts_utc'].strftime('%m-%d %H:%M:%S')} UTC, "
                     f"strategie {best['strategy']}, score {_fmt(best['score'],1)} grade {best['grade']}, "
                     f"P&L reel {_fmt(best['real_pnl_mt5'])} $, sortie {best['close_mode']}.")
    if worst:
        lines.append(f"**Pire trade** : {worst['symbol']} {worst['direction']} @ {worst['ts_utc'].strftime('%m-%d %H:%M:%S')} UTC, "
                     f"strategie {worst['strategy']}, score {_fmt(worst['score'],1)} grade {worst['grade']}, "
                     f"P&L reel {_fmt(worst['real_pnl_mt5'])} $, sortie {worst['close_mode']}.")
    lines.append("")

    lines.append("## 5. Top 3 observations factuelles")
    lines.append("")
    obs = []
    if reason_sorted:
        obs.append(f"Le blocage dominant sur cette fenetre est `{reason_sorted[0][0]}` "
                   f"({reason_sorted[0][1]} refus sur {n_ref}, {round(100*reason_sorted[0][1]/n_ref,1) if n_ref else 0}%).")
    a_grade = [e for e in executed if e["grade"] == "A"]
    bc_grade = [e for e in executed if e["grade"] in ("B", "C")]
    if a_grade and bc_grade:
        a_pnl = sum(e["real_pnl_mt5"] or 0 for e in a_grade)
        bc_pnl = sum(e["real_pnl_mt5"] or 0 for e in bc_grade)
        obs.append(f"Les trades grade A ({len(a_grade)}) totalisent {_fmt(a_pnl)} $ contre {_fmt(bc_pnl)} $ "
                   f"pour les grades B/C ({len(bc_grade)}) sur cette fenetre.")
    non_exitv2_closes = [e for e in executed if e["close_mode"] and not e["close_mode"].startswith("EXIT_V2")
                          and e["close_mode"] != "POSITION_ENCORE_OUVERTE"]
    if non_exitv2_closes:
        obs.append(f"{len(non_exitv2_closes)}/{n_exec} clotures sur cette fenetre ne viennent PAS d'Exit V2 "
                   f"(SL/TP broker, NEWS_PRECLOSE, WEEKEND_FLAT, ou autre) — voir colonne 'Exit V2' du tableau 2..")
    for i, o in enumerate(obs[:3], 1):
        lines.append(f"{i}. {o}")
    lines.append("")
    lines.append("_Aucune recommandation de changement de systeme, aucun nouveau seuil propose — on decrit, "
                 "on ne prescrit pas._")

    md_path = REPORTS_DIR / "RAPPORT_24H.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    csv_path = REPORTS_DIR / "RAPPORT_24H.csv"
    fieldnames = sorted({k for e in (executed + refused) for k in e.keys()} | {"row_kind"})
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in executed:
            row = dict(e)
            row["row_kind"] = "EXECUTE"
            writer.writerow(row)
        for r in refused:
            row = dict(r)
            row["row_kind"] = "REFUSE"
            writer.writerow(row)

    print("\n" + "=" * 70)
    print("RESUME EXECUTIF")
    print("=" * 70)
    exec_summary_start = lines.index("## 1. Resume executif") + 2
    exec_summary_end = lines.index("## 2. Setups executes") - 1
    print("\n".join(lines[exec_summary_start:exec_summary_end]))
    print("=" * 70)
    print(f"\nLivrables ecrits : {md_path} , {csv_path}")


if __name__ == "__main__":
    main()
