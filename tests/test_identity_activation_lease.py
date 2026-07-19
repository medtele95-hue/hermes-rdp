"""T1.2B2B2A1 — tests for the expiring SHADOW activation lease
(app/services/event_identity_runtime.evaluate_identity_activation).

FAIL-OFF: any lease problem leaves SHADOW OFF with a precise reason_code and
never raises. Pure: no MT5, no network, no production lease. Files live under
tempfile only.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import threading

from app.services.event_identity import EventIdentityContext, MonotonicUUID7Generator
from app.services.event_identity_runtime import (
    MAX_IDENTITY_SHADOW_LEASE_SECONDS,
    CausationCache,
    IdentityActivationDecision,
    IdentityShadowEnricher,
    LifecycleRegistry,
    _parse_utc,
    evaluate_identity_activation,
    maybe_build_identity_enricher,
)


def _ctx():
    return EventIdentityContext(
        server_id="srv-" + "0" * 16, bot_instance_id="bot-lease-test",
        uuid_generator=MonotonicUUID7Generator(),
    )


def _dec(setup="s1"):
    return {"event_type": "DEMO_ORDER_READY", "broker_symbol": "GOLD#",
            "strategy": "S", "direction": "BUY", "setup_id": setup}


class _CountingUUID(MonotonicUUID7Generator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def new(self):
        self.calls += 1
        return super().new()


class _CountingRegistry(LifecycleRegistry):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.clears = 0

    def clear(self):
        self.clears += 1
        super().clear()

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _valid_lease(now=NOW, commit=COMMIT, duration_s=3600, created_offset_s=-60, **over):
    created = now + timedelta(seconds=created_offset_s)
    expires = created + timedelta(seconds=duration_s)
    lease = {
        "schema_version": 1,
        "enabled": True,
        "mode": "SHADOW",
        "activation_id": str(uuid.uuid4()),
        "created_at_utc": _iso(created),
        "expires_at_utc": _iso(expires),
        "expected_git_commit": commit,
        "requested_by": "operator-fixture",
        "reason": "shadow observation window",
    }
    lease.update(over)
    return lease


class _Tmp:
    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)

    def write(self, obj_or_text, name="identity_shadow_activation.json"):
        p = self.dir / name
        if isinstance(obj_or_text, (bytes, bytearray)):
            p.write_bytes(obj_or_text)
        elif isinstance(obj_or_text, str):
            p.write_text(obj_or_text, encoding="utf-8")
        else:
            p.write_text(json.dumps(obj_or_text), encoding="utf-8")
        return p

    def close(self):
        self.d.cleanup()


def _ev(lease_path, *, env=None, commit=COMMIT, now=NOW, allowed=None):
    return evaluate_identity_activation(
        env_value=env, lease_path=lease_path, current_git_commit=commit,
        now_utc=now, allowed_dir=allowed,
    )


class TestValidAndPrecedence(unittest.TestCase):
    def setUp(self):
        self.t = _Tmp()

    def tearDown(self):
        self.t.close()

    def test_no_env_no_file_off(self):
        d = _ev(self.t.dir / "identity_shadow_activation.json")
        self.assertFalse(d.enabled)
        self.assertEqual(d.source, "OFF")
        self.assertEqual(d.reason_code, "LEASE_MISSING")

    def test_env_false_no_file_off(self):
        d = _ev(self.t.dir / "identity_shadow_activation.json", env="false")
        self.assertFalse(d.enabled)
        self.assertEqual(d.reason_code, "LEASE_MISSING")

    def test_env_true_environment(self):
        d = _ev(None, env="1")
        self.assertTrue(d.enabled)
        self.assertEqual(d.source, "ENVIRONMENT")
        self.assertEqual(d.reason_code, "ENV_ENABLED")

    def test_valid_lease_runtime(self):
        p = self.t.write(_valid_lease())
        d = _ev(p)
        self.assertTrue(d.enabled)
        self.assertEqual(d.source, "RUNTIME_LEASE")
        self.assertEqual(d.reason_code, "LEASE_VALID")
        self.assertEqual(d.expected_git_commit, COMMIT)
        self.assertIsNotNone(d.activation_id)

    def test_lease_enabled_false_off(self):
        p = self.t.write(_valid_lease(enabled=False))
        self.assertFalse(_ev(p).enabled)

    def test_lease_mode_wrong_off(self):
        p = self.t.write(_valid_lease(mode="LIVE"))
        self.assertEqual(_ev(p).reason_code, "LEASE_SCHEMA_INVALID")

    def test_lease_expired_off(self):
        p = self.t.write(_valid_lease(created_offset_s=-7200, duration_s=3600))  # expired 1h ago
        self.assertEqual(_ev(p).reason_code, "LEASE_EXPIRED")

    def test_lease_not_yet_active_off(self):
        p = self.t.write(_valid_lease(created_offset_s=3600, duration_s=600))  # starts in 1h
        self.assertEqual(_ev(p).reason_code, "LEASE_NOT_YET_VALID")

    def test_duration_too_long_off(self):
        p = self.t.write(_valid_lease(created_offset_s=-1, duration_s=MAX_IDENTITY_SHADOW_LEASE_SECONDS + 60))
        self.assertEqual(_ev(p).reason_code, "LEASE_DURATION_EXCEEDED")

    def test_commit_match_on_mismatch_off(self):
        p = self.t.write(_valid_lease(commit=COMMIT))
        self.assertTrue(_ev(p, commit=COMMIT).enabled)
        self.assertEqual(_ev(p, commit=OTHER_COMMIT).reason_code, "LEASE_COMMIT_MISMATCH")

    def test_commit_unknown_off(self):
        p = self.t.write(_valid_lease())
        self.assertEqual(_ev(p, commit="unknown").reason_code, "LEASE_COMMIT_MISMATCH")

    def test_duration_exactly_at_limit_ok(self):
        p = self.t.write(_valid_lease(created_offset_s=-1, duration_s=MAX_IDENTITY_SHADOW_LEASE_SECONDS))
        self.assertTrue(_ev(p).enabled)

    def test_expiry_exactly_now_off(self):
        # expires_at == now -> expired (strict >)
        p = self.t.write(_valid_lease(created_offset_s=-600, duration_s=600))
        self.assertEqual(_ev(p).reason_code, "LEASE_EXPIRED")


class TestAdversarial(unittest.TestCase):
    def setUp(self):
        self.t = _Tmp()

    def tearDown(self):
        self.t.close()

    def _reason(self, obj_or_text):
        p = self.t.write(obj_or_text)
        d = _ev(p)
        self.assertFalse(d.enabled)
        return d.reason_code

    def test_empty_json(self):
        self.assertEqual(self._reason(""), "LEASE_INVALID_JSON")

    def test_truncated_json(self):
        self.assertEqual(self._reason('{"schema_version": 1, "enabled": tr'), "LEASE_INVALID_JSON")

    def test_root_list(self):
        self.assertEqual(self._reason("[1,2,3]"), "LEASE_SCHEMA_INVALID")

    def test_missing_field(self):
        lease = _valid_lease()
        del lease["reason"]
        self.assertEqual(self._reason(lease), "LEASE_SCHEMA_INVALID")

    def test_extra_field(self):
        lease = _valid_lease()
        lease["surprise"] = 1
        self.assertEqual(self._reason(lease), "LEASE_SCHEMA_INVALID")

    def test_wrong_type_schema_version(self):
        self.assertEqual(self._reason(_valid_lease(schema_version="1")), "LEASE_SCHEMA_INVALID")

    def test_enabled_as_int_one(self):
        self.assertEqual(self._reason(_valid_lease(enabled=1)), "LEASE_SCHEMA_INVALID")

    def test_timestamp_without_tz(self):
        self.assertEqual(self._reason(_valid_lease(created_at_utc="2026-07-19T11:59:00")), "LEASE_SCHEMA_INVALID")

    def test_timestamp_invalid(self):
        self.assertEqual(self._reason(_valid_lease(expires_at_utc="not-a-date")), "LEASE_SCHEMA_INVALID")

    def test_created_after_expires(self):
        lease = _valid_lease()
        lease["created_at_utc"], lease["expires_at_utc"] = lease["expires_at_utc"], lease["created_at_utc"]
        self.assertEqual(self._reason(lease), "LEASE_SCHEMA_INVALID")

    def test_uuid_invalid(self):
        self.assertEqual(self._reason(_valid_lease(activation_id="not-a-uuid")), "LEASE_SCHEMA_INVALID")

    def test_commit_short(self):
        self.assertEqual(self._reason(_valid_lease(expected_git_commit="abc123")), "LEASE_SCHEMA_INVALID")

    def test_commit_non_hex(self):
        self.assertEqual(self._reason(_valid_lease(expected_git_commit="z" * 40)), "LEASE_SCHEMA_INVALID")

    def test_requested_by_empty(self):
        self.assertEqual(self._reason(_valid_lease(requested_by="  ")), "LEASE_SCHEMA_INVALID")

    def test_reason_empty(self):
        self.assertEqual(self._reason(_valid_lease(reason="")), "LEASE_SCHEMA_INVALID")

    def test_file_too_large(self):
        big = _valid_lease()
        big["reason"] = "x" * (16 * 1024 + 10)
        self.assertEqual(self._reason(big), "LEASE_SCHEMA_INVALID")

    def test_invalid_utf8(self):
        self.assertEqual(self._reason(b"\xff\xfe\x00bad"), "LEASE_INVALID_JSON")

    def test_symlink_rejected(self):
        target = self.t.write(_valid_lease(), name="real.json")
        link = self.t.dir / "identity_shadow_activation.json"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlink unavailable on this host")
        d = _ev(link)
        self.assertFalse(d.enabled)
        self.assertEqual(d.reason_code, "LEASE_PATH_INVALID")

    def test_path_outside_allowed_dir(self):
        p = self.t.write(_valid_lease())
        other = _Tmp()
        try:
            d = _ev(p, allowed=other.dir)  # lease sits in self.t.dir, not other.dir
            self.assertEqual(d.reason_code, "LEASE_PATH_INVALID")
        finally:
            other.close()

    def test_directory_not_file(self):
        d = _ev(self.t.dir)  # a directory, not a file
        self.assertFalse(d.enabled)
        self.assertEqual(d.reason_code, "LEASE_PATH_INVALID")

    def test_no_exception_ever_propagates(self):
        # a grab-bag of malformed inputs must all resolve to OFF without raising
        for bad in ('{}', '123', 'null', '"str"', '{"a":1}', "[]"):
            p = self.t.write(bad)
            d = _ev(p)
            self.assertFalse(d.enabled)
            self.assertIsInstance(d, IdentityActivationDecision)


class TestRestartSemantics(unittest.TestCase):
    """Conceptual crash/restart: the same lease re-read at a later boot time."""

    def setUp(self):
        self.t = _Tmp()

    def tearDown(self):
        self.t.close()

    def test_lease_valid_then_expired_across_reboots(self):
        p = self.t.write(_valid_lease(created_offset_s=-60, duration_s=600))
        self.assertTrue(_ev(p, now=NOW).enabled)                       # boot 1: ON
        self.assertTrue(_ev(p, now=NOW + timedelta(seconds=300)).enabled)  # boot 2 (< expiry): ON
        self.assertFalse(_ev(p, now=NOW + timedelta(seconds=3600)).enabled)  # boot 3 (> expiry): OFF
        self.assertEqual(_ev(p, now=NOW + timedelta(seconds=3600)).reason_code, "LEASE_EXPIRED")

    def test_lease_deleted_then_off(self):
        p = self.t.write(_valid_lease())
        self.assertTrue(_ev(p).enabled)
        p.unlink()
        self.assertEqual(_ev(p).reason_code, "LEASE_MISSING")

    def test_commit_change_then_off(self):
        p = self.t.write(_valid_lease(commit=COMMIT))
        self.assertTrue(_ev(p, commit=COMMIT).enabled)
        self.assertEqual(_ev(p, commit=OTHER_COMMIT).reason_code, "LEASE_COMMIT_MISMATCH")


class TestFactoryIntegration(unittest.TestCase):
    def setUp(self):
        self.t = _Tmp()

    def tearDown(self):
        self.t.close()

    def test_factory_off_when_no_lease_no_env(self):
        enr = maybe_build_identity_enricher(
            getenv=lambda k: None, lease_path=self.t.dir / "identity_shadow_activation.json",
            current_git_commit=COMMIT, now_utc=NOW,
        )
        self.assertIsNone(enr)

    def test_factory_on_with_valid_lease(self):
        p = self.t.write(_valid_lease())
        enr = maybe_build_identity_enricher(
            getenv=lambda k: None, lease_path=p, current_git_commit=COMMIT, now_utc=NOW,
        )
        self.assertIsNotNone(enr)
        self.assertTrue(enr.enabled)

    def test_factory_on_with_env(self):
        enr = maybe_build_identity_enricher(
            getenv=lambda k: "1", lease_path=self.t.dir / "identity_shadow_activation.json",
            current_git_commit=COMMIT, now_utc=NOW,
        )
        self.assertIsNotNone(enr)

    def test_factory_off_with_expired_lease(self):
        p = self.t.write(_valid_lease(created_offset_s=-7200, duration_s=3600))
        enr = maybe_build_identity_enricher(
            getenv=lambda k: None, lease_path=p, current_git_commit=COMMIT, now_utc=NOW,
        )
        self.assertIsNone(enr)


class TestBenchmark(unittest.TestCase):
    def test_boot_cost_negligible_and_zero_io_note(self):
        t = _Tmp()
        try:
            p = t.write(_valid_lease())
            n = 5000
            t0 = time.perf_counter()
            for _ in range(n):
                _ev(p)
            dt = time.perf_counter() - t0
            print("\n  [bench] evaluate_identity_activation valid lease: %.1f us/boot-eval (n=%d)"
                  % (dt / n * 1e6, n))
            # boot-only; the enricher's per-event path never calls this
            self.assertLess(dt / n, 0.01)  # < 10 ms per evaluation, very loose
        finally:
            t.close()


class TestUtcNormalization(unittest.TestCase):
    def test_offset_equals_z(self):
        a = _parse_utc("2026-07-19T03:00:00+02:00")
        b = _parse_utc("2026-07-19T01:00:00Z")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)                       # same UTC instant
        self.assertEqual(a.tzinfo, timezone.utc)     # stored in UTC

    def test_naive_rejected(self):
        self.assertIsNone(_parse_utc("2026-07-19T03:00:00"))


class TestRuntimeExpiry(unittest.TestCase):
    """The live process must self-deactivate at the monotonic deadline WITHOUT
    re-reading the file and WITHOUT any per-event I/O."""

    def _enricher(self, mono_ref, deadline, registry=None):
        return IdentityShadowEnricher(
            context=_ctx(),
            registry=registry if registry is not None else LifecycleRegistry(),
            enabled=True, expiry_monotonic=deadline,
            monotonic_clock=lambda: mono_ref[0],
        )

    def test_reproduction_old_behavior_would_leak(self):
        # Reproduces the defect: WITHOUT a deadline (boot-only gate), enrich
        # keeps adding identity_shadow forever (this is the OLD behavior).
        enr = IdentityShadowEnricher(context=_ctx(), enabled=True)  # no expiry
        self.assertIn("identity_shadow", enr.enrich(_dec()))        # T0+30s equiv
        self.assertIn("identity_shadow", enr.enrich(_dec()))        # T0+61s equiv -> STILL leaks

    def test_active_before_off_at_and_after(self):
        mono = [0.0]
        enr = self._enricher(mono, deadline=60.0)
        mono[0] = 30.0
        self.assertIn("identity_shadow", enr.enrich(_dec()))        # before expiry -> ON
        mono[0] = 59.0
        self.assertIn("identity_shadow", enr.enrich(_dec()))        # 1s before -> ON
        mono[0] = 60.0
        self.assertNotIn("identity_shadow", enr.enrich(_dec()))     # exactly at expiry -> OFF
        mono[0] = 61.0
        self.assertNotIn("identity_shadow", enr.enrich(_dec()))     # after -> OFF
        self.assertTrue(enr.expired)

    def test_100k_after_expiry_zero_uuid_zero_registry(self):
        mono = [100.0]  # already past deadline
        reg = LifecycleRegistry()
        gen = _CountingUUID()
        ctx = EventIdentityContext(server_id="srv-" + "0" * 16, bot_instance_id="bot-x", uuid_generator=gen)
        enr = IdentityShadowEnricher(context=ctx, registry=reg, enabled=True,
                                     expiry_monotonic=60.0, monotonic_clock=lambda: mono[0])
        for _ in range(100_000):
            out = enr.enrich(_dec())
            self.assertNotIn("identity_shadow", out)
        self.assertEqual(gen.calls, 0)               # zero UUID generated after expiry
        self.assertEqual(len(reg), 0)                # registry never consulted/filled

    def test_cleanup_runs_exactly_once(self):
        mono = [0.0]
        reg = _CountingRegistry()
        enr = self._enricher(mono, deadline=10.0, registry=reg)
        mono[0] = 5.0
        enr.enrich(_dec())                            # active, fills registry
        self.assertGreaterEqual(len(reg), 0)
        mono[0] = 20.0
        for _ in range(1000):                         # many post-expiry calls
            enr.enrich(_dec())
        self.assertEqual(reg.clears, 1)               # cleanup executed exactly once
        self.assertEqual(len(reg), 0)

    def test_legacy_object_unchanged_after_expiry(self):
        mono = [100.0]
        enr = self._enricher(mono, deadline=60.0)
        ev = {**_dec(), "sl": 1.23, "reason": "X"}
        before = dict(ev)
        out = enr.enrich(ev)
        self.assertIs(out, ev)
        self.assertEqual(out, before)                 # zero field added/changed

    def test_no_reactivation_after_expiry(self):
        mono = [100.0]
        enr = self._enricher(mono, deadline=60.0)
        self.assertNotIn("identity_shadow", enr.enrich(_dec()))
        mono[0] = 40.0                                # clock "goes back" below deadline
        self.assertNotIn("identity_shadow", enr.enrich(_dec()))  # stays EXPIRED (one-way)

    def test_wall_clock_recede_uses_monotonic(self):
        # monotonic never recedes; even if we do not advance it, no premature expiry
        mono = [10.0]
        enr = self._enricher(mono, deadline=60.0)
        for _ in range(100):
            self.assertIn("identity_shadow", enr.enrich(_dec()))  # still active (10 < 60)

    def test_no_expiry_when_none(self):
        # ENVIRONMENT-style enricher: no deadline -> never self-expires
        enr = IdentityShadowEnricher(context=_ctx(), enabled=True, expiry_monotonic=None)
        for _ in range(100):
            self.assertIn("identity_shadow", enr.enrich(_dec()))

    def test_concurrency_8_threads_at_boundary(self):
        mono = [59.9]
        reg = _CountingRegistry()
        enr = self._enricher(mono, deadline=60.0, registry=reg)
        errors = []
        barrier = threading.Barrier(8)

        def w():
            try:
                barrier.wait()
                for i in range(5000):
                    if i == 100:
                        mono[0] = 61.0               # cross the boundary mid-run
                    enr.enrich(_dec())
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        ts = [threading.Thread(target=w) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])                  # no deadlock, no exception
        self.assertEqual(reg.clears, 1)               # cleanup still exactly once under races
        self.assertTrue(enr.expired)

    def test_factory_lease_produces_runtime_deadline(self):
        # end-to-end: a valid lease via the factory yields an enricher that
        # expires at the lease deadline (injected monotonic clock).
        t = _Tmp()
        try:
            p = t.write(_valid_lease(created_offset_s=-10, duration_s=100))  # expires 90s after NOW
            mono = [1000.0]
            enr = maybe_build_identity_enricher(
                getenv=lambda k: None, lease_path=p, current_git_commit=COMMIT,
                now_utc=NOW, monotonic_clock=lambda: mono[0],
            )
            self.assertIsNotNone(enr)
            self.assertIn("identity_shadow", enr.enrich(_dec()))     # active now
            mono[0] = 1000.0 + 90.0                                  # reach lease deadline
            self.assertNotIn("identity_shadow", enr.enrich(_dec()))  # expired in live process
        finally:
            t.close()

    def test_factory_env_source_has_no_expiry(self):
        mono = [0.0]
        enr = maybe_build_identity_enricher(
            getenv=lambda k: "1", current_git_commit=COMMIT, now_utc=NOW,
            monotonic_clock=lambda: mono[0],
        )
        self.assertIsNotNone(enr)
        mono[0] = 10 ** 9                              # far future
        self.assertIn("identity_shadow", enr.enrich(_dec()))  # ENV source never expires


class TestBoundedCorrelationState(unittest.TestCase):
    """T1.2B2B2A1-R2 — ALL per-correlation state must be bounded while ACTIVE,
    dropped with LRU/TTL eviction, and fully freed on ACTIVE -> EXPIRED."""

    def _enricher(self, max_entries=128, mono=None, deadline=None, ttl=1e9, registry=None):
        ctx = _ctx()
        reg = registry if registry is not None else LifecycleRegistry(
            max_entries=max_entries, active_ttl_seconds=ttl,
            terminal_grace_seconds=1e9, clock=(lambda: mono[0]) if mono else None,
        )
        enr = IdentityShadowEnricher(
            context=ctx, registry=reg, enabled=True,
            expiry_monotonic=deadline,
            monotonic_clock=(lambda: mono[0]) if mono else None,
        )
        return enr, ctx, reg

    def test_reproduction_old_model_context_store_grows(self):
        # Reproduces the defect: the OLD wiring fed context.next_correlation_sequence
        # per lifecycle -> the pure module's plain dict grows with every id seen.
        ctx = _ctx()
        for i in range(10_000):
            ctx.next_correlation_sequence("corr-%d" % i)
        self.assertEqual(len(ctx._corr_seq), 10_000)  # unbounded growth demonstrated

    def test_active_10k_lifecycles_registry_128_no_hidden_table(self):
        enr, ctx, reg = self._enricher(max_entries=128)
        for i in range(10_000):
            out = enr.enrich(_dec("setup-%d" % i))
            self.assertIn("identity_shadow", out)
        self.assertLessEqual(len(reg), 128)                 # bounded registry
        self.assertEqual(len(ctx._corr_seq), 0)             # NO hidden sequence table
        self.assertLessEqual(len(enr._causation), 4096)     # bounded causation

    def test_sequence_lives_in_bounded_entry(self):
        enr, ctx, reg = self._enricher()
        seqs = [enr.enrich(_dec("S1"))["identity_shadow"]["correlation_sequence"]
                for _ in range(4)]
        self.assertEqual(seqs, [1, 2, 3, 4])                # per-lifecycle monotone
        self.assertEqual(len(ctx._corr_seq), 0)             # pure store untouched

    def test_lru_eviction_drops_sequence_and_restarts_at_1(self):
        enr, ctx, reg = self._enricher(max_entries=2)
        first = enr.enrich(_dec("A"))["identity_shadow"]
        enr.enrich(_dec("B"))
        enr.enrich(_dec("C"))                                # evicts A (LRU)
        again = enr.enrich(_dec("A"))["identity_shadow"]     # A reappears
        self.assertNotEqual(again["correlation_id"], first["correlation_id"])  # new id
        self.assertEqual(again["correlation_sequence"], 1)   # sequence restarted
        self.assertIsNone(again["causation_id"])             # no stale causation reused
        self.assertLessEqual(len(reg), 2)
        self.assertEqual(len(ctx._corr_seq), 0)

    def test_ttl_expiry_drops_sequence_and_restarts_at_1(self):
        mono = [0.0]
        reg = LifecycleRegistry(max_entries=100, active_ttl_seconds=100.0,
                                terminal_grace_seconds=10.0, clock=lambda: mono[0])
        enr, ctx, _ = self._enricher(registry=reg, mono=mono)
        a = enr.enrich(_dec("S"))["identity_shadow"]
        self.assertEqual(a["correlation_sequence"], 1)
        mono[0] = 101.0                                      # TTL elapses
        b = enr.enrich(_dec("S"))["identity_shadow"]
        self.assertNotEqual(b["correlation_id"], a["correlation_id"])
        self.assertEqual(b["correlation_sequence"], 1)
        self.assertIsNone(b["causation_id"])                 # no cross-incarnation causation
        self.assertEqual(len(ctx._corr_seq), 0)

    def test_expiry_frees_everything_then_100k_no_growth(self):
        mono = [0.0]
        enr, ctx, reg = self._enricher(max_entries=4096, mono=mono, deadline=60.0)
        for i in range(500):
            enr.enrich(_dec("setup-%d" % i))                 # ACTIVE: fills registry
        self.assertGreater(len(reg), 0)
        mono[0] = 61.0                                       # cross the lease deadline
        gen_calls_before = None
        enr.enrich(_dec("post-0"))                           # triggers one-time cleanup
        self.assertEqual(len(reg), 0)                        # registry freed
        self.assertEqual(len(enr._causation), 0)             # causation freed
        self.assertEqual(len(ctx._corr_seq), 0)              # sequence state freed (never fed)
        for i in range(100_000):
            out = enr.enrich(_dec("post-%d" % i))
            self.assertNotIn("identity_shadow", out)
        self.assertEqual(len(reg), 0)                        # zero growth after expiry
        self.assertEqual(len(enr._causation), 0)
        self.assertEqual(len(ctx._corr_seq), 0)

    def test_volume_100k_lifecycles_all_state_bounded(self):
        enr, ctx, reg = self._enricher(max_entries=2048)
        for i in range(100_000):
            enr.enrich(_dec("setup-%d" % i))
        self.assertLessEqual(len(reg), 2048)                 # registry bounded
        self.assertLessEqual(len(enr._causation), 4096)      # causation bounded
        self.assertEqual(len(ctx._corr_seq), 0)              # no ~100k hidden table

    def test_concurrency_8_threads_no_duplicate_sequence(self):
        mono = [0.0]
        enr, ctx, reg = self._enricher(max_entries=10_000, mono=mono, deadline=10_000.0)
        pairs: list[set] = [set() for _ in range(8)]
        errors = []

        def w(k):
            try:
                for i in range(5_000):
                    sh = enr.enrich(_dec("S%d" % (i % 50)))["identity_shadow"]
                    pairs[k].add((sh["correlation_id"], sh["correlation_sequence"]))
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])                          # no deadlock/KeyError
        allp = [p for s in pairs for p in s]
        self.assertEqual(len(allp), len(set(allp)))           # no duplicated (cid, seq)
        self.assertEqual(len(ctx._corr_seq), 0)
        # then cross the lease deadline under load: cleanup exactly once
        reg2 = _CountingRegistry(max_entries=100)
        enr2 = IdentityShadowEnricher(context=_ctx(), registry=reg2, enabled=True,
                                      expiry_monotonic=60.0, monotonic_clock=lambda: mono[0])
        mono[0] = 59.9
        errors2 = []
        barrier = threading.Barrier(8)

        def w2():
            try:
                barrier.wait()
                for i in range(2_000):
                    if i == 50:
                        mono[0] = 61.0
                    enr2.enrich(_dec("X"))
            except Exception as exc:  # pragma: no cover
                errors2.append(repr(exc))

        ts2 = [threading.Thread(target=w2) for _ in range(8)]
        [t.start() for t in ts2]
        [t.join() for t in ts2]
        self.assertEqual(errors2, [])
        self.assertEqual(reg2.clears, 1)                      # cleanup exactly once
        self.assertEqual(len(reg2), 0)


if __name__ == "__main__":
    unittest.main()
