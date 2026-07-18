"""T1.2B1 — unit tests for the pure event-identity layer (app/services/event_identity.py).

Ports the T1.2A laboratory proofs into the repo test suite. Pure: no MT5, no
disk, no wiring, no secret. All values are fictitious. Assertions target
invariants, never absolute wall-clock time.
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid

from app.services.event_identity import (
    AccountScope,
    EventIdentityContext,
    EventIdentityError,
    EventIdentityValidationError,
    MonotonicUUID7Generator,
    RuntimeAnchor,
    UnsupportedIdempotencyValue,
    build_idempotency_key,
    derive_account_scope_id,
    validate_account_scope,
    validate_correlation_id,
    validate_event_id,
    validate_idempotency_key,
)

_FAKE_KEY = b"\x11" * 32           # fictitious 32-byte key
_FAKE_KEY2 = b"\x22" * 40          # fictitious rotated key
_FAKE_LOGIN = "10000001"           # fictitious login, never a real account
_FAKE_SERVER = "Broker-Demo 7"


class _FakeClock:
    def __init__(self, ms: int) -> None:
        self._ms = ms

    def set(self, ms: int) -> None:
        self._ms = ms

    def __call__(self) -> int:
        return self._ms


def _u(u: uuid.UUID) -> int:
    return u.int


# --------------------------------------------------------------------------- #
# UUIDv7
# --------------------------------------------------------------------------- #
class TestUUID7(unittest.TestCase):
    def test_500k_no_collision_version_variant_and_sorted(self):
        gen = MonotonicUUID7Generator()
        n = 500_000
        ids = [gen.new() for _ in range(n)]
        self.assertEqual(len({str(x) for x in ids}), n)  # 0 collision
        for x in ids[::997]:                              # sampled bit checks
            self.assertEqual(x.version, 7)
            self.assertEqual((x.int >> 62) & 0b11, 0b10)  # RFC variant
        ints = [_u(x) for x in ids]
        self.assertEqual(ints, sorted(ints))              # strictly k-sortable

    def test_bit_layout_exact(self):
        clock = _FakeClock(0x0123456789AB)  # 48-bit ms
        gen = MonotonicUUID7Generator(clock_ms=clock, random_bits=lambda n: 0)
        u = gen.new()
        self.assertEqual(u.int >> 80, 0x0123456789AB)     # timestamp 48 bits
        self.assertEqual((u.int >> 76) & 0xF, 0x7)        # version
        self.assertEqual((u.int >> 64) & 0xFFF, 0)        # counter (first in ms)
        self.assertEqual((u.int >> 62) & 0x3, 0b10)       # variant
        self.assertEqual(u.int & ((1 << 62) - 1), 0)      # rand_b == injected 0

    def test_50k_same_millisecond_monotonic(self):
        clock = _FakeClock(1_800_000_000_000)
        gen = MonotonicUUID7Generator(clock_ms=clock)
        ids = [gen.new() for _ in range(50_000)]
        self.assertEqual(len({str(x) for x in ids}), 50_000)
        ints = [_u(x) for x in ids]
        self.assertEqual(ints, sorted(ints))

    def test_clock_regression_never_goes_backwards(self):
        clock = _FakeClock(1_800_000_000_000)
        gen = MonotonicUUID7Generator(clock_ms=clock)
        a = [gen.new() for _ in range(1000)]
        clock.set(1_800_000_000_000 - 5000)  # -5s
        b = [gen.new() for _ in range(1000)]
        merged = [_u(x) for x in a + b]
        self.assertEqual(merged, sorted(merged))
        self.assertGreater(min(_u(x) for x in b), max(_u(x) for x in a))
        self.assertEqual(len({str(x) for x in a + b}), 2000)

    def test_counter_overflow_advances_logical_ms(self):
        clock = _FakeClock(1_000_000)
        gen = MonotonicUUID7Generator(clock_ms=clock, random_bits=lambda n: 0)
        ids = [gen.new() for _ in range(4096 + 10)]  # force overflow of 12-bit counter
        ints = [_u(x) for x in ids]
        self.assertEqual(ints, sorted(ints))
        self.assertEqual(len(set(ints)), len(ints))
        # after overflow the logical ms advanced beyond the frozen clock value
        self.assertGreater(ids[-1].int >> 80, 1_000_000)

    def test_injected_random_provider(self):
        gen = MonotonicUUID7Generator(clock_ms=_FakeClock(5), random_bits=lambda n: (1 << n) - 1)
        u = gen.new()
        self.assertEqual(u.int & ((1 << 62) - 1), (1 << 62) - 1)

    def test_8_threads_no_collision(self):
        gen = MonotonicUUID7Generator()
        buckets: list[list[str]] = [[] for _ in range(8)]

        def w(k):
            buckets[k] = [str(gen.new()) for _ in range(40_000)]

        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        allids = [x for b in buckets for x in b]
        self.assertEqual(len(set(allids)), len(allids))


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
class TestIdempotency(unittest.TestCase):
    def test_deterministic_and_format(self):
        k1 = build_idempotency_key("ORDER_INTENT", "c1", "reqA")
        k2 = build_idempotency_key("ORDER_INTENT", "c1", "reqA")
        self.assertEqual(k1, k2)
        validate_idempotency_key(k1)
        self.assertTrue(k1.startswith("ORDER_INTENT:"))
        self.assertEqual(len(k1.split(":")[1]), 32)

    def test_category_and_part_sensitivity(self):
        self.assertNotEqual(
            build_idempotency_key("A", "x"), build_idempotency_key("B", "x")
        )
        self.assertNotEqual(
            build_idempotency_key("A", "x"), build_idempotency_key("A", "y")
        )

    def test_part_order_matters(self):
        self.assertNotEqual(
            build_idempotency_key("C", "a", "b"), build_idempotency_key("C", "b", "a")
        )

    def test_types_all_distinct(self):
        keys = {
            build_idempotency_key("T", v)
            for v in (None, False, True, 0, 0.0, 2, 2.0, "", b"", "2", b"2")
        }
        self.assertEqual(len(keys), 11)  # every value produces a distinct key

    def test_separator_injection_proof(self):
        self.assertNotEqual(
            build_idempotency_key("C", "a", "b"), build_idempotency_key("C", "a\x1fb")
        )
        self.assertNotEqual(
            build_idempotency_key("C", "a", "b"), build_idempotency_key("C", "ab")
        )

    def test_unicode_and_empty(self):
        validate_idempotency_key(build_idempotency_key("U", "éà中🚀"))
        validate_idempotency_key(build_idempotency_key("U", ""))

    def test_rejects_nan_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(UnsupportedIdempotencyValue):
                build_idempotency_key("X", bad)

    def test_rejects_complex_types(self):
        for bad in ({"a": 1}, [1, 2], {1, 2}, object(), (1, 2)):
            with self.assertRaises(UnsupportedIdempotencyValue):
                build_idempotency_key("X", bad)

    def test_invalid_category_rejected(self):
        for bad in ("", "a", "lower", "1ABC", "A B", "A:", "x" * 41):
            with self.assertRaises(EventIdentityValidationError):
                build_idempotency_key(bad, "p")

    def test_100k_corpus_no_collision(self):
        seen = set()
        for i in range(100_000):
            k = build_idempotency_key("CORPUS", "c%d" % (i % 7), i, float(i) + 0.5, str(i))
            seen.add(k)
        self.assertEqual(len(seen), 100_000)


# --------------------------------------------------------------------------- #
# Account scope
# --------------------------------------------------------------------------- #
class TestAccountScope(unittest.TestCase):
    def test_stable_and_valid_format(self):
        s1 = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        s2 = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        self.assertIsInstance(s1, AccountScope)
        self.assertEqual(s1.account_scope_id, s2.account_scope_id)
        validate_account_scope(s1.account_scope_id)
        self.assertEqual(s1.algorithm, "HMAC-SHA256")

    def test_separation_login_server_key(self):
        base = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        other_login = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login="19999999", broker_server=_FAKE_SERVER)
        other_server = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server="Other 1")
        rotated = derive_account_scope_id(key=_FAKE_KEY2, key_id="k2", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        ids = {base.account_scope_id, other_login.account_scope_id, other_server.account_scope_id, rotated.account_scope_id}
        self.assertEqual(len(ids), 4)
        self.assertEqual(rotated.key_id, "k2")

    def test_server_normalization(self):
        a = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server="Broker-Demo 7")
        b = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server="  broker-demo   7 ")
        self.assertEqual(a.account_scope_id, b.account_scope_id)

    def test_login_accepts_int_and_str_equally(self):
        a = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=10000001, broker_server=_FAKE_SERVER)
        b = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login="10000001", broker_server=_FAKE_SERVER)
        self.assertEqual(a.account_scope_id, b.account_scope_id)

    def test_rejects_short_and_missing_key(self):
        with self.assertRaises(EventIdentityError):
            derive_account_scope_id(key=b"short", key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        with self.assertRaises(EventIdentityError):
            derive_account_scope_id(key=_FAKE_KEY, key_id="", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)

    def test_rejects_empty_server_and_bad_login(self):
        with self.assertRaises(EventIdentityError):
            derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server="   ")
        with self.assertRaises(EventIdentityError):
            derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=True, broker_server=_FAKE_SERVER)


# --------------------------------------------------------------------------- #
# Confidentiality
# --------------------------------------------------------------------------- #
class TestConfidentiality(unittest.TestCase):
    def test_login_and_key_absent_from_scope(self):
        s = derive_account_scope_id(key=_FAKE_KEY, key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        blob = s.account_scope_id + "|" + repr(s)
        self.assertNotIn(_FAKE_LOGIN, blob)
        self.assertNotIn(_FAKE_KEY.hex(), blob)
        self.assertNotIn("1111111111", blob)

    def test_key_absent_from_context_repr(self):
        ctx = EventIdentityContext(
            server_id="srv-" + "ab" * 8, bot_instance_id="bot-x1",
            account_hmac_key=_FAKE_KEY, account_key_id="k1",
        )
        self.assertNotIn(_FAKE_KEY.hex(), repr(ctx))

    def test_exceptions_carry_no_secret(self):
        try:
            derive_account_scope_id(key=b"short", key_id="k1", login=_FAKE_LOGIN, broker_server=_FAKE_SERVER)
        except EventIdentityError as exc:
            self.assertNotIn(_FAKE_LOGIN, str(exc))
            self.assertNotIn("short", str(exc))


# --------------------------------------------------------------------------- #
# Context: sequences + validators + concurrency
# --------------------------------------------------------------------------- #
class TestContext(unittest.TestCase):
    def _ctx(self) -> EventIdentityContext:
        return EventIdentityContext(server_id="srv-" + "cd" * 8, bot_instance_id="bot-abc123")

    def test_sequence_starts_at_one(self):
        ctx = self._ctx()
        self.assertEqual(ctx.next_sequence_number(), 1)
        self.assertEqual(ctx.next_sequence_number(), 2)

    def test_sequence_thread_safe_200k(self):
        ctx = self._ctx()
        buckets: list[list[int]] = [[] for _ in range(8)]

        def w(k):
            buckets[k] = [ctx.next_sequence_number() for _ in range(25_000)]

        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        allv = sorted(x for b in buckets for x in b)
        self.assertEqual(allv, list(range(1, 200_001)))

    def test_correlation_sequence_isolated(self):
        ctx = self._ctx()
        a = [ctx.next_correlation_sequence("A") for _ in range(5)]
        b = [ctx.next_correlation_sequence("B") for _ in range(3)]
        self.assertEqual(a, [1, 2, 3, 4, 5])
        self.assertEqual(b, [1, 2, 3])

    def test_seed_never_goes_backwards(self):
        ctx = self._ctx()
        ctx.next_correlation_sequence("A")  # -> 1
        ctx.seed_correlation_sequence("A", 42)
        self.assertEqual(ctx.next_correlation_sequence("A"), 43)
        ctx.seed_correlation_sequence("A", 5)  # lower is ignored
        self.assertEqual(ctx.next_correlation_sequence("A"), 44)

    def test_seed_rejects_negative(self):
        ctx = self._ctx()
        with self.assertRaises(EventIdentityError):
            ctx.seed_correlation_sequence("A", -1)

    def test_new_ids_are_uuid7(self):
        ctx = self._ctx()
        validate_event_id(ctx.new_event_id())
        validate_correlation_id(ctx.new_correlation_id())

    def test_uuid4_rejected_as_correlation_id(self):
        v4 = str(uuid.uuid4())
        with self.assertRaises(EventIdentityValidationError):
            validate_correlation_id(v4)
        with self.assertRaises(EventIdentityValidationError):
            validate_event_id(v4)

    def test_context_rejects_bad_server_or_instance(self):
        with self.assertRaises(EventIdentityValidationError):
            EventIdentityContext(server_id="nope", bot_instance_id="bot-x1")
        with self.assertRaises(EventIdentityValidationError):
            EventIdentityContext(server_id="srv-" + "ab" * 8, bot_instance_id="nope")

    def test_context_scope_requires_key(self):
        ctx = self._ctx()
        with self.assertRaises(EventIdentityError):
            ctx.derive_account_scope_id(_FAKE_LOGIN, _FAKE_SERVER)

    def test_concurrent_mixed_operations(self):
        ctx = self._ctx()
        errors: list[str] = []
        eids: list[str] = []
        lock = threading.Lock()

        def w(k):
            try:
                local = []
                for i in range(10_000):
                    local.append(ctx.new_event_id())
                    ctx.next_sequence_number()
                    ctx.next_correlation_sequence("c%d" % (i % 4))
                    ctx.build_idempotency_key("MIX", k, i)
                with lock:
                    eids.extend(local)
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(repr(exc))

        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(eids)), len(eids))  # 80000 unique event_ids


class TestRuntimeAnchor(unittest.TestCase):
    def test_frozen(self):
        a = RuntimeAnchor(
            git_commit="fb99dab7", git_branch="feature/geo-confluence-hardening",
            source_dirty=False, tracked_config_dirty=False, worktree_dirty=True,
            deployment_id="dep-1", server_id="srv-" + "ab" * 8, bot_instance_id="bot-x1",
        )
        with self.assertRaises(Exception):
            a.git_commit = "x"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
