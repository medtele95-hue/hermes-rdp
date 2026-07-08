"""INVARIANTS HISTORIQUES ABSOLUS — preuve permanente (mission FIX_BTC 2026-07-07).

Ces tests sont le contrat : ils prouvent qu'AUCUN chemin de code ne peut
envoyer un ordre sur un symbole hors SYMBOL_ALLOWLIST (=GOLD#), même avec
un candidat parfait qui a déjà franchi toutes les gates amont — le verrou
vit au choke-point, juste avant mt5.order_send. Ils prouvent aussi les
quatre autres invariants : MAX_OPEN_TRADES_PER_SYMBOL=1, lot figé 0.01,
magic 909002 sur tout ordre, SL/TP obligatoires (jamais d'ordre nu).

NE PAS affaiblir ces tests. Si l'un d'eux casse, c'est que l'invariant
GOLD-only a été perdu — le restaurer, pas adapter le test.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.mt5.demo_router import (
    LOT_HARD_CAP,
    MAGIC_HARD,
    SYMBOL_ALLOWLIST,
    _execution_invariants_block,
)


def _request(**overrides) -> dict:
    payload = {
        "action": 1,
        "symbol": "GOLD#",
        "volume": 0.01,
        "type": 1,
        "price": 4000.0,
        "sl": 4010.0,
        "tp": 3980.0,
        "magic": 909002,
        "comment": "HERMES_DEMO_KELLY_24H",
    }
    payload.update(overrides)
    return payload


class SymbolAllowlistInvariantTests(unittest.TestCase):
    """Invariant 1 — GOLD-ONLY : allowlist stricte au choke-point."""

    def test_allowlist_is_gold_hash_only(self) -> None:
        self.assertEqual(tuple(SYMBOL_ALLOWLIST), ("GOLD#",))

    def test_btcusd_perfect_candidate_is_blocked(self) -> None:
        request = _request(symbol="BTCUSD#", price=63000.0, sl=63500.0, tp=62000.0)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            with self.assertLogs("hermes", level="WARNING") as captured:
                reason = _execution_invariants_block(request, "BTC_SCALPING_AGENT")
        self.assertEqual(reason, "SYMBOL_BLOCKED")
        self.assertTrue(any("[SYMBOL_BLOCKED]" in line and "BTCUSD#" in line for line in captured.output))

    def test_every_non_gold_symbol_is_blocked(self) -> None:
        for symbol in ("BTCUSD", "BTCUSD#", "EURUSD", "US100Cash#", "US100", "NAS100", "USTEC", "GOLD", "XAUUSD", "GOLDCASH#", "", None):
            with self.subTest(symbol=symbol):
                with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
                    reason = _execution_invariants_block(_request(symbol=symbol), "ANY_STRATEGY")
                self.assertEqual(reason, "SYMBOL_BLOCKED")

    def test_gold_hash_passes(self) -> None:
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            reason = _execution_invariants_block(_request(), "GOLD_LIQUIDITY_HUNTER_PRO")
        self.assertIsNone(reason)


class NakedOrderInvariantTests(unittest.TestCase):
    """Invariant 4 — SL/TP obligatoires sur tout ordre."""

    def test_missing_sl_is_blocked(self) -> None:
        for bad_sl in (0, 0.0, None, ""):
            with self.subTest(sl=bad_sl):
                with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
                    reason = _execution_invariants_block(_request(sl=bad_sl), "S")
                self.assertEqual(reason, "NAKED_ORDER_BLOCKED")

    def test_missing_tp_is_blocked(self) -> None:
        for bad_tp in (0, 0.0, None, ""):
            with self.subTest(tp=bad_tp):
                with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
                    reason = _execution_invariants_block(_request(tp=bad_tp), "S")
                self.assertEqual(reason, "NAKED_ORDER_BLOCKED")


class LotHardCapInvariantTests(unittest.TestCase):
    """Invariant 2 — lot figé 0.01 (LOT_HARD_CAP)."""

    def test_hard_cap_constant(self) -> None:
        self.assertEqual(LOT_HARD_CAP, 0.01)

    def test_oversized_lot_is_forced_to_cap(self) -> None:
        request = _request(volume=0.55)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            reason = _execution_invariants_block(request, "S")
        self.assertIsNone(reason)
        self.assertEqual(request["volume"], LOT_HARD_CAP)

    def test_zero_or_invalid_lot_is_blocked(self) -> None:
        for bad in (0, 0.0, None, "", -0.01):
            with self.subTest(volume=bad):
                with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
                    reason = _execution_invariants_block(_request(volume=bad), "S")
                self.assertEqual(reason, "LOT_INVALID_BLOCKED")


class MagicInvariantTests(unittest.TestCase):
    """Invariant 3 — magic 909002 sur tous les ordres."""

    def test_magic_constant(self) -> None:
        self.assertEqual(MAGIC_HARD, 909002)

    def test_foreign_magic_is_forced(self) -> None:
        request = _request(magic=909001)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            reason = _execution_invariants_block(request, "S")
        self.assertIsNone(reason)
        self.assertEqual(request["magic"], MAGIC_HARD)

    def test_missing_magic_is_forced(self) -> None:
        request = _request()
        request.pop("magic")
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            reason = _execution_invariants_block(request, "S")
        self.assertIsNone(reason)
        self.assertEqual(request["magic"], MAGIC_HARD)


class MaxOpenPerSymbolInvariantTests(unittest.TestCase):
    """Invariant 5 — MAX_OPEN_TRADES_PER_SYMBOL=1 au choke-point."""

    def test_blocked_while_a_hermes_position_lives(self) -> None:
        open_pos = SimpleNamespace(ticket=1, symbol="GOLD#", magic=MAGIC_HARD)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[open_pos]):
            reason = _execution_invariants_block(_request(), "S", max_open_per_symbol=1)
        self.assertEqual(reason, "MAX_OPEN_TRADES_PER_SYMBOL")

    def test_foreign_magic_positions_do_not_count(self) -> None:
        open_pos = SimpleNamespace(ticket=1, symbol="GOLD#", magic=123456)
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[open_pos]):
            reason = _execution_invariants_block(_request(), "S", max_open_per_symbol=1)
        self.assertIsNone(reason)


class ChokePointEndToEndTests(unittest.TestCase):
    """Preuve au choke-point réel : un candidat parfait hors GOLD# n'atteint
    JAMAIS mt5.order_send, même en appelant _send_order directement (donc en
    court-circuitant volontairement toutes les gates amont)."""

    def setUp(self) -> None:
        import sys
        from pathlib import Path
        import tempfile

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_paper_learning_safety import demo_settings
        from app.mt5.demo_router import DemoKellyRouter

        self.tmp = tempfile.TemporaryDirectory()
        self.router = DemoKellyRouter(demo_settings(), Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _event(self, symbol: str, entry: float, sl: float, tp: float, strategy: str) -> dict:
        return {
            "symbol": symbol,
            "broker_symbol": symbol,
            "strategy": strategy,
            "direction": "SELL",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "final_capped_lot": 0.01,
            "market_bid": entry,
            "market_ask": entry,
            "symbol_specs": {"point": 0.01, "tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01},
        }

    def _send(self, event: dict) -> tuple[dict, object]:
        symbol_info = SimpleNamespace(
            trade_tick_value=1.0,
            trade_tick_size=0.01,
            digits=2,
            point=0.01,
            trade_stops_level=0,
            trade_mode=4,
            filling_mode=1,
        )
        tick = SimpleNamespace(bid=event["entry"], ask=event["entry"])
        fake_result = SimpleNamespace(retcode=10009, order=999999, deal=0, price=event["entry"])
        with (
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.symbol_info", return_value=symbol_info),
            patch("app.mt5.demo_router.mt5.symbol_info_tick", return_value=tick),
            patch("app.mt5.demo_router.mt5.order_check", return_value=SimpleNamespace(retcode=0, comment="Done")),
            patch("app.mt5.demo_router.mt5.order_send", return_value=fake_result) as send,
        ):
            outcome = self.router._send_order(event)
        return outcome, send

    def test_perfect_btc_candidate_never_reaches_order_send(self) -> None:
        event = self._event("BTCUSD#", entry=63000.0, sl=63500.0, tp=62000.0, strategy="BTC_SCALPING_AGENT")
        with self.assertLogs("hermes", level="WARNING") as captured:
            outcome, send = self._send(event)
        send.assert_not_called()
        self.assertEqual(outcome["reason"], "SYMBOL_BLOCKED")
        self.assertEqual(outcome["status"], "BLOCK")
        self.assertTrue(any("[SYMBOL_BLOCKED]" in line for line in captured.output))

    def test_perfect_eurusd_candidate_never_reaches_order_send(self) -> None:
        event = self._event("EURUSD", entry=1.1000, sl=1.1500, tp=1.0000, strategy="ORDER_FLOW_EXECUTION_AGENT")
        outcome, send = self._send(event)
        send.assert_not_called()
        self.assertEqual(outcome["reason"], "SYMBOL_BLOCKED")

    def test_perfect_us100_candidate_never_reaches_order_send(self) -> None:
        event = self._event("US100Cash#", entry=20000.0, sl=20100.0, tp=19800.0, strategy="SIMO_ATM_BREAKOUT")
        outcome, send = self._send(event)
        send.assert_not_called()
        self.assertEqual(outcome["reason"], "SYMBOL_BLOCKED")

    def test_us100_pending_order_never_reaches_order_send(self) -> None:
        event = self._event("US100Cash#", entry=20000.0, sl=19900.0, tp=20200.0, strategy="SIMO_ATM_BREAKOUT")
        event["direction"] = "BUY"
        with (
            patch("app.mt5.demo_router.mt5.orders_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_check", return_value=SimpleNamespace(retcode=0, comment="Done")),
            patch("app.mt5.demo_router.mt5.positions_get", return_value=[]),
            patch("app.mt5.demo_router.mt5.order_send") as send,
        ):
            outcome = self.router._send_pending_order(event)
        send.assert_not_called()
        self.assertEqual(outcome["reason"], "SYMBOL_BLOCKED")

    def test_gold_hash_still_executes(self) -> None:
        event = self._event("GOLD#", entry=4000.0, sl=4010.0, tp=3980.0, strategy="GOLD_LIQUIDITY_HUNTER_PRO")
        outcome, send = self._send(event)
        send.assert_called_once()
        request = send.call_args.args[0]
        self.assertEqual(request["symbol"], "GOLD#")
        self.assertEqual(request["volume"], LOT_HARD_CAP)
        self.assertEqual(request["magic"], MAGIC_HARD)
        self.assertGreater(request["sl"], 0)
        self.assertGreater(request["tp"], 0)
        self.assertEqual(outcome["event_type"], "DEMO_ORDER")


if __name__ == "__main__":
    unittest.main()
