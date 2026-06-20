"""CLI: trace the origin of a demo trade by ticket number.

Usage:
    python -m app.tools.trace_trade_origin --ticket <ticket>

Reads demo_pilot_events.jsonl and prints all events matching the ticket
or the trace_id / setup_id associated with that ticket.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _jsonl_lines(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def trace_ticket(ticket: int, events_path: Path) -> None:
    ticket_str = str(ticket)
    matched_trace_ids: set[str] = set()

    # First pass: find all events that mention this ticket and collect trace_ids
    all_events = list(_jsonl_lines(events_path))
    for ev in all_events:
        data = ev if isinstance(ev, dict) else ev.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if (
            str(data.get("ticket") or "") == ticket_str
            or str(ev.get("ticket") or "") == ticket_str
        ):
            for field in ("trace_id", "setup_id"):
                val = str(data.get(field) or ev.get(field) or "")
                if val:
                    matched_trace_ids.add(val)

    print(f"\n=== Trade origin trace for ticket={ticket} ===")
    print(f"Events file: {events_path}")
    print(f"Trace IDs found: {matched_trace_ids or 'none'}\n")

    hits = 0
    for ev in all_events:
        data = ev if isinstance(ev, dict) else (ev.get("data") or {})
        if not isinstance(data, dict):
            data = {}

        ticket_match = (
            str(data.get("ticket") or "") == ticket_str
            or str(ev.get("ticket") or "") == ticket_str
        )
        trace_match = False
        for field in ("trace_id", "setup_id"):
            val = str(data.get(field) or ev.get(field) or "")
            if val and val in matched_trace_ids:
                trace_match = True
                break

        if ticket_match or trace_match:
            hits += 1
            event_type = ev.get("event_type") or data.get("event_type") or ev.get("type") or "?"
            created_at = data.get("created_at") or ev.get("created_at") or ev.get("timestamp") or ""
            strategy = data.get("strategy") or ev.get("strategy") or ""
            mode = data.get("old_btc_mode") or data.get("mode") or ev.get("mode") or ""
            direction = data.get("direction") or ev.get("direction") or ""
            trace_id = data.get("trace_id") or ev.get("trace_id") or data.get("setup_id") or ev.get("setup_id") or ""
            print(
                f"[{hits:03d}] {event_type:<28} ts={created_at[:19] if created_at else 'N/A'}"
                f"  strategy={strategy or 'N/A'}  mode={mode or 'N/A'}"
                f"  dir={direction or 'N/A'}  trace={trace_id or 'N/A'}"
            )

    if hits == 0:
        print(f"No events found for ticket={ticket}.")
    else:
        print(f"\nTotal matching events: {hits}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace demo trade origin by ticket")
    parser.add_argument("--ticket", type=int, required=True, help="MT5 ticket number")
    parser.add_argument(
        "--events-file",
        default=None,
        help="Path to demo_pilot_events.jsonl (auto-detected if not given)",
    )
    args = parser.parse_args()

    if args.events_file:
        events_path = Path(args.events_file)
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [
            root / "demo_pilot_events.jsonl",
            root / "app" / "demo_pilot_events.jsonl",
            Path("demo_pilot_events.jsonl"),
        ]
        events_path = next((p for p in candidates if p.exists()), candidates[0])

    trace_ticket(args.ticket, events_path)


if __name__ == "__main__":
    main()
