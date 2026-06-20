import MetaTrader5 as mt5
from datetime import datetime
from collections import defaultdict

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    raise SystemExit

now = datetime.now()
start = now.replace(hour=0, minute=0, second=0, microsecond=0)

deals = mt5.history_deals_get(start, now)
if deals is None:
    print("No deals / MT5 error:", mt5.last_error())
    mt5.shutdown()
    raise SystemExit

by_magic = defaultdict(float)
by_symbol = defaultdict(float)

print("Window:", start, "->", now)
print("Deals today:", len(deals))

for d in deals:
    x = d._asdict()
    pnl = float(x.get("profit", 0) or 0) + float(x.get("commission", 0) or 0) + float(x.get("swap", 0) or 0)
    magic = int(x.get("magic", 0) or 0)
    symbol = x.get("symbol", "")
    by_magic[magic] += pnl
    by_symbol[symbol] += pnl

print("\n=== PNL BY MAGIC ===")
for magic, pnl in sorted(by_magic.items()):
    label = "HERMES" if magic == 909002 else ("MANUAL" if magic == 0 else "OTHER_ROBOT")
    print(f"MAGIC {magic} ({label}) = {pnl:.2f}")

print("\n=== PNL BY SYMBOL ===")
for symbol, pnl in sorted(by_symbol.items()):
    print(f"{symbol} = {pnl:.2f}")

mt5.shutdown()
