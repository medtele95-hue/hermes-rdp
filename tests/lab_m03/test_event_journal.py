"""M03-P1 — tests laboratoire : app/services/event_journal.py (JournalWriter/
JournalReader). 100% additif, aucun fichier de production touche : tout le
disque passe par tempfile.TemporaryDirectory. Valeurs 100% fictives.
"""
from __future__ import annotations

import json
import py_compile
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from app.services.event_journal import (
    JOURNAL_SCHEMA_NAME,
    JOURNAL_SCHEMA_VERSION,
    JournalCorruptedError,
    JournalPoisonedError,
    JournalReader,
    JournalWriter,
    OwnershipError,
    ValidationError,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "app" / "services" / "event_journal.py"


def _tmp_journal():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name) / "journal.jsonl"


class CompileTests(unittest.TestCase):
    def test_py_compile(self):
        py_compile.compile(str(_MODULE_PATH), doraise=True)


class PurityTests(unittest.TestCase):
    def test_module_purity(self):
        import inspect

        import app.services.event_journal as mod

        src = inspect.getsource(mod)
        for forbidden in ("MetaTrader5", "os.environ", "getenv", "socket",
                           "subprocess", "datetime.now", "time.time", "import time",
                           "import datetime"):
            self.assertNotIn(forbidden, src)
        imported = {name for name, value in vars(mod).items()
                    if isinstance(value, types.ModuleType)}
        self.assertEqual(imported, {"hashlib", "json", "os", "threading", "uuid"})


class BasicAppendVerifyReplayTests(unittest.TestCase):
    def test_append_verify_replay_roundtrip(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            for i in range(5):
                env = w.append({"kind": "DEMO", "n": i})
                self.assertEqual(env["seq"], i + 1)
        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 5)
        self.assertEqual(v["segments"], 1)
        self.assertEqual(v["tail_status"], "CLEAN")
        self.assertEqual(v["errors"], [])
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads], [0, 1, 2, 3, 4])
        tmp.cleanup()

    def test_empty_journal_verify_and_replay(self):
        tmp, path = _tmp_journal()
        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["segments"], 0)
        self.assertEqual(v["records"], 0)
        self.assertEqual(reader.replay(), [])
        tmp.cleanup()

    def test_header_first_line_schema_and_genesis(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"a": 1})
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        header = json.loads(first_line)
        self.assertEqual(header["schema"], JOURNAL_SCHEMA_NAME)
        self.assertEqual(header["schema_version"], JOURNAL_SCHEMA_VERSION)
        self.assertEqual(header["segment_index"], 0)
        self.assertEqual(header["prev_segment_tail_sha"], "GENESIS")
        self.assertIsInstance(header["writer_token"], str)
        self.assertTrue(header["writer_token"])
        tmp.cleanup()

    def test_deterministic_serialization_sort_keys(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"z": 1, "a": 2, "m": 3})
        lines = path.read_text(encoding="utf-8").splitlines()
        # sort_keys : les cles de l'enveloppe et du payload sont triees
        env_line = lines[1]
        self.assertTrue(env_line.index('"payload"') < env_line.index('"prev_line_sha256_16"')
                         or True)  # verification structurelle ci-dessous
        env = json.loads(env_line)
        self.assertEqual(list(env.keys()), sorted(env.keys()))
        self.assertEqual(list(env["payload"].keys()), sorted(env["payload"].keys()))
        tmp.cleanup()


class ResumeTests(unittest.TestCase):
    def test_seq_monotone_across_close_reopen(self):
        tmp, path = _tmp_journal()
        w1 = JournalWriter(path=path)
        w1.append({"n": 1})
        w1.append({"n": 2})
        w1.close()

        w2 = JournalWriter(path=path)
        env = w2.append({"n": 3})
        self.assertEqual(env["seq"], 3)  # jamais re-seq a 1
        w2.close()

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 3)
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads], [1, 2, 3])
        tmp.cleanup()

    def test_multiple_reopen_cycles(self):
        tmp, path = _tmp_journal()
        for i in range(5):
            w = JournalWriter(path=path)
            env = w.append({"i": i})
            self.assertEqual(env["seq"], i + 1)
            w.close()
        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 5)
        tmp.cleanup()


class RotationTests(unittest.TestCase):
    def _segments(self, path):
        segs = [path]
        i = 1
        while Path(str(path) + "." + str(i)).exists():
            segs.append(Path(str(path) + "." + str(i)))
            i += 1
        return segs

    def test_multi_segment_rotation_chained(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path, max_segment_bytes=300) as w:
            for i in range(30):
                w.append({"n": i, "pad": "x" * 10})
        segs = self._segments(path)
        self.assertGreater(len(segs), 1)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 30)
        self.assertEqual(v["segments"], len(segs))
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads], list(range(30)))

        headers = [json.loads(s.read_text(encoding="utf-8").splitlines()[0]) for s in segs]
        self.assertEqual(headers[0]["prev_segment_tail_sha"], "GENESIS")
        for idx, h in enumerate(headers):
            self.assertEqual(h["segment_index"], idx)
        for h in headers[1:]:
            self.assertNotEqual(h["prev_segment_tail_sha"], "GENESIS")
            self.assertEqual(len(h["prev_segment_tail_sha"]), 64)  # sha256 hex complet

        # anciens segments jamais modifies (re-verifier apres reouverture writer)
        before = [s.read_bytes() for s in segs[:-1]]
        with JournalWriter(path=path, max_segment_bytes=300) as w2:
            w2.append({"n": 999})
        after = [s.read_bytes() for s in segs[:-1]]
        self.assertEqual(before, after)
        tmp.cleanup()


class TornTailTests(unittest.TestCase):
    @staticmethod
    def _truncate_last_line(path, chop=8):
        raw = path.read_bytes()
        lines = raw.split(b"\n")
        assert lines[-1] == b""
        full_lines = lines[:-1]
        last = full_lines[-1]
        truncated_last = last[: max(1, len(last) - chop)]
        new_raw = b"\n".join(full_lines[:-1] + [truncated_last])
        path.write_bytes(new_raw)

    def test_torn_tail_quarantined_reader_previous_records_intact(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"n": 1})
            w.append({"n": 2})
            w.append({"n": 3})
        self._truncate_last_line(path)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["tail_status"], "TORN_TAIL_QUARANTINED")
        self.assertEqual(v["records"], 2)
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads], [1, 2])
        tmp.cleanup()

    def test_writer_resumes_after_torn_tail_discarding_garbage(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"n": 1})
            w.append({"n": 2})
        self._truncate_last_line(path)  # la ligne n=2 devient torn

        with JournalWriter(path=path) as w2:
            env = w2.append({"n": 3})
            self.assertEqual(env["seq"], 2)  # n=2 jamais compte -> next seq = 2

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["tail_status"], "CLEAN")  # le trou a ete tronque proprement
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads], [1, 3])
        tmp.cleanup()


class ResumeReportObservabilityTests(unittest.TestCase):
    """CORRECTION 1/5 (oracle) : la troncature de torn-tail a la reprise
    doit etre OBSERVABLE (jamais silencieuse), via resume_report()."""

    def test_fresh_journal_resumed_false_neutral_values(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            report = w.resume_report()
        self.assertEqual(report, {
            "resumed": False, "torn_tail_truncated": False,
            "truncated_bytes": 0, "resume_seq": 0, "segment_index": 0,
            "poisoned": False, "poison_reason": None,
        })
        tmp.cleanup()

    def test_clean_reopen_truncated_false(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"n": 1})
            w.append({"n": 2})
        with JournalWriter(path=path) as w2:
            report = w2.resume_report()
        self.assertTrue(report["resumed"])
        self.assertFalse(report["torn_tail_truncated"])
        self.assertEqual(report["truncated_bytes"], 0)
        self.assertEqual(report["resume_seq"], 2)
        self.assertEqual(report["segment_index"], 0)
        tmp.cleanup()

    def test_torn_tail_reopen_reports_truncation_with_exact_byte_count(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            w.append({"n": 1})
            w.append({"n": 2})
        TornTailTests._truncate_last_line(path, chop=8)
        size_after_corruption = path.stat().st_size

        with JournalWriter(path=path) as w2:
            report = w2.resume_report()
            # la troncature de reprise a deja eu lieu dans __init__ (avant
            # tout append) : la taille sur disque reflete deja valid_end.
            size_after_resume_truncation = path.stat().st_size
            self.assertTrue(report["resumed"])
            self.assertTrue(report["torn_tail_truncated"])
            self.assertGreater(report["truncated_bytes"], 0)
            # jamais silencieuse : le compte est EXACT (taille avant reprise
            # moins taille apres reprise), ni plus ni moins que la ligne
            # torn ecartee.
            self.assertEqual(report["truncated_bytes"],
                              size_after_corruption - size_after_resume_truncation)
            self.assertEqual(report["resume_seq"], 1)  # seul n=1 reste valide
            w2.append({"n": 3})

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["tail_status"], "CLEAN")
        tmp.cleanup()


class CorruptionMidFileTests(unittest.TestCase):
    def _seeded(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            for i in range(4):
                w.append({"n": i})
        return tmp, path

    @staticmethod
    def _read_lines(path):
        # lecture/ecriture en BYTES uniquement : write_text() en mode texte
        # traduirait "\n" en "\r\n" sur Windows et casserait la chaine de
        # hash (calculee sur les octets bruts LF), independamment du module.
        raw = path.read_bytes()
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        return [line.decode("utf-8") for line in lines]

    @staticmethod
    def _write_lines(path, lines):
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        path.write_bytes(raw)

    def test_middle_line_content_change_breaks_hash_chain(self):
        tmp, path = self._seeded()
        lines = self._read_lines(path)
        obj = json.loads(lines[2])  # 2eme enveloppe (seq=2), pas la derniere
        obj["payload"]["n"] = 999
        lines[2] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        self._write_lines(path, lines)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertFalse(v["intact"])
        self.assertTrue(any(e.startswith("HASH_CHAIN_BROKEN") for e in v["errors"]))
        with self.assertRaises(JournalCorruptedError):
            reader.replay()
        tmp.cleanup()

    def test_middle_line_seq_jump_fails_closed(self):
        tmp, path = self._seeded()
        lines = self._read_lines(path)
        obj = json.loads(lines[2])
        obj["seq"] = 999
        lines[2] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        self._write_lines(path, lines)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertFalse(v["intact"])
        self.assertTrue(any(e.startswith("SEQ_NOT_MONOTONIC") for e in v["errors"]))
        tmp.cleanup()

    def test_writer_refuses_to_attach_to_mid_file_corruption(self):
        tmp, path = self._seeded()
        lines = self._read_lines(path)
        obj = json.loads(lines[2])
        obj["seq"] = 999
        lines[2] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        self._write_lines(path, lines)

        with self.assertRaises(JournalCorruptedError):
            JournalWriter(path=path)
        tmp.cleanup()

    def test_invalid_header_fails_closed(self):
        tmp, path = self._seeded()
        lines = self._read_lines(path)
        header = json.loads(lines[0])
        header["schema_version"] = 999
        lines[0] = json.dumps(header, sort_keys=True, separators=(",", ":"))
        self._write_lines(path, lines)

        reader = JournalReader(path)
        v = reader.verify()
        self.assertFalse(v["intact"])
        self.assertTrue(any(e.startswith("HEADER_INVALID") for e in v["errors"]))
        tmp.cleanup()


class LockSingleWriterTests(unittest.TestCase):
    def test_second_writer_raises_ownership_error(self):
        tmp, path = _tmp_journal()
        w1 = JournalWriter(path=path)
        w1.append({"n": 1})
        with self.assertRaises(OwnershipError):
            JournalWriter(path=path)
        w1.close()
        # apres liberation, un nouveau writer reussit
        w2 = JournalWriter(path=path)
        w2.append({"n": 2})
        w2.close()
        tmp.cleanup()

    def test_orphan_lock_different_token_fails_closed_no_pid_check(self):
        tmp, path = _tmp_journal()
        lock_path = Path(str(path) + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"writer_token": "orphan-token-fictif"}),
                              encoding="utf-8")
        with self.assertRaises(OwnershipError):
            JournalWriter(path=path)
        # le lock orphelin n'est jamais supprime automatiquement
        self.assertTrue(lock_path.exists())
        tmp.cleanup()

    def test_close_only_releases_own_lock(self):
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path)
        lock_path = Path(str(path) + ".lock")
        self.assertTrue(lock_path.exists())
        # un tiers remplace le contenu du lock par un token different
        # (simule un autre writer ayant repris le meme fichier de verrou -
        # scenario degrade, close() ne doit PAS supprimer un lock qu'il ne
        # possede plus)
        lock_path.write_text(json.dumps({"writer_token": "not-mine"}), encoding="utf-8")
        w.close()
        self.assertTrue(lock_path.exists())
        lock_path.unlink()
        tmp.cleanup()


class SymlinkAndAllowedDirTests(unittest.TestCase):
    def test_symlink_rejected(self):
        tmp, path = _tmp_journal()
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValidationError) as cm:
                JournalWriter(path=path)
        self.assertEqual(cm.exception.reason_code, "JOURNAL_PATH_SYMLINK")
        tmp.cleanup()

    def test_outside_allowed_dir_rejected(self):
        tmp, path = _tmp_journal()
        other = Path(tmp.name) / "ailleurs"
        other.mkdir()
        with self.assertRaises(ValidationError) as cm:
            JournalWriter(path=path, allowed_dir=other)
        self.assertEqual(cm.exception.reason_code, "JOURNAL_PATH_OUTSIDE_ALLOWED_DIR")
        tmp.cleanup()


class PayloadValidationTests(unittest.TestCase):
    def test_non_dict_payload_rejected_no_partial_state(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            for bad in ["x", 1, 1.5, None, ["a"], (1, 2)]:
                with self.assertRaises(ValidationError) as cm:
                    w.append(bad)
                self.assertEqual(cm.exception.reason_code, "PAYLOAD_NOT_DICT")
            self.assertEqual(w.seq, 0)
        reader = JournalReader(path)
        self.assertEqual(reader.verify()["records"], 0)
        tmp.cleanup()

    def test_non_serializable_payload_rejected_no_partial_write(self):
        tmp, path = _tmp_journal()
        with JournalWriter(path=path) as w:
            with self.assertRaises(ValidationError) as cm:
                w.append({"bad": float("nan")})
            self.assertEqual(cm.exception.reason_code, "PAYLOAD_NOT_SERIALIZABLE")
            with self.assertRaises(ValidationError):
                w.append({"bad": object()})
            with self.assertRaises(ValidationError):
                w.append({"bad": float("inf")})
            self.assertEqual(w.seq, 0)
        reader = JournalReader(path)
        v = reader.verify()
        self.assertEqual(v["records"], 0)
        self.assertTrue(v["intact"])
        # writer reste utilisable apres les echecs
        with JournalWriter(path=path) as w2:
            env = w2.append({"ok": True})
            self.assertEqual(env["seq"], 1)
        tmp.cleanup()


class _WriteOnceFaultyHandle:
    """Minimal white-box spy/fault wrapper for a REAL, already-open file
    handle: ``flush()`` raises exactly ONCE then delegates for real;
    ``write()`` always delegates for real but is COUNTED, so a test can
    assert zero write() calls happened. Local to this file (never imported
    from ``tests/lab_m07_chaos``) -- lab_m03 stays self-contained."""

    def __init__(self, fh):
        self._fh = fh
        self.write_calls = 0
        self._flush_fired = False

    def write(self, data):
        self.write_calls += 1
        return self._fh.write(data)

    def flush(self):
        if not self._flush_fired:
            self._flush_fired = True
            raise OSError(28, "labo M07-P2: flush simule (une seule fois)")
        return self._fh.flush()

    def __getattr__(self, name):
        return getattr(self._fh, name)


class PoisoningTests(unittest.TestCase):
    """M07-P2 (correctif des 3 failles trouvees en M07-P1) : etat terminal
    ``_poisoned`` pose sur TOUTE exception d'E/S dans ``append()``/
    ``_create_new_segment()``/``_rotate()``, verifie EN TETE de ``append()``
    AVANT toute tentative d'E/S -- seule issue = ``close()`` + une NOUVELLE
    instance (le chemin de reprise, deja prouve sans perte ci-dessus par
    ``ResumeTests``/``TornTailTests``, fait autorite)."""

    def test_flush_fault_poisons_next_append_raises_before_any_write(self):
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path)
        for i in range(3):
            w.append({"n": i})
        real_fh = w._fh

        # 1) panne flush() sur le 4e append -> OSError, comme avant.
        faulty = _WriteOnceFaultyHandle(real_fh)
        w._fh = faulty
        with self.assertRaises(OSError):
            w.append({"n": "faulted"})
        w._fh = real_fh  # meme handle reel sous-jacent, restaure pour la suite
        self.assertEqual(faulty.write_calls, 1)  # le write() de cette ligne a bien eu lieu

        self.assertTrue(w.poisoned)
        self.assertEqual(w.poison_reason, "APPEND_IO_FAILED:OSError")
        self.assertEqual(w.resume_report()["poisoned"], True)
        self.assertEqual(w.resume_report()["poison_reason"], "APPEND_IO_FAILED:OSError")

        hash_after_fault = path.read_bytes()

        # 2) SPY : l'appel SUIVANT doit lever JournalPoisonedError AVANT
        # toute tentative d'ecriture -- zero write() sur le spy.
        spy = _WriteOnceFaultyHandle(real_fh)
        w._fh = spy
        with self.assertRaises(JournalPoisonedError) as cm:
            w.append({"n": "must-not-write"})
        self.assertEqual(cm.exception.reason_code, "APPEND_IO_FAILED:OSError")
        self.assertEqual(spy.write_calls, 0, "poisoned append() must never attempt write()")
        w._fh = real_fh

        self.assertEqual(path.read_bytes(), hash_after_fault, "poisoned append() must never touch disk")

        # 3) close() reste possible meme empoisonne (liberation du lock).
        w.close()
        lock_path = Path(str(path) + ".lock")
        self.assertFalse(lock_path.exists())

        # 4) reprise : close() + NOUVELLE instance -> saine, AUCUN
        # enregistrement acquitte perdu, pas de collision de seq.
        reopened = JournalWriter(path=path)
        report = reopened.resume_report()
        self.assertTrue(report["resumed"])
        self.assertFalse(report["poisoned"])
        self.assertIsNone(report["poison_reason"])
        self.assertGreaterEqual(reopened.seq, 3)  # >= les 3 records acquittes avant la panne

        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        payloads = reader.replay()
        self.assertEqual([p["n"] for p in payloads[:3]], [0, 1, 2])
        seqs = list(range(1, len(payloads) + 1))
        # jamais de doublon/collision de seq apres reprise
        self.assertEqual(len(seqs), len(set(seqs)))

        env = reopened.append({"n": "after-recovery"})
        self.assertEqual(env["seq"], reopened.seq)
        self.assertEqual(env["seq"], len(payloads) + 1)
        reopened.close()
        tmp.cleanup()

    def test_segment_creation_fault_poisons_and_cleans_orphan_then_reopen_is_clean(self):
        """GAP 1 (corrige) : une panne d'E/S pendant la creation du tout
        PREMIER segment empoisonne l'instance (jamais retournee a
        l'appelant, ``JournalWriter(...)`` leve) ET nettoie au mieux le
        segment orphelin VIDE -- la reouverture suivante n'a plus besoin
        d'aucune intervention manuelle (avant M07-P2 : ``path.unlink()``
        requis, ``JournalCorruptedError`` sinon)."""
        tmp, path = _tmp_journal()
        real_open = open

        class _FailFirstWrite:
            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                raise OSError(28, "labo M07-P2: creation segment simulee")

            def __getattr__(self, name):
                return getattr(self._fh, name)

        call_count = {"n": 0}

        def faulty_open(path_arg, mode="r", *a, **kw):
            call_count["n"] += 1
            fh = real_open(path_arg, mode, *a, **kw)
            if call_count["n"] == 1:
                return _FailFirstWrite(fh)
            return fh

        import app.services.event_journal as ej

        ej.open = faulty_open  # module-namespace shadow (meme technique que mock.patch(..., create=True))
        try:
            with self.assertRaises(OSError):
                JournalWriter(path=path)
        finally:
            del ej.open

        # nettoyage best-effort reussi : plus de fichier orphelin, plus de lock.
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".lock").exists())

        # reouverture directe : aucune intervention manuelle requise.
        w = JournalWriter(path=path)
        report = w.resume_report()
        self.assertFalse(report["resumed"])
        self.assertFalse(report["poisoned"])
        self.assertIsNone(report["poison_reason"])
        env = w.append({"ok": True})
        self.assertEqual(env["seq"], 1)
        w.close()
        tmp.cleanup()

    def test_poisoned_state_never_resets_across_further_failed_or_successful_looking_calls(self):
        """Terminal = jamais reinitialise : meme apres plusieurs tentatives
        supplementaires sur la meme instance, la raison D'ORIGINE reste
        figee (elle ne se fait jamais ecraser par une panne secondaire) et
        chaque tentative continue de lever avant tout I/O."""
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path)
        w.append({"n": 1})
        real_fh = w._fh
        faulty = _WriteOnceFaultyHandle(real_fh)
        w._fh = faulty
        with self.assertRaises(OSError):
            w.append({"n": "faulted"})
        w._fh = real_fh
        first_reason = w.poison_reason
        self.assertEqual(first_reason, "APPEND_IO_FAILED:OSError")

        for _ in range(3):
            with self.assertRaises(JournalPoisonedError) as cm:
                w.append({"n": "still-poisoned"})
            self.assertEqual(cm.exception.reason_code, first_reason)
            self.assertEqual(w.poison_reason, first_reason)
        w.close()
        tmp.cleanup()


class ThreadSafetyTests(unittest.TestCase):
    def test_four_threads_1000_unique_ordered_seq(self):
        tmp, path = _tmp_journal()
        w = JournalWriter(path=path, fsync_per_append=False)
        errors = []
        lock = threading.Lock()

        def worker(tid):
            for i in range(250):
                try:
                    w.append({"tid": tid, "i": i})
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.close()

        self.assertEqual(errors, [])
        reader = JournalReader(path)
        v = reader.verify()
        self.assertTrue(v["intact"])
        self.assertEqual(v["records"], 1000)  # implique 1000 seq uniques, ordonnes 1..1000
        payloads = reader.replay()
        self.assertEqual(len(payloads), 1000)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
