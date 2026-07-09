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


def main() -> None:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    try:
        day_start = datetime(2026, 7, 8, 21, 0, 0, tzinfo=timezone.utc)
        day_end = datetime(2026, 7, 9, 21, 0, 0, tzinfo=timezone.utc)
        print(f"[killswitch_replay] jour analyse (fenetre broker): {day_start.isoformat()} -> {day_end.isoformat()}")

        rows = load_day_rows(day_start, day_end)
        print(f"[killswitch_replay] {len(rows)} decisions loguees ce jour")

        first_trigger = find_first_trigger(rows)
        if first_trigger is None:
            print("[killswitch_replay] aucun declenchement DAILY_KILLSWITCH_MAX_LOSSES ce jour -- rien a rejouer.")
            return
        trigger_ts = first_trigger["_ts"]
        print(f"[killswitch_replay] premier declenchement kill-switch: {trigger_ts.isoformat()} "
              f"({broker_session_name(trigger_ts)})")

        raw_population = [r for r in rows if (r.get("failed_gate") or "") == "DAILY_KILLSWITCH_MAX_LOSSES"
                           and r["_ts"] >= trigger_ts]
        print(f"[killswitch_replay] population brute (blocages kill-switch apres declenchement): {len(raw_population)}")

        news_cal = NewsCalendar()
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
        print(f"[killswitch_replay] apres filtre contrefactuel: {len(clean_rows)} retenus, "
              f"{len(excluded_rows)} ecartes (auraient echoue ailleurs)")

        opportunities, collapsed = dedup_opportunities(clean_rows)
        print(f"[killswitch_replay] apres dedup: {len(opportunities)} opportunites "
              f"({collapsed} re-evaluations consecutives fusionnees)")

        for opp in opportunities:
            candles = fetch_m1_candles(opp["symbol"], opp["ts"], day_end)
            result = replay_opportunity(opp, candles, day_end)
            opp.update(result)
            upp = usd_per_point_01(opp["symbol"])
            opp["usd_per_point_01"] = upp
            opp["pnl_usd"] = round(opp["price_move"] * upp, 2) if upp else None
            opp["session"] = broker_session_name(opp["ts"])
            print(f"  {opp['ts'].isoformat()} {opp['symbol']} {opp['direction']} "
                  f"-> {opp['outcome']} pnl={opp['pnl_usd']}")

        write_reports(day_start, day_end, trigger_ts, raw_population, clean_rows, excluded_rows,
                      exclusion_reasons_count, opportunities, collapsed, rows)
    finally:
        mt5.shutdown()


def _fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def latest_real_daily_pnl(rows: list[dict]) -> tuple[float | None, datetime | None]:
    """Most recent daily_killswitch.daily_pnl reading logged today -- the
    real, MT5-cross-checked P&L as of the last evaluated cycle (the broker
    day is not necessarily over yet when this runs)."""
    for r in reversed(rows):
        pnl = r.get("daily_killswitch.daily_pnl")
        if pnl is not None:
            return float(pnl), r["_ts"]
    return None, None


def write_reports(day_start, day_end, trigger_ts, raw_population, clean_rows, excluded_rows,
                   exclusion_reasons_count, opportunities, collapsed, all_day_rows) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    total_pnl = sum(o["pnl_usd"] for o in opportunities if o["pnl_usd"] is not None)
    wins = [o for o in opportunities if (o["pnl_usd"] or 0) > 0]
    losses = [o for o in opportunities if (o["pnl_usd"] or 0) < 0]
    win_rate = round(100 * len(wins) / len(opportunities), 1) if opportunities else None

    by_session: dict[str, list] = defaultdict(list)
    for o in opportunities:
        by_session[o["session"]].append(o)

    best = max(opportunities, key=lambda o: o["pnl_usd"] or float("-inf"), default=None)
    worst = min(opportunities, key=lambda o: o["pnl_usd"] or float("inf"), default=None)

    real_daily_pnl, real_pnl_as_of = latest_real_daily_pnl(all_day_rows)
    counterfactual_total = round((real_daily_pnl or 0.0) + total_pnl, 2)

    lines: list[str] = []
    lines.append("# KILLSWITCH_REPLAY — 2026-07-09 (lecture seule, simulation)")
    lines.append("")
    lines.append(f"Fenetre analysee : **{day_start.isoformat()}** -> **{day_end.isoformat()}** (jour broker UTC+3).")
    lines.append(f"Premier declenchement kill-switch : **{trigger_ts.isoformat()}** "
                 f"(session **{broker_session_name(trigger_ts)}**, 7e perte -- 6 pertes deja atteintes juste avant).")
    lines.append("")

    lines.append("## Population retenue")
    lines.append("")
    lines.append(f"- Blocages bruts `DAILY_KILLSWITCH_MAX_LOSSES` apres declenchement : **{len(raw_population)}**")
    lines.append(f"- Apres filtre contrefactuel (auraient passe TOUT le reste) : **{len(clean_rows)}** retenus, "
                 f"**{len(excluded_rows)}** ecartes")
    if exclusion_reasons_count:
        lines.append("")
        lines.append("Raisons d'exclusion (un signal peut cumuler plusieurs raisons) :")
        lines.append("")
        lines.append("| raison | count |")
        lines.append("|---|---|")
        for reason, count in sorted(exclusion_reasons_count.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
    lines.append("")
    lines.append(f"- Deduplication : {collapsed} re-evaluations consecutives du meme setup "
                 f"(meme symbole+sens+strategie, ecart <= {DEDUP_GAP_SECONDS//60} min) fusionnees "
                 f"-> **{len(opportunities)} opportunites de trade distinctes** a rejouer.")
    lines.append("")

    lines.append("## Opportunites rejouees")
    lines.append("")
    lines.append("| heure UTC | session | symbole | sens | entree | SL | TP | 1er touche | R | $ simule |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for o in opportunities:
        lines.append(
            f"| {o['ts'].strftime('%H:%M:%S')} | {o['session']} | {o['symbol']} | {o['direction']} | "
            f"{_fmt(o['entry'])} | {_fmt(o['sl'])} | {_fmt(o['tp'])} | {o['outcome']} | "
            f"{_fmt(o['r_multiple'])} | {_fmt(o['pnl_usd'])} |"
        )
    lines.append("")

    lines.append("## Agregats par session")
    lines.append("")
    lines.append("| session | N | win rate | P&L simule $ |")
    lines.append("|---|---|---|---|")
    for session in ("ASIE", "LONDRES", "OVERLAP", "NEW_YORK"):
        opps = by_session.get(session, [])
        if not opps:
            continue
        s_wins = [o for o in opps if (o["pnl_usd"] or 0) > 0]
        s_pnl = sum(o["pnl_usd"] or 0 for o in opps)
        s_wr = round(100 * len(s_wins) / len(opps), 1) if opps else None
        lines.append(f"| {session} | {len(opps)} | {_fmt(s_wr,1)}% | {_fmt(s_pnl)} |")
    lines.append("")

    lines.append("## Comparaison reel vs contrefactuel")
    lines.append("")
    lines.append(f"- **Reel** (avec kill-switch, tel que le jour broker s'est deroule jusqu'a "
                 f"{real_pnl_as_of.isoformat() if real_pnl_as_of else '?'} UTC -- derniere lecture "
                 f"daily_killswitch.daily_pnl disponible, le jour broker n'est pas necessairement termine) : "
                 f"**{_fmt(real_daily_pnl)} $**")
    lines.append(f"- **P&L simule des opportunites bloquees** : **{_fmt(total_pnl)} $** "
                 f"({len(wins)}G/{len(losses)}P, win rate {_fmt(win_rate,1)}%)")
    lines.append(f"- **Contrefactuel** (reel + simule, si le kill-switch avait ete eteint apres 6 pertes) : "
                 f"**{_fmt(counterfactual_total)} $**")
    lines.append("")
    if best:
        lines.append(f"**Meilleure opportunite simulee** : {best['symbol']} {best['direction']} @ "
                     f"{best['ts'].strftime('%H:%M:%S')} UTC ({best['session']}), {best['outcome']}, "
                     f"{_fmt(best['pnl_usd'])} $.")
    if worst:
        lines.append(f"**Pire opportunite simulee** : {worst['symbol']} {worst['direction']} @ "
                     f"{worst['ts'].strftime('%H:%M:%S')} UTC ({worst['session']}), {worst['outcome']}, "
                     f"{_fmt(worst['pnl_usd'])} $.")
    lines.append("")

    lines.append("## Caveats (obligatoires)")
    lines.append("")
    lines.append("- **C'est une SIMULATION sur bougies historiques, pas la realite.** Ces trades n'ont jamais ete ouverts.")
    lines.append("- Utilise le SL/TP **du signal au moment du blocage** -- n'imite PAS le trailing dynamique d'Exit V2 "
                 "(le vrai resultat aurait pu differer, dans un sens comme dans l'autre, si le trade avait vraiment "
                 "ete pris et gere par Exit V2).")
    lines.append(f"- Suppose un remplissage au prix du signal, avec un spread **estime** (aucune valeur de spread brute "
                 f"n'est loguee sur les lignes refusees) : GOLD# ~{_fmt(SPREAD_ESTIMATE_PRICE['GOLD#'])}$, "
                 f"BTCUSD# ~{_fmt(SPREAD_ESTIMATE_PRICE['BTCUSD#'])}$ (valeurs conservatrices, coherentes avec les "
                 "spreads live observes cette session) -- et que 0.01 lot ne bouge pas le marche.")
    lines.append("- **Un seul jour de donnees ne prouve rien sur le futur.** C'est un indice mesure, pas une loi.")
    n_still_open = sum(1 for o in opportunities if o["outcome"] == "STILL_OPEN")
    if n_still_open:
        lines.append(f"- **{n_still_open}/{len(opportunities)} opportunites sont encore \"STILL_OPEN\"** au moment "
                     "ou ce rapport a ete genere (le jour broker n'etait pas termine) -- valorisees au prix M1 le "
                     "plus recent disponible, PAS a une cloture definitive. Leur contribution au P&L total bouge "
                     "avec le marche a chaque nouvelle execution de ce script tant que le jour n'est pas clos -- "
                     "confirme empiriquement : deux executions a quelques minutes d'intervalle pendant la "
                     "construction de ce rapport ont donne des totaux differents. Le resultat marginal ci-dessous "
                     "en tient compte, mais re-executer ce script apres 21:00 UTC (jour broker clos) donnerait "
                     "un chiffre definitif plutot qu'une estimation en mouvement.")
    lines.append("")

    # Trois issues honnetes, pas deux forcees : un resultat proche de zero
    # (petit P&L, win rate proche de 50%) n'est ni "nettement vert" ni "a
    # clairement saigne" -- le forcer dans l'un des deux gabarits de la
    # mission serait malhonnete. Seuils : "nettement" exige a la fois un
    # P&L notable (>= 2x le pire trade simule individuel, pour ne pas
    # qualifier un resultat qu'un seul trade renverserait) ET un win rate
    # clairement au-dessus de 50%.
    worst_trade_abs = abs(worst["pnl_usd"]) if worst and worst["pnl_usd"] else 1.0
    clearly_green = total_pnl > 0 and total_pnl >= 2 * worst_trade_abs and win_rate is not None and win_rate >= 55
    clearly_red = total_pnl < 0 and abs(total_pnl) >= 2 * worst_trade_abs and win_rate is not None and win_rate <= 45

    if clearly_green:
        verdict = (f"Les heures verrouillees apres le kill-switch (session {broker_session_name(trigger_ts)} "
                   f"et suivantes) auraient ete **nettement vertes** ({_fmt(total_pnl)} $ simules, "
                   f"{_fmt(win_rate,1)}% win rate sur {len(opportunities)} opportunites) : l'intuition de SIMO est "
                   "**mesurement validee** sur cette journee -- un kill-switch par-session pour la phase post-gel "
                   "merite d'etre concu, chiffres a l'appui (mais un seul jour, voir caveats).")
    elif clearly_red:
        verdict = (f"Les heures verrouillees apres le kill-switch auraient **clairement continue a saigner** "
                   f"({_fmt(total_pnl)} $ simules, {_fmt(win_rate,1)}% win rate sur {len(opportunities)} "
                   "opportunites) : le stop journalier est **vindique** sur cette journee -- sujet clos, "
                   "sous reserve d'un seul jour de donnees (voir caveats).")
    else:
        verdict = (f"Resultat **marginal, ni nettement vert ni nettement rouge** ({_fmt(total_pnl)} $ simules sur "
                   f"{len(opportunities)} opportunites, win rate {_fmt(win_rate,1)}% -- quasiment un tirage a pile "
                   f"ou face, et un seul gros trade (le meilleur ou le pire, {_fmt(worst_trade_abs)} $) suffirait a "
                   "inverser le signe). **Aucun verdict tranche sur cette seule journee** : ni l'intuition de SIMO "
                   "ni le statu quo ne sont mesurablement valides ou invalides ici. Rejouer sur plusieurs jours "
                   "additionnels de kill-switch declenche avant de trancher.")
    lines.append("## Verdict")
    lines.append("")
    lines.append(verdict)

    md_path = REPORTS_DIR / "KILLSWITCH_REPLAY.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    csv_path = REPORTS_DIR / "KILLSWITCH_REPLAY.csv"
    import csv
    fieldnames = ["ts", "session", "symbol", "direction", "strategy", "entry", "sl", "tp",
                  "re_evaluations", "outcome", "exit_price", "exit_ts", "r_multiple", "pnl_usd"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for o in opportunities:
            row = {k: o.get(k) for k in fieldnames}
            writer.writerow(row)

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(verdict)
    print("=" * 70)
    print(f"\nLivrables ecrits : {md_path} , {csv_path}")


if __name__ == "__main__":
    main()
