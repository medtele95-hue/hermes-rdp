"""BLOC 4 — Exit V2 active + death of the QUICK_EXIT parasite on GOLD.

Proves:
- GOLD exits are handled exclusively by Exit V2 (parasite TP $1.50 / lock
  $0.80 / trailing / dynamic exit fully skipped on GOLD).
- Exit V2 is CLOSE-BASED: no SL-modify path exists by construction.
- BE arms at +$2.00 (floor +$0.10), trailing keeps $1.20 below the peak.
- Money TP disabled (tp_usd=0) — a +$1.60 winner is NOT harvested.
- Account gate: DEMO executes, non-ACTIVE mode shadows.
- Fail-closed: exceptions leave SL/TP untouched.
"""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.services.exit_v2 as exit_v2_module
from app.config import Settings
from app.mt5.demo_router import DemoKellyRouter
from app.services.exit_v2 import ExitV2Config, evaluate_exit_v2, is_gold_symbol


def _pos(ticket=111, symbol="GOLD#", profit=0.0, side="BUY", magic=909002):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        profit=profit,
        magic=magic,
        comment="HERMES_DEMO",
        type=0 if side == "BUY" else 1,
        volume=0.01,
        price_open=3300.0,
        sl=3290.0,
        tp=0.0,
        time=0,
    )


class TestExitV2Engine(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = ExitV2Config()
        self.state: dict = {}

    def _eval(self, profit: float, ticket: int = 111) -> dict:
        return evaluate_exit_v2(_pos(ticket=ticket, profit=profit), None, None, self.cfg, self.state)

    def test_small_profit_holds_no_parasite_tp(self) -> None:
        # +$1.60 would have been harvested by the parasite (TP $1.50) — must HOLD
        action = self._eval(1.60)
        self.assertEqual(action["action"], "NONE")
        self.assertFalse(action["be_armed"])

    def test_be_arms_at_2_usd_and_floor_never_below_0_10(self) -> None:
        self.assertEqual(self._eval(2.10)["action"], "NONE")   # armed, holding
        self.assertTrue(self.state[111]["be_armed"])
        action = self._eval(0.05)                               # fell below every floor
        self.assertEqual(action["action"], "CLOSE")
        self.assertGreaterEqual(action["floor_usd"], self.cfg.be_floor_usd)

    def test_be_floor_dominates_when_trailing_would_be_lower(self) -> None:
        # wide trail gap: trailing floor would be negative — the BE floor
        # (+0.10) is the hard minimum lock once armed
        cfg = ExitV2Config(trail_gap_usd=5.0)
        state: dict = {}
        evaluate_exit_v2(_pos(profit=2.50), None, None, cfg, state)  # arm BE, peak 2.5
        action = evaluate_exit_v2(_pos(profit=0.05), None, None, cfg, state)
        self.assertEqual(action["action"], "CLOSE")
        self.assertEqual(action["reason"], "EXIT_V2_BE_FLOOR")
        self.assertAlmostEqual(action["floor_usd"], 0.10)

    def test_trailing_keeps_1_20_below_peak(self) -> None:
        self._eval(5.00)                                        # peak 5.0
        self.assertEqual(self._eval(4.00)["action"], "NONE")    # above 3.8 floor
        action = self._eval(3.75)
        self.assertEqual(action["action"], "CLOSE")
        self.assertEqual(action["reason"], "EXIT_V2_TRAIL_FLOOR")
        self.assertAlmostEqual(action["floor_usd"], 3.80)

    def test_winner_runs_with_tp_disabled(self) -> None:
        for profit in (1.0, 3.0, 6.0, 12.0, 25.0):
            self.assertEqual(self._eval(profit)["action"], "NONE")

    def test_money_tp_fires_only_when_configured(self) -> None:
        cfg = ExitV2Config(tp_usd=8.0)
        state: dict = {}
        action = evaluate_exit_v2(_pos(profit=9.0), None, None, cfg, state)
        self.assertEqual(action["action"], "CLOSE")
        self.assertEqual(action["reason"], "EXIT_V2_TP")

    def test_close_based_by_construction_no_sl_modify(self) -> None:
        src = inspect.getsource(exit_v2_module)
        self.assertNotIn("TRADE_ACTION_SLTP", src)
        self.assertNotIn("order_send", src)
        self.assertNotIn("MOVE_BREAKEVEN", src)
        self.assertNotIn("TRAIL_SL", src)

    def test_is_gold_symbol(self) -> None:
        for sym in ("GOLD#", "GOLD", "XAUUSD", "gold#"):
            self.assertTrue(is_gold_symbol(sym))
        for sym in ("BTCUSD#", "EURUSD", ""):
            self.assertFalse(is_gold_symbol(sym))


class TestRouterGoldAuthority(unittest.TestCase):
    def _router(self, name: str, **overrides) -> DemoKellyRouter:
        settings = Settings(
            demo_trading=True,
            demo_only=True,
            demo_pilot_enabled=True,
            quick_exit_enabled=True,
            allow_live_trading=False,
            hermes_dynamic_exit_enabled=False,
            **overrides,
        )
        events = Path("tests") / "__tmp_bloc4_events" / f"{name}.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        if events.exists():
            events.unlink()
        return DemoKellyRouter(settings, events_path=events)

    def _account(self) -> dict:
        return {"login": 345297734, "trade_mode": 0, "trade_allowed": True, "trade_expert": True}

    def _run(self, router: DemoKellyRouter, positions: list, send_result=None):
        fake_send = send_result or SimpleNamespace(retcode=10009, order=999, deal=42)
        tick = SimpleNamespace(bid=3300.0, ask=3300.3)
        info = SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01, digits=2, point=0.01)
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=positions),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=info),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_send) as send,
        ):
            items = router.process_quick_exits(account=self._account(), mt5_connected=True)
        return items, send

    def test_gold_parasite_tp_150_no_longer_fires(self) -> None:
        router = self._router("parasite_dead")
        # +$1.60 profit: the parasite would close (TP $1.50) — Exit V2 must HOLD
        items, send = self._run(router, [_pos(profit=1.60)])
        send.assert_not_called()
        self.assertFalse(any(item["data"].get("event_type") == "QUICK_EXIT_CLOSED" for item in items))

    def test_gold_trailing_close_executes_on_demo(self) -> None:
        router = self._router("trail_close")
        pos = _pos(profit=5.0)
        self._run(router, [pos])            # cycle 1: peak 5.0
        pos.profit = 3.5                     # cycle 2: below 3.8 floor
        items, send = self._run(router, [pos])
        send.assert_called_once()
        events = [item["data"] for item in items]
        self.assertTrue(any(e.get("event_type") == "EXIT_V2_CLOSE" for e in events))
        close_event = next(e for e in events if e.get("event_type") == "EXIT_V2_CLOSE")
        self.assertEqual(close_event.get("exit_v2_reason"), "EXIT_V2_TRAIL_FLOOR")

    def test_shadow_mode_never_sends_orders(self) -> None:
        router = self._router("shadow", exit_v2_mode="SHADOW")
        pos = _pos(profit=5.0)
        self._run(router, [pos])
        pos.profit = 3.5
        items, send = self._run(router, [pos])
        send.assert_not_called()
        self.assertTrue(any(item["data"].get("event_type") == "EXIT_V2_SHADOW" for item in items))

    def test_no_sl_modify_request_ever_emitted_for_gold(self) -> None:
        router = self._router("no_sl_modify")
        pos = _pos(profit=2.5)  # parasite would MOVE_BREAKEVEN at lock $0.80
        items, send = self._run(router, [pos])
        send.assert_not_called()

    def test_btc_positions_route_to_exit_v2(self) -> None:
        # GRAND_PLAN 2026-07-08 (décision SIMO, deux symboles officiels) :
        # Exit V2 est l'autorité de sortie unique pour BTCUSD# aussi. Le
        # QUICK_EXIT parasite (TP money $4) ne touche plus JAMAIS une position
        # BTC : à +$5.00 Exit V2 arme le BE et HOLD (le gagnant court), là où
        # le parasite aurait fermé ; puis le trailing floor Exit V2 ferme.
        router = self._router("btc_exit_v2")
        pos = _pos(symbol="BTCUSD#", profit=5.0)
        pos.price_open = 3295.0
        items, send = self._run(router, [pos])   # cycle 1: peak 5.0 -> HOLD
        send.assert_not_called()                  # le parasite aurait fermé ici
        self.assertFalse(any(str(item["data"].get("event_type", "")).startswith("QUICK_EXIT") for item in items))
        pos.profit = 3.5                          # cycle 2: sous le floor 3.8
        items, send = self._run(router, [pos])
        send.assert_called_once()
        events = [item["data"] for item in items]
        close_event = next(e for e in events if e.get("event_type") == "EXIT_V2_CLOSE")
        self.assertEqual(close_event.get("exit_v2_reason"), "EXIT_V2_TRAIL_FLOOR")

    def test_eurusd_positions_keep_legacy_quick_exit(self) -> None:
        # Hors allowlist (position résiduelle éventuelle) : le legacy engine
        # reste le gestionnaire — Exit V2 est réservé aux symboles officiels.
        router = self._router("eur_legacy")
        pos = _pos(symbol="EURUSD", profit=5.0)
        pos.price_open = 3295.0
        items, send = self._run(router, [pos])
        send.assert_called_once()
        self.assertFalse(any(str(item["data"].get("event_type", "")).startswith("EXIT_V2") for item in items))

    def test_fail_closed_on_engine_exception(self) -> None:
        router = self._router("fail_closed")
        pos = _pos(profit=5.0)
        with patch("app.mt5.demo_router.evaluate_exit_v2", side_effect=RuntimeError("boom")):
            items, send = self._run(router, [pos])
        send.assert_not_called()  # SL/TP untouched, no close attempted


if __name__ == "__main__":
    unittest.main()
