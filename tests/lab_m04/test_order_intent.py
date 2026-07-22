"""M04-P1 -- tests laboratoire : app/services/order_intent.py
(OrderIntentBook). 100% additif, aucun fichier de production touche : tout
le disque passe par tempfile.TemporaryDirectory. Valeurs 100% fictives.
Aucun MT5, aucun order_send/order_check (meme simule).
"""
from __future__ import annotations

import py_compile
import statistics
import tempfile
import time
import types
import unittest
from pathlib import Path

from app.services.event_identity import MonotonicUUID7Generator
from app.services.event_journal import JournalCorruptedError, JournalReader, JournalWriter
from app.services.order_intent import (
    IntentNotFoundError,
    IntentReplayError,
    IntentTransitionError,
    IntentValidationError,
    OrderIntentBook,
    STATE_CANCELLED,
    STATE_CREATED,
    STATE_EXPIRED,
    STATE_FILLED,
    STATE_REJECTED,
    STATE_SUBMITTED,
    _ALL_STATES,
    _TRANSITIONS,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "app" / "services" / "order_intent.py"

T0 = "2026-07-22T02:00:00+00:00"
T1 = "2026-07-22T02:00:01+00:00"
T2 = "2026-07-22T02:00:02+00:00"
T3 = "2026-07-22T02:00:03+00:00"

CANARY_TICKET = "111000999"
CANARY_USER = "canary-user"


def _tmp_journal():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name) / "order_intent.jsonl"


def _book(path):
    return OrderIntentBook(path, uuid_generator=MonotonicUUID7Generator())


def _req(**overrides):
    base = {"symbol": "GOLD#", "direction": "BUY", "volume": 0.10, "magic": 909333}
    base.update(overrides)
    return base


class CompileTests(unittest.TestCase):
    def test_py_compile(self):
        py_compile.compile(str(_MODULE_PATH), doraise=True)


class PurityTests(unittest.TestCase):
    def test_module_purity(self):
        import inspect
        import re

        import app.services.order_intent as mod

        src = inspect.getsource(mod)
        # Docstrings intentionally NAME order_send/order_check/os.environ to
        # document what this module never does -- strip triple-quoted
        # docstrings before scanning so only actual CODE is checked.
        code_only = re.sub(r'"""[\s\S]*?"""', "", src)
        for forbidden in (
            "MetaTrader5", "os.environ", "getenv", "socket", "subprocess",
            "datetime.now", "time.time", "import time", "import datetime",
            "order_send", "order_check", "import os",
        ):
            self.assertNotIn(forbidden, code_only)
        imported_modules = {
            name for name, value in vars(mod).items() if isinstance(value, types.ModuleType)
        }
        self.assertEqual(imported_modules, {"math", "threading"})


class RequestValidationTests(unittest.TestCase):
    def test_missing_required_field_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent({"direction": "BUY", "volume": 0.1, "magic": 1}, created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_unknown_field_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(bogus=1), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_direction_invalid_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(direction="LONG"), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_volume_zero_or_negative_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        for bad in (0.0, -0.01, -1.0):
            with self.assertRaises(IntentValidationError):
                book.create_intent(_req(volume=bad), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_volume_bool_rejected_not_treated_as_int_or_float(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(volume=True), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_volume_int_rejected_strict_float_only(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(volume=1), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_magic_bool_rejected_not_treated_as_int(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(magic=True), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_magic_negative_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(magic=-1), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_magic_float_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(magic=1.0), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_symbol_empty_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(symbol=""), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_sl_tp_price_nan_inf_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(IntentValidationError):
                book.create_intent(_req(sl=bad), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_sl_tp_price_int_rejected_strict_float_only(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(tp=100), created_at_utc=T0)
        book.close()
        tmp.cleanup()

    def test_sl_tp_price_optional_when_absent(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        result = book.create_intent(_req(), created_at_utc=T0)
        self.assertIsNone(result["request"]["sl"])
        self.assertIsNone(result["request"]["tp"])
        self.assertIsNone(result["request"]["price"])
        book.close()
        tmp.cleanup()

    def test_valid_request_with_all_optional_fields(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        result = book.create_intent(
            _req(sl=1900.0, tp=2000.0, price=1950.0), created_at_utc=T0)
        self.assertEqual(result["request"]["sl"], 1900.0)
        self.assertEqual(result["request"]["tp"], 2000.0)
        self.assertEqual(result["request"]["price"], 1950.0)
        self.assertEqual(result["state"], STATE_CREATED)
        self.assertEqual(result["deduplicated"], False)
        book.close()
        tmp.cleanup()

    def test_created_at_utc_must_be_nonempty_str(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(), created_at_utc="")
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(), created_at_utc=None)
        book.close()
        tmp.cleanup()


class DedupTests(unittest.TestCase):
    def test_same_intention_twice_deduplicates(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        first = book.create_intent(_req(), created_at_utc=T0)
        second = book.create_intent(_req(), created_at_utc=T1)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(len(book.active_intents()), 1)
        diag = book.diagnostics()
        self.assertEqual(diag["created"], 1)
        self.assertEqual(diag["deduplicated"], 1)
        book.close()
        tmp.cleanup()

    def test_key_sensitive_to_each_part_volume(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(_req(volume=0.10), created_at_utc=T0)
        b = book.create_intent(_req(volume=0.20), created_at_utc=T1)
        self.assertNotEqual(a["intent_id"], b["intent_id"])
        self.assertFalse(b["deduplicated"])
        book.close()
        tmp.cleanup()

    def test_key_sensitive_to_each_part_sl(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(_req(sl=1900.0), created_at_utc=T0)
        b = book.create_intent(_req(sl=1901.0), created_at_utc=T1)
        self.assertNotEqual(a["intent_id"], b["intent_id"])
        book.close()
        tmp.cleanup()

    def test_key_sensitive_to_setup_id_and_cycle_id(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(
            _req(), created_at_utc=T0,
            provenance={"cycle_id": "cycle-1"}, links={"setup_id": "setup-A"})
        b = book.create_intent(
            _req(), created_at_utc=T1,
            provenance={"cycle_id": "cycle-2"}, links={"setup_id": "setup-A"})
        c = book.create_intent(
            _req(), created_at_utc=T2,
            provenance={"cycle_id": "cycle-1"}, links={"setup_id": "setup-A"})
        self.assertNotEqual(a["intent_id"], b["intent_id"])
        self.assertTrue(c["deduplicated"])
        self.assertEqual(a["intent_id"], c["intent_id"])
        book.close()
        tmp.cleanup()

    def test_terminal_intent_allows_new_intent_same_key(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        first = book.create_intent(_req(), created_at_utc=T0)
        book.transition(first["intent_id"], STATE_SUBMITTED, at_utc=T1)
        book.transition(first["intent_id"], STATE_REJECTED, at_utc=T2)

        second = book.create_intent(_req(), created_at_utc=T3)
        self.assertFalse(second["deduplicated"])
        self.assertNotEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(len(book.active_intents()), 1)
        book.close()
        tmp.cleanup()


class StateMachineLegalTests(unittest.TestCase):
    def _new(self, book):
        return book.create_intent(_req(), created_at_utc=T0)["intent_id"]

    def test_created_to_submitted(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        iid = self._new(book)
        result = book.transition(iid, STATE_SUBMITTED, at_utc=T1)
        self.assertEqual(result["state"], STATE_SUBMITTED)
        self.assertEqual(result["transition_result"], "APPLIED")
        self.assertEqual([h["state"] for h in result["history"]], [STATE_CREATED, STATE_SUBMITTED])
        book.close()
        tmp.cleanup()

    def test_submitted_to_each_terminal_state(self):
        for terminal in (STATE_FILLED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED):
            tmp, path = _tmp_journal()
            book = _book(path)
            iid = self._new(book)
            book.transition(iid, STATE_SUBMITTED, at_utc=T1)
            result = book.transition(iid, terminal, at_utc=T2, detail="ok")
            self.assertEqual(result["state"], terminal)
            self.assertEqual(book.active_intents(), [])
            book.close()
            tmp.cleanup()

    def test_retransition_to_current_state_is_idempotent_noop(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        iid = self._new(book)
        book.transition(iid, STATE_SUBMITTED, at_utc=T1)
        before_diag = book.diagnostics()
        result = book.transition(iid, STATE_SUBMITTED, at_utc=T2)
        self.assertEqual(result["transition_result"], "ALREADY_SUBMITTED")
        after_diag = book.diagnostics()
        self.assertEqual(before_diag["transitions"], after_diag["transitions"])
        # no journal write on the no-op path
        self.assertEqual(JournalReader(path).verify()["records"], 2)  # create + 1 real transition
        book.close()
        tmp.cleanup()

    def test_retransition_to_current_terminal_state_is_idempotent_noop(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        iid = self._new(book)
        book.transition(iid, STATE_SUBMITTED, at_utc=T1)
        book.transition(iid, STATE_FILLED, at_utc=T2)
        result = book.transition(iid, STATE_FILLED, at_utc=T3)
        self.assertEqual(result["transition_result"], "ALREADY_FILLED")
        book.close()
        tmp.cleanup()


class StateMachineIllegalTests(unittest.TestCase):
    def _new_in_state(self, book, target_state):
        iid = book.create_intent(_req(), created_at_utc=T0)["intent_id"]
        if target_state == STATE_CREATED:
            return iid
        book.transition(iid, STATE_SUBMITTED, at_utc=T1)
        if target_state == STATE_SUBMITTED:
            return iid
        book.transition(iid, target_state, at_utc=T2)
        return iid

    def test_all_illegal_pairs_refused_exhaustive(self):
        illegal_pairs = [
            (frm, to)
            for frm in _ALL_STATES
            for to in _ALL_STATES
            if to not in _TRANSITIONS[frm] and to != frm
        ]
        self.assertGreater(len(illegal_pairs), 0)
        for frm, to in illegal_pairs:
            tmp, path = _tmp_journal()
            book = _book(path)
            iid = self._new_in_state(book, frm)
            diag_before = book.diagnostics()
            with self.assertRaises(IntentTransitionError):
                book.transition(iid, to, at_utc=T3)
            diag_after = book.diagnostics()
            self.assertEqual(book.get(iid)["state"], frm)  # never overwritten
            self.assertEqual(diag_after["illegal_refused"], diag_before["illegal_refused"] + 1)
            book.close()
            tmp.cleanup()

    def test_unknown_intent_id_raises_not_found(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentNotFoundError):
            book.transition("does-not-exist", STATE_SUBMITTED, at_utc=T0)
        self.assertIsInstance(IntentNotFoundError("x"), IntentTransitionError)
        book.close()
        tmp.cleanup()

    def test_unknown_target_state_raises_validation_error(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        iid = book.create_intent(_req(), created_at_utc=T0)["intent_id"]
        with self.assertRaises(IntentValidationError):
            book.transition(iid, "TELEPORTED", at_utc=T1)
        book.close()
        tmp.cleanup()


class ReplayRestartTests(unittest.TestCase):
    def test_rebuild_reproduces_identical_states(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(_req(volume=0.10), created_at_utc=T0)
        b = book.create_intent(_req(volume=0.20), created_at_utc=T0)
        book.transition(a["intent_id"], STATE_SUBMITTED, at_utc=T1)
        book.transition(a["intent_id"], STATE_FILLED, at_utc=T2, detail="filled ok")
        book.transition(b["intent_id"], STATE_SUBMITTED, at_utc=T1)
        expected_a = book.get(a["intent_id"])
        expected_b = book.get(b["intent_id"])
        expected_diag = book.diagnostics()
        book.close()

        rebuilt = OrderIntentBook.rebuild_from_journal(
            path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(rebuilt.get(a["intent_id"]), expected_a)
        self.assertEqual(rebuilt.get(b["intent_id"]), expected_b)
        rebuilt_diag = rebuilt.diagnostics()
        self.assertEqual(rebuilt_diag["created"], expected_diag["created"])
        self.assertEqual(rebuilt_diag["transitions"], expected_diag["transitions"])
        self.assertEqual(rebuilt_diag["by_state"], expected_diag["by_state"])
        rebuilt.close()
        tmp.cleanup()

    def test_seq_continues_after_rebuild_and_new_writes(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(_req(), created_at_utc=T0)
        book.transition(a["intent_id"], STATE_SUBMITTED, at_utc=T1)
        records_before = JournalReader(path).verify()["records"]
        book.close()

        rebuilt = OrderIntentBook.rebuild_from_journal(
            path, uuid_generator=MonotonicUUID7Generator())
        rebuilt.create_intent(_req(volume=0.33), created_at_utc=T2)
        v = JournalReader(path).verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], records_before + 1)  # seq continued, not reset to 1
        rebuilt.close()
        tmp.cleanup()

    def test_rebuild_from_empty_journal_gives_empty_book(self):
        tmp, path = _tmp_journal()
        book = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        self.assertEqual(book.active_intents(), [])
        self.assertEqual(book.diagnostics()["created"], 0)
        book.close()
        tmp.cleanup()


class CorruptedJournalTests(unittest.TestCase):
    def test_hash_chain_corruption_fails_closed(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        rec = book.create_intent(_req(), created_at_utc=T0)
        book.transition(rec["intent_id"], STATE_SUBMITTED, at_utc=T1)
        book.close()

        # tamper a byte in the FIRST (non-last) record so the hash pointer
        # stored in the SECOND record's envelope no longer matches.
        raw = path.read_bytes()
        lines = raw.split(b"\n")
        # header=0, envelope1=1 (intent_created), envelope2=2 (intent_transition)
        tampered = lines[1].replace(b'"GOLD#"', b'"SILV#"')
        self.assertNotEqual(tampered, lines[1])
        lines[1] = tampered
        path.write_bytes(b"\n".join(lines))

        with self.assertRaises(JournalCorruptedError):
            OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        tmp.cleanup()

    def test_structurally_malformed_payload_fails_closed(self):
        tmp, path = _tmp_journal()
        writer = JournalWriter(path=path)
        writer.append({"op": "intent_created", "intent_id": "x"})  # missing required fields
        writer.close()

        with self.assertRaises(IntentReplayError):
            OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        tmp.cleanup()

    def test_unknown_op_fails_closed(self):
        tmp, path = _tmp_journal()
        writer = JournalWriter(path=path)
        writer.append({"op": "teleport_intent"})
        writer.close()

        with self.assertRaises(IntentReplayError):
            OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        tmp.cleanup()

    def test_transition_referencing_unknown_intent_fails_closed(self):
        tmp, path = _tmp_journal()
        writer = JournalWriter(path=path)
        writer.append({
            "op": "intent_transition", "intent_id": "ghost", "from_state": STATE_CREATED,
            "to_state": STATE_SUBMITTED, "at_utc": T0, "detail": None,
        })
        writer.close()

        with self.assertRaises(IntentReplayError):
            OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        tmp.cleanup()


class ProvenanceLinksTests(unittest.TestCase):
    def test_provenance_and_links_carried_through(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        corr = str(MonotonicUUID7Generator().new())
        result = book.create_intent(
            _req(), created_at_utc=T0,
            provenance={"boot_id": "boot-fictif-1", "cycle_id": 42},
            links={"setup_id": "setup-fictif-1", "correlation_id": corr, "lifecycle_id": "lc-fictif-1"},
        )
        self.assertEqual(result["provenance"], {"boot_id": "boot-fictif-1", "cycle_id": 42})
        self.assertEqual(result["links"], {
            "setup_id": "setup-fictif-1", "correlation_id": corr, "lifecycle_id": "lc-fictif-1"})
        fetched = book.get(result["intent_id"])
        self.assertEqual(fetched["provenance"], result["provenance"])
        self.assertEqual(fetched["links"], result["links"])
        book.close()
        tmp.cleanup()

    def test_provenance_links_survive_rebuild(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        result = book.create_intent(
            _req(), created_at_utc=T0,
            provenance={"boot_id": "boot-fictif-2", "cycle_id": "c9"},
            links={"setup_id": "setup-fictif-2"},
        )
        book.close()
        rebuilt = OrderIntentBook.rebuild_from_journal(path, uuid_generator=MonotonicUUID7Generator())
        fetched = rebuilt.get(result["intent_id"])
        self.assertEqual(fetched["provenance"], {"boot_id": "boot-fictif-2", "cycle_id": "c9"})
        self.assertEqual(fetched["links"], {
            "setup_id": "setup-fictif-2", "correlation_id": None, "lifecycle_id": None})
        rebuilt.close()
        tmp.cleanup()

    def test_correlation_id_must_be_valid_uuid7(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(), created_at_utc=T0, links={"correlation_id": "not-a-uuid"})
        book.close()
        tmp.cleanup()

    def test_provenance_unknown_field_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(), created_at_utc=T0, provenance={"bogus": 1})
        book.close()
        tmp.cleanup()

    def test_links_unknown_field_rejected(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        with self.assertRaises(IntentValidationError):
            book.create_intent(_req(), created_at_utc=T0, links={"bogus": 1})
        book.close()
        tmp.cleanup()


class DiagnosticsTests(unittest.TestCase):
    def test_counters_accumulate_correctly(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        a = book.create_intent(_req(volume=0.1), created_at_utc=T0)
        book.create_intent(_req(volume=0.1), created_at_utc=T0)  # dedup
        b = book.create_intent(_req(volume=0.2), created_at_utc=T0)
        book.transition(a["intent_id"], STATE_SUBMITTED, at_utc=T1)
        book.transition(a["intent_id"], STATE_FILLED, at_utc=T2)
        try:
            book.transition(a["intent_id"], STATE_SUBMITTED, at_utc=T3)
        except IntentTransitionError:
            pass

        diag = book.diagnostics()
        self.assertEqual(diag["created"], 2)
        self.assertEqual(diag["deduplicated"], 1)
        self.assertEqual(diag["transitions"], 2)
        self.assertEqual(diag["illegal_refused"], 1)
        self.assertEqual(diag["by_state"][STATE_FILLED], 1)
        self.assertEqual(diag["by_state"][STATE_CREATED], 1)  # b still CREATED
        book.close()
        tmp.cleanup()


class CanaryAbsenceTests(unittest.TestCase):
    def test_canary_values_never_appear_in_module_or_outputs(self):
        import inspect

        import app.services.order_intent as mod

        src = inspect.getsource(mod)
        self.assertNotIn(CANARY_TICKET, src)
        self.assertNotIn(CANARY_USER, src)

        tmp, path = _tmp_journal()
        book = _book(path)
        result = book.create_intent(_req(), created_at_utc=T0)
        book.transition(result["intent_id"], STATE_SUBMITTED, at_utc=T1, detail="normal detail")
        book.close()

        raw_journal_text = path.read_text(encoding="utf-8")
        self.assertNotIn(CANARY_TICKET, raw_journal_text)
        self.assertNotIn(CANARY_USER, raw_journal_text)
        self.assertNotIn(CANARY_TICKET, repr(result))
        self.assertNotIn(CANARY_USER, repr(result))
        tmp.cleanup()


class BenchTests(unittest.TestCase):
    def test_create_and_transition_median_under_10ms_fsync_on(self):
        tmp, path = _tmp_journal()
        book = _book(path)
        n = 60
        create_samples = []
        transition_samples = []
        for i in range(n):
            t0 = time.perf_counter()
            rec = book.create_intent(_req(volume=round(0.01 * (i + 1), 4)), created_at_utc=T0)
            t1 = time.perf_counter()
            create_samples.append((t1 - t0) * 1000.0)

            t2 = time.perf_counter()
            book.transition(rec["intent_id"], STATE_SUBMITTED, at_utc=T1)
            t3 = time.perf_counter()
            transition_samples.append((t3 - t2) * 1000.0)
        book.close()

        create_median = statistics.median(create_samples)
        transition_median = statistics.median(transition_samples)
        print(
            "[M04-P1 BENCH] create_intent median=%.4fms | transition median=%.4fms (n=%d, fsync on)"
            % (create_median, transition_median, n)
        )
        self.assertLess(create_median, 10.0)
        self.assertLess(transition_median, 10.0)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
