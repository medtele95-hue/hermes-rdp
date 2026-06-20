import MetaTrader5 as mt5
from datetime import datetime

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    raise SystemExit

now = datetime.now()
start = now.replace(hour=0, minute=0, second=0, microsecond=0)

deals = mt5.history_deals_get(start, now) or []

print("=== TODAY DEALS DETAILS ===")
for d in deals:
    x = d._asdict()
    pnl = float(x.get("profit",0) or 0) + float(x.get("commission",0) or 0) + float(x.get("swap",0) or 0)
    print({
        "time": datetime.fromtimestamp(x["time"]).strftime("%H:%M:%S"),
        "ticket": x.get("ticket"),
        "position_id": x.get("position_id"),
        "magic": x.get("magic"),
        "symbol": x.get("symbol"),
        "type": x.get("type"),
        "entry": x.get("entry"),
        "price": x.get("price"),
        "profit": round(pnl, 2),
        "comment": x.get("comment"),
    })

mt5.shutdown()
