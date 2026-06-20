import MetaTrader5 as mt5
from datetime import datetime, timedelta
from collections import defaultdict

HERMES_MAGIC = 909002

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    raise SystemExit

now = datetime.now()
start = now - timedelta(hours=24)

deals = mt5.history_deals_get(start, now) or []

positions = defaultdict(list)

for d in deals:
    x = d._asdict()
    pid = x.get("position_id")
    if not pid:
        continue
    positions[pid].append(x)

print("=== LAST 24H CLOSED POSITIONS ===")
print("Window:", start, "->", now)

account_total = 0.0
hermes_total = 0.0

for pid, ds in sorted(positions.items()):
    ds = sorted(ds, key=lambda z: z["time"])
    open_magic = 0
    symbol = ds[-1].get("symbol")
    pnl = 0.0
    open_time = None
    close_time = None
    comments = []

    for x in ds:
        profit = float(x.get("profit",0) or 0) + float(x.get("commission",0) or 0) + float(x.get("swap",0) or 0)
        pnl += profit
        comments.append(x.get("comment",""))
        entry = int(x.get("entry") if x.get("entry") is not None else -1)
        magic = int(x.get("magic") if x.get("magic") is not None else 0)

        if entry == 0:
            open_magic = magic
            open_time = datetime.fromtimestamp(x["time"]).strftime("%Y-%m-%d %H:%M:%S")
        if entry == 1:
            close_time = datetime.fromtimestamp(x["time"]).strftime("%Y-%m-%d %H:%M:%S")

    owner = "HERMES" if open_magic == HERMES_MAGIC else ("MANUAL/OTHER" if open_magic == 0 else f"OTHER_MAGIC_{open_magic}")
    account_total += pnl
    if owner == "HERMES":
        hermes_total += pnl

    print(f"{close_time} | position={pid} | {symbol} | owner={owner} | open_magic={open_magic} | pnl={pnl:.2f} | comments={comments}")

print("\n=== SUMMARY LAST 24H ===")
print(f"ACCOUNT 24H PNL = {account_total:.2f}")
print(f"HERMES 24H PNL  = {hermes_total:.2f}")

mt5.shutdown()
