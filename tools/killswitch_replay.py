# -*- coding: utf-8 -*-
"""mission/MISSION_KILLSWITCH_REPLAY.md -- READ-ONLY backtest of signals
blocked by the daily kill-switch, to measure (not guess) whether the hours
locked out after the Asia-session loss streak would have been profitable.

Isolated tool, outside every decision path. Never calls order_send, never
imports/modifies the trading path, never writes to decision_dataset.jsonl
(read-only). Reads: app/data/decision_dataset.jsonl, MT5 M1 candle history
(read-only copy_rates_range). Writes two files only: reports/KILLSWITCH_REPLAY.md
and reports/KILLSWITCH_REPLAY.csv.

Methodology (see module functions for exact citations to app/mt5/demo_router.py
and app/services/daily_killswitch.py):
  1. Find the FIRST decision row today with failed_gate=DAILY_KILLSWITCH_MAX_LOSSES
     (daily_killswitch.py: triggers when losses_today >= max_losses, i.e. right
     after the 6th loss closes).
  2. Population = every DAILY_KILLSWITCH_MAX_LOSSES block AFTER that instant.
  3. Counterfactual filter: keep only rows where every OTHER gate the mission
     names would also have passed, using the SAME logged gate_statuses.* fields
     demo_router.py itself computed for that cycle (not re-derived from
     scratch): confluence_threshold_pass, rr>=1.5, spread_ok, sl_tp_valid,
     current_symbol_open_count < demo_max_open_trades_per_symbol (1),
     open_demo_trades_total < demo_max_open_trades_total (3),
     current_symbol_strategy_open_count < demo_max_open_trades_per_symbol_strategy (1),
     symbol_trade_cooldown_active == False, market_open, time_gate PASS, and
     (ORDER_FLOW_EXECUTION_AGENT specifically) order_flow_execution_agent_score>=75
     -- app/mt5/demo_router.py:1848-1860. News blackout re-checked live against
     the same app.services.protected_calendar.NewsCalendar HERMES itself uses.
  4. Dedup: the bot re-evaluates the same setup every ~20-60s cycle. Consecutive
     rows sharing (symbol, direction, strategy) collapse into ONE opportunity
     when the gap to the previous row in the streak is <= DEDUP_GAP_SECONDS;
     the opportunity's entry/SL/TP are the FIRST row's (earliest moment it
     could have been taken).
  5. Replay: walk M1 candles (mt5.copy_rates_range) from the opportunity's
     timestamp to the broker reset (21:00 UTC same day), find the first TP/SL
     touch. Spread modeled per the mission's own instruction (no raw spread
     value is logged on refused rows, so a documented conservative estimate is
     used -- see SPREAD_ESTIMATE_PRICE below): MT5 M1 OHLC is bid-based, so a
     BUY's SL/TP (checked against bid) needs no adjustment, while a SELL's
     SL/TP (checked against ask) is approximated as bid+spread.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.services.protected_calendar import NewsCalendar  # noqa: E402
from app.utils.broker_time import to_mt5_query_bounds  # noqa: E402

DATASET_PATH = REPO_ROOT / "app" / "data" / "decision_dataset.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"
BROKER_OFFSET_HOURS = 3.0
DEDUP_GAP_SECONDS = 600  # 10 minutes -- see module docstring point 4

# -- thresholds, cited from app/config.py defaults (verified, not guessed) --
MAX_OPEN_PER_SYMBOL = 1          # demo_max_open_trades_per_symbol
MAX_OPEN_TOTAL = 3               # demo_max_open_trades_total
MAX_OPEN_PER_SYMBOL_STRATEGY = 1 # demo_max_open_trades_per_symbol_strategy
ORDER_FLOW_MIN_SCORE = 75        # order_flow_min_score
ORDER_FLOW_MIN_RR = 1.5          # order_flow_min_rr
NEWS_BLACKOUT_WINDOW_MIN = 10    # news_blackout_window_minutes

# -- spread modeling (mission: "spread logue OU estimation prudente" -- no raw
# spread value is present on refused rows, only the spread_ok boolean, so a
# conservative fixed estimate is used, matching live SPREAD_DIAG readings
# observed this session: GOLD# ~27-30pts (~$0.27-0.30), BTCUSD# ~2250pts
# (~$22.50) at point=0.01. --
SPREAD_ESTIMATE_PRICE = {"GOLD#": 0.30, "BTCUSD#": 25.0}


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


def load_day_rows(day_start: datetime, day_end: datetime) -> list[dict]:
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
            if not (day_start <= ts <= day_end):
                continue
            d["_ts"] = ts
            rows.append(d)
    rows.sort(key=lambda d: d["_ts"])
    return rows


def find_first_trigger(rows: list[dict]) -> dict | None:
    for r in rows:
        if (r.get("failed_gate") or "") == "DAILY_KILLSWITCH_MAX_LOSSES":
            return r
    return None


def counterfactual_ok(row: dict, news_cal: NewsCalendar) -> tuple[bool, list[str]]:
    """Returns (ok, reasons_would_have_blocked). Empty reasons list == would
    have passed everything else; ONLY the kill-switch stopped it."""
    reasons = []
    g = {k[len("gate_statuses."):]: v for k, v in row.items() if k.startswith("gate_statuses.")}

    if g.get("market_open") is False:
        reasons.append("MARKET_CLOSED")
    if g.get("time_gate_status") not in (None, "PASS"):
        reasons.append(f"TIME_GATE={g.get('time_gate_status')}")
    if g.get("spread_ok") is False:
        reasons.append("MAX_SPREAD")
    if g.get("sl_tp_valid") is False:
        reasons.append("INVALID_SL_TP")
    if g.get("confluence_threshold_pass") is False:
        reasons.append("CONFLUENCE_TOO_LOW")
    rr = g.get("rr")
    if rr is not None and rr < ORDER_FLOW_MIN_RR:
        reasons.append(f"RR_BELOW_{ORDER_FLOW_MIN_RR}")
    if (g.get("current_symbol_open_count") or 0) >= MAX_OPEN_PER_SYMBOL:
        reasons.append("MAX_OPEN_TRADES_PER_SYMBOL")
    if (g.get("open_demo_trades_total") or 0) >= MAX_OPEN_TOTAL:
        reasons.append("MAX_OPEN_TRADES_TOTAL")
    if (g.get("current_symbol_strategy_open_count") or 0) >= MAX_OPEN_PER_SYMBOL_STRATEGY:
        reasons.append("MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY")
    if g.get("symbol_trade_cooldown_active") is True:
        reasons.append("SYMBOL_TRADE_COOLDOWN")

    strategy = str(row.get("strategy") or "").upper()
    if strategy == "ORDER_FLOW_EXECUTION_AGENT":
        score = g.get("order_flow_execution_agent_score")
        if score is not None and score < ORDER_FLOW_MIN_SCORE:
            reasons.append(f"ORDER_FLOW_SCORE_BELOW_{ORDER_FLOW_MIN_SCORE}")

    try:
        blackout = news_cal.news_blackout(row["_ts"], window_minutes=NEWS_BLACKOUT_WINDOW_MIN)
        if blackout:
            reasons.append(f"NEWS_BLACKOUT({blackout.get('title')})")
    except Exception:
        pass  # fail-safe, matches production news-shield behavior

    return (len(reasons) == 0, reasons)


def dedup_opportunities(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapses consecutive re-evaluations of the same setup into one
    opportunity (module docstring point 4). Returns (opportunities,
    n_collapsed_rows)."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("symbol"), r.get("direction"), r.get("strategy"))
        by_key[key].append(r)

    opportunities = []
    collapsed = 0
    for key, group in by_key.items():
        group.sort(key=lambda r: r["_ts"])
        current_streak = [group[0]]
        for r in group[1:]:
            gap = (r["_ts"] - current_streak[-1]["_ts"]).total_seconds()
            if gap <= DEDUP_GAP_SECONDS:
                current_streak.append(r)
                collapsed += 1
            else:
                opportunities.append(_streak_to_opportunity(current_streak))
                current_streak = [r]
        opportunities.append(_streak_to_opportunity(current_streak))
    opportunities.sort(key=lambda o: o["ts"])
    return opportunities, collapsed


def _streak_to_opportunity(streak: list[dict]) -> dict:
    first = streak[0]
    return {
        "ts": first["_ts"],
        "symbol": first.get("symbol"),
        "direction": str(first.get("direction") or "").upper(),
        "strategy": first.get("strategy"),
        "entry": first.get("entry"),
        "sl": first.get("sl"),
        "tp": first.get("tp"),
        "re_evaluations": len(streak),
        "last_ts": streak[-1]["_ts"],
    }


def fetch_m1_candles(symbol: str, start: datetime, end: datetime):
    import MetaTrader5 as mt5

    q_start, q_end = to_mt5_query_bounds(start, end, BROKER_OFFSET_HOURS)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, q_start, q_end)
    return list(rates) if rates is not None else []


def replay_opportunity(opp: dict, candles, day_end_utc: datetime) -> dict:
    """See module docstring point 5 for the exact spread-modeling rule."""
    direction = opp["direction"]
    entry = float(opp["entry"])
    sl = float(opp["sl"]) if opp.get("sl") else None
    tp = float(opp["tp"]) if opp.get("tp") else None
    spread = SPREAD_ESTIMATE_PRICE.get(opp["symbol"], 0.0)

    outcome = "STILL_OPEN"
    exit_price = None
    exit_ts = None
    last_close = entry

    for c in candles:
        c_time_broker_naive = datetime.utcfromtimestamp(c["time"])
        c_time_utc = c_time_broker_naive.replace(tzinfo=timezone.utc) - timedelta(hours=BROKER_OFFSET_HOURS)
        if c_time_utc < opp["ts"]:
            continue
        if c_time_utc > day_end_utc:
            break
        low = float(c["low"])
        high = float(c["high"])
        last_close = float(c["close"])

        if direction == "BUY":
            # BID-based candle, BUY's SL/TP checked against bid directly.
            hit_sl = sl is not None and low <= sl
            hit_tp = tp is not None and high >= tp
        else:
            # approximate ask = bid + spread for a SELL's SL/TP check.
            ask_low = low + spread
            ask_high = high + spread
            hit_sl = sl is not None and ask_high >= sl
            hit_tp = tp is not None and ask_low <= tp

        if hit_sl and hit_tp:
            # ambiguous same-candle double-touch -- conservative: SL first.
            outcome, exit_price, exit_ts = "SL_HIT", sl, c_time_utc
            break
        if hit_sl:
            outcome, exit_price, exit_ts = "SL_HIT", sl, c_time_utc
            break
        if hit_tp:
            outcome, exit_price, exit_ts = "TP_HIT", tp, c_time_utc
            break

    if outcome == "STILL_OPEN":
        exit_price = last_close
        exit_ts = day_end_utc

    sign = 1.0 if direction == "BUY" else -1.0
    price_move = sign * (exit_price - entry)
    risk_price = abs(entry - sl) if sl else None
    r_multiple = round(price_move / risk_price, 3) if risk_price else None

    return {
        "outcome": outcome, "exit_price": exit_price, "exit_ts": exit_ts,
        "price_move": price_move, "r_multiple": r_multiple,
    }


def usd_per_point_01(symbol: str) -> float | None:
    import MetaTrader5 as mt5
    info = mt5.symbol_info(symbol)
    if info is None or not getattr(info, "trade_tick_value", None) or not getattr(info, "trade_tick_size", None):
        return None
    return (info.trade_tick_value / info.trade_tick_size) * 0.01


def verified_daily_pnl_from_mt5(day_start: datetime, day_end: datetime) -> tuple[float, int, datetime | None]:
    """Independently recomputes the day's real closed P&L and true loss
    count DIRECTLY from MT5 deal history, using the CORRECT query-window
    conversion (to_mt5_query_bounds) -- bypassing decision_dataset's own
    logged daily_killswitch.* fields entirely, since those are known
    unreliable before the 3h-query-window fix went live mid-2026-07-08
    (mission/DATASET.md's own finding: pnl_cross_check_divergence field
    absent before ~18:10 UTC that day). Returns (real_pnl, loss_count,
    true_breach_ts or None if losses never reached 6)."""
    import MetaTrader5 as mt5

    q_start, q_end = to_mt5_query_bounds(day_start, day_end, BROKER_OFFSET_HOURS)
    deals = mt5.history_deals_get(q_start, q_end) or []
    closes = sorted((d for d in deals if d.magic == 909002 and d.entry == 1), key=lambda d: d.time)
    running = 0.0
    losses = 0
    breach_ts = None
    for d in closes:
        pnl = d.profit + d.swap + d.commission
        running += pnl
        if pnl < 0:
            losses += 1
            if losses == 6 and breach_ts is None:
                breach_ts = datetime.fromtimestamp(d.time, tz=timezone.utc) - timedelta(hours=BROKER_OFFSET_HOURS)
    return round(running, 2), losses, breach_ts


# Day configuration. 2026-07-07 has ZERO kill-switch triggers (verified,
# nothing to replay). 2026-07-08's OWN logged failed_gate=DAILY_KILLSWITCH_
# MAX_LOSSES first-occurrence (04:38:36 UTC) is UNRELIABLE: it comes from
# the pre-fix, 3h-shifted MT5 query window (mission/DATASET.md), and an
# independent recomputation directly from MT5 deals with the CORRECT window
# shows the TRUE 6th loss only closed at 17:50:20 UTC (the same trade as
# reports/AUTOPSIE_377299478.md) -- 13+ hours later, meaning the old
# kill-switch falsely blocked nearly the entire day before that point. Using
# the buggy logged trigger would build a population of signals that were
# NEVER legitimately over-quota. trigger_override forces the TRUE,
# independently-verified breach instant for that day; 2026-07-09 uses the
# normal logged-based detection (already verified reliable: divergence=0.0
# present from its very first trigger).
DAYS = [
    {
        "label": "2026-07-08",
        "start": datetime(2026, 7, 7, 21, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc),
        "trigger_override": datetime(2026, 7, 8, 17, 50, 20, tzinfo=timezone.utc),
        "trigger_note": ("declencheur RECALCULE independamment depuis MT5 (fenetre corrigee) -- le "
                          "declencheur LOGUE ce jour-la (04:38:36 UTC) est invalide, issu du bug de "
                          "fenetre de requete 3h corrige depuis, voir mission/DATASET.md"),
    },
    {
        "label": "2026-07-09",
        "start": datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 7, 9, 21, 0, 0, tzinfo=timezone.utc),
        "trigger_override": None,
        "trigger_note": "declencheur logue directement (fiable : pnl_cross_check_divergence=0.0 des la premiere occurrence)",
    },
]


def process_day(day_cfg: dict, news_cal: NewsCalendar) -> dict | None:
    day_start, day_end, label = day_cfg["start"], day_cfg["end"], day_cfg["label"]
    print(f"\n[killswitch_replay] === jour {label} (fenetre broker {day_start.isoformat()} -> {day_end.isoformat()}) ===")

    rows = load_day_rows(day_start, day_end)
    print(f"[killswitch_replay] {len(rows)} decisions loguees")

    if day_cfg["trigger_override"] is not None:
        trigger_ts = day_cfg["trigger_override"]
    else:
        first_trigger = find_first_trigger(rows)
        if first_trigger is None:
            print(f"[killswitch_replay] {label}: aucun declenchement DAILY_KILLSWITCH_MAX_LOSSES -- rien a rejouer.")
            return None
        trigger_ts = first_trigger["_ts"]
    print(f"[killswitch_replay] declenchement retenu: {trigger_ts.isoformat()} ({broker_session_name(trigger_ts)}) "
          f"-- {day_cfg['trigger_note']}")

    raw_population = [r for r in rows if (r.get("failed_gate") or "") == "DAILY_KILLSWITCH_MAX_LOSSES"
                       and r["_ts"] >= trigger_ts]
    print(f"[killswitch_replay] population brute apres declenchement: {len(raw_population)}")
    if not raw_population:
        print(f"[killswitch_replay] {label}: population vide apres le vrai declenchement -- rien a rejouer.")
        return None

    clean_rows, excluded_rows = [], []
    exclusion_reasons_count: dict[str, int] = defaultdict(int)
    for r in raw_population:
        ok, reasons = counterfactual_ok(r, news_cal)
        if ok:
            clean_rows.append(r)
        else:
            excluded_rows.append((r, reasons))
            for reason in reasons:
                exclusion_reasons_count[reason] += 1
    print(f"[killswitch_replay] apres filtre contrefactuel: {len(clean_rows)} retenus, {len(excluded_rows)} ecartes")

    opportunities, collapsed = dedup_opportunities(clean_rows)
    print(f"[killswitch_replay] apres dedup: {len(opportunities)} opportunites ({collapsed} re-evaluations fusionnees)")

    for opp in opportunities:
        candles = fetch_m1_candles(opp["symbol"], opp["ts"], day_end)
        result = replay_opportunity(opp, candles, day_end)
        opp.update(result)
        upp = usd_per_point_01(opp["symbol"])
        opp["usd_per_point_01"] = upp
        opp["pnl_usd"] = round(opp["price_move"] * upp, 2) if upp else None
        opp["session"] = broker_session_name(opp["ts"])
        print(f"  {opp['ts'].isoformat()} {opp['symbol']} {opp['direction']} -> {opp['outcome']} pnl={opp['pnl_usd']}")

    real_pnl, real_losses, verified_breach = verified_daily_pnl_from_mt5(day_start, day_end)
    print(f"[killswitch_replay] P&L reel du jour (recalcule independamment depuis MT5): {real_pnl} $ "
          f"({real_losses} pertes reelles)")

    return {
        "label": label, "day_start": day_start, "day_end": day_end, "trigger_ts": trigger_ts,
        "trigger_note": day_cfg["trigger_note"], "rows": rows, "raw_population": raw_population,
        "clean_rows": clean_rows, "excluded_rows": excluded_rows,
        "exclusion_reasons_count": exclusion_reasons_count, "opportunities": opportunities,
        "collapsed": collapsed, "real_pnl": real_pnl, "real_losses": real_losses,
    }


def main() -> None:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    try:
        news_cal = NewsCalendar()
        day_results = []
        for day_cfg in DAYS:
            result = process_day(day_cfg, news_cal)
            if result is not None:
                day_results.append(result)
        if not day_results:
            print("[killswitch_replay] aucun jour exploitable -- aucun rapport genere.")
            return
        write_reports(day_results)
    finally:
        mt5.shutdown()


def _fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _session_aggregate_lines(opportunities: list[dict]) -> list[str]:
    by_session: dict[str, list] = defaultdict(list)
    for o in opportunities:
        by_session[o["session"]].append(o)
    out = ["| session | N | win rate | P&L simule $ |", "|---|---|---|---|"]
    for session in ("ASIE", "LONDRES", "OVERLAP", "NEW_YORK"):
        opps = by_session.get(session, [])
        if not opps:
            continue
        s_wins = [o for o in opps if (o["pnl_usd"] or 0) > 0]
        s_pnl = sum(o["pnl_usd"] or 0 for o in opps)
        s_wr = round(100 * len(s_wins) / len(opps), 1) if opps else None
        out.append(f"| {session} | {len(opps)} | {_fmt(s_wr,1)}% | {_fmt(s_pnl)} |")
    return out


def write_reports(day_results: list[dict]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# KILLSWITCH_REPLAY — multi-jours (lecture seule, simulation)")
    lines.append("")
    lines.append(f"Jours analyses : **{', '.join(d['label'] for d in day_results)}**. "
                 "2026-07-07 est exclu : verifie, zero declenchement kill-switch ce jour-la, rien a rejouer.")
    lines.append("")

    all_opportunities: list[dict] = []
    for d in day_results:
        d["opp_with_day"] = [{**o, "day": d["label"]} for o in d["opportunities"]]
        all_opportunities.extend(d["opp_with_day"])

    # ---- per-day sections ----
    for d in day_results:
        opportunities = d["opportunities"]
        total_pnl = sum(o["pnl_usd"] for o in opportunities if o["pnl_usd"] is not None)
        wins = [o for o in opportunities if (o["pnl_usd"] or 0) > 0]
        losses = [o for o in opportunities if (o["pnl_usd"] or 0) < 0]
        win_rate = round(100 * len(wins) / len(opportunities), 1) if opportunities else None
        best = max(opportunities, key=lambda o: o["pnl_usd"] or float("-inf"), default=None)
        worst = min(opportunities, key=lambda o: o["pnl_usd"] or float("inf"), default=None)
        counterfactual_total = round(d["real_pnl"] + total_pnl, 2)

        lines.append(f"## Jour {d['label']}")
        lines.append("")
        lines.append(f"Fenetre : **{d['day_start'].isoformat()}** -> **{d['day_end'].isoformat()}** (jour broker UTC+3).")
        lines.append(f"Declenchement kill-switch retenu : **{d['trigger_ts'].isoformat()}** "
                     f"(session **{broker_session_name(d['trigger_ts'])}**) -- {d['trigger_note']}.")
        lines.append("")
        lines.append(f"- Blocages bruts apres declenchement : **{len(d['raw_population'])}**")
        lines.append(f"- Apres filtre contrefactuel : **{len(d['clean_rows'])}** retenus, "
                     f"**{len(d['excluded_rows'])}** ecartes")
        if d["exclusion_reasons_count"]:
            lines.append("")
            lines.append("| raison d'exclusion | count |")
            lines.append("|---|---|")
            for reason, count in sorted(d["exclusion_reasons_count"].items(), key=lambda kv: -kv[1]):
                lines.append(f"| {reason} | {count} |")
        lines.append("")
        lines.append(f"- Dedup : {d['collapsed']} re-evaluations fusionnees -> "
                     f"**{len(opportunities)} opportunites** rejouees.")
        lines.append("")

        if opportunities:
            lines.append("| heure UTC | session | symbole | sens | entree | SL | TP | 1er touche | R | $ simule |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for o in opportunities:
                lines.append(
                    f"| {o['ts'].strftime('%H:%M:%S')} | {o['session']} | {o['symbol']} | {o['direction']} | "
                    f"{_fmt(o['entry'])} | {_fmt(o['sl'])} | {_fmt(o['tp'])} | {o['outcome']} | "
                    f"{_fmt(o['r_multiple'])} | {_fmt(o['pnl_usd'])} |"
                )
            lines.append("")
            lines.extend(_session_aggregate_lines(opportunities))
            lines.append("")

        lines.append(f"- **Reel** (P&L recalcule independamment depuis MT5 pour ce jour broker, pas la valeur "
                     f"loguee par daily_killswitch qui peut etre celle du bug pre-fix) : **{_fmt(d['real_pnl'])} $** "
                     f"({d['real_losses']} pertes reelles)")
        lines.append(f"- **P&L simule des opportunites bloquees** : **{_fmt(total_pnl)} $** "
                     f"({len(wins)}G/{len(losses)}P, win rate {_fmt(win_rate,1)}%)")
        lines.append(f"- **Contrefactuel** (reel + simule) : **{_fmt(counterfactual_total)} $**")
        if best:
            lines.append(f"- Meilleure opportunite : {best['symbol']} {best['direction']} @ "
                         f"{best['ts'].strftime('%H:%M:%S')} UTC, {best['outcome']}, {_fmt(best['pnl_usd'])} $.")
        if worst:
            lines.append(f"- Pire opportunite : {worst['symbol']} {worst['direction']} @ "
                         f"{worst['ts'].strftime('%H:%M:%S')} UTC, {worst['outcome']}, {_fmt(worst['pnl_usd'])} $.")
        lines.append("")

    # ---- combined aggregate across all days ----
    total_pnl_all = sum(o["pnl_usd"] for o in all_opportunities if o["pnl_usd"] is not None)
    wins_all = [o for o in all_opportunities if (o["pnl_usd"] or 0) > 0]
    losses_all = [o for o in all_opportunities if (o["pnl_usd"] or 0) < 0]
    win_rate_all = round(100 * len(wins_all) / len(all_opportunities), 1) if all_opportunities else None
    best_all = max(all_opportunities, key=lambda o: o["pnl_usd"] or float("-inf"), default=None)
    worst_all = min(all_opportunities, key=lambda o: o["pnl_usd"] or float("inf"), default=None)
    real_pnl_all = sum(d["real_pnl"] for d in day_results)
    counterfactual_all = round(real_pnl_all + total_pnl_all, 2)

    lines.append("## Combine — tous les jours exploitables")
    lines.append("")
    lines.append(f"- **{len(all_opportunities)} opportunites** rejouees au total sur {len(day_results)} jour(s) "
                 f"({', '.join(d['label'] for d in day_results)}).")
    lines.append(f"- **P&L simule combine** : **{_fmt(total_pnl_all)} $** "
                 f"({len(wins_all)}G/{len(losses_all)}P, win rate combine {_fmt(win_rate_all,1)}%)")
    lines.append(f"- **Reel combine** (somme des P&L reels recalcules par jour) : **{_fmt(real_pnl_all)} $**")
    lines.append(f"- **Contrefactuel combine** : **{_fmt(counterfactual_all)} $**")
    lines.append("")
    lines.extend(_session_aggregate_lines(all_opportunities))
    lines.append("")
    if best_all:
        lines.append(f"**Meilleure opportunite (tous jours)** : {best_all['day']} {best_all['symbol']} "
                     f"{best_all['direction']} @ {best_all['ts'].strftime('%H:%M:%S')} UTC, {best_all['outcome']}, "
                     f"{_fmt(best_all['pnl_usd'])} $.")
    if worst_all:
        lines.append(f"**Pire opportunite (tous jours)** : {worst_all['day']} {worst_all['symbol']} "
                     f"{worst_all['direction']} @ {worst_all['ts'].strftime('%H:%M:%S')} UTC, {worst_all['outcome']}, "
                     f"{_fmt(worst_all['pnl_usd'])} $.")
    lines.append("")

    lines.append("## Caveats (obligatoires)")
    lines.append("")
    lines.append("- **C'est une SIMULATION sur bougies historiques, pas la realite.** Ces trades n'ont jamais ete ouverts.")
    lines.append("- Utilise le SL/TP **du signal au moment du blocage** -- n'imite PAS le trailing dynamique d'Exit V2 "
                 "(le vrai resultat aurait pu differer, dans un sens comme dans l'autre, si le trade avait vraiment "
                 "ete pris et gere par Exit V2).")
    lines.append(f"- Suppose un remplissage au prix du signal, avec un spread **estime** (aucune valeur de spread brute "
                 f"n'est loguee sur les lignes refusees) : GOLD# ~{_fmt(SPREAD_ESTIMATE_PRICE['GOLD#'])}$, "
                 f"BTCUSD# ~{_fmt(SPREAD_ESTIMATE_PRICE['BTCUSD#'])}$ -- et que 0.01 lot ne bouge pas le marche.")
    lines.append("- **2026-07-08 utilise un declenchement RECALCULE independamment** (17:50:20 UTC, pas la valeur "
                 "loguee 04:38:36 UTC issue du bug de fenetre MT5 corrige depuis) -- voir la section de ce jour "
                 "pour la justification complete. 2026-07-09 utilise le declenchement logue directement, deja "
                 "verifie fiable.")
    lines.append("- **Deux jours de donnees restent peu.** C'est un indice mesure, pas une loi -- surtout que l'un "
                 "des deux jours (07-08) a un declenchement tres tardif (17:50 UTC), laissant peu d'heures a "
                 "rejouer ce jour-la.")
    now_utc = datetime.now(timezone.utc)
    ongoing_days = [d for d in day_results if d["day_end"] > now_utc]
    closed_days = [d for d in day_results if d["day_end"] <= now_utc]
    n_still_open_ongoing = sum(
        1 for d in ongoing_days for o in d["opportunities"] if o["outcome"] == "STILL_OPEN")
    n_still_open_closed = sum(
        1 for d in closed_days for o in d["opportunities"] if o["outcome"] == "STILL_OPEN")
    if n_still_open_closed:
        lines.append(f"- **{n_still_open_closed} opportunite(s) sur un jour deja termine** "
                     "(broker day clos avant maintenant) n'ont touche ni TP ni SL avant la cloture de leur "
                     "jour respectif -- valorisees a la derniere bougie M1 disponible CE jour-la (une valeur "
                     "historique fixe, verifiee stable sur deux executions successives de ce script, pas une "
                     "estimation en mouvement).")
    if n_still_open_ongoing:
        lines.append(f"- **{n_still_open_ongoing} opportunite(s) sur un jour encore en cours** "
                     "(2026-07-09, pas encore a 21:00 UTC) sont valorisees au prix M1 le plus recent "
                     "disponible, PAS une cloture definitive -- confirme empiriquement : leur contribution "
                     "bouge d'une execution du script a l'autre tant que ce jour n'est pas clos. Re-executer "
                     "ce script apres 21:00 UTC le jour meme donnerait un chiffre stable pour 07-09 aussi.")
    lines.append("")

    # Verdict sur la population COMBINEE -- plus de donnees que le rapport
    # a un jour, seuils identiques (voir mission_killswitch_replay premiere
    # execution) : "nettement" exige un P&L notable (>= 2x le pire trade
    # individuel) ET un win rate clairement au-dessus/en-dessous de 50%.
    worst_trade_abs = abs(worst_all["pnl_usd"]) if worst_all and worst_all["pnl_usd"] else 1.0
    clearly_green = (total_pnl_all > 0 and total_pnl_all >= 2 * worst_trade_abs
                     and win_rate_all is not None and win_rate_all >= 55)
    clearly_red = (total_pnl_all < 0 and abs(total_pnl_all) >= 2 * worst_trade_abs
                   and win_rate_all is not None and win_rate_all <= 45)

    if clearly_green:
        verdict = (f"Sur les {len(day_results)} jours combines, les heures verrouillees apres le kill-switch "
                   f"auraient ete **nettement vertes** ({_fmt(total_pnl_all)} $ simules, {_fmt(win_rate_all,1)}% "
                   f"win rate sur {len(all_opportunities)} opportunites) : l'intuition de SIMO est **mesurement "
                   "validee** -- un kill-switch par-session pour la phase post-gel merite d'etre concu, chiffres "
                   "a l'appui (mais deux jours restent peu, voir caveats).")
    elif clearly_red:
        verdict = (f"Sur les {len(day_results)} jours combines, les heures verrouillees apres le kill-switch "
                   f"auraient **clairement continue a saigner** ({_fmt(total_pnl_all)} $ simules, "
                   f"{_fmt(win_rate_all,1)}% win rate sur {len(all_opportunities)} opportunites) : le stop "
                   "journalier est **vindique** -- sujet clos, sous reserve du nombre de jours disponibles "
                   "(voir caveats).")
    else:
        verdict = (f"Meme combine sur {len(day_results)} jours, resultat **marginal, ni nettement vert ni "
                   f"nettement rouge** ({_fmt(total_pnl_all)} $ simules sur {len(all_opportunities)} opportunites, "
                   f"win rate {_fmt(win_rate_all,1)}%). **Toujours aucun verdict tranche** : ni l'intuition de "
                   "SIMO ni le statu quo ne sont mesurablement valides ou invalides par ces donnees. Il faudra "
                   "accumuler d'autres jours de declenchement kill-switch avant de trancher.")
    lines.append("## Verdict")
    lines.append("")
    lines.append(verdict)

    md_path = REPORTS_DIR / "KILLSWITCH_REPLAY.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    csv_path = REPORTS_DIR / "KILLSWITCH_REPLAY.csv"
    import csv
    fieldnames = ["day", "ts", "session", "symbol", "direction", "strategy", "entry", "sl", "tp",
                  "re_evaluations", "outcome", "exit_price", "exit_ts", "r_multiple", "pnl_usd"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for o in all_opportunities:
            row = {k: o.get(k) for k in fieldnames}
            writer.writerow(row)

    print("\n" + "=" * 70)
    print("VERDICT COMBINE")
    print("=" * 70)
    print(verdict)
    print("=" * 70)
    print(f"\nLivrables ecrits : {md_path} , {csv_path}")


if __name__ == "__main__":
    main()
