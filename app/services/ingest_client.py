from __future__ import annotations

import math
import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable
from urllib.parse import urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.exceptions import RequestException

from app.config import Settings
from app.logger import log, utc_now_iso
from app.utils.throttle import should_emit

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional at import time
    np = None

try:
    import pandas as pd
except ImportError:  # pragma: no cover - optional at import time
    pd = None


_BOT_LOG_TIME_FIELDS = (
    "utc_time",
    "casablanca_time",
    "broker_time_estimate",
    "broker_utc_offset_hours",
    "local_hour",
    "utc_hour",
    "broker_hour",
    "weekday",
    "session_name",
    "asia_window",
    "asia_trading_allowed",
    "asia_block_reason",
    "market_open",
    "is_weekend",
    "is_bad_hour",
    "time_gate_status",
    "time_gate_reason",
)
_BOT_LOG_DUPLICATE_TIME_COLUMNS = (
    "utc_time",
    "casablanca_time",
    "broker_time_estimate",
    "session_name",
    "time_gate_status",
)


class IngestClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(settings.hermes_ingest_url and settings.hermes_ingest_secret)
        self.latest_time_snapshot: dict | None = None
        self._trades_update_missing_logged: set[str] = set()
        self._fail_soft_until = 0.0
        self._fail_soft_logged = False

    def connect(self) -> bool:
        if not self.enabled:
            log.warning("Hermes ingest credentials missing; writes will be skipped")
            return False
        log.info("Lovable ingest configured")
        return True

    def send_row(self, table: str, data: dict) -> dict:
        data = self.prepare_row(table, data)
        if table == "bot_logs":
            _print_bot_log_time_debug(data)
        if not self.enabled:
            return _failure(table, "Hermes ingest credentials missing")
        if self._fail_soft_skip_active():
            self._log_skip()
            # §6: alert when a trade fill would be silently dropped by the circuit breaker
            if table == "execution_events":
                _action = str(
                    data.get("demo_action") or data.get("event_type") or ""
                ).upper()
                if "OPEN" in _action or _action in {"DEMO_FILL", "ORDER_PLACED"}:
                    log.warning(
                        "[LOVABLE_INGEST_FILL_INVISIBLE] table=%s demo_action=%s "
                        "reason=CIRCUIT_BREAKER_ACTIVE fill_will_not_be_recorded=True",
                        table, _action,
                    )
            return _failure(table, "LOVABLE_INGEST_FAIL_SOFT_SKIP")
        response = None
        try:
            clean_data = sanitize_for_json(data)
            response = requests.post(
                self.settings.hermes_ingest_url,
                headers={
                    "x-hermes-secret": self.settings.hermes_ingest_secret,
                    "Content-Type": "application/json",
                },
                json={"table": table, "data": clean_data},
                timeout=self._timeout(),
            )
            body = response.text
            if 200 <= response.status_code <= 299:
                self._check_recovery()
                return {"ok": True, "table": table, "status_code": response.status_code, "error": None, "body": body}

            if _is_conflict_tolerated(table, response.status_code, body):
                log.warning("Duplicate/conflict ignored for %s: status=%s body=%s", table, response.status_code, body)
                return {
                    "ok": True,
                    "table": table,
                    "status_code": response.status_code,
                    "error": "DUPLICATE_OR_CONFLICT",
                    "body": body,
                    "duplicate": True,
                }

            self._mark_fail_soft()
            return _failure(table, f"HTTP {response.status_code}", response.status_code, body)
        except RequestException as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body)
        except ValueError as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body)
        except Exception as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body)

    def update_row(self, table: str, match: dict, data: dict) -> dict:
        data = self.prepare_row(table, data)
        if table == "bot_logs":
            _print_bot_log_time_debug(data)
        if not self.enabled:
            return _failure(table, "Hermes ingest credentials missing", action="update")
        if self._fail_soft_skip_active():
            self._log_skip()
            return _failure(table, "LOVABLE_INGEST_FAIL_SOFT_SKIP", action="update")
        response = None
        try:
            clean_match = sanitize_for_json(match)
            clean_data = sanitize_for_json(data)
            response = requests.post(
                self.settings.hermes_ingest_url,
                headers={
                    "x-hermes-secret": self.settings.hermes_ingest_secret,
                    "Content-Type": "application/json",
                },
                json={"table": table, "action": "update", "match": clean_match, "data": clean_data},
                timeout=self._timeout(),
            )
            body = response.text
            if 200 <= response.status_code <= 299 and not _is_not_found_response(body):
                self._check_recovery()
                return {"ok": True, "table": table, "action": "update", "status_code": response.status_code, "error": None, "body": body}

            not_found = response.status_code == 404 or _is_not_found_response(body)
            if table == "trades" and not_found:
                ticket = str(clean_match.get("ticket") or "")
                key = f"{ticket}:{clean_match.get('magic_number')}"
                if key not in self._trades_update_missing_logged:
                    self._trades_update_missing_logged.add(key)
                    log.warning("Hermes ingest update missing row for %s: status=%s match=%s body=%s", table, response.status_code, clean_match, body)
            else:
                self._mark_fail_soft()
            out = _failure(table, "NOT_FOUND" if not_found else f"HTTP {response.status_code}", response.status_code, body, action="update")
            out["not_found"] = not_found
            return out
        except RequestException as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body, action="update")
        except ValueError as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body, action="update")
        except Exception as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return _failure(table, str(exc), status_code, body, action="update")

    def send_bulk(self, table: str, rows: Iterable[dict]) -> dict:
        try:
            results = []
            for row in rows:
                results.append(self.send_row(table, row))
            ok_count = sum(1 for result in results if result.get("ok"))
            duplicate_count = sum(1 for result in results if result.get("duplicate"))
            return {
                "ok": ok_count == len(results),
                "status_code": None,
                "error": None if ok_count == len(results) else "One or more rows failed",
                "body": None,
                "sent": ok_count - duplicate_count,
                "duplicates": duplicate_count,
                "failed": len(results) - ok_count,
                "results": results,
            }
        except (RequestException, ValueError) as exc:
            log.error("Hermes ingest bulk failed for %s: %s", table, exc)
            log.error("Failed table: %s", table)
            return {"ok": False, "table": table, "status_code": None, "error": str(exc), "body": None, "sent": 0, "failed": 0, "results": []}
        except Exception as exc:
            log.error("Hermes ingest bulk unexpected failure for %s: %s", table, exc)
            log.error("Failed table: %s", table)
            return {"ok": False, "table": table, "status_code": None, "error": str(exc), "body": None, "sent": 0, "failed": 0, "results": []}

    def get_paper_report(self, hours: int = 1) -> dict:
        if not self.enabled:
            return {"ok": False, "status_code": None, "error": "Hermes ingest credentials missing", "body": None}

        url = self._paper_report_url(hours)
        response = None
        try:
            response = requests.get(
                url,
                headers={"x-hermes-secret": self.settings.hermes_ingest_secret},
                timeout=self._timeout(),
            )
            body = response.text
            if 200 <= response.status_code <= 299:
                return {"ok": True, "status_code": response.status_code, "error": None, "body": body}

            self._mark_fail_soft()
            return {"ok": False, "status_code": response.status_code, "error": f"HTTP {response.status_code}", "body": body}
        except RequestException as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body}
        except ValueError as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body}
        except Exception as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body}

    def get_open_paper_trades(self, magic_number: int) -> dict:
        if not self.enabled:
            return {"ok": False, "status_code": None, "error": "Hermes ingest credentials missing", "body": None, "rows": []}

        response = None
        try:
            data = sanitize_for_json({"magic_number": magic_number, "mode": "PAPER", "open_only": True})
            log.info(
                "[PAPER_RECOVERY] read request prepared table=trades action=read has_data=%s",
                isinstance(data, dict) and bool(data),
            )
            response = requests.get(
                self._open_paper_trades_url(magic_number),
                headers={"x-hermes-secret": self.settings.hermes_ingest_secret},
                timeout=self._timeout(),
            )
            body = response.text
            if 200 <= response.status_code <= 299:
                rows = _filter_open_paper_rows(_rows_from_body(body), magic_number)
                log.info("[PAPER_RECOVERY] read response rows=%s", len(rows))
                return {
                    "ok": True,
                    "status_code": response.status_code,
                    "error": None,
                    "body": body,
                    "rows": rows,
                }

            self._mark_fail_soft()
            return {"ok": False, "status_code": response.status_code, "error": f"HTTP {response.status_code}", "body": body, "rows": []}
        except RequestException as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body, "rows": []}
        except ValueError as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body, "rows": []}
        except Exception as exc:
            body = response.text if response is not None else None
            status_code = response.status_code if response is not None else None
            self._mark_fail_soft()
            return {"ok": False, "status_code": status_code, "error": str(exc), "body": body, "rows": []}

    def _timeout(self) -> float:
        try:
            return max(0.1, float(getattr(self.settings, "lovable_ingest_timeout_seconds", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    def _fail_soft_enabled(self) -> bool:
        return bool(getattr(self.settings, "lovable_ingest_fail_soft", True))

    def _circuit_breaker_enabled(self) -> bool:
        return bool(getattr(self.settings, "lovable_ingest_circuit_breaker_enabled", True))

    def _circuit_breaker_seconds(self) -> float:
        try:
            return max(30.0, float(getattr(self.settings, "lovable_ingest_circuit_breaker_seconds", 300.0)))
        except (TypeError, ValueError):
            return 300.0

    def _fail_soft_skip_active(self) -> bool:
        return self._fail_soft_enabled() and time.monotonic() < self._fail_soft_until

    def fail_soft_skip_active(self) -> bool:
        return self._fail_soft_skip_active()

    def _mark_fail_soft(self) -> None:
        if not self._fail_soft_enabled():
            log.warning("[LOVABLE_INGEST] status=SKIP reason=TIMEOUT_OR_HTTP_ERROR")
            return
        cb = self._circuit_breaker_seconds() if self._circuit_breaker_enabled() else 60.0
        self._fail_soft_until = time.monotonic() + cb
        if not self._fail_soft_logged:
            log.warning(
                "[LOVABLE_INGEST_HEALTH] status=DEGRADED reason=TIMEOUT_OR_HTTP_ERROR cooldown_seconds=%s",
                int(cb),
            )
            self._fail_soft_logged = True

    def _check_recovery(self) -> None:
        """Log RECOVERED once after the first successful write following a degraded period."""
        if self._fail_soft_logged:
            log.info("[LOVABLE_INGEST_HEALTH] status=RECOVERED")
            self._fail_soft_logged = False

    def _log_skip(self) -> None:
        if should_emit("LOVABLE_INGEST_CB_SKIP"):
            log.warning("[LOVABLE_INGEST_HEALTH] status=SKIP reason=CIRCUIT_BREAKER_ACTIVE")

    def get_open_demo_trades(self, magic_number: int) -> dict:
        """Remote open-demo-trade read is not implemented; returns fast without HTTP call."""
        if not self.enabled:
            return {"ok": False, "status_code": None, "error": "Hermes ingest credentials missing", "body": None, "rows": []}
        if should_emit("REMOTE_DEMO_READ_UNAVAILABLE"):
            log.warning("[POSITION_SYNC] remote open demo trades read unavailable; using local reconciliation fallback")
        return {"ok": False, "status_code": None, "error": "REMOTE_DEMO_READ_UNAVAILABLE", "body": None, "rows": []}

    def _paper_report_url(self, hours: int) -> str:
        parsed = urlparse(self.settings.hermes_ingest_url)
        path = "/api/public/hermes-paper-report"
        query = urlencode({"hours": max(1, int(hours))})
        return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))

    def _open_paper_trades_url(self, magic_number: int) -> str:
        parsed = urlparse(self.settings.hermes_ingest_url)
        path = "/api/public/hermes-open-paper-trades"
        query = urlencode({"magic_number": int(magic_number), "mode": "PAPER", "open_only": "true"})
        return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))

    def log_event(self, level: str, message: str, context: dict | None = None) -> None:
        return self.send_row(
            "bot_logs",
            {
                "level": level,
                "source": "HERMES_BACKEND",
                "message": message,
                "context": context or {},
                "created_at": utc_now_iso(),
            },
        )

    def emit_bot_log(
        self,
        token: str,
        message: str,
        payload: dict | None = None,
    ) -> dict:
        """Emit a structured telemetry token to bot_logs.

        Uses the existing circuit breaker — never raises, never blocks the cycle.
        token is stored in source so the frontend can filter by structured token.
        Payload is kept small; never include secrets or broker credentials.
        """
        try:
            return self.send_row(
                "bot_logs",
                {
                    "level": "INFO",
                    "source": token,
                    "message": f"[{token}] {message}",
                    "context": payload if isinstance(payload, dict) else {},
                    "created_at": utc_now_iso(),
                },
            )
        except Exception as exc:
            log.warning("[TELEMETRY] emit_bot_log failed token=%s reason=%s", token, exc)
            return {"ok": False, "table": "bot_logs", "error": str(exc)}

    def set_time_snapshot(self, snapshot: dict | None) -> None:
        if isinstance(snapshot, dict):
            self.latest_time_snapshot = dict(snapshot)

    def prepare_row(self, table: str, data: dict) -> dict:
        if table != "bot_logs":
            return data
        row = dict(data)
        row["raw_payload"] = self.bot_log_raw_payload(row)
        for field in _BOT_LOG_DUPLICATE_TIME_COLUMNS:
            row[field] = row["raw_payload"].get(field)
        return row

    def bot_log_raw_payload(self, row: dict) -> dict:
        current_raw = _coerce_raw_payload(row.get("raw_payload"))
        message = str(row.get("message") or current_raw.get("message") or "")
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        snapshot = _bot_log_snapshot(current_raw, context, self.latest_time_snapshot)
        time_payload = _bot_log_time_payload(snapshot, self.settings)
        return {
            **current_raw,
            **time_payload,
            "message": message,
            "level": row.get("level"),
            "source": row.get("source"),
            "context": context,
        }


def format_response_body(body: str | None) -> str:
    if body is None:
        return ""
    try:
        return json.dumps(json.loads(body), indent=2, sort_keys=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        return body


def _rows_from_body(body: str | None) -> list[dict]:
    if not body:
        return []
    parsed = json.loads(body)
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if not isinstance(parsed, dict):
        return []
    for key in ("rows", "data", "trades", "items", "open_trades", "open_paper_trades", "paper_trades"):
        value = parsed.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    for value in parsed.values():
        if isinstance(value, dict):
            rows = _rows_from_parsed_dict(value)
            if rows:
                return rows
    return [parsed] if parsed.get("id") or parsed.get("paper_trade_id") else []


def _rows_from_parsed_dict(parsed: dict) -> list[dict]:
    for key in ("rows", "data", "trades", "items", "open_trades", "open_paper_trades", "paper_trades"):
        value = parsed.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _filter_open_paper_rows(rows: list[dict], magic_number: int) -> list[dict]:
    return [row for row in rows if _is_open_paper_row(row, magic_number)]


def _filter_open_demo_rows(rows: list[dict], magic_number: int) -> list[dict]:
    return [row for row in rows if _is_open_demo_row(row, magic_number)]


def _is_open_paper_row(row: dict, magic_number: int) -> bool:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    row_magic = row.get("magic_number", row.get("magic", raw_payload.get("magic_number", raw_payload.get("magic"))))
    if _number(row_magic) != float(magic_number):
        return False
    mode = str(row.get("mode") or raw_payload.get("mode") or "").upper()
    if mode and mode != "PAPER":
        return False
    if row.get("closed_at") not in {None, "", "-"}:
        return False
    if row.get("result") not in {None, "", "-"}:
        return False
    status = str(row.get("status") or raw_payload.get("status") or "").upper()
    if status and status not in {"OPEN", "OPENED", "PAPER_OPEN"}:
        return False
    return True


def _is_open_demo_row(row: dict, magic_number: int) -> bool:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    row_magic = row.get("magic_number", row.get("magic", raw_payload.get("magic_number", raw_payload.get("magic"))))
    if _number(row_magic) != float(magic_number):
        return False
    symbol = str(row.get("display_symbol") or row.get("symbol") or raw_payload.get("symbol") or "").upper()
    if symbol not in {"GOLD", "GOLD#", "BTCUSD", "BTCUSD#", "EURUSD"}:
        return False
    if row.get("closed_at") not in {None, "", "-"}:
        return False
    if raw_payload.get("is_open") is False:
        return False
    result = str(row.get("result") or raw_payload.get("status") or "").upper()
    if result and result != "OPEN":
        return False
    return True


def _number(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _coerce_raw_payload(value: object) -> dict:
    if isinstance(value, dict):
        out = dict(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            out = {}
        else:
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"raw_payload_text": value}
            out = dict(parsed) if isinstance(parsed, dict) else {"raw_payload_value": parsed}
    elif value is None:
        out = {}
    else:
        return {"raw_payload_value": sanitize_for_json(value)}

    nested = out.get("raw_payload")
    if isinstance(nested, (dict, str)):
        nested_payload = _coerce_raw_payload(nested)
        out = {**nested_payload, **{key: item for key, item in out.items() if key != "raw_payload"}}
    return out


def _bot_log_snapshot(current_raw: dict, context: dict, latest_time_snapshot: dict | None) -> dict:
    snapshot: dict = {}
    if isinstance(latest_time_snapshot, dict):
        snapshot.update(latest_time_snapshot)
    context_time = context.get("time_engine") if isinstance(context, dict) else None
    if isinstance(context_time, dict):
        snapshot.update(context_time)
    snapshot.update({key: current_raw.get(key) for key in _BOT_LOG_TIME_FIELDS if current_raw.get(key) not in {None, ""}})
    if "market_open" not in snapshot and current_raw.get("symbol_market_open") not in {None, ""}:
        snapshot["market_open"] = current_raw.get("symbol_market_open")
    return snapshot


def _print_bot_log_time_debug(row: dict) -> None:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    print(
        '[BOT_LOG_TIME] message="%s" utc_time=%s casablanca_time=%s broker_time_estimate=%s session_name=%s time_gate_status=%s'
        % (
            str(row.get("message") or raw_payload.get("message") or "").replace('"', "'"),
            raw_payload.get("utc_time"),
            raw_payload.get("casablanca_time"),
            raw_payload.get("broker_time_estimate"),
            raw_payload.get("session_name"),
            raw_payload.get("time_gate_status"),
        )
    )


def sanitize_for_json(value: object) -> object:
    clean, supported = _sanitize(value)
    if not supported:
        return None
    return clean


def _bot_log_time_payload(snapshot: dict, settings: Settings) -> dict:
    utc_dt = datetime.now(timezone.utc)
    local_zone = _zone(getattr(settings, "timezone_local", None) or getattr(settings, "report_timezone", None) or "Africa/Casablanca")
    local_dt = utc_dt.astimezone(local_zone)
    broker_time = snapshot.get("broker_time_estimate") if isinstance(snapshot, dict) else None
    return {
        "utc_time": snapshot.get("utc_time") or utc_dt.isoformat(),
        "casablanca_time": snapshot.get("casablanca_time") or local_dt.isoformat(),
        "broker_time_estimate": broker_time if broker_time not in {None, ""} else "UNKNOWN",
        "broker_utc_offset_hours": snapshot.get("broker_utc_offset_hours", "UNKNOWN"),
        "local_hour": snapshot.get("local_hour", local_dt.hour),
        "utc_hour": snapshot.get("utc_hour", utc_dt.hour),
        "broker_hour": snapshot.get("broker_hour", "UNKNOWN"),
        "weekday": snapshot.get("weekday") or local_dt.strftime("%A").upper(),
        "session_name": snapshot.get("session_name", "UNKNOWN"),
        "asia_window": snapshot.get("asia_window", "NONE"),
        "asia_trading_allowed": snapshot.get("asia_trading_allowed", False),
        "asia_block_reason": snapshot.get("asia_block_reason"),
        "market_open": snapshot.get("market_open", snapshot.get("symbol_market_open", "UNKNOWN")),
        "is_weekend": snapshot.get("is_weekend", "UNKNOWN"),
        "is_bad_hour": snapshot.get("is_bad_hour", "UNKNOWN"),
        "time_gate_status": snapshot.get("time_gate_status", "UNKNOWN"),
        "time_gate_reason": snapshot.get("time_gate_reason", "UNKNOWN"),
    }


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Casablanca")


def _sanitize(value: object) -> tuple[object, bool]:
    if value is None or isinstance(value, (str, bool, int)):
        return value, True

    if isinstance(value, float):
        return (value, True) if math.isfinite(value) else (None, True)

    if isinstance(value, Decimal):
        as_float = float(value)
        return (as_float, True) if math.isfinite(as_float) else (None, True)

    if isinstance(value, (datetime, date)):
        return value.isoformat(), True

    if pd is not None:
        if isinstance(value, pd.Timestamp):
            return value.isoformat(), True
        try:
            if pd.isna(value):
                return None, True
        except (TypeError, ValueError):
            pass

    if np is not None:
        if isinstance(value, np.bool_):
            return bool(value), True
        if isinstance(value, np.integer):
            return int(value), True
        if isinstance(value, np.floating):
            as_float = float(value)
            return (as_float, True) if math.isfinite(as_float) else (None, True)
        if isinstance(value, np.ndarray):
            return _sanitize(value.tolist())

    if isinstance(value, dict):
        clean_dict = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            clean_item, supported = _sanitize(item)
            if supported:
                clean_dict[key] = clean_item
        return clean_dict, True

    if isinstance(value, (list, tuple, set)):
        clean_list = []
        for item in value:
            clean_item, supported = _sanitize(item)
            if supported:
                clean_list.append(clean_item)
        return clean_list, True

    return None, False


def _is_duplicate_key_response(body: str | None) -> bool:
    if not body:
        return False
    normalized = body.lower()
    duplicate_markers = [
        "duplicate key",
        "duplicate_key",
        "unique constraint",
        "conflict",
        "already exists",
        "23505",
    ]
    return any(marker in normalized for marker in duplicate_markers)


def _is_conflict_tolerated(table: str, status_code: int | None, body: str | None) -> bool:
    conflict_tolerated_tables = {"market_candles", "bot_status", "hermes_agents"}
    return table in conflict_tolerated_tables and (status_code == 409 or _is_duplicate_key_response(body))


def _failure(
    table: str,
    error: str,
    status_code: int | None = None,
    body: str | None = None,
    action: str = "insert",
) -> dict:
    return {"ok": False, "table": table, "action": action, "status_code": status_code, "error": error, "body": body}


def _is_not_found_response(body: str | None) -> bool:
    if not body:
        return False
    normalized = body.lower()
    markers = [
        "not found",
        "no existing",
        "no matching",
        "0 rows",
        "updated 0",
        '"updated":0',
        '"count":0',
    ]
    return any(marker in normalized for marker in markers)
