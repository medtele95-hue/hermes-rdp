"""Tests for the /local-api/trades live MT5 position sync fix.

Verifies:
  1. Position with magic 909002 appears in /trades open_trades
  2. Position with comment containing HERMES appears (e.g. HERMES_DEMO_KELL)
  3. Non-HERMES position is hidden from the journal
  4. Debug endpoint returns all MT5 positions with visibility reasons
  5. Frontend-compatible Trade fields are present (symbol, ticket, direction…)
  6. No live trading enabled anywhere
  7. mt5.order_send is not called from server.py
"""
from __future__ import annotations

import re
import sys
import types
import unittest
from collections import namedtuple
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SERVER_PATH = ROOT / "app" / "local_api" / "server.py"

# ── Fake MT5 NamedTuple (mirrors MetaTrader5.TradePosition) ──────────────────

_TradePosition = namedtuple(
    "_TradePosition",
    ["ticket", "symbol", "type", "volume", "price_open",
     "sl", "tp", "profit", "magic", "comment", "time"],
)

_HistoryDeal = namedtuple(
    "_HistoryDeal",
    ["ticket", "order", "position_id", "symbol", "type", "entry", "price",
     "volume", "profit", "commission", "swap", "magic", "comment", "time"],
)


def _fake_position(
    ticket: int = 346422452,
    symbol: str = "BTCUSD#",
    pos_type: int = 0,          # 0 = BUY
    volume: float = 0.01,
    price_open: float = 66626.4,
    sl: float = 64529.42,
    tp: float = 66796.8,
    profit: float = 0.22,
    magic: int = 909002,
    comment: str = "HERMES_DEMO_KELL",
    time: int = 1718400000,     # arbitrary epoch
) -> _TradePosition:
    return _TradePosition(
        ticket=ticket, symbol=symbol, type=pos_type, volume=volume,
        price_open=price_open, sl=sl, tp=tp, profit=profit,
        magic=magic, comment=comment, time=time,
    )


def _fake_deal(
    *,
    ticket: int,
    order: int | None = None,
    position_id: int = 700001,
    symbol: str = "BTCUSD#",
    deal_type: int = 0,
    entry: int = 0,
    price: float = 65000.0,
    volume: float = 0.01,
    profit: float = 0.0,
    commission: float = 0.0,
    swap: float = 0.0,
    magic: int = 909002,
    comment: str = "HERMES_DEMO_KELL",
    time: int = 1718400000,
) -> _HistoryDeal:
    return _HistoryDeal(
        ticket=ticket,
        order=order if order is not None else ticket,
        position_id=position_id,
        symbol=symbol,
        type=deal_type,
        entry=entry,
        price=price,
        volume=volume,
        profit=profit,
        commission=commission,
        swap=swap,
        magic=magic,
        comment=comment,
        time=time,
    )


# ── Import helpers under test ─────────────────────────────────────────────────

def _import_server_helpers():
    """Import _live_hermes_open_positions and _pos_to_trade from server.py."""
    # Stub heavy dependencies before import
    for mod in ("MetaTrader5",):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    # Ensure positions_get exists on the stub
    mt5_stub = sys.modules["MetaTrader5"]
    if not hasattr(mt5_stub, "positions_get"):
        mt5_stub.positions_get = lambda: []  # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "history_deals_get"):
        mt5_stub.history_deals_get = lambda start, end: []  # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "last_error"):
        mt5_stub.last_error = lambda: (1, "Success")  # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "POSITION_TYPE_BUY"):
        mt5_stub.POSITION_TYPE_BUY = 0       # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "POSITION_TYPE_SELL"):
        mt5_stub.POSITION_TYPE_SELL = 1      # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "DEAL_TYPE_BUY"):
        mt5_stub.DEAL_TYPE_BUY = 0           # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "DEAL_TYPE_SELL"):
        mt5_stub.DEAL_TYPE_SELL = 1          # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "DEAL_ENTRY_IN"):
        mt5_stub.DEAL_ENTRY_IN = 0           # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "DEAL_ENTRY_OUT"):
        mt5_stub.DEAL_ENTRY_OUT = 1          # type: ignore[attr-defined]
    if not hasattr(mt5_stub, "DEAL_ENTRY_OUT_BY"):
        mt5_stub.DEAL_ENTRY_OUT_BY = 3       # type: ignore[attr-defined]

    from app.local_api import server  # noqa: PLC0415
    return server


# ── 1. _pos_to_trade shape ───────────────────────────────────────────────────

class TestPosToTradeShape(unittest.TestCase):

    def setUp(self) -> None:
        self.server = _import_server_helpers()

    def _trade(self, **kw) -> dict:
        pos = _fake_position(**kw)
        return self.server._pos_to_trade(dict(pos._asdict()), 909002)

    def test_ticket_preserved(self) -> None:
        t = self._trade(ticket=346422452)
        self.assertEqual(t["ticket"], 346422452)

    def test_symbol_canonicalised(self) -> None:
        t = self._trade(symbol="BTCUSD#")
        self.assertEqual(t["symbol"], "BTCUSD#")

    def test_buy_direction(self) -> None:
        t = self._trade(pos_type=0)
        self.assertEqual(t["direction"], "BUY")

    def test_sell_direction(self) -> None:
        t = self._trade(pos_type=1)
        self.assertEqual(t["direction"], "SELL")

    def test_entry_price(self) -> None:
        t = self._trade(price_open=66626.4)
        self.assertAlmostEqual(t["entry"], 66626.4)

    def test_sl_present(self) -> None:
        t = self._trade(sl=64529.42)
        self.assertIsNotNone(t["sl"])
        self.assertAlmostEqual(t["sl"], 64529.42)

    def test_tp_present(self) -> None:
        t = self._trade(tp=66796.8)
        self.assertIsNotNone(t["tp"])
        self.assertAlmostEqual(t["tp"], 66796.8)

    def test_pnl_present(self) -> None:
        t = self._trade(profit=0.22)
        self.assertAlmostEqual(t["pnl"], 0.22)

    def test_lot_present(self) -> None:
        t = self._trade(volume=0.01)
        self.assertAlmostEqual(t["lot"], 0.01)

    def test_magic_preserved(self) -> None:
        t = self._trade(magic=909002)
        self.assertEqual(t["magic"], 909002)

    def test_comment_preserved(self) -> None:
        t = self._trade(comment="HERMES_DEMO_KELL")
        self.assertEqual(t["comment"], "HERMES_DEMO_KELL")

    def test_source_is_mt5_live(self) -> None:
        t = self._trade()
        self.assertEqual(t["source"], "MT5_LIVE")

    def test_timestamp_iso_format(self) -> None:
        t = self._trade(time=1718400000)
        self.assertIsNotNone(t["timestamp"])
        self.assertIn("T", t["timestamp"])

    def test_zero_sl_returns_none(self) -> None:
        t = self._trade(sl=0.0)
        self.assertIsNone(t["sl"])

    def test_zero_tp_returns_none(self) -> None:
        t = self._trade(tp=0.0)
        self.assertIsNone(t["tp"])

    def test_strategy_derived_from_kell_comment(self) -> None:
        t = self._trade(comment="HERMES_DEMO_KELL")
        self.assertEqual(t["strategy"], "HERMES_DEMO")

    def test_strategy_derived_from_btc_scalping_comment(self) -> None:
        t = self._trade(comment="BTC_SCALPING_AGENT_DEMO")
        self.assertEqual(t["strategy"], "BTC_SCALPING_AGENT")

    def test_strategy_derived_from_order_flow_comment(self) -> None:
        t = self._trade(comment="ORDER_FLOW_EXECUTION_AGENT")
        self.assertEqual(t["strategy"], "ORDER_FLOW_EXECUTION_AGENT")

    def test_match_reason_magic_only(self) -> None:
        t = self._trade(magic=909002, comment="other")
        self.assertEqual(t["reason"], "MAGIC_909002")

    def test_match_reason_both(self) -> None:
        t = self._trade(magic=909002, comment="HERMES_DEMO_KELL")
        self.assertEqual(t["reason"], "MAGIC_909002_AND_HERMES_COMMENT")

    def test_match_reason_comment_only(self) -> None:
        t = self._trade(magic=0, comment="HERMES_SOME_OTHER")
        self.assertEqual(t["reason"], "HERMES_COMMENT")


# ── 2. _live_hermes_open_positions matching logic ────────────────────────────

class TestLiveHermesOpenPositions(unittest.TestCase):

    def setUp(self) -> None:
        self.server = _import_server_helpers()

    def _run(self, positions: list) -> list:
        with patch.object(self.server.mt5, "positions_get", return_value=positions):
            with patch.object(self.server, "get_settings") as mock_cfg:
                mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                return self.server._live_hermes_open_positions()

    def test_magic_909002_position_included(self) -> None:
        pos = _fake_position(magic=909002, comment="HERMES_DEMO_KELL")
        result = self._run([pos])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticket"], 346422452)

    def test_hermes_in_comment_included_even_if_magic_differs(self) -> None:
        pos = _fake_position(magic=0, comment="HERMES_DEMO_KELL")
        result = self._run([pos])
        self.assertEqual(len(result), 1)

    def test_hermes_demo_kell_comment_matches(self) -> None:
        """The exact reported position comment must match."""
        pos = _fake_position(magic=909002, comment="HERMES_DEMO_KELL")
        result = self._run([pos])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["comment"], "HERMES_DEMO_KELL")

    def test_non_hermes_position_excluded(self) -> None:
        pos = _fake_position(magic=999999, comment="SOME_OTHER_EA")
        result = self._run([pos])
        self.assertEqual(result, [])

    def test_multiple_positions_mixed(self) -> None:
        hermes = _fake_position(ticket=111, magic=909002, comment="HERMES_DEMO_KELL")
        non_hermes = _fake_position(ticket=222, magic=1234, comment="EA_OTHER")
        result = self._run([hermes, non_hermes])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticket"], 111)

    def test_mt5_unavailable_returns_empty(self) -> None:
        with patch.object(self.server.mt5, "positions_get", side_effect=Exception("MT5 not connected")):
            with patch.object(self.server, "get_settings") as mock_cfg:
                mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                result = self.server._live_hermes_open_positions()
        self.assertEqual(result, [])

    def test_mt5_returns_none_returns_empty(self) -> None:
        with patch.object(self.server.mt5, "positions_get", return_value=None):
            with patch.object(self.server, "get_settings") as mock_cfg:
                mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                result = self.server._live_hermes_open_positions()
        self.assertEqual(result, [])

    def test_exact_ticket_346422452_matches(self) -> None:
        """Proof test: the actual reported open position appears."""
        pos = _fake_position(
            ticket=346422452,
            symbol="BTCUSD#",
            pos_type=0,
            volume=0.01,
            price_open=66626.4,
            sl=64529.42,
            tp=66796.8,
            profit=0.22,
            magic=909002,
            comment="HERMES_DEMO_KELL",
        )
        result = self._run([pos])
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertEqual(t["ticket"], 346422452)
        self.assertEqual(t["symbol"], "BTCUSD#")
        self.assertEqual(t["direction"], "BUY")
        self.assertAlmostEqual(t["entry"], 66626.4)
        self.assertAlmostEqual(t["sl"], 64529.42)
        self.assertAlmostEqual(t["tp"], 66796.8)
        self.assertAlmostEqual(t["pnl"], 0.22)
        self.assertAlmostEqual(t["lot"], 0.01)
        self.assertEqual(t["magic"], 909002)
        self.assertEqual(t["comment"], "HERMES_DEMO_KELL")


# ── 3. Debug endpoint positions structure ─────────────────────────────────────

class TestMt5OpenPositionsDebug(unittest.TestCase):

    def setUp(self) -> None:
        self.server = _import_server_helpers()

    def _call_debug(self, positions: list) -> dict:
        with patch.object(self.server.mt5, "positions_get", return_value=positions):
            with patch.object(self.server, "get_settings") as mock_cfg:
                mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                resp = self.server.mt5_open_positions_debug()
        import json
        body = json.loads(resp.body)
        return body["data"]

    def test_debug_shows_hermes_position_as_visible(self) -> None:
        pos = _fake_position(magic=909002, comment="HERMES_DEMO_KELL")
        data = self._call_debug([pos])
        self.assertEqual(data["dashboard_open_count"], 1)
        row = data["positions"][0]
        self.assertTrue(row["is_hermes_demo"])
        self.assertIn("VISIBLE", row["why_visible_or_hidden"])

    def test_debug_shows_non_hermes_position_as_hidden(self) -> None:
        pos = _fake_position(magic=999999, comment="OTHER_EA")
        data = self._call_debug([pos])
        self.assertEqual(data["dashboard_open_count"], 0)
        row = data["positions"][0]
        self.assertFalse(row["is_hermes_demo"])
        self.assertIn("HIDDEN", row["why_visible_or_hidden"])

    def test_debug_shows_all_positions_including_non_hermes(self) -> None:
        hermes = _fake_position(ticket=111, magic=909002, comment="HERMES_DEMO_KELL")
        other = _fake_position(ticket=222, magic=1234, comment="SOME_EA")
        data = self._call_debug([hermes, other])
        self.assertEqual(data["mt5_open_positions_total"], 2)
        self.assertEqual(data["dashboard_open_count"], 1)
        self.assertEqual(len(data["positions"]), 2)

    def test_debug_includes_hide_reason_with_magic_and_comment(self) -> None:
        pos = _fake_position(magic=1111, comment="EA_CLASSIC")
        data = self._call_debug([pos])
        row = data["positions"][0]
        self.assertIn("1111", row["why_visible_or_hidden"])
        self.assertIn("909002", row["why_visible_or_hidden"])

    def test_debug_returns_hermes_magic_number(self) -> None:
        data = self._call_debug([])
        self.assertEqual(data["hermes_magic_number"], 909002)

    def test_debug_mt5_unavailable_returns_ok_with_error(self) -> None:
        with patch.object(self.server.mt5, "positions_get", side_effect=Exception("no MT5")):
            with patch.object(self.server, "get_settings") as mock_cfg:
                mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                resp = self.server.mt5_open_positions_debug()
        import json
        body = json.loads(resp.body)
        data = body["data"]
        self.assertFalse(data["mt5_available"])
        self.assertEqual(data["dashboard_open_count"], 0)


# ── 4. /trades endpoint integrates live positions ─────────────────────────────

class TestTradesEndpoint(unittest.TestCase):

    def setUp(self) -> None:
        self.server = _import_server_helpers()

    def _call_trades(
        self,
        positions: list,
        events: list | None = None,
        deals: list | None = None,
        history_unavailable: bool = True,
        position_sync: dict | None = None,
    ) -> dict:
        mock_state = MagicMock()
        mock_state.get_demo_events.return_value = events or []
        mock_state.get_position_sync.return_value = position_sync or {}

        with patch.object(self.server, "get_local_state", return_value=mock_state):
            with patch.object(self.server.mt5, "positions_get", return_value=positions):
                history_value = None if history_unavailable else (deals or [])
                with patch.object(self.server.mt5, "history_deals_get", return_value=history_value, create=True):
                    with patch.object(self.server, "get_settings") as mock_cfg:
                        mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                        resp = self.server.trades()

        import json
        body = json.loads(resp.body)
        return body["data"]

    def test_open_trades_populated_from_live_mt5(self) -> None:
        pos = _fake_position(magic=909002, comment="HERMES_DEMO_KELL")
        data = self._call_trades([pos])
        self.assertEqual(len(data["open_trades"]), 1)
        self.assertEqual(data["open_count"], 1)

    def test_ticket_346422452_appears_in_open_trades(self) -> None:
        pos = _fake_position(ticket=346422452, magic=909002, comment="HERMES_DEMO_KELL")
        data = self._call_trades([pos])
        tickets = [t["ticket"] for t in data["open_trades"]]
        self.assertIn(346422452, tickets)

    def test_non_hermes_position_not_in_open_trades(self) -> None:
        pos = _fake_position(magic=12345, comment="EA_OTHER")
        data = self._call_trades([pos])
        self.assertEqual(data["open_trades"], [])

    def test_hermes_comment_only_position_appears(self) -> None:
        pos = _fake_position(magic=0, comment="HERMES_DEMO_KELL")
        data = self._call_trades([pos])
        self.assertEqual(len(data["open_trades"]), 1)

    def test_closed_trades_from_events(self) -> None:
        ev = {
            "event_type": "DEMO_CLOSE", "action": "CLOSE",
            "symbol": "BTCUSD#", "ticket": 111111,
            "magic_number": 909002, "direction": "BUY",
            "entry": 65000.0, "sl": 64000.0, "tp": 66000.0,
            "lot": 0.01, "pnl": 12.5, "exit_price": 66000.0,
            "timestamp": "2026-06-15T10:00:00Z",
        }
        data = self._call_trades([], events=[ev])
        self.assertEqual(len(data["closed_trades"]), 1)
        self.assertEqual(data["closed_trades"][0]["ticket"], 111111)
        self.assertEqual(data["closed_source"], "FALLBACK_MEMORY")
        self.assertTrue(data["fallback_used"])

    def test_closed_trades_from_mt5_history_deals(self) -> None:
        deals = [
            _fake_deal(ticket=5001, position_id=700001, deal_type=0, entry=0, price=65000.0, profit=0.0),
            _fake_deal(ticket=5002, position_id=700001, deal_type=1, entry=1, price=64950.0, profit=-3.5, commission=-0.1, swap=-0.09, time=1718400300),
        ]
        data = self._call_trades([], deals=deals, history_unavailable=False, position_sync={"pnl_source": "MT5_HISTORY_DEALS"})
        self.assertEqual(data["closed_source"], "MT5_HISTORY_DEALS")
        self.assertEqual(data["pnl_source"], "MT5_HISTORY_DEALS")
        self.assertFalse(data["fallback_used"])
        self.assertEqual(data["mt5_closed_deals_count"], 1)
        row = data["closed_trades"][0]
        self.assertEqual(row["source"], "MT5_HISTORY_DEALS")
        self.assertEqual(row["position_id"], 700001)
        self.assertEqual(row["deal_ticket"], 5002)
        self.assertAlmostEqual(row["pnl"], -3.69)

    def test_trades_closed_rows_from_mt5_history_deals(self) -> None:
        self.test_closed_trades_from_mt5_history_deals()

    def test_non_hermes_history_deals_ignored(self) -> None:
        deals = [
            _fake_deal(ticket=6001, position_id=800001, magic=111111, comment="OTHER_EA", entry=0),
            _fake_deal(ticket=6002, position_id=800001, magic=111111, comment="OTHER_EA", entry=1, profit=10.0),
        ]
        data = self._call_trades([], deals=deals, history_unavailable=False)
        self.assertEqual(data["closed_source"], "MT5_HISTORY_DEALS")
        self.assertEqual(data["closed_trades"], [])
        self.assertEqual(data["mt5_closed_deals_count"], 0)

    def test_trades_closed_non_hermes_deals_ignored(self) -> None:
        self.test_non_hermes_history_deals_ignored()

    def test_fallback_only_when_mt5_history_unavailable(self) -> None:
        ev = {"event_type": "DEMO_CLOSE", "ticket": 123, "symbol": "BTCUSD#", "pnl": 1.0}
        available = self._call_trades([], events=[ev], deals=[], history_unavailable=False)
        self.assertEqual(available["closed_source"], "MT5_HISTORY_DEALS")
        self.assertFalse(available["fallback_used"])
        self.assertEqual(available["closed_trades"], [])

        unavailable = self._call_trades([], events=[ev], history_unavailable=True)
        self.assertEqual(unavailable["closed_source"], "FALLBACK_MEMORY")
        self.assertTrue(unavailable["fallback_used"])
        self.assertEqual(len(unavailable["closed_trades"]), 1)

    def test_trades_closed_fallback_only_when_history_unavailable(self) -> None:
        self.test_fallback_only_when_mt5_history_unavailable()

    def test_open_trades_not_from_open_events(self) -> None:
        """Open positions must come from live MT5, not from OPEN events in the log."""
        ev = {
            "event_type": "DEMO_ORDER", "action": "OPEN",
            "symbol": "BTCUSD#", "ticket": 999999,
            "magic_number": 909002, "direction": "BUY",
        }
        # No live MT5 positions
        data = self._call_trades([], events=[ev])
        # open_trades must be empty because no live MT5 positions
        self.assertEqual(data["open_trades"], [])

    def test_response_has_required_fields(self) -> None:
        data = self._call_trades([])
        self.assertIn("open_trades", data)
        self.assertIn("closed_trades", data)
        self.assertIn("open_count", data)
        self.assertIn("closed_count", data)
        self.assertIn("open_source", data)
        self.assertIn("closed_source", data)
        self.assertIn("pnl_source", data)
        self.assertIn("fallback_used", data)

    def test_open_count_agrees_with_risk_and_account_snapshot(self) -> None:
        pos = _fake_position(ticket=900001, magic=909002, comment="HERMES_DEMO_KELL")
        mock_state = MagicMock()
        mock_state.get_demo_events.return_value = []
        mock_state.get_position_sync.return_value = {
            "hermes_mt5_open_positions_count": 1,
            "mt5_open_positions_count": 1,
            "pnl_source": "MT5_HISTORY_DEALS",
        }
        mock_state.get_account_snapshot.return_value = {
            "balance": 10000.0,
            "equity": 10000.0,
            "open": 1,
        }
        mock_state.get_dashboard_snapshot.return_value = {"risk_exposure": {}}
        mock_state.get_latest_safety_guard.return_value = None
        mock_state.get_settings_snapshot.return_value = {
            "demo_max_lot": 0.01,
            "demo_max_open_trades": 3,
            "demo_max_trades_per_day": 5,
            "demo_max_daily_loss_pct": 1.0,
            "demo_max_risk_per_trade_pct": 0.25,
            "demo_stop_after_consecutive_losses": 3,
        }
        with patch.object(self.server, "get_local_state", return_value=mock_state):
            with patch.object(self.server.mt5, "positions_get", return_value=[pos]):
                with patch.object(self.server.mt5, "history_deals_get", return_value=[], create=True):
                    with patch.object(self.server, "get_settings") as mock_cfg:
                        mock_cfg.return_value = MagicMock(demo_magic_number=909002)
                        import json
                        trades_data = json.loads(self.server.trades().body)["data"]
                        risk_data = json.loads(self.server.risk().body)["data"]
                        account_data = json.loads(self.server.account_snapshot().body)["data"]
        self.assertEqual(trades_data["open_source"], "MT5_LIVE")
        self.assertEqual(trades_data["open_count"], 1)
        self.assertEqual(risk_data["open_positions_count"], 1)
        self.assertEqual(account_data["open_positions_count"], 1)


# ── 5. Source code: server.py uses live MT5 for open positions ────────────────

class TestServerCodeStructure(unittest.TestCase):

    def setUp(self) -> None:
        self.content = SERVER_PATH.read_text(encoding="utf-8")

    def test_server_imports_mt5(self) -> None:
        self.assertIn("import MetaTrader5 as mt5", self.content)

    def test_server_imports_log(self) -> None:
        self.assertIn("from app.logger import log", self.content)

    def test_server_imports_get_settings(self) -> None:
        self.assertIn("from app.config import get_settings", self.content)

    def test_live_hermes_open_positions_defined(self) -> None:
        self.assertIn("def _live_hermes_open_positions(", self.content)

    def test_trades_endpoint_calls_live_positions(self) -> None:
        self.assertIn("_live_hermes_open_positions()", self.content)

    def test_debug_endpoint_defined(self) -> None:
        self.assertIn('"/local-api/mt5-open-positions-debug"', self.content)

    def test_mt5_positions_get_called(self) -> None:
        self.assertIn("mt5.positions_get()", self.content)

    def test_required_log_token_mt5_open_position_found(self) -> None:
        self.assertIn("[MT5_OPEN_POSITION_FOUND]", self.content)

    def test_required_log_token_hermes_open_position_match(self) -> None:
        self.assertIn("[HERMES_OPEN_POSITION_MATCH]", self.content)

    def test_required_log_token_trades_dashboard_open_count(self) -> None:
        self.assertIn("[TRADES_DASHBOARD_OPEN_COUNT]", self.content)

    def test_required_log_tokens_for_closed_history(self) -> None:
        self.assertIn("[TRADES_CLOSED_HISTORY_READ]", self.content)
        self.assertIn("[TRADES_CLOSED_HISTORY_ROW]", self.content)
        self.assertIn("[TRADES_CLOSED_HISTORY_UNAVAILABLE]", self.content)
        self.assertIn("[TRADES_ENDPOINT_SOURCE]", self.content)

    def test_trades_endpoint_uses_mt5_history_deals(self) -> None:
        self.assertIn("mt5.history_deals_get", self.content)
        self.assertIn("MT5_HISTORY_DEALS", self.content)

    def test_no_order_send_in_server(self) -> None:
        self.assertFalse(
            re.search(r"\bmt5\.order_send\s*\(", self.content),
            "server.py must not call mt5.order_send",
        )

    def test_server_is_read_only(self) -> None:
        for method in ('app.post(', 'app.put(', 'app.delete(', 'app.patch('):
            self.assertNotIn(method, self.content, f"server.py must not define {method} routes")


class TestAuditSafetyFrontendSource(unittest.TestCase):

    def test_allow_live_trading_check_is_not_forced_true(self) -> None:
        src = (ROOT / "local_dashboard" / "src" / "pages" / "AuditSafety.tsx").read_text(encoding="utf-8")
        self.assertIn("flags.ALLOW_LIVE_TRADING === false", src)
        self.assertNotIn("flags.ALLOW_LIVE_TRADING === false || true", src)

    def test_audit_safety_live_disabled_check_is_real(self) -> None:
        self.test_allow_live_trading_check_is_not_forced_true()


# ── 6. Safety invariants ─────────────────────────────────────────────────────

class TestSafetyInvariants(unittest.TestCase):

    def setUp(self) -> None:
        self._app_dir = ROOT / "app"

    def test_allow_live_trading_false_in_config_default(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.allow_live_trading)

    def test_demo_only_true_in_config_default(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertTrue(s.demo_only)

    def test_demo_max_lot_is_micro(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertLessEqual(s.demo_max_lot, 0.01)

    def test_demo_magic_number_is_909002(self) -> None:
        from app.config import Settings
        s = Settings()
        self.assertEqual(s.demo_magic_number, 909002)

    def test_order_send_only_in_demo_router(self) -> None:
        call_pattern = re.compile(r"\bmt5\.order_send\s*\(")
        violators = []
        for py_file in self._app_dir.rglob("*.py"):
            rel = str(py_file.relative_to(ROOT))
            if rel in ("app\\mt5\\demo_router.py", "app/mt5/demo_router.py"):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            if call_pattern.search(content):
                violators.append(rel)
        self.assertEqual(violators, [], f"mt5.order_send call outside DemoRouter: {violators}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
