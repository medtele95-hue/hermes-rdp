"""
========================================================================
  ORDER FLOW STRATEGY BOT  —  MetaTrader 5
========================================================================
  Applique la stratégie Order Flow (du fichier strategie) sur MT5 :
    - Volume Profile (POC / VAH / VAL)
    - CVD / Delta (proxy calculé depuis les ticks)
    - VWAP de session
    - Détection de divergence Prix vs CVD
    - Stack de confirmation + gestion du risque (% du capital)

------------------------------------------------------------------------
  ⚠️  À LIRE ABSOLUMENT
------------------------------------------------------------------------
  1) ÉDUCATIF — ce n'est PAS un conseil financier. Le trading comporte
     un risque élevé de perte. Teste TOUJOURS sur compte DÉMO d'abord.

  2) ORDER FLOW SUR FOREX MT5 = APPROXIMATION.
     Le forex retail n'a pas de carnet d'ordres centralisé ni de vrai
     footprint. Le "volume" de MT5 est du TICK VOLUME. Ce bot calcule
     donc un PROXY du delta via la "tick rule" (uptick = achat,
     downtick = vente). C'est fiable sur Gold / indices / futures CFD,
     beaucoup moins sur les paires forex pures. Le footprint RÉEL
     nécessite des futures (NQ, ES, CL...) avec data de la bourse.

  3) SÉCURITÉ — par défaut DRY_RUN = True : le bot ANALYSE et AFFICHE
     les signaux sans passer aucun ordre. Pour trader réellement il faut
     mettre DRY_RUN = False ET ALLOW_LIVE_TRADING = True (à tes risques).

------------------------------------------------------------------------
  INSTALLATION
------------------------------------------------------------------------
    pip install MetaTrader5 pandas numpy

    - MetaTrader 5 doit être INSTALLÉ et OUVERT sur la machine.
    - Le package MetaTrader5 est officiellement Windows uniquement.
    - Active "Algo Trading" / "AutoTrading" dans le terminal MT5.
    - Active le symbole dans le Market Watch (ex: XAUUSD).
========================================================================
"""

import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERREUR] Package manquant. Lance:  pip install MetaTrader5")
    sys.exit(1)


# ======================================================================
#  CONFIGURATION  —  modifie ici
# ======================================================================
class Config:
    # --- Connexion -----------------------------------------------------
    # Laisse LOGIN = None si MT5 est déjà ouvert et connecté à un compte.
    LOGIN = None                 # ex: 12345678
    PASSWORD = ""
    SERVER = ""                  # ex: "ICMarketsSC-Demo"

    # --- Marché --------------------------------------------------------
    SYMBOL = "XAUUSD"
    TIMEFRAME = mt5.TIMEFRAME_M5
    BARS = 400                   # bougies pour Volume Profile / structure
    CVD_BARS = 120               # bougies récentes pour le calcul du delta (ticks)

    # --- Volume Profile ------------------------------------------------
    VP_BINS = 60                 # nombre de niveaux de prix
    VALUE_AREA_PCT = 0.70        # 70% du volume = Value Area

    # --- Détection des signaux -----------------------------------------
    DIVERGENCE_LOOKBACK = 24     # bougies pour chercher la divergence
    SWING_LOOKBACK = 12          # bougies pour le swing high/low (stop)
    LEVEL_PROXIMITY_PCT = 0.0018 # proximité à une zone-clé (0.18% du prix)
    MIN_CONFIRMATIONS = 3        # confirmations minimum pour entrer

    # --- Risk management ----------------------------------------------
    RISK_PCT = 1.0               # % du capital risqué par trade
    RR_RATIO = 2.0               # Risk : Reward (target = 2x le risque)
    SL_BUFFER_PCT = 0.0008       # marge ajoutée au-delà du swing pour le stop

    # --- Sécurité (NE PAS NÉGLIGER) -----------------------------------
    DRY_RUN = True               # True = analyse seulement, AUCUN ordre
    ALLOW_LIVE_TRADING = False   # doit être True pour trader en compte RÉEL
    MAX_OPEN_POSITIONS = 1       # 1 position max sur ce symbole à la fois

    # --- Exécution -----------------------------------------------------
    MAGIC = 20250108
    DEVIATION = 20               # slippage autorisé (points)
    LOOP = True                  # boucle en continu (False = un seul passage)
    SLEEP_SECONDS = 5            # pause entre les checks


# Conversion timeframe -> secondes (pour grouper les ticks par bougie)
TF_SECONDS = {
    mt5.TIMEFRAME_M1: 60, mt5.TIMEFRAME_M5: 300, mt5.TIMEFRAME_M15: 900,
    mt5.TIMEFRAME_M30: 1800, mt5.TIMEFRAME_H1: 3600, mt5.TIMEFRAME_H4: 14400,
    mt5.TIMEFRAME_D1: 86400,
}


# ======================================================================
#  CONNEXION
# ======================================================================
def connect(cfg):
    if cfg.LOGIN:
        ok = mt5.initialize(login=cfg.LOGIN, password=cfg.PASSWORD, server=cfg.SERVER)
    else:
        ok = mt5.initialize()
    if not ok:
        print(f"[ERREUR] initialize() a échoué : {mt5.last_error()}")
        return False

    acc = mt5.account_info()
    if acc is None:
        print(f"[ERREUR] Pas de compte connecté : {mt5.last_error()}")
        return False

    modes = {0: "DÉMO", 1: "CONCOURS", 2: "RÉEL"}
    mode = modes.get(acc.trade_mode, "INCONNU")
    print("=" * 60)
    print(f"  Connecté  |  #{acc.login}  |  {acc.server}")
    print(f"  Type de compte : {mode}")
    print(f"  Balance : {acc.balance:.2f} {acc.currency}   "
          f"Equity : {acc.equity:.2f} {acc.currency}")
    print("=" * 60)

    # S'assurer que le symbole est dispo
    if mt5.symbol_info(cfg.SYMBOL) is None:
        print(f"[ERREUR] Symbole {cfg.SYMBOL} introuvable.")
        return False
    if not mt5.symbol_info(cfg.SYMBOL).visible:
        mt5.symbol_select(cfg.SYMBOL, True)

    # Garde-fou compte réel
    if not cfg.DRY_RUN and acc.trade_mode == 2 and not cfg.ALLOW_LIVE_TRADING:
        print("\n[BLOQUÉ] Compte RÉEL détecté mais ALLOW_LIVE_TRADING = False.")
        print("         Passage forcé en DRY_RUN (analyse seulement).\n")
        cfg.DRY_RUN = True
    return True


# ======================================================================
#  DONNÉES
# ======================================================================
def get_rates(cfg):
    """Récupère les bougies et renvoie un DataFrame."""
    rates = mt5.copy_rates_from_pos(cfg.SYMBOL, cfg.TIMEFRAME, 0, cfg.BARS)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["epoch"] = df["time"].astype("int64")          # epoch (s) = open de la bougie
    df["dt"] = pd.to_datetime(df["time"], unit="s")
    return df


def compute_delta_cvd(df, cfg):
    """Calcule un PROXY du delta par bougie via les ticks, puis le CVD.
       (tick rule : uptick = achat agressif, downtick = vente agressive)"""
    df["delta"] = 0.0
    tf_sec = TF_SECONDS.get(cfg.TIMEFRAME, 300)

    # On ne récupère les ticks que sur les CVD_BARS dernières bougies
    n = min(cfg.CVD_BARS, len(df))
    start_dt = df["dt"].iloc[-n].to_pydatetime()
    # +2h pour éviter de tronquer les ticks récents (décalage fuseau serveur)
    end_dt = datetime.now() + timedelta(hours=2)

    ticks = mt5.copy_ticks_range(cfg.SYMBOL, start_dt, end_dt, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) == 0:
        print("[INFO] Pas de ticks disponibles -> CVD non calculé (proxy = 0).")
        df["cvd"] = 0.0
        return df

    t = pd.DataFrame(ticks)
    for col in ("last", "volume", "volume_real"):
        if col not in t.columns:
            t[col] = 0.0

    # Prix de référence : 'last' si dispo (futures), sinon le mid (forex)
    t["mid"] = (t["bid"] + t["ask"]) / 2.0
    t["pref"] = np.where(t["last"] > 0, t["last"], t["mid"])

    # Direction (tick rule), les ticks neutres héritent du précédent
    raw = np.sign(t["pref"].diff().fillna(0))
    direction = pd.Series(raw).replace(0, np.nan).ffill().fillna(0).values

    # Volume du tick : real -> tick -> 1 par défaut
    vol = np.where(t["volume_real"] > 0, t["volume_real"],
          np.where(t["volume"] > 0, t["volume"], 1.0))

    t["buy_vol"] = np.where(direction > 0, vol, 0.0)
    t["sell_vol"] = np.where(direction < 0, vol, 0.0)

    # Rattacher chaque tick à sa bougie (epoch arrondi au timeframe)
    t["bar_epoch"] = (t["time"].astype("int64") // tf_sec) * tf_sec
    grp = t.groupby("bar_epoch").agg(buy=("buy_vol", "sum"),
                                     sell=("sell_vol", "sum"))
    delta_map = (grp["buy"] - grp["sell"])

    df["delta"] = df["epoch"].map(delta_map).fillna(0.0)
    df["cvd"] = df["delta"].cumsum()
    return df


def compute_vwap(df):
    """VWAP de session (reset chaque jour)."""
    typ = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["tick_volume"].replace(0, 1)
    day = df["dt"].dt.date
    pv = (typ * vol).groupby(day).cumsum()
    cv = vol.groupby(day).cumsum()
    df["vwap"] = pv / cv
    return df


def volume_profile(df, cfg):
    """Volume Profile -> POC, VAH, VAL. Le volume de chaque bougie est
       réparti sur les bins couverts par son range high-low."""
    lo, hi = df["low"].min(), df["high"].max()
    if hi <= lo:
        return None
    bins = np.linspace(lo, hi, cfg.VP_BINS + 1)
    centers = (bins[:-1] + bins[1:]) / 2.0
    profile = np.zeros(cfg.VP_BINS)

    for _, r in df.iterrows():
        b_lo = np.searchsorted(bins, r["low"], side="right") - 1
        b_hi = np.searchsorted(bins, r["high"], side="right") - 1
        b_lo = max(0, min(b_lo, cfg.VP_BINS - 1))
        b_hi = max(0, min(b_hi, cfg.VP_BINS - 1))
        span = b_hi - b_lo + 1
        profile[b_lo:b_hi + 1] += r["tick_volume"] / span

    poc_idx = int(np.argmax(profile))
    poc = centers[poc_idx]

    # Value Area : on étend autour du POC jusqu'à 70% du volume total
    total = profile.sum()
    target = total * cfg.VALUE_AREA_PCT
    covered = profile[poc_idx]
    lo_i = hi_i = poc_idx
    while covered < target and (lo_i > 0 or hi_i < cfg.VP_BINS - 1):
        down = profile[lo_i - 1] if lo_i > 0 else -1
        up = profile[hi_i + 1] if hi_i < cfg.VP_BINS - 1 else -1
        if up >= down:
            hi_i += 1
            covered += profile[hi_i]
        else:
            lo_i -= 1
            covered += profile[lo_i]

    return {"poc": poc, "vah": centers[hi_i], "val": centers[lo_i]}


# ======================================================================
#  LOGIQUE DE SIGNAL
# ======================================================================
def detect_divergence(df, cfg):
    """Divergence Prix vs CVD sur les dernières bougies (heuristique 2 moitiés)."""
    rec = df.tail(cfg.DIVERGENCE_LOOKBACK)
    if len(rec) < 6 or "cvd" not in rec:
        return None
    h = len(rec) // 2
    a, b = rec.iloc[:h], rec.iloc[h:]

    # Bullish : prix fait un plus-bas, CVD fait un plus-haut
    if b["low"].min() < a["low"].min() and b["cvd"].min() > a["cvd"].min():
        return "bull"
    # Bearish : prix fait un plus-haut, CVD fait un plus-bas
    if b["high"].max() > a["high"].max() and b["cvd"].max() < a["cvd"].max():
        return "bear"
    return None


def near_levels(price, levels, cfg):
    """Renvoie la liste des zones-clés dont le prix est proche."""
    prox = price * cfg.LEVEL_PROXIMITY_PCT
    hits = []
    for name, lvl in levels.items():
        if lvl is not None and abs(price - lvl) <= prox:
            hits.append(name)
    return hits


def generate_signal(df, levels, cfg):
    """Construit un signal à partir du stack de confirmation."""
    price = df["close"].iloc[-1]
    div = detect_divergence(df, cfg)
    if div is None:
        return None

    side = "buy" if div == "bull" else "sell"
    confs = [f"divergence_{div}"]

    # 1) Localisation à une zone-clé (OBLIGATOIRE : pas de zone, pas de trade)
    hits = near_levels(price, levels, cfg)
    if not hits:
        return None
    confs.append("zone:" + "+".join(hits))

    # 2) Pente du CVD alignée
    if "cvd" in df and len(df) > 6:
        slope = df["cvd"].iloc[-1] - df["cvd"].iloc[-6]
        if (side == "buy" and slope > 0) or (side == "sell" and slope < 0):
            confs.append("cvd_aligned")

    # 3) Position vs VWAP
    vwap = levels.get("vwap")
    if vwap is not None:
        if (side == "buy" and price > vwap) or (side == "sell" and price < vwap):
            confs.append("vwap_side")

    # 4) Delta de la dernière bougie dans le sens du trade
    last_delta = df["delta"].iloc[-1]
    if (side == "buy" and last_delta > 0) or (side == "sell" and last_delta < 0):
        confs.append("delta_confirm")

    if len(confs) < cfg.MIN_CONFIRMATIONS:
        return None

    # Stop = swing récent + buffer ; Target = RR x risque
    swing = df.tail(cfg.SWING_LOOKBACK)
    buf = price * cfg.SL_BUFFER_PCT
    if side == "buy":
        sl = swing["low"].min() - buf
        tp = price + cfg.RR_RATIO * (price - sl)
    else:
        sl = swing["high"].max() + buf
        tp = price - cfg.RR_RATIO * (sl - price)

    return {"side": side, "entry": price, "sl": sl, "tp": tp,
            "confirmations": confs}


# ======================================================================
#  EXÉCUTION DES ORDRES
# ======================================================================
def calc_lot(cfg, entry, sl):
    """Taille de lot calculée sur le % de risque du capital."""
    info = mt5.symbol_info(cfg.SYMBOL)
    acc = mt5.account_info()
    risk_amount = acc.balance * (cfg.RISK_PCT / 100.0)

    tick_val = info.trade_tick_value
    tick_size = info.trade_tick_size
    if tick_size == 0 or tick_val == 0:
        return info.volume_min

    loss_per_lot = (abs(entry - sl) / tick_size) * tick_val
    if loss_per_lot <= 0:
        return info.volume_min

    lot = risk_amount / loss_per_lot
    # Arrondir au pas, borner min/max
    step = info.volume_step
    lot = max(info.volume_min, min(info.volume_max, round(lot / step) * step))
    return round(lot, 2)


def pick_filling(symbol):
    fm = mt5.symbol_info(symbol).filling_mode
    if fm & 1:        # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    if fm & 2:        # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def count_positions(cfg):
    pos = mt5.positions_get(symbol=cfg.SYMBOL)
    if pos is None:
        return 0
    return sum(1 for p in pos if p.magic == cfg.MAGIC)


def place_order(cfg, sig):
    tick = mt5.symbol_info_tick(cfg.SYMBOL)
    digits = mt5.symbol_info(cfg.SYMBOL).digits
    if sig["side"] == "buy":
        otype, price = mt5.ORDER_TYPE_BUY, tick.ask
    else:
        otype, price = mt5.ORDER_TYPE_SELL, tick.bid

    lot = calc_lot(cfg, price, sig["sl"])
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": cfg.SYMBOL,
        "volume": lot,
        "type": otype,
        "price": price,
        "sl": round(sig["sl"], digits),
        "tp": round(sig["tp"], digits),
        "deviation": cfg.DEVIATION,
        "magic": cfg.MAGIC,
        "comment": "OrderFlowBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": pick_filling(cfg.SYMBOL),
    }
    result = mt5.order_send(request)
    if result is None:
        print(f"[ORDRE] échec : {mt5.last_error()}")
        return
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[ORDRE ✅] {sig['side'].upper()} {lot} {cfg.SYMBOL} @ {price} "
              f"| SL {request['sl']} | TP {request['tp']}")
    else:
        print(f"[ORDRE ⚠️] retcode={result.retcode} : {result.comment}")


# ======================================================================
#  BOUCLE PRINCIPALE
# ======================================================================
def analyze_once(cfg):
    df = get_rates(cfg)
    if df is None or len(df) < 50:
        print("[INFO] Données insuffisantes.")
        return

    df = compute_delta_cvd(df, cfg)
    df = compute_vwap(df)
    vp = volume_profile(df, cfg)

    levels = {"vwap": df["vwap"].iloc[-1]}
    if vp:
        levels.update(vp)

    price = df["close"].iloc[-1]
    lvl_str = "  ".join(f"{k.upper()}={v:.2f}" for k, v in levels.items()
                        if v is not None)
    print(f"\n[{df['dt'].iloc[-1]}] {cfg.SYMBOL} @ {price:.2f}")
    print(f"   {lvl_str}")
    print(f"   CVD={df['cvd'].iloc[-1]:.0f}  Delta(last)={df['delta'].iloc[-1]:.0f}")

    sig = generate_signal(df, levels, cfg)
    if not sig:
        print("   → Aucun signal valide (attente d'un setup + confluences).")
        return

    print(f"   ★ SIGNAL {sig['side'].upper()}  "
          f"entry={sig['entry']:.2f}  SL={sig['sl']:.2f}  TP={sig['tp']:.2f}")
    print(f"     Confirmations: {', '.join(sig['confirmations'])}")

    if cfg.DRY_RUN:
        print("     [DRY_RUN] aucun ordre envoyé.")
        return
    if count_positions(cfg) >= cfg.MAX_OPEN_POSITIONS:
        print("     Position déjà ouverte -> on n'empile pas.")
        return
    place_order(cfg, sig)


def run(cfg):
    if not connect(cfg):
        return
    if cfg.DRY_RUN:
        print(">>> MODE DRY_RUN : analyse seulement, aucun ordre réel.\n")
    else:
        print(">>> ⚠️  TRADING ACTIF : des ordres réels peuvent être envoyés.\n")

    last_epoch = None
    try:
        while True:
            df = get_rates(cfg)
            if df is not None:
                cur = df["epoch"].iloc[-1]
                # On analyse à chaque nouvelle bougie
                if cur != last_epoch:
                    last_epoch = cur
                    analyze_once(cfg)
            if not cfg.LOOP:
                break
            time.sleep(cfg.SLEEP_SECONDS)
    except KeyboardInterrupt:
        print("\n[STOP] Arrêt manuel.")
    finally:
        mt5.shutdown()
        print("[MT5] Déconnecté.")


if __name__ == "__main__":
    run(Config)
