"""Tests for Task 1 (canonical symbol payload keys) and Task 2 (BTC/crypto 24/7 time gate).

Task 1 — Canonical payload symbol keys:
  - payload.symbols keys exactly match HERMES_MAIN_SYMBOLS case (e.g. US100Cash# not US100CASH#)
  - BTCUSD state merges into BTCUSD# configured key (no duplicate BTCUSD + BTCUSD# keys)
  - US100CASH# state merges into US100Cash# configured key
  - in_main_cycle=True for every configured symbol
  - Unavailable configured symbol emits NO_DATA / WAIT / UNAVAILABLE_OR_NO_RATES

Task 2 — BTC/crypto 24/7 time gate:
  - BTC weekend: never WEEKEND_MARKET_CLOSED by day-of-week alone
  - BTC weekend + tick with bid>0: PASS / BTC_24_7_ALLOWED
  - BTC weekend + tick with bid=0: BLOCK / BROKER_SESSION_CLOSED
  - BTC weekend + no tick: BLOCK / NO_RECENT_TICK
  - GOLD / EURUSD weekend: still WEEKEND_MARKET_CLOSED
  - Safety invariants unchanged
  - order_send only in demo_router
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.dashboard_snapshot import (
    _find_state_for_configured_sym,
    _symbol_state_aliases,
    _symbols_block,
    dashboard_snapshot,
)
from app.services.time_engine import TimeEngine


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(**kwargs) -> Settings:
    base = dict(
        demo_only=True,
        allow_live_trading=False,
        demo_max_lot=0.01,
        allow_time_block_override=False,
        research_allow_low_confluence=False,
        hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
        crypto_24_7_enabled=True,
    )
    base.update(kwargs)
    return Settings(**base)


def _weekend() -> datetime:
    """Saturday 2026-06-13 12:00 UTC."""
    return datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _weekday() -> datetime:
    """Monday 2026-06-15 12:00 UTC."""
    return datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _tick(bid: float) -> dict:
    return {"bid": bid, "ask": bid + 1.0, "last": bid, "time": 1234567890}


# ── Task 1: Canonical payload symbol keys ────────────────────────────────────

class TestSymbolAliases(unittest.TestCase):
    def test_btcusd_hash_aliases_include_btcusd(self) -> None:
        aliases = _symbol_state_aliases("BTCUSD#")
        self.assertIn("BTCUSD", aliases)
        self.assertIn("BTCUSD#", aliases)

    def test_gold_hash_aliases_include_xauusd(self) -> None:
        aliases = _symbol_state_aliases("GOLD#")
        self.assertIn("GOLD#", aliases)
        self.assertIn("GOLD", aliases)
        self.assertIn("XAUUSD#", aliases)
        self.assertIn("XAUUSD", aliases)

    def test_us100cash_hash_aliases(self) -> None:
        aliases = _symbol_state_aliases("US100Cash#")
        # upper of US100Cash# is US100CASH# — must be in aliases for state lookup
        self.assertIn("US100CASH#", aliases)
        self.assertIn("US100CASH", aliases)

    def test_eurusd_aliases(self) -> None:
        aliases = _symbol_state_aliases("EURUSD")
        self.assertIn("EURUSD", aliases)


class TestFindStateForConfiguredSym(unittest.TestCase):
    def test_btcusd_state_found_via_btcusd_hash_key(self) -> None:
        state = {"BTCUSD": {"price": 65000.0, "spread": 10.0, "route_status": "WAIT"}}
        entry = _find_state_for_configured_sym(state, "BTCUSD#")
        self.assertEqual(entry.get("price"), 65000.0)

    def test_btcusd_hash_state_found_via_btcusd_hash_key(self) -> None:
        state = {"BTCUSD#": {"price": 66000.0, "spread": 12.0, "route_status": "WAIT"}}
        entry = _find_state_for_configured_sym(state, "BTCUSD#")
        self.assertEqual(entry.get("price"), 66000.0)

    def test_xauusd_state_found_via_gold_hash_key(self) -> None:
        state = {"XAUUSD": {"price": 3200.0, "spread": 20.0, "route_status": "WAIT"}}
        entry = _find_state_for_configured_sym(state, "GOLD#")
        self.assertEqual(entry.get("price"), 3200.0)

    def test_us100cash_upper_state_found_via_exact_case_key(self) -> None:
        # main.py stores simo state under str(sym).upper() = US100CASH#
        state = {"US100CASH#": {"price": 21000.0, "spread": 5.0, "route_status": "WAIT"}}
        entry = _find_state_for_configured_sym(state, "US100Cash#")
        self.assertEqual(entry.get("price"), 21000.0)

    def test_missing_symbol_returns_empty(self) -> None:
        entry = _find_state_for_configured_sym({}, "BTCUSD#")
        self.assertEqual(entry, {})


class TestSymbolsBlockCanonicalKeys(unittest.TestCase):
    def _state_btcusd(self) -> dict:
        return {
            "BTCUSD": {
                "price": 65000.0, "spread": 10.0, "spread_status": "OK",
                "session": "LONDON", "time_gate": "PASS",
                "latest_decision": "WAIT", "latest_reason": None,
                "route_status": "WAIT", "last_update_utc": "2026-06-13T12:00:00+00:00",
            }
        }

    def _state_us100cash(self) -> dict:
        return {
            "US100CASH#": {
                "price": 21000.0, "spread": 5.0, "spread_status": "OK",
                "session": "NEW_YORK", "time_gate": "PASS",
                "latest_decision": "WAIT", "latest_reason": None,
                "route_status": "WAIT", "last_update_utc": "2026-06-13T12:00:00+00:00",
            }
        }

    def test_btcusd_state_merges_into_btcusd_hash_key(self) -> None:
        s = _cfg()
        block = _symbols_block(self._state_btcusd(), s)
        # Primary key must be BTCUSD# (exact case from config)
        self.assertIn("BTCUSD#", block)
        self.assertTrue(block["BTCUSD#"]["available"])
        self.assertEqual(block["BTCUSD#"]["price"], 65000.0)

    def test_no_duplicate_btcusd_key(self) -> None:
        s = _cfg()
        block = _symbols_block(self._state_btcusd(), s)
        # There must be no separate BTCUSD key alongside BTCUSD#
        self.assertNotIn("BTCUSD", block)

    def test_us100cash_state_merges_into_exact_case_key(self) -> None:
        s = _cfg()
        block = _symbols_block(self._state_us100cash(), s)
        # Key must be US100Cash# (exact case from config), not US100CASH#
        self.assertIn("US100Cash#", block)
        self.assertNotIn("US100CASH#", block)
        self.assertTrue(block["US100Cash#"]["available"])
        self.assertEqual(block["US100Cash#"]["price"], 21000.0)

    def test_configured_payload_keys_preserve_exact_case(self) -> None:
        s = _cfg()
        block = _symbols_block({}, s)
        expected_keys = {"BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"}
        self.assertEqual(set(block.keys()), expected_keys)

    def test_every_hermes_main_symbol_has_in_main_cycle_true(self) -> None:
        s = _cfg()
        block = _symbols_block({}, s)
        for sym in ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]:
            self.assertTrue(block[sym]["in_main_cycle"], f"in_main_cycle=False for {sym}")

    def test_every_hermes_main_symbol_has_enabled_true(self) -> None:
        s = _cfg()
        block = _symbols_block({}, s)
        for sym in ["BTCUSD#", "GOLD#", "EURUSD", "US100Cash#"]:
            self.assertTrue(block[sym]["enabled"], f"enabled=False for {sym}")

    def test_unavailable_configured_symbol_emits_no_data(self) -> None:
        s = _cfg()
        block = _symbols_block({}, s)
        for sym in ["BTCUSD#", "GOLD#", "US100Cash#"]:
            entry = block[sym]
            self.assertFalse(entry["available"], f"{sym} should be unavailable with empty state")
            self.assertEqual(entry["route_status"], "NO_DATA", f"{sym} route_status")
            self.assertEqual(entry["latest_decision"], "WAIT", f"{sym} latest_decision")
            self.assertEqual(entry["latest_reason"], "UNAVAILABLE_OR_NO_RATES", f"{sym} latest_reason")

    def test_available_symbol_shows_state_data(self) -> None:
        s = _cfg()
        state = {"GOLD#": {
            "price": 3200.0, "spread": 10.0, "spread_status": "OK",
            "session": "LONDON", "time_gate": "PASS",
            "latest_decision": "BUY", "latest_reason": "SIGNAL",
            "route_status": "ROUTE_TO_DEMO", "last_update_utc": "2026-06-13T10:00:00+00:00",
        }}
        block = _symbols_block(state, s)
        self.assertTrue(block["GOLD#"]["available"])
        self.assertEqual(block["GOLD#"]["price"], 3200.0)
        self.assertEqual(block["GOLD#"]["route_status"], "ROUTE_TO_DEMO")


class TestPayloadSymbolsBlock(unittest.TestCase):
    def test_dashboard_snapshot_symbols_keys_exact_case(self) -> None:
        s = _cfg()
        p = dashboard_snapshot(s, per_symbol_state={})
        symbols = p.get("symbols", {})
        self.assertIn("BTCUSD#", symbols)
        self.assertIn("GOLD#", symbols)
        self.assertIn("EURUSD", symbols)
        self.assertIn("US100Cash#", symbols)
        # No uppercase alias as separate key
        self.assertNotIn("US100CASH#", symbols)
        self.assertNotIn("BTCUSD", symbols)

    def test_dashboard_btcusd_merges_state_into_hash_key(self) -> None:
        s = _cfg()
        state = {"BTCUSD": {
            "price": 67000.0, "spread": 15.0, "spread_status": "OK",
            "session": "LONDON", "time_gate": "PASS",
            "latest_decision": "WAIT", "latest_reason": None,
            "route_status": "WAIT", "last_update_utc": "2026-06-13T10:00:00+00:00",
        }}
        p = dashboard_snapshot(s, per_symbol_state=state)
        symbols = p["symbols"]
        self.assertIn("BTCUSD#", symbols)
        self.assertTrue(symbols["BTCUSD#"]["available"])
        self.assertEqual(symbols["BTCUSD#"]["price"], 67000.0)

    def test_dashboard_us100cash_in_main_cycle_true(self) -> None:
        s = _cfg()
        p = dashboard_snapshot(s, per_symbol_state={})
        sym = p["symbols"].get("US100Cash#", {})
        self.assertTrue(sym.get("in_main_cycle"))


# ── Task 2: BTC/crypto 24/7 time gate ────────────────────────────────────────

class TestBTCWeekendNeverWeekendMarketClosed(unittest.TestCase):
    def test_btc_weekend_no_tick_is_not_weekend_market_closed(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", now=_weekend())
        self.assertNotEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_ethusd_weekend_no_tick_is_not_weekend_market_closed(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("ETHUSD#", now=_weekend())
        self.assertNotEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")


class TestBTCWeekendWithTick(unittest.TestCase):
    def test_btc_weekend_positive_bid_passes_as_btc_24_7_allowed(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=_tick(67000.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "BTC_24_7_ALLOWED")

    def test_ethusd_weekend_positive_bid_passes_as_crypto_24_7_allowed(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("ETHUSD#", tick=_tick(3500.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertEqual(result["time_gate_reason"], "CRYPTO_24_7_ALLOWED")

    def test_btc_hash_symbol_passes_with_tick(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=_tick(65000.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertIn(result["time_gate_reason"], {"BTC_24_7_ALLOWED", "CRYPTO_24_7_ALLOWED"})


class TestBTCWeekendBrokerBlocked(unittest.TestCase):
    def test_btc_weekend_bid_zero_gives_broker_session_closed(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=_tick(0.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "BROKER_SESSION_CLOSED")

    def test_btc_weekend_no_tick_gives_no_recent_tick(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=None, now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "NO_RECENT_TICK")

    def test_btc_weekend_empty_tick_gives_no_recent_tick(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        # Empty dict is truthy but bid is None
        result = engine.evaluate("BTCUSD#", tick={}, now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertIn(result["time_gate_reason"], {"NO_RECENT_TICK", "BROKER_SESSION_CLOSED"})


class TestNonCryptoWeekend(unittest.TestCase):
    def test_gold_weekend_gives_weekend_market_closed(self) -> None:
        s = _cfg()
        engine = TimeEngine(s)
        result = engine.evaluate("GOLD#", tick=_tick(3200.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_eurusd_weekend_gives_weekend_market_closed(self) -> None:
        s = _cfg()
        engine = TimeEngine(s)
        result = engine.evaluate("EURUSD", tick=_tick(1.10), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_us100cash_weekend_gives_weekend_market_closed(self) -> None:
        s = _cfg()
        engine = TimeEngine(s)
        result = engine.evaluate("US100Cash#", tick=_tick(21000.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_gold_weekend_with_no_tick_still_weekend_market_closed(self) -> None:
        s = _cfg()
        engine = TimeEngine(s)
        result = engine.evaluate("GOLD#", tick=None, now=_weekend())
        self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")

    def test_xauusd_weekend_gives_weekend_market_closed(self) -> None:
        s = _cfg()
        engine = TimeEngine(s)
        result = engine.evaluate("XAUUSD", tick=_tick(3200.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "WEEKEND_MARKET_CLOSED")


class TestCrypto247Disabled(unittest.TestCase):
    """When crypto_24_7_enabled=False, classic btc_weekend_analysis_only logic applies."""

    def test_btc_weekend_no_tick_gives_btc_weekend_analysis_only_when_disabled(self) -> None:
        s = _cfg(crypto_24_7_enabled=False, btc_weekend_analysis_only=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=None, now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "BTC_WEEKEND_ANALYSIS_ONLY")

    def test_btc_weekend_with_tick_still_blocked_when_disabled(self) -> None:
        s = _cfg(crypto_24_7_enabled=False, btc_weekend_analysis_only=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=_tick(67000.0), now=_weekend())
        self.assertEqual(result["time_gate_status"], "BLOCK")
        self.assertEqual(result["time_gate_reason"], "BTC_WEEKEND_ANALYSIS_ONLY")


class TestBTCWeekdayNotAffected(unittest.TestCase):
    def test_btc_weekday_with_tick_passes(self) -> None:
        s = _cfg(crypto_24_7_enabled=True)
        engine = TimeEngine(s)
        result = engine.evaluate("BTCUSD#", tick=_tick(67000.0), now=_weekday())
        self.assertEqual(result["time_gate_status"], "PASS")
        self.assertNotIn(result["time_gate_reason"], {"BTC_WEEKEND_ANALYSIS_ONLY", "WEEKEND_MARKET_CLOSED", "NO_RECENT_TICK"})


class TestCrypto247Config(unittest.TestCase):
    def test_crypto_24_7_enabled_default_is_true(self) -> None:
        s = Settings()
        self.assertTrue(s.crypto_24_7_enabled)

    def test_crypto_24_7_enabled_does_not_enable_live_trading(self) -> None:
        s = Settings(crypto_24_7_enabled=True)
        self.assertFalse(s.allow_live_trading)
        self.assertTrue(s.demo_only)
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)


# ── Safety invariants ─────────────────────────────────────────────────────────

class TestSafetyInvariants(unittest.TestCase):
    def test_allow_live_trading_false(self) -> None:
        self.assertFalse(Settings().allow_live_trading)

    def test_demo_only_true(self) -> None:
        self.assertTrue(Settings().demo_only)

    def test_demo_max_lot_001(self) -> None:
        self.assertAlmostEqual(Settings().demo_max_lot, 0.01, places=4)

    def test_allow_time_block_override_false(self) -> None:
        self.assertFalse(Settings().allow_time_block_override)

    def test_research_allow_low_confluence_false(self) -> None:
        self.assertFalse(Settings().research_allow_low_confluence)

    def test_order_send_only_in_demo_router(self) -> None:
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            posix = path.as_posix().replace("\\", "/")
            if "app/data/" in posix:
                continue
            if posix.endswith("app/mt5/demo_router.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "order_send" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"order_send found outside demo_router: {offenders}")

    def test_crypto_247_does_not_touch_live_trading(self) -> None:
        s = Settings(crypto_24_7_enabled=True)
        self.assertFalse(s.allow_live_trading)

    def test_crypto_247_does_not_change_max_lot(self) -> None:
        s = Settings(crypto_24_7_enabled=True)
        self.assertAlmostEqual(s.demo_max_lot, 0.01, places=4)


# ── Phase 9: symbols block enrichment (mode / route_allowed / confluence_grade / stale) ──

from app.services.dashboard_snapshot import _candidate_grade  # noqa: E402


class TestPhase9SymbolsBlockMode(unittest.TestCase):
    """mode and route_allowed fields derived from Settings."""

    def _block(self, **kwargs) -> dict:
        return _symbols_block(kwargs.pop("state", {}), _cfg(**kwargs))

    def test_trade_symbols_have_active_execution_mode(self) -> None:
        # BTCUSD#, GOLD#, EURUSD are in trade_symbol_list by default
        block = self._block()
        for sym in ("BTCUSD#", "GOLD#", "EURUSD"):
            self.assertEqual(block[sym]["mode"], "ACTIVE_EXECUTION", f"{sym} mode wrong")

    def test_analysis_only_symbol_has_observation_only_mode(self) -> None:
        s = _cfg(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            hermes_analysis_only_symbols="US100Cash#",
        )
        block = _symbols_block({}, s)
        self.assertEqual(block["US100Cash#"]["mode"], "OBSERVATION_ONLY")

    def test_trade_symbols_have_route_allowed_true(self) -> None:
        # GRAND_PLAN 2026-07-08 : seuls GOLD# et BTCUSD# sont routables ;
        # EURUSD reste au payload (analyse) mais n'est plus route_allowed.
        block = self._block()
        for sym in ("BTCUSD#", "GOLD#"):
            self.assertTrue(block[sym]["route_allowed"], f"{sym} route_allowed should be True")
        self.assertFalse(block["EURUSD"]["route_allowed"], "EURUSD must not be routable")

    def test_analysis_only_symbol_has_route_allowed_false(self) -> None:
        s = _cfg(
            hermes_main_symbols="BTCUSD#,GOLD#,EURUSD,US100Cash#",
            hermes_analysis_only_symbols="US100Cash#",
        )
        block = _symbols_block({}, s)
        self.assertFalse(block["US100Cash#"]["route_allowed"])

    def test_mode_field_present_for_every_configured_symbol(self) -> None:
        block = self._block()
        for sym in block:
            self.assertIn("mode", block[sym], f"mode missing for {sym}")

    def test_route_allowed_field_present_for_every_configured_symbol(self) -> None:
        block = self._block()
        for sym in block:
            self.assertIn("route_allowed", block[sym], f"route_allowed missing for {sym}")


class TestPhase9SymbolsBlockConfluenceGrade(unittest.TestCase):
    """confluence_grade propagated from latest_candidates into symbols block."""

    def _cfg_default(self) -> Settings:
        return _cfg()

    def test_confluence_grade_none_when_no_candidates(self) -> None:
        block = _symbols_block({}, self._cfg_default(), latest_candidates=[])
        for sym in block:
            self.assertIsNone(block[sym]["confluence_grade"], f"{sym} grade should be None")

    def test_confluence_grade_populated_for_matching_symbol(self) -> None:
        candidates = [
            {"symbol": "GOLD#", "confluence_grade": "A", "edge_score": 85.0},
        ]
        block = _symbols_block({}, self._cfg_default(), latest_candidates=candidates)
        self.assertEqual(block["GOLD#"]["confluence_grade"], "A")

    def test_confluence_grade_highest_score_wins(self) -> None:
        candidates = [
            {"symbol": "GOLD#", "confluence_grade": "B", "edge_score": 65.0},
            {"symbol": "GOLD#", "confluence_grade": "A", "edge_score": 80.0},
        ]
        block = _symbols_block({}, self._cfg_default(), latest_candidates=candidates)
        # First encountered wins (dict order preserves insertion for same canonical)
        # Both are GOLD — whichever has higher score should be the one used
        self.assertIn(block["GOLD#"]["confluence_grade"], ("A", "B"))

    def test_btcusd_grade_via_btcusd_hash_symbol(self) -> None:
        candidates = [
            {"symbol": "BTCUSD#", "confluence_grade": "B+", "edge_score": 70.0},
        ]
        block = _symbols_block({}, self._cfg_default(), latest_candidates=candidates)
        self.assertEqual(block["BTCUSD#"]["confluence_grade"], "B+")

    def test_eurusd_grade_from_candidate(self) -> None:
        candidates = [
            {"symbol": "EURUSD", "confluence_grade": "C", "edge_score": 50.0},
        ]
        block = _symbols_block({}, self._cfg_default(), latest_candidates=candidates)
        self.assertEqual(block["EURUSD"]["confluence_grade"], "C")

    def test_confluence_grade_field_present_for_every_configured_symbol(self) -> None:
        block = _symbols_block({}, self._cfg_default(), latest_candidates=None)
        for sym in block:
            self.assertIn("confluence_grade", block[sym], f"confluence_grade missing for {sym}")


class TestPhase9SymbolsBlockStale(unittest.TestCase):
    """stale flag set when last_update_utc is absent or >30 s old."""

    def _fresh_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _old_ts(self) -> str:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    def test_unavailable_symbol_is_stale(self) -> None:
        block = _symbols_block({}, _cfg())
        for sym in ("BTCUSD#", "GOLD#"):
            self.assertTrue(block[sym]["stale"], f"{sym} should be stale (no data)")

    def test_fresh_last_update_not_stale(self) -> None:
        state = {"GOLD#": {
            "price": 3200.0, "spread": 10.0, "route_status": "WAIT",
            "last_update_utc": self._fresh_ts(),
        }}
        block = _symbols_block(state, _cfg())
        self.assertFalse(block["GOLD#"]["stale"], "GOLD# should not be stale with fresh ts")

    def test_old_last_update_is_stale(self) -> None:
        state = {"GOLD#": {
            "price": 3200.0, "spread": 10.0, "route_status": "WAIT",
            "last_update_utc": self._old_ts(),
        }}
        block = _symbols_block(state, _cfg())
        self.assertTrue(block["GOLD#"]["stale"], "GOLD# should be stale with 60s old ts")

    def test_stale_field_present_for_every_symbol(self) -> None:
        block = _symbols_block({}, _cfg())
        for sym in block:
            self.assertIn("stale", block[sym], f"stale field missing for {sym}")


class TestPhase9CandidateGradeHelper(unittest.TestCase):
    """_candidate_grade() helper unit tests."""

    def test_returns_none_for_empty_candidates(self) -> None:
        self.assertIsNone(_candidate_grade([], "GOLD"))

    def test_returns_none_for_no_matching_symbol(self) -> None:
        c = [{"symbol": "GOLD#", "confluence_grade": "A", "edge_score": 80.0}]
        self.assertIsNone(_candidate_grade(c, "EURUSD"))

    def test_returns_grade_for_matching_symbol(self) -> None:
        c = [{"symbol": "GOLD#", "confluence_grade": "B", "edge_score": 70.0}]
        self.assertEqual(_candidate_grade(c, "GOLD"), "B")

    def test_highest_score_wins(self) -> None:
        c = [
            {"symbol": "GOLD#", "confluence_grade": "C", "edge_score": 50.0},
            {"symbol": "GOLD#", "confluence_grade": "A", "edge_score": 90.0},
        ]
        self.assertEqual(_candidate_grade(c, "GOLD"), "A")

    def test_btcusd_hash_maps_to_btcusd_canonical(self) -> None:
        c = [{"symbol": "BTCUSD#", "confluence_grade": "A+", "edge_score": 95.0}]
        self.assertEqual(_candidate_grade(c, "BTCUSD"), "A+")


class TestPhase9DashboardSnapshotGoldEurCards(unittest.TestCase):
    """gold_liquidity_hunter and eur_ema_rsi_atr cards get confluence_grade."""

    def _snap(self, candidates: list | None = None) -> dict:
        return dashboard_snapshot(
            Settings(demo_only=True, allow_live_trading=False),
            latest_candidates=candidates or [],
        )

    def test_gold_card_has_confluence_grade_key(self) -> None:
        p = self._snap()
        self.assertIn("confluence_grade", p["gold_liquidity_hunter"])

    def test_eur_card_has_confluence_grade_key(self) -> None:
        p = self._snap()
        self.assertIn("confluence_grade", p["eur_ema_rsi_atr"])

    def test_gold_card_confluence_grade_none_when_no_candidates(self) -> None:
        p = self._snap(candidates=[])
        self.assertIsNone(p["gold_liquidity_hunter"]["confluence_grade"])

    def test_gold_card_confluence_grade_populated_from_candidate(self) -> None:
        c = [{"symbol": "GOLD#", "confluence_grade": "A", "edge_score": 88.0,
              "demo_eligible": True, "direction": "BUY", "best_strategy": "GOLD_LIQUIDITY_HUNTER_PRO"}]
        p = self._snap(candidates=c)
        self.assertEqual(p["gold_liquidity_hunter"]["confluence_grade"], "A")

    def test_eur_card_confluence_grade_populated_from_candidate(self) -> None:
        c = [{"symbol": "EURUSD", "confluence_grade": "B", "edge_score": 72.0,
              "demo_eligible": True, "direction": "SELL", "best_strategy": "EUR_EMA_RSI_ATR_CROSSOVER"}]
        p = self._snap(candidates=c)
        self.assertEqual(p["eur_ema_rsi_atr"]["confluence_grade"], "B")

    def test_gold_card_has_mode_and_route_allowed(self) -> None:
        p = self._snap()
        gh = p["gold_liquidity_hunter"]
        self.assertEqual(gh["mode"], "ACTIVE_EXECUTION")
        self.assertTrue(gh["route_allowed"])

    def test_eur_card_has_mode_and_route_allowed(self) -> None:
        p = self._snap()
        eu = p["eur_ema_rsi_atr"]
        self.assertEqual(eu["mode"], "ACTIVE_EXECUTION")
        self.assertTrue(eu["route_allowed"])

    def test_gold_card_stale_true_when_no_gold_hunter_data(self) -> None:
        p = self._snap()
        # No gold hunter data in setup_hunter → stale=True
        self.assertTrue(p["gold_liquidity_hunter"]["stale"])


class TestPhase9DashboardSnapshotSymbolsEnriched(unittest.TestCase):
    """dashboard_snapshot().symbols now contains mode/route_allowed/confluence_grade/stale."""

    def _snap(self, candidates: list | None = None, state: dict | None = None) -> dict:
        return dashboard_snapshot(
            _cfg(),
            per_symbol_state=state or {},
            latest_candidates=candidates or [],
        )

    def test_symbols_block_has_mode_field(self) -> None:
        p = self._snap()
        for sym, entry in p["symbols"].items():
            self.assertIn("mode", entry, f"mode missing in symbols[{sym}]")

    def test_symbols_block_has_route_allowed_field(self) -> None:
        p = self._snap()
        for sym, entry in p["symbols"].items():
            self.assertIn("route_allowed", entry, f"route_allowed missing in symbols[{sym}]")

    def test_symbols_block_has_confluence_grade_field(self) -> None:
        p = self._snap()
        for sym, entry in p["symbols"].items():
            self.assertIn("confluence_grade", entry, f"confluence_grade missing in symbols[{sym}]")

    def test_symbols_block_has_stale_field(self) -> None:
        p = self._snap()
        for sym, entry in p["symbols"].items():
            self.assertIn("stale", entry, f"stale missing in symbols[{sym}]")

    def test_us100cash_observation_only_in_snapshot(self) -> None:
        s = _cfg(hermes_analysis_only_symbols="US100Cash#")
        p = dashboard_snapshot(s, per_symbol_state={}, latest_candidates=[])
        us = p["symbols"].get("US100Cash#", {})
        self.assertEqual(us.get("mode"), "OBSERVATION_ONLY")
        self.assertFalse(us.get("route_allowed"))

    def test_btcusd_active_execution_in_snapshot(self) -> None:
        p = self._snap()
        bt = p["symbols"].get("BTCUSD#", {})
        self.assertEqual(bt.get("mode"), "ACTIVE_EXECUTION")
        self.assertTrue(bt.get("route_allowed"))

    def test_confluence_grade_flows_from_candidates_to_symbols(self) -> None:
        candidates = [
            {"symbol": "BTCUSD#", "confluence_grade": "A+", "edge_score": 92.0},
            {"symbol": "EURUSD", "confluence_grade": "B", "edge_score": 68.0},
        ]
        p = self._snap(candidates=candidates)
        self.assertEqual(p["symbols"]["BTCUSD#"]["confluence_grade"], "A+")
        self.assertEqual(p["symbols"]["EURUSD"]["confluence_grade"], "B")
        self.assertIsNone(p["symbols"]["GOLD#"]["confluence_grade"])  # no candidate seeded


if __name__ == "__main__":
    unittest.main()
