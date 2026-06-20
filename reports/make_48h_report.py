import MetaTrader5 as mt5
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import csv
import os
import math

HERMES_MAGIC = 909002
HOURS = 48

def money(x):
    try:
        return float(x or 0.0)
    except Exception:
        return 0.0

def fmt_money(x):
    return f"{x:+.2f}"

def deal_dict(d):
    return d._asdict() if hasattr(d, "_asdict") else dict(d)

def side_from_type(t):
    # MT5: 0 BUY, 1 SELL
    if t == 0:
        return "BUY"
    if t == 1:
        return "SELL"
    return str(t)

def summarize(rows):
    closed = [r for r in rows if r["is_closed"]]
    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] < 0]
    breakeven = [r for r in closed if abs(r["pnl"]) < 1e-9]
    total_pnl = sum(r["pnl"] for r in closed)
    gross_profit = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else None
    wr = (len(wins) / len(closed) * 100) if closed else 0
    return {
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "be": len(breakeven),
        "pnl": total_pnl,
        "winrate": wr,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "pf": pf,
    }

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    raise SystemExit

acc = mt5.account_info()
now = datetime.now()
start = now - timedelta(hours=HOURS)

deals = mt5.history_deals_get(start, now)
if deals is None:
    print("No deals / MT5 error:", mt5.last_error())
    mt5.shutdown()
    raise SystemExit

deals = [deal_dict(d) for d in deals]
positions = defaultdict(list)

for x in deals:
    pid = x.get("position_id")
    if pid:
        positions[pid].append(x)

rows = []

for pid, ds in positions.items():
    ds = sorted(ds, key=lambda z: z.get("time", 0))

    open_deal = None
    close_deals = []
    all_magic = set()
    comments = []

    for x in ds:
        entry = int(x.get("entry") if x.get("entry") is not None else -1)
        magic = int(x.get("magic") if x.get("magic") is not None else 0)
        all_magic.add(magic)
        comments.append(str(x.get("comment") or ""))

        if entry == 0 and open_deal is None:
            open_deal = x
        if entry == 1:
            close_deals.append(x)

    first = open_deal or ds[0]
    last = close_deals[-1] if close_deals else ds[-1]

    pnl = sum(
        money(x.get("profit")) + money(x.get("commission")) + money(x.get("swap"))
        for x in ds
    )

    open_magic = int(first.get("magic") if first.get("magic") is not None else 0)
    is_hermes = (open_magic == HERMES_MAGIC) or (HERMES_MAGIC in all_magic and open_deal is None)

    owner = "HERMES" if is_hermes else ("MANUAL" if open_magic == 0 else f"OTHER_MAGIC_{open_magic}")

    open_time = datetime.fromtimestamp(first["time"]).strftime("%Y-%m-%d %H:%M:%S") if first.get("time") else ""
    close_time = datetime.fromtimestamp(last["time"]).strftime("%Y-%m-%d %H:%M:%S") if last.get("time") else ""

    side = side_from_type(int(first.get("type") if first.get("type") is not None else -1))
    symbol = first.get("symbol") or last.get("symbol") or ""
    volume = money(first.get("volume"))
    open_price = money(first.get("price"))
    close_price = money(last.get("price"))

    is_closed = len(close_deals) > 0

    rows.append({
        "position_id": pid,
        "owner": owner,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "open_magic": open_magic,
        "all_magic": ",".join(str(m) for m in sorted(all_magic)),
        "open_time": open_time,
        "close_time": close_time,
        "open_price": open_price,
        "close_price": close_price,
        "pnl": pnl,
        "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE"),
        "is_closed": is_closed,
        "comments": " | ".join([c for c in comments if c])[:300],
    })

rows = sorted(rows, key=lambda r: r["close_time"] or r["open_time"])

hermes_rows = [r for r in rows if r["owner"] == "HERMES"]
account_stats = summarize(rows)
hermes_stats = summarize(hermes_rows)

by_symbol = defaultdict(list)
by_side = defaultdict(list)
by_owner = defaultdict(list)
by_hour = defaultdict(list)

for r in rows:
    by_symbol[r["symbol"]].append(r)
    by_side[r["side"]].append(r)
    by_owner[r["owner"]].append(r)
    hour = (r["close_time"] or r["open_time"])[11:13] if (r["close_time"] or r["open_time"]) else "UNKNOWN"
    by_hour[hour].append(r)

csv_path = os.path.join("reports", "hermes_48h_positions.csv")
txt_path = os.path.join("reports", "HERMES_48H_REPORT.txt")

with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "position_id","owner","symbol","side","volume","open_magic","all_magic",
        "open_time","close_time","open_price","close_price","pnl","result","is_closed","comments"
    ])
    writer.writeheader()
    writer.writerows(rows)

with open(txt_path, "w", encoding="utf-8") as f:
    f.write("HERMES 48H MT5 REPORT\n")
    f.write("=" * 70 + "\n")
    f.write(f"Generated: {now}\n")
    f.write(f"Window: {start} -> {now}\n")
    f.write(f"Account: {getattr(acc, 'login', None)} | Server: {getattr(acc, 'server', None)} | Trade mode: {getattr(acc, 'trade_mode', None)}\n")
    f.write(f"HERMES_MAGIC: {HERMES_MAGIC}\n")
    f.write("\n")

    f.write("ACCOUNT SUMMARY — ALL POSITIONS\n")
    f.write("-" * 70 + "\n")
    f.write(f"Closed positions: {account_stats['closed']}\n")
    f.write(f"Wins/Losses/BE: {account_stats['wins']} / {account_stats['losses']} / {account_stats['be']}\n")
    f.write(f"Win rate: {account_stats['winrate']:.2f}%\n")
    f.write(f"PnL: {fmt_money(account_stats['pnl'])}\n")
    f.write(f"Gross profit: {fmt_money(account_stats['gross_profit'])}\n")
    f.write(f"Gross loss: -{account_stats['gross_loss']:.2f}\n")
    f.write(f"Profit factor: {account_stats['pf']:.2f}\n" if account_stats['pf'] is not None else "Profit factor: INF / N/A\n")
    f.write("\n")

    f.write("HERMES SUMMARY — MAGIC 909002 BY POSITION\n")
    f.write("-" * 70 + "\n")
    f.write(f"Closed positions: {hermes_stats['closed']}\n")
    f.write(f"Wins/Losses/BE: {hermes_stats['wins']} / {hermes_stats['losses']} / {hermes_stats['be']}\n")
    f.write(f"Win rate: {hermes_stats['winrate']:.2f}%\n")
    f.write(f"PnL: {fmt_money(hermes_stats['pnl'])}\n")
    f.write(f"Gross profit: {fmt_money(hermes_stats['gross_profit'])}\n")
    f.write(f"Gross loss: -{hermes_stats['gross_loss']:.2f}\n")
    f.write(f"Profit factor: {hermes_stats['pf']:.2f}\n" if hermes_stats['pf'] is not None else "Profit factor: INF / N/A\n")
    f.write("\n")

    f.write("BY OWNER\n")
    f.write("-" * 70 + "\n")
    for k, v in sorted(by_owner.items()):
        s = summarize(v)
        f.write(f"{k}: trades={s['closed']} pnl={fmt_money(s['pnl'])} winrate={s['winrate']:.2f}% pf={s['pf'] if s['pf'] is not None else 'N/A'}\n")
    f.write("\n")

    f.write("BY SYMBOL\n")
    f.write("-" * 70 + "\n")
    for k, v in sorted(by_symbol.items()):
        s = summarize(v)
        f.write(f"{k}: trades={s['closed']} pnl={fmt_money(s['pnl'])} winrate={s['winrate']:.2f}% wins={s['wins']} losses={s['losses']}\n")
    f.write("\n")

    f.write("BY SIDE\n")
    f.write("-" * 70 + "\n")
    for k, v in sorted(by_side.items()):
        s = summarize(v)
        f.write(f"{k}: trades={s['closed']} pnl={fmt_money(s['pnl'])} winrate={s['winrate']:.2f}% wins={s['wins']} losses={s['losses']}\n")
    f.write("\n")

    f.write("BY CLOSE HOUR\n")
    f.write("-" * 70 + "\n")
    for k, v in sorted(by_hour.items()):
        s = summarize(v)
        f.write(f"{k}:00 trades={s['closed']} pnl={fmt_money(s['pnl'])} winrate={s['winrate']:.2f}%\n")
    f.write("\n")

    f.write("LAST 25 POSITIONS\n")
    f.write("-" * 70 + "\n")
    for r in rows[-25:]:
        f.write(
            f"{r['close_time'] or r['open_time']} | {r['owner']} | {r['symbol']} | {r['side']} | "
            f"lot={r['volume']} | pnl={fmt_money(r['pnl'])} | pos={r['position_id']} | magic={r['all_magic']} | {r['comments']}\n"
        )

print("OK")
print("TXT:", txt_path)
print("CSV:", csv_path)

mt5.shutdown()
