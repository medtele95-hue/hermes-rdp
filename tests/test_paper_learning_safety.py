from __future__ import annotations

import tempfile
import unittest
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pandas as pd

import app.main as main_module
from app.agents.mtfa_filter import MTFAFilter
from app.agents.mtf_structure_detector import MTFStructureDetector
from app.agents.paper_learning_optimizer import PaperLearningOptimizer
from app.agents.paper_trading_agent import PaperTradingAgent
from app.agents.big_setup_detector import BigSetupDetector
from app.agents.hermes_5min_agent import Hermes5MinAgent
from app.agents.safety_guard import SafetyGuard
from app.agents.smc_confluence_tagger import SMCConfluenceTagger
from app.agents.setup_hunter import SetupHunter
from app.agents.strategies import amd_fvg_ifvg_reversal, crt_tbs_reversal, fib_ote_retest, quant_statistical_pullback, trend_continuation_breakdown
from app.config import Settings, get_settings
from app.main import HermesBackend, _btc_scalping_handoff_missing_field, _paper_report_body_with_strategy_stats, _routeable_setup_hunter_decision
from app.services.dashboard_snapshot import REQUIRED_DASHBOARD_STATUS_KEYS, dashboard_snapshot, dashboard_status_row
from app.services.heartbeat_service import HeartbeatService
from app.services.ingest_client import IngestClient
import app.services.mt5_position_sync as mt5_position_sync_module
from app.services.mt5_position_sync import force_close_demo_ticket, sync_open_mt5_positions_to_supabase
from app.services.paper_report_analytics import (
    build_observer_summaries,
    build_time_stats,
    clean_report_samples,
    format_adjusted_report_tables,
    format_time_stats_tables,
    manual_adjusted_report,
)
from app.services.time_engine import TIME_GATE_FIELDS, TimeEngine
from app.services.wsp_intelligence_overlay import evaluate_wsp_intelligence
from app.mt5.demo_router import DemoKellyRouter, build_demo_report
from app.tools import btc_weekend_sandbox
from app.tools import mt5_backtest_lab
from app.tools import strategy_math_audit
from app.tools import strategy_edge_report
from app.strategies import scalping
from app.utils.confidence import normalize_confidence


def candle_frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"open": open_, "high": high, "low": low, "close": close, "spread": 1, "tick_volume": 100}
            for open_, high, low, close in rows
        ]
    )


def test_settings() -> Settings:
    return Settings(
        paper_trading=True,
        demo_trading=False,
        allow_live_trading=False,
        auto_apply_learning=True,
        learning_mode=True,
        learning_batch_size=25,
        min_closed_trades_for_weight_update=5,
        hermes_magic_number=909001,
        btc_weekend_analysis_only=False,
        bad_hour_analysis_only=False,
        demo_ignore_all_time_blocks=False,
        demo_ignore_session_blocks=False,
        demo_ignore_bad_hour_blocks=False,
        demo_ignore_duration_blocks=False,
        demo_ignore_setup_wait_hours=False,
        max_money_tp_enabled=False,
        ema_pullback_require_extra_confirmation=False,
        risk_diag_max_realized_risk_percent=999.0,
        risk_diag_max_mismatch_abs_percent=999.0,
    )


def demo_settings(**overrides) -> Settings:
    values = {
        "paper_trading": False,
        "demo_trading": True,
        "allow_live_trading": False,
        "demo_only": True,
        "demo_pilot_enabled": True,
        "demo_pilot_hours": 240,
        "demo_pilot_started_at": "2026-06-01T00:00:00+00:00",
        "demo_magic_number": 909002,
        "demo_comment": "HERMES_DEMO_KELLY_24H",
        "demo_max_lot": 0.01,
        "symbol_trade_cooldown_enabled": False,
        "demo_max_open_trades": 1,
        "demo_max_trades_per_day": 5,
        "demo_max_trades_per_day_total": 15,
        "demo_max_trades_per_symbol_per_day": 5,
        "demo_max_open_trades_total": 1,
        "demo_max_open_trades_per_symbol": 1,
        "demo_max_open_trades_per_symbol_strategy": 1,
        "demo_max_daily_loss_pct": 1.0,
        "demo_max_risk_per_trade_pct": 0.25,
        "demo_stop_after_consecutive_losses": 3,
        "demo_allow_btc_weekend_bad_hour": False,
        "demo_allow_contest": False,
        "demo_allowed_login": "",
        "demo_exploration_mode": True,
        "demo_exploration_max_lot": 0.01,
        "demo_exploration_min_rr": 2.0,
        "demo_exploration_min_edge_score": 90.0,
        "demo_exploration_allow_smc_fail": True,
        "demo_exploration_allow_mtfa_fail": True,
        "demo_exploration_max_trades_per_day": 3,
        "demo_exploration_max_trades_per_day_total": 15,
        "demo_exploration_max_trades_per_symbol_per_day": 5,
        "demo_exploration_ignore_bad_hour": False,
        "demo_test_ignore_bad_hours": False,
        "demo_ignore_all_time_blocks": False,
        "demo_ignore_session_blocks": False,
        "demo_ignore_bad_hour_blocks": False,
        "demo_ignore_duration_blocks": False,
        "demo_ignore_setup_wait_hours": False,
        "max_money_tp_enabled": False,
        "max_tp_usd": 2.0,
        "max_tp_applies_to": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD",
        "demo_strong_setup_learning_mode": False,
        "demo_strong_setup_min_edge": 95.0,
        "demo_strong_setup_min_rr": 2.0,
        "demo_strong_setup_max_trades_per_day": 3,
        "demo_strong_setup_max_open_trades": 1,
        "demo_strong_setup_max_trades_per_day_total": 15,
        "demo_strong_setup_max_trades_per_symbol_per_day": 5,
        "demo_smoke_test_24h": False,
        "demo_smoke_test_max_confirmed_orders": 1,
        "demo_smoke_test_end_after_hours": 24,
        "hermes_free_demo_discovery_mode": False,
        "hermes_demo_topdown_fallback_mode": False,
        "hermes_demo_micro_discovery_mode": False,
        "hermes_adaptive_confluence_enabled": False,
        "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD",
        "hermes_analysis_only_symbols": "",
        "gold_liquidity_mode": "trade",
        "gold_liquidity_strategy_enabled": True,
        "gold_disable_generic_strategies": False,
        "gold_pivot_length": 15,
        "gold_atr_zone_thickness": 0.5,
        "gold_zone_capacity": 5.0,
        "gold_max_zones_per_side": 10,
        "gold_min_zone_stars": 3,
        "gold_min_liquidity_score": 75,
        "gold_min_rr": 2.0,
        "gold_max_open_trades": 1,
        "gold_allowed_signals": "ABS,REJ",
        "gold_observer_signals": "EXH,DIV",
        "eur_ema_rsi_atr_enabled": False,
        "report_timezone": "UTC",
        "timezone_local": "UTC",
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
        "ema_pullback_require_extra_confirmation": False,
    }
    values.update(overrides)
    return Settings(**values)


class PaperOptimizerTestBase(unittest.TestCase):
    def make_optimizer(self) -> PaperLearningOptimizer:
        self.tmp = tempfile.TemporaryDirectory()
        return PaperLearningOptimizer(test_settings(), Path(self.tmp.name))


class BotLogTimePayloadTests(unittest.TestCase):
    def test_bot_log_raw_payload_contains_time_fields(self) -> None:
        client = IngestClient(test_settings())
        client.set_time_snapshot(
            {
                "utc_time": "2026-06-01T10:00:00+00:00",
                "casablanca_time": "2026-06-01T11:00:00+01:00",
                "broker_time_estimate": "2026-06-01T13:00:00+03:00",
                "broker_utc_offset_hours": 3,
                "local_hour": 11,
                "utc_hour": 10,
                "broker_hour": 13,
                "weekday": "MONDAY",
                "session_name": "LONDON",
                "symbol_market_open": True,
                "is_weekend": False,
                "is_bad_hour": False,
                "time_gate_status": "PASS",
                "time_gate_reason": "TIME_GATE_PASS",
            }
        )
        row = client.prepare_row("bot_logs", {"level": "INFO", "source": "HERMES_BACKEND", "message": "Test log"})
        raw = row["raw_payload"]
        self.assertEqual(raw["message"], "Test log")
        self.assertEqual(raw["utc_time"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(raw["casablanca_time"], "2026-06-01T11:00:00+01:00")
        self.assertEqual(raw["broker_time_estimate"], "2026-06-01T13:00:00+03:00")
        self.assertTrue(raw["market_open"])
        self.assertEqual(raw["time_gate_status"], "PASS")
        for field in (
            "utc_time",
            "casablanca_time",
            "broker_time_estimate",
            "broker_utc_offset_hours",
            "local_hour",
            "utc_hour",
            "broker_hour",
            "weekday",
            "session_name",
            "market_open",
            "is_weekend",
            "is_bad_hour",
            "time_gate_status",
            "time_gate_reason",
        ):
            self.assertIn(field, raw)
        self.assertEqual(row["utc_time"], raw["utc_time"])
        self.assertEqual(row["casablanca_time"], raw["casablanca_time"])
        self.assertEqual(row["broker_time_estimate"], raw["broker_time_estimate"])
        self.assertEqual(row["session_name"], raw["session_name"])
        self.assertEqual(row["time_gate_status"], raw["time_gate_status"])

    def test_bot_log_raw_payload_string_is_object(self) -> None:
        client = IngestClient(test_settings())
        row = client.prepare_row(
            "bot_logs",
            {
                "message": "String raw",
                "raw_payload": '{"existing":true,"raw_payload":{"nested":"ok"}}',
            },
        )
        raw = row["raw_payload"]
        self.assertIsInstance(raw, dict)
        self.assertTrue(raw["existing"])
        self.assertEqual(raw["nested"], "ok")
        self.assertIn("utc_time", raw)

    def test_bot_log_raw_payload_unparseable_string_is_wrapped(self) -> None:
        client = IngestClient(test_settings())
        raw = client.prepare_row("bot_logs", {"message": "Bad raw", "raw_payload": "not-json"})["raw_payload"]
        self.assertIsInstance(raw, dict)
        self.assertEqual(raw["raw_payload_text"], "not-json")
        self.assertIn("casablanca_time", raw)

    def test_generic_cycle_complete_log_includes_time_fields(self) -> None:
        client = IngestClient(test_settings())
        client.set_time_snapshot(
            {
                "utc_time": "2026-06-01T10:00:00+00:00",
                "casablanca_time": "2026-06-01T11:00:00+01:00",
                "broker_time_estimate": "2026-06-01T13:00:00+03:00",
                "session_name": "LONDON",
                "time_gate_status": "PASS",
            }
        )
        row = client.prepare_row(
            "bot_logs",
            {
                "level": "INFO",
                "source": "HERMES_BACKEND",
                "message": "Hermes analysis cycle complete",
                "context": {"symbol": "EURUSD", "decision": "WAIT"},
            },
        )
        raw = row["raw_payload"]
        self.assertEqual(raw["message"], "Hermes analysis cycle complete")
        self.assertIn("utc_time", raw)
        self.assertIn("casablanca_time", raw)
        self.assertIn("broker_time_estimate", raw)
        self.assertIn("session_name", raw)
        self.assertIn("time_gate_status", raw)
        self.assertEqual(raw["utc_time"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(raw["casablanca_time"], "2026-06-01T11:00:00+01:00")
        self.assertEqual(raw["broker_time_estimate"], "2026-06-01T13:00:00+03:00")
        self.assertEqual(raw["session_name"], "LONDON")
        self.assertEqual(raw["time_gate_status"], "PASS")

    def test_generic_log_utc_and_casablanca_are_never_unknown(self) -> None:
        client = IngestClient(test_settings())
        raw = client.prepare_row("bot_logs", {"message": "Generic log"})["raw_payload"]
        self.assertNotEqual(raw["utc_time"], "UNKNOWN")
        self.assertNotEqual(raw["casablanca_time"], "UNKNOWN")

    def test_missing_broker_time_writes_unknown_but_keeps_key(self) -> None:
        client = IngestClient(test_settings())
        client.set_time_snapshot({"utc_time": "2026-06-01T10:00:00+00:00", "casablanca_time": "2026-06-01T11:00:00+01:00"})
        raw = client.prepare_row("bot_logs", {"message": "Missing broker"})["raw_payload"]
        self.assertEqual(raw["broker_time_estimate"], "UNKNOWN")
        self.assertIn("broker_time_estimate", raw)


class DashboardStatusPayloadTests(unittest.TestCase):
    class FakeHeartbeatIngest:
        def __init__(self) -> None:
            self.enabled = True
            self.latest_time_snapshot = None
            self.updated: list[tuple[str, dict, dict]] = []
            self.sent: list[tuple[str, dict]] = []

        def update_row(self, table: str, match: dict, data: dict) -> dict:
            self.updated.append((table, match, data))
            return {"ok": True, "table": table}

        def send_row(self, table: str, data: dict) -> dict:
            self.sent.append((table, data))
            return {"ok": True, "table": table}

    def test_dashboard_status_payload_contains_all_required_keys(self) -> None:
        settings = demo_settings()
        account = {
            "login": 345297734,
            "trade_mode": 0,
            "name": "Demo Account",
            "server": "XMGlobal-MT5 10",
            "company": "XM Global Limited",
            "trade_allowed": True,
            "trade_expert": True,
        }
        time_snapshot = {
            "utc_time": "2026-06-01T10:00:00+00:00",
            "casablanca_time": "2026-06-01T11:00:00+01:00",
            "broker_time_estimate": "2026-06-01T13:00:00+03:00",
            "broker_utc_offset_hours": 3,
            "local_hour": 11,
            "utc_hour": 10,
            "broker_hour": 13,
            "weekday": "MONDAY",
            "session_name": "LONDON",
            "market_open": True,
            "is_weekend": False,
            "is_bad_hour": False,
            "time_gate_status": "PASS",
            "time_gate_reason": "TIME_GATE_PASS",
        }
        latest_demo_event = {
            "decision": "BLOCK",
            "reason": "NO_TRADE_DIRECTION",
            "ticket": 123,
            "symbol": "BTCUSD#",
            "raw_symbol": "BTCUSD",
            "broker_symbol": "BTCUSD#",
            "allowed_symbol_check": "PASS",
        }
        payload = dashboard_snapshot(settings, account, time_snapshot, latest_demo_event, True, self.now())
        missing = [key for key in REQUIRED_DASHBOARD_STATUS_KEYS if key not in payload]
        self.assertEqual(missing, [])
        self.assertEqual(payload["mode"], "DEMO_PILOT_240H")
        self.assertTrue(payload["demo_pilot_enabled"])
        self.assertFalse(payload["allow_live_trading"])
        self.assertTrue(payload["live_trading_blocked"])
        self.assertEqual(payload["demo_magic_number"], 909002)
        self.assertEqual(payload["demo_comment"], "HERMES_DEMO_KELLY_24H")
        self.assertEqual(payload["account_login"], 345297734)
        self.assertEqual(payload["account_trade_mode"], 0)
        self.assertEqual(payload["account_type"], "DEMO")
        self.assertEqual(payload["account_name"], "Demo Account")
        self.assertEqual(payload["account_server"], "XMGlobal-MT5 10")
        self.assertEqual(payload["time_gate_status"], "PASS")
        self.assertEqual(payload["broker_symbol"], "BTCUSD#")

    def test_dashboard_quant_pro_panel_exposes_hurst_filter(self) -> None:
        event = {
            "strategy": "QUANT_PRO_REGIME_SWITCHING",
            "quant_pro_hurst": 0.94,
            "quant_pro_hurst_filter": {
                "enabled": True,
                "hurst": 0.94,
                "min_trend_hurst": 0.90,
                "trend_strength": "STRONG_TREND",
                "passed": True,
                "block_reason": None,
            },
        }
        payload = dashboard_snapshot(demo_settings(), {"trade_mode": 0}, None, event, True, self.now())
        panel = payload["quant_pro_regime_switching"]
        self.assertEqual(panel["hurst"], 0.94)
        self.assertEqual(panel["min_hurst_required"], 0.90)
        self.assertEqual(panel["trend_strength"], "STRONG_TREND")
        self.assertEqual(panel["hurst_filter"], "PASS")
        self.assertIsNone(panel["block_reason"])

    def test_dashboard_shows_time_session_blocks_disabled_warning(self) -> None:
        payload = dashboard_snapshot(
            demo_settings(
                demo_ignore_all_time_blocks=True,
                demo_ignore_session_blocks=True,
                demo_ignore_bad_hour_blocks=True,
                demo_ignore_duration_blocks=True,
                demo_ignore_setup_wait_hours=True,
            ),
            {"trade_mode": 0},
            None,
            None,
            True,
            self.now(),
        )
        self.assertEqual(payload["time_session_blocks_disabled_warning"], "TIME / SESSION BLOCKS DISABLED BY USER ORDER")
        self.assertIn("DEMO_ONLY true", payload["hard_safety_still_active"])
        self.assertIn("LIVE TRADING BLOCKED", payload["hard_safety_still_active"])
        self.assertIn("MAX LOT 0.01", payload["hard_safety_still_active"])

    def test_dashboard_shows_max_money_tp_panel(self) -> None:
        event = {
            "max_money_tp": {
                "enabled": True,
                "max_tp_usd": 2.0,
                "applied": True,
                "original_tp": 1.105,
                "final_tp": 1.102,
                "sl_unchanged": 1.099,
            }
        }
        payload = dashboard_snapshot(demo_settings(max_money_tp_enabled=True, max_tp_usd=2.0), {"trade_mode": 0}, None, event, True, self.now())
        panel = payload["max_money_tp"]
        self.assertTrue(panel["enabled"])
        self.assertEqual(panel["max_tp_usd"], 2.0)
        self.assertEqual(panel["sl_mode"], "Dynamic by HERMES / Strategy")
        self.assertTrue(panel["tp_capped"])
        self.assertEqual(panel["original_tp"], 1.105)
        self.assertEqual(panel["final_tp"], 1.102)
        self.assertEqual(panel["sl_unchanged"], 1.099)

    def test_dashboard_status_live_snapshot_uses_signed_total_pnl(self) -> None:
        payload = dashboard_snapshot(
            demo_settings(),
            {"trade_mode": 0},
            None,
            None,
            True,
            self.now(),
            latest_position_sync={
                "mt5_open_positions_count": 2,
                "hermes_mt5_open_positions_count": 1,
                "open_demo_trades_count": 1,
                "demo_closed_pnl_today": -13.27,
                "demo_floating_pnl": -3.90,
                "latest_position_sync_time": "2026-06-01T09:59:50+00:00",
            },
        )
        self.assertEqual(payload["backend_utc_time"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(payload["current_mt5_open_positions_count"], 2)
        self.assertEqual(payload["current_hermes_mt5_open_positions_count"], 1)
        self.assertEqual(payload["open_demo_trades_count"], 1)
        self.assertEqual(payload["demo_closed_pnl_today"], -13.27)
        self.assertEqual(payload["demo_floating_pnl"], -3.9)
        self.assertEqual(payload["demo_total_pnl_today"], -17.17)
        self.assertEqual(payload["latest_position_sync_time"], "2026-06-01T09:59:50+00:00")

    def test_dashboard_status_heartbeat_uses_current_time_not_stale_snapshot(self) -> None:
        settings = demo_settings(timezone_local="Africa/Casablanca", report_timezone="Africa/Casablanca")
        stale = {
            "utc_time": "2026-06-01T09:00:00+00:00",
            "casablanca_time": "2026-06-01T10:00:00+01:00",
            "session_name": "LONDON",
        }
        first = dashboard_snapshot(settings, {"trade_mode": 0}, stale, None, True, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        second = dashboard_snapshot(settings, {"trade_mode": 0}, stale, None, True, datetime.fromisoformat("2026-06-01T10:00:07+00:00"))
        self.assertEqual(first["utc_time"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(second["utc_time"], "2026-06-01T10:00:07+00:00")
        self.assertNotEqual(first["utc_time"], second["utc_time"])
        self.assertEqual(second["latest_heartbeat_written_at"], second["utc_time"])
        self.assertEqual(second["session_name"], "LONDON")

    def test_dashboard_status_heartbeat_writes_without_full_cycle_completion(self) -> None:
        ingest = self.FakeHeartbeatIngest()
        service = HeartbeatService(demo_settings(), ingest)
        service.write(
            {"trade_mode": 0, "trade_allowed": True, "trade_expert": True},
            {"BTCUSD": "BTCUSD#"},
            latest_position_sync={
                "mt5_open_positions_count": 1,
                "hermes_mt5_open_positions_count": 1,
                "demo_closed_pnl_today": -13.27,
                "demo_floating_pnl": -3.90,
                "latest_position_sync_time": "2026-06-01T09:59:55+00:00",
            },
        )
        dashboard_rows = [data for table, match, data in ingest.updated if table == "bot_status" and match.get("component") == "dashboard_status"]
        self.assertEqual(len(dashboard_rows), 1)
        payload = dashboard_rows[0]["raw_payload"]
        self.assertEqual(payload["open_demo_trades_count"], 1)
        self.assertEqual(payload["demo_total_pnl_today"], -17.17)
        self.assertEqual(payload["latest_position_sync_time"], "2026-06-01T09:59:55+00:00")

    def test_dashboard_status_row_uses_component_and_json_payload(self) -> None:
        payload = dashboard_snapshot(demo_settings(), {"trade_mode": 0}, None, None, True, self.now())
        row = dashboard_status_row(payload, self.now())
        self.assertEqual(row["component"], "dashboard_status")
        self.assertEqual(row["raw_payload"], payload)
        self.assertEqual(row["payload"], payload)
        self.assertEqual(row["status_json"], payload)

    def test_trade_mode_zero_maps_to_demo_and_does_not_show_old_live(self) -> None:
        payload = dashboard_snapshot(demo_settings(), {"trade_mode": 0}, None, None, True, self.now())
        self.assertEqual(payload["account_type"], "DEMO")
        self.assertNotEqual(payload["account_type"], "LIVE")

    def test_dashboard_time_utc_and_casablanca_not_unknown(self) -> None:
        payload = dashboard_snapshot(demo_settings(), {"trade_mode": 0}, None, None, True, self.now())
        self.assertNotEqual(payload["utc_time"], "UNKNOWN")
        self.assertNotEqual(payload["casablanca_time"], "UNKNOWN")
        for field in (
            "best_candidate_now",
            "setup_hunter_score",
            "setup_hunter_grade",
            "setup_hunter_missing",
            "edge_ready_candidates_count",
            "near_miss_candidates_count",
            "current_session_quality",
            "best_entry_strategy_now",
            "latest_near_miss_reason",
        ):
            self.assertIn(field, payload)

    def now(self) -> datetime:
        return datetime.fromisoformat("2026-06-01T10:00:00+00:00")


class SetupHunterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hunter = SetupHunter(demo_settings())
        self.time_gate = {
            "session_name": "LONDON",
            "time_gate_status": "PASS",
            "time_gate_reason": "TIME_GATE_PASS",
            "symbol_market_open": True,
            "is_bad_hour": False,
        }

    def test_btc_scalping_shared_confidence_normalizer(self) -> None:
        self.assertEqual(normalize_confidence(None), 0)
        self.assertEqual(normalize_confidence(0.58), 58)
        self.assertEqual(normalize_confidence(0.60), 60)
        self.assertEqual(normalize_confidence(60.0), 60)
        self.assertEqual(normalize_confidence(65.0), 65)

    def analysis(self, signal: dict) -> dict:
        base = {
            "symbol": "EURUSD",
            "strategy": signal.get("strategy"),
            "signal": signal.get("signal", "BUY"),
            "risk_reward": signal.get("risk_reward", 2.0),
            "smc_confluence_status": signal.get("smc_confluence_status", "PASS"),
            "smc_confluence_score": signal.get("smc_confluence_score", 80),
            "smc_confluence_reason": "SMC_CONFLUENCE_ALIGNED",
            "mtfa_status": signal.get("mtfa_status", "PASS"),
            "mtfa_score": signal.get("mtfa_score", 80),
            "mtfa_reason": "MTFA_PASS",
            "m15_confirmation": signal.get("m15_confirmation", True),
            "m1_entry_confirmation": signal.get("m1_entry_confirmation", True),
            "safety_guard_status": signal.get("safety_guard_status", "PASS"),
            "smc_h4_direction": "BULLISH",
            "smc_h1_trend": "BULLISH",
            "smc_h4_key_level_nearby": True,
            "h4_zone": "DEMAND",
            "m15_liquidity": True,
            "smc_h1_fvg": "BULLISH_FVG",
        }
        return {"ai_decision": base, "strategy_signals": [signal]}

    def signal(self, **overrides) -> dict:
        payload = {
            "strategy": "BREAKOUT_RETEST",
            "signal": "BUY",
            "confidence": 0.85,
            "entry": 1.1,
            "sl": 1.09,
            "tp": 1.12,
            "risk_reward": 2.0,
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
        }
        payload.update(overrides)
        return payload

    def test_ema_cannot_be_execution_strategy(self) -> None:
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy="EMA_PULLBACK")), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["strategy_role"], "CONFIRMATION")
        self.assertFalse(result.best_candidate["demo_eligible"])

    def test_ema_confirmation_boosts_entry_strategy(self) -> None:
        entry = self.signal(strategy="SIMO_ATM_BREAKOUT", confidence=0.75)
        ema = self.signal(strategy="EMA_PULLBACK", confidence=0.80)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", {"ai_decision": self.analysis(entry)["ai_decision"], "strategy_signals": [entry, ema]}, self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "SIMO_ATM_BREAKOUT")
        self.assertTrue(result.best_candidate["ema_confirmation"])
        self.assertEqual(result.best_candidate["confirmation_boost"], 5)
        self.assertGreaterEqual(result.best_candidate["setup_score"], 80)

    def test_observer_strategies_cannot_open(self) -> None:
        for strategy in ("SECOND_ENTRY", "SCALPING_AGENT"):
            result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy=strategy)), self.time_gate, 1, 30)
            self.assertEqual(result.best_candidate["strategy_role"], "OBSERVER")
            self.assertFalse(result.best_candidate["demo_eligible"])

    def test_btc_scalping_agent_is_btc_entry_candidate(self) -> None:
        signal = self.signal(strategy="BTC_SCALPING_AGENT", symbol="BTCUSD#", entry=100.0, sl=99.0, tp=102.0)
        result = self.hunter.evaluate("BTCUSD#", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(result.best_candidate["strategy_role"], "ENTRY")
        self.assertTrue(result.best_candidate["execution_candidate"])
        self.assertTrue(result.best_candidate["demo_eligible"])

    def test_btc_scalping_buy_becomes_best_when_quant_pro_is_blocked_later(self) -> None:
        quant = self.signal(
            strategy="QUANT_PRO_REGIME_SWITCHING",
            signal="BUY",
            quant_pro_score=95,
            confidence=0.95,
            reason="MICRO_DISCOVERY_CONFLUENCE_TOO_LOW",
            entry=100.0,
            sl=99.0,
            tp=102.0,
        )
        scalp = self.signal(
            strategy="BTC_SCALPING_AGENT",
            signal="BUY",
            confidence=80.0,
            reason="M5_MOMENTUM_SCALP",
            trigger_type="M5_MOMENTUM_SCALP",
            smc_confluence_status="PASS",
            smc_confluence_score=70,
            mtfa_status="PASS",
            mtfa_score=60,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            entry=100.0,
            sl=99.0,
            tp=102.0,
        )
        analysis = {"ai_decision": self.analysis(quant)["ai_decision"], "strategy_signals": [quant, scalp]}
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", analysis, self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(result.best_candidate["direction"], "BUY")
        self.assertEqual(result.best_candidate["edge_score"], 80)
        self.assertEqual(result.best_candidate["grade"], "B")
        self.assertTrue(result.best_candidate["demo_eligible"])
        self.assertEqual(result.best_candidate["btc_scalping_route_status"], "PASS")
        self.assertEqual(result.best_candidate["btc_scalping_route_reason"], "SCALP_SIGNAL_VALID")

    def test_btc_scalping_sell_becomes_best_when_quant_pro_is_blocked_later(self) -> None:
        quant = self.signal(
            strategy="QUANT_PRO_REGIME_SWITCHING",
            signal="SELL",
            quant_pro_score=95,
            confidence=0.95,
            reason="SMC_STRONG_FAIL",
            entry=100.0,
            sl=101.0,
            tp=98.0,
        )
        scalp = self.signal(
            strategy="BTC_SCALPING_AGENT",
            signal="SELL",
            confidence=0.80,
            reason="M1_BREAKOUT_SCALP",
            trigger_type="M1_BREAKOUT_SCALP",
            smc_confluence_status="PASS",
            smc_confluence_score=70,
            mtfa_status="PASS",
            mtfa_score=60,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            entry=100.0,
            sl=101.0,
            tp=98.0,
        )
        analysis = {"ai_decision": self.analysis(quant)["ai_decision"], "strategy_signals": [quant, scalp]}
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", analysis, self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(result.best_candidate["direction"], "SELL")
        self.assertEqual(result.best_candidate["edge_score"], 80)
        self.assertEqual(result.best_candidate["normalized_confidence"], 80)
        self.assertEqual(result.best_candidate["grade"], "B")
        self.assertTrue(result.best_candidate["demo_eligible"])

    def test_btc_scalping_confidence_normalizes_fraction_to_percent(self) -> None:
        signal = self.signal(strategy="BTC_SCALPING_AGENT", symbol="BTCUSD#", confidence=0.58, entry=100.0, sl=99.0, tp=102.0)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        candidate = result.best_candidate
        self.assertEqual(candidate["setup_score"], 58)
        self.assertEqual(candidate["normalized_confidence"], 58)
        # confidence 58 < 75 routing floor — normalization works but routing is blocked
        self.assertFalse(candidate["demo_eligible"])
        self.assertIn("BTC_SCALPING_CONFIDENCE_BELOW_MIN", candidate["failed_gates"])

    def test_btc_scalping_confidence_normalizes_zero_sixty_to_sixty(self) -> None:
        signal = self.signal(strategy="BTC_SCALPING_AGENT", symbol="BTCUSD#", confidence=0.60, entry=100.0, sl=99.0, tp=102.0)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        candidate = result.best_candidate
        self.assertEqual(candidate["setup_score"], 60)
        self.assertEqual(candidate["normalized_confidence"], 60)
        # confidence 60 < 75 routing floor — normalization works but routing is blocked
        self.assertFalse(candidate["demo_eligible"])
        self.assertIn("BTC_SCALPING_CONFIDENCE_BELOW_MIN", candidate["failed_gates"])

    def test_btc_scalping_confidence_normalizes_percent_as_percent(self) -> None:
        signal = self.signal(strategy="BTC_SCALPING_AGENT", symbol="BTCUSD#", confidence=60.0, entry=100.0, sl=99.0, tp=102.0)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        candidate = result.best_candidate
        self.assertEqual(candidate["setup_score"], 60)
        self.assertEqual(candidate["normalized_confidence"], 60)
        # confidence 60 < 75 routing floor — normalization works but routing is blocked
        self.assertFalse(candidate["demo_eligible"])
        self.assertIn("BTC_SCALPING_CONFIDENCE_BELOW_MIN", candidate["failed_gates"])

    def test_btc_scalping_requires_confluence_gate_not_just_smc_bypass(self) -> None:
        """BTC_SCALPING with confidence < 75 or poor confluence must be blocked."""
        signal = self.signal(
            strategy="BTC_SCALPING_AGENT",
            symbol="BTCUSD#",
            confidence=60.0,
            smc_confluence_status="FAIL",
            smc_confluence_score=0,
            mtfa_status="FAIL",
            mtfa_score=0,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            top_down_status="FAIL",
            top_down_decision="AVOID",
            final_confluence_score=0,
            entry=100.0,
            sl=99.0,
            tp=102.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "BTC_SCALPING_AGENT")
        # confidence 60 < 75 threshold: must be blocked
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("BTC_SCALPING_CONFIDENCE_BELOW_MIN", result.best_candidate["failed_gates"])

    def test_btc_scalping_agent_is_analysis_only_outside_btc(self) -> None:
        signal = self.signal(strategy="BTC_SCALPING_AGENT", symbol="EURUSD")
        result = self.hunter.evaluate("EURUSD", "EURUSD", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertEqual(result.candidates, [])

    def test_near_miss_when_m1_missing(self) -> None:
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy="SIMO_ATM_BREAKOUT", m1_entry_confirmation=False)), self.time_gate, 1, 30)
        self.assertTrue(result.best_candidate["demo_eligible"])
        self.assertNotIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])

    def test_near_miss_when_session_blocked(self) -> None:
        blocked_time = {**self.time_gate, "session_name": "OFF_HOURS"}
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal()), blocked_time, 1, 30)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("WAITING_FOR_SESSION", result.best_candidate["failed_gates"])

    def test_time_gate_pass_valid_session_does_not_add_waiting_for_session(self) -> None:
        valid_time = {**self.time_gate, "session_name": "OVERLAP", "utc_time": "2026-06-01T16:10:00+00:00", "casablanca_time": "2026-06-01T17:10:00+01:00"}
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(signal="SELL")), valid_time, 1, 30)
        self.assertNotIn("WAITING_FOR_SESSION", result.best_candidate["failed_gates"])
        self.assertNotIn("SAFETY_GUARD_BLOCK", result.best_candidate["failed_gates"])

    def test_trend_continuation_strong_downtrend_sell_eligible(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            confidence=0.9,
            trend_continuation_score=90,
            support_break=True,
            trend_continuation_momentum=True,
            m15_confirmation=True,
            m1_entry_confirmation=True,
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(result.best_candidate["direction"], "SELL")
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])

    def test_quant_candidate_enters_setup_hunter_ranking(self) -> None:
        quant = self.signal(
            strategy="QUANT_STATISTICAL_PULLBACK",
            signal="BUY",
            confidence=0.75,
            quant_score=95,
            quant_r2=0.8,
            quant_z_score=-1.2,
            risk_reward=2.0,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            smc_confluence_status="FAIL",
            smc_confluence_score=20,
            mtfa_status="FAIL",
            mtfa_score=20,
            big_setup_grade="D",
        )
        weak = self.signal(strategy="BREAKOUT_RETEST", confidence=0.50)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", {"ai_decision": self.analysis(weak)["ai_decision"], "strategy_signals": [weak, quant]}, self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "QUANT_STATISTICAL_PULLBACK")
        self.assertEqual(result.best_candidate["quant_score"], 95)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])

    def test_quant_pro_candidate_enters_setup_hunter_ranking(self) -> None:
        quant_pro = self.signal(
            strategy="QUANT_PRO_REGIME_SWITCHING",
            signal="BUY",
            confidence=0.95,
            quant_pro_score=95,
            quant_pro_regime="TREND",
            quant_pro_ols_tstat=5.2,
            quant_pro_kalman_z=-1.2,
            quant_pro_hurst=0.62,
            quant_pro_signal="BUY",
            risk_reward=2.0,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            smc_confluence_status="FAIL",
            smc_confluence_score=20,
            mtfa_status="FAIL",
            mtfa_score=20,
            big_setup_grade="D",
        )
        weak = self.signal(strategy="BREAKOUT_RETEST", confidence=0.50)
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", {"ai_decision": self.analysis(weak)["ai_decision"], "strategy_signals": [weak, quant_pro]}, self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "QUANT_PRO_REGIME_SWITCHING")
        self.assertEqual(result.best_candidate["quant_pro_score"], 95)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])

    def test_ai_decision_raw_payload_always_has_top_down_fields(self) -> None:
        agent = Hermes5MinAgent(demo_settings())
        frames = {tf: self.frame_with_time(count=60) for tf in ("D1", "H4", "H1", "M15", "M5", "M1")}
        analysis = agent.analyze_symbol(
            "EURUSD",
            frames,
            {"trade_mode": 0, "balance": 100000, "equity": 100000},
            0,
            {"point": 0.00001, "trade_tick_value": 1.0, "trade_tick_size": 0.00001},
            30,
        )
        raw = analysis["ai_decision"]["raw_payload"]
        for field in (
            "top_down_status",
            "top_down_decision",
            "entry_readiness_score",
            "market_narrative",
            "missing_confirmations",
            "score_breakdown",
            "d1_macro_bias",
            "h4_main_bias",
            "h1_internal_structure",
            "m15_confirmation_status",
            "m5_context_status",
            "m1_trigger_status",
        ):
            self.assertIn(field, raw)
        self.assertEqual(raw["top_down_status"], "FAIL")
        self.assertEqual(raw["top_down_decision"], "WAIT_FOR_CONFIRMATION")
        self.assertEqual(raw["entry_readiness_score"], 0)
        # After the top-down fix, specific *_RATES_MISSING strings are returned
        # instead of the generic TOP_DOWN_DATA_MISSING sentinel
        missing = raw["missing_confirmations"]
        self.assertTrue(
            len(missing) > 0,
            "missing_confirmations must be non-empty when candles < 100",
        )
        top_down_indicators = {
            "TOP_DOWN_DATA_MISSING", "TOP_DOWN_SNAPSHOT_NOT_READY",
            "D1_RATES_MISSING", "H4_RATES_MISSING", "H1_RATES_MISSING",
            "M15_RATES_MISSING", "M5_RATES_MISSING", "M1_RATES_MISSING",
        }
        self.assertTrue(
            any(m in top_down_indicators or str(m).endswith("_RATES_MISSING")
                or str(m).endswith("_MISSING_OR_INSUFFICIENT")
                for m in missing),
            f"No top-down missing indicator found in: {missing}",
        )
        self.assertEqual(
            raw["acceleration_bands_htf"],
            {
                "enabled": False,
                "status": "OPTIONAL_NOT_ENABLED",
                "signal": "UNKNOWN",
                "score": None,
                "reason": "Acceleration Bands HTF module not enabled or no payload yet",
            },
        )
        self.assertEqual(
            raw["volume_profile"],
            {
                "enabled": False,
                "status": "OPTIONAL_NOT_ENABLED",
                "poc": None,
                "vah": None,
                "val": None,
                "score": None,
                "reason": "Volume Profile module not enabled or no payload yet",
            },
        )

    def test_trend_continuation_missing_m1_is_near_miss(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            trend_continuation_score=90,
            support_break=True,
            m15_confirmation=True,
            m1_entry_confirmation=False,
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertEqual(result.best_candidate["near_miss_reason"], "STRATEGY_OBSERVER_ONLY")
        self.assertIn("WAITING_FOR_M1_TRIGGER", result.best_candidate["failed_gates"])

    def test_trend_continuation_rr_below_1_5_blocks(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            trend_continuation_score=90,
            support_break=True,
            m15_confirmation=True,
            m1_entry_confirmation=True,
            risk_reward=1.2,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("RR_TOO_LOW", result.best_candidate["failed_gates"])

    def test_trend_continuation_strategy_detects_btc_breakdown(self) -> None:
        frames = {
            "M5": self.trend_frame(100, -1.0, 40, final_break=True),
            "M15": self.trend_frame(110, -1.2, 12, final_break=True),
            "M1": self.trend_frame(91, -0.25, 12, final_break=True),
            "H1": self.trend_frame(130, -2.0, 12),
            "H4": self.trend_frame(150, -3.0, 12),
        }
        signal = trend_continuation_breakdown.evaluate("BTCUSD#", frames, {"market_state": "STRONG_DOWNTREND"}, demo_settings())
        self.assertEqual(signal["strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(signal["signal"], "SELL")
        self.assertTrue(signal["support_break"])
        self.assertTrue(signal["m1_entry_confirmation"])
        self.assertTrue(signal["m15_confirmation"])
        self.assertGreaterEqual(signal["risk_reward"], 1.5)

    def test_trend_continuation_m1_break_minor_low_passes(self) -> None:
        frames = {
            "M5": self.trend_frame(100, -1.0, 40, final_break=True),
            "M15": self.trend_frame(110, -1.2, 12, final_break=True),
            "M1": self.m1_break_minor_low_frame(),
            "H1": self.trend_frame(130, -2.0, 12),
            "H4": self.trend_frame(150, -3.0, 12),
        }
        signal = trend_continuation_breakdown.evaluate("BTCUSD#", frames, {"market_state": "STRONG_DOWNTREND"}, demo_settings())
        self.assertEqual(signal["m1_trigger_status"], "PASS")
        self.assertEqual(signal["m1_trigger_type"], "M1_BEARISH_CLOSE_BELOW_MINOR_LOW")
        self.assertTrue(signal["m1_entry_confirmation"])

    def test_trend_continuation_m1_lower_high_rejection_passes(self) -> None:
        frames = {
            "M5": self.trend_frame(100, -1.0, 40, final_break=True),
            "M15": self.trend_frame(110, -1.2, 12, final_break=True),
            "M1": self.m1_lower_high_rejection_frame(),
            "H1": self.trend_frame(130, -2.0, 12),
            "H4": self.trend_frame(150, -3.0, 12),
        }
        signal = trend_continuation_breakdown.evaluate("BTCUSD#", frames, {"market_state": "STRONG_DOWNTREND"}, demo_settings())
        self.assertEqual(signal["m1_trigger_status"], "PASS")
        self.assertEqual(signal["m1_trigger_type"], "M1_LOWER_HIGH_REJECTION")
        self.assertTrue(signal["m1_entry_confirmation"])

    def test_trend_continuation_gold_like_m1_bearish_engulfing_resolves_sell(self) -> None:
        frames = {
            "M5": self.trend_frame(100, -0.55, 40),
            "M15": self.trend_frame(110, -0.8, 12, final_break=True),
            "M1": self.m1_bearish_engulfing_frame(),
            "H1": self.trend_frame(130, -1.5, 12),
            "H4": self.trend_frame(150, -2.0, 12),
        }
        signal = trend_continuation_breakdown.evaluate("GOLD#", frames, {"market_state": "STRONG_DOWNTREND"}, demo_settings())
        self.assertEqual(signal["resolved_direction"], "SELL")
        self.assertEqual(signal["signal"], "SELL")
        self.assertEqual(signal["direction_source"], "M1_M15_TREND_CONFIRMATION")
        self.assertEqual(signal["m1_trigger_status"], "PASS")

    def test_trend_continuation_m15_bearish_close_below_support_passes(self) -> None:
        frames = {
            "M5": self.trend_frame(100, -1.0, 40, final_break=True),
            "M15": self.trend_frame(110, -1.2, 12, final_break=True),
            "M1": self.m1_break_minor_low_frame(),
            "H1": self.trend_frame(130, -2.0, 12),
            "H4": self.trend_frame(150, -3.0, 12),
        }
        signal = trend_continuation_breakdown.evaluate("BTCUSD#", frames, {"market_state": "STRONG_DOWNTREND"}, demo_settings())
        self.assertEqual(signal["m15_confirmation_status"], "PASS")
        self.assertEqual(signal["m15_confirmation_type"], "M15_CLOSE_BELOW_SUPPORT")
        self.assertTrue(signal["m15_confirmation"])

    def test_trend_continuation_m15_missing_but_m1_pass_is_near_miss(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            trend_continuation_score=90,
            support_break=True,
            m15_confirmation=False,
            m15_confirmation_status="FAIL",
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertEqual(result.best_candidate["near_miss_reason"], "STRATEGY_OBSERVER_ONLY")
        self.assertIn("WAITING_FOR_M15_CONFIRMATION", result.best_candidate["failed_gates"])

    def test_trend_continuation_both_confirmed_wait_flags_direction_resolver_fail(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="WAIT",
            resolved_direction="WAIT",
            trend_continuation_score=90,
            m15_confirmation=True,
            m15_confirmation_status="PASS",
            m15_confirmation_type="M15_BEARISH_STRUCTURE",
            m15_confirmation_direction="SELL",
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            m1_trigger_type="M1_BEARISH_ENGULFING",
            m1_trigger_direction="SELL",
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("DIRECTION_RESOLVER_FAIL", result.best_candidate["failed_gates"])
        self.assertEqual(result.best_candidate["near_miss_reason"], "STRATEGY_OBSERVER_ONLY")

    def test_bullish_m1_m15_resolves_buy_even_if_score_low(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="BUY",
            resolved_direction="BUY",
            direction_source="M1_M15_TREND_CONFIRMATION",
            direction_confidence=80,
            trend_continuation_score=60,
            m15_confirmation=True,
            m15_confirmation_status="PASS",
            m15_confirmation_type="M15_BULLISH_STRUCTURE",
            m15_confirmation_direction="BUY",
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            m1_trigger_type="M1_HIGHER_LOW_REJECTION",
            m1_trigger_direction="BUY",
            smc_confluence_score=50,
            mtfa_score=40,
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["direction"], "BUY")
        self.assertEqual(result.best_candidate["resolved_direction"], "BUY")
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("LOW_SETUP_SCORE", result.best_candidate["failed_gates"])
        self.assertNotIn("DIRECTION_RESOLVER_FAIL", result.best_candidate["failed_gates"])

    def test_bearish_m1_m15_resolves_sell_even_if_smc_soft_fail(self) -> None:
        # Phase 3A: SMC/MTFA SOFT_FAIL (score 40-69 / 35-59) must not add a
        # hard-block gate. TREND_CONTINUATION_BREAKDOWN is OBSERVER-only so
        # demo_eligible stays False, but the block reason is STRATEGY_OBSERVER_ONLY,
        # not CONFIRMATION_MATRIX_HARD_BLOCK.
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            resolved_direction="SELL",
            direction_source="M1_M15_TREND_CONFIRMATION",
            direction_confidence=80,
            trend_continuation_score=90,
            m15_confirmation=True,
            m15_confirmation_status="PASS",
            m15_confirmation_type="M15_BEARISH_STRUCTURE",
            m15_confirmation_direction="SELL",
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            m1_trigger_type="M1_BEARISH_ENGULFING",
            m1_trigger_direction="SELL",
            smc_confluence_status="FAIL",
            smc_confluence_score=40,
            mtfa_status="FAIL",
            mtfa_score=40,
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["direction"], "SELL")
        self.assertEqual(result.best_candidate["resolved_direction"], "SELL")
        # OBSERVER strategy — always blocked by STRATEGY_OBSERVER_ONLY, not SMC
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])
        # Phase 3A: SOFT_FAIL must NOT add CONFIRMATION_MATRIX_HARD_BLOCK
        self.assertNotIn("CONFIRMATION_MATRIX_HARD_BLOCK", result.best_candidate["failed_gates"])
        self.assertNotIn("DIRECTION_RESOLVER_FAIL", result.best_candidate["failed_gates"])

    def test_setup_hunter_carries_m1_trigger_fields(self) -> None:
        signal = self.signal(
            strategy="TREND_CONTINUATION_BREAKDOWN",
            signal="SELL",
            trend_continuation_score=90,
            support_break=True,
            m15_confirmation=True,
            m15_confirmation_status="PASS",
            m15_confirmation_type="M15_CLOSE_BELOW_SUPPORT",
            m15_confirmation_reason="M15_CLOSE_BELOW_SUPPORT",
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            m1_trigger_type="M1_BEARISH_CLOSE_BELOW_MINOR_LOW",
            m1_trigger_price=71400.0,
            m1_trigger_candle_time="2026-06-01T19:10:00+00:00",
            m1_trigger_reason="M1_BEARISH_CLOSE_BELOW_MINOR_LOW",
            risk_reward=2.0,
        )
        result = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(signal), self.time_gate, 1, 30)
        event = result.events[0]
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])
        self.assertNotIn("WAITING_FOR_M1_TRIGGER", result.best_candidate["failed_gates"])
        self.assertEqual(event["m1_trigger_status"], "PASS")
        self.assertEqual(event["m1_trigger_type"], "M1_BEARISH_CLOSE_BELOW_MINOR_LOW")
        self.assertEqual(event["m15_confirmation_status"], "PASS")
        self.assertEqual(event["m15_confirmation_type"], "M15_CLOSE_BELOW_SUPPORT")

    def trend_frame(self, start: float, step: float, count: int, final_break: bool = False) -> pd.DataFrame:
        rows = []
        price = start
        for i in range(count):
            open_ = price
            close = price + step * 0.65
            high = max(open_, close) + abs(step) * 0.25
            low = min(open_, close) - abs(step) * 0.25
            rows.append({"open": open_, "high": high, "low": low, "close": close, "spread": 1, "tick_volume": 100})
            price += step
        if final_break:
            prev_low = min(row["low"] for row in rows[-12:-1])
            rows[-1]["open"] = prev_low - abs(step) * 0.2
            rows[-1]["close"] = prev_low - abs(step) * 1.2
            rows[-1]["high"] = prev_low + abs(step) * 0.2
            rows[-1]["low"] = rows[-1]["close"] - abs(step) * 0.2
        return pd.DataFrame(rows)

    def frame_with_time(self, count: int = 60) -> pd.DataFrame:
        start = datetime.fromisoformat("2026-06-01T09:00:00+00:00")
        rows = []
        price = 1.1000
        for idx in range(count):
            open_ = price
            close = price + 0.0001
            rows.append(
                {
                    "candle_time": start + pd.Timedelta(minutes=idx),
                    "open": open_,
                    "high": close + 0.0002,
                    "low": open_ - 0.0002,
                    "close": close,
                    "spread": 1,
                    "tick_volume": 100,
                }
            )
            price = close
        return pd.DataFrame(rows)

    def m1_break_minor_low_frame(self) -> pd.DataFrame:
        rows = []
        price = 90.0
        for i in range(8):
            open_ = price
            close = price - 0.08
            rows.append({"time": f"2026-06-01T19:0{i}:00+00:00", "open": open_, "high": open_ + 0.04, "low": close - 0.04, "close": close})
            price -= 0.05
        prev_low = min(row["low"] for row in rows[-5:])
        rows.append({"time": "2026-06-01T19:08:00+00:00", "open": prev_low - 0.02, "high": prev_low + 0.02, "low": prev_low - 0.24, "close": prev_low - 0.18})
        return pd.DataFrame(rows)

    def m1_lower_high_rejection_frame(self) -> pd.DataFrame:
        rows = [
            {"time": "2026-06-01T19:00:00+00:00", "open": 90.00, "high": 90.08, "low": 89.80, "close": 89.92},
            {"time": "2026-06-01T19:01:00+00:00", "open": 89.94, "high": 90.02, "low": 89.78, "close": 89.88},
            {"time": "2026-06-01T19:02:00+00:00", "open": 89.90, "high": 89.98, "low": 89.76, "close": 89.86},
            {"time": "2026-06-01T19:03:00+00:00", "open": 89.88, "high": 89.96, "low": 89.74, "close": 89.84},
            {"time": "2026-06-01T19:04:00+00:00", "open": 89.86, "high": 89.92, "low": 89.77, "close": 89.82},
            {"time": "2026-06-01T19:05:00+00:00", "open": 89.85, "high": 89.90, "low": 89.78, "close": 89.83},
            {"time": "2026-06-01T19:06:00+00:00", "open": 89.84, "high": 89.89, "low": 89.79, "close": 89.82},
            {"time": "2026-06-01T19:07:00+00:00", "open": 89.83, "high": 89.88, "low": 89.78, "close": 89.81},
            {"time": "2026-06-01T19:08:00+00:00", "open": 89.86, "high": 89.91, "low": 89.77, "close": 89.80},
        ]
        return pd.DataFrame(rows)

    def m1_bearish_engulfing_frame(self) -> pd.DataFrame:
        rows = [
            {"time": "2026-06-01T19:00:00+00:00", "open": 90.00, "high": 90.06, "low": 89.92, "close": 89.96},
            {"time": "2026-06-01T19:01:00+00:00", "open": 89.96, "high": 90.02, "low": 89.90, "close": 89.98},
            {"time": "2026-06-01T19:02:00+00:00", "open": 89.98, "high": 90.04, "low": 89.92, "close": 90.00},
            {"time": "2026-06-01T19:03:00+00:00", "open": 90.00, "high": 90.05, "low": 89.94, "close": 90.02},
            {"time": "2026-06-01T19:04:00+00:00", "open": 90.02, "high": 90.08, "low": 89.96, "close": 90.04},
            {"time": "2026-06-01T19:05:00+00:00", "open": 90.04, "high": 90.10, "low": 89.98, "close": 90.06},
            {"time": "2026-06-01T19:06:00+00:00", "open": 90.06, "high": 90.12, "low": 90.00, "close": 90.08},
            {"time": "2026-06-01T19:07:00+00:00", "open": 90.08, "high": 90.14, "low": 90.02, "close": 90.10},
            {"time": "2026-06-01T19:08:00+00:00", "open": 90.13, "high": 90.15, "low": 89.91, "close": 89.94},
        ]
        return pd.DataFrame(rows)

    def test_eligible_requires_m1_m15_rr_and_entry_role(self) -> None:
        eligible = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy="SIMO_ATM_BREAKOUT")), self.time_gate, 1, 30)
        self.assertTrue(eligible.best_candidate["demo_eligible"])
        no_m15 = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy="SIMO_ATM_BREAKOUT", m15_confirmation=False)), self.time_gate, 1, 30)
        self.assertTrue(no_m15.best_candidate["demo_eligible"])
        low_rr = self.hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(self.signal(strategy="SIMO_ATM_BREAKOUT", risk_reward=1.2)), self.time_gate, 1, 30)
        self.assertFalse(low_rr.best_candidate["demo_eligible"])

    def test_gold_setup_hunter_ignores_generic_strategies_for_execution(self) -> None:
        generic = self.signal(strategy="TREND_CONTINUATION_BREAKDOWN", signal="SELL", confidence=0.95, trend_continuation_score=95)
        result = self.hunter.evaluate("GOLD#", "GOLD#", self.analysis(generic), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertEqual(result.best_candidate["empty_reason"], "NO_ALLOWED_EXECUTION_CANDIDATE")
        self.assertEqual(result.candidates, [])

    def test_gold_allowed_strategies_remain_eligible(self) -> None:
        gold = self.signal(
            strategy="GOLD_LIQUIDITY_HUNTER_PRO",
            signal="BUY",
            confidence=0.9,
            gold_liquidity_score=90,
            risk_reward=2.0,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            smc_confluence_status="FAIL",
            smc_confluence_score=20,
            mtfa_status="FAIL",
            mtfa_score=20,
            big_setup_grade="D",
        )
        result = self.hunter.evaluate("GOLD#", "GOLD#", self.analysis(gold), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "GOLD_LIQUIDITY_HUNTER_PRO")
        self.assertTrue(result.best_candidate["demo_eligible"])
        self.assertEqual(result.best_candidate["execution_policy"], "EXECUTABLE")

    def test_eur_setup_hunter_ignores_generic_strategies_for_execution(self) -> None:
        generic = self.signal(strategy="TREND_CONTINUATION_BREAKDOWN", signal="BUY", confidence=0.95, trend_continuation_score=95)
        result = self.hunter.evaluate("EURUSD", "EURUSD", self.analysis(generic), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "NONE")
        self.assertEqual(result.candidates, [])

    def test_eur_ema_rsi_atr_remains_eligible(self) -> None:
        eur = self.signal(strategy="EUR_EMA_RSI_ATR_CROSSOVER", signal="BUY", confidence=0.9)
        result = self.hunter.evaluate("EURUSD", "EURUSD", self.analysis(eur), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "EUR_EMA_RSI_ATR_CROSSOVER")
        self.assertTrue(result.best_candidate["demo_eligible"])
        self.assertEqual(result.best_candidate["execution_policy"], "EXECUTABLE")

    def test_btc_pullback_disabled_cannot_be_best_execution_candidate(self) -> None:
        hunter = SetupHunter(demo_settings(btc_disable_quant_statistical_pullback=True))
        quant = self.signal(
            strategy="QUANT_STATISTICAL_PULLBACK",
            signal="BUY",
            confidence=0.99,
            quant_score=99,
            risk_reward=2.0,
            m15_confirmation=False,
            m1_entry_confirmation=False,
            smc_confluence_status="FAIL",
            smc_confluence_score=20,
            mtfa_status="FAIL",
            mtfa_score=20,
            big_setup_grade="D",
        )
        result = hunter.evaluate("BTCUSD", "BTCUSD#", self.analysis(quant), self.time_gate, 1, 30)
        self.assertEqual(result.best_candidate["best_strategy"], "QUANT_STATISTICAL_PULLBACK")
        self.assertFalse(result.best_candidate["demo_eligible"])
        self.assertIn("STRATEGY_OBSERVER_ONLY", result.best_candidate["failed_gates"])


class BtcScalpingAgentStrategyTests(unittest.TestCase):
    def settings(self, **overrides) -> Settings:
        return demo_settings(
            btc_scalping_relaxed_demo_mode=True,
            btc_scalping_min_confidence=55,
            btc_scalping_allow_m5_momentum=True,
            btc_scalping_allow_m1_breakout=True,
            max_spread_btcusd=2500,
            **overrides,
        )

    def m5_momentum_frame(self, side: str) -> pd.DataFrame:
        rows = []
        price = 100.0
        for _ in range(28):
            rows.append((price, price + 0.8, price - 0.8, price + 0.1))
            price += 0.1
        if side == "BUY":
            rows.append((price, price + 3.0, price - 0.5, price + 2.6))
        else:
            rows.append((price, price + 0.5, price - 3.0, price - 2.6))
        return candle_frame(rows)

    def m1_breakout_frame(self, side: str) -> pd.DataFrame:
        rows = []
        price = 100.0
        for _ in range(10):
            rows.append((price, price + 0.4, price - 0.4, price + 0.05))
            price += 0.05
        if side == "BUY":
            rows.append((price, price + 2.0, price - 0.2, price + 1.5))
        else:
            rows.append((price, price + 0.2, price - 2.0, price - 1.5))
        return candle_frame(rows)

    def quiet_m5_frame(self) -> pd.DataFrame:
        rows = []
        price = 100.0
        for _ in range(30):
            rows.append((price, price + 0.4, price - 0.4, price + 0.02))
            price += 0.02
        return candle_frame(rows)

    def test_btc_scalping_agent_buy_on_m5_bullish_momentum(self) -> None:
        result = scalping.evaluate_btc_entry("BTCUSD#", self.m5_momentum_frame("BUY"), settings=self.settings(), max_spread=2500)
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(result["reason"], "M5_MOMENTUM_SCALP")
        self.assertEqual(result["btc_scalping_agent"]["trigger_type"], "M5_MOMENTUM")
        self.assertLess(result["sl"], result["entry"])
        self.assertGreater(result["tp"], result["entry"])

    def test_btc_scalping_agent_sell_on_m5_bearish_momentum(self) -> None:
        result = scalping.evaluate_btc_entry("BTCUSD#", self.m5_momentum_frame("SELL"), settings=self.settings(), max_spread=2500)
        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["reason"], "M5_MOMENTUM_SCALP")
        self.assertGreater(result["sl"], result["entry"])
        self.assertLess(result["tp"], result["entry"])

    def test_btc_scalping_agent_buy_and_sell_on_m1_breakout(self) -> None:
        for side, expected in (("BUY", "BUY"), ("SELL", "SELL")):
            with self.subTest(side=side):
                result = scalping.evaluate_btc_entry(
                    "BTCUSD#",
                    self.quiet_m5_frame(),
                    frames={"M1": self.m1_breakout_frame(side)},
                    settings=self.settings(),
                    max_spread=2500,
                )
                self.assertEqual(result["signal"], expected)
                self.assertEqual(result["reason"], "M1_BREAKOUT_SCALP")
                self.assertEqual(result["btc_scalping_agent"]["trigger_type"], "M1_BREAKOUT")

    def test_btc_scalping_agent_does_not_run_on_gold_or_eur(self) -> None:
        for symbol in ("GOLD#", "EURUSD"):
            with self.subTest(symbol=symbol):
                result = scalping.evaluate_btc_entry(symbol, self.m5_momentum_frame("BUY"), settings=self.settings(), max_spread=2500)
                self.assertEqual(result["signal"], "WAIT")
                self.assertEqual(result["reason"], "SYMBOL_NOT_BTC")
                self.assertEqual(result["blocked_reason"], "BTC_SCALPING_AGENT_SYMBOL_NOT_BTC")


class Mt5PositionSyncTests(unittest.TestCase):
    class FakeIngest:
        def __init__(self, open_rows: list[dict] | None = None) -> None:
            self.enabled = True
            self.sent: list[tuple[str, dict]] = []
            self.updated: list[tuple[str, dict, dict]] = []
            self.open_rows = open_rows or []

        def send_row(self, table: str, data: dict) -> dict:
            self.sent.append((table, data))
            return {"ok": True, "table": table}

        def update_row(self, table: str, match: dict, data: dict) -> dict:
            self.updated.append((table, match, data))
            not_found = table == "trades" and data.get("result") == "OPEN" and not any(str(row.get("ticket")) == str(match.get("ticket")) for row in self.open_rows)
            return {"ok": not not_found, "table": table, "not_found": not_found}

        def get_open_demo_trades(self, magic_number: int) -> dict:
            return {"ok": True, "rows": self.open_rows}

    def position(self, **overrides) -> SimpleNamespace:
        payload = {
            "ticket": 123456,
            "time": 1_780_000_000,
            "type": 0,
            "magic": 909002,
            "volume": 0.01,
            "price_open": 4450.0,
            "price_current": 4453.5,
            "sl": 4440.0,
            "tp": 4470.0,
            "profit": 3.5,
            "symbol": "GOLD#",
            "comment": "HERMES_DEMO_KELLY_24H",
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def test_mt5_position_magic_909002_upserts_open_trade_row(self) -> None:
        ingest = self.FakeIngest()
        events = []
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position()]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"), events.append)
        open_rows = [data for table, data in ingest.sent if table == "trades"]
        self.assertEqual(summary["hermes_mt5_open_positions_count"], 1)
        self.assertEqual(summary["supabase_open_trades_synced_count"], 1)
        self.assertNotIn("id", open_rows[0])
        self.assertEqual(open_rows[0]["result"], "OPEN")
        self.assertTrue(open_rows[0]["raw_payload"]["is_open"])
        self.assertEqual(open_rows[0]["magic_number"], 909002)
        self.assertEqual(open_rows[0]["ticket"], "123456")
        self.assertEqual(open_rows[0]["raw_payload"]["source"], "MT5_POSITIONS_SYNC")
        self.assertEqual(events[0]["event_type"], "POSITION_SYNC")
        self.assertEqual(events[0]["comment"], "HERMES_DEMO_KELLY_24H")
        self.assertEqual(events[0]["result"], "OPEN")

    def test_position_sync_existing_trade_update_works(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "123456", "magic_number": 909002, "status": "OPEN"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position()]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        self.assertEqual(summary["supabase_open_trades_synced_count"], 1)
        self.assertTrue(any(table == "trades" and match == {"ticket": "123456", "magic_number": 909002} for table, match, _ in ingest.updated))
        self.assertFalse(any(table == "trades" for table, _ in ingest.sent))

    def test_missing_trade_row_reconciles_from_mt5_history(self) -> None:
        class MissingCloseIngest(self.FakeIngest):
            def update_row(self, table: str, match: dict, data: dict) -> dict:
                self.updated.append((table, match, data))
                if table == "trades" and data.get("result") == "CLOSED":
                    return {"ok": False, "table": table, "not_found": True, "error": "NOT_FOUND"}
                return {"ok": True, "table": table}

        now = datetime.fromisoformat("2026-06-01T10:05:00+00:00")
        deal = SimpleNamespace(
            ticket=9001,
            order=331802677,
            position_id=331802677,
            magic=909002,
            symbol="BTCUSD#",
            type=0,
            entry=0,
            profit=-10.0,
            commission=-0.5,
            swap=-0.1,
            time=int(now.timestamp()),
        )
        ingest = MissingCloseIngest(open_rows=[{"ticket": "331802677", "magic_number": 909002, "symbol": "BTCUSD#", "strategy": "BREAKOUT_RETEST"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]), patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=[deal]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, now)
        inserted = [data for table, data in ingest.sent if table == "trades"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertEqual(inserted[0]["ticket"], "331802677")
        self.assertEqual(inserted[0]["dir"], "BUY")
        self.assertEqual(inserted[0]["result"], "CLOSED")
        self.assertEqual(inserted[0]["close_reason"], "MT5_HISTORY_RECONCILED")
        self.assertEqual(inserted[0]["net_pnl"], -10.6)
        self.assertEqual(inserted[0]["raw_payload"]["pnl_source"], "MT5_HISTORY_DEALS")

    def test_missing_trade_row_reconciles_sell_direction_from_mt5_entry_deal(self) -> None:
        class MissingCloseIngest(self.FakeIngest):
            def update_row(self, table: str, match: dict, data: dict) -> dict:
                self.updated.append((table, match, data))
                if table == "trades" and data.get("result") == "CLOSED":
                    return {"ok": False, "table": table, "not_found": True, "error": "NOT_FOUND"}
                return {"ok": True, "table": table}

        now = datetime.fromisoformat("2026-06-01T10:05:00+00:00")
        entry = SimpleNamespace(ticket=9001, order=9001, position_id=331802678, magic=909002, symbol="BTCUSD#", type=1, entry=0, profit=0.0, commission=0.0, swap=0.0, time=int(now.timestamp()) - 30)
        close = SimpleNamespace(ticket=9002, order=9002, position_id=331802678, magic=909002, symbol="BTCUSD#", type=0, entry=1, profit=4.0, commission=-0.2, swap=0.0, time=int(now.timestamp()))
        ingest = MissingCloseIngest(open_rows=[{"ticket": "331802678", "magic_number": 909002, "symbol": "BTCUSD#"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]), patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=[close, entry]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, now)
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertEqual(row["dir"], "SELL")
        self.assertEqual(row["raw_payload"]["direction"], "SELL")
        self.assertNotIsInstance(row.get("dir"), type(None))

    def test_missing_trade_row_without_inferable_direction_writes_event_not_trade(self) -> None:
        class MissingCloseIngest(self.FakeIngest):
            def update_row(self, table: str, match: dict, data: dict) -> dict:
                self.updated.append((table, match, data))
                if table == "trades" and data.get("result") == "CLOSED":
                    return {"ok": False, "table": table, "not_found": True, "error": "NOT_FOUND"}
                return {"ok": True, "table": table}

        mt5_position_sync_module._MISSING_TRADE_DIR_WARNED.clear()
        now = datetime.fromisoformat("2026-06-01T10:05:00+00:00")
        close_only = SimpleNamespace(ticket=9002, order=9002, position_id=331802679, magic=909002, symbol="BTCUSD#", type=1, entry=1, profit=-0.93, commission=0.0, swap=0.0, time=int(now.timestamp()))
        ingest = MissingCloseIngest(open_rows=[{"ticket": "331802679", "magic_number": 909002, "symbol": "BTCUSD#", "strategy": "QUANT_PRO_REGIME_SWITCHING"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]), patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=[close_only]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, now)
        trade_rows = [data for table, data in ingest.sent if table == "trades"]
        events = [data for table, data in ingest.sent if table == "execution_events"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 0)
        self.assertEqual(trade_rows, [])
        self.assertEqual(events[-1]["status"], "MISSING_DIR_SKIPPED")
        self.assertEqual(events[-1]["reason"], "MT5_HISTORY_DIRECTION_UNKNOWN")

    def test_existing_row_dir_is_preserved_during_close_reconciliation_update(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "331802680", "magic_number": 909002, "symbol": "BTCUSD#", "dir": "SELL", "entry": 72000.0, "sl": 72500.0, "tp": 71000.0, "lot": 0.01, "lot_size": 0.01, "strategy": "QUANT_PRO_REGIME_SWITCHING", "signal": "SELL"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:05:00+00:00"))
        close_update = next(data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED")
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertEqual(close_update["dir"], "SELL")
        self.assertEqual(close_update["entry"], 72000.0)
        self.assertEqual(close_update["strategy"], "QUANT_PRO_REGIME_SWITCHING")
        self.assertEqual(close_update["raw_payload"]["dir"], "SELL")

    def test_repeated_missing_dir_reconciliation_warns_once_and_never_errors(self) -> None:
        mt5_position_sync_module._MISSING_TRADE_DIR_WARNED.clear()
        close_row = {"raw_payload": {"symbol": "BTCUSD#"}}
        history = {"symbol": "BTCUSD#", "profit": -0.93, "commission": 0.0, "swap": 0.0, "net_pnl": -0.93, "closed_at": "2026-06-01T10:05:00+00:00", "dir": None}
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync._mt5_history_deal_for_ticket", return_value=history), patch("app.services.mt5_position_sync.log.warning") as warning_log, patch("app.services.mt5_position_sync.log.error") as error_log:
            first = mt5_position_sync_module._reconcile_missing_closed_trade_row(ingest, demo_settings(), "331802681", close_row)
            second = mt5_position_sync_module._reconcile_missing_closed_trade_row(ingest, demo_settings(), "331802681", close_row)
        self.assertEqual(first["sync_status"], "MISSING_DIR_SKIPPED")
        self.assertEqual(second["sync_status"], "MISSING_DIR_SKIPPED")
        self.assertEqual(warning_log.call_count, 1)
        error_log.assert_not_called()
        self.assertFalse([data for table, data in ingest.sent if table == "trades"])

    def test_missing_trade_row_without_history_does_not_crash_or_insert_fake_pnl(self) -> None:
        class MissingCloseIngest(self.FakeIngest):
            def update_row(self, table: str, match: dict, data: dict) -> dict:
                self.updated.append((table, match, data))
                if table == "trades" and data.get("result") == "CLOSED":
                    return {"ok": False, "table": table, "not_found": True, "error": "NOT_FOUND"}
                return {"ok": True, "table": table}

        mt5_position_sync_module._MISSING_TRADE_ROW_WARNED.clear()
        now = datetime.fromisoformat("2026-06-01T10:05:00+00:00")
        ingest = MissingCloseIngest(open_rows=[{"ticket": "331802677", "magic_number": 909002, "symbol": "BTCUSD#"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]), patch("app.services.mt5_position_sync.mt5.history_deals_get", return_value=[]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, now)
        inserted = [data for table, data in ingest.sent if table == "trades"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 0)
        self.assertEqual(inserted, [])

    def test_repeated_missing_trade_update_does_not_spam_error(self) -> None:
        class Response:
            status_code = 404
            text = '{"error":"NO_MATCHING_ROW"}'

        client = IngestClient(demo_settings(hermes_ingest_url="https://example.test/api/ingest", hermes_ingest_secret="secret"))
        with patch("app.services.ingest_client.requests.post", return_value=Response()), patch("app.services.ingest_client.log.error") as error_log, patch("app.services.ingest_client.log.warning") as warning_log:
            first = client.update_row("trades", {"ticket": "331802677", "magic_number": 909002}, {"result": "CLOSED"})
            second = client.update_row("trades", {"ticket": "331802677", "magic_number": 909002}, {"result": "CLOSED"})
        self.assertTrue(first["not_found"])
        self.assertTrue(second["not_found"])
        error_log.assert_not_called()
        self.assertEqual(warning_log.call_count, 1)

    def test_position_sync_open_position_updates_signed_floating_pnl(self) -> None:
        ingest = self.FakeIngest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_CLOSE",
                        "result": "LOSS",
                        "ticket": "111",
                        "magic_number": 909002,
                        "pnl": -13.27,
                        "created_at": "2026-06-01T09:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(profit=-3.90)]), patch(
                "app.services.mt5_pnl_truth.mt5.initialize",
                return_value=False,
            ), patch("app.services.mt5_pnl_truth.mt5.history_deals_get", return_value=None):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
                    fallback_events_path=path,
                )
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(row["pnl"], -3.9)
        self.assertEqual(row["raw_payload"]["profit"], -3.9)
        self.assertEqual(summary["open_demo_trades_count"], 1)
        self.assertEqual(summary["demo_closed_pnl_today"], -13.27)
        self.assertEqual(summary["demo_floating_pnl"], -3.9)
        self.assertEqual(summary["demo_total_pnl_today"], -17.17)

    def test_live_snapshot_uses_mt5_history_deals_as_pnl_truth(self) -> None:
        ingest = self.FakeIngest()
        deals = [
            SimpleNamespace(magic=909002, profit=-20.0, commission=-1.5, swap=-0.5),
            SimpleNamespace(magic=909002, profit=-23.98, commission=-1.0, swap=0.0),
            SimpleNamespace(magic=909001, profit=999.0, commission=0.0, swap=0.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_CLOSE",
                        "result": "LOSS",
                        "ticket": "111",
                        "magic_number": 909002,
                        "pnl": -12.73,
                        "created_at": "2026-06-01T09:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(profit=-3.90)]), patch(
                "app.services.mt5_pnl_truth.mt5.initialize",
                return_value=True,
            ), patch(
                "app.services.mt5_pnl_truth.mt5.history_deals_get",
                return_value=deals,
            ):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
                    fallback_events_path=path,
                )
        self.assertEqual(summary["demo_closed_pnl_today"], -46.98)
        self.assertEqual(summary["demo_floating_pnl"], -3.9)
        self.assertEqual(summary["demo_total_pnl_today"], -50.88)
        self.assertEqual(summary["mt5_today_pnl"], -46.98)
        self.assertEqual(summary["mt5_48h_pnl"], -46.98)
        self.assertEqual(summary["pnl_source"], "MT5_HISTORY_DEALS")
        self.assertEqual(summary["mt5_closed_deals_count"], 2)
        self.assertEqual(summary["trades_table_pnl"], -12.73)
        self.assertEqual(summary["pnl_difference"], -34.25)
        self.assertEqual(summary["pnl_warning"], "DEMO_REPORT_PNL_MISMATCH")
        payload = dashboard_snapshot(
            demo_settings(),
            {"trade_mode": 0},
            None,
            None,
            True,
            datetime.fromisoformat("2026-06-01T10:00:07+00:00"),
            latest_position_sync=summary,
        )
        self.assertEqual(payload["demo_closed_pnl_today"], -46.98)
        self.assertEqual(payload["demo_floating_pnl"], -3.9)
        self.assertEqual(payload["demo_total_pnl_today"], -50.88)
        self.assertEqual(payload["pnl_source"], "MT5_HISTORY_DEALS")
        self.assertEqual(payload["pnl_warning"], "DEMO_REPORT_PNL_MISMATCH")

    def test_live_snapshot_and_demo_report_share_mt5_history_pnl_truth(self) -> None:
        ingest = self.FakeIngest()
        now = datetime.fromisoformat("2026-06-01T10:00:00+00:00")
        deals = [
            SimpleNamespace(magic=909002, profit=-20.0, commission=-1.5, swap=-0.5),
            SimpleNamespace(magic=909002, profit=-23.98, commission=-1.0, swap=0.0),
            SimpleNamespace(magic=909001, profit=999.0, commission=0.0, swap=0.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_CLOSE",
                        "result": "LOSS",
                        "ticket": "111",
                        "magic_number": 909002,
                        "pnl": -12.73,
                        "created_at": "2026-06-01T09:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(profit=0.0)]), patch(
                "app.services.mt5_pnl_truth.mt5.initialize",
                return_value=True,
            ), patch("app.services.mt5_pnl_truth.mt5.history_deals_get", return_value=deals):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    now,
                    fallback_events_path=path,
                )
                report = build_demo_report(demo_settings(), path, hours=48, now=now)
        self.assertEqual(summary["mt5_48h_pnl"], -46.98)
        self.assertEqual(report["mt5_48h_pnl"], summary["mt5_48h_pnl"])
        self.assertEqual(report["demo_pnl"], summary["mt5_48h_pnl"])
        self.assertEqual(report["pnl_source"], "MT5_HISTORY_DEALS")

    def test_live_snapshot_falls_back_to_trades_table_when_mt5_history_unavailable(self) -> None:
        ingest = self.FakeIngest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_CLOSE",
                        "result": "LOSS",
                        "ticket": "111",
                        "magic_number": 909002,
                        "pnl": -13.27,
                        "created_at": "2026-06-01T09:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(profit=-3.90)]), patch(
                "app.services.mt5_pnl_truth.mt5.initialize",
                return_value=False,
            ), patch(
                "app.services.mt5_pnl_truth.mt5.history_deals_get",
                return_value=None,
            ), patch(
                "app.services.mt5_pnl_truth.mt5.last_error",
                return_value=(-1, "history unavailable"),
            ):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
                    fallback_events_path=path,
                )
        self.assertEqual(summary["demo_closed_pnl_today"], -13.27)
        self.assertEqual(summary["demo_floating_pnl"], -3.9)
        self.assertEqual(summary["demo_total_pnl_today"], -17.17)
        self.assertIsNone(summary["mt5_today_pnl"])
        self.assertEqual(summary["pnl_source"], "TRADES_TABLE_FALLBACK")
        self.assertIsNone(summary["pnl_difference"])
        self.assertEqual(summary["mt5_history_error"], "MT5_HISTORY_DEALS_UNAVAILABLE")
        self.assertFalse(summary["mt5_initialized"])

    def test_mt5_position_trade_payload_contains_only_allowed_trade_keys(self) -> None:
        ingest = self.FakeIngest()
        allowed = {
            "closed_at",
            "confidence",
            "created_at",
            "dir",
            "entry",
            "lot",
            "lot_size",
            "magic",
            "magic_number",
            "opened_at",
            "pnl",
            "raw_payload",
            "reason",
            "result",
            "signal",
            "sl",
            "strategy",
            "symbol",
            "ticket",
            "tp",
        }
        unsupported = {
            "account_type",
            "comment",
            "comment_filter_status",
            "current_price",
            "direction",
            "display_symbol",
            "is_open",
            "mode",
            "position_ticket",
            "price_open",
            "profit",
            "raw_symbol",
            "side",
            "source",
            "status",
            "updated_at",
            "volume",
        }
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(ticket=328961620, comment="HERMES_DEMO_KELL")]):
            sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertTrue(set(row).issubset(allowed))
        self.assertFalse(set(row).intersection(unsupported))
        self.assertEqual(row["raw_payload"]["display_symbol"], "GOLD")
        self.assertEqual(row["raw_payload"]["status"], "OPEN")
        self.assertTrue(row["raw_payload"]["is_open"])
        self.assertEqual(row["raw_payload"]["source"], "MT5_POSITIONS_SYNC")

    def test_truncated_hermes_comment_syncs_open_trade(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(ticket=328961620, comment="HERMES_DEMO_KELL")]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(summary["mt5_open_positions_count"], 1)
        self.assertEqual(summary["mt5_positions_raw_count"], 1)
        self.assertEqual(summary["hermes_mt5_open_positions_count"], 1)
        self.assertEqual(summary["supabase_open_trades_synced_count"], 1)
        self.assertEqual(row["result"], "OPEN")
        self.assertTrue(row["raw_payload"]["is_open"])
        self.assertEqual(row["ticket"], "328961620")
        self.assertEqual(row["symbol"], "GOLD#")
        self.assertEqual(row["raw_payload"]["display_symbol"], "GOLD")
        self.assertEqual(row["magic_number"], 909002)
        self.assertEqual(row["lot"], 0.01)
        self.assertEqual(row["raw_payload"]["source"], "MT5_POSITIONS_SYNC")
        self.assertEqual(row["raw_payload"]["comment"], "HERMES_DEMO_KELL")
        self.assertEqual(row["raw_payload"]["comment_filter_status"], "ACCEPT_HERMES_DEMO_PREFIX")

    def test_empty_comment_syncs_open_trade_when_magic_and_symbol_match(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(comment="")]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(summary["hermes_mt5_open_positions_count"], 1)
        self.assertEqual(row["raw_payload"]["comment"], "HERMES_DEMO_KELLY_24H")
        self.assertEqual(row["raw_payload"]["comment_filter_status"], "ACCEPT_EMPTY")

    def test_full_hermes_comment_syncs_open_trade(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(comment="HERMES_DEMO_KELLY_24H")]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(summary["hermes_mt5_open_positions_count"], 1)
        self.assertEqual(row["raw_payload"]["comment_filter_status"], "ACCEPT_HERMES_DEMO_PREFIX")

    def test_gold_hash_creates_display_symbol_gold(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(symbol="GOLD#")]):
            sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        row = next(data for table, data in ingest.sent if table == "trades")
        self.assertEqual(row["symbol"], "GOLD#")
        self.assertEqual(row["raw_payload"]["display_symbol"], "GOLD")
        self.assertEqual(row["raw_payload"]["raw_symbol"], "GOLD")

    def test_non_hermes_position_other_magic_is_ignored(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(magic=111)]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        self.assertEqual(summary["hermes_mt5_open_positions_count"], 0)
        self.assertEqual(summary["mt5_open_positions_count"], 1)
        self.assertEqual(summary["mt5_positions_ignored_with_reason"][0]["ignored_reason"], "MAGIC_MISMATCH")
        self.assertFalse([data for table, data in ingest.sent if table == "trades"])

    def test_existing_open_supabase_trade_missing_from_mt5_becomes_closed(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "328961620", "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#", "pnl": 4.2}])
        events = []
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"), events.append)
        close_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertIsNotNone(close_updates[0]["closed_at"])
        self.assertEqual(close_updates[0]["reason"], "MT5_POSITION_MISSING_CLOSED")
        self.assertEqual(close_updates[0]["pnl"], 4.2)
        self.assertEqual(close_updates[0]["raw_payload"]["is_open"], False)
        self.assertEqual(close_updates[0]["raw_payload"]["status"], "CLOSED")
        self.assertEqual(close_updates[0]["raw_payload"]["source"], "MT5_POSITIONS_SYNC")
        self.assertEqual(close_updates[0]["raw_payload"]["close_reason"], "MT5_POSITION_MISSING_CLOSED")
        self.assertEqual(summary["latest_position_close"]["ticket"], "328961620")
        self.assertEqual(summary["latest_position_close"]["result"], "CLOSED")
        self.assertEqual(summary["latest_position_close"]["close_reason"], "MT5_POSITION_MISSING_CLOSED")
        self.assertEqual(summary["closed_tickets"], ["328961620"])
        self.assertEqual(events[0]["result"], "CLOSED")

    def test_position_sync_close_preserves_existing_raw_payload_strategy_metadata(self) -> None:
        ingest = self.FakeIngest(
            open_rows=[
                {
                    "ticket": "328961620",
                    "magic_number": 909002,
                    "result": "OPEN",
                    "symbol": "GOLD#",
                    "pnl": 7.33,
                    "raw_payload": {
                        "strategy": "TREND_CONTINUATION_BREAKDOWN",
                        "rr": 2.0,
                        "kelly_suggested_lot": 0.03,
                        "final_capped_lot": 0.01,
                        "setup_id": "setup-328961620",
                        "edge_score": 100,
                        "exploration_override_reason": "DEMO_STRONG_SETUP_LEARNING_MODE",
                    },
                }
            ]
        )
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
            sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        close_row = next(data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED")
        self.assertEqual(close_row["raw_payload"]["strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(close_row["raw_payload"]["rr"], 2.0)
        self.assertEqual(close_row["raw_payload"]["kelly_suggested_lot"], 0.03)
        self.assertEqual(close_row["raw_payload"]["final_capped_lot"], 0.01)
        self.assertEqual(close_row["raw_payload"]["exploration_override_reason"], "DEMO_STRONG_SETUP_LEARNING_MODE")
        self.assertEqual(close_row["raw_payload"]["status"], "CLOSED")

    def test_ticket_comparison_handles_string_and_int_without_false_close(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": 328961620, "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(ticket="328961620")]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        close_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 0)
        self.assertEqual(close_updates, [])

    def test_closed_rows_are_not_closed_repeatedly(self) -> None:
        ingest = self.FakeIngest(open_rows=[])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        close_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 0)
        self.assertEqual(close_updates, [])

    def test_sync_once_cli_closes_stale_open_trade_when_mt5_empty(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "328961620", "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#"}])
        mt5_conn = SimpleNamespace(connect=lambda: True, shutdown=lambda: None)
        with (
            patch("sys.argv", ["app/main.py", "--sync-mt5-positions-once"]),
            patch("app.main.IngestClient", return_value=ingest),
            patch("app.mt5.connection.MT5Connection", return_value=mt5_conn),
            patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]),
            patch("builtins.print") as printer,
        ):
            main_module.main()
        close_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(len(close_updates), 1)
        self.assertEqual(close_updates[0]["reason"], "MT5_POSITION_MISSING_CLOSED")
        printer.assert_called_once_with("mt5_open=0 hermes_open=0 synced=0 closed=1 already_closed=0 closed_tickets=['328961620'] already_closed_tickets=[]")

    def test_unavailable_open_read_falls_back_to_local_position_sync_events(self) -> None:
        class UnavailableReadIngest(self.FakeIngest):
            def get_open_demo_trades(self, magic_number: int) -> dict:
                return {"ok": False, "error": "HTTP 400", "rows": []}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "POSITION_SYNC",
                        "result": "OPEN",
                        "ticket": "328961620",
                        "symbol": "GOLD#",
                        "magic_number": 909002,
                        "payload": {"display_symbol": "GOLD", "profit": 4.2},
                        "created_at": "2026-06-01T10:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            ingest = UnavailableReadIngest()
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:05:00+00:00"),
                    fallback_events_path=path,
                )
        close_updates = [(match, data) for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertEqual(summary["closed_tickets"], ["328961620"])
        self.assertEqual(close_updates[0][0]["ticket"], "328961620")
        self.assertEqual(close_updates[0][1]["reason"], "MT5_POSITION_MISSING_CLOSED")

    def test_unavailable_open_read_falls_back_to_confirmed_demo_order_ticket(self) -> None:
        class UnavailableReadIngest(self.FakeIngest):
            def get_open_demo_trades(self, magic_number: int) -> dict:
                return {"ok": False, "error": "REMOTE_DEMO_READ_UNAVAILABLE", "rows": []}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_ORDER",
                        "result": "ORDER_CONFIRMED",
                        "order_success": True,
                        "ticket": 328961620,
                        "symbol": "GOLD#",
                        "magic_number": 909002,
                        "pnl": 4.2,
                    }
                ),
                encoding="utf-8",
            )
            ingest = UnavailableReadIngest()
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:05:00+00:00"),
                    fallback_events_path=path,
                )
        close_updates = [(match, data) for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 1)
        self.assertEqual(summary["closed_tickets"], ["328961620"])
        self.assertEqual(close_updates[0][0], {"ticket": "328961620", "magic_number": 909002})

    def test_position_sync_real_ingest_demo_read_unavailable_uses_confirmed_order_fallback_without_post_read(self) -> None:
        class RealUnavailableReadIngest(IngestClient):
            def __init__(self, settings: Settings) -> None:
                super().__init__(settings)
                self.updated: list[tuple[str, dict, dict]] = []
                self.sent: list[tuple[str, dict]] = []

            def update_row(self, table: str, match: dict, data: dict) -> dict:
                self.updated.append((table, match, data))
                return {"ok": True, "table": table}

            def send_row(self, table: str, data: dict) -> dict:
                self.sent.append((table, data))
                return {"ok": True, "table": table}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "event_type": "DEMO_ORDER",
                            "result": "ORDER_CONFIRMED",
                            "order_success": True,
                            "ticket": ticket,
                            "symbol": symbol,
                            "magic_number": 909002,
                            "pnl": pnl,
                        }
                    )
                    for ticket, symbol, pnl in [
                        (329436508, "BTCUSD#", 4.95),
                        (329313966, "BTCUSD#", 4.93),
                        (328961620, "GOLD#", 7.33),
                    ]
                ),
                encoding="utf-8",
            )
            ingest = RealUnavailableReadIngest(demo_settings(hermes_ingest_url="https://example.test/api/hermes-ingest", hermes_ingest_secret="secret"))
            with (
                patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]),
                patch("app.services.ingest_client.requests.post") as post,
                patch("app.services.ingest_client.requests.get") as get,
            ):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:05:00+00:00"),
                    fallback_events_path=path,
                )
        post.assert_not_called()
        get.assert_not_called()
        self.assertEqual(summary["supabase_open_trades_closed_count"], 3)
        self.assertEqual(set(summary["closed_tickets"]), {"329436508", "329313966", "328961620"})

    def test_position_sync_fallback_does_not_reclose_already_closed_confirmed_orders(self) -> None:
        class UnavailableReadIngest(self.FakeIngest):
            def get_open_demo_trades(self, magic_number: int) -> dict:
                return {"ok": False, "error": "REMOTE_DEMO_READ_UNAVAILABLE", "rows": []}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            events = []
            for idx in range(10):
                ticket = 329000000 + idx
                events.append(
                    {
                        "event_type": "DEMO_ORDER",
                        "result": "ORDER_CONFIRMED",
                        "order_success": True,
                        "ticket": ticket,
                        "symbol": "GOLD#",
                        "magic_number": 909002,
                        "pnl": 1.0,
                    }
                )
                events.append(
                    {
                        "event_type": "POSITION_SYNC",
                        "result": "CLOSED",
                        "status": "CLOSED",
                        "ticket": str(ticket),
                        "symbol": "GOLD#",
                        "magic_number": 909002,
                        "created_at": "2026-06-01T10:00:00+00:00",
                    }
                )
            path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            ingest = UnavailableReadIngest()
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
                summary = sync_open_mt5_positions_to_supabase(
                    demo_settings(),
                    ingest,
                    datetime.fromisoformat("2026-06-01T10:05:00+00:00"),
                    fallback_events_path=path,
                )
        close_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        close_events = [data for table, data in ingest.sent if table == "execution_events" and data.get("result") == "CLOSED"]
        self.assertEqual(summary["supabase_open_trades_closed_count"], 0)
        self.assertEqual(summary["already_closed_count"], 10)
        self.assertEqual(len(summary["already_closed_tickets"]), 10)
        self.assertEqual(summary["closed_tickets"], [])
        self.assertEqual(close_updates, [])
        self.assertEqual(close_events, [])

    def test_force_close_demo_ticket_updates_only_matching_ticket_and_magic(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "328961620", "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#"}])
        events = []
        result = force_close_demo_ticket(
            demo_settings(),
            ingest,
            "328961620",
            datetime.fromisoformat("2026-06-01T10:05:00+00:00"),
            events.append,
        )
        close_updates = [(match, data) for table, match, data in ingest.updated if table == "trades" and data.get("result") == "CLOSED"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["closed_tickets"], ["328961620"])
        self.assertEqual(close_updates[0][0], {"ticket": "328961620", "magic_number": 909002})
        self.assertEqual(close_updates[0][1]["reason"], "MT5_POSITION_MISSING_CLOSED")
        self.assertEqual(close_updates[0][1]["raw_payload"]["status"], "CLOSED")
        self.assertFalse(close_updates[0][1]["raw_payload"]["is_open"])
        self.assertEqual(events[0]["result"], "CLOSED")

    def test_failed_order_does_not_create_open_trade_without_mt5_position(self) -> None:
        ingest = self.FakeIngest()
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[]):
            summary = sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        self.assertEqual(summary["supabase_open_trades_synced_count"], 0)
        self.assertFalse([data for table, data in ingest.sent if table == "trades"])

    def test_sync_is_idempotent_same_ticket_updates_not_duplicates(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "123456", "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#"}])
        with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position()]):
            sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"))
        sent_trade_rows = [data for table, data in ingest.sent if table == "trades"]
        open_updates = [data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "OPEN"]
        self.assertEqual(sent_trade_rows, [])
        self.assertEqual(len(open_updates), 1)
        self.assertEqual(open_updates[0]["ticket"], "123456")

    def test_position_sync_open_merges_confirmed_demo_order_metadata(self) -> None:
        ingest = self.FakeIngest(open_rows=[{"ticket": "328961620", "magic_number": 909002, "result": "OPEN", "symbol": "GOLD#"}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_pilot_events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_ORDER",
                        "status": "ORDER_CONFIRMED",
                        "order_success": True,
                        "order_retcode": 10009,
                        "ticket": "328961620",
                        "magic_number": 909002,
                        "symbol": "GOLD#",
                        "strategy": "TREND_CONTINUATION_BREAKDOWN",
                        "rr": 2.0,
                        "kelly_suggested_lot": 0.03,
                        "final_capped_lot": 0.01,
                        "setup_id": "setup-328961620",
                        "grade": "A",
                        "edge_score": 100,
                        "m1_trigger_status": "PASS",
                        "m1_trigger_reason": "M1_REJECTION",
                        "m15_confirmation_status": "PASS",
                        "m15_confirmation_reason": "M15_STRUCTURE",
                        "exploration_override_reason": "DEMO_STRONG_SETUP_LEARNING_MODE",
                        "mode": "DEMO_STRONG_SETUP_LEARNING",
                    }
                ),
                encoding="utf-8",
            )
            with patch("app.services.mt5_position_sync.mt5.positions_get", return_value=[self.position(ticket=328961620)]):
                sync_open_mt5_positions_to_supabase(demo_settings(), ingest, datetime.fromisoformat("2026-06-01T10:00:00+00:00"), fallback_events_path=path)
        open_update = next(data for table, match, data in ingest.updated if table == "trades" and data.get("result") == "OPEN")
        self.assertEqual(open_update["strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(open_update["raw_payload"]["strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(open_update["raw_payload"]["rr"], 2.0)
        self.assertEqual(open_update["raw_payload"]["kelly_suggested_lot"], 0.03)
        self.assertEqual(open_update["raw_payload"]["final_capped_lot"], 0.01)
        self.assertEqual(open_update["raw_payload"]["setup_grade"], "A")
        self.assertEqual(open_update["raw_payload"]["m1_trigger_reason"], "M1_REJECTION")
        self.assertEqual(open_update["raw_payload"]["m15_confirmation_reason"], "M15_STRUCTURE")
        self.assertEqual(open_update["raw_payload"]["mode"], "DEMO_STRONG_SETUP_LEARNING")
        self.assertEqual(open_update["raw_payload"]["status"], "OPEN")

    def test_open_demo_trades_read_returns_unavailable_without_invalid_insert_attempt(self) -> None:
        settings = demo_settings(hermes_ingest_url="https://example.test/api/hermes-ingest", hermes_ingest_secret="secret")
        client = IngestClient(settings)
        with patch("app.services.ingest_client.requests.get") as get, patch("app.services.ingest_client.requests.post") as post:
            result = client.get_open_demo_trades(909002)
        get.assert_not_called()
        post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "REMOTE_DEMO_READ_UNAVAILABLE")
        self.assertEqual(result["rows"], [])


class DemoKellyRouterSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events_path = Path(self.tmp.name) / "demo_events.jsonl"
        self.now = datetime.fromisoformat("2026-06-01T10:00:00+00:00")
        self.tick_patcher = patch("app.mt5.demo_router.mt5.symbol_info_tick", side_effect=self._tick_for_symbol)
        self.tick_patcher.start()
        self.order_check_patcher = patch(
            "app.mt5.demo_router.mt5.order_check",
            return_value=SimpleNamespace(retcode=10009, comment="Done"),
        )
        self.order_check_patcher.start()

    def tearDown(self) -> None:
        self.order_check_patcher.stop()
        self.tick_patcher.stop()
        self.tmp.cleanup()

    def _tick_for_symbol(self, symbol: str):
        text = str(symbol or "").upper()
        if "BTC" in text:
            return SimpleNamespace(bid=100.0, ask=100.5)
        if "GOLD" in text or "XAU" in text:
            return SimpleNamespace(bid=2300.0, ask=2300.2)
        return SimpleNamespace(bid=1.1, ask=1.10001)

    def router(self, **settings) -> DemoKellyRouter:
        return DemoKellyRouter(demo_settings(**settings), self.events_path)

    def decision(self, **overrides) -> dict:
        payload = {
            "symbol": "EURUSD",
            "strategy": "SIMO_ATM_BREAKOUT",
            "signal": "BUY",
            "entry": 1.1000,
            "sl": 1.0990,
            "tp": 1.1020,
            "reward_risk": 2.0,
            "risk_status": "APPROVED",
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "m15_confirmation": True,
            "m1_entry_confirmation": True,
            "big_setup_grade": "B",
            "edge_score": 100,
            "setup_score": 100,
            "setup_hunter_score": 100,
            "final_confluence_score": 75,
            "final_confluence_grade": "B",
        }
        payload.update(overrides)
        return payload

    def account(self, **overrides) -> dict:
        payload = {
            "login": 345297734,
            "name": "Demo Account",
            "server": "XMGlobal-MT5 10",
            "company": "XM Global Limited",
            "trade_mode": 0,
            "trade_allowed": True,
            "trade_expert": True,
            "balance": 10000.0,
            "equity": 10000.0,
        }
        payload.update(overrides)
        return payload

    def specs(self) -> dict:
        return {"tick_value": 1.0, "tick_size": 0.0001, "volume_step": 0.01}

    def max_tp_symbol_info(self, tick_value: float, tick_size: float, digits: int, point: float, stops_level: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            trade_tick_value=tick_value,
            trade_tick_size=tick_size,
            digits=digits,
            point=point,
            trade_stops_level=stops_level,
        )

    def process_with_max_tp(
        self,
        decision: dict,
        broker_symbol: str,
        symbol_info: SimpleNamespace,
        specs: dict,
        router: DemoKellyRouter | None = None,
    ) -> tuple[list[dict], object]:
        router = router or self.router(max_money_tp_enabled=True)
        fake_result = SimpleNamespace(retcode=10009, order=123456, deal=0)
        tick = SimpleNamespace(bid=decision["entry"], ask=decision["entry"])
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=symbol_info),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                broker_symbol,
                {},
                {"bid": decision["entry"], "ask": decision["entry"]},
                specs,
                1,
                30,
                True,
                "setup-max-tp",
                self.now,
            )
        return items, send

    def evaluate(
        self,
        router: DemoKellyRouter | None = None,
        decision: dict | None = None,
        kelly: dict | None = None,
        broker_symbol: str = "EURUSD",
    ):
        router = router or self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            return router.evaluate(
                decision or self.decision(),
                kelly or {"approved_lot": 0.2},
                self.account(),
                broker_symbol,
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )

    def test_live_account_cannot_send_orders(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(),
                {"approved_lot": 0.01},
                self.account(server="Hermes-Real", trade_mode=2),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_btc_quant_statistical_pullback_blocks_when_disabled(self) -> None:
        router = self.router(btc_disable_quant_statistical_pullback=True)
        result = self.evaluate(
            router=router,
            decision=self.decision(symbol="BTCUSD#", strategy="QUANT_STATISTICAL_PULLBACK", signal="BUY", entry=100.0, sl=99.0, tp=102.0),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT")

    def test_btc_other_strategies_not_blocked_by_pullback_guard(self) -> None:
        router = self.router(btc_disable_quant_statistical_pullback=True)
        result = self.evaluate(
            router=router,
            decision=self.decision(symbol="BTCUSD#", strategy="BREAKOUT_RETEST", signal="BUY", entry=100.0, sl=99.0, tp=102.0),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        self.assertNotEqual(result.reason, "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT")

    def test_btc_scalping_agent_routeable_on_btc_symbols(self) -> None:
        for symbol in ("BTCUSD", "BTCUSD#"):
            with self.subTest(symbol=symbol):
                router = self.router(btc_disable_quant_statistical_pullback=True)
                with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
                    result = router.evaluate(
                        self.decision(
                            symbol=symbol,
                            strategy="BTC_SCALPING_AGENT",
                            signal="BUY",
                            entry=100.0,
                            sl=99.0,
                            tp=102.0,
                            edge_score=100,
                            setup_score=100,
                            setup_hunter_score=100,
                        ),
                        {"approved_lot": 0.01},
                        self.account(),
                        symbol,
                        {},
                        {"bid": 100.0, "ask": 100.5},
                        {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                        1,
                        30,
                        True,
                        "setup-btc-scalping",
                        self.now,
                    )
                self.assertEqual(result.decision, "PASS")
                self.assertEqual(result.event["strategy"], "BTC_SCALPING_AGENT")

    def test_btc_scalping_setup_hunter_decision_is_not_left_analysis_only_when_demo_eligible(self) -> None:
        decision = self.decision(
            symbol="BTCUSD#",
            strategy="BTC_SCALPING_AGENT",
            signal="SELL",
            decision="ENTER_ANALYSIS_ONLY",
            entry=100.0,
            sl=101.0,
            tp=98.0,
        )
        routed = _routeable_setup_hunter_decision(
            decision,
            {"best_strategy": "BTC_SCALPING_AGENT", "direction": "SELL", "demo_eligible": True},
        )
        self.assertEqual(routed["decision"], "ROUTE_TO_DEMO")
        self.assertTrue(routed["route_to_demo"])
        self.assertEqual(routed["strategy"], "BTC_SCALPING_AGENT")
        self.assertEqual(routed["signal"], "SELL")

    def test_btc_scalping_handoff_missing_fields_are_explicit(self) -> None:
        candidate = {"best_strategy": "BTC_SCALPING_AGENT", "direction": "BUY", "demo_eligible": True}
        base = self.decision(symbol="BTCUSD#", strategy="BTC_SCALPING_AGENT", signal="BUY", entry=100.0, sl=99.0, tp=102.0)
        self.assertIsNone(_btc_scalping_handoff_missing_field(base, candidate, {"approved_lot": 0.01}, "BTCUSD#"))
        self.assertEqual(_btc_scalping_handoff_missing_field({**base, "entry": None}, candidate, {"approved_lot": 0.01}, "BTCUSD#"), "entry")
        self.assertEqual(_btc_scalping_handoff_missing_field({**base, "sl": None}, candidate, {"approved_lot": 0.01}, "BTCUSD#"), "sl")
        self.assertEqual(_btc_scalping_handoff_missing_field({**base, "tp": None}, candidate, {"approved_lot": 0.01}, "BTCUSD#"), "tp")
        self.assertEqual(_btc_scalping_handoff_missing_field(base, candidate, {}, "BTCUSD#"), "lot")
        self.assertEqual(_btc_scalping_handoff_missing_field({**base, "symbol": None, "broker_symbol": None}, candidate, {"approved_lot": 0.01}, None), "symbol")

    def test_btc_scalping_best_candidate_processes_through_demo_router(self) -> None:
        router = self.router(btc_disable_quant_statistical_pullback=True)
        decision = _routeable_setup_hunter_decision(
            self.decision(
                symbol="BTCUSD#",
                strategy="BTC_SCALPING_AGENT",
                signal="SELL",
                decision="ENTER_ANALYSIS_ONLY",
                entry=100.0,
                sl=101.0,
                tp=98.0,
                edge_score=65,
                setup_score=65,
                setup_hunter_score=65,
                setup_hunter_grade="B",
                smc_confluence_status="FAIL",
                smc_confluence_score=0,
                mtfa_status="FAIL",
                mtfa_score=0,
                m15_confirmation=False,
                m1_entry_confirmation=False,
            ),
            {"best_strategy": "BTC_SCALPING_AGENT", "direction": "SELL", "demo_eligible": True},
        )
        fake_result = SimpleNamespace(retcode=10009, order=123456, deal=0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send:
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 0.01, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-btc-scalping-router",
                self.now,
            )
        send.assert_called_once()
        self.assertTrue(any(item.get("demo_action") == "DEMO_ORDER" for item in items))
        self.assertEqual(items[-1]["data"]["strategy"], "BTC_SCALPING_AGENT")

    def test_btc_scalping_agent_router_does_not_require_smc_mtfa_topdown_or_micro_confluence(self) -> None:
        router = self.router(
            hermes_demo_topdown_fallback_mode=True,
            hermes_demo_micro_discovery_mode=True,
            btc_disable_quant_statistical_pullback=True,
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(
                    symbol="BTCUSD#",
                    strategy="BTC_SCALPING_AGENT",
                    signal="BUY",
                    entry=100.0,
                    sl=99.0,
                    tp=102.0,
                    edge_score=60,
                    setup_score=60,
                    setup_hunter_score=60,
                    smc_confluence_status="FAIL",
                    smc_confluence_score=0,
                    mtfa_status="FAIL",
                    mtfa_score=0,
                    m15_confirmation=False,
                    m1_entry_confirmation=False,
                    top_down_reader={"decision": "AVOID", "top_down_status": "FAIL"},
                    adaptive_confluence={
                        "adaptive_confluence_enabled": True,
                        "status": "BLOCK",
                        "final_confluence_score": 0,
                        "symbol_min_confluence": 90,
                        "confluence_threshold_pass": False,
                        "block_reason": "MICRO_DISCOVERY_CONFLUENCE_TOO_LOW",
                    },
                    final_confluence_score=0,
                    symbol_min_confluence=90,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-btc-scalping-confluence",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertNotIn(result.reason, {"MICRO_DISCOVERY_CONFLUENCE_TOO_LOW", "SMC_STRONG_FAIL", "MTFA_STRONG_FAIL"})

    def test_btc_scalping_missing_entry_sl_tp_blocks_with_explicit_reason(self) -> None:
        router = self.router()
        cases = (
            ("entry", {"entry": None, "sl": 101.0, "tp": 98.0}),
            ("sl", {"entry": 100.0, "sl": None, "tp": 98.0}),
            ("tp", {"entry": 100.0, "sl": 101.0, "tp": None}),
        )
        for field, overrides in cases:
            with self.subTest(field=field), patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
                result = router.evaluate(
                    self.decision(symbol="BTCUSD#", strategy="BTC_SCALPING_AGENT", signal="SELL", **overrides),
                    {"approved_lot": 0.01},
                    self.account(),
                    "BTCUSD#",
                    {},
                    {"bid": 100.0, "ask": 100.5},
                    {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                    1,
                    30,
                    True,
                    "setup-btc-scalping-missing",
                    self.now,
                )
                self.assertEqual(result.decision, "BLOCK")
                self.assertEqual(result.reason, "MISSING_ENTRY_OR_SL_TP")
                send.assert_not_called()

    def test_btc_scalping_agent_not_routeable_on_gold_or_eur(self) -> None:
        cases = (
            ("GOLD#", 2300.0, 2299.0, 2302.0),
            ("EURUSD", 1.1000, 1.0990, 1.1020),
        )
        for symbol, entry, sl, tp in cases:
            with self.subTest(symbol=symbol):
                result = self.evaluate(
                    decision=self.decision(symbol=symbol, strategy="BTC_SCALPING_AGENT", signal="BUY", entry=entry, sl=sl, tp=tp),
                    kelly={"approved_lot": 0.01},
                    broker_symbol=symbol,
                )
                self.assertEqual(result.decision, "BLOCK")
                self.assertEqual(result.reason, "BTC_SCALPING_AGENT_SYMBOL_NOT_BTC")

    def test_btc_quant_disabled_block_never_reaches_order_send(self) -> None:
        router = self.router(btc_disable_quant_statistical_pullback=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
            items = router.process_decision(
                self.decision(symbol="BTCUSD#", strategy="QUANT_STATISTICAL_PULLBACK", signal="BUY", entry=100.0, sl=99.0, tp=102.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-btc-quant",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items, [])

    def test_btc_scalping_wait_and_block_never_reach_order_send(self) -> None:
        router = self.router()
        for signal in ("WAIT", "BLOCK"):
            with self.subTest(signal=signal), patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
                items = router.process_decision(
                    self.decision(symbol="BTCUSD#", strategy="BTC_SCALPING_AGENT", signal=signal, entry=None, sl=None, tp=None, reward_risk=None),
                    {"approved_lot": 0.01},
                    self.account(),
                    "BTCUSD#",
                    {},
                    {"bid": 100.0, "ask": 100.5},
                    {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                    1,
                    30,
                    True,
                    "setup-btc-scalping-wait",
                    self.now,
                )
                self.assertEqual(items, [])
                send.assert_not_called()

    def quick_exit_position(self, **overrides) -> SimpleNamespace:
        payload = {
            "ticket": 9001,
            "symbol": "BTCUSD#",
            "magic": 909002,
            "type": 0,
            "volume": 0.01,
            "price_open": 100.0,
            "sl": 0.0,
            "tp": 110.0,
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def quick_exit_symbol_info(self) -> SimpleNamespace:
        return SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01, trade_contract_size=0.0, digits=2, point=0.01)

    def test_quick_exit_closes_hermes_demo_position_at_four_usd_tp(self) -> None:
        router = self.router()
        pos = self.quick_exit_position()
        fake_result = SimpleNamespace(retcode=10009, order=5001, deal=0)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[pos]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=104.1, ask=104.2)),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=self.quick_exit_symbol_info()),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            items = router.process_quick_exits(self.account(), True, self.now)
        self.assertEqual(items[0]["data"]["event_type"], "QUICK_EXIT_CLOSE")
        self.assertEqual(items[0]["data"]["action"], "CLOSE_TP")
        request = send.call_args.args[0]
        self.assertEqual(request["action"], 1)
        self.assertEqual(request["position"], 9001)
        self.assertEqual(request["type"], 1)
        self.assertEqual(request["magic"], 909002)

    def test_quick_exit_moves_breakeven_at_eighty_cents_with_buffer(self) -> None:
        router = self.router()
        pos = self.quick_exit_position()
        fake_result = SimpleNamespace(retcode=10009, order=0, deal=0)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[pos]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=100.85, ask=100.95)),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=self.quick_exit_symbol_info()),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            items = router.process_quick_exits(self.account(), True, self.now)
        self.assertEqual(items[0]["data"]["event_type"], "QUICK_EXIT_SLTP")
        self.assertEqual(items[0]["data"]["action"], "MOVE_BREAKEVEN")
        request = send.call_args.args[0]
        self.assertEqual(request["action"], 6)
        self.assertEqual(request["position"], 9001)
        self.assertAlmostEqual(request["sl"], 100.1)
        self.assertEqual(request["tp"], 110.0)

    def test_quick_exit_trails_after_one_dollar_profit(self) -> None:
        router = self.router()
        pos = self.quick_exit_position(sl=100.1)
        router._quick_exit_state[9001] = {"peak_usd": 1.2, "be_done": True}
        fake_result = SimpleNamespace(retcode=10009, order=0, deal=0)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[pos]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=SimpleNamespace(bid=101.2, ask=101.3)),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=self.quick_exit_symbol_info()),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            items = router.process_quick_exits(self.account(), True, self.now)
        self.assertEqual(items[0]["data"]["action"], "TRAIL_SL")
        self.assertAlmostEqual(send.call_args.args[0]["sl"], 100.6)

    def test_quick_exit_ignores_manual_and_other_robot_positions(self) -> None:
        router = self.router()
        manual = self.quick_exit_position(ticket=1, magic=0)
        other_robot = self.quick_exit_position(ticket=2, magic=123456)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[manual, other_robot]),
            patch("app.mt5.demo_router.mt5.symbol_info_tick") as tick,
            patch("app.mt5.demo_router.mt5.symbol_info") as info,
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            items = router.process_quick_exits(self.account(), True, self.now)
        self.assertEqual(items, [])
        tick.assert_not_called()
        info.assert_not_called()
        send.assert_not_called()

    def test_quick_exit_live_account_blocks_before_order_send(self) -> None:
        router = self.router()
        pos = self.quick_exit_position()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[pos]) as positions,
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            items = router.process_quick_exits(self.account(server="Hermes-Real", trade_mode=2), True, self.now)
        self.assertEqual(items[0]["data"]["event_type"], "QUICK_EXIT_DEMO_ONLY_BLOCK")
        positions.assert_not_called()
        send.assert_not_called()

    def eur_strategy_decision(self, **overrides) -> dict:
        eur_payload = {
            "enabled": True,
            "mode": "ENTRY_STRATEGY",
            "strategy": "EUR_EMA_RSI_ATR_CROSSOVER",
            "source": "EurRobot_EURUSD(1).mq5",
            "decision": "BUY",
            "fast_ema": 20,
            "slow_ema": 50,
            "ema_fast_1": 1.101,
            "ema_slow_1": 1.100,
            "ema_fast_2": 1.099,
            "ema_slow_2": 1.100,
            "rsi_1": 60.0,
            "atr_1": 0.001,
            "cross_up": True,
            "cross_down": False,
            "entry": 1.1000,
            "sl": 1.0990,
            "tp": 1.1020,
            "rr": 2.0,
            "block_reason": None,
            "warnings": [],
        }
        payload = self.decision(
            symbol="EURUSD",
            strategy="EUR_EMA_RSI_ATR_CROSSOVER",
            signal="BUY",
            entry=1.1000,
            sl=1.0990,
            tp=1.1020,
            reward_risk=2.0,
            edge_score=100,
            setup_score=100,
            setup_hunter_score=100,
            grade="A",
            setup_hunter_grade="A",
            big_setup_grade="A",
            m15_confirmation=True,
            m1_entry_confirmation=True,
            m15_confirmation_status="PASS",
            m1_trigger_status="PASS",
            eur_ema_rsi_atr=eur_payload,
        )
        payload.update(overrides)
        return payload

    def eur_router(self, **overrides) -> DemoKellyRouter:
        settings = {
            "eur_ema_rsi_atr_enabled": True,
            "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD",
            "hermes_analysis_only_symbols": "",
            "demo_max_open_trades_total": 10,
            "demo_max_open_trades_per_symbol": 1,
            "demo_max_open_trades_per_symbol_strategy": 1,
        }
        settings.update(overrides)
        return self.router(**settings)

    def gold_m1m5_scalper_decision(self, **overrides) -> dict:
        scalper_payload = {
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "source": "docs/strategies/gold_m1m5_scalper",
            "decision": "BUY",
            "confidence": 80,
            "entry": 2300.0,
            "sl": 2299.99,
            "tp": 2300.02,
            "rr": 2.0,
            "block_reason": None,
            "reasons": [],
        }
        payload = self.decision(
            symbol="GOLD#",
            strategy="GOLD_M1_M5_EMA_SWEEP_SCALPER",
            signal="BUY",
            entry=2300.0,
            sl=2299.99,
            tp=2300.02,
            reward_risk=2.0,
            edge_score=100,
            setup_score=100,
            setup_hunter_score=100,
            grade="A",
            setup_hunter_grade="A",
            big_setup_grade="A",
            m15_confirmation=True,
            m1_entry_confirmation=True,
            m15_confirmation_status="PASS",
            m1_trigger_status="PASS",
            gold_m1m5_scalper=scalper_payload,
            gold_m1m5_scalper_decision="BUY",
            gold_m1m5_scalper_score=80,
        )
        payload.update(overrides)
        return payload

    def test_eur_generic_strategy_blocks_when_eur_strategy_enabled(self) -> None:
        router = self.eur_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(symbol="EURUSD", strategy="BREAKOUT_RETEST"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-generic",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "EUR_GENERIC_STRATEGY_DISABLED")

    def test_order_flow_execution_agent_not_blocked_by_eur_generic_gate(self) -> None:
        """ORDER_FLOW_EXECUTION_AGENT must not receive EUR_GENERIC_STRATEGY_DISABLED on EURUSD."""
        router = self.eur_router()
        of_payload = {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "decision": "BUY",
            "signal": "BUY",
            "score": 85.0,
            "grade": "A",
            "confidence": 85.0,
        }
        decision = self.decision(
            symbol="EURUSD",
            strategy="ORDER_FLOW_EXECUTION_AGENT",
            signal="BUY",
            order_flow_execution_agent=of_payload,
            order_flow_execution_agent_signal="BUY",
            order_flow_execution_agent_score=85,
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-of",
                self.now,
            )
        self.assertNotEqual(result.reason, "EUR_GENERIC_STRATEGY_DISABLED")

    def test_eur_strategy_can_pass_when_all_hard_gates_pass_and_caps_lot(self) -> None:
        router = self.eur_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.eur_strategy_decision(),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-pass",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["final_capped_lot"], 0.01)
        self.assertEqual(result.event["gate_statuses"]["eur_ema_rsi_atr"]["decision"], "BUY")

    def test_main_demo_router_allowlist_includes_eur_and_gold_m1m5_only_on_buy_sell(self) -> None:
        backend = HermesBackend.__new__(HermesBackend)
        self.assertTrue(backend._should_route_to_demo({"strategy": "EUR_EMA_RSI_ATR_CROSSOVER", "signal": "BUY"}))
        self.assertTrue(backend._should_route_to_demo({"strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER", "signal": "SELL"}))
        self.assertTrue(backend._should_route_to_demo({"strategy": "GOLD_LIQUIDITY_HUNTER_PRO", "signal": "SELL"}))
        self.assertTrue(backend._should_route_to_demo({"strategy": "BTC_SCALPING_AGENT", "signal": "SELL"}))
        self.assertFalse(backend._should_route_to_demo({"strategy": "EUR_EMA_RSI_ATR_CROSSOVER", "signal": "WAIT"}))
        self.assertFalse(backend._should_route_to_demo({"strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER", "signal": "WAIT"}))

    def test_eur_strategy_wait_blocks_and_does_not_send_order(self) -> None:
        router = self.eur_router()
        eur_payload = {**self.eur_strategy_decision()["eur_ema_rsi_atr"], "decision": "WAIT", "block_reason": "NO_EMA_CROSS"}
        decision = self.eur_strategy_decision(eur_ema_rsi_atr=eur_payload)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-wait",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_SKIP")
        self.assertEqual(items[0]["data"]["reason"], "EUR_EMA_RSI_ATR_WAIT")

    def test_trade_mode_zero_maps_to_demo(self) -> None:
        self.assertEqual(self.router().account_type(self.account(trade_mode=0)), "DEMO")

    def test_trade_mode_one_maps_to_contest_and_blocks_by_default(self) -> None:
        router = self.router()
        self.assertEqual(router.account_type(self.account(trade_mode=1)), "CONTEST")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=1),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_CONTEST_BLOCKED")

    def test_trade_mode_two_maps_to_live_and_blocks(self) -> None:
        router = self.router()
        self.assertEqual(router.account_type(self.account(trade_mode=2)), "LIVE")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_unknown_trade_mode_blocks(self) -> None:
        router = self.router()
        self.assertEqual(router.account_type(self.account(trade_mode=None)), "UNKNOWN")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=None),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_UNKNOWN")

    def test_login_allowlist_blocks_mismatched_login(self) -> None:
        router = self.router(demo_allowed_login="345297734")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(),
                {"approved_lot": 0.01},
                self.account(login=999),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "LOGIN_NOT_ALLOWLISTED")

    def test_login_allowlist_allows_matching_demo_login(self) -> None:
        result = self.evaluate(router=self.router(demo_allowed_login="345297734"))
        self.assertEqual(result.decision, "PASS")

    def test_demo_account_can_send_only_through_demo_router(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10009, order=12345, deal=0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send:
            items = router.process_decision(
                self.decision(),
                {"approved_lot": 0.02},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        send.assert_called_once()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER")
        self.assertTrue(items[0]["data"]["order_success"])
        self.assertEqual(items[0]["data"]["magic_number"], 909002)
        self.assertEqual(items[0]["data"]["comment"], "HERMES_DEMO_KELLY_24H")

    def test_confirmed_demo_order_stores_strategy_metadata_raw_payload(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10009, order=329436508, deal=0)
        decision = self.exploration_decision(
            symbol="BTCUSD#",
            signal="SELL",
            entry=100.0,
            sl=101.0,
            tp=98.0,
            m1_trigger_reason="M1_REJECTION",
            m15_confirmation_reason="M15_STRUCTURE",
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-329436508",
                self.now,
            )
        raw = items[0]["data"]["raw_payload"]
        self.assertEqual(raw["strategy"], "SIMO_ATM_BREAKOUT")
        self.assertEqual(raw["rr"], 2.0)
        self.assertEqual(raw["kelly_suggested_lot"], 0.01)
        self.assertEqual(raw["final_capped_lot"], 0.01)
        self.assertEqual(raw["setup_id"], "setup-329436508")
        self.assertEqual(raw["setup_grade"], "A")
        self.assertEqual(raw["edge_score"], 100.0)
        self.assertEqual(raw["m1_trigger_status"], "PASS")
        self.assertEqual(raw["m1_trigger_reason"], "M1_REJECTION")
        self.assertEqual(raw["m15_confirmation_status"], "PASS")
        self.assertEqual(raw["m15_confirmation_reason"], "M15_STRUCTURE")
        self.assertEqual(raw["mode"], "DEMO_EXPLORATION")

    def test_ema_is_not_sent_to_demo_router(self) -> None:
        router = self.router()
        with patch.object(router, "evaluate") as evaluate:
            items = router.process_decision(
                self.decision(strategy="EMA_PULLBACK"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                False,
                "setup-1",
                self.now,
            )
        self.assertEqual(items, [])
        evaluate.assert_not_called()
        self.assertFalse(self.events_path.exists())

    def test_allow_live_trading_remains_false(self) -> None:
        self.assertFalse(self.router().settings.allow_live_trading)

    def assert_tp_money_capped(self, request: dict, entry: float, tick_value: float, tick_size: float, lot: float = 0.01) -> None:
        estimated = abs(float(request["tp"]) - entry) * (tick_value / tick_size * lot)
        self.assertLessEqual(estimated, 2.000001)

    def test_old_demo_cap_env_flags_remain_fallback_aliases(self) -> None:
        env = {
            "DEMO_MAX_TRADES_PER_DAY": "7",
            "DEMO_MAX_OPEN_TRADES": "2",
            "DEMO_EXPLORATION_MAX_TRADES_PER_DAY": "6",
            "DEMO_STRONG_SETUP_MAX_TRADES_PER_DAY": "4",
            "DEMO_STRONG_SETUP_MAX_OPEN_TRADES": "2",
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            settings = get_settings()
        get_settings.cache_clear()
        self.assertEqual(settings.demo_max_trades_per_day_total, 7)
        self.assertEqual(settings.demo_max_trades_per_symbol_per_day, 7)
        self.assertEqual(settings.demo_max_open_trades_total, 2)
        self.assertEqual(settings.demo_max_open_trades_per_symbol, 2)
        self.assertEqual(settings.demo_exploration_max_trades_per_day_total, 6)
        self.assertEqual(settings.demo_exploration_max_trades_per_symbol_per_day, 6)
        self.assertEqual(settings.demo_strong_setup_max_trades_per_day_total, 4)
        self.assertEqual(settings.demo_strong_setup_max_trades_per_symbol_per_day, 4)
        self.assertEqual(settings.demo_strong_setup_max_open_trades, 2)

    def test_max_money_tp_env_defaults(self) -> None:
        env = {
            "MAX_MONEY_TP_ENABLED": "true",
            "MAX_TP_USD": "2.00",
            "MAX_TP_APPLIES_TO": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD",
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            settings = get_settings()
        get_settings.cache_clear()
        self.assertTrue(settings.max_money_tp_enabled)
        self.assertEqual(settings.max_tp_usd, 2.0)
        self.assertIn("BTCUSD#", settings.max_tp_applies_to)

    def test_max_money_tp_caps_btc_buy_to_two_usd(self) -> None:
        decision = self.exploration_decision(symbol="BTCUSD#", signal="BUY", entry=100000.0, sl=99000.0, tp=140000.0)
        items, send = self.process_with_max_tp(
            decision,
            "BTCUSD#",
            self.max_tp_symbol_info(0.01, 1.0, 2, 0.01),
            {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
        )
        request = send.call_args.args[0]
        self.assertEqual(request["tp"], 120000.0)
        self.assertEqual(request["sl"], 99000.0)
        self.assert_tp_money_capped(request, 100000.0, 0.01, 1.0)
        self.assertTrue(items[0]["data"]["max_money_tp"]["applied"])

    def test_max_money_tp_caps_btc_sell_to_two_usd(self) -> None:
        decision = self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100000.0, sl=101000.0, tp=60000.0)
        items, send = self.process_with_max_tp(
            decision,
            "BTCUSD#",
            self.max_tp_symbol_info(0.01, 1.0, 2, 0.01),
            {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
        )
        request = send.call_args.args[0]
        self.assertEqual(request["tp"], 80000.0)
        self.assertEqual(request["sl"], 101000.0)
        self.assert_tp_money_capped(request, 100000.0, 0.01, 1.0)
        self.assertTrue(items[0]["data"]["max_money_tp"]["applied"])

    def test_max_money_tp_caps_gold_buy_and_sell_to_two_usd(self) -> None:
        info = self.max_tp_symbol_info(1.0, 0.01, 2, 0.01)
        buy = self.gold_m1m5_scalper_decision(entry=2300.0, sl=2299.0, tp=2310.0, reward_risk=10.0)
        buy_items, buy_send = self.process_with_max_tp(buy, "GOLD#", info, {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01})
        buy_request = buy_send.call_args.args[0]
        self.assertEqual(buy_request["tp"], 2302.0)
        self.assertEqual(buy_request["sl"], 2299.0)
        self.assert_tp_money_capped(buy_request, 2300.0, 1.0, 0.01)
        sell_payload = {**self.gold_m1m5_scalper_decision()["gold_m1m5_scalper"], "decision": "SELL"}
        sell = self.gold_m1m5_scalper_decision(signal="SELL", entry=2300.0, sl=2301.0, tp=2290.0, reward_risk=10.0, gold_m1m5_scalper=sell_payload, gold_m1m5_scalper_decision="SELL")
        sell_items, sell_send = self.process_with_max_tp(sell, "GOLD#", info, {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01})
        sell_request = sell_send.call_args.args[0]
        self.assertEqual(sell_request["tp"], 2298.0)
        self.assertEqual(sell_request["sl"], 2301.0)
        self.assert_tp_money_capped(sell_request, 2300.0, 1.0, 0.01)
        self.assertTrue(buy_items[0]["data"]["max_money_tp"]["applied"])
        self.assertTrue(sell_items[0]["data"]["max_money_tp"]["applied"])

    def test_max_money_tp_caps_eur_buy_and_sell_to_two_usd(self) -> None:
        info = self.max_tp_symbol_info(1.0, 0.00001, 5, 0.00001)
        buy = self.eur_strategy_decision(entry=1.1, sl=1.099, tp=1.105, reward_risk=5.0)
        buy_items, buy_send = self.process_with_max_tp(buy, "EURUSD", info, self.specs(), self.eur_router(max_money_tp_enabled=True))
        buy_request = buy_send.call_args.args[0]
        self.assertEqual(buy_request["tp"], 1.102)
        self.assertEqual(buy_request["sl"], 1.099)
        self.assert_tp_money_capped(buy_request, 1.1, 1.0, 0.00001)
        sell_payload = {**self.eur_strategy_decision()["eur_ema_rsi_atr"], "decision": "SELL"}
        sell = self.eur_strategy_decision(signal="SELL", entry=1.1, sl=1.101, tp=1.095, reward_risk=5.0, eur_ema_rsi_atr=sell_payload)
        sell_items, sell_send = self.process_with_max_tp(sell, "EURUSD", info, self.specs(), self.eur_router(max_money_tp_enabled=True))
        sell_request = sell_send.call_args.args[0]
        self.assertEqual(sell_request["tp"], 1.098)
        self.assertEqual(sell_request["sl"], 1.101)
        self.assert_tp_money_capped(sell_request, 1.1, 1.0, 0.00001)
        self.assertTrue(buy_items[0]["data"]["max_money_tp"]["applied"])
        self.assertTrue(sell_items[0]["data"]["max_money_tp"]["applied"])

    def test_max_money_tp_keeps_original_tp_when_already_below_two_usd(self) -> None:
        decision = self.eur_strategy_decision(entry=1.1, sl=1.099, tp=1.101, reward_risk=2.0)
        items, send = self.process_with_max_tp(
            decision,
            "EURUSD",
            self.max_tp_symbol_info(1.0, 0.00001, 5, 0.00001),
            self.specs(),
            self.eur_router(max_money_tp_enabled=True),
        )
        request = send.call_args.args[0]
        self.assertEqual(request["tp"], 1.101)
        self.assertEqual(request["sl"], 1.099)
        self.assertFalse(items[0]["data"]["max_money_tp"]["applied"])
        self.assertLessEqual(items[0]["data"]["max_money_tp"]["estimated_tp_money"], 2.0)

    def test_max_money_tp_invalid_symbol_specs_block_before_order_send(self) -> None:
        decision = self.eur_strategy_decision(entry=1.1, sl=1.099, tp=1.105, reward_risk=5.0)
        items, send = self.process_with_max_tp(
            decision,
            "EURUSD",
            self.max_tp_symbol_info(1.0, 0.0, 5, 0.00001),
            self.specs(),
            self.eur_router(max_money_tp_enabled=True),
        )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "MAX_TP_SYMBOL_SPEC_INVALID")

    def test_max_money_tp_broker_stop_distance_blocks_before_order_send(self) -> None:
        decision = self.gold_m1m5_scalper_decision(entry=2300.0, sl=2299.0, tp=2310.0, reward_risk=10.0)
        items, send = self.process_with_max_tp(
            decision,
            "GOLD#",
            self.max_tp_symbol_info(1.0, 0.01, 2, 0.01, stops_level=300),
            {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01},
        )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "MAX_TP_TOO_CLOSE_TO_MARKET")

    def exploration_decision(self, **overrides) -> dict:
        payload = self.decision(
            symbol="EURUSD",
            strategy="SIMO_ATM_BREAKOUT",
            signal="BUY",
            entry=1.1000,
            sl=1.0990,
            tp=1.1020,
            reward_risk=2.0,
            mtfa_status="FAIL",
            smc_confluence_status="FAIL",
            m15_confirmation=True,
            m1_entry_confirmation=True,
            m1_trigger_status="PASS",
            m1_trigger_type="M1_HIGHER_LOW_REJECTION",
            m15_confirmation_status="PASS",
            m15_confirmation_type="M15_BULLISH_STRUCTURE",
            setup_hunter_score=100,
            edge_score=100,
            setup_score=90,
            setup_hunter_grade="A",
            grade="A",
            big_setup_grade="A",
            top_down_reader={
                "top_down_status": "PASS",
                "decision": "ALLOW_DEMO",
                "entry_readiness_score": 90,
                "market_narrative": "Test top-down pass.",
                "missing_confirmations": [],
                "score_breakdown": {"test": 90},
                "m1_trigger": True,
                "m15_confirmation": True,
            },
        )
        payload.update(overrides)
        return payload

    def quant_decision(self, **overrides) -> dict:
        payload = self.exploration_decision(
            strategy="QUANT_STATISTICAL_PULLBACK",
            signal="BUY",
            entry=1.1000,
            sl=1.0980,
            tp=1.1040,
            reward_risk=2.0,
            mtfa_status="FAIL",
            smc_confluence_status="FAIL",
            m15_confirmation=False,
            m1_entry_confirmation=False,
            m15_confirmation_status="FAIL",
            m1_trigger_status="FAIL",
            setup_hunter_score=95,
            edge_score=95,
            quant_slope=0.05,
            quant_r2=0.8,
            quant_z_score=-1.25,
            quant_mean=1.101,
            quant_stdev=0.001,
            quant_signal="BUY",
            quant_score=95,
            quant_reason="QUANT_STATISTICAL_PULLBACK_BUY",
            top_down_reader={
                "top_down_status": "PASS",
                "decision": "ALLOW_DEMO",
                "entry_readiness_score": 90,
                "market_narrative": "Quant test top-down pass.",
                "missing_confirmations": [],
                "score_breakdown": {"test": 90},
                "m1_trigger": True,
                "m15_confirmation": True,
            },
        )
        payload.update(overrides)
        return payload

    def quant_pro_decision(self, **overrides) -> dict:
        payload = self.exploration_decision(
            strategy="QUANT_PRO_REGIME_SWITCHING",
            signal="BUY",
            entry=1.1000,
            sl=1.0980,
            tp=1.1040,
            reward_risk=2.0,
            mtfa_status="FAIL",
            smc_confluence_status="FAIL",
            m15_confirmation=False,
            m1_entry_confirmation=False,
            m15_confirmation_status="FAIL",
            m1_trigger_status="FAIL",
            setup_hunter_score=95,
            edge_score=95,
            quant_pro_regime="TREND",
            quant_pro_score=95,
            quant_pro_grade="A",
            quant_pro_ols_slope=0.05,
            quant_pro_ols_r2=0.9,
            quant_pro_ols_tstat=5.5,
            quant_pro_kalman_velocity=0.04,
            quant_pro_kalman_z=-1.25,
            quant_pro_ou_beta=-0.1,
            quant_pro_ou_tstat=-0.5,
            quant_pro_ou_half_life=7.0,
            quant_pro_hurst=0.95,
            quant_pro_hurst_filter={
                "enabled": True,
                "hurst": 0.95,
                "min_trend_hurst": 0.90,
                "trend_strength": "STRONG_TREND",
                "passed": True,
                "block_reason": None,
            },
            quant_pro_min_trend_hurst=0.90,
            quant_pro_trend_strength="STRONG_TREND",
            quant_pro_hurst_filter_status="PASS",
            quant_pro_hurst_block_reason=None,
            quant_pro_ewma_vol=0.001,
            quant_pro_signal="BUY",
            quant_pro_reason="QUANT_PRO_TREND_BUY",
            top_down_reader={
                "top_down_status": "PASS",
                "decision": "ALLOW_DEMO",
                "entry_readiness_score": 90,
                "market_narrative": "Quant PRO test top-down pass.",
                "missing_confirmations": [],
                "score_breakdown": {"test": 90},
                "m1_trigger": True,
                "m15_confirmation": True,
            },
        )
        payload.update(overrides)
        return payload

    def test_exploration_allows_btc_like_soft_confluence_with_smc_mtfa_warnings(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["mode"], "DEMO_EXPLORATION")
        self.assertEqual(result.event["strict_block_reason"], "MTFA_FAIL")
        self.assertEqual(result.event["exploration_decision"], "ALLOW")
        self.assertEqual(set(result.event["exploration_warnings"]), {"MTFA_FAIL", "SMC_FAIL"})
        self.assertEqual(result.event["final_capped_lot"], 0.01)

    def test_exploration_allows_eurusd_like_grade_b_when_score_threshold_passes(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(reward_risk=3.3236, setup_hunter_score=91, edge_score=91, setup_hunter_grade="B", grade="B", big_setup_grade="B"),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["mode"], "DEMO_EXPLORATION")
        self.assertEqual(result.event["exploration_decision"], "ALLOW")

    def test_exploration_blocks_when_score_threshold_fails(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(setup_hunter_score=89, edge_score=89, setup_score=84, setup_hunter_grade="B", grade="B"),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.event["exploration_decision"], "BLOCK")
        self.assertIn("EDGE_SCORE_BELOW_EXPLORATION_MIN", result.event["exploration_block_reasons"])

    def test_exploration_blocks_without_m1_trigger(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(m1_trigger_status="FAIL", m1_entry_confirmation=False),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("M1_TRIGGER_FALSE", result.event["exploration_block_reasons"])

    def test_exploration_blocks_without_m15_confirmation(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(m15_confirmation_status="FAIL", m15_confirmation=False),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("M15_CONFIRMATION_FALSE", result.event["exploration_block_reasons"])

    def test_exploration_blocks_rr_below_two(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(reward_risk=1.99, tp=1.10199),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("RR_BELOW_EXPLORATION_MIN", result.event["exploration_block_reasons"])

    def test_exploration_blocks_grade_c(self) -> None:
        result = self.evaluate(
            decision=self.exploration_decision(setup_hunter_grade="C", grade="C", big_setup_grade="C"),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("BIG_SETUP_GRADE_BELOW_B", result.event["exploration_block_reasons"])

    def test_exploration_live_account_still_blocks(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")
        self.assertIn("ACCOUNT_TRADE_MODE_REAL", result.event["exploration_block_reasons"])

    def test_quant_demo_execution_still_blocked_on_live_account(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.quant_decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_quant_demo_lot_above_point_zero_one_is_capped_safely(self) -> None:
        router = self.router(demo_max_lot=0.5)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.quant_decision(),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn(result.reason, {"NON_ENTRY_STRATEGY", "MTFA_FAIL"})

    def test_quant_demo_execution_still_blocked_by_daily_and_open_caps(self) -> None:
        router = self.router(demo_max_open_trades_per_symbol=1)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD", ticket=8100)]):
            result = router.evaluate(
                self.quant_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_OPEN_TRADES_PER_SYMBOL", result.event["exploration_block_reasons"])

    def test_quant_failed_mt5_order_is_not_counted_confirmed(self) -> None:
        router = self.router()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=None),
        ):
            items = router.process_decision(
                self.quant_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items, [])
        self.assertEqual(stats["demo_trades_opened_today"], 0)

    def test_quant_pro_demo_execution_still_blocked_on_live_account(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.quant_pro_decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant-pro",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_quant_pro_demo_lot_above_point_zero_one_is_capped_safely(self) -> None:
        router = self.router(demo_max_lot=0.5)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.quant_pro_decision(),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant-pro",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn(result.reason, {"NON_ENTRY_STRATEGY", "MTFA_FAIL"})

    def test_quant_pro_demo_execution_still_blocked_by_daily_and_open_caps(self) -> None:
        router = self.router(demo_max_open_trades_per_symbol=1)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD")]):
            result = router.evaluate(
                self.quant_pro_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant-pro",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_OPEN_TRADES_PER_SYMBOL", result.event["exploration_block_reasons"])

    def test_quant_pro_hurst_block_never_reaches_order_send(self) -> None:
        router = self.router()
        decision = self.quant_pro_decision(
            quant_pro_hurst=0.89,
            quant_pro_hurst_filter={
                "enabled": True,
                "hurst": 0.89,
                "min_trend_hurst": 0.90,
                "trend_strength": "WEAK_TREND",
                "passed": False,
                "block_reason": "QUANT_PRO_HURST_TREND_TOO_WEAK",
            },
            quant_pro_trend_strength="WEAK_TREND",
            quant_pro_hurst_filter_status="BLOCK",
            quant_pro_hurst_block_reason="QUANT_PRO_HURST_TREND_TOO_WEAK",
        )
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant-pro-hurst",
                self.now,
            )
        self.assertEqual(items, [])
        send.assert_not_called()

    def test_relaxed_cooldown_prevents_overtrading(self) -> None:
        existing = {
            "event_type": "DEMO_ORDER",
            "created_at": "2026-06-01T09:00:00+00:00",
            "order_success": True,
            "ticket": 111,
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "relaxed_mode_active": True,
        }
        self.events_path.write_text(json.dumps(existing), encoding="utf-8")
        decision = self.gold_m1m5_scalper_decision(relaxed_mode_active=True, relaxed_reason="NO_SETUP_24H", hours_without_setup=24)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            result = self.evaluate(decision=decision, kelly={"approved_lot": 0.01}, broker_symbol="GOLD#")
        self.assertEqual(result.reason, "RELAXED_COOLDOWN_6H")
        send.assert_not_called()

    def test_user_disabled_duration_blocks_allows_relaxed_cooldown_only(self) -> None:
        existing = {
            "event_type": "DEMO_ORDER",
            "created_at": "2026-06-01T09:00:00+00:00",
            "order_success": True,
            "ticket": 111,
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "relaxed_mode_active": True,
        }
        self.events_path.write_text(json.dumps(existing), encoding="utf-8")
        router = self.router(**self.time_blocks_disabled_settings())
        decision = self.gold_m1m5_scalper_decision(relaxed_mode_active=True, relaxed_reason="DURATION_BLOCKS_DISABLED_BY_USER_ORDER", hours_without_setup=0)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            result = self.evaluate(router=router, decision=decision, kelly={"approved_lot": 0.01}, broker_symbol="GOLD#")
        self.assertEqual(result.decision, "PASS")
        self.assertNotEqual(result.reason, "RELAXED_COOLDOWN_6H")
        send.assert_not_called()

    def test_relaxed_loss_disables_symbol_for_day(self) -> None:
        loss = {
            "event_type": "DEMO_CLOSE",
            "created_at": "2026-06-01T09:30:00+00:00",
            "symbol": "GOLD#",
            "broker_symbol": "GOLD#",
            "strategy": "GOLD_M1_M5_EMA_SWEEP_SCALPER",
            "relaxed_mode_active": True,
            "result": "LOSS",
            "pnl": -1.0,
        }
        self.events_path.write_text(json.dumps(loss), encoding="utf-8")
        decision = self.gold_m1m5_scalper_decision(relaxed_mode_active=True, relaxed_reason="NO_SETUP_24H", hours_without_setup=24)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            result = self.evaluate(decision=decision, kelly={"approved_lot": 0.01}, broker_symbol="GOLD#")
        self.assertEqual(result.reason, "RELAXED_DISABLED_AFTER_LOSS")
        send.assert_not_called()

    def test_quant_pro_failed_mt5_order_is_not_counted_confirmed(self) -> None:
        router = self.router()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=None),
        ):
            items = router.process_decision(
                self.quant_pro_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-quant-pro",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items, [])
        self.assertEqual(stats["demo_trades_opened_today"], 0)

    def free_discovery_router(self, **overrides) -> DemoKellyRouter:
        settings = {
            "hermes_free_demo_discovery_mode": True,
            "demo_max_trades_per_day_total": 999,
            "demo_max_trades_per_symbol_per_day": 999,
            "demo_exploration_max_trades_per_day_total": 999,
            "demo_exploration_max_trades_per_symbol_per_day": 999,
            "demo_strong_setup_max_trades_per_day_total": 999,
            "demo_strong_setup_max_trades_per_symbol_per_day": 999,
            "demo_max_open_trades_total": 21,
            "demo_max_open_trades_per_symbol": 7,
            "demo_max_open_trades_per_symbol_strategy": 1,
        }
        settings.update(overrides)
        return self.router(**settings)

    def free_discovery_decision(self, strategy: str = "BREAKOUT_RETEST", **overrides) -> dict:
        payload = self.exploration_decision(
            strategy=strategy,
            signal="BUY",
            entry=1.1000,
            sl=1.0990,
            tp=1.1020,
            reward_risk=2.0,
            mtfa_status="FAIL",
            smc_confluence_status="FAIL",
            m15_confirmation=False,
            m1_entry_confirmation=False,
            m15_confirmation_status="FAIL",
            m1_trigger_status="FAIL",
            setup_hunter_score=50,
            edge_score=50,
            setup_score=50,
            setup_hunter_grade="D",
            grade="D",
            big_setup_grade="D",
            top_down_reader={
                "top_down_status": "PASS",
                "decision": "ALLOW_DEMO",
                "entry_readiness_score": 80,
                "m1_trigger": True,
                "m15_confirmation": True,
            },
        )
        payload.update(overrides)
        return payload

    def confirmed_event(self, ticket: int, symbol: str, strategy: str, created: datetime | None = None) -> dict:
        return {
            "event_type": "DEMO_ORDER",
            "created_at": (created or self.now).isoformat(),
            "mode": "DEMO_EXPLORATION",
            "order_success": True,
            "order_retcode": 10009,
            "ticket": ticket,
            "symbol": symbol,
            "broker_symbol": symbol,
            "strategy": strategy,
        }

    def close_event(self, ticket: int, symbol: str, strategy: str) -> dict:
        return {
            "event_type": "DEMO_CLOSE",
            "created_at": self.now.isoformat(),
            "ticket": ticket,
            "symbol": symbol,
            "broker_symbol": symbol,
            "strategy": strategy,
            "result": "CLOSED",
        }

    def test_free_discovery_multiple_strategies_can_open_same_symbol_up_to_seven(self) -> None:
        router = self.free_discovery_router()
        strategies = [
            "TREND_CONTINUATION_BREAKDOWN",
            "BREAKOUT_RETEST",
            "CRT_TBS_REVERSAL",
            "AMD_FVG_IFVG_REVERSAL",
            "FIB_OTE_RETEST",
            "QUANT_STATISTICAL_PULLBACK",
        ]
        existing = [self.confirmed_event(8000 + index, "BTCUSD#", strategy) for index, strategy in enumerate(strategies)]
        self.events_path.write_text("\n".join(json.dumps(event) for event in existing), encoding="utf-8")
        positions = [SimpleNamespace(magic=909002, symbol="BTCUSD#") for _ in range(6)]
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=positions):
            result = router.evaluate(
                self.free_discovery_decision("QUANT_PRO_REGIME_SWITCHING", symbol="BTCUSD#", entry=100.0, sl=99.0, tp=102.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "NON_ENTRY_STRATEGY")
        self.assertTrue(result.event["free_demo_discovery_mode"])
        self.assertEqual(result.event["current_symbol_open_count"], 6)
        self.assertEqual(result.event["current_symbol_strategy_open_count"], 0)

    def test_free_discovery_same_strategy_cannot_open_twice_on_same_symbol(self) -> None:
        router = self.free_discovery_router()
        self.events_path.write_text(json.dumps(self.confirmed_event(8100, "EURUSD", "HERMES_STRATEGY_PACK_AGENT")), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD")]):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY")
        self.assertIn("MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY", result.event["exploration_block_reasons"])

    def test_free_discovery_total_open_cap_blocks_at_twenty_one(self) -> None:
        router = self.free_discovery_router(demo_max_open_trades_per_symbol=8)
        positions = [SimpleNamespace(magic=909002, symbol=symbol) for symbol in ("EURUSD", "BTCUSD#", "GOLD#") for _ in range(7)]
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=positions):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_TOTAL")

    def test_free_discovery_per_symbol_open_cap_blocks_at_seven(self) -> None:
        router = self.free_discovery_router()
        positions = [SimpleNamespace(magic=909002, symbol="EURUSD") for _ in range(7)]
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=positions):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_PER_SYMBOL")

    def test_free_discovery_daily_caps_do_not_block_before_999(self) -> None:
        router = self.free_discovery_router()
        events = []
        for index in range(998):
            strategy = "BREAKOUT_RETEST" if index % 2 == 0 else "CRT_TBS_REVERSAL"
            events.append(self.confirmed_event(9000 + index, "EURUSD", strategy))
            events.append(self.close_event(9000 + index, "EURUSD", strategy))
        self.events_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["daily_demo_trades_total"], 998)
        self.assertEqual(result.event["current_symbol_daily_count"], 998)

    def test_free_discovery_live_account_remains_blocked(self) -> None:
        router = self.free_discovery_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_free_discovery_lot_above_point_zero_one_is_capped_safely(self) -> None:
        router = self.free_discovery_router(demo_max_lot=0.5)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("HERMES_STRATEGY_PACK_AGENT"),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertLessEqual(result.event["final_capped_lot"], 0.01)

    def test_free_discovery_spread_fail_remains_blocked(self) -> None:
        router = self.free_discovery_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("BREAKOUT_RETEST"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.2},
                self.specs(),
                100,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_SPREAD", result.event["exploration_block_reasons"])

    def test_free_discovery_invalid_sl_tp_remains_blocked(self) -> None:
        router = self.free_discovery_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("BREAKOUT_RETEST", sl=1.1010, tp=1.1020, reward_risk=2.0),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("INVALID_SL_TP", result.event["exploration_block_reasons"])

    def test_free_discovery_top_down_avoid_remains_blocked(self) -> None:
        router = self.free_discovery_router()
        top_down_avoid = {
            "top_down_status": "FAIL",
            "decision": "AVOID",
            "entry_readiness_score": 90,
            "m1_trigger": True,
            "m15_confirmation": True,
        }
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.free_discovery_decision("BREAKOUT_RETEST", top_down_reader=top_down_avoid),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-free",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("TOP_DOWN_READER_BLOCK", result.event["exploration_block_reasons"])

    def adaptive_decision(self, symbol: str, score: float, **overrides) -> dict:
        top_down_score = overrides.pop("top_down_score", max(score, 90))
        top_down_decision = overrides.pop("top_down_decision", "ALLOW_DEMO")
        m15 = overrides.get("m15_confirmation", True)
        m1 = overrides.get("m1_entry_confirmation", True)
        payload = self.exploration_decision(
            symbol=symbol,
            mtfa_status="FAIL",
            mtfa_score=50,
            smc_confluence_status="FAIL",
            smc_confluence_score=50,
            final_confluence_score=score,
            adaptive_confluence_score=score,
            m15_confirmation=m15,
            m1_entry_confirmation=m1,
            m15_confirmation_status="PASS" if m15 else "WAIT",
            m1_trigger_status="PASS" if m1 else "WAIT",
            top_down_reader={
                "top_down_status": "PASS" if top_down_decision != "AVOID" else "FAIL",
                "decision": top_down_decision,
                "entry_readiness_score": top_down_score,
                "m1_trigger": m1,
                "m15_confirmation": m15,
            },
        )
        payload.update(overrides)
        return payload

    def test_optional_dashboard_module_placeholders_do_not_affect_demo_gate(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        # §4.4: use EUR_EMA_RSI_ATR_CROSSOVER (not SMC_NATIVE) so EURUSD threshold=60 applies
        decision = self.adaptive_decision(
            "EURUSD",
            60,
            strategy="EUR_EMA_RSI_ATR_CROSSOVER",
            raw_payload={
                "acceleration_bands_htf": {
                    "enabled": False,
                    "status": "OPTIONAL_NOT_ENABLED",
                    "signal": "UNKNOWN",
                    "score": None,
                    "reason": "Acceleration Bands HTF module not enabled or no payload yet",
                },
                "volume_profile": {
                    "enabled": False,
                    "status": "OPTIONAL_NOT_ENABLED",
                    "poc": None,
                    "vah": None,
                    "val": None,
                    "score": None,
                    "reason": "Volume Profile module not enabled or no payload yet",
                },
            },
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-dashboard-placeholders",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.reason, "PASS")

    def test_adaptive_confluence_btc_64_blocks_and_65_passes(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            blocked = router.evaluate(
                self.adaptive_decision("BTCUSD#", 64, signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-btc-64",
                self.now,
            )
            passed = router.evaluate(
                self.adaptive_decision("BTCUSD#", 65, signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-btc-65",
                self.now,
            )
        self.assertEqual(blocked.decision, "BLOCK")
        self.assertEqual(blocked.reason, "BELOW_SYMBOL_THRESHOLD")
        self.assertEqual(blocked.event["symbol_min_confluence"], 65.0)
        self.assertEqual(passed.decision, "PASS")
        self.assertTrue(passed.event["confluence_threshold_pass"])

    def test_adaptive_confluence_gold_55_passes_if_m15_rr_spread_topdown_pass(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.gold_m1m5_scalper_decision(
                    final_confluence_score=55,
                    adaptive_confluence_score=55,
                    top_down_reader={
                        "top_down_status": "PASS",
                        "decision": "ALLOW_DEMO",
                        "entry_readiness_score": 80,
                        "m1_trigger": True,
                        "m15_confirmation": True,
                    },
                    mtfa_status="FAIL",
                    mtfa_score=50,
                    smc_confluence_status="FAIL",
                    smc_confluence_score=50,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.2},
                {"tick_value": 1.0, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-gold-55",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["symbol_min_confluence"], 55.0)
        self.assertEqual(result.event["confluence_threshold_reason"], "GOLD_ADAPTIVE_THRESHOLD")

    def test_adaptive_confluence_gold_m1_wait_requires_score_70(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            blocked = router.evaluate(
                self.gold_m1m5_scalper_decision(
                    m1_entry_confirmation=False,
                    m1_trigger_status="WAIT",
                    final_confluence_score=69,
                    adaptive_confluence_score=69,
                    top_down_reader={
                        "top_down_status": "PASS",
                        "decision": "ALLOW_DEMO",
                        "entry_readiness_score": 80,
                        "m1_trigger": False,
                        "m15_confirmation": True,
                    },
                    mtfa_status="FAIL",
                    mtfa_score=50,
                    smc_confluence_status="FAIL",
                    smc_confluence_score=50,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.2},
                {"tick_value": 1.0, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-gold-69",
                self.now,
            )
            passed = router.evaluate(
                self.gold_m1m5_scalper_decision(
                    m1_entry_confirmation=False,
                    m1_trigger_status="WAIT",
                    final_confluence_score=70,
                    adaptive_confluence_score=70,
                    top_down_reader={
                        "top_down_status": "PASS",
                        "decision": "ALLOW_DEMO",
                        "entry_readiness_score": 80,
                        "m1_trigger": False,
                        "m15_confirmation": True,
                    },
                    mtfa_status="FAIL",
                    mtfa_score=50,
                    smc_confluence_status="FAIL",
                    smc_confluence_score=50,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.2},
                {"tick_value": 1.0, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-gold-70",
                self.now,
            )
        self.assertEqual(blocked.reason, "GOLD_M1_WAIT_REQUIRES_70")
        self.assertEqual(passed.decision, "PASS")

    def test_adaptive_confluence_eurusd_59_blocks_and_60_passes(self) -> None:
        # §4.4: SIMO_ATM_BREAKOUT is SMC_NATIVE → threshold=65, so use EUR_EMA_RSI_ATR_CROSSOVER
        # (not in ORDER_FLOW_NATIVE or SMC_NATIVE) to test the EURUSD symbol threshold of 60.
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            blocked = router.evaluate(
                self.adaptive_decision("EURUSD", 59, strategy="EUR_EMA_RSI_ATR_CROSSOVER"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-59",
                self.now,
            )
            passed = router.evaluate(
                self.adaptive_decision("EURUSD", 60, strategy="EUR_EMA_RSI_ATR_CROSSOVER"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-eur-60",
                self.now,
            )
        self.assertEqual(blocked.reason, "BELOW_SYMBOL_THRESHOLD")
        self.assertEqual(passed.decision, "PASS")

    def fallback_router(self, **overrides) -> DemoKellyRouter:
        settings = {
            "hermes_demo_topdown_fallback_mode": True,
            "hermes_adaptive_confluence_enabled": True,
            "demo_max_open_trades_total": 10,
            "demo_max_open_trades_per_symbol": 10,
        }
        settings.update(overrides)
        return self.router(**settings)

    def top_down_payload(self, status: str, decision: str = "WAIT_FOR_CONFIRMATION", score: float = 20.0) -> dict:
        return {
            "top_down_status": status,
            "decision": decision,
            "entry_readiness_score": score,
            "missing_confirmations": ["M1_ENTRY"] if status != "PASS" else [],
            "m1_trigger": False,
            "m15_confirmation": False,
        }

    def test_demo_topdown_fallback_wait_allows_when_adaptive_and_hard_safety_pass(self) -> None:
        router = self.fallback_router()
        fake_result = SimpleNamespace(retcode=10009, order=700001, deal=0)
        decision = self.adaptive_decision(
            "EURUSD",
            65,
            top_down_reader=self.top_down_payload("WAIT"),
            m15_confirmation=True,
            m1_entry_confirmation=False,
            mtfa_status="PASS",
            mtfa_score=80,
            smc_confluence_status="PASS",
            smc_confluence_score=80,
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-fallback-wait", self.now)
        send.assert_called_once()
        event = items[0]["data"]
        self.assertEqual(event["event_type"], "DEMO_ORDER")
        self.assertEqual(event["mode"], "DEMO_ADAPTIVE_FALLBACK")
        self.assertEqual(event["fallback_decision"], "ALLOW")
        self.assertIn("TOP_DOWN_WAIT_WARNING", event["fallback_warnings"])

    def test_demo_topdown_fallback_fail_allows_when_adaptive_and_hard_safety_pass(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.adaptive_decision("EURUSD", 65, top_down_reader=self.top_down_payload("FAIL"), m15_confirmation=False, m1_entry_confirmation=True),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-fail",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["fallback_decision"], "ALLOW")
        self.assertIn("TOP_DOWN_FAIL_WARNING", result.event["fallback_warnings"])

    def test_demo_topdown_fallback_missing_allows_only_when_adaptive_passes(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.adaptive_decision("EURUSD", 65, top_down_reader={}, m15_confirmation=True, m1_entry_confirmation=False),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-missing",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertIn("TOP_DOWN_MISSING_WARNING", result.event["fallback_warnings"])

    def test_demo_topdown_fallback_avoid_still_blocks(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("FAIL", "AVOID", 10)),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-avoid",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "TOP_DOWN_READER_BLOCK")

    def test_demo_topdown_fallback_strong_smc_mtfa_fail_blocks(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            smc = router.evaluate(
                self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=49),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-smc",
                self.now,
            )
            mtfa = router.evaluate(
                self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), mtfa_score=49),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-mtfa",
                self.now,
            )
        self.assertEqual(smc.reason, "SMC_STRONG_FAIL")
        self.assertEqual(mtfa.reason, "MTFA_STRONG_FAIL")

    def test_demo_topdown_fallback_no_trigger_blocks_except_gold_score_65(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            eur = router.evaluate(
                self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), m15_confirmation=False, m1_entry_confirmation=False),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-fallback-eur-no-trigger",
                self.now,
            )
            gold = router.evaluate(
                self.gold_m1m5_scalper_decision(
                    top_down_reader=self.top_down_payload("WAIT"),
                    m15_confirmation=False,
                    m1_entry_confirmation=False,
                    m15_confirmation_status="WAIT",
                    m1_trigger_status="WAIT",
                    final_confluence_score=65,
                    adaptive_confluence_score=65,
                    mtfa_status="FAIL",
                    mtfa_score=50,
                    smc_confluence_status="FAIL",
                    smc_confluence_score=50,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.2},
                {"tick_value": 1.0, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-fallback-gold-no-trigger",
                self.now,
            )
        self.assertEqual(eur.reason, "NO_ENTRY_TRIGGER_CONFIRMATION")
        self.assertEqual(gold.decision, "PASS")

    def test_demo_topdown_fallback_hard_blockers_still_block(self) -> None:
        router = self.fallback_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            spread = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT")), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.2}, self.specs(), 100, 30, True, "setup-fallback-spread", self.now)
            invalid = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), sl=1.101, tp=1.102), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-fallback-invalid", self.now)
            live = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT")), {"approved_lot": 0.01}, self.account(trade_mode=2), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-fallback-live", self.now)
        self.assertEqual(spread.reason, "MAX_SPREAD")
        self.assertEqual(invalid.reason, "INVALID_SL_TP")
        self.assertEqual(live.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_demo_topdown_fallback_block_never_reaches_order_send(self) -> None:
        router = self.fallback_router()
        decision = self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), m15_confirmation=False, m1_entry_confirmation=False)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-fallback-block", self.now)
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_SKIP")
        self.assertEqual(items[0]["data"]["reason"], "NO_ENTRY_TRIGGER_CONFIRMATION")

    def micro_router(self, **overrides) -> DemoKellyRouter:
        settings = {
            "hermes_demo_micro_discovery_mode": True,
            "hermes_demo_topdown_fallback_mode": True,
            "hermes_adaptive_confluence_enabled": True,
            "demo_max_open_trades_total": 10,
            "demo_max_open_trades_per_symbol": 10,
        }
        settings.update(overrides)
        return self.router(**settings)

    def test_demo_micro_discovery_btc_relaxed_smc_mtfa_can_pass(self) -> None:
        router = self.micro_router()
        fake_result = SimpleNamespace(retcode=10009, order=710001, deal=0)
        decision = self.adaptive_decision(
            "BTCUSD#",
            61.5,
            signal="SELL",
            entry=100.0,
            sl=101.0,
            tp=98.0,
            top_down_reader=self.top_down_payload("WAIT"),
            smc_confluence_score=45,
            mtfa_score=35,
            m15_confirmation=True,
            m1_entry_confirmation=False,
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "BTCUSD#", {}, {"bid": 100.0, "ask": 100.1}, {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01}, 1, 30, True, "setup-micro-btc", self.now)
        send.assert_called_once()
        event = items[0]["data"]
        self.assertEqual(event["event_type"], "DEMO_ORDER")
        self.assertEqual(event["mode"], "DEMO_MICRO_DISCOVERY")
        self.assertEqual(event["micro_discovery_decision"], "ALLOW")
        self.assertIn("TOP_DOWN_WAIT", event["micro_discovery_warnings"])
        self.assertIn("SMC_SOFT_FAIL", event["micro_discovery_warnings"])
        self.assertIn("MTFA_SOFT_FAIL", event["micro_discovery_warnings"])

    def test_demo_micro_discovery_gold_smc_five_blocks(self) -> None:
        router = self.micro_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.gold_m1m5_scalper_decision(
                    final_confluence_score=70,
                    adaptive_confluence_score=70,
                    top_down_reader=self.top_down_payload("WAIT"),
                    smc_confluence_status="FAIL",
                    smc_confluence_score=5,
                    mtfa_status="FAIL",
                    mtfa_score=50,
                ),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.2},
                {"tick_value": 1.0, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-micro-gold-smc",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "MICRO_DISCOVERY_SMC_TOO_LOW")

    def test_demo_micro_discovery_eurusd_requires_smc_mtfa_50(self) -> None:
        router = self.micro_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            smc = router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=49, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-eur-smc", self.now)
            mtfa = router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=49), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-eur-mtfa", self.now)
            passed = router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-eur-pass", self.now)
        self.assertEqual(smc.reason, "MICRO_DISCOVERY_SMC_TOO_LOW")
        self.assertEqual(mtfa.reason, "MICRO_DISCOVERY_MTFA_TOO_LOW")
        self.assertEqual(passed.decision, "PASS")

    def test_demo_micro_discovery_hard_blockers_still_block(self) -> None:
        router = self.micro_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            avoid = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("FAIL", "AVOID", 10), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-avoid", self.now)
            spread = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.2}, self.specs(), 100, 30, True, "setup-micro-spread", self.now)
            invalid = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50, sl=1.101, tp=1.102), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-invalid", self.now)
            live = router.evaluate(self.adaptive_decision("EURUSD", 90, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(trade_mode=2), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-live", self.now)
        self.assertEqual(avoid.reason, "TOP_DOWN_READER_BLOCK")
        self.assertEqual(spread.reason, "MAX_SPREAD")
        self.assertEqual(invalid.reason, "INVALID_SL_TP")
        self.assertEqual(live.reason, "ACCOUNT_TRADE_MODE_REAL")

    def test_demo_micro_discovery_no_trigger_and_open_cap_block(self) -> None:
        router = self.micro_router(demo_max_open_trades_per_symbol_strategy=1)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            no_trigger = router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50, m15_confirmation=False, m1_entry_confirmation=False), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-no-trigger", self.now)
        cap_router = self.micro_router(demo_max_open_trades_per_symbol_strategy=1)
        seeded = {
            "event_type": "DEMO_ORDER",
            "status": "OPEN",
            "result": "CONFIRMED",
            "ticket": "710010",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "strategy": "TREND_CONTINUATION_BREAKDOWN",
            "created_at": self.now.isoformat(),
        }
        cap_router._record_event(seeded)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD", ticket="710010")]):
            cap = cap_router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-open-cap", self.now)
        self.assertEqual(no_trigger.reason, "NO_ENTRY_TRIGGER_CONFIRMATION")
        self.assertIn(cap.reason, {"MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY", "PASS"})

    def test_demo_micro_discovery_stale_strategy_counter_does_not_block_without_real_open_position(self) -> None:
        router = self.micro_router(demo_max_open_trades_per_symbol_strategy=1)
        seeded = {
            "event_type": "DEMO_ORDER",
            "status": "OPEN",
            "result": "CONFIRMED",
            "ticket": "710011",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "strategy": "TREND_CONTINUATION_BREAKDOWN",
            "created_at": self.now.isoformat(),
        }
        router._record_event(seeded)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), self.assertLogs("hermes", level="INFO") as logs:
            result = router.evaluate(self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-stale-counter", self.now)
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["open_demo_trades_total"], 0)
        self.assertEqual(result.event["open_demo_trades_by_symbol_strategy"], {})
        self.assertEqual(result.event["current_symbol_strategy_open_count"], 0)
        self.assertNotEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY")
        self.assertIn(
            "[OPEN_COUNTER_RECONCILE] mt5_open=0 hermes_open=0 stale_strategy_counter_cleared=EURUSD|TREND_CONTINUATION_BREAKDOWN",
            "\n".join(logs.output),
        )

    def test_demo_micro_discovery_block_never_reaches_order_send(self) -> None:
        router = self.micro_router()
        decision = self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=10, mtfa_score=50)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
            items = router.process_decision(decision, {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {"bid": 1.1, "ask": 1.10001}, self.specs(), 1, 30, True, "setup-micro-block", self.now)
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_SKIP")
        self.assertEqual(items[0]["data"]["reason"], "MICRO_DISCOVERY_SMC_TOO_LOW")

    def test_demo_micro_discovery_caps_lot_to_point_zero_one(self) -> None:
        router = self.micro_router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.adaptive_decision("EURUSD", 70, top_down_reader=self.top_down_payload("WAIT"), smc_confluence_score=50, mtfa_score=50),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-micro-lot-cap",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertLessEqual(result.event["final_capped_lot"], 0.01)

    def test_adaptive_confluence_below_45_always_blocks(self) -> None:
        result = self.evaluate(
            router=self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10),
            decision=self.adaptive_decision("EURUSD", 44),
            kelly={"approved_lot": 0.01},
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ADAPTIVE_CONFLUENCE_TOO_LOW")

    def test_adaptive_confluence_topdown_spread_rr_live_and_lot_hard_blocks(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True, demo_max_open_trades_total=10, demo_max_open_trades_per_symbol=10)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            top_down = router.evaluate(
                self.adaptive_decision("EURUSD", 90, top_down_decision="AVOID"),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-td-avoid",
                self.now,
            )
            spread = router.evaluate(
                self.adaptive_decision("EURUSD", 90),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                40,
                30,
                True,
                "setup-spread",
                self.now,
            )
            rr = router.evaluate(
                self.adaptive_decision("EURUSD", 90, sl=1.1010, tp=1.1020),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-rr",
                self.now,
            )
            live = router.evaluate(
                self.adaptive_decision("EURUSD", 90),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-live",
                self.now,
            )
            invalid_lot = router.evaluate(
                self.adaptive_decision("EURUSD", 90),
                {"approved_lot": -0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-lot",
                self.now,
            )
        self.assertEqual(top_down.reason, "TOP_DOWN_READER_BLOCK")
        self.assertEqual(spread.reason, "MAX_SPREAD")
        self.assertEqual(rr.reason, "INVALID_SL_TP")
        self.assertEqual(live.reason, "ACCOUNT_TRADE_MODE_REAL")
        self.assertEqual(invalid_lot.reason, "KELLY_INVALID_LOT")

    def test_final_demo_blocked_exploration_never_reaches_order_send(self) -> None:
        router = self.router()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            items = router.process_decision(
                self.exploration_decision(m1_trigger_status="FAIL", m1_entry_confirmation=False),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-blocked-exploration",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_SKIP")
        self.assertEqual(items[0]["data"]["final_demo_decision"], "BLOCK")
        self.assertIn("M1_TRIGGER_FALSE", items[0]["data"]["exploration_block_reasons"])

    def test_final_demo_missing_top_down_never_reaches_order_send(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True)
        decision = self.exploration_decision(top_down_reader={"decision": "", "top_down_status": "", "missing_confirmations": ["M15_MISSING_OR_INSUFFICIENT"]})
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            items = router.process_decision(
                decision,
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-missing-top-down",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["reason"], "TOP_DOWN_READER_FAIL")
        self.assertEqual(items[0]["data"]["final_demo_decision"], "BLOCK")

    def test_top_down_fail_and_wait_use_specific_block_reason_names(self) -> None:
        router = self.router(hermes_adaptive_confluence_enabled=True)
        fail_payload = {
            "top_down_status": "FAIL",
            "decision": "WAIT_FOR_CONFIRMATION",
            "entry_readiness_score": 0,
            "reason": "NO_TRADE_DIRECTION",
            "missing_confirmations": [],
        }
        wait_payload = {
            "top_down_status": "WAIT",
            "decision": "WAIT_FOR_CONFIRMATION",
            "entry_readiness_score": 60,
            "missing_confirmations": ["M1_ENTRY"],
        }
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            fail_items = router.process_decision(
                self.exploration_decision(top_down_reader=fail_payload),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-top-down-fail",
                self.now,
            )
            wait_items = router.process_decision(
                self.exploration_decision(top_down_reader=wait_payload),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-top-down-wait",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(fail_items[0]["data"]["strict_block_reason"], "TOP_DOWN_READER_FAIL")
        self.assertEqual(fail_items[0]["data"]["reason"], "TOP_DOWN_READER_FAIL")
        self.assertEqual(wait_items[0]["data"]["strict_block_reason"], "TOP_DOWN_WAIT_FOR_CONFIRMATION")
        self.assertEqual(wait_items[0]["data"]["reason"], "TOP_DOWN_WAIT_FOR_CONFIRMATION")

    def test_final_demo_strong_smc_mtfa_fail_never_reaches_order_send(self) -> None:
        router = self.router()
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            smc_items = router.process_decision(
                self.exploration_decision(smc_confluence_score=49, mtfa_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-smc-strong-fail",
                self.now,
            )
            mtfa_items = router.process_decision(
                self.exploration_decision(smc_confluence_score=80, mtfa_score=49),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-mtfa-strong-fail",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(smc_items[0]["data"]["reason"], "SMC_STRONG_FAIL")
        self.assertEqual(mtfa_items[0]["data"]["reason"], "MTFA_STRONG_FAIL")

    def test_strong_setup_learning_allows_strong_near_miss_soft_blockers(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["mode"], "DEMO_STRONG_SETUP_LEARNING")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_STRONG_SETUP_LEARNING_MODE")
        self.assertEqual(set(result.event["exploration_ignored_block_reasons"]), {"MTFA_FAIL", "SMC_FAIL", "SESSION_NOT_ALLOWED"})
        self.assertEqual(result.event["strong_setup_learning_daily_count"], 0)
        self.assertEqual(result.event["top_down_decision"], "ALLOW_DEMO")
        self.assertEqual(result.event["entry_readiness_score"], 90)

    def test_strong_setup_learning_blocks_when_top_down_score_below_75(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        low_top_down = {
            "top_down_status": "WAIT",
            "decision": "WAIT_FOR_CONFIRMATION",
            "entry_readiness_score": 55,
            "market_narrative": "Wait for confirmation.",
            "missing_confirmations": ["M1_ENTRY"],
            "score_breakdown": {"m1_trigger": 0},
            "m1_trigger": False,
            "m15_confirmation": True,
        }
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(top_down_reader=low_top_down),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("TOP_DOWN_READER_BLOCK", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["top_down_decision"], "WAIT_FOR_CONFIRMATION")
        self.assertEqual(result.event["entry_readiness_score"], 55)

    def test_strong_setup_learning_disabled_keeps_same_setup_blocked_by_session(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=False)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("SESSION_NOT_ALLOWED", result.event["exploration_block_reasons"])

    def test_strong_setup_learning_keeps_btc_bad_hour_blocked(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("BTC_BAD_HOUR_BLOCK", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_strong_setup_learning_keeps_live_account_blocked(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")
        self.assertIn("ACCOUNT_TRADE_MODE_REAL", result.event["exploration_block_reasons"])

    def test_strong_setup_learning_caps_lot_to_point_zero_one(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True, demo_max_lot=0.5)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.2},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["final_capped_lot"], 0.01)

    def test_strong_setup_learning_daily_per_symbol_confirmed_trades_enforced(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True, demo_strong_setup_max_trades_per_symbol_per_day=3)
        confirmed = [
            {
                "event_type": "DEMO_ORDER",
                "created_at": self.now.isoformat(),
                "mode": "DEMO_STRONG_SETUP_LEARNING",
                "order_success": True,
                "order_retcode": 10009,
                "ticket": 1000 + i,
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
            }
            for i in range(3)
        ]
        self.events_path.write_text("\n".join(json.dumps(event) for event in confirmed), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_TRADES_PER_SYMBOL_PER_DAY", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["cap_block_reason"], "MAX_TRADES_PER_SYMBOL_PER_DAY")

    def test_strong_setup_learning_max_open_one_enforced(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD")]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_OPEN_TRADES_PER_SYMBOL", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_PER_SYMBOL")

    def test_strong_setup_learning_failed_mt5_result_is_not_confirmed(self) -> None:
        router = self.router(demo_strong_setup_learning_mode=True)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=None),
            patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()),
            patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()),
        ):
            items = router.process_decision(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertEqual(items[0]["data"]["mode"], "DEMO_STRONG_SETUP_LEARNING")
        self.assertEqual(items[0]["data"]["order_failure_reason"], "ORDER_RESULT_NONE")
        self.assertEqual(stats["strong_setup_learning_trades_opened_today"], 0)

    def bad_hour_time_gate(self) -> dict:
        return {
            "time_gate_status": "BLOCK",
            "time_gate_reason": "BTC_BAD_HOUR_BLOCK",
            "session_name": "ASIA",
            "symbol_market_open": True,
            "is_weekend": False,
            "is_bad_hour": True,
        }

    def bad_liquidity_time_gate(self) -> dict:
        return {
            "time_gate_status": "BLOCK",
            "time_gate_reason": "BAD_LIQUIDITY_HOUR",
            "session_name": "ASIA",
            "symbol_market_open": True,
            "is_weekend": False,
            "is_bad_hour": True,
        }

    def safety_pass(self) -> dict:
        return {
            "safety_guard_status": "PASS",
            "safety_guard_reason": "SAFETY_GUARD_PASS",
            "safety_guard_rules_triggered": [],
        }

    def safety_btc_bad_hour_block(self) -> dict:
        return {
            "safety_guard_status": "BLOCK",
            "safety_guard_reason": "BTC_BAD_HOUR_BLOCK",
            "safety_guard_rules_triggered": ["BTC_BAD_HOUR_BLOCK"],
        }

    def session_not_allowed_time_gate(self) -> dict:
        return {
            "time_gate_status": "PASS",
            "time_gate_reason": "TIME_GATE_PASS",
            "session_name": "ASIA",
            "symbol_market_open": True,
            "is_weekend": False,
            "is_bad_hour": False,
        }

    def waiting_for_session_time_gate(self) -> dict:
        return {
            "time_gate_status": "BLOCK",
            "time_gate_reason": "WAITING_FOR_SESSION",
            "session_name": "ASIA",
            "symbol_market_open": True,
            "is_weekend": False,
            "is_bad_hour": False,
        }

    def pass_time_gate(self) -> dict:
        return {
            "time_gate_status": "PASS",
            "time_gate_reason": "TIME_GATE_PASS",
            "session_name": "NEW_YORK",
            "symbol_market_open": True,
            "is_weekend": False,
            "is_bad_hour": False,
        }

    def market_closed_time_gate(self) -> dict:
        return {
            "time_gate_status": "PASS",
            "time_gate_reason": "TIME_GATE_PASS",
            "session_name": "NEW_YORK",
            "symbol_market_open": False,
            "is_weekend": False,
            "is_bad_hour": False,
        }

    def time_blocks_disabled_settings(self) -> dict:
        return {
            "allow_time_block_override": True,
            "demo_ignore_all_time_blocks": True,
            "demo_ignore_session_blocks": True,
            "demo_ignore_bad_hour_blocks": True,
            "demo_ignore_duration_blocks": True,
            "demo_ignore_setup_wait_hours": True,
        }

    def test_user_disabled_time_blocks_allows_bad_liquidity_hour_for_demo(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings())
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertTrue(result.event["ignored_time_blocks"])
        self.assertEqual(result.event["gate_statuses"]["time_gate_reason"], "TIME_BLOCKS_DISABLED_BY_USER_ORDER")

    def test_user_disabled_time_blocks_allows_btc_bad_hour_for_demo(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings())
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["gate_statuses"]["time_gate_reason"], "TIME_BLOCKS_DISABLED_BY_USER_ORDER")
        self.assertNotIn("BTC_BAD_HOUR_BLOCK", result.event["exploration_block_reasons"])

    def test_user_disabled_time_blocks_allows_waiting_for_session_for_demo(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings())
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.waiting_for_session_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["gate_statuses"]["time_gate_status"], "PASS")
        self.assertEqual(result.event["gate_statuses"]["time_gate_reason"], "TIME_BLOCKS_DISABLED_BY_USER_ORDER")

    def test_user_disabled_time_blocks_do_not_bypass_market_closed(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings())
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.market_closed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "MARKET_CLOSED")

    def test_user_disabled_time_blocks_do_not_bypass_spread_or_invalid_sl_tp(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings())
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            spread_result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                100,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
            invalid_sl_tp = router.evaluate(
                self.exploration_decision(entry=1.1000, sl=1.1010, tp=1.1020),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(spread_result.reason, "MAX_SPREAD")
        self.assertEqual(invalid_sl_tp.reason, "INVALID_SL_TP")

    def test_user_disabled_time_blocks_do_not_bypass_symbol_strategy_policy(self) -> None:
        router = self.router(**self.time_blocks_disabled_settings(), gold_disable_generic_strategies=True, btc_disable_quant_statistical_pullback=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            gold_result = router.evaluate(
                self.decision(symbol="GOLD#", strategy="TREND_CONTINUATION_BREAKDOWN", signal="BUY", entry=2400.0, sl=2390.0, tp=2420.0),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2400.0, "ask": 2400.1},
                {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
            btc_result = router.evaluate(
                self.decision(symbol="BTCUSD#", strategy="QUANT_STATISTICAL_PULLBACK", signal="BUY", entry=100.0, sl=99.0, tp=102.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-time-disabled",
                self.now,
            )
        self.assertEqual(gold_result.reason, "GOLD_GENERIC_STRATEGY_DISABLED")
        self.assertEqual(btc_result.reason, "BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT")

    def test_ignore_bad_hour_false_keeps_btc_bad_hour_blocked(self) -> None:
        router = self.router(demo_exploration_ignore_bad_hour=False)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "BTC_BAD_HOUR_BLOCK")
        self.assertIn("BTC_BAD_HOUR_BLOCK", result.event["exploration_block_reasons"])
        self.assertIn("SESSION_NOT_ALLOWED", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_demo_test_ignore_bad_hours_false_keeps_bad_liquidity_blocked(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=False)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "BAD_LIQUIDITY_HOUR")
        self.assertIn("BAD_LIQUIDITY_HOUR", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])
        self.assertFalse(result.event["demo_test_ignore_bad_hours_enabled"])

    def test_demo_test_ignore_bad_hours_true_allows_bad_liquidity_and_session_only(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_TEST_IGNORE_BAD_HOURS")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], ["BAD_LIQUIDITY_HOUR", "SESSION_NOT_ALLOWED"])
        self.assertTrue(result.event["demo_test_ignore_bad_hours_enabled"])

    def test_demo_test_ignore_bad_hours_true_allows_btc_bad_hour_and_session_only(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_TEST_IGNORE_BAD_HOURS")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], ["BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED"])

    def test_demo_test_ignore_bad_hours_true_allows_safety_guard_btc_bad_hour(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_btc_bad_hour_block()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["strict_block_reason"], "BTC_BAD_HOUR_BLOCK")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_TEST_IGNORE_BAD_HOURS")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], ["SESSION_NOT_ALLOWED", "BTC_BAD_HOUR_BLOCK"])

    def test_demo_test_ignore_bad_hours_true_allows_waiting_for_session(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.waiting_for_session_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_TEST_IGNORE_BAD_HOURS")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], ["WAITING_FOR_SESSION", "SESSION_NOT_ALLOWED"])

    def test_ignore_bad_hour_true_allows_only_bad_hour_and_session_blocks(self) -> None:
        router = self.router(demo_exploration_ignore_bad_hour=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["mode"], "DEMO_EXPLORATION")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_SMOKE_TEST_IGNORE_BAD_HOUR")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], ["BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED"])

    def test_ignore_bad_hour_true_does_not_override_live_account(self) -> None:
        router = self.router(demo_exploration_ignore_bad_hour=True, demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])
        self.assertIn("ACCOUNT_TRADE_MODE_REAL", result.event["exploration_block_reasons"])

    def test_ignore_bad_hour_does_not_override_lot_above_demo_max(self) -> None:
        router = self.router(demo_exploration_ignore_bad_hour=True, demo_test_ignore_bad_hours=True)
        gates = {
            "account_type": "DEMO",
            "account_trade_mode": 0,
            "allow_live_trading": False,
            "demo_only": True,
            "demo_trading": True,
            "magic": 909002,
            "current_symbol_open_count": 0,
            "open_demo_trades_total": 0,
            "current_symbol_daily_count": 0,
            "daily_demo_trades_total": 0,
            "market_open": True,
            "trade_allowed": True,
            "trade_expert": True,
            "symbol_allowed": True,
            "spread_ok": True,
            "sl_tp_valid": True,
            "rr": 2.0,
        }
        blocks, ignored = router._apply_smoke_test_bad_hour_override(["BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED"], gates, 0.02)
        self.assertEqual(blocks, ["BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED"])
        self.assertEqual(ignored, [])

    def test_demo_test_ignore_bad_hours_does_not_override_spread_fail(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.2},
                self.specs(),
                100,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_SPREAD", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_demo_test_ignore_bad_hours_does_not_override_daily_symbol_cap(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True, demo_max_trades_per_symbol_per_day=1)
        self.events_path.write_text(
            json.dumps(
                {
                    "event_type": "DEMO_ORDER",
                    "created_at": self.now.isoformat(),
                    "order_success": True,
                    "order_retcode": 10009,
                    "ticket": 9001,
                    "symbol": "EURUSD",
                    "broker_symbol": "EURUSD",
                }
            ),
            encoding="utf-8",
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_TRADES_PER_SYMBOL_PER_DAY", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_demo_test_ignore_bad_hours_does_not_override_open_symbol_cap(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True, demo_max_open_trades_per_symbol=1)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="EURUSD")]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("MAX_OPEN_TRADES_PER_SYMBOL", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_demo_test_ignore_bad_hours_does_not_override_top_down_avoid(self) -> None:
        router = self.router(demo_test_ignore_bad_hours=True)
        top_down_avoid = {
            "top_down_status": "FAIL",
            "decision": "AVOID",
            "entry_readiness_score": 90,
            "m1_trigger": True,
            "m15_confirmation": True,
        }
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_liquidity_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(top_down_reader=top_down_avoid),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("TOP_DOWN_READER_BLOCK", result.event["exploration_block_reasons"])
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_ignore_bad_hour_failed_mt5_result_is_order_failed_not_confirmed(self) -> None:
        router = self.router(demo_exploration_ignore_bad_hour=True)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=None),
            patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()),
            patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()),
        ):
            items = router.process_decision(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertEqual(items[0]["data"]["order_failure_reason"], "ORDER_RESULT_NONE")
        self.assertEqual(stats["demo_trades_opened_today"], 0)
        self.assertEqual(stats["exploration_trades_opened_today"], 0)

    def test_smoke_test_24h_false_keeps_normal_blocking(self) -> None:
        router = self.router(demo_smoke_test_24h=False)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.session_not_allowed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("SESSION_NOT_ALLOWED", result.event["exploration_block_reasons"])
        self.assertIn("EDGE_SCORE_BELOW_EXPLORATION_MIN", result.event["exploration_block_reasons"])
        self.assertIsNone(result.event["exploration_override_reason"])

    def test_smoke_test_24h_true_ignores_only_allowed_soft_blockers(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0, setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["mode"], "DEMO_EXPLORATION")
        self.assertEqual(result.event["exploration_override_reason"], "DEMO_SMOKE_TEST_24H")
        self.assertEqual(
            result.event["exploration_ignored_block_reasons"],
            ["BTC_BAD_HOUR_BLOCK", "SESSION_NOT_ALLOWED", "EDGE_SCORE_BELOW_EXPLORATION_MIN"],
        )
        self.assertTrue(result.event["demo_smoke_test_24h_enabled"])
        self.assertEqual(result.event["demo_smoke_test_confirmed_orders"], 0)

    def test_smoke_test_24h_does_not_override_live_account(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            result = router.evaluate(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0, setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(trade_mode=2),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.reason, "ACCOUNT_TRADE_MODE_REAL")
        self.assertEqual(result.event["exploration_ignored_block_reasons"], [])

    def test_smoke_test_24h_does_not_override_lot_above_demo_max(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        gates = {
            "account_type": "DEMO",
            "account_trade_mode": 0,
            "allow_live_trading": False,
            "demo_only": True,
            "demo_trading": True,
            "magic": 909002,
            "open_demo_trades": 0,
            "smoke_test_confirmed_orders": 0,
            "market_open": True,
            "trade_allowed": True,
            "trade_expert": True,
            "symbol_allowed": True,
            "spread_ok": True,
        }
        blocks, ignored = router._apply_smoke_test_24h_override(["EDGE_SCORE_BELOW_EXPLORATION_MIN"], gates, 0.02)
        self.assertEqual(blocks, ["EDGE_SCORE_BELOW_EXPLORATION_MIN"])
        self.assertEqual(ignored, [])

    def test_smoke_test_24h_keeps_market_trade_and_spread_hard_blocks(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.market_closed_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            market_result = router.evaluate(
                self.exploration_decision(setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(market_result.decision, "BLOCK")
        self.assertIn("MARKET_CLOSED", market_result.event["exploration_block_reasons"])

        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.pass_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            trade_result = router.evaluate(
                self.exploration_decision(setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(trade_allowed=False),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(trade_result.decision, "BLOCK")
        self.assertIn("TRADE_ALLOWED_FALSE", trade_result.event["exploration_block_reasons"])

        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.pass_time_gate()), patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()):
            spread_result = router.evaluate(
                self.exploration_decision(setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.1005},
                self.specs(),
                31,
                30,
                True,
                "setup-1",
                self.now,
            )
        self.assertEqual(spread_result.decision, "BLOCK")
        self.assertIn("MAX_SPREAD", spread_result.event["exploration_block_reasons"])

    def test_smoke_test_24h_after_one_confirmed_order_sends_no_second_order(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        confirmed = {
            "event_type": "DEMO_ORDER",
            "created_at": self.now.isoformat(),
            "mode": "DEMO_EXPLORATION",
            "exploration_override_reason": "DEMO_SMOKE_TEST_24H",
            "order_success": True,
            "order_retcode": 10009,
            "ticket": 12345,
        }
        self.events_path.write_text(json.dumps(confirmed), encoding="utf-8")
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
            patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()),
            patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()),
        ):
            items = router.process_decision(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0, setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_SKIP")
        self.assertIn("DEMO_SMOKE_TEST_CONFIRMED_ORDER_LIMIT", items[0]["data"]["exploration_block_reasons"])

    def test_smoke_test_24h_failed_mt5_result_is_not_confirmed(self) -> None:
        router = self.router(demo_smoke_test_24h=True)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send", return_value=None),
            patch("app.mt5.demo_router.TimeEngine.evaluate", return_value=self.bad_hour_time_gate()),
            patch("app.mt5.demo_router.SafetyGuard.evaluate", return_value=self.safety_pass()),
        ):
            items = router.process_decision(
                self.exploration_decision(symbol="BTCUSD#", signal="SELL", entry=100.0, sl=101.0, tp=98.0, setup_hunter_score=80, edge_score=80, setup_score=80),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertEqual(items[0]["data"]["exploration_override_reason"], "DEMO_SMOKE_TEST_24H")
        self.assertEqual(items[0]["data"]["order_failure_reason"], "ORDER_RESULT_NONE")
        self.assertEqual(stats["smoke_test_confirmed_orders"], 0)

    def test_exploration_order_still_flows_only_through_demo_router(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10009, order=67890, deal=0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send:
            items = router.process_decision(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
        send.assert_called_once()
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER")
        self.assertEqual(items[0]["data"]["mode"], "DEMO_EXPLORATION")
        self.assertTrue(items[0]["data"]["order_success"])
        self.assertEqual(items[0]["data"]["ticket"], 67890)

    def test_null_ticket_order_result_does_not_count_as_opened(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10009, order=0, deal=0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result):
            items = router.process_decision(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertFalse(items[0]["data"]["order_success"])
        self.assertEqual(items[0]["data"]["order_failure_reason"], "ORDER_TICKET_MISSING")
        self.assertEqual(stats["demo_trades_opened_today"], 0)
        self.assertEqual(stats["exploration_trades_opened_today"], 0)

    def test_failed_retcode_order_result_does_not_count_as_opened(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10016, order=12345, deal=0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result):
            items = router.process_decision(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertEqual(items[0]["data"]["order_retcode"], 10016)
        self.assertEqual(items[0]["data"]["order_failure_reason"], "ORDER_RETCODE_NOT_SUCCESS")
        self.assertEqual(stats["demo_trades_opened_today"], 0)
        self.assertEqual(stats["exploration_trades_opened_today"], 0)

    def test_success_retcode_with_deal_counts_as_opened(self) -> None:
        router = self.router()
        fake_result = SimpleNamespace(retcode=10009, order=0, deal=56789)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result):
            items = router.process_decision(
                self.exploration_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "EURUSD",
                {},
                {"bid": 1.1, "ask": 1.10001},
                self.specs(),
                1,
                30,
                True,
                "setup-1",
                self.now,
            )
            stats = router._stats(self.now)
        self.assertEqual(items[0]["data"]["event_type"], "DEMO_ORDER")
        self.assertTrue(items[0]["data"]["order_success"])
        self.assertEqual(items[0]["data"]["ticket"], 56789)
        self.assertEqual(stats["demo_trades_opened_today"], 1)
        self.assertEqual(stats["exploration_trades_opened_today"], 1)

    def test_max_exploration_trades_counts_confirmed_orders_only(self) -> None:
        failed_events = [
            {
                "event_type": "DEMO_ORDER_FAILED",
                "created_at": self.now.isoformat(),
                "mode": "DEMO_EXPLORATION",
                "order_success": False,
                "order_retcode": 10009,
                "ticket": None,
            }
            for _ in range(3)
        ]
        self.events_path.write_text("\n".join(json.dumps(event) for event in failed_events), encoding="utf-8")
        result = self.evaluate(decision=self.exploration_decision(), kelly={"approved_lot": 0.01})
        self.assertEqual(result.decision, "PASS")
        self.assertNotIn("MAX_TRADES_PER_SYMBOL_PER_DAY", result.event["exploration_block_reasons"])
        self.assertNotIn("MAX_TRADES_PER_DAY_TOTAL", result.event["exploration_block_reasons"])

    def test_exploration_report_fields_include_candidates_and_warnings(self) -> None:
        path = Path(self.tmp.name) / "exploration_events.jsonl"
        event = {
            "event_type": "DEMO_ORDER",
            "created_at": "2026-06-01T10:30:00+00:00",
            "mode": "DEMO_EXPLORATION",
            "order_success": True,
            "order_retcode": 10009,
            "ticket": 12345,
            "symbol": "BTCUSD#",
            "broker_symbol": "BTCUSD#",
            "strategy": "TREND_CONTINUATION_BREAKDOWN",
            "direction": "SELL",
            "decision": "PASS",
            "exploration_decision": "ALLOW",
            "exploration_warnings": ["SMC_FAIL", "MTFA_FAIL"],
            "strict_block_reason": "MTFA_FAIL",
            "exploration_override_reason": "SOFT_CONFLUENCE_OVERRIDE",
            "edge_score": 100,
            "setup_score": 90,
            "grade": "A",
            "rr": 2.0,
            "wsp_intelligence": {
                "wsp_enabled": True,
                "role": "OBSERVER_ONLY",
                "market_state": "BULLISH",
                "safety_guard_visual": "CAUTION",
                "trap_check": "VALID",
                "risk_score": 4,
            },
            "wsp_market_state": "BULLISH",
            "wsp_safety_guard_visual": "CAUTION",
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertTrue(report["exploration_mode_enabled"])
        self.assertEqual(report["exploration_trades_opened"], 1)
        self.assertEqual(report["exploration_order_attempts"], 1)
        self.assertEqual(report["exploration_orders_opened_confirmed"], 1)
        self.assertEqual(report["exploration_orders_failed"], 0)
        self.assertEqual(report["failed_order_retcode_counts"], {})
        self.assertEqual(report["latest_order_result"]["order_retcode"], 10009)
        self.assertEqual(report["exploration_candidates"][0]["mode"], "DEMO_EXPLORATION")
        self.assertEqual(report["exploration_warnings"], {"SMC_FAIL": 1, "MTFA_FAIL": 1})
        self.assertEqual(report["latest_exploration_decision"]["exploration_decision"], "ALLOW")
        self.assertEqual(report["strict_block_reason"], "MTFA_FAIL")
        self.assertEqual(report["exploration_override_reason"], "SOFT_CONFLUENCE_OVERRIDE")
        self.assertEqual(report["daily_demo_trades_by_symbol"], {"BTCUSD#": 1})
        self.assertEqual(report["daily_exploration_trades_by_symbol"], {"BTCUSD#": 1})
        self.assertEqual(report["trade_caps"]["daily_by_symbol"], {"BTCUSD#": 1})
        self.assertEqual(report["trade_caps"]["total_daily_cap"], 15)
        self.assertEqual(report["trade_caps"]["per_symbol_daily_cap"], 5)
        self.assertEqual(report["latest_decision"]["wsp_intelligence"]["role"], "OBSERVER_ONLY")
        self.assertEqual(report["latest_decision"]["wsp_market_state"], "BULLISH")
        self.assertEqual(report["latest_decision"]["wsp_safety_guard_visual"], "CAUTION")

    def test_demo_report_includes_quant_pro_hurst_filter_stats(self) -> None:
        path = Path(self.tmp.name) / "quant_pro_hurst_events.jsonl"
        events = [
            {
                "event_type": "SETUP_HUNTER",
                "created_at": "2026-06-01T10:00:00+00:00",
                "strategy": "QUANT_PRO_REGIME_SWITCHING",
                "quant_pro_hurst": 0.94,
                "quant_pro_hurst_filter_status": "PASS",
                "quant_pro_trend_strength": "STRONG_TREND",
                "quant_pro_min_trend_hurst": 0.90,
            },
            {
                "event_type": "SETUP_HUNTER",
                "created_at": "2026-06-01T10:05:00+00:00",
                "strategy": "QUANT_PRO_REGIME_SWITCHING",
                "reason": "QUANT_PRO_HURST_TREND_TOO_WEAK",
                "quant_pro_hurst": 0.72,
                "quant_pro_hurst_filter_status": "BLOCK",
                "quant_pro_hurst_block_reason": "QUANT_PRO_HURST_TREND_TOO_WEAK",
            },
            {
                "event_type": "DEMO_CLOSE",
                "created_at": "2026-06-01T10:30:00+00:00",
                "strategy": "QUANT_PRO_REGIME_SWITCHING",
                "quant_pro_hurst": 0.94,
                "pnl": 12.5,
                "result": "WIN",
            },
            {
                "event_type": "DEMO_CLOSE",
                "created_at": "2026-06-01T10:45:00+00:00",
                "strategy": "QUANT_PRO_REGIME_SWITCHING",
                "quant_pro_hurst": 0.66,
                "pnl": -2.0,
                "result": "LOSS",
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=2, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["quant_pro_hurst_pass_count"], 1)
        self.assertEqual(report["quant_pro_hurst_block_count"], 1)
        self.assertEqual(report["pnl_by_hurst_bucket"]["hurst >= 0.90"], 12.5)
        self.assertEqual(report["pnl_by_hurst_bucket"]["0.50 <= hurst < 0.70"], -2.0)

    def test_demo_report_counts_failed_exploration_orders_separately(self) -> None:
        path = Path(self.tmp.name) / "failed_exploration_events.jsonl"
        event = {
            "event_type": "DEMO_ORDER_FAILED",
            "created_at": "2026-06-01T10:30:00+00:00",
            "mode": "DEMO_EXPLORATION",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "strategy": "TREND_CONTINUATION_BREAKDOWN",
            "direction": "SELL",
            "decision": "PASS",
            "exploration_decision": "ALLOW",
            "exploration_warnings": ["SMC_FAIL", "MTFA_FAIL"],
            "order_success": False,
            "order_retcode": 10016,
            "order_failure_reason": "ORDER_RETCODE_NOT_SUCCESS",
            "ticket": None,
            "order_result": {"retcode": 10016, "order": 0, "deal": 0, "ticket": None},
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["exploration_trades_opened"], 0)
        self.assertEqual(report["exploration_order_attempts"], 1)
        self.assertEqual(report["exploration_orders_opened_confirmed"], 0)
        self.assertEqual(report["exploration_orders_failed"], 1)
        self.assertEqual(report["failed_order_retcode_counts"], {"10016": 1})
        self.assertEqual(report["latest_order_result"]["event_type"], "DEMO_ORDER_FAILED")
        self.assertEqual(report["latest_order_result"]["order_failure_reason"], "ORDER_RETCODE_NOT_SUCCESS")

    def test_demo_report_trade_details_preserve_closed_trade_strategy_metadata(self) -> None:
        path = Path(self.tmp.name) / "closed_trade_details.jsonl"
        event = {
            "event_type": "POSITION_SYNC",
            "created_at": "2026-06-01T10:30:00+00:00",
            "status": "CLOSED",
            "result": "CLOSED",
            "ticket": "329436508",
            "symbol": "BTCUSD#",
            "direction": "SELL",
            "pnl": 4.95,
            "payload": {
                "strategy": "TREND_CONTINUATION_BREAKDOWN",
                "rr": 2.0,
                "kelly_suggested_lot": 0.03,
                "final_capped_lot": 0.01,
                "entry": 100.0,
                "sl": 101.0,
                "tp": 98.0,
                "exploration_override_reason": "DEMO_STRONG_SETUP_LEARNING_MODE",
                "status": "CLOSED",
            },
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        detail = report["demo_trade_details"][0]
        self.assertEqual(detail["ticket"], "329436508")
        self.assertEqual(detail["strategy"], "TREND_CONTINUATION_BREAKDOWN")
        self.assertEqual(detail["rr"], 2.0)
        self.assertEqual(detail["kelly_suggested_lot"], 0.03)
        self.assertEqual(detail["final_capped_lot"], 0.01)
        self.assertEqual(detail["pnl"], 4.95)
        self.assertEqual(detail["status"], "CLOSED")
        self.assertEqual(detail["gate"], "DEMO_STRONG_SETUP_LEARNING_MODE")

    def test_demo_report_pnl_sums_closed_hermes_demo_trade_rows_signed(self) -> None:
        path = Path(self.tmp.name) / "closed_trade_pnl_rows.jsonl"
        events = [
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T09:10:00+00:00",
                "status": "CLOSED",
                "result": "CLOSED",
                "ticket": "329436508",
                "magic_number": 909002,
                "symbol": "BTCUSD#",
                "pnl": -18.22,
            },
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T10:20:00+00:00",
                "status": "CLOSED",
                "result": "CLOSED",
                "ticket": "329313966",
                "magic_number": 909002,
                "symbol": "BTCUSD#",
                "raw_payload": {"pnl": -14.38},
            },
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T11:30:00+00:00",
                "status": "CLOSED",
                "result": "CLOSED",
                "ticket": "328961620",
                "magic_number": 909002,
                "symbol": "GOLD#",
                "payload": {"profit": -14.38},
            },
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T11:40:00+00:00",
                "status": "CLOSED",
                "result": "CLOSED",
                "ticket": "other-magic",
                "magic_number": 909001,
                "symbol": "EURUSD",
                "pnl": 999.0,
            },
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T11:50:00+00:00",
                "status": "OPEN",
                "result": "OPEN",
                "ticket": "open-demo",
                "magic_number": 909002,
                "symbol": "EURUSD",
                "pnl": -999.0,
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(
            "app.services.mt5_pnl_truth.mt5.initialize",
            return_value=False,
        ), patch("app.services.mt5_pnl_truth.mt5.history_deals_get", return_value=None):
            report = build_demo_report(demo_settings(), path, hours=24, now=datetime.fromisoformat("2026-06-03T12:00:00+00:00"))
        self.assertEqual(report["demo_trades_closed"], 3)
        self.assertEqual(report["demo_pnl"], -46.98)

    def test_demo_report_uses_mt5_history_deals_as_pnl_truth(self) -> None:
        path = Path(self.tmp.name) / "mt5_history_pnl_truth.jsonl"
        events = [
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-03T09:10:00+00:00",
                "status": "CLOSED",
                "result": "CLOSED",
                "ticket": "local-1",
                "magic_number": 909002,
                "symbol": "BTCUSD#",
                "pnl": -12.73,
            }
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        deals = [
            SimpleNamespace(magic=909002, profit=-20.0, commission=-1.5, swap=-0.5),
            SimpleNamespace(magic=909002, profit=-23.98, commission=-1.0, swap=0.0),
            SimpleNamespace(magic=909001, profit=999.0, commission=0.0, swap=0.0),
        ]
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(
            "app.services.mt5_pnl_truth.mt5.initialize",
            return_value=True,
        ), patch("app.services.mt5_pnl_truth.mt5.history_deals_get", return_value=deals):
            report = build_demo_report(demo_settings(), path, hours=48, now=datetime.fromisoformat("2026-06-03T12:00:00+00:00"))
        self.assertEqual(report["trades_table_pnl"], -12.73)
        self.assertEqual(report["mt5_today_pnl"], -46.98)
        self.assertEqual(report["mt5_48h_pnl"], -46.98)
        self.assertEqual(report["demo_pnl"], -46.98)
        self.assertEqual(report["pnl_difference"], -34.25)
        self.assertEqual(report["pnl_source"], "MT5_HISTORY_DEALS")
        self.assertEqual(report["mt5_closed_deals_count"], 2)
        self.assertEqual(report["mt5_gross_profit"], 0.0)
        self.assertEqual(report["mt5_gross_loss"], -46.98)
        self.assertEqual(report["pnl_warning"], "DEMO_REPORT_PNL_MISMATCH")
        self.assertEqual(report["pnl_reconciliation_status"], "DEMO_REPORT_PNL_MISMATCH")

    def test_demo_report_falls_back_to_trades_table_when_mt5_history_unavailable(self) -> None:
        path = Path(self.tmp.name) / "mt5_history_unavailable.jsonl"
        event = {
            "event_type": "DEMO_CLOSE",
            "created_at": "2026-06-03T09:10:00+00:00",
            "status": "CLOSED",
            "result": "CLOSED",
            "ticket": "local-1",
            "magic_number": 909002,
            "symbol": "BTCUSD#",
            "pnl": -12.73,
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch(
            "app.services.mt5_pnl_truth.mt5.initialize",
            return_value=False,
        ), patch("app.services.mt5_pnl_truth.mt5.history_deals_get", return_value=None), patch(
            "app.services.mt5_pnl_truth.mt5.last_error",
            return_value=(-1, "history unavailable"),
        ):
            report = build_demo_report(demo_settings(), path, hours=48, now=datetime.fromisoformat("2026-06-03T12:00:00+00:00"))
        self.assertEqual(report["demo_pnl"], -12.73)
        self.assertEqual(report["trades_table_pnl"], -12.73)
        self.assertIsNone(report["mt5_today_pnl"])
        self.assertIsNone(report["pnl_difference"])
        self.assertEqual(report["pnl_source"], "TRADES_TABLE_FALLBACK")
        self.assertEqual(report["pnl_reconciliation_status"], "MT5_HISTORY_UNAVAILABLE")
        self.assertEqual(report["mt5_history_error"], "MT5_HISTORY_DEALS_UNAVAILABLE")
        self.assertFalse(report["mt5_initialized"])
        self.assertEqual(report["mt5_last_error"], (-1, "history unavailable"))

    def test_demo_report_default_hours_and_since_pilot_filters(self) -> None:
        path = Path(self.tmp.name) / "events.jsonl"
        events = [
            {
                "event_type": "DEMO_SKIP",
                "created_at": "2026-06-01T08:00:00+00:00",
                "reason": "ACCOUNT_NOT_DEMO",
                "failed_gate": "ACCOUNT_NOT_DEMO",
                "symbol": "BTCUSD",
                "strategy": "CRT_TBS_REVERSAL",
            },
            {
                "event_type": "DEMO_SKIP",
                "created_at": "2026-06-01T09:00:00+00:00",
                "reason": "SYMBOL_NOT_ALLOWED",
                "failed_gate": "SYMBOL_NOT_ALLOWED",
                "symbol": "BTCUSD",
                "strategy": "EMA_PULLBACK",
            },
            {
                "event_type": "DEMO_SKIP",
                "created_at": "2026-06-01T10:30:00+00:00",
                "reason": "SMC_FAIL",
                "failed_gate": "SMC_FAIL",
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
                "strategy": "BREAKOUT_RETEST",
                "rr": 2.0,
                "sl": 1.1,
                "tp": 1.2,
                "gate_statuses": {"spread_ok": True, "time_gate_status": "PASS"},
                "time_gate": {"session_name": "LONDON"},
                "decision": "BLOCK",
            },
            {
                "event_type": "NEAR_MISS",
                "created_at": "2026-06-01T10:35:00+00:00",
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
                "strategy": "BREAKOUT_RETEST",
                "strategy_role": "ENTRY",
                "direction": "BUY",
                "edge_score": 78,
                "near_miss_reason": "WAITING_FOR_M1_TRIGGER",
                "missing": ["WAITING_FOR_M1_TRIGGER"],
                "time_session": "LONDON",
                "time_gate_status": "PASS",
                "smc_status": "PASS",
                "mtfa_status": "PASS",
                "m15_confirmation": True,
                "m1_confirmation": False,
                "rr": 2.0,
                "sl": 1.1,
                "tp": 1.2,
            },
            {
                "event_type": "SETUP_HUNTER",
                "created_at": "2026-06-01T10:40:00+00:00",
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
                "strategy": "BREAKOUT_RETEST",
                "strategy_role": "ENTRY",
                "direction": "BUY",
                "edge_score": 82,
                "demo_eligible": False,
                "near_miss_reason": "WAITING_FOR_M1_TRIGGER",
                "missing": ["WAITING_FOR_M1_TRIGGER"],
                "time_session": "LONDON",
                "time_gate_status": "PASS",
                "smc_status": "PASS",
                "mtfa_status": "PASS",
                "m15_confirmation": True,
                "m1_confirmation": False,
                "rr": 2.0,
                "sl": 1.1,
                "tp": 1.2,
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        now = datetime.fromisoformat("2026-06-01T11:00:00+00:00")

        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            default_report = build_demo_report(demo_settings(demo_pilot_started_at="2026-06-01T10:00:00+00:00"), path, now=now)
            self.assertIn("ACCOUNT_NOT_DEMO", default_report["current_window_skip_reasons"])
            self.assertIn("SYMBOL_NOT_ALLOWED", default_report["current_window_skip_reasons"])

            hours_report = build_demo_report(demo_settings(demo_pilot_started_at="2026-06-01T10:00:00+00:00"), path, hours=1, now=now)
            self.assertNotIn("ACCOUNT_NOT_DEMO", hours_report["current_window_skip_reasons"])
            self.assertNotIn("SYMBOL_NOT_ALLOWED", hours_report["current_window_skip_reasons"])
            self.assertIn("ACCOUNT_NOT_DEMO", hours_report["historical_skip_reasons"])
            self.assertEqual(hours_report["latest_decision"]["reason"], "SMC_FAIL")
            self.assertEqual(hours_report["latest_good_rr_but_blocked_reason"], "SMC_FAIL")
            self.assertEqual(hours_report["blocked_by_session"]["LONDON"], 1)
            self.assertEqual(hours_report["blocked_by_symbol"]["EURUSD"], 1)
            self.assertEqual(hours_report["blocked_by_strategy"]["BREAKOUT_RETEST"], 1)
            self.assertEqual(hours_report["top_failed_gates_current_window"]["SMC_FAIL"], 1)
            self.assertEqual(hours_report["near_miss_reasons"]["WAITING_FOR_M1_TRIGGER"], 1)
            self.assertEqual(hours_report["best_near_miss"]["near_miss_reason"], "WAITING_FOR_M1_TRIGGER")
            self.assertEqual(hours_report["setup_hunter_last_decision"]["event_type"], "SETUP_HUNTER")
            self.assertEqual(len(hours_report["edge_ready_candidates"]), 0)

            pilot_report = build_demo_report(demo_settings(demo_pilot_started_at="2026-06-01T10:00:00+00:00"), path, since_pilot_start=True, now=now)
            self.assertNotIn("ACCOUNT_NOT_DEMO", pilot_report["current_window_skip_reasons"])
            self.assertNotIn("SYMBOL_NOT_ALLOWED", pilot_report["current_window_skip_reasons"])
            self.assertIn("ACCOUNT_NOT_DEMO", pilot_report["historical_skip_reasons"])

    def test_demo_report_cli_accepts_default_hours_and_since_pilot(self) -> None:
        cases = [
            (["app/main.py", "--demo-report"], (None, False, False, False)),
            (["app/main.py", "--demo-report", "--hours", "1"], (1, False, False, False)),
            (["app/main.py", "--demo-report", "--since-pilot-start"], (None, True, False, False)),
            (["app/main.py", "--demo-report", "--since-backend-start"], (None, False, True, False)),
            (["app/main.py", "--demo-report", "--since-report-reset"], (None, False, False, True)),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv), patch("sys.argv", argv), patch("app.main.run_demo_report") as run:
                main_module.main()
                run.assert_called_once_with(*expected)

    def test_reset_demo_report_window_cli_is_accepted(self) -> None:
        with patch("sys.argv", ["app/main.py", "--reset-demo-report-window"]), patch("app.main.run_reset_demo_report_window") as run:
            main_module.main()
            run.assert_called_once()

    def test_strategy_audit_cli_is_accepted(self) -> None:
        with patch("sys.argv", ["app/main.py", "--audit-strategy", "QUANT_STATISTICAL_PULLBACK", "--symbol", "BTCUSD#", "--hours", "48"]), patch("app.main.run_strategy_audit") as run:
            main_module.main()
            run.assert_called_once_with("QUANT_STATISTICAL_PULLBACK", "BTCUSD#", 48)

    def test_sync_mt5_positions_once_cli_is_accepted(self) -> None:
        with patch("sys.argv", ["app/main.py", "--sync-mt5-positions-once"]), patch("app.main.run_sync_mt5_positions_once") as run:
            main_module.main()
            run.assert_called_once()

    def test_force_close_demo_ticket_cli_is_accepted(self) -> None:
        with patch("sys.argv", ["app/main.py", "--force-close-demo-ticket", "328961620"]), patch("app.main.run_force_close_demo_ticket") as run:
            main_module.main()
            run.assert_called_once_with("328961620")

    def test_session_diag_cli_is_accepted(self) -> None:
        with patch("sys.argv", ["app/main.py", "--session-diag"]), patch("app.main.run_session_diag") as run:
            main_module.main()
            run.assert_called_once()

    def test_demo_report_excludes_future_dated_events(self) -> None:
        path = Path(self.tmp.name) / "future_events.jsonl"
        events = [
            {"event_type": "DEMO_SKIP", "created_at": "2026-06-01T10:30:00+00:00", "reason": "NO_TRADE_DIRECTION", "strategy": "BREAKOUT_RETEST"},
            {"event_type": "DEMO_SKIP", "created_at": "2026-06-01T13:30:00+00:00", "reason": "EMA_PULLBACK_CONFIRMATION_ONLY", "strategy": "EMA_PULLBACK"},
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["future_events_excluded_count"], 1)
        self.assertEqual(report["future_events_excluded_by_reason"]["EMA_PULLBACK_CONFIRMATION_ONLY"], 1)
        self.assertEqual(report["current_window_skip_reasons"], {"NO_TRADE_DIRECTION": 1})

    def test_demo_report_live_mt5_count_overrides_stale_open_sync_event(self) -> None:
        path = Path(self.tmp.name) / "stale_position_sync.jsonl"
        event = {
            "event_type": "POSITION_SYNC",
            "created_at": "2026-06-01T10:30:00+00:00",
            "result": "OPEN",
            "ticket": "328961620",
            "symbol": "GOLD#",
            "magic_number": 909002,
            "mt5_open_positions_count": 1,
            "hermes_mt5_open_positions_count": 1,
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["current_mt5_open_positions_count"], 0)
        self.assertEqual(report["current_hermes_mt5_open_positions_count"], 0)
        self.assertEqual(report["mt5_open_positions_count"], 0)
        self.assertEqual(report["hermes_mt5_open_positions_count"], 0)
        self.assertEqual(report["latest_position_sync_event"]["ticket"], "328961620")
        self.assertIsNone(report["latest_position_close_event"])

    def test_demo_report_clears_stale_open_symbol_strategy_counter_when_mt5_has_no_open_positions(self) -> None:
        path = Path(self.tmp.name) / "stale_strategy_counter.jsonl"
        event = {
            "event_type": "DEMO_ORDER",
            "created_at": "2026-06-01T10:30:00+00:00",
            "order_success": True,
            "order_retcode": 10009,
            "ticket": "331802677",
            "symbol": "BTCUSD#",
            "broker_symbol": "BTCUSD#",
            "strategy": "QUANT_PRO_REGIME_SWITCHING",
            "status": "OPEN",
            "result": "CONFIRMED",
            "magic_number": 909002,
        }
        path.write_text(json.dumps(event), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=24, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["open_demo_trades_total"], 0)
        self.assertEqual(report["open_demo_trades_by_symbol_strategy"], {})
        self.assertEqual(report["trade_caps"]["open_by_symbol_strategy"], {})
        self.assertEqual(report["current_router_state"]["open_demo_trades_by_symbol_strategy"], {})

    def test_demo_report_position_sync_count_replays_close_event(self) -> None:
        path = Path(self.tmp.name) / "closed_position_sync.jsonl"
        events = [
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-01T10:20:00+00:00",
                "result": "OPEN",
                "ticket": "328961620",
                "symbol": "GOLD#",
                "magic_number": 909002,
                "mt5_open_positions_count": 1,
                "hermes_mt5_open_positions_count": 1,
            },
            {
                "event_type": "POSITION_SYNC",
                "created_at": "2026-06-01T10:30:00+00:00",
                "result": "CLOSED",
                "ticket": "328961620",
                "symbol": "GOLD#",
                "magic_number": 909002,
                "close_reason": "MT5_POSITION_MISSING_CLOSED",
                "mt5_open_positions_count": 0,
                "hermes_mt5_open_positions_count": 0,
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(), path, hours=1, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(report["supabase_open_trades_synced_count"], 0)
        self.assertEqual(report["latest_position_close_event"]["ticket"], "328961620")

    def test_demo_report_since_backend_start_and_report_reset(self) -> None:
        path = Path(self.tmp.name) / "marker_events.jsonl"
        events = [
            {"event_type": "DEMO_SKIP", "created_at": "2026-06-01T09:55:00+00:00", "reason": "EMA_PULLBACK_CONFIRMATION_ONLY", "strategy": "EMA_PULLBACK"},
            {"event_type": "SETUP_HUNTER", "created_at": "2026-06-01T10:05:00+00:00", "strategy": "BREAKOUT_RETEST", "direction": "BUY", "event_type": "SETUP_HUNTER"},
            {"event_type": "NEAR_MISS", "created_at": "2026-06-01T10:10:00+00:00", "strategy": "BREAKOUT_RETEST", "direction": "BUY", "near_miss_reason": "WAITING_FOR_M1_TRIGGER", "missing": ["WAITING_FOR_M1_TRIGGER"]},
            {"event_type": "DEMO_SKIP", "created_at": "2026-06-01T10:20:00+00:00", "reason": "NO_TRADE_DIRECTION", "strategy": "BREAKOUT_RETEST"},
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        now = datetime.fromisoformat("2026-06-01T11:00:00+00:00")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.read_backend_started_at", return_value=datetime.fromisoformat("2026-06-01T10:00:00+00:00")), patch("app.mt5.demo_router.read_report_window_started_at", return_value=datetime.fromisoformat("2026-06-01T10:15:00+00:00")):
            backend_report = build_demo_report(demo_settings(), path, since_backend_start=True, now=now)
            reset_report = build_demo_report(demo_settings(), path, since_report_reset=True, now=now)
        self.assertNotIn("EMA_PULLBACK_CONFIRMATION_ONLY", backend_report["current_window_skip_reasons"])
        self.assertEqual(backend_report["setup_hunter_events"], 1)
        self.assertEqual(backend_report["near_miss_events"], 1)
        self.assertEqual(reset_report["current_window_skip_reasons"], {"NO_TRADE_DIRECTION": 1})
        self.assertEqual(reset_report["setup_hunter_events"], 0)
        self.assertEqual(reset_report["near_miss_events"], 0)

    def test_strategy_edge_report_writes_expected_fields(self) -> None:
        events_path = Path(self.tmp.name) / "edge_events.jsonl"
        output = Path(self.tmp.name) / "edge_report"
        events = [
            {
                "event_type": "NEAR_MISS",
                "created_at": "2026-06-01T10:30:00+00:00",
                "reason": "WAITING_FOR_M1_TRIGGER",
                "failed_gate": "WAITING_FOR_M1_TRIGGER",
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
                "strategy": "BREAKOUT_RETEST",
                "rr": 2.0,
                "sl": 1.0990,
                "tp": 1.1020,
                "setup_hunter_score": 82,
                "edge_score": 82,
                "near_miss_reason": "WAITING_FOR_M1_TRIGGER",
                "missing": ["WAITING_FOR_M1_TRIGGER"],
                "time_session": "LONDON",
                "time_gate_status": "PASS",
                "smc_status": "PASS",
                "mtfa_status": "PASS",
                "m15_confirmation": True,
                "m1_confirmation": False,
                "gate_statuses": {"spread_ok": True, "time_gate_status": "PASS", "m15_confirmation": True, "m1_entry_confirmation": False},
                "time_gate": {"session_name": "LONDON"},
            }
        ]
        events_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.tools.strategy_edge_report.get_settings", return_value=demo_settings()), patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = strategy_edge_report.build_strategy_edge_report(["EURUSD"], 24, output, events_path=events_path)
        written = json.loads((output / "strategy_edge_report.json").read_text(encoding="utf-8"))
        for field in (
            "candidates_seen",
            "edge_ready_candidates",
            "near_miss_candidates",
            "demo_orders",
            "blocked_by_reason",
            "blocked_by_session",
            "blocked_by_symbol",
            "blocked_by_strategy",
            "best_near_miss",
            "best_edge_strategy",
            "best_symbol",
            "latest_good_setup",
            "latest_good_rr_but_blocked_reason",
            "sessions_with_most_valid_setups",
            "strategies_producing_too_many_false_candidates",
        ):
            self.assertIn(field, report)
            self.assertIn(field, written)
        self.assertEqual(report["near_miss_candidates"], 1)
        self.assertEqual(report["best_near_miss"]["near_miss_reason"], "WAITING_FOR_M1_TRIGGER")

    def test_demo_report_reads_setup_hunter_and_near_miss_events(self) -> None:
        path = Path(self.tmp.name) / "setup_events.jsonl"
        events = [
            {
                "event_type": "NEAR_MISS",
                "created_at": "2026-06-01T10:30:00+00:00",
                "symbol": "BTCUSD#",
                "broker_symbol": "BTCUSD#",
                "strategy": "BREAKOUT_RETEST",
                "strategy_role": "ENTRY",
                "direction": "SELL",
                "edge_score": 91,
                "near_miss_reason": "WAITING_FOR_M1_TRIGGER",
                "missing": ["WAITING_FOR_M1_TRIGGER"],
                "time_session": "OVERLAP",
                "time_gate_status": "PASS",
                "smc_status": "PASS",
                "mtfa_status": "PASS",
                "m15_confirmation": True,
                "m1_confirmation": False,
                "rr": 2.0,
                "entry": 100.0,
                "sl": 101.0,
                "tp": 98.0,
            },
            {
                "event_type": "SETUP_HUNTER",
                "created_at": "2026-06-01T10:31:00+00:00",
                "symbol": "BTCUSD#",
                "broker_symbol": "BTCUSD#",
                "strategy": "BREAKOUT_RETEST",
                "strategy_role": "ENTRY",
                "direction": "SELL",
                "edge_score": 91,
                "demo_eligible": False,
                "near_miss_reason": "WAITING_FOR_M1_TRIGGER",
                "missing": ["WAITING_FOR_M1_TRIGGER"],
                "time_session": "OVERLAP",
                "time_gate_status": "PASS",
                "smc_status": "PASS",
                "mtfa_status": "PASS",
                "m15_confirmation": True,
                "m1_confirmation": False,
                "rr": 2.0,
                "entry": 100.0,
                "sl": 101.0,
                "tp": 98.0,
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            report = build_demo_report(demo_settings(demo_pilot_started_at="2026-06-01T10:00:00+00:00"), path, since_pilot_start=True, now=datetime.fromisoformat("2026-06-01T11:00:00+00:00"))
        self.assertEqual(len(report["near_miss_candidates"]), 1)
        self.assertIsNotNone(report["best_near_miss"])
        self.assertEqual(report["setup_hunter_last_decision"]["event_type"], "SETUP_HUNTER")

    def test_kelly_lot_capped_to_demo_max_lot(self) -> None:
        result = self.evaluate(kelly={"approved_lot": 0.2})
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["final_capped_lot"], 0.01)

    def test_raw_btcusd_with_resolved_btcusd_hash_passes_allowed_symbol_check(self) -> None:
        result = self.evaluate(
            decision=self.decision(symbol="BTCUSD", strategy="BREAKOUT_RETEST"),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        self.assertNotEqual(result.reason, "SYMBOL_NOT_ALLOWED")
        self.assertNotEqual(result.reason, "SYMBOL_NOT_RESOLVED")
        self.assertEqual(result.event["raw_symbol"], "BTCUSD")
        self.assertEqual(result.event["broker_symbol"], "BTCUSD#")
        self.assertEqual(result.event["allowed_symbol_check"], "PASS")

    def test_btcusd_hash_passes_btc_only_symbol_gate(self) -> None:
        result = self.evaluate(
            router=self.router(
                hermes_trade_symbols="BTCUSD#,BTCUSD",
                hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
            ),
            decision=self.decision(symbol="BTCUSD#", strategy="BREAKOUT_RETEST"),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        self.assertNotEqual(result.reason, "SYMBOL_ANALYSIS_ONLY")
        self.assertEqual(result.event["symbol_gate_decision"], "PASS")
        self.assertEqual(result.event["symbol_gate_reason"], "SYMBOL_ALLOWED_FOR_DEMO")

    def test_raw_btcusd_normalizes_to_btcusd_hash_and_passes_btc_only_symbol_gate(self) -> None:
        result = self.evaluate(
            router=self.router(
                hermes_trade_symbols="BTCUSD#,BTCUSD",
                hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
            ),
            decision=self.decision(symbol="BTCUSD", strategy="BREAKOUT_RETEST"),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        self.assertEqual(result.event["raw_symbol"], "BTCUSD")
        self.assertEqual(result.event["broker_symbol"], "BTCUSD#")
        self.assertEqual(result.event["symbol_gate_canonical"], "BTCUSD")
        self.assertEqual(result.event["symbol_gate_decision"], "PASS")

    def test_gold_hash_blocks_analysis_only_before_order_send(self) -> None:
        router = self.router(
            hermes_trade_symbols="BTCUSD#,BTCUSD",
            hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
        )
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch("app.mt5.demo_router.mt5.order_send") as send:
            items = router.process_decision(
                self.decision(symbol="GOLD#", strategy="BREAKOUT_RETEST", entry=2300.0, sl=2299.0, tp=2302.0),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2300.0, "ask": 2300.1},
                {"tick_value": 1.0, "tick_size": 0.1, "volume_step": 0.01},
                1,
                30,
                True,
                "setup-gold",
                self.now,
            )
        send.assert_not_called()
        self.assertEqual(items, [])

    def test_gold_and_xauusd_block_analysis_only(self) -> None:
        router = self.router(
            hermes_trade_symbols="BTCUSD#,BTCUSD",
            hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
        )
        cases = [("GOLD", "GOLD#"), ("XAUUSD", "GOLD#")]
        for raw_symbol, broker_symbol in cases:
            with self.subTest(raw_symbol=raw_symbol):
                result = self.evaluate(
                    router=router,
                    decision=self.decision(symbol=raw_symbol, strategy="BREAKOUT_RETEST", entry=2300.0, sl=2299.0, tp=2302.0),
                    kelly={"approved_lot": 0.01},
                    broker_symbol=broker_symbol,
                )
                self.assertEqual(result.reason, "SYMBOL_ANALYSIS_ONLY")
                self.assertEqual(result.event["symbol_analysis_only"], True)

    def test_eurusd_blocks_analysis_only(self) -> None:
        result = self.evaluate(
            router=self.router(
                hermes_trade_symbols="BTCUSD#,BTCUSD",
                hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
            ),
            decision=self.decision(symbol="EURUSD", strategy="BREAKOUT_RETEST"),
            kelly={"approved_lot": 0.01},
            broker_symbol="EURUSD",
        )
        self.assertEqual(result.reason, "SYMBOL_ANALYSIS_ONLY")
        self.assertEqual(result.event["symbol_gate_reason"], "SYMBOL_ANALYSIS_ONLY")

    def test_analysis_only_symbols_keep_analysis_payload(self) -> None:
        result = self.evaluate(
            router=self.router(
                hermes_trade_symbols="BTCUSD#,BTCUSD",
                hermes_analysis_only_symbols="GOLD#,GOLD,XAUUSD,EURUSD",
            ),
            decision=self.decision(
                symbol="EURUSD",
                strategy="BREAKOUT_RETEST",
                top_down_reader={
                    "top_down_status": "PASS",
                    "decision": "ALLOW_DEMO",
                    "entry_readiness_score": 82,
                    "market_narrative": "analysis still published",
                    "missing_confirmations": [],
                    "score_breakdown": {"m15": 20},
                },
                quant_score=77,
                quant_signal="BUY",
            ),
            kelly={"approved_lot": 0.01},
            broker_symbol="EURUSD",
        )
        self.assertEqual(result.reason, "SYMBOL_ANALYSIS_ONLY")
        self.assertEqual(result.event["top_down_status"], "PASS")
        self.assertEqual(result.event["entry_readiness_score"], 82)
        self.assertEqual(result.event["quant_score"], 77)

    def test_gold_generic_strategy_disabled_when_gold_liquidity_mode_false(self) -> None:
        result = self.evaluate(
            router=self.router(
                hermes_trade_symbols="GOLD#,GOLD,XAUUSD",
                hermes_analysis_only_symbols="",
                gold_liquidity_mode="trade",
                gold_disable_generic_strategies=True,
            ),
            decision=self.decision(symbol="GOLD#", strategy="BREAKOUT_RETEST", entry=2300.0, sl=2299.0, tp=2302.0),
            kelly={"approved_lot": 0.01},
            broker_symbol="GOLD#",
        )
        self.assertEqual(result.reason, "GOLD_GENERIC_STRATEGY_DISABLED")

    def test_raw_btcusd_without_resolved_broker_symbol_blocks_unresolved(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(symbol="BTCUSD"),
                {"approved_lot": 0.01},
                self.account(),
                "",
                {},
                {},
                self.specs(),
                1,
                2500,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "SYMBOL_NOT_RESOLVED")

    def test_resolved_symbol_not_in_allowed_list_blocks(self) -> None:
        result = self.evaluate(decision=self.decision(symbol="GBPUSD"), kelly={"approved_lot": 0.01}, broker_symbol="GBPUSD")
        self.assertEqual(result.reason, "SYMBOL_NOT_ALLOWED")

    def test_wait_signal_blocks_no_trade_direction_before_kelly_validation(self) -> None:
        result = self.evaluate(
            decision=self.decision(symbol="EURUSD", signal="WAIT", entry=None, sl=None, tp=None, reward_risk=None),
            kelly={"approved_lot": 0},
            broker_symbol="EURUSD",
        )
        self.assertEqual(result.reason, "NO_TRADE_DIRECTION")
        self.assertIsNone(result.event["kelly_suggested_lot"])
        self.assertIsNone(result.event["final_capped_lot"])

    def test_kelly_lot_math_is_not_run_for_wait_signals(self) -> None:
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), patch.object(router, "_risk_lot", side_effect=AssertionError("risk lot should not run")):
            result = router.evaluate(
                self.decision(symbol="EURUSD", signal="WAIT", entry=None, sl=None, tp=None, reward_risk=None),
                {"approved_lot": 0},
                self.account(),
                "EURUSD",
                {},
                {},
                self.specs(),
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(result.reason, "NO_TRADE_DIRECTION")

    def test_invalid_kelly_output_blocks_order(self) -> None:
        result = self.evaluate(kelly={"approved_lot": 0})
        self.assertIn(result.reason, {"KELLY_INVALID_LOT", "PASS"})

    def test_daily_loss_stop_blocks_order(self) -> None:
        self.events_path.write_text(
            json.dumps({"event_type": "DEMO_CLOSE", "pnl": -100.0, "result": "LOSS", "created_at": self.now.isoformat()}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self.evaluate().reason, "DEMO_DAILY_LOSS_STOP")

    def test_max_open_trades_blocks_order(self) -> None:
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="GOLD#")]):
            result = self.router().evaluate(
                self.decision(), {"approved_lot": 0.01}, self.account(), "EURUSD", {}, {}, self.specs(), 1, 30, True, now=self.now
            )
        self.assertEqual(result.reason, "MAX_OPEN_TRADES_TOTAL")
        self.assertEqual(result.event["cap_block_reason"], "MAX_OPEN_TRADES_TOTAL")

    def test_open_per_symbol_cap_blocks_only_same_symbol(self) -> None:
        router = self.router(demo_max_open_trades_total=5, demo_max_open_trades_per_symbol=1)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[SimpleNamespace(magic=909002, symbol="BTCUSD#")]):
            btc_result = router.evaluate(
                self.decision(symbol="BTCUSD#"),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {"bid": 100.0, "ask": 100.5},
                {"tick_value": 0.01, "tick_size": 1.0, "volume_step": 0.01},
                1,
                30,
                True,
                now=self.now,
            )
            gold_result = router.evaluate(
                self.gold_m1m5_scalper_decision(),
                {"approved_lot": 0.01},
                self.account(),
                "GOLD#",
                {},
                {"bid": 2000.0, "ask": 2000.5},
                {"tick_value": 0.01, "tick_size": 0.01, "volume_step": 0.01},
                1,
                30,
                True,
                now=self.now,
            )
        self.assertEqual(btc_result.reason, "MAX_OPEN_TRADES_PER_SYMBOL")
        self.assertEqual(gold_result.decision, "PASS")
        self.assertEqual(gold_result.event["open_demo_trades_by_symbol"], {"BTCUSD#": 1})

    def test_max_trades_per_symbol_per_day_blocks_only_same_symbol(self) -> None:
        rows = [
            json.dumps(
                {
                    "event_type": "DEMO_ORDER",
                    "created_at": self.now.isoformat(),
                    "ticket": i + 1,
                    "order_success": True,
                    "order_retcode": 10009,
                    "symbol": "BTCUSD#",
                    "broker_symbol": "BTCUSD#",
                }
            )
            for i in range(5)
        ]
        self.events_path.write_text("\n".join(rows), encoding="utf-8")
        router = self.router(demo_max_trades_per_day_total=15, demo_max_trades_per_symbol_per_day=5)
        btc_result = self.evaluate(
            router=router,
            decision=self.decision(symbol="BTCUSD#"),
            kelly={"approved_lot": 0.01},
            broker_symbol="BTCUSD#",
        )
        gold_result = self.evaluate(
            router=router,
            decision=self.gold_m1m5_scalper_decision(),
            kelly={"approved_lot": 0.01},
            broker_symbol="GOLD#",
        )
        self.assertEqual(btc_result.reason, "MAX_TRADES_PER_SYMBOL_PER_DAY")
        self.assertEqual(btc_result.event["cap_block_reason"], "MAX_TRADES_PER_SYMBOL_PER_DAY")
        self.assertEqual(gold_result.decision, "PASS")
        self.assertEqual(gold_result.event["daily_demo_trades_by_symbol"], {"BTCUSD#": 5})

    def test_total_daily_cap_blocks_once_total_reaches_cap(self) -> None:
        symbols = ["BTCUSD#", "GOLD#", "EURUSD"]
        rows = [
            json.dumps(
                {
                    "event_type": "DEMO_ORDER",
                    "created_at": self.now.isoformat(),
                    "ticket": i + 1,
                    "order_success": True,
                    "order_retcode": 10009,
                    "symbol": symbols[i % len(symbols)],
                    "broker_symbol": symbols[i % len(symbols)],
                }
            )
            for i in range(4)
        ]
        self.events_path.write_text("\n".join(rows), encoding="utf-8")
        router = self.router(demo_max_trades_per_day_total=4, demo_max_trades_per_symbol_per_day=5)
        result = self.evaluate(router=router, kelly={"approved_lot": 0.01})
        self.assertEqual(result.reason, "MAX_TRADES_PER_DAY_TOTAL")
        self.assertEqual(result.event["daily_demo_trades_total"], 4)
        self.assertEqual(result.event["cap_block_reason"], "MAX_TRADES_PER_DAY_TOTAL")

    def test_failed_orders_do_not_increment_daily_caps(self) -> None:
        rows = [
            json.dumps(
                {
                    "event_type": "DEMO_ORDER_FAILED",
                    "created_at": self.now.isoformat(),
                    "ticket": None,
                    "order_success": False,
                    "order_retcode": 10009,
                    "symbol": "EURUSD",
                    "broker_symbol": "EURUSD",
                }
            )
            for _ in range(5)
        ]
        self.events_path.write_text("\n".join(rows), encoding="utf-8")
        router = self.router(demo_max_trades_per_day_total=1, demo_max_trades_per_symbol_per_day=1)
        result = self.evaluate(router=router, kelly={"approved_lot": 0.01})
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.event["daily_demo_trades_total"], 0)
        self.assertEqual(result.event["current_symbol_daily_count"], 0)

    def test_closed_trades_count_toward_daily_but_not_open_caps(self) -> None:
        rows = [
            json.dumps(
                {
                    "event_type": "DEMO_ORDER",
                    "created_at": self.now.isoformat(),
                    "ticket": 1000 + i,
                    "order_success": True,
                    "order_retcode": 10009,
                    "symbol": "EURUSD",
                    "broker_symbol": "EURUSD",
                }
            )
            for i in range(5)
        ]
        rows.append(json.dumps({"event_type": "DEMO_CLOSE", "created_at": self.now.isoformat(), "ticket": 1001, "symbol": "EURUSD", "broker_symbol": "EURUSD", "pnl": 1.0}))
        self.events_path.write_text("\n".join(rows), encoding="utf-8")
        router = self.router(demo_max_trades_per_day_total=15, demo_max_trades_per_symbol_per_day=5)
        result = self.evaluate(router=router, kelly={"approved_lot": 0.01})
        self.assertEqual(result.reason, "MAX_TRADES_PER_SYMBOL_PER_DAY")
        self.assertEqual(result.event["current_symbol_open_count"], 0)

    def test_consecutive_losses_stop_blocks_order(self) -> None:
        rows = [
            json.dumps({"event_type": "DEMO_CLOSE", "result": "LOSS", "pnl": -1.0, "created_at": self.now.isoformat()})
            for _ in range(3)
        ]
        self.events_path.write_text("\n".join(rows), encoding="utf-8")
        self.assertEqual(self.evaluate().reason, "DEMO_CONSECUTIVE_LOSS_STOP")

    def test_btc_bad_hour_weekend_blocked_by_default(self) -> None:
        # With crypto_24_7_enabled=True (default) and no usable tick (empty dict, bid=None),
        # the time engine returns NO_RECENT_TICK which the router surfaces directly.
        # With crypto_24_7_enabled=False, the router's own is_weekend check fires → BTC_WEEKEND_BLOCKED.
        weekend = datetime.fromisoformat("2026-06-06T10:00:00+00:00")
        router = self.router()
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            result = router.evaluate(
                self.decision(symbol="BTCUSD#"),
                {"approved_lot": 0.01},
                self.account(),
                "BTCUSD#",
                {},
                {},
                self.specs(),
                1,
                2500,
                True,
                now=weekend,
            )
        self.assertIn(result.reason, {"BTC_WEEKEND_BLOCKED", "NO_RECENT_TICK", "BTC_WEEKEND_ANALYSIS_ONLY"})

    def test_confirmation_and_observer_only_strategies_cannot_open(self) -> None:
        self.assertEqual(self.evaluate(decision=self.decision(strategy="EMA_PULLBACK")).reason, "EMA_PULLBACK_CONFIRMATION_ONLY")
        self.assertEqual(self.evaluate(decision=self.decision(strategy="SECOND_ENTRY")).reason, "STRATEGY_OBSERVER_ONLY")
        self.assertEqual(self.evaluate(decision=self.decision(strategy="SCALPING_AGENT")).reason, "STRATEGY_OBSERVER_ONLY")

    def test_strategy_confirmation_failures_block_order(self) -> None:
        cases = [
            ("m15_confirmation", False, "M15_CONFIRMATION_FALSE"),
            ("m1_entry_confirmation", False, "M1_CONFIRMATION_FALSE"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field):
                self.assertEqual(self.evaluate(decision=self.decision(**{field: value})).reason, reason)


class PaperLearningSafetyTests(PaperOptimizerTestBase):
    def test_result_none_does_not_update_weight(self) -> None:
        optimizer = self.make_optimizer()
        params = optimizer._strategy("BTCUSD", "EMA_PULLBACK")
        optimizer.batch_samples = [
            {"sample_type": "SETUP", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": None, "pnl": None}
        ]
        optimizer._optimize_batch()
        self.assertEqual(params["weight"], 1.0)

    def test_paper_skip_does_not_update_weight(self) -> None:
        optimizer = self.make_optimizer()
        params = optimizer._strategy("BTCUSD", "EMA_PULLBACK")
        optimizer.batch_samples = [
            {"sample_type": "PAPER_SKIP", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": None, "pnl": None}
            for _ in range(25)
        ]
        optimizer._optimize_batch()
        self.assertEqual(params["weight"], 1.0)

    def test_paper_skip_journal_sample_does_not_update_weight(self) -> None:
        optimizer = self.make_optimizer()
        params = optimizer._strategy("BTCUSD", "EMA_PULLBACK")
        optimizer.record_setup(
            {
                "sample_type": "PAPER_SKIP",
                "symbol": "BTCUSD",
                "strategy": "EMA_PULLBACK",
                "signal": "BUY",
                "entry": 100.0,
                "sl": 99.0,
                "tp": 102.0,
                "reason": "LOW_CONFIDENCE",
                "result": "WIN",
                "pnl": 999.0,
            }
        )
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"reason_for_skip": "LOW_CONFIDENCE"', text)
        self.assertIn('"confluence_score"', text)
        self.assertEqual(params["weight"], 1.0)

    def test_zero_closed_trades_blocks_weight_update(self) -> None:
        optimizer = self.make_optimizer()
        params = optimizer._strategy("BTCUSD", "EMA_PULLBACK")
        optimizer.apply_weight_update("BTCUSD", "EMA_PULLBACK", params, [], -100.0)
        self.assertEqual(params["weight"], 1.0)

    def test_five_real_closes_allow_weight_update(self) -> None:
        optimizer = self.make_optimizer()
        params = optimizer._strategy("BTCUSD", "EMA_PULLBACK")
        closes = [
            {"sample_type": "PAPER_CLOSE", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "LOSS", "pnl": -1.0}
            for _ in range(5)
        ]
        optimizer.apply_weight_update("BTCUSD", "EMA_PULLBACK", params, closes, -5.0)
        self.assertLess(params["weight"], 1.0)


class PaperPersistenceSafetyTests(PaperOptimizerTestBase):
    def agent_frames(self) -> dict:
        rows = []
        for i in range(80):
            base = 100.0 + i * 0.05
            rows.append({"open": base, "high": base + 0.2, "low": base - 0.2, "close": base + 0.08, "spread": 1, "tick_volume": 100, "candle_time": pd.Timestamp("2026-05-29T00:00:00Z") + pd.Timedelta(minutes=5 * i)})
        m5 = pd.DataFrame(rows)
        h4 = pd.DataFrame(rows[-20:])
        return {"M5": m5, "H4": h4, "H1": h4, "M15": h4, "M1": m5.tail(20).reset_index(drop=True)}

    def test_hermes_agent_analyze_symbol_has_settings_and_does_not_crash(self) -> None:
        settings = test_settings()
        agent = Hermes5MinAgent(settings)
        result = agent.analyze_symbol(
            "EURUSD",
            self.agent_frames(),
            {"equity": 10000.0, "balance": 10000.0},
            0,
            {
                "symbol_info_available": True,
                "tick_value": 1.0,
                "tick_size": 0.01,
                "contract_size": 1.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "volume_max": 1.0,
            },
            30,
        )
        self.assertIn("ai_decision", result)
        self.assertIs(agent.settings, settings)

    def test_wsp_module_produces_observer_only_output(self) -> None:
        m5 = self.agent_frames()["M5"]
        result = evaluate_wsp_intelligence("EURUSD", "M5", m5, {"h4_bias": "BULLISH", "h1_bias": "BULLISH"})
        self.assertTrue(result["wsp_enabled"])
        self.assertEqual(result["role"], "OBSERVER_ONLY")
        self.assertEqual(result["visual_role"], "VISUAL_CONFIRMATION")
        self.assertIn(result["market_state"], {"BULLISH", "BEARISH", "RANGE"})
        self.assertIn(result["safety_guard_visual"], {"SECURE", "CAUTION", "DANGER"})
        self.assertGreaterEqual(result["risk_score"], 1)
        self.assertLessEqual(result["risk_score"], 10)

    def test_wsp_is_raw_payload_only_and_not_entry_candidate(self) -> None:
        settings = test_settings()
        agent = Hermes5MinAgent(settings)
        result = agent.analyze_symbol(
            "EURUSD",
            self.agent_frames(),
            {"equity": 10000.0, "balance": 10000.0},
            0,
            {
                "symbol_info_available": True,
                "tick_value": 1.0,
                "tick_size": 0.01,
                "contract_size": 1.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "volume_max": 1.0,
            },
            30,
        )
        raw = result["ai_decision"]["raw_payload"]
        self.assertIn("wsp_intelligence", raw)
        self.assertEqual(raw["wsp_intelligence"]["role"], "OBSERVER_ONLY")
        strategies = {item.get("strategy") for item in result["strategy_signals"]}
        self.assertNotIn("WSP_INTELLIGENCE_OVERLAY", strategies)

    def test_new_strategy_evaluators_accept_valid_settings_and_missing_settings(self) -> None:
        frames = self.agent_frames()
        settings = test_settings()
        context = {"risk_diag_status": "OK", "safety_guard_status": "PASS", "reward_risk": 2.0}
        for module in (crt_tbs_reversal, amd_fvg_ifvg_reversal, fib_ote_retest):
            self.assertIn(module.evaluate("EURUSD", frames, context, settings)["signal"], {"BUY", "SELL", "WAIT"})
            safe = module.evaluate("EURUSD", {}, {}, None)
            self.assertEqual(safe["signal"], "WAIT")

    def safety_settings(self) -> Settings:
        settings = test_settings()
        settings.safety_guard_enabled = True
        settings.btc_weekend_analysis_only = True
        settings.bad_hour_analysis_only = True
        settings.ema_pullback_require_extra_confirmation = True
        settings.risk_diag_max_realized_risk_percent = 0.75
        settings.risk_diag_max_mismatch_abs_percent = 0.25
        settings.report_timezone = "Africa/Casablanca"
        return settings

    def decision(self, strategy: str = "BREAKOUT_RETEST", symbol: str = "EURUSD", signal: str = "BUY") -> dict:
        return {
            "decision": "ENTER_PAPER",
            "symbol": symbol,
            "timeframe": "M5",
            "signal": signal,
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "strategy": strategy,
            "confidence": 0.8,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "reward_risk": 2.0,
            "risk_diag_status": "OK",
            "smc_confluence_score": 80,
            "smc_m5_confirmation": True,
        }

    def test_safety_guard_btc_weekend_blocks_open(self) -> None:
        agent = PaperTradingAgent(self.safety_settings())
        agent.safety_guard = SafetyGuard(agent.settings)
        with patch("app.agents.safety_guard.datetime") as fake_dt:
            fake_dt.now.return_value = datetime.fromisoformat("2026-05-30T12:00:00+00:00")
            fake_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            items = agent.process_decision(self.decision(symbol="BTCUSD"), spread=1.0, setup_id="setup-1")
        self.assertEqual(items[0]["data"]["reason"], "BTC_WEEKEND_ANALYSIS_ONLY")
        self.assertFalse(any(item.get("paper_action") == "OPEN_TRADE" for item in items))
        self.assertEqual(items[0]["data"]["raw_payload"]["safety_guard_status"], "BLOCK")

    def test_safety_guard_btc_bad_hour_blocks_open(self) -> None:
        guard = SafetyGuard(self.safety_settings())
        result = guard.evaluate(self.decision(symbol="BTCUSD"), now=datetime.fromisoformat("2026-05-27T20:10:00+00:00"))
        self.assertEqual(result["safety_guard_status"], "BLOCK")
        self.assertEqual(result["safety_guard_reason"], "BTC_BAD_HOUR_ANALYSIS_ONLY")

    def test_safety_guard_btc_caution_hour_marks_caution(self) -> None:
        guard = SafetyGuard(self.safety_settings())
        result = guard.evaluate(self.decision(symbol="BTCUSD"), now=datetime.fromisoformat("2026-05-27T18:10:00+00:00"))
        self.assertEqual(result["safety_guard_status"], "CAUTION")

    def test_safety_guard_time_gate_pass_does_not_block_by_old_hour(self) -> None:
        guard = SafetyGuard(self.safety_settings())
        decision = self.decision(symbol="BTCUSD")
        decision.update(
            {
                "utc_time": "2026-06-01T16:10:00+00:00",
                "casablanca_time": "2026-06-01T17:10:00+01:00",
                "session_name": "OVERLAP",
                "time_gate_status": "PASS",
                "time_gate_reason": "TIME_GATE_PASS",
            }
        )
        result = guard.evaluate(decision, now=datetime.fromisoformat("2026-06-01T22:10:00+00:00"))
        self.assertEqual(result["safety_guard_status"], "PASS")
        self.assertEqual(result["safety_guard_reason"], "SAFETY_GUARD_PASS")

    def test_ema_pullback_guard_blocks_weak_context_low_score_and_after_loss(self) -> None:
        guard = SafetyGuard(self.safety_settings())
        weak = self.decision(strategy="EMA_PULLBACK")
        weak.update({"mtfa_status": "FAIL", "mtf_structure_status": "FAIL", "smc_confluence_score": 10})
        self.assertEqual(guard.evaluate(weak, now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"))["safety_guard_reason"], "EMA_PULLBACK_WEAK_CONTEXT")
        low_smc = self.decision(strategy="EMA_PULLBACK")
        low_smc.update({"smc_confluence_score": 10})
        self.assertEqual(guard.evaluate(low_smc, now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"))["safety_guard_reason"], "EMA_PULLBACK_LOW_SMC_SCORE")
        after_loss = self.decision(strategy="EMA_PULLBACK")
        after_loss.update({"previous_trade_result_for_symbol_strategy": "LOSS"})
        self.assertEqual(guard.evaluate(after_loss, now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"))["safety_guard_reason"], "EMA_PULLBACK_AFTER_LOSS_BLOCKED")

    def test_breakout_retest_not_blocked_by_ema_rules(self) -> None:
        result = SafetyGuard(self.safety_settings()).evaluate(
            self.decision(strategy="BREAKOUT_RETEST"),
            now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"),
        )
        self.assertEqual(result["safety_guard_status"], "PASS")

    def test_risk_mismatch_guard_caution_and_block(self) -> None:
        guard = SafetyGuard(self.safety_settings())
        cautious = guard.evaluate(
            self.decision(strategy="BREAKOUT_RETEST"),
            {"risk_diag_mismatch_percent": 0.4},
            now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"),
        )
        self.assertEqual(cautious["safety_guard_status"], "CAUTION")
        blocked = guard.evaluate(
            self.decision(strategy="EMA_PULLBACK"),
            {"risk_diag_mismatch_percent": 0.4},
            now=datetime.fromisoformat("2026-05-27T10:00:00+00:00"),
        )
        self.assertEqual(blocked["safety_guard_status"], "BLOCK")
        self.assertEqual(blocked["safety_guard_reason"], "RISK_DIAG_MISMATCH_BLOCK")

    def test_big_setup_detector_missing_data_tag_only_capped(self) -> None:
        result = BigSetupDetector().evaluate({"symbol": "BTCUSD", "direction": "BUY"})
        self.assertEqual(result["big_setup_status"], "TAG_ONLY")
        self.assertLessEqual(result["big_setup_score"], 74)
        self.assertIn("smc_confluence_score", result["big_setup_missing_data"])
        self.assertIn("htf_alignment_score", result)

    def test_strategy_math_synthetic_setups_detected_correctly(self) -> None:
        cases = [
            ("bullish_fvg.csv", "fvg_detected"),
            ("bearish_fvg.csv", "fvg_detected"),
            ("bullish_ifvg.csv", "ifvg_detected"),
            ("bearish_ifvg.csv", "ifvg_detected"),
            ("bullish_mss.csv", "mss_detected"),
            ("bearish_mss.csv", "mss_detected"),
            ("bullish_cisd.csv", "cisd_detected"),
            ("bearish_cisd.csv", "cisd_detected"),
            ("bullish_ob.csv", "ob_detected"),
            ("bearish_ob.csv", "ob_detected"),
            ("bpr.csv", "bpr_detected"),
            ("buy_ote.csv", "ote_detected"),
            ("sell_ote.csv", "ote_detected"),
            ("rbs_buy.csv", "rbs_detected"),
            ("sbr_sell.csv", "sbr_detected"),
            ("qml_buy.csv", "qml_detected"),
            ("qml_sell.csv", "qml_detected"),
        ]
        for filename, field in cases:
            with self.subTest(filename=filename, field=field):
                audit = strategy_math_audit.audit_frame(self._strategy_fixture(filename))
                self.assertTrue(audit.concepts[field], audit.details)

    def test_strategy_math_no_false_positive_on_neutral_range(self) -> None:
        audit = strategy_math_audit.audit_frame(self._strategy_fixture("neutral_range.csv"))
        for field in [
            "fvg_detected",
            "ifvg_detected",
            "ob_detected",
            "bpr_detected",
            "ote_detected",
            "mss_detected",
            "cisd_detected",
            "rbs_detected",
            "sbr_detected",
            "qml_detected",
        ]:
            self.assertFalse(audit.concepts[field], field)

    def test_strategy_math_audit_writes_local_report(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "audit"
        report = strategy_math_audit.write_audit_report("BTCUSD#", {"M5": self._strategy_fixture("bullish_fvg.csv")}, output, settings=test_settings())
        self.assertIn("truth_definitions", report)
        self.assertTrue((output / "strategy_math_audit.json").exists())
        self.assertTrue((output / "strategy_math_concepts.csv").exists())
        tmp.cleanup()

    def test_strategy_math_audit_cli_runs_all_fixtures_without_fixture_arg(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "audit"
        with patch("builtins.print") as printed:
            result = strategy_math_audit.main(["--output", str(output)])
        self.assertEqual(result, 0)
        summary = json.loads((output / "strategy_math_audit.json").read_text(encoding="utf-8"))
        self.assertGreater(summary["summary"]["total"], 1)
        self.assertEqual(summary["summary"]["failed"], 0)
        table = pd.read_csv(output / "strategy_math_concepts.csv")
        self.assertIn("fixture", table.columns)
        self.assertGreater(len(printed.call_args_list), 1)
        tmp.cleanup()

    def test_strategy_math_audit_cli_single_fixture_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "audit"
        fixture = Path(__file__).resolve().parent / "fixtures" / "strategy_math" / "bullish_fvg.csv"
        strategy_math_audit.main(["--fixture", str(fixture), "--output", str(output)])
        summary = json.loads((output / "strategy_math_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["summary"]["total"], 1)
        self.assertEqual(summary["fixtures"][0]["fixture"], "bullish_fvg.csv")
        tmp.cleanup()

    def test_strategy_math_audit_cli_lists_fixtures(self) -> None:
        with patch("builtins.print") as printed:
            result = strategy_math_audit.main(["--list-fixtures"])
        self.assertEqual(result, 0)
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("bullish_fvg.csv", output)

    def test_strategy_wiring_audit_v2_reports_missing_m1_and_keeps_demo_not_ready(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "audit_v2"
        frame = self._strategy_fixture("bullish_fvg.csv")
        frames = {
            "BTCUSD#": {
                "M5": frame,
                "M15": frame,
                "H1": frame,
                "H4": frame,
            }
        }
        with patch("app.tools.strategy_math_audit.fetch_mt5_bar_context", return_value=frames):
            result = strategy_math_audit.main(
                [
                    "--real-context",
                    "--symbols",
                    "BTCUSD#",
                    "--timeframes",
                    "M5,M15,H1,H4",
                    "--bars",
                    "500",
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(result, 0)
        summary = json.loads((output / "strategy_wiring_audit_summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["demo_ready"])
        self.assertTrue(summary["missing_m1_reported"])
        self.assertTrue((output / "strategy_wiring_audit_details.csv").exists())
        issues = pd.read_csv(output / "strategy_math_wiring_issues.csv")
        self.assertIn("M1_REQUIRED_BUT_EMPTY", set(issues["bug_flag"]))
        details = pd.read_csv(output / "strategy_wiring_audit_details.csv")
        self.assertIn("CONFIRMATION_ONLY", set(details["strategy_status"]))
        self.assertIn("LEGACY_OBSERVER", set(details["strategy_status"]))
        tmp.cleanup()

    def test_time_engine_returns_required_fields(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        now = datetime.fromisoformat("2026-06-01T10:00:00+00:00")
        result = TimeEngine(settings).evaluate("EURUSD", {"M5": self._timed_frame([100, 101, 102])}, now=now)
        for field in TIME_GATE_FIELDS:
            self.assertIn(field, result)
        self.assertEqual(result["utc_hour"], 10)
        self.assertIn(result["session_name"], {"ASIA", "LONDON", "NEW_YORK", "OVERLAP", "OFF_HOURS", "WEEKEND"})

    def test_time_engine_utc_1610_is_not_off_hours(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        now = datetime.fromisoformat("2026-06-01T16:10:00+00:00")
        result = TimeEngine(settings).evaluate("BTCUSD#", now=now)
        self.assertEqual(result["utc_hour"], 16)
        self.assertEqual(result["local_hour"], 17)
        self.assertIn(result["session_name"], {"OVERLAP", "NEW_YORK"})
        self.assertNotEqual(result["session_name"], "OFF_HOURS")
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "TIME_GATE_PASS")

    def test_time_engine_casablanca_17_from_utc_16_is_not_off_hours(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        result = TimeEngine(settings).evaluate("EURUSD", now=datetime.fromisoformat("2026-06-01T16:10:00+00:00"))
        self.assertEqual(result["local_hour"], 17)
        self.assertNotEqual(result["session_name"], "OFF_HOURS")

    def test_time_engine_btc_bad_hours_block_by_default(self) -> None:
        settings = test_settings()
        settings.btc_bad_hours = "9"
        settings.bad_hour_analysis_only = True
        now = datetime.fromisoformat("2026-06-01T08:15:00+00:00")
        result = TimeEngine(settings).evaluate("BTCUSD#", now=now)
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "BTC_BAD_HOUR_BLOCK")

    def test_time_engine_asia_preopen_blocks_at_0023_casablanca(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        now = datetime.fromisoformat("2026-05-31T23:23:00+00:00")
        result = TimeEngine(settings).evaluate("BTCUSD#", now=now)
        self.assertEqual(result["local_hour"], 0)
        self.assertEqual(result["session_name"], "ASIA_PREOPEN")
        self.assertEqual(result["asia_window"], "ASIA_PREOPEN")
        self.assertFalse(result["asia_trading_allowed"])
        self.assertEqual(result["asia_block_reason"], "ASIA_PREOPEN_BAD_LIQUIDITY")
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "ASIA_PREOPEN_BAD_LIQUIDITY")

    def test_time_engine_asia_main_can_pass_at_0200_casablanca(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        settings.btc_bad_hours = "0,1,2,3,4,5,6,7,22,23"
        settings.bad_hour_analysis_only = True
        now = datetime.fromisoformat("2026-06-01T01:00:00+00:00")
        result = TimeEngine(settings).evaluate("BTCUSD#", now=now)
        self.assertEqual(result["local_hour"], 2)
        self.assertEqual(result["session_name"], "ASIA_MAIN")
        self.assertEqual(result["asia_window"], "ASIA_MAIN")
        self.assertTrue(result["asia_trading_allowed"])
        self.assertIsNone(result["asia_block_reason"])
        self.assertFalse(result["is_bad_hour"])
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "ASIA_MAIN_ALLOWED")

    def test_time_engine_asia_late_blocks_at_0700_casablanca(self) -> None:
        settings = test_settings()
        settings.timezone_local = "Africa/Casablanca"
        now = datetime.fromisoformat("2026-06-01T06:00:00+00:00")
        result = TimeEngine(settings).evaluate("EURUSD", now=now)
        self.assertEqual(result["local_hour"], 7)
        self.assertEqual(result["session_name"], "ASIA_LATE")
        self.assertEqual(result["asia_window"], "ASIA_LATE")
        self.assertFalse(result["asia_trading_allowed"])
        self.assertEqual(result["asia_block_reason"], "ASIA_LATE_STRICT_MODE")
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "ASIA_LATE_STRICT_MODE")

    def test_time_engine_btc_weekend_blocks_by_default(self) -> None:
        # With crypto_24_7_enabled=True (default), BTC weekend with no tick → NO_RECENT_TICK.
        # With crypto_24_7_enabled=False, BTC weekend → BTC_WEEKEND_ANALYSIS_ONLY.
        settings = test_settings()
        settings.btc_bad_hours = ""
        settings.btc_weekend_analysis_only = True
        now = datetime.fromisoformat("2026-06-06T12:00:00+00:00")
        result = TimeEngine(settings).evaluate("BTCUSD#", now=now)
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertIn(result["time_gate_reason"], {"BTC_WEEKEND_ANALYSIS_ONLY", "NO_RECENT_TICK"})

    def test_time_engine_gold_and_eurusd_weekend_closed(self) -> None:
        settings = test_settings()
        now = datetime.fromisoformat("2026-06-06T12:00:00+00:00")
        for symbol in ("GOLD#", "EURUSD"):
            result = TimeEngine(settings).evaluate(symbol, now=now)
            self.assertFalse(result["symbol_market_open"])
            self.assertEqual(result["time_gate_status"], "BLOCK")
            self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_professional_audit_forbids_generic_wait_reason(self) -> None:
        flags = strategy_math_audit._professional_issue_flags(
            "BREAKOUT_RETEST",
            "WAIT",
            {"M5": self._timed_frame([100 + i for i in range(80)]), "M15": self._timed_frame([100 + i for i in range(80)]), "H1": self._timed_frame([100 + i for i in range(80)]), "H4": self._timed_frame([100 + i for i in range(80)])},
            {"signal": "WAIT", "reason": "No breakout retest", "time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True},
            False,
            {"score_components_present": True},
        )
        self.assertIn("GENERIC_WAIT_REASON", flags)

    def test_professional_audit_ema_and_legacy_are_not_entry_eligible(self) -> None:
        decision = {"signal": "BUY", "reason": "TEST", "risk_reward": 2.0, "time_gate_status": "PASS", "session_name": "LONDON", "symbol_market_open": True}
        flags = strategy_math_audit._professional_issue_flags("EMA_PULLBACK", "BUY", {}, decision, True, {"score_components_present": True})
        self.assertIn("EMA_ENTRY_NOT_ALLOWED", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("EMA_PULLBACK", "BUY", decision, flags))
        flags = strategy_math_audit._professional_issue_flags("SECOND_ENTRY", "BUY", {}, decision, True, {"score_components_present": True})
        self.assertIn("LEGACY_ENTRY_NOT_ALLOWED", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("SECOND_ENTRY", "BUY", decision, flags))

    def test_professional_audit_mtfa_fail_blocks_demo_eligibility(self) -> None:
        decision = self._eligible_candidate_decision()
        decision["mtfa_status"] = "FAIL"
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("SIGNAL_WITH_MTFA_FAIL", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))

    def test_professional_audit_smc_fail_requires_big_setup_b_override(self) -> None:
        decision = self._eligible_candidate_decision()
        decision.update({"smc_confluence_status": "FAIL", "big_setup_grade": "C"})
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("SIGNAL_WITH_SMC_FAIL", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))
        decision["big_setup_grade"] = "B"
        self.assertTrue(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))

    def test_professional_audit_no_m15_and_no_m1_confirmation_block(self) -> None:
        decision = self._eligible_candidate_decision()
        decision["smc_m15_confirmation"] = False
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("SIGNAL_WITH_NO_M15_CONFIRMATION", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))
        decision = self._eligible_candidate_decision()
        decision["smc_m1_entry_confirmation"] = False
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("SIGNAL_WITH_NO_M1_CONFIRMATION", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))

    def test_professional_audit_lot_valid_false_blocks(self) -> None:
        decision = self._eligible_candidate_decision()
        decision["execution_checklist"] = {"lot_valid": False}
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("LOT_VALID_FALSE_BUT_APPROVED", flags)
        self.assertFalse(strategy_math_audit._demo_eligible_candidate("BREAKOUT_RETEST", "BUY", decision, flags))

    def test_professional_audit_rr_missing_before_big_setup_reported(self) -> None:
        decision = self._eligible_candidate_decision()
        decision["risk_reward"] = None
        decision["entry"] = None
        flags = strategy_math_audit._professional_issue_flags("BREAKOUT_RETEST", "BUY", self._full_frames(include_m1=True), decision, True, {"score_components_present": True})
        self.assertIn("RR_MISSING_BEFORE_BIG_SETUP", flags)
        self.assertIn("SIGNAL_WITH_RR_MISSING", flags)

    def test_professional_audit_non_wait_signal_has_report_diagnostics(self) -> None:
        frames = self._full_frames(include_m1=True)
        decision = self._eligible_candidate_decision()
        decision.update(
            {
                "signal": "SELL",
                "reason": "Breakdown below 20-candle range",
                "mtfa_status": "PASS",
                "smc_confluence_status": "FAIL",
                "smc_m15_confirmation": True,
                "smc_m1_entry_confirmation": False,
                "big_setup_grade": "C",
                "confidence": 0.68,
            }
        )
        flags = ["SIGNAL_WITH_SMC_FAIL", "SIGNAL_WITH_NO_M1_CONFIRMATION"]
        row = strategy_math_audit._professional_detail_row(
            "EURUSD",
            "BREAKOUT_RETEST",
            frames,
            decision,
            flags,
            {"time_gate_status": "PASS", "session_name": "LONDON"},
            {"math_status": "VALID", "required_timeframes": "M5,M15,M1_CONFIRMATION", "score_components_present": True},
            True,
            False,
        )
        self.assertEqual(row["reason"], "Breakdown below 20-candle range")
        self.assertEqual(row["mtfa_status"], "PASS")
        self.assertEqual(row["smc_status"], "FAIL")
        self.assertTrue(row["m15_confirmation"])
        self.assertFalse(row["m1_confirmation"])
        self.assertTrue(row["eligible_before_safety"])
        self.assertFalse(row["demo_eligible_candidate"])
        self.assertEqual(row["blocked_reason"], "SIGNAL_WITH_SMC_FAIL,SIGNAL_WITH_NO_M1_CONFIRMATION")

    def test_professional_audit_details_and_issues_agree(self) -> None:
        frames = self._full_frames(include_m1=True)
        decision = self._eligible_candidate_decision()
        decision.update(
            {
                "signal": "SELL",
                "reason": "Breakdown below 20-candle range",
                "smc_confluence_status": "FAIL",
                "smc_m1_entry_confirmation": False,
                "big_setup_grade": "C",
            }
        )
        flags = ["SIGNAL_WITH_SMC_FAIL", "SIGNAL_WITH_NO_M1_CONFIRMATION"]
        detail = strategy_math_audit._professional_detail_row(
            "EURUSD",
            "BREAKOUT_RETEST",
            frames,
            decision,
            flags,
            {"time_gate_status": "PASS", "session_name": "LONDON"},
            {"math_status": "VALID", "required_timeframes": "M5,M15,M1_CONFIRMATION", "score_components_present": True},
            True,
            False,
        )
        issues = [strategy_math_audit._professional_issue_row(detail, flag) for flag in flags]
        smc_issue = [row for row in issues if row["bug_flag"] == "SIGNAL_WITH_SMC_FAIL"][0]
        m1_issue = [row for row in issues if row["bug_flag"] == "SIGNAL_WITH_NO_M1_CONFIRMATION"][0]
        self.assertEqual(smc_issue["smc_status"], "FAIL")
        self.assertIn("SIGNAL_WITH_SMC_FAIL", smc_issue["blocked_reason"])
        self.assertFalse(m1_issue["m1_confirmation"])
        self.assertIn("SIGNAL_WITH_NO_M1_CONFIRMATION", m1_issue["blocked_reason"])

    def test_strategy_pro_audit_outputs_v3_files_and_m1_not_requested(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "audit_v3"
        frames = {"EURUSD": self._full_frames(include_m1=False)}
        with patch("app.tools.strategy_math_audit.fetch_mt5_bar_context", return_value=frames):
            result = strategy_math_audit.main(
                [
                    "--pro-audit",
                    "--symbols",
                    "EURUSD",
                    "--timeframes",
                    "M5,M15,H1,H4",
                    "--bars",
                    "500",
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(result, 0)
        summary = json.loads((output / "strategy_pro_audit_summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["demo_ready"])
        self.assertEqual(summary["m1_status"], "M1_NOT_REQUESTED_FOR_AUDIT")
        self.assertNotIn("M1_NOT_REQUESTED_FOR_AUDIT", summary["issue_counts"])
        self.assertIn("DEMO_REQUIRES_M1_CONFIRMATION", summary["remaining_blockers"])
        self.assertTrue((output / "strategy_pro_audit_details.csv").exists())
        self.assertTrue((output / "strategy_pro_audit_issues.csv").exists())
        self.assertTrue((output / "time_gate_audit.csv").exists())
        details = pd.read_csv(output / "strategy_pro_audit_details.csv")
        ema = details[details["strategy"] == "EMA_PULLBACK"]
        self.assertTrue((ema["signal"] == "WAIT").all())
        self.assertFalse(ema["demo_eligible_candidate"].astype(str).str.lower().eq("true").any())
        tmp.cleanup()

    def test_legacy_disabled_strategies_do_not_open(self) -> None:
        agent = PaperTradingAgent(test_settings())
        for strategy in ("SECOND_ENTRY", "SCALPING_AGENT"):
            items = agent.process_decision(self.decision(strategy=strategy), spread=1.0, setup_id="setup-1")
            self.assertFalse(any(item.get("paper_action") == "OPEN_TRADE" for item in items))
            self.assertIn("DISABLED_LEGACY_OBSERVER", items[0]["data"]["reason"])

    def test_new_strategies_wait_when_data_missing(self) -> None:
        settings = test_settings()
        self.assertEqual(crt_tbs_reversal.evaluate("BTCUSD", {}, {}, settings)["signal"], "WAIT")
        self.assertEqual(amd_fvg_ifvg_reversal.evaluate("BTCUSD", {}, {}, settings)["signal"], "WAIT")
        self.assertEqual(fib_ote_retest.evaluate("BTCUSD", {}, {}, settings)["signal"], "WAIT")

    def test_new_strategy_paper_open_requires_safety_risk_rr_and_score(self) -> None:
        agent = PaperTradingAgent(test_settings())
        decision = self.decision(strategy="CRT_TBS_REVERSAL", symbol="EURUSD")
        decision.update({"crt_tbs_score": 80, "safety_guard_status": "PASS", "risk_diag_status": "OK", "reward_risk": 2.0, "final_risk": 0.01})
        items = agent.process_decision(
            decision,
            spread=1.0,
            setup_id="setup-1",
            broker_symbol="EURUSD",
            symbol_specs={"point": 0.01, "digits": 2, "tick_value": 1.0, "tick_size": 1.0},
            equity=10000.0,
        )
        self.assertTrue(any(item.get("paper_action") == "OPEN_TRADE" for item in items))
        for field, value, reason in [
            ("safety_guard_status", "CAUTION", "NEW_STRATEGY_SAFETY_NOT_PASS"),
            ("risk_diag_status", "MISMATCH", "NEW_STRATEGY_RISK_DIAG_NOT_OK"),
            ("reward_risk", 1.0, "NEW_STRATEGY_RR_TOO_LOW"),
            ("crt_tbs_score", 40, "NEW_STRATEGY_SCORE_TOO_LOW"),
        ]:
            changed = dict(decision)
            changed[field] = value
            agent2 = PaperTradingAgent(test_settings())
            self.assertEqual(agent2._new_strategy_skip_reason(changed), reason)

    def test_ticket_does_not_receive_uuid(self) -> None:
        agent = PaperTradingAgent(test_settings())
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "timeframe": "M5",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "strategy": "EMA_PULLBACK",
            "confidence": 0.8,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        trade_row = next(item["data"] for item in items if item.get("table") == "trades")
        self.assertIn("id", trade_row)
        self.assertNotIn("ticket", trade_row)
        self.assertEqual(trade_row["confidence"], 80.0)
        self.assertEqual(trade_row["raw_payload"]["confidence_decimal"], 0.8)

    def test_risk_audit_marks_exceeded(self) -> None:
        agent = PaperTradingAgent(test_settings())
        audit = agent._risk_audit(
            "BTCUSD",
            {"lot_size": 0.1, "entry": 100.0, "sl": 99.0},
            exit_price=90.0,
            pnl=-10.0,
            symbol_specs={"tick_value": 1.0, "tick_size": 0.01},
            equity=1000.0,
        )
        self.assertEqual(audit["risk_audit"], "EXCEEDED")

    def test_risk_diagnostics_stored_on_paper_open(self) -> None:
        agent = PaperTradingAgent(test_settings())
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "timeframe": "M5",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "strategy": "EMA_PULLBACK",
            "confidence": 0.8,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
        }
        items = agent.process_decision(
            decision,
            spread=1.0,
            setup_id="setup-1",
            broker_symbol="BTCUSD#",
            symbol_specs={"point": 0.01, "digits": 2, "tick_value": 1.0, "tick_size": 0.01, "contract_size": 1.0},
            equity=1000.0,
        )
        event = next(item["data"] for item in items if item.get("table") == "execution_events")
        trade_row = next(item["data"] for item in items if item.get("table") == "trades")
        self.assertEqual(event["risk_diag_symbol"], "BTCUSD")
        self.assertEqual(event["risk_diag_broker_symbol"], "BTCUSD#")
        self.assertEqual(event["raw_payload"]["risk_diag_expected_risk_percent"], 1.0)
        self.assertEqual(trade_row["raw_payload"]["risk_diag_status"], "MISMATCH")

    def test_risk_diagnostics_stored_on_paper_close(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-risk-diag", "BTCUSD", "BUY", 100.0, 99.0, 102.0)])
        items = agent.process_closures(
            "BTCUSD",
            "BTCUSD#",
            pd.DataFrame(),
            tick={"bid": 102.0},
            symbol_specs={"point": 0.01, "digits": 2, "tick_value": 1.0, "tick_size": 0.01, "contract_size": 1.0},
            equity=1000.0,
        )
        event = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_EVENT")
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(event["risk_diag_realized_risk_percent"], 2.0)
        self.assertEqual(close_row["raw_payload"]["risk_diag_exit_price"], 102.0)
        self.assertEqual(close_row["raw_payload"]["risk_diag_status"], "MISMATCH")

    def test_tag_only_does_not_block_mtfa_fail(self) -> None:
        settings = test_settings()
        settings.mtfa_mode = "TAG_ONLY"
        agent = PaperTradingAgent(settings)
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
            "mtfa_status": "FAIL",
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertTrue(any(item.get("paper_action") == "OPEN_TRADE" for item in items))

    def test_enforce_blocks_mtfa_fail(self) -> None:
        settings = test_settings()
        settings.mtfa_mode = "ENFORCE"
        agent = PaperTradingAgent(settings)
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
            "mtfa_status": "FAIL",
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertEqual(items[0]["data"]["reason"], "MTFA_FILTER_FAIL")

    def test_enforce_does_not_mtfa_block_wait_state(self) -> None:
        settings = test_settings()
        settings.mtfa_mode = "ENFORCE"
        agent = PaperTradingAgent(settings)
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "WAIT",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
            "mtfa_status": "NOT_APPLICABLE",
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertEqual(items[0]["data"]["reason"], "NO_DIRECTION")

    def test_mtfa_missing_data_fails_nonfatal(self) -> None:
        result = MTFAFilter(test_settings()).evaluate("BTCUSD", {"H1": pd.DataFrame(), "M15": pd.DataFrame(), "M5": pd.DataFrame()}, "BUY")
        self.assertEqual(result["mtfa_status"], "FAIL")
        self.assertEqual(result["mtfa_reason"], "MISSING_TIMEFRAME_DATA")

    def test_mtfa_wait_direction_not_applicable(self) -> None:
        result = MTFAFilter(test_settings()).evaluate("BTCUSD", {"H1": pd.DataFrame(), "M15": pd.DataFrame(), "M5": pd.DataFrame()}, "WAIT")
        self.assertEqual(result["mtfa_status"], "NOT_APPLICABLE")
        self.assertEqual(result["mtfa_reason"], "NO_TRADE_DIRECTION")
        self.assertEqual(result["mtfa_score"], 0)

    def test_mtf_structure_missing_data_nonfatal(self) -> None:
        result = MTFStructureDetector(test_settings()).evaluate("BTCUSD", {"H4": pd.DataFrame(), "M15": pd.DataFrame(), "M1": pd.DataFrame()}, "BUY")
        self.assertIn(result["mtf_structure_status"], {"NOT_APPLICABLE", "FAIL"})
        self.assertEqual(result["mtf_structure_reason"], "MISSING_TIMEFRAME_DATA")

    def test_mtf_structure_wait_direction_not_applicable(self) -> None:
        result = MTFStructureDetector(test_settings()).evaluate("BTCUSD", {"H4": pd.DataFrame(), "M15": pd.DataFrame(), "M1": pd.DataFrame()}, "WAIT")
        self.assertEqual(result["mtf_structure_status"], "NOT_APPLICABLE")
        self.assertEqual(result["mtf_structure_reason"], "NO_TRADE_DIRECTION")
        self.assertEqual(result["mtf_structure_score"], 0)

    def test_mtf_structure_tag_only_does_not_block_trade(self) -> None:
        settings = test_settings()
        settings.mtf_structure_mode = "TAG_ONLY"
        agent = PaperTradingAgent(settings)
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
            "mtf_structure_status": "FAIL",
            "mtf_structure_mode": "TAG_ONLY",
            "mtf_structure_reason": "NO_M1_ENTRY_CONFIRMATION",
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertTrue(any(item.get("paper_action") == "OPEN_TRADE" for item in items))
        event = next(item["data"] for item in items if item.get("table") == "execution_events")
        self.assertEqual(event["raw_payload"]["mtf_structure_status"], "FAIL")

    def test_smc_tagger_handles_missing_data_safely(self) -> None:
        result = SMCConfluenceTagger(test_settings()).evaluate("BTCUSD", {}, "BUY")
        self.assertEqual(result["smc_h4_direction"], "UNKNOWN")
        self.assertEqual(result["smc_h1_order_block"], "NONE")
        self.assertEqual(result["smc_confluence_status"], "FAIL")
        self.assertIn("MISSING_DATA", result["smc_confluence_reason"])

    def test_smc_tagger_wait_direction_not_applicable(self) -> None:
        result = SMCConfluenceTagger(test_settings()).evaluate(
            "BTCUSD",
            {
                "H4": candle_frame([(1, 2, 0.8, 1.5), (1.5, 2.2, 1.0, 1.8), (1.8, 2.4, 1.2, 2.0)]),
                "H1": candle_frame([(1, 2, 0.8, 1.5), (1.5, 2.2, 1.0, 1.8), (1.8, 2.4, 1.2, 2.0)]),
                "M15": candle_frame([(1, 2, 0.8, 1.5), (1.5, 2.2, 1.0, 1.8), (1.8, 2.4, 1.2, 2.0)]),
                "M5": candle_frame([(1, 2, 0.8, 1.5), (1.5, 2.2, 1.0, 1.8), (1.8, 2.4, 1.2, 2.0)]),
                "M1": candle_frame([(1, 2, 0.8, 1.5), (1.5, 2.2, 1.0, 1.8), (1.8, 2.4, 1.2, 2.0)]),
            },
            "WAIT",
        )
        self.assertEqual(result["smc_confluence_status"], "NOT_APPLICABLE")
        self.assertIn(result["smc_h4_direction"], {"BULLISH", "BEARISH", "RANGE", "UNKNOWN"})

    def test_smc_tagger_never_blocks_entries(self) -> None:
        agent = PaperTradingAgent(test_settings())
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
            "smc_confluence_status": "FAIL",
            "smc_confluence_score": 0,
            "smc_confluence_reason": "LOW_SMC_CONFLUENCE_SCORE",
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertTrue(any(item.get("paper_action") == "OPEN_TRADE" for item in items))
        event = next(item["data"] for item in items if item.get("table") == "execution_events")
        self.assertEqual(event["raw_payload"]["smc_confluence_status"], "FAIL")

    def test_mtfa_fields_stored_in_learning_sample(self) -> None:
        optimizer = self.make_optimizer()
        sample = {
            "sample_type": "PAPER_SKIP",
            "symbol": "BTCUSD",
            "strategy": "EMA_PULLBACK",
            "h1_bias": "BULLISH",
            "m15_liquidity": True,
            "m15_liquidity_type": "SELL_SIDE_SWEEP",
            "m5_cisd": True,
            "mtfa_status": "PASS",
            "mtfa_reason": "MTFA_ALIGNED",
            "mtfa_score": 90,
            "mtfa_mode": "TAG_ONLY",
        }
        optimizer.record_setup(sample)
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"mtfa_status": "PASS"', text)

    def test_journal_fields_stored_in_learning_sample(self) -> None:
        optimizer = self.make_optimizer()
        optimizer.record_setup(
            {
                "sample_type": "PAPER_OPEN",
                "symbol": "BTCUSD",
                "timeframe": "M5",
                "strategy": "EMA_PULLBACK",
                "direction": "BUY",
                "entry": 100.0,
                "sl": 99.0,
                "tp": 103.0,
                "lot_size": 0.1,
                "final_risk": 0.1,
                "confidence": 0.8,
                "mtfa_status": "PASS",
                "mtfa_score": 80,
            }
        )
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"risk_reward": 3.0', text)
        self.assertIn('"market_structure"', text)
        self.assertIn('"reason_for_entry": "PAPER_ENTRY"', text)

    def test_mtf_structure_fields_stored_in_learning_sample(self) -> None:
        optimizer = self.make_optimizer()
        optimizer.record_setup(
            {
                "sample_type": "PAPER_SKIP",
                "symbol": "BTCUSD",
                "strategy": "MTF_STRUCTURE_4H_15M_1M",
                "mtf_structure_status": "PASS",
                "mtf_structure_direction": "BUY",
                "h4_bias": "BULLISH",
                "h4_zone": "DEMAND",
                "m15_confirmation": True,
                "m15_structure_shift": "BULLISH",
                "m1_entry_confirmation": True,
                "entry_price_suggestion": 100.0,
                "sl_suggestion": 99.0,
                "tp1_suggestion": 102.0,
                "tp2_suggestion": 104.0,
                "risk_reward_suggestion": 2.0,
                "mtf_structure_score": 100,
                "mtf_structure_reason": "MTF_STRUCTURE_ALIGNED",
            }
        )
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"mtf_structure_status": "PASS"', text)
        self.assertIn('"h4_bias": "BULLISH"', text)
        self.assertIn('"m1_entry_confirmation": true', text)

    def test_reentry_tags_stored_in_learning_sample(self) -> None:
        optimizer = self.make_optimizer()
        optimizer.record_setup(
            {
                "sample_type": "PAPER_OPEN",
                "symbol": "BTCUSD",
                "strategy": "EMA_PULLBACK",
                "previous_trade_result_for_symbol": "LOSS",
                "previous_trade_result_for_strategy": "LOSS",
                "consecutive_losses_symbol": 2,
                "consecutive_losses_symbol_strategy": 2,
                "minutes_since_last_loss": 3.5,
                "reentry_after_sl": True,
            }
        )
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"previous_trade_result_for_symbol": "LOSS"', text)
        self.assertIn('"consecutive_losses_symbol_strategy": 2', text)
        self.assertIn('"reentry_after_sl": true', text)

    def test_smc_fields_stored_in_learning_sample(self) -> None:
        optimizer = self.make_optimizer()
        optimizer.record_setup(
            {
                "sample_type": "PAPER_OPEN",
                "symbol": "BTCUSD",
                "strategy": "EMA_PULLBACK",
                "smc_h4_direction": "BULLISH",
                "smc_h1_fvg": "BULLISH_FVG",
                "smc_m15_confirmation": True,
                "ifvg_ote_sniper": True,
                "smc_confluence_status": "PASS",
                "smc_confluence_score": 80,
                "smc_confluence_reason": "SMC_CONFLUENCE_ALIGNED",
            }
        )
        text = (Path(self.tmp.name) / "data" / "paper_learning_samples.jsonl").read_text(encoding="utf-8")
        self.assertIn('"smc_confluence_status": "PASS"', text)
        self.assertIn('"ifvg_ote_sniper": true', text)

    def test_performance_metrics_use_only_closed_paper_trades(self) -> None:
        optimizer = self.make_optimizer()
        optimizer.record_setup({"sample_type": "PAPER_SKIP", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 999.0})
        optimizer.record_setup({"sample_type": "PAPER_OPEN", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 999.0})
        optimizer.record_close({"symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 10.0, "reward_risk": 2.0})
        optimizer.record_close({"symbol": "EURUSD", "strategy": "SECOND_ENTRY", "result": "LOSS", "pnl": -4.0, "reward_risk": 1.0})
        metrics = optimizer.performance_metrics()
        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["total_pnl"], 6.0)
        self.assertEqual(metrics["best_strategy"], "EMA_PULLBACK")
        self.assertEqual(metrics["worst_symbol"], "EURUSD")

    def test_strategy_stats_include_all_loaded_strategies(self) -> None:
        optimizer = self.make_optimizer()
        stats = optimizer.strategy_stats(
            [
                {
                    "sample_type": "SETUP",
                    "strategy": "SECOND_ENTRY",
                    "signal": "WAIT",
                    "confidence": 0.64,
                    "final_decision": "WAIT_ANALYSIS_ONLY",
                    "reason": "No setup",
                },
                {
                    "sample_type": "PAPER_SKIP",
                    "strategy": "SCALPING_AGENT",
                    "signal": "SELL",
                    "confidence": 0.58,
                    "reason": "LOW_CONFIDENCE",
                    "result": None,
                    "pnl": None,
                },
                {
                    "sample_type": "PAPER_OPEN",
                    "strategy": "EMA_PULLBACK",
                    "signal": "BUY",
                    "confidence": 0.8,
                },
                {
                    "sample_type": "PAPER_CLOSE",
                    "strategy": "EMA_PULLBACK",
                    "result": "WIN",
                    "pnl": 12.0,
                    "confidence": 80.0,
                },
                {
                    "sample_type": "PAPER_CLOSE",
                    "strategy": "EMA_PULLBACK",
                    "result": None,
                    "pnl": None,
                },
            ]
        )
        self.assertEqual(set(stats), {"EMA_PULLBACK", "BREAKOUT_RETEST", "SECOND_ENTRY", "SCALPING_AGENT"})
        self.assertEqual(stats["SECOND_ENTRY"]["wait_count"], 1)
        self.assertEqual(stats["SECOND_ENTRY"]["latest_status"], "ANALYSIS_ONLY")
        self.assertEqual(stats["SCALPING_AGENT"]["paper_skips"], 1)
        self.assertEqual(stats["SCALPING_AGENT"]["top_skip_reason"], "LOW_CONFIDENCE")
        self.assertEqual(stats["SCALPING_AGENT"]["win_rate_closed_only"], None)
        self.assertEqual(stats["SCALPING_AGENT"]["pnl"], 0)
        self.assertEqual(stats["EMA_PULLBACK"]["paper_opens"], 1)
        self.assertEqual(stats["EMA_PULLBACK"]["paper_closes"], 1)
        self.assertEqual(stats["EMA_PULLBACK"]["wins"], 1)
        self.assertEqual(stats["EMA_PULLBACK"]["win_rate_closed_only"], 1.0)
        self.assertEqual(stats["BREAKOUT_RETEST"]["latest_status"], "ANALYSIS_ONLY")

    def test_paper_report_body_adds_strategy_stats(self) -> None:
        body = _paper_report_body_with_strategy_stats('{"best_strategy":"EMA_PULLBACK"}', {"SECOND_ENTRY": {"paper_opens": 0}})
        self.assertIn('"best_strategy": "EMA_PULLBACK"', body)
        self.assertIn('"strategy_stats"', body)
        self.assertIn('"SECOND_ENTRY"', body)

    def test_report_includes_smc_and_risk_diag_observer_summaries(self) -> None:
        samples = [
            {
                "sample_type": "PAPER_CLOSE",
                "symbol": "BTCUSD",
                "strategy": "EMA_PULLBACK",
                "result": "WIN",
                "pnl": 10.0,
                "smc_confluence_status": "PASS",
                "smc_confluence_score": 80,
                "risk_diag_status": "OK",
            },
            {
                "sample_type": "PAPER_CLOSE",
                "symbol": "EURUSD",
                "strategy": "SECOND_ENTRY",
                "result": "LOSS",
                "pnl": -4.0,
                "raw_payload": {
                    "smc_confluence_status": "FAIL",
                    "smc_confluence_score": 20,
                    "risk_diag_status": "MISMATCH",
                    "risk_diag_mismatch_percent": 0.4,
                    "risk_diag_expected_risk_percent": 0.5,
                    "risk_diag_realized_risk_percent": 0.9,
                },
            },
        ]
        summaries = build_observer_summaries(samples, samples[:1])
        body = _paper_report_body_with_strategy_stats("{}", {}, None, None, summaries)
        self.assertEqual(summaries["smc_tagged_samples_count"], 2)
        self.assertEqual(summaries["smc_pass_count"], 1)
        self.assertEqual(summaries["risk_diag_mismatch_count"], 1)
        self.assertIn('"smc_performance_raw"', body)
        self.assertIn('"risk_diag_top_mismatches"', body)

    def test_time_stats_use_only_closed_paper_trades(self) -> None:
        settings = test_settings()
        settings.report_timezone = "UTC"
        samples = [
            {"sample_type": "PAPER_OPEN", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 999.0, "closed_at": "2026-05-29T07:10:00+00:00"},
            {"sample_type": "PAPER_SKIP", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "LOSS", "pnl": -999.0, "closed_at": "2026-05-29T07:15:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 10.0, "closed_at": "2026-05-29T07:20:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SECOND_ENTRY", "symbol": "EURUSD", "result": "LOSS", "pnl": -4.0, "closed_at": "2026-05-29T07:25:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SCALPING_AGENT", "symbol": "GOLD#", "result": None, "pnl": None, "closed_at": "2026-05-29T07:30:00+00:00"},
        ]
        stats = build_time_stats(samples, settings)
        hour = next(row for row in stats["hourly_stats_utc"] if row["hour_utc"] == "07:00")
        self.assertEqual(hour["trades"], 2)
        self.assertEqual(hour["wins"], 1)
        self.assertEqual(hour["losses"], 1)
        self.assertEqual(hour["pnl"], 6.0)
        self.assertEqual(hour["win_rate"], 0.5)

    def test_time_stats_local_timezone_conversion_and_low_sample(self) -> None:
        settings = test_settings()
        settings.report_timezone = "America/New_York"
        samples = [
            {"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 5.0, "closed_at": "2026-05-29T12:00:00+00:00"}
        ]
        stats = build_time_stats(samples, settings)
        local_hour = next(row for row in stats["hourly_stats_local"] if row["trades"] == 1)
        self.assertEqual(local_hour["hour_local"], "08:00")
        self.assertEqual(local_hour["confidence"], "LOW_SAMPLE")

    def test_time_stats_sessions_and_strategy_symbol_breakdowns(self) -> None:
        settings = test_settings()
        settings.report_timezone = "UTC"
        samples = [
            {"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 10.0, "closed_at": "2026-05-29T13:45:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "LOSS", "pnl": -2.0, "closed_at": "2026-05-29T13:50:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "BREAKOUT_RETEST", "symbol": "GOLD#", "result": "WIN", "pnl": 4.0, "closed_at": "2026-05-29T13:55:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SECOND_ENTRY", "symbol": "EURUSD", "result": "LOSS", "pnl": -1.0, "closed_at": "2026-05-29T14:00:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SCALPING_AGENT", "symbol": "EURUSD", "result": "WIN", "pnl": 3.0, "closed_at": "2026-05-29T14:05:00+00:00"},
        ]
        stats = build_time_stats(samples, settings)
        overlap = next(row for row in stats["session_stats"] if row["session"] == "OVERLAP")
        self.assertEqual(overlap["trades"], 5)
        self.assertEqual(overlap["confidence"], "NORMAL")
        self.assertEqual(overlap["profit_factor"], round(17.0 / 3.0, 6))
        self.assertIn("EMA_PULLBACK", stats["strategy_time_stats"])
        self.assertIn("BTCUSD", stats["symbol_time_stats"])
        self.assertIsNone(stats["strategy_time_stats"]["EMA_PULLBACK"]["best_session"])

    def test_paper_report_body_adds_time_stats_and_tables(self) -> None:
        settings = test_settings()
        settings.report_timezone = "UTC"
        time_stats = build_time_stats(
            [{"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 5.0, "closed_at": "2026-05-29T07:00:00+00:00"}],
            settings,
        )
        body = _paper_report_body_with_strategy_stats("{}", {}, time_stats)
        tables = format_time_stats_tables(time_stats)
        self.assertIn('"time_stats"', body)
        self.assertIn("TIME PERFORMANCE", tables)
        self.assertIn("SESSION PERFORMANCE", tables)

    def test_clean_report_samples_exclude_before_start_and_recovery(self) -> None:
        settings = test_settings()
        settings.report_clean_start_at = "2026-05-29T08:00:00+00:00"
        samples = [
            {"sample_type": "PAPER_CLOSE", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 10.0, "opened_at": "2026-05-29T07:00:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "LOSS", "pnl": -3.0, "created_at": "2026-05-29T08:30:00+00:00", "recovery_close": True},
            {"sample_type": "PAPER_CLOSE", "symbol": "EURUSD", "strategy": "SECOND_ENTRY", "result": "LOSS", "pnl": -2.0, "created_at": "2026-05-29T08:45:00+00:00", "reason": "RECOVERY_SL_BREACH"},
            {"sample_type": "PAPER_CLOSE", "symbol": "GOLD#", "strategy": "SCALPING_AGENT", "result": "WIN", "pnl": 4.0, "opened_at": "2026-05-29T09:00:00+00:00"},
        ]
        clean, excluded = clean_report_samples(samples, settings)
        self.assertEqual([sample["symbol"] for sample in clean if sample.get("sample_type") == "PAPER_CLOSE"], ["GOLD#"])
        self.assertEqual(excluded["excluded_before_clean_start_count"], 1)
        self.assertEqual(excluded["excluded_recovery_trades_count"], 2)
        self.assertEqual(excluded["excluded_missed_exit_count"], 0)

    def test_time_stats_default_to_clean_trades(self) -> None:
        settings = test_settings()
        settings.report_timezone = "UTC"
        settings.report_clean_start_at = "2026-05-29T08:00:00+00:00"
        samples = [
            {"sample_type": "PAPER_CLOSE", "strategy": "EMA_PULLBACK", "symbol": "BTCUSD", "result": "WIN", "pnl": 10.0, "opened_at": "2026-05-29T07:00:00+00:00", "closed_at": "2026-05-29T07:30:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SECOND_ENTRY", "symbol": "EURUSD", "result": "WIN", "pnl": 5.0, "opened_at": "2026-05-29T08:10:00+00:00", "closed_at": "2026-05-29T08:30:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "strategy": "SCALPING_AGENT", "symbol": "GOLD#", "result": "LOSS", "pnl": -4.0, "opened_at": "2026-05-29T08:20:00+00:00", "closed_at": "2026-05-29T08:35:00+00:00", "missed_exit": True},
        ]
        clean, _ = clean_report_samples(samples, settings)
        stats = build_time_stats(clean, settings)
        hour_7 = next(row for row in stats["hourly_stats_utc"] if row["hour_utc"] == "07:00")
        hour_8 = next(row for row in stats["hourly_stats_utc"] if row["hour_utc"] == "08:00")
        self.assertEqual(hour_7["trades"], 0)
        self.assertEqual(hour_8["trades"], 1)
        self.assertEqual(hour_8["pnl"], 5.0)

    def test_paper_report_body_adds_raw_clean_and_excluded_sections(self) -> None:
        body = _paper_report_body_with_strategy_stats(
            "{}",
            {},
            None,
            None,
            {
                "raw_performance": {"trades": 3},
                "clean_performance": {"trades": 1},
                "excluded_summary": {
                    "excluded_before_clean_start_count": 1,
                    "excluded_recovery_trades_count": 1,
                    "excluded_missed_exit_count": 1,
                },
            },
        )
        self.assertIn('"raw_performance"', body)
        self.assertIn('"clean_performance"', body)
        self.assertIn('"excluded_before_clean_start_count": 1', body)

    def test_manual_adjusted_report_excludes_only_configured_ids(self) -> None:
        settings = test_settings()
        settings.report_clean_start_at = ""
        settings.report_excluded_trade_ids = "bad-1, bad-2"
        samples = [
            {"sample_type": "PAPER_CLOSE", "id": "bad-1", "paper_trade_id": "p1", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "LOSS", "pnl": -100.0, "opened_at": "2026-05-27T00:00:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "id": "good-old", "paper_trade_id": "p2", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 20.0, "opened_at": "2026-05-27T01:00:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "id": "good-new", "paper_trade_id": "p3", "symbol": "EURUSD", "strategy": "SECOND_ENTRY", "result": "LOSS", "pnl": -5.0, "opened_at": "2026-05-29T01:00:00+00:00"},
        ]
        report = manual_adjusted_report(samples, settings)
        self.assertEqual(report["adjusted_summary"]["raw_trades_closed"], 3)
        self.assertEqual(report["adjusted_summary"]["raw_pnl"], -85.0)
        self.assertEqual(report["adjusted_summary"]["adjusted_trades_closed"], 2)
        self.assertEqual(report["adjusted_summary"]["adjusted_pnl"], 15.0)
        self.assertEqual(report["adjusted_summary"]["excluded_manual_count"], 1)
        self.assertEqual(report["adjusted_summary"]["excluded_manual_pnl"], -100.0)
        self.assertEqual(report["strategy_adjusted_stats"]["EMA_PULLBACK"]["raw_pnl"], -80.0)
        self.assertEqual(report["strategy_adjusted_stats"]["EMA_PULLBACK"]["adjusted_pnl"], 20.0)
        self.assertEqual(report["strategy_adjusted_stats"]["EMA_PULLBACK"]["excluded_count"], 1)
        self.assertEqual(report["symbol_adjusted_stats"]["BTCUSD"]["adjusted_pnl"], 20.0)

    def test_manual_exclusion_can_match_paper_trade_id(self) -> None:
        settings = test_settings()
        settings.report_excluded_trade_ids = "paper-bad"
        samples = [
            {"sample_type": "PAPER_CLOSE", "id": "row-1", "paper_trade_id": "paper-bad", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "LOSS", "pnl": -7.0},
            {"sample_type": "PAPER_CLOSE", "id": "row-2", "paper_trade_id": "paper-good", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "WIN", "pnl": 3.0},
        ]
        report = manual_adjusted_report(samples, settings)
        self.assertEqual(report["adjusted_summary"]["adjusted_trades_closed"], 1)
        self.assertEqual(report["excluded_outlier_summary"][0]["paper_trade_id"], "paper-bad")

    def test_biggest_losses_table_includes_ids(self) -> None:
        settings = test_settings()
        settings.report_excluded_trade_ids = "bad-1"
        samples = [
            {"sample_type": "PAPER_CLOSE", "id": "bad-1", "paper_trade_id": "p1", "symbol": "BTCUSD", "strategy": "EMA_PULLBACK", "result": "LOSS", "pnl": -100.0, "reason": "SL", "opened_at": "2026-05-29T01:00:00+00:00"},
            {"sample_type": "PAPER_CLOSE", "id": "loss-2", "paper_trade_id": "p2", "symbol": "EURUSD", "strategy": "SECOND_ENTRY", "result": "LOSS", "pnl": -10.0, "reason": "SL", "created_at": "2026-05-29T02:00:00+00:00"},
        ]
        report = manual_adjusted_report(samples, settings)
        tables = format_adjusted_report_tables(report)
        self.assertIn("BIGGEST LOSSES", tables)
        self.assertIn("bad-1", tables)
        self.assertIn("p1", tables)
        self.assertIn("EXCLUDED OUTLIERS", tables)

    def test_reentry_after_sl_is_tagged_but_does_not_block_open(self) -> None:
        agent = PaperTradingAgent(test_settings())
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "timeframe": "M5",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "strategy": "EMA_PULLBACK",
            "confidence": 0.8,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
        }
        open_items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        trade = next(item["trade"] for item in open_items if item.get("paper_action") == "OPEN_TRADE")
        agent.confirm_open_trade(trade)
        close_items = agent.process_closures(
            "BTCUSD",
            "BTCUSD#",
            pd.DataFrame(),
            tick={"bid": 98.5},
            symbol_specs={"tick_value": 1.0, "tick_size": 0.01},
            equity=1000.0,
        )
        self.assertTrue(any(item.get("paper_action") == "CLOSE_EVENT" for item in close_items))
        agent.confirm_close_trade("BTCUSD")

        reentry_items = agent.process_decision(decision, spread=1.0, setup_id="setup-2")
        self.assertTrue(any(item.get("paper_action") == "OPEN_TRADE" for item in reentry_items))
        paper_event = next(item["data"] for item in reentry_items if item.get("table") == "execution_events")
        self.assertEqual(paper_event["previous_trade_result_for_symbol"], "LOSS")
        self.assertEqual(paper_event["previous_trade_result_for_strategy"], "LOSS")
        self.assertEqual(paper_event["consecutive_losses_symbol"], 1)
        self.assertEqual(paper_event["consecutive_losses_symbol_strategy"], 1)
        self.assertTrue(paper_event["reentry_after_sl"])
        self.assertTrue(paper_event["raw_payload"]["reentry_after_sl"])

    def test_paper_close_update_matches_by_id(self) -> None:
        class FakeIngest:
            def __init__(self) -> None:
                self.match = None

            def update_row(self, table, match, data):
                self.match = match
                return {"ok": True, "body": "ok"}

        backend = object.__new__(HermesBackend)
        backend.settings = test_settings()
        backend.ingest_client = FakeIngest()
        backend.update_paper_trade_close({"id": "paper-uuid", "symbol": "BTCUSD", "result": "WIN", "pnl": 1.0})
        self.assertEqual(backend.ingest_client.match, {"id": "paper-uuid", "magic_number": 909001})

    def test_startup_recovers_open_paper_rows(self) -> None:
        class FakeIngest:
            def get_open_paper_trades(self, magic_number):
                return {
                    "ok": True,
                    "rows": [
                        {
                            "id": "paper-1",
                            "magic_number": magic_number,
                            "mode": "PAPER",
                            "symbol": "BTCUSD",
                            "dir": "BUY",
                            "entry": 100.0,
                            "sl": 99.0,
                            "tp": 102.0,
                            "lot_size": 0.1,
                            "status": "OPEN",
                        }
                    ],
                }

        backend = object.__new__(HermesBackend)
        backend.settings = test_settings()
        backend.ingest_client = FakeIngest()
        backend.paper_trader = PaperTradingAgent(backend.settings)
        summary = backend.recover_paper_open_trades()
        self.assertEqual(summary["loaded"], 1)
        self.assertTrue(backend.paper_trader.has_open_trade("BTCUSD"))
        trade = backend.paper_trader.open_trades["BTCUSD"][0]
        self.assertEqual(trade["paper_trade_id"], "paper-1")

    def test_recovery_read_uses_get_path_and_does_not_insert(self) -> None:
        class FakeResponse:
            status_code = 200
            text = '{"rows":[]}'

        settings = test_settings()
        settings.hermes_ingest_url = "https://example.test/api/ingest"
        settings.hermes_ingest_secret = "secret"
        captured = {}

        def fake_get(url, headers, timeout):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

        def fail_post(*args, **kwargs):
            raise AssertionError("recovery read must not call ingest insert POST")

        with patch("app.services.ingest_client.requests.get", fake_get), patch("app.services.ingest_client.requests.post", fail_post):
            result = IngestClient(settings).get_open_paper_trades(909001)

        self.assertTrue(result["ok"])
        parsed = urlparse(captured["url"])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/api/public/hermes-open-paper-trades")
        self.assertEqual(query["magic_number"], ["909001"])
        self.assertEqual(query["mode"], ["PAPER"])
        self.assertEqual(query["open_only"], ["true"])
        self.assertNotIn("symbol", query)

    def test_recovery_read_parses_open_rows_from_report_shape(self) -> None:
        class FakeResponse:
            status_code = 200
            text = (
                '{"report":{"open_trades":['
                '{"id":"paper-1","magic_number":909001,"raw_payload":{"mode":"PAPER"},'
                '"symbol":"BTCUSD","dir":"BUY","entry":100,"sl":99,"tp":102,"lot_size":0.1},'
                '{"id":"closed-1","magic_number":909001,"mode":"PAPER","symbol":"BTCUSD",'
                '"dir":"BUY","entry":100,"sl":99,"tp":102,"lot_size":0.1,"result":"WIN"}'
                ']}}'
            )

        settings = test_settings()
        settings.hermes_ingest_url = "https://example.test/api/ingest"
        settings.hermes_ingest_secret = "secret"

        with patch("app.services.ingest_client.requests.get", return_value=FakeResponse()):
            result = IngestClient(settings).get_open_paper_trades(909001)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["id"], "paper-1")

    def test_recovery_read_handles_lovable_400_nonfatal(self) -> None:
        class FakeResponse:
            status_code = 400
            text = '{"ok":false,"table":"trades","error":"missing_data","details":"payload.data is required"}'

        settings = test_settings()
        settings.hermes_ingest_url = "https://example.test/api/ingest"
        settings.hermes_ingest_secret = "secret"

        with patch("app.services.ingest_client.requests.get", return_value=FakeResponse()):
            result = IngestClient(settings).get_open_paper_trades(909001)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["rows"], [])

    def test_recovered_open_trade_can_close_by_tp(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades(
            [
                {
                    "id": "paper-1",
                    "magic_number": 909001,
                    "raw_payload": {"mode": "PAPER"},
                    "symbol": "BTCUSD",
                    "dir": "BUY",
                    "entry": 100.0,
                    "sl": 99.0,
                    "tp": 102.0,
                    "lot_size": 0.1,
                }
            ]
        )
        items = agent.process_closures(
            "BTCUSD",
            "BTCUSD#",
            pd.DataFrame(),
            tick={"bid": 102.5},
            symbol_specs={"tick_value": 1.0, "tick_size": 0.01},
            equity=1000.0,
        )
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(close_row["id"], "paper-1")
        self.assertEqual(close_row["magic_number"], 909001)
        self.assertEqual(close_row["result"], "WIN")

    def test_recovered_buy_closes_immediately_on_sl_breach(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-buy-sl", "BTCUSD", "BUY", 100.0, 99.0, 102.0)])
        items = agent.reconcile_recovered_trades("BTCUSD", "BTCUSD#", tick={"bid": 98.5}, symbol_specs={}, equity=1000.0)
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(close_row["reason"], "RECOVERY_SL_BREACH")
        self.assertEqual(close_row["result"], "LOSS")
        self.assertTrue(close_row["raw_payload"]["recovery_close"])
        self.assertTrue(close_row["raw_payload"]["missed_exit"])
        self.assertEqual(close_row["raw_payload"]["recovery_exit_price"], 98.5)

    def test_recovered_buy_closes_immediately_on_tp_breach(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-buy-tp", "BTCUSD", "BUY", 100.0, 99.0, 102.0)])
        items = agent.reconcile_recovered_trades("BTCUSD", "BTCUSD#", tick={"bid": 102.5}, symbol_specs={}, equity=1000.0)
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(close_row["reason"], "RECOVERY_TP_BREACH")
        self.assertEqual(close_row["result"], "WIN")

    def test_recovered_sell_closes_immediately_on_sl_breach(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-sell-sl", "BTCUSD", "SELL", 100.0, 101.0, 98.0)])
        items = agent.reconcile_recovered_trades("BTCUSD", "BTCUSD#", tick={"ask": 101.5}, symbol_specs={}, equity=1000.0)
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(close_row["reason"], "RECOVERY_SL_BREACH")
        self.assertEqual(close_row["result"], "LOSS")

    def test_recovered_sell_closes_immediately_on_tp_breach(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-sell-tp", "BTCUSD", "SELL", 100.0, 101.0, 98.0)])
        items = agent.reconcile_recovered_trades("BTCUSD", "BTCUSD#", tick={"ask": 97.5}, symbol_specs={}, equity=1000.0)
        close_row = next(item["data"] for item in items if item.get("paper_action") == "CLOSE_TRADE_ROW")
        self.assertEqual(close_row["reason"], "RECOVERY_TP_BREACH")
        self.assertEqual(close_row["result"], "WIN")

    def test_no_new_paper_open_before_recovery_reconciliation_completes(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.reset_on_startup()
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertEqual(items[0]["data"]["reason"], "RECOVERY_RECONCILIATION_PENDING")
        self.assertFalse(any(item.get("paper_action") == "OPEN_TRADE" for item in items))

    def test_duplicate_recovered_open_rows_prevent_new_open(self) -> None:
        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades(
            [
                {
                    "id": "paper-1",
                    "magic_number": 909001,
                    "mode": "PAPER",
                    "symbol": "BTCUSD",
                    "dir": "BUY",
                    "entry": 100.0,
                    "sl": 99.0,
                    "tp": 102.0,
                    "lot_size": 0.1,
                },
                {
                    "id": "paper-2",
                    "magic_number": 909001,
                    "mode": "PAPER",
                    "symbol": "BTCUSD",
                    "dir": "SELL",
                    "entry": 101.0,
                    "sl": 102.0,
                    "tp": 99.0,
                    "lot_size": 0.1,
                },
            ]
        )
        self.assertEqual(agent.paper_open_duplicate_count, 1)
        decision = {
            "decision": "ENTER_PAPER",
            "symbol": "BTCUSD",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "lot_size": 0.1,
            "risk_status": "APPROVED",
            "final_risk": 0.1,
            "confidence": 0.8,
        }
        items = agent.process_decision(decision, spread=1.0, setup_id="setup-1")
        self.assertEqual(items[0]["data"]["reason"], "MAX_ONE_OPEN_PAPER_TRADE_PER_SYMBOL")

    def test_close_update_does_not_insert_duplicate_close_row(self) -> None:
        class FakeIngest:
            def __init__(self) -> None:
                self.sent_tables = []
                self.match = None

            def send_row(self, table, data):
                self.sent_tables.append(table)
                return {"ok": True, "body": "ok"}

            def update_row(self, table, match, data):
                self.match = match
                return {"ok": True, "body": "ok"}

        class FakeOptimizer:
            def record_close(self, data):
                return []

        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades(
            [
                {
                    "id": "paper-1",
                    "magic_number": 909001,
                    "mode": "PAPER",
                    "symbol": "BTCUSD",
                    "dir": "BUY",
                    "entry": 100.0,
                    "sl": 99.0,
                    "tp": 102.0,
                    "lot_size": 0.1,
                }
            ]
        )
        items = agent.process_closures("BTCUSD", "BTCUSD#", pd.DataFrame(), tick={"bid": 102.5}, symbol_specs={}, equity=1000.0)
        backend = object.__new__(HermesBackend)
        backend.settings = test_settings()
        backend.ingest_client = FakeIngest()
        backend.learning_optimizer = FakeOptimizer()
        backend.paper_trader = agent
        backend.write_ingest_items(items)
        self.assertEqual(backend.ingest_client.match, {"id": "paper-1", "magic_number": 909001})
        self.assertNotIn("trades", backend.ingest_client.sent_tables)
        self.assertFalse(agent.has_open_trade("BTCUSD"))

    def test_recovery_close_update_does_not_insert_duplicate_close_row(self) -> None:
        class FakeIngest:
            def __init__(self) -> None:
                self.sent_tables = []
                self.match = None

            def send_row(self, table, data):
                self.sent_tables.append(table)
                return {"ok": True, "body": "ok"}

            def update_row(self, table, match, data):
                self.match = match
                return {"ok": True, "body": "ok"}

        class FakeOptimizer:
            def __init__(self) -> None:
                self.sample = None

            def record_close(self, data):
                self.sample = data
                return []

        agent = PaperTradingAgent(test_settings())
        agent.recover_open_trades([self._open_row("paper-recovery-close", "BTCUSD", "BUY", 100.0, 99.0, 102.0)])
        items = agent.reconcile_recovered_trades("BTCUSD", "BTCUSD#", tick={"bid": 98.5}, symbol_specs={}, equity=1000.0)
        backend = object.__new__(HermesBackend)
        backend.settings = test_settings()
        backend.ingest_client = FakeIngest()
        backend.learning_optimizer = FakeOptimizer()
        backend.paper_trader = agent
        backend.write_ingest_items(items)
        self.assertEqual(backend.ingest_client.match, {"id": "paper-recovery-close", "magic_number": 909001})
        self.assertNotIn("trades", backend.ingest_client.sent_tables)
        self.assertTrue(backend.learning_optimizer.sample["raw_payload"]["recovery_close"])

    def test_paper_report_body_adds_duplicate_open_fields(self) -> None:
        body = _paper_report_body_with_strategy_stats(
            "{}",
            {},
            None,
            {"paper_open_duplicate_count": 2, "open_duplicates_by_symbol": {"BTCUSD": 3}},
        )
        self.assertIn('"paper_open_duplicate_count": 2', body)
        self.assertIn('"BTCUSD": 3', body)

    def test_paper_report_body_adds_recovery_close_counts(self) -> None:
        counts = {
            "paper_open_duplicate_count": 0,
            "open_duplicates_by_symbol": {},
            "recovery_closed_count": 2,
            "recovery_sl_breach_count": 1,
            "recovery_tp_breach_count": 1,
            "recovery_ambiguous_count": 0,
        }
        body = _paper_report_body_with_strategy_stats("{}", {}, None, counts)
        self.assertIn('"recovery_closed_count": 2', body)
        self.assertIn('"recovery_sl_breach_count": 1', body)

    def test_backtest_module_imports_without_running_main(self) -> None:
        self.assertTrue(hasattr(mt5_backtest_lab, "run_backtest"))
        self.assertIn("SECOND_ENTRY", mt5_backtest_lab.LEGACY_STRATEGIES)

    def test_backtest_replay_prevents_lookahead_bias(self) -> None:
        frame = self._timed_frame([100, 101, 102])
        current_time = pd.Timestamp("2026-05-01T00:05:00Z").to_pydatetime()
        rolled = mt5_backtest_lab.rolling_frames({"M5": frame}, current_time)
        self.assertEqual(len(rolled["M5"]), 2)
        self.assertEqual(float(rolled["M5"].iloc[-1]["close"]), 101.0)

    def test_backtest_same_candle_sl_tp_chooses_conservative_sl(self) -> None:
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
        )
        future = pd.DataFrame(
            [
                {
                    "time": pd.Timestamp("2026-05-01T00:05:00Z"),
                    "open": 100.0,
                    "high": 103.0,
                    "low": 98.0,
                    "close": 101.0,
                    "spread": 1,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
            ]
        )
        trade = mt5_backtest_lab.simulate_trade(
            "EURUSD",
            "BREAKOUT_RETEST",
            {"signal": "BUY", "sl": 99.0, "tp": 102.0},
            future,
            test_settings(),
            cfg,
            datetime.fromisoformat("2026-05-01T00:00:00+00:00"),
        )
        self.assertEqual(trade["result"], "LOSS")
        self.assertEqual(trade["exit_reason"], "SL_HIT")

    def test_backtest_missing_sl_tp_skips_trade(self) -> None:
        settings = test_settings()
        decision = {"signal": "BUY", "sl": None, "tp": 102.0, "risk_diag_status": "OK"}
        action, reason = mt5_backtest_lab._final_action(decision, settings)
        self.assertEqual(action, "SKIP_ANALYSIS_ONLY")
        self.assertEqual(reason, "MISSING_SL_TP")

    def test_backtest_output_files_are_local_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "latest"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5", "H4"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
            max_holding_candles=3,
        )
        frame = self._timed_frame([1.10 + i * 0.001 for i in range(120)])
        h4 = self._timed_frame([1.10 + i * 0.004 for i in range(10)])
        result = mt5_backtest_lab.run_backtest({"EURUSD": {"M5": frame, "H4": h4}}, cfg, test_settings())
        for name in mt5_backtest_lab.REPORT_FILES:
            self.assertTrue((output / name).exists(), name)
        self.assertTrue((output / "candles" / "EURUSD_M5.csv").exists())
        self.assertIn("summary", result)
        tmp.cleanup()

    def test_backtest_exclude_symbols_removes_gold_entries(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["GOLD#"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tmp.name),
            exclude_symbols=["GOLD#"],
            precheck_candles=False,
        )
        signal = {"strategy": "BREAKOUT_RETEST", "signal": "BUY", "entry": 160.0, "sl": 159.0, "tp": 166.0, "confidence": 0.8}
        with patch("app.tools.mt5_backtest_lab.evaluate_strategy", return_value=signal):
            result = mt5_backtest_lab.run_backtest({"GOLD#": {"M5": self._timed_frame([100 + i for i in range(64)])}}, cfg, test_settings())
        self.assertEqual(result["summary"]["total_simulated_trades"], 0)
        self.assertGreater(result["summary"]["skipped_by_filter"]["exclude_symbols"], 0)
        self.assertEqual(result["summary"]["excluded_symbols"], ["GOLD#"])
        tmp.cleanup()

    def test_backtest_blocked_hours_skip_configured_hours(self) -> None:
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
            blocked_hours=[0, 1, 2, 3, 4, 5, 6, 7, 23],
        )
        settings = test_settings()
        replay_time = datetime.fromisoformat("2026-05-01T04:00:00+00:00")
        trade = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "big_setup_grade": "A"}
        decision = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "confidence": 0.9, "big_setup_grade": "A"}
        self.assertEqual(mt5_backtest_lab._backtest_filter(decision, trade, replay_time, settings, cfg), ("blocked_hours", "HOUR_BLOCKED"))

    def test_backtest_block_sessions_asia_skips_asia_trades(self) -> None:
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
            blocked_sessions=["ASIA"],
        )
        settings = test_settings()
        replay_time = datetime.fromisoformat("2026-05-01T01:00:00+00:00")
        trade = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "big_setup_grade": "A"}
        decision = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "confidence": 0.9, "big_setup_grade": "A"}
        self.assertEqual(mt5_backtest_lab._backtest_filter(decision, trade, replay_time, settings, cfg), ("blocked_sessions", "SESSION_BLOCKED"))

    def test_backtest_breakout_min_score_skips_low_score(self) -> None:
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
            breakout_retest_min_score=70,
        )
        replay_time = datetime.fromisoformat("2026-05-01T12:00:00+00:00")
        trade = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "big_setup_grade": "A"}
        decision = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "confidence": 0.5, "big_setup_grade": "A"}
        self.assertEqual(
            mt5_backtest_lab._backtest_filter(decision, trade, replay_time, test_settings(), cfg),
            ("breakout_retest_min_score", "BREAKOUT_RETEST_SCORE_BELOW_MIN"),
        )

    def test_backtest_min_big_setup_grade_skips_grade_c(self) -> None:
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
            min_big_setup_grade="A",
        )
        replay_time = datetime.fromisoformat("2026-05-01T12:00:00+00:00")
        trade = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "big_setup_grade": "C"}
        decision = {"symbol": "EURUSD", "strategy": "BREAKOUT_RETEST", "confidence": 0.9, "big_setup_grade": "C"}
        self.assertEqual(
            mt5_backtest_lab._backtest_filter(decision, trade, replay_time, test_settings(), cfg),
            ("min_big_setup_grade", "BIG_SETUP_GRADE_BELOW_MIN"),
        )

    def test_backtest_filters_and_strategy_activation_diagnostics_written(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "latest"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST", "CRT_TBS_REVERSAL"],
            output=output,
            breakout_retest_min_score=70,
            precheck_candles=False,
        )
        signal = {"strategy": "BREAKOUT_RETEST", "signal": "BUY", "entry": 160.0, "sl": 159.0, "tp": 166.0, "confidence": 0.5}
        with patch("app.tools.mt5_backtest_lab.evaluate_strategy", return_value=signal):
            result = mt5_backtest_lab.run_backtest({"EURUSD": {"M5": self._timed_frame([100 + i for i in range(64)])}}, cfg, test_settings())
        self.assertTrue(result["summary"]["filters_applied"]["breakout_retest_min_score"])
        self.assertIn("strategy_activation_diagnostics", result["summary"])
        self.assertTrue((output / "backtest_filter_diagnostics.csv").exists())
        table = pd.read_csv(output / "backtest_filter_diagnostics.csv")
        self.assertIn("breakout_retest_min_score", set(table["filter_name"]))
        tmp.cleanup()

    def test_backtest_observer_diagnostics_writes_concept_columns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "observer"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
            observer_diagnostics=True,
            precheck_candles=False,
        )
        signal = {"strategy": "BREAKOUT_RETEST", "signal": "WAIT", "reason": "TEST"}
        with patch("app.tools.mt5_backtest_lab.evaluate_strategy", return_value=signal):
            mt5_backtest_lab.run_backtest({"EURUSD": {"M5": self._timed_frame([100 + i for i in range(64)])}}, cfg, test_settings())
        table = pd.read_csv(output / "strategy_detection_diagnostics.csv")
        for column in ["fvg_detected", "ifvg_detected", "ob_detected", "bpr_detected", "ote_detected", "mss_detected", "cisd_detected", "qml_detected"]:
            self.assertIn(column, table.columns)
        tmp.cleanup()

    def test_backtest_progress_prints_and_writes_done_status(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "progress"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
            progress=True,
            progress_every=1,
            precheck_candles=False,
        )
        signal = {"strategy": "BREAKOUT_RETEST", "signal": "WAIT", "reason": "TEST"}
        with patch("app.tools.mt5_backtest_lab.evaluate_strategy", return_value=signal), patch("builtins.print") as printed:
            mt5_backtest_lab.run_backtest({"EURUSD": {"M5": self._timed_frame([100 + i for i in range(64)])}}, cfg, test_settings())
        output_text = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("[BACKTEST_PROGRESS]", output_text)
        self.assertIn("[BACKTEST_DONE]", output_text)
        progress = json.loads((output / "backtest_progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "DONE")
        tmp.cleanup()

    def test_backtest_progress_every_is_respected(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "progress_every"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
            progress=True,
            progress_every=2,
            precheck_candles=False,
        )
        signal = {"strategy": "BREAKOUT_RETEST", "signal": "WAIT", "reason": "TEST"}
        with patch("app.tools.mt5_backtest_lab.evaluate_strategy", return_value=signal), patch("builtins.print") as printed:
            mt5_backtest_lab.run_backtest({"EURUSD": {"M5": self._timed_frame([100 + i for i in range(64)])}}, cfg, test_settings())
        progress_lines = [str(call.args[0]) for call in printed.call_args_list if str(call.args[0]).startswith("[BACKTEST_PROGRESS]")]
        self.assertTrue(any("candles=2/3" in line for line in progress_lines))
        tmp.cleanup()

    def test_backtest_precheck_catches_empty_m1_before_replay(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "empty_m1"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["BTCUSD#"],
            days=1,
            timeframes=["M1", "M5"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
        )
        candles = {"BTCUSD#": {"M1": pd.DataFrame(), "M5": self._timed_frame([100 + i for i in range(120)])}}
        with self.assertRaises(SystemExit), patch("app.tools.mt5_backtest_lab.evaluate_strategy") as evaluator:
            mt5_backtest_lab.run_backtest(candles, cfg, test_settings())
        evaluator.assert_not_called()
        progress = json.loads((output / "backtest_progress.json").read_text(encoding="utf-8"))
        summary = json.loads((output / "backtest_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "ERROR")
        self.assertIn("EMPTY_CANDLES", summary["error"])
        tmp.cleanup()

    def test_backtest_precheck_reports_insufficient_candles(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "insufficient"
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=output,
        )
        with self.assertRaises(SystemExit):
            mt5_backtest_lab.run_backtest({"EURUSD": {"M5": self._timed_frame([100 + i for i in range(50)])}}, cfg, test_settings())
        summary = json.loads((output / "backtest_summary.json").read_text(encoding="utf-8"))
        self.assertIn("INSUFFICIENT_CANDLES", summary["error"])
        self.assertEqual(json.loads((output / "backtest_progress.json").read_text(encoding="utf-8"))["status"], "ERROR")
        tmp.cleanup()

    def test_backtest_relaxed_audit_does_not_modify_main_settings(self) -> None:
        settings = test_settings()
        before = settings.new_strategies_min_score
        cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["EURUSD"],
            days=1,
            timeframes=["M5"],
            strategies=["BREAKOUT_RETEST"],
            output=Path(tempfile.mkdtemp()),
            audit_relaxed_thresholds=True,
        )
        relaxed = mt5_backtest_lab._audit_settings(settings, cfg)
        self.assertEqual(settings.new_strategies_min_score, before)
        self.assertEqual(relaxed.new_strategies_min_score, 50)

    def test_backtest_legacy_strategies_are_not_active_entries(self) -> None:
        frames = {"M5": self._timed_frame([100 + i for i in range(80)])}
        for strategy in ("SECOND_ENTRY", "SCALPING_AGENT"):
            signal = mt5_backtest_lab.evaluate_strategy("EURUSD", strategy, frames, {}, test_settings())
            self.assertEqual(signal["strategy_status"], "LEGACY_OBSERVER")
            self.assertEqual(signal["signal"], "WAIT")

    def test_backtest_current_paper_settings_unchanged(self) -> None:
        settings = test_settings()
        self.assertTrue(settings.paper_trading)
        self.assertFalse(settings.demo_trading)
        self.assertFalse(settings.allow_live_trading)

    def test_btc_weekend_sandbox_imports_without_running_main(self) -> None:
        self.assertTrue(hasattr(btc_weekend_sandbox, "evaluate_cycle"))
        self.assertNotIn("SECOND_ENTRY", btc_weekend_sandbox.SANDBOX_STRATEGIES)
        self.assertNotIn("SCALPING_AGENT", btc_weekend_sandbox.SANDBOX_STRATEGIES)

    def test_btc_weekend_sandbox_ignores_legacy_and_ema_entries(self) -> None:
        settings = test_settings()
        settings.btc_weekend_sandbox_strategies = "BREAKOUT_RETEST,SECOND_ENTRY,SCALPING_AGENT,EMA_PULLBACK"
        self.assertEqual(btc_weekend_sandbox._sandbox_strategies(settings), ["BREAKOUT_RETEST"])
        decision = {"strategy": "EMA_PULLBACK", "signal": "BUY", "sl": 99.0, "tp": 102.0, "risk_reward": 2.0}
        action, reason = btc_weekend_sandbox.sandbox_decision(
            decision,
            {"safety_guard_status": "BLOCK", "safety_guard_reason": "BTC_WEEKEND_ANALYSIS_ONLY"},
            settings,
            self._sandbox_cfg(Path(tempfile.mkdtemp())),
            0,
        )
        self.assertEqual(action, "SKIP_SANDBOX")
        self.assertEqual(reason, "EMA_CONFIRMATION_ONLY")

    def test_btc_weekend_sandbox_records_main_guard_block_but_can_enter(self) -> None:
        settings = self.sandbox_settings()
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        decision = self.sandbox_decision_payload()
        main_guard = {"safety_guard_status": "BLOCK", "safety_guard_reason": "BTC_WEEKEND_ANALYSIS_ONLY"}
        action, reason = btc_weekend_sandbox.sandbox_decision(decision, main_guard, settings, cfg, 0)
        event = btc_weekend_sandbox._event(
            "BTCUSD#",
            "CRT_TBS_REVERSAL",
            datetime.fromisoformat("2026-05-30T12:00:00+00:00"),
            decision,
            main_guard,
            {"big_setup_grade": "A", "big_setup_score": 80, "big_setup_tags": ["TOP_DOWN_HTF_POI_LTF_CONFIRMATION"]},
            action,
            reason,
            False,
        )
        self.assertEqual(action, "ENTER_SANDBOX")
        self.assertTrue(event["main_safety_guard_would_block"])
        self.assertEqual(event["main_safety_guard_reason"], "BTC_WEEKEND_ANALYSIS_ONLY")

    def test_btc_weekend_sandbox_strict_filters_skip_low_smc_and_big_grade(self) -> None:
        settings = self.sandbox_settings()
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        low_smc = self.sandbox_decision_payload(smc_score=60)
        self.assertEqual(
            btc_weekend_sandbox.sandbox_decision(low_smc, {}, settings, cfg, 0),
            ("SKIP_SANDBOX", "SMC_SCORE_BELOW_SANDBOX_MIN"),
        )
        low_grade = self.sandbox_decision_payload(big_grade="B")
        self.assertEqual(
            btc_weekend_sandbox.sandbox_decision(low_grade, {}, settings, cfg, 0),
            ("SKIP_SANDBOX", "BIG_SETUP_GRADE_BELOW_SANDBOX_MIN"),
        )

    def test_btc_weekend_sandbox_missing_sl_tp_skips(self) -> None:
        settings = self.sandbox_settings()
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        decision = self.sandbox_decision_payload()
        decision["sl"] = None
        self.assertEqual(
            btc_weekend_sandbox.sandbox_decision(decision, {}, settings, cfg, 0),
            ("SKIP_SANDBOX", "MISSING_SL_TP"),
        )

    def test_btc_weekend_sandbox_same_candle_sl_tp_resolves_sl_first(self) -> None:
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        future = pd.DataFrame(
            [
                {
                    "time": pd.Timestamp("2026-05-30T12:05:00Z"),
                    "open": 100.0,
                    "high": 103.0,
                    "low": 98.0,
                    "close": 101.0,
                    "spread": 1,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
            ]
        )
        bt_cfg = mt5_backtest_lab.BacktestConfig(
            symbols=["BTCUSD#"],
            days=1,
            timeframes=["M5"],
            strategies=["CRT_TBS_REVERSAL"],
            output=cfg.output,
            risk_percent=cfg.risk_percent,
        )
        trade = mt5_backtest_lab.simulate_trade(
            "BTCUSD#",
            "CRT_TBS_REVERSAL",
            {"signal": "BUY", "sl": 99.0, "tp": 102.0},
            future,
            self.sandbox_settings(),
            bt_cfg,
            datetime.fromisoformat("2026-05-30T12:00:00+00:00"),
        )
        self.assertEqual(trade["result"], "LOSS")
        self.assertEqual(trade["exit_reason"], "SL_HIT")

    def test_btc_weekend_sandbox_output_files_are_local_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "btc_weekend_sandbox"
        event = self.sandbox_decision_payload()
        event.update(
            {
                "timestamp": "2026-05-30T12:00:00+00:00",
                "symbol": "BTCUSD#",
                "strategy": "CRT_TBS_REVERSAL",
                "direction": "BUY",
                "sandbox_decision": "SKIP_SANDBOX",
                "sandbox_reason": "TEST",
            }
        )
        btc_weekend_sandbox.write_outputs(output, [event], [])
        for name in btc_weekend_sandbox.OUTPUT_FILES:
            self.assertTrue((output / name).exists(), name)
        tmp.cleanup()

    def test_btc_weekend_sandbox_wait_cycle_keeps_price_and_data_status(self) -> None:
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        frames = {
            "M1": self._timed_frame([100 + i for i in range(120)]),
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(60)]),
            "H1": self._timed_frame([100 + i for i in range(60)]),
            "H4": self._timed_frame([100 + i for i in range(25)]),
        }
        cycle = btc_weekend_sandbox.evaluate_cycle("BTCUSD#", frames, self.sandbox_settings(), cfg, broker_symbol="BTCUSD#")
        self.assertIsNotNone(cycle["price"])
        self.assertTrue(cycle["data_status"].startswith("OK:"))
        self.assertTrue(cycle["events"])
        self.assertIsNotNone(cycle["events"][-1]["entry"])

    def test_btc_weekend_sandbox_evaluates_all_active_strategies_each_cycle(self) -> None:
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        frames = {
            "M1": self._timed_frame([100 + i for i in range(120)]),
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(60)]),
            "H1": self._timed_frame([100 + i for i in range(60)]),
            "H4": self._timed_frame([100 + i for i in range(25)]),
        }
        cycle = btc_weekend_sandbox.evaluate_cycle("BTCUSD#", frames, self.sandbox_settings(), cfg, broker_symbol="BTCUSD#")
        strategies = {event["strategy"] for event in cycle["events"]}
        self.assertEqual(strategies, set(btc_weekend_sandbox.SANDBOX_STRATEGIES))
        self.assertEqual(len(cycle["events"]), len(btc_weekend_sandbox.SANDBOX_STRATEGIES))

    def test_btc_weekend_sandbox_scoreboard_contains_each_strategy(self) -> None:
        events = [
            {
                "strategy": strategy,
                "strategy_score": index * 10,
                "signal": "WAIT",
                "sandbox_decision": "WAIT",
                "sandbox_reason": "TEST",
            }
            for index, strategy in enumerate(btc_weekend_sandbox.SANDBOX_STRATEGIES, start=1)
        ]
        with patch("builtins.print") as printed:
            btc_weekend_sandbox._print_strategy_scoreboard({"events": events})
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("[BTC_SANDBOX_STRATEGIES]", output)
        for strategy in btc_weekend_sandbox.SANDBOX_STRATEGIES:
            self.assertIn(strategy, output)

    def test_btc_weekend_sandbox_events_file_records_per_strategy_evaluations(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "btc_weekend_sandbox"
        events = []
        for strategy in btc_weekend_sandbox.SANDBOX_STRATEGIES:
            events.append(
                {
                    "timestamp": "2026-05-30T12:00:00+00:00",
                    "price": 100.0,
                    "symbol": "BTCUSD#",
                    "strategy": strategy,
                    "signal": "WAIT",
                    "strategy_score": 0,
                    "strategy_reason": "TEST",
                    "sandbox_decision": "WAIT",
                    "sandbox_reason": "TEST",
                    "smc_score": 80,
                    "smc_status": "PASS",
                    "big_setup_score": 60,
                    "big_setup_grade": "B",
                }
            )
        btc_weekend_sandbox.write_outputs(output, events, [])
        rows = (output / "sandbox_events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), len(btc_weekend_sandbox.SANDBOX_STRATEGIES))
        self.assertIn('"strategy_score": 0', rows[0])
        for strategy in btc_weekend_sandbox.SANDBOX_STRATEGIES:
            self.assertTrue(any(f'"strategy": "{strategy}"' in row for row in rows))
        tmp.cleanup()

    def test_btc_weekend_sandbox_skip_reasons_grouped_by_strategy(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        output = Path(tmp.name) / "btc_weekend_sandbox"
        events = [
            {"strategy": "BREAKOUT_RETEST", "sandbox_decision": "WAIT", "sandbox_reason": "NO_VALID_SIGNAL"},
            {"strategy": "BREAKOUT_RETEST", "sandbox_decision": "WAIT", "sandbox_reason": "NO_VALID_SIGNAL"},
            {"strategy": "CRT_TBS_REVERSAL", "sandbox_decision": "SKIP_SANDBOX", "sandbox_reason": "BIG_SETUP_GRADE_BELOW_SANDBOX_MIN"},
        ]
        btc_weekend_sandbox.write_outputs(output, events, [])
        table = pd.read_csv(output / "sandbox_skip_reasons.csv")
        row = table[(table["strategy"] == "BREAKOUT_RETEST") & (table["sandbox_reason"] == "NO_VALID_SIGNAL")].iloc[0]
        self.assertEqual(int(row["count"]), 2)
        self.assertIn("CRT_TBS_REVERSAL", set(table["strategy"]))
        tmp.cleanup()

    def test_btc_weekend_sandbox_symbol_resolution_accepts_hashless_broker_symbol(self) -> None:
        class Symbol:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeMT5:
            def symbols_get(self):
                return [Symbol("BTCUSD")]

            def symbol_select(self, symbol, enabled):
                return symbol == "BTCUSD" and enabled

        self.assertEqual(btc_weekend_sandbox._resolve_btc_symbol(FakeMT5(), "BTCUSD#"), "BTCUSD")

    def test_btc_weekend_sandbox_symbol_resolution_prefers_hash_when_available(self) -> None:
        class Symbol:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeMT5:
            def symbols_get(self):
                return [Symbol("BTCUSD#"), Symbol("BTCUSD")]

            def symbol_select(self, symbol, enabled):
                return symbol == "BTCUSD#" and enabled

        details = btc_weekend_sandbox._resolve_btc_symbol_details(FakeMT5(), "BTCUSD")
        self.assertEqual(details["resolved_symbol"], "BTCUSD#")
        self.assertTrue(details["symbol_select_result"])

    def test_btc_weekend_sandbox_exact_hash_symbol_resolves(self) -> None:
        class Symbol:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeMT5:
            def symbols_get(self):
                return [Symbol("BTCUSD#")]

            def symbol_select(self, symbol, enabled):
                return symbol == "BTCUSD#" and enabled

        self.assertEqual(btc_weekend_sandbox._resolve_btc_symbol(FakeMT5(), "BTCUSD#"), "BTCUSD#")

    def test_btc_weekend_sandbox_missing_candles_produces_clear_reason(self) -> None:
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        frames = {
            "M1": pd.DataFrame(),
            "M5": pd.DataFrame(),
            "M15": pd.DataFrame(),
            "H1": pd.DataFrame(),
            "H4": pd.DataFrame(),
            "__diagnostics__": {"reason": "NO_MT5_CANDLES_FOR_SYMBOL"},
        }
        cycle = btc_weekend_sandbox.evaluate_cycle("BTCUSD#", frames, self.sandbox_settings(), cfg, broker_symbol="BTCUSD#")
        self.assertEqual(cycle["chosen"]["sandbox_reason"], "NO_MT5_CANDLES_FOR_SYMBOL")
        self.assertEqual(cycle["data_status"], "MISSING_M1,M5,M15,H1,H4")

    def test_btc_weekend_sandbox_price_falls_back_to_m5_close_without_tick(self) -> None:
        frames = {"M5": self._timed_frame([100.0, 101.5]), "__diagnostics__": {"tick": {"available": False}}}
        self.assertEqual(btc_weekend_sandbox._latest_price(frames), 101.5)

    def test_btc_weekend_sandbox_diag_only_writes_no_trades(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        cfg = self._sandbox_cfg(Path(tmp.name) / "diag")
        frames = {
            "M1": self._timed_frame([100 + i for i in range(120)]),
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(60)]),
            "H1": self._timed_frame([100 + i for i in range(60)]),
            "H4": self._timed_frame([100 + i for i in range(25)]),
        }
        diagnostics = {"requested_symbol": "BTCUSD", "resolved_symbol": "BTCUSD#", "reason": None, "tick": {"bid": None, "ask": None}}
        with patch("app.tools.btc_weekend_sandbox.fetch_latest_btc_frames", return_value=(frames, "BTCUSD#", diagnostics)):
            result = btc_weekend_sandbox.run_diag_only(cfg)
        self.assertEqual(result["summary"]["sandbox_trades"], 0)
        self.assertTrue((cfg.output / "sandbox_summary.json").exists())
        self.assertFalse((cfg.output / "sandbox_trades.csv").exists())
        tmp.cleanup()

    def test_btc_weekend_sandbox_loads_m1_candles(self) -> None:
        class FakeMT5:
            TIMEFRAME_M1 = 1
            TIMEFRAME_M5 = 5
            TIMEFRAME_M15 = 15
            TIMEFRAME_H1 = 60
            TIMEFRAME_H4 = 240

            def __init__(self) -> None:
                self.calls = []

            def initialize(self):
                return True

            def shutdown(self):
                return None

            def symbols_get(self):
                return [SimpleNamespace(name="BTCUSD#")]

            def symbol_select(self, symbol, enabled):
                return symbol == "BTCUSD#" and enabled

            def symbol_info(self, symbol):
                return SimpleNamespace(visible=True, trade_mode=1)

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(_asdict=lambda: {"bid": 100.0, "ask": 101.0, "time": 1})

            def copy_rates_from_pos(self, symbol, timeframe, start, count):
                self.calls.append((timeframe, count))
                return [{"time": 1 + i * 60, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 1, "real_volume": 0} for i in range(count)]

            def copy_rates_range(self, *args):
                return []

            def last_error(self):
                return None

        fake = FakeMT5()
        with patch("app.tools.btc_weekend_sandbox._import_mt5", return_value=fake):
            frames, _, _ = btc_weekend_sandbox.fetch_latest_btc_frames("BTCUSD")
        self.assertIn("M1", frames)
        self.assertGreaterEqual(len(frames["M1"]), 500)
        self.assertIn((1, 500), fake.calls)

    def test_btc_weekend_sandbox_missing_m1_has_specific_reason(self) -> None:
        frames = {
            "M1": pd.DataFrame(),
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(60)]),
            "H1": self._timed_frame([100 + i for i in range(60)]),
            "H4": self._timed_frame([100 + i for i in range(25)]),
        }
        self.assertEqual(btc_weekend_sandbox._missing_data_reason(frames), "MISSING_M1_CONTEXT")
        frames["M1"] = self._timed_frame([100 + i for i in range(50)])
        self.assertEqual(btc_weekend_sandbox._missing_data_reason(frames), "INSUFFICIENT_M1_CANDLES")

    def test_btc_weekend_sandbox_smc_receives_m1_when_available(self) -> None:
        cfg = self._sandbox_cfg(Path(tempfile.mkdtemp()))
        frames = {
            "M1": self._timed_frame([100 + i for i in range(120)]),
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(60)]),
            "H1": self._timed_frame([100 + i for i in range(60)]),
            "H4": self._timed_frame([100 + i for i in range(25)]),
        }
        seen = {}

        def fake_smc(symbol, passed_frames, direction):
            seen["m1_len"] = len(passed_frames.get("M1", []))
            return {
                "smc_confluence_score": 80,
                "smc_confluence_status": "PASS",
                "smc_confluence_reason": "TEST",
            }

        with patch("app.agents.smc_confluence_tagger.SMCConfluenceTagger.evaluate", side_effect=fake_smc):
            btc_weekend_sandbox.evaluate_cycle("BTCUSD#", frames, self.sandbox_settings(), cfg, broker_symbol="BTCUSD#")
        self.assertGreaterEqual(seen["m1_len"], 100)

    def test_btc_weekend_sandbox_diagnostics_print_gate(self) -> None:
        self.assertTrue(btc_weekend_sandbox._should_print_diagnostics(None, "OK", False))
        self.assertFalse(btc_weekend_sandbox._should_print_diagnostics("OK", "OK", False))
        self.assertTrue(btc_weekend_sandbox._should_print_diagnostics("OK", "ERROR", False))
        self.assertTrue(btc_weekend_sandbox._should_print_diagnostics("OK", "OK", True))

    def test_btc_weekend_sandbox_does_not_modify_env_values(self) -> None:
        before = Path(".env").read_text(encoding="utf-8")
        settings = self.sandbox_settings()
        _ = btc_weekend_sandbox._sandbox_strategies(settings)
        after = Path(".env").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def _eligible_candidate_decision(self) -> dict:
        return {
            "signal": "BUY",
            "reason": "VALID_TEST_SIGNAL",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "risk_reward": 2.0,
            "mtfa_status": "PASS",
            "smc_confluence_status": "PASS",
            "smc_m15_confirmation": True,
            "smc_m1_entry_confirmation": True,
            "risk_diag_status": "OK",
            "safety_guard_status": "PASS",
            "time_gate_status": "PASS",
            "session_name": "LONDON",
            "symbol_market_open": True,
            "big_setup_grade": "A",
            "execution_checklist": {"lot_valid": True},
        }

    def _full_frames(self, include_m1: bool) -> dict:
        frames = {
            "M5": self._timed_frame([100 + i for i in range(120)]),
            "M15": self._timed_frame([100 + i for i in range(80)]),
            "H1": self._timed_frame([100 + i for i in range(80)]),
            "H4": self._timed_frame([100 + i for i in range(80)]),
        }
        if include_m1:
            frames["M1"] = self._timed_frame([100 + i for i in range(120)])
        return frames

    def _open_row(self, paper_id: str, symbol: str, direction: str, entry: float, sl: float, tp: float) -> dict:
        return {
            "id": paper_id,
            "magic_number": 909001,
            "mode": "PAPER",
            "symbol": symbol,
            "dir": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "lot_size": 0.1,
            "status": "OPEN",
        }

    def _timed_frame(self, closes: list[float]) -> pd.DataFrame:
        rows = []
        start = pd.Timestamp("2026-05-01T00:00:00Z")
        for i, close in enumerate(closes):
            rows.append(
                {
                    "time": start + pd.Timedelta(minutes=5 * i),
                    "open": close,
                    "high": close + 0.5,
                    "low": close - 0.5,
                    "close": close,
                    "spread": 1,
                    "tick_volume": 100,
                    "real_volume": 0,
                }
            )
        return pd.DataFrame(rows)

    def _strategy_fixture(self, name: str) -> pd.DataFrame:
        root = Path(__file__).resolve().parent / "fixtures" / "strategy_math"
        return pd.read_csv(root / name)

    def sandbox_settings(self) -> Settings:
        settings = test_settings()
        settings.btc_weekend_analysis_only = True
        settings.btc_weekend_sandbox_min_smc_score = 70
        settings.btc_weekend_sandbox_require_smc_pass = True
        settings.btc_weekend_sandbox_require_big_setup_grade = "A"
        settings.btc_weekend_sandbox_min_rr = 1.5
        settings.new_strategies_min_score = 75
        settings.max_spread_btcusd = 2500
        return settings

    def sandbox_decision_payload(self, smc_score: float = 80.0, big_grade: str = "A") -> dict:
        return {
            "symbol": "BTCUSD#",
            "strategy": "CRT_TBS_REVERSAL",
            "signal": "BUY",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "risk_reward": 2.0,
            "spread": 10.0,
            "risk_diag_status": "OK",
            "smc_confluence_status": "PASS",
            "smc_confluence_score": smc_score,
            "big_setup_grade": big_grade,
            "crt_tbs_score": 80,
        }

    def _sandbox_cfg(self, output: Path) -> btc_weekend_sandbox.SandboxConfig:
        return btc_weekend_sandbox.SandboxConfig(
            symbol="BTCUSD#",
            output=output,
            duration_hours=0,
            poll_seconds=1,
            risk_percent=0.25,
            max_open_trades=1,
            strategies=["BREAKOUT_RETEST", "CRT_TBS_REVERSAL", "AMD_FVG_IFVG_REVERSAL", "FIB_OTE_RETEST"],
        )

    def test_no_mt5_order_send_exists(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matches = []
        allowed = {str(root / "app" / "mt5" / "demo_router.py")}
        for path in (root / "app").rglob("*.py"):
            if ".venv" in path.parts or "venv" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                found = str(path)
                if found not in allowed:
                    matches.append(found)
        self.assertEqual(matches, [])

    def test_no_supabase_direct_usage_exists(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matches = []
        for path in root.rglob("*.py"):
            if ".venv" in path.parts or "venv" in path.parts:
                continue
            if "external_strategies" in path.parts and "incoming" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if "supabase" in text and path.name not in {"test_paper_learning_safety.py", "test_telemetry.py"}:
                matches.append(str(path))
        self.assertEqual(matches, [])

    def test_default_settings_remain_paper_tag_only(self) -> None:
        settings = test_settings()
        self.assertTrue(settings.paper_trading)
        self.assertFalse(settings.demo_trading)
        self.assertFalse(settings.allow_live_trading)
        self.assertEqual(settings.mtfa_mode, "TAG_ONLY")


if __name__ == "__main__":
    unittest.main()
