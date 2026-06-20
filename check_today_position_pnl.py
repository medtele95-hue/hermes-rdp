import MetaTrader5 as mt5
from datetime import datetime
from collections import defaultdict

HERMES_MAGIC = 909002

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    raise SystemExit

now = datetime.now()
start = now.replace(hour=0, minute=0, second=0, microsecond=0)

deals = mt5.history_deals_get(start, now) or []

position_open_magic = {}
position_symbol = {}
position_pnl = defaultdict(float)

for d in deals:
    x = d._asdict()
    pid = x.get("position_id")
    magic = int(x.get("magic", 0) or 0)
    entry = int(x.get("entry", -1) or -1)
    symbol = x.get("symbol")
    pnl = float(x.get("profit",0) or 0) + float(x.get("commission",0) or 0) + float(x.get("swap",0) or 0)

    if pid:
        position_symbol[pid] = symbol
        position_pnl[pid] += pnl

        # entry=0 ????? opening deal
        if entry == 0 and magic != 0:
            position_open_magic[pid] = magic

account_pnl = sum(position_pnl.values())
hermes_position_pnl = 0.0
manual_position_pnl = 0.0

print("=== TODAY POSITION PNL ===")
print("Window:", start, "->", now)

for pid, pnl in position_pnl.items():
    open_magic = position_open_magic.get(pid, 0)
    owner = "HERMES" if open_magic == HERMES_MAGIC else ("MANUAL/OTHER" if open_magic == 0 else f"OTHER_MAGIC_{open_magic}")
    if owner == "HERMES":
        hermes_position_pnl += pnl
    else:
        manual_position_pnl += pnl

    print(f"position={pid} symbol={position_symbol.get(pid)} owner={owner} pnl={pnl:.2f}")

print("\n=== SUMMARY ===")
print(f"ACCOUNT TODAY PNL        = {account_pnl:.2f}")
print(f"HERMES POSITION PNL      = {hermes_position_pnl:.2f}")
print(f"MANUAL/OTHER POSITION PNL= {manual_position_pnl:.2f}")

mt5.shutdown()
