# =====================================================================
#  backtest.py  —  historical backtest (MT5 data), separate from live agent
#  Run on Windows RDP with MT5. Reports the metrics from the spec.
# =====================================================================
import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

import config as cfg
import strategy
import risk_manager


def get_rates(symbol, tf_name, n):
    tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5}[tf_name]
    r = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if r is None or len(r) == 0:
        raise RuntimeError(f"No rates for {symbol} {tf_name}: {mt5.last_error()}")
    df = pd.DataFrame(r)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df


def run_backtest(symbol=cfg.SYMBOL, m1_bars=60000, m5_bars=14000, start_balance=10000.0):
    if mt5 is None:
        raise RuntimeError("MetaTrader5 not installed. Run on Windows RDP with MT5.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    m1 = get_rates(symbol, "M1", m1_bars)
    m5 = get_rates(symbol, "M5", m5_bars)
    point = mt5.symbol_info(symbol).point if mt5.symbol_info(symbol) else 0.01
    mt5.shutdown()

    m1 = strategy.prepare_entry(m1)
    # align M5 trend to M1 (use last CLOSED M5 bar -> shift(1) -> ffill)
    trend = strategy.trend_from_m5(m5).shift(1).reindex(m1.index, method='ffill').fillna(0)
    tvals = trend.values
    spread_pts = m1['spread'].values if 'spread' in m1.columns else np.zeros(len(m1))

    rm = risk_manager.RiskManager(start_balance)
    C = m1['close'].values; H = m1['high'].values; L = m1['low'].values
    idx = m1.index
    pos = None; trades = []; eq_curve = [start_balance]

    start_i = cfg.ATR_BAND_WINDOW + 5
    for i in range(start_i, len(m1)):
        now = idx[i].to_pydatetime()
        # ---- manage open position (SL/TP1) ----
        if pos is not None and i > pos['i']:
            hit = None
            if pos['dir'] == 1:
                if L[i] <= pos['sl']:   hit = pos['sl']
                elif H[i] >= pos['tp1']: hit = pos['tp1']
            else:
                if H[i] >= pos['sl']:   hit = pos['sl']
                elif L[i] <= pos['tp1']: hit = pos['tp1']
            if hit is not None:
                R = ((hit - pos['entry']) if pos['dir'] == 1 else (pos['entry'] - hit)) / pos['risk']
                R -= cfg.COST_R
                pnl = rm.balance * cfg.RISK_PCT / 100.0 * R
                rm.on_close(pnl)
                trades.append({'time': idx[i], 'R': R, 'hour': now.hour, 'pnl': pnl, 'dir': pos['dir']})
                eq_curve.append(rm.equity); pos = None
            continue
        # ---- look for new entry ----
        if pos is None:
            ok, _ = rm.can_open(now, True)
            if not ok:
                continue
            dec = strategy.analyze(m1, int(tvals[i]), float(spread_pts[i]), now.hour, i=i)
            if dec['decision'] in ('BUY', 'SELL'):
                entry = dec['entry']; sl = dec['stop_loss']; tp1 = dec['take_profit_1']
                risk = abs(entry - sl)
                if risk > 0:
                    rm.on_open()
                    pos = {'dir': 1 if dec['decision'] == 'BUY' else -1,
                           'entry': entry, 'sl': sl, 'tp1': tp1, 'risk': risk, 'i': i}

    _report(trades, eq_curve, start_balance, idx)


def _report(trades, eq_curve, start_balance, idx):
    print(f"\n=== BACKTEST {cfg.SYMBOL} M1/M5 ({len(idx)} M1 bars) ===")
    if not trades:
        print("  0 trades (filters too strict / no qualifying setups).")
        return
    df = pd.DataFrame(trades).set_index('time')
    R = df['R'].values; n = len(R)
    wins = R > 0
    pf = R[wins].sum() / (-R[~wins].sum()) if (~wins).any() else float('inf')
    eq = pd.Series(eq_curve)
    dd = ((eq.cummax() - eq) / eq.cummax()).max() * 100
    mc = 0; cur = 0
    for x in R:
        cur = cur + 1 if x < 0 else 0; mc = max(mc, cur)
    print(f"  trades={n}  win={wins.mean()*100:.1f}%  avgR={R.mean():+.3f}  totalR={R.sum():+.1f}")
    print(f"  profit_factor={pf:.2f}  max_drawdown={dd:.1f}%  max_consec_losses={mc}")
    print(f"  final_equity={eq.iloc[-1]:.0f} (start {start_balance:.0f})")
    # daily PnL
    daily = df.groupby(df.index.date)['pnl'].sum()
    print(f"  best_day={daily.max():+.0f}  worst_day={daily.min():+.0f}  avg_day={daily.mean():+.1f}")
    # by session (UTC)
    print("  by session:")
    for lab, cond in [("London", (df['hour'] >= 7) & (df['hour'] < 12)),
                      ("NewYork", (df['hour'] >= 12) & (df['hour'] < 21)),
                      ("Asian", (df['hour'] >= 21) | (df['hour'] < 7))]:
        s = df[cond]
        if len(s):
            print(f"    {lab:<8} trades={len(s):<4} totalR={s['R'].sum():+.1f} win={(s['R']>0).mean()*100:.0f}%")


if __name__ == "__main__":
    run_backtest()
