"""
48-hour setup pipeline audit tool — read-only, no trading logic changes.

Usage:
    python -m app.tools.setup_audit [--hours 48] [--out-dir reports]

Reads app/data/demo_pilot_events.jsonl and any available log snapshots.
Produces JSON, CSV, and Markdown reports.
No execution calls. No live trading. DEMO_ONLY=true enforced externally.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        _CASA_TZ: Any = ZoneInfo("Africa/Casablanca")
    except ZoneInfoNotFoundError:
        _CASA_TZ = timezone(timedelta(hours=1))
except ImportError:
    _CASA_TZ = timezone(timedelta(hours=1))

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVENTS_PATH = ROOT / "app" / "data" / "demo_pilot_events.jsonl"
DEFAULT_OUT_DIR = ROOT / "reports"

# ── Final status values ────────────────────────────────────────────────────────

FINAL_STATUSES = (
    "EXECUTED_BY_HERMES",       # DEMO_ORDER / DEMO_ROUTER_ORDER_SENT confirmed
    "MT5_SYNCED_CLOSED",        # POSITION_SYNC without matching DemoRouter send
    "SENT_TO_DEMO_ROUTER",      # Signal sent to DemoRouter (order not yet confirmed)
    "ACCEPTED_BUT_ROUTER_BLOCKED",  # SH accepted, safety pass, router blocked
    "ACCEPTED_BUT_NOT_SENT",    # SH accepted, safety pass, no router send yet
    "SAFETY_BLOCKED",
    "SETUP_HUNTER_REJECTED",
    "WAITING_CONFIRMATION",
    "NO_VALID_SETUP",
    "OBSERVE_ONLY",
    "UNKNOWN",
)

# Block-reason strings that indicate top-down data unavailability
_TOP_DOWN_MISSING_REASONS = frozenset({
    "TOP_DOWN_DATA_MISSING",
    "TOP_DOWN_SNAPSHOT_NOT_READY",
    "TOP_DOWN_READER_MISSING",
    "TOP_DOWN_READER_FAIL",
    "TOP_DOWN_WAIT_FOR_CONFIRMATION",
    "D1_RATES_MISSING",
    "H4_RATES_MISSING",
    "H1_RATES_MISSING",
    "M15_RATES_MISSING",
    "M5_RATES_MISSING",
    "M1_RATES_MISSING",
    "D1_MISSING_OR_INSUFFICIENT",
    "H4_MISSING_OR_INSUFFICIENT",
    "H1_MISSING_OR_INSUFFICIENT",
    "M15_MISSING_OR_INSUFFICIENT",
    "M5_MISSING_OR_INSUFFICIENT",
    "M1_MISSING_OR_INSUFFICIENT",
})

# Pipeline stages (ordered)
ALL_STAGES = (
    "CANDIDATE_CREATED",
    "SETUP_HUNTER_IN",
    "SETUP_HUNTER_ACCEPT",
    "SETUP_HUNTER_REJECT",
    "SAFETY_GUARD_PASS",
    "SAFETY_GUARD_BLOCK",
    "ROUTER_PASS",
    "ROUTER_BLOCK",
    "DEMO_ROUTER_ORDER_SENT",
    "MT5_POSITION_OPENED",
    "POSITION_SYNC",
    "LIVE_SNAPSHOT",
)


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _to_casa(dt: datetime) -> str:
    """Convert UTC datetime to Casablanca local time string."""
    try:
        return dt.astimezone(_CASA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _hour_label(dt: datetime) -> str:
    try:
        local = dt.astimezone(_CASA_TZ)
        return local.strftime("%Y-%m-%d %H:00 CASA")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:00 UTC")


# ── Final status determination ─────────────────────────────────────────────────

def determine_final_status(event: dict) -> str:
    etype = event.get("event_type", "")

    # POSITION_SYNC: MT5 found position, but no DemoRouter send in this log
    # → always MT5_SYNCED_CLOSED unless an explicit Hermes-send flag is set
    if etype == "POSITION_SYNC":
        return "MT5_SYNCED_CLOSED"

    # Explicit DemoRouter order send events
    if etype in ("DEMO_ORDER", "DEMO_ROUTER_ORDER_SENT"):
        return "EXECUTED_BY_HERMES"

    if etype == "DEMO_ORDER_FAILED":
        return "ACCEPTED_BUT_ROUTER_BLOCKED"

    if etype == "DEMO_SKIP":
        allowed = str(event.get("allowed_symbol_check") or "").upper()
        if allowed == "BLOCK":
            return "ACCEPTED_BUT_ROUTER_BLOCKED"
        return "SENT_TO_DEMO_ROUTER"

    if etype == "NEAR_MISS":
        return "ACCEPTED_BUT_ROUTER_BLOCKED"

    # SETUP_HUNTER events
    ec = bool(event.get("execution_candidate"))
    de = bool(event.get("demo_eligible"))
    ep = str(event.get("execution_policy") or "").upper()
    tg = str(event.get("time_gate_status") or "").upper()
    analysis_only = event.get("analysis_only_reason")

    if not ec or ep == "NONE" or ep == "":
        strat = str(event.get("strategy") or "").strip()
        if not strat or strat.upper() in ("NONE", ""):
            return "NO_VALID_SETUP"
        return "SETUP_HUNTER_REJECTED"

    if not de:
        return "SETUP_HUNTER_REJECTED"

    if tg == "BLOCK":
        return "SAFETY_BLOCKED"

    if analysis_only:
        return "OBSERVE_ONLY"

    if ep == "ANALYSIS_ONLY":
        return "OBSERVE_ONLY"

    # Accepted by SetupHunter, SafetyGuard PASS — check router
    missing = event.get("missing_confirmations") or []
    smc = str(event.get("smc_status") or "").upper()
    mtfa = str(event.get("mtfa_status") or "").upper()
    td = str(event.get("top_down_status") or "").upper()
    route_status = str(event.get("route_status") or "").upper()

    if route_status in ("SENT", "ROUTED", "EXECUTED"):
        return "EXECUTED_BY_HERMES"

    if missing:
        return "ACCEPTED_BUT_ROUTER_BLOCKED"

    if smc == "FAIL" and mtfa == "FAIL" and td == "FAIL":
        return "ACCEPTED_BUT_ROUTER_BLOCKED"

    if smc == "FAIL" or mtfa == "FAIL" or td == "FAIL":
        return "ACCEPTED_BUT_ROUTER_BLOCKED"

    return "ACCEPTED_BUT_NOT_SENT"


def extract_stages(event: dict) -> list[str]:
    """Return the pipeline stages this event passed through."""
    etype = event.get("event_type", "")
    stages: list[str] = []

    if etype == "POSITION_SYNC":
        return ["MT5_POSITION_OPENED", "POSITION_SYNC"]

    if etype in ("DEMO_ORDER", "DEMO_ROUTER_ORDER_SENT"):
        return ["CANDIDATE_CREATED", "SETUP_HUNTER_IN", "SETUP_HUNTER_ACCEPT",
                "SAFETY_GUARD_PASS", "ROUTER_PASS", "DEMO_ROUTER_ORDER_SENT",
                "MT5_POSITION_OPENED"]

    if etype == "DEMO_ORDER_FAILED":
        return ["CANDIDATE_CREATED", "SETUP_HUNTER_IN", "SETUP_HUNTER_ACCEPT",
                "SAFETY_GUARD_PASS", "ROUTER_BLOCK"]

    if etype == "DEMO_SKIP":
        allowed = str(event.get("allowed_symbol_check") or "").upper()
        base = ["CANDIDATE_CREATED", "SETUP_HUNTER_IN", "SETUP_HUNTER_ACCEPT",
                "SAFETY_GUARD_PASS"]
        if allowed == "BLOCK":
            return base + ["ROUTER_BLOCK"]
        return base + ["ROUTER_PASS", "DEMO_ROUTER_ORDER_SENT"]

    # SETUP_HUNTER
    stages = ["CANDIDATE_CREATED", "SETUP_HUNTER_IN"]
    ec = bool(event.get("execution_candidate"))
    de = bool(event.get("demo_eligible"))
    ep = str(event.get("execution_policy") or "").upper()
    tg = str(event.get("time_gate_status") or "").upper()

    if not ec or ep in ("NONE", ""):
        stages.append("SETUP_HUNTER_REJECT")
        return stages

    stages.append("SETUP_HUNTER_ACCEPT")

    if tg == "BLOCK":
        stages.append("SAFETY_GUARD_BLOCK")
        return stages

    stages.append("SAFETY_GUARD_PASS")

    # Router
    fs = determine_final_status(event)
    if fs in ("ACCEPTED_BUT_ROUTER_BLOCKED", "ROUTER_BLOCKED"):
        stages.append("ROUTER_BLOCK")
    elif fs in ("SENT_TO_DEMO_ROUTER", "EXECUTED_BY_HERMES", "EXECUTED_IN_MT5"):
        stages.append("ROUTER_PASS")
        stages.append("DEMO_ROUTER_ORDER_SENT")
    # else ACCEPTED_BUT_NOT_SENT / WAITING_CONFIRMATION — no router stage yet

    return stages


def extract_block_reason(event: dict) -> str | None:
    """Primary block reason for this event."""
    etype = event.get("event_type", "")

    if etype == "DEMO_SKIP":
        cr = event.get("cap_block_reason") or event.get("block_reason")
        if cr:
            return str(cr)
        allowed = str(event.get("allowed_symbol_check") or "").upper()
        if allowed == "BLOCK":
            return "SYMBOL_NOT_ALLOWED"
        return None

    if etype == "POSITION_SYNC":
        return event.get("close_reason")

    # SETUP_HUNTER
    ec = bool(event.get("execution_candidate"))
    ep = str(event.get("execution_policy") or "").upper()

    if not ec or ep in ("NONE", ""):
        strat = str(event.get("strategy") or "").strip()
        if not strat or strat.upper() in ("NONE", ""):
            return "NO_STRATEGY_SIGNAL"
        return "EXECUTION_CANDIDATE_FALSE"

    if not bool(event.get("demo_eligible")):
        reasons = event.get("eligibility_block_reasons") or []
        if reasons:
            return str(reasons[0])
        return "NOT_DEMO_ELIGIBLE"

    tg = str(event.get("time_gate_status") or "").upper()
    if tg == "BLOCK":
        return "TIME_GATE_BLOCK"

    # Router-level block reasons
    missing = list(event.get("missing_confirmations") or [])
    if missing:
        return missing[0]

    smc = str(event.get("smc_status") or "").upper()
    mtfa = str(event.get("mtfa_status") or "").upper()
    td = str(event.get("top_down_status") or "").upper()

    if smc == "FAIL" and mtfa == "FAIL" and td == "FAIL":
        return "FINAL_CONFLUENCE_TOO_LOW"
    if smc == "FAIL":
        return f"SMC_FAIL (score={event.get('smc_score')})"
    if mtfa == "FAIL":
        return f"MTFA_FAIL (score={event.get('mtfa_score')})"
    if td == "FAIL":
        return "TOP_DOWN_FAIL"

    return None


def extract_setup_reason(event: dict) -> str | None:
    """Why this setup was generated."""
    # Order flow reason
    of_reason = event.get("order_flow_execution_agent_reason")
    if of_reason:
        return str(of_reason)
    # Strategy-specific reason
    for field in ("gold_liquidity_reason", "eur_ema_rsi_atr_reason",
                  "btc_scalping_reason", "simo_atm_reason"):
        v = event.get(field)
        if v:
            return str(v)
    td = event.get("top_down_decision")
    if td:
        return str(td)
    return None


# ── Event parsing ──────────────────────────────────────────────────────────────

def parse_event(event: dict) -> dict:
    """Convert raw JSONL event to audit record."""
    etype = event.get("event_type", "UNKNOWN")
    ts = _parse_ts(event.get("created_at"))
    ts_utc = ts.isoformat() if ts else None
    ts_casa = _to_casa(ts) if ts else None
    hour_label = _hour_label(ts) if ts else "UNKNOWN"

    final_status = determine_final_status(event)
    stages = extract_stages(event)
    block_reason = extract_block_reason(event)
    setup_reason = extract_setup_reason(event)

    sym = str(event.get("broker_symbol") or event.get("symbol") or "UNKNOWN")
    strategy = str(event.get("strategy") or etype)
    session = str(event.get("time_session") or event.get("time_session") or "UNKNOWN")

    # For POSITION_SYNC events, extract symbol from payload if needed
    if etype == "POSITION_SYNC":
        sym = str(event.get("broker_symbol") or event.get("symbol") or "UNKNOWN")
        strategy = "POSITION_SYNC"
        session = "N/A"

    # For DEMO_SKIP events, pull confluence_grade if present
    grade = event.get("grade") or event.get("confluence_grade")

    # Top-down missing field analysis
    missing_conf = list(event.get("missing_confirmations") or [])
    top_down_reader_dict = event.get("top_down_reader") or {}
    td_missing_raw = list(
        top_down_reader_dict.get("missing_confirmations") or
        top_down_reader_dict.get("timeframe_missing_confirmations") or
        missing_conf
    )
    top_down_missing = bool(
        any(m in _TOP_DOWN_MISSING_REASONS or
            str(m).endswith("_RATES_MISSING") or
            str(m).endswith("_MISSING_OR_INSUFFICIENT")
            for m in (missing_conf + td_missing_raw))
    )
    top_down_missing_fields: list[str] = list(dict.fromkeys(
        m for m in (td_missing_raw + missing_conf)
        if m in _TOP_DOWN_MISSING_REASONS or
        str(m).endswith("_RATES_MISSING") or
        str(m).endswith("_MISSING_OR_INSUFFICIENT")
    ))

    is_executed = final_status == "EXECUTED_BY_HERMES"
    is_synced = final_status == "MT5_SYNCED_CLOSED"
    router_pass = "ROUTER_PASS" in stages or "DEMO_ROUTER_ORDER_SENT" in stages

    return {
        "event_type": etype,
        "timestamp_utc": ts_utc,
        "timestamp_casa": ts_casa,
        "hour": hour_label,
        "symbol": sym,
        "broker_symbol": event.get("broker_symbol") or sym,
        "strategy": strategy,
        "direction": event.get("direction"),
        "entry": event.get("entry"),
        "stop_loss": event.get("sl"),
        "take_profit": event.get("tp"),
        "rr": event.get("rr"),
        "score": event.get("edge_score") or event.get("setup_score"),
        "grade": grade,
        "confidence": event.get("setup_score"),
        "demo_eligible": event.get("demo_eligible"),
        "execution_candidate": event.get("execution_candidate"),
        "execution_policy": event.get("execution_policy"),
        "time_gate_status": event.get("time_gate_status"),
        "session": session,
        "smc_status": event.get("smc_status"),
        "smc_score": event.get("smc_score"),
        "mtfa_status": event.get("mtfa_status"),
        "mtfa_score": event.get("mtfa_score"),
        "top_down_status": event.get("top_down_status"),
        "missing_confirmations": missing_conf,
        "top_down_missing": top_down_missing,
        "top_down_missing_fields": top_down_missing_fields,
        "setup_reason": setup_reason,
        "rejection_reason": block_reason,
        "final_status": final_status,
        "pipeline_stages": stages,
        # Accepted-but-not-executed detail
        "sh_decision": "ACCEPT" if bool(event.get("execution_candidate")) else "REJECT",
        "safety_decision": (
            "PASS" if str(event.get("time_gate_status") or "").upper() == "PASS"
            else "BLOCK" if str(event.get("time_gate_status") or "").upper() == "BLOCK"
            else "N/A"
        ),
        "router_decision": (
            "PASS" if router_pass
            else "BLOCK" if not router_pass and "SETUP_HUNTER_ACCEPT" in stages
            else "N/A"
        ),
        "mt5_status": (
            "OPENED" if is_executed
            else "SYNCED_CLOSED" if is_synced
            else "SENT" if final_status == "SENT_TO_DEMO_ROUTER"
            else "NOT_SENT"
        ),
    }


# ── Data loading ───────────────────────────────────────────────────────────────

def load_events(
    events_path: Path = DEFAULT_EVENTS_PATH,
    hours: int = 48,
) -> list[dict]:
    """Load events from JSONL, filtered to last `hours` hours."""
    if not events_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    records: list[dict] = []
    with events_path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(event.get("created_at"))
            if ts is not None and ts < cutoff:
                continue
            records.append(event)
    return records


# ── Summary generation ─────────────────────────────────────────────────────────

def generate_summary(records: list[dict], hours: int = 48) -> dict:
    """Generate the full audit summary from parsed audit records."""
    total = len(records)
    by_status: Counter = Counter(r["final_status"] for r in records)
    by_symbol: Counter = Counter(r["symbol"] for r in records)
    by_strategy: Counter = Counter(r["strategy"] for r in records)
    by_hour: Counter = Counter(r["hour"] for r in records)
    by_session: Counter = Counter(r["session"] for r in records)

    sh_accepted = sum(1 for r in records if r["sh_decision"] == "ACCEPT")
    sh_rejected = sum(1 for r in records if r["sh_decision"] == "REJECT")
    safety_pass = sum(1 for r in records if r["safety_decision"] == "PASS")
    safety_block = sum(1 for r in records if r["safety_decision"] == "BLOCK")
    # New precise status counts
    executed_by_hermes = sum(1 for r in records if r["final_status"] == "EXECUTED_BY_HERMES")
    mt5_synced_closed = sum(1 for r in records if r["final_status"] == "MT5_SYNCED_CLOSED")
    accepted_but_blocked = sum(1 for r in records if r["final_status"] == "ACCEPTED_BUT_ROUTER_BLOCKED")
    accepted_but_not_sent = sum(1 for r in records if r["final_status"] == "ACCEPTED_BUT_NOT_SENT")
    demo_sent = sum(1 for r in records if r["final_status"] in ("SENT_TO_DEMO_ROUTER", "EXECUTED_BY_HERMES"))

    # Top-down missing analysis
    top_down_missing_count = sum(1 for r in records if r.get("top_down_missing"))
    top_missing_fields: Counter = Counter(
        field
        for r in records
        for field in (r.get("top_down_missing_fields") or [])
    )
    top_down_missing_by_symbol: Counter = Counter(
        r["symbol"] for r in records if r.get("top_down_missing")
    )

    # Block reason counts (all records with a rejection_reason)
    block_reasons: Counter = Counter(
        r["rejection_reason"] for r in records
        if r["rejection_reason"]
    )

    # Accepted by symbol/strategy
    accepted = [r for r in records if r["sh_decision"] == "ACCEPT"]
    accepted_by_symbol: Counter = Counter(r["symbol"] for r in accepted)
    accepted_by_strategy: Counter = Counter(r["strategy"] for r in accepted)

    # Accepted but not executed (not sent to router or confirmed by MT5)
    accepted_not_executed = [
        r for r in records
        if r["sh_decision"] == "ACCEPT"
        and r["final_status"] not in ("EXECUTED_BY_HERMES", "SENT_TO_DEMO_ROUTER", "MT5_SYNCED_CLOSED")
    ]

    # Confirmed executions
    executed = [r for r in records if r["final_status"] == "EXECUTED_BY_HERMES"]
    synced_closed = [r for r in records if r["final_status"] == "MT5_SYNCED_CLOSED"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "total_setups_detected": total,
        "setup_hunter_accepted": sh_accepted,
        "setup_hunter_rejected": sh_rejected,
        "safety_guard_pass": safety_pass,
        "safety_guard_blocked": safety_block,
        "demo_router_sent": demo_sent,
        "executed_by_hermes": executed_by_hermes,
        "mt5_synced_closed": mt5_synced_closed,
        "accepted_but_router_blocked": accepted_but_blocked,
        "accepted_but_not_sent": accepted_but_not_sent,
        "top_down_missing_count": top_down_missing_count,
        "top_missing_fields": dict(top_missing_fields.most_common()),
        "top_down_missing_by_symbol": dict(top_down_missing_by_symbol.most_common()),
        "by_final_status": dict(by_status.most_common()),
        "by_symbol": dict(by_symbol.most_common()),
        "by_strategy": dict(by_strategy.most_common()),
        "by_hour": dict(sorted(by_hour.items())),
        "by_session": dict(by_session.most_common()),
        "top_block_reasons": dict(block_reasons.most_common(10)),
        "top_symbols_by_accepted": dict(accepted_by_symbol.most_common(10)),
        "top_strategies_by_accepted": dict(accepted_by_strategy.most_common(10)),
        "accepted_not_executed_count": len(accepted_not_executed),
        "accepted_not_executed": accepted_not_executed,
        "executed_records": executed,
        "synced_closed_records": synced_closed,
        "all_records": records,
    }


# ── Export functions ───────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "timestamp_utc", "timestamp_casa", "hour", "symbol", "broker_symbol",
    "strategy", "direction", "entry", "stop_loss", "take_profit", "rr",
    "score", "grade", "confidence", "demo_eligible",
    "session", "smc_status", "smc_score", "mtfa_status", "mtfa_score",
    "top_down_status", "top_down_missing", "top_down_missing_fields",
    "setup_reason", "rejection_reason", "final_status",
    "sh_decision", "safety_decision", "router_decision", "mt5_status",
    "event_type",
]


def export_json(summary: dict, out_dir: Path, tag: str = "last_48h") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"setup_audit_{tag}.json"
    # Remove large list from JSON export — keep all_records separately
    export = {k: v for k, v in summary.items() if k != "all_records"}
    path.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")
    return path


def export_csv(summary: dict, out_dir: Path, tag: str = "last_48h") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"setup_audit_{tag}.csv"
    records = summary.get("all_records", [])
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = dict(r)
            row["missing_confirmations"] = "; ".join(row.get("missing_confirmations") or [])
            row["top_down_missing_fields"] = "; ".join(row.get("top_down_missing_fields") or [])
            writer.writerow(row)
    return path


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{100.0 * n / total:.1f}%"


def export_markdown(summary: dict, out_dir: Path, tag: str = "last_48h") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"setup_audit_summary_{tag}.md"
    h = summary["hours"]
    total = summary["total_setups_detected"]
    acc = summary["setup_hunter_accepted"]
    rej = summary["setup_hunter_rejected"]
    sp = summary["safety_guard_pass"]
    sb = summary["safety_guard_blocked"]
    ds = summary["demo_router_sent"]
    ebh = summary["executed_by_hermes"]
    msc = summary["mt5_synced_closed"]
    abr = summary["accepted_but_router_blocked"]
    abns = summary["accepted_but_not_sent"]
    tdm = summary["top_down_missing_count"]
    ane = summary["accepted_not_executed_count"]

    lines = [
        f"# HERMES 48H Setup Audit Report",
        f"",
        f"**Generated:** {summary['generated_at']}  ",
        f"**Window:** Last {h} hours  ",
        f"",
        f"## Summary Counts",
        f"",
        f"| Metric | Count | % of Total |",
        f"|--------|------:|------------|",
        f"| Total Setups Detected | {total} | 100% |",
        f"| SetupHunter Accepted | {acc} | {_pct(acc, total)} |",
        f"| SetupHunter Rejected | {rej} | {_pct(rej, total)} |",
        f"| SafetyGuard Pass | {sp} | {_pct(sp, total)} |",
        f"| SafetyGuard Blocked | {sb} | {_pct(sb, total)} |",
        f"| DemoRouter Sent | {ds} | {_pct(ds, total)} |",
        f"| Executed by Hermes (confirmed) | {ebh} | {_pct(ebh, total)} |",
        f"| MT5 Synced Closed (no DemoRouter match) | {msc} | {_pct(msc, total)} |",
        f"| Accepted But Router Blocked | {abr} | {_pct(abr, acc) if acc else '0%'} of accepted |",
        f"| Accepted But Not Sent | {abns} | {_pct(abns, acc) if acc else '0%'} of accepted |",
        f"| Top-Down Data Missing | {tdm} | {_pct(tdm, total)} |",
        f"| Accepted But Not Executed | {ane} | {_pct(ane, acc) if acc else '0%'} of accepted |",
        f"",
        f"## Final Status Breakdown",
        f"",
        f"| Final Status | Count |",
        f"|-------------|------:|",
    ]
    for status, cnt in summary["by_final_status"].items():
        lines.append(f"| {status} | {cnt} |")

    lines += [
        f"",
        f"## Top-Down Missing Fields",
        f"",
        f"| Missing Field | Count |",
        f"|--------------|------:|",
    ]
    if summary["top_missing_fields"]:
        for field, cnt in list(summary["top_missing_fields"].items())[:10]:
            lines.append(f"| {field} | {cnt} |")
    else:
        lines.append("| *(none)* | 0 |")

    lines += [
        f"",
        f"## Top Symbols with Top-Down Missing",
        f"",
        f"| Symbol | Missing Count |",
        f"|--------|-------------:|",
    ]
    if summary["top_down_missing_by_symbol"]:
        for sym, cnt in list(summary["top_down_missing_by_symbol"].items())[:10]:
            lines.append(f"| {sym} | {cnt} |")
    else:
        lines.append("| *(none)* | 0 |")

    lines += [
        f"",
        f"## Top 10 Block Reasons",
        f"",
        f"| Reason | Count |",
        f"|--------|------:|",
    ]
    for reason, cnt in list(summary["top_block_reasons"].items())[:10]:
        lines.append(f"| {reason} | {cnt} |")

    lines += [
        f"",
        f"## Top Symbols by Accepted Setups",
        f"",
        f"| Symbol | Accepted Count |",
        f"|--------|---------------:|",
    ]
    for sym, cnt in list(summary["top_symbols_by_accepted"].items())[:10]:
        lines.append(f"| {sym} | {cnt} |")

    lines += [
        f"",
        f"## Top Strategies by Accepted Setups",
        f"",
        f"| Strategy | Accepted Count |",
        f"|----------|---------------:|",
    ]
    for strat, cnt in list(summary["top_strategies_by_accepted"].items())[:10]:
        lines.append(f"| {strat} | {cnt} |")

    lines += [
        f"",
        f"## Accepted But Not Executed ({ane} setups)",
        f"",
        f"| Timestamp CASA | Symbol | Strategy | Direction | Grade | Score | "
        f"SetupHunter | SafetyGuard | Router | MT5 | Block Reason |",
        f"|----------------|--------|----------|-----------|-------|-------|"
        f"------------|-------------|--------|-----|--------------|",
    ]
    for r in summary["accepted_not_executed"][:50]:
        lines.append(
            f"| {r.get('timestamp_casa','?')} "
            f"| {r.get('symbol','?')} "
            f"| {r.get('strategy','?')} "
            f"| {r.get('direction','?')} "
            f"| {r.get('grade','?')} "
            f"| {r.get('score','?')} "
            f"| {r.get('sh_decision','?')} "
            f"| {r.get('safety_decision','?')} "
            f"| {r.get('router_decision','BLOCK')} "
            f"| {r.get('mt5_status','NOT_SENT')} "
            f"| {r.get('rejection_reason','?')} |"
        )

    lines += [
        f"",
        f"## By Session",
        f"",
        f"| Session | Count |",
        f"|---------|------:|",
    ]
    for sess, cnt in summary["by_session"].items():
        lines.append(f"| {sess} | {cnt} |")

    lines += [
        f"",
        f"---",
        f"*Read-only audit. No trading logic was changed. "
        f"ALLOW_LIVE_TRADING=false, DEMO_ONLY=true.*",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Console output ─────────────────────────────────────────────────────────────

def print_console_summary(summary: dict) -> None:
    h = summary["hours"]
    total = summary["total_setups_detected"]
    print(f"\n{'='*60}")
    print(f"  HERMES SETUP AUDIT - LAST {h} HOURS")
    print(f"  Generated: {summary['generated_at']}")
    print(f"{'='*60}")
    print(f"\n  SUMMARY COUNTS")
    print(f"  {'-'*40}")
    acc = summary["setup_hunter_accepted"]
    rows = [
        ("Total setups detected",          total,                                    ""),
        ("SetupHunter ACCEPTED",           acc,                                      _pct(acc, total)),
        ("SetupHunter REJECTED",           summary["setup_hunter_rejected"],         _pct(summary["setup_hunter_rejected"], total)),
        ("SafetyGuard PASS",               summary["safety_guard_pass"],             _pct(summary["safety_guard_pass"], total)),
        ("SafetyGuard BLOCKED",            summary["safety_guard_blocked"],          _pct(summary["safety_guard_blocked"], total)),
        ("DemoRouter Sent",                summary["demo_router_sent"],              _pct(summary["demo_router_sent"], total)),
        ("Executed by Hermes",             summary["executed_by_hermes"],            _pct(summary["executed_by_hermes"], total)),
        ("MT5 Synced Closed",              summary["mt5_synced_closed"],             _pct(summary["mt5_synced_closed"], total)),
        ("Accepted But Router Blocked",    summary["accepted_but_router_blocked"],   _pct(summary["accepted_but_router_blocked"], acc) if acc else ""),
        ("Accepted But Not Sent",          summary["accepted_but_not_sent"],         _pct(summary["accepted_but_not_sent"], acc) if acc else ""),
        ("Top-Down Data Missing",          summary["top_down_missing_count"],        _pct(summary["top_down_missing_count"], total)),
        ("Accepted Not Executed",          summary["accepted_not_executed_count"],   ""),
    ]
    for label, count, pct in rows:
        pct_str = f"  {pct}" if pct else ""
        print(f"  {label:<30} {count:>6}{pct_str}")

    print(f"\n  FINAL STATUS BREAKDOWN")
    print(f"  {'-'*40}")
    for status, cnt in summary["by_final_status"].items():
        print(f"  {status:<30} {cnt:>6}  {_pct(cnt, total)}")

    print(f"\n  TOP-DOWN MISSING FIELDS")
    print(f"  {'-'*40}")
    if summary["top_missing_fields"]:
        for field, cnt in list(summary["top_missing_fields"].items())[:10]:
            print(f"  {str(field):<40} {cnt:>6}")
    else:
        print(f"  (none)")

    print(f"\n  TOP SYMBOLS WITH TOP-DOWN MISSING")
    print(f"  {'-'*40}")
    if summary["top_down_missing_by_symbol"]:
        for sym, cnt in list(summary["top_down_missing_by_symbol"].items())[:10]:
            print(f"  {sym:<20} {cnt:>6}")
    else:
        print(f"  (none)")

    print(f"\n  TOP BLOCK REASONS")
    print(f"  {'-'*40}")
    for reason, cnt in list(summary["top_block_reasons"].items())[:10]:
        print(f"  {str(reason):<40} {cnt:>6}")

    print(f"\n  TOP SYMBOLS (by accepted setups)")
    print(f"  {'-'*40}")
    for sym, cnt in list(summary["top_symbols_by_accepted"].items())[:10]:
        print(f"  {sym:<20} {cnt:>6}")

    print(f"\n  TOP STRATEGIES (by accepted setups)")
    print(f"  {'-'*40}")
    for strat, cnt in list(summary["top_strategies_by_accepted"].items())[:10]:
        print(f"  {strat:<40} {cnt:>6}")

    ane = summary["accepted_not_executed"]
    if ane:
        print(f"\n  ACCEPTED BUT NOT EXECUTED ({len(ane)} setups)")
        print(f"  {'-'*40}")
        hdr = f"  {'CASA TIME':<20} {'SYMBOL':<12} {'STRAT':<25} {'DIR':<5} {'GRD':<4} {'SCR':<5} {'BLOCK REASON'}"
        print(hdr)
        for r in ane[:20]:
            print(
                f"  {str(r.get('timestamp_casa','?')):<20} "
                f"{str(r.get('symbol','?')):<12} "
                f"{str(r.get('strategy','?')):<25} "
                f"{str(r.get('direction','?')):<5} "
                f"{str(r.get('grade','?')):<4} "
                f"{str(r.get('score','?')):<5} "
                f"{str(r.get('rejection_reason','?'))}"
            )
        if len(ane) > 20:
            print(f"  ... and {len(ane)-20} more (see CSV/JSON reports)")

    print(f"\n{'='*60}\n")


# ── Main entry point ───────────────────────────────────────────────────────────

def run_audit(
    hours: int = 48,
    events_path: Path = DEFAULT_EVENTS_PATH,
    out_dir: Path = DEFAULT_OUT_DIR,
    quiet: bool = False,
) -> dict:
    """Run the full setup audit and return the summary dict."""
    raw_events = load_events(events_path, hours=hours)
    records = [parse_event(e) for e in raw_events]
    summary = generate_summary(records, hours=hours)

    tag = f"last_{hours}h"
    json_path = export_json(summary, out_dir, tag)
    csv_path = export_csv(summary, out_dir, tag)
    md_path = export_markdown(summary, out_dir, tag)

    if not quiet:
        print_console_summary(summary)
        print(f"Reports written to:")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")
        print(f"  MD:   {md_path}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HERMES 48-hour setup audit — read-only analysis of pipeline stages."
    )
    parser.add_argument("--hours", type=int, default=48, help="Lookback window in hours (default: 48)")
    parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS_PATH,
                        help="Path to demo_pilot_events.jsonl")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="Output directory for reports")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output")
    args = parser.parse_args()
    run_audit(
        hours=args.hours,
        events_path=args.events_path,
        out_dir=args.out_dir,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
