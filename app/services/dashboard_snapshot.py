from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.strategies.registry import (
    ACTIVE_EXECUTION_STRATEGIES,
    CONFIRMATION_MODULE_STRATEGIES,
    INTERNAL_DATA_FEED_STRATEGIES,
    OBSERVATION_STRATEGIES,
    StrategyClass,
    strategy_class,
    strategy_mode,
    strategy_role,
    allowed_for_symbol,
)


TIME_FIELDS = (
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


REQUIRED_DASHBOARD_STATUS_KEYS = (
    "mode",
    "demo_pilot_enabled",
    "demo_trading",
    "demo_only",
    "paper_trading",
    "allow_live_trading",
    "live_trading_blocked",
    "demo_magic_number",
    "demo_comment",
    "demo_max_lot",
    "demo_max_open_trades",
    "demo_max_trades_per_day",
    "demo_max_daily_loss_pct",
    "demo_max_risk_per_trade_pct",
    "account_login",
    "account_type",
    "account_trade_mode",
    "account_name",
    "account_server",
    "account_company",
    "mt5_connected",
    "trade_allowed",
    "trade_expert",
    "pilot_started_at",
    "pilot_expires_at",
    "pilot_hours_remaining",
    "last_demo_gate_decision",
    "last_demo_gate_reason",
    "last_demo_ticket",
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
    "time_session_blocks_disabled_warning",
    "hard_safety_still_active",
    "latest_symbol",
    "raw_symbol",
    "broker_symbol",
    "allowed_symbol_check",
    "hermes_trade_symbols",
    "hermes_analysis_only_symbols",
    "symbol_gate_status_by_symbol",
    "latest_symbol_gate_decision",
    "latest_symbol_gate_reason",
    "gold_liquidity_hunter",
    "gold_liquidity_labels",
    "eur_ema_rsi_atr",
    "eur_ema_rsi_atr_labels",
    "best_candidate_now",
    "setup_hunter_score",
    "setup_hunter_grade",
    "setup_hunter_missing",
    "edge_ready_candidates_count",
    "near_miss_candidates_count",
    "current_session_quality",
    "best_entry_strategy_now",
    "latest_near_miss_reason",
    "backend_started_at",
    "demo_exploration_mode",
    "latest_exploration_candidate",
    "latest_exploration_decision",
    "exploration_warnings",
    "exploration_trade_count_today",
    "backend_utc_time",
    "current_mt5_open_positions_count",
    "current_hermes_mt5_open_positions_count",
    "open_demo_trades_count",
    "demo_closed_pnl_today",
    "demo_floating_pnl",
    "demo_total_pnl_today",
    "latest_position_sync_time",
    "latest_heartbeat_written_at",
)


def dashboard_snapshot(
    settings: Settings,
    account: dict | None = None,
    time_snapshot: dict | None = None,
    latest_demo_event: dict | None = None,
    mt5_connected: bool = False,
    now: datetime | None = None,
    setup_hunter: dict | None = None,
    latest_position_sync: dict | None = None,
    order_flow_snapshots: dict | None = None,
    cycle_status: dict | None = None,
    per_symbol_state: dict | None = None,
    latest_candidates: list | None = None,
    latest_safety_guard: dict | None = None,
    ingest_health: dict | None = None,
) -> dict:
    account = account or {}
    latest_demo_event = latest_demo_event or {}
    now_dt = now or datetime.now(timezone.utc)
    time_payload = dashboard_time_payload(settings, time_snapshot, now_dt)
    trade_mode = account.get("trade_mode")
    account_type = _trade_mode_account_type(trade_mode) if trade_mode is not None else "NO_ACCOUNT"
    mode = _mode(settings)
    pilot_started = _parse_iso(settings.demo_pilot_started_at) or now_dt
    pilot_expires = pilot_started + timedelta(hours=max(0, int(settings.demo_pilot_hours)))
    hours_remaining = max(0.0, round((pilot_expires - now_dt).total_seconds() / 3600.0, 4))
    gates = latest_demo_event.get("gate_statuses") if isinstance(latest_demo_event.get("gate_statuses"), dict) else {}
    setup_hunter = setup_hunter or {}
    _sh_routeable = _is_routeable_setup_hunter(setup_hunter)
    latest_position_sync = latest_position_sync or {}
    live_snapshot = _live_snapshot_payload(latest_position_sync, now_dt)
    time_blocks_disabled = bool(
        getattr(settings, "demo_ignore_all_time_blocks", False)
        or getattr(settings, "demo_ignore_session_blocks", False)
        or getattr(settings, "demo_ignore_bad_hour_blocks", False)
    )
    session_quality = "EDGE_READY" if setup_hunter.get("demo_eligible") else ("NEAR_MISS" if setup_hunter.get("near_miss_reason") else "NO_EDGE")
    backend_started_at = _backend_started_at()
    exploration_candidate = latest_demo_event if latest_demo_event.get("exploration_decision") else (
        setup_hunter if setup_hunter.get("best_strategy") == "TREND_CONTINUATION_BREAKDOWN" else {}
    )
    symbol_gate_status = _symbol_gate_status_by_symbol(settings)
    gold_hunter = _gold_liquidity_payload(latest_demo_event, setup_hunter)
    eur_strategy = _eur_ema_rsi_atr_payload(latest_demo_event, setup_hunter)
    quant_pro = _quant_pro_payload(latest_demo_event, setup_hunter, settings)
    order_flow_payload = _order_flow_dashboard_payload(order_flow_snapshots or {}, latest_demo_event, setup_hunter, now_dt)
    max_money_tp = latest_demo_event.get("max_money_tp") if isinstance(latest_demo_event.get("max_money_tp"), dict) else {}
    strategy_manager_panel = _strategy_manager_panel(settings)
    confluence_card = _confluence_engine_card(setup_hunter)
    geometry_card = _geometry_card(setup_hunter)
    exec_agent_card = _order_flow_exec_agent_dashboard(setup_hunter, latest_demo_event, settings)
    account_block = _account_block(settings, account, account_type)
    cycle_status_block = _cycle_status_block(cycle_status)
    symbols_block = _symbols_block(per_symbol_state, settings, latest_candidates=latest_candidates)
    setup_hunter_block = _setup_hunter_telemetry_block(latest_candidates, setup_hunter)
    cm_block = _confirmation_matrix_telemetry_block(latest_candidates)
    safety_guard_block = _safety_guard_telemetry_block(latest_safety_guard)
    ingest_health_block = _lovable_ingest_health_telemetry_block(ingest_health, now_dt)
    fib_confluence_block = _fib_confluence_dashboard_block(settings, latest_candidates)
    strategy_pack_block = _strategy_pack_dashboard_block(settings, latest_candidates)
    balanced_selector_block = _balanced_selector_dashboard_block(settings, latest_candidates)
    risk_exposure_block = _risk_exposure_dashboard_block(live_snapshot, settings)
    return {
        "mode": mode,
        "demo_pilot_enabled": bool(settings.demo_pilot_enabled),
        "demo_trading": bool(settings.demo_trading),
        "demo_only": bool(settings.demo_only),
        "paper_trading": bool(settings.paper_trading),
        "allow_live_trading": bool(settings.allow_live_trading),
        "live_trading_blocked": not bool(settings.allow_live_trading),
        "demo_magic_number": settings.demo_magic_number,
        "demo_comment": settings.demo_comment,
        "demo_max_lot": settings.demo_max_lot,
        "demo_max_open_trades": settings.demo_max_open_trades,
        "demo_max_trades_per_day": settings.demo_max_trades_per_day,
        "demo_max_daily_loss_pct": settings.demo_max_daily_loss_pct,
        "demo_max_risk_per_trade_pct": settings.demo_max_risk_per_trade_pct,
        "max_money_tp": {
            "enabled": bool(getattr(settings, "max_money_tp_enabled", True)),
            "max_tp_usd": float(getattr(settings, "max_tp_usd", 2.0)),
            "sl_mode": "Dynamic by HERMES / Strategy",
            "tp_capped": bool(max_money_tp.get("applied")),
            "original_tp": max_money_tp.get("original_tp"),
            "final_tp": max_money_tp.get("final_tp"),
            "sl_unchanged": max_money_tp.get("sl_unchanged"),
        },
        "account_login": account.get("login"),
        "account_type": account_type,
        "account_trade_mode": trade_mode,
        "account_name": account.get("name"),
        "account_server": account.get("server"),
        "account_company": account.get("company"),
        "mt5_connected": bool(mt5_connected),
        "trade_allowed": account.get("trade_allowed"),
        "trade_expert": account.get("trade_expert"),
        "pilot_started_at": pilot_started.isoformat(),
        "pilot_expires_at": pilot_expires.isoformat(),
        "pilot_hours_remaining": hours_remaining,
        "last_demo_gate_decision": latest_demo_event.get("decision"),
        "last_demo_gate_reason": latest_demo_event.get("reason") or latest_demo_event.get("failed_gate"),
        "last_demo_ticket": latest_demo_event.get("ticket"),
        **time_payload,
        "time_session_blocks_disabled_warning": "TIME / SESSION BLOCKS DISABLED BY USER ORDER" if time_blocks_disabled else None,
        "hard_safety_still_active": [
            f"DEMO_ONLY {str(bool(settings.demo_only)).lower()}",
            "LIVE TRADING BLOCKED" if not bool(settings.allow_live_trading) else "LIVE TRADING NOT BLOCKED",
            f"MAX LOT {settings.demo_max_lot}",
        ],
        "latest_symbol": latest_demo_event.get("symbol") or gates.get("broker_symbol"),
        "raw_symbol": latest_demo_event.get("raw_symbol") or gates.get("raw_symbol"),
        "broker_symbol": latest_demo_event.get("broker_symbol") or gates.get("broker_symbol"),
        "allowed_symbol_check": latest_demo_event.get("allowed_symbol_check") or gates.get("allowed_symbol_check"),
        "hermes_trade_symbols": settings.trade_symbol_list,
        "hermes_analysis_only_symbols": settings.analysis_only_symbol_list,
        "symbol_gate_status_by_symbol": symbol_gate_status,
        "latest_symbol_gate_decision": latest_demo_event.get("symbol_gate_decision") or latest_demo_event.get("symbol_gate_status") or gates.get("symbol_gate_status"),
        "latest_symbol_gate_reason": latest_demo_event.get("symbol_gate_reason") or gates.get("symbol_gate_reason"),
        "gold_liquidity_hunter": {
            **gold_hunter,
            "mode": "ACTIVE_EXECUTION",
            "route_allowed": True,
            "stale": not bool(gold_hunter),
            "confluence_grade": _candidate_grade(latest_candidates, "GOLD"),
        },
        "gold_liquidity_labels": [
            "GOLD TRADES ONLY ON BSL/SSL SWEEP + ABS/REJ",
            "EXH/DIV OBSERVER ONLY",
            "GENERIC GOLD STRATEGIES DISABLED",
        ],
        "eur_ema_rsi_atr": {
            **eur_strategy,
            "mode": "ACTIVE_EXECUTION",
            "route_allowed": True,
            "stale": not bool(eur_strategy),
            "confluence_grade": _candidate_grade(latest_candidates, "EURUSD"),
        },
        "eur_ema_rsi_atr_labels": [
            "EURUSD TRADES ONLY WITH EMA CROSS + RSI + ATR",
        ],
        "account": account_block,
        "strategy_manager": strategy_manager_panel,
        "cycle_status": cycle_status_block,
        "symbols": symbols_block,
        "setup_hunter": setup_hunter_block,
        "confirmation_matrix": cm_block,
        "safety_guard": safety_guard_block,
        "lovable_ingest_health": ingest_health_block,
        "confluence_engine": confluence_card,
        "geometry_engine": geometry_card,
        "quant_pro_regime_switching": quant_pro,
        "order_flow": {**order_flow_payload, "execution_agent": exec_agent_card},
        "order_flow_badge": "ORDER FLOW = OBSERVE-ONLY INTELLIGENCE. IT DOES NOT BLOCK TRADES.",
        "order_flow_execution_agent": exec_agent_card,
        "backend_stale": bool(order_flow_payload.get("backend_stale")),
        "backend_stale_warning": "BACKEND STALE" if order_flow_payload.get("backend_stale") else None,
        "best_candidate_now": setup_hunter,
        "selected_candidate": setup_hunter if _sh_routeable else None,
        "blocked_best_candidate": setup_hunter if (setup_hunter and not _sh_routeable) else None,
        "rejected_best_candidate": setup_hunter if (setup_hunter and not _sh_routeable) else None,
        "setup_hunter_score": setup_hunter.get("edge_score"),
        "setup_hunter_grade": setup_hunter.get("grade"),
        "setup_hunter_missing": setup_hunter.get("what_is_missing_to_enter") or [],
        "edge_ready_candidates_count": 1 if setup_hunter.get("demo_eligible") else 0,
        "near_miss_candidates_count": 1 if setup_hunter.get("near_miss_reason") else 0,
        "current_session_quality": session_quality,
        "best_entry_strategy_now": setup_hunter.get("best_strategy"),
        "latest_near_miss_reason": setup_hunter.get("near_miss_reason"),
        "relaxed_mode_active": bool(latest_demo_event.get("relaxed_mode_active") or setup_hunter.get("relaxed_mode_active")),
        "relaxed_reason": latest_demo_event.get("relaxed_reason") or setup_hunter.get("relaxed_reason"),
        "hours_without_setup": latest_demo_event.get("hours_without_setup") or setup_hunter.get("hours_without_setup"),
        "strict_threshold": latest_demo_event.get("strict_threshold") or setup_hunter.get("strict_threshold"),
        "relaxed_threshold": latest_demo_event.get("relaxed_threshold") or setup_hunter.get("relaxed_threshold"),
        "relaxed_trade_count_today": latest_demo_event.get("relaxed_trade_count_today") or setup_hunter.get("relaxed_trade_count_today") or 0,
        "last_relaxed_trade_result": latest_demo_event.get("last_relaxed_trade_result") or setup_hunter.get("last_relaxed_trade_result"),
        "backend_started_at": backend_started_at,
        "demo_exploration_mode": bool(getattr(settings, "demo_exploration_mode", False)),
        "latest_exploration_candidate": exploration_candidate,
        "latest_exploration_decision": latest_demo_event.get("exploration_decision"),
        "exploration_warnings": latest_demo_event.get("exploration_warnings") or [],
        "exploration_trade_count_today": gates.get("daily_exploration_trades", 0),
        **live_snapshot,
        "fib_confluence": fib_confluence_block,
        "strategy_pack": strategy_pack_block,
        "balanced_selector": balanced_selector_block,
        "risk_exposure": risk_exposure_block,
    }


def _fib_confluence_dashboard_block(settings: Settings, candidates: list | None) -> dict:
    enabled = bool(getattr(settings, "fib_confluence_execution_enabled", False))
    fib = next(
        (c for c in (candidates or [])
         if str(c.get("best_strategy") or c.get("strategy") or "").upper() == "FIB_CONFLUENCE_EXECUTION_AGENT"),
        {},
    )
    return {
        "enabled": enabled,
        "mode": "ACTIVE_EXECUTION" if enabled else "DISABLED",
        "last_symbol": fib.get("symbol") or fib.get("broker_symbol"),
        "last_decision": fib.get("direction") or fib.get("signal") or "WAIT",
        "last_grade": fib.get("grade"),
        "last_score": fib.get("confidence") or fib.get("edge_score"),
        "last_rr": fib.get("rr"),
        "last_reason": fib.get("reason") or fib.get("blocked_reason"),
        "demo_eligible": bool(fib.get("demo_eligible")) if fib else False,
        "fib_zone": fib.get("fib_confluence", {}).get("zone_low") and {
            "zone_low": fib["fib_confluence"]["zone_low"],
            "zone_high": fib["fib_confluence"]["zone_high"],
            "fib_618": fib["fib_confluence"]["fib_618"],
        },
    }


def _strategy_pack_dashboard_block(settings: Settings, candidates: list | None) -> dict:
    enabled = bool(getattr(settings, "hermes_strategy_pack_enabled", True))
    pack = next(
        (c for c in (candidates or [])
         if str(c.get("best_strategy") or c.get("strategy") or "").upper() == "HERMES_STRATEGY_PACK_AGENT"),
        {},
    )
    return {
        "enabled": enabled,
        "mode": "WRAPPER_SIGNAL_SOURCE",
        "last_symbol": pack.get("symbol") or pack.get("broker_symbol"),
        "last_decision": pack.get("direction") or pack.get("signal") or "WAIT",
        "last_grade": pack.get("grade"),
        "last_score": pack.get("confidence") or pack.get("edge_score"),
        "last_internal": pack.get("setup_type"),
        "last_rr": pack.get("rr"),
        "demo_eligible": bool(pack.get("demo_eligible")) if pack else False,
    }


def _balanced_selector_dashboard_block(settings: Settings, latest_candidates: list | None = None) -> dict:
    def _rank_key(c: dict) -> tuple:
        grade_ranks = {"A_PLUS": 4, "A+": 4, "A": 3, "B": 2, "C": 1, "D": 0}
        grade = str(c.get("final_confluence_grade") or c.get("grade") or "").upper()
        score = 0.0
        for key in ("final_confluence_score", "edge_score", "confidence"):
            val = c.get(key)
            if val is not None:
                try:
                    score = float(val)
                    break
                except (TypeError, ValueError):
                    pass
        return (grade_ranks.get(grade, 0), score)

    candidates = [c for c in (latest_candidates or []) if isinstance(c, dict)]
    routeable = [c for c in candidates if _is_routeable_setup_hunter(c)]
    blocked = [
        c for c in candidates
        if not _is_routeable_setup_hunter(c)
        and str(c.get("best_strategy") or c.get("strategy") or "").upper() not in ("", "NONE")
    ]
    best_routeable = max(routeable, key=_rank_key) if routeable else None
    best_blocked = max(blocked, key=_rank_key) if blocked else None
    return {
        "best_candidate": _slim_candidate(best_routeable) if best_routeable else None,
        "best_blocked_candidate": _slim_candidate(best_blocked) if best_blocked else None,
        "max_total_open_demo_trades": getattr(settings, "demo_max_open_trades_total", 3),
        "max_open_per_symbol": getattr(settings, "demo_max_open_trades_per_symbol", 1),
        "demo_max_lot": settings.demo_max_lot,
        "demo_magic_number": settings.demo_magic_number,
        "fib_confluence_enabled": bool(getattr(settings, "fib_confluence_execution_enabled", False)),
        "strategy_pack_enabled": bool(getattr(settings, "hermes_strategy_pack_enabled", True)),
        "allowed_strategies": sorted([
            "BTC_SCALPING_AGENT",
            "EUR_EMA_RSI_ATR_CROSSOVER",
            "FIB_CONFLUENCE_EXECUTION_AGENT",
            "GOLD_LIQUIDITY_HUNTER_PRO",
            "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "HERMES_STRATEGY_PACK_AGENT",
            "ORDER_FLOW_EXECUTION_AGENT",
            "SIMO_ATM_BREAKOUT",
        ]),
        "no_d_grade_routing": True,
        "no_market_closed_routing": True,
    }


def _risk_exposure_dashboard_block(live_snapshot: dict, settings: Settings) -> dict:
    return {
        "open_demo_trades": live_snapshot.get("open_demo_trades_count", 0),
        "hermes_open_positions": live_snapshot.get("current_hermes_mt5_open_positions_count", 0),
        "demo_floating_pnl": live_snapshot.get("demo_floating_pnl", 0.0),
        "demo_closed_pnl_today": live_snapshot.get("demo_closed_pnl_today", 0.0),
        "demo_total_pnl_today": live_snapshot.get("demo_total_pnl_today", 0.0),
        "max_lot": settings.demo_max_lot,
        "max_total_open": getattr(settings, "demo_max_open_trades_total", 3),
        "max_per_symbol": getattr(settings, "demo_max_open_trades_per_symbol", 1),
        "max_daily_loss_pct": settings.demo_max_daily_loss_pct,
        "max_risk_per_trade_pct": settings.demo_max_risk_per_trade_pct,
        "allow_live_trading": bool(settings.allow_live_trading),
        "demo_only": bool(settings.demo_only),
    }


def dashboard_time_payload(settings: Settings, snapshot: dict | None = None, now: datetime | None = None) -> dict:
    snapshot = snapshot or {}
    utc_dt = now or datetime.now(timezone.utc)
    return _dashboard_time_payload(settings, snapshot, utc_dt)


def _dashboard_time_payload(settings: Settings, snapshot: dict, utc_dt: datetime) -> dict:
    local_zone = _zone(getattr(settings, "timezone_local", None) or getattr(settings, "report_timezone", None) or "Africa/Casablanca")
    local_dt = utc_dt.astimezone(local_zone)
    broker_time = snapshot.get("broker_time_estimate")
    return {
        "utc_time": utc_dt.isoformat(),
        "casablanca_time": local_dt.isoformat(),
        "latest_heartbeat_written_at": utc_dt.isoformat(),
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


def _mode(settings: Settings) -> str:
    from app.profiles.lovable_btc_old_system import is_active as _lvbtc_active, PROFILE_NAME as _LVBTC_NAME
    if _lvbtc_active(settings):
        return _LVBTC_NAME
    if settings.demo_trading and settings.demo_only and settings.demo_pilot_enabled:
        return f"DEMO_PILOT_{int(settings.demo_pilot_hours)}H"
    if settings.demo_trading:
        return "DEMO"
    if settings.paper_trading:
        return "PAPER"
    if settings.read_only:
        return "READ_ONLY"
    return "LIVE_DISABLED"


def _gold_liquidity_payload(latest_demo_event: dict, setup_hunter: dict) -> dict:
    for source in (latest_demo_event, setup_hunter):
        if isinstance(source.get("gold_liquidity_hunter"), dict):
            return source["gold_liquidity_hunter"]
        raw = source.get("raw_payload") if isinstance(source.get("raw_payload"), dict) else {}
        if isinstance(raw.get("gold_liquidity_hunter"), dict):
            return raw["gold_liquidity_hunter"]
    return {}


def _eur_ema_rsi_atr_payload(latest_demo_event: dict, setup_hunter: dict) -> dict:
    for source in (latest_demo_event, setup_hunter):
        if isinstance(source.get("eur_ema_rsi_atr"), dict):
            return source["eur_ema_rsi_atr"]
        raw = source.get("raw_payload") if isinstance(source.get("raw_payload"), dict) else {}
        if isinstance(raw.get("eur_ema_rsi_atr"), dict):
            return raw["eur_ema_rsi_atr"]
        gates = source.get("gate_statuses") if isinstance(source.get("gate_statuses"), dict) else {}
        if isinstance(gates.get("eur_ema_rsi_atr"), dict):
            return gates["eur_ema_rsi_atr"]
    return {}


def _quant_pro_payload(latest_demo_event: dict, setup_hunter: dict, settings: Settings) -> dict:
    for source in (latest_demo_event, setup_hunter):
        if isinstance(source.get("quant_pro_hurst_filter"), dict):
            payload = dict(source["quant_pro_hurst_filter"])
        else:
            raw = source.get("raw_payload") if isinstance(source.get("raw_payload"), dict) else {}
            payload = dict(raw.get("quant_pro_hurst_filter")) if isinstance(raw.get("quant_pro_hurst_filter"), dict) else {}
        if payload:
            return {
                "hurst": payload.get("hurst", source.get("quant_pro_hurst")),
                "min_hurst_required": payload.get("min_trend_hurst", getattr(settings, "quant_pro_min_trend_hurst", 0.90)),
                "trend_strength": payload.get("trend_strength", source.get("quant_pro_trend_strength") or "UNKNOWN"),
                "hurst_filter": "PASS" if payload.get("passed") else ("BLOCK" if payload.get("block_reason") else "UNKNOWN"),
                "block_reason": payload.get("block_reason"),
            }
        if source.get("strategy") == "QUANT_PRO_REGIME_SWITCHING" or source.get("quant_pro_hurst") is not None:
            return {
                "hurst": source.get("quant_pro_hurst"),
                "min_hurst_required": source.get("quant_pro_min_trend_hurst") or getattr(settings, "quant_pro_min_trend_hurst", 0.90),
                "trend_strength": source.get("quant_pro_trend_strength") or "UNKNOWN",
                "hurst_filter": source.get("quant_pro_hurst_filter_status") or "UNKNOWN",
                "block_reason": source.get("quant_pro_hurst_block_reason"),
            }
    return {
        "hurst": None,
        "min_hurst_required": getattr(settings, "quant_pro_min_trend_hurst", 0.90),
        "trend_strength": "UNKNOWN",
        "hurst_filter": "UNKNOWN",
        "block_reason": None,
    }


def _order_flow_dashboard_payload(snapshots: dict, latest_demo_event: dict, setup_hunter: dict, now_dt: datetime) -> dict:
    by_symbol = {}
    for key, value in (snapshots or {}).items():
        if isinstance(value, dict):
            canonical = _canonical_symbol(value.get("symbol") or value.get("broker_symbol") or key)
            by_symbol[canonical] = _order_flow_card(value, now_dt)
    for source in (latest_demo_event, setup_hunter):
        for key in ("order_flow_snapshot", "order_flow_reader"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, dict):
                canonical = _canonical_symbol(value.get("symbol") or value.get("broker_symbol"))
                by_symbol.setdefault(canonical, _order_flow_card(value, now_dt))
    tabs = {
        "BTCUSD": by_symbol.get("BTCUSD") or _empty_order_flow_card("BTCUSD", now_dt),
        "GOLD": by_symbol.get("GOLD") or _empty_order_flow_card("GOLD", now_dt),
        "EURUSD": by_symbol.get("EURUSD") or _empty_order_flow_card("EURUSD", now_dt),
    }
    detected = {
        key: value
        for key, value in by_symbol.items()
        if key not in tabs and key
    }
    backend_stale = any(card.get("stale") for card in tabs.values())
    return {
        "mode": "OBSERVE_ONLY",
        "badge": "ORDER FLOW = OBSERVE-ONLY INTELLIGENCE. IT DOES NOT BLOCK TRADES.",
        "tabs": tabs,
        "detected_symbols": detected,
        "backend_stale": backend_stale,
        "stale_after_seconds": 60,
    }


def _order_flow_card(snapshot: dict, now_dt: datetime) -> dict:
    created_at = snapshot.get("created_at")
    age = _age_seconds(created_at, now_dt)
    stale = age is None or age > 60
    return {
        "symbol": _canonical_symbol(snapshot.get("symbol") or snapshot.get("broker_symbol")),
        "broker_symbol": snapshot.get("broker_symbol"),
        "timeframe": snapshot.get("timeframe") or "M5",
        "price": snapshot.get("price"),
        "vwap": snapshot.get("vwap"),
        "poc": snapshot.get("poc"),
        "vah": snapshot.get("vah"),
        "val": snapshot.get("val"),
        "cvd_proxy": snapshot.get("cvd_proxy"),
        "cvd_slope": snapshot.get("cvd_slope"),
        "delta_proxy": snapshot.get("delta_proxy"),
        "buy_pressure": snapshot.get("buy_pressure"),
        "sell_pressure": snapshot.get("sell_pressure"),
        "divergence": snapshot.get("divergence"),
        "confidence": snapshot.get("confidence"),
        "signal": snapshot.get("signal"),
        "status": "STALE" if stale else snapshot.get("status"),
        "warnings": list(snapshot.get("warnings") or []) + (["BACKEND_STALE"] if stale else []),
        "source": snapshot.get("source"),
        "mode": "OBSERVE_ONLY",
        "created_at": created_at,
        "last_update": created_at,
        "age_seconds": age,
        "stale": stale,
        "chart_series": {
            "price": snapshot.get("price"),
            "vwap": snapshot.get("vwap"),
            "poc": snapshot.get("poc"),
            "vah": snapshot.get("vah"),
            "val": snapshot.get("val"),
            "cvd_proxy": snapshot.get("cvd_proxy"),
            "delta_proxy": snapshot.get("delta_proxy"),
            "buy_pressure": snapshot.get("buy_pressure"),
            "sell_pressure": snapshot.get("sell_pressure"),
            "divergence": snapshot.get("divergence"),
            "confidence": snapshot.get("confidence"),
        },
    }


def _empty_order_flow_card(symbol: str, now_dt: datetime) -> dict:
    return _order_flow_card(
        {
            "symbol": symbol,
            "broker_symbol": symbol,
            "timeframe": "M5",
            "status": "STALE",
            "warnings": ["ORDER_FLOW_MISSING"],
            "created_at": None,
        },
        now_dt,
    )


def _order_flow_exec_agent_dashboard(setup_hunter: dict, latest_demo_event: dict, settings: Settings) -> dict:
    enabled = bool(getattr(settings, "order_flow_execution_enabled", False))
    sh = setup_hunter or {}
    ev = latest_demo_event or {}
    strategy = str(sh.get("best_strategy") or ev.get("strategy") or "").upper()
    is_of_agent = strategy == "ORDER_FLOW_EXECUTION_AGENT"
    source = sh if is_of_agent else {}
    ev_source = ev if str(ev.get("strategy") or "").upper() == "ORDER_FLOW_EXECUTION_AGENT" else {}
    return {
        "enabled": enabled,
        "mode": "ACTIVE_EXECUTION",
        "last_symbol": source.get("symbol") or ev_source.get("symbol"),
        "last_setup": source.get("setup_type") or ev_source.get("setup_type"),
        "last_direction": source.get("direction") or ev_source.get("direction"),
        "last_score": source.get("order_flow_execution_agent_score") or source.get("edge_score") or ev_source.get("order_flow_execution_agent_score"),
        "last_grade": source.get("grade") or ev_source.get("grade"),
        "last_reason": source.get("order_flow_execution_agent_reason") or source.get("reason") or ev_source.get("reason"),
        "last_route_decision": ev_source.get("decision") or (source.get("direction") if source.get("demo_eligible") else None),
        "last_update": source.get("created_at") or ev_source.get("created_at"),
    }


def _age_seconds(created_at: object, now_dt: datetime) -> float | None:
    parsed = _parse_iso(str(created_at or ""))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, round((now_dt - parsed).total_seconds(), 3))


def _symbol_gate_status_by_symbol(settings: Settings) -> dict:
    trade = {_canonical_symbol(item) for item in settings.trade_symbol_list}
    analysis_only = {_canonical_symbol(item) for item in settings.analysis_only_symbol_list}
    symbols = list(dict.fromkeys(settings.symbol_list + settings.trade_symbol_list + settings.analysis_only_symbol_list))
    out = {}
    for symbol in symbols:
        canonical = _canonical_symbol(symbol)
        if canonical in analysis_only:
            decision = "BLOCK"
            reason = "SYMBOL_ANALYSIS_ONLY"
        elif canonical in trade:
            decision = "PASS"
            reason = "SYMBOL_ALLOWED_FOR_DEMO"
        else:
            decision = "BLOCK"
            reason = "SYMBOL_NOT_ALLOWED"
        out[str(symbol or "").upper()] = {"decision": decision, "reason": reason, "canonical_symbol": canonical}
    return out


def _canonical_symbol(symbol: object) -> str:
    normalized = str(symbol or "").upper().strip()
    if normalized in {"BTCUSD", "BTCUSD#"} or normalized.startswith("BTCUSD"):
        return "BTCUSD"
    if normalized in {"GOLD", "GOLD#", "XAUUSD", "XAUUSD#"} or normalized.startswith("GOLD") or normalized.startswith("XAUUSD"):
        return "GOLD"
    if normalized.startswith("EURUSD"):
        return "EURUSD"
    return normalized.replace("#", "")


def _account_block_reason(settings: Settings, account_type: str, account: dict) -> str | None:
    if account_type == "LIVE":
        return "ACCOUNT_TRADE_MODE_REAL"
    if account_type == "CONTEST" and not settings.demo_allow_contest:
        return "ACCOUNT_TRADE_MODE_CONTEST_BLOCKED"
    if account_type == "UNKNOWN":
        return "ACCOUNT_TRADE_MODE_UNKNOWN"
    if account.get("trade_allowed") is False:
        return "TRADE_ALLOWED_FALSE"
    if account.get("trade_expert") is False:
        return "TRADE_EXPERT_FALSE"
    allowed_login = str(settings.demo_allowed_login or "").strip()
    if allowed_login and str(account.get("login") or "") != allowed_login:
        return "LOGIN_NOT_ALLOWLISTED"
    return None


def _strategy_manager_panel(settings: Settings) -> dict:
    """Three-way classification panel — no MT5 call needed."""

    def _enabled(name: str) -> bool:
        if name == "GOLD_ORDER_FLOW_CVD_VWAP":
            return bool(getattr(settings, "gold_order_flow_execution_enabled", False))
        if name == "ORDER_FLOW_EXECUTION_AGENT":
            return bool(getattr(settings, "order_flow_execution_enabled", False))
        if name == "SIMO_ATM_BREAKOUT":
            return bool(getattr(settings, "simo_atm_breakout_enabled", True))
        return True

    def _route_allowed(name: str, cls: StrategyClass) -> bool:
        if cls != StrategyClass.ACTIVE_EXECUTION_STRATEGY:
            return False
        return _enabled(name)

    active = sorted(ACTIVE_EXECUTION_STRATEGIES)
    confirm = sorted(CONFIRMATION_MODULE_STRATEGIES)
    feeds = sorted(INTERNAL_DATA_FEED_STRATEGIES)
    observation = sorted(OBSERVATION_STRATEGIES)  # backward-compat view

    route_allowed_by_strategy: dict[str, bool] = {}
    enabled_by_strategy: dict[str, bool] = {}
    rows: dict[str, dict] = {}

    for s in active:
        cls = StrategyClass.ACTIVE_EXECUTION_STRATEGY
        en = _enabled(s)
        ra = _route_allowed(s, cls)
        route_allowed_by_strategy[s] = ra
        enabled_by_strategy[s] = en
        rows[s] = {
            "class": cls.value,
            "mode": "ACTIVE_EXECUTION",
            "route_allowed": ra,
            "enabled": en,
            "role": strategy_role(s),
        }

    for s in confirm:
        route_allowed_by_strategy[s] = False
        enabled_by_strategy[s] = True
        rows[s] = {
            "class": StrategyClass.CONFIRMATION_MODULE.value,
            "mode": "OBSERVATION_ONLY",
            "route_allowed": False,
            "enabled": True,
            "role": strategy_role(s),
        }

    for s in feeds:
        route_allowed_by_strategy[s] = False
        enabled_by_strategy[s] = True
        rows[s] = {
            "class": StrategyClass.INTERNAL_DATA_FEED.value,
            "mode": "OBSERVATION_ONLY",
            "route_allowed": False,
            "enabled": True,
            "role": strategy_role(s),
        }

    return {
        # Three-way classification (Phase 2 canonical)
        "active_execution_strategies": active,
        "confirmation_modules": confirm,
        "internal_data_feeds": feeds,
        "route_allowed_by_strategy": route_allowed_by_strategy,
        "enabled_by_strategy": enabled_by_strategy,
        # Backward-compatible counters
        "active_execution_count": len(active),
        "observation_only_count": len(observation),
        "total_strategies": len(active) + len(confirm) + len(feeds),
        "simo_atm_breakout_enabled": bool(getattr(settings, "simo_atm_breakout_enabled", False)),
        "strategy_manager_enabled": bool(getattr(settings, "strategy_manager_enabled", True)),
        "strategies": rows,
        "badge": "OBSERVATION STRATEGIES AND MODULES NEVER ROUTE TO EXECUTION",
    }


def _confluence_engine_card(setup_hunter: dict) -> dict:
    """Extract ConfluenceEngine output from the best candidate dict."""
    score = _float_value(setup_hunter.get("confluence_score"))
    grade = setup_hunter.get("confluence_grade")
    recommendation = setup_hunter.get("confluence_recommendation")
    components = setup_hunter.get("confluence_components")
    trend = setup_hunter.get("confluence_trend_direction") or setup_hunter.get("trend_direction")
    ote = setup_hunter.get("ote_zone")
    pd_zone = setup_hunter.get("pd_zone")
    atr = _float_value(setup_hunter.get("atr"))
    swing_high = _float_value(setup_hunter.get("swing_high"))
    swing_low = _float_value(setup_hunter.get("swing_low"))
    stale = score is None
    return {
        "score": score,
        "grade": grade,
        "recommendation": recommendation,
        "trend_direction": trend,
        "ote_zone": ote,
        "pd_zone": pd_zone,
        "atr": atr,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "components": components,
        "stale": stale,
        "mode": "ENRICH_ONLY",
        "badge": "CONFLUENCE ENGINE = SCORING ONLY. DOES NOT EXECUTE.",
    }


def _geometry_card(setup_hunter: dict) -> dict:
    """Extract geometry engine outputs from the best candidate dict."""
    _geo = setup_hunter.get("geo_result") or {}
    return {
        # Quant geometry engine (existing)
        "range_compression": _float_value(setup_hunter.get("range_compression_score")),
        "impulse_score": _float_value(setup_hunter.get("impulse_score")),
        "volatility_expansion": _float_value(setup_hunter.get("volatility_expansion_score")),
        "geometric_confluence": _float_value(setup_hunter.get("geometric_confluence_score")),
        "triangle_pattern": setup_hunter.get("triangle_pattern"),
        "channel_direction": setup_hunter.get("channel_direction"),
        "channel_r_squared": _float_value(setup_hunter.get("channel_r_squared")),
        "mode": "OBSERVE_ONLY",
        # §v1.6 harmonic / Fibonacci geometric confluence layer
        "geometric_confluence_layer": {
            "enabled": bool(_geo),
            "mode": setup_hunter.get("geometric_confluence_mode") or (_geo.get("mode") or "SHADOW"),
            "harmonic_pattern":    _geo.get("harmonic_pattern") or "NONE",
            "harmonic_quality":    _geo.get("harmonic_quality") or "D",
            "geometric_score":     _float_value(_geo.get("geometric_score")),
            "geometric_grade":     _geo.get("geometric_grade") or "D",
            "decision":            _geo.get("decision") or "WAIT",
            "fib_retracement_level": _geo.get("fib_retracement_level"),
            "fib_extension_level":   _geo.get("fib_extension_level"),
            "abcd_projection_level": _geo.get("abcd_projection_level"),
            "confluence_zone_count": _geo.get("confluence_zone_count") or 0,
            "confluence_zone_price": _geo.get("confluence_zone_price"),
            "gann_angle":            _geo.get("gann_angle") or "NONE",
            "spiral_confluence_hit": bool(_geo.get("spiral_confluence_hit")),
            "would_be_bonus":        _float_value(setup_hunter.get("geo_would_be_bonus")),
            "reason":                _geo.get("reason"),
        },
    }


def dashboard_status_row(payload: dict, now: datetime | None = None) -> dict:
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "bot_name": "HERMES_5MIN_AGENT",
        "component": "dashboard_status",
        "status": "RUNNING",
        "mode": payload.get("mode"),
        "read_only": payload.get("mode") == "READ_ONLY",
        "paper_trading": payload.get("paper_trading"),
        "demo_trading": payload.get("demo_trading"),
        "allow_live_trading": payload.get("allow_live_trading"),
        "magic_number": payload.get("demo_magic_number"),
        "updated_at": now_iso,
        "raw_payload": payload,
        "payload": payload,
        "status_json": payload,
    }


def compact_dashboard_status_row(payload: dict, now: datetime | None = None, json_field: str = "raw_payload") -> dict:
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "bot_name": "HERMES_5MIN_AGENT",
        "component": "dashboard_status",
        "status": "RUNNING",
        "mode": payload.get("mode"),
        "updated_at": now_iso,
        json_field: payload,
    }


def dashboard_status_debug_line(payload: dict) -> str:
    return (
        "[DASHBOARD_STATUS] component=dashboard_status mode=%s account_type=%s "
        "demo_pilot_enabled=%s allow_live_trading=%s utc_time=%s"
        % (
            payload.get("mode"),
            payload.get("account_type"),
            str(bool(payload.get("demo_pilot_enabled"))).lower(),
            str(bool(payload.get("allow_live_trading"))).lower(),
            payload.get("utc_time"),
        )
    )


def live_snapshot_debug_line(payload: dict) -> str:
    return (
        "[LIVE_SNAPSHOT] utc_time=%s open=%s closed_pnl=%.2f floating_pnl=%.2f total=%.2f source=%s"
        % (
            payload.get("utc_time"),
            payload.get("open_demo_trades_count") or 0,
            _float_value(payload.get("demo_closed_pnl_today")) or 0.0,
            _float_value(payload.get("demo_floating_pnl")) or 0.0,
            _float_value(payload.get("demo_total_pnl_today")) or 0.0,
            payload.get("pnl_source") or "UNKNOWN",
        )
    )


def _live_snapshot_payload(latest_position_sync: dict, now_dt: datetime) -> dict:
    closed_pnl = _float_value(latest_position_sync.get("demo_closed_pnl_today")) or 0.0
    floating_pnl = _float_value(latest_position_sync.get("demo_floating_pnl")) or 0.0
    hermes_open = _int_value(latest_position_sync.get("hermes_mt5_open_positions_count"))
    open_demo = _int_value(latest_position_sync.get("open_demo_trades_count"))
    latest_sync_time = latest_position_sync.get("latest_position_sync_time")
    if latest_sync_time is None:
        latest_event = latest_position_sync.get("latest_position_sync") or latest_position_sync.get("latest_position_close")
        if isinstance(latest_event, dict):
            latest_sync_time = latest_event.get("created_at")
    return {
        "backend_utc_time": now_dt.isoformat(),
        "current_mt5_open_positions_count": _int_value(latest_position_sync.get("mt5_open_positions_count")) or 0,
        "current_hermes_mt5_open_positions_count": hermes_open or 0,
        "open_demo_trades_count": open_demo if open_demo is not None else (hermes_open or 0),
        "demo_closed_pnl_today": round(closed_pnl, 6),
        "demo_floating_pnl": round(floating_pnl, 6),
        "demo_total_pnl_today": round(closed_pnl + floating_pnl, 6),
        "mt5_today_pnl": latest_position_sync.get("mt5_today_pnl"),
        "mt5_48h_pnl": latest_position_sync.get("mt5_48h_pnl"),
        "mt5_gross_profit": latest_position_sync.get("mt5_gross_profit"),
        "mt5_gross_loss": latest_position_sync.get("mt5_gross_loss"),
        "mt5_profit_factor": latest_position_sync.get("mt5_profit_factor"),
        "mt5_history_error": latest_position_sync.get("mt5_history_error"),
        "mt5_initialized": latest_position_sync.get("mt5_initialized"),
        "mt5_last_error": latest_position_sync.get("mt5_last_error"),
        "history_start": latest_position_sync.get("history_start"),
        "history_end": latest_position_sync.get("history_end"),
        "deals_total_before_magic_filter": latest_position_sync.get("deals_total_before_magic_filter"),
        "deals_total_after_magic_filter": latest_position_sync.get("deals_total_after_magic_filter"),
        "pnl_source": latest_position_sync.get("pnl_source"),
        "mt5_closed_deals_count": latest_position_sync.get("mt5_closed_deals_count"),
        "trades_table_pnl": latest_position_sync.get("trades_table_pnl"),
        "pnl_difference": latest_position_sync.get("pnl_difference"),
        "pnl_warning": latest_position_sync.get("pnl_warning"),
        "latest_position_sync_time": latest_sync_time,
    }


def _float_value(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_mode_account_type(trade_mode: object) -> str:
    try:
        mode = int(trade_mode)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if mode == 0:
        return "DEMO"
    if mode == 1:
        return "CONTEST"
    if mode == 2:
        return "LIVE"
    return "UNKNOWN"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Casablanca")


def _account_block(settings: Settings, account: dict, account_type: str) -> dict:
    return {
        "account_type": account_type,
        "server": account.get("server"),
        "allow_live_trading": bool(settings.allow_live_trading),
        "demo_only": bool(settings.demo_only),
        "demo_max_lot": settings.demo_max_lot,
        "magic": settings.demo_magic_number,
    }


def _cycle_status_block(cycle_status: dict | None) -> dict:
    cs = cycle_status or {}
    return {
        "last_cycle_start_utc": cs.get("last_cycle_start_utc"),
        "last_cycle_end_utc": cs.get("last_cycle_end_utc"),
        "analyzed": cs.get("analyzed", 0),
        "skipped": cs.get("skipped", 0),
        "demo_orders": cs.get("demo_orders", 0),
        "last_status": cs.get("last_status", "UNKNOWN"),
    }


def _is_routeable_setup_hunter(c: dict | None) -> bool:
    """True when a SetupHunter best_candidate is safe to surface as selected_candidate."""
    if not c or not c.get("demo_eligible"):
        return False
    strategy = str(c.get("best_strategy") or c.get("strategy") or "").upper()
    if strategy in ("", "NONE"):
        return False
    grade = str(c.get("final_confluence_grade") or c.get("grade") or "").upper()
    if grade == "D":
        return False
    market = c.get("market_open", c.get("symbol_market_open", True))
    if str(market).upper() == "FALSE" or market is False:
        return False
    return True


def _candidate_grade(candidates: list | None, canonical_sym: str) -> str | None:
    """Return the confluence_grade of the highest-scoring candidate for *canonical_sym*."""
    best_grade: str | None = None
    best_score = -1.0
    for c in (candidates or []):
        if not isinstance(c, dict):
            continue
        sym = _canonical_symbol(str(c.get("symbol") or c.get("broker_symbol") or ""))
        if sym != canonical_sym:
            continue
        score = _float_value(c.get("edge_score") or c.get("confidence")) or 0.0
        if score >= best_score:
            best_score = score
            best_grade = c.get("confluence_grade")
    return best_grade


def _symbols_block(
    per_symbol_state: dict | None,
    settings: Settings,
    latest_candidates: list | None = None,
) -> dict:
    """Build the symbols block using exact configured keys from hermes_main_symbol_list.

    Keys preserve exact case (e.g. US100Cash#, not US100CASH#).
    State is looked up by aliases so BTCUSD state appears under BTCUSD# key.
    All configured symbols have in_main_cycle=True regardless of hermes_trade_symbols.
    Unavailable configured symbols emit NO_DATA / WAIT / UNAVAILABLE_OR_NO_RATES.

    Phase 9 additions
    -----------------
    mode            — "ACTIVE_EXECUTION" for trade symbols, "OBSERVATION_ONLY" for
                      analysis-only symbols.
    route_allowed   — True only when the symbol is in trade_symbol_list and NOT in
                      analysis_only_symbol_list.
    confluence_grade — Grade string from the best candidate for this symbol ("A", "B", …)
                       or None when no candidate is available.
    stale           — True when last_update_utc is missing or older than 30 s.
    """
    state = per_symbol_state or {}
    main_syms = list(settings.hermes_main_symbol_list)
    trade_set = {_canonical_symbol(s) for s in settings.trade_symbol_list}
    analysis_set = {_canonical_symbol(s) for s in settings.analysis_only_symbol_list}
    # Grade lookup by canonical symbol (first/highest-scored candidate wins)
    grade_by_sym: dict[str, str | None] = {}
    for c in (latest_candidates or []):
        if not isinstance(c, dict):
            continue
        canon = _canonical_symbol(str(c.get("symbol") or c.get("broker_symbol") or ""))
        if canon and canon not in grade_by_sym:
            grade_by_sym[canon] = c.get("confluence_grade")
    now_dt = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for cfg_sym in main_syms:
        if cfg_sym in out:
            continue
        entry = _find_state_for_configured_sym(state, cfg_sym)
        available = bool(entry)
        canonical = _canonical_symbol(cfg_sym)
        in_analysis = canonical in analysis_set
        in_trade = canonical in trade_set
        route_allowed = in_trade and not in_analysis
        mode = "OBSERVATION_ONLY" if in_analysis else "ACTIVE_EXECUTION"
        last_update = entry.get("last_update_utc") if available else None
        age = _age_seconds(last_update, now_dt) if last_update else None
        stale = age is None or age > 30
        out[cfg_sym] = {
            "enabled": True,
            "available": available,
            "in_main_cycle": True,
            "mode": mode,
            "route_allowed": route_allowed,
            "broker_symbol": entry.get("broker_symbol") or cfg_sym,
            "price": entry.get("price"),
            "spread": entry.get("spread"),
            "spread_status": entry.get("spread_status"),
            "session": entry.get("session"),
            "time_gate": entry.get("time_gate"),
            "latest_decision": entry.get("latest_decision") if available else "WAIT",
            "latest_reason": entry.get("latest_reason") if available else "UNAVAILABLE_OR_NO_RATES",
            "route_status": entry.get("route_status") if available else "NO_DATA",
            "last_update_utc": last_update,
            "confluence_grade": grade_by_sym.get(canonical),
            "stale": stale,
        }
    return out


def _find_state_for_configured_sym(state: dict, cfg_sym: str) -> dict:
    """Return per-symbol state for a configured symbol, trying known aliases."""
    for alias in _symbol_state_aliases(cfg_sym):
        if alias in state:
            return state[alias]
    return {}


def _symbol_state_aliases(cfg_sym: str) -> list[str]:
    """Return ordered list of state-dict keys to try for a configured symbol key."""
    upper = cfg_sym.upper()
    without_hash = upper.replace("#", "")
    seen: set[str] = set()
    result: list[str] = []

    def _add(v: str) -> None:
        if v not in seen:
            seen.add(v)
            result.append(v)

    _add(upper)
    _add(without_hash)
    if without_hash.startswith("BTCUSD"):
        _add("BTCUSD#")
        _add("BTCUSD")
    elif without_hash.startswith("GOLD") or without_hash.startswith("XAUUSD"):
        _add("GOLD#")
        _add("GOLD")
        _add("XAUUSD#")
        _add("XAUUSD")
    elif without_hash.startswith("EURUSD"):
        _add("EURUSD#")
        _add("EURUSD")
    elif "US100" in without_hash or "NAS100" in without_hash or "USTEC" in without_hash or "NASDAQ" in without_hash:
        _add("US100CASH#")
        _add("US100CASH")
        _add("US100#")
        _add("US100")
        _add("NAS100")
        _add("USTEC")
        _add("NASDAQ")
    return result


def _slim_candidate(c: dict) -> dict:
    failed = (c.get("failed_gates") or c.get("eligibility_block_reasons") or [])[:4]
    return {
        "symbol": c.get("symbol"),
        "broker_symbol": c.get("broker_symbol"),
        "strategy": c.get("best_strategy"),
        "direction": c.get("direction"),
        "grade": c.get("grade"),
        "score": c.get("edge_score"),
        "demo_eligible": c.get("demo_eligible"),
        "raw_strategy_grade": c.get("raw_strategy_grade"),
        "final_verdict": c.get("final_verdict"),
        "final_verdict_reason": c.get("final_verdict_reason"),
        "near_miss_reason": c.get("near_miss_reason"),
        "failed_gates": failed,
        "smc_score": c.get("smc_score"),
        "mtfa_score": c.get("mtfa_score"),
        "entry": c.get("entry"),
        "sl": c.get("sl"),
        "tp": c.get("tp"),
        "rr": c.get("rr"),
        # §v1.6 geometric confluence fields
        "geometric_score":           c.get("geometric_score"),
        "geometric_grade":           c.get("geometric_grade"),
        "geometric_decision":        c.get("geometric_decision"),
        "harmonic_pattern":          c.get("harmonic_pattern"),
        "geometric_confluence_mode": c.get("geometric_confluence_mode"),
        "geo_would_be_bonus":        c.get("geo_would_be_bonus"),
        "geo_fib_retracement_level": (c.get("geo_result") or {}).get("fib_retracement_level"),
        "geo_confluence_zone_count": (c.get("geo_result") or {}).get("confluence_zone_count"),
    }


def _setup_hunter_telemetry_block(latest_candidates: list | None, best_candidate: dict | None) -> dict:
    by_symbol_strategy: list[dict] = []
    seen: set[tuple] = set()
    for c in (latest_candidates or []):
        if not isinstance(c, dict):
            continue
        key = (c.get("symbol"), c.get("best_strategy"))
        if key in seen:
            continue
        seen.add(key)
        by_symbol_strategy.append(_slim_candidate(c))
    return {
        "latest_by_symbol_strategy": by_symbol_strategy,
        "best_candidate": _slim_candidate(best_candidate) if isinstance(best_candidate, dict) else None,
    }


def _confirmation_matrix_telemetry_block(latest_candidates: list | None) -> dict:
    by_symbol_strategy: list[dict] = []
    seen: set[tuple] = set()
    for c in (latest_candidates or []):
        if not isinstance(c, dict):
            continue
        key = (c.get("symbol"), c.get("best_strategy"))
        if key in seen:
            continue
        seen.add(key)
        hard_block = "CONFIRMATION_MATRIX_HARD_BLOCK" in (c.get("failed_gates") or [])
        smc_score = _float_value(c.get("smc_score")) or 0.0
        mtfa_score = _float_value(c.get("mtfa_score")) or 0.0
        smc_soft = 40.0 <= smc_score < 70.0
        mtfa_soft = 35.0 <= mtfa_score < 60.0
        has_warning = not hard_block and (smc_soft or mtfa_soft)
        status = "BLOCK" if hard_block else ("WARN" if has_warning else "PASS")
        by_symbol_strategy.append({
            "symbol": c.get("symbol"),
            "strategy": c.get("best_strategy"),
            "smc_score": smc_score,
            "mtfa_score": mtfa_score,
            "hard_block": hard_block,
            "status": status,
        })
    return {"latest_by_symbol_strategy": by_symbol_strategy}


def _safety_guard_telemetry_block(latest_safety_guard: dict | None) -> dict:
    sg = latest_safety_guard or {}
    return {
        "last_status": sg.get("last_status"),
        "last_reason": sg.get("last_reason"),
        "last_update_utc": sg.get("last_update_utc"),
    }


def _lovable_ingest_health_telemetry_block(ingest_health: dict | None, now_dt: datetime | None = None) -> dict:
    ih = ingest_health or {}
    return {
        "status": ih.get("status", "UNKNOWN"),
        "reason": ih.get("reason"),
        "last_update_utc": ih.get("last_update_utc") or (now_dt.isoformat() if now_dt else None),
    }


def _backend_started_at() -> str | None:
    try:
        from app.mt5.demo_router import read_backend_started_at

        marker = read_backend_started_at()
        return marker.isoformat() if marker else None
    except Exception:
        return None
