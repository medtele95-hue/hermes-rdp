# =====================================================================
#  mt5_agent.py  —  LIVE DEMO analysis loop (Windows RDP + MT5)
#  - Real M1/M5 data, real spread.
#  - Hard demo-account gate. EXECUTE_TRADES off by default.
#  - The ONLY place mt5.order_send is called (demo-gated send_demo_order).
# =====================================================================
import time
import json
import datetime
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

import config as cfg
import strategy
import risk_manager


def _tf(name):
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5}[name]


def get_rates(symbol, tf_name, n):
    r = mt5.copy_rates_from_pos(symbol, _tf(tf_name), 0, n)
    if r is None or len(r) == 0:
        raise RuntimeError(f"No rates for {symbol} {tf_name}: {mt5.last_error()}")
    df = pd.DataFrame(r)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close'}, inplace=True)
    return df


def connect():
    if mt5 is None:
        raise RuntimeError("MetaTrader5 module not installed. Run on Windows RDP with MT5 + `pip install MetaTrader5`.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    acc = mt5.account_info()
    if acc is None:
        mt5.shutdown()
        raise RuntimeError("No MT5 account info. Log into your DEMO account in the terminal first.")
    is_demo = (acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)
    if cfg.DEMO_ONLY and not is_demo:
        mt5.shutdown()
        raise RuntimeError("SAFETY STOP: account is NOT a demo account and DEMO_ONLY=True. Refusing to run.")
    if not mt5.symbol_select(cfg.SYMBOL, True):
        raise RuntimeError(f"Cannot select symbol {cfg.SYMBOL}.")
    return acc, is_demo


def _filling_mode(info):
    # pick a filling mode the broker supports
    try:
        if info.filling_mode & 1:  return mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:  return mt5.ORDER_FILLING_IOC
    except Exception:
        pass
    return mt5.ORDER_FILLING_IOC


def send_demo_order(symbol, decision, lot, sl, tp, is_demo):
    """HARD demo gate. Never sends on a live account or when DEMO_ONLY/EXECUTE flags say no."""
    if not is_demo or not cfg.DEMO_ONLY or not cfg.EXECUTE_TRADES:
        return {"sent": False, "reason": "BLOCKED_NOT_DEMO_OR_EXEC_OFF"}
    info = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    if lot <= 0:
        return {"sent": False, "reason": "INVALID_LOT"}
    price = tick.ask if decision == "BUY" else tick.bid
    otype = mt5.ORDER_TYPE_BUY if decision == "BUY" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot),
        "type": otype, "price": float(price), "sl": float(sl), "tp": float(tp),
        "deviation": 20, "magic": cfg.MAGIC, "comment": "XAU_SCALPER_DEMO",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling_mode(info),
    }
    res = mt5.order_send(req)
    return {"sent": getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE,
            "retcode": getattr(res, "retcode", None)}


def sync_risk(rm, symbol):
    acc = mt5.account_info()
    pos = mt5.positions_get(symbol=symbol) or []
    rm.sync(acc.equity, acc.balance, sum(1 for p in pos if p.magic == cfg.MAGIC))


def run_once(rm, is_demo):
    symbol = cfg.SYMBOL
    m1 = strategy.prepare_entry(get_rates(symbol, cfg.TF_ENTRY, cfg.M1_BARS))
    m5 = get_rates(symbol, cfg.TF_TREND, cfg.M5_BARS)
    trend_dir, _ = strategy.trend_context(m5)
    info = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    spread_points = (tick.ask - tick.bid) / info.point
    now = datetime.datetime.utcnow()

    dec = strategy.analyze(m1, trend_dir, spread_points, now.hour)

    if dec["decision"] in ("BUY", "SELL"):
        ok, reason = rm.can_open(now, is_demo)
        if not ok:
            dec["decision"] = "WAIT"; dec["blocked_reason"] = reason
        else:
            sl_dist = abs(dec["entry"] - dec["stop_loss"])
            lot = rm.compute_lot(sl_dist, info.point, info.trade_tick_value, info.trade_tick_size,
                                 info.volume_min, info.volume_max, info.volume_step)
            if lot <= 0:
                dec["decision"] = "WAIT"; dec["blocked_reason"] = "RISK_TOO_SMALL_FOR_MIN_LOT"
            else:
                dec["lot_size"] = lot
                res = send_demo_order(symbol, dec["decision"], lot,
                                      dec["stop_loss"], dec["take_profit_1"], is_demo)
                dec["reasons"].append(f"order={res}")
                if res.get("sent"):
                    rm.on_open()
    return dec


def main_loop():
    acc, is_demo = connect()
    rm = risk_manager.RiskManager(acc.balance)
    print(f"[AGENT] connected DEMO={is_demo} balance={acc.balance} EXECUTE_TRADES={cfg.EXECUTE_TRADES}")
    last_bar = None
    try:
        while True:
            last = mt5.copy_rates_from_pos(cfg.SYMBOL, _tf("M1"), 0, 1)
            bar_t = last[0]['time'] if last is not None and len(last) else None
            if bar_t != last_bar:                # act once per new closed M1 bar
                last_bar = bar_t
                sync_risk(rm, cfg.SYMBOL)
                dec = run_once(rm, is_demo)
                print(json.dumps(dec, ensure_ascii=False))
                with open("last_decision.json", "w") as f:
                    json.dump(dec, f, ensure_ascii=False)
            time.sleep(2)
    except KeyboardInterrupt:
        print("[AGENT] stopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_loop()
