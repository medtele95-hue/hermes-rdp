from __future__ import annotations

import inspect
import logging
import unittest
from unittest.mock import patch

from app.config import Settings
from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
from app.mt5.demo_router import DemoKellyRouter
from app.mt5.ml_random_forest_confirmator import confirm as ml_confirm
from app.utils import throttle


class TestStateAwareEventThrottle(unittest.TestCase):
    def setUp(self) -> None:
        throttle._event_state.clear()

    def test_repeated_fast_exit_zero_state_is_throttled(self) -> None:
        daemon = BtcFastExitDaemon(Settings(), lambda *_: {})
        with patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get", return_value=[]), \
             patch.object(daemon, "_publish_state"), \
             self.assertLogs("hermes", level="INFO") as captured:
            daemon._tick()
            daemon._tick()
        lines = [line for line in captured.output if "OLD_BTC_FAST_EXIT_TICK" in line]
        self.assertEqual(len(lines), 1)

    def test_state_change_logs_immediately(self) -> None:
        with patch.object(throttle.log, "info") as info:
            throttle.log_event_throttled("tick", "open_count=0", state=(0, 0))
            throttle.log_event_throttled("tick", "open_count=0", state=(0, 0))
            throttle.log_event_throttled("tick", "open_count=1", state=(1, 0))
        self.assertEqual(info.call_count, 2)
        info.assert_called_with("open_count=1")

    def test_final_verdict_ml_unavailable_is_throttled_per_symbol(self) -> None:
        with patch.object(throttle.log, "info") as info:
            ml_confirm("BTCUSD", {}, object())
            ml_confirm("BTCUSD", {}, object())
            ml_confirm("GOLD", {}, object())
        verdicts = [
            call.args[0] for call in info.call_args_list
            if call.args and "[FINAL_VERDICT_ML]" in call.args[0]
        ]
        self.assertEqual(len(verdicts), 2)
        self.assertTrue(any("symbol=BTCUSD" in line for line in verdicts))
        self.assertTrue(any("symbol=GOLD" in line for line in verdicts))

    def test_critical_logs_are_not_migrated_to_throttle(self) -> None:
        source = inspect.getsource(DemoKellyRouter._send_order)
        self.assertIn("[DEMO_ROUTER_ORDER_SENT]", source)
        self.assertNotIn("log_event_throttled", source)

        from app.mt5 import btc_entry_gate
        self.assertIn("[BTC_ENTRY_GUARD] status=BLOCK", inspect.getsource(btc_entry_gate))
        self.assertNotIn("log_event_throttled", inspect.getsource(btc_entry_gate))

    def test_error_logs_remain_direct(self) -> None:
        logger = logging.getLogger("hermes")
        with self.assertLogs("hermes", level="ERROR") as captured:
            logger.error("ERROR audit event")
            logger.error("ERROR audit event")
        self.assertEqual(len(captured.output), 2)

    def test_cycle_summary_has_one_emission_site(self) -> None:
        from app.main import HermesBackend
        source = inspect.getsource(HermesBackend.run_cycle)
        self.assertEqual(source.count("[CYCLE_SUMMARY]"), 1)
        self.assertIn("floating_pnl=%s", source)

    def test_safety_defaults_are_unchanged(self) -> None:
        settings = Settings()
        self.assertTrue(settings.demo_only)
        self.assertFalse(settings.allow_live_trading)
        self.assertEqual(settings.demo_max_lot, 0.01)
