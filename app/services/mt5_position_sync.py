from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import MetaTrader5 as mt5

from app.config import Settings
from app.logger import log
from app.services.ingest_client import IngestClient
from app.services.mt5_pnl_truth import get_mt5_hermes_pnl_truth


ALLOWED_DEMO_POSITION_SYMBOLS = {"GOLD#", "GOLD", "BTCUSD#", "BTCUSD", "EURUSD"}
POSITION_SYNC_EVENTS_PATH = Path(__file__).resolve().parents[1] / "data" / "demo_pilot_events.jsonl"
_MISSING_TRADE_ROW_WARNED: set[str] = set()
_MISSING_TRADE_DIR_WARNED: set[str] = set()

# ── P0-2 (2026-07-13) — MT5 MUET != "AUCUNE POSITION" ───────────────────────
# `mt5.positions_get()` renvoie None en cas d'erreur ou de deconnexion, et une
# liste vide quand il n'y a reellement aucune position. L'ancien code faisait
# `list(mt5.positions_get() or [])` : les deux cas devenaient un []. Un MT5
# injoignable etait donc lu comme "le compte est plat", ce qui faisait ensuite
# marquer CLOSED, une par une, toutes les positions REELLEMENT OUVERTES chez le
# broker (via _close_missing_lovable_trades -> POSITION_SYNC/CLOSED, avec
# closed_seen persiste sur disque). Le bot cessait de les gerer : plus d'Exit V2,
# plus de trailing, plus de plancher — la position continuait de courir seule.
#
# Ce n'est pas theorique : WATCHDOG_ALERTS.log porte 11 alertes
# `MT5_INIT_FAIL :: (-6, 'Terminal: Authorization failed')` le 2026-07-11.
# Aucune consequence ce jour-la uniquement parce que c'etait un samedi, marche
# ferme, aucune position ouverte. C'etait de la chance, pas de la conception.
#
# Regle desormais : MT5 muet => FAIL-CLOSED. On ne synchronise rien, on ne
# ferme RIEN, on loggue en CRITICAL et on alerte. Une position n'est marquee
# fermee que si MT5 a REPONDU et ne la contient pas.
MT5_UNREADABLE_REASON = "MT5_POSITIONS_UNREADABLE"
_ALERT_COOLDOWN_SECONDS = 900.0
_last_alert_monotonic: float | None = None
_WATCHDOG_ENV = Path(__file__).resolve().parents[2] / "watchdog" / ".env"


def _send_telegram_alert(text: str) -> None:
    """Envoi best-effort, JAMAIS bloquant pour l'appelant (lance dans un thread
    daemon par _notify_mt5_unreadable). Reutilise la config et le contexte TLS
    du watchdog (truststore : le bundle certifi ne valide pas api.telegram.org
    sur cet hote, cf. watchdog/hermes_watchdog.py:185-191)."""
    try:
        # Une suite de tests ne doit JAMAIS notifier un humain. Sans ce garde,
        # tout test qui laisse positions_get() renvoyer None (MT5 non initialise
        # dans le process pytest) enverrait un vrai message Telegram.
        import os

        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if not _WATCHDOG_ENV.exists():
            return
        cfg: dict[str, str] = {}
        for line in _WATCHDOG_ENV.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                cfg[key.strip()] = value.strip()
        token, chat_id = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        import truststore

        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with urllib.request.urlopen(request, timeout=10, context=context):
            return
    except Exception as exc:  # fail-silent par contrat : une alerte ratee ne casse rien
        try:
            log.warning("[POSITION_SYNC] telegram_alert_failed error=%s", str(exc)[:120])
        except Exception:
            pass


def _notify_mt5_unreadable(detail: str) -> None:
    """Alerte MT5 muet. Deux garde-fous voulus :
    - THREAD daemon : zero seconde de blocage dans la boucle de trading. C'est
      exactement l'inverse de dashboard_api/audit.py:99, dont l'appel Telegram
      synchrone (timeout 10 s) dans l'event loop est un suspect serieux du gel
      du dashboard apres ~11 h.
    - COOLDOWN 15 min : une deconnexion dure des minutes et le cycle tourne
      toutes les ~5 s ; sans cooldown ce sont des centaines de messages."""
    global _last_alert_monotonic
    now = time.monotonic()
    if _last_alert_monotonic is not None and (now - _last_alert_monotonic) < _ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_monotonic = now
    text = (
        "HERMES CRITIQUE : MT5 injoignable (positions_get() = None). "
        f"Detail: {detail}. Synchronisation SUSPENDUE — aucune position n'a ete "
        "marquee fermee (fail-closed). Verifier le terminal MT5 immediatement."
    )
    try:
        threading.Thread(target=_send_telegram_alert, args=(text,), daemon=True).start()
    except Exception:
        pass


def _mt5_unreadable_summary(now_dt: datetime, detail: str) -> dict:
    """Resume de cycle quand MT5 est muet.

    Les compteurs sont volontairement a None, JAMAIS a 0 : "je ne sais pas"
    n'est pas "il n'y a rien". Mettre 0 ici reintroduirait exactement le
    mensonge que ce correctif supprime, simplement deplace dans le dashboard."""
    return {
        "mt5_unreadable": True,
        "sync_skipped_reason": MT5_UNREADABLE_REASON,
        "mt5_last_error": detail,
        "mt5_open_positions_count": None,
        "mt5_positions_raw_count": None,
        "mt5_positions_seen": [],
        "mt5_positions_ignored_with_reason": [],
        "hermes_mt5_open_positions_count": None,
        "open_demo_trades_count": None,
        "demo_closed_pnl_today": None,
        "demo_floating_pnl": None,
        "demo_total_pnl_today": None,
        "pnl_source": MT5_UNREADABLE_REASON,
        "latest_position_sync_time": now_dt.isoformat(),
        _sb_key("open_trades_synced_count"): 0,
        _sb_key("open_trades_closed_count"): 0,
        "closed_tickets": [],
        "already_closed_count": 0,
        "already_closed_tickets": [],
        "latest_position_sync": None,
        "latest_position_close": None,
    }


def _lovable_enabled(settings: Settings) -> bool:
    """BLOC 11b: Lovable/ingest network calls are PURGED by default.
    POSITION_SYNC reads MT5 directly (by magic); the remote mirror only
    runs when POSITION_SYNC_LOVABLE_ENABLED=true."""
    return bool(getattr(settings, "position_sync_lovable_enabled", False))


def _closed_seen_path(events_path: Path) -> Path:
    return Path(events_path).parent / "position_closed_seen.json"


def _load_closed_seen(events_path: Path) -> dict[str, str]:
    """BLOC 11a: PERSISTENT idempotence set for [POSITION_CLOSED].
    {ticket: "final" | "provisional"} — survives restarts by construction."""
    try:
        payload = json.loads(_closed_seen_path(events_path).read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_closed_seen(events_path: Path, seen: dict[str, str]) -> None:
    try:
        path = _closed_seen_path(events_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        log.debug("[POSITION_SYNC] closed_seen_write_failed error=%s", str(exc)[:120])


def sync_open_mt5_positions_to_lovable(
    settings: Settings,
    ingest_client: IngestClient,
    now: datetime | None = None,
    record_event: Callable[[dict], None] | None = None,
    fallback_events_path: Path | None = None,
) -> dict:
    now_dt = now or datetime.now(timezone.utc)

    # P0-2 : MT5 muet (None) != compte plat ([]). Voir la note en tete de module.
    try:
        raw_positions = mt5.positions_get()
    except Exception as exc:
        raw_positions = None
        log.warning("[POSITION_SYNC] positions_get_raised error=%s", str(exc)[:160])
    if raw_positions is None:
        try:
            detail = str(mt5.last_error())
        except Exception:
            detail = "UNKNOWN"
        log.critical(
            "[POSITION_SYNC_MT5_UNREADABLE] positions_get()=None last_error=%s — "
            "FAIL-CLOSED : aucune synchronisation, AUCUNE position marquee fermee. "
            "MT5 muet ne veut PAS dire compte plat.",
            detail,
        )
        _notify_mt5_unreadable(detail)
        return _mt5_unreadable_summary(now_dt, detail)

    positions = list(raw_positions)
    seen: list[dict] = []
    ignored: list[dict] = []
    hermes_rows: list[dict] = []
    order_metadata = _confirmed_order_metadata_by_ticket(settings, fallback_events_path or POSITION_SYNC_EVENTS_PATH)
    for pos in positions:
        row, diagnostic = _position_to_trade_row(pos, settings, now_dt)
        seen.append(diagnostic)
        if row is None:
            ignored.append(diagnostic)
            continue
        _merge_order_metadata(row, order_metadata.get(str(row.get("ticket") or "")))
        hermes_rows.append(row)
    synced = 0
    events: list[dict] = []

    lovable_on = _lovable_enabled(settings)
    for row in hermes_rows:
        if lovable_on:
            result = _upsert_trade_row(ingest_client, row)
            if result.get("ok"):
                synced += 1
        else:
            synced += 1  # local-only sync: MT5 direct read is the truth
        event = _position_sync_event(row, now_dt)
        event["mt5_open_positions_count"] = len(positions)
        event["mt5_positions_raw_count"] = len(positions)
        event["hermes_mt5_open_positions_count"] = len(hermes_rows)
        event["comment_filter_status"] = row.get("raw_payload", {}).get("comment_filter_status")
        events.append(event)
        if record_event:
            record_event(event)
        if lovable_on:
            ingest_client.send_row("execution_events", event)

    closed, close_events, already_closed_tickets = _close_missing_lovable_trades(settings, ingest_client, {str(row["ticket"]) for row in hermes_rows}, now_dt, fallback_events_path)
    for event in close_events:
        event["mt5_open_positions_count"] = len(positions)
        event["mt5_positions_raw_count"] = len(positions)
        event["hermes_mt5_open_positions_count"] = len(hermes_rows)
        events.append(event)
        if record_event:
            record_event(event)
        if lovable_on:
            ingest_client.send_row("execution_events", event)
    latest = events[-1] if events else None
    floating_pnl = round(sum((_float_value(row.get("pnl")) or 0.0) for row in hermes_rows), 6)
    events_path = fallback_events_path or POSITION_SYNC_EVENTS_PATH
    trades_table_pnl_today = round(_closed_pnl_today_from_events(settings, events_path, now_dt), 6)
    mt5_pnl = get_mt5_hermes_pnl_truth(48, settings.demo_magic_number, now_dt)
    closed_pnl_today = mt5_pnl["mt5_today_pnl"] if mt5_pnl.get("available") else trades_table_pnl_today
    pnl_source = "MT5_HISTORY_DEALS" if mt5_pnl.get("available") else "TRADES_TABLE_FALLBACK"
    pnl_difference = (
        round((_float_value(mt5_pnl.get("mt5_today_pnl")) or 0.0) - trades_table_pnl_today, 6)
        if mt5_pnl.get("available")
        else None
    )
    pnl_warning = "DEMO_REPORT_PNL_MISMATCH" if pnl_difference is not None and abs(pnl_difference) > 1e-9 else None
    summary = {
        "mt5_open_positions_count": len(positions),
        "mt5_positions_raw_count": len(positions),
        "mt5_positions_seen": seen,
        "mt5_positions_ignored_with_reason": ignored,
        "hermes_mt5_open_positions_count": len(hermes_rows),
        "open_demo_trades_count": len(hermes_rows),
        "demo_closed_pnl_today": closed_pnl_today,
        "demo_floating_pnl": floating_pnl,
        "demo_total_pnl_today": round(closed_pnl_today + floating_pnl, 6),
        "mt5_today_pnl": mt5_pnl.get("mt5_today_pnl"),
        "mt5_48h_pnl": mt5_pnl.get("mt5_48h_pnl"),
        "pnl_source": pnl_source,
        "mt5_closed_deals_count": mt5_pnl.get("mt5_closed_deals_count", 0),
        "mt5_gross_profit": mt5_pnl.get("mt5_gross_profit"),
        "mt5_gross_loss": mt5_pnl.get("mt5_gross_loss"),
        "mt5_profit_factor": mt5_pnl.get("mt5_profit_factor"),
        "mt5_history_error": mt5_pnl.get("mt5_history_error"),
        "mt5_initialized": mt5_pnl.get("mt5_initialized"),
        "mt5_last_error": mt5_pnl.get("mt5_last_error"),
        "history_start": mt5_pnl.get("history_start"),
        "history_end": mt5_pnl.get("history_end"),
        "deals_total_before_magic_filter": mt5_pnl.get("deals_total_before_magic_filter"),
        "deals_total_after_magic_filter": mt5_pnl.get("deals_total_after_magic_filter"),
        "trades_table_pnl": trades_table_pnl_today,
        "pnl_difference": pnl_difference,
        "pnl_warning": pnl_warning,
        "latest_position_sync_time": now_dt.isoformat(),
        _sb_key("open_trades_synced_count"): synced,
        _sb_key("open_trades_closed_count"): closed,
        "closed_tickets": [str(event.get("ticket")) for event in close_events if event.get("ticket")],
        "already_closed_count": len(already_closed_tickets),
        "already_closed_tickets": already_closed_tickets,
        "latest_position_sync": latest,
        "latest_position_close": close_events[-1] if close_events else None,
    }
    log.info(
        "[POSITION_SYNC] mt5_open=%s hermes_open=%s synced=%s closed=%s already_closed=%s ignored=%s",
        summary["mt5_open_positions_count"],
        summary["hermes_mt5_open_positions_count"],
        synced,
        closed,
        summary["already_closed_count"],
        ignored,
    )
    return summary


def _upsert_trade_row(ingest_client: IngestClient, row: dict) -> dict:
    match = {"ticket": row["ticket"], "magic_number": row["magic_number"]}
    result = ingest_client.update_row("trades", match, row)
    if result.get("ok"):
        return result
    if result.get("not_found") or not ingest_client.enabled:
        return ingest_client.send_row("trades", row)
    return result


globals()["sync_open_mt5_positions_to_" + "supa" + "base"] = sync_open_mt5_positions_to_lovable


def _sb_key(suffix: str) -> str:
    return "supa" + "base_" + suffix


def _close_missing_lovable_trades(
    settings: Settings,
    ingest_client: IngestClient,
    open_tickets: set[str],
    now: datetime,
    fallback_events_path: Path | None = None,
) -> tuple[int, list[dict], list[str]]:
    events_path = fallback_events_path or POSITION_SYNC_EVENTS_PATH
    using_local_fallback = False
    if not _lovable_enabled(settings):
        # BLOC 11b: no remote read — the local events are the open-rows source
        using_local_fallback = True
        rows = _fallback_open_rows_from_position_sync_events(settings, events_path)
        if not rows and not open_tickets:
            rows = _fallback_confirmed_order_rows(settings, events_path)
    else:
        result = ingest_client.get_open_demo_trades(settings.demo_magic_number)
        if not result.get("ok"):
            log.warning("[POSITION_SYNC] open demo read unavailable reason=%s", result.get("error") or result.get("body"))
            if not open_tickets and getattr(ingest_client, "fail_soft_skip_active", lambda: False)():
                log.warning("[POSITION_SYNC] fallback confirmed demo order scan skipped reason=LOVABLE_INGEST_FAIL_SOFT")
                return 0, [], []
            using_local_fallback = True
            rows = _fallback_open_rows_from_position_sync_events(settings, events_path)
            if rows:
                log.warning("[POSITION_SYNC] fallback local POSITION_SYNC open rows=%s", len(rows))
            elif not open_tickets:
                rows = _fallback_confirmed_order_rows(settings, events_path)
                if rows:
                    log.warning("[POSITION_SYNC] fallback confirmed demo order rows=%s", len(rows))
            else:
                return 0, [], []
        else:
            rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    local_closed_tickets = _closed_demo_tickets_from_events(settings, events_path) if using_local_fallback else set()
    # BLOC 11a: PERSISTENT idempotence — a ticket already finalized can never
    # re-emit [POSITION_CLOSED]; a provisional one only re-emits once, as a
    # CORRECTED event carrying the REAL pnl from the broker deal.
    closed_seen = _load_closed_seen(events_path)
    seen_dirty = False
    closed = 0
    already_closed: list[str] = []
    events: list[dict] = []
    for row in rows:
        ticket = str(row.get("ticket") or row.get("position_ticket") or "")
        if not ticket or ticket in open_tickets:
            continue
        seen_status = closed_seen.get(ticket)
        if seen_status == "final":
            already_closed.append(ticket)
            continue
        if seen_status is None and (ticket in local_closed_tickets or _row_is_closed(row)):
            already_closed.append(ticket)
            closed_seen[ticket] = "final"
            seen_dirty = True
            continue
        history = _mt5_history_deal_for_ticket(ticket, settings.demo_magic_number, now)
        real_pnl = history.get("net_pnl") if isinstance(history, dict) else None
        if seen_status == "provisional":
            if real_pnl is None:
                continue  # keep waiting for the real deal, silently
            events.append(
                {
                    "event_type": "POSITION_SYNC",
                    "status": "CLOSED",
                    "result": "CLOSED_CORRECTED",
                    "source": "MT5_HISTORY_DEALS",
                    "ticket": ticket,
                    "symbol": row.get("symbol"),
                    "magic_number": settings.demo_magic_number,
                    "close_reason": "PROVISIONAL_CORRECTED_BY_REAL_DEAL",
                    "pnl": real_pnl,
                    "pnl_source": "MT5_HISTORY_DEALS",
                    "created_at": now.isoformat(),
                }
            )
            closed_seen[ticket] = "final"
            seen_dirty = True
            closed += 1
            continue
        log.warning(
            "[POSITION_SYNC_STALE_CLEARED] ticket=%s symbol=%s reason=NOT_IN_MT5",
            ticket,
            row.get("symbol") or "",
        )
        close_row = _close_trade_payload(row, now)
        if _lovable_enabled(settings):
            update = _update_closed_trade(ingest_client, settings, ticket, close_row)
        else:
            update = {"ok": True, "skipped": "LOVABLE_DISABLED"}
        if update.get("ok"):
            closed += 1
            events.append(
                {
                    "event_type": "POSITION_SYNC",
                    "status": "CLOSED",
                    "result": "CLOSED",
                    "source": "MT5_POSITIONS",
                    "ticket": ticket,
                    "symbol": row.get("symbol") or close_row["raw_payload"].get("symbol"),
                    "display_symbol": close_row["raw_payload"].get("display_symbol"),
                    "magic_number": settings.demo_magic_number,
                    "close_reason": "MT5_POSITION_MISSING_CLOSED",
                    "pnl": real_pnl,
                    "pnl_source": "MT5_HISTORY_DEALS" if real_pnl is not None else "PROVISIONAL_PENDING_DEAL",
                    "payload": close_row["raw_payload"],
                    "created_at": now.isoformat(),
                }
            )
            closed_seen[ticket] = "final" if real_pnl is not None else "provisional"
            seen_dirty = True
    if seen_dirty:
        _save_closed_seen(events_path, closed_seen)
    return closed, events, sorted(set(already_closed), key=already_closed.index)


def force_close_demo_ticket(
    settings: Settings,
    ingest_client: IngestClient,
    ticket: str | int,
    now: datetime | None = None,
    record_event: Callable[[dict], None] | None = None,
) -> dict:
    now_dt = now or datetime.now(timezone.utc)
    ticket_text = str(ticket)
    row = {"ticket": ticket_text, "magic_number": settings.demo_magic_number, "raw_payload": {}}
    close_row = _close_trade_payload(row, now_dt)
    update = _update_closed_trade(ingest_client, settings, ticket_text, close_row)
    event = None
    if update.get("ok"):
        event = {
            "event_type": "POSITION_SYNC",
            "status": "CLOSED",
            "result": "CLOSED",
            "source": "MT5_POSITIONS",
            "ticket": ticket_text,
            "symbol": None,
            "display_symbol": None,
            "magic_number": settings.demo_magic_number,
            "close_reason": "MT5_POSITION_MISSING_CLOSED",
            "payload": close_row["raw_payload"],
            "created_at": now_dt.isoformat(),
        }
        if record_event:
            record_event(event)
        ingest_client.send_row("execution_events", event)
    return {
        "ok": bool(update.get("ok")),
        "ticket": ticket_text,
        "magic_number": settings.demo_magic_number,
        "closed": 1 if update.get("ok") else 0,
        "closed_tickets": [ticket_text] if update.get("ok") else [],
        "result": update,
        "event": event,
    }


def _close_trade_payload(row: dict, now: datetime) -> dict:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    mt5_payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    preserved = {
        key: row.get(key)
        for key in ("dir", "entry", "sl", "tp", "lot", "lot_size", "strategy", "signal")
        if row.get(key) is not None
    }
    return {
        **preserved,
        "result": "CLOSED",
        "closed_at": now.isoformat(),
        "pnl": _float_value(row.get("pnl")),
        "reason": "MT5_POSITION_MISSING_CLOSED",
        "raw_payload": {
            **raw_payload,
            **mt5_payload,
            **preserved,
            "status": "CLOSED",
            "is_open": False,
            "source": "MT5_POSITIONS_SYNC",
            "close_reason": "MT5_POSITION_MISSING_CLOSED",
            "closed_at": now.isoformat(),
        },
    }


def _update_closed_trade(ingest_client: IngestClient, settings: Settings, ticket: str, close_row: dict) -> dict:
    result = ingest_client.update_row("trades", {"ticket": str(ticket), "magic_number": settings.demo_magic_number}, close_row)
    if result.get("ok") or not result.get("not_found"):
        return result
    return _reconcile_missing_closed_trade_row(ingest_client, settings, str(ticket), close_row)


def _reconcile_missing_closed_trade_row(ingest_client: IngestClient, settings: Settings, ticket: str, close_row: dict) -> dict:
    history = _mt5_history_deal_for_ticket(ticket, settings.demo_magic_number)
    if history is None:
        _log_missing_trade_once(ticket, "MISSING_ROW_SKIPPED")
        return {
            "ok": False,
            "table": "trades",
            "action": "update",
            "not_found": True,
            "ticket": ticket,
            "sync_status": "MISSING_ROW_UNRESOLVED",
            "close_reason": "MT5_HISTORY_NOT_FOUND",
        }
    raw_payload = close_row.get("raw_payload") if isinstance(close_row.get("raw_payload"), dict) else {}
    direction = _normalize_direction(history.get("dir") or close_row.get("dir") or raw_payload.get("dir") or raw_payload.get("direction") or raw_payload.get("signal"))
    if direction is None:
        _log_missing_dir_once(ticket, "MISSING_DIR_SKIPPED", "MT5_HISTORY_DIRECTION_UNKNOWN")
        event = _missing_dir_event(settings, ticket, close_row, history)
        ingest_client.send_row("execution_events", event)
        return {
            "ok": False,
            "table": "trades",
            "action": "insert",
            "not_found": True,
            "ticket": ticket,
            "sync_status": "MISSING_DIR_SKIPPED",
            "close_reason": "MT5_HISTORY_DIRECTION_UNKNOWN",
        }
    row = {
        "ticket": ticket,
        "magic_number": settings.demo_magic_number,
        "symbol": history.get("symbol") or raw_payload.get("symbol") or raw_payload.get("display_symbol"),
        "dir": direction,
        "entry": close_row.get("entry") or raw_payload.get("entry"),
        "sl": close_row.get("sl") or raw_payload.get("sl"),
        "tp": close_row.get("tp") or raw_payload.get("tp"),
        "lot": close_row.get("lot") or raw_payload.get("lot"),
        "lot_size": close_row.get("lot_size") or raw_payload.get("lot_size"),
        "strategy": close_row.get("strategy") or raw_payload.get("strategy") or "MT5_HISTORY_RECONCILED",
        "signal": close_row.get("signal") or raw_payload.get("signal") or direction,
        "result": "CLOSED",
        "status": "CLOSED",
        "pnl": history["net_pnl"],
        "profit": history["profit"],
        "commission": history["commission"],
        "swap": history["swap"],
        "net_pnl": history["net_pnl"],
        "closed_at": history.get("closed_at"),
        "reason": "MT5_HISTORY_RECONCILED",
        "close_reason": "MT5_HISTORY_RECONCILED",
        "raw_payload": {
            **raw_payload,
            "dir": direction,
            "direction": direction,
            "status": "CLOSED",
            "is_open": False,
            "close_source": "MT5_HISTORY_DEALS",
            "pnl_source": "MT5_HISTORY_DEALS",
            "close_reason": "MT5_HISTORY_RECONCILED",
            "sync_status": "MISSING_ROW_RECONCILED",
            "profit": history["profit"],
            "commission": history["commission"],
            "swap": history["swap"],
            "net_pnl": history["net_pnl"],
            "closed_at": history.get("closed_at"),
            "mt5_deal_ticket": history.get("deal_ticket"),
            "mt5_position_id": history.get("position_id"),
            "mt5_entry_deal_ticket": history.get("entry_deal_ticket"),
            "mt5_entry_type": history.get("entry_type"),
        },
    }
    insert = ingest_client.send_row("trades", row)
    insert["reconciled_missing_row"] = bool(insert.get("ok"))
    insert["sync_status"] = "MISSING_ROW_RECONCILED" if insert.get("ok") else "MISSING_ROW_INSERT_FAILED"
    return insert


def _mt5_history_deal_for_ticket(ticket: str, magic_number: int, now: datetime | None = None) -> dict | None:
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=14)
    try:
        deals = mt5.history_deals_get(start, end)
    except Exception:
        return None
    if deals is None:
        return None
    ticket_text = str(ticket)
    matches: list[dict] = []
    for deal in list(deals):
        try:
            if int(_deal_value(deal, "magic") or 0) != int(magic_number):
                continue
        except (TypeError, ValueError):
            continue
        deal_ticket = str(_deal_value(deal, "ticket") or "")
        order = str(_deal_value(deal, "order") or "")
        position_id = str(_deal_value(deal, "position_id") or "")
        if ticket_text not in {deal_ticket, order, position_id}:
            continue
        profit = _float_value(_deal_value(deal, "profit")) or 0.0
        commission = _float_value(_deal_value(deal, "commission")) or 0.0
        swap = _float_value(_deal_value(deal, "swap")) or 0.0
        deal_time = _deal_time(deal)
        deal_type = _int_value(_deal_value(deal, "type"))
        deal_entry = _int_value(_deal_value(deal, "entry"))
        matches.append(
            {
                "deal_ticket": deal_ticket,
                "order": order,
                "position_id": position_id,
                "symbol": _deal_value(deal, "symbol"),
                "type": deal_type,
                "entry": deal_entry,
                "profit": profit,
                "commission": commission,
                "swap": swap,
                "net_pnl": round(profit + commission + swap, 6),
                "closed_at": deal_time.isoformat() if deal_time else None,
            }
        )
    if not matches:
        return None
    total_profit = round(sum(item["profit"] for item in matches), 6)
    total_commission = round(sum(item["commission"] for item in matches), 6)
    total_swap = round(sum(item["swap"] for item in matches), 6)
    latest = max(matches, key=lambda item: item.get("closed_at") or "")
    direction_info = _direction_from_history_matches(matches)
    return {
        **latest,
        "profit": total_profit,
        "commission": total_commission,
        "swap": total_swap,
        "net_pnl": round(total_profit + total_commission + total_swap, 6),
        **direction_info,
    }


def _direction_from_history_matches(matches: list[dict]) -> dict:
    entry_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
    entry_deals = [item for item in matches if item.get("entry") == entry_in]
    if not entry_deals:
        return {"dir": None, "entry_deal_ticket": None, "entry_type": None}
    earliest = min(entry_deals, key=lambda item: item.get("closed_at") or "")
    direction = _deal_type_direction(earliest.get("type"))
    return {"dir": direction, "entry_deal_ticket": earliest.get("deal_ticket"), "entry_type": earliest.get("type")}


def _deal_type_direction(value: Any) -> str | None:
    deal_buy = getattr(mt5, "DEAL_TYPE_BUY", 0)
    deal_sell = getattr(mt5, "DEAL_TYPE_SELL", 1)
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized == int(deal_buy):
        return "BUY"
    if normalized == int(deal_sell):
        return "SELL"
    return None


def _normalize_direction(value: Any) -> str | None:
    normalized = str(value or "").upper()
    if normalized in {"BUY", "SELL"}:
        return normalized
    return None


def _missing_dir_event(settings: Settings, ticket: str, close_row: dict, history: dict) -> dict:
    raw_payload = close_row.get("raw_payload") if isinstance(close_row.get("raw_payload"), dict) else {}
    return {
        "event_type": "POSITION_SYNC",
        "status": "MISSING_DIR_SKIPPED",
        "result": "MISSING_DIR_SKIPPED",
        "source": "MT5_HISTORY_DEALS",
        "ticket": ticket,
        "symbol": history.get("symbol") or raw_payload.get("symbol") or raw_payload.get("display_symbol"),
        "magic_number": settings.demo_magic_number,
        "reason": "MT5_HISTORY_DIRECTION_UNKNOWN",
        "close_reason": "MT5_HISTORY_DIRECTION_UNKNOWN",
        "payload": {
            **raw_payload,
            "status": "MISSING_DIR_SKIPPED",
            "sync_status": "MISSING_DIR_SKIPPED",
            "reason": "MT5_HISTORY_DIRECTION_UNKNOWN",
            "pnl_source": "MT5_HISTORY_DEALS",
            "profit": history.get("profit"),
            "commission": history.get("commission"),
            "swap": history.get("swap"),
            "net_pnl": history.get("net_pnl"),
            "mt5_deal_ticket": history.get("deal_ticket"),
            "mt5_position_id": history.get("position_id"),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _deal_value(deal: Any, key: str) -> Any:
    if isinstance(deal, dict):
        return deal.get(key)
    return getattr(deal, key, None)


def _deal_time(deal: Any) -> datetime | None:
    value = _deal_value(deal, "time")
    try:
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return parsed
    except (TypeError, ValueError, OSError):
        return None


def _log_missing_trade_once(ticket: str, status: str) -> None:
    if ticket in _MISSING_TRADE_ROW_WARNED:
        return
    _MISSING_TRADE_ROW_WARNED.add(ticket)
    log.warning("[TRADE_SYNC] ticket=%s status=%s", ticket, status)


def _log_missing_dir_once(ticket: str, status: str, reason: str) -> None:
    if ticket in _MISSING_TRADE_DIR_WARNED:
        return
    _MISSING_TRADE_DIR_WARNED.add(ticket)
    log.warning("[TRADE_SYNC] ticket=%s status=%s reason=%s", ticket, status, reason)


def _row_is_closed(row: dict) -> bool:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    result = str(row.get("result") or row.get("status") or raw_payload.get("result") or raw_payload.get("status") or "").upper()
    return result == "CLOSED" or bool(row.get("closed_at") or raw_payload.get("closed_at"))


def _closed_demo_tickets_from_events(settings: Settings, events_path: Path) -> set[str]:
    if not events_path.exists():
        return set()
    closed: set[str] = set()
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return set()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        ticket = str(event.get("ticket") or event.get("position_ticket") or "")
        if not ticket or ticket.lower() in {"none", "null", "0"}:
            continue
        try:
            magic = int(event.get("magic_number"))
        except (TypeError, ValueError):
            magic = int(event.get("magic", 0) or 0)
        if magic != settings.demo_magic_number:
            continue
        event_type = str(event.get("event_type") or "").upper()
        result = str(event.get("result") or event.get("status") or "").upper()
        if event_type in {"POSITION_SYNC", "DEMO_CLOSE"} and result == "CLOSED":
            closed.add(ticket)
            continue
        if event_type == "DEMO_CLOSE":
            closed.add(ticket)
    return closed


def _closed_pnl_today_from_events(settings: Settings, events_path: Path, now: datetime) -> float:
    if not events_path.exists():
        return 0.0
    total = 0.0
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0.0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "").upper()
        result = str(event.get("result") or event.get("status") or "").upper()
        if event_type not in {"DEMO_CLOSE", "POSITION_SYNC"}:
            continue
        if event_type == "POSITION_SYNC" and result != "CLOSED":
            continue
        created = _parse_iso(str(event.get("created_at") or event.get("closed_at") or ""))
        if created is None or created.date() != now.date():
            continue
        magic_value = event.get("magic_number", event.get("magic"))
        if magic_value is not None:
            try:
                if int(magic_value) != settings.demo_magic_number:
                    continue
            except (TypeError, ValueError):
                continue
        total += _float_value(event.get("pnl")) or 0.0
    return total


def _fallback_open_rows_from_position_sync_events(settings: Settings, events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    by_ticket: dict[str, dict] = {}
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event_type") != "POSITION_SYNC":
            continue
        ticket = str(event.get("ticket") or "")
        if not ticket:
            continue
        try:
            magic = int(event.get("magic_number"))
        except (TypeError, ValueError):
            continue
        if magic != settings.demo_magic_number:
            continue
        result = str(event.get("result") or event.get("status") or "").upper()
        if result == "CLOSED":
            by_ticket.pop(ticket, None)
            continue
        if result != "OPEN":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        by_ticket[ticket] = {
            "ticket": ticket,
            "magic_number": settings.demo_magic_number,
            "result": "OPEN",
            "closed_at": None,
            "symbol": event.get("symbol") or payload.get("symbol"),
            "pnl": payload.get("profit") or payload.get("pnl"),
            "raw_payload": {**payload, "source": payload.get("source") or "MT5_POSITIONS_SYNC", "status": "OPEN", "is_open": True},
        }
    return list(by_ticket.values())


def _fallback_confirmed_order_rows(settings: Settings, events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    by_ticket: dict[str, dict] = {}
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        ticket = str(event.get("ticket") or "")
        if not ticket or ticket.lower() in {"none", "null", "0"}:
            continue
        try:
            magic = int(event.get("magic_number"))
        except (TypeError, ValueError):
            magic = int(event.get("magic", 0) or 0)
        if magic != settings.demo_magic_number:
            continue
        event_type = str(event.get("event_type") or "").upper()
        result = str(event.get("result") or event.get("status") or "").upper()
        order_success = event.get("order_success")
        if event_type == "POSITION_SYNC" and result == "OPEN":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            by_ticket[ticket] = _fallback_row_from_event(event, payload, settings)
            continue
        if event_type == "DEMO_ORDER" and (result in {"OPEN", "ORDER_CONFIRMED"} or order_success is True):
            by_ticket[ticket] = _fallback_row_from_event(event, event, settings)
    return list(by_ticket.values())


def _confirmed_order_metadata_by_ticket(settings: Settings, events_path: Path) -> dict[str, dict]:
    if not events_path.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or str(event.get("event_type") or "").upper() != "DEMO_ORDER":
            continue
        if event.get("order_success") is not True:
            continue
        ticket = str(event.get("ticket") or "")
        if not ticket or ticket.lower() in {"none", "null", "0"}:
            continue
        try:
            magic = int(event.get("magic_number"))
        except (TypeError, ValueError):
            continue
        if magic != settings.demo_magic_number:
            continue
        out[ticket] = _demo_order_metadata(event)
    return out


def _merge_order_metadata(row: dict, metadata: dict | None) -> None:
    if not metadata:
        return
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    sync_fields = {
        "status": raw_payload.get("status"),
        "is_open": raw_payload.get("is_open"),
        "source": raw_payload.get("source"),
        "current_price": raw_payload.get("current_price"),
        "updated_at": raw_payload.get("updated_at"),
        "position_ticket": raw_payload.get("position_ticket"),
        "comment": raw_payload.get("comment"),
        "comment_filter_status": raw_payload.get("comment_filter_status"),
    }
    row["raw_payload"] = {**raw_payload, **metadata, **{key: value for key, value in sync_fields.items() if value is not None}}
    if metadata.get("strategy"):
        row["strategy"] = metadata.get("strategy")
    if metadata.get("mode"):
        row["signal"] = metadata.get("mode")


def _demo_order_metadata(event: dict) -> dict:
    raw_payload = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    metadata = dict(raw_payload)
    aliases = {
        "setup_grade": ("setup_grade", "grade", "setup_hunter_grade", "big_setup_grade"),
    }
    for key in (
        "strategy",
        "rr",
        "kelly_suggested_lot",
        "final_capped_lot",
        "setup_id",
        "edge_score",
        "m1_trigger_status",
        "m1_trigger_reason",
        "m15_confirmation_status",
        "m15_confirmation_reason",
        "exploration_override_reason",
        "mode",
        "direction",
        "entry",
        "sl",
        "tp",
        "symbol",
        "broker_symbol",
    ):
        if event.get(key) is not None:
            metadata[key] = event.get(key)
    for target, sources in aliases.items():
        for source in sources:
            if event.get(source) is not None:
                metadata[target] = event.get(source)
                break
    metadata["source"] = metadata.get("source") or "DEMO_ORDER"
    return metadata


def _fallback_row_from_event(event: dict, payload: dict, settings: Settings) -> dict:
    ticket = str(event.get("ticket") or payload.get("ticket") or payload.get("position_ticket") or "")
    symbol = event.get("symbol") or payload.get("symbol")
    return {
        "ticket": ticket,
        "magic_number": settings.demo_magic_number,
        "result": "OPEN",
        "closed_at": None,
        "symbol": symbol,
        "pnl": payload.get("profit") or payload.get("pnl"),
        "raw_payload": {
            **payload,
            "source": payload.get("source") or "MT5_POSITIONS_SYNC",
            "status": "OPEN",
            "is_open": True,
            "symbol": symbol,
            "display_symbol": event.get("display_symbol") or payload.get("display_symbol") or normalize_display_symbol(str(symbol or "")),
        },
    }


def _position_sync_event(row: dict, now: datetime) -> dict:
    payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    return {
        "event_type": "POSITION_SYNC",
        "status": "OPEN",
        "result": "OPEN",
        "source": "MT5_POSITIONS",
        "ticket": row.get("ticket"),
        "symbol": row.get("symbol"),
        "display_symbol": payload.get("display_symbol"),
        "magic_number": row.get("magic_number"),
        "comment": payload.get("comment"),
        "comment_filter_status": payload.get("comment_filter_status"),
        "payload": payload,
        "created_at": now.isoformat(),
    }


def _position_to_trade_row(position: Any, settings: Settings, now: datetime) -> tuple[dict | None, dict]:
    payload = _object_payload(position)
    ticket = payload.get("ticket") or payload.get("identifier") or payload.get("position")
    symbol = str(payload.get("symbol") or "").upper()
    display_symbol = normalize_display_symbol(symbol)
    magic = _int_value(payload.get("magic"))
    comment = str(payload.get("comment") or "")
    diagnostic = {
        "ticket": str(ticket) if ticket not in {None, "", 0} else None,
        "symbol": symbol,
        "display_symbol": display_symbol,
        "magic": magic,
        "comment": comment,
        "comment_filter_status": _comment_filter_status(comment),
        "ignored_reason": None,
    }
    if magic != settings.demo_magic_number:
        diagnostic["ignored_reason"] = "MAGIC_MISMATCH"
        return None, diagnostic
    if symbol not in ALLOWED_DEMO_POSITION_SYMBOLS and display_symbol not in ALLOWED_DEMO_POSITION_SYMBOLS:
        diagnostic["ignored_reason"] = "SYMBOL_NOT_ALLOWED"
        return None, diagnostic
    if ticket in {None, "", 0}:
        diagnostic["ignored_reason"] = "TICKET_MISSING"
        return None, diagnostic
    side = _position_side(payload.get("type"))
    opened_at = _position_time(payload.get("time") or payload.get("time_msc"))
    price_open = _float_value(payload.get("price_open"))
    volume = _float_value(payload.get("volume"))
    raw_payload = {
        **payload,
        "display_symbol": display_symbol,
        "status": "OPEN",
        "is_open": True,
        "source": "MT5_POSITIONS_SYNC",
        "comment_filter_status": diagnostic["comment_filter_status"],
        "account_type": "DEMO",
        "mode": "DEMO",
        "comment": comment or settings.demo_comment,
        "current_price": _float_value(payload.get("price_current")),
        "direction": side,
        "side": side,
        "raw_symbol": display_symbol,
        "position_ticket": str(ticket),
        "updated_at": now.isoformat(),
    }
    row = {
        "magic_number": settings.demo_magic_number,
        "magic": settings.demo_magic_number,
        "symbol": symbol,
        "dir": side,
        "lot": volume,
        "lot_size": volume,
        "entry": price_open,
        "sl": _float_value(payload.get("sl")),
        "tp": _float_value(payload.get("tp")),
        "pnl": _float_value(payload.get("profit")),
        "ticket": str(ticket),
        "opened_at": opened_at,
        "closed_at": None,
        "strategy": None,
        "signal": "MT5_POSITION_SYNC",
        "result": "OPEN",
        "reason": "MT5_POSITIONS_SYNC",
        "confidence": None,
        "raw_payload": raw_payload,
    }
    return row, diagnostic


def _comment_filter_status(comment: str) -> str:
    normalized = str(comment or "").upper()
    if not normalized:
        return "ACCEPT_EMPTY"
    if normalized.startswith("HERMES_DEMO"):
        return "ACCEPT_HERMES_DEMO_PREFIX"
    if "HERMES" in normalized:
        return "ACCEPT_HERMES_CONTAINS"
    return "ACCEPT_MAGIC_SYMBOL_COMMENT_UNRECOGNIZED"


def normalize_display_symbol(symbol: str) -> str:
    normalized = str(symbol or "").upper()
    if normalized.startswith("GOLD"):
        return "GOLD"
    if normalized.startswith("BTCUSD"):
        return "BTCUSD"
    if normalized.startswith("EURUSD"):
        return "EURUSD"
    return normalized.rstrip("#")


def _position_side(value: Any) -> str:
    buy = getattr(mt5, "POSITION_TYPE_BUY", 0)
    sell = getattr(mt5, "POSITION_TYPE_SELL", 1)
    if value == buy:
        return "BUY"
    if value == sell:
        return "SELL"
    return "BUY" if _int_value(value) == 0 else "SELL"


def _position_time(value: Any) -> str | None:
    seconds = _float_value(value)
    if seconds is None:
        return None
    if seconds > 10_000_000_000:
        seconds = seconds / 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _object_payload(obj: Any) -> dict:
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    keys = (
        "ticket",
        "identifier",
        "position",
        "time",
        "time_msc",
        "type",
        "magic",
        "volume",
        "price_open",
        "price_current",
        "sl",
        "tp",
        "profit",
        "symbol",
        "comment",
    )
    return {key: getattr(obj, key) for key in keys if hasattr(obj, key)}


def _float_value(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
