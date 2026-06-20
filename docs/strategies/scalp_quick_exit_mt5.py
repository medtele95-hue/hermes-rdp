"""
scalp_quick_exit_mt5.py
=======================================================================
SCALP QUICK EXIT — clean trade manager for MT5 (DEMO).
Ndabber l'KHROJ dyal صفقات MAFTOU7A (ma kanftah walo, ma kanmodifich entry logic).
  1) TP sghir   -> ykhtef l'rib7 (default +1.5$ a 0.01 lot).
  2) BREAKEVEN  -> melli rib7 ywsel +lock$ , SL -> entry (+buffer) => ma tb9ach tkhsar.
  3) TRAILING   -> melli rib7 ykber , SL kaytbe3 si3r (yqfel l'rib7).
Safety: DEMO ONLY hard gate. NO new position opened here. order_send GHIR
        l modify SL/TP (machi dakhla). dashboard read-only.
ma kaybeddelch l'live safety dyalek.
=======================================================================
"""
from dataclasses import dataclass

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


# ----------------------- config -----------------------
@dataclass
class ExitConfig:
    tp_usd: float = 1.5         # rib7 l'hadaf -> ykhtef w ykhroj
    lock_usd: float = 0.80      # melli rib7 ywsel hada -> SL l'breakeven
    be_buffer_usd: float = 0.10 # breakeven chwiya fou9 entry (yghatti spread)
    trail_start_usd: float = 1.0  # men hna kaybda trailing
    trail_gap_usd: float = 0.60   # masafa dyal trailing (rib7 m2amman = peak - gap)
    only_magic: int = 0         # 0 = kul l'positions ; wla 7ddد b magic dyal robot dakhla
    demo_only: bool = True


# ----------------- value per price unit ----------------
def _dollar_per_price(symbol):
    """$ value of a 1.0 price move for the position's volume is computed per-position below;
    here we return value-per-(price*lot) factor from symbol_info (tick_value/tick_size)."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    if info.trade_tick_size and info.trade_tick_size > 0:
        return info.trade_tick_value / info.trade_tick_size   # per 1.0 lot, per 1.0 price
    return info.trade_contract_size or 1.0


def _price_for_usd(symbol, volume, usd):
    """Price distance that equals `usd` profit for `volume` lots."""
    vpp = _dollar_per_price(symbol)
    if not vpp or volume <= 0:
        return None
    return usd / (vpp * volume)


# ----------------- core: manage one position ----------------
def _modify_sl_tp(symbol, ticket, sl, tp):
    """The ONLY order call here: modify SL/TP of an EXISTING position. Never opens a trade."""
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": float(sl),
        "tp": float(tp),
    }
    res = mt5.order_send(req)
    return getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE


def manage_position(pos, cfg: ExitConfig, state: dict):
    """
    pos: an MT5 position object (positions_get()).
    state: per-ticket memory {ticket: {"peak_usd": float, "be_done": bool}}.
    Returns a dict describing the action taken (read-only friendly / for logs).
    """
    symbol = pos.symbol
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"ticket": pos.ticket, "action": "skip", "reason": "no_tick"}

    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    cur_price = tick.bid if is_buy else tick.ask          # exit side
    direction = 1 if is_buy else -1
    vpp = _dollar_per_price(symbol)
    if not vpp:
        return {"ticket": pos.ticket, "action": "skip", "reason": "no_symbol_info"}

    profit_usd = (cur_price - pos.price_open) * direction * vpp * pos.volume
    st = state.setdefault(pos.ticket, {"peak_usd": profit_usd, "be_done": False})
    st["peak_usd"] = max(st["peak_usd"], profit_usd)

    # 1) TP HIT -> close (grab the small profit)
    tp_dist = _price_for_usd(symbol, pos.volume, cfg.tp_usd)
    if tp_dist is not None and profit_usd >= cfg.tp_usd:
        ok = _close_position(pos)
        return {"ticket": pos.ticket, "action": "CLOSE_TP", "profit_usd": round(profit_usd, 2), "ok": ok}

    # current SL/TP
    point = mt5.symbol_info(symbol).point
    digits = mt5.symbol_info(symbol).digits
    new_sl = pos.sl
    action = "hold"

    # 2) BREAKEVEN
    if not st["be_done"] and profit_usd >= cfg.lock_usd:
        be_dist = _price_for_usd(symbol, pos.volume, cfg.be_buffer_usd) or 0.0
        be_price = pos.price_open + direction * be_dist
        if (is_buy and (pos.sl == 0 or be_price > pos.sl)) or ((not is_buy) and (pos.sl == 0 or be_price < pos.sl)):
            new_sl = be_price; st["be_done"] = True; action = "MOVE_BREAKEVEN"

    # 3) TRAILING (after trail_start, lock peak - gap)
    if st["peak_usd"] >= cfg.trail_start_usd:
        lock_usd = st["peak_usd"] - cfg.trail_gap_usd
        if lock_usd > cfg.be_buffer_usd:
            lock_dist = _price_for_usd(symbol, pos.volume, lock_usd)
            if lock_dist is not None:
                trail_price = pos.price_open + direction * lock_dist
                if (is_buy and trail_price > (new_sl or 0)) or ((not is_buy) and (new_sl == 0 or trail_price < new_sl)):
                    new_sl = trail_price; action = "TRAIL_SL"

    if action in ("MOVE_BREAKEVEN", "TRAIL_SL") and new_sl and new_sl != pos.sl:
        ok = _modify_sl_tp(symbol, pos.ticket, round(new_sl, digits), pos.tp)
        return {"ticket": pos.ticket, "action": action, "new_sl": round(new_sl, digits),
                "profit_usd": round(profit_usd, 2), "peak_usd": round(st["peak_usd"], 2), "ok": ok}

    return {"ticket": pos.ticket, "action": action, "profit_usd": round(profit_usd, 2),
            "peak_usd": round(st["peak_usd"], 2)}


def _close_position(pos):
    symbol = pos.symbol
    tick = mt5.symbol_info_tick(symbol)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "position": pos.ticket,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 20,
        "comment": "scalp_quick_exit",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    return getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE


# ----------------------- live loop -----------------------
def run(cfg: ExitConfig = ExitConfig(), poll_sec: float = 1.0):
    import time
    if mt5 is None:
        raise RuntimeError("MetaTrader5 not installed. Run on Windows RDP with MT5.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    acc = mt5.account_info()
    is_demo = acc and acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    if cfg.demo_only and not is_demo:
        mt5.shutdown()
        raise RuntimeError("SAFETY STOP: not a demo account and demo_only=True. Refusing to run.")
    print(f"[QUICK_EXIT] running on DEMO={is_demo}. Manages OPEN positions only (no new entries).")
    state = {}
    try:
        while True:
            positions = mt5.positions_get() or []
            for pos in positions:
                if cfg.only_magic and pos.magic != cfg.only_magic:
                    continue
                res = manage_position(pos, cfg, state)
                if res["action"] != "hold":
                    print(f"[QUICK_EXIT] {res}")
            # drop memory of closed tickets
            live = {p.ticket for p in positions}
            for t in list(state):
                if t not in live:
                    state.pop(t, None)
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("[QUICK_EXIT] stopped.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run()
