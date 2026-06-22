from __future__ import annotations

from app.config import Settings
from app.logger import log, utc_now_iso
from app.services.dashboard_snapshot import (
    compact_dashboard_status_row,
    dashboard_snapshot,
    dashboard_status_debug_line,
    dashboard_status_row,
    live_snapshot_debug_line,
)
from app.services.ingest_client import IngestClient
from app.utils.throttle import log_event_throttled


def _enrich_account_snapshot(
    account_snapshot: dict,
    latest_position_sync: dict,
) -> dict:
    """Merge PnL fields from position_sync into account_snapshot before writing."""
    enriched = dict(account_snapshot)
    closed_pnl = latest_position_sync.get("demo_closed_pnl_today")
    floating_pnl = latest_position_sync.get("demo_floating_pnl")
    total_pnl = latest_position_sync.get("demo_total_pnl_today")
    pnl_source = latest_position_sync.get("pnl_source")
    open_count = latest_position_sync.get("hermes_mt5_open_positions_count") or latest_position_sync.get("open_demo_trades_count")

    def _f(v: object) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    has_sync = bool(latest_position_sync)
    _closed = _f(closed_pnl) if closed_pnl is not None else (0.0 if has_sync else None)
    _floating = _f(floating_pnl) if floating_pnl is not None else (0.0 if has_sync else None)
    _total = _f(total_pnl) if total_pnl is not None else (0.0 if has_sync else None)
    _open = open_count

    if closed_pnl is None:
        log.info("[SNAPSHOT_FIELD_MISSING] field=closed_pnl reason=not_available_from_position_sync")
    if floating_pnl is None:
        log.info("[SNAPSHOT_FIELD_MISSING] field=floating_pnl reason=not_available_from_position_sync")
    if total_pnl is None:
        log.info("[SNAPSHOT_FIELD_MISSING] field=total reason=not_available_from_position_sync")

    enriched["open"] = _open
    enriched["closed_pnl"] = _closed
    enriched["floating_pnl"] = _floating
    enriched["total"] = _total
    enriched["daily_pnl"] = _closed
    enriched["total_pnl"] = _total
    enriched["source"] = pnl_source
    enriched.setdefault("utc_time", account_snapshot.get("snapshot_time") or utc_now_iso())
    return enriched


class HeartbeatService:
    def __init__(self, settings: Settings, ingest_client: IngestClient) -> None:
        self.settings = settings
        self.ingest_client = ingest_client
        self._last_live_snapshot_emit_state: tuple | None = None
        self._last_live_snapshot_log_state: tuple | None = None

    def write(
        self,
        account_snapshot: dict | None,
        resolved_symbols: dict[str, str],
        latest_agent_state: dict | None = None,
        latest_demo_event: dict | None = None,
        mt5_connected: bool = False,
        latest_setup_hunter: dict | None = None,
        latest_position_sync: dict | None = None,
        latest_order_flow_snapshots: dict | None = None,
        cycle_status: dict | None = None,
        per_symbol_state: dict | None = None,
        latest_candidates: list | None = None,
        latest_safety_guard: dict | None = None,
        ingest_health: dict | None = None,
    ) -> None:
        now = utc_now_iso()
        dashboard_payload = dashboard_snapshot(
            self.settings,
            account_snapshot,
            self.ingest_client.latest_time_snapshot,
            latest_demo_event,
            mt5_connected,
            setup_hunter=latest_setup_hunter,
            latest_position_sync=latest_position_sync,
            order_flow_snapshots=latest_order_flow_snapshots,
            cycle_status=cycle_status,
            per_symbol_state=per_symbol_state,
            latest_candidates=latest_candidates,
            latest_safety_guard=latest_safety_guard,
            ingest_health=ingest_health,
        )
        status = {
            "bot_name": "HERMES_5MIN_AGENT",
            "component": "hermes_core",
            "status": "RUNNING",
            "mode": "READ_ONLY" if self.settings.read_only else "LIVE_DISABLED",
            "read_only": self.settings.read_only,
            "paper_trading": self.settings.paper_trading,
            "demo_trading": self.settings.demo_trading,
            "allow_live_trading": self.settings.allow_live_trading,
            "magic_number": self.settings.hermes_magic_number,
            "symbols": list(resolved_symbols.keys()),
            "updated_at": now,
        }
        agent = {
            "name": "HERMES_5MIN_AGENT",
            "display_name": "MT5 x HERMES 5-MIN AI TRADING AGENT",
            "status": "RUNNING",
            "mode": "READ_ONLY" if self.settings.read_only else "LIVE_DISABLED",
            "magic_number": self.settings.hermes_magic_number,
            "symbols": list(resolved_symbols.keys()),
            "updated_at": now,
            "last_update": now,
        }
        if latest_agent_state:
            agent.update(latest_agent_state)
        results = [
            self.ingest_client.send_row("bot_status", status),
            self.ingest_client.send_row("hermes_agents", agent),
            self.write_dashboard_status(dashboard_payload),
        ]
        if account_snapshot:
            # Always enrich so zero values from position_sync are preserved (not converted to None).
            enriched = _enrich_account_snapshot(account_snapshot, latest_position_sync if latest_position_sync is not None else {})
            snapshot_result = self.ingest_client.send_row("account_snapshots", enriched)
            if not snapshot_result.get("ok") and "snapshot_time" in enriched:
                fallback = dict(enriched)
                fallback.pop("snapshot_time", None)
                snapshot_result = self.ingest_client.send_row("account_snapshots", fallback)
            results.append(snapshot_result)
            # Emit LIVE_SNAPSHOT structured token
            try:
                pos = latest_position_sync or {}
                snapshot_state = (
                    enriched.get("open"),
                    enriched.get("closed_pnl") if enriched.get("closed_pnl") is not None else 0.0,
                    enriched.get("floating_pnl") if enriched.get("floating_pnl") is not None else 0.0,
                )
                if snapshot_state != self._last_live_snapshot_emit_state:
                    self._last_live_snapshot_emit_state = snapshot_state
                    self.ingest_client.emit_bot_log(
                        "LIVE_SNAPSHOT",
                        f"open={snapshot_state[0]} closed_pnl={snapshot_state[1]} "
                        f"floating_pnl={snapshot_state[2]} total={enriched.get('total') or 0.0} "
                        f"source={enriched.get('source') or 'UNKNOWN'}",
                        {
                        "open": enriched.get("open"),
                        "closed_pnl": enriched.get("closed_pnl") if enriched.get("closed_pnl") is not None else 0.0,
                        "floating_pnl": enriched.get("floating_pnl") if enriched.get("floating_pnl") is not None else 0.0,
                        "total": enriched.get("total") if enriched.get("total") is not None else 0.0,
                        "daily_pnl": enriched.get("daily_pnl") if enriched.get("daily_pnl") is not None else 0.0,
                        "total_pnl": enriched.get("total_pnl") if enriched.get("total_pnl") is not None else 0.0,
                        "source": enriched.get("source"),
                        "utc_time": enriched.get("utc_time") or now,
                        },
                    )
            except Exception as exc:
                log.warning("[LIVE_SNAPSHOT] emit failed reason=%s", exc)
        if all(result.get("ok") for result in results):
            log.info("Heartbeat written")

    def emit_ingest_health(self, ingest_health: dict | None) -> None:
        if not isinstance(ingest_health, dict):
            return
        status = ingest_health.get("status", "UNKNOWN")
        self.ingest_client.emit_bot_log(
            "LOVABLE_INGEST_HEALTH",
            f"status={status} reason={ingest_health.get('reason')}",
            {
                "status": status,
                "reason": ingest_health.get("reason"),
                "last_update_utc": ingest_health.get("last_update_utc"),
            },
        )

    def write_dashboard_status(self, payload: dict) -> dict:
        log_event_throttled(
            "DASHBOARD_STATUS",
            dashboard_status_debug_line(payload),
            state=(payload.get("mode"), payload.get("account_type"), payload.get("demo_pilot_enabled"), payload.get("allow_live_trading")),
        )
        snapshot_state = (
            payload.get("open_demo_trades_count") or 0,
            payload.get("demo_closed_pnl_today") or 0.0,
            payload.get("demo_floating_pnl") or 0.0,
        )
        if snapshot_state != self._last_live_snapshot_log_state:
            self._last_live_snapshot_log_state = snapshot_state
            log.info(live_snapshot_debug_line(payload))
        variants = [
            dashboard_status_row(payload),
            compact_dashboard_status_row(payload, json_field="raw_payload"),
            compact_dashboard_status_row(payload, json_field="payload"),
            compact_dashboard_status_row(payload, json_field="status_json"),
        ]
        last_result: dict | None = None
        for row in variants:
            result = self.ingest_client.update_row(
                "bot_status",
                {"bot_name": "HERMES_5MIN_AGENT", "component": "dashboard_status"},
                row,
            )
            if result.get("ok"):
                return result
            if result.get("not_found"):
                result = self.ingest_client.send_row("bot_status", row)
                if result.get("ok"):
                    return result
            last_result = result
        return last_result or {"ok": False, "table": "bot_status", "error": "dashboard_status_write_failed"}
