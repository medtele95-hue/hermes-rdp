"""M07-P1 — Campaign 4/7: PNL (M05 ``lifecycle_pnl``) chaos.

Proves EXACT ``Decimal`` money discipline under adverse deal amounts (tiny
sub-cent volumes, huge profits, zero amounts, exact banker's-rounding tie
boundaries -- never a float round-trip / never an off-by-a-cent drift), a
journal-in-panne during ``PnlLedger.compute_and_record`` never marks a
digest "seen" without actually journaling it (so a retry always records,
never silently no-ops), a corrupted ledger journal always fails closed
(never a silent partial load), a tampered/mismatched ``broker_ref``/
``broker_key`` degrades safely (never crashes, always typed integrity), and
``reconcile_intent_lifecycle_pnl`` never crashes on adverse/duplicate/
boundary-shaped input.

100% additive, no ``app/`` file touched. All disk I/O goes through
``tempfile.TemporaryDirectory``. All values are 100% fictional.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from chaos_tools import ByteCorruptor, FaultyOS, Ledger, faulty_existing_handle

from app.services.event_journal import JournalCorruptedError, JournalPoisonedError
from app.services.lifecycle_identity_store import try_build_broker_ref
from app.services.lifecycle_pnl import (
    DealRecord,
    DealValidationError,
    LifecyclePnl,
    LifecyclePnlError,
    PnlLedger,
    reconcile_intent_lifecycle_pnl,
)

T0 = "2026-07-22T02:00:00+00:00"
T1 = "2026-07-22T02:00:01+00:00"
ACCOUNT_SCOPE = "acct-v1-" + "07" * 16

LEDGER = Ledger()


def _lifecycle_record(
    lifecycle_id="lc-chaos-0001", *, ticket=700001, position_identifier=800001,
    symbol="GOLD#", magic=909707, direction="BUY", opened_at=T0,
):
    ref, reason = try_build_broker_ref(
        account_scope_id=ACCOUNT_SCOPE, ticket=ticket, broker_symbol=symbol,
        opened_at=opened_at, magic=magic, position_identifier=position_identifier,
        direction=direction,
    )
    assert reason is None, reason
    from app.services.lifecycle_identity_store import broker_ref_key

    return {"lifecycle_id": lifecycle_id, "broker_ref": ref, "broker_key": broker_ref_key(ref)}


def _deal(deal_id, *, position_id=800001, ticket=700001, kind="ENTRY", volume="0.01",
          price="2000.00", profit="0", commission="0", swap="0", fee="0",
          at_utc=T0, symbol="GOLD#"):
    return DealRecord.from_mapping({
        "deal_id": deal_id, "position_id": position_id, "ticket": ticket, "kind": kind,
        "volume": volume, "price": price, "profit": profit, "commission": commission,
        "swap": swap, "fee": fee, "at_utc": at_utc, "symbol": symbol,
    })


# --------------------------------------------------------------------------- #
# Exact Decimal money discipline under adverse amounts
# --------------------------------------------------------------------------- #
class DecimalPrecisionChaosTests(unittest.TestCase):
    def test_classic_float_trap_sums_exact_via_decimal(self) -> None:
        lc = _lifecycle_record()
        deals = [
            _deal(1, kind="EXIT", profit="0.10"),
            _deal(2, kind="EXIT", profit="0.20"),
            _deal(3, kind="EXIT", profit="0.30"),
        ]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["gross_profit"], Decimal("0.60"))
        self.assertEqual(result["net_profit"], Decimal("0.60"))

    def test_huge_and_tiny_amounts_no_precision_loss(self) -> None:
        lc = _lifecycle_record()
        deals = [
            _deal(1, kind="EXIT", profit="999999999999.99", volume="0.00000001"),
            _deal(2, kind="EXIT", profit="-999999999999.98", volume="0.00000001"),
        ]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["net_profit"], Decimal("0.01"))
        self.assertEqual(result["volume_exited"], Decimal("0.00000002"))  # full precision, unrounded

    def test_zero_amounts_exact(self) -> None:
        lc = _lifecycle_record()
        deals = [_deal(1, kind="EXIT", profit="0.00", commission="0.00", swap="0.00", fee="0.00")]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["net_profit"], Decimal("0.00"))

    def test_many_tiny_deals_exact_sum(self) -> None:
        lc = _lifecycle_record()
        deals = [_deal(i, kind="EXIT", profit="0.001") for i in range(1, 501)]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["net_profit"], Decimal("0.50"))  # 500 * 0.001 = 0.500, exact

    def test_banker_rounding_boundary_ties_to_even(self) -> None:
        # 1.005 -> tie between 1.00 (even) and 1.01 (odd) -> 1.00
        lc1 = _lifecycle_record(lifecycle_id="lc-tie-down")
        r1 = LifecyclePnl.compute(lc1, [_deal(1, kind="EXIT", profit="1.005")])
        self.assertEqual(r1["net_profit"], Decimal("1.00"))

        # 2.015 -> tie between 2.01 (odd) and 2.02 (even) -> 2.02
        lc2 = _lifecycle_record(lifecycle_id="lc-tie-up")
        r2 = LifecyclePnl.compute(lc2, [_deal(2, kind="EXIT", profit="2.015")])
        self.assertEqual(r2["net_profit"], Decimal("2.02"))

    def test_float_profit_rejected_never_silently_coerced(self) -> None:
        with self.assertRaises(DealValidationError) as cm:
            DealRecord.from_mapping({
                "deal_id": 1, "position_id": 800001, "ticket": 700001, "kind": "EXIT",
                "volume": "0.01", "price": "2000.00", "profit": 0.1,  # float -- rejected
                "commission": "0", "swap": "0", "fee": "0", "at_utc": T0, "symbol": "GOLD#",
            })
        self.assertTrue(LEDGER.record(cm.exception))
        with self.assertRaises(DealValidationError) as cm2:
            DealRecord.from_mapping({
                "deal_id": 1, "position_id": 800001, "ticket": 700001, "kind": "EXIT",
                "volume": "0.01", "price": "2000.00", "profit": True,  # bool -- rejected
                "commission": "0", "swap": "0", "fee": "0", "at_utc": T0, "symbol": "GOLD#",
            })
        self.assertTrue(LEDGER.record(cm2.exception))


# --------------------------------------------------------------------------- #
# PnlLedger journal-in-panne
# --------------------------------------------------------------------------- #
class PnlLedgerFaultTests(unittest.TestCase):
    def test_ledger_journal_fault_never_marks_seen_retry_records(self) -> None:
        """FIXED (M07-P2): event_journal.JournalWriter now self-poisons on
        ANY I/O exception (including a fully-clean write-fault) -- retrying
        on the SAME (un-reopened) ledger instance is refused synchronously
        by JournalPoisonedError, never a silent write, and never a digest
        marked "seen" without it. The documented recovery is close()+a NEW
        ``PnlLedger`` over the same path (its ``__init__`` already rebuilds
        the dedup index from the journal)."""
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(path)
        lc = _lifecycle_record()
        deals = [_deal(1, kind="ENTRY"), _deal(2, kind="EXIT", price="2015.00", profit="15.00")]

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(ledger._writer, "_fh", faulty):
                with self.assertRaises(OSError) as cm:
                    ledger.compute_and_record(lc, deals)
        self.assertTrue(LEDGER.record(cm.exception))

        self.assertEqual(ledger.history(lc["lifecycle_id"]), [])
        self.assertTrue(ledger._writer.poisoned)

        with self.assertRaises(JournalPoisonedError) as cm2:
            ledger.compute_and_record(lc, deals)
        self.assertTrue(LEDGER.record(cm2.exception))
        self.assertEqual(ledger.history(lc["lifecycle_id"]), [])
        ledger.close()

        # documented recovery: close()+reopen a NEW ledger over the same path.
        fresh = PnlLedger(path)
        self.assertEqual(fresh.history(lc["lifecycle_id"]), [])
        result = fresh.compute_and_record(lc, deals)
        self.assertEqual(result["ledger_status"], "RECORDED")
        result2 = fresh.compute_and_record(lc, deals)
        self.assertEqual(result2["ledger_status"], "ALREADY_COMPUTED")
        fresh.close()
        tmp.cleanup()

    def test_ledger_rebuild_on_corrupted_journal_sweep_fails_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(path)
        lc1 = _lifecycle_record(lifecycle_id="lc-a", ticket=700001, position_identifier=800001)
        lc2 = _lifecycle_record(lifecycle_id="lc-b", ticket=700002, position_identifier=800002)
        ledger.compute_and_record(lc1, [_deal(1, position_id=800001, ticket=700001, kind="EXIT", profit="5.00")])
        ledger.compute_and_record(lc2, [_deal(2, position_id=800002, ticket=700002, kind="EXIT", profit="7.00")])
        ledger.close()

        baseline = path.read_bytes()
        step = max(1, len(baseline) // 8)
        positions = [p for p in ByteCorruptor.sweep_positions(len(baseline), step)
                     if p < int(len(baseline) * 0.6)]
        self.assertGreaterEqual(len(positions), 3)

        for pos in positions:
            path.write_bytes(ByteCorruptor.flip_byte(baseline, pos))
            with self.subTest(position=pos):
                with self.assertRaises(JournalCorruptedError) as cm:
                    PnlLedger(path)
                self.assertTrue(LEDGER.record(cm.exception))
            path.write_bytes(baseline)

        healthy = PnlLedger(path)
        self.assertEqual(len(healthy.history("lc-a")), 1)
        self.assertEqual(len(healthy.history("lc-b")), 1)
        healthy.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Matching integrity under a tampered/mismatched broker_ref
# --------------------------------------------------------------------------- #
class IntegrityDegradationTests(unittest.TestCase):
    def test_mismatched_broker_key_degrades_safely_never_crashes(self) -> None:
        ref, reason = try_build_broker_ref(
            account_scope_id=ACCOUNT_SCOPE, ticket=1, broker_symbol="GOLD#",
            opened_at=T0, magic=1, position_identifier=1, direction="BUY",
        )
        assert reason is None, reason
        lc = {"lifecycle_id": "lc-mismatch", "broker_ref": ref, "broker_key": "tampered-not-matching"}
        deals = [_deal(1, position_id=1, ticket=1, kind="ENTRY", profit="100.00")]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["flags"]["integrity"], "MISMATCH_BROKER_REF")
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["net_profit"], Decimal("0.00"))

    def test_structurally_invalid_broker_ref_dict_degrades_safely(self) -> None:
        lc = {"lifecycle_id": "lc-garbage-ref", "broker_ref": {"garbage": True}, "broker_key": "whatever"}
        deals = [_deal(1, position_id=1, ticket=1, kind="ENTRY", profit="1.00")]
        result = LifecyclePnl.compute(lc, deals)
        self.assertEqual(result["flags"]["integrity"], "MISMATCH_BROKER_REF")
        self.assertEqual(result["net_profit"], Decimal("0.00"))


# --------------------------------------------------------------------------- #
# reconcile_intent_lifecycle_pnl: adverse/duplicate/boundary input
# --------------------------------------------------------------------------- #
class ReconcileAdverseInputTests(unittest.TestCase):
    def test_malformed_top_level_shape_typed_error(self) -> None:
        with self.assertRaises(LifecyclePnlError) as cm:
            reconcile_intent_lifecycle_pnl({"not_intents": []}, {"lifecycles": {}}, {"lifecycles": {}})
        self.assertTrue(LEDGER.record(cm.exception))
        with self.assertRaises(LifecyclePnlError) as cm2:
            reconcile_intent_lifecycle_pnl({"intents": []}, {"not_lifecycles": {}}, {"lifecycles": {}})
        self.assertTrue(LEDGER.record(cm2.exception))

    def test_duplicate_and_boundary_intents_never_crash_always_report(self) -> None:
        lifecycles = {"lc-x": {"lifecycle_id": "lc-x", "state": "CLOSED", "correlation_id": None}}
        intents = [
            {"intent_id": "i1", "state": "FILLED", "request": {"volume": 0.01},
             "links": {"lifecycle_id": "lc-x"}},
            {"intent_id": "i1", "state": "FILLED", "request": {"volume": 0.01},  # exact duplicate
             "links": {"lifecycle_id": "lc-x"}},
            {"intent_id": "i2", "state": "FILLED", "request": {"volume": 0.02},  # volume mismatch
             "links": {"lifecycle_id": "lc-x"}},
            {"weird": "shape-missing-most-fields"},  # boundary: no intent_id/state/links
            None,  # boundary: not even a dict
        ]
        report = reconcile_intent_lifecycle_pnl({"intents": intents}, {"lifecycles": lifecycles}, {"lifecycles": {}})
        self.assertIn("VOLUME_MISMATCH", report["counts"])
        self.assertIn("LIFECYCLE_CLOSED_SANS_PNL", report["counts"])
        self.assertEqual(report["checked"]["intents"], 5)


# --------------------------------------------------------------------------- #
# Transversal: zero untyped exceptions observed
# --------------------------------------------------------------------------- #
class ZZPnlCampaignLedgerTests(unittest.TestCase):
    def test_zz_no_untyped_exceptions_observed(self) -> None:
        self.assertEqual(LEDGER.untyped, [], LEDGER.describe_untyped())
        self.assertGreater(LEDGER.total, 0)


if __name__ == "__main__":
    unittest.main()
