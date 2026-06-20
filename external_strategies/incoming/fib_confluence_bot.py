"""
FibConfluence — Auto-Trading Bot for MetaTrader 5 (Python)
==========================================================

Ports the FibConfluence strategy (Fibonacci golden pocket + market structure +
volume spike + liquidity sweep + order-flow PROXY) into a live bot that:
  * pulls data via the official MetaTrader5 package
  * evaluates the signal on each CLOSED bar
  * sizes the position by % risk of balance (auto lot)
  * sets SL behind the swing and TP at a fixed R multiple (default 2R)
  * sends a market order

HONEST NOTES (read before using real money):
  * "Order flow" here is a PROXY (where the candle closed inside its range),
    NOT real footprint/delta data.
  * "Volume" is TICK volume unless your broker provides real exchange volume.
  * This is decision-support automation, NOT a guaranteed-profit system.
  * The MetaTrader5 package runs on the SAME machine as a running MT5 terminal
    (Windows). Test on a DEMO account first. Past performance != future results.

REQUIREMENTS:
    pip install MetaTrader5 pandas numpy

USAGE:
    1) Open your MT5 terminal and log into the (demo) account.
    2) Edit the Config() values below (symbol, timeframe, risk, etc.).
    3) python fib_confluence_bot.py
    4) Stop with Ctrl+C.
"""

from __future__ import annotations

import time
import math
import logging
from dataclasses import dataclass

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit(
        "Install the MetaTrader5 package first:  pip install MetaTrader5  "
        "(Windows, with the MT5 terminal installed and running)."
    )

import numpy as np
import pandas as pd


# ============================ CONFIG (edit me) ============================
@dataclass
class Config:
    # --- connection ---
    symbol: str = "EURUSD"
    timeframe: str = "M15"            # M1, M5, M15, M30, H1, H4, D1 ...
    magic: int = 905001               # unique id so the bot only manages its own trades

    # Leave login=None to attach to the already-running terminal session.
    login: int | None = None
    password: str | None = None
    server: str | None = None
    terminal_path: str | None = None  # e.g. r"C:\\Program Files\\MetaTrader 5\\terminal64.exe"

    # --- safety ---
    confirm_live_trading: bool = False  # MUST be True to send orders on a REAL account

    # --- risk / exits ---
    risk_percent: float = 0.5           # % of balance risked per trade
    rr_ratio: float = 2.0               # TP = entry + RR * risk  (=> 2R)
    sl_buffer_points: int = 20          # extra points placed BEYOND the swing for the SL
    deviation: int = 20                 # max slippage (points)

    # --- strategy (mirrors the FibConfluence indicator) ---
    lookback: int = 30
    zone_top: float = 0.618             # golden zone start
    zone_bottom: float = 0.786          # golden zone end
    struct_window: int = 15
    vol_period: int = 20
    vol_mult: float = 1.5
    liq_lookback: int = 10

    use_structure: bool = True
    use_volume: bool = True
    use_liquidity: bool = True
    use_orderflow: bool = True
    min_confluence: int = 2             # min confirmations on top of the golden-pocket rejection

    # --- loop ---
    poll_seconds: int = 5
    history_bars: int = 400
# =========================================================================


log = logging.getLogger("FibBot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def tf_const(tf: str):
    name = f"TIMEFRAME_{tf.upper()}"
    if not hasattr(mt5, name):
        raise ValueError(f"Unknown timeframe: {tf}")
    return getattr(mt5, name)


def connect(cfg: Config) -> bool:
    """Initialize MT5, select the symbol, return True if the account is DEMO."""
    kwargs = {}
    if cfg.terminal_path:
        kwargs["path"] = cfg.terminal_path
    if cfg.login is not None:
        kwargs.update(login=cfg.login, password=cfg.password, server=cfg.server)

    if not mt5.initialize(**kwargs):
        raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")

    acc = mt5.account_info()
    if acc is None:
        raise SystemExit(f"account_info() failed: {mt5.last_error()}")

    is_demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    log.info("Connected: account=%s | %s | balance=%.2f %s",
             acc.login, "DEMO" if is_demo else "REAL/CONTEST", acc.balance, acc.currency)

    if not mt5.symbol_select(cfg.symbol, True):
        raise SystemExit(f"symbol_select({cfg.symbol}) failed: {mt5.last_error()}")

    return is_demo


def get_rates(cfg: Config) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(cfg.symbol, tf_const(cfg.timeframe), 0, cfg.history_bars)
    need = cfg.lookback + 2 * cfg.struct_window + cfg.vol_period + 5
    if rates is None or len(rates) < need:
        return None
    return pd.DataFrame(rates)


def detect_signal(df: pd.DataFrame, cfg: Config) -> dict | None:
    """Evaluate the LAST CLOSED bar (index -2). Returns a signal dict or None."""
    n = len(df)
    sig = n - 2                          # last closed bar (-1 is still forming)

    high  = df["high"].to_numpy()
    low   = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    vol   = df["tick_volume"].to_numpy()
    t = int(df["time"].iloc[sig])

    # defensive bounds check
    w_lo = sig - cfg.lookback
    if min(w_lo, sig - 2 * cfg.struct_window, sig - cfg.vol_period, sig - cfg.liq_lookback) < 0:
        return None

    # --- 1) swing leg from bars BEFORE the signal bar ---
    sw_high_idx = w_lo + int(np.argmax(high[w_lo:sig]))
    sw_low_idx  = w_lo + int(np.argmin(low[w_lo:sig]))
    swing_high = high[sw_high_idx]
    swing_low  = low[sw_low_idx]
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    leg_up = sw_high_idx > sw_low_idx     # high more recent -> up-leg -> BUY context

    # --- 2) fib levels ---
    if leg_up:
        z_top = swing_high - rng * cfg.zone_top      # 61.8 (higher price)
        z_bot = swing_high - rng * cfg.zone_bottom   # 78.6 (lower price)
        lvl618 = z_top
    else:
        z_bot = swing_low + rng * cfg.zone_top       # 61.8 (lower price)
        z_top = swing_low + rng * cfg.zone_bottom    # 78.6 (higher price)
        lvl618 = z_bot

    o, h, l, c = open_[sig], high[sig], low[sig], close[sig]
    bar_rng = max(h - l, 1e-12)

    # --- 3) market structure (HH/HL vs LH/LL) ---
    rec_hh = high[sig - cfg.struct_window:sig].max()
    rec_ll = low[sig - cfg.struct_window:sig].min()
    pri_hh = high[sig - 2 * cfg.struct_window:sig - cfg.struct_window].max()
    pri_ll = low[sig - 2 * cfg.struct_window:sig - cfg.struct_window].min()
    bull_struct = rec_hh > pri_hh and rec_ll > pri_ll
    bear_struct = rec_hh < pri_hh and rec_ll < pri_ll

    # --- 4) volume spike ---
    avg_vol = vol[sig - cfg.vol_period:sig].mean()
    vol_spike = vol[sig] > avg_vol * cfg.vol_mult

    # --- 5) order-flow PROXY (close position in candle range) ---
    cp = (c - l) / bar_rng
    of_bull = cp >= 0.66
    of_bear = cp <= 0.34

    # --- 6) liquidity sweep ---
    sweep_low  = low[sig - cfg.liq_lookback:sig].min()
    sweep_high = high[sig - cfg.liq_lookback:sig].max()
    liq_bull = l < sweep_low and c > sweep_low
    liq_bear = h > sweep_high and c < sweep_high

    # --- 7) decision ---
    direction, score = None, 0

    if leg_up and c > swing_low:
        touched = l <= z_top
        rejection = c > lvl618 and c > o
        if touched and rejection:
            if cfg.use_structure and bull_struct: score += 1
            if cfg.use_volume and vol_spike:      score += 1
            if cfg.use_liquidity and liq_bull:    score += 1
            if cfg.use_orderflow and of_bull:     score += 1
            if score >= cfg.min_confluence:
                direction = "buy"

    elif (not leg_up) and c < swing_high:
        touched = h >= z_bot
        rejection = c < lvl618 and c < o
        if touched and rejection:
            if cfg.use_structure and bear_struct: score += 1
            if cfg.use_volume and vol_spike:      score += 1
            if cfg.use_liquidity and liq_bear:    score += 1
            if cfg.use_orderflow and of_bear:     score += 1
            if score >= cfg.min_confluence:
                direction = "sell"

    if direction is None:
        return None

    return {
        "direction": direction,
        "bar_time": t,
        "swing_high": float(swing_high),
        "swing_low": float(swing_low),
        "zone": (float(min(z_top, z_bot)), float(max(z_top, z_bot))),
        "score": int(score),
    }


def calc_lot(cfg: Config, entry: float, sl: float) -> float | None:
    """Lot size so that hitting the SL loses ~risk_percent of balance."""
    info = mt5.symbol_info(cfg.symbol)
    acc = mt5.account_info()
    if info is None or acc is None:
        return None

    tick_size = info.trade_tick_size or info.point
    tick_value = info.trade_tick_value
    sl_dist = abs(entry - sl)
    if sl_dist <= 0 or tick_size <= 0 or tick_value <= 0:
        return None

    risk_money = acc.balance * cfg.risk_percent / 100.0
    loss_per_lot = sl_dist / tick_size * tick_value
    if loss_per_lot <= 0:
        return None

    lot = risk_money / loss_per_lot
    step = info.volume_step or 0.01
    lot = math.floor(lot / step) * step
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    lot = round(lot, decimals)

    if lot < info.volume_min:
        log.warning("Lot %.4f < min %.4f (risk %.2f%% too small for this SL). Skipping trade.",
                    lot, info.volume_min, cfg.risk_percent)
        return None
    return min(lot, info.volume_max)


def pick_filling(symbol: str):
    try:
        mode = mt5.symbol_info(symbol).filling_mode
    except Exception:
        return mt5.ORDER_FILLING_IOC
    if mode & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if mode & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def has_open_position(cfg: Config) -> bool:
    positions = mt5.positions_get(symbol=cfg.symbol)
    if not positions:
        return False
    return any(p.magic == cfg.magic for p in positions)


def open_trade(cfg: Config, sig: dict, can_trade: bool) -> None:
    tick = mt5.symbol_info_tick(cfg.symbol)
    info = mt5.symbol_info(cfg.symbol)
    if tick is None or info is None:
        log.error("No tick/symbol info; aborting trade.")
        return

    buf = cfg.sl_buffer_points * info.point

    if sig["direction"] == "buy":
        entry = tick.ask
        sl = sig["swing_low"] - buf
        if entry <= sl:
            log.warning("BUY entry <= SL, skip.")
            return
        risk = entry - sl
        tp = entry + cfg.rr_ratio * risk
        order_type = mt5.ORDER_TYPE_BUY
    else:
        entry = tick.bid
        sl = sig["swing_high"] + buf
        if entry >= sl:
            log.warning("SELL entry >= SL, skip.")
            return
        risk = sl - entry
        tp = entry - cfg.rr_ratio * risk
        order_type = mt5.ORDER_TYPE_SELL

    lot = calc_lot(cfg, entry, sl)
    if lot is None:
        return

    d = info.digits
    entry, sl, tp = round(entry, d), round(sl, d), round(tp, d)

    log.info("SIGNAL %s | score=%d/4 | entry=%.*f SL=%.*f TP=%.*f | lot=%.2f | risk=%.*f",
             sig["direction"].upper(), sig["score"], d, entry, d, sl, d, tp, lot, d, risk)

    if not can_trade:
        log.warning("REAL account not confirmed -> OBSERVE mode, no order sent. "
                    "Set confirm_live_trading=True to trade a real account.")
        return

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": cfg.symbol,
        "volume": lot,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": cfg.deviation,
        "magic": cfg.magic,
        "comment": "FibConfluence",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": pick_filling(cfg.symbol),
    }

    # --- OPTIONAL: route execution through Easy_Trading instead of raw order_send ---
    #   from Easy_Trading import Easy_Trading
    #   et = Easy_Trading(cfg.login, cfg.password, cfg.server)
    #   if sig["direction"] == "buy":  et.buy(...)      # <- check exact args in the repo
    #   else:                          et.sell(...)
    #   return
    # --------------------------------------------------------------------------------

    result = mt5.order_send(request)
    if result is None:
        log.error("order_send returned None: %s", mt5.last_error())
    elif result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order rejected: retcode=%s | %s", result.retcode, result.comment)
    else:
        log.info("Order FILLED: ticket=%s | price=%.*f | vol=%.2f",
                 result.order, d, result.price, result.volume)


def run(cfg: Config) -> None:
    setup_logging()
    is_demo = connect(cfg)
    can_trade = is_demo or cfg.confirm_live_trading
    if not is_demo and not cfg.confirm_live_trading:
        log.warning("REAL account + confirm_live_trading=False -> running in OBSERVE mode (no orders).")

    log.info("FibConfluence bot started | %s %s | risk=%.2f%% | RR=1:%.1f | min_conf=%d",
             cfg.symbol, cfg.timeframe, cfg.risk_percent, cfg.rr_ratio, cfg.min_confluence)

    last_forming_time = None
    last_traded_bar = None

    try:
        while True:
            df = get_rates(cfg)
            if df is None:
                time.sleep(cfg.poll_seconds)
                continue

            forming_time = int(df["time"].iloc[-1])

            if last_forming_time is None:
                last_forming_time = forming_time          # don't act mid-bar on startup
            elif forming_time != last_forming_time:
                last_forming_time = forming_time          # new bar -> previous one just CLOSED
                sig = detect_signal(df, cfg)
                if sig is None:
                    pass
                elif sig["bar_time"] == last_traded_bar:
                    pass
                elif has_open_position(cfg):
                    log.info("Signal %s ignored: a position is already open on %s.",
                             sig["direction"].upper(), cfg.symbol)
                else:
                    last_traded_bar = sig["bar_time"]
                    open_trade(cfg, sig, can_trade)

            time.sleep(cfg.poll_seconds)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run(Config())
