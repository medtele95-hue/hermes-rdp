# -*- coding: utf-8 -*-
"""mission/MISSION_AUTOPSY_TRADE.md -- READ-ONLY forensic autopsy of GOLD#
BUY ticket 379832386 (-$20.08, SL hit). Explains, does not correct.

Isolated tool, outside every decision path. Never calls order_send, never
imports/modifies the trading path, never writes to decision_dataset.jsonl.
Reads: MT5 M1 candle history (read-only copy_rates_range) + MT5 deal
history (read-only history_deals_get), mirroring the exact safe pattern
already used by tools/killswitch_replay.py this session (mt5.initialize()
in a short-lived isolated process, never conflicts with the live bot's own
connection -- proven safe earlier this session). Writes nothing; prints to
stdout only, for the author to fold into reports/AUTOPSY_379832386.md.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.utils.broker_time import to_mt5_query_bounds  # noqa: E402

BROKER_OFFSET_HOURS = 3.0
TICKET = 379832386
SYMBOL = "GOLD#"
ENTRY_FILL = 4119.74
SL = 4099.68
TP = 4148.86
OPEN_UTC = datetime(2026, 7, 10, 4, 12, 59, tzinfo=timezone.utc)
# generous window past the log-observed close (~08:37 UTC) to be sure we
# capture the true close candle
CLOSE_WINDOW_END_UTC = datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)


def main() -> None:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(SYMBOL, True)
        # --- deal history cross-check (authoritative close price/time/reason) ---
        q_start, q_end = to_mt5_query_bounds(OPEN_UTC - timedelta(minutes=5), CLOSE_WINDOW_END_UTC + timedelta(hours=1), BROKER_OFFSET_HOURS)
        deals = mt5.history_deals_get(q_start, q_end)
        print(f"[deals] {len(deals or [])} total deals fetched in window, filtering for position {TICKET}")
        ticket_deals = [d for d in (deals or []) if d.position_id == TICKET or d.order == TICKET or d.ticket == TICKET]
        print(f"[deals] found {len(ticket_deals)} deal(s) for position/order {TICKET}")
        close_deal = None
        for d in ticket_deals:
            broker_naive = datetime.utcfromtimestamp(d.time)
            true_utc = broker_naive.replace(tzinfo=timezone.utc) - timedelta(hours=BROKER_OFFSET_HOURS)
            print(
                f"  deal={d.ticket} entry_type={d.entry} price={d.price} profit={d.profit} "
                f"reason={d.reason} comment={d.comment!r} time_broker_raw={broker_naive} time_utc_true={true_utc}"
            )
            if d.entry == 1:  # DEAL_ENTRY_OUT
                close_deal = (d, true_utc)

        if close_deal is None:
            print("[deals] NO closing deal found (entry=1/OUT) -- cannot confirm close mechanism")
            close_utc = CLOSE_WINDOW_END_UTC
        else:
            close_utc = close_deal[1]
            print(f"[deals] CLOSE confirmed: price={close_deal[0].price} reason_code={close_deal[0].reason} "
                  f"(4=SL, 5=TP, 0=CLIENT, 3=EXPERT) at {close_utc} UTC true")

        # --- M1 candles for MFE/MAE across the trade's life ---
        # NOTE: to_mt5_query_bounds's verified +3h offset is documented and
        # proven ONLY for deal.time (see close-deal cross-check above, which
        # matched the mission's stated broker time exactly with offset=3.0).
        # Empirically, M1 rate bar "time" for GOLD# returned a window shifted
        # exactly 1h earlier than requested under that same offset -- probing
        # here to find the bar-time offset that actually covers the trade's
        # full life through the confirmed SL close at 08:37:11 UTC true.
        expected_minutes = int((close_utc - OPEN_UTC).total_seconds() // 60) + 1
        candles = []
        used_offset = None
        for probe_offset in (3.0, 2.0):
            q_start2, q_end2 = to_mt5_query_bounds(OPEN_UTC, close_utc + timedelta(minutes=5), probe_offset)
            rates = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, q_start2, q_end2)
            probe_candles = list(rates) if rates is not None else []
            if probe_candles:
                last_broker_naive = datetime.utcfromtimestamp(probe_candles[-1]["time"])
                last_true_utc = last_broker_naive.replace(tzinfo=timezone.utc) - timedelta(hours=probe_offset)
                covers_close = last_true_utc >= close_utc - timedelta(minutes=2)
                print(f"[m1 probe] offset={probe_offset}h -> {len(probe_candles)} candles, last bar true_utc={last_true_utc} covers_close={covers_close}")
                if covers_close:
                    candles = probe_candles
                    used_offset = probe_offset
                    break
        if not candles:
            print("[m1] WARNING: no offset probe covered the full trade life -- using best-effort last probe")
            candles = probe_candles
            used_offset = probe_offset
        RATE_OFFSET_HOURS = used_offset
        print(f"[m1] using RATE_OFFSET_HOURS={RATE_OFFSET_HOURS} for this trade's M1 analysis (expected ~{expected_minutes} candles, got {len(candles)})")

        best_high = ENTRY_FILL
        best_high_ts = None
        worst_low = ENTRY_FILL
        worst_low_ts = None
        for c in candles:
            c_broker_naive = datetime.utcfromtimestamp(c["time"])
            c_true_utc = c_broker_naive.replace(tzinfo=timezone.utc) - timedelta(hours=RATE_OFFSET_HOURS)
            if c_true_utc < OPEN_UTC or c_true_utc > close_utc:
                continue
            if c["high"] > best_high:
                best_high = c["high"]
                best_high_ts = c_true_utc
            if c["low"] < worst_low:
                worst_low = c["low"]
                worst_low_ts = c_true_utc

        mfe_pts = best_high - ENTRY_FILL
        mae_pts = ENTRY_FILL - worst_low
        print(f"[mfe] best high reached = {best_high} at {best_high_ts} UTC true -> MFE = +{mfe_pts:.2f} pts from fill")
        print(f"[mae] worst low reached = {worst_low} at {worst_low_ts} UTC true -> MAE = -{mae_pts:.2f} pts from fill")
        raw_times = [datetime.utcfromtimestamp(c["time"]) for c in candles]
        print(f"[debug] raw candle time range: min={min(raw_times)} max={max(raw_times)} sorted={raw_times == sorted(raw_times)}")
        print("[debug] last 8 M1 candles by array order:")
        for c in candles[-8:]:
            c_broker_naive = datetime.utcfromtimestamp(c["time"])
            c_true_utc = c_broker_naive.replace(tzinfo=timezone.utc) - timedelta(hours=RATE_OFFSET_HOURS)
            print(f"  {c_true_utc} UTC true  O={c['open']} H={c['high']} L={c['low']} C={c['close']}")
        print(f"[context] SL={SL} ({ENTRY_FILL - SL:.2f} pts away) TP={TP} ({TP - ENTRY_FILL:.2f} pts away)")

        # --- descriptive ATR (H1, 14-period, standard Wilder) at entry time, for context only ---
        atr_end = OPEN_UTC
        atr_start = OPEN_UTC - timedelta(hours=24 * 3)
        aq_start, aq_end = to_mt5_query_bounds(atr_start, atr_end, BROKER_OFFSET_HOURS)
        h1 = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_H1, aq_start, aq_end)
        h1 = list(h1) if h1 is not None else []
        if len(h1) >= 15:
            trs = []
            for i in range(1, len(h1)):
                high = h1[i]["high"]
                low = h1[i]["low"]
                prev_close = h1[i - 1]["close"]
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            last14 = trs[-14:]
            atr14 = sum(last14) / len(last14)
            print(f"[atr] descriptive H1 ATR(14) approx at entry = {atr14:.2f} pts (simple mean of TR, {len(h1)} H1 candles fetched)")
            print(f"[atr] sl_dist_atr (theoretical, entry 4119.35/sl 4099.67782) = {(4119.35 - 4099.67782) / atr14:.3f}")
            print(f"[atr] sl_dist_atr (realized fill 4119.74/sl 4099.68) = {(ENTRY_FILL - SL) / atr14:.3f}")
        else:
            print(f"[atr] insufficient H1 history fetched ({len(h1)} candles) -- skipping ATR estimate")

    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
