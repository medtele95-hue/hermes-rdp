"""
FibConfluence — BTCUSD Auto-Trading Bot for MetaTrader 5 (Python)
================================================================

Same FibConfluence strategy as the base bot, but TUNED FOR CRYPTO (BTCUSD CFD on MT5).
What changed vs the forex version, and WHY:

  * SL buffer is now ATR-BASED (not fixed points). BTC's "point" value differs per
    broker and a fixed point buffer is meaningless on a $90k asset -> the SL room
    scales with volatility instead.
  * Lower default risk_percent (BTC can move several % in a day).
  * Max-spread filter: skips entries when the spread is wide relative to ATR
    (crypto spreads blow out fast).
  * Respects the broker's minimum stop distance (trade_stops_level) so orders
    aren't rejected for SL/TP being too close.
  * Symbol auto-detect: if "BTCUSD" doesn't exist, it searches for a *BTC* symbol.
  * 24/7 friendly (no session assumptions).

HONEST NOTES (unchanged): "order flow" = a PROXY (close position in range), "volume"
= TICK volume, this is NOT a guaranteed system. Run on the SAME machine as a running
MT5 terminal (Windows). TEST ON DEMO FIRST.

REQUIREMENTS:  pip install MetaTrader5 pandas numpy
USAGE:
    1) Open MT5, log into the (demo) account, make sure a BTC symbol is in Market Watch.
    2) Edit Config() below.
    3) python fib_confluence_bot_btc.py   (stop with Ctrl+C)
    4) READ the "Symbol ..." line it prints at startup and sanity-check tick_value etc.
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
    symbol: str = "BTCUSD"            # auto-detected if this exact name isn't found
    timeframe: str = "M15"
    magic: int = 905002

    login: int | None = None
    password: str | None = None
    server: str | None = None
    terminal_path: str | None = None

    # --- safety ---
    confirm_live_trading: bool = False  # MUST be True to send orders on a REAL account

    # --- risk / exits (BTC-tuned) ---
    risk_percent: float = 0.25          # % of balance per trade (low: BTC is volatile)
    rr_ratio: float = 2.0               # TP = entry + RR * risk (2R)
    sl_atr_period: int = 14
    sl_atr_mult: float = 0.5            # SL buffer beyond the swing = 0.5 * ATR
    min_sl_points: int = 0              # optional extra floor in points (0 = ATR + broker min)
    max_spread_atr_frac: float = 0.15   # skip entry if spread > 15% of ATR
    deviation: int = 100                # slippage room (points) — adjust to your symbol digits

    # --- strategy (same as the indicator) ---
    lookback: int = 30
    zone_top: float = 0.618
    zone_bottom: float = 0.786
    struct_window: int = 15
    vol_period: int = 20
    vol_mult: float = 1.5
    liq_lookback: int = 10

    use_structure: bool = True
    use_volume: bool = True
    use_liquidity: bool = True
    use_orderflow: bool = True
    min_confluence: int = 2

    # --- loop ---
    poll_seconds: int = 5
    history_bars: int = 500
# =========================================================================


log = logging.getLogger("FibBotBTC")


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


def resolve_symbol(preferred: str) -> str:
    """Return the exact symbol name; if not found, search for a *BTC* / USD symbol."""
    if mt5.symbol_info(preferred) is not None:
        return preferred
    found = mt5.symbols_get("*BTC*")
    if found:
        usd = [s.name for s in found if "USD" in s.name.upper()]
        return usd[0] if usd else found[0].name
    return preferred


def log_symbol_specs(symbol: str) -> None:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None:
        return
    spread = (tick.ask - tick.bid) if tick else float("nan")
    log.info("Symbol %s | digits=%d point=%g | vol[min=%g step=%g max=%g] | "
             "tick_value=%g tick_size=%g contract=%g | stops_level=%d pts | spread=%.2f",
             symbol, info.digits, info.point, info.volume_min, info.volume_step,
             info.volume_max, info.trade_tick_value, info.trade_tick_size,
             info.trade_contract_size, info.trade_stops_level, spread)


def connect(cfg: Config) -> bool:
    """Initialize MT5, resolve + select the symbol, return True if account is DEMO."""
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

    resolved = resolve_symbol(cfg.symbol)
    if resolved != cfg.symbol:
        log.warning("Symbol '%s' not found; using '%s' instead.", cfg.symbol, resolved)
        cfg.symbol = resolved

    if not mt5.symbol_select(cfg.symbol, True):
        raise SystemExit(f"symbol_select({cfg.symbol}) failed: {mt5.last_error()}")

    log_symbol_specs(cfg.symbol)
    return is_demo


def get_rates(cfg: Config) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(cfg.symbol, tf_const(cfg.timeframe), 0, cfg.history_bars)
    need = cfg.lookback + 2 * cfg.struct_window + max(cfg.vol_period, cfg.sl_atr_period) + 5
    if rates is None or len(rates) < need:
        return None
    return pd.DataFrame(rates)


def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def detect_signal(df: pd.DataFrame, cfg: Config) -> dict | None:
    """Evaluate the LAST CLOSED bar (index -2). Returns a signal dict or None."""
    n = len(df)
    sig = n - 2

    high  = df["high"].to_numpy()
    low   = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    vol   = df["tick_volume"].to_numpy()
    t = int(df["time"].iloc[sig])

    w_lo = sig - cfg.lookback
    if min(w_lo, sig - 2 * cfg.struct_window, sig - cfg.vol_period, sig - cfg.liq_lookback) < 0:
        return None

    # --- 1) swing leg ---
    sw_high_idx = w_lo + int(np.argmax(high[w_lo:sig]))
    sw_low_idx  = w_lo + int(np.argmin(low[w_lo:sig]))
    swing_high = high[sw_high_idx]
    swing_low  = low[sw_low_idx]
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    leg_up = sw_high_idx > sw_low_idx

    # --- 2) fib levels ---
    if leg_up:
        z_top = swing_high - rng * cfg.zone_top
        z_bot = swing_high - rng * cfg.zone_bottom
        lvl618 = z_top
    else:
        z_bot = swing_low + rng * cfg.zone_top
        z_top = swing_low + rng * cfg.zone_bottom
        lvl618 = z_bot

    o, h, l, c = open_[sig], high[sig], low[sig], close[sig]
    bar_rng = max(h - l, 1e-12)

    # --- 3) structure ---
    rec_hh = high[sig - cfg.struct_window:sig].max()
    rec_ll = low[sig - cfg.struct_window:sig].min()
    pri_hh = high[sig - 2 * cfg.struct_window:sig - cfg.struct_window].max()
    pri_ll = low[sig - 2 * cfg.struct_window:sig - cfg.struct_window].min()
    bull_struct = rec_hh > pri_hh and rec_ll > pri_ll
    bear_struct = rec_hh < pri_hh and rec_ll < pri_ll

    # --- 4) volume spike ---
    avg_vol = vol[sig - cfg.vol_period:sig].mean()
    vol_spike = vol[sig] > avg_vol * cfg.vol_mult

    # --- 5) order-flow proxy ---
    cp = (c - l) / bar_rng
    of_bull = cp >= 0.66
    of_bear = cp <= 0.34

    # --- 6) liquidity sweep ---
    sweep_low  = low[sig - cfg.liq_lookback:sig].min()
    sweep_high = high[sig - cfg.liq_lookback:sig].max()
    liq_bull = l < sweep_low and c > sweep_low
    liq_bear = h > sweep_high and c < sweep_high

    # --- ATR (for the volatility-scaled SL buffer) ---
    atr_val = float(atr_series(df, cfg.sl_atr_period).iloc[sig])
    if not math.isfinite(atr_val) or atr_val <= 0:
        atr_val = 0.0

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
        "atr": atr_val,
        "score": int(score),
    }


def calc_lot(cfg: Config, entry: float, sl: float) -> float | None:
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
        log.warning("Lot %.4f < min %.4f (risk %.2f%% too small for this SL distance). Skipping.",
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

    point = info.point
    atr = sig["atr"]

    # --- spread filter (crypto spreads blow out) ---
    spread_price = tick.ask - tick.bid
    if atr > 0 and spread_price > cfg.max_spread_atr_frac * atr:
        log.info("Spread %.2f too wide vs ATR %.2f (> %.0f%%). Skipping %s.",
                 spread_price, atr, cfg.max_spread_atr_frac * 100, sig["direction"].upper())
        return

    # volatility-scaled SL buffer beyond the swing
    buf = max(cfg.sl_atr_mult * atr, cfg.min_sl_points * point)
    min_stop = info.trade_stops_level * point   # broker's minimum SL/TP distance

    if sig["direction"] == "buy":
        entry = tick.ask
        sl = sig["swing_low"] - buf
        if entry - sl < min_stop:                # respect broker minimum distance
            sl = entry - min_stop
        if entry <= sl:
            log.warning("BUY entry <= SL, skip.")
            return
        risk = entry - sl
        tp = entry + cfg.rr_ratio * risk
        order_type = mt5.ORDER_TYPE_BUY
    else:
        entry = tick.bid
        sl = sig["swing_high"] + buf
        if sl - entry < min_stop:
            sl = entry + min_stop
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

    log.info("SIGNAL %s | score=%d/4 | entry=%.*f SL=%.*f TP=%.*f | lot=%g | risk=%.*f | ATR=%.2f",
             sig["direction"].upper(), sig["score"], d, entry, d, sl, d, tp, lot, d, risk, atr)

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
        "comment": "FibConfluence-BTC",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": pick_filling(cfg.symbol),
    }

    # --- OPTIONAL: route through Easy_Trading instead of raw order_send ---
    #   from Easy_Trading import Easy_Trading
    #   et = Easy_Trading(cfg.login, cfg.password, cfg.server)
    #   et.buy(...) / et.sell(...)   # check the exact signature in the repo
    #   return
    # ----------------------------------------------------------------------

    result = mt5.order_send(request)
    if result is None:
        log.error("order_send returned None: %s", mt5.last_error())
    elif result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order rejected: retcode=%s | %s", result.retcode, result.comment)
    else:
        log.info("Order FILLED: ticket=%s | price=%.*f | vol=%g",
                 result.order, d, result.price, result.volume)


def run(cfg: Config) -> None:
    setup_logging()
    is_demo = connect(cfg)
    can_trade = is_demo or cfg.confirm_live_trading
    if not is_demo and not cfg.confirm_live_trading:
        log.warning("REAL account + confirm_live_trading=False -> OBSERVE mode (no orders).")

    log.info("FibConfluence-BTC started | %s %s | risk=%.2f%% | RR=1:%.1f | SL=%.2f*ATR | min_conf=%d",
             cfg.symbol, cfg.timeframe, cfg.risk_percent, cfg.rr_ratio, cfg.sl_atr_mult, cfg.min_confluence)

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
                last_forming_time = forming_time
            elif forming_time != last_forming_time:
                last_forming_time = forming_time
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
