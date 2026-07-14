# -*- coding: utf-8 -*-
"""REPLAY D'IMPACT P0-TER — LECTURE SEULE. AUCUN ORDRE MT5 N'EST ENVOYE.

Combien de verdicts top-down auraient change si le lecteur n'avait jamais vu la
bougie EN COURS ? C'est l'ampleur du bruit qu'on retire de la collecte v2.

METHODE — on ne devine pas, on RECONSTRUIT ce que le bot voyait vraiment.
Le dataset ne stocke pas les frames : impossible de rejouer les decisions loggees
telles quelles. Mais on peut reconstituer, a la minute pres, ce que
`copy_rates_from_pos(symbol, tf, 0, count)` renvoyait a un instant t : la bougie en
cours d'un M5 a l'instant t vaut open = son open, high = max des M1 ecoulees,
low = min des M1 ecoulees, close = close de la derniere M1. Les bougies M1 nous
donnent donc l'etat PARTIEL exact de toutes les bougies superieures.

Pour chaque instant de decision t :
  - frames(t) = bougies CLOTUREES + la bougie EN COURS reconstituee  (ce que le bot recevait)
  - verdict ANCIEN = le lecteur lisant cette bougie en cours  (closed_frame neutralise)
  - verdict NOUVEAU = le lecteur l'ignorant                    (code actuel)
Meme entry/SL/TP dans les deux cas : on isole STRICTEMENT l'effet des frames.

Usage :  python tools/replay_p0ter_impact.py [--points 240] [--symbol GOLD#]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5  # noqa: E402

from app.services import top_down_market_reader as tdm  # noqa: E402
from app.utils.candles import closed_frame  # noqa: E402
from app.utils.indicators import atr_last  # noqa: E402

# MT5DataReader.get_all_timeframes() charge 300 bougies par timeframe : le replay
# doit voir EXACTEMENT ce que le bot voit, sinon il rejoue un autre systeme.
PRODUCTION_COUNT = 300

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
TF_MT5 = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
}
# Les champs qui DECIDENT (les confirmations dures) et le verdict lui-meme.
CHAMPS = ("decision", "top_down_status", "entry_readiness_score", "liquidity_sweep",
          "bos_choch", "m15_confirmation", "m1_trigger", "ob_fvg_present", "m5_context")


def _fetch(symbol: str, tf: str, count: int) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, TF_MT5[tf], 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["candle_time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def _partial_candle(m1: pd.DataFrame, open_time: pd.Timestamp, t: pd.Timestamp) -> dict | None:
    """La bougie EN COURS a l'instant t, reconstituee depuis les M1 ecoulees."""
    inside = m1[(m1["candle_time"] >= open_time) & (m1["candle_time"] < t)]
    if inside.empty:
        return None
    return {
        "candle_time": open_time,
        "open": float(inside.iloc[0]["open"]),
        "high": float(inside["high"].max()),
        "low": float(inside["low"].min()),
        "close": float(inside.iloc[-1]["close"]),
        "tick_volume": int(inside["tick_volume"].sum()),
        "spread": float(inside.iloc[-1].get("spread", 0)),
    }


def _frames_as_of(hist: dict[str, pd.DataFrame], m1: pd.DataFrame, t: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Les frames que le bot recevait a l'instant t : cloturees + bougie en cours.

    On garde PRODUCTION_COUNT bougies par timeframe : c'est exactement ce que
    MT5DataReader.get_all_timeframes(count=300) livre au bot. Rejouer sur des frames
    plus longs donnerait un autre systeme (EMA200 et profils de volume changeraient).
    """
    out: dict[str, pd.DataFrame] = {}
    for tf, df in hist.items():
        minutes = TF_MINUTES[tf]
        closes = df[df["candle_time"] + pd.Timedelta(minutes=minutes) <= t]
        open_time = df[df["candle_time"] <= t]["candle_time"].max()
        rows = closes
        if pd.notna(open_time) and open_time + pd.Timedelta(minutes=minutes) > t:
            partial = _partial_candle(m1, open_time, t)
            if partial is not None:
                rows = pd.concat([closes, pd.DataFrame([partial])], ignore_index=True)
        out[tf] = rows.tail(PRODUCTION_COUNT).reset_index(drop=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GOLD#")
    ap.add_argument("--points", type=int, default=240, help="instants de decision rejoues")
    args = ap.parse_args()

    if not mt5.initialize():
        print("MT5 indisponible")
        return 1

    # On charge PLUS que 300 pour pouvoir couper a l'instant t et qu'il en reste 300.
    hist = {tf: _fetch(args.symbol, tf, PRODUCTION_COUNT + 400) for tf in TF_MINUTES}
    m1 = _fetch(args.symbol, "M1", 5000)
    mt5.shutdown()
    if m1.empty or any(df.empty for df in hist.values()):
        print("historique insuffisant")
        return 1

    reader = tdm.TopDownMarketReader()
    # Les instants de decision : toutes les 5 minutes, comme le cycle du bot, mais
    # DECALES d'1 a 4 minutes dans la bougie M5 -> le bot decide bel et bien PENDANT
    # qu'une bougie M5 se forme. C'est exactement la situation du repaint.
    m5_times = hist["M5"]["candle_time"].tolist()
    instants: list[pd.Timestamp] = []
    for open_time in m5_times[-(args.points // 4 + 5):]:
        for offset in (1, 2, 3, 4):
            instants.append(open_time + pd.Timedelta(minutes=offset))
    instants = [t for t in instants if t > m1["candle_time"].min() + pd.Timedelta(hours=2)][-args.points:]

    total = 0
    changes: Counter = Counter()
    verdict_flip = Counter()
    exemples: list[str] = []

    for t in instants:
        frames = _frames_as_of(hist, m1, t)
        if any(len(df) < tdm.MIN_CLOSED_CANDLES + 1 for df in frames.values()):
            continue

        # entry/SL/TP identiques dans les deux runs : on isole l'effet des frames.
        m5_closed = closed_frame(frames["M5"])
        price = float(m5_closed.iloc[-1]["close"])
        atr = atr_last(m5_closed, 14) or (price * 0.001)

        for direction in ("BUY", "SELL"):
            sign = 1.0 if direction == "BUY" else -1.0
            sl = price - sign * atr
            tp = price + sign * atr * 2.0

            nouveau = reader.evaluate(args.symbol, frames, direction, price, sl, tp, 1, 30, None)

            tdm.closed_frame = lambda df: df          # <- l'ANCIEN lecteur, qui voit le live
            try:
                ancien = reader.evaluate(args.symbol, frames, direction, price, sl, tp, 1, 30, None)
            finally:
                tdm.closed_frame = closed_frame       # on remet le vrai

            total += 1
            diff = [c for c in CHAMPS if ancien.get(c) != nouveau.get(c)]
            if diff:
                for c in diff:
                    changes[c] += 1
            if ancien.get("decision") != nouveau.get("decision"):
                verdict_flip[f'{ancien.get("decision")} -> {nouveau.get("decision")}'] += 1
                if len(exemples) < 5:
                    exemples.append(
                        f'    {t:%Y-%m-%d %H:%M} {direction:<4} '
                        f'{ancien.get("decision")} -> {nouveau.get("decision")}  '
                        f'(score {ancien.get("entry_readiness_score")} -> {nouveau.get("entry_readiness_score")})'
                    )

    print("=" * 72)
    print(f"REPLAY P0-TER — {args.symbol} — {total} verdicts rejoues (LECTURE SEULE)")
    print("=" * 72)
    if not total:
        print("aucun instant exploitable")
        return 1

    flips = sum(verdict_flip.values())
    print(f"\nVERDICTS QUI CHANGENT (decision ALLOW_DEMO/AVOID/WAIT) : {flips} / {total}"
          f"  = {100.0 * flips / total:.1f} %")
    for k, v in verdict_flip.most_common():
        print(f"    {k:<28} {v:>5}  ({100.0 * v / total:.1f} %)")
    if exemples:
        print("\n  exemples :")
        for e in exemples:
            print(e)

    print(f"\nCHAMPS QUI REPEIGNENT (un champ = une confirmation du verdict) :")
    for champ in CHAMPS:
        n = changes.get(champ, 0)
        if n:
            print(f"    {champ:<24} {n:>5} / {total}  ({100.0 * n / total:.1f} %)")
    if not changes:
        print("    aucun — le verdict etait deja stable sur cet echantillon")

    print("\nLecture : ces verdicts changeaient parce que le lecteur decidait sur une")
    print("bougie non close. Depuis FIX 1 de P0-BIS, ce verdict BLOQUE les ordres :")
    print("chaque ligne ci-dessus est un ordre pris — ou refuse — sur du bruit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
