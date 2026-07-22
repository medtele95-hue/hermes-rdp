"""M05-P1 -- tests laboratoire : app/services/lifecycle_pnl.py
(LifecyclePnl / PnlLedger / reconcile_intent_lifecycle_pnl). 100% additif,
aucun fichier de production touche : tout le disque passe par
tempfile.TemporaryDirectory. Valeurs 100% fictives. Aucun MT5, aucun
order_send/order_check (meme simule), aucun reseau.
"""
from __future__ import annotations

import py_compile
import statistics
import tempfile
import time
import types
import unittest
from decimal import Decimal
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalReader
from app.services.lifecycle_identity_store import LifecycleIdentityStore, try_build_broker_ref
from app.services.lifecycle_pnl import (
    DEAL_KIND_ADJUSTMENT,
    DEAL_KIND_ENTRY,
    DEAL_KIND_EXIT,
    DealRecord,
    DealValidationError,
    LEDGER_OP,
    LifecyclePnl,
    LifecyclePnlError,
    PnlLedger,
    reconcile_intent_lifecycle_pnl,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "app" / "services" / "lifecycle_pnl.py"

T0 = "2026-07-22T02:00:00+00:00"
T1 = "2026-07-22T02:00:01+00:00"
T2 = "2026-07-22T02:00:02+00:00"
T3 = "2026-07-22T02:00:03+00:00"

ACCOUNT_SCOPE = "acct-v1-" + "a" * 32
CANARY_DEAL_ID = 999000111
CANARY_SYMBOL = "CANARY-FIXTURE#"


def _tmp_dir():
    return tempfile.TemporaryDirectory()


def _tmp_store(tmp):
    path = Path(tmp.name) / "store.json"
    return LifecycleIdentityStore(path=path, uuid_generator=MonotonicUUID7Generator())


def _make_lifecycle(
    store, *, ticket, position_identifier=None, symbol="GOLD#", magic=909333,
    direction="BUY", opened_at=T0, correlation_id=None, created_at=T0,
):
    record = store.create_lifecycle(correlation_id=correlation_id, created_at_utc=created_at)
    ref, reason = try_build_broker_ref(
        account_scope_id=ACCOUNT_SCOPE, ticket=ticket, broker_symbol=symbol,
        opened_at=opened_at, magic=magic, position_identifier=position_identifier,
        direction=direction,
    )
    assert ref is not None, reason
    store.bind_broker_position(record["lifecycle_id"], ref)
    return store.get_lifecycle(record["lifecycle_id"])


def _deal(
    deal_id, position_id, ticket, kind, volume, price, profit,
    commission="0", swap="0", fee="0", at_utc=T0, symbol="GOLD#",
):
    return DealRecord.from_mapping({
        "deal_id": deal_id, "position_id": position_id, "ticket": ticket, "kind": kind,
        "volume": volume, "price": price, "profit": profit, "commission": commission,
        "swap": swap, "fee": fee, "at_utc": at_utc, "symbol": symbol,
    })


class CompileTests(unittest.TestCase):
    def test_py_compile(self):
        py_compile.compile(str(_MODULE_PATH), doraise=True)


class PurityTests(unittest.TestCase):
    def test_module_purity(self):
        import inspect
        import re

        import app.services.lifecycle_pnl as mod

        src = inspect.getsource(mod)
        code_only = re.sub(r'"""[\s\S]*?"""', "", src)
        # NOTE: this module legitimately does `from datetime import datetime,
        # timezone` (needed for _parse_iso_utc's ISO parsing), so "import
        # datetime" is NOT in this forbidden list (unlike order_intent.py,
        # which imports no datetime symbol at all). `datetime.now` (wall
        # clock read) remains forbidden.
        for forbidden in (
            "MetaTrader5", "os.environ", "getenv", "socket", "subprocess",
            "datetime.now", "time.time", "import time",
            "order_send", "order_check", "import os",
        ):
            self.assertNotIn(forbidden, code_only)
        imported_modules = {
            name for name, value in vars(mod).items() if isinstance(value, types.ModuleType)
        }
        self.assertEqual(imported_modules, {"hashlib", "json", "threading"})


# --------------------------------------------------------------------------- #
# DealRecord validation
# --------------------------------------------------------------------------- #
class DealValidationTests(unittest.TestCase):
    def _base(self, **overrides):
        base = {
            "deal_id": 1, "position_id": 10, "ticket": 100, "kind": DEAL_KIND_ENTRY,
            "volume": "1.00", "price": "2000.00", "profit": "0", "commission": "0",
            "swap": "0", "fee": "0", "at_utc": T0, "symbol": "GOLD#",
        }
        base.update(overrides)
        return base

    def test_valid_deal_roundtrips(self):
        d = DealRecord.from_mapping(self._base())
        self.assertEqual(d.deal_id, 1)
        self.assertEqual(d.volume, Decimal("1.00"))
        self.assertEqual(d.price, Decimal("2000.00"))
        self.assertEqual(d.at_utc, T0)

    def test_missing_field_rejected(self):
        raw = self._base()
        del raw["profit"]
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(raw)

    def test_unknown_field_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(bogus=1))

    def test_not_a_dict_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(["not", "a", "dict"])

    def test_deal_id_bool_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(deal_id=True))

    def test_deal_id_zero_or_negative_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(deal_id=bad))

    def test_deal_id_string_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(deal_id="1"))

    def test_position_id_invalid_rejected(self):
        for bad in (0, -5, True, "10", 1.5):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(position_id=bad))

    def test_ticket_invalid_rejected(self):
        for bad in (0, -5, True, "100"):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(ticket=bad))

    def test_kind_invalid_rejected(self):
        for bad in ("BUY", "entry", 1, None):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(kind=bad))

    def test_kind_adjustment_accepted(self):
        d = DealRecord.from_mapping(self._base(kind=DEAL_KIND_ADJUSTMENT))
        self.assertEqual(d.kind, DEAL_KIND_ADJUSTMENT)

    def test_volume_float_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(volume=1.0))

    def test_volume_bool_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(volume=True))

    def test_volume_zero_or_negative_rejected(self):
        for bad in ("0", "-1.0", 0, -1):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(volume=bad))

    def test_volume_as_decimal_instance_accepted(self):
        d = DealRecord.from_mapping(self._base(volume=Decimal("0.10")))
        self.assertEqual(d.volume, Decimal("0.10"))

    def test_volume_as_int_accepted_exact(self):
        d = DealRecord.from_mapping(self._base(volume=2))
        self.assertEqual(d.volume, Decimal(2))

    def test_price_zero_or_negative_rejected(self):
        for bad in ("0", "-100"):
            with self.assertRaises(DealValidationError):
                DealRecord.from_mapping(self._base(price=bad))

    def test_price_non_numeric_string_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(price="not-a-number"))

    def test_profit_float_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(profit=1.5))

    def test_commission_negative_accepted(self):
        d = DealRecord.from_mapping(self._base(commission="-4.50"))
        self.assertEqual(d.commission, Decimal("-4.50"))

    def test_swap_negative_accepted(self):
        d = DealRecord.from_mapping(self._base(swap="-1.25"))
        self.assertEqual(d.swap, Decimal("-1.25"))

    def test_fee_nan_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(fee=Decimal("NaN")))

    def test_fee_infinite_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(fee=Decimal("Infinity")))

    def test_at_utc_naive_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(at_utc="2026-07-22T02:00:00"))

    def test_at_utc_not_string_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(at_utc=12345))

    def test_symbol_empty_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(symbol=""))

    def test_symbol_not_string_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord.from_mapping(self._base(symbol=42))

    def test_direct_constructor_noncanonical_at_utc_rejected(self):
        # Direct dataclass construction (bypassing from_mapping's coercion)
        # still enforces the canonical isoformat() fixpoint via __post_init__.
        with self.assertRaises(DealValidationError):
            DealRecord(
                deal_id=1, position_id=10, ticket=100, kind=DEAL_KIND_ENTRY,
                volume=Decimal("1"), price=Decimal("2000"), profit=Decimal("0"),
                commission=Decimal("0"), swap=Decimal("0"), fee=Decimal("0"),
                at_utc="2026-07-22T02:00:00Z", symbol="GOLD#",
            )

    def test_direct_constructor_float_money_rejected(self):
        with self.assertRaises(DealValidationError):
            DealRecord(
                deal_id=1, position_id=10, ticket=100, kind=DEAL_KIND_ENTRY,
                volume=1.0, price=2000.0, profit=0.0,
                commission=0.0, swap=0.0, fee=0.0,
                at_utc=T0, symbol="GOLD#",
            )


# --------------------------------------------------------------------------- #
# LifecyclePnl.compute -- exact Decimal aggregation
# --------------------------------------------------------------------------- #
class AggregationExactTests(unittest.TestCase):
    def test_simple_entry_exit(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        deals = [
            _deal(1, 555, 100, DEAL_KIND_ENTRY, "1.00", "2000.00", "0",
                  commission="-4.50", swap="0", at_utc=T0),
            _deal(2, 555, 100, DEAL_KIND_EXIT, "1.00", "2015.37", "1537.00",
                  commission="-4.50", swap="-1.25", at_utc=T1),
        ]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["gross_profit"], Decimal("1537.00"))
        self.assertEqual(result["commission"], Decimal("-9.00"))
        self.assertEqual(result["swap"], Decimal("-1.25"))
        self.assertEqual(result["fees"], Decimal("0.00"))
        self.assertEqual(result["net_profit"], Decimal("1526.75"))
        self.assertEqual(result["volume_entered"], Decimal("1.00"))
        self.assertEqual(result["volume_exited"], Decimal("1.00"))
        self.assertEqual(result["entry_avg_price"], Decimal("2000.00"))
        self.assertEqual(result["exit_avg_price"], Decimal("2015.37"))
        self.assertTrue(result["flags"]["fully_closed"])
        self.assertEqual(result["flags"]["orphan_deals"], 0)
        self.assertEqual(result["flags"]["integrity"], "OK")
        self.assertEqual([t["deal_id"] for t in result["timeline"]], [1, 2])
        tmp.cleanup()

    def test_partial_multiple_fills_weighted_average(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=200, position_identifier=777)
        deals = [
            _deal(1, 777, 200, DEAL_KIND_ENTRY, "0.5", "2000", "0", at_utc=T0),
            _deal(2, 777, 200, DEAL_KIND_ENTRY, "0.5", "2010", "0", at_utc=T1),
            _deal(3, 777, 200, DEAL_KIND_EXIT, "0.3", "2020", "6.00", at_utc=T2),
            _deal(4, 777, 200, DEAL_KIND_EXIT, "0.2", "2030", "6.00", at_utc=T3),
        ]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["volume_entered"], Decimal("1.0"))
        self.assertEqual(result["volume_exited"], Decimal("0.5"))
        self.assertEqual(result["entry_avg_price"], Decimal("2005"))
        self.assertEqual(result["exit_avg_price"], Decimal("2024"))
        self.assertFalse(result["flags"]["fully_closed"])
        self.assertEqual(result["gross_profit"], Decimal("12.00"))

    def test_rounding_half_even_on_final_aggregate_only(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=300, position_identifier=1)
        # 1000.125 -> ties to even -> 1000.12 (2 is even)
        deals = [_deal(1, 1, 300, DEAL_KIND_ENTRY, "1", "1", "1000.125")]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["net_profit"], Decimal("1000.12"))
        self.assertEqual(result["gross_profit"], Decimal("1000.12"))
        # 1000.135 -> ties to even -> 1000.14 (4 is even)
        deals2 = [_deal(1, 1, 300, DEAL_KIND_ENTRY, "1", "1", "1000.135")]
        result2 = LifecyclePnl.compute(record, deals2)
        self.assertEqual(result2["net_profit"], Decimal("1000.14"))

    def test_adjustment_deal_included_in_money_excluded_from_volume(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=400, position_identifier=9)
        deals = [
            _deal(1, 9, 400, DEAL_KIND_ENTRY, "1", "2000", "0", at_utc=T0),
            _deal(2, 9, 400, DEAL_KIND_ADJUSTMENT, "0.01", "1", "-3.00", at_utc=T1),
        ]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["gross_profit"], Decimal("-3.00"))
        self.assertEqual(result["volume_entered"], Decimal("1"))
        self.assertEqual(result["volume_exited"], Decimal("0"))

    def test_empty_deals_zero_aggregate_fully_closed_trivially(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=500, position_identifier=1)
        result = LifecyclePnl.compute(record, [])
        self.assertEqual(result["net_profit"], Decimal("0.00"))
        self.assertTrue(result["flags"]["fully_closed"])
        self.assertEqual(result["flags"]["orphan_deals"], 0)
        self.assertEqual(result["flags"]["integrity"], "OK")
        self.assertIsNone(result["entry_avg_price"])
        self.assertIsNone(result["exit_avg_price"])


# --------------------------------------------------------------------------- #
# Matching -- position_id vs composite key, NEVER ticket alone
# --------------------------------------------------------------------------- #
class MatchingTests(unittest.TestCase):
    def test_position_id_branch_ignores_ticket_recycled_ticket_orphaned(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555, symbol="GOLD#")
        deals = [
            _deal(1, 555, 100, DEAL_KIND_ENTRY, "1", "2000", "0", symbol="GOLD#"),
            # SAME ticket (100), DIFFERENT position_id -> must be orphaned,
            # never matched by ticket alone.
            _deal(2, 999, 100, DEAL_KIND_EXIT, "1", "2010", "10", symbol="GOLD#"),
        ]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["flags"]["orphan_deals"], 1)
        self.assertEqual(result["rejected"], [{"deal_id": 2, "reason": "POSITION_ID_MISMATCH"}])
        self.assertEqual([t["deal_id"] for t in result["timeline"]], [1])
        self.assertEqual(result["flags"]["integrity"], "MISMATCH_ORPHAN_DEALS")

    def test_symbol_mismatch_orphaned_even_with_matching_position_id(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555, symbol="GOLD#")
        deals = [_deal(1, 555, 100, DEAL_KIND_ENTRY, "1", "2000", "0", symbol="EURUSD#")]
        result = LifecyclePnl.compute(record, deals)
        self.assertEqual(result["rejected"], [{"deal_id": 1, "reason": "SYMBOL_MISMATCH"}])

    def test_no_position_identifier_falls_back_to_ticket_and_symbol_composite(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=222, position_identifier=None, symbol="GOLD#")
        matching = _deal(1, 42, 222, DEAL_KIND_ENTRY, "1", "2000", "0", symbol="GOLD#")
        wrong_ticket = _deal(2, 43, 223, DEAL_KIND_EXIT, "1", "2010", "10", symbol="GOLD#")
        result = LifecyclePnl.compute(record, [matching, wrong_ticket])
        self.assertEqual(result["flags"]["orphan_deals"], 1)
        self.assertEqual(result["rejected"], [{"deal_id": 2, "reason": "TICKET_MISMATCH"}])
        self.assertEqual([t["deal_id"] for t in result["timeline"]], [1])

    def test_unbound_lifecycle_orphans_all_offered_deals(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = store.create_lifecycle(correlation_id=None, created_at_utc=T0)
        deal = _deal(1, 1, 1, DEAL_KIND_ENTRY, "1", "2000", "0")
        result = LifecyclePnl.compute(record, [deal])
        self.assertEqual(result["rejected"], [{"deal_id": 1, "reason": "NO_BROKER_REF"}])
        self.assertEqual(result["flags"]["integrity"], "MISMATCH_ORPHAN_DEALS")

    def test_tampered_broker_key_yields_mismatch_broker_ref(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        tampered = dict(record)
        tampered["broker_key"] = "brkref-v1|tampered"
        deal = _deal(1, 555, 100, DEAL_KIND_ENTRY, "1", "2000", "0")
        result = LifecyclePnl.compute(tampered, [deal])
        self.assertEqual(result["flags"]["integrity"], "MISMATCH_BROKER_REF")
        self.assertEqual(result["rejected"], [{"deal_id": 1, "reason": "NO_BROKER_REF"}])

    def test_deals_must_be_dealrecord_instances(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        with self.assertRaises(LifecyclePnlError):
            LifecyclePnl.compute(record, [{"deal_id": 1}])

    def test_lifecycle_record_missing_id_rejected(self):
        with self.assertRaises(LifecyclePnlError):
            LifecyclePnl.compute({}, [])


# --------------------------------------------------------------------------- #
# PnlLedger -- idempotence by digest, versioned recompute, replay equivalence
# --------------------------------------------------------------------------- #
class LedgerTests(unittest.TestCase):
    def _record_and_deals(self, store):
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        deals = [
            _deal(1, 555, 100, DEAL_KIND_ENTRY, "1", "2000", "0", commission="-5", at_utc=T0),
            _deal(2, 555, 100, DEAL_KIND_EXIT, "1", "2100", "100", commission="-5", at_utc=T1),
        ]
        return record, deals

    def test_idempotent_recompute_same_digest(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        first = ledger.compute_and_record(record, deals)
        self.assertEqual(first["ledger_status"], "RECORDED")
        second = ledger.compute_and_record(record, deals)
        self.assertEqual(second["ledger_status"], "ALREADY_COMPUTED")
        self.assertEqual(first["pnl"]["net_profit"], second["pnl"]["net_profit"])
        verify = JournalReader(ledger_path).verify()
        self.assertEqual(verify["records"], 1)  # second call wrote NOTHING
        ledger.close()
        tmp.cleanup()

    def test_different_deals_digest_creates_versioned_entry_never_overwrites(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        first = ledger.compute_and_record(record, deals)
        more_deals = deals + [
            _deal(3, 555, 100, DEAL_KIND_ADJUSTMENT, "0.01", "1", "-1.00", at_utc=T2)
        ]
        second = ledger.compute_and_record(record, more_deals)
        self.assertEqual(second["ledger_status"], "RECORDED")
        self.assertNotEqual(first["deals_digest"], second["deals_digest"])
        history = ledger.history(record["lifecycle_id"])
        self.assertEqual(len(history), 2)
        self.assertEqual({h["deals_digest"] for h in history},
                          {first["deals_digest"], second["deals_digest"]})
        verify = JournalReader(ledger_path).verify()
        self.assertEqual(verify["records"], 2)
        ledger.close()
        tmp.cleanup()

    def test_rebuild_after_restart_preserves_idempotence(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        first = ledger.compute_and_record(record, deals)
        ledger.close()

        reopened = PnlLedger(ledger_path)
        replay_status = reopened.rebuild()
        self.assertEqual(replay_status["applied"], 1)
        again = reopened.compute_and_record(record, deals)
        self.assertEqual(again["ledger_status"], "ALREADY_COMPUTED")
        verify = JournalReader(ledger_path).verify()
        self.assertEqual(verify["records"], 1)
        reopened.close()
        tmp.cleanup()

    def test_rebuild_is_pure_and_idempotent(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        ledger.compute_and_record(record, deals)
        r1 = ledger.rebuild()
        r2 = ledger.rebuild()
        self.assertEqual(r1, r2)
        ledger.close()
        tmp.cleanup()

    def test_snapshot_shape(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        ledger.compute_and_record(record, deals)
        snap = ledger.snapshot()
        self.assertIn("lifecycles", snap)
        self.assertIn(record["lifecycle_id"], snap["lifecycles"])
        self.assertEqual(len(snap["lifecycles"][record["lifecycle_id"]]), 1)
        ledger.close()
        tmp.cleanup()

    def test_payload_op_is_pnl_computed(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record, deals = self._record_and_deals(store)
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        ledger.compute_and_record(record, deals)
        payloads = JournalReader(ledger_path).replay()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["op"], LEDGER_OP)
        self.assertIsInstance(payloads[0]["net_profit"], str)
        ledger.close()
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# reconcile_intent_lifecycle_pnl -- pure detection, each anomaly + healthy
# --------------------------------------------------------------------------- #
class ReconciliationTests(unittest.TestCase):
    def test_healthy_scenario_zero_anomalies(self):
        lifecycles = {"L1": {"lifecycle_id": "L1", "correlation_id": "C1", "state": "CLOSED"}}
        intents = [{
            "intent_id": "I1", "state": "FILLED",
            "request": {"volume": 0.1},
            "links": {"lifecycle_id": "L1", "correlation_id": None, "setup_id": None},
        }]
        ledger = {"lifecycles": {"L1": [{"net_profit": "10.00", "computed_over_deals": 2,
                                          "deals_digest": "abc123"}]}}
        result = reconcile_intent_lifecycle_pnl({"intents": intents}, {"lifecycles": lifecycles}, ledger)
        self.assertEqual(result["anomalies"], [])
        self.assertEqual(result["counts"], {})
        self.assertEqual(result["checked"], {"intents": 1, "lifecycles": 1, "ledger_lifecycles": 1})

    def test_intent_filled_sans_lifecycle(self):
        intents = [{"intent_id": "I1", "state": "FILLED", "request": {"volume": 0.1}, "links": None}]
        result = reconcile_intent_lifecycle_pnl(
            {"intents": intents}, {"lifecycles": {}}, {"lifecycles": {}})
        types_seen = {a["type"] for a in result["anomalies"]}
        self.assertIn("INTENT_FILLED_SANS_LIFECYCLE", types_seen)
        self.assertEqual(result["counts"]["INTENT_FILLED_SANS_LIFECYCLE"], 1)

    def test_intent_lifecycle_unknown(self):
        intents = [{
            "intent_id": "I1", "state": "CREATED", "request": {"volume": 0.1},
            "links": {"lifecycle_id": "L_GHOST", "correlation_id": None, "setup_id": None},
        }]
        result = reconcile_intent_lifecycle_pnl(
            {"intents": intents}, {"lifecycles": {}}, {"lifecycles": {}})
        types_seen = {a["type"] for a in result["anomalies"]}
        self.assertIn("INTENT_LIFECYCLE_UNKNOWN", types_seen)

    def test_volume_mismatch_between_two_intents_same_lifecycle(self):
        lifecycles = {"L1": {"lifecycle_id": "L1", "correlation_id": None, "state": "OPEN"}}
        intents = [
            {"intent_id": "I1", "state": "SUBMITTED", "request": {"volume": 0.1},
             "links": {"lifecycle_id": "L1", "correlation_id": None, "setup_id": None}},
            {"intent_id": "I2", "state": "SUBMITTED", "request": {"volume": 0.2},
             "links": {"lifecycle_id": "L1", "correlation_id": None, "setup_id": None}},
        ]
        result = reconcile_intent_lifecycle_pnl(
            {"intents": intents}, {"lifecycles": lifecycles}, {"lifecycles": {}})
        types_seen = {a["type"] for a in result["anomalies"]}
        self.assertIn("VOLUME_MISMATCH", types_seen)

    def test_lifecycle_closed_sans_pnl(self):
        lifecycles = {"L1": {"lifecycle_id": "L1", "correlation_id": None, "state": "CLOSED"}}
        result = reconcile_intent_lifecycle_pnl(
            {"intents": []}, {"lifecycles": lifecycles}, {"lifecycles": {}})
        types_seen = {a["type"] for a in result["anomalies"]}
        self.assertIn("LIFECYCLE_CLOSED_SANS_PNL", types_seen)

    def test_pnl_sans_lifecycle(self):
        ledger = {"lifecycles": {"L_ORPHAN": [{"net_profit": "1.00"}]}}
        result = reconcile_intent_lifecycle_pnl({"intents": []}, {"lifecycles": {}}, ledger)
        types_seen = {a["type"] for a in result["anomalies"]}
        self.assertIn("PNL_SANS_LIFECYCLE", types_seen)

    def test_correlation_based_resolution_no_anomaly(self):
        lifecycles = {"L1": {"lifecycle_id": "L1", "correlation_id": "CORR1", "state": "OPEN"}}
        intents = [{
            "intent_id": "I1", "state": "FILLED", "request": {"volume": 0.1},
            "links": {"lifecycle_id": None, "correlation_id": "CORR1", "setup_id": None},
        }]
        result = reconcile_intent_lifecycle_pnl(
            {"intents": intents}, {"lifecycles": lifecycles}, {"lifecycles": {}})
        self.assertEqual(
            [a for a in result["anomalies"] if a["type"] == "INTENT_FILLED_SANS_LIFECYCLE"], [])

    def test_malformed_intent_book_state_rejected(self):
        with self.assertRaises(LifecyclePnlError):
            reconcile_intent_lifecycle_pnl({}, {"lifecycles": {}}, {"lifecycles": {}})

    def test_malformed_store_state_rejected(self):
        with self.assertRaises(LifecyclePnlError):
            reconcile_intent_lifecycle_pnl({"intents": []}, {}, {"lifecycles": {}})

    def test_malformed_ledger_state_rejected(self):
        with self.assertRaises(LifecyclePnlError):
            reconcile_intent_lifecycle_pnl({"intents": []}, {"lifecycles": {}}, {})


# --------------------------------------------------------------------------- #
# Canary absence -- no fictitious value leaks into module code or journal
# --------------------------------------------------------------------------- #
class CanaryAbsenceTests(unittest.TestCase):
    def test_canaries_absent_from_source(self):
        import inspect

        import app.services.lifecycle_pnl as mod

        src = inspect.getsource(mod)
        self.assertNotIn(str(CANARY_DEAL_ID), src)
        self.assertNotIn(CANARY_SYMBOL, src)

    def test_canaries_absent_from_journal_content(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        deals = [_deal(1, 555, 100, DEAL_KIND_ENTRY, "1", "2000", "0")]
        ledger_path = Path(tmp.name) / "pnl_ledger.jsonl"
        ledger = PnlLedger(ledger_path)
        ledger.compute_and_record(record, deals)
        ledger.close()
        raw = ledger_path.read_text(encoding="utf-8")
        self.assertNotIn(str(CANARY_DEAL_ID), raw)
        self.assertNotIn(CANARY_SYMBOL, raw)
        tmp.cleanup()


# --------------------------------------------------------------------------- #
# Bench -- light
# --------------------------------------------------------------------------- #
class BenchTests(unittest.TestCase):
    def test_compute_bench(self):
        tmp = _tmp_dir()
        store = _tmp_store(tmp)
        record = _make_lifecycle(store, ticket=100, position_identifier=555)
        deals = [
            _deal(i, 555, 100, DEAL_KIND_ENTRY if i % 2 == 0 else DEAL_KIND_EXIT,
                  "0.1", "2000", "1.00", at_utc=T0)
            for i in range(1, 21)
        ]
        samples = []
        for _ in range(30):
            start = time.perf_counter()
            LifecyclePnl.compute(record, deals)
            samples.append((time.perf_counter() - start) * 1000)
        median = statistics.median(samples)
        print("[M05-P1 BENCH] LifecyclePnl.compute median=%.4fms (n=%d, %d deals)"
              % (median, len(samples), len(deals)))
        self.assertLess(median, 20.0)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
