# =====================================================================
#  strategy.py  —  pure strategy logic (no MT5). Used by backtest + agent.
#  3 phases: trend_context() -> setup_detection() -> entry_confirmation()
# =====================================================================
import numpy as np
import pandas as pd
import config as cfg


# ---------------- indicators ----------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(c, n):
    d = c.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / dn.ewm(alpha=1/n, adjust=False).mean().replace(0, np.nan)
    return 100 - 100/(1+rs)

def atr(df, n):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def prepare_entry(m1):
    """Add M1 indicators + last-confirmed fractal swing levels (no lookahead)."""
    df = m1.copy()
    df['ema_fast'] = ema(df['close'], cfg.EMA_FAST)
    df['ema_slow'] = ema(df['close'], cfg.EMA_SLOW)
    df['rsi']      = rsi(df['close'], cfg.RSI_PERIOD)
    df['atr']      = atr(df, cfg.ATR_PERIOD)
    df['atr_lo']   = df['atr'].rolling(cfg.ATR_BAND_WINDOW).quantile(cfg.ATR_BAND[0])
    df['atr_hi']   = df['atr'].rolling(cfg.ATR_BAND_WINDOW).quantile(cfg.ATR_BAND[1])
    F = cfg.FRACTAL; H = df['high'].values; L = df['low'].values; n = len(df)
    sh = np.full(n, np.nan); sl = np.full(n, np.nan)
    for i in range(F, n-F):
        if H[i] == H[i-F:i+F+1].max(): sh[i] = H[i]
        if L[i] == L[i-F:i+F+1].min(): sl[i] = L[i]
    last_sh = np.nan; last_sl = np.nan
    SH = np.full(n, np.nan); SL = np.full(n, np.nan)
    for i in range(n):                       # swing confirmed F bars after it forms
        j = i - F
        if j >= 0 and not np.isnan(sh[j]): last_sh = sh[j]
        if j >= 0 and not np.isnan(sl[j]): last_sl = sl[j]
        SH[i] = last_sh; SL[i] = last_sl
    df['swing_high'] = SH; df['swing_low'] = SL
    return df


def trend_from_m5(m5):
    e1, e2, e3 = (ema(m5['close'], p) for p in cfg.EMA_TREND)
    up = (e1 > e2) & (e2 > e3); dn = (e1 < e2) & (e2 < e3)
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=m5.index)


# ---------------- PHASE 1: trend context (M5) ----------------
def trend_context(m5):
    t = trend_from_m5(m5)
    d = int(t.iloc[-1])
    txt = {1: "EMA20>50>200 (bull)", -1: "EMA20<50<200 (bear)", 0: "EMA mixed (flat)"}[d]
    return d, [f"M5 trend: {txt}"]


# ---------------- PHASE 2: setup detection (M1 pullback) ----------------
def setup_detection(m1, direction, i):
    """Pullback to EMA_SLOW in trend direction within SETUP_WINDOW bars (need not be current bar)."""
    if direction == 0:
        return False, []
    c = m1['close'].values; es = m1['ema_slow'].values; a = m1['atr'].values
    for k in range(max(0, i-cfg.SETUP_WINDOW), i+1):
        if not np.isnan(a[k]) and abs(c[k]-es[k]) < cfg.PULLBACK_ATR*a[k]:
            return True, ["pullback to EMA21 in setup window"]
    return False, []


# ---------------- PHASE 3: entry confirmation (M1 trigger) ----------------
def _cross(m1, i, direction):
    lb = cfg.CROSS_LOOKBACK
    if i-lb < 0: return False
    f = m1['ema_fast'].values; s = m1['ema_slow'].values
    return (f[i] > s[i] and f[i-lb] <= s[i-lb]) if direction == 1 else (f[i] < s[i] and f[i-lb] >= s[i-lb])

def entry_confirmation(m1, direction, i):
    """Trigger on current closed bar: EMA9/21 cross (recent) + close beyond EMA9 + (structure break OR sweep)."""
    f = m1['ema_fast'].values; C = m1['close'].values; H = m1['high'].values; L = m1['low'].values
    sh = m1['swing_high'].values[i]; sl = m1['swing_low'].values[i]
    cross  = _cross(m1, i, direction)
    beyond = C[i] > f[i] if direction == 1 else C[i] < f[i]
    if direction == 1:
        struct = (not np.isnan(sh)) and C[i] > sh
        sweep  = (not np.isnan(sl)) and L[i] < sl and C[i] > sl          # sweep low, close back above
    else:
        struct = (not np.isnan(sl)) and C[i] < sl
        sweep  = (not np.isnan(sh)) and H[i] > sh and C[i] < sh          # FIXED: sweep high, close back below high
    confirmed = cross and beyond and (struct or sweep)                  # structure OR sweep (not AND)
    return confirmed, dict(cross=cross, beyond=beyond, struct=struct, sweep=sweep)


# ---------------- JSON decision ----------------
def _decision(decision, confidence, entry=None, sl=None, tp1=None, tp2=None,
              reasons=None, blocked=None, lot=None):
    rr = None
    if entry is not None and sl is not None and tp1 is not None:
        risk = abs(entry-sl); rr = round(abs(tp1-entry)/risk, 2) if risk > 0 else None
    return {
        "symbol": cfg.SYMBOL, "decision": decision, "confidence": int(confidence),
        "entry": round(entry, 2) if entry is not None else None,
        "stop_loss": round(sl, 2) if sl is not None else None,
        "take_profit_1": round(tp1, 2) if tp1 is not None else None,
        "take_profit_2": round(tp2, 2) if tp2 is not None else None,
        "risk_reward": rr, "lot_size": lot,
        "reasons": reasons or [], "blocked_reason": blocked,
    }


# ---------------- orchestrator ----------------
def analyze(m1, trend_dir, spread_points, hour, i=-1, max_spread=None):
    """Run the 3 phases + scoring at bar i. trend_dir from M5 (caller). Returns JSON decision (lot filled by risk_manager)."""
    if max_spread is None: max_spread = cfg.MAX_SPREAD_POINTS
    n = len(m1); i = i if i >= 0 else n + i
    row = m1.iloc[i]
    a = row['atr']

    if any(pd.isna(x) for x in [a, row['atr_lo'], row['atr_hi'], row['swing_high'], row['swing_low'], row['rsi']]):
        return _decision("WAIT", 0, blocked="WARMUP")
    if spread_points > max_spread:
        return _decision("WAIT", 0, reasons=[f"spread {spread_points:.0f}>{max_spread}"], blocked="SPREAD_HIGH")
    if a < row['atr_lo']:
        return _decision("WAIT", 0, blocked="ATR_TOO_LOW")
    if a > row['atr_hi']:
        return _decision("WAIT", 0, blocked="ATR_TOO_HIGH")
    if trend_dir == 0:
        return _decision("WAIT", 0, reasons=["no M5 trend (flat)"])

    direction = trend_dir
    rsi_v = row['rsi']
    if direction == 1 and rsi_v > cfg.RSI_BUY_REJECT:  return _decision("WAIT", 0, blocked="RSI_OVERBOUGHT")
    if direction == -1 and rsi_v < cfg.RSI_SELL_REJECT: return _decision("WAIT", 0, blocked="RSI_OVERSOLD")

    setup_ok, _ = setup_detection(m1, direction, i)
    conf_ok, comps = entry_confirmation(m1, direction, i)

    reasons = []; score = 0
    score += cfg.W_TREND; reasons.append(f"trend M5 aligned (+{cfg.W_TREND})")
    if comps['cross']:  score += cfg.W_CROSS;  reasons.append(f"EMA9/21 cross (+{cfg.W_CROSS})")
    rsi_zone = (cfg.RSI_BUY[0] <= rsi_v <= cfg.RSI_BUY[1]) if direction == 1 else (cfg.RSI_SELL[0] <= rsi_v <= cfg.RSI_SELL[1])
    if rsi_zone:        score += cfg.W_RSI;    reasons.append(f"RSI zone (+{cfg.W_RSI})")
    score += cfg.W_ATR; reasons.append(f"ATR in band (+{cfg.W_ATR})")          # passed band rejects above
    if comps['struct']: score += cfg.W_STRUCT; reasons.append(f"structure break (+{cfg.W_STRUCT})")
    if comps['sweep']:  score += cfg.W_SWEEP;  reasons.append(f"liquidity sweep (+{cfg.W_SWEEP})")
    if spread_points <= max_spread: score += cfg.W_SPREAD
    in_sess = cfg.LONDON[0] <= hour < cfg.NEWYORK[1]
    if in_sess:         score += cfg.W_SESSION; reasons.append(f"session ok (+{cfg.W_SESSION})")

    entry = float(row['close']); sh = float(row['swing_high']); slw = float(row['swing_low'])
    if direction == 1:
        sl = slw - cfg.SL_ATR_MULT*a; risk = entry - sl
    else:
        sl = sh + cfg.SL_ATR_MULT*a; risk = sl - entry

    if setup_ok and conf_ok and score >= cfg.MIN_SCORE_TRADE and risk > 0:
        if direction == 1:
            tp1 = entry + cfg.TP1_R*risk; tp2 = entry + cfg.TP2_R*risk
        else:
            tp1 = entry - cfg.TP1_R*risk; tp2 = entry - cfg.TP2_R*risk
        dec = "BUY" if direction == 1 else "SELL"
        return _decision(dec, score, entry=entry, sl=sl, tp1=tp1, tp2=tp2, reasons=reasons)
    if score >= cfg.MIN_SCORE_WAIT:
        return _decision("WAIT", score, reasons=reasons + ["awaiting full confirmation"])
    return _decision("WAIT", score, reasons=reasons)
