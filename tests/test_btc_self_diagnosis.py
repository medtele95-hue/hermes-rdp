"""Tests for app/mt5/btc_self_diagnosis.py.

Covers:
- Pattern detection from loss records
- Temp block created after >= 2 occurrences
- Temp block NOT created after exactly 1 occurrence
- Expired blocks not returned
- Active blocks returned before expiry
- NARRATIVE_BLOCK_IGNORED pattern (9th pattern)
- All patterns have valid TTL (> 0 minutes)
- Thread safety and singleton
- No MT5 import / no order_send
"""
from __future__ import annotations

import inspect
import threading
import time
import unittest

from app.mt5.btc_self_diagnosis import (
    PATTERN_TAXONOMY,
    BtcSelfDiagnosis,
    _PATTERN_THRESHOLD,
    _RECENT_LOSS_WINDOW,
    get_btc_self_diagnosis,
    log_active_diagnosis_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loss(**flags) -> dict:
    """Build a minimal loss trade dict with optional pattern flags."""
    base = {"profit": -12.50, "strategy": "BTC_SCALPING_AGENT"}
    base.update(flags)
    return base


def _losses_with_flag(flag: str, count: int) -> list[dict]:
    """Return `count` loss records all bearing the given flag."""
    return [_loss(**{flag: True}) for _ in range(count)]


# ---------------------------------------------------------------------------
# Pattern taxonomy tests
# ---------------------------------------------------------------------------

class TestPatternTaxonomy(unittest.TestCase):

    def test_nine_patterns_defined(self):
        self.assertEqual(len(PATTERN_TAXONOMY), 9)

    def test_all_patterns_have_valid_ttl(self):
        """Every pattern must have ttl_minutes > 0 and reasonable (< 1 day)."""
        for name, spec in PATTERN_TAXONOMY.items():
            self.assertIn("ttl_minutes", spec, f"Missing ttl_minutes in {name}")
            ttl = spec["ttl_minutes"]
            self.assertIsInstance(ttl, int, f"{name}: ttl_minutes must be int")
            self.assertGreater(ttl, 0, f"{name}: ttl_minutes must be > 0")
            self.assertLess(ttl, 1440, f"{name}: ttl_minutes must be < 1 day (1440)")

    def test_all_patterns_have_description(self):
        for name, spec in PATTERN_TAXONOMY.items():
            self.assertIn("description", spec, f"Missing description in {name}")
            self.assertIsInstance(spec["description"], str)
            self.assertGreater(len(spec["description"]), 5, f"{name}: description too short")

    def test_all_patterns_have_trade_flag(self):
        for name, spec in PATTERN_TAXONOMY.items():
            self.assertIn("trade_flag", spec, f"Missing trade_flag in {name}")
            self.assertIsInstance(spec["trade_flag"], str)
            self.assertGreater(len(spec["trade_flag"]), 0)

    def test_required_patterns_present(self):
        required = {
            "LATE_AFTER_IMPULSE",
            "AGAINST_VWAP",
            "AGAINST_POC",
            "WEAK_CVD",
            "HIGH_SPREAD",
            "SMC_MTFA_STRONG_FAIL",
            "BEFORE_REVERSAL_CONFIRM",
            "BAD_SESSION",
            "NARRATIVE_BLOCK_IGNORED",
        }
        self.assertEqual(required, set(PATTERN_TAXONOMY.keys()))

    def test_all_trade_flags_unique(self):
        flags = [spec["trade_flag"] for spec in PATTERN_TAXONOMY.values()]
        self.assertEqual(len(flags), len(set(flags)), "trade_flag values must be unique")

    def test_pattern_threshold_is_two(self):
        self.assertEqual(_PATTERN_THRESHOLD, 2)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

class TestPatternDetection(unittest.TestCase):

    def setUp(self):
        self.diag = BtcSelfDiagnosis()

    def test_late_impulse_pattern_detected(self):
        """LATE_AFTER_IMPULSE detected when 2+ losses carry late_entry flag."""
        flag = PATTERN_TAXONOMY["LATE_AFTER_IMPULSE"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("LATE_AFTER_IMPULSE", activated)

    def test_against_vwap_pattern_detected(self):
        """AGAINST_VWAP detected when 2+ losses carry against_vwap flag."""
        flag = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("AGAINST_VWAP", activated)

    def test_against_poc_pattern_detected(self):
        flag = PATTERN_TAXONOMY["AGAINST_POC"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("AGAINST_POC", activated)

    def test_weak_cvd_pattern_detected(self):
        flag = PATTERN_TAXONOMY["WEAK_CVD"]["trade_flag"]
        losses = _losses_with_flag(flag, 3)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("WEAK_CVD", activated)

    def test_high_spread_pattern_detected(self):
        flag = PATTERN_TAXONOMY["HIGH_SPREAD"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("HIGH_SPREAD", activated)

    def test_smc_mtfa_strong_fail_pattern_detected(self):
        flag = PATTERN_TAXONOMY["SMC_MTFA_STRONG_FAIL"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("SMC_MTFA_STRONG_FAIL", activated)

    def test_before_reversal_confirm_pattern_detected(self):
        flag = PATTERN_TAXONOMY["BEFORE_REVERSAL_CONFIRM"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("BEFORE_REVERSAL_CONFIRM", activated)

    def test_bad_session_pattern_detected(self):
        flag = PATTERN_TAXONOMY["BAD_SESSION"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("BAD_SESSION", activated)

    def test_narrative_block_ignored_pattern(self):
        """NARRATIVE_BLOCK_IGNORED pattern fires when 2+ losses carry the flag."""
        flag = PATTERN_TAXONOMY["NARRATIVE_BLOCK_IGNORED"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("NARRATIVE_BLOCK_IGNORED", activated)

    def test_narrative_block_ignored_ttl_is_strict(self):
        """NARRATIVE_BLOCK_IGNORED has a TTL >= 60 minutes (severe pattern)."""
        ttl = PATTERN_TAXONOMY["NARRATIVE_BLOCK_IGNORED"]["ttl_minutes"]
        self.assertGreaterEqual(ttl, 60)

    def test_no_flag_produces_no_activation(self):
        """Losses with no pattern flags produce no activations."""
        losses = [_loss() for _ in range(5)]
        activated = self.diag.diagnose_losses(losses)
        self.assertEqual(activated, [])

    def test_empty_loss_list_produces_no_activation(self):
        activated = self.diag.diagnose_losses([])
        self.assertEqual(activated, [])

    def test_only_pattern_trades_in_window_counted(self):
        """Mixed losses: only those with the flag count toward the threshold."""
        flag = PATTERN_TAXONOMY["WEAK_CVD"]["trade_flag"]
        losses = [
            _loss(**{flag: True}),
            _loss(),                   # no flag — does not count
            _loss(**{flag: False}),    # explicit False — does not count
        ]
        activated = self.diag.diagnose_losses(losses)
        self.assertNotIn("WEAK_CVD", activated)  # only 1 match < threshold=2


# ---------------------------------------------------------------------------
# Temp block lifecycle
# ---------------------------------------------------------------------------

class TestTempBlockLifecycle(unittest.TestCase):

    def setUp(self):
        self.diag = BtcSelfDiagnosis()

    def test_temp_block_created_after_2_occurrences(self):
        """Pattern seen in 2 losses → block is created."""
        flag = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        self.diag.diagnose_losses(losses)
        self.assertTrue(self.diag.is_blocked("AGAINST_VWAP"))

    def test_temp_block_not_created_after_1_occurrence(self):
        """Pattern seen in exactly 1 loss → no block created."""
        flag = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        losses = _losses_with_flag(flag, 1)
        self.diag.diagnose_losses(losses)
        self.assertFalse(self.diag.is_blocked("AGAINST_VWAP"))

    def test_temp_block_created_after_3_occurrences(self):
        """More than 2 occurrences also triggers the block."""
        flag = PATTERN_TAXONOMY["BAD_SESSION"]["trade_flag"]
        losses = _losses_with_flag(flag, 5)
        self.diag.diagnose_losses(losses)
        self.assertTrue(self.diag.is_blocked("BAD_SESSION"))

    def test_expired_block_not_returned(self):
        """A block whose expire_ts is in the past is pruned and not returned."""
        self.diag._raw_set_block("AGAINST_VWAP", time.time() - 1.0)
        blocks = self.diag.get_active_blocks()
        self.assertNotIn("AGAINST_VWAP", blocks)

    def test_active_block_returned_before_expiry(self):
        """A block with future expire_ts appears in get_active_blocks()."""
        self.diag._raw_set_block("AGAINST_VWAP", time.time() + 3600.0)
        blocks = self.diag.get_active_blocks()
        self.assertIn("AGAINST_VWAP", blocks)

    def test_is_blocked_false_for_expired(self):
        self.diag._raw_set_block("WEAK_CVD", time.time() - 0.001)
        self.assertFalse(self.diag.is_blocked("WEAK_CVD"))

    def test_is_blocked_true_for_active(self):
        self.diag._raw_set_block("WEAK_CVD", time.time() + 3600.0)
        self.assertTrue(self.diag.is_blocked("WEAK_CVD"))

    def test_is_blocked_false_for_unknown_pattern(self):
        self.assertFalse(self.diag.is_blocked("NONEXISTENT_PATTERN"))

    def test_get_active_blocks_returns_dict(self):
        result = self.diag.get_active_blocks()
        self.assertIsInstance(result, dict)

    def test_get_active_block_names_returns_list(self):
        result = self.diag.get_active_block_names()
        self.assertIsInstance(result, list)

    def test_get_active_blocks_expire_ts_values(self):
        """expire_ts values in active blocks are floats in the future."""
        self.diag._raw_set_block("HIGH_SPREAD", time.time() + 1800.0)
        blocks = self.diag.get_active_blocks()
        self.assertIn("HIGH_SPREAD", blocks)
        self.assertIsInstance(blocks["HIGH_SPREAD"], float)
        self.assertGreater(blocks["HIGH_SPREAD"], time.time())

    def test_block_ttl_matches_taxonomy(self):
        """Block duration set by diagnose_losses matches PATTERN_TAXONOMY ttl_minutes."""
        flag = PATTERN_TAXONOMY["BAD_SESSION"]["trade_flag"]
        expected_ttl_m = PATTERN_TAXONOMY["BAD_SESSION"]["ttl_minutes"]
        losses = _losses_with_flag(flag, 2)

        t_before = time.time()
        self.diag.diagnose_losses(losses)
        t_after = time.time()

        blocks = self.diag.get_active_blocks()
        self.assertIn("BAD_SESSION", blocks)
        expire = blocks["BAD_SESSION"]
        expected_min = t_before + expected_ttl_m * 60
        expected_max = t_after + expected_ttl_m * 60
        self.assertGreaterEqual(expire, expected_min)
        self.assertLessEqual(expire, expected_max)

    def test_diagnose_losses_refreshes_existing_block(self):
        """Calling diagnose_losses again on same pattern refreshes expiry."""
        flag = PATTERN_TAXONOMY["LATE_AFTER_IMPULSE"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)

        self.diag.diagnose_losses(losses)
        first_expire = self.diag.get_active_blocks()["LATE_AFTER_IMPULSE"]

        time.sleep(0.01)
        self.diag.diagnose_losses(losses)
        second_expire = self.diag.get_active_blocks()["LATE_AFTER_IMPULSE"]

        self.assertGreaterEqual(second_expire, first_expire)

    def test_clear_all_blocks_removes_everything(self):
        self.diag._raw_set_block("AGAINST_VWAP", time.time() + 3600.0)
        self.diag._raw_set_block("WEAK_CVD", time.time() + 3600.0)
        self.diag._clear_all_blocks()
        self.assertEqual(self.diag.get_active_blocks(), {})

    def test_get_ttl_remaining_minutes_positive_when_active(self):
        self.diag._raw_set_block("HIGH_SPREAD", time.time() + 30 * 60)
        ttl = self.diag.get_ttl_remaining_minutes("HIGH_SPREAD")
        self.assertGreater(ttl, 0.0)
        self.assertLessEqual(ttl, 30.0)

    def test_get_ttl_remaining_minutes_zero_when_not_blocked(self):
        ttl = self.diag.get_ttl_remaining_minutes("NONEXISTENT")
        self.assertEqual(ttl, 0.0)


# ---------------------------------------------------------------------------
# Recent loss window
# ---------------------------------------------------------------------------

class TestRecentLossWindow(unittest.TestCase):

    def setUp(self):
        self.diag = BtcSelfDiagnosis()

    def test_only_recent_window_losses_counted(self):
        """Losses beyond _RECENT_LOSS_WINDOW are ignored."""
        flag = PATTERN_TAXONOMY["HIGH_SPREAD"]["trade_flag"]
        # Put the two matching losses BEFORE the window
        old_losses = _losses_with_flag(flag, 2)
        padding = [_loss() for _ in range(_RECENT_LOSS_WINDOW)]
        losses = old_losses + padding  # old matches are outside the window
        activated = self.diag.diagnose_losses(losses)
        self.assertNotIn("HIGH_SPREAD", activated)

    def test_window_boundary_exact(self):
        """Exactly _RECENT_LOSS_WINDOW losses — all are in window."""
        flag = PATTERN_TAXONOMY["AGAINST_POC"]["trade_flag"]
        losses = _losses_with_flag(flag, _RECENT_LOSS_WINDOW)
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("AGAINST_POC", activated)


# ---------------------------------------------------------------------------
# Multiple patterns simultaneously
# ---------------------------------------------------------------------------

class TestMultiplePatterns(unittest.TestCase):

    def setUp(self):
        self.diag = BtcSelfDiagnosis()

    def test_multiple_patterns_detected_in_one_call(self):
        """Two different patterns in same loss batch both activate."""
        flag_vwap = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        flag_session = PATTERN_TAXONOMY["BAD_SESSION"]["trade_flag"]
        losses = [
            _loss(**{flag_vwap: True, flag_session: True}),
            _loss(**{flag_vwap: True, flag_session: True}),
        ]
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("AGAINST_VWAP", activated)
        self.assertIn("BAD_SESSION", activated)

    def test_one_pattern_threshold_met_other_not(self):
        """Only the pattern with >= threshold fires."""
        flag_vwap = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        flag_cvd = PATTERN_TAXONOMY["WEAK_CVD"]["trade_flag"]
        losses = [
            _loss(**{flag_vwap: True, flag_cvd: True}),
            _loss(**{flag_vwap: True}),   # second vwap but NOT cvd
        ]
        activated = self.diag.diagnose_losses(losses)
        self.assertIn("AGAINST_VWAP", activated)
        self.assertNotIn("WEAK_CVD", activated)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton(unittest.TestCase):

    def test_get_btc_self_diagnosis_returns_same_instance(self):
        inst1 = get_btc_self_diagnosis()
        inst2 = get_btc_self_diagnosis()
        self.assertIs(inst1, inst2)

    def test_get_btc_self_diagnosis_returns_btc_self_diagnosis_instance(self):
        inst = get_btc_self_diagnosis()
        self.assertIsInstance(inst, BtcSelfDiagnosis)


# ---------------------------------------------------------------------------
# Integration helper (log_active_diagnosis_blocks)
# ---------------------------------------------------------------------------

class TestLogIntegrationHelper(unittest.TestCase):

    def test_log_active_blocks_runs_without_error(self):
        """log_active_diagnosis_blocks() should not raise."""
        try:
            log_active_diagnosis_blocks("BTCUSD#", "BTC_SCALPING_AGENT")
        except Exception as exc:
            self.fail(f"log_active_diagnosis_blocks raised {exc}")


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    def test_concurrent_diagnose_losses_no_crash(self):
        """Multiple threads calling diagnose_losses concurrently must not crash."""
        diag = BtcSelfDiagnosis()
        flag = PATTERN_TAXONOMY["AGAINST_VWAP"]["trade_flag"]
        losses = _losses_with_flag(flag, 2)
        errors: list[Exception] = []

        def worker():
            try:
                diag.diagnose_losses(losses)
                diag.get_active_blocks()
                diag.is_blocked("AGAINST_VWAP")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Thread errors: {errors}")


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariants(unittest.TestCase):

    def test_no_mt5_import_in_self_diagnosis(self):
        import app.mt5.btc_self_diagnosis as mod
        src = inspect.getsource(mod)
        self.assertNotIn("import MetaTrader5", src)
        self.assertNotIn("import mt5", src)

    def test_no_order_send_in_self_diagnosis(self):
        import app.mt5.btc_self_diagnosis as mod
        src = inspect.getsource(mod)
        self.assertNotIn("mt5.order_send", src)
        self.assertNotIn("order_send(", src)

    def test_no_lot_size_modification(self):
        import app.mt5.btc_self_diagnosis as mod
        src = inspect.getsource(mod)
        self.assertNotIn("lot_size", src)
        self.assertNotIn("demo_max_lot", src)

    def test_no_live_trading_in_self_diagnosis(self):
        import app.mt5.btc_self_diagnosis as mod
        src = inspect.getsource(mod)
        self.assertNotIn("allow_live_trading = True", src)
        self.assertNotIn("allow_live_trading=True", src)

    def test_all_blocks_are_advisory_only(self):
        """The module docstring and log_active_diagnosis_blocks must state advisory."""
        import app.mt5.btc_self_diagnosis as mod
        src = inspect.getsource(mod)
        self.assertIn("advisory", src.lower())


if __name__ == "__main__":
    unittest.main()
