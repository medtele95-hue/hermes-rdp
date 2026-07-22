"""M06-P1 -- tests laboratoire : app/services/reconciliation_lab.py
(BrokerSnapshot / Reconciler / RestartSimulator). 100% additif, aucun
fichier de production touche : tout le disque passe par
tempfile.TemporaryDirectory. Valeurs 100% fictives. Aucun MT5, aucun
order_send/order_check (meme simule), aucun reseau.
"""
from __future__ import annotations

import py_compile
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from app.services.event_journal import JournalWriter
from app.services.lifecycle_identity_store import LifecycleIdentityStore, try_build_broker_ref
from app.services.lifecycle_pnl import DealRecord
from app.services.reconciliation_lab import (
    ANOMALY_INTENT_FILLED_SANS_LIFECYCLE,
    ANOMALY_INTENT_LIFECYCLE_UNKNOWN,
    ANOMALY_INTENT_STUCK_SUBMITTED,
    ANOMALY_LEDGER_LIFECYCLE_GAP,
    ANOMALY_ORPHAN_BROKER_POSITION,
    ANOMALY_STALE_OPEN_LIFECYCLE,
    ANOMALY_STORE_JOURNAL_DIVERGENCE,
    ANOMALY_UNMATCHABLE_BROKER_POSITION,
    ANOMALY_VOLUME_MISMATCH,
    BrokerSnapshot,
    Reconciler,
    ReconciliationInputError,
    RestartSimulator,
    SCENARIO_BROKER_POSITION_UNKNOWN,
    SCENARIO_CLEAN_SHUTDOWN,
    SCENARIO_CRASH_AFTER_FILL_BEFORE_BIND,
    SCENARIO_CRASH_BETWEEN_CLOSE_AND_PNL,
    SCENARIO_INTENT_SUBMITTED_NO_OUTCOME,
    SCENARIO_JOURNAL_TAIL_TORN,
    SnapshotValidationError,
    VERDICT_CLEAN,
    VERDICT_CRITICAL,
    VERDICT_WARN,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "app" / "services" / "reconciliation_lab.py"

ACCT = "acct-v1-" + "ab" * 16
T0 = "2026-07-22T03:00:00+00:00"
T1 = "2026-07-22T03:05:00+00:00"
T2 = "2026-07-22T03:20:00+00:00"

CANARY_TICKET = 900000111
CANARY_SYMBOL = "CANARY-FIXTURE#"


def _pos(ticket=100, pid=555, symbol="GOLD#", direction="BUY", volume="0.10", opened_at=T0, magic=1):
    return {
        "ticket": ticket, "position_identifier": pid, "symbol": symbol, "direction": direction,
        "volume": volume, "opened_at": opened_at, "magic": magic,
    }


def _snapshot(positions=None, deals=None, account_scope_id=ACCT):
    return BrokerSnapshot.from_mapping({
        "account_scope_id": account_scope_id,
        "positions": positions or [],
        "recent_deals": deals or [],
    })


def _ref(**overrides):
    base = dict(account_scope_id=ACCT, ticket=100, broker_symbol="GOLD#", opened_at=T0,
                magic=1, position_identifier=555, direction="BUY")
    base.update(overrides)
    ref, reason = try_build_broker_ref(**base)
    assert ref is not None, reason
    return ref


def _lifecycle_record(lifecycle_id="L1", state="OPEN", broker_ref=None, correlation_id=None,
                       created_at_utc=T0, closed_at_utc=None):
    return {
        "lifecycle_id": lifecycle_id, "correlation_id": correlation_id, "boot_id": None,
        "cycle_id": None, "setup_id": None, "state": state, "created_at_utc": created_at_utc,
        "closed_at_utc": closed_at_utc, "broker_ref": broker_ref,
        "broker_key": None if broker_ref is None else _broker_key(broker_ref),
    }


def _broker_key(ref):
    from app.services.lifecycle_identity_store import broker_ref_key
    return broker_ref_key(ref)


def _deal(deal_id, position_id, ticket, kind, at_utc, price="2000", profit="0", volume="0.10"):
    return DealRecord.from_mapping({
        "deal_id": deal_id, "position_id": position_id, "ticket": ticket, "kind": kind,
        "volume": volume, "price": price, "profit": profit, "commission": "0", "swap": "0",
        "fee": "0", "at_utc": at_utc, "symbol": "GOLD#",
    })


def _store(lifecycles=None, journal_rebuilt=None):
    return {
        "lifecycles": {} if lifecycles is None else lifecycles,
        "journal_rebuilt": journal_rebuilt,
    }


def _intent(intent_id="I1", state="CREATED", lifecycle_id=None, correlation_id=None,
            volume=0.1, created_at_utc=T0):
    return {
        "intent_id": intent_id, "idempotency_key": "ORDER_INTENT:" + "0" * 32, "state": state,
        "request": {"symbol": "GOLD#", "direction": "BUY", "volume": volume, "magic": 1,
                    "sl": None, "tp": None, "price": None},
        "provenance": None,
        "links": {"lifecycle_id": lifecycle_id, "correlation_id": correlation_id, "setup_id": None},
        "created_at_utc": created_at_utc,
        "history": [{"state": state, "at_utc": created_at_utc}],
    }


def _ledger(lifecycles=None):
    return {"lifecycles": lifecycles or {}}


class CompileTests(unittest.TestCase):
    def test_py_compile(self):
        py_compile.compile(str(_MODULE_PATH), doraise=True)


class PurityTests(unittest.TestCase):
    def test_module_purity(self):
        import inspect
        import re

        import app.services.reconciliation_lab as mod

        src = inspect.getsource(mod)
        code_only = re.sub(r'"""[\s\S]*?"""', "", src)
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
# BrokerSnapshot validation
# --------------------------------------------------------------------------- #
class BrokerSnapshotValidationTests(unittest.TestCase):
    def test_valid_snapshot_roundtrips(self):
        snap = _snapshot(positions=[_pos()], deals=[_deal(1, 555, 100, "ENTRY", T0)])
        self.assertEqual(len(snap.positions), 1)
        self.assertEqual(snap.positions[0]["volume"], Decimal("0.10"))
        self.assertEqual(len(snap.recent_deals), 1)
        self.assertIsInstance(snap.recent_deals[0], DealRecord)

    def test_recent_deals_omitted_defaults_empty(self):
        snap = BrokerSnapshot.from_mapping({"account_scope_id": ACCT, "positions": []})
        self.assertEqual(snap.recent_deals, ())

    def test_recent_deals_accepts_dealrecord_instances_directly(self):
        d = _deal(1, 555, 100, "ENTRY", T0)
        snap = BrokerSnapshot.from_mapping({
            "account_scope_id": ACCT, "positions": [], "recent_deals": [d],
        })
        self.assertIs(snap.recent_deals[0], d)

    def test_not_a_dict_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping(["nope"])

    def test_missing_field_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping({"positions": []})

    def test_unknown_field_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping({"account_scope_id": ACCT, "positions": [], "bogus": 1})

    def test_account_scope_id_invalid_rejected(self):
        for bad in ("not-a-scope", "", 123, None):
            with self.assertRaises(SnapshotValidationError):
                BrokerSnapshot.from_mapping({"account_scope_id": bad, "positions": []})

    def test_positions_not_list_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping({"account_scope_id": ACCT, "positions": {}})

    def test_recent_deals_not_list_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping({"account_scope_id": ACCT, "positions": [], "recent_deals": {}})

    def test_recent_deal_invalid_propagates_as_snapshot_error(self):
        with self.assertRaises(SnapshotValidationError):
            BrokerSnapshot.from_mapping({
                "account_scope_id": ACCT, "positions": [],
                "recent_deals": [{"deal_id": "not-an-int"}],
            })


class PositionValidationTests(unittest.TestCase):
    def test_missing_field_rejected(self):
        raw = _pos()
        del raw["ticket"]
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[raw])

    def test_unknown_field_rejected(self):
        raw = _pos()
        raw["bogus"] = 1
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[raw])

    def test_ticket_invalid_rejected(self):
        for bad in (0, -1, True, "100", 1.5):
            with self.assertRaises(SnapshotValidationError):
                _snapshot(positions=[_pos(ticket=bad)])

    def test_position_identifier_invalid_rejected(self):
        for bad in (0, -1, True, "555"):
            with self.assertRaises(SnapshotValidationError):
                _snapshot(positions=[_pos(pid=bad)])

    def test_position_identifier_none_accepted(self):
        snap = _snapshot(positions=[_pos(pid=None)])
        self.assertIsNone(snap.positions[0]["position_identifier"])

    def test_symbol_empty_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[_pos(symbol="")])

    def test_symbol_canonicalized_upper(self):
        snap = _snapshot(positions=[_pos(symbol="gold#")])
        self.assertEqual(snap.positions[0]["symbol"], "GOLD#")

    def test_direction_invalid_rejected(self):
        for bad in ("LONG", "buy", 1, None):
            with self.assertRaises(SnapshotValidationError):
                _snapshot(positions=[_pos(direction=bad)])

    def test_volume_float_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[_pos(volume=1.0)])

    def test_volume_bool_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[_pos(volume=True)])

    def test_volume_zero_or_negative_rejected(self):
        for bad in ("0", "-1", 0, -1):
            with self.assertRaises(SnapshotValidationError):
                _snapshot(positions=[_pos(volume=bad)])

    def test_opened_at_naive_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[_pos(opened_at="2026-07-22T03:00:00")])

    def test_opened_at_not_string_rejected(self):
        with self.assertRaises(SnapshotValidationError):
            _snapshot(positions=[_pos(opened_at=12345)])

    def test_magic_invalid_rejected(self):
        for bad in (-1, True, "1", 1.0):
            with self.assertRaises(SnapshotValidationError):
                _snapshot(positions=[_pos(magic=bad)])

    def test_magic_zero_accepted(self):
        snap = _snapshot(positions=[_pos(magic=0)])
        self.assertEqual(snap.positions[0]["magic"], 0)


# --------------------------------------------------------------------------- #
# Reconciler input validation
# --------------------------------------------------------------------------- #
class ReconcilerInputValidationTests(unittest.TestCase):
    def test_store_missing_keys_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler({"lifecycles": {}}, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0)

    def test_store_lifecycles_not_dict_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(_store(lifecycles=[]), {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0)

    def test_journal_rebuilt_malformed_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(
                _store(journal_rebuilt={"bogus": {}}), {"intents": []}, _ledger(), _snapshot(),
                as_of_utc=T0)

    def test_intent_book_state_invalid_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(_store(), {}, _ledger(), _snapshot(), as_of_utc=T0)

    def test_ledger_state_invalid_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(_store(), {"intents": []}, {}, _snapshot(), as_of_utc=T0)

    def test_snapshot_not_broker_snapshot_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(_store(), {"intents": []}, _ledger(), {"positions": []}, as_of_utc=T0)

    def test_as_of_utc_invalid_rejected(self):
        with self.assertRaises(ReconciliationInputError):
            Reconciler(_store(), {"intents": []}, _ledger(), _snapshot(), as_of_utc="not-a-date")

    def test_valid_minimal_inputs_clean(self):
        report = Reconciler(_store(), {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["verdict"], VERDICT_CLEAN)
        self.assertEqual(report["anomalies"], [])


# --------------------------------------------------------------------------- #
# Each anomaly type isolated
# --------------------------------------------------------------------------- #
class OrphanBrokerPositionTests(unittest.TestCase):
    def test_position_no_lifecycle_at_all(self):
        snap = _snapshot(positions=[_pos(ticket=100, pid=555)])
        report = Reconciler(_store(), {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_ORPHAN_BROKER_POSITION])
        self.assertEqual(report["verdict"], VERDICT_CRITICAL)

    def test_position_matches_open_lifecycle_no_anomaly(self):
        ref = _ref(ticket=100, position_identifier=555)
        rec = _lifecycle_record(broker_ref=ref, state="OPEN")
        store = _store(lifecycles={"L1": rec})
        snap = _snapshot(positions=[_pos(ticket=100, pid=555)])
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_position_matches_only_closed_lifecycle_still_orphan(self):
        ref = _ref(ticket=100, position_identifier=555)
        rec = _lifecycle_record(broker_ref=ref, state="CLOSED", closed_at_utc=T1)
        store = _store(lifecycles={"L1": rec})
        snap = _snapshot(positions=[_pos(ticket=100, pid=555)])
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertIn(ANOMALY_ORPHAN_BROKER_POSITION, types_seen)

    def test_never_matches_by_ticket_alone_recycled_ticket(self):
        # Two OPEN lifecycles share the SAME ticket, discriminated only by
        # position_identifier + opened_at. A position declaring a THIRD,
        # unrelated position_identifier must be ORPHAN and must NOT be
        # silently matched to either lifecycle via the shared ticket.
        ref_a = _ref(ticket=100, position_identifier=555, opened_at=T0)
        ref_b = _ref(ticket=100, position_identifier=777, opened_at=T1)
        store = _store(lifecycles={
            "A": _lifecycle_record("A", broker_ref=ref_a),
            "B": _lifecycle_record("B", broker_ref=ref_b),
        })
        snap = _snapshot(positions=[_pos(ticket=100, pid=999, opened_at=T0)])  # unknown pid
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        orphans = [a for a in report["anomalies"] if a["type"] == ANOMALY_ORPHAN_BROKER_POSITION]
        self.assertEqual(len(orphans), 1)
        # The orphan keeps ITS OWN (wrong) position_identifier -- proof it
        # was never silently re-attributed to A's or B's ticket-sharing ref.
        self.assertEqual(orphans[0]["position_identifier"], 999)
        self.assertNotEqual(orphans[0]["position_identifier"], 555)
        self.assertNotEqual(orphans[0]["position_identifier"], 777)


class UnmatchableBrokerPositionTests(unittest.TestCase):
    def test_no_position_identifier_recycled_ticket_is_unmatchable(self):
        ref = _ref(ticket=100, position_identifier=555)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref)})
        snap = _snapshot(positions=[_pos(ticket=100, pid=None)])
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_UNMATCHABLE_BROKER_POSITION])

    def test_no_position_identifier_no_ambiguity_falls_back_to_orphan(self):
        # No lifecycle at all shares this ticket -- no ambiguity, so an
        # unmatched pid-less position is an honest ORPHAN, not UNMATCHABLE.
        snap = _snapshot(positions=[_pos(ticket=100, pid=None)])
        report = Reconciler(_store(), {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_ORPHAN_BROKER_POSITION])

    def test_no_position_identifier_matches_unbound_lifecycle_via_ticket_symbol(self):
        # No pid on either side (lifecycle bound via ticket+symbol fallback)
        # and no recycled-ticket ambiguity -> a clean, direct key match.
        ref = _ref(ticket=100, position_identifier=None)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref)})
        snap = _snapshot(positions=[_pos(ticket=100, pid=None)])
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])


class StaleOpenLifecycleTests(unittest.TestCase):
    def test_bound_open_no_position_no_deal_is_stale(self):
        ref = _ref(ticket=100, position_identifier=555)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref, state="OPEN")})
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_STALE_OPEN_LIFECYCLE])

    def test_bound_open_with_matching_position_not_stale(self):
        ref = _ref(ticket=100, position_identifier=555)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref, state="OPEN")})
        snap = _snapshot(positions=[_pos(ticket=100, pid=555)])
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_bound_open_with_closing_deal_not_stale(self):
        ref = _ref(ticket=100, position_identifier=555)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref, state="OPEN")})
        deals = [_deal(1, 555, 100, "EXIT", T0)]
        snap = _snapshot(deals=deals)
        report = Reconciler(store, {"intents": []}, _ledger(), snap, as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_unbound_open_lifecycle_not_evaluated(self):
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=None, state="OPEN")})
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_closed_lifecycle_not_evaluated(self):
        ref = _ref(ticket=100, position_identifier=555)
        store = _store(lifecycles={"L1": _lifecycle_record(broker_ref=ref, state="CLOSED", closed_at_utc=T1)})
        # A ledger entry is provided so the (unrelated) LEDGER_LIFECYCLE_GAP
        # check stays quiet -- this test isolates STALE_OPEN_LIFECYCLE only.
        ledger = _ledger(lifecycles={"L1": [{"net_profit": "1.00", "computed_over_deals": 1,
                                              "deals_digest": "abc"}]})
        report = Reconciler(store, {"intents": []}, ledger, _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertNotIn(ANOMALY_STALE_OPEN_LIFECYCLE, types_seen)
        self.assertEqual(report["anomalies"], [])


class IntentStuckSubmittedTests(unittest.TestCase):
    def test_submitted_intent_flagged_with_age(self):
        intent = _intent(state="SUBMITTED", created_at_utc=T0)
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T2).reconcile()
        anomalies = [a for a in report["anomalies"] if a["type"] == ANOMALY_INTENT_STUCK_SUBMITTED]
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["age_seconds"], 1200.0)

    def test_created_state_not_flagged(self):
        intent = _intent(state="CREATED")
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_filled_state_not_flagged(self):
        intent = _intent(state="FILLED", lifecycle_id=None)
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertNotIn(ANOMALY_INTENT_STUCK_SUBMITTED, types_seen)

    def test_missing_created_at_age_is_none(self):
        intent = _intent(state="SUBMITTED")
        intent["created_at_utc"] = None
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        anomalies = [a for a in report["anomalies"] if a["type"] == ANOMALY_INTENT_STUCK_SUBMITTED]
        self.assertIsNone(anomalies[0]["age_seconds"])


class StoreJournalDivergenceTests(unittest.TestCase):
    def test_state_mismatch_is_critical(self):
        rec_store = _lifecycle_record(state="CLOSED", closed_at_utc=T1)
        rec_journal = _lifecycle_record(state="OPEN", closed_at_utc=None)
        store = _store(
            lifecycles={"L1": rec_store},
            journal_rebuilt={"lifecycles": {"L1": rec_journal}},
        )
        # A ledger entry keeps the (unrelated) LEDGER_LIFECYCLE_GAP check
        # quiet -- rec_store is CLOSED, so this test isolates
        # STORE_JOURNAL_DIVERGENCE only.
        ledger = _ledger(lifecycles={"L1": [{"net_profit": "1.00", "computed_over_deals": 1,
                                              "deals_digest": "abc"}]})
        report = Reconciler(store, {"intents": []}, ledger, _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_STORE_JOURNAL_DIVERGENCE])
        self.assertEqual(report["verdict"], VERDICT_CRITICAL)

    def test_lifecycle_present_only_in_store(self):
        store = _store(
            lifecycles={"L1": _lifecycle_record()},
            journal_rebuilt={"lifecycles": {}},
        )
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_STORE_JOURNAL_DIVERGENCE])

    def test_lifecycle_present_only_in_journal(self):
        store = _store(
            lifecycles={},
            journal_rebuilt={"lifecycles": {"L1": _lifecycle_record()}},
        )
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_STORE_JOURNAL_DIVERGENCE])

    def test_identical_states_no_divergence(self):
        rec = _lifecycle_record()
        store = _store(lifecycles={"L1": rec}, journal_rebuilt={"lifecycles": {"L1": dict(rec)}})
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])

    def test_journal_rebuilt_none_disables_check_never_crashes(self):
        store = _store(lifecycles={"L1": _lifecycle_record()}, journal_rebuilt=None)
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertNotIn(ANOMALY_STORE_JOURNAL_DIVERGENCE, types_seen)


class LedgerLifecycleGapTests(unittest.TestCase):
    def test_closed_no_pnl(self):
        store = _store(lifecycles={"L1": _lifecycle_record(state="CLOSED", closed_at_utc=T1)})
        report = Reconciler(store, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        gaps = [a for a in report["anomalies"] if a["type"] == ANOMALY_LEDGER_LIFECYCLE_GAP]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["subtype"], "CLOSED_NO_PNL")
        self.assertEqual(gaps[0]["severity"], "WARN")

    def test_pnl_without_lifecycle(self):
        ledger = _ledger(lifecycles={"L_GHOST": [{"net_profit": "1.00", "computed_over_deals": 1,
                                                    "deals_digest": "abc"}]})
        report = Reconciler(_store(), {"intents": []}, ledger, _snapshot(), as_of_utc=T0).reconcile()
        gaps = [a for a in report["anomalies"] if a["type"] == ANOMALY_LEDGER_LIFECYCLE_GAP]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["subtype"], "PNL_NO_LIFECYCLE")
        self.assertEqual(gaps[0]["severity"], "CRITICAL")

    def test_closed_with_pnl_no_gap(self):
        store = _store(lifecycles={"L1": _lifecycle_record(state="CLOSED", closed_at_utc=T1)})
        ledger = _ledger(lifecycles={"L1": [{"net_profit": "1.00", "computed_over_deals": 1,
                                              "deals_digest": "abc"}]})
        report = Reconciler(store, {"intents": []}, ledger, _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["anomalies"], [])


class CrossLayerJoinPassthroughTests(unittest.TestCase):
    def test_intent_filled_sans_lifecycle(self):
        intent = _intent(state="FILLED", lifecycle_id=None)
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertIn(ANOMALY_INTENT_FILLED_SANS_LIFECYCLE, types_seen)
        matched = next(a for a in report["anomalies"] if a["type"] == ANOMALY_INTENT_FILLED_SANS_LIFECYCLE)
        self.assertEqual(matched["severity"], "CRITICAL")

    def test_intent_lifecycle_unknown(self):
        intent = _intent(state="CREATED", lifecycle_id="L_GHOST")
        report = Reconciler(
            _store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertIn(ANOMALY_INTENT_LIFECYCLE_UNKNOWN, types_seen)

    def test_volume_mismatch(self):
        store = _store(lifecycles={"L1": _lifecycle_record(state="OPEN")})
        intents = [
            _intent("I1", state="SUBMITTED", lifecycle_id="L1", volume=0.1),
            _intent("I2", state="SUBMITTED", lifecycle_id="L1", volume=0.2),
        ]
        report = Reconciler(store, {"intents": intents}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertIn(ANOMALY_VOLUME_MISMATCH, types_seen)


# --------------------------------------------------------------------------- #
# Combined anomalies + verdict/counts
# --------------------------------------------------------------------------- #
class CombinedTests(unittest.TestCase):
    def test_multiple_anomaly_types_combined(self):
        store = _store(lifecycles={
            "L1": _lifecycle_record("L1", state="CLOSED", closed_at_utc=T1),
        })
        snap = _snapshot(positions=[_pos(ticket=999, pid=888)])
        intent = _intent(state="SUBMITTED", created_at_utc=T0)
        report = Reconciler(store, {"intents": [intent]}, _ledger(), snap, as_of_utc=T2).reconcile()
        types_seen = {a["type"] for a in report["anomalies"]}
        self.assertEqual(types_seen, {
            ANOMALY_ORPHAN_BROKER_POSITION, ANOMALY_LEDGER_LIFECYCLE_GAP, ANOMALY_INTENT_STUCK_SUBMITTED,
        })
        self.assertEqual(report["verdict"], VERDICT_CRITICAL)  # ORPHAN is CRITICAL
        self.assertEqual(report["counts"][ANOMALY_ORPHAN_BROKER_POSITION], 1)

    def test_warn_only_verdict(self):
        intent = _intent(state="SUBMITTED", created_at_utc=T0)
        report = Reconciler(_store(), {"intents": [intent]}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["verdict"], VERDICT_WARN)

    def test_clean_verdict_zero_anomalies(self):
        report = Reconciler(_store(), {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(report["verdict"], VERDICT_CLEAN)
        self.assertEqual(report["anomalies"], [])
        self.assertEqual(report["counts"], {})

    def test_checked_counts(self):
        snap = _snapshot(positions=[_pos()], deals=[_deal(1, 555, 100, "ENTRY", T0)])
        intent = _intent()
        store = _store(lifecycles={"L1": _lifecycle_record()})
        ledger = _ledger(lifecycles={"L1": []})
        report = Reconciler(store, {"intents": [intent]}, ledger, snap, as_of_utc=T0).reconcile()
        self.assertEqual(report["checked"], {
            "positions": 1, "recent_deals": 1, "lifecycles": 1, "intents": 1, "ledger_lifecycles": 1,
        })


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_digest(self):
        store = _store(lifecycles={"L1": _lifecycle_record(state="CLOSED", closed_at_utc=T1)})
        snap = _snapshot(positions=[_pos(ticket=999, pid=888)])
        intent = _intent(state="SUBMITTED", created_at_utc=T0)
        r1 = Reconciler(store, {"intents": [intent]}, _ledger(), snap, as_of_utc=T2).reconcile()
        r2 = Reconciler(store, {"intents": [intent]}, _ledger(), snap, as_of_utc=T2).reconcile()
        self.assertEqual(r1["digest"], r2["digest"])
        self.assertEqual(r1, r2)

    def test_dict_insertion_order_does_not_affect_digest(self):
        ref_a = _ref(ticket=100, position_identifier=1, opened_at=T0)
        ref_b = _ref(ticket=200, position_identifier=2, opened_at=T0)
        rec_a = _lifecycle_record("A", broker_ref=ref_a, state="CLOSED", closed_at_utc=T1)
        rec_b = _lifecycle_record("B", broker_ref=ref_b, state="CLOSED", closed_at_utc=T1)

        store_1 = _store(lifecycles={"A": rec_a, "B": rec_b})
        store_2 = _store(lifecycles={"B": rec_b, "A": rec_a})

        r1 = Reconciler(store_1, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        r2 = Reconciler(store_2, {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        self.assertEqual(r1["digest"], r2["digest"])
        self.assertEqual(r1["anomalies"], r2["anomalies"])

    def test_digest_excludes_itself_but_is_stable(self):
        report = Reconciler(_store(), {"intents": []}, _ledger(), _snapshot(), as_of_utc=T0).reconcile()
        without_digest = {k: v for k, v in report.items() if k != "digest"}
        from app.services.reconciliation_lab import _digest16
        self.assertEqual(report["digest"], _digest16(without_digest))


# --------------------------------------------------------------------------- #
# Never a write -- mock/spy proof
# --------------------------------------------------------------------------- #
class NeverWritesTests(unittest.TestCase):
    def test_reconcile_never_calls_store_or_journal_write_methods(self):
        create_mock = Mock(side_effect=AssertionError("must never be called by Reconciler"))
        bind_mock = Mock(side_effect=AssertionError("must never be called by Reconciler"))
        close_mock = Mock(side_effect=AssertionError("must never be called by Reconciler"))
        append_mock = Mock(side_effect=AssertionError("must never be called by Reconciler"))

        originals = (
            LifecycleIdentityStore.create_lifecycle,
            LifecycleIdentityStore.bind_broker_position,
            LifecycleIdentityStore.mark_closed,
            JournalWriter.append,
        )
        LifecycleIdentityStore.create_lifecycle = create_mock
        LifecycleIdentityStore.bind_broker_position = bind_mock
        LifecycleIdentityStore.mark_closed = close_mock
        JournalWriter.append = append_mock
        try:
            store = _store(lifecycles={"L1": _lifecycle_record(state="CLOSED", closed_at_utc=T1)})
            snap = _snapshot(positions=[_pos(ticket=999, pid=888)])
            intent = _intent(state="SUBMITTED", created_at_utc=T0)
            report = Reconciler(store, {"intents": [intent]}, _ledger(), snap, as_of_utc=T2).reconcile()
            self.assertTrue(report["anomalies"])  # sanity: this scenario is non-trivial
        finally:
            (
                LifecycleIdentityStore.create_lifecycle,
                LifecycleIdentityStore.bind_broker_position,
                LifecycleIdentityStore.mark_closed,
                JournalWriter.append,
            ) = originals
        create_mock.assert_not_called()
        bind_mock.assert_not_called()
        close_mock.assert_not_called()
        append_mock.assert_not_called()

    def test_restart_simulator_write_call_counts_match_world_building_only(self):
        """Integration-level proof: spy (real, self-binding) wrappers count
        EVERY call across a FULL run_scenario (world-building THEN restart
        THEN reconcile). If Reconciler ever wrote anywhere, the observed
        count would exceed the analytically-known world-building count for
        the ``clean_shutdown`` scenario: 1 create_lifecycle, 1
        bind_broker_position, 1 mark_closed, 7 JournalWriter.append (3
        mirror + 3 order-intent + 1 ledger)."""
        counts = {"create": 0, "bind": 0, "close": 0, "append": 0}
        orig_create = LifecycleIdentityStore.create_lifecycle
        orig_bind = LifecycleIdentityStore.bind_broker_position
        orig_close = LifecycleIdentityStore.mark_closed
        orig_append = JournalWriter.append

        def spy_create(self, *a, **kw):
            counts["create"] += 1
            return orig_create(self, *a, **kw)

        def spy_bind(self, *a, **kw):
            counts["bind"] += 1
            return orig_bind(self, *a, **kw)

        def spy_close(self, *a, **kw):
            counts["close"] += 1
            return orig_close(self, *a, **kw)

        def spy_append(self, *a, **kw):
            counts["append"] += 1
            return orig_append(self, *a, **kw)

        LifecycleIdentityStore.create_lifecycle = spy_create
        LifecycleIdentityStore.bind_broker_position = spy_bind
        LifecycleIdentityStore.mark_closed = spy_close
        JournalWriter.append = spy_append
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report = RestartSimulator().run_scenario(tmp, SCENARIO_CLEAN_SHUTDOWN)
        finally:
            LifecycleIdentityStore.create_lifecycle = orig_create
            LifecycleIdentityStore.bind_broker_position = orig_bind
            LifecycleIdentityStore.mark_closed = orig_close
            JournalWriter.append = orig_append

        self.assertEqual(report["verdict"], VERDICT_CLEAN)
        self.assertEqual(counts, {"create": 1, "bind": 1, "close": 1, "append": 7})


# --------------------------------------------------------------------------- #
# RestartSimulator -- the 6 named scenarios
# --------------------------------------------------------------------------- #
class RestartSimulatorScenarioTests(unittest.TestCase):
    def _run(self, scenario):
        with tempfile.TemporaryDirectory() as tmp:
            return RestartSimulator().run_scenario(tmp, scenario)

    def test_clean_shutdown_is_clean(self):
        report = self._run(SCENARIO_CLEAN_SHUTDOWN)
        self.assertEqual(report["verdict"], VERDICT_CLEAN)
        self.assertEqual(report["anomalies"], [])

    def test_crash_after_fill_before_bind_is_orphan_only(self):
        report = self._run(SCENARIO_CRASH_AFTER_FILL_BEFORE_BIND)
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_ORPHAN_BROKER_POSITION])
        self.assertEqual(report["verdict"], VERDICT_CRITICAL)

    def test_crash_between_close_and_pnl_is_ledger_gap_only(self):
        report = self._run(SCENARIO_CRASH_BETWEEN_CLOSE_AND_PNL)
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_LEDGER_LIFECYCLE_GAP])
        self.assertEqual(report["anomalies"][0]["subtype"], "CLOSED_NO_PNL")

    def test_journal_tail_torn_is_divergence_only(self):
        report = self._run(SCENARIO_JOURNAL_TAIL_TORN)
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_STORE_JOURNAL_DIVERGENCE])
        self.assertEqual(report["verdict"], VERDICT_CRITICAL)

    def test_broker_position_unknown_is_orphan_only(self):
        report = self._run(SCENARIO_BROKER_POSITION_UNKNOWN)
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_ORPHAN_BROKER_POSITION])

    def test_intent_submitted_no_outcome_is_stuck_only(self):
        report = self._run(SCENARIO_INTENT_SUBMITTED_NO_OUTCOME)
        types_seen = [a["type"] for a in report["anomalies"]]
        self.assertEqual(types_seen, [ANOMALY_INTENT_STUCK_SUBMITTED])
        self.assertEqual(report["verdict"], VERDICT_WARN)

    def test_unknown_scenario_name_rejected(self):
        with self.assertRaises(Exception):
            self._run("not_a_real_scenario")

    def test_all_scenarios_produce_deterministic_report_shape(self):
        for name in sorted(RestartSimulator.SCENARIO_NAMES):
            report = self._run(name)
            self.assertIn("digest", report)
            self.assertIn(report["verdict"], (VERDICT_CLEAN, VERDICT_WARN, VERDICT_CRITICAL))


# --------------------------------------------------------------------------- #
# Canary absence -- no fictitious value leaks into source or produced files
# --------------------------------------------------------------------------- #
class CanaryAbsenceTests(unittest.TestCase):
    def test_canaries_absent_from_source(self):
        import inspect

        import app.services.reconciliation_lab as mod

        src = inspect.getsource(mod)
        self.assertNotIn(str(CANARY_TICKET), src)
        self.assertNotIn(CANARY_SYMBOL, src)

    def test_canaries_absent_from_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            RestartSimulator().run_scenario(tmp, SCENARIO_CLEAN_SHUTDOWN)
            for path in Path(tmp).iterdir():
                if path.is_file():
                    raw = path.read_bytes()
                    self.assertNotIn(str(CANARY_TICKET).encode("utf-8"), raw)
                    self.assertNotIn(CANARY_SYMBOL.encode("utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
