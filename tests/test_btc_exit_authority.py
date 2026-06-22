"""URGENT PATCH tests — BTC exit authority and realized_rr logging.

Tests 7, 8, 9, 10 from the patch specification.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mt5.btc_dynamic_exit import BtcDynamicExit
from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
from app.mt5.btc_sl_engine import BtcSlEngine


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_rates_large(n: int = 20) -> list[dict]:
    """Candles with very large ATR swings to force sl/tp cap hits."""
    rates = []
    for i in range(n):
        price = 60000.0 + (i % 2) * 500.0
        rates.append({"high": price + 200.0, "low": price - 200.0, "close": price})
    return rates


def _daemon_settings(**kwargs) -> SimpleNamespace:
    s = SimpleNamespace(
        old_btc_fast_exit_daemon_enabled=True,
        old_btc_fast_exit_interval_ms=250,
        old_btc_fast_exit_min_profit_usd=0.03,
        old_btc_fast_exit_hard_min_profit_usd=0.01,
        old_btc_fast_exit_close_at_any_positive=True,
        demo_magic_number=909002,
        allow_live_trading=False,
        demo_only=True,
        hermes_execution_profile="",
        btc_exit_arbiter_enabled=False,
    )
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _pos(ticket: int = 1001, profit: float = 0.10, type_: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket, profit=profit, magic=909002, symbol="BTCUSD#",
        comment="HERMES_BTC", type=type_, volume=0.01, price_open=65000.0,
    )


def _close_ok(pos, reason):
    return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}


# ─── Test 7: realized_rr logged after caps ─────────────────────────────────────

class TestDynamicExitRealizedRr(unittest.TestCase):
    """Test 7: realized_rr = round(tp_usd / sl_usd, 4) present in result and logs."""

    def setUp(self):
        self.engine = BtcDynamicExit()

    def test_realized_rr_in_return_dict(self):
        """compute() must include realized_rr key."""
        rates = _make_rates_large()
        result = self.engine.compute(
            confluence_score=50.0, rates_m5=rates,
            cvd_slope=None, entry_price=65000.0, direction="BUY",
        )
        self.assertIn("realized_rr", result)
        self.assertIsNotNone(result["realized_rr"])
        self.assertGreater(float(result["realized_rr"]), 0.0)

    def test_realized_rr_equals_tp_over_sl(self):
        """realized_rr must equal round(tp_usd / sl_usd, 4) in all cases."""
        rates = _make_rates_large()
        result = self.engine.compute(
            confluence_score=80.0, rates_m5=rates,
            cvd_slope=None, entry_price=65000.0, direction="BUY",
        )
        expected = round(result["tp_usd"] / result["sl_usd"], 4)
        self.assertAlmostEqual(result["realized_rr"], expected, places=4)

    def test_realized_rr_reflects_cap_divergence(self):
        """When both sl and tp are capped, realized_rr != rr_target."""
        rates = _make_rates_large()  # ATR ~400 → sl=3.0, tp=6.0 (both capped)
        result = self.engine.compute(
            confluence_score=80.0, rates_m5=rates,
            cvd_slope=None, entry_price=65000.0, direction="BUY",
        )
        if result["sl_usd"] == 3.0 and result["tp_usd"] == 6.0:
            self.assertAlmostEqual(result["realized_rr"], 2.0, places=4,
                                   msg="Capped sl=3 tp=6 → realized_rr=2.0")
            self.assertGreater(result["rr_target"], 2.0,
                               "rr_target must exceed realized_rr when caps apply")

    def test_realized_rr_in_log_output(self):
        """[BTC_DYNAMIC_EXIT] log must contain realized_rr=."""
        rates = _make_rates_large()
        with self.assertLogs("hermes", level="INFO") as cm:
            self.engine.compute(
                confluence_score=50.0, rates_m5=rates,
                cvd_slope=None, entry_price=65000.0, direction="BUY",
            )
        dyn_lines = [l for l in cm.output if "[BTC_DYNAMIC_EXIT]" in l]
        self.assertTrue(dyn_lines, "Expected at least one [BTC_DYNAMIC_EXIT] log line")
        self.assertTrue(
            any("realized_rr" in l for l in dyn_lines),
            f"realized_rr missing from [BTC_DYNAMIC_EXIT] log: {dyn_lines}",
        )


# ─── Test 8: exit authority log ───────────────────────────────────────────────

class TestExitAuthorityLog(unittest.TestCase):
    """Test 8: [BTC_EXIT_AUTHORITY] and [BTC_EXIT_DECISION] are emitted per evaluation."""

    def _daemon(self, close_fn=None) -> BtcFastExitDaemon:
        return BtcFastExitDaemon(_daemon_settings(), close_fn or _close_ok)

    def test_exit_authority_log_emitted_on_profitable_position(self):
        """[BTC_EXIT_AUTHORITY] with correct fields is emitted for each position."""
        d = self._daemon()
        with self.assertLogs("hermes", level="INFO") as cm:
            d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        authority_lines = [l for l in cm.output if "[BTC_EXIT_AUTHORITY]" in l]
        self.assertTrue(authority_lines, "Expected [BTC_EXIT_AUTHORITY] log")
        line = authority_lines[0]
        self.assertIn("manager=QUICK", line)
        self.assertIn("enabled_managers=QUICK", line)

    def test_exit_authority_log_emitted_on_losing_position(self):
        """[BTC_EXIT_AUTHORITY] is emitted even when position is not yet profitable."""
        d = self._daemon()
        with self.assertLogs("hermes", level="INFO") as cm:
            d._evaluate_position(_pos(profit=-0.20), 0.03, 0.01, True)
        authority_lines = [l for l in cm.output if "[BTC_EXIT_AUTHORITY]" in l]
        self.assertTrue(authority_lines, "Expected [BTC_EXIT_AUTHORITY] even for losing positions")

    def test_exit_decision_close_log_emitted_on_close(self):
        """[BTC_EXIT_DECISION] action=CLOSE authority=OLD_BTC_QUICK_EXIT is emitted on close."""
        d = self._daemon()
        with self.assertLogs("hermes", level="INFO") as cm:
            d._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        decision_lines = [l for l in cm.output if "[BTC_EXIT_DECISION]" in l]
        self.assertTrue(decision_lines, "Expected [BTC_EXIT_DECISION] log")
        self.assertTrue(any("action=CLOSE" in l for l in decision_lines))
        self.assertTrue(any("authority=QUICK" in l for l in decision_lines))


# ─── Test 9: OLD_BTC_QUICK_EXIT is profit-close authority ─────────────────────

class TestOldBtcQuickExitAuthority(unittest.TestCase):
    """Test 9: Profit closes use OLD_BTC_QUICK_EXIT thresholds; DYNAMIC_EXIT is advisory."""

    def test_fallback_mode_closes_at_old_btc_threshold(self):
        """No rates → fallback mode; close uses old_btc_fast_exit_min_profit_usd threshold."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        s = _daemon_settings(old_btc_fast_exit_min_profit_usd=0.05)
        d = BtcFastExitDaemon(s, capture)
        d._evaluate_position(_pos(profit=0.05), 0.05, 0.01, True, rates_for_dynamic=[])
        self.assertEqual(len(closed), 1, "Should close at old_btc threshold")
        self.assertEqual(closed[0], "ANY_POSITIVE_FAST_EXIT")

    def test_below_old_btc_threshold_no_close(self):
        """Profit below old_btc threshold → no close in fallback mode."""
        closed = []

        def capture(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        s = _daemon_settings(old_btc_fast_exit_min_profit_usd=0.10)
        d = BtcFastExitDaemon(s, capture)
        d._evaluate_position(_pos(profit=0.05), 0.10, 0.01, True, rates_for_dynamic=[])
        self.assertEqual(len(closed), 0, "Must not close below threshold")

    def test_dynamic_advisory_log_uses_new_name(self):
        """Advisory log name is [BTC_DYNAMIC_EXIT_ADVISORY], not [OLD_BTC_DYNAMIC_EXIT_APPLIED]."""
        d = BtcFastExitDaemon(_daemon_settings(), _close_ok)
        rates = _make_rates_large()
        with self.assertLogs("hermes", level="INFO") as cm:
            d._evaluate_position(_pos(profit=0.01), 0.03, 0.01, True, rates_for_dynamic=rates)
        old_name_lines = [l for l in cm.output if "[OLD_BTC_DYNAMIC_EXIT_APPLIED]" in l]
        self.assertEqual(old_name_lines, [],
                         "[OLD_BTC_DYNAMIC_EXIT_APPLIED] must be replaced by [BTC_DYNAMIC_EXIT_ADVISORY]")
        advisory_lines = [l for l in cm.output if "[BTC_DYNAMIC_EXIT_ADVISORY]" in l]
        self.assertTrue(advisory_lines,
                        "[BTC_DYNAMIC_EXIT_ADVISORY] must be emitted when ATR is available")

    def test_dynamic_advisory_log_contains_realized_rr(self):
        """[BTC_DYNAMIC_EXIT_ADVISORY] must include realized_rr."""
        d = BtcFastExitDaemon(_daemon_settings(), _close_ok)
        rates = _make_rates_large()
        with self.assertLogs("hermes", level="INFO") as cm:
            d._evaluate_position(_pos(profit=0.01), 0.03, 0.01, True, rates_for_dynamic=rates)
        advisory_lines = [l for l in cm.output if "[BTC_DYNAMIC_EXIT_ADVISORY]" in l]
        self.assertTrue(advisory_lines, "Expected [BTC_DYNAMIC_EXIT_ADVISORY]")
        self.assertTrue(
            any("realized_rr" in l for l in advisory_lines),
            f"realized_rr missing from [BTC_DYNAMIC_EXIT_ADVISORY]: {advisory_lines}",
        )


# ─── Test 10: SL_ENGINE never widens SL ──────────────────────────────────────

class TestSlEngineNeverWidensSl(unittest.TestCase):
    """Test 10: BtcSlEngine rejects any SL modification that would widen the stop."""

    def setUp(self):
        self.engine = BtcSlEngine()

    def _mock_mt5(self, current_sl: float, pos_type: int):
        pos = SimpleNamespace(sl=current_sl, tp=65500.0, type=pos_type)
        mock = MagicMock()
        mock.positions_get.return_value = [pos]
        return mock

    def test_sl_widen_buy_rejected(self):
        """BUY: new_sl < current_sl → wider stop → SL_WOULD_WIDEN."""
        with patch("app.mt5.btc_sl_engine.mt5", self._mock_mt5(current_sl=64800.0, pos_type=0)):
            result = self.engine.apply_sl_tp_to_mt5(1001, sl_price=64700.0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "SL_WOULD_WIDEN")

    def test_sl_widen_sell_rejected(self):
        """SELL: new_sl > current_sl → wider stop → SL_WOULD_WIDEN."""
        with patch("app.mt5.btc_sl_engine.mt5", self._mock_mt5(current_sl=65200.0, pos_type=1)):
            result = self.engine.apply_sl_tp_to_mt5(1001, sl_price=65300.0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "SL_WOULD_WIDEN")

    def test_sl_widen_large_deviation_buy_rejected(self):
        """BUY: moving SL far below current SL → still SL_WOULD_WIDEN."""
        with patch("app.mt5.btc_sl_engine.mt5", self._mock_mt5(current_sl=64800.0, pos_type=0)):
            result = self.engine.apply_sl_tp_to_mt5(1001, sl_price=60000.0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "SL_WOULD_WIDEN")

    def test_sl_engine_run_skips_rescue_mode(self):
        """SL engine skips modification when position is in rescue mode (was negative)."""
        result = self.engine.run(
            ticket=1001, entry_price=65000.0, direction="BUY",
            atr=41.0, current_profit=0.15,
            min_seen_profit=-1.0,  # was negative → rescue mode
            positive_count=0, be_buffer_usd=0.1, exit_mode="fixed",
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "RESCUE_MODE_ACTIVE")

    def test_sl_engine_run_skips_when_positive_candidates(self):
        """SL engine yields to fast-exit daemon when positive_count > 0."""
        result = self.engine.run(
            ticket=1001, entry_price=65000.0, direction="BUY",
            atr=41.0, current_profit=0.15, min_seen_profit=0.0,
            positive_count=1, be_buffer_usd=0.1, exit_mode="fixed",
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "POSITIVE_CANDIDATES_PRESENT")


if __name__ == "__main__":
    unittest.main()
