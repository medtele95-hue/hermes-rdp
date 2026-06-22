"""Observability cleanup v1.7 — log rename and throttle verification.

Covers:
- SETUP_HUNTER_RAW_ACCEPT: source file has renamed tag + analysis-only fields
- No SETUP_HUNTER_ACCEPT emitted before final verdict (only after PASS)
- BTC_ATR_COMPUTED and BTC_DYNAMIC_EXIT throttled per ticket
- SL_ENGINE_SKIP throttled per ticket+reason
- OLD_BTC_RESCUE_THRESHOLD throttled per ticket
- Critical close logs (FAST_EXIT_CLOSE_NOW etc.) are never throttled
- Fast exit close behavior unchanged
- Safety invariants: live trading off, lot 0.01, geometric SHADOW
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ─── Helpers ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _capture_hermes():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    hermes = logging.getLogger("hermes")
    hermes.addHandler(h)
    try:
        yield buf
    finally:
        hermes.removeHandler(h)
        h.close()


def _make_rates(n: int = 20, base: float = 100.0, swing: float = 0.5) -> list[dict]:
    out = []
    for i in range(n):
        c = base + (i % 2) * swing
        out.append({"high": c + swing, "low": c - swing, "close": c})
    return out


def _settings(**kw) -> SimpleNamespace:
    defaults = {
        "old_btc_fast_exit_daemon_enabled": True,
        "old_btc_fast_exit_interval_ms": 250,
        "old_btc_fast_exit_min_profit_usd": 0.03,
        "old_btc_fast_exit_hard_min_profit_usd": 0.01,
        "old_btc_fast_exit_close_at_any_positive": True,
        "demo_magic_number": 909002,
        "allow_live_trading": False,
        "demo_only": True,
        "btc_exit_arbiter_enabled": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _pos(ticket=1001, profit=0.05, magic=909002, symbol="BTCUSD#",
         comment="HERMES_BTC", type_=0):
    return SimpleNamespace(
        ticket=ticket, profit=profit, magic=magic, symbol=symbol,
        comment=comment, type=type_, volume=0.01, price_open=65000.0,
    )


def _close_ok(pos, reason):
    return {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}}


# ─── 1. SETUP_HUNTER_RAW_ACCEPT rename ───────────────────────────────────────

class TestSetupHunterRawAcceptRename(unittest.TestCase):

    def _src(self) -> str:
        return Path("app/agents/setup_hunter.py").read_text(encoding="utf-8")

    def test_raw_accept_tag_present_in_source(self):
        self.assertIn("[SETUP_HUNTER_RAW_ACCEPT]", self._src())

    def test_raw_accept_has_accepted_for_execution_false(self):
        src = self._src()
        # Find the RAW_ACCEPT log line and check the field is present
        self.assertIn("accepted_for_execution=false", src)

    def test_raw_accept_has_accepted_for_analysis_true(self):
        self.assertIn("accepted_for_analysis=true", self._src())

    def test_raw_accept_uses_raw_grade_field_name(self):
        self.assertIn("raw_grade=", self._src())

    def test_raw_accept_uses_raw_score_field_name(self):
        self.assertIn("raw_score=", self._src())

    def test_old_accept_tag_not_in_setup_hunter_source(self):
        """[SETUP_HUNTER_ACCEPT] must not appear in setup_hunter.py (only RAW variant)."""
        src = self._src()
        matches = re.findall(r'\[SETUP_HUNTER_ACCEPT\]', src)
        self.assertEqual(matches, [], "setup_hunter.py must not emit [SETUP_HUNTER_ACCEPT]")

    def test_final_verdict_pass_emits_setup_hunter_accept_in_main(self):
        """main.py emits [SETUP_HUNTER_ACCEPT] after final verdict PASS."""
        main_src = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("[SETUP_HUNTER_ACCEPT]", main_src)
        self.assertIn("accepted_for_execution=true", main_src)

    def test_setup_hunter_raw_accept_in_main_ingest(self):
        """main.py ingest event uses SETUP_HUNTER_RAW_ACCEPT before final verdict."""
        main_src = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("SETUP_HUNTER_RAW_ACCEPT", main_src)


# ─── 2. ATR log throttle ─────────────────────────────────────────────────────

class TestAtrLogThrottle(unittest.TestCase):

    def setUp(self):
        from app.mt5.btc_dynamic_exit import BtcDynamicExit
        self.engine = BtcDynamicExit()
        self.rates = _make_rates(20)

    def test_atr_log_emitted_on_first_call(self):
        with _capture_hermes() as buf:
            with patch("app.mt5.btc_dynamic_exit.time") as mt:
                mt.monotonic.return_value = 0.0
                self.engine.compute_atr(self.rates, _log_key=1001)
        self.assertIn("[BTC_ATR_COMPUTED]", buf.getvalue())

    def test_atr_log_suppressed_within_5s_same_value(self):
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute_atr(self.rates, _log_key=1001)
            mt.monotonic.return_value = 2.0  # 2 s elapsed
            with _capture_hermes() as buf:
                self.engine.compute_atr(self.rates, _log_key=1001)
        self.assertNotIn("[BTC_ATR_COMPUTED]", buf.getvalue())

    def test_atr_log_re_emitted_after_5s(self):
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute_atr(self.rates, _log_key=1001)
            mt.monotonic.return_value = 6.0  # > 5 s elapsed
            with _capture_hermes() as buf:
                self.engine.compute_atr(self.rates, _log_key=1001)
        self.assertIn("[BTC_ATR_COMPUTED]", buf.getvalue())

    def test_atr_log_re_emitted_on_material_change(self):
        rates_low = _make_rates(20, base=100.0, swing=0.1)
        rates_high = _make_rates(20, base=100.0, swing=5.0)
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute_atr(rates_low, _log_key=1001)
            mt.monotonic.return_value = 1.0  # only 1 s
            with _capture_hermes() as buf:
                self.engine.compute_atr(rates_high, _log_key=1001)  # ATR ~50× larger
        self.assertIn("[BTC_ATR_COMPUTED]", buf.getvalue())

    def test_atr_log_fires_unconditionally_without_log_key(self):
        """When _log_key=None throttle is bypassed — backward compatible."""
        with _capture_hermes() as buf1:
            self.engine.compute_atr(self.rates)
        with _capture_hermes() as buf2:
            self.engine.compute_atr(self.rates)
        self.assertIn("[BTC_ATR_COMPUTED]", buf1.getvalue())
        self.assertIn("[BTC_ATR_COMPUTED]", buf2.getvalue())


# ─── 3. BTC_DYNAMIC_EXIT log throttle ────────────────────────────────────────

class TestDynamicExitLogThrottle(unittest.TestCase):

    def setUp(self):
        from app.mt5.btc_dynamic_exit import BtcDynamicExit
        self.engine = BtcDynamicExit()
        self.rates = _make_rates(20)

    def test_dynamic_exit_log_emitted_on_first_call(self):
        with _capture_hermes() as buf:
            with patch("app.mt5.btc_dynamic_exit.time") as mt:
                mt.monotonic.return_value = 0.0
                self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=1001)
        self.assertIn("[BTC_DYNAMIC_EXIT]", buf.getvalue())

    def test_dynamic_exit_log_suppressed_within_5s_same_params(self):
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=1001)
            mt.monotonic.return_value = 2.0
            with _capture_hermes() as buf:
                self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=1001)
        self.assertNotIn("[BTC_DYNAMIC_EXIT]", buf.getvalue())

    def test_dynamic_exit_log_re_emitted_after_5s(self):
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=1001)
            mt.monotonic.return_value = 6.0
            with _capture_hermes() as buf:
                self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=1001)
        self.assertIn("[BTC_DYNAMIC_EXIT]", buf.getvalue())

    def test_dynamic_exit_log_fires_on_sl_change(self):
        """Different rates producing different sl_usd triggers immediate re-log."""
        rates_sm = _make_rates(20, swing=0.1)   # small ATR → small sl
        rates_lg = _make_rates(20, swing=5.0)   # large ATR → large sl
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute(70, rates_sm, None, 100.0, "BUY", _log_key=2001)
            mt.monotonic.return_value = 1.0
            with _capture_hermes() as buf:
                self.engine.compute(70, rates_lg, None, 100.0, "BUY", _log_key=2001)
        self.assertIn("[BTC_DYNAMIC_EXIT]", buf.getvalue())

    def test_dynamic_exit_independent_per_ticket(self):
        """Different tickets have independent throttle state."""
        with patch("app.mt5.btc_dynamic_exit.time") as mt:
            mt.monotonic.return_value = 0.0
            self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=100)
            mt.monotonic.return_value = 1.0
            with _capture_hermes() as buf:
                # Ticket 200 never logged before → fires immediately
                self.engine.compute(70, self.rates, None, 100.0, "BUY", _log_key=200)
        self.assertIn("[BTC_DYNAMIC_EXIT]", buf.getvalue())


# ─── 4. SL_ENGINE_SKIP throttle ──────────────────────────────────────────────

class TestSlEngineSkipThrottle(unittest.TestCase):

    def setUp(self):
        from app.mt5.btc_sl_engine import BtcSlEngine
        self.engine = BtcSlEngine()

    def _mock_pos(self, sl=65300.0, pos_type=1):
        p = MagicMock()
        p.sl = sl
        p.tp = 64000.0
        p.type = pos_type
        return p

    def test_sl_unchanged_suppressed_within_5s(self):
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5:
            mock_mt5.positions_get.return_value = [self._mock_pos(sl=65300.0)]
            with patch("app.mt5.btc_sl_engine.time") as mt:
                mt.monotonic.return_value = 0.0
                with _capture_hermes() as _buf1:
                    self.engine.apply_sl_tp_to_mt5(1001, 65300.001)
                mt.monotonic.return_value = 1.0
                with _capture_hermes() as buf2:
                    self.engine.apply_sl_tp_to_mt5(1001, 65300.001)
        self.assertNotIn("SL_ENGINE_SKIP", buf2.getvalue())

    def test_sl_unchanged_re_emitted_after_5s(self):
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5:
            mock_mt5.positions_get.return_value = [self._mock_pos(sl=65300.0)]
            with patch("app.mt5.btc_sl_engine.time") as mt:
                mt.monotonic.return_value = 0.0
                self.engine.apply_sl_tp_to_mt5(1001, 65300.001)
                mt.monotonic.return_value = 6.0
                with _capture_hermes() as buf:
                    self.engine.apply_sl_tp_to_mt5(1001, 65300.001)
        self.assertIn("[SL_ENGINE_SKIP]", buf.getvalue())

    def test_sl_unchanged_fires_when_sl_value_changes(self):
        """Different new_sl value → new throttle key → fires immediately."""
        with patch("app.mt5.btc_sl_engine.mt5") as mock_mt5:
            mock_mt5.positions_get.return_value = [self._mock_pos(sl=65300.0)]
            with patch("app.mt5.btc_sl_engine.time") as mt:
                mt.monotonic.return_value = 0.0
                self.engine.apply_sl_tp_to_mt5(1001, 65300.001)  # sl=65300.00 unchanged
                mt.monotonic.return_value = 1.0
                # Different SL value that is also "unchanged" but at a different level
                mock_mt5.positions_get.return_value = [self._mock_pos(sl=65400.0)]
                with _capture_hermes() as buf:
                    self.engine.apply_sl_tp_to_mt5(1001, 65400.001)
        self.assertIn("[SL_ENGINE_SKIP]", buf.getvalue())

    def test_rescue_mode_skip_suppressed_within_5s(self):
        with patch("app.mt5.btc_sl_engine.time") as mt:
            mt.monotonic.return_value = 0.0
            with _capture_hermes() as _buf1:
                self.engine.run(
                    ticket=2001, entry_price=65000.0, direction="BUY", atr=50.0,
                    current_profit=0.5, min_seen_profit=-1.0, positive_count=0,
                    be_buffer_usd=0.1, exit_mode="dynamic",
                )
            mt.monotonic.return_value = 2.0
            with _capture_hermes() as buf2:
                self.engine.run(
                    ticket=2001, entry_price=65000.0, direction="BUY", atr=50.0,
                    current_profit=0.5, min_seen_profit=-1.0, positive_count=0,
                    be_buffer_usd=0.1, exit_mode="dynamic",
                )
        self.assertNotIn("RESCUE_MODE_ACTIVE", buf2.getvalue())

    def test_positive_candidates_skip_suppressed_within_5s(self):
        with patch("app.mt5.btc_sl_engine.time") as mt:
            mt.monotonic.return_value = 0.0
            with _capture_hermes() as _:
                self.engine.run(
                    ticket=3001, entry_price=65000.0, direction="BUY", atr=50.0,
                    current_profit=0.5, min_seen_profit=0.0, positive_count=2,
                    be_buffer_usd=0.1, exit_mode="dynamic",
                )
            mt.monotonic.return_value = 1.0
            with _capture_hermes() as buf:
                self.engine.run(
                    ticket=3001, entry_price=65000.0, direction="BUY", atr=50.0,
                    current_profit=0.5, min_seen_profit=0.0, positive_count=2,
                    be_buffer_usd=0.1, exit_mode="dynamic",
                )
        self.assertNotIn("POSITIVE_CANDIDATES_PRESENT", buf.getvalue())

    def test_skip_returns_unchanged_regardless_of_throttle(self):
        """Return value must never be affected by throttle."""
        with patch("app.mt5.btc_sl_engine.time") as mt:
            mt.monotonic.return_value = 0.0
            r1 = self.engine.run(
                ticket=4001, entry_price=65000.0, direction="BUY", atr=50.0,
                current_profit=0.5, min_seen_profit=0.0, positive_count=1,
                be_buffer_usd=0.1, exit_mode="dynamic",
            )
            mt.monotonic.return_value = 1.0
            r2 = self.engine.run(
                ticket=4001, entry_price=65000.0, direction="BUY", atr=50.0,
                current_profit=0.5, min_seen_profit=0.0, positive_count=1,
                be_buffer_usd=0.1, exit_mode="dynamic",
            )
        self.assertFalse(r1.get("applied"))
        self.assertFalse(r2.get("applied"))
        self.assertEqual(r1.get("reason"), "POSITIVE_CANDIDATES_PRESENT")
        self.assertEqual(r2.get("reason"), "POSITIVE_CANDIDATES_PRESENT")


# ─── 5. OLD_BTC_RESCUE_THRESHOLD throttle ────────────────────────────────────

class TestRescueThresholdThrottle(unittest.TestCase):
    """Uses LOVABLE_BTC_OLD_SYSTEM profile to exercise the rescue-threshold log path."""

    def _make_daemon(self):
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        return BtcFastExitDaemon(
            _settings(hermes_execution_profile="LOVABLE_BTC_OLD_SYSTEM"),
            _close_ok,
        )

    def _eval(self, daemon, profit, close_any=True):
        """Call _evaluate_position with was_negative set; no rates → fallback mode."""
        p = _pos(profit=profit)
        daemon._was_negative[p.ticket] = True
        daemon._min_seen_profit[p.ticket] = min(-0.10, profit)
        daemon._max_seen_profit[p.ticket] = max(0.0, profit)
        daemon._evaluate_position(p, 0.03, 0.01, close_any, [], None, 50.0)

    def test_rescue_threshold_suppressed_within_5s_same_state(self):
        daemon = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.time") as mt:
            mt.time.return_value = 0.0
            with _capture_hermes() as _:
                self._eval(daemon, profit=-0.02)  # first call: threshold_changed fires
            mt.time.return_value = 2.0
            with _capture_hermes() as buf:
                self._eval(daemon, profit=-0.02)  # same state within 5s: suppressed
        self.assertNotIn("[OLD_BTC_RESCUE_THRESHOLD]", buf.getvalue())

    def test_rescue_threshold_fires_when_profit_crosses(self):
        daemon = self._make_daemon()
        # close_any=False prevents ANY_POSITIVE_FAST_EXIT from firing before rescue branch
        with _capture_hermes() as _:
            self._eval(daemon, profit=-0.02, close_any=False)   # below threshold: logs (first call)
        with _capture_hermes() as buf:
            self._eval(daemon, profit=0.11, close_any=False)    # profit crosses 0.10: crossed fires
        self.assertIn("[OLD_BTC_RESCUE_THRESHOLD]", buf.getvalue())

    def test_rescue_threshold_re_emits_after_5s(self):
        daemon = self._make_daemon()
        with patch("app.mt5.btc_fast_exit_daemon.time") as mt:
            mt.time.return_value = 0.0
            with _capture_hermes() as _:
                self._eval(daemon, profit=-0.02, close_any=False)
            mt.time.return_value = 6.0
            with _capture_hermes() as buf:
                self._eval(daemon, profit=-0.02, close_any=False)
        self.assertIn("[OLD_BTC_RESCUE_THRESHOLD]", buf.getvalue())


# ─── 6. Critical logs NOT throttled ──────────────────────────────────────────

class TestCriticalLogsNotThrottled(unittest.TestCase):

    def setUp(self):
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        self.daemon = BtcFastExitDaemon(_settings(), _close_ok)

    def test_fast_exit_close_now_fires_every_close(self):
        """FAST_EXIT_CLOSE_NOW fires each time a close is triggered."""
        logs = []
        original_close = _close_ok

        def capturing_close(pos, reason):
            return original_close(pos, reason)

        daemon = type(self.daemon)(self.daemon.settings, capturing_close)
        with _capture_hermes() as buf:
            daemon._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        self.assertIn("[OLD_BTC_FAST_EXIT_CLOSE_NOW]", buf.getvalue())

    def test_fast_exit_closed_fires_on_success(self):
        with _capture_hermes() as buf:
            self.daemon._evaluate_position(_pos(profit=0.10), 0.03, 0.01, True)
        self.assertIn("[OLD_BTC_FAST_EXIT_CLOSED]", buf.getvalue())

    def test_close_now_not_subject_to_any_time_gate(self):
        """Close fires even on back-to-back calls (fresh ticket each time)."""
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        d = BtcFastExitDaemon(_settings(), _close_ok)
        with _capture_hermes() as buf:
            d._evaluate_position(_pos(ticket=101, profit=0.10), 0.03, 0.01, True)
            d._evaluate_position(_pos(ticket=102, profit=0.10), 0.03, 0.01, True)
        count = buf.getvalue().count("[OLD_BTC_FAST_EXIT_CLOSE_NOW]")
        self.assertEqual(count, 2)


# ─── 7. Fast exit behavior unchanged ─────────────────────────────────────────

class TestFastExitBehaviorUnchanged(unittest.TestCase):

    def _daemon(self, **kw):
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        return BtcFastExitDaemon(_settings(**kw), _close_ok)

    def test_closes_at_min_profit(self):
        closed = []

        def cap(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        d = BtcFastExitDaemon(_settings(), cap)
        d._evaluate_position(_pos(profit=0.03), 0.03, 0.01, True)
        self.assertEqual(closed, ["ANY_POSITIVE_FAST_EXIT"])

    def test_does_not_close_below_min(self):
        closed = []

        def cap(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        d = BtcFastExitDaemon(_settings(), cap)
        d._evaluate_position(_pos(profit=0.02), 0.03, 0.01, True)
        self.assertEqual(closed, [])

    def test_closes_after_negative_at_hard_min(self):
        closed = []

        def cap(pos, reason):
            closed.append(reason)
            return _close_ok(pos, reason)

        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        d = BtcFastExitDaemon(_settings(), cap)
        d._was_negative[1001] = True
        d._evaluate_position(_pos(profit=0.01), 0.03, 0.01, True)
        self.assertEqual(closed, ["NEGATIVE_THEN_TINY_POSITIVE"])

    def test_positive_count_tracked_correctly(self):
        from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
        d = BtcFastExitDaemon(_settings(), _close_ok)
        with (
            patch("app.mt5.btc_fast_exit_daemon.mt5.positions_get") as mock_pos,
            patch("app.mt5.btc_fast_exit_daemon.mt5.account_info") as mock_ai,
        ):
            mock_ai.return_value = SimpleNamespace(trade_mode=0)
            mock_pos.return_value = [_pos(profit=0.05)]
            d._tick()
        with d._lock:
            self.assertEqual(d._fast_exit_positive_candidates, 1)


# ─── 8. Safety invariants ─────────────────────────────────────────────────────

class TestObservabilityCleanupSafetyInvariants(unittest.TestCase):

    def test_live_trading_remains_disabled(self):
        from app.config import get_settings
        self.assertFalse(get_settings().allow_live_trading)

    def test_demo_lot_remains_001(self):
        from app.config import get_settings
        self.assertAlmostEqual(get_settings().demo_max_lot, 0.01)

    def test_geometric_mode_remains_shadow(self):
        from app.config import get_settings
        mode = str(getattr(get_settings(), "geometric_confluence_mode", "SHADOW") or "SHADOW").upper()
        self.assertEqual(mode, "SHADOW")

    def test_no_order_send_in_btc_dynamic_exit(self):
        src = Path("app/mt5/btc_dynamic_exit.py").read_text(encoding="utf-8")
        self.assertNotIn("order_send", src)

    def test_no_order_send_in_btc_sl_engine(self):
        src = Path("app/mt5/btc_sl_engine.py").read_text(encoding="utf-8")
        # sl_engine only calls sl_engine_apply_modification, not order_send directly
        hits = re.findall(r'\bmt5\.order_send\b', src)
        self.assertEqual(hits, [])

    def test_btc_dynamic_exit_returns_correct_keys(self):
        """Throttle changes must not affect return value."""
        from app.mt5.btc_dynamic_exit import BtcDynamicExit
        engine = BtcDynamicExit()
        rates = _make_rates(20)
        result = engine.compute(70, rates, None, 100.0, "BUY", _log_key=9999)
        for key in ("tp_usd", "sl_usd", "lock_usd", "trail_gap_usd",
                    "trail_start_usd", "atr_value", "rr_target", "mode"):
            self.assertIn(key, result)
        self.assertEqual(result["mode"], "dynamic")

    def test_sl_engine_skip_does_not_change_return_value(self):
        """Throttle on SL_ENGINE_SKIP must not affect the returned dict."""
        from app.mt5.btc_sl_engine import BtcSlEngine
        engine = BtcSlEngine()
        r = engine.run(
            ticket=99, entry_price=65000.0, direction="BUY", atr=50.0,
            current_profit=0.5, min_seen_profit=0.0, positive_count=3,
            be_buffer_usd=0.1, exit_mode="dynamic",
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "POSITIVE_CANDIDATES_PRESENT")


if __name__ == "__main__":
    unittest.main()
