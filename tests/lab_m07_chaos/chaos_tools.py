"""M07-P1 — chaos_tools: reusable fault injectors for the M07 chaos campaign.

TEST UTILITY ONLY. Lives under ``tests/`` (never ``app/``), imported by NO
production runtime and by no other test package outside
``tests/lab_m07_chaos``. Purpose: prove that every fault injected into the
M02-M06 laboratory stack (``lifecycle_identity_store``, ``event_journal``,
``order_intent``, ``lifecycle_pnl``, ``reconciliation_lab``,
``lifecycle_capture``) surfaces as a TYPED, fail-closed error and never
silently corrupts on-disk or in-memory state.

Four reusable building blocks:

- ``FaultyOS`` : deterministic OS-level fault injector. Scoped narrowly (a
  ``with FaultyOS(...):`` block wraps exactly ONE call under test) so it
  never leaks into unrelated pytest/runtime machinery. It patches, for the
  duration of the block only:
    * ``<open_target>.open`` (the target MODULE's own ``open`` name, e.g.
      ``app.services.event_journal.open`` — NOT the global ``builtins.open``,
      so no other module/thread is ever affected) for a chosen 1-based call
      occurrence ;
    * the file handle's own ``write()``/``flush()`` methods (via a thin
      wrapper), for a chosen 1-based occurrence ;
    * ``os.fsync`` / ``os.replace`` (process-global by necessity — these
      modules call them as bare ``os.fsync``/``os.replace`` — but again only
      for the lifetime of the narrow ``with`` block).
  Every occurrence is counted independently per function; call indices not
  in the configured fail-set pass through to the REAL implementation
  unchanged, so exactly one deterministic fault fires per test.

- ``ByteCorruptor`` : pure, in-memory, deterministic 1-byte flip helper plus
  a deterministic position sweep (``range(0, length, step)`` — no
  randomness). Callers write the corrupted bytes to disk, exercise the
  target, then restore the baseline before the next position.

- ``ClockChaos`` : constants/helpers for adversarial timestamp VALUES fed as
  INJECTED arguments (``created_at_utc``, ``at_utc``, ``opened_at``, ...).
  None of these modules read a wall clock, so "clock chaos" here always
  means "what happens when the caller hands us a naive / reversed / Y2038 /
  far-future timestamp", never patching ``time.time``.

- ``LockStealer`` : plants a foreign ``<path>.lock`` file (a different
  writer_token) to simulate a second writer racing a crashed first one, and
  a tiny helper to compute a journal's lock path.

Plus small shared helpers: ``sha256_bytes``/``sha256_file`` (disk-hash
before/after property), ``Ledger`` (per-test-module untyped-exception
counter — never a cross-file global, so behaviour never depends on pytest's
collection order), and ``TYPED_EXCEPTION_BASES`` (the curated allow-list of
"this is an expected, typed failure mode" exception classes).

DETERMINISM: ``DETERMINISTIC_SEED`` is the single fixed seed reused
anywhere a plan needs one (currently only documentary — every fault plan in
this module is derived from explicit call-indices or a fixed step, which is
already fully deterministic without consuming randomness).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from pathlib import Path
from unittest import mock

DETERMINISTIC_SEED = 70701  # M07-P1 fixed seed, reused everywhere a plan needs one

# --------------------------------------------------------------------------- #
# Typed-exception allow-list (transversal property: 0 untyped exceptions)
# --------------------------------------------------------------------------- #
from app.services.event_journal import JournalError  # noqa: E402
from app.services.lifecycle_identity_store import LifecycleStoreError  # noqa: E402
from app.services.lifecycle_pnl import LifecyclePnlError  # noqa: E402
from app.services.order_intent import OrderIntentError  # noqa: E402
from app.services.reconciliation_lab import ReconciliationLabError  # noqa: E402

# A raw OSError (with or without an errno) is an HONEST, typed surfacing of a
# genuine OS-level fault (disk full, permission, ...) -- it is never a bug
# signature (AttributeError/TypeError/KeyError/bare RuntimeError would be).
# It is included in the allow-list deliberately; see chaos_tools module
# docstring and the M07 writer report for the modules where a raw OSError
# (rather than a module-specific wrapped error) is the OBSERVED outcome.
TYPED_EXCEPTION_BASES: "tuple[type, ...]" = (
    OSError,
    JournalError,
    LifecycleStoreError,
    OrderIntentError,
    LifecyclePnlError,
    ReconciliationLabError,
)


class Ledger:
    """Per-test-module untyped-exception counter. Fresh instance per test
    file (never a cross-file global): a file's own final test asserts its
    own ``untyped == []``, so the "0 untyped exceptions" property holds
    regardless of pytest collection order or which subset of files runs."""

    def __init__(self, typed_bases: "tuple[type, ...]" = TYPED_EXCEPTION_BASES) -> None:
        self._typed_bases = typed_bases
        self.typed: "list[BaseException]" = []
        self.untyped: "list[BaseException]" = []
        self._lock = threading.Lock()

    def record(self, exc: BaseException) -> bool:
        """Classifies ``exc``; returns True iff it is a TYPED (expected)
        failure mode."""
        is_typed = isinstance(exc, self._typed_bases)
        with self._lock:
            (self.typed if is_typed else self.untyped).append(exc)
        return is_typed

    @property
    def total(self) -> int:
        return len(self.typed) + len(self.untyped)

    def describe_untyped(self) -> str:
        return ", ".join("%s:%s" % (type(e).__name__, e) for e in self.untyped)


# --------------------------------------------------------------------------- #
# FaultyOS
# --------------------------------------------------------------------------- #
class _FaultyFile:
    """Wraps a real, already-opened file handle so selected ``write()``/
    ``flush()`` occurrences raise instead of touching the real handle at
    all -- meaning a faulted call NEVER reaches the OS (no partial write is
    even attempted), which is exactly the property under test."""

    def __init__(self, fh, faulty: "FaultyOS") -> None:
        self._fh = fh
        self._faulty = faulty

    def write(self, data):
        n = self._faulty._bump("write")
        if n in self._faulty._fail_write_at:
            self._faulty._raise()
        return self._fh.write(data)

    def flush(self):
        n = self._faulty._bump("flush")
        if n in self._faulty._fail_flush_at:
            self._faulty._raise()
        return self._fh.flush()

    def fileno(self):
        return self._fh.fileno()

    def close(self):
        return self._fh.close()

    def truncate(self, *a):
        return self._fh.truncate(*a)

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._fh.close()
        return False


class FaultyOS:
    """Deterministic, narrowly-scoped OS-level fault injector. See module
    docstring for the full contract. Use as::

        with FaultyOS(open_target="app.services.event_journal",
                      fail_fsync_at={1}) as faulty:
            with self.assertRaises(SOME_ERROR):
                writer.append({"n": 1})
        self.assertEqual(faulty.counts["fsync"], 1)

    Every ``fail_*_at`` kwarg is a set/iterable of 1-based call occurrence
    numbers (counted independently per function). Anything not in the set
    passes through untouched to the real implementation.
    """

    def __init__(
        self,
        *,
        open_target: "str | None" = None,
        fail_open_at: "set[int] | frozenset[int]" = frozenset(),
        fail_write_at: "set[int] | frozenset[int]" = frozenset(),
        fail_flush_at: "set[int] | frozenset[int]" = frozenset(),
        fail_fsync_at: "set[int] | frozenset[int]" = frozenset(),
        fail_replace_at: "set[int] | frozenset[int]" = frozenset(),
        fail_os_open_at: "set[int] | frozenset[int]" = frozenset(),
        errno: int = 28,
        message: str = "ENOSPC (simulated, M07 chaos)",
    ) -> None:
        self._open_target = open_target
        self._fail_open_at = set(fail_open_at)
        self._fail_write_at = set(fail_write_at)
        self._fail_flush_at = set(fail_flush_at)
        self._fail_fsync_at = set(fail_fsync_at)
        self._fail_replace_at = set(fail_replace_at)
        self._fail_os_open_at = set(fail_os_open_at)
        self._errno = errno
        self._message = message
        self.counts = {"open": 0, "write": 0, "flush": 0, "fsync": 0, "replace": 0, "os_open": 0}
        self._lock = threading.Lock()
        self._patchers: "list" = []
        self._real_fsync = None
        self._real_replace = None
        self._real_os_open = None

    def _bump(self, key: str) -> int:
        with self._lock:
            self.counts[key] += 1
            return self.counts[key]

    def _raise(self) -> None:
        raise OSError(self._errno, self._message)

    def _wrap_open(self, real_open):
        def _open(file, mode="r", *args, **kwargs):
            n = self._bump("open")
            if n in self._fail_open_at:
                self._raise()
            fh = real_open(file, mode, *args, **kwargs)
            if self._fail_write_at or self._fail_flush_at:
                fh = _FaultyFile(fh, self)
            return fh

        return _open

    def _fsync(self, fd):
        n = self._bump("fsync")
        if n in self._fail_fsync_at:
            self._raise()
        return self._real_fsync(fd)

    def _replace(self, src, dst):
        n = self._bump("replace")
        if n in self._fail_replace_at:
            self._raise()
        return self._real_replace(src, dst)

    def _os_open(self, path, flags, mode=0o777):
        n = self._bump("os_open")
        if n in self._fail_os_open_at:
            self._raise()
        return self._real_os_open(path, flags, mode)

    def __enter__(self) -> "FaultyOS":
        import builtins
        import os

        self._real_fsync = os.fsync
        self._real_replace = os.replace
        self._real_os_open = os.open
        self._patchers = [
            mock.patch("os.fsync", side_effect=self._fsync),
            mock.patch("os.replace", side_effect=self._replace),
        ]
        if self._fail_os_open_at:
            self._patchers.append(mock.patch("os.open", side_effect=self._os_open))
        if self._open_target:
            real_open = builtins.open
            self._patchers.append(
                mock.patch(
                    self._open_target + ".open",
                    side_effect=self._wrap_open(real_open),
                    create=True,
                )
            )
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        for p in reversed(self._patchers):
            p.stop()
        return False


@contextlib.contextmanager
def faulty_existing_handle(obj, attr: str, faulty: "FaultyOS"):
    """Wraps an ALREADY-OPEN private file handle (e.g. a live
    ``JournalWriter``'s ``_fh``) with ``faulty``'s write()/flush() fault
    plan, for the duration of the block, then restores the original handle.

    ``FaultyOS``'s own ``open()`` patch only ever wraps handles opened WHILE
    it is active; a handle opened BEFORE the fault window (the normal case
    for testing a STEADY-STATE ``append()`` rather than journal/segment
    creation) needs this instead. Use nested inside a ``with faulty:`` block
    (which independently patches ``os.fsync``/``os.replace`` -- those still
    apply globally, exactly as they do for a freshly-opened handle)::

        faulty = FaultyOS(fail_write_at={1})
        with faulty:
            with faulty_existing_handle(writer, "_fh", faulty):
                with self.assertRaises(OSError):
                    writer.append({...})
    """
    real_fh = getattr(obj, attr)
    setattr(obj, attr, _FaultyFile(real_fh, faulty))
    try:
        yield
    finally:
        setattr(obj, attr, real_fh)


# --------------------------------------------------------------------------- #
# ByteCorruptor
# --------------------------------------------------------------------------- #
class ByteCorruptor:
    """Pure, deterministic 1-byte corruption helpers. Never touches disk
    itself -- callers write/restore bytes so each test controls exactly
    when the corrupted file becomes visible to the module under test."""

    @staticmethod
    def sweep_positions(length: int, step: int) -> "list[int]":
        if length <= 0 or step <= 0:
            return []
        return list(range(0, length, step))

    @staticmethod
    def flip_byte(data: bytes, position: int) -> bytes:
        if not (0 <= position < len(data)):
            raise IndexError("position %d out of range for %d bytes" % (position, len(data)))
        b = bytearray(data)
        b[position] ^= 0xFF
        return bytes(b)


# --------------------------------------------------------------------------- #
# ClockChaos — adversarial INJECTED timestamp values (never a real clock)
# --------------------------------------------------------------------------- #
class ClockChaos:
    NAIVE_NO_TZ = "2026-07-22T03:00:00"          # missing tzinfo
    EMPTY = ""
    NOT_A_STRING = 12345
    Y2038_EDGE_ISO = "2038-01-19T03:14:08+00:00"  # int32 unix-time overflow instant
    Y2038_PLUS_ONE_ISO = "2038-01-19T03:14:09+00:00"
    FAR_FUTURE_ISO = "2099-12-31T23:59:59+00:00"
    Y9999_ISO = "9999-12-31T23:59:59+00:00"
    EPOCH_MS_NEGATIVE = -1
    EPOCH_MS_ZERO = 0
    EPOCH_MS_FAR_FUTURE = 4102444800000  # 2100-01-01T00:00:00Z, milliseconds

    @staticmethod
    def reversed_walk(start_iso: str, count: int, step_seconds: int = 60) -> "list[str]":
        """Deterministic sequence of ISO-8601 UTC timestamps walking
        BACKWARD in time (models "a wall clock that recule") -- purely
        arithmetic on the injected ``start_iso``, never a real clock read."""
        from datetime import datetime, timedelta

        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        return [(dt - timedelta(seconds=step_seconds * i)).isoformat() for i in range(count)]


# --------------------------------------------------------------------------- #
# LockStealer
# --------------------------------------------------------------------------- #
class LockStealer:
    @staticmethod
    def lock_path_for(journal_path: "Path | str") -> Path:
        return Path(str(journal_path) + ".lock")

    @staticmethod
    def plant_foreign_lock(journal_path: "Path | str", token: str = "m07-chaos-foreign-token") -> Path:
        """Simulates a lock left behind by an unrelated/crashed writer whose
        token this process does not know -- the exact shape ``_acquire_lock``
        writes, but with a foreign token."""
        lp = LockStealer.lock_path_for(journal_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"writer_token": token}), encoding="utf-8")
        return lp


# --------------------------------------------------------------------------- #
# Disk-hash before/after property
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: "Path | str") -> str:
    return sha256_bytes(Path(path).read_bytes())


def assert_file_unchanged(testcase, path: "Path | str", expected_hash: str, msg: str = "") -> None:
    testcase.assertEqual(
        sha256_file(path), expected_hash,
        msg or ("on-disk state changed after a FAILED operation: %s" % path),
    )
